"""Живая проверка magnet-потока на НАСТОЯЩЕМ NAS (по настройкам приложения).

Добавляет открытую раздачу Sintel (CC BY, Blender Foundation) в папку
Downloads через настоящее окно добавления, автоматически выбирает два
маленьких файла и следит, чтобы задача не застряла в «Проверке» (статус 6).
В конце задача удаляется. Качается не больше пары сотен КБ.

Запуск: python tests\\e2e_magnet_live.py [папка]
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import syno_bridge.gui as g
from syno_bridge.bencode import TorrentMeta, magnet_info_hash
from syno_bridge.config import load_config

MAGNET = ("magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel"
          "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337&tr=udp%3A%2F%2Fexplodie.org%3A6969"
          "&tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce"
          "&tr=udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce&ws=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2F")
PICK = {"Sintel.de.srt", "Sintel.fr.srt"}
DEST = sys.argv[1] if len(sys.argv) > 1 else "Downloads"
FAILED = []


def check(name, cond, extra=""):
    print(("[OK ] " if cond else "[FAIL] ") + name + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def remove_test_dir(cl, path):
    """Удаляет папку, созданную тестом (Download Station не удаляет файлы
    завершённой задачи вместе с ней)."""
    try:
        r = cl._call("SYNO.FileStation.Delete", 2, "start",
                     extra={"path": json.dumps(["/" + path], ensure_ascii=False), "recursive": "true"})
        for _ in range(30):
            st = cl._call("SYNO.FileStation.Delete", 2, "status", extra={"taskid": r.get("taskid")})
            if st.get("finished"):
                break
            time.sleep(0.5)
        print(f"   тестовая папка /{path} удалена:", cl.folder_exists(path) is False)
    except Exception as e:  # noqa: BLE001
        print(f"   не удалось удалить /{path}: {e}")


msgs = []
g.messagebox.showwarning = lambda *a, **k: msgs.append(("warn",) + a)
g.messagebox.showinfo = lambda *a, **k: msgs.append(("info",) + a)
g.messagebox.showerror = lambda *a, **k: msgs.append(("error",) + a)
g.messagebox.askyesno = lambda *a, **k: msgs.append(("ask",) + a) or True
g.save_config = lambda cfg: None

client = g.make_client(load_config())
client.login()
TEST_DIR = f"{DEST}/Sintel"
if client.folder_exists(TEST_DIR):
    print(f"Папка /{TEST_DIR} уже есть — тест её не трогает. Удалите или выберите другую папку.")
    sys.exit(2)
seen = {}
orig = g.MagnetFilesDialog


class AutoDialog(orig):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        seen["task_id"] = self.task_id
        seen["keep_id"] = self.keep_id
        seen["files"] = [f.path for f in self.meta.files]
        st = client.task_status(self.task_id)
        seen["status_during_dialog"] = st
        for f in self.meta.files:
            f.selected = f.path.rsplit("/", 1)[-1] in PICK
        self.win.after(1500, self._ok)


g.MagnetFilesDialog = AutoDialog
root = g.setup_root()
meta = TorrentMeta(name="Sintel", is_magnet=True, info_hash=magnet_info_hash(MAGNET))
dlg = g.AddTorrentDialog(root, client, meta, magnet_uri=MAGNET)
dlg.v_dest.set(DEST)
root.after(300, dlg._submit)
root.after(300_000, root.quit)
t0 = time.time()
root.mainloop()
print(f"окно отработало за {time.time() - t0:.0f} с")

c = g.make_client(load_config())
c.login()
tid = seen.get("task_id")
try:
    check("метаданные получены, окно выбора открылось", bool(tid), str(msgs))
    if tid:
        print("   файлов:", len(seen["files"]), "временный keep_id:", seen["keep_id"])
        check("во время выбора задача не в «Проверке»", seen["status_during_dialog"][0] != 6,
              str(seen["status_during_dialog"]))
        history = []
        for _ in range(20):
            history.append(c.task_status(tid))
            if history[-1][0] in (5, 8):
                break
            time.sleep(2)
        print("   статусы после выбора:", [h[0] for h in history])
        check("после выбора задача не застряла в «Проверке»",
              history[-1][0] != 6, str(history[-3:]))
        check("задача докачала выбранное без ошибок", history[-1][0] in (5, 8, 13),
              str(history[-3:]))
        try:
            items = c._call("SYNO.DownloadStation2.Task.BT.File", 2, "list",
                            extra={"task_id": tid, "limit": -1}).get("items", [])
        except Exception:  # noqa: BLE001 — у завершённой задачи список файлов недоступен
            items = []
        wanted = {i["name"].rsplit("/", 1)[-1] for i in items if i.get("wanted")}
        if items:
            check("включены ровно выбранные файлы", wanted == PICK, str(wanted))
        else:
            print("   (список файлов недоступен — задача уже завершена)")
    check("сообщение «Готово» показано", any(m[0] == "info" and m[1] == "Готово" for m in msgs), str(msgs))
    check("без ошибок", not any(m[0] == "error" for m in msgs), str(msgs))
finally:
    if tid:
        c.delete_task(tid)
        print("   тестовая задача удалена")
    remove_test_dir(c, TEST_DIR)
    c.logout()

print()
if FAILED:
    print("ПРОВАЛЕНО:", FAILED)
    sys.exit(1)
print("magnet на живом NAS: всё в порядке ✓")
