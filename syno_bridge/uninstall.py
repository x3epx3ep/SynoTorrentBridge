"""Полное удаление SynoTorrentBridge (запуск: --uninstall).

Убирает: перехват magnet/.torrent, автозагрузку монитора, запись в списке
установленных программ, настройки/журналы (по желанию) и сам exe.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

from . import registry_win, theme
from .config import APP_DIR

log = logging.getLogger("uninstall")


def run() -> None:
    root = tk.Tk()
    root.withdraw()
    theme.apply_theme(root)

    if not messagebox.askyesno(
            "Удаление SynoTorrentBridge",
            "Будет удалено:\n\n"
            "• перехват magnet-ссылок и .torrent файлов\n"
            "• фоновый монитор уведомлений (автозагрузка)\n"
            "• запись в списке установленных программ\n"
            "• сам файл программы\n\n"
            "Продолжить?", parent=root):
        root.destroy()
        return

    for what, fn in (("перехват", registry_win.unregister),
                     ("автозагрузка", lambda: registry_win.set_autostart(False)),
                     ("запись в реестре", registry_win.unregister_app)):
        try:
            fn()
        except OSError as e:
            log.warning("Не удалось убрать %s: %s", what, e)

    if messagebox.askyesno(
            "Настройки",
            "Удалить также настройки и журналы?\n"
            f"({APP_DIR})", parent=root):
        shutil.rmtree(APP_DIR, ignore_errors=True)

    exe = sys.executable
    if not exe.lower().endswith(("python.exe", "pythonw.exe")):
        # exe заблокирован самим процессом — удаляем через отложенную команду
        subprocess.Popen(
            ["cmd", "/c", "timeout", "/t", "2", "/nobreak", "&",
             "del", "/f", "/q", exe],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    messagebox.showinfo("Удаление завершено",
                        "SynoTorrentBridge удалён. Задачи в Download Station "
                        "на NAS не затронуты.", parent=root)
    root.destroy()
