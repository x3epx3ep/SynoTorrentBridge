"""Общая тема оформления SynoTorrentBridge (tkinter/ttk)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# палитра
BG = "#f4f6fb"          # фон окон
CARD = "#ffffff"        # карточки
ACCENT = "#2f6fed"      # основной акцент
ACCENT_DARK = "#1f56c4" # акцент при нажатии/наведении
TEXT = "#1f2430"
MUTED = "#6b7280"
BORDER = "#dfe3ee"
OK = "#15803d"
DANGER = "#dc2626"
SEL_BG = "#e4ecfe"

FONT = "Segoe UI"

# цвета плоских кнопок: (фон, текст, фон при наведении, текст при наведении)
_BUTTON_KINDS = {
    "accent": (ACCENT, "#ffffff", ACCENT_DARK, "#ffffff"),
    "normal": (CARD, TEXT, SEL_BG, ACCENT_DARK),
    "danger": (CARD, DANGER, "#fde8e8", DANGER),
    "chip": ("#edf1fa", TEXT, SEL_BG, ACCENT_DARK),
}


def flat_button(parent, text: str, command=None, kind: str = "normal",
                bold: bool | None = None, padx: int = 14, pady: int = 6,
                width: int = 0) -> tk.Button:
    """Плоская кнопка с hover-эффектом (ttk на Windows не красит фон кнопок)."""
    bg, fg, hbg, hfg = _BUTTON_KINDS[kind]
    if bold is None:
        bold = kind == "accent"
    b = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                  activebackground=hbg, activeforeground=hfg,
                  disabledforeground=MUTED, relief="flat", bd=0,
                  padx=padx, pady=pady, cursor="hand2",
                  font=(FONT, 9, "bold" if bold else "normal"),
                  highlightthickness=1, highlightbackground=BORDER)
    if kind == "accent":
        b.configure(highlightthickness=0, padx=padx + 1, pady=pady + 1)
    if width:
        b.configure(width=width)
    b.bind("<Enter>", lambda _e: b.config(bg=hbg, fg=hfg))
    b.bind("<Leave>", lambda _e: b.config(bg=bg, fg=fg))
    return b


def card_frame(parent, **pack_kw) -> tk.Frame:
    """Белая карточка с тонкой рамкой."""
    f = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                 highlightthickness=1, bd=0)
    if pack_kw:
        f.pack(**pack_kw)
    return f


def apply_theme(root: tk.Misc) -> None:
    """Настраивает ttk-стили и фон. Вызывать один раз на root."""
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    style.configure(".", font=(FONT, 9), background=BG, foreground=TEXT,
                    borderwidth=0, focuscolor="none")
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Band.TFrame", background=ACCENT)

    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("Band.TLabel", background=ACCENT, foreground="#ffffff")
    style.configure("BandSub.TLabel", background=ACCENT, foreground="#d8e4ff")
    style.configure("Title.TLabel", font=(FONT, 12, "bold"))
    style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT,
                    font=(FONT, 10, "bold"))
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
    style.configure("Ok.TLabel", foreground=OK)
    style.configure("Err.TLabel", foreground=DANGER)

    style.configure("TButton", padding=(12, 6), background=CARD)
    style.map("TButton",
              background=[("active", SEL_BG), ("pressed", BORDER)])
    style.configure("Accent.TButton", padding=(16, 7), background=ACCENT,
                    foreground="#ffffff", font=(FONT, 9, "bold"))
    style.map("Accent.TButton",
              background=[("active", ACCENT_DARK), ("pressed", ACCENT_DARK),
                          ("disabled", BORDER)],
              foreground=[("disabled", MUTED)])
    style.configure("Danger.TButton", padding=(12, 6), foreground=DANGER)

    style.configure("TCheckbutton", background=BG)
    style.configure("Card.TCheckbutton", background=CARD)
    style.configure("TEntry", padding=4)
    style.configure("TCombobox", padding=4)

    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED)

    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, rowheight=24, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER)
    style.configure("Treeview.Heading", font=(FONT, 9, "bold"),
                    background=BG, foreground=MUTED, padding=(6, 4))
    style.map("Treeview",
              background=[("selected", SEL_BG)],
              foreground=[("selected", TEXT)])

    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=BORDER, bordercolor=BORDER)

    root.configure(bg=BG)
    root.option_add("*Toplevel*background", BG)
