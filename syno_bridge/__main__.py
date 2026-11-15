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
import sys
from pathlib import Path

from .config import APP_DIR, LOG_PATH


def _setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def main(argv: list | None = None) -> int:
    _setup_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    log = logging.getLogger("main")
    log.info("Запуск: %s", args)

    from . import registry_win
    from . import gui

    if not args:
        gui.open_settings()
        return 0

    cmd = args[0]
    if cmd == "--apply-update" and len(args) >= 4:
        # режим второй стадии обновления: нас запустила старая версия
        from . import updater
        updater.apply_update(args[1], int(args[2]), args[3])
        return 0
    if cmd in ("--settings", "-s"):
        gui.open_settings()
        return 0
    if cmd == "--register":
        registry_win.register()
        from . import __version__
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
