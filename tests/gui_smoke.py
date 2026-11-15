"""Дымовой тест GUI: открывает диалог добавления против mock-NAS и делает скриншот."""

import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_all import MockNAS
from syno_bridge import bencode
from syno_bridge.syno_api import SynoClient
from syno_bridge.gui import setup_root, AddTorrentDialog

server = HTTPServer(("127.0.0.1", 0), MockNAS)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

client = SynoClient("127.0.0.1", port, use_https=False, username="user", password="pass")
client.login()

torrent = {
    b"info": {
        b"name": "Test Razdacha 2024",
        b"piece length": 262144,
        b"pieces": b"x" * 40,
        b"files": [
            {b"length": 1234567, b"path": [b"Film", b"film.mkv"]},
            {b"length": 5000, b"path": [b"Subs", "russian.srt"]},
            {b"length": 800, b"path": [b"Subs", "english.srt"]},
            {b"length": 100, b"path": [b"readme.txt"]},
            {b"length": 950000, b"path": [b"Film", b"trailer.mp4"]},
        ],
    },
}
data = bencode.dumps(torrent)
meta = bencode.parse_torrent(data)

root = setup_root()
dlg = AddTorrentDialog(root, client, meta, torrent_bytes=data, torrent_filename="test.torrent")
root.deiconify()
dlg.win.attributes("-topmost", True)
dlg.win.lift()
dlg.win.focus_force()
dlg.win.update()


def verify():
    ok = True

    def walk(w, path=""):
        nonlocal ok
        for child in w.winfo_children():
            cpath = f"{path}/{child.winfo_class()}"
            if child.winfo_ismapped() and child.winfo_height() < 2:
                print("НЕВИДИМ:", cpath)
                ok = False
            walk(child, cpath)

    dlg.win.update_idletasks()
    dlg.win.update()
    walk(dlg.win)
    # ключевые элементы
    checks = {
        "заголовок": dlg.meta.name == "Test Razdacha 2024",
        "combobox папки": dlg.cmb_dest.winfo_ismapped() and dlg.cmb_dest.winfo_height() > 10,
        "кнопка Скачать": dlg.btn_ok.winfo_ismapped() and dlg.btn_ok.winfo_height() > 10,
        "список файлов": dlg.panel.tree.winfo_ismapped() and len(dlg.panel.tree.get_children()) == 5,
    }
    for name, cond in checks.items():
        print(("[OK ] " if cond else "[FAIL] ") + name)
        ok = ok and cond
    print("GUI layout:", "OK" if ok else "FAIL")

    # попытка скриншота (необязательно)
    try:
        from PIL import ImageGrab
        dlg.win.geometry("+50+50")
        dlg.win.lift()
        dlg.win.update()
        time.sleep(0.4)
        img = ImageGrab.grab(bbox=(0, 0, 1500, 1300))
        out = Path(__file__).resolve().parent / "gui_screenshot.png"
        img.save(out)
        print("screenshot:", out)
    except Exception as e:
        print("screenshot skipped:", e)
    dlg.win.destroy()
    root.quit()
    server.shutdown()
    sys.exit(0 if ok else 1)


root.after(1200, verify)
root.mainloop()
root.destroy()
print("GUI smoke OK")
