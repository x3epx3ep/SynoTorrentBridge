"""Тонкая точка входа для PyInstaller / запуска из исходников."""

from syno_bridge.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
