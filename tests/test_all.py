"""Тесты SynoTorrentBridge: bencode, DPAPI, клиент API против mock-сервера."""

import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syno_bridge import bencode
from syno_bridge.config import encrypt_password, decrypt_password
from syno_bridge.syno_api import SynoClient

FAILED = []


def check(name, cond, extra=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


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

    check("magnet dn", bencode.magnet_display_name(
        "magnet:?xt=urn:btih:abc&dn=Some%20Release%202024") == "Some Release 2024")
    check("human_size", bencode.human_size(1536) == "1.5 КБ", bencode.human_size(1536))


# ------------------------------------------------------------- DPAPI

def test_dpapi():
    enc = encrypt_password("Sekret#123")
    check("dpapi: зашифровано", enc and enc != "Sekret#123")
    check("dpapi: расшифровка", decrypt_password(enc) == "Sekret#123")
    check("dpapi: пустой", decrypt_password("") == "")


# ------------------------------------------------------------- mock NAS

class MockNAS(BaseHTTPRequestHandler):
    captured = {}

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        api = q.get("api", [""])[0]
        method = q.get("method", [""])[0]
        if api == "SYNO.API.Info":
            self._json({"success": True, "data": {
                "SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7},
                "SYNO.DownloadStation2.Task": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.DownloadStation2.Task.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.DownloadStation2.Task.BT.File": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.FileStation.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.FileStation.CreateFolder": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
            }})
        elif api == "SYNO.API.Auth" and method == "login":
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
        elif api == "SYNO.FileStation.List" and method == "list_share":
            self._json({"success": True, "data": {"shares": [
                {"name": "downloads", "path": "/downloads", "isdir": True},
                {"name": "video", "path": "/video", "isdir": True}]}})
        elif api == "SYNO.FileStation.List" and method == "list":
            self._json({"success": True, "data": {"files": [
                {"name": "films", "path": "/video/films", "isdir": True}]}})
        elif api == "SYNO.FileStation.CreateFolder":
            self._json({"success": True, "data": {}})
        elif api == "SYNO.DownloadStation2.Task.BT.File" and method == "list":
            self._json({"success": True, "data": {"total": 3, "items": [
                {"id": 0, "name": "Test Razdacha 2024/Film/film.mkv", "size": 1234567, "priority": "normal"},
                {"id": 1, "name": "Test Razdacha 2024/Subs/Русские субтитры.srt", "size": 5000, "priority": "normal"},
                {"id": 2, "name": "Test Razdacha 2024/readme.txt", "size": 100, "priority": "normal"}]}})
        elif api == "SYNO.DownloadStation2.Task" and method == "list":
            self._json({"success": True, "data": {"total": 1, "task": [
                {"id": "TID-URL-1", "title": "Some Release 2024", "type": "bt"}]}})
        elif api == "SYNO.DownloadStation2.Task.List" and method == "get":
            self._json({"success": True, "data": {"files": [
                {"index": 0, "name": "Test Razdacha 2024/Film/film.mkv", "size": 1234567},
                {"index": 1, "name": "Test Razdacha 2024/Subs/Русские субтитры.srt", "size": 5000},
                {"index": 2, "name": "Test Razdacha 2024/readme.txt", "size": 100}]}})
        else:
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
            self._json({"success": True, "data": {}})
        elif api == "SYNO.DownloadStation2.Task.BT.File" and method == "list":
            self._json({"success": True, "data": {"total": 3, "items": [
                {"id": 0, "name": "Test Razdacha 2024/Film/film.mkv", "size": 1234567, "priority": "normal"},
                {"id": 1, "name": "Test Razdacha 2024/Subs/Русские субтитры.srt", "size": 5000, "priority": "normal"},
                {"id": 2, "name": "Test Razdacha 2024/readme.txt", "size": 100, "priority": "normal"}]}})
        elif api == "SYNO.DownloadStation2.Task.BT.File" and method == "set":
            MockNAS.captured["bt_set"] = q
            self._json({"success": True, "data": {}})
        elif api == "SYNO.DownloadStation2.Task" and method == "list":
            self._json({"success": True, "data": {"total": 1, "task": [
                {"id": "TID-URL-1", "title": "Some Release 2024", "type": "bt"}]}})
        else:
            self._json({"success": False, "error": {"code": 103}})


def test_client():
    server = HTTPServer(("127.0.0.1", 0), MockNAS)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    c = SynoClient("127.0.0.1", port, use_https=False, username="user", password="pass")
    try:
        c.login()
        check("auth: sid", c.sid == "TESTSID123")
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

        found = c.find_task_by_hash_or_title(title="Some Release 2024")
        check("find by title", found == "TID-URL-1", found)

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


if __name__ == "__main__":
    test_bencode()
    test_dpapi()
    test_client()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("Все тесты пройдены ✓")
