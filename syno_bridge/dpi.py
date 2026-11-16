"""Масштабирование интерфейса под DPI экрана (125 %, 150 %, 200 % — 4K).

Порядок важен: процесс объявляется DPI-aware ДО создания первого tk.Tk().
Тогда Tk при старте получает настоящий DPI и сам масштабирует шрифты
(они заданы в пунктах). Если объявить позже, Tk уже посчитал экран 96 DPI,
а Windows перестаёт растягивать окно — всё становится мелким.

Размеры в пикселях (геометрия окон, отступы, ширина колонок, высота строк)
Tk не масштабирует — их пропускаем через px().
"""

from __future__ import annotations

_factor = 1.0
_enabled = False


def enable() -> None:
    """Объявить процесс DPI-aware (system aware). Вызывать до первого tk.Tk()."""
    global _enabled
    if _enabled:
        return
    _enabled = True
    try:
        from ctypes import windll
    except ImportError:
        return
    try:
        windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        return
    except Exception:  # noqa: BLE001 — Windows 7/8 без shcore
        pass
    try:
        windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def init(root) -> float:
    """Запомнить коэффициент масштаба по уже созданному root."""
    global _factor
    try:
        _factor = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:  # noqa: BLE001
        _factor = 1.0
    return _factor


def factor() -> float:
    return _factor


def px(*values):
    """px(10) → 20 при 200 %; px(10, 5) → (20, 10) — удобно для pady=(…)."""
    if len(values) == 1 and isinstance(values[0], (tuple, list)):
        values = tuple(values[0])
    out = tuple(int(round(v * _factor)) for v in values)
    return out[0] if len(out) == 1 else out


def size(w: int, h: int) -> str:
    """Строка геометрии окна 'ШxВ' в масштабе."""
    return f"{px(w)}x{px(h)}"
