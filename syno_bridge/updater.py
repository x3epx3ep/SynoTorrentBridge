"""Проверка и установка обновлений с GitHub Releases.

Релиз на GitHub: тег вида ``v1.4.0`` + asset ``SynoTorrentBridge.exe``.
Сравнение версий — по числовому кортежу.

Установка — двухстадийная, без bat-скриптов: старая версия скачивает новый
exe во временную папку и запускает его с ``--apply-update <target> <pid>
<handshake>``. Новый exe (уже нормально запустившийся, со своей временной
распаковкой) сигнализирует рукопожатием, ждёт завершения старого процесса,
копирует себя поверх старого пути и запускает обновлённую копию. Если новый
exe не стартовал (например, антивирус задержал первый запуск свежего файла) —
рукопожатия нет, старая версия остаётся нетронутой и просто сообщает об
ошибке.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from . import __version__, GITHUB_REPO

log = logging.getLogger("updater")

UA = {"User-Agent": f"SynoTorrentBridge/{__version__}",
      "Accept": "application/vnd.github+json"}


def parse_version(s: str) -> tuple:
    """'v1.4.0' -> (1, 4, 0); нечисловые хвосты ('1.4.0-beta') отбрасываются."""
    s = s.strip().lstrip("vV")
    parts = []
    for chunk in s.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check_latest(repo: str = GITHUB_REPO, timeout: int = 15) -> dict | None:
    """Возвращает {'tag', 'url', 'name'} если на GitHub есть версия новее,
    иначе None. Бросает исключение при сетевых ошибках."""
    if not repo:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest", headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    if parse_version(tag) <= parse_version(__version__):
        return None
    asset = next((a for a in data.get("assets", [])
                  if a.get("name", "").lower().endswith(".exe")), None)
    if not asset:
        log.warning("В релизе %s нет exe-файла", tag)
        return None
    return {"tag": tag, "url": asset["browser_download_url"],
            "name": asset["name"]}


def download(url: str, dst: str, timeout: int = 300) -> str:
    """Скачивает файл по url в dst, возвращает dst."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dst, "wb") as f:
        shutil.copyfileobj(resp, f)
    if os.path.getsize(dst) < 100_000:
        raise OSError("Скачанный файл подозрительно мал — вероятно, ошибка загрузки")
    return dst


def temp_target() -> str:
    return os.path.join(tempfile.gettempdir(), "SynoTorrentBridge_update.exe")


# ---- стадия 1: старая версия запускает новый exe ----

def launch_applier(new_exe: str, target: str, handshake: str) -> None:
    """Запускает новый exe в режиме --apply-update.

    target — путь текущего exe (его заменят). handshake — файл-флажок,
    который новый exe создаст, когда его Python реально стартует.
    Работает только в собранном exe (PyInstaller).
    """
    if not getattr(sys, "frozen", False):
        raise OSError("Обновление возможно только в собранном exe")
    log.info("Обновление: %s применит себя поверх %s", new_exe, target)
    subprocess.Popen([new_exe, "--apply-update", target, str(os.getpid()), handshake],
                     close_fds=True)


def wait_handshake(path: str, timeout: float = 30.0) -> bool:
    """Ждёт появления handshake-файла (вызывать из рабочего потока)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.3)
    return False


# ---- стадия 2: новый exe применяет себя (--apply-update) ----

def _pid_alive(pid: int) -> bool:
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def _wait_pid_exit(pid: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.3)


def apply_update(target: str, old_pid: int, handshake: str, launch: bool = True) -> None:
    """Выполняется НОВЫМ exe: ждёт смерти старого, копирует себя поверх target
    и запускает обновлённую копию. handshake создаётся сразу — это сигнал
    старой версии, что новый exe жив и можно выходить."""
    with open(handshake, "w", encoding="ascii") as f:
        f.write("ok")
    log.info("apply-update: жду завершения PID %d", old_pid)
    _wait_pid_exit(old_pid)
    src = sys.executable
    last_err: OSError | None = None
    for _ in range(60):
        try:
            shutil.copyfile(src, target)
            log.info("apply-update: %s заменён", target)
            break
        except OSError as e:  # файл ещё занят старым процессом/антивирусом
            last_err = e
            time.sleep(1)
    else:
        raise OSError(f"Не удалось заменить {target}: {last_err}")
    try:
        os.remove(handshake)
    except OSError:
        pass
    if launch:
        time.sleep(1)  # дать ОС/антивирусу отпустить файл
        log.info("apply-update: запускаю %s", target)
        subprocess.Popen([target], close_fds=True)
