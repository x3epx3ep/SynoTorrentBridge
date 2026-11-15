"""Тест 2FA-флоу: mock-NAS требует код, окно должно появиться, вход с кодом — успех."""

import sys
import threading
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_all import MockNAS
import syno_bridge.gui as _g
_g.save_config = lambda cfg: None  # не трогаем реальный config.json
from syno_bridge.gui import setup_root, login_flow, OtpDialog

server = HTTPServer(("127.0.0.1", 0), MockNAS)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

cfg = {
    "host": "127.0.0.1", "port": port, "https": False, "verify_ssl": True,
    "username": "otpuser", "password_enc": "", "last_destination": "",
}
# password пустой — mock принимает otpuser с любым паролем при наличии otp_code
from syno_bridge.config import encrypt_password
cfg["password_enc"] = encrypt_password("x")

root = setup_root()
result = {"client": None, "error": None, "otp_shown": False}


def on_ok(client):
    result["client"] = client
    root.quit()


def on_err(e):
    result["error"] = e
    root.quit()


def watch_for_otp_dialog():
    """Ищем OtpDialog среди окон и вводим код."""
    for w in root.winfo_children():
        if isinstance(w, OtpDialog):
            result["otp_shown"] = True
            w.var.set("123456")
            w._ok()
            return
    root.after(200, watch_for_otp_dialog)


login_flow(root, cfg, on_ok, on_err)
root.after(300, watch_for_otp_dialog)
root.mainloop()
root.destroy()
server.shutdown()

ok = True
for name, cond in {
    "окно 2FA появилось": result["otp_shown"],
    "вход с кодом успешен": result["client"] is not None and result["client"].sid == "OTPSID",
    "ошибок нет": result["error"] is None,
}.items():
    print(("[OK ] " if cond else "[FAIL] ") + name)
    ok = ok and cond

sys.exit(0 if ok else 1)
