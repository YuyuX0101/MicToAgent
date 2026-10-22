"""DSH 自签浏览器会话 Cookie：名字（sha256）、值格式、HMAC 输入、过期重签。"""

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

from targets.dsh import _COOKIE_TTL_MS, _CookieSigner, sign_cookie  # noqa: E402

SECRET = bytes(range(32))
AUTHORITY = "127.0.0.1:12192"
NOW = 1_700_000_000_000


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_cookie_name_is_sha256_of_authority():
    name, _, _ = sign_cookie(SECRET, AUTHORITY, now_ms=NOW)
    assert name == "dsh-auth-" + _b64url(hashlib.sha256(AUTHORITY.encode()).digest())


def test_cookie_value_format_and_payload():
    name, value, expires = sign_cookie(SECRET, AUTHORITY, now_ms=NOW)
    prefix, body, sig = value.split(".")
    assert prefix == "v1"
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload == {
        "version": 1,
        "authority": AUTHORITY,
        "issuedAt": NOW,
        "expiresAt": NOW + _COOKIE_TTL_MS,
    }
    assert expires == NOW + _COOKIE_TTL_MS
    # HMAC 输入是 base64url 编码后的 body 字符串，不是原始 JSON
    expected = hmac.new(SECRET, body.encode(), hashlib.sha256).digest()
    assert sig == _b64url(expected)


def test_signer_caches_until_expiry():
    signer = _CookieSigner(SECRET)
    _, v1 = signer.cookie(AUTHORITY, now_ms=NOW)
    _, v2 = signer.cookie(AUTHORITY, now_ms=NOW + 1000)
    assert v1 == v2  # 未过期不重签
    _, v3 = signer.cookie(AUTHORITY, now_ms=NOW + _COOKIE_TTL_MS)
    assert v3 != v1  # 过期重签


def test_signer_resigns_on_authority_change():
    signer = _CookieSigner(SECRET)
    n1, _ = signer.cookie(AUTHORITY, now_ms=NOW)
    n2, _ = signer.cookie("127.0.0.1:29999", now_ms=NOW)
    assert n1 != n2
