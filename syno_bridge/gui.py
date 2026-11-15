"""GUI SynoTorrentBridge (tkinter): настройки, диалог добавления торрента."""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from urllib.parse import unquote

from . import __version__
from .bencode import TorrentMeta, TorrentFile, human_size, magnet_display_name
from .config import (load_config, save_config, is_configured, base_url,
                     encrypt_password, decrypt_password, remember_destination)
from .syno_api import SynoClient, SynoError
from . import registry_win
from . import theme
from . import updater

log = logging.getLogger("gui")

POLL_INTERVAL = 3          # сек между опросами списка файлов
POLL_TIMEOUT = 180         # макс. ожидание метаданных magnet


def run_bg(root: tk.Misc, func, on_done=None, on_error=None):
    """Выполняет func() в потоке; результат — в UI-потоке.

    ВАЖНО: виджеты Tk нельзя трогать из рабочего потока — root.after() из
    чужого потока падает с «main thread is not in main loop», когда главный
    поток сидит во вложенном цикле wait_window (модальные диалоги). Поэтому
    поток кладёт результат в queue, а главный поток опрашивает её сам.
    """
    results: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            results.put(("ok", func()))
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка фоновой задачи")
            results.put(("err", e))

    def poll():
        try:
            kind, payload = results.get_nowait()
        except queue.Empty:
            try:
                root.after(50, poll)
            except tk.TclError:
                pass  # окно уже уничтожено
            return
        if kind == "ok":
            (on_done or (lambda res: None))(payload)
        else:
            (on_error or (lambda err: None))(payload)

    root.after(50, poll)  # планируется из главного потока
    threading.Thread(target=worker, daemon=True).start()


def make_client(cfg: dict, otp: str = "") -> SynoClient:
    host, port, use_https = normalize_host(cfg["host"], int(cfg["port"]), bool(cfg["https"]))
    return SynoClient(
        host=host, port=port, use_https=use_https,
        verify_ssl=bool(cfg.get("verify_ssl", True)),
        username=cfg["username"], password=decrypt_password(cfg.get("password_enc", "")),
        otp_code=otp,
        # при входе по свежему OTP device_id не шлём — NAS выдаст новый токен
        device_id="" if otp else str(cfg.get("device_id", "")),
    )


# коды ошибок авторизации Synology, означающие «нужен/неверен код 2FA»
OTP_CODES = {403, 404, 406}


class OtpDialog(tk.Toplevel):
    """Собственное окно ввода 6-значного кода 2FA — всегда поверх других окон."""

    def __init__(self, parent, title="Двухфакторная аутентификация",
                 message="На NAS включена двухфакторная аутентификация.\n"
                         "Введите 6-значный код из приложения Authenticator.\n"
                         "Этот компьютер будет запомнен — код понадобится только один раз."):
        super().__init__(parent)
        self.configure(bg=theme.BG)
        self.result: str | None = None
        self.title(title)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=18, pady=16)
        ttk.Label(frm, text=message, justify="left").pack(anchor="w")
        self.var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=self.var, width=14,
                          font=("Segoe UI", 13), justify="center")
        entry.pack(pady=(10, 12))
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self._ok())
        btns = ttk.Frame(frm)
        btns.pack()
        theme.flat_button(btns, text="Войти", kind="accent",
                          command=self._ok).pack(side="left", padx=4)
        theme.flat_button(btns, text="Отмена",
                          command=self._cancel).pack(side="left", padx=4)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # центрируем над родительским окном
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x = px + max(0, (pw - self.winfo_width()) // 2)
            y = py + max(0, (ph - self.winfo_height()) // 2)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        self.grab_set()
        self.lift()
        self.focus_force()

    def _ok(self):
        code = self.var.get().strip()
        if code:
            self.result = code
            self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def ask_otp(parent) -> str | None:
    """Показывает окно ввода кода 2FA; возвращает код или None (отмена)."""
    dlg = OtpDialog(parent)
    parent.wait_window(dlg)
    return dlg.result


def login_flow(parent, cfg: dict, on_ok, on_error, status_label=None):
    """Логин с автоматическим запросом кода 2FA, если NAS его требует.

    on_ok(client) и on_error(exception) вызываются в главном потоке.
    """
    def attempt(otp: str = ""):
        def work():
            client = make_client(cfg, otp=otp)
            client.login()
            return client

        def ok(client):
            # NAS выдал device token при входе с OTP — сохраняем, код больше не нужен
            if client.device_id and client.device_id != cfg.get("device_id"):
                cfg["device_id"] = client.device_id
                try:
                    save_config(cfg)
                except OSError:
                    log.warning("Не удалось сохранить device token")
            on_ok(client)

        def err(e):
            if isinstance(e, SynoError) and e.code in OTP_CODES:
                if cfg.get("device_id") and not otp:
                    # сохранённый device token отклонён (отозван/устарел) — сбрасываем
                    log.info("Device token отклонён (код %s), сбрасываю", e.code)
                    cfg["device_id"] = ""
                    try:
                        save_config(cfg)
                    except OSError:
                        pass
                    attempt("")
                    return
                log.info("NAS запросил код 2FA (код %s)", e.code)
                if status_label is not None:
                    try:
                        status_label.config(text="Ожидание кода 2FA…", foreground="gray")
                    except tk.TclError:
                        pass
                code = ask_otp(parent)
                if code:
                    attempt(code.strip())
                    return
                on_error(SynoError("Вход отменён: не введён код 2FA"))
                return
            on_error(e)

        run_bg(parent, work, ok, err)

    attempt()


def normalize_host(host: str, port: int, https: bool):
    """Принимает '192.168.1.10', 'https://nas.local:5001/' и т.п. → (host, port, https)."""
    from urllib.parse import urlparse
    host = (host or "").strip().rstrip("/")
    if "://" in host:
        parsed = urlparse(host)
        https = parsed.scheme == "https"
        if parsed.port:
            port = parsed.port
        host = parsed.hostname or host
    # если в поле порта пусто/мусор — подставим разумный дефолт
    if not port:
        port = 5001 if https else 5000
    return host, port, https


# ---------------------------------------------------------------- настройки

class SettingsDialog(tk.Toplevel):
    def __init__(self, master, on_saved=None):
        super().__init__(master)
        self.on_saved = on_saved
        self.title(f"SynoTorrentBridge {__version__} — настройки")
        self.resizable(False, False)
        self.cfg = load_config()
        self._build()
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build(self):
        self.configure(bg=theme.BG)
        # шапка
        band = ttk.Frame(self, style="Band.TFrame")
        band.pack(fill="x")
        inner = ttk.Frame(band, style="Band.TFrame")
        inner.pack(fill="x", padx=16, pady=12)
        ttk.Label(inner, text="⚙  Настройки", style="Band.TLabel",
                  font=(theme.FONT, 13, "bold")).pack(anchor="w")
        ttk.Label(inner, text=f"SynoTorrentBridge {__version__}",
                  style="BandSub.TLabel").pack(anchor="w")

        # карточка подключения
        card = theme.card_frame(self)
        card.pack(fill="x", padx=10, pady=(10, 5))
        pad = {"padx": 10, "pady": 4}
        ttk.Label(card, text="Подключение к NAS", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(card, text="Адрес NAS:", style="Card.TLabel").grid(row=1, column=0, sticky="w", **pad)
        self.v_host = tk.StringVar(value=self.cfg.get("host", ""))
        ttk.Entry(card, textvariable=self.v_host, width=28).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(card, text="Порт:", style="Card.TLabel").grid(row=2, column=0, sticky="w", **pad)
        self.v_port = tk.StringVar(value=str(self.cfg.get("port", 5000)))
        ttk.Entry(card, textvariable=self.v_port, width=8).grid(row=2, column=1, sticky="w", **pad)

        self.v_https = tk.BooleanVar(value=bool(self.cfg.get("https")))
        ttk.Checkbutton(card, text="HTTPS", style="Card.TCheckbutton",
                        variable=self.v_https).grid(row=3, column=1, sticky="w", **pad)
        self.v_verify = tk.BooleanVar(value=bool(self.cfg.get("verify_ssl", True)))
        ttk.Checkbutton(card, text="Проверять SSL-сертификат", style="Card.TCheckbutton",
                        variable=self.v_verify).grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(card, text="Логин:", style="Card.TLabel").grid(row=5, column=0, sticky="w", **pad)
        self.v_user = tk.StringVar(value=self.cfg.get("username", ""))
        ttk.Entry(card, textvariable=self.v_user, width=28).grid(row=5, column=1, sticky="w", **pad)

        ttk.Label(card, text="Пароль:", style="Card.TLabel").grid(row=6, column=0, sticky="w", **pad)
        self.v_pass = tk.StringVar()
        if self.cfg.get("password_enc"):
            self.v_pass.set("••••••••")
            self._pass_unchanged = True
        else:
            self._pass_unchanged = False
        e_pass = ttk.Entry(card, textvariable=self.v_pass, width=28, show="*")
        e_pass.grid(row=6, column=1, sticky="w", **pad)
        e_pass.bind("<Key>", lambda _e: setattr(self, "_pass_unchanged", False))

        ttk.Label(card, text="Если на NAS включена 2FA — код будет запрошен автоматически.",
                  style="CardMuted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", **pad)
        self.lbl_status = ttk.Label(card, text="", style="CardMuted.TLabel")
        self.lbl_status.grid(row=8, column=0, columnspan=2, sticky="w", **pad)

        # карточка интеграции
        sys_card = theme.card_frame(self)
        sys_card.pack(fill="x", padx=10, pady=5)
        ttk.Label(sys_card, text="Интеграция с Windows", style="CardTitle.TLabel").pack(
            anchor="w", padx=10, pady=(10, 2))

        reg_row = tk.Frame(sys_card, bg=theme.CARD)
        reg_row.pack(fill="x", padx=10, pady=4)
        self.lbl_reg = ttk.Label(reg_row, style="CardMuted.TLabel")
        self.lbl_reg.pack(side="left")
        theme.flat_button(reg_row, text="Выключить", kind="chip",
                          command=self._reg_off).pack(side="right", padx=2)
        theme.flat_button(reg_row, text="Включить", kind="chip",
                          command=self._reg_on).pack(side="right", padx=2)

        self.v_notify = tk.BooleanVar(value=registry_win.is_autostart())
        ttk.Checkbutton(sys_card, style="Card.TCheckbutton",
                        text="Уведомлять о завершении закачек (фоновый монитор в автозагрузке)",
                        variable=self.v_notify,
                        command=self._notify_toggle).pack(anchor="w", padx=10, pady=(2, 10))
        self._refresh_reg()

        # карточка обновлений
        upd_card = theme.card_frame(self)
        upd_card.pack(fill="x", padx=10, pady=5)
        ttk.Label(upd_card, text="Обновления", style="CardTitle.TLabel").pack(
            anchor="w", padx=10, pady=(10, 2))
        upd_row = tk.Frame(upd_card, bg=theme.CARD)
        upd_row.pack(fill="x", padx=10, pady=(2, 10))
        self.lbl_upd = ttk.Label(upd_row, style="CardMuted.TLabel",
                                 text=f"Текущая версия: {__version__}")
        self.lbl_upd.pack(side="left")
        self.btn_upd = theme.flat_button(upd_row, text="Проверить", kind="chip",
                                         command=lambda: self._update_check(manual=True))
        self.btn_upd.pack(side="right", padx=2)
        # тихая автопроверка при открытии настроек
        self.after(400, lambda: self._update_check(manual=False))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(6, 12))
        theme.flat_button(btns, text="Проверить подключение",
                          command=self._test).pack(side="left")
        theme.flat_button(btns, text="Отмена", command=self.destroy).pack(side="right")
        theme.flat_button(btns, text="Сохранить", kind="accent",
                          command=self._save).pack(side="right", padx=8)

    def _notify_toggle(self):
        try:
            registry_win.set_autostart(self.v_notify.get())
        except OSError as e:
            messagebox.showerror("Ошибка", str(e), parent=self)

    def _refresh_reg(self):
        self.lbl_reg.config(text="Перехват: " + ("включён ✓" if registry_win.is_registered() else "выключен"))

    def _reg_on(self):
        try:
            registry_win.register()
        except OSError as e:
            messagebox.showerror("Ошибка", f"Не удалось записать в реестр: {e}", parent=self)
        self._refresh_reg()

    def _reg_off(self):
        try:
            registry_win.unregister()
        except OSError as e:
            messagebox.showerror("Ошибка", str(e), parent=self)
        self._refresh_reg()

    # ---- обновления ----

    def _update_check(self, manual: bool):
        """Проверяет наличие новой версии на GitHub. manual=False — тихий режим."""
        if not updater.GITHUB_REPO:
            if manual:
                self.lbl_upd.config(text=f"Текущая версия: {__version__} • источник обновлений не настроен")
            return
        self.btn_upd.config(state="disabled")
        if manual:
            self.lbl_upd.config(text="Проверка обновлений…")

        def done(info):
            try:
                self.btn_upd.config(state="normal")
            except tk.TclError:
                return
            if info:
                self.lbl_upd.config(text=f"Доступна новая версия: {info['tag']}!")
                self._update_offer(info)
            elif manual:
                self.lbl_upd.config(text=f"Текущая версия: {__version__} • у вас последняя версия ✓")

        def err(_e):
            try:
                self.btn_upd.config(state="normal")
                if manual:
                    self.lbl_upd.config(text="Не удалось проверить обновления (нет сети?)")
            except tk.TclError:
                pass

        run_bg(self, updater.check_latest, done, err)

    def _update_offer(self, info: dict):
        if not getattr(sys, "frozen", False):
            messagebox.showinfo("Обновление",
                                f"Доступна версия {info['tag']}.\n"
                                f"Автообновление работает только в собранном exe.",
                                parent=self)
            return
        if not messagebox.askyesno(
                "Обновление",
                f"Доступна новая версия {info['tag']} (у вас {__version__}).\n\n"
                f"Скачать и установить сейчас? Приложение перезапустится.",
                parent=self):
            return
        self.lbl_upd.config(text=f"Скачивание {info['tag']}…")
        dst = updater.temp_target()

        def downloaded(_path):
            try:
                self.lbl_upd.config(text="Установка…")
                updater.install_and_restart(dst)
            except OSError as e:
                messagebox.showerror("Обновление", str(e), parent=self)
                return
            # bat-скрипт уже запущен и ждёт — выходим немедленно, чтобы освободить exe.
            # destroy() может упасть, если root уже уничтожен — игнорируем.
            try:
                self.master.destroy()
            except tk.TclError:
                pass
            os._exit(0)

        def dl_err(e):
            try:
                self.lbl_upd.config(text="Ошибка скачивания обновления")
            except tk.TclError:
                pass
            messagebox.showerror("Обновление", f"Не удалось скачать: {e}", parent=self)

        run_bg(self, lambda: updater.download(info["url"], dst), downloaded, dl_err)

    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        try:
            port = int(self.v_port.get().strip() or 0)
        except ValueError:
            port = 0
        host, port, https = normalize_host(self.v_host.get(), port, self.v_https.get())
        if not port:
            port = 5001 if https else 5000
        cfg.update({
            "host": host,
            "port": port,
            "https": https,
            "verify_ssl": self.v_verify.get(),
            "username": self.v_user.get().strip(),
        })
        if not self._pass_unchanged:
            cfg["password_enc"] = encrypt_password(self.v_pass.get())
        return cfg

    def _test(self):
        cfg = self._collect()
        self.lbl_status.config(text="Подключение…", foreground="gray")

        def ok(client):
            feats = []
            feats.append("DSM 7 (Download Station 2)" if client.ds2 else "DSM 6 (Download Station)")
            feats.append("выбор файлов: да" if client.supports_file_selection else "выбор файлов: нет")
            try:
                shares = client.list_shares()
                feats.append(f"общих папок: {len(shares)}")
            except Exception:
                feats.append("FileStation недоступен")
            client.logout()
            self.lbl_status.config(text="OK — " + "; ".join(feats), foreground="green")

        def err(e):
            self.lbl_status.config(text=f"Ошибка: {e}", foreground="red")

        login_flow(self, cfg, ok, err, status_label=self.lbl_status)

    def _save(self):
        cfg = self._collect()
        # не затирать device token 2FA, сохранённый после открытия этого окна
        cfg["device_id"] = load_config().get("device_id", cfg.get("device_id", ""))
        save_config(cfg)
        try:
            registry_win.register_app(__version__)  # запись в «Установка и удаление программ»
        except OSError:
            pass
        self.cfg = cfg
        messagebox.showinfo("Сохранено", "Настройки сохранены.", parent=self)
        if self.on_saved:
            self.on_saved(cfg)
        self.destroy()


# ------------------------------------------------------- выбор папки на NAS

class FolderPickerDialog(tk.Toplevel):
    """Дерево папок NAS с ленивой подгрузкой."""

    def __init__(self, master, client: SynoClient, initial: str = ""):
        super().__init__(master)
        self.configure(bg=theme.BG)
        self.client = client
        self.result: str | None = None
        self.title("Папка назначения на NAS")
        self.geometry("480x420")
        self.minsize(360, 280)

        self.tree = ttk.Treeview(self, columns=("path",), displaycolumns=())
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree.bind("<<TreeviewOpen>>", self._on_open)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        theme.flat_button(btns, text="Новая папка", kind="chip",
                          command=self._mkdir).pack(side="left")
        theme.flat_button(btns, text="Выбрать", kind="accent",
                          command=self._ok).pack(side="right", padx=4)
        theme.flat_button(btns, text="Отмена", command=self.destroy).pack(side="right")

        self.tree.insert("", "end", iid="__loading__", text="Загрузка…")
        run_bg(self, self.client.list_shares, self._fill_root,
               lambda e: self._fail(e))
        self.grab_set()

    def _fail(self, e):
        self.tree.delete("__loading__")
        messagebox.showerror("Ошибка", f"Не удалось получить список папок: {e}", parent=self)

    def _fill_root(self, shares):
        if not self.tree.winfo_exists():
            return
        self.tree.delete("__loading__")
        for s in shares:
            node = self.tree.insert("", "end", text=s["name"], values=(s["path"],))
            self.tree.insert(node, "end", text="…")  # заглушка для ленивой загрузки

    def _on_open(self, _event):
        node = self.tree.focus()
        if not node:
            return
        children = self.tree.get_children(node)
        if len(children) == 1 and self.tree.item(children[0], "text") == "…":
            path = self.tree.item(node, "values")[0]
            run_bg(self, lambda: self.client.list_dirs(path),
                   lambda dirs, n=node: self._fill_node(n, dirs),
                   lambda e: messagebox.showerror("Ошибка", str(e), parent=self))

    def _fill_node(self, node, dirs):
        if not self.tree.winfo_exists() or not self.tree.exists(node):
            return
        for c in self.tree.get_children(node):
            self.tree.delete(c)
        for d in dirs:
            child = self.tree.insert(node, "end", text=d["name"], values=(d["path"],))
            self.tree.insert(child, "end", text="…")

    def _mkdir(self):
        node = self.tree.focus()
        if not node:
            messagebox.showinfo("Новая папка", "Сначала выберите родительскую папку.", parent=self)
            return
        parent = self.tree.item(node, "values")[0]
        name = simpledialog.askstring("Новая папка", "Имя папки:", parent=self)
        if not name:
            return
        run_bg(self, lambda: self.client.create_folder(parent, name),
               lambda _r: self._on_open_force(node),
               lambda e: messagebox.showerror("Ошибка", str(e), parent=self))

    def _on_open_force(self, node):
        for c in self.tree.get_children(node):
            self.tree.delete(c)
        self.tree.insert(node, "end", text="…")
        self.tree.item(node, open=True)
        self._on_open(None)

    def _ok(self):
        node = self.tree.focus()
        if node:
            self.result = self.tree.item(node, "values")[0]
        self.destroy()


# --------------------------------------------------------- ожидание magnet

class WaitDialog(tk.Toplevel):
    """Прогресс ожидания метаданных magnet-ссылки."""

    def __init__(self, master, title: str, text: str):
        super().__init__(master)
        self.configure(bg=theme.BG)
        self.cancelled = False
        self.title(title)
        self.resizable(False, False)
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=16, pady=16)
        self.lbl = ttk.Label(frm, text=text, wraplength=360)
        self.lbl.pack(pady=(0, 10))
        self.bar = ttk.Progressbar(frm, mode="indeterminate", length=340)
        self.bar.pack()
        self.bar.start(12)
        theme.flat_button(frm, text="Отмена", kind="chip",
                          command=self._cancel).pack(pady=(10, 0))
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()

    def set_text(self, text: str):
        if self.winfo_exists():
            self.lbl.config(text=text)

    def _cancel(self):
        self.cancelled = True
        self.destroy()


# ------------------------------------------------- панель выбора файлов

VIDEO_EXT = {".mkv", ".avi", ".mp4", ".m4v", ".mov", ".wmv", ".ts", ".m2ts",
             ".mpg", ".mpeg", ".flv", ".webm", ".vob"}
SUBS_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}


class FileSelectPanel(tk.Frame):
    """Список файлов с поиском и фильтрами. Мутирует TorrentFile.selected."""

    def __init__(self, master, files: list, on_change=None):
        super().__init__(master, bg=theme.CARD, highlightbackground=theme.BORDER,
                         highlightthickness=1, bd=0)
        self.files = files
        self.on_change = on_change
        self._sorted = sorted(files, key=lambda x: x.path.lower())

        bar = tk.Frame(self, bg=theme.CARD)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(bar, text="Поиск:", style="CardMuted.TLabel").pack(side="left")
        self.v_search = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.v_search, width=20)
        e.pack(side="left", padx=(4, 10))
        e.bind("<KeyRelease>", lambda _e: self._rebuild())
        for text, cmd in (("Все", lambda: self.set_visible(True)),
                          ("Ничего", lambda: self.set_visible(False)),
                          ("Только видео", self.only_video),
                          ("Без субтитров", self.drop_subs)):
            theme.flat_button(bar, text=text, command=cmd, kind="chip",
                              padx=8, pady=3).pack(side="left", padx=2)
        self.lbl = ttk.Label(bar, text="", style="CardMuted.TLabel")
        self.lbl.pack(side="right")

        tree_frm = ttk.Frame(self, style="Card.TFrame")
        tree_frm.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.tree = ttk.Treeview(tree_frm, columns=("sel", "size"),
                                 selectmode="extended")
        self.tree.heading("#0", text="Файл")
        self.tree.heading("sel", text="✓")
        self.tree.heading("size", text="Размер")
        self.tree.column("#0", width=400)
        self.tree.column("sel", width=34, anchor="center", stretch=False)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        sb = ttk.Scrollbar(tree_frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda _e: self._toggle_sel())
        self._rebuild()

    # --- отображение ---

    def _rebuild(self):
        q = self.v_search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self._visible = []
        for f in self._sorted:
            if q and q not in f.path.lower():
                continue
            iid = self.tree.insert("", "end", text=f.path,
                                   values=("☑" if f.selected else "☐",
                                           human_size(f.size)))
            self._visible.append((iid, f))
        self._update_label()

    def _update_label(self):
        sel = [f for f in self.files if f.selected]
        size = sum(f.size for f in sel)
        self.lbl.config(
            text=f"{len(sel)}/{len(self.files)} • {human_size(size)}")
        if self.on_change:
            self.on_change()

    # --- выбор ---

    def toggle(self, iid):
        for i, f in self._visible:
            if i == iid:
                f.selected = not f.selected
                self.tree.item(iid, values=("☑" if f.selected else "☐",
                                            human_size(f.size)))
                break
        self._update_label()

    def _click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.toggle(iid)
            return "break"

    def _toggle_sel(self):
        for iid in self.tree.selection():
            self.toggle(iid)

    def set_visible(self, val: bool):
        for iid, f in self._visible:
            f.selected = val
            self.tree.item(iid, values=("☑" if val else "☐", human_size(f.size)))
        self._update_label()

    def only_video(self):
        for f in self.files:
            ext = "." + f.path.rsplit(".", 1)[-1].lower() if "." in f.path else ""
            f.selected = ext in VIDEO_EXT
        self._rebuild()

    def drop_subs(self):
        for f in self.files:
            ext = "." + f.path.rsplit(".", 1)[-1].lower() if "." in f.path else ""
            if ext in SUBS_EXT:
                f.selected = False
        self._rebuild()


# ------------------------------------------------- главный диалог добавления

class AddTorrentDialog:
    """Диалог: файлы + папка назначения → отправка в Download Station."""

    def __init__(self, root: tk.Tk, client: SynoClient, meta: TorrentMeta,
                 magnet_uri: str = "", torrent_bytes: bytes = b"", torrent_filename: str = ""):
        self.root = root
        self.client = client
        self.meta = meta
        self.magnet_uri = magnet_uri
        self.torrent_bytes = torrent_bytes
        self.torrent_filename = torrent_filename
        self.cfg = load_config()

        self.win = tk.Toplevel(root)
        self.win.configure(bg=theme.BG)
        self.win.title("Добавить загрузку — SynoTorrentBridge")
        self.win.geometry("700x620")
        self.win.minsize(560, 480)
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

        self._build()
        self.win.grab_set()

    # ---------- построение ----------

    def _build(self):
        # цветная шапка
        band = ttk.Frame(self.win, style="Band.TFrame")
        band.pack(fill="x")
        inner = ttk.Frame(band, style="Band.TFrame")
        inner.pack(fill="x", padx=16, pady=12)
        ttk.Label(inner, text="⬇  Добавить загрузку", style="Band.TLabel",
                  font=(theme.FONT, 13, "bold")).pack(anchor="w")
        title = self.meta.name or "(без имени)"
        info = title
        parts = []
        if self.meta.files:
            parts.append(f"файлов: {len(self.meta.files)}")
        parts.append(human_size(self.meta.total_size))
        if self.meta.is_magnet:
            parts.append("magnet-ссылка")
        ttk.Label(inner, text=info, style="BandSub.TLabel",
                  wraplength=620).pack(anchor="w", pady=(2, 0))
        ttk.Label(inner, text="   •   ".join(parts), style="BandSub.TLabel").pack(anchor="w")

        # --- файлы (карточка)
        self.can_select = bool(self.meta.files)
        if self.can_select:
            self.panel = FileSelectPanel(self.win, self.meta.files)
            self.panel.pack(fill="both", expand=True, padx=10, pady=(10, 5))
        else:
            card = theme.card_frame(self.win)
            card.pack(fill="both", expand=True, padx=10, pady=(10, 5))
            note = ("Список файлов magnet-ссылки станет известен, когда NAS получит "
                    "метаданные от пиров.")
            if not self.client.supports_file_selection:
                note += ("\n\nНа вашем NAS недоступен API выбора файлов "
                         "(SYNO.DownloadStation2.Task.BT.File) — торрент будет скачан целиком.")
            ttk.Label(card, text=note, style="Card.TLabel", wraplength=580,
                      justify="left").pack(padx=14, pady=(16, 8), anchor="w")
            self.v_pick_later = tk.BooleanVar(value=self.client.supports_file_selection)
            if self.client.supports_file_selection:
                ttk.Checkbutton(
                    card, style="Card.TCheckbutton",
                    text="Выбрать файлы после получения метаданных (NAS сначала получит список)",
                    variable=self.v_pick_later).pack(padx=14, pady=(0, 14), anchor="w")

        # --- папка назначения (карточка)
        dest_card = theme.card_frame(self.win)
        dest_card.pack(fill="x", padx=10, pady=5)
        ttk.Label(dest_card, text="Папка назначения на NAS",
                  style="CardTitle.TLabel").pack(anchor="w", padx=12, pady=(10, 4))
        row = tk.Frame(dest_card, bg=theme.CARD)
        row.pack(fill="x", padx=12, pady=(0, 10))
        recent = self.cfg.get("recent_destinations", [])
        default_dest = self.cfg.get("last_destination") or ""
        self.v_dest = tk.StringVar(value=default_dest)
        self.cmb_dest = ttk.Combobox(row, textvariable=self.v_dest,
                                     values=recent, width=52)
        self.cmb_dest.pack(side="left", fill="x", expand=True)
        theme.flat_button(row, text="Обзор…", kind="chip",
                          command=self._browse).pack(side="left", padx=(6, 0))

        # --- кнопки
        btns = ttk.Frame(self.win)
        btns.pack(fill="x", padx=12, pady=(6, 12))
        self.lbl_status = ttk.Label(btns, text="", style="Muted.TLabel")
        self.lbl_status.pack(side="left")
        theme.flat_button(btns, text="Отмена", command=self._cancel).pack(side="right")
        self.btn_ok = theme.flat_button(btns, text="Скачать", kind="accent",
                                        command=self._submit)
        self.btn_ok.pack(side="right", padx=8)

    # ---------- файлы ----------

    # ---------- папка ----------

    def _browse(self):
        dlg = FolderPickerDialog(self.win, self.client, self.v_dest.get())
        self.win.wait_window(dlg)
        if dlg.result:
            self.v_dest.set(dlg.result.lstrip("/"))

    # ---------- отправка ----------

    def _cancel(self):
        self.client.logout()
        self.win.destroy()
        self.root.quit()

    def _selected_paths(self) -> set:
        return {f.path for f in self.meta.files if f.selected}

    def _submit(self):
        dest = self.v_dest.get().strip().strip("/")
        if not dest:
            if not messagebox.askyesno("Папка не выбрана",
                                       "Не указана папка назначения — будет использована "
                                       "папка по умолчанию Download Station. Продолжить?", parent=self.win):
                return
        self._check_duplicate(dest)

    def _check_duplicate(self, dest: str):
        """Если задача с таким именем уже есть в Download Station — предупреждаем."""
        self.btn_ok.config(state="disabled")
        self.lbl_status.config(text="Проверка дубликатов…")

        def work():
            try:
                info_hash = "" if self.meta.is_magnet else getattr(self.meta, "info_hash", "")
                return self.client.find_task_by_hash_or_title(info_hash, self.meta.name)
            except SynoError as e:
                log.warning("Проверка дубликатов не удалась: %s", e)
                return ""

        def ok(found):
            self.btn_ok.config(state="normal")
            self.lbl_status.config(text="")
            if found:
                if not messagebox.askyesno(
                        "Такая задача уже есть",
                        f"В Download Station уже есть задача «{self.meta.name}».\n\n"
                        "Добавить ещё одну копию?", parent=self.win):
                    self._cancel()
                    return
            self._proceed(dest)

        def err(_e):
            self.btn_ok.config(state="normal")
            self.lbl_status.config(text="")
            self._proceed(dest)  # не смогли проверить — не мешаем добавлению

        run_bg(self.win, work, ok, err)

    def _proceed(self, dest: str):
        if self.meta.is_magnet:
            pick_later = getattr(self, "v_pick_later", tk.BooleanVar(value=False)).get()
            if pick_later:
                self._magnet_flow(dest)
                return
            self._create_and_finish(self.magnet_uri, dest, task_id_then=None)
            return

        # .torrent файл — файлы известны заранее
        unselected = [f for f in self.meta.files if not f.selected]
        if unselected and not self.client.supports_file_selection:
            if not messagebox.askyesno(
                    "Выбор файлов недоступен",
                    "На вашем NAS не найден API выбора файлов (SYNO.DownloadStation2.Task.BT.File).\n"
                    "Торрент будет скачан целиком. Продолжить?", parent=self.win):
                return
            unselected = []
        self._create_torrent_flow(dest, unselected)

    # --- поток для .torrent ---

    @staticmethod
    def _name_variants(name: str):
        """Имя файла с корневой папкой раздачи и без неё."""
        name = name.replace("\\", "/").lstrip("/")
        yield name
        if "/" in name:
            yield name.split("/", 1)[1]

    def _pending_indexes(self, pending: list, selected_paths: set) -> list | None:
        """Индексы выбранных файлов среди pending; None, если не все имена совпали."""
        indexes = []
        for p in pending:
            names = set(self._name_variants(p["name"]))
            if names & selected_paths:
                indexes.append(p["index"])
        return indexes if len(indexes) == len(selected_paths) else None

    def _create_torrent_flow(self, dest: str, unselected: list):
        self.btn_ok.config(state="disabled")
        self.lbl_status.config(text="Отправка торрента на NAS…")

        def work():
            if not unselected:
                return self.client.create_task_file(
                    self.torrent_bytes, self.torrent_filename, dest), 0
            selected_paths = {f.path for f in self.meta.files if f.selected}
            # основной путь (как в DS get): список выбора → задача стартует сразу с выбором
            try:
                list_id = self.client.create_pending_list_file(
                    self.torrent_bytes, self.torrent_filename, dest)
                if list_id:
                    indexes = None
                    try:
                        pending = self.client.get_pending_files(list_id)
                        indexes = self._pending_indexes(pending, selected_paths)
                        if indexes is not None:
                            # основной путь: selected применяется на стороне NAS
                            # до старта закачки (Task.List.Polling download)
                            try:
                                self.client.pending_download_selected(list_id, dest, indexes)
                                log.info("Задача создана с выбором на стороне NAS: "
                                         "%s файлов из %s", len(indexes), len(pending))
                                return "", len(pending) - len(indexes)
                            except SynoError as e:
                                log.warning("Polling selected не сработал (%s) — "
                                            "скачиваю список целиком со skip", e)
                                task_id = self.client.pending_download(list_id, dest)
                                skipped = 0
                                if task_id and len(indexes) < len(pending):
                                    skipped = self._apply_skip(task_id, unselected)
                                return task_id, skipped
                        log.warning("Имена файлов не совпали со списком NAS — старый путь")
                    finally:
                        if not indexes:
                            self.client.pending_delete(list_id)
            except SynoError as e:
                log.warning("Список выбора не удался (%s) — старый путь", e)
            # запасной путь: создать задачу и пометить skip на лету
            task_id = self.client.create_task_file(self.torrent_bytes, self.torrent_filename, dest)
            skipped = 0
            if unselected and task_id:
                skipped = self._apply_skip(task_id, unselected)
            elif unselected and not task_id:
                task_id = self.client.find_task_by_hash_or_title(self.meta.info_hash, self.meta.name)
                if task_id:
                    skipped = self._apply_skip(task_id, unselected)
            return task_id, skipped

        def ok(result):
            _task_id, skipped = result
            self._finish_ok(dest, skipped)

        def err(e):
            self.btn_ok.config(state="normal")
            self.lbl_status.config(text="")
            messagebox.showerror("Ошибка", f"Не удалось добавить задачу:\n{e}", parent=self.win)

        run_bg(self.win, work, ok, err)

    def _apply_skip(self, task_id: str, unselected: list) -> int:
        """Ставит skip на невыбранные файлы. Возвращает число помеченных."""
        deadline = time.time() + 30
        ds_files = []
        while time.time() < deadline:
            ds_files = self.client.list_bt_files(task_id)
            if ds_files:
                break
            time.sleep(2)
        if not ds_files:
            log.warning("Не удалось получить список файлов задачи %s", task_id)
            return 0

        by_path = {f.path: f for f in unselected}
        ids = []
        for dsf in ds_files:
            name = dsf["name"].replace("\\", "/")
            candidates = [name]
            if "/" in name:
                candidates.append(name.split("/", 1)[1])  # без корневой папки раздачи
            for cand in candidates:
                cand = cand.lstrip("/")
                if cand in by_path:
                    ids.append(dsf["id"])
                    break
        if ids:
            ok = self.client.set_files_skip(task_id, ids, skip=True)
            if ok:
                return len(ids)
            log.warning("set_files_skip не сработал для %s", ids)
        return 0

    def _skip_by_names(self, task_id: str, names: list) -> int:
        """Помечает skip файлы задачи по именам (имена уже в NAS-формате)."""
        targets = set()
        for n in names:
            targets.update(self._name_variants(n))
        deadline = time.time() + 30
        ds_files = []
        while time.time() < deadline:
            try:
                ds_files = self.client.list_bt_files(task_id)
            except SynoError:
                ds_files = []
            if ds_files:
                break
            time.sleep(2)
        if not ds_files:
            log.warning("Не удалось получить список файлов задачи %s", task_id)
            return 0
        ids = []
        for dsf in ds_files:
            variants = set(self._name_variants(dsf["name"]))
            if variants & targets:
                ids.append(dsf["id"])
        if ids and self.client.set_files_skip(task_id, ids, skip=True):
            return len(ids)
        if ids:
            log.warning("set_files_skip не сработал для %s", ids)
        return 0

    # --- поток для magnet ---

    def _magnet_flow(self, dest: str):
        self.btn_ok.config(state="disabled")
        self.lbl_status.config(text="Добавление magnet-ссылки…")
        wait = WaitDialog(self.win, "Добавление magnet-ссылки", "Отправка ссылки на NAS…")

        # основной путь: пробуем список выбора. ВАЖНО: для magnet NAS игнорирует
        # create_list и сразу создаёт задачу — её task_id надо переиспользовать,
        # иначе повторный create сделает дубль.
        def create():
            return self.client.create_url_or_list(self.magnet_uri, dest)

        def after_create(result):
            list_id, task_id = result
            if list_id:
                wait.set_text("NAS получает метаданные торрента от пиров…\n"
                              "Это может занять до пары минут.")
                self._poll_pending(wait, list_id, dest, time.time())
                return
            # magnet: задача уже создана — идём в поток «unwant-all → диалог»
            self._magnet_flow_legacy(wait, dest, task_id=task_id)

        def create_err(e):
            wait.destroy()
            self.btn_ok.config(state="normal")
            self.lbl_status.config(text="")
            messagebox.showerror("Ошибка", f"Не удалось добавить magnet:\n{e}", parent=self.win)

        run_bg(self.win, create, after_create, create_err)

    def _poll_pending(self, wait: WaitDialog, list_id: str, dest: str, started: float):
        if wait.cancelled:
            self.client.pending_delete(list_id)
            self.client.logout()
            self.win.destroy()
            self.root.quit()
            return
        if time.time() - started > POLL_TIMEOUT:
            wait.destroy()
            self.client.pending_delete(list_id)
            messagebox.showwarning(
                "Таймаут",
                "NAS не смог получить метаданные за отведённое время.\n"
                "Задача не создана — попробуйте ещё раз позже.", parent=self.win)
            self.btn_ok.config(state="normal")
            self.lbl_status.config(text="")
            return

        def check():
            return self.client.get_pending_files(list_id)

        def got(files):
            if files:
                wait.destroy()
                self._show_pending_dialog(files, list_id, dest)
            else:
                self.win.after(POLL_INTERVAL * 1000,
                               lambda: self._poll_pending(wait, list_id, dest, started))

        def check_err(e):
            log.warning("Опрос списка выбора: %s", e)
            self.win.after(POLL_INTERVAL * 1000,
                           lambda: self._poll_pending(wait, list_id, dest, started))

        run_bg(self.win, check, got, check_err)

    def _show_pending_dialog(self, pending: list, list_id: str, dest: str):
        meta = TorrentMeta(name=self.meta.name,
                           files=[TorrentFile(path=p["name"], size=p["size"]) for p in pending],
                           total_size=sum(p["size"] for p in pending))
        dlg = MagnetFilesDialog(self.root, self.client, meta, "", [], dest, collect_only=True)
        self.win.wait_window(dlg.win)
        if not dlg.done:
            self.client.pending_delete(list_id)
            self.client.logout()
            self.win.destroy()
            self.root.quit()
            return
        selected = dlg.selected_paths
        indexes = [p["index"] for p in pending if p["name"] in selected]
        unselected_names = [p["name"] for p in pending if p["name"] not in selected]

        def work():
            # основной путь: выбор применяется на стороне NAS до старта закачки
            try:
                self.client.pending_download_selected(list_id, dest, indexes)
                return "", len(pending) - len(indexes)
            except SynoError as e:
                log.warning("Polling selected не сработал (%s) — со skip", e)
            task_id = self.client.pending_download(list_id, dest)
            skipped = 0
            if task_id and unselected_names:
                skipped = self._skip_by_names(task_id, unselected_names)
            return task_id, skipped

        def ok(result):
            _task_id, skipped = result
            self._finish_ok(dest, skipped)

        def err(e):
            self.btn_ok.config(state="normal")
            self.lbl_status.config(text="")
            messagebox.showerror("Ошибка", f"Не удалось создать задачу:\n{e}", parent=self.win)

        run_bg(self.win, work, ok, err)

    def _magnet_flow_legacy(self, wait: WaitDialog, dest: str, task_id: str = ""):
        state = {"task_id": task_id}

        def create():
            return task_id or self.client.create_task_url(self.magnet_uri, dest)

        def after_create(task_id):
            state["task_id"] = task_id
            if not task_id:
                task_id = self.client.find_task_by_hash_or_title(title=self.meta.name)
                state["task_id"] = task_id
            if not task_id:
                wait.destroy()
                self._finish_ok(dest, 0, note="Задача добавлена, но выбор файлов недоступен "
                                              "(NAS не вернул id задачи).")
                return
            wait.set_text("NAS получает метаданные торрента от пиров…\n"
                          "Это может занять до пары минут.")
            self._poll_files(wait, state, dest, time.time())

        def create_err(e):
            wait.destroy()
            self.btn_ok.config(state="normal")
            self.lbl_status.config(text="")
            messagebox.showerror("Ошибка", f"Не удалось добавить magnet:\n{e}", parent=self.win)

        run_bg(self.win, create, after_create, create_err)

    def _poll_files(self, wait: WaitDialog, state: dict, dest: str, started: float):
        if wait.cancelled:
            self.client.delete_task(state["task_id"])
            self.client.logout()
            self.win.destroy()
            self.root.quit()
            return
        if time.time() - started > POLL_TIMEOUT:
            wait.destroy()
            messagebox.showwarning(
                "Таймаут",
                "NAS не смог получить метаданные за отведённое время.\n"
                "Задача оставлена в Download Station — файлы можно выбрать вручную "
                "в веб-интерфейсе.", parent=self.win)
            self._finish_ok(dest, 0, note="Задача добавлена (без выбора файлов).")
            return

        def check():
            return self.client.list_bt_files(state["task_id"])

        def got(files):
            if files:
                wait.destroy()
                self._unwant_all_then_dialog(files, state["task_id"], dest)
            else:
                self.win.after(POLL_INTERVAL * 1000,
                               lambda: self._poll_files(wait, state, dest, started))

        def check_err(e):
            log.warning("Опрос файлов: %s", e)
            self.win.after(POLL_INTERVAL * 1000,
                           lambda: self._poll_files(wait, state, dest, started))

        run_bg(self.win, check, got, check_err)

    def _unwant_all_then_dialog(self, ds_files: list, task_id: str, dest: str):
        """Сразу снимает wanted со ВСЕХ файлов (закачка замирает, ничего не качается),
        затем показывает диалог выбора. Выбранным файлам wanted вернёт диалог."""
        def work():
            ids = [f["id"] for f in ds_files]
            return self.client.set_files_skip(task_id, ids, skip=True)

        def ok(_res):
            self._show_magnet_file_dialog(ds_files, task_id, dest, all_unwanted=True)

        def err(e):
            log.warning("Не удалось снять wanted со всех файлов: %s — "
                        "диалог в режиме skip", e)
            self._show_magnet_file_dialog(ds_files, task_id, dest, all_unwanted=False)

        run_bg(self.win, work, ok, err)

    def _show_magnet_file_dialog(self, ds_files: list, task_id: str, dest: str,
                                 all_unwanted: bool = False):
        meta = TorrentMeta(name=self.meta.name,
                           files=[TorrentFile(path=f["name"], size=f["size"]) for f in ds_files],
                           total_size=sum(f["size"] for f in ds_files))
        dlg = MagnetFilesDialog(self.root, self.client, meta, task_id, ds_files, dest,
                                all_unwanted=all_unwanted)
        self.win.wait_window(dlg.win)
        if dlg.done:
            self._finish_ok(dest, dlg.skipped)
        else:
            self.client.delete_task(task_id)
            self.client.logout()
            self.win.destroy()
            self.root.quit()

    # --- завершение ---

    def _create_and_finish(self, url: str, dest: str, task_id_then):
        self.btn_ok.config(state="disabled")
        self.lbl_status.config(text="Отправка на NAS…")

        def work():
            return self.client.create_task_url(url, dest)

        run_bg(self.win, work,
               lambda _t: self._finish_ok(dest, 0),
               lambda e: (self.btn_ok.config(state="normal"),
                          messagebox.showerror("Ошибка", str(e), parent=self.win)))

    def _finish_ok(self, dest: str, skipped: int, note: str = ""):
        cfg = self.cfg
        if dest:
            remember_destination(cfg, dest)
            save_config(cfg)
        msg = "Задача добавлена в Download Station."
        if dest:
            msg += f"\nПапка: /{dest}"
        if skipped:
            msg += f"\nИсключено файлов: {skipped}"
        if note:
            msg += f"\n{note}"
        self.client.logout()
        messagebox.showinfo("Готово", msg, parent=self.win)
        self.win.destroy()
        self.root.quit()


class MagnetFilesDialog:
    """Выбор файлов для magnet после получения метаданных.

    collect_only=True — только вернуть выбор (selected_paths), не применять skip.
    all_unwanted=True — у всех файлов уже снят wanted: при OK вернуть wanted
    выбранным (вместо снятия wanted с невыбранных).
    """

    def __init__(self, root, client, meta: TorrentMeta, task_id: str, ds_files: list, dest: str,
                 collect_only: bool = False, all_unwanted: bool = False):
        self.client = client
        self.task_id = task_id
        self.ds_files = ds_files
        self.collect_only = collect_only
        self.all_unwanted = all_unwanted
        self.selected_paths: set = set()
        self.done = False
        self.skipped = 0

        self.win = tk.Toplevel(root)
        self.win.configure(bg=theme.BG)
        self.win.title("Выбор файлов — magnet")
        self.win.geometry("660x540")
        self.meta = meta

        band = ttk.Frame(self.win, style="Band.TFrame")
        band.pack(fill="x")
        inner = ttk.Frame(band, style="Band.TFrame")
        inner.pack(fill="x", padx=16, pady=12)
        ttk.Label(inner, text="⬇  Выбор файлов", style="Band.TLabel",
                  font=(theme.FONT, 13, "bold")).pack(anchor="w")
        ttk.Label(inner, text=meta.name, style="BandSub.TLabel",
                  wraplength=620).pack(anchor="w", pady=(2, 0))
        ttk.Label(inner, text=f"файлов: {len(meta.files)}   •   {human_size(meta.total_size)}",
                  style="BandSub.TLabel").pack(anchor="w")

        self.panel = FileSelectPanel(self.win, meta.files)
        self.panel.pack(fill="both", expand=True, padx=10, pady=(10, 5))

        btns = ttk.Frame(self.win)
        btns.pack(fill="x", padx=12, pady=(6, 12))
        theme.flat_button(btns, text="Отмена", command=self._cancel).pack(side="right")
        theme.flat_button(btns, text="Скачать выбранные", kind="accent",
                          command=self._ok).pack(side="right", padx=8)
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.grab_set()

    def _cancel(self):
        self.done = False
        self.win.destroy()

    def _ok(self):
        if self.collect_only:
            self.selected_paths = {f.path for f in self.meta.files if f.selected}
            self.done = True
            self.win.destroy()
            return
        def find_id(path):
            name_to_id = getattr(self, "_name_to_id", None)
            if name_to_id is None:
                name_to_id = {str(f["name"]).replace("\\", "/"): f["id"]
                              for f in self.ds_files}
                self._name_to_id = name_to_id
            fid = name_to_id.get(path)
            if fid is None:
                # пробуем без корневой папки
                for name, i in name_to_id.items():
                    if name.split("/", 1)[-1] == path or name.endswith("/" + path):
                        fid = i
                        break
            return fid

        if self.all_unwanted:
            # wanted уже снят со всех — возвращаем его выбранным
            targets = [f for f in self.meta.files if f.selected]
            ids = [fid for f in targets if (fid := find_id(f.path)) is not None]

            def work():
                if ids:
                    return (self.client.set_files_skip(self.task_id, ids, skip=False),
                            len(self.meta.files) - len(ids))
                return True, len(self.meta.files)
        else:
            unselected = [f for f in self.meta.files if not f.selected]
            ids = [fid for f in unselected if (fid := find_id(f.path)) is not None]

            def work():
                if ids:
                    return self.client.set_files_skip(self.task_id, ids, skip=True), len(ids)
                return True, 0

        def done(res):
            ok, n = res
            self.skipped = n if ok else 0
            self.done = True
            self.win.destroy()

        def err(e):
            messagebox.showerror("Ошибка", f"Не удалось применить выбор файлов:\n{e}\n\n"
                                           "Задача останется в Download Station.", parent=self.win)
            self.done = True
            self.win.destroy()

        run_bg(self.win, work, done, err)


# ------------------------------------------------------------------ запуск

def setup_root() -> tk.Tk:
    root = tk.Tk()
    root.withdraw()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    theme.apply_theme(root)
    return root


def ensure_settings(root: tk.Tk) -> dict | None:
    cfg = load_config()
    if not is_configured(cfg):
        messagebox.showinfo("Первый запуск",
                            "Сначала нужно настроить подключение к NAS.", parent=root)
        saved = {}
        dlg = SettingsDialog(root, on_saved=lambda c: saved.update(c))
        root.wait_window(dlg)
        cfg = load_config()
        if not is_configured(cfg):
            return None
    return cfg


def open_settings():
    root = setup_root()
    dlg = SettingsDialog(root)
    root.wait_window(dlg)
    root.destroy()


def run_add(source: str):
    """Точка входа: source — magnet-URI или путь к .torrent."""
    root = setup_root()
    cfg = ensure_settings(root)
    if not cfg:
        root.destroy()
        return

    splash = WaitDialog(root, "SynoTorrentBridge", f"Подключение к {cfg['host']}…")
    state = {}

    def connected(client):
        splash.destroy()
        state["client"] = client
        try:
            if source.lower().startswith("magnet:"):
                meta = TorrentMeta(name=magnet_display_name(source), is_magnet=True)
                AddTorrentDialog(root, client, meta, magnet_uri=source)
            else:
                with open(source, "rb") as fh:
                    data = fh.read()
                from .bencode import parse_torrent
                meta = parse_torrent(data)
                import os
                AddTorrentDialog(root, client, meta,
                                 torrent_bytes=data,
                                 torrent_filename=os.path.basename(source))
        except Exception as e:  # noqa: BLE001
            log.exception("Не удалось открыть источник")
            client.logout()
            messagebox.showerror("Ошибка", str(e), parent=root)
            root.quit()

    def conn_err(e):
        splash.destroy()
        if "2FA" in str(e):
            # пользователь сам отменил ввод кода — просто закрываемся, без лишних окон
            root.destroy()
            return
        messagebox.showerror("Ошибка подключения",
                             f"Не удалось подключиться к NAS:\n{e}\n\n"
                             f"Проверьте настройки.", parent=root)
        open_settings()
        root.destroy()

    login_flow(splash, cfg, connected, conn_err)
    root.mainloop()
    root.destroy()
