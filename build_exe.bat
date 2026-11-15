@echo off
cd /d "%~dp0"
if not exist icon.ico python make_icon.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SynoTorrentBridge --icon icon.ico main.py
echo.
echo Готово: dist\SynoTorrentBridge.exe
pause
