"""Межпроцессная синхронизация на объектах Windows (ctypes, без зависимостей).

Фоновый монитор держит именованный мьютекс (один экземпляр на сеанс) и
слушает именованное событие «остановись». Обновление и удаление программы
поднимают это событие и ждут, пока монитор выйдет и отпустит exe.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

MONITOR_MUTEX = "Local\\SynoTorrentBridge.Monitor"
MONITOR_STOP_EVENT = "Local\\SynoTorrentBridge.MonitorStop"

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = wt.HANDLE
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_k32.OpenMutexW.restype = wt.HANDLE
_k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_k32.CreateEventW.restype = wt.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_k32.SetEvent.argtypes = [wt.HANDLE]
_k32.ResetEvent.argtypes = [wt.HANDLE]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.WaitForSingleObject.restype = wt.DWORD
_k32.CloseHandle.argtypes = [wt.HANDLE]


class MonitorLock:
    """Мьютекс единственного экземпляра монитора + событие остановки."""

    def __init__(self):
        self.mutex = None
        self.event = None

    def acquire(self) -> bool:
        """True — мы единственный монитор; False — монитор уже запущен."""
        h = _k32.CreateMutexW(None, False, MONITOR_MUTEX)
        if not h:
            return True  # не смогли создать — не мешаем работе
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            _k32.CloseHandle(h)
            return False
        self.mutex = h
        # manual-reset событие, изначально не поднято
        self.event = _k32.CreateEventW(None, True, False, MONITOR_STOP_EVENT)
        return True

    def stop_requested(self) -> bool:
        return bool(self.event) and _k32.WaitForSingleObject(self.event, 0) == WAIT_OBJECT_0

    def release(self) -> None:
        for h in (self.event, self.mutex):
            if h:
                _k32.CloseHandle(h)
        self.event = self.mutex = None


def monitor_running() -> bool:
    h = _k32.OpenMutexW(SYNCHRONIZE, False, MONITOR_MUTEX)
    if not h:
        return False
    _k32.CloseHandle(h)
    return True


def stop_monitor(timeout: float = 15.0) -> bool:
    """Просит запущенный монитор завершиться и ждёт этого.

    Возвращает True, если монитор был запущен (и теперь остановлен
    или не успел за timeout — тогда в журнал пишет вызывающий)."""
    if not monitor_running():
        return False
    ev = _k32.CreateEventW(None, True, False, MONITOR_STOP_EVENT)
    try:
        if ev:
            _k32.SetEvent(ev)
        deadline = time.time() + timeout
        while time.time() < deadline and monitor_running():
            time.sleep(0.2)
    finally:
        if ev:
            # сбрасываем, чтобы новый монитор не вышел сразу после старта
            _k32.ResetEvent(ev)
            _k32.CloseHandle(ev)
    return True
