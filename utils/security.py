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

from settings_helpers import get_setting

_RATE_LIMIT: dict = {}
_RATE_LIMIT_LOCK = threading.Lock()

_ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
_ALLOWED_AUDIO_EXTS = {'mp3', 'wav', 'ogg'}


# ── Rate limiting ─────────────────────────────────────────────────────────────

def client_ip() -> str:
    xf = request.headers.get('X-Forwarded-For', '')
    if xf:
        return (xf.split(',')[0] or '').strip() or 'unknown'
    return request.remote_addr or 'unknown'


def rate_limited(bucket: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    key = f'{bucket}:{client_ip()}'
    with _RATE_LIMIT_LOCK:
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
    form_token = (
        request.form.get('_csrf_token')
        or request.args.get('_csrf_token')
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
        proto = (request.headers.get('X-Forwarded-Proto') or '').lower()
        if request.is_secure or proto == 'https':
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


# ── Password trimming ─────────────────────────────────────────────────────────

def trim_pw(s: str) -> str:
    try:
        return s.encode('utf-8')[:72].decode('utf-8', 'ignore')
    except Exception:
        return s


# ── Encryption helpers (for tokens stored in DB) ──────────────────────────────

def get_fernet(secret_key: str):
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    key = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token(token: str, secret_key: str) -> str:
    if not token:
        return ''
    return get_fernet(secret_key).encrypt(token.encode()).decode()


def decrypt_token(encrypted: str, secret_key: str) -> str:
    if not encrypted:
        return ''
    try:
        return get_fernet(secret_key).decrypt(encrypted.encode()).decode()
    except Exception:
        return ''
