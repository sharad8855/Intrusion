"""
Security / RTSP-URL utilities.

Ported from the cctv-management project so the camera-add logic is
identical:

* `build_rtsp_url()` — smart URL builder (full URL / webcam index /
  bare host[/path]) with percent-encoded credentials.
* `encrypt_value()` / `decrypt_value()` — Fernet (AES) encryption for
  the camera password stored in the database.
* `mask_rtsp_url()` — hides the password when logging a URL.

Encryption is OPTIONAL: if no Fernet key is configured (or the
`cryptography` package is missing) values are stored as plain text and
everything still works — exactly like the cctv-management fallback.
"""
from __future__ import annotations

from urllib.parse import quote

from backend.core.config import CONFIG

# Marker prefix so we never double-encrypt an already-encrypted value.
_ENC_PREFIX = "ENC:"

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:                         # cryptography not installed
    Fernet = None
    InvalidToken = Exception
    _HAS_CRYPTO = False


# ── Fernet cipher ────────────────────────────────────────────────────
def _get_fernet():
    """Return a Fernet cipher, or None if encryption is not configured."""
    if not _HAS_CRYPTO:
        return None
    key = CONFIG.get_path("security.fernet_key", "")
    if not key:
        return None
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_value(plaintext: str | None) -> str | None:
    """Encrypt a string for DB storage. No-op if encryption is disabled."""
    if not plaintext:
        return plaintext
    if plaintext.startswith(_ENC_PREFIX):
        return plaintext                    # already encrypted
    f = _get_fernet()
    if f is None:
        return plaintext                    # encryption disabled — store as-is
    return _ENC_PREFIX + f.encrypt(plaintext.encode()).decode()


def decrypt_value(value: str | None) -> str | None:
    """Decrypt a value produced by `encrypt_value`. Plain values pass through."""
    if not value:
        return value
    if not value.startswith(_ENC_PREFIX):
        return value                        # legacy / unencrypted value
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        return None                         # wrong key / corrupted


# ── RTSP URL builder (cctv-management `_build_url` logic) ────────────
def build_rtsp_url(url: str, port=None, username: str | None = None,
                   password: str | None = None) -> str | None:
    """
    Build a connectable stream URL.  Ported from cctv-management `_build_url`.

    * `url` is already a full URL (`rtsp://`, `http://`...) or a webcam
      index (a plain digit) → returned unchanged.
    * Otherwise `url` is treated as `host` or `host/path`; the username
      and password are percent-encoded (@ → %40, : → %3A, etc.) and
      assembled as `rtsp://user:pass@host:port/path`.

    Special characters in credentials (like @, :, %) MUST be
    percent-encoded so the URL parser does not mis-read them as
    delimiters.  `urllib.parse.quote(safe="")` handles this correctly.
    """
    if not url:
        return None
    url = url.strip()

    # Full URL or local webcam index — use verbatim.
    if "://" in url or url.isdigit():
        return url

    # Separate host from an optional path (e.g. 192.168.1.10/stream1).
    host, path = url, ""
    if "/" in url:
        host, rest = url.split("/", 1)
        path = "/" + rest

    # Percent-encode credentials — safe="" encodes ALL special chars.
    auth = ""
    if username and password:
        safe_user = quote(username, safe="")
        safe_pass = quote(password, safe="")
        auth = f"{safe_user}:{safe_pass}@"

    cam_port = f":{port}" if port else ":554"
    return f"rtsp://{auth}{host}{cam_port}{path}"


def mask_rtsp_url(url: str | None) -> str:
    """`rtsp://user:pass@host/path` -> `rtsp://user:****@host/path` (for logs)."""
    if not url or "://" not in url:
        return url or ""
    try:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, address = rest.rsplit("@", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                return f"{scheme}://{user}:****@{address}"
        return url
    except Exception:
        return "[URL MASKED]"
