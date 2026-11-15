"""Регистрация перехвата magnet: и .torrent в реестре Windows (HKCU — без прав администратора)."""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

HKCU = winreg.HKEY_CURRENT_USER
CLASSES = r"Software\Classes"
PROGID_TORRENT = "SynoTorrentBridge.torrent"
PROGID_MAGNET = "SynoTorrentBridge.magnet"
APP_NAME_EXE = "SynoTorrentBridge.exe"


def _exe_cmd() -> str:
    exe = sys.executable
    if exe.lower().endswith(("python.exe", "pythonw.exe")):
        # запуск из исходников — указываем модуль
        main = Path(__file__).resolve().parent.parent / "main.py"
        return f'"{exe}" "{main}" "%1"'
    return f'"{exe}" "%1"'


def _set(root, subkey: str, name: str, value: str) -> None:
    key = winreg.CreateKeyEx(root, subkey, 0, winreg.KEY_SET_VALUE)
    with key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _delete_tree(root, subkey: str) -> None:
    try:
        winreg.DeleteKey(root, subkey)
    except FileNotFoundError:
        return
    except OSError:
        # есть подключи — удаляем рекурсивно
        with winreg.OpenKey(root, subkey) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(k, child)
        try:
            winreg.DeleteKey(root, subkey)
        except OSError:
            pass


def register() -> None:
    cmd = _exe_cmd()
    # magnet: прямая ассоциация протокола (хватает для ShellExecute/браузеров)
    _set(HKCU, rf"{CLASSES}\magnet", "", "URL:Magnet Protocol")
    _set(HKCU, rf"{CLASSES}\magnet", "URL Protocol", "")
    _set(HKCU, rf"{CLASSES}\magnet\shell\open\command", "", cmd)
    # .torrent
    _set(HKCU, rf"{CLASSES}\.torrent", "", PROGID_TORRENT)
    _set(HKCU, rf"{CLASSES}\{PROGID_TORRENT}", "", "Torrent-файл (SynoTorrentBridge)")
    _set(HKCU, rf"{CLASSES}\{PROGID_TORRENT}", "FriendlyAppName", "SynoTorrentBridge")
    _set(HKCU, rf"{CLASSES}\{PROGID_TORRENT}\shell\open\command", "", cmd)
    # собственный ProgID для magnet — чтобы приложение видели списки выбора
    _set(HKCU, rf"{CLASSES}\{PROGID_MAGNET}", "", "Magnet-ссылка (SynoTorrentBridge)")
    _set(HKCU, rf"{CLASSES}\{PROGID_MAGNET}", "URL Protocol", "")
    _set(HKCU, rf"{CLASSES}\{PROGID_MAGNET}", "FriendlyAppName", "SynoTorrentBridge")
    _set(HKCU, rf"{CLASSES}\{PROGID_MAGNET}\shell\open\command", "", cmd)
    # полноценная регистрация приложения: Applications\<exe> + Capabilities +
    # RegisteredApplications — без этого Firefox и «Открыть с помощью» не показывают
    # программу в списке (работаем только для exe, не для запуска из исходников)
    if not sys.executable.lower().endswith(("python.exe", "pythonw.exe")):
        appkey = rf"{CLASSES}\Applications\{APP_NAME_EXE}"
        _set(HKCU, appkey, "FriendlyAppName", "SynoTorrentBridge")
        _set(HKCU, rf"{appkey}\shell\open\command", "", cmd)
        cap = rf"{appkey}\Capabilities"
        _set(HKCU, cap, "ApplicationName", "SynoTorrentBridge")
        _set(HKCU, cap, "ApplicationDescription",
             "Отправка magnet-ссылок и .torrent в Synology Download Station")
        _set(HKCU, rf"{cap}\UrlAssociations", "magnet", PROGID_MAGNET)
        _set(HKCU, rf"{cap}\FileAssociations", ".torrent", PROGID_TORRENT)
        key = winreg.CreateKeyEx(HKCU, r"Software\RegisteredApplications",
                                 0, winreg.KEY_SET_VALUE)
        with key:
            winreg.SetValueEx(key, "SynoTorrentBridge", 0, winreg.REG_SZ, cap)


def unregister() -> None:
    _delete_tree(HKCU, rf"{CLASSES}\magnet")
    _delete_tree(HKCU, rf"{CLASSES}\.torrent")
    _delete_tree(HKCU, rf"{CLASSES}\{PROGID_TORRENT}")
    _delete_tree(HKCU, rf"{CLASSES}\{PROGID_MAGNET}")
    _delete_tree(HKCU, rf"{CLASSES}\Applications\{APP_NAME_EXE}")
    try:
        with winreg.OpenKey(HKCU, r"Software\RegisteredApplications", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "SynoTorrentBridge")
    except OSError:
        pass


def is_registered() -> bool:
    try:
        with winreg.OpenKey(HKCU, rf"{CLASSES}\magnet\shell\open\command") as k:
            val, _ = winreg.QueryValueEx(k, "")
            return "SynoTorrentBridge" in val or "syno_bridge" in val or "main.py" in val
    except OSError:
        return False


# ------------------------------------------------- «Установка и удаление программ»

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SynoTorrentBridge"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SynoTorrentBridgeMonitor"


def register_app(version: str) -> None:
    """Запись в список установленных программ (Параметры → Приложения)."""
    if sys.executable.lower().endswith(("python.exe", "pythonw.exe")):
        return  # из исходников не регистрируем
    exe = sys.executable
    values = [
        ("DisplayName", "SynoTorrentBridge"),
        ("DisplayVersion", version),
        ("Publisher", "SynoTorrentBridge"),
        ("DisplayIcon", exe),
        ("InstallLocation", str(Path(exe).parent)),
        ("UninstallString", f'"{exe}" --uninstall'),
    ]
    key = winreg.CreateKeyEx(HKCU, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE)
    with key:
        for name, value in values:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def unregister_app() -> None:
    _delete_tree(HKCU, UNINSTALL_KEY)


# ------------------------------------------------- автозагрузка монитора

def set_autostart(enabled: bool) -> None:
    """Добавляет/удаляет фоновый монитор уведомлений в автозагрузке (HKCU Run)."""
    try:
        with winreg.OpenKey(HKCU, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                exe = sys.executable
                if exe.lower().endswith(("python.exe", "pythonw.exe")):
                    return
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}" --monitor')
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except OSError:
                    pass
    except OSError:
        pass


def is_autostart() -> bool:
    try:
        with winreg.OpenKey(HKCU, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False
