"""
discord_webhooks.py – Discord webhook sending, site-log helpers, Discord OAuth helpers.
"""
import json
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime

from flask import has_request_context, request, current_app
from flask_login import current_user

import auxiliary.settings_helpers as sh
from config import WEBHOOK_URL, LOG_WEBHOOK_URL  # re-exported for other modules

# Event filter choice lists used by admin panel
_ANNOUNCE_WEBHOOK_EVENT_CHOICES = [
    ('record_approved', 'Record approved'),
    ('level_submission_approved', 'Level submission approved'),
    ('level_moved', 'Level moved'),
    ('level_deleted', 'Level deleted'),
    ('level_verified', 'Level verified'),
    ('level_unverified', 'Level unverified'),
    ('site_shutdown', 'Site shutdown'),
    ('test', 'Test messages'),
]
_LOG_WEBHOOK_EVENT_CHOICES = [
    ('admin_action', 'Admin actions'),
    ('points_change', 'Points changes'),
    ('level_create', 'Level creations'),
    ('level_move', 'Level moves'),
    ('profile_edit', 'Profile edits'),
    ('login', 'Logins'),
    ('account_create', 'Account creates'),
    ('account_link', 'Account links'),
    ('account_link_fail', 'Account link fails'),
    ('account_unlink', 'Account unlinks'),
    ('account_delete', 'Account deletes'),
    ('record_submit', 'Record submissions'),
    ('level_submit', 'Level submissions'),
    ('guidelines_accept', 'Guidelines accepts'),
    ('site_launch', 'Site launches'),
    ('site_shutdown', 'Site shutdowns'),
    ('data_save', 'Data saves'),
    ('test', 'Test messages'),
]

_shutdown_webhook_lock = threading.Lock()
_shutdown_webhook_sent = False

_launch_webhook_lock = threading.Lock()
_launch_webhook_sent = False
_launch_webhook_last_attempt_ts = 0


# ── Low-level HTTP helpers ────────────────────────────────────────────────────

def post_discord_webhook(url: str, content: str, timeout: int = 6):
    try:
        payload = json.dumps({"content": content})
        req = urllib.request.Request(
            url, data=payload.encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 204) or 204
            if int(status) >= 400:
                try:
                    body = resp.read().decode('utf-8', 'ignore')
                except Exception:
                    body = ''
                return False, f'HTTP {status} {body[:300]}'.strip()
        return True, ''
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', 'ignore')
        except Exception:
            body = ''
        return False, f'HTTP {e.code} {body[:300]}'.strip()
    except urllib.error.URLError as e:
        return False, f"URL error: {getattr(e, 'reason', e)}"
    except Exception as e:
        return False, f'Error: {e}'


def post_discord_webhook_payload(url: str, payload_obj, timeout: int = 6):
    try:
        payload = json.dumps(payload_obj)
        req = urllib.request.Request(
            url, data=payload.encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 204) or 204
            if int(status) >= 400:
                try:
                    body = resp.read().decode('utf-8', 'ignore')
                except Exception:
                    body = ''
                return False, f'HTTP {status} {body[:300]}'.strip()
        return True, ''
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', 'ignore')
        except Exception:
            body = ''
        return False, f'HTTP {e.code} {body[:300]}'.strip()
    except urllib.error.URLError as e:
        return False, f"URL error: {getattr(e, 'reason', e)}"
    except Exception as e:
        return False, f'Error: {e}'


# ── Event filtering ───────────────────────────────────────────────────────────

def _parse_event_filter_text(raw: str):
    s = (raw or '').strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [str(v or '').strip().lower() for v in data if str(v or '').strip()]
    except Exception:
        pass
    parts = []
    for p in s.replace('\n', ',').split(','):
        t = (p or '').strip().lower()
        if t:
            parts.append(t)
    return parts


def get_event_filter_set(setting_key: str):
    raw = (sh.get_setting(setting_key) or '').strip()
    items = _parse_event_filter_text(raw)
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return set(out)


def save_event_filter(setting_key: str, values):
    cleaned = []
    seen = set()
    for v in (values or []):
        t = str(v or '').strip().lower()
        if t and t not in seen:
            seen.add(t)
            cleaned.append(t)
    sh.set_setting(setting_key, json.dumps(cleaned) if cleaned else '')


def event_allowed(setting_key: str, event_type: str) -> bool:
    filt = get_event_filter_set(setting_key)
    if not filt:
        return True
    return (event_type or 'event').strip().lower() in filt


# ── Public send helpers ───────────────────────────────────────────────────────

def send_discord_message(content: str, event_type: str = 'event'):
    if not event_allowed('discord_webhook_events', event_type):
        return
    url = sh.get_setting('discord_webhook_url') or WEBHOOK_URL
    if not url:
        return
    ok, _ = post_discord_webhook(url, content)
    return ok


def send_discord_log_message(content: str):
    url = sh.get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL
    if not url:
        return
    ok, _ = post_discord_webhook(url, content)
    return ok


# ── Rich embed log payload ────────────────────────────────────────────────────

def _is_sensitive_log_key(key: str) -> bool:
    k = (key or '').strip().lower()
    if not k or k in ('q', 'cat', 'page', 'sort', 'device', 'status', 'list', 'level_id', 'user_id', 'clan_id'):
        return False
    return bool(re.search(r'(pass|password|secret|token|api[_-]?key|webhook|hcaptcha|recaptcha|captcha|code|state|session|cookie)', k))


_TOKENISH_RE = re.compile(r'[A-Za-z0-9_\-]{28,}')

def _scrub_secret_values(text: str) -> str:
    """Value-level defense: mask long unbroken token-like strings even when
    the surrounding key name was not flagged as sensitive."""
    try:
        return _TOKENISH_RE.sub('[redacted]', str(text or ''))
    except Exception:
        return text


def _truncate_text(text: str, limit: int) -> str:
    s = str(text or '')
    if len(s) <= int(limit):
        return s
    return s[:max(0, int(limit) - 1)] + '…'


def _format_pairs(pairs: list, *, empty: str = '—', limit_chars: int = 950) -> str:
    if not pairs:
        return empty
    parts = []
    total = 0
    for k, v in pairs:
        if _is_sensitive_log_key(k):
            shown = '[redacted]'
        else:
            shown = _scrub_secret_values(v)
        seg = f'{k}={shown}'
        if total + len(seg) + (2 if parts else 0) > limit_chars:
            parts.append('…')
            break
        parts.append(seg)
        total += len(seg) + (2 if parts else 0)
    return ', '.join(parts)


def _get_request_client_ip() -> str:
    if not has_request_context():
        return ''
    try:
        xf = (request.headers.get('X-Forwarded-For') or '').strip()
    except Exception:
        xf = ''
    if xf:
        return xf.split(',')[0].strip()
    for hdr in ('CF-Connecting-IP', 'True-Client-IP', 'X-Real-IP'):
        try:
            v = (request.headers.get(hdr) or '').strip()
        except Exception:
            v = ''
        if v:
            return v
    try:
        return (request.remote_addr or '').strip()
    except Exception:
        return ''


def discord_log_payload(event_type: str, message: str, actor_user_id=None):
    from config import db
    from models import User
    ev = (event_type or 'event')[:50]
    msg = (message or '')[:4000]
    actor_line = 'system'
    if actor_user_id:
        try:
            u = db.session.get(User, actor_user_id)
            if u and u.username:
                actor_line = f'{u.username} (id={u.id}, rank={getattr(u, "rank", "") or ""})'.strip()
        except Exception:
            pass
    elif has_request_context() and getattr(current_user, 'is_authenticated', False):
        try:
            actor_line = f'{current_user.username} (id={current_user.id}, rank={getattr(current_user, "rank", "") or ""})'.strip()
        except Exception:
            pass

    fields = [
        {"name": "Event", "value": _truncate_text(ev, 256), "inline": True},
        {"name": "Actor", "value": _truncate_text(actor_line, 1024), "inline": True},
    ]

    if has_request_context():
        try:
            endpoint = str(request.endpoint or '')
            method = str(request.method or '').upper()
            path = str(request.path or '')
            full_url = str(request.url or '')
            req_bits = [b for b in [endpoint, f'{method} {path}' if method and path else '', full_url] if b]
            if req_bits:
                fields.append({"name": "Request", "value": _truncate_text('\n'.join(req_bits), 1024), "inline": False})
        except Exception:
            pass

    color_map = {
        'admin_action': 0xEF4444, 'account_delete': 0xEF4444,
        'points_change': 0xF59E0B,
        'login': 0x3B82F6, 'account_link': 0x3B82F6, 'account_unlink': 0x3B82F6,
        'site_launch': 0x22C55E, 'data_save': 0x22C55E,
    }
    color = color_map.get(ev.strip().lower(), 0x64748B)

    return {
        "embeds": [{
            "title": f'[{ev}]',
            "description": _truncate_text(msg, 3500),
            "color": int(color),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "fields": fields[:25],
        }]
    }


# ── add_site_log ──────────────────────────────────────────────────────────────

def add_site_log(event_type: str, message: str, actor_user_id=None, send_to_discord=True):
    from config import db
    from models import SiteLog
    ev = (event_type or 'event')[:50]
    msg = (message or '')[:4000]
    try:
        row = SiteLog(kind=ev, body=msg, actor_user_id=actor_user_id)
        db.session.add(row)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    if not send_to_discord:
        return
    if not event_allowed('discord_log_webhook_events', ev):
        return
    try:
        url = sh.get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL
        payload = discord_log_payload(ev, msg, actor_user_id=actor_user_id)
        if url:
            ok, _ = post_discord_webhook_payload(url, payload)
            if ok:
                return
        # Fallback to plain text
        send_discord_log_message(f'[{ev}] {msg}'.strip()[:1900])
    except Exception:
        pass


# ── Site launch / shutdown announce ──────────────────────────────────────────

def _get_uptime_webhook_target():
    url = (sh.get_setting('discord_site_launch_webhook_url') or '').strip()
    if url:
        return url, True
    return (sh.get_setting('discord_webhook_url') or WEBHOOK_URL or '').strip(), False


def _post_uptime_webhook(message: str, *, title: str, color: int, require_startup_webhook: bool = False):
    url, want_embed = _get_uptime_webhook_target()
    if require_startup_webhook and not want_embed:
        return False, 'startup_webhook_not_set'
    if not url:
        return False, 'missing_webhook'
    if want_embed:
        payload = {"embeds": [{"title": title, "description": message, "color": int(color), "timestamp": datetime.utcnow().isoformat() + "Z"}]}
        return post_discord_webhook_payload(url, payload)
    return post_discord_webhook(url, message)


def announce_site_launch(app):
    global _launch_webhook_sent, _launch_webhook_last_attempt_ts
    with _launch_webhook_lock:
        now = time.time()
        if _launch_webhook_sent:
            return
        if _launch_webhook_last_attempt_ts and (now - _launch_webhook_last_attempt_ts) < 2:
            return
        _launch_webhook_last_attempt_ts = now
    try:
        with app.app_context():
            now_ts = int(time.time())
            try:
                last_ts = int(float(sh.get_setting('last_site_launch_webhook_ok_ts') or 0))
            except Exception:
                last_ts = 0
            if last_ts and (now_ts - last_ts) < 60:
                return
            ok = False
            try:
                ok, err = _post_uptime_webhook('Server is online.', title='Server online', color=0x22C55E)
                if not ok and err:
                    add_site_log('site_launch', f'Launch webhook failed: {err}', actor_user_id=None, send_to_discord=False)
            except Exception:
                ok = False
            if ok:
                with _launch_webhook_lock:
                    _launch_webhook_sent = True
                try:
                    sh.set_setting('last_site_launch_webhook_ok_ts', str(now_ts))
                except Exception:
                    pass
            try:
                add_site_log('site_launch', 'Site launched.', actor_user_id=None, send_to_discord=False)
            except Exception:
                pass
    except Exception:
        pass


def announce_site_shutdown(app):
    global _shutdown_webhook_sent
    with _shutdown_webhook_lock:
        if _shutdown_webhook_sent:
            return
        _shutdown_webhook_sent = True
    try:
        with app.app_context():
            now_ts = int(time.time())
            try:
                last_ts = int(float(sh.get_setting('last_site_shutdown_webhook_ok_ts') or 0))
            except Exception:
                last_ts = 0
            if last_ts and (now_ts - last_ts) < 60:
                return
            try:
                ok, _ = _post_uptime_webhook('Server is shutting down.', title='Server shutting down', color=0xEF4444, require_startup_webhook=True)
                if ok:
                    try:
                        sh.set_setting('last_site_shutdown_webhook_ok_ts', str(now_ts))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                add_site_log('site_shutdown', 'Server is shutting down.', actor_user_id=None, send_to_discord=False)
            except Exception:
                pass
    except Exception:
        pass


# ── Discord OAuth helpers ─────────────────────────────────────────────────────

def get_discord_client_id() -> str:
    from flask import current_app
    return (sh.get_setting('discord_client_id') or current_app.config.get('DISCORD_CLIENT_ID') or '').strip()


def get_discord_client_secret() -> str:
    from flask import current_app
    return (sh.get_setting('discord_client_secret') or current_app.config.get('DISCORD_CLIENT_SECRET') or '').strip()


def get_discord_redirect_uri() -> str:
    from flask import current_app
    uri = (sh.get_setting('discord_redirect_uri') or current_app.config.get('DISCORD_REDIRECT_URI') or '').strip()
    if uri and uri.startswith('http://'):
        uri = 'https://' + uri[7:]
    return uri


def discord_oauth_ready() -> bool:
    return bool(get_discord_client_id() and get_discord_client_secret() and get_discord_redirect_uri())
