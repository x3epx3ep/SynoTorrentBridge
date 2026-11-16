"""Проверка и установка обновлений с GitHub Releases.

Релиз на GitHub: тег вида ``v1.4.0`` + asset ``SynoTorrentBridge.exe``.
Сравнение версий — по числовому кортежу.

Скачанный файл сверяется с контрольной суммой SHA-256, которую GitHub
публикует для каждого файла релиза (поле ``digest``), и с размером.

Установка — двухстадийная, без bat-скриптов: старая версия скачивает новый
exe во временную папку и запускает его с ``--apply-update <target> <pid>
<handshake>``. Новый exe (уже нормально запустившийся, со своей временной
распаковкой) сигнализирует рукопожатием, останавливает фоновый монитор,
ждёт завершения старого процесса, переименовывает старый exe в ``*.old``
(Windows разрешает переименовать даже запущенный файл), копирует себя на
его место и запускает обновлённую копию (и монитор, если он работал).
``*.old`` и временный файл удаляются при следующем запуске. Если новый exe
не стартовал (например, антивирус задержал первый запуск свежего файла) —
рукопожатия нет, старая версия остаётся нетронутой и просто сообщает об
ошибке.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from . import __version__, GITHUB_REPO, tls, winsync

log = logging.getLogger("updater")

EXE_NAME = "SynoTorrentBridge.exe"

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
    """Возвращает {'tag', 'url', 'name', 'sha256', 'size'} если на GitHub есть
    версия новее, иначе None. Бросает исключение при сетевых ошибках."""
    if not repo:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest", headers=UA)
    with tls.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    if parse_version(tag) <= parse_version(__version__):
        return None
    assets = [a for a in data.get("assets", [])
              if a.get("name", "").lower().endswith(".exe")]
    # предпочитаем файл с «родным» именем, иначе — первый exe
    asset = next((a for a in assets if a.get("name", "").lower() == EXE_NAME.lower()),
                 assets[0] if assets else None)
    if not asset:
        log.warning("В релизе %s нет exe-файла", tag)
        return None
    digest = str(asset.get("digest") or "")
    sha256 = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
    return {"tag": tag, "url": asset["browser_download_url"],
            "name": asset["name"], "sha256": sha256,
            "size": int(asset.get("size") or 0)}


def download(url: str, dst: str, timeout: int = 300, sha256: str = "",
             size: int = 0) -> str:
    """Скачивает файл по url в dst и проверяет его. Возвращает dst.

    sha256 — ожидаемая контрольная сумма (hex); при несовпадении файл
    удаляется и бросается OSError — такой exe никогда не запускается."""
    req = urllib.request.Request(url, headers=UA)
    h = hashlib.sha256()
    with tls.urlopen(req, timeout=timeout) as resp, open(dst, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
    try:
        got = os.path.getsize(dst)
        if got < 100_000:
            raise OSError("Скачанный файл подозрительно мал — вероятно, ошибка загрузки")
        if size and got != size:
            raise OSError(f"Размер скачанного файла ({got}) не совпадает с релизом ({size})")
        if sha256:
            if h.hexdigest() != sha256.lower():
                raise OSError("Контрольная сумма скачанного файла не совпадает с релизом "
                              "на GitHub — обновление отменено")
            log.info("Обновление: SHA-256 совпадает (%s…)", sha256[:12])
        else:
            log.warning("Обновление: в релизе нет контрольной суммы — проверен только размер")
    except OSError:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise
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


def old_path(target: str) -> str:
    return target + ".old"


def replace_exe(src: str, target: str, attempts: int = 30) -> None:
    """Ставит src на место target, даже если target сейчас запущен.

    Запущенный exe нельзя перезаписать, но можно переименовать: target
    уходит в target.old, на его место копируется src. При неудаче копирования
    target.old возвращается обратно."""
    old = old_path(target)
    last_err: OSError | None = None
    for _ in range(attempts):
        try:
            if os.path.exists(old):
                try:
                    os.remove(old)
                except OSError:
                    # прошлый .old ещё занят — отодвигаем под уникальным именем
                    os.replace(old, f"{old}.{int(time.time() * 1000)}")
            moved = False
            if os.path.exists(target):
                os.replace(target, old)
                moved = True
            try:
                shutil.copyfile(src, target)
            except OSError:
                if moved and not os.path.exists(target):
                    os.replace(old, target)
                raise
            return
        except OSError as e:  # антивирус держит файл — повторим
            last_err = e
            time.sleep(1)
    raise OSError(f"Не удалось заменить {target}: {last_err}")


def cleanup_leftovers(exe: str | None = None) -> None:
    """Удаляет хвосты прошлых обновлений: exe.old(.*) рядом с программой и
    временный exe обновления. Занятые файлы молча пропускаются."""
    exe = exe or sys.executable
    folder, base = os.path.split(exe)
    candidates = [temp_target()]
    try:
        candidates += [os.path.join(folder, n) for n in os.listdir(folder)
                       if n.lower().startswith(base.lower() + ".old")]
    except OSError:
        pass
    me = os.path.normcase(os.path.abspath(exe))
    for path in candidates:
        if os.path.normcase(os.path.abspath(path)) == me:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("Удалён хвост обновления: %s", path)
        except OSError:
            pass


def apply_update(target: str, old_pid: int, handshake: str, launch: bool = True) -> None:
    """Выполняется НОВЫМ exe: останавливает монитор, ждёт смерти старого
    процесса, ставит себя на место target и запускает обновлённую копию.
    handshake создаётся сразу — это сигнал старой версии, что новый exe жив
    и можно выходить."""
    with open(handshake, "w", encoding="ascii") as f:
        f.write("ok")
    monitor_was_running = winsync.stop_monitor(timeout=15)
    if monitor_was_running:
        log.info("apply-update: фоновый монитор остановлен")
    log.info("apply-update: жду завершения PID %d", old_pid)
    _wait_pid_exit(old_pid)
    replace_exe(sys.executable, target)
    log.info("apply-update: %s заменён", target)
    try:
        os.remove(handshake)
    except OSError:
        pass
    if launch:
        time.sleep(1)  # дать ОС/антивирусу отпустить файл
        log.info("apply-update: запускаю %s", target)
        subprocess.Popen([target], close_fds=True)
        if monitor_was_running:
            subprocess.Popen([target, "--monitor"], close_fds=True)
