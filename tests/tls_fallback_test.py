"""Сертификат, корня которого нет в хранилище Windows, но он есть в списке Mozilla.

Так бывает на компьютерах, где политика отключает автообновление корневых
сертификатов: Windows отвечает «корень не доверенный», а Chrome/Edge (у них свой
список) открывают сайт без предупреждений. Программа должна подключиться,
а сертификат от неизвестного центра — по-прежнему отвергать.

Поднимается локальный HTTPS mock-NAS с сертификатом от одноразового тестового
центра; «список Mozilla» (certifi.where) подменяется на этот центр.
Нужен openssl (есть в Git for Windows).
"""

import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import certifi  # noqa: E402

from syno_bridge import tls  # noqa: E402
from syno_bridge.syno_api import SynoClient, SynoError  # noqa: E402
from tests.test_all import MockNAS  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("[OK ] " if cond else "[FAIL] ") + name + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def find_openssl():
    for p in (shutil.which("openssl"), r"C:\Program Files\Git\usr\bin\openssl.exe",
              r"C:\Program Files\Git\mingw64\bin\openssl.exe"):
        if p and os.path.exists(p):
            return p
    return None


def make_certs(d, openssl):
    def run(*a):
        subprocess.run([openssl, *a], cwd=d, check=True, capture_output=True)
    for name in ("ca", "other"):
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", f"{name}.key", "-out", f"{name}.crt",
            "-subj", f"/CN=SynoTorrentBridge Test {name} CA")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr",
        "-subj", "/CN=127.0.0.1")
    with open(os.path.join(d, "ext.cnf"), "w") as f:
        f.write("subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n"
                "extendedKeyUsage=serverAuth\n")
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "2", "-out", "srv.crt", "-extfile", "ext.cnf")


def main():
    openssl = find_openssl()
    if not openssl:
        print("[SKIP] нет openssl")
        return 0
    d = tempfile.mkdtemp()
    make_certs(d, openssl)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockNAS)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(os.path.join(d, "srv.crt"), os.path.join(d, "srv.key"))
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig = certifi.where
    try:
        certifi.where = lambda: os.path.join(d, "ca.crt")
        c = SynoClient("127.0.0.1", port, True, verify_ssl=True)
        try:
            n = len(c.discover())
            check("корня нет в Windows, но он в списке Mozilla — подключение есть", n > 0)
            check("сработала запасная проверка", c._tls_fallback)
            c.discover()
            check("повторный запрос идёт без новой попытки через Windows", c._tls_fallback)
        except SynoError as e:
            check("корня нет в Windows, но он в списке Mozilla — подключение есть", False, str(e))

        url = f"https://127.0.0.1:{port}/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query"
        try:
            with tls.urlopen(urllib.request.Request(url), timeout=10) as r:
                check("tls.urlopen (обновления): запасная проверка", r.status == 200)
        except Exception as e:  # noqa: BLE001
            check("tls.urlopen (обновления): запасная проверка", False, str(e))

        certifi.where = lambda: os.path.join(d, "other.crt")
        c = SynoClient("127.0.0.1", port, True, verify_ssl=True)
        try:
            c.discover()
            check("сертификат неизвестного центра отвергнут", False)
        except SynoError as e:
            check("сертификат неизвестного центра отвергнут", "сертификат" in str(e), str(e))

        c = SynoClient("127.0.0.1", port, True, verify_ssl=False)
        try:
            check("без проверки — подключение есть", len(c.discover()) > 0)
        except SynoError as e:
            check("без проверки — подключение есть", False, str(e))
    finally:
        certifi.where = orig
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)

    print()
    print("Запасная проверка сертификата:", "есть ошибки" if FAILED else "всё в порядке ✓")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
