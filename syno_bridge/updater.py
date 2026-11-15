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
    а отложенный bat-скрипт ждёт, подменяет файл и запускает новую копию.
    Работает только в собранном exe (PyInstaller).

    Команда пишется в .bat, а не передаётся через cmd /c: subprocess
    экранирует вложенные кавычки бэкслешами (\\"), а cmd их не снимает —
    пути ломаются. chcp 65001 + UTF-8 — чтобы cmd правильно прочитал
    кириллицу в путях.
    """
    current = sys.executable
    if not getattr(sys, "frozen", False):
        raise OSError("Обновление возможно только в собранном exe")
    bat = os.path.join(tempfile.gettempdir(), "syno_update.bat")
    ps_sleep = 'powershell -NoProfile -Command "Start-Sleep -Milliseconds {ms}" >nul 2>&1'
    with open(bat, "w", encoding="utf-8") as f:
        f.write("@echo off\n"
                "chcp 65001 >nul\n"
                # timeout/ping без консоли завершаются мгновенно — ждём через PowerShell
                + ps_sleep.format(ms=1500) + "\n"
                "set /a tries=0\n"
                ":retry\n"
                f'move /y "{new_exe}" "{current}" >nul 2>&1\n'
                "if %errorlevel%==0 goto moved\n"
                "set /a tries+=1\n"
                "if %tries% geq 20 goto end\n"
                + ps_sleep.format(ms=700) + "\n"
                "goto retry\n"
                ":moved\n"
                # даём антивирусу дочекать новый файл перед запуском
                + ps_sleep.format(ms=500) + "\n"
                f'start "" "{current}"\n'
                ":end\n"
                'del "%~f0"\n')
    log.info("Обновление: %s -> %s", new_exe, current)
    # строковая форма — subprocess НЕ экранирует кавычки
    subprocess.Popen(f'cmd /c ""{bat}""', creationflags=subprocess.CREATE_NO_WINDOW,
                     close_fds=True)


def temp_target() -> str:
    return os.path.join(tempfile.gettempdir(), "SynoTorrentBridge_update.exe")
