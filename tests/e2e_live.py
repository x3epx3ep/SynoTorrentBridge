"""Проверка на НАСТОЯЩЕМ NAS по настройкам приложения (%APPDATA%\\SynoTorrentBridge).

Ничего не скачивает и не меняет: вход, список задач, проверка папок,
«отложенный список» из маленького .torrent создаётся и сразу удаляется
(задача Download Station при этом не появляется).

Запуск: python tests\\e2e_live.py   (нужна настроенная программа и вход без 2FA-кода,
то есть сохранённое доверенное устройство)
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syno_bridge import bencode
from syno_bridge.config import load_config, is_configured
from syno_bridge.gui import make_client
from syno_bridge.syno_api import DS2_FINISHED

FAILED = []


def check(name, cond, extra=""):
    print(("[OK ] " if cond else "[FAIL] ") + name + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


cfg = load_config()
if not is_configured(cfg):
    print("Программа не настроена — пропуск")
    sys.exit(0)

c = make_client(cfg)
c.login()
check("вход (POST, доверенное устройство)", bool(c.sid))
try:
    tasks = c.list_tasks(detail=True)
    check("список задач получен", isinstance(tasks, list), str(type(tasks)))
    statuses = sorted({t.get("status") for t in tasks}, key=str)
    print("   задач:", len(tasks), "статусы:", statuses)
    if tasks:
        t0 = tasks[0]
        check("дубль по точному имени находится",
              c.find_duplicate(title=str(t0.get("title"))) != "")
        check("подстрока имени — не дубль",
              c.find_duplicate(title=str(t0.get("title"))[:-3] + "~~~") == "")
    finished = [t for t in tasks if t.get("status") in DS2_FINISHED]
    print("   завершённых (5/8):", len(finished))

    dest = cfg.get("last_destination") or "Downloads"
    share = dest.split("/")[0]
    check(f"папка /{share} существует", c.folder_exists(share) is True)
    check("несуществующая папка → False",
          c.folder_exists(f"{share}/__stb_no_such_dir_{int(time.time())}__") is False)

    before = c.task_ids()
    payload = os.urandom(32)
    torrent = bencode.dumps({b"info": {b"name": b"stb-e2e-check.bin", b"piece length": 16384,
                                       b"pieces": b"\x00" * 20, b"length": len(payload)}})
    lid = c.create_pending_list_file(torrent, "stb-e2e-check.torrent", share)
    check("отложенный список создан", bool(lid), repr(lid))
    if lid:
        files = c.get_pending_files(lid)
        check("файлы списка получены", len(files) == 1 and files[0]["name"].endswith(".bin"), str(files))
        c.pending_delete(lid)
    after = c.task_ids()
    check("задача в Download Station не появилась", after - before == set(), str(after - before))
finally:
    c.logout()

print()
if FAILED:
    print("ПРОВАЛЕНО:", FAILED)
    sys.exit(1)
print("Живой NAS: всё в порядке ✓")
