"""
SSO 认证模块的共享基础设施。

提供 OAuth2 端点常量、日志脱敏工具（redact）、日志初始化（setup_logging）、
请求日志钩子（install_request_logging）、accounts.json 持久化（load_data / save_data）、
以及授权码换 token（exchange_code_for_token）。

Usage:
    from sso.sso_common import setup_logging, redact, load_data
    setup_logging()
    data = load_data()
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key


APPID = "sso20240408001"
SECRET = "002658359667427995860705420291461"

# ---------- RSA 公钥（PKCS#8 / X.509 SubjectPublicKeyInfo）----------
# JSEncrypt.setPublicKey 所用格式。schedule_tool.py / sso_login.py 共用。
PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCAllwmit21QS9mQTG3Ry1pYIOJ"
    "xffUuYJTyXKOLIYTmQZMvWEKASShMXjHTzogU+oN5LZQX2HCZyP96mkOeXZvgpnj"
    "R9xKltr2KndH4R6SbcqbrNWX2q9+uagCgz1MUF8jZhDswiMWMYmLds433qXpCXFB"
    "cbA4avIEYzsjcjk5yQIDAQAB"
)
_PUB_KEY = load_der_public_key(base64.b64decode(PUBLIC_KEY_B64))


def rsa_encrypt(plaintext: str) -> str:
    """RSA-1024 PKCS#1 v1.5 加密，输出 base64（172 字符）。"""
    ct = _PUB_KEY.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(ct).decode("ascii")

BASE = "http://szxy.cqtbi.edu.cn/oauth2"
TOKEN_URL = f"{BASE}/v1/access_token"
AUTH_URL_TEMPLATE = (
    BASE + "/v1/auth2orize?client_id={appid}&state=callback&redirect_uri={redirect_uri}"
)
DEFAULT_REDIRECT = f"{BASE}/api-docs/access_token.html"
LOGIN_URL = f"{BASE}/v1/auth2Login"
LOGIN_PAGE_URL = f"{BASE}/Login.html"
CAPTCHA_URL_TEMPLATE = BASE + "/v1/createVertifyCode?checkId={checkId}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_FILE = PROJECT_ROOT / "accounts.json"


# ---------- 日志 ----------
_SENSITIVE_KEYS = ("upwd", "password", "secret", "access_token", "ticket", "portal_ticket", "refresh_token", "ssid", "cookie")


def _mask(value: str, keep: int = 4) -> str:
    """对字符串脱敏：保留首尾各 keep 个字符，中间替换为 … 并标注原始长度。

    Args:
        value: 待脱敏的字符串。
        keep: 首尾保留的字符数，默认 4。

    Returns:
        脱敏后的字符串。空值返回空串；过短的值全掩为 *。
    """
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}({len(value)})"


def redact(text: str) -> str:
    """脱敏日志文本中的敏感字段值（密码、token、secret 等）。

    同时支持 form-urlencoded（key=value）和 JSON（"key":"value"）两种格式。

    Args:
        text: 原始日志文本。

    Returns:
        已将敏感字段值替换为脱敏格式的文本。
    """
    if not text:
        return text
    out = text
    for k in _SENSITIVE_KEYS:
        # form-urlencoded: key=value&
        import re
        out = re.sub(
            rf"({k}=)([^&\s\"']+)",
            lambda m: m.group(1) + _mask(m.group(2)),
            out,
            flags=re.IGNORECASE,
        )
        # JSON: "key":"value"
        out = re.sub(
            rf'("{k}"\s*:\s*")([^"]+)(")',
            lambda m: m.group(1) + _mask(m.group(2)) + m.group(3),
            out,
            flags=re.IGNORECASE,
        )
    return out


_LOGGING_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger 输出到 stderr，并对第三方库降噪。

    多次调用幂等（仅首次生效）。

    Args:
        level: 日志级别，默认 logging.INFO。
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(level)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(h)
    # 三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("mitmproxy").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    _LOGGING_CONFIGURED = True


_http_log = logging.getLogger("sso.http")


def install_request_logging(session: requests.Session) -> None:
    """给 requests.Session 安装响应钩子，自动记录请求日志。

    日志包含：HTTP method、URL、状态码、耗时（ms）、脱敏后的请求体和响应体。

    Args:
        session: 待安装日志钩子的 requests.Session 实例。
    """

    def hook(resp: requests.Response, *args, **kwargs):
        elapsed_ms = int(resp.elapsed.total_seconds() * 1000)
        req = resp.request
        body_preview = ""
        if req.body:
            body = req.body if isinstance(req.body, str) else req.body.decode("utf-8", "replace")
            body_preview = " body=" + redact(body[:300])
        resp_preview = ""
        try:
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct or "text" in ct or "javascript" in ct:
                resp_preview = " resp=" + redact(resp.text[:300])
            else:
                resp_preview = f" resp=<{ct or 'binary'} {len(resp.content)}B>"
        except Exception:  # noqa: BLE001
            resp_preview = " resp=<unreadable>"
        _http_log.info(
            "%s %s -> %s %dms%s%s",
            req.method,
            req.url,
            resp.status_code,
            elapsed_ms,
            body_preview,
            resp_preview,
        )

    session.hooks.setdefault("response", []).append(hook)


# ---------- 持久化 ----------
def load_data() -> dict:
    """从 accounts.json 加载持久化数据。

    文件不存在或格式错误时返回默认结构：
    ``{"appid": ..., "secret": ..., "login_endpoint": None, "accounts": []}``。

    Returns:
        包含 appid、secret、login_endpoint 和 accounts 列表的字典。
    """
    if not DATA_FILE.exists():
        return {
            "appid": APPID,
            "secret": SECRET,
            "login_endpoint": None,
            "accounts": [],
        }
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "appid": APPID,
            "secret": SECRET,
            "login_endpoint": None,
            "accounts": [],
        }
    data.setdefault("appid", APPID)
    data.setdefault("secret", SECRET)
    data.setdefault("login_endpoint", None)
    data.setdefault("accounts", [])
    return data


def save_data(data: dict) -> None:
    """将数据持久化写入 accounts.json。

    Args:
        data: 包含 appid、secret、login_endpoint 和 accounts 的字典。
    """
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_token_log = logging.getLogger("sso.token")


def exchange_code_for_token(code: str) -> dict:
    """用授权码（code）换取 access_token。

    调用 OAuth2 token 端点，返回接口原始 JSON 响应。

    Args:
        code: 授权码。

    Returns:
        接口返回的 JSON 字典，包含 access_token 等字段。

    Raises:
        requests.RequestException: 网络或服务端错误。
        ValueError: 响应不是合法 JSON。
    """
    _token_log.info("换 token：code=%s", _mask(code, 6))
    t0 = time.time()
    sess = requests.Session()
    sess.trust_env = False
    install_request_logging(sess)
    try:
        r = sess.get(
            TOKEN_URL,
            params={"client_id": APPID, "secret": SECRET, "code": code},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        _token_log.exception("换 token 失败 (耗时 %dms)", int((time.time() - t0) * 1000))
        raise
    if data.get("success"):
        _token_log.info(
            "换 token 成功：token=%s expires_in=%s",
            _mask(data.get("access_token", "")),
            data.get("expires_in"),
        )
    else:
        _token_log.warning("换 token 返回失败：%s", data)
    return data

