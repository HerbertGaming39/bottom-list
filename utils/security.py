"""
utils/security.py – CSRF validation, rate limiting, file upload helpers, captcha.
"""
import os
import re
import time
import uuid
import secrets
import threading
import urllib.request
import urllib.error
import urllib.parse
import json

from flask import request, session, abort, redirect, url_for, flash
from werkzeug.utils import secure_filename

from auxiliary.settings_helpers import get_setting

_RATE_LIMIT: dict = {}
_RATE_LIMIT_LOCK = threading.Lock()

_ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
_ALLOWED_AUDIO_EXTS = {'mp3', 'wav', 'ogg'}

_RESERVED_USERNAMES = {
    'admin', 'administrator', 'mod', 'moderator', 'staff', 'system',
    'root', 'support', 'help', 'official', 'owner', 'blist', 'bottomlist',
}

_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-]{2,32}$')


def validate_username(name: str) -> str | None:
    """Returns an error string, or None if acceptable."""
    n = (name or '').strip()
    if not n:
        return 'Username is required'
    if len(n) < 2:
        return 'Username must be at least 2 characters long'
    if len(n) > 32:
        return 'Username must be at most 32 characters long'
    if not _USERNAME_RE.match(n):
        return 'Username may only contain letters, numbers, dots, dashes and underscores'
    if n.lower() in _RESERVED_USERNAMES:
        return 'That username is reserved'
    return None


def validate_url_scheme(url: str, allowed_hosts: tuple = ('youtube.com', 'youtu.be', 'www.youtube.com')) -> str | None:
    """Returns error string or None. Only http(s) with allowed hosts permitted
    for user-submitted links (blocks javascript:, data:, etc.)."""
    u = (url or '').strip()
    if not u:
        return None  # empty is fine (optional field)
    try:
        parsed = urllib.parse.urlparse(u)
    except Exception:
        return 'Invalid URL'
    if parsed.scheme not in ('http', 'https'):
        return 'URL must start with http:// or https://'
    host = (parsed.netloc or '').lower()
    if allowed_hosts and not any(host == h or host.endswith('.' + h) for h in allowed_hosts):
        return 'Only YouTube links are accepted'
    return None


# ── Rate limiting ─────────────────────────────────────────────────────────────

def client_ip() -> str:
    # Behind Cloudflare: trust only CF-Connecting-IP (set by CF edge).
    # X-Forwarded-For is client-spoofable and must not be trusted first.
    cf_ip = request.headers.get('CF-Connecting-IP', '')
    if cf_ip.strip():
        return cf_ip.strip()
    xff = request.headers.get('X-Forwarded-For', '')
    if xff and app_trusted_proxy():
        return (xff.split(',')[0] or '').strip() or 'unknown'
    return request.remote_addr or 'unknown'


def app_trusted_proxy() -> bool:
    try:
        from flask import current_app
        return bool(current_app.config.get('TRUST_X_FORWARDED_FOR'))
    except Exception:
        return False


_RATE_LIMIT_MAX_KEYS = 10_000  # memory cap: prevents unbounded dict growth

def rate_limited(bucket: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    key = f'{bucket}:{client_ip()}'
    with _RATE_LIMIT_LOCK:
        if len(_RATE_LIMIT) > _RATE_LIMIT_MAX_KEYS:
            # Evict expired entries wholesale when the table grows too large.
            for k in [k for k, v in _RATE_LIMIT.items() if not v or (now - v[-1]) >= window_seconds][:len(_RATE_LIMIT) // 2]:
                _RATE_LIMIT.pop(k, None)
            if len(_RATE_LIMIT) > _RATE_LIMIT_MAX_KEYS:
                _RATE_LIMIT.clear()
        hits = [t for t in (_RATE_LIMIT.get(key) or []) if (now - t) < window_seconds]
        if len(hits) >= limit:
            _RATE_LIMIT[key] = hits
            return True
        hits.append(now)
        _RATE_LIMIT[key] = hits
    return False


# ── CSRF ──────────────────────────────────────────────────────────────────────

def get_csrf_token() -> str:
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def validate_csrf() -> bool:
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return True
    if request.path.startswith('/api/'):
        return True
    token = session.get('_csrf_token') or ''
    # SECURITY: token is accepted from form body or header ONLY.
    # Never from request.args - URL-carried tokens leak via Referer,
    # browser history and access logs (fixed: was request.args.get).
    form_token = (
        request.form.get('_csrf_token')
        or request.headers.get('X-CSRF-Token')
        or ''
    )
    if not token or not form_token:
        return False
    try:
        return secrets.compare_digest(str(token), str(form_token))
    except Exception:
        return False


def enforce_csrf():
    """Call as a before_request hook."""
    if request.method == 'POST':
        if not validate_csrf():
            try:
                flash('Security check failed. Please refresh the page and try again.', 'danger')
                return redirect(request.referrer or url_for('main.home'))
            except Exception:
                abort(403)


# ── File uploads ──────────────────────────────────────────────────────────────

def file_ext(name: str) -> str:
    s = (name or '').rsplit('.', 1)
    if len(s) != 2:
        return ''
    return (s[1] or '').lower().strip()


_MAGIC_SIGNATURES = {
    'png': b'\x89PNG\r\n\x1a\n',
    'jpg': b'\xff\xd8\xff',
    'jpeg': b'\xff\xd8\xff',
    'gif': b'GIF87a',  # also GIF89a handled below
}


def _magic_bytes_ok(ext: str, head: bytes) -> bool:
    if ext == 'webp':
        return head[0:4] == b'RIFF' and head[8:12] == b'WEBP'
    if ext == 'gif':
        return head[0:6] in (b'GIF87a', b'GIF89a')
    sig = _MAGIC_SIGNATURES.get(ext)
    if not sig:
        return True  # type without a registered signature: extension check only
    return head[:len(sig)] == sig


def save_uploaded_file(f, folder: str, allowed_exts: set, want_mime_prefix: str):
    """Returns (unique_filename, error_string). error_string is '' on success."""
    if not f or not getattr(f, 'filename', None):
        return None, 'missing_file'
    if rate_limited('upload', limit=10, window_seconds=60):
        return None, 'rate_limited'
    filename = secure_filename(f.filename)
    if not filename:
        return None, 'bad_name'
    ext = file_ext(filename)
    if allowed_exts and ext not in allowed_exts:
        return None, 'bad_ext'
    mime = (getattr(f, 'mimetype', '') or '').lower()
    if want_mime_prefix and not mime.startswith(want_mime_prefix):
        return None, 'bad_mime'
    # Content sniffing: verify the real bytes match the claimed image type
    if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
        try:
            head = f.stream.read(16)
            f.stream.seek(0)
        except Exception:
            return None, 'bad_file'
        if not _magic_bytes_ok(ext, head):
            return None, 'bad_content'
    os.makedirs(folder, exist_ok=True)
    unique = f"{uuid.uuid4().hex}_{filename}"
    path = os.path.join(folder, unique)
    f.save(path)
    return unique, ''


# ── Security response headers ─────────────────────────────────────────────────

def add_security_headers(resp):
    try:
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        resp.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        resp.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
        resp.headers['Cross-Origin-Resource-Policy'] = 'same-site'
        resp.headers['Cross-Origin-Embedder-Policy'] = 'unsafe-none'
        # Host header validation
        host = request.host
        if host:
            import re
            if not re.match(r'^[a-zA-Z0-9._-]+(:\d+)?$', host):
                resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
                resp.data = b'Bad Request'
                resp.status_code = 400
                return resp
        # Minimize OPTIONS response information disclosure
        if request.method == 'OPTIONS':
            resp.headers['Allow'] = resp.headers.get('Allow', 'GET, HEAD, OPTIONS')
            resp.headers.pop('Server', None)
            resp.headers.pop('X-Powered-By', None)
        if resp.mimetype and resp.mimetype.startswith('text/html'):
            resp.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "base-uri 'self'; "
                "form-action 'self'; "
                "frame-ancestors 'none'; "
                "object-src 'none'; "
                "frame-src 'self' https://hcaptcha.com https://*.hcaptcha.com "
                "https://www.google.com https://www.recaptcha.net "
                "https://www.youtube.com https://www.youtube-nocookie.com; "
                "media-src 'self'; "
                "font-src 'self' https://fonts.gstatic.com; "
                "script-src 'self' 'unsafe-inline' "
                "https://hcaptcha.com https://*.hcaptcha.com "
                "https://www.google.com https://www.recaptcha.net "
                "https://www.youtube.com https://www.youtube-nocookie.com "
                "https://fonts.googleapis.com "
                "https://cdn.tailwindcss.com "
                "https://cdn.jsdelivr.net "
                "https://html2canvas.hertzen.com; "
                "style-src 'self' 'unsafe-inline' "
                "https://fonts.googleapis.com "
                "https://hcaptcha.com https://*.hcaptcha.com; "
                "img-src 'self' data: https://hcaptcha.com https://*.hcaptcha.com "
                "https://www.google.com https://www.recaptcha.net "
                "https://cdn.discordapp.com "
                "https://i.ytimg.com; "
                "connect-src 'self' "
                "https://hcaptcha.com https://*.hcaptcha.com "
                "https://www.google.com https://www.recaptcha.net;"
                "worker-src 'self' https://cdn.tailwindcss.com;"
            )
        # HSTS sent on every response; harmless over plain HTTP during local dev
        resp.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        # Remove Server header to prevent info leak
        if 'Server' in resp.headers:
            del resp.headers['Server']
        # Remove X-Powered-By header
        if 'X-Powered-By' in resp.headers:
            del resp.headers['X-Powered-By']
        # Cache static assets aggressively (hashed filenames or versioned)
        if request.endpoint == 'static':
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            resp.headers['Vary'] = 'Accept-Encoding'
    except Exception:
        pass
    return resp


# ── CAPTCHA ───────────────────────────────────────────────────────────────────

def get_recaptcha_site_key() -> str:
    from flask import current_app
    return (get_setting('recaptcha_site_key') or current_app.config.get('RECAPTCHA_SITE_KEY') or '').strip()


def get_recaptcha_secret_key() -> str:
    from flask import current_app
    return (get_setting('recaptcha_secret_key') or current_app.config.get('RECAPTCHA_SECRET_KEY') or '').strip()


def get_hcaptcha_site_key() -> str:
    from flask import current_app
    return (get_setting('hcaptcha_site_key') or current_app.config.get('HCAPTCHA_SITE_KEY') or '').strip()


def get_hcaptcha_secret_key() -> str:
    from flask import current_app
    return (get_setting('hcaptcha_secret_key') or current_app.config.get('HCAPTCHA_SECRET_KEY') or '').strip()


def get_captcha_provider() -> str:
    raw = str(get_setting('captcha_provider') or '').strip().lower()
    if raw in ('recaptcha', 'hcaptcha'):
        return raw
    if get_recaptcha_site_key() and get_recaptcha_secret_key():
        return 'recaptcha'
    if get_hcaptcha_site_key() and get_hcaptcha_secret_key():
        return 'hcaptcha'
    return ''


def get_captcha_site_key(provider=None) -> str:
    provider = (provider or get_captcha_provider() or '').strip().lower()
    if provider == 'hcaptcha':
        return get_hcaptcha_site_key()
    if provider == 'recaptcha':
        return get_recaptcha_site_key()
    return ''


def verify_captcha():
    """Returns True (ok), False (failed), or None (no captcha configured)."""
    provider = get_captcha_provider()
    if provider == 'recaptcha':
        site_key = get_recaptcha_site_key()
        secret = get_recaptcha_secret_key()
        if not site_key or not secret:
            return None
        token = (request.form.get('g-recaptcha-response') or '').strip()
        if not token:
            return False
        payload = urllib.parse.urlencode({
            'secret': secret, 'response': token, 'remoteip': request.remote_addr or ''
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://www.google.com/recaptcha/api/siteverify',
            data=payload, headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8', 'ignore') or '{}')
            return bool(data.get('success'))
        except Exception:
            return False
    if provider == 'hcaptcha':
        site_key = get_hcaptcha_site_key()
        secret = get_hcaptcha_secret_key()
        if not site_key or not secret:
            return None
        token = (request.form.get('h-captcha-response') or request.form.get('g-recaptcha-response') or '').strip()
        if not token:
            return False
        payload = urllib.parse.urlencode({
            'secret': secret, 'response': token, 'remoteip': request.remote_addr or ''
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://hcaptcha.com/siteverify',
            data=payload, headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8', 'ignore') or '{}')
            return bool(data.get('success'))
        except Exception:
            return False
    return None


# ── TOTP (2FA) – RFC 6238, dependency-free ────────────────────────────────────

import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac
import struct as _struct

def totp_generate_secret() -> str:
    return _b64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')

def _totp_code(secret: str, counter: int) -> str:
    pad = '=' * ((8 - len(secret) % 8) % 8)
    key = _b64.b32decode((secret + pad).upper())
    msg = _struct.pack('>Q', counter)
    digest = _hmac.new(key, msg, _hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (_struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f'{code:06d}'

def totp_verify(secret: str, code: str, window: int = 1) -> bool:
    """Verify a 6-digit code; allows +/- `window` 30s steps for clock drift."""
    if not secret or not (code or '').strip().isdigit() or len(code.strip()) != 6:
        return False
    import time as _time
    counter = int(_time.time()) // 30
    code = code.strip()
    # constant-time-ish comparison across the drift window
    ok = False
    result = 0
    for off in range(-window, window + 1):
        expected = _totp_code(secret, counter + off)
        result |= _hmac.compare_digest(code, expected)
    ok = bool(result)
    return ok

def totp_provisioning_uri(secret: str, account: str, issuer: str = 'The Bottom List') -> str:
    from urllib.parse import quote as _quote
    pad = '=' * ((8 - len(secret) % 8) % 8)
    return (
        f'otpauth://totp/{_quote(issuer)}:{_quote(account)}'
        f'?secret={secret}{pad}&issuer={_quote(issuer)}&algorithm=SHA1&digits=6&period=30'
    )

# ── Password trimming ─────────────────────────────────────────────────────────

def trim_pw(s: str) -> str:
    try:
        return s.encode('utf-8')[:72].decode('utf-8', 'ignore')
    except Exception:
        return s


# ── Encryption helpers (for tokens stored in DB) ──────────────────────────────

_DB_ENCRYPTION_KEY: str | None = None

def _get_db_encryption_key() -> str:
    """Dedicated key for DB-field encryption, independent of SECRET_KEY so
    rotating the session key never destroys encrypted data."""
    global _DB_ENCRYPTION_KEY
    if _DB_ENCRYPTION_KEY is not None:
        return _DB_ENCRYPTION_KEY
    import os as _os
    from flask import current_app
    try:
        env = (_os.environ.get('DB_ENCRYPTION_KEY') or '').strip()
        if env:
            _DB_ENCRYPTION_KEY = env
            return _DB_ENCRYPTION_KEY
        key_path = _os.path.join(current_app.instance_path, 'db_encryption_key.txt')
        if _os.path.exists(key_path):
            with open(key_path, 'r', encoding='utf-8') as f:
                s = (f.read() or '').strip()
                if s:
                    _DB_ENCRYPTION_KEY = s
                    return _DB_ENCRYPTION_KEY
        generated = secrets.token_urlsafe(64)
        _os.makedirs(current_app.instance_path, exist_ok=True)
        with open(key_path, 'w', encoding='utf-8') as f:
            f.write(generated)
        _DB_ENCRYPTION_KEY = generated
        return _DB_ENCRYPTION_KEY
    except Exception:
        # Last resort only - keeps old behaviour rather than crashing.
        return (current_app.config.get('SECRET_KEY') or 'insecure-fallback')


def get_fernet(secret_key: str | None = None):
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    if secret_key is None:
        secret_key = _get_db_encryption_key()
    key = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token(token: str) -> str:
    if not token:
        return ''
    return get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    if not encrypted:
        return ''
    try:
        return get_fernet().decrypt(encrypted.encode()).decode()
    except Exception:
        return ''
