"""Выбор файлов magnet: у задачи НИКОГДА не должно оставаться ноль нужных файлов.

Download Station переводит задачу без единого wanted-файла в «Проверку»
(статус 6), из которой она сама не выходит (проверено на DS 4.x / DSM 7).
Тест гоняет настоящие AddTorrentDialog._unwant_then_dialog и
MagnetFilesDialog._ok против фальшивого клиента, который следит за wanted.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import syno_bridge.gui as g
from syno_bridge.bencode import TorrentMeta

warnings = []
g.messagebox.showwarning = lambda *a, **k: warnings.append(a)
g.messagebox.showinfo = lambda *a, **k: None
g.messagebox.showerror = lambda *a, **k: warnings.append(a)
g.save_config = lambda cfg: None
g.load_config = lambda: {"last_destination": "", "recent_destinations": []}

FAILED = []


def check(name, cond, extra=""):
    print(("[OK ] " if cond else "[FAIL] ") + name + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeClient:
    supports_file_selection = True

    def __init__(self, files):
        self.wanted = {f["id"]: True for f in files}
        self.min_wanted = len(files)
        self.calls = []
        self.unstick = []

    def set_files_skip(self, task_id, ids, skip=True):
        self.calls.append((list(ids), skip))
        for i in ids:
            self.wanted[i] = not skip
        self.min_wanted = min(self.min_wanted, sum(self.wanted.values()))
        return True

    def unstick_task(self, task_id, observe=6.0):
        self.unstick.append(task_id)
        return False

    def logout(self, timeout=None):
        pass

    def delete_task(self, task_id):
        self.calls.append(("delete", task_id))


FILES = [{"id": 0, "name": "Sintel/Sintel.mp4", "size": 129_000_000},
         {"id": 1, "name": "Sintel/Sintel.en.srt", "size": 1514},
         {"id": 2, "name": "Sintel/Sintel.de.srt", "size": 1652},
         {"id": 3, "name": "Sintel/poster.jpg", "size": 46_000}]


def run_case(files, pick_names):
    """Полный путь «метаданные пришли → снятие wanted → диалог → OK»."""
    root = g.setup_root()
    client = FakeClient(files)
    meta = TorrentMeta(name="Sintel", is_magnet=True)
    add = g.AddTorrentDialog(root, client, meta, magnet_uri="magnet:?xt=urn:btih:" + "0" * 40)
    seen = {}
    orig = g.MagnetFilesDialog

    class AutoDialog(orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen["keep_id"] = self.keep_id
            seen["wanted_during_dialog"] = sum(client.wanted.values())
            for f in self.meta.files:
                f.selected = f.path.rsplit("/", 1)[-1] in pick_names
            self.win.after(200, self._ok)

    g.MagnetFilesDialog = AutoDialog
    try:
        root.after(100, lambda: add._unwant_then_dialog(files, "T1", "Downloads"))
        root.after(15000, root.quit)
        root.mainloop()
    finally:
        g.MagnetFilesDialog = orig
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    return client, seen


# 1) многофайловая раздача, выбраны видео и субтитры
c, seen = run_case(FILES, {"Sintel.mp4", "Sintel.de.srt"})
check("на время выбора оставлен самый маленький файл", seen.get("keep_id") == 1, str(seen))
check("во время диалога включён ровно 1 файл", seen.get("wanted_during_dialog") == 1)
check("ни на миг ноль нужных файлов", c.min_wanted >= 1, str(c.calls))
check("в итоге включены ровно выбранные",
      {i for i, w in c.wanted.items() if w} == {0, 2}, str(c.wanted))
check("временный файл выключен последним", c.calls[-1] == ([1], True), str(c.calls))
check("после выбора — проверка «Проверки»", c.unstick == ["T1"])

# 2) выбран только тот самый маленький файл
c, seen = run_case(FILES, {"Sintel.en.srt"})
check("только маленький файл: ноль нужных не было", c.min_wanted >= 1, str(c.calls))
check("только маленький файл: включён он один",
      {i for i, w in c.wanted.items() if w} == {1}, str(c.wanted))

# 3) раздача из одного файла — wanted не трогаем вовсе
one = [{"id": 0, "name": "silo.s01e08.mkv", "size": 3_500_000_000}]
c, seen = run_case(one, {"silo.s01e08.mkv"})
check("один файл: wanted не снимается", c.calls == [] and c.min_wanted == 1, str(c.calls))

check("без предупреждений и ошибок", not warnings, str(warnings))
print()
if FAILED:
    print("ПРОВАЛЕНО:", FAILED)
    sys.exit(1)
print("Выбор файлов magnet: всё в порядке ✓")
