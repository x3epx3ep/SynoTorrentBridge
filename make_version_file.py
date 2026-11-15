"""Создаёт version_info.txt для PyInstaller (--version-file) из syno_bridge.__version__,
чтобы в свойствах exe (Проводник → Подробно) была видна версия."""

from pathlib import Path

from syno_bridge import __version__

nums = [int(x) for x in __version__.split(".")[:3]] + [0]
nums = (nums + [0, 0, 0, 0])[:4]
tup = ", ".join(str(n) for n in nums)

TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'SynoTorrentBridge'),
      StringStruct('FileDescription', 'SynoTorrentBridge — magnet/.torrent в Synology Download Station'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'SynoTorrentBridge'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'SynoTorrentBridge.exe'),
      StringStruct('ProductName', 'SynoTorrentBridge'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"""

out = Path(__file__).resolve().parent / "version_info.txt"
out.write_text(TEMPLATE, encoding="utf-8")
print("version_info.txt:", __version__)
