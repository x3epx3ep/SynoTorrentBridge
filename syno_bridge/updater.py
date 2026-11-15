"""Проверка и установка обновлений с GitHub Releases.

Релиз на GitHub: тег вида ``v1.4.0`` + asset ``SynoTorrentBridge.exe``.
Сравнение версий — по числовому кортежу. Замена exe — через отложенный
cmd-скрипт (запущенный exe заблокирован, поэтому сначала завершаем процесс,
а cmd ждёт 2 секунды, подменяет файл и запускает новую версию).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
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


def install_and_restart(new_exe: str) -> None:
    """Подменяет текущий exe на new_exe и перезапускает приложение.

    Должно вызываться ПОСЛЕДНИМ действием перед выходом: процесс завершается,
    а отложенный cmd ждёт, подменяет файл и запускает новую копию.
    Работает только в собранном exe (PyInstaller).
    """
    current = sys.executable
    if not getattr(sys, "frozen", False):
        raise OSError("Обновление возможно только в собранном exe")
    # start с пустым заголовком окна: start "" "path"
    cmd = (f'timeout /t 2 /nobreak >nul & '
           f'move /y "{new_exe}" "{current}" >nul & '
           f'start "" "{current}"')
    log.info("Обновление: %s", cmd)
    subprocess.Popen(["cmd", "/c", cmd], creationflags=subprocess.CREATE_NO_WINDOW,
                     close_fds=True)


def temp_target() -> str:
    return os.path.join(tempfile.gettempdir(), "SynoTorrentBridge_update.exe")
