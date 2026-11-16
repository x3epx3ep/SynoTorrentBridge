"""Клиент Synology Web API: авторизация, Download Station (v1/v2), FileStation.

Только стандартная библиотека (urllib). Поддерживает:
  - автоопределение доступных API через SYNO.API.Info (query=ALL)
  - DSM 7 / Download Station 2: SYNO.DownloadStation2.Task (+ Task.BT.File — выбор файлов)
  - DSM 6 fallback: SYNO.DownloadStation.Task (без выбора файлов)
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import tls

log = logging.getLogger("syno_api")

# статусы задач DownloadStation2 (проверено на DS 4.x / DSM 7):
# 5 — завершена, 8 — завершена и раздаётся (seeding)
DS2_FINISHED = {5, 8}
DS2_CHECKING = 6   # «Проверка» (hash checking)


def short_list(items, limit: int = 8) -> str:
    """Короткое представление длинного списка для журнала."""
    items = list(items)
    if len(items) <= limit:
        return str(items)
    head = ", ".join(str(x) for x in items[:limit])
    return f"[{head}, … всего {len(items)}]"

COMMON_ERRORS = {
    100: "Неизвестная ошибка",
    101: "Неверный параметр",
    102: "API не существует",
    103: "Метод не существует",
    104: "Версия API не поддерживает функцию",
    105: "Нет прав доступа",
    106: "Сессия истекла",
    107: "Сессия прервана дублирующим входом",
    119: "SID не найден",
    400: "Неверный логин или пароль / ошибка загрузки файла",
    401: "Учётная запись отключена / достигнут лимит задач",
    402: "Доступ запрещён / назначение запрещено",
    403: "Папка назначения не существует",
    404: "Неверный id задачи",
    405: "Недопустимое действие",
    406: "Не задана папка по умолчанию / требуется код 2FA",
    407: "Не удалось установить папку назначения",
    408: "Файл не существует",
}


class SynoError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _err_text(code) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    return COMMON_ERRORS.get(code, f"Код ошибки {code}")


class SynoClient:
    def __init__(self, host: str, port: int, use_https: bool, verify_ssl: bool = True,
                 username: str = "", password: str = "", otp_code: str = "", timeout: int = 30,
                 device_id: str = "", device_name: str = "SynoTorrentBridge"):
        scheme = "https" if use_https else "http"
        self.host = host
        self.base = f"{scheme}://{host}:{int(port)}"
        self.username = username
        self.password = password
        self.otp_code = otp_code
        self.device_id = device_id
        self.device_name = device_name
        self.timeout = timeout
        self.sid: str | None = None
        self.api_info: dict = {}
        # cookie-jar: DSM отдаёт device id (did) cookie'ой — ловим и её тоже,
        # и автоматически шлём обратно при следующих входах
        self._cookies = http.cookiejar.CookieJar()
        self._use_https = use_https
        self._verify = verify_ssl
        self._tls_fallback = False
        if use_https:
            ctx = tls.verified_context() if verify_ssl else tls.unverified_context()
        else:
            ctx = None
        self._build_opener(ctx)

    def _build_opener(self, ctx) -> None:
        handlers = [urllib.request.HTTPCookieProcessor(self._cookies)]
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)

    def _cookie(self, name: str) -> str:
        for c in self._cookies:
            if c.name == name and c.value:
                return c.value
        return ""

    # ---------- низкий уровень ----------

    def _request(self, path: str, params: dict | None = None, post: dict | None = None,
                 raw_body: bytes | None = None, headers: dict | None = None) -> dict:
        url = f"{self.base}/webapi/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        hdrs = dict(headers or {})
        if raw_body is not None:
            data = raw_body
        elif post is not None:
            data = urllib.parse.urlencode(post).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 400 and url.startswith("http://") and url.split(":")[2].split("/")[0] == "5001":
                raise SynoError(
                    "HTTP Error 400: похоже, используется HTTP на HTTPS-порту 5001. "
                    "Включите галочку «HTTPS» или смените порт на 5000.") from e
            raise SynoError(f"HTTP Error {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if (self._use_https and self._verify and not self._tls_fallback
                    and tls.is_cert_error(reason)):
                # Windows не подтвердила цепочку (часто: автообновление корней
                # отключено политикой) — повторяем по списку Mozilla, один раз
                log.info("Windows не подтвердила сертификат %s (%s) — проверка по списку Mozilla",
                         self.host, tls.describe(reason))
                self._tls_fallback = True
                self._build_opener(tls.fallback_context())
                return self._request(path, params=params, post=post,
                                     raw_body=raw_body, headers=headers)
            hint = ""
            text = str(reason)
            if "10061" in text or "Connection refused" in text:
                hint = " (соединение отклонено — проверьте порт и что DSM доступен)"
            elif "11001" in text or "getaddrinfo" in text or "Name or service" in text:
                hint = " (адрес не найден — проверьте имя/IP NAS)"
            elif "timed out" in text or "10060" in text:
                hint = " (тайм-аут — NAS недоступен по сети или порт закрыт брандмауэром)"
            elif tls.cert_hint(reason, self.host):
                hint = " — " + tls.cert_hint(reason, self.host)
            elif "SSL" in text:
                hint = " (ошибка SSL — проверьте, что на этом порту включён HTTPS)"
            raise SynoError(f"NAS недоступен ({self.base}): {tls.describe(reason)}{hint}") from e
        except TimeoutError as e:
            # тайм-аут на чтении ответа приходит не как URLError, а напрямую
            raise SynoError(f"NAS не ответил вовремя ({self.base}): тайм-аут "
                            f"{self.timeout} с — NAS перегружен или связь нестабильна") from e
        except (OSError, http.client.HTTPException) as e:
            raise SynoError(f"Связь с NAS прервалась ({self.base}): {e}") from e
        try:
            result = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as e:
            raise SynoError(f"Некорректный ответ NAS: {body[:200]!r}") from e
        if not result.get("success"):
            code = (result.get("error") or {}).get("code")
            raise SynoError(_err_text(code), code=code)
        return result.get("data") or {}

    def _api(self, api: str, version: int, method: str, path: str | None = None,
             extra: dict | None = None, post: bool = False) -> dict:
        params = {"api": api, "version": version, "method": method}
        if extra:
            params.update(extra)
        if self.sid:
            params["_sid"] = self.sid
        if path is None:
            info = self.api_info.get(api) or {}
            path = info.get("path", "entry.cgi")
        if post:
            return self._request(path, post=params)
        return self._request(path, params=params)

    # ---------- обнаружение и авторизация ----------

    def discover(self) -> dict:
        """Запрашивает список всех API на NAS."""
        data = self._request("query.cgi", params={
            "api": "SYNO.API.Info", "version": 1, "method": "query", "query": "ALL"})
        self.api_info = data or {}
        log.info("Обнаружено API: %d шт.; DS2=%s, BT.File=%s",
                 len(self.api_info),
                 "SYNO.DownloadStation2.Task" in self.api_info,
                 "SYNO.DownloadStation2.Task.BT.File" in self.api_info)
        return self.api_info

    def has_api(self, name: str) -> bool:
        return name in self.api_info

    def api_max_version(self, name: str, default: int = 1) -> int:
        return int((self.api_info.get(name) or {}).get("maxVersion", default))

    @property
    def ds2(self) -> bool:
        return self.has_api("SYNO.DownloadStation2.Task")

    @property
    def supports_file_selection(self) -> bool:
        return self.has_api("SYNO.DownloadStation2.Task.BT.File")

    def login(self) -> None:
        if not self.api_info:
            self.discover()
        version = min(self.api_max_version("SYNO.API.Auth", 3), 7)
        params = {
            "api": "SYNO.API.Auth", "version": version, "method": "login",
            "account": self.username, "passwd": self.password,
            "session": "DownloadStation", "format": "sid",
        }
        if self.device_id:
            # доверенное устройство: 2FA пропускается (DSM Login Web API Guide, пример 4)
            params["device_id"] = self.device_id
            params["device_name"] = self.device_name
        if self.otp_code:
            params["otp_code"] = self.otp_code
            if version >= 6:
                # просим device token, чтобы в следующий раз войти без кода (пример 3)
                params["enable_device_token"] = "yes"
                params.setdefault("device_name", self.device_name)
        try:
            # POST: пароль и код не попадают в строку запроса (и в журналы веб-сервера)
            data = self._request("auth.cgi", post=params)
        except SynoError as e:
            # коды auth.cgi: 400 — неверный логин/пароль, 402 — нет прав,
            # 403 — требуется OTP, 404 — неверный OTP, 406 — OTP обязателен
            if e.code == 400:
                raise SynoError("Неверный логин или пароль", code=400) from e
            if e.code == 402:
                raise SynoError("Пользователю запрещён вход — проверьте права в DSM "
                                "(Панель управления → Пользователи → Приложения)", code=402) from e
            if e.code == 403 and not self.otp_code:
                raise SynoError("NAS требует одноразовый код 2FA", code=403) from e
            if e.code == 404:
                raise SynoError("Неверный одноразовый код 2FA", code=404) from e
            if e.code == 406 and not self.otp_code:
                raise SynoError("NAS требует одноразовый код 2FA", code=406) from e
            raise
        self.sid = data.get("sid")
        if not self.sid:
            raise SynoError("NAS не вернул sid при авторизации")
        # device id приходит как did (auth v6), device_id (auth v7) или cookie'ой did
        did = data.get("did") or data.get("device_id") or self._cookie("did")
        if did:
            self.device_id = str(did)
            log.info("Получен device token — 2FA больше не понадобится на этом компьютере")
        elif self.otp_code:
            log.warning("NAS не вернул device token (ответ: keys=%s, cookies=%s)",
                        sorted(data.keys()), [c.name for c in self._cookies])
        log.info("Авторизация успешна (auth v%s)", version)

    def logout(self, timeout: int | None = None) -> None:
        if self.sid:
            saved = self.timeout
            if timeout is not None:
                self.timeout = timeout
            try:
                self._request("auth.cgi", params={
                    "api": "SYNO.API.Auth", "version": 1, "method": "logout",
                    "session": "DownloadStation", "_sid": self.sid})
            except Exception:
                pass
            finally:
                self.timeout = saved
            self.sid = None

    def ensure(self) -> None:
        """Гарантирует авторизацию (перелогин при истекшей сессии)."""
        if not self.sid:
            self.login()

    def _call(self, api: str, version: int, method: str, extra: dict | None = None,
              post: bool = False, retry: bool = True) -> dict:
        self.ensure()
        try:
            return self._api(api, version, method, extra=extra, post=post)
        except SynoError as e:
            if e.code in (106, 107, 119) and retry:
                log.info("Сессия истекла, перелогин")
                self.sid = None
                self.ensure()
                return self._api(api, version, method, extra=extra, post=post)
            raise

    def _upload(self, path: str, body: bytes, boundary: str) -> dict:
        """multipart-запрос с перелогином при истекшей сессии (как в _call)."""
        self.ensure()
        hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        try:
            return self._request(f"{path}?_sid={self.sid}", raw_body=body, headers=hdrs)
        except SynoError as e:
            if e.code not in (106, 107, 119):
                raise
            log.info("Сессия истекла, перелогин")
            self.sid = None
            self.ensure()
            return self._request(f"{path}?_sid={self.sid}", raw_body=body, headers=hdrs)

    # ---------- Download Station: создание задач ----------

    def default_destination(self) -> str:
        try:
            if self.ds2:
                data = self._call("SYNO.DownloadStation2.Settings.Location", 1, "get")
                # на всякий случай разные варианты ответа
                for key in ("default_destination", "destination"):
                    if data.get(key):
                        return str(data[key]).strip('"')
            data = self._call("SYNO.DownloadStation.Info", 1, "getconfig",
                              path=self.api_info.get("SYNO.DownloadStation.Info", {}).get("path"))
            return str(data.get("default_destination", "") or "")
        except Exception as e:
            log.warning("Не удалось получить папку по умолчанию: %s", e)
            return ""

    def create_task_url(self, url: str, destination: str = "") -> str:
        """Добавляет задачу по magnet/URL. Возвращает task_id (может быть пустым)."""
        if self.ds2:
            params = {
                "type": '"url"',
                "url": url,
                "create_list": "false",
            }
            if destination:
                params["destination"] = json.dumps(destination, ensure_ascii=False)
            data = self._call("SYNO.DownloadStation2.Task", 2, "create", extra=params, post=True)
            ids = data.get("task_id") or []
            return ids[0] if ids else ""
        # DSM 6
        params = {"uri": url}
        if destination:
            params["destination"] = destination
        version = min(self.api_max_version("SYNO.DownloadStation.Task", 1), 3)
        self._call("SYNO.DownloadStation.Task", version, "create", extra=params, post=True)
        return ""

    @staticmethod
    def _multipart(fields: list, file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
        """Собирает multipart/form-data; файл — строго последним полем (требование DS API)."""
        boundary = "----SynoBridge" + uuid.uuid4().hex
        parts = []
        for name, value in fields:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/x-bittorrent\r\n\r\n".encode("utf-8"))
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return b"".join(parts), boundary

    def create_task_file(self, torrent_bytes: bytes, filename: str, destination: str = "") -> str:
        """Загружает .torrent файл. Возвращает task_id (может быть пустым)."""
        self.ensure()
        if self.ds2:
            fields = [
                ("api", "SYNO.DownloadStation2.Task"),
                ("method", "create"),
                ("version", "2"),
                ("type", '"file"'),
                ("file", '["torrent"]'),
            ]
            if destination:
                fields.append(("destination", json.dumps(destination, ensure_ascii=False)))
            fields.append(("create_list", "false"))
            body, boundary = self._multipart(fields, "torrent", filename, torrent_bytes)
            path = self.api_info["SYNO.DownloadStation2.Task"]["path"]
            data = self._upload(path, body, boundary)
            ids = data.get("task_id") or []
            return ids[0] if ids else ""
        # DSM 6: параметры в query, файл — POST body
        info = self.api_info.get("SYNO.DownloadStation.Task", {})
        path = info.get("path", "DownloadStation/task.cgi")
        version = min(int(info.get("maxVersion", 1)), 3)
        q = {"api": "SYNO.DownloadStation.Task", "version": version, "method": "create",
             "_sid": self.sid}
        if destination:
            q["destination"] = destination
        body, boundary = self._multipart([], "file", filename, torrent_bytes)
        self._request(path + "?" + urllib.parse.urlencode(q), raw_body=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return ""

    # ---------- Download Station: списки выбора файлов (create_list) ----------

    def create_pending_list_file(self, torrent_bytes: bytes, filename: str, destination: str) -> str:
        """Создаёт ожидающий список файлов из .torrent (задача НЕ стартует). Возвращает list_id."""
        self.ensure()
        fields = [
            ("api", "SYNO.DownloadStation2.Task"),
            ("method", "create"),
            ("version", "2"),
            ("type", '"file"'),
            ("file", '["torrent"]'),
            ("create_list", "true"),
        ]
        if destination:
            fields.append(("destination", json.dumps(destination, ensure_ascii=False)))
        body, boundary = self._multipart(fields, "torrent", filename, torrent_bytes)
        path = self.api_info["SYNO.DownloadStation2.Task"]["path"]
        data = self._upload(path, body, boundary)
        ids = data.get("list_id") or []
        return ids[0] if ids else ""

    def create_pending_list_url(self, url: str, destination: str) -> str:
        """То же для magnet/URL. Возвращает list_id (может быть пустым, если не поддерживается)."""
        list_id, _task_id = self.create_url_or_list(url, destination)
        return list_id

    def create_url_or_list(self, url: str, destination: str) -> tuple[str, str]:
        """Для magnet/URL с create_list=true. Возвращает (list_id, task_id).

        Проверено на живом NAS: magnet игнорирует create_list — задача создаётся
        сразу, list_id пуст, а task_id заполнен. Это важно: повторный вызов
        create_task_url создаст ДУБЛЬ, поэтому task_id надо переиспользовать."""
        if not self.ds2:
            return "", ""
        params = {"type": '"url"', "url": url, "create_list": "true"}
        if destination:
            params["destination"] = json.dumps(destination, ensure_ascii=False)
        data = self._call("SYNO.DownloadStation2.Task", 2, "create", extra=params, post=True)
        lids = data.get("list_id") or []
        tids = data.get("task_id") or []
        return (lids[0] if lids else ""), (tids[0] if tids else "")

    def get_pending_files(self, list_id: str) -> list:
        """Файлы ожидающего списка: [{index, name, size}]. Для magnet пусто, пока нет метаданных."""
        data = self._call("SYNO.DownloadStation2.Task.List", 2, "get",
                          extra={"list_id": list_id})
        out = []
        for i, f in enumerate(data.get("files") or []):
            if not isinstance(f, dict):
                continue
            out.append({
                "index": f.get("index", i),
                "name": str(f.get("name") or ""),
                "size": int(f.get("size") or 0),
            })
        return out

    def pending_download(self, list_id: str, destination: str, indexes: list | None = None) -> str:
        """Превращает список в задачу. Возвращает task_id.

        indexes формально передаётся как параметр index, но на практике NAS его
        игнорирует (проверено на DSM 7: задача всегда создаётся со всеми файлами
        wanted) — выбор файлов надо применять через set_files_skip после создания."""
        params = {"list_id": list_id}
        if destination:
            params["destination"] = json.dumps(destination, ensure_ascii=False)
        if indexes is not None:
            params["index"] = json.dumps(indexes)
        data = self._call("SYNO.DownloadStation2.Task.List", 2, "download",
                          extra=params, post=True)
        ids = data.get("task_id") or []
        return ids[0] if ids else ""

    def pending_download_selected(self, list_id: str, destination: str, indexes: list) -> bool:
        """Создаёт задачу из списка ТОЛЬКО с выбранными файлами (selected).

        В отличие от Task.List download (где index игнорируется), вызов
        Task.List.Polling download применяет выбор на стороне NAS — wanted
        выставляется до старта закачки, гонки нет. Проверено на живом NAS:
        selected=[0,1,2] → wanted ровно у файлов 0,1,2 из 85.

        Реальный dbid задачи появляется асинхронно (ответ содержит служебный
        polling-handle), поэтому метод возвращает только факт успеха."""
        params = {"list_id": list_id, "selected": json.dumps(indexes)}
        if destination:
            params["destination"] = json.dumps(destination, ensure_ascii=False)
        self._call("SYNO.DownloadStation2.Task.List.Polling", 2, "download",
                   extra=params, post=True)
        return True

    def pending_delete(self, list_id: str) -> None:
        """Удаляет ожидающий список (отмена)."""
        if not list_id:
            return
        try:
            self._call("SYNO.DownloadStation2.Task.List", 2, "delete",
                       extra={"list_id": list_id}, post=True)
        except SynoError as e:
            log.warning("Не удалось удалить список %s: %s", list_id, e)

    # ---------- Download Station: управление задачей ----------

    def _ds2_action(self, method: str, task_id: str) -> None:
        if not task_id:
            return
        for idval in (json.dumps([task_id]), task_id):
            try:
                self._call("SYNO.DownloadStation2.Task", 2, method, extra={"id": idval}, post=True)
                return
            except SynoError as e:
                log.warning("DS2 %s с id=%s не удалось: %s", method, idval, e)
        raise SynoError(f"Не удалось выполнить {method} для задачи")

    def pause_task(self, task_id: str) -> None:
        if self.ds2:
            self._ds2_action("pause", task_id)
        elif task_id:
            self._call("SYNO.DownloadStation.Task", 1, "pause", extra={"id": task_id})

    def resume_task(self, task_id: str) -> None:
        if self.ds2:
            self._ds2_action("resume", task_id)
        elif task_id:
            self._call("SYNO.DownloadStation.Task", 1, "resume", extra={"id": task_id})

    def task_status(self, task_id: str) -> tuple:
        """(status, size_downloaded) задачи DS2; (None, 0), если узнать нельзя."""
        if not (self.ds2 and task_id):
            return None, 0
        try:
            data = self._call("SYNO.DownloadStation2.Task", 2, "get",
                              extra={"id": json.dumps([task_id]),
                                     "additional": json.dumps(["transfer"])})
        except SynoError as e:
            log.warning("Статус задачи %s: %s", task_id, e)
            return None, 0
        tasks = data.get("task") or []
        if not tasks:
            return None, 0
        t = tasks[0]
        tr = (t.get("additional") or {}).get("transfer") or {}
        try:
            status = int(t.get("status"))
        except (TypeError, ValueError):
            status = None
        return status, int(tr.get("size_downloaded") or 0)

    def unstick_task(self, task_id: str, observe: float = 6.0) -> bool:
        """Страховка после смены wanted: если задача стоит в «Проверке»
        (статус 6), ничего не скачав, — один раз пауза + возобновление.
        Возвращает True, если задачу пришлось «толкнуть»."""
        deadline = time.time() + observe
        status, done = self.task_status(task_id)
        while status == DS2_CHECKING and done == 0 and time.time() < deadline:
            time.sleep(1)
            status, done = self.task_status(task_id)
        if status != DS2_CHECKING or done:
            return False
        log.warning("Задача %s застряла в «Проверке» — пауза и возобновление", task_id)
        try:
            self.pause_task(task_id)
            for _ in range(10):
                time.sleep(0.5)
                if self.task_status(task_id)[0] == 3:
                    break
            self.resume_task(task_id)
        except SynoError as e:
            log.warning("Не удалось «толкнуть» задачу %s: %s", task_id, e)
        return True

    def delete_task(self, task_id: str) -> None:
        if not task_id:
            return
        try:
            if self.ds2:
                self._ds2_action("delete", task_id)
            else:
                self._call("SYNO.DownloadStation.Task", 1, "delete",
                           extra={"id": task_id, "force_complete": "false"})
        except SynoError as e:
            log.warning("Не удалось удалить задачу %s: %s", task_id, e)

    def list_tasks(self, detail: bool = False, page: int = 100) -> list:
        """Все задачи Download Station (постранично). Элементы — dict как у NAS:
        id, title, status, size, (additional.detail.uri при detail=True)."""
        out: list = []
        offset = 0
        while True:
            if self.ds2:
                extra = {"limit": page, "offset": offset}
                if detail:
                    extra["additional"] = json.dumps(["detail"])
                data = self._call("SYNO.DownloadStation2.Task", 2, "list", extra=extra)
                tasks = data.get("task") or data.get("tasks") or []
            else:
                extra = {"limit": page, "offset": offset}
                if detail:
                    extra["additional"] = "detail"
                data = self._call("SYNO.DownloadStation.Task", 1, "list", extra=extra)
                tasks = data.get("tasks") or []
            out.extend(t for t in tasks if isinstance(t, dict))
            total = int(data.get("total") or 0)
            offset += len(tasks)
            if not tasks or offset >= total or offset > 20000:
                return out

    @staticmethod
    def _task_matches(t: dict, info_hash: str, title: str) -> bool:
        """Совпадение только по точным признакам: info-hash в uri (magnet-задачи)
        или точное имя задачи. Подстрока имени НЕ считается совпадением."""
        t_title = str(t.get("title", "")).strip()
        t_uri = str(((t.get("additional") or {}).get("detail") or {}).get("uri", ""))
        if info_hash and info_hash.lower() in t_uri.lower():
            return True
        return bool(title) and t_title == title.strip()

    def task_ids(self) -> set:
        """Множество id всех задач — снимок до создания новой."""
        return {str(t.get("id", "")) for t in self.list_tasks()}

    def find_duplicate(self, info_hash: str = "", title: str = "") -> str:
        """Есть ли уже такая задача? Возвращает её название или ''.
        Только для предупреждения пользователя — найденную задачу не трогаем."""
        if not (info_hash or title):
            return ""
        try:
            for t in self.list_tasks(detail=True):
                if self._task_matches(t, info_hash, title):
                    return str(t.get("title", "")) or title
        except SynoError as e:
            log.warning("Проверка дубликатов не удалась: %s", e)
        return ""

    def find_new_task(self, before_ids: set, info_hash: str = "", title: str = "") -> str:
        """id задачи, которая появилась ПОСЛЕ снимка before_ids и совпадает по
        хешу/имени. Задачи, существовавшие раньше, не возвращаются никогда —
        иначе можно изменить или удалить чужую (старую) задачу."""
        try:
            fresh = [t for t in self.list_tasks(detail=True)
                     if str(t.get("id", "")) not in before_ids]
        except SynoError as e:
            log.warning("Поиск новой задачи не удался: %s", e)
            return ""
        matched = [t for t in fresh if self._task_matches(t, info_hash, title)]
        if len(matched) == 1:
            return str(matched[0].get("id", ""))
        if len(matched) > 1:
            log.warning("Новых задач-кандидатов несколько (%d) — не выбираю ни одну", len(matched))
        return ""

    # ---------- Download Station: файлы внутри BT-задачи ----------

    def list_bt_files(self, task_id: str) -> list:
        """Список файлов задачи: [{id, name, size, priority}]. Пусто, если недоступно."""
        if not (self.ds2 and self.supports_file_selection and task_id):
            return []
        try:
            data = self._call("SYNO.DownloadStation2.Task.BT.File", 2, "list",
                              extra={"task_id": task_id, "limit": -1})
        except SynoError as e:
            log.warning("BT.File list v2 не удался (%s), пробую v1", e)
            try:
                data = self._call("SYNO.DownloadStation2.Task.BT.File", 1, "list",
                                  extra={"task_id": task_id, "limit": -1})
            except SynoError as e2:
                log.warning("BT.File list не доступен: %s", e2)
                return []
        items = data.get("items") or data.get("files") or data.get("file") or []
        out = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            fid = it.get("id", it.get("index", it.get("num", i)))
            name = it.get("name") or it.get("filename") or ""
            out.append({
                "id": fid,
                "name": str(name),
                "size": int(it.get("size") or 0),
                "priority": str(it.get("priority", "normal")),
            })
        return out

    def set_files_skip(self, task_id: str, file_ids: list, skip: bool = True) -> bool:
        """Помечает файлы как skip/normal. Возвращает True при успехе."""
        if not (self.ds2 and task_id and file_ids):
            return False
        ids = json.dumps(file_ids)
        brief = short_list(file_ids)
        # DS 4.x (проверено на 4.1.2): параметр index (JSON-массив) + wanted=false.
        # priority="skip" эта версия отклоняет (ошибка 120, priority: condition).
        try:
            self._call("SYNO.DownloadStation2.Task.BT.File", 2, "set",
                       extra={"task_id": task_id, "index": ids,
                              "wanted": "false" if skip else "true"}, post=True)
            log.info("BT.File set: OK (index=%s, wanted=%s)", brief, not skip)
            return True
        except SynoError as e:
            log.warning("BT.File set (index/wanted) %s: %s", brief, e)
        # запасные варианты для старых версий DS
        priority = '"skip"' if skip else '"normal"'
        for method in ("set", "edit"):
            for idparam in ("index", "file"):
                params = {"task_id": task_id, idparam: ids, "priority": priority}
                try:
                    self._call("SYNO.DownloadStation2.Task.BT.File", 2, method, extra=params, post=True)
                    log.info("BT.File %s: OK (%s=%s, %s)", method, idparam, brief, priority)
                    return True
                except SynoError as e:
                    log.warning("BT.File %s (%s): %s", method, idparam, e)
        return False

    # ---------- FileStation: дерево папок ----------

    def list_shares(self) -> list:
        """Общие папки верхнего уровня: [{name, path}]."""
        data = self._call("SYNO.FileStation.List", 2, "list_share",
                          extra={"filetype": "dir", "limit": 1000})
        return [{"name": f["name"], "path": f["path"]}
                for f in data.get("shares", []) if f.get("isdir")]

    def create_folder(self, parent_path: str, name: str, force_parent: bool = False) -> None:
        extra = {"folder_path": parent_path, "name": name}
        if force_parent:
            extra["force_parent"] = "true"
        self._call("SYNO.FileStation.CreateFolder", 2, "create", extra=extra)

    def folder_exists(self, dest: str) -> bool | None:
        """Есть ли папка dest ('Share/sub', без ведущего '/') на NAS.
        None — проверить нельзя (FileStation недоступен), True/False — ответ NAS."""
        path = "/" + dest.strip().strip("/")
        try:
            data = self._call("SYNO.FileStation.List", 2, "getinfo",
                              extra={"path": json.dumps([path], ensure_ascii=False)})
        except SynoError as e:
            log.warning("Проверка папки %s невозможна: %s", path, e)
            return None
        files = data.get("files") or []
        if not files or not isinstance(files[0], dict):
            return None
        f = files[0]
        if f.get("code"):
            return False
        return bool(f.get("isdir", True))

    def create_path(self, dest: str) -> None:
        """Создаёт папку dest вместе с промежуточными. Общую папку (первый
        уровень) создать нельзя — для неё бросается SynoError."""
        parts = [p for p in dest.strip().strip("/").split("/") if p]
        if len(parts) < 2:
            raise SynoError(f"Общей папки «{dest}» нет на NAS. Общие папки создаются "
                            "в DSM (Панель управления → Общая папка).")
        self.create_folder("/" + "/".join(parts[:-1]), parts[-1], force_parent=True)

    def list_dirs(self, folder_path: str) -> list:
        """Подпапки внутри folder_path: [{name, path}]."""
        data = self._call("SYNO.FileStation.List", 2, "list",
                          extra={"folder_path": folder_path, "filetype": "dir",
                                 "sort_by": "name", "limit": 1000})
        return [{"name": f["name"], "path": f["path"]}
                for f in data.get("files", []) if f.get("isdir")]
