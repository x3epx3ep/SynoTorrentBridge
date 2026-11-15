"""Сценарий окна добавления .torrent против mock-NAS (без участия человека).

1. Снять все галочки → «Скачать» → предупреждение, на NAS ничего не уходит.
2. Отметить один файл, указать несуществующую папку → предложение создать →
   папка создаётся → задача создаётся с выбором на стороне NAS (selected).
Диалоги messagebox подменены, реальные настройки не трогаются.
"""

import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_all import MockNAS, default_tasks
from syno_bridge import bencode
from syno_bridge.syno_api import SynoClient
import syno_bridge.gui as g

asked = []
g.messagebox.showwarning = lambda title, msg, **k: asked.append(("warn", title))
g.messagebox.showinfo = lambda title, msg, **k: asked.append(("info", title))
g.messagebox.showerror = lambda title, msg, **k: asked.append(("error", title, msg))
g.messagebox.askyesno = lambda title, msg, **k: asked.append(("ask", title)) or True
g.save_config = lambda cfg: None
g.load_config = lambda: {"last_destination": "", "recent_destinations": []}

MockNAS.tasks = default_tasks()
MockNAS.captured.clear()
server = ThreadingHTTPServer(("127.0.0.1", 0), MockNAS)
threading.Thread(target=server.serve_forever, daemon=True).start()
client = SynoClient("127.0.0.1", server.server_address[1], use_https=False,
                    username="user", password="pass")
client.login()

torrent = {b"info": {b"name": "Test Razdacha 2024", b"piece length": 262144, b"pieces": b"x" * 40,
                     b"files": [{b"length": 1234567, b"path": [b"Film", b"film.mkv"]},
                                {b"length": 5000, b"path": [b"Subs", "Русские субтитры.srt".encode()]},
                                {b"length": 100, b"path": [b"readme.txt"]}]}}
data = bencode.dumps(torrent)
meta = bencode.parse_torrent(data)

root = g.setup_root()
dlg = g.AddTorrentDialog(root, client, meta, torrent_bytes=data, torrent_filename="t.torrent")
results = {}


def step1():
    dlg.panel.set_visible(False)
    dlg._submit()
    results["empty_blocked"] = asked == [("warn", "Ничего не выбрано")] and "multipart" not in MockNAS.captured
    # шаг 2: один файл, несуществующая папка
    for f in meta.files:
        f.selected = f.path.endswith("film.mkv")
    dlg.v_dest.set("video/новая/папка")
    dlg._submit()


def watchdog():
    print("TIMEOUT")
    root.quit()


root.after(300, step1)
root.after(20000, watchdog)
root.mainloop()

cf = MockNAS.captured.get("create_folder", {})
pd = MockNAS.captured.get("list_polling_download", {})
checks = {
    "пустой выбор не отправляется": results.get("empty_blocked"),
    "спросили про создание папки": ("ask", "Папки нет на NAS") in asked,
    "папка создана с родителями": cf.get("folder_path") == ["/video/новая"]
                                  and cf.get("force_parent") == ["true"],
    "задача создана с выбором на NAS": pd.get("selected") == ["[0]"],
    "в задачу передана новая папка": pd.get("destination") == ['"video/новая/папка"'],
    "показано «Готово»": ("info", "Готово") in asked,
    "без ошибок": not any(a[0] == "error" for a in asked),
}
ok = True
for name, cond in checks.items():
    print(("[OK ] " if cond else "[FAIL] ") + name)
    ok = ok and bool(cond)
if not ok:
    print("asked:", asked)
server.shutdown()
sys.exit(0 if ok else 1)
