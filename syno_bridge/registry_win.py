"""Регистрация перехвата magnet: и .torrent в реестре Windows (HKCU — без прав администратора).

При включении перехвата прежние значения (команда magnet: другого клиента,
ProgID для .torrent) сохраняются в HKCU\\Software\\SynoTorrentBridge\\Backup,
а при выключении — возвращаются на место. Чужие подключи (например,
.torrent\\OpenWithProgids других программ) не удаляются.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import winreg
from pathlib import Path

log = logging.getLogger("registry")

HKCU = winreg.HKEY_CURRENT_USER
CLASSES = r"Software\Classes"
BACKUP_KEY = r"Software\SynoTorrentBridge\Backup"
PROGID_TORRENT = "SynoTorrentBridge.torrent"
PROGID_MAGNET = "SynoTorrentBridge.magnet"
APP_NAME_EXE = "SynoTorrentBridge.exe"
NONE_MARK = "::none::"   # «значения не было» — при восстановлении удалить


def _from_source() -> bool:
    return sys.executable.lower().endswith(("python.exe", "pythonw.exe"))


def _exe_cmd() -> str:
    exe = sys.executable
    if _from_source():
        # запуск из исходников — указываем модуль
        main = Path(__file__).resolve().parent.parent / "main.py"
        return f'"{exe}" "{main}" "%1"'
    return f'"{exe}" "%1"'


def _set(root, subkey: str, name: str, value: str) -> None:
    key = winreg.CreateKeyEx(root, subkey, 0, winreg.KEY_SET_VALUE)
    with key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _get(root, subkey: str, name: str = "") -> str | None:
    try:
        with winreg.OpenKey(root, subkey) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return str(val)
    except OSError:
        return None


def _del_value(root, subkey: str, name: str) -> None:
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def _delete_tree(root, subkey: str) -> None:
    try:
        winreg.DeleteKey(root, subkey)
    except FileNotFoundError:
        return
    except OSError:
        # есть подключи — удаляем рекурсивно
        try:
            with winreg.OpenKey(root, subkey) as k:
                while True:
                    try:
                        child = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    _delete_tree(k, child)
        except OSError:
            return
        try:
            winreg.DeleteKey(root, subkey)
        except OSError:
            pass


def _key_empty(root, subkey: str) -> bool:
    try:
        with winreg.OpenKey(root, subkey) as k:
            n_sub, n_val, _ = winreg.QueryInfoKey(k)
            if n_sub:
                return False
            if n_val == 0:
                return True
            # единственное пустое значение по умолчанию — тоже «пусто»
            if n_val == 1:
                name, val, _t = winreg.EnumValue(k, 0)
                return name == "" and not val
            return False
    except OSError:
        return False


def _is_ours(value: str | None) -> bool:
    return bool(value) and ("SynoTorrentBridge" in value or "syno_bridge" in value)


# ------------------------------------------------- резервная копия чужих значений

def _backup_once(name: str, value: str | None) -> None:
    """Запоминает прежнее значение, если оно не наше и ещё не сохранено."""
    if _get(HKCU, BACKUP_KEY, name) is not None:
        return
    if _is_ours(value):
        return
    _set(HKCU, BACKUP_KEY, name, NONE_MARK if value is None else value)


def _restore(name: str) -> str | None:
    """Прежнее значение из резервной копии; NONE_MARK — значения не было;
    None — копии нет."""
    return _get(HKCU, BACKUP_KEY, name)


# ------------------------------------------------- перехват

def register() -> None:
    cmd = _exe_cmd()
    magnet_cmd_key = rf"{CLASSES}\magnet\shell\open\command"
    _backup_once("magnet_cmd", _get(HKCU, magnet_cmd_key))
    _backup_once("magnet_default", _get(HKCU, rf"{CLASSES}\magnet"))
    _backup_once("torrent_progid", _get(HKCU, rf"{CLASSES}\.torrent"))

    # magnet: прямая ассоциация протокола (хватает для ShellExecute/браузеров)
    _set(HKCU, rf"{CLASSES}\magnet", "", "URL:Magnet Protocol")
    _set(HKCU, rf"{CLASSES}\magnet", "URL Protocol", "")
    _set(HKCU, magnet_cmd_key, "", cmd)
    # .torrent
    _set(HKCU, rf"{CLASSES}\.torrent", "", PROGID_TORRENT)
    _set(HKCU, rf"{CLASSES}\.torrent\OpenWithProgids", PROGID_TORRENT, "")
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
    if not _from_source():
        appkey = rf"{CLASSES}\Applications\{APP_NAME_EXE}"
        _set(HKCU, appkey, "FriendlyAppName", "SynoTorrentBridge")
        _set(HKCU, rf"{appkey}\shell\open\command", "", cmd)
        cap = rf"{appkey}\Capabilities"
        _set(HKCU, cap, "ApplicationName", "SynoTorrentBridge")
        _set(HKCU, cap, "ApplicationDescription",
             "Отправка magnet-ссылок и .torrent в Synology Download Station")
        _set(HKCU, rf"{cap}\UrlAssociations", "magnet", PROGID_MAGNET)
        _set(HKCU, rf"{cap}\FileAssociations", ".torrent", PROGID_TORRENT)
        _set(HKCU, r"Software\RegisteredApplications", "SynoTorrentBridge", cap)


def unregister() -> None:
    """Снимает перехват и возвращает то, что было до включения."""
    # --- magnet:
    prev_cmd = _restore("magnet_cmd")
    magnet_cmd_key = rf"{CLASSES}\magnet\shell\open\command"
    if prev_cmd is not None and prev_cmd != NONE_MARK:
        _set(HKCU, magnet_cmd_key, "", prev_cmd)
        prev_def = _restore("magnet_default")
        if prev_def is not None and prev_def != NONE_MARK:
            _set(HKCU, rf"{CLASSES}\magnet", "", prev_def)
        log.info("magnet: возвращена прежняя команда")
    elif _is_ours(_get(HKCU, magnet_cmd_key)):
        _delete_tree(HKCU, rf"{CLASSES}\magnet")
    # --- .torrent: только своё значение и своя запись в OpenWithProgids
    _del_value(HKCU, rf"{CLASSES}\.torrent\OpenWithProgids", PROGID_TORRENT)
    if _key_empty(HKCU, rf"{CLASSES}\.torrent\OpenWithProgids"):
        _delete_tree(HKCU, rf"{CLASSES}\.torrent\OpenWithProgids")
    if _get(HKCU, rf"{CLASSES}\.torrent") == PROGID_TORRENT:
        prev = _restore("torrent_progid")
        if prev is not None and prev != NONE_MARK:
            _set(HKCU, rf"{CLASSES}\.torrent", "", prev)
            log.info(".torrent: возвращён прежний ProgID %s", prev)
        else:
            _del_value(HKCU, rf"{CLASSES}\.torrent", "")
    if _key_empty(HKCU, rf"{CLASSES}\.torrent"):
        _delete_tree(HKCU, rf"{CLASSES}\.torrent")
    # --- свои ключи
    _delete_tree(HKCU, rf"{CLASSES}\{PROGID_TORRENT}")
    _delete_tree(HKCU, rf"{CLASSES}\{PROGID_MAGNET}")
    _delete_tree(HKCU, rf"{CLASSES}\Applications\{APP_NAME_EXE}")
    _del_value(HKCU, r"Software\RegisteredApplications", "SynoTorrentBridge")
    _delete_tree(HKCU, BACKUP_KEY)
    parent = BACKUP_KEY.rsplit("\\", 1)[0]
    if _key_empty(HKCU, parent):
        _delete_tree(HKCU, parent)


def registered_command() -> str | None:
    return _get(HKCU, rf"{CLASSES}\magnet\shell\open\command")


def is_registered() -> bool:
    return _is_ours(registered_command())


# ------------------------------------------------- «Установка и удаление программ»

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SynoTorrentBridge"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SynoTorrentBridgeMonitor"


def register_app(version: str) -> None:
    """Запись в список установленных программ (Параметры → Приложения)."""
    if _from_source():
        return  # из исходников не регистрируем
    exe = sys.executable
    values = [
        ("DisplayName", "SynoTorrentBridge"),
        ("DisplayVersion", version),
        ("Publisher", "SynoTorrentBridge"),
        ("DisplayIcon", exe),
        ("InstallLocation", str(Path(exe).parent)),
        ("UninstallString", f'"{exe}" --uninstall'),
        ("URLInfoAbout", "https://github.com/x3epx3ep/SynoTorrentBridge"),
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

def _autostart_cmd() -> str:
    return f'"{sys.executable}" --monitor'


def set_autostart(enabled: bool) -> None:
    """Добавляет/удаляет фоновый монитор уведомлений в автозагрузке (HKCU Run)."""
    try:
        with winreg.OpenKey(HKCU, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                if _from_source():
                    return
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except OSError:
                    pass
    except OSError:
        pass


def autostart_command() -> str | None:
    return _get(HKCU, RUN_KEY, RUN_VALUE)


def is_autostart() -> bool:
    return autostart_command() is not None


# ------------------------------------------------- самовосстановление путей

def _in_temp(path: str) -> bool:
    tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    return os.path.normcase(os.path.abspath(path)).startswith(tmp + os.sep)


def repair_paths(version: str) -> list:
    """Если exe переместили или скачали заново под другим именем, а перехват,
    автозагрузка или запись «Приложения» указывают на старый путь —
    переписывает их на текущий exe. Возвращает список исправленного."""
    fixed: list = []
    if _from_source() or _in_temp(sys.executable):
        return fixed  # из исходников и из временной папки обновления — не трогаем
    try:
        cmd = registered_command()
        if _is_ours(cmd) and cmd != _exe_cmd():
            register()
            fixed.append("перехват magnet/.torrent")
        auto = autostart_command()
        if auto is not None and auto != _autostart_cmd():
            set_autostart(True)
            fixed.append("автозагрузка монитора")
        shown = _get(HKCU, UNINSTALL_KEY, "UninstallString")
        if shown is not None and (shown != f'"{sys.executable}" --uninstall'
                                  or _get(HKCU, UNINSTALL_KEY, "DisplayVersion") != version):
            register_app(version)
            fixed.append("запись в «Приложения»")
    except OSError as e:
        log.warning("Самовосстановление путей: %s", e)
    if fixed:
        log.info("Пути обновлены на %s: %s", sys.executable, ", ".join(fixed))
    return fixed
