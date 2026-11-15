"""Тесты SynoTorrentBridge: bencode, DPAPI, клиент API против mock-сервера,
монитор, обновление, реестр, удаление, журнал.

Запуск: python tests\\test_all.py (только стандартная библиотека, Windows).
Реальные настройки, реестр перехвата и запущенный монитор не трогаются:
тесты работают в своих ветках реестра и со своими именами объектов Windows.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syno_bridge import bencode
from syno_bridge.config import encrypt_password, decrypt_password
from syno_bridge.syno_api import SynoClient, SynoError
from syno_bridge import updater, winsync

FAILED = []

# объекты Windows для тестов — не пересекаются с настоящим монитором
winsync.MONITOR_MUTEX = "Local\\SynoTorrentBridge.Test.Monitor"
winsync.MONITOR_STOP_EVENT = "Local\\SynoTorrentBridge.Test.MonitorStop"

PING = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PING.EXE")
HASH_A = "0123456789abcdef0123456789abcdef01234567"


def check(name, cond, extra=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def run_locked_exe(path: str, seconds: int = 6) -> subprocess.Popen:
    """Копия PING.EXE, запущенная на seconds секунд: настоящий «занятый» exe."""
    shutil.copyfile(PING, path)
    return subprocess.Popen([path, "-n", str(seconds), "127.0.0.1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------- bencode

def test_bencode():
    torrent = {
        b"announce": b"http://tracker.example/announce",
        b"info": {
            b"name": "Test Razdacha 2024",
            b"piece length": 262144,
            b"pieces": b"x" * 40,
            b"files": [
                {b"length": 1234567, b"path": [b"Film", b"film.mkv"]},
                {b"length": 5000, b"path": [b"Subs", "Русские субтитры.srt".encode("utf-8")]},
                {b"length": 100, b"path": [b"readme.txt"]},
            ],
        },
    }
    data = bencode.dumps(torrent)
    meta = bencode.parse_torrent(data)
    check("torrent: имя", meta.name == "Test Razdacha 2024", meta.name)
    check("torrent: 3 файла", len(meta.files) == 3)
    paths = sorted(f.path for f in meta.files)
    check("torrent: пути", paths == ["Film/film.mkv", "Subs/Русские субтитры.srt", "readme.txt"], str(paths))
    check("torrent: размер", meta.total_size == 1234567 + 5000 + 100)
    check("torrent: info_hash", len(meta.info_hash) == 40)

    single = {b"info": {b"name": b"one.bin", b"piece length": 1, b"pieces": b"y" * 20, b"length": 42}}
    m2 = bencode.parse_torrent(bencode.dumps(single))
    check("single-file torrent", m2.files[0].path == "one.bin" and m2.total_size == 42)

    # info-hash считается по исходным байтам: ключи здесь НЕ отсортированы
    raw_info = b"d4:name1:x6:lengthi1e12:piece lengthi1e6:pieces20:" + b"z" * 20 + b"e"
    raw = b"d8:announce3:abc4:info" + raw_info + b"e"
    m3 = bencode.parse_torrent(raw)
    check("info_hash по исходным байтам",
          m3.info_hash == hashlib.sha1(raw_info).hexdigest(), m3.info_hash)

    check("magnet dn", bencode.magnet_display_name(
        "magnet:?xt=urn:btih:abc&dn=Some%20Release%202024") == "Some Release 2024")
    check("magnet hash hex", bencode.magnet_info_hash(
        f"magnet:?xt=urn:btih:{HASH_A.upper()}&dn=x") == HASH_A)
    b32 = base64.b32encode(bytes.fromhex(HASH_A)).decode()
    check("magnet hash base32", bencode.magnet_info_hash(f"magnet:?xt=urn:btih:{b32}") == HASH_A)
    check("magnet без хеша", bencode.magnet_info_hash("magnet:?dn=x") == "")
    check("human_size", bencode.human_size(1536) == "1.5 КБ", bencode.human_size(1536))


# ------------------------------------------------------------- DPAPI

def test_dpapi():
    enc = encrypt_password("Sekret#123")
    check("dpapi: зашифровано", enc and enc != "Sekret#123")
    check("dpapi: расшифровка", decrypt_password(enc) == "Sekret#123")
    check("dpapi: пустой", decrypt_password("") == "")


# ------------------------------------------------------------- mock NAS

def default_tasks():
    return [{"id": "TID-URL-1", "title": "Some Release 2024", "type": "bt", "status": 2,
             "additional": {"detail": {"uri": f"magnet:?xt=urn:btih:{HASH_A}&dn=Some"}}}]


class MockNAS(BaseHTTPRequestHandler):
    captured = {}
    task_status = {}          # id -> статус для Task get
    tasks = default_tasks()
    dirs = {"/downloads", "/video", "/video/films"}

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, q):
        if q.get("account") == ["otpuser"]:
            # имитация NAS с включённой 2FA
            if q.get("device_id") == ["DEVID123"]:
                # доверенное устройство — вход без кода
                self._json({"success": True, "data": {"sid": "DEVSID"}})
            elif q.get("otp_code") == ["123456"]:
                data = {"sid": "OTPSID"}
                if q.get("enable_device_token") == ["yes"]:
                    data["device_id"] = "DEVID123"  # auth v7 отдаёт device_id, не did
                self._json({"success": True, "data": data})
            else:
                self._json({"success": False, "error": {"code": 403}})
        elif q.get("account") == ["user"] and q.get("passwd") == ["pass"]:
            self._json({"success": True, "data": {"sid": "TESTSID123"}})
        else:
            self._json({"success": False, "error": {"code": 400}})

    def _task_list(self, q):
        limit = int(q.get("limit", ["100"])[0])
        offset = int(q.get("offset", ["0"])[0])
        with_detail = "detail" in q.get("additional", [""])[0]
        page = []
        for t in MockNAS.tasks[offset:offset + limit]:
            t = dict(t)
            if not with_detail:
                t.pop("additional", None)
            page.append(t)
        MockNAS.captured.setdefault("task_list_calls", []).append((offset, limit, with_detail))
        self._json({"success": True, "data": {"total": len(MockNAS.tasks), "offset": offset,
                                              "task": page}})

    def _common(self, q, http_method):
        api = q.get("api", [""])[0]
        method = q.get("method", [""])[0]
        if api == "SYNO.API.Auth" and method == "login":
            MockNAS.captured["login_method"] = http_method
            MockNAS.captured["login_query_has_passwd"] = "passwd=" in self.path
            self._auth(q)
        elif api == "SYNO.API.Auth" and method == "logout":
            self._json({"success": True, "data": {}})
        elif api == "SYNO.Test.Slow":
            time.sleep(2.5)
            try:
                self._json({"success": True, "data": {}})
            except OSError:
                pass  # клиент уже отключился по тайм-ауту — так и задумано
        elif api == "SYNO.FileStation.List" and method == "list_share":
            self._json({"success": True, "data": {"shares": [
                {"name": "downloads", "path": "/downloads", "isdir": True},
                {"name": "video", "path": "/video", "isdir": True}]}})
        elif api == "SYNO.FileStation.List" and method == "list":
            self._json({"success": True, "data": {"files": [
                {"name": "films", "path": "/video/films", "isdir": True}]}})
        elif api == "SYNO.FileStation.List" and method == "getinfo":
            path = json.loads(q.get("path", ["[]"])[0])[0]
            if path in MockNAS.dirs:
                f = {"isdir": True, "name": path.rsplit("/", 1)[-1], "path": path}
            else:
                f = {"code": 408, "path": path}
            self._json({"success": True, "data": {"files": [f]}})
        elif api == "SYNO.FileStation.CreateFolder":
            MockNAS.captured["create_folder"] = q
            self._json({"success": True, "data": {}})
        elif api == "SYNO.DownloadStation2.Task.BT.File" and method == "list":
            self._json({"success": True, "data": {"total": 3, "items": [
                {"id": 0, "name": "Test Razdacha 2024/Film/film.mkv", "size": 1234567, "priority": "normal"},
                {"id": 1, "name": "Test Razdacha 2024/Subs/Русские субтитры.srt", "size": 5000, "priority": "normal"},
                {"id": 2, "name": "Test Razdacha 2024/readme.txt", "size": 100, "priority": "normal"}]}})
        elif api == "SYNO.DownloadStation2.Task" and method == "list":
            self._task_list(q)
        elif api == "SYNO.DownloadStation2.Task" and method == "get":
            tid = json.loads(q.get("id", ["[]"])[0])[0]
            self._json({"success": True, "data": {"task": [
                {"id": tid, "status": MockNAS.task_status.get(tid, 2),
                 "additional": {"transfer": {"size_downloaded": 0}}}]}})
        elif api == "SYNO.DownloadStation2.Task.List" and method == "get":
            self._json({"success": True, "data": {"files": [
                {"index": 0, "name": "Test Razdacha 2024/Film/film.mkv", "size": 1234567},
                {"index": 1, "name": "Test Razdacha 2024/Subs/Русские субтитры.srt", "size": 5000},
                {"index": 2, "name": "Test Razdacha 2024/readme.txt", "size": 100}]}})
        else:
            return False
        return True

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        api = q.get("api", [""])[0]
        if api == "SYNO.API.Info":
            self._json({"success": True, "data": {
                "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7},
                "SYNO.DownloadStation2.Task": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.DownloadStation2.Task.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.DownloadStation2.Task.BT.File": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.FileStation.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.FileStation.CreateFolder": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
            }})
        elif not self._common(q, "GET"):
            self._json({"success": False, "error": {"code": 102}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            text = body.decode("utf-8", errors="replace")
            MockNAS.captured["multipart"] = text
            if "SYNO.DownloadStation2.Task" in text and "create" in text:
                if 'name="create_list"\r\n\r\ntrue' in text:
                    self._json({"success": True, "data": {"list_id": ["LIST-FILE-1"]}})
                else:
                    self._json({"success": True, "data": {"task_id": ["TID-FILE-1"]}})
                return
            self._json({"success": False, "error": {"code": 101}})
            return
        q = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
        api = q.get("api", [""])[0]
        method = q.get("method", [""])[0]
        if self._common(q, "POST"):
            return
        if api == "SYNO.DownloadStation2.Task" and method == "create":
            MockNAS.captured["create"] = q
            if "realmagnet" in q.get("url", [""])[0]:
                # как живой NAS: magnet игнорирует create_list, задача создаётся сразу
                self._json({"success": True, "data": {"list_id": [], "task_id": ["TID-MAG-1"]}})
            elif q.get("create_list") == ["true"]:
                self._json({"success": True, "data": {"list_id": ["LIST-URL-1"]}})
            else:
                self._json({"success": True, "data": {"task_id": ["TID-URL-1"]}})
        elif api == "SYNO.DownloadStation2.Task.List" and method == "download":
            MockNAS.captured["list_download"] = q
            self._json({"success": True, "data": {"task_id": ["TID-LIST-1"]}})
        elif api == "SYNO.DownloadStation2.Task.List.Polling" and method == "download":
            MockNAS.captured["list_polling_download"] = q
            self._json({"success": True,
                        "data": {"task_id": "admin/SYNODLTaskListDownload1"}})
        elif api == "SYNO.DownloadStation2.Task.List" and method == "delete":
            MockNAS.captured["list_delete"] = q
            self._json({"success": True, "data": {}})
        elif api == "SYNO.DownloadStation2.Task" and method in ("pause", "resume", "delete"):
            MockNAS.captured[method] = q
            MockNAS.captured.setdefault("actions", []).append(method)
            for tid in json.loads(q.get("id", ["[]"])[0]) if q.get("id", [""])[0].startswith("[") else q.get("id", []):
                if method == "pause":
                    MockNAS.task_status[tid] = 3
                elif method == "resume":
                    MockNAS.task_status[tid] = 2
            self._json({"success": True, "data": {}})
        elif api == "SYNO.DownloadStation2.Task.BT.File" and method == "set":
            MockNAS.captured["bt_set"] = q
            self._json({"success": True, "data": {}})
        else:
            self._json({"success": False, "error": {"code": 103}})


def start_mock():
    MockNAS.tasks = default_tasks()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockNAS)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_client():
    server, port = start_mock()

    c = SynoClient("127.0.0.1", port, use_https=False, username="user", password="pass")
    try:
        c.login()
        check("auth: sid", c.sid == "TESTSID123")
        check("auth: вход POST-запросом", MockNAS.captured.get("login_method") == "POST")
        check("auth: пароля нет в строке запроса", not MockNAS.captured.get("login_query_has_passwd"))
        check("discover: DS2", c.ds2)
        check("discover: выбор файлов", c.supports_file_selection)

        tid = c.create_task_url("magnet:?xt=urn:btih:abc&dn=Test", "downloads/films")
        check("create url: task_id", tid == "TID-URL-1")
        create_q = MockNAS.captured.get("create", {})
        check("create url: destination", create_q.get("destination") == ['"downloads/films"'],
              str(create_q.get("destination")))
        check("create url: create_list=false", create_q.get("create_list") == ["false"])

        tid2 = c.create_task_file(bencode.dumps({b"info": {b"name": b"x", b"piece length": 1,
                                                           b"pieces": b"z" * 20, b"length": 1}}),
                                  "x.torrent", "downloads")
        check("create file: task_id", tid2 == "TID-FILE-1")
        mp = MockNAS.captured.get("multipart", "")
        check("multipart: поле torrent", 'name="torrent"' in mp and "x.torrent" in mp)
        check("multipart: type=file", 'name="type"\r\n\r\n"file"' in mp)

        files = c.list_bt_files("TID-URL-1")
        check("bt files: 3 шт.", len(files) == 3, str(len(files)))
        check("bt files: имена", files[0]["name"].endswith("film.mkv"))

        ok = c.set_files_skip("TID-URL-1", [1, 2], skip=True)
        check("bt set skip: ok", ok)
        bs = MockNAS.captured.get("bt_set", {})
        check("bt set skip: index", bs.get("index") == ["[1, 2]"], str(bs.get("index")))
        check("bt set skip: wanted", bs.get("wanted") == ["false"], str(bs.get("wanted")))

        c.pause_task("TID-URL-1")
        check("pause вызван", "pause" in MockNAS.captured)
        c.resume_task("TID-URL-1")
        check("resume вызван", "resume" in MockNAS.captured)

        shares = c.list_shares()
        check("shares", [s["name"] for s in shares] == ["downloads", "video"])
        dirs = c.list_dirs("/video")
        check("dirs", dirs[0]["path"] == "/video/films")
        c.create_folder("/video", "newdir")

        # --- папка назначения
        check("folder_exists: есть", c.folder_exists("video/films") is True)
        check("folder_exists: нет", c.folder_exists("video/nope") is False)
        c.create_path("video/new/sub")
        cf = MockNAS.captured.get("create_folder", {})
        check("create_path: родитель и имя",
              cf.get("folder_path") == ["/video/new"] and cf.get("name") == ["sub"], str(cf))
        check("create_path: force_parent", cf.get("force_parent") == ["true"])
        try:
            c.create_path("noshare")
            check("create_path: общую папку не создаём", False)
        except SynoError:
            check("create_path: общую папку не создаём", True)

        # --- дубликаты: только точное имя или хеш, не подстрока
        check("duplicate: точное имя", c.find_duplicate(title="Some Release 2024") == "Some Release 2024")
        check("duplicate: подстрока — не дубль", c.find_duplicate(title="Some Release") == "")
        check("duplicate: по хешу magnet", c.find_duplicate(info_hash=HASH_A.upper()) != "")
        check("duplicate: чужой хеш", c.find_duplicate(info_hash="f" * 40) == "")

        # --- новая задача ищется только среди появившихся после снимка
        before = c.task_ids()
        check("снимок задач", before == {"TID-URL-1"}, str(before))
        check("новой задачи нет — старую не берём",
              c.find_new_task(before, "", "Some Release 2024") == "")
        MockNAS.tasks = default_tasks() + [{"id": "TID-NEW", "title": "Some Release 2024",
                                            "status": 1, "type": "bt",
                                            "additional": {"detail": {"uri": "x.torrent"}}}]
        check("новая задача с тем же именем — берём новую",
              c.find_new_task(before, "", "Some Release 2024") == "TID-NEW")

        # --- постраничный список задач
        MockNAS.tasks = [{"id": f"T{i}", "title": f"t{i}", "status": 2} for i in range(250)]
        MockNAS.captured["task_list_calls"] = []
        all_tasks = c.list_tasks(page=100)
        check("list_tasks: все 250 задач", len(all_tasks) == 250, str(len(all_tasks)))
        check("list_tasks: 3 страницы", len(MockNAS.captured["task_list_calls"]) == 3)
        MockNAS.tasks = default_tasks()

        # --- тайм-аут превращается в SynoError с понятным текстом
        slow = SynoClient("127.0.0.1", port, use_https=False, username="user",
                          password="pass", timeout=1)
        slow.sid = "x"
        try:
            slow._api("SYNO.Test.Slow", 1, "get")
            check("тайм-аут → SynoError", False)
        except SynoError as e:
            check("тайм-аут → SynoError", "тайм-аут" in str(e), str(e))
        except Exception as e:  # noqa: BLE001
            check("тайм-аут → SynoError", False, f"{type(e).__name__}: {e}")

        # --- поток create_list (как в DS get) ---
        lid = c.create_pending_list_url("magnet:?xt=urn:btih:abc&dn=Test", "downloads/films")
        check("pending url: list_id", lid == "LIST-URL-1", lid)
        create_q = MockNAS.captured.get("create", {})
        check("pending url: create_list=true", create_q.get("create_list") == ["true"])

        lid2 = c.create_pending_list_file(
            bencode.dumps({b"info": {b"name": b"x", b"piece length": 1,
                                     b"pieces": b"z" * 20, b"length": 1}}),
            "x.torrent", "downloads")
        check("pending file: list_id", lid2 == "LIST-FILE-1", lid2)

        pfiles = c.get_pending_files(lid)
        check("pending files: 3 шт.", len(pfiles) == 3, str(len(pfiles)))
        check("pending files: индексы/имена",
              pfiles[0]["index"] == 0 and pfiles[1]["name"].endswith("Русские субтитры.srt"))

        tid3 = c.pending_download(lid, "downloads/films", [0, 1])
        check("pending download: task_id", tid3 == "TID-LIST-1", tid3)
        ld = MockNAS.captured.get("list_download", {})
        check("pending download: index", ld.get("index") == ["[0, 1]"], str(ld.get("index")))
        check("pending download: destination", ld.get("destination") == ['"downloads/films"'],
              str(ld.get("destination")))

        c.pending_delete(lid)
        check("pending delete: вызван",
              MockNAS.captured.get("list_delete", {}).get("list_id") == [lid])

        # Task.List.Polling download с selected — выбор на стороне NAS
        ok = c.pending_download_selected(lid, "downloads/films", [0, 1])
        check("polling selected: ok", ok)
        pd = MockNAS.captured.get("list_polling_download", {})
        check("polling selected: selected", pd.get("selected") == ["[0, 1]"],
              str(pd.get("selected")))
        check("polling selected: destination", pd.get("destination") == ['"downloads/films"'])

        # magnet: create_list игнорируется NAS — задача создаётся сразу,
        # task_id надо переиспользовать, иначе будет дубль
        lid3, tid4 = c.create_url_or_list("magnet:?xt=urn:btih:realmagnet", "downloads")
        check("magnet: list_id пуст", lid3 == "", repr(lid3))
        check("magnet: task_id переиспользуется", tid4 == "TID-MAG-1", tid4)

        # «Проверка» (статус 6) после смены wanted — один толчок паузой/возобновлением
        MockNAS.task_status = {"T-STUCK": 6, "T-OK": 2}
        MockNAS.captured["actions"] = []
        check("unstick: нормальную задачу не трогаем", c.unstick_task("T-OK", observe=1) is False
              and MockNAS.captured["actions"] == [])
        check("unstick: застрявшую толкаем", c.unstick_task("T-STUCK", observe=1.5) is True)
        check("unstick: пауза, затем возобновление",
              MockNAS.captured["actions"] == ["pause", "resume"], str(MockNAS.captured["actions"]))
        check("unstick: задача снова качается", c.task_status("T-STUCK")[0] == 2)

        # неверный пароль
        bad = SynoClient("127.0.0.1", port, use_https=False, username="user", password="wrong")
        try:
            bad.login()
            check("auth: ошибка пароля", False)
        except Exception as e:
            check("auth: ошибка пароля", "400" in str(e) or "парол" in str(e).lower(), str(e))

        # 2FA: без кода — ошибка 403, с кодом — успех
        c2 = SynoClient("127.0.0.1", port, use_https=False, username="otpuser", password="x")
        try:
            c2.login()
            check("2fa: требует код", False)
        except Exception as e:
            check("2fa: требует код", getattr(e, "code", None) == 403, str(e))
        c3 = SynoClient("127.0.0.1", port, use_https=False, username="otpuser", password="x",
                        otp_code="123456")
        c3.login()
        check("2fa: вход с кодом", c3.sid == "OTPSID")
        check("2fa: получен device token", c3.device_id == "DEVID123", c3.device_id)

        # повторный вход с device token — без кода
        c4 = SynoClient("127.0.0.1", port, use_https=False, username="otpuser", password="x",
                        device_id=c3.device_id)
        c4.login()
        check("2fa: вход по device token без кода", c4.sid == "DEVSID")

        # отозванный device token — NAS снова требует код (403)
        c5 = SynoClient("127.0.0.1", port, use_https=False, username="otpuser", password="x",
                        device_id="REVOKE")
        try:
            c5.login()
            check("2fa: отозванный токен → 403", False)
        except Exception as e:
            check("2fa: отозванный токен → 403", getattr(e, "code", None) == 403, str(e))

        c.logout()
    finally:
        server.shutdown()


# ------------------------------------------------------------- монитор

def test_monitor():
    from syno_bridge import monitor

    check("переход 2→8 (докачалась и раздаётся) = завершение", monitor.is_finish_transition(2, 8))
    check("переход 2→5 = завершение", monitor.is_finish_transition(2, 5))
    check("переход 8→5 — не новое завершение", not monitor.is_finish_transition(8, 5))
    check("новая задача сразу 5 — без тоста", not monitor.is_finish_transition(None, 5))

    toasts = []
    orig_toast, orig_save = monitor.show_toast, monitor._save_state
    monitor.show_toast = lambda _root, _title, text, **_k: toasts.append(text)
    monitor._save_state = lambda _s: None
    try:
        class FakeClient:
            tasks = []

            def list_tasks(self):
                return self.tasks

        fc = FakeClient()
        m = monitor.Monitor.__new__(monitor.Monitor)
        m.root, m.cfg, m.client = None, {}, fc
        m.state, m.snapshot_done, m.otp_warned = {}, False, False
        fc.tasks = [{"id": "a", "title": "A", "status": 2},
                    {"id": "b", "title": "B", "status": 2},
                    {"id": "c", "title": "C", "status": 8}]
        m._poll()  # первый опрос — только снимок
        check("монитор: первый опрос без тостов", toasts == [])
        fc.tasks = [{"id": "a", "title": "A", "status": 8},
                    {"id": "b", "title": "B", "status": 5},
                    {"id": "c", "title": "C", "status": 5}]
        m._poll()
        check("монитор: тосты для A (раздача) и B", sorted(toasts) == ["A", "B"], str(toasts))
    finally:
        monitor.show_toast, monitor._save_state = orig_toast, orig_save


# ------------------------------------------------------------- winsync

def test_winsync():
    script = (
        "import sys, time; sys.path.insert(0, sys.argv[1]);"
        "from syno_bridge import winsync;"
        "winsync.MONITOR_MUTEX = sys.argv[2]; winsync.MONITOR_STOP_EVENT = sys.argv[3];"
        "l = winsync.MonitorLock(); ok = l.acquire(); print('ready' if ok else 'busy', flush=True);"
        "t = time.time()\n"
        "while ok and not l.stop_requested() and time.time() - t < 20: time.sleep(0.1)\n"
        "l.release()"
    )
    p = subprocess.Popen([sys.executable, "-c", script, str(ROOT), winsync.MONITOR_MUTEX,
                          winsync.MONITOR_STOP_EVENT], stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline().strip()
    check("winsync: монитор захватил мьютекс", line == "ready", line)
    check("winsync: monitor_running", winsync.monitor_running())
    second = winsync.MonitorLock()
    check("winsync: второй монитор не запускается", second.acquire() is False)
    t0 = time.time()
    check("winsync: stop_monitor сообщил о запущенном", winsync.stop_monitor(timeout=10))
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    check("winsync: монитор вышел по сигналу", p.returncode == 0 and time.time() - t0 < 8)
    check("winsync: мьютекс освобождён", not winsync.monitor_running())
    fresh = winsync.MonitorLock()
    check("winsync: новый монитор стартует", fresh.acquire() is True)
    check("winsync: событие сброшено для нового монитора", not fresh.stop_requested())
    fresh.release()
    check("winsync: stop без монитора → False", winsync.stop_monitor(timeout=1) is False)


# ------------------------------------------------------------- updater

def test_updater():
    import io
    from unittest import mock

    check("updater: parse_version v1.4.0", updater.parse_version("v1.4.0") == (1, 4, 0))
    check("updater: parse_version 1.10.2 > 1.9.9",
          updater.parse_version("1.10.2") > updater.parse_version("1.9.9"))
    check("updater: parse_version с суффиксом",
          updater.parse_version("v2.0.0-beta") == (2, 0, 0))

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def payload(tag, assets):
        return FakeResp(json.dumps({"tag_name": tag, "assets": assets}).encode())

    exe_asset = [{"name": "other.exe", "browser_download_url": "https://example/other.exe"},
                 {"name": "SynoTorrentBridge.exe",
                  "browser_download_url": "https://example/SynoTorrentBridge.exe",
                  "digest": "sha256:" + "AB" * 32, "size": 123456}]

    with mock.patch.object(updater, "__version__", "1.4.0"), \
         mock.patch.object(updater.urllib.request, "urlopen",
                           return_value=payload("v1.5.0", exe_asset)):
        info = updater.check_latest("owner/repo")
        check("updater: новая версия найдена",
              info is not None and info["tag"] == "v1.5.0"
              and info["url"].endswith("/SynoTorrentBridge.exe"), str(info))
        check("updater: sha256 из digest", info and info["sha256"] == "ab" * 32)
        check("updater: размер из релиза", info and info["size"] == 123456)

    with mock.patch.object(updater, "__version__", "1.4.0"), \
         mock.patch.object(updater.urllib.request, "urlopen",
                           return_value=payload("v1.4.0", exe_asset)):
        check("updater: та же версия → None", updater.check_latest("owner/repo") is None)

    with mock.patch.object(updater, "__version__", "1.4.0"), \
         mock.patch.object(updater.urllib.request, "urlopen",
                           return_value=payload("v1.3.9", exe_asset)):
        check("updater: старая версия → None", updater.check_latest("owner/repo") is None)

    with mock.patch.object(updater, "__version__", "1.4.0"), \
         mock.patch.object(updater.urllib.request, "urlopen",
                           return_value=payload("v1.5.0", [{"name": "source.zip"}])):
        check("updater: нет exe в релизе → None", updater.check_latest("owner/repo") is None)

    check("updater: пустой repo → None", updater.check_latest("") is None)

    # download: проверка SHA-256 и размера
    tmp = tempfile.mkdtemp(prefix="syno_upd_test_")
    blob = os.urandom(200_000)
    good = hashlib.sha256(blob).hexdigest()
    dst = os.path.join(tmp, "dl.exe")
    with mock.patch.object(updater.urllib.request, "urlopen", return_value=FakeResp(blob)):
        updater.download("https://x", dst, sha256=good, size=len(blob))
    check("download: верная сумма — файл на месте", open(dst, "rb").read() == blob)
    with mock.patch.object(updater.urllib.request, "urlopen", return_value=FakeResp(blob)):
        try:
            updater.download("https://x", dst, sha256="0" * 64)
            check("download: неверная сумма → ошибка", False)
        except OSError:
            check("download: неверная сумма → ошибка", True)
    check("download: файл с неверной суммой удалён", not os.path.exists(dst))
    with mock.patch.object(updater.urllib.request, "urlopen", return_value=FakeResp(blob)):
        try:
            updater.download("https://x", dst, sha256=good, size=len(blob) + 1)
            check("download: неверный размер → ошибка", False)
        except OSError:
            check("download: неверный размер → ошибка", True)

    # _pid_alive: текущий процесс жив, несуществующий — нет
    check("updater: _pid_alive(себя)", updater._pid_alive(os.getpid()))
    check("updater: _pid_alive(мёртвый pid)", not updater._pid_alive(0x7FFFFFF0))

    # replace_exe: target ЗАПУЩЕН (как монитор из того же exe) — замена всё равно проходит
    target = os.path.join(tmp, "Syno (2).exe")
    proc = run_locked_exe(target, seconds=8)
    time.sleep(0.5)
    try:
        shutil.copyfile(sys.executable, target)
        locked = False
    except OSError:
        locked = True
    check("replace_exe: запущенный exe действительно не перезаписать", locked)
    new_src = os.path.join(tmp, "new.bin")
    with open(new_src, "wb") as f:
        f.write(b"NEW VERSION" * 1000)
    try:
        updater.replace_exe(new_src, target, attempts=3)
        check("replace_exe: заменил запущенный exe", open(target, "rb").read() == open(new_src, "rb").read())
    except OSError as e:
        check("replace_exe: заменил запущенный exe", False, str(e))
    check("replace_exe: старый exe отодвинут в .old", os.path.exists(updater.old_path(target)))
    check("replace_exe: старый процесс продолжает работать", proc.poll() is None)
    updater.cleanup_leftovers(target)
    check("cleanup: .old занят — пропущен без ошибки", os.path.exists(updater.old_path(target)))
    proc.kill()
    proc.wait()
    time.sleep(0.5)
    updater.cleanup_leftovers(target)
    check("cleanup: .old удалён после выхода процесса", not os.path.exists(updater.old_path(target)))
    check("cleanup: сам exe не тронут", os.path.exists(target))

    # apply_update: ждёт смерти pid, ставит себя на место target, handshake чистится
    target = os.path.join(tmp, "старая версия (2).exe")
    with open(target, "wb") as f:
        f.write(b"OLD")
    hs = os.path.join(tmp, "hs.ok")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])

    done = {}

    def run_apply():
        try:
            updater.apply_update(target, sleeper.pid, hs, launch=False)
            done["ok"] = True
        except Exception as e:  # noqa: BLE001
            done["err"] = e

    t0 = time.time()
    th = threading.Thread(target=run_apply, daemon=True)
    th.start()
    th.join(timeout=30)
    data = open(target, "rb").read()
    check("updater: apply_update дождался смерти старого", time.time() - t0 >= 1.2)
    check("updater: apply_update заменил target", data == open(sys.executable, "rb").read())
    check("updater: apply_update без ошибок", done.get("ok") is True, str(done.get("err", "")))
    check("updater: handshake удалён после применения", not os.path.exists(hs))
    check("updater: wait_handshake отрабатывает таймаут",
          not updater.wait_handshake(os.path.join(tmp, "нет_такого"), timeout=1))
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- реестр

def test_registry():
    import winreg
    from syno_bridge import registry_win as rw

    base = r"Software\SynoTorrentBridgeTest"
    saved = (rw.CLASSES, rw.BACKUP_KEY)
    rw.CLASSES = base + r"\Classes"
    rw.BACKUP_KEY = base + r"\App\Backup"
    try:
        rw._delete_tree(rw.HKCU, base)
        # «до нас» стоял другой клиент
        rw._set(rw.HKCU, rf"{rw.CLASSES}\magnet", "", "URL:Magnet link")
        rw._set(rw.HKCU, rf"{rw.CLASSES}\magnet\shell\open\command", "", '"qbittorrent.exe" "%1"')
        rw._set(rw.HKCU, rf"{rw.CLASSES}\.torrent", "", "qBittorrent.File.Torrent")
        rw._set(rw.HKCU, rf"{rw.CLASSES}\.torrent\OpenWithProgids", "qBittorrent.File.Torrent", "")

        rw.register()
        check("реестр: перехват включён", rw.is_registered())
        check("реестр: .torrent → наш ProgID",
              rw._get(rw.HKCU, rf"{rw.CLASSES}\.torrent") == rw.PROGID_TORRENT)
        rw.register()  # повторное включение не должно затереть копию
        rw.unregister()
        check("реестр: magnet вернулся к qBittorrent",
              rw._get(rw.HKCU, rf"{rw.CLASSES}\magnet\shell\open\command") == '"qbittorrent.exe" "%1"')
        check("реестр: .torrent вернулся к qBittorrent",
              rw._get(rw.HKCU, rf"{rw.CLASSES}\.torrent") == "qBittorrent.File.Torrent")
        check("реестр: чужой OpenWithProgids не удалён",
              rw._get(rw.HKCU, rf"{rw.CLASSES}\.torrent\OpenWithProgids",
                      "qBittorrent.File.Torrent") == "")
        check("реестр: наш ProgID удалён",
              rw._get(rw.HKCU, rf"{rw.CLASSES}\{rw.PROGID_TORRENT}") is None)
        check("реестр: перехват выключен", not rw.is_registered())

        # «до нас» ничего не было — после выключения ничего не остаётся
        rw._delete_tree(rw.HKCU, base)
        rw.register()
        rw.unregister()
        check("реестр: magnet удалён целиком",
              rw._get(rw.HKCU, rf"{rw.CLASSES}\magnet\shell\open\command") is None)
        try:
            winreg.OpenKey(rw.HKCU, rf"{rw.CLASSES}\.torrent").Close()
            torrent_left = True
        except OSError:
            torrent_left = False
        check("реестр: пустой .torrent удалён", not torrent_left)
        check("реестр: из исходников пути не чинятся", rw.repair_paths("0") == [])
    finally:
        rw._delete_tree(rw.HKCU, base)
        rw.CLASSES, rw.BACKUP_KEY = saved


# ------------------------------------------------------------- удаление

def test_uninstall_delete():
    from syno_bridge import uninstall
    tmp = tempfile.mkdtemp(prefix="syno_uninst_test_")
    exe = os.path.join(tmp, "Syno Torrent's Bridge.exe")   # пробел и апостроф в пути
    proc = run_locked_exe(exe, seconds=3)
    time.sleep(0.5)
    other = os.path.join(tmp, "x.old")
    open(other, "wb").close()
    uninstall.schedule_delete([exe, other, os.path.join(tmp, "нет.exe")], wait_pids=[proc.pid])
    deadline = time.time() + 30
    while time.time() < deadline and (os.path.exists(exe) or os.path.exists(other)):
        time.sleep(0.5)
    check("uninstall: занятый exe удалён после выхода процесса", not os.path.exists(exe))
    check("uninstall: второй файл удалён", not os.path.exists(other))
    proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- журнал

def test_log_rotation():
    import syno_bridge.__main__ as m
    tmp = Path(tempfile.mkdtemp(prefix="syno_log_test_"))
    saved = m.LOG_PATH
    m.LOG_PATH = tmp / "bridge.log"
    try:
        m.LOG_PATH.write_bytes(b"x" * (m.LOG_MAX_BYTES + 10))
        (tmp / "bridge.log.1").write_bytes(b"old1")
        m._rotate_log()
        check("журнал: большой файл ушёл в .1", (tmp / "bridge.log.1").stat().st_size > m.LOG_MAX_BYTES)
        check("журнал: прежний .1 стал .2", (tmp / "bridge.log.2").read_bytes() == b"old1")
        check("журнал: основной файл освобождён", not m.LOG_PATH.exists())
        m.LOG_PATH.write_bytes(b"small")
        m._rotate_log()
        check("журнал: маленький файл не трогается", m.LOG_PATH.read_bytes() == b"small")
    finally:
        m.LOG_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)
    from syno_bridge.syno_api import short_list
    check("журнал: длинные списки сокращаются",
          short_list(range(240)) == "[0, 1, 2, 3, 4, 5, 6, 7, … всего 240]", short_list(range(240)))


if __name__ == "__main__":
    test_bencode()
    test_dpapi()
    test_client()
    test_monitor()
    test_winsync()
    test_updater()
    test_registry()
    test_uninstall_delete()
    test_log_rotation()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("Все тесты пройдены ✓")
