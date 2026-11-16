@echo off
cd /d "%~dp0"
if not exist icon.ico python make_icon.py
python -m pip install -q truststore || goto :err
python make_version_file.py || goto :err
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SynoTorrentBridge --icon icon.ico --version-file version_info.txt main.py || goto :err
echo.
echo Готово: dist\SynoTorrentBridge.exe
powershell -NoProfile -Command "(Get-FileHash dist\SynoTorrentBridge.exe -Algorithm SHA256).Hash.ToLower()"
pause
exit /b 0
:err
echo Ошибка сборки
pause
exit /b 1
