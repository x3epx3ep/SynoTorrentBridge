"""Репро 2: точная копия open_settings() — wait_window вместо mainloop."""

import sys
import threading
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_all import MockNAS
import syno_bridge.config as config_mod
import syno_bridge.gui as _g
_g.save_config = lambda cfg: None  # не трогаем реальный config.json
from syno_bridge.gui import setup_root, SettingsDialog, OtpDialog

server = HTTPServer(("127.0.0.1", 0), MockNAS)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

from syno_bridge.config import encrypt_password

fake_cfg = {
    "host": "127.0.0.1", "port": port, "https": False, "verify_ssl": True,
    "username": "otpuser", "password_enc": encrypt_password("x"),
    "last_destination": "",
}
config_mod.load_config = lambda: dict(fake_cfg)
import syno_bridge.gui as gui_mod
gui_mod.load_config = lambda: dict(fake_cfg)

root = setup_root()
result = {"otp_shown": False, "status": ""}

# --- как open_settings() ---
dlg = SettingsDialog(root)


def watch():
    for parent in (root, dlg):
        for w in parent.winfo_children():
            if isinstance(w, OtpDialog):
                result["otp_shown"] = True
                w.var.set("123456")
                w._ok()
                return
    root.after(200, watch)


def check_done():
    txt = dlg.lbl_status.cget("text")
    result["status"] = txt
    if txt.startswith("OK") or txt.startswith("Ошибка"):
        dlg.destroy()          # закрываем настройки → wait_window вернётся
        return
    root.after(300, check_done)


root.after(300, dlg._test)
root.after(500, watch)
root.after(800, check_done)
root.wait_window(dlg)          # <-- ключевое отличие: БЕЗ mainloop
root.destroy()

print("Окно 2FA появилось:", result["otp_shown"])
print("Статус в конце:", result["status"])
server.shutdown()
sys.exit(0 if result["otp_shown"] else 1)
