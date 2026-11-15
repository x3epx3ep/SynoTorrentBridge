"""Точка входа SynoTorrentBridge.

Использование:
  SynoTorrentBridge.exe  "magnet:?xt=..."   — добавить magnet
  SynoTorrentBridge.exe  "C:\\...\\file.torrent" — добавить торрент-файл
  SynoTorrentBridge.exe  --settings          — окно настроек
  SynoTorrentBridge.exe  --register          — включить перехват magnet/.torrent
  SynoTorrentBridge.exe  --unregister        — выключить перехват
  SynoTorrentBridge.exe  --monitor           — фоновый монитор уведомлений (автозагрузка)
  SynoTorrentBridge.exe  --uninstall         — удалить программу и все её следы
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .config import APP_DIR, LOG_PATH

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3


def _rotate_log() -> None:
    """Ротация при старте: bridge.log → bridge.log.1 → … → .3.

    Делается до открытия файла, а не RotatingFileHandler'ом: журнал пишут
    сразу несколько процессов (окно + монитор), и переименование открытого
    файла в Windows не удалось бы."""
    try:
        if not LOG_PATH.exists() or LOG_PATH.stat().st_size < LOG_MAX_BYTES:
            return
        for i in range(LOG_BACKUPS, 0, -1):
            src = LOG_PATH.with_name(f"{LOG_PATH.name}.{i - 1}") if i > 1 else LOG_PATH
            dst = LOG_PATH.with_name(f"{LOG_PATH.name}.{i}")
            if src.exists():
                os.replace(src, dst)
    except OSError:
        pass  # файл занят другим процессом — повернём в следующий раз


def _setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _rotate_log()
    handlers: list = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if sys.stderr is not None:  # в собранном exe без консоли stderr нет
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _housekeeping(repair: bool) -> None:
    """Уборка после обновления и починка путей, если exe переместили."""
    if not getattr(sys, "frozen", False):
        return
    from . import __version__, registry_win, updater
    updater.cleanup_leftovers()
    # SYNO_BRIDGE_NO_REPAIR=1 — для проверок копий exe, чтобы не перетянуть
    # на тестовую копию перехват и автозагрузку настоящей установки
    if repair and not os.environ.get("SYNO_BRIDGE_NO_REPAIR"):
        registry_win.repair_paths(__version__)


def main(argv: list | None = None) -> int:
    _setup_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    log = logging.getLogger("main")
    from . import __version__
    log.info("Запуск %s: %s", __version__, args)

    cmd = args[0] if args else ""
    if cmd == "--apply-update" and len(args) >= 4:
        # режим второй стадии обновления: нас запустила старая версия
        from . import updater
        try:
            updater.apply_update(args[1], int(args[2]), args[3])
        except Exception as e:  # noqa: BLE001
            log.exception("Обновление не применено")
            try:
                import tkinter as tk
                from tkinter import messagebox
                r = tk.Tk()
                r.withdraw()
                messagebox.showerror("Обновление SynoTorrentBridge",
                                     f"Не удалось установить обновление:\n{e}\n\n"
                                     f"Прежняя версия осталась на месте: {args[1]}")
                r.destroy()
            except Exception:  # noqa: BLE001
                pass
            return 1
        return 0

    if cmd not in ("--uninstall", "--unregister"):
        try:
            # монитор пути не чинит: его запускает автозагрузка, и он не должен
            # перетягивать перехват на себя, если копий программы несколько
            _housekeeping(repair=cmd != "--monitor")
        except Exception:  # noqa: BLE001
            log.exception("Уборка при старте")

    from . import registry_win
    from . import gui

    if not args:
        gui.open_settings()
        return 0

    if cmd in ("--settings", "-s"):
        gui.open_settings()
        return 0
    if cmd == "--register":
        registry_win.register()
        registry_win.register_app(__version__)
        print("Перехват magnet: и .torrent включён.")
        return 0
    if cmd == "--unregister":
        registry_win.unregister()
        print("Перехват выключен.")
        return 0
    if cmd == "--monitor":
        from . import monitor
        monitor.run()
        return 0
    if cmd == "--uninstall":
        from . import uninstall
        uninstall.run()
        return 0

    source = cmd.strip().strip('"')
    if source.lower().startswith("magnet:"):
        gui.run_add(source)
        return 0
    if Path(source).is_file():
        gui.run_add(str(Path(source).resolve()))
        return 0

    print(f"Не понимаю аргумент: {source}\nОжидается magnet-ссылка, путь к .torrent или --settings.")
    gui.open_settings()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
