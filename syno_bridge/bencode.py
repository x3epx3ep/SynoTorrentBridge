"""Минимальный bencode-парсер и извлечение метаданных .torrent файла.

Только стандартная библиотека. Поддерживает v1 (info.files / info.length)
и базово v2/hybrid (игнорирует pieces layers, берёт файлы из info).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


class BencodeError(ValueError):
    pass


def _decode(data: bytes, pos: int = 0):
    """Возвращает (объект, новая_позиция)."""
    if pos >= len(data):
        raise BencodeError("Unexpected end of data")
    c = data[pos:pos + 1]
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if c == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            item, pos = _decode(data, pos)
            out.append(item)
        return out, pos + 1
    if c == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _decode(data, pos)
            val, pos = _decode(data, pos)
            out[key] = val
        return out, pos + 1
    if c.isdigit():
        colon = data.index(b":", pos)
        n = int(data[pos:colon])
        start = colon + 1
        return data[start:start + n], start + n
    raise BencodeError(f"Invalid token at {pos}: {c!r}")


def loads(data: bytes):
    obj, pos = _decode(data, 0)
    if pos != len(data):
        raise BencodeError("Trailing data")
    return obj


def _encode(obj) -> bytes:
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, (bytes, bytearray)):
        return str(len(obj)).encode() + b":" + bytes(obj)
    if isinstance(obj, str):
        return _encode(obj.encode("utf-8"))
    if isinstance(obj, (list, tuple)):
        return b"l" + b"".join(_encode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0] if isinstance(kv[0], bytes) else str(kv[0]).encode())
        return b"d" + b"".join(_encode(k) + _encode(v) for k, v in items) + b"e"
    raise BencodeError(f"Cannot encode {type(obj)}")


def dumps(obj) -> bytes:
    return _encode(obj)


def _text(value) -> str:
    """Декодирует bytes в строку (utf-8, иначе cp1251/latin-1 как fallback)."""
    if isinstance(value, str):
        return value
    if not isinstance(value, (bytes, bytearray)):
        return str(value)
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return bytes(value).decode(enc)
        except UnicodeDecodeError:
            continue
    return bytes(value).decode("utf-8", errors="replace")


@dataclass
class TorrentFile:
    path: str          # относительный путь внутри раздачи, '/'-разделители
    size: int
    selected: bool = True


@dataclass
class TorrentMeta:
    name: str
    files: list = field(default_factory=list)   # list[TorrentFile]
    total_size: int = 0
    info_hash: str = ""
    is_magnet: bool = False


def _raw_info_slice(data: bytes) -> bytes:
    """Исходные байты словаря info — именно по ним считается info-hash.

    Перекодирование разобранного словаря дало бы другой хеш у торрентов
    с нестандартной кодировкой (несортированные ключи и т.п.)."""
    if data[:1] != b"d":
        return b""
    pos = 1
    while data[pos:pos + 1] != b"e":
        key, pos = _decode(data, pos)
        start = pos
        _val, pos = _decode(data, pos)
        if key == b"info":
            return data[start:pos]
    return b""


def parse_torrent(data: bytes) -> TorrentMeta:
    raw = loads(data)
    if not isinstance(raw, dict) or b"info" not in raw:
        raise BencodeError("Это не похоже на .torrent файл (нет секции info)")
    info = raw[b"info"]
    meta = TorrentMeta(name="", files=[])
    meta.info_hash = hashlib.sha1(_raw_info_slice(data) or _encode(info)).hexdigest()

    name = info.get(b"name.utf-8") or info.get(b"name") or b""
    meta.name = _text(name)

    files = []
    if b"files" in info:  # multi-file
        for f in info[b"files"]:
            parts = f.get(b"path.utf-8") or f.get(b"path") or []
            rel = "/".join(_text(p) for p in parts)
            files.append(TorrentFile(path=rel, size=int(f.get(b"length", 0))))
    else:  # single-file
        files.append(TorrentFile(path=meta.name, size=int(info.get(b"length", 0))))

    meta.files = files
    meta.total_size = sum(f.size for f in files)
    return meta


def magnet_display_name(uri: str) -> str:
    """Извлекает dn= из magnet-ссылки."""
    from urllib.parse import urlparse, parse_qs, unquote
    try:
        q = parse_qs(urlparse(uri).query)
        dn = q.get("dn", [])
        if dn:
            return unquote(dn[0])
    except Exception:
        pass
    return "magnet-ссылка"


def magnet_info_hash(uri: str) -> str:
    """info-hash (40 hex, нижний регистр) из xt=urn:btih:…; base32-форма
    переводится в hex. Пустая строка, если хеша нет."""
    import base64
    import re
    m = re.search(r"xt=urn:btih:([0-9a-zA-Z]+)", uri or "")
    if not m:
        return ""
    h = m.group(1)
    if len(h) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", h):
        return h.lower()
    if len(h) == 32:
        try:
            return base64.b32decode(h.upper()).hex()
        except (ValueError, TypeError):
            return ""
    return ""


def human_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"
