"""Полное удаление SynoTorrentBridge (запуск: --uninstall).

Убирает: перехват magnet/.torrent (с возвратом прежних ассоциаций),
автозагрузку и сам фоновый монитор, запись в списке установленных
программ, настройки/журналы (по желанию) и сам exe.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

from . import registry_win, theme, winsync
from .config import APP_DIR

log = logging.getLogger("uninstall")


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def schedule_delete(paths: list, wait_pids: list | None = None, attempts: int = 60) -> subprocess.Popen:
    """Удаляет файлы после выхода процессов wait_pids.

    Запущенный exe удалить нельзя, поэтому удаление выполняет отдельный
    скрытый PowerShell: ждёт процессы, затем до attempts раз (раз в секунду)
    пытается удалить каждый файл. `timeout` из cmd здесь не годится — без
    консоли он завершается сразу с ошибкой."""
    pids = ",".join(str(int(p)) for p in (wait_pids or []))
    items = ",".join(_ps_quote(p) for p in paths)
    script = (
        (f"Wait-Process -Id {pids} -Timeout 60 -ErrorAction SilentlyContinue; " if pids else "")
        + f"$left = @({items}); "
        + f"for ($i = 0; $i -lt {int(attempts)} -and $left.Count -gt 0; $i++) {{ "
        + "$left = @($left | Where-Object { "
        + "  try { if (Test-Path -LiteralPath $_) { Remove-Item -LiteralPath $_ -Force -ErrorAction Stop }; $false } "
        + "  catch { $true } }); "
        + "if ($left.Count -gt 0) { Start-Sleep -Seconds 1 } }"
    )
    return subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-Command", script],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _own_pids() -> list:
    """PID этого процесса и, для собранного exe, родительского загрузчика
    PyInstaller (onefile запускает два процесса, оба держат exe)."""
    pids = [os.getpid()]
    if getattr(sys, "frozen", False):
        try:
            pids.append(os.getppid())
        except (AttributeError, OSError):
            pass
    return pids


def run() -> None:
    root = tk.Tk()
    root.withdraw()
    theme.apply_theme(root)

    if not messagebox.askyesno(
            "Удаление SynoTorrentBridge",
            "Будет удалено:\n\n"
            "• перехват magnet-ссылок и .torrent файлов\n"
            "  (прежние ассоциации, если были, вернутся)\n"
            "• фоновый монитор уведомлений и его автозагрузка\n"
            "• запись в списке установленных программ\n"
            "• сам файл программы\n\n"
            "Продолжить?", parent=root):
        root.destroy()
        return

    if winsync.stop_monitor(timeout=15):
        log.info("Фоновый монитор остановлен")

    for what, fn in (("перехват", registry_win.unregister),
                     ("автозагрузка", lambda: registry_win.set_autostart(False)),
                     ("запись в реестре", registry_win.unregister_app)):
        try:
            fn()
        except OSError as e:
            log.warning("Не удалось убрать %s: %s", what, e)

    wipe_settings = messagebox.askyesno(
        "Настройки",
        "Удалить также настройки и журналы?\n"
        f"({APP_DIR})", parent=root)

    messagebox.showinfo("Удаление завершено",
                        "SynoTorrentBridge удалён. Задачи в Download Station "
                        "на NAS не затронуты.", parent=root)
    root.destroy()

    if wipe_settings:
        logging.shutdown()  # отпустить bridge.log, иначе папка не удалится
        shutil.rmtree(APP_DIR, ignore_errors=True)

    exe = sys.executable
    if getattr(sys, "frozen", False):
        # exe заблокирован самим процессом — удалит отдельный процесс после нашего выхода
        from . import updater
        targets = [exe, updater.old_path(exe), updater.temp_target()]
        schedule_delete(targets, wait_pids=_own_pids())
