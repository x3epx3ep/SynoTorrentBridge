"""TLS-контексты: проверка сертификатов через хранилище Windows.

Стандартный ssl Python проверяет цепочку только по корневым сертификатам,
которые УЖЕ лежат в хранилище Windows. Windows же докачивает корневые
сертификаты по требованию (Automatic Root Update) — браузер видит сертификат
Let's Encrypt как надёжный, а Python на той же машине может ругаться
«unable to get local issuer certificate», пока корень ни разу не скачивался.

truststore проверяет цепочку штатным механизмом Windows (CertGetCertificateChain),
так что результат совпадает с браузером. Если пакета нет (запуск из исходников
без него) — обычный ssl.create_default_context().
"""

from __future__ import annotations

import ipaddress
import logging
import ssl

log = logging.getLogger("tls")

# коды CryptoAPI, которые truststore кладёт в verify_code
CERT_E_EXPIRED = 0x800B0101
CERT_E_UNTRUSTEDROOT = 0x800B0109
CERT_E_CHAINING = 0x800B010A
CERT_E_CN_NO_MATCH = 0x800B010F

# коды OpenSSL (обычный ssl)
X509_V_ERR_CERT_HAS_EXPIRED = 10
X509_V_ERR_DEPTH_ZERO_SELF_SIGNED_CERT = 18
X509_V_ERR_SELF_SIGNED_CERT_IN_CHAIN = 19
X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY = 20
X509_V_ERR_HOSTNAME_MISMATCH = 62
X509_V_ERR_IP_ADDRESS_MISMATCH = 64

_backend = None


def backend() -> str:
    """'windows' — проверка через хранилище Windows, 'openssl' — запасной вариант."""
    verified_context()
    return _backend or "openssl"


def verified_context() -> ssl.SSLContext:
    """Контекст с проверкой сертификата и имени хоста."""
    global _backend
    try:
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        _backend = "windows"
        return ctx
    except Exception as e:  # нет пакета или не Windows
        if _backend is None:
            log.info("truststore недоступен (%s) — проверка сертификатов средствами OpenSSL", e)
        _backend = "openssl"
        return ssl.create_default_context()


def unverified_context() -> ssl.SSLContext:
    """Контекст без проверки — только когда пользователь сам снял галочку."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def describe(err) -> str:
    """Текст ошибки без кортежей: SSLError печатает себя как ('текст',)."""
    if not isinstance(err, BaseException):
        return str(err)
    vm = getattr(err, "verify_message", None)
    if vm:
        return str(vm)
    if len(err.args) == 1 and isinstance(err.args[0], str):
        return err.args[0]
    return str(err)


def cert_hint(err: BaseException, host: str) -> str:
    """Понятная подсказка к ошибке проверки сертификата (пустая строка — это не она)."""
    if not isinstance(err, ssl.SSLCertVerificationError):
        text = str(err)
        if "CERTIFICATE_VERIFY_FAILED" not in text:
            return ""
    code = getattr(err, "verify_code", None)
    try:
        code = int(code) & 0xFFFFFFFF if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in (CERT_E_CN_NO_MATCH, X509_V_ERR_HOSTNAME_MISMATCH, X509_V_ERR_IP_ADDRESS_MISMATCH):
        if is_ip(host):
            return ("сертификат выписан на имя, а подключение идёт по IP-адресу. "
                    "Укажите в поле «Адрес NAS» имя из сертификата (например, nas.example.ru) "
                    "или снимите галочку «Проверять SSL-сертификат»")
        return (f"сертификат NAS выписан не на имя {host}. Укажите имя, на которое он выписан, "
                "или назначьте этот сертификат службе DSM (Панель управления → Безопасность → "
                "Сертификат → Настройки)")
    if code in (CERT_E_EXPIRED, X509_V_ERR_CERT_HAS_EXPIRED):
        return "срок действия сертификата NAS истёк — обновите его в DSM"
    if code in (X509_V_ERR_DEPTH_ZERO_SELF_SIGNED_CERT, X509_V_ERR_SELF_SIGNED_CERT_IN_CHAIN,
                CERT_E_UNTRUSTEDROOT, CERT_E_CHAINING,
                X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY):
        return ("сертификат NAS не подтверждён доверенным центром (самоподписанный или "
                "не хватает промежуточного сертификата). Для самоподписанного снимите "
                "галочку «Проверять SSL-сертификат»")
    return ("сертификат NAS не прошёл проверку. Если он самоподписанный — снимите галочку "
            "«Проверять SSL-сертификат»")
