"""Фоновый монитор: уведомления о завершении закачек (запуск: --monitor).

Работает без окон, из автозагрузки (HKCU Run). Раз в POLL_SECONDS опрашивает
Download Station и показывает тост, когда задача переходит в статус «завершена».
Состояние хранится в monitor_state.json — перезапуск не порождает повторных
уведомлений.
"""

from __future__ import annotations

import json
import logging
import tkinter as tk

from . import theme
from .config import APP_DIR, load_config, is_configured
from .syno_api import SynoError

log = logging.getLogger("monitor")

POLL_SECONDS = 60
STATE_PATH = APP_DIR / "monitor_state.json"
FINISHED = {5}          # DS2: 5 = finished
OTP_CODES = {403, 404, 406}


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def show_toast(root: tk.Tk, title: str, text: str, timeout_ms: int = 9000) -> None:
    """Тост в правом нижнем углу; клик — закрыть."""
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.attributes("-topmost", True)
    top.configure(bg=theme.CARD, highlightbackground=theme.BORDER,
                  highlightthickness=1)
    tk.Frame(top, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
    body = tk.Frame(top, bg=theme.CARD)
    body.pack(side="left", fill="both", expand=True, padx=12, pady=10)
    tk.Label(body, text=title, bg=theme.CARD, fg=theme.TEXT,
             font=(theme.FONT, 10, "bold")).pack(anchor="w")
    tk.Label(body, text=text, bg=theme.CARD, fg=theme.MUTED,
             font=(theme.FONT, 9), wraplength=320,
             justify="left").pack(anchor="w", pady=(2, 0))
    top.update_idletasks()
    w, h = top.winfo_width(), top.winfo_height()
    x = top.winfo_screenwidth() - w - 16
    y = top.winfo_screenheight() - h - 64
    top.geometry(f"+{x}+{y}")
    for wgt in (top, body, *body.winfo_children()):
        wgt.bind("<Button-1>", lambda _e: top.destroy())
    top.after(timeout_ms, top.destroy)


class Monitor:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.client = None
        self.state = _load_state()          # task_id -> status
        self.snapshot_done = bool(self.state)
        self.otp_warned = False

    def _make_client(self):
        from .gui import make_client  # фабрика клиента из GUI (без OTP-диалога)
        return make_client(self.cfg)

    def poll(self):
        try:
            self._poll()
        except Exception as e:  # noqa: BLE001
            log.warning("Опрос Download Station: %s", e)
        self.root.after(POLL_SECONDS * 1000, self.poll)

    def _poll(self):
        if self.client is None:
            self.client = self._make_client()
        try:
            data = self.client._call("SYNO.DownloadStation2.Task", 2, "list",
                                     extra={"limit": 100})
        except SynoError as e:
            if e.code in OTP_CODES:
                # монитор не может спросить код — предупреждаем один раз
                if not self.otp_warned:
                    self.otp_warned = True
                    show_toast(self.root, "SynoTorrentBridge",
                               "NAS требует код 2FA. Откройте настройки приложения "
                               "и проверьте подключение.")
                return
            self.client = None  # пересоздадим на следующем цикле
            raise

        tasks = data.get("task") or []
        current = {str(t.get("id")): int(t.get("status", 0)) for t in tasks}

        if not self.snapshot_done:
            # первый успешный опрос — только запоминаем, без уведомлений
            self.snapshot_done = True
        else:
            for tid, status in current.items():
                old = self.state.get(tid)
                if status in FINISHED and old is not None and old not in FINISHED:
                    title = next((str(t.get("title", "")) for t in tasks
                                  if str(t.get("id")) == tid), tid)
                    log.info("Завершена задача: %s", title)
                    show_toast(self.root, "Загрузка завершена ✓", title)

        self.state = current
        _save_state(self.state)


def run() -> None:
    cfg = load_config()
    if not is_configured(cfg):
        log.warning("Монитор запущен без настроек — выход")
        return
    root = tk.Tk()
    root.withdraw()
    theme.apply_theme(root)
    mon = Monitor(root, cfg)
    root.after(2000, mon.poll)  # первый опрос почти сразу
    log.info("Монитор запущен (опрос каждые %s с)", POLL_SECONDS)
    root.mainloop()
