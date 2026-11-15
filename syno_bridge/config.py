"""Конфиг приложения: %APPDATA%\\SynoTorrentBridge\\config.json.

Пароль хранится зашифрованным через Windows DPAPI (CryptProtectData) —
расшифровать его может только этот пользователь на этом компьютере.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "SynoTorrentBridge"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "bridge.log"

_DEFAULTS = {
    "host": "",            # например 192.168.1.10 или nas.local
    "port": 5000,
    "https": False,
    "verify_ssl": True,
    "username": "",
    "password_enc": "",    # base64(DPAPI)
    "device_id": "",       # device token Synology — вход без кода 2FA
    "last_destination": "",
    "recent_destinations": [],
    "download_all_default": True,
}


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    blob_in = _DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    func = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encrypt_password(plain: str) -> str:
    if not plain:
        return ""
    return base64.b64encode(_dpapi(plain.encode("utf-8"), True)).decode("ascii")


def decrypt_password(enc: str) -> str:
    if not enc:
        return ""
    try:
        return _dpapi(base64.b64decode(enc), False).decode("utf-8")
    except Exception:
        return ""


def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("host") and cfg.get("username"))


def base_url(cfg: dict) -> str:
    scheme = "https" if cfg.get("https") else "http"
    return f"{scheme}://{cfg['host']}:{int(cfg.get('port', 5001 if cfg.get('https') else 5000))}"


def remember_destination(cfg: dict, dest: str) -> None:
    if not dest:
        return
    cfg["last_destination"] = dest
    recent = [dest] + [d for d in cfg.get("recent_destinations", []) if d != dest]
    cfg["recent_destinations"] = recent[:10]
