from flask import Flask, render_template, redirect, url_for, flash, request, abort, session, jsonify, send_from_directory, send_file, Response, has_request_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
from models import db, User, Clan, ClanJoinRequest, Level, Record, LevelSubmission, NewsPost, Page, Event, Setting, SiteLog, CustomList, MapPack, SongSubmission, LevelNong

import os
import json
import atexit
import signal
import sys
import urllib.request
import urllib.error
import urllib.parse
import shutil
import threading
import time
import re
from datetime import datetime, timedelta

import secrets
import string
import uuid
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from werkzeug.exceptions import NotFound, RequestEntityTooLarge
from sqlalchemy.exc import OperationalError

app = Flask(__name__)
WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '')
LOG_WEBHOOK_URL = os.environ.get('DISCORD_LOGS_WEBHOOK_URL', '')

def _load_secret_key():
    env = (os.environ.get('SECRET_KEY') or '').strip()
    if env:
        return env
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except Exception:
        pass
    key_path = os.path.join(app.instance_path, 'secret_key.txt')
    try:
        if os.path.exists(key_path):
            with open(key_path, 'r', encoding='utf-8') as f:
                s = (f.read() or '').strip()
                if s:
                    return s
    except Exception:
        pass
    generated = secrets.token_urlsafe(64)
    try:
        with open(key_path, 'w', encoding='utf-8') as f:
            f.write(generated)
    except Exception:
        pass
    return generated

app.config['SECRET_KEY'] = _load_secret_key()

from cryptography.fernet import Fernet
import base64
import hashlib

def _get_fernet():
    key = hashlib.sha256(app.config['SECRET_KEY'].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))

def _encrypt_token(token: str) -> str:
    if not token: return ''
    f = _get_fernet()
    return f.encrypt(token.encode()).decode()

def _decrypt_token(encrypted: str) -> str:
    if not encrypted: return ''
    try:
        f = _get_fernet()
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        return ''

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SONG_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'songs')
app.config['VC_PROOF_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'vc_proofs')
app.config['RECORD_PROOF_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'record_proofs')
app.config['PROFILE_MEDIA_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'profile_media')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
app.config['RECAPTCHA_SITE_KEY'] = os.environ.get('RECAPTCHA_SITE_KEY', '')
app.config['RECAPTCHA_SECRET_KEY'] = os.environ.get('RECAPTCHA_SECRET_KEY', '')

@app.route('/assets/discordicon.png')
def discord_icon_png():
    p = os.path.join(app.root_path, 'templates', 'discordicon.png')
    try:
        return send_file(p, mimetype='image/png')
    except Exception:
        abort(404)
app.config['HCAPTCHA_SITE_KEY'] = os.environ.get('HCAPTCHA_SITE_KEY', '')
app.config['HCAPTCHA_SECRET_KEY'] = os.environ.get('HCAPTCHA_SECRET_KEY', '')
app.config['DISCORD_CLIENT_ID'] = os.environ.get('DISCORD_CLIENT_ID', '')
app.config['DISCORD_CLIENT_SECRET'] = os.environ.get('DISCORD_CLIENT_SECRET', '')
app.config['DISCORD_REDIRECT_URI'] = os.environ.get('DISCORD_REDIRECT_URI', '')
app.config['DISCORD_GUILD_ID'] = os.environ.get('DISCORD_GUILD_ID', '')
app.config['DISCORD_BOT_TOKEN'] = os.environ.get('DISCORD_BOT_TOKEN', '')
app.config['TEMPLATES_AUTO_RELOAD'] = True
_USE_SECURE_COOKIES = str(os.environ.get('COOKIE_SECURE') or '').strip().lower() in ('1', 'true', 'yes', 'on')
app.config['SESSION_COOKIE_SECURE'] = _USE_SECURE_COOKIES
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = _USE_SECURE_COOKIES
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.jinja_env.auto_reload = True

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db.init_app(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
application = app

_db_bootstrapped = False

_RATE_LIMIT = {}
_RATE_LIMIT_LOCK = threading.Lock()

_TTL_CACHE = {}
_TTL_CACHE_LOCK = threading.Lock()

def _cache_get(key: str):
    now = time.time()
    with _TTL_CACHE_LOCK:
        item = _TTL_CACHE.get(key)
        if not item:
            return None
        exp, val = item
        try:
            if exp and float(exp) < now:
                _TTL_CACHE.pop(key, None)
                return None
        except Exception:
            _TTL_CACHE.pop(key, None)
            return None
        return val

def _cache_set(key: str, val, ttl_seconds: int):
    try:
        ttl = int(ttl_seconds or 0)
    except Exception:
        ttl = 0
    if ttl <= 0:
        return
    exp = time.time() + ttl
    with _TTL_CACHE_LOCK:
        _TTL_CACHE[key] = (exp, val)

def _cache_bust_prefixes(prefixes):
    if not prefixes:
        return
    prefs = tuple([str(p or '') for p in prefixes if str(p or '')])
    if not prefs:
        return
    with _TTL_CACHE_LOCK:
        keys = list(_TTL_CACHE.keys())
        for k in keys:
            try:
                if any(str(k).startswith(p) for p in prefs):
                    _TTL_CACHE.pop(k, None)
            except Exception:
                continue

_ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'webp'}
_ALLOWED_AUDIO_EXTS = {'mp3', 'wav', 'ogg'}

def _client_ip():
    xf = request.headers.get('X-Forwarded-For', '')
    if xf:
        return (xf.split(',')[0] or '').strip() or 'unknown'
    return request.remote_addr or 'unknown'

def _file_ext(name: str):
    s = (name or '').rsplit('.', 1)
    if len(s) != 2:
        return ''
    return (s[1] or '').lower().strip()

def _save_uploaded_file(f, folder: str, allowed_exts: set, want_mime_prefix: str):
    if not f or not getattr(f, 'filename', None):
        return None, 'missing_file'
    if _rate_limited('upload', limit=10, window_seconds=60):
        return None, 'rate_limited'
    filename = secure_filename(f.filename)
    if not filename:
        return None, 'bad_name'
    ext = _file_ext(filename)
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

def _rate_limited(bucket: str, limit: int, window_seconds: int):
    now = time.time()
    key = f'{bucket}:{_client_ip()}'
    with _RATE_LIMIT_LOCK:
        hits = [t for t in (_RATE_LIMIT.get(key) or []) if (now - t) < window_seconds]
        if len(hits) >= limit:
            _RATE_LIMIT[key] = hits
            return True
        hits.append(now)
        _RATE_LIMIT[key] = hits
    return False

@app.before_request
def _bootstrap_db_once():
    global _db_bootstrapped
    if _db_bootstrapped:
        return
    try:
        db.create_all()
        ensure_schema()
    except Exception:
        pass
    try:
        _announce_site_launch()
    except Exception:
        pass
    _db_bootstrapped = True

@app.before_request
def _rate_limit_requests():
    if request.path.startswith('/api/'):
        if _rate_limited('api', limit=60, window_seconds=60):
            abort(429)
        return
    if request.method == 'POST':
        if request.endpoint in ('login', 'register'):
            if _rate_limited(f'ep:{request.endpoint}', limit=15, window_seconds=60):
                abort(429)
        else:
            if _rate_limited('post', limit=90, window_seconds=60):
                abort(429)

@app.after_request
def _add_security_headers(resp):
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
                "frame-src 'self' https://hcaptcha.com https://*.hcaptcha.com https://www.google.com https://www.recaptcha.net https://www.youtube.com https://www.youtube-nocookie.com; "
                "img-src 'self' data: https:; "
                "media-src 'self' https:; "
                "script-src 'self' 'unsafe-inline' https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "connect-src 'self' https: wss:;"
            )
        proto = (request.headers.get('X-Forwarded-Proto') or '').lower()
        if request.is_secure or proto == 'https':
            resp.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    except Exception:
        pass
    return resp

@app.errorhandler(RequestEntityTooLarge)
def _handle_too_large(_e):
    flash('File too large (max 10MB).', 'danger')
    try:
        return redirect(request.referrer or url_for('home'))
    except Exception:
        return 'File too large (max 10MB).', 413

def _get_setting(key: str):
    try:
        row = db.session.get(Setting, key)
        return row.value if row else None
    except Exception as e:
        print(f'_get_setting error for {key}: {e}')
        return None

def _set_setting(key: str, value: str):
    row = db.session.get(Setting, key)
    if not row:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()

def _post_discord_webhook(url: str, content: str, timeout: int = 6):
    try:
        payload = json.dumps({"content": content})
        req = urllib.request.Request(
            url,
            data=payload.encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0'
            }
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


def _get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def _validate_csrf():
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return True
    if request.path.startswith('/api/'):
        return True
    token = session.get('_csrf_token') or ''
    form_token = request.form.get('_csrf_token') or request.args.get('_csrf_token') or request.headers.get('X-CSRF-Token') or ''
    if not token or not form_token:
        return False
    try:
        return secrets.compare_digest(str(token), str(form_token))
    except Exception:
        return False


@app.before_request
def _enforce_csrf_token():
    if request.method == 'POST':
        if not _validate_csrf():
            try:
                flash('Security check failed. Please refresh the page and try again.', 'danger')
                return redirect(request.referrer or url_for('home'))
            except Exception:
                abort(403)


app.jinja_env.globals['csrf_token'] = _get_csrf_token
app.jinja_env.globals['get_setting'] = _get_setting


def _ensure_api_token_for(user):
    try:
        tok = (getattr(user, 'api_token', '') or '').strip()
    except Exception:
        tok = ''
    if tok:
        return tok
    tok = secrets.token_urlsafe(32)
    try:
        user.api_token = tok
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    return tok


def _api_require_user():
    if getattr(current_user, 'is_authenticated', False) and not bool(getattr(current_user, 'is_disabled', False)):
        token = (request.headers.get('X-API-Token') or request.args.get('api_token') or '').strip()
        if not token:
            return None, ('missing_token', 401)
        expected = (getattr(current_user, 'api_token', '') or '').strip()
        if not expected:
            expected = _ensure_api_token_for(current_user)
        if not expected:
            return None, ('invalid_token', 403)
        try:
            ok = secrets.compare_digest(str(expected), str(token))
        except Exception:
            ok = False
        if not ok:
            return None, ('invalid_token', 403)
        return current_user, None

    username = (request.headers.get('X-API-Username') or request.args.get('api_username') or '').strip()
    raw_pw = request.headers.get('X-API-Password') or request.args.get('api_password') or ''
    password = _trim_pw(raw_pw) if raw_pw is not None else ''
    token = (request.headers.get('X-API-Token') or request.args.get('api_token') or '').strip()

    if not username or not password or not token:
        return None, ('login_required', 401)

    user = User.query.filter_by(username=username).first()
    if not user:
        return None, ('invalid_credentials', 401)
    if bool(getattr(user, 'is_disabled', False)):
        return None, ('forbidden', 403)

    expected = (getattr(user, 'api_token', '') or '').strip()
    if not expected:
        expected = _ensure_api_token_for(user)
    if not expected:
        return None, ('invalid_token', 403)
    try:
        ok_token = secrets.compare_digest(str(expected), str(token))
    except Exception:
        ok_token = False
    if not ok_token:
        return None, ('invalid_token', 403)

    try:
        ok_pw = bcrypt.check_password_hash(user.password, password)
    except Exception:
        ok_pw = False
    if not ok_pw:
        return None, ('invalid_credentials', 401)

    return user, None


def _api_auth():
    user, err = _api_require_user()
    if err:
        key, status = err
        return None, (jsonify({'error': key}), status)
    try:
        msg = f'{request.method} {request.path} via token for {getattr(user, "username", "unknown")} (id={getattr(user, "id", "unknown")})'
        add_site_log('api_call', msg, actor_user_id=getattr(user, 'id', None), send_to_discord=False)
    except Exception:
        pass
    return user, None


def _roulette_require_user():
    if not getattr(current_user, 'is_authenticated', False):
        return None, ('login_required', 401)
    if bool(getattr(current_user, 'is_disabled', False)):
        return None, ('forbidden', 403)
    return current_user, None


@app.route('/custom-lists')
def custom_lists():
    lists = CustomList.query.order_by(CustomList.created_at.desc()).all()
    # Warning always shows per request
    return render_template('custom_lists.html', lists=lists, show_warning=True)

@app.route('/admin/custom-lists', methods=['GET', 'POST'])
@login_required
def admin_custom_lists():
    _require_perm('manage_settings')
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'add':
            name = (request.form.get('name') or '').strip()
            url = (request.form.get('url') or '').strip()
            if not name or not url:
                flash('Name and URL are required.', 'danger')
            else:
                cl = CustomList(name=name, spreadsheet_url=url)
                db.session.add(cl)
                db.session.commit()
                flash('Custom list added.', 'success')
        elif action == 'delete':
            cid = request.form.get('id')
            cl = CustomList.query.get(cid)
            if cl:
                db.session.delete(cl)
                db.session.commit()
                flash('Custom list deleted.', 'success')
        return redirect(url_for('admin_custom_lists'))
    lists = CustomList.query.order_by(CustomList.created_at.desc()).all()
    return render_template('admin_custom_lists.html', lists=lists)

@app.route('/map-packs')
def map_packs():
    packs = MapPack.query.order_by(MapPack.created_at.desc()).all()
    
    # Resolve beaten levels for the current user
    beaten_ids = set()
    if current_user.is_authenticated:
        try:
            beaten_ids = set(
                lid
                for (lid,) in (
                    db.session.query(Record.level_id)
                    .filter(Record.user_id == current_user.id, Record.status == 'approved')
                    .distinct()
                    .all()
                )
                if lid
            )
        except Exception:
            beaten_ids = set()

    resolved_packs = []
    all_level_ids = []
    per_pack_ids = {}
    for p in packs:
        ids = [int(i.strip()) for i in (p.level_ids or '').split(',') if i.strip().isdigit()]
        per_pack_ids[p.id] = ids
        for i in ids:
            if i not in all_level_ids:
                all_level_ids.append(i)
    level_map = {}
    if all_level_ids:
        try:
            lv_rows = Level.query.filter(Level.id.in_(all_level_ids)).all()
            level_map = {l.id: l for l in lv_rows}
        except Exception:
            level_map = {}
    for p in packs:
        ids = per_pack_ids.get(p.id) or []
        sorted_levels = [level_map[i] for i in ids if i in level_map]
        
        beaten_count = 0
        for l in sorted_levels:
            if l.id in beaten_ids:
                beaten_count += 1
        
        total_count = len(sorted_levels)
        percent = (beaten_count / total_count * 100) if total_count > 0 else 0
        
        resolved_packs.append({
            'pack': p, 
            'levels': sorted_levels,
            'beaten_count': beaten_count,
            'total_count': total_count,
            'percent': round(percent, 1),
            'beaten_ids': beaten_ids
        })
    return render_template('map_packs.html', packs=resolved_packs)

@app.route('/admin/map-packs', methods=['GET', 'POST'])
@login_required
def admin_map_packs():
    _require_perm('manage_settings')
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'add':
            name = (request.form.get('name') or '').strip()
            ids = (request.form.get('level_ids') or '').strip()
            
            thumb_filename = None
            if 'thumbnail' in request.files:
                file = request.files['thumbnail']
                if file and file.filename:
                    ext = os.path.splitext(file.filename)[1].lower()
                    if ext in ('.png', '.jpg', '.jpeg', '.webp'):
                        thumb_filename = f"mappack_{uuid.uuid4().hex}{ext}"
                        tdir = os.path.join(app.root_path, 'static', 'mappack_thumbs')
                        os.makedirs(tdir, exist_ok=True)
                        file.save(os.path.join(tdir, thumb_filename))
            
            if not name:
                flash('Name is required.', 'danger')
            else:
                mp = MapPack(name=name, level_ids=ids, thumbnail_filename=thumb_filename)
                db.session.add(mp)
                db.session.commit()
                flash('Map pack added.', 'success')
        elif action == 'delete':
            mid = request.form.get('id')
            mp = MapPack.query.get(mid)
            if mp:
                # delete thumb if exists
                if mp.thumbnail_filename:
                    try:
                        os.remove(os.path.join(app.root_path, 'static', 'mappack_thumbs', mp.thumbnail_filename))
                    except Exception:
                        pass
                db.session.delete(mp)
                db.session.commit()
                flash('Map pack deleted.', 'success')
        return redirect(url_for('admin_map_packs'))
    packs = MapPack.query.order_by(MapPack.created_at.desc()).all()
    return render_template('admin_map_packs.html', packs=packs)

@app.route('/admin/shutdown', methods=['POST'])
@login_required
def admin_shutdown():
    _require_perm('manage_settings')
    func = request.environ.get('werkzeug.server.shutdown')
    if not func:
        return jsonify({'ok': False, 'error': 'shutdown_not_supported'}), 503
    add_site_log('admin_action', f'{current_user.username} triggered shutdown.', actor_user_id=current_user.id)
    func()
    return jsonify({'ok': True})

def _post_discord_webhook_payload(url: str, payload_obj, timeout: int = 6):
    try:
        payload = json.dumps(payload_obj)
        req = urllib.request.Request(
            url,
            data=payload.encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0'
            }
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

def send_discord_log_message(content: str):
    url = _get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL or _get_setting('discord_webhook_url') or WEBHOOK_URL
    if not url:
        return
    ok, _ = _post_discord_webhook(url, content)
    return ok

def _is_sensitive_log_key(key: str) -> bool:
    k = (key or '').strip().lower()
    if not k:
        return False
    if k in ('q', 'cat', 'page', 'sort', 'device', 'status', 'list', 'level_id', 'user_id', 'clan_id'):
        return False
    return bool(re.search(r'(pass|password|secret|token|api[_-]?key|webhook|hcaptcha|recaptcha|captcha|code|state|session|cookie)', k))

def _truncate_text(text: str, limit: int) -> str:
    s = str(text or '')
    if len(s) <= int(limit):
        return s
    return s[: max(0, int(limit) - 1)] + '…'

def _safe_pairs_from_multidict(md) -> list:
    out = []
    if not md:
        return out
    try:
        keys = list(md.keys())
    except Exception:
        keys = []
    for k in keys:
        try:
            vals = md.getlist(k)
        except Exception:
            try:
                vals = [md.get(k)]
            except Exception:
                vals = []
        if _is_sensitive_log_key(str(k)):
            out.append((str(k), '<redacted>'))
            continue
        try:
            if vals is None:
                out.append((str(k), ''))
            elif isinstance(vals, (list, tuple)):
                cleaned = []
                for v in vals[:5]:
                    cleaned.append(_truncate_text(str(v), 120))
                more = f' (+{len(vals) - 5} more)' if len(vals) > 5 else ''
                out.append((str(k), ', '.join(cleaned) + more))
            else:
                out.append((str(k), _truncate_text(str(vals), 120)))
        except Exception:
            out.append((str(k), '<unavailable>'))
    return out

def _safe_pairs_from_dict(d) -> list:
    out = []
    if not d:
        return out
    try:
        items = list(d.items())
    except Exception:
        items = []
    for k, v in items:
        if _is_sensitive_log_key(str(k)):
            out.append((str(k), '<redacted>'))
            continue
        if isinstance(v, (dict, list, tuple)):
            try:
                txt = json.dumps(v, ensure_ascii=False)
            except Exception:
                txt = str(v)
        else:
            txt = str(v)
        out.append((str(k), _truncate_text(txt, 200)))
    return out

def _format_pairs(pairs: list, *, empty: str = '—', limit_chars: int = 950) -> str:
    if not pairs:
        return empty
    parts = []
    total = 0
    for k, v in pairs:
        seg = f'{k}={v}'
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

def _discord_log_payload(event_type: str, message: str, actor_user_id=None):
    ev = (event_type or 'event')[:50]
    msg = (message or '')[:4000]
    actor_line = 'system'
    if actor_user_id:
        try:
            u = db.session.get(User, actor_user_id)
            if u and u.username:
                actor_line = f'{u.username} (id={u.id}, rank={getattr(u, "rank", "") or ""})'.strip()
        except Exception:
            actor_line = actor_line
    elif has_request_context() and getattr(current_user, 'is_authenticated', False):
        try:
            actor_line = f'{current_user.username} (id={current_user.id}, rank={getattr(current_user, "rank", "") or ""})'.strip()
        except Exception:
            actor_line = actor_line

    endpoint = ''
    method = ''
    path = ''
    full_url = ''
    ref = ''
    ua = ''
    ct = ''
    clen = ''
    ip = ''
    args_pairs = []
    form_pairs = []
    json_pairs = []
    if has_request_context():
        try:
            endpoint = str(request.endpoint or '').strip()
        except Exception:
            endpoint = ''
        try:
            method = str(request.method or '').strip().upper()
        except Exception:
            method = ''
        try:
            path = str(request.path or '').strip()
        except Exception:
            path = ''
        try:
            full_url = str(request.url or '').strip()
        except Exception:
            full_url = ''
        try:
            ref = str(request.referrer or '').strip()
        except Exception:
            ref = ''
        try:
            ua = str(request.headers.get('User-Agent') or '').strip()
        except Exception:
            ua = ''
        try:
            ct = str(request.headers.get('Content-Type') or '').strip()
        except Exception:
            ct = ''
        try:
            clen = str(request.headers.get('Content-Length') or '').strip()
        except Exception:
            clen = ''
        ip = _get_request_client_ip()
        try:
            args_pairs = _safe_pairs_from_multidict(request.args)
        except Exception:
            args_pairs = []
        try:
            form_pairs = _safe_pairs_from_multidict(request.form)
        except Exception:
            form_pairs = []
        try:
            if request.is_json:
                j = request.get_json(silent=True) or {}
                if isinstance(j, dict):
                    json_pairs = _safe_pairs_from_dict(j)
                else:
                    json_pairs = [('json', _truncate_text(str(j), 200))]
        except Exception:
            json_pairs = []

    fields = [
        {"name": "Event", "value": _truncate_text(ev, 256), "inline": True},
        {"name": "Actor", "value": _truncate_text(actor_line, 1024), "inline": True},
    ]

    req_bits = []
    if endpoint:
        req_bits.append(endpoint)
    if method and path:
        req_bits.append(f'{method} {path}')
    if full_url:
        req_bits.append(full_url)
    if req_bits:
        fields.append({"name": "Request", "value": _truncate_text('\n'.join(req_bits), 1024), "inline": False})

    client_bits = []
    if ct:
        client_bits.append(f'ct={ct}')
    if clen:
        client_bits.append(f'len={clen}')
    if client_bits:
        fields.append({"name": "Client", "value": _truncate_text(', '.join(client_bits), 1024), "inline": False})
    if ua:
        fields.append({"name": "User-Agent", "value": _truncate_text(ua, 1024), "inline": False})
    if ref:
        fields.append({"name": "Referrer", "value": _truncate_text(ref, 1024), "inline": False})

    qv = _format_pairs(args_pairs)
    fv = _format_pairs(form_pairs)
    jv = _format_pairs(json_pairs)
    if qv != '—':
        fields.append({"name": "Query", "value": _truncate_text(qv, 1024), "inline": False})
    if fv != '—':
        fields.append({"name": "Form", "value": _truncate_text(fv, 1024), "inline": False})
    if jv != '—':
        fields.append({"name": "JSON", "value": _truncate_text(jv, 1024), "inline": False})

    color = 0x64748B
    ev_l = (ev or '').strip().lower()
    if ev_l in ('admin_action', 'account_delete'):
        color = 0xEF4444
    elif ev_l in ('points_change',):
        color = 0xF59E0B
    elif ev_l in ('login', 'account_link', 'account_unlink'):
        color = 0x3B82F6
    elif ev_l in ('site_launch', 'data_save'):
        color = 0x22C55E

    payload = {
        "embeds": [
            {
                "title": f'[{ev}]',
                "description": _truncate_text(msg, 3500),
                "color": int(color),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "fields": fields[:25],
            }
        ]
    }
    return payload

def send_site_launch_webhook():
    url = (_get_setting('discord_site_launch_webhook_url') or '').strip()
    if not url:
        return False
    try:
        payload = {
            "embeds": [
                {
                    "title": "Site launched",
                    "description": "The site has started successfully.",
                    "color": 0x22C55E,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            ]
        }
    except Exception:
        payload = {"content": "Site launched."}
    ok, _ = _post_discord_webhook_payload(url, payload)
    return ok

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

def _parse_event_filter_text(raw: str):
    s = (raw or '').strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, list):
            out = []
            for v in data:
                t = str(v or '').strip().lower()
                if t:
                    out.append(t)
            return out
    except Exception:
        pass
    parts = []
    for p in s.replace('\n', ',').split(','):
        t = (p or '').strip().lower()
        if t:
            parts.append(t)
    return parts

def _get_event_filter_set(setting_key: str):
    raw = (_get_setting(setting_key) or '').strip()
    items = _parse_event_filter_text(raw)
    out = []
    seen = set()
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return set(out)

def _save_event_filter(setting_key: str, values):
    cleaned = []
    seen = set()
    for v in (values or []):
        t = str(v or '').strip().lower()
        if not t or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
    if cleaned:
        _set_setting(setting_key, json.dumps(cleaned))
    else:
        _set_setting(setting_key, '')

def _event_allowed(setting_key: str, event_type: str) -> bool:
    filt = _get_event_filter_set(setting_key)
    if not filt:
        return True
    ev = (event_type or '').strip().lower() or 'event'
    return ev in filt

def send_discord_message(content: str, event_type: str = 'event'):
    if not _event_allowed('discord_webhook_events', event_type):
        return
    url = _get_setting('discord_webhook_url') or WEBHOOK_URL
    if not url:
        return
    ok, _ = _post_discord_webhook(url, content)
    return ok

_shutdown_webhook_lock = threading.Lock()
_shutdown_webhook_sent = False

_launch_webhook_lock = threading.Lock()
_launch_webhook_sent = False
_launch_webhook_last_attempt_ts = 0

def _get_uptime_webhook_target():
    url = (_get_setting('discord_site_launch_webhook_url') or '').strip()
    if url:
        return url, True
    return (_get_setting('discord_webhook_url') or WEBHOOK_URL or '').strip(), False

def _post_uptime_webhook(message: str, *, title: str, color: int, require_startup_webhook: bool = False):
    url, want_embed = _get_uptime_webhook_target()
    if require_startup_webhook and not want_embed:
        return False, 'startup_webhook_not_set'
    if not url:
        return False, 'missing_webhook'
    if want_embed:
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": message,
                    "color": int(color),
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            ]
        }
        return _post_discord_webhook_payload(url, payload)
    return _post_discord_webhook(url, message)

def _announce_site_launch():
    global _launch_webhook_sent
    global _launch_webhook_last_attempt_ts
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
                last_ts = int(float(_get_setting('last_site_launch_webhook_ok_ts') or 0))
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
                    _set_setting('last_site_launch_webhook_ok_ts', str(now_ts))
                except Exception:
                    pass
            try:
                send_site_launch_webhook()
            except Exception:
                pass
            try:
                add_site_log('site_launch', 'Site launched.', actor_user_id=None, send_to_discord=False)
            except Exception:
                pass
    except Exception:
        pass

def _announce_site_shutdown():
    global _shutdown_webhook_sent
    with _shutdown_webhook_lock:
        if _shutdown_webhook_sent:
            return
        _shutdown_webhook_sent = True
    try:
        with app.app_context():
            now_ts = int(time.time())
            try:
                last_ts = int(float(_get_setting('last_site_shutdown_webhook_ok_ts') or 0))
            except Exception:
                last_ts = 0
            if last_ts and (now_ts - last_ts) < 60:
                return
            try:
                ok = False
                ok, _ = _post_uptime_webhook('Server is shutting down.', title='Server shutting down', color=0xEF4444, require_startup_webhook=True)
                if ok:
                    try:
                        _set_setting('last_site_shutdown_webhook_ok_ts', str(now_ts))
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

def _install_shutdown_signal_handlers():
    def _wrap(signum, prev_handler):
        def _handler(_signum, _frame):
            try:
                _announce_site_shutdown()
            except Exception:
                pass
            if callable(prev_handler):
                return prev_handler(_signum, _frame)
            if signum == getattr(signal, 'SIGINT', None):
                raise KeyboardInterrupt()
            raise SystemExit(0)
        return _handler

    for signum in (getattr(signal, 'SIGINT', None), getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGBREAK', None)):
        if signum is None:
            continue
        try:
            prev = signal.getsignal(signum)
            signal.signal(signum, _wrap(signum, prev))
        except Exception:
            continue

atexit.register(_announce_site_shutdown)
_install_shutdown_signal_handlers()

def _start_uptime_announce_thread():
    def _run():
        try:
            time.sleep(1.0)
        except Exception:
            pass
        try:
            with app.app_context():
                try:
                    db.create_all()
                except Exception:
                    pass
                try:
                    ensure_schema()
                except Exception:
                    pass
                _announce_site_launch()
        except Exception:
            pass
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass

if os.environ.get('WERKZEUG_RUN_MAIN') in (None, 'true'):
    _start_uptime_announce_thread()

def add_site_log(event_type: str, message: str, actor_user_id=None, send_to_discord=True):
    ev = (event_type or 'event')[:50]
    msg = (message or '')[:4000]
    try:
        row = SiteLog(
            event_type=ev,
            message=msg,
            actor_user_id=actor_user_id
        )
        db.session.add(row)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    if send_to_discord:
        if not _event_allowed('discord_log_webhook_events', ev):
            return
        try:
            url = _get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL or _get_setting('discord_webhook_url') or WEBHOOK_URL
            payload = _discord_log_payload(ev, msg, actor_user_id=actor_user_id)
            sent = False
            if url:
                ok, _ = _post_discord_webhook_payload(url, payload)
                sent = bool(ok)
            if not sent:
                actor = ''
                if actor_user_id:
                    try:
                        u = db.session.get(User, actor_user_id)
                        if u and u.username:
                            actor = u.username
                    except Exception:
                        actor = ''
                prefix = f'[{ev}]'
                if actor:
                    prefix = f'{prefix} ({actor})'
                ctx = ''
                if has_request_context():
                    try:
                        ep = str(request.endpoint or '').strip()
                    except Exception:
                        ep = ''
                    try:
                        m = str(request.method or '').strip().upper()
                    except Exception:
                        m = ''
                    try:
                        p = str(request.path or '').strip()
                    except Exception:
                        p = ''
                    parts = []
                    if ep:
                        parts.append(ep)
                    if m and p:
                        parts.append(f'{m} {p}')
                    if parts:
                        ctx = ' [' + ' | '.join(parts) + ']'
                out = f'{prefix}{ctx} {msg}'.strip()
                if len(out) > 1900:
                    out = out[:1900] + '…'
                send_discord_log_message(out)
        except Exception:
            pass

@app.before_request
def _touch_last_seen_at():
    if not getattr(current_user, 'is_authenticated', False):
        return
    if request.endpoint in ('static',):
        return
    now = datetime.utcnow()
    try:
        last = float(session.get('last_seen_ping_ts') or 0)
    except Exception:
        last = 0
    if last and (now.timestamp() - last) < 60:
        return
    try:
        current_user.last_seen_at = now
        db.session.commit()
        session['last_seen_ping_ts'] = now.timestamp()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

GUIDELINES_URL_DEFAULT = '/guidelines-site'

def _get_guidelines_url() -> str:
    return (_get_setting('guidelines_url') or GUIDELINES_URL_DEFAULT or '').strip()

def _get_guidelines_reset_ts() -> int:
    raw = str(_get_setting('guidelines_reset_ts') or '').strip()
    try:
        return int(float(raw))
    except Exception:
        return 0

def _needs_guidelines_acceptance(user) -> bool:
    try:
        accepted_at = getattr(user, 'guidelines_accepted_at', None)
    except Exception:
        accepted_at = None
    if not accepted_at:
        return True
    reset_ts = _get_guidelines_reset_ts()
    if not reset_ts:
        return False
    try:
        reset_dt = datetime.utcfromtimestamp(reset_ts)
    except Exception:
        return False
    return accepted_at < reset_dt

RANKS = ['member', 'active_member', 'mod', 'elder_moderator', 'admin']

def _rank_label(rank: str) -> str:
    r = (rank or '').strip().lower()
    if r == 'admin':
        return 'Admin'
    if r == 'elder_moderator':
        return 'Elder Moderator'
    if r == 'mod':
        return 'Mod'
    if r == 'active_member':
        return 'Active Member'
    return 'Member'

def _get_user_rank(user) -> str:
    if not user:
        return 'member'
    try:
        if bool(getattr(user, 'is_admin', False)):
            return 'admin'
    except Exception:
        pass
    try:
        raw = str(getattr(user, 'rank', '') or '').strip().lower()
    except Exception:
        raw = ''
    if raw in RANKS and raw != 'admin':
        return raw
    try:
        if bool(getattr(user, 'is_mod', False)):
            return 'mod'
    except Exception:
        pass
    return 'member'

PERMISSIONS = [
    ('access_admin', 'Access admin panel'),
    ('manage_levels', 'Create/edit/delete levels'),
    ('move_levels', 'Move/reorder levels'),
    ('moderate_records', 'Approve/reject records'),
    ('moderate_submissions', 'Approve/reject level submissions'),
    ('manage_news', 'Manage news posts'),
    ('manage_pages', 'Manage site pages'),
    ('manage_events', 'Manage events'),
    ('manage_users', 'Manage users'),
    ('manage_settings', 'Manage site settings'),
    ('admin_db', 'Use database editor/download DB'),
]

def _default_rank_permissions() -> dict:
    return {
        'member': [],
        'active_member': [],
        'mod': ['access_admin', 'manage_levels', 'moderate_records', 'moderate_submissions'],
        'elder_moderator': ['access_admin', 'manage_levels', 'move_levels', 'moderate_records', 'moderate_submissions', 'manage_pages', 'manage_events'],
        'admin': [p[0] for p in PERMISSIONS],
    }

def _get_rank_permissions() -> dict:
    defaults = _default_rank_permissions()
    raw = (_get_setting('rank_permissions') or '').strip()
    if not raw:
        return defaults
    try:
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            return defaults
    except Exception:
        return defaults
    out = dict(defaults)
    for k, v in loaded.items():
        rk = str(k or '').strip().lower()
        if rk not in RANKS:
            continue
        perms = []
        if isinstance(v, list):
            for item in v:
                s = str(item or '').strip()
                if not s:
                    continue
                perms.append(s)
        out[rk] = perms
    out['admin'] = [p[0] for p in PERMISSIONS]
    return out

def _has_perm(user, perm: str) -> bool:
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    r = _get_user_rank(user)
    if r == 'admin':
        return True
    perms = _get_rank_permissions().get(r) or []
    return perm in set(perms)

def _require_perm(perm: str):
    if not _has_perm(current_user, perm):
        abort(403)

DEFAULT_AUTO_POINTS_RULES = """1=500
2-5=250
6-10=100
11-20=50
"""

def _auto_points_enabled() -> bool:
    raw = str(_get_setting('auto_points_enabled') or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')

def _auto_points_scope() -> str:
    raw = str(_get_setting('auto_points_scope') or '').strip().lower()
    if raw in ('main', 'challenge', 'platformer', 'both', 'all'):
        return raw
    return 'both'

def _auto_points_applies_to(list_type: str) -> bool:
    if not _auto_points_enabled():
        return False
    scope = _auto_points_scope()
    lt = (list_type or '').strip().lower() or 'main'
    if lt not in ('main', 'challenge', 'platformer'):
        lt = 'main'
    if scope == 'all':
        return True
    if scope == 'both':
        return lt in ('main', 'challenge')
    return scope == lt

def _auto_points_rules_key(list_type: str, is_verified: bool) -> str:
    lt = (list_type or '').strip().lower() or 'main'
    if lt not in ('main', 'challenge', 'platformer'):
        lt = 'main'
    return f'auto_points_rules_{lt}_{"verified" if bool(is_verified) else "unverified"}'

def _get_auto_points_rules_text_for(list_type: str, is_verified: bool) -> str:
    key = _auto_points_rules_key(list_type, is_verified)
    raw = (_get_setting(key) or '').strip()
    if raw:
        return raw
    lt = (list_type or '').strip().lower() or 'main'
    if bool(is_verified) and lt == 'main':
        legacy = (_get_setting('auto_points_rules') or '').strip()
        if legacy:
            return legacy
    return DEFAULT_AUTO_POINTS_RULES

def _get_auto_points_rules_text() -> str:
    return _get_auto_points_rules_text_for('main', True)

def _parse_auto_points_rules(text: str):
    rules = []
    for line in (text or '').splitlines():
        s = (line or '').strip()
        if not s:
            continue
        if s.startswith('#'):
            continue
        if '=' not in s:
            continue
        left, right = s.split('=', 1)
        left = (left or '').strip()
        right = (right or '').strip()
        if not left or not right:
            continue
        try:
            pts = float(right)
        except Exception:
            continue
        if '-' in left:
            a, b = left.split('-', 1)
            try:
                start = int(a.strip())
                end = int(b.strip())
            except Exception:
                continue
        else:
            try:
                start = int(left)
                end = start
            except Exception:
                continue
        if start < 1:
            start = 1
        if end < start:
            end = start
        rules.append((start, end, pts))
    rules.sort(key=lambda x: (x[0], x[1]))
    return rules

def _points_for_position(pos: int, rules) -> float:
    if not pos:
        return 0.0
    for start, end, pts in (rules or []):
        if pos >= start and pos <= end:
            return float(pts or 0.0)
    return 0.0

def _auto_points_recalc_for_list(list_type: str, is_verified: bool):
    rules = _parse_auto_points_rules(_get_auto_points_rules_text_for(list_type, is_verified))
    levels = Level.query.filter(
        Level.list_type == list_type,
        Level.is_verified == bool(is_verified),
        Level.position.isnot(None)
    ).order_by(Level.position).all()
    for lv in levels:
        want = _points_for_position(int(lv.position or 0), rules)
        lv.points_reward = want
    db.session.commit()

def _auto_points_recalc_all():
    scope = _auto_points_scope()
    q = Level.query.filter(Level.position.isnot(None))
    if scope in ('main', 'challenge'):
        q = q.filter(Level.list_type == scope)
    levels = q.order_by(Level.list_type, Level.is_verified.desc(), Level.position).all()
    cache = {}
    for lv in levels:
        key = ((lv.list_type or 'main'), bool(lv.is_verified))
        rules = cache.get(key)
        if rules is None:
            rules = _parse_auto_points_rules(_get_auto_points_rules_text_for(key[0], key[1]))
            cache[key] = rules
        want = _points_for_position(int(lv.position or 0), rules)
        lv.points_reward = want
    db.session.commit()
    try:
        _recalc_all_user_points()
    except Exception:
        pass

def _record_proof_top_n() -> int:
    raw = str(_get_setting('record_proof_top_n') or '').strip()
    try:
        n = int(float(raw))
    except Exception:
        n = 30
    if n < 0:
        n = 0
    if n > 10000:
        n = 10000
    return n

def _record_proof_top_n_for_list(list_type: str) -> int:
    lt = (list_type or '').strip().lower() or 'main'
    if lt not in ('main', 'challenge', 'platformer'):
        lt = 'main'
    raw = str(_get_setting(f'record_proof_top_n_{lt}') or '').strip()
    if raw:
        try:
            n = int(float(raw))
        except Exception:
            n = 30
        if n < 0:
            n = 0
        if n > 10000:
            n = 10000
        return n
    if lt == 'platformer':
        return 10000
    return _record_proof_top_n()

def _recalc_all_user_points():
    rows = (
        db.session.query(Record.user_id, db.func.coalesce(db.func.sum(Level.points_reward), 0.0))
        .join(Level, Level.id == Record.level_id)
        .filter(Record.status == 'approved')
        .group_by(Record.user_id)
        .all()
    )
    by_user = {int(uid): float(pts or 0.0) for uid, pts in (rows or []) if uid is not None}
    users = User.query.all()
    for u in users:
        u.points = max(0.0, float(by_user.get(int(u.id), 0.0)))
    db.session.commit()

def _recalc_user_points_for_users(user_ids):
    cleaned = []
    for uid in (user_ids or []):
        try:
            v = int(uid)
        except Exception:
            continue
        if v > 0 and v not in cleaned:
            cleaned.append(v)
    if not cleaned:
        return
    rows = (
        db.session.query(Record.user_id, db.func.coalesce(db.func.sum(Level.points_reward), 0.0))
        .join(Level, Level.id == Record.level_id)
        .filter(Record.status == 'approved', Record.user_id.in_(cleaned))
        .group_by(Record.user_id)
        .all()
    )
    by_user = {int(uid): float(pts or 0.0) for uid, pts in (rows or []) if uid is not None}
    users = User.query.filter(User.id.in_(cleaned)).all()
    for u in users:
        u.points = max(0.0, float(by_user.get(int(u.id), 0.0)))
    db.session.commit()

def _sum_approved_points_for_user(user_id: int) -> float:
    try:
        uid = int(user_id)
    except Exception:
        return 0.0
    pts = (
        db.session.query(db.func.coalesce(db.func.sum(Level.points_reward), 0.0))
        .select_from(Record)
        .join(Level, Level.id == Record.level_id)
        .filter(Record.user_id == uid, Record.status == 'approved')
        .scalar()
    )
    try:
        return float(pts or 0.0)
    except Exception:
        return 0.0

def _sum_approved_points_by_list_for_user(user_id: int):
    try:
        uid = int(user_id)
    except Exception:
        return {'main': 0.0, 'challenge': 0.0}
    try:
        rows = (
            db.session.query(Level.list_type, db.func.coalesce(db.func.sum(Level.points_reward), 0.0))
            .select_from(Record)
            .join(Level, Level.id == Record.level_id)
            .filter(Record.user_id == uid, Record.status == 'approved')
            .group_by(Level.list_type)
            .all()
        )
    except Exception:
        rows = []
    out = {'main': 0.0, 'challenge': 0.0}
    for lt, pts in (rows or []):
        key = (lt or 'main').strip().lower() or 'main'
        if key not in ('main', 'challenge'):
            continue
        try:
            out[key] = float(pts or 0.0)
        except Exception:
            out[key] = 0.0
    return out

def _year_recap_enabled() -> bool:
    raw = str(_get_setting('year_recap_enabled') or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')

def _setting_truthy(key: str) -> bool:
    raw = str(_get_setting(key) or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')

@app.context_processor
def _inject_rank_helpers():
    try:
        disable_thumbs = str(request.cookies.get('disable_thumbnails') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    except Exception:
        disable_thumbs = False
    return {
        'has_perm': _has_perm,
        'user_rank': _get_user_rank,
        'rank_label': _rank_label,
        'rank_permissions': _get_rank_permissions(),
        'permission_defs': PERMISSIONS,
        'rank_list': [(r, _rank_label(r)) for r in RANKS],
        'year_recap_enabled': _year_recap_enabled(),
        'disable_thumbnails': disable_thumbs,
        'footer_discord_url': str(_get_setting('footer_discord_url') or '').strip(),
        'april_fools_enabled': _setting_truthy('april_fools_enabled'),
    }

@app.before_request
def _enforce_account_state_and_guidelines():
    if request.endpoint in ('static',):
        return
    if not getattr(current_user, 'is_authenticated', False):
        return
    try:
        if bool(getattr(current_user, 'is_disabled', False)):
            logout_user()
            flash('Your account has been disabled.', 'danger')
            return redirect(url_for('login'))
    except Exception:
        pass
    if not _needs_guidelines_acceptance(current_user):
        return
    if request.endpoint in ('guidelines', 'logout'):
        return
    nxt = request.full_path if (request.query_string or b'') else request.path
    return redirect(url_for('guidelines', next=nxt))

def _trim_pw(s: str) -> str:
    try:
        b = s.encode('utf-8')
        return b[:72].decode('utf-8', 'ignore')
    except Exception:
        return s

def _get_recaptcha_site_key() -> str:
    return (_get_setting('recaptcha_site_key') or app.config.get('RECAPTCHA_SITE_KEY') or '').strip()

def _get_recaptcha_secret_key() -> str:
    return (_get_setting('recaptcha_secret_key') or app.config.get('RECAPTCHA_SECRET_KEY') or '').strip()

def _get_hcaptcha_site_key() -> str:
    return (_get_setting('hcaptcha_site_key') or app.config.get('HCAPTCHA_SITE_KEY') or '').strip()

def _get_hcaptcha_secret_key() -> str:
    return (_get_setting('hcaptcha_secret_key') or app.config.get('HCAPTCHA_SECRET_KEY') or '').strip()

def _get_captcha_provider() -> str:
    raw = str(_get_setting('captcha_provider') or '').strip().lower()
    if raw in ('recaptcha', 'hcaptcha'):
        return raw
    if _get_recaptcha_site_key() and _get_recaptcha_secret_key():
        return 'recaptcha'
    if _get_hcaptcha_site_key() and _get_hcaptcha_secret_key():
        return 'hcaptcha'
    return ''

def _get_captcha_site_key(provider=None) -> str:
    provider = (provider or _get_captcha_provider() or '').strip().lower()
    if provider == 'hcaptcha':
        return _get_hcaptcha_site_key()
    if provider == 'recaptcha':
        return _get_recaptcha_site_key()
    return ''

def _verify_captcha():
    provider = _get_captcha_provider()
    if provider == 'recaptcha':
        site_key = _get_recaptcha_site_key()
        secret = _get_recaptcha_secret_key()
        if not site_key or not secret:
            return None
        token = (request.form.get('g-recaptcha-response') or '').strip()
        if not token:
            return False
        payload = urllib.parse.urlencode({
            'secret': secret,
            'response': token,
            'remoteip': request.remote_addr or ''
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://www.google.com/recaptcha/api/siteverify',
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode('utf-8', 'ignore')
            data = json.loads(raw or '{}')
            return bool(data.get('success'))
        except Exception:
            return False
    if provider == 'hcaptcha':
        site_key = _get_hcaptcha_site_key()
        secret = _get_hcaptcha_secret_key()
        if not site_key or not secret:
            return None
        token = (request.form.get('h-captcha-response') or request.form.get('g-recaptcha-response') or '').strip()
        if not token:
            return False
        payload = urllib.parse.urlencode({
            'secret': secret,
            'response': token,
            'remoteip': request.remote_addr or ''
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://hcaptcha.com/siteverify',
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode('utf-8', 'ignore')
            data = json.loads(raw or '{}')
            return bool(data.get('success'))
        except Exception:
            return False
    return None

def _verify_recaptcha():
    return _verify_captcha()

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None

@app.route('/')
def home():
    page = request.args.get('page', 1, type=int)
    if not page or page < 1:
        page = 1
    per_page = 40
    query = request.args.get('q')
    selected_cats = request.args.getlist('cat')
    banned = {'sonic wave brother', 'nugget', 'hey lois do you remember the time i was in the bottom list'}
    all_categories = [
        '2.1','2.2','Medium','Long','XL','XXL+','Ship','Cube','Wave','Robot','UFO','Spider','Swing Copter','Dual','Timings','Memory','Nerve Control','Slow Paced','Fast Paced','High CPS','NONG','Chokepoints','buffer click abuse','buffer click',
        'Ball','Spam','Unbalanced','Precision','Flow','Learny','Sync','Technical','Consistency','Endurance','Straight Fly','Click Pattern','Low Refresh Friendly'
    ]
    is_search = bool(query or selected_cats)
    hide_categories_on_main_list = str(_get_setting('hide_categories_on_main_list') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    def has_all_categories(level, cats):
        src = (level.categories or '').lower()
        for c in cats:
            if c.lower() not in src:
                return False
        for b in banned:
            if b in src:
                return False
        return True
    if query:
        q = (query or '').strip()
        q_lower = q.lower()
        base = Level.query.filter(Level.name.ilike(f'%{q}%') | Level.creator.ilike(f'%{q}%')).all()
        if selected_cats:
            base = [lv for lv in base if has_all_categories(lv, selected_cats)]
        from sqlalchemy import case
        users = (
            User.query
            .filter(
                User.username.ilike(f'%{q}%')
                | db.func.coalesce(User.discord_username, '').ilike(f'%{q}%')
                | db.func.coalesce(User.discord_tag, '').ilike(f'%{q}%')
            )
            .order_by(
                case(
                    (db.func.lower(User.username) == q_lower, 0),
                    (db.func.lower(db.func.coalesce(User.discord_username, '')) == q_lower, 0),
                    (db.func.lower(db.func.coalesce(User.discord_tag, '')) == q_lower, 0),
                    (db.func.lower(User.username).like(q_lower + '%'), 1),
                    else_=2,
                ),
                db.func.lower(User.username),
            )
            .limit(25)
            .all()
        )
        clans = Clan.query.filter(Clan.name.ilike(f'%{q}%'), ~db.func.lower(Clan.name).like('%raid%')).limit(25).all()
        map_packs = MapPack.query.filter(MapPack.name.ilike(f'%{q}%')).limit(25).all()
        news_posts = NewsPost.query.order_by(NewsPost.created_at.desc()).limit(5).all()
        return render_template('index.html', levels=base, users=users, clans=clans, map_packs=map_packs, search_query=q, is_search=is_search, all_categories=all_categories, selected_cats=selected_cats, news_posts=news_posts, hide_categories_on_main_list=hide_categories_on_main_list, pagination=None)
    levels = _cache_get('levels:main:verified')
    if levels is None:
        levels = Level.query.filter_by(is_verified=True, list_type='main').order_by(Level.position).all()
        _cache_set('levels:main:verified', levels, 20)
    if selected_cats:
        levels = [lv for lv in levels if has_all_categories(lv, selected_cats)]
    news_posts = _cache_get('news:latest5')
    if news_posts is None:
        news_posts = NewsPost.query.order_by(NewsPost.created_at.desc()).limit(5).all()
        _cache_set('news:latest5', news_posts, 20)
    total = len(levels)
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    start = (page - 1) * per_page
    end = start + per_page
    levels_page = levels[start:end]
    pagination = {
        'page': page,
        'pages': pages,
        'has_prev': page > 1,
        'has_next': page < pages,
        'prev_num': page - 1,
        'next_num': page + 1,
    }
    return render_template('index.html', levels=levels_page, users=[], clans=[], map_packs=[], is_search=is_search, all_categories=all_categories, selected_cats=selected_cats, news_posts=news_posts, hide_categories_on_main_list=hide_categories_on_main_list, pagination=pagination)

@app.route('/challenges')
def challenge_list():
    page = request.args.get('page', 1, type=int)
    if not page or page < 1:
        page = 1
    pagination = Level.query.filter_by(is_verified=True, list_type='challenge').order_by(Level.position).paginate(page=page, per_page=40, error_out=False)
    return render_template('challenge_list.html', levels=pagination.items, pagination=pagination)

@app.route('/platformer')
def platformer_list():
    page = request.args.get('page', 1, type=int)
    if not page or page < 1:
        page = 1
    pagination_obj = Level.query.filter_by(is_verified=True, list_type='platformer').order_by(Level.position).paginate(page=page, per_page=40, error_out=False)
    return render_template('index.html', levels=pagination_obj.items, is_search=False, all_categories=[], selected_cats=[], news_posts=[], hide_categories_on_main_list=True, show_home_header=False, list_title='Platformer List', empty_message='No platformer levels verified yet.', list_accent='purple', pagination=pagination_obj)

@app.route('/unverified')
def unverified_list():
    # Show unverified levels (or levels waiting to be placed)
    # The user asked for "unverified and verified list". 
    # Usually "unverified" in GD lists means levels that are on the list but records aren't accepted yet, OR levels submitted but not yet placed.
    # I'll treat it as levels that are NOT verified (is_verified=False)
    page = request.args.get('page', 1, type=int)
    if not page or page < 1:
        page = 1
    pagination = Level.query.filter_by(is_verified=False).order_by(Level.list_type.asc(), Level.position.asc().nullslast(), Level.id.asc()).paginate(page=page, per_page=40, error_out=False)
    levels = list(pagination.items or [])
    wr_by_level = {}
    try:
        level_ids = [int(lv.id) for lv in levels if getattr(lv, 'id', None)]
        if level_ids:
            from sqlalchemy.orm import joinedload
            recs = (
                Record.query.options(joinedload(Record.user))
                .filter(Record.status == 'approved', Record.level_id.in_(level_ids))
                .order_by(Record.level_id.asc(), Record.submitted_at.asc().nullslast(), Record.id.asc())
                .all()
            )
            seen = set()
            for rec in recs:
                lid = int(rec.level_id or 0)
                if not lid or lid in seen:
                    continue
                seen.add(lid)
                wr_by_level[lid] = getattr(getattr(rec, 'user', None), 'username', '') or 'Unknown'
    except Exception:
        wr_by_level = {}
    return render_template('unverified.html', levels=levels, pagination=pagination, wr_by_level=wr_by_level)

@app.route('/guidelines-site')
def guidelines_site():
    return render_template('guidelines_site.html')

@app.route('/unbeaten')
@login_required
def unbeaten_levels():
    list_type = (request.args.get('list_type') or 'all').strip().lower()
    if list_type not in ('all', 'main', 'challenge', 'platformer'):
        list_type = 'all'

    beaten_ids = set()
    try:
        beaten_ids = set(
            r[0]
            for r in (
                db.session.query(Record.level_id)
                .filter(Record.user_id == current_user.id, Record.status == 'approved')
                .distinct()
                .all()
            )
            if r and r[0]
        )
    except Exception:
        beaten_ids = set()

    from sqlalchemy import case, or_
    skip_now = str(request.args.get('skip') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    skipped_ids = set()
    try:
        skipped_ids = set([int(x) for x in (session.get('rec_skip_ids') or []) if str(x).strip().isdigit()])
    except Exception:
        skipped_ids = set()
    unbeaten_q = Level.query.filter(Level.is_verified.is_(True))
    if list_type != 'all':
        unbeaten_q = unbeaten_q.filter(Level.list_type == list_type)
    if beaten_ids:
        unbeaten_q = unbeaten_q.filter(~Level.id.in_(list(beaten_ids)))
    unbeaten_levels_list = unbeaten_q.order_by(
        case(
            (Level.list_type == 'main', 0),
            (Level.list_type == 'challenge', 1),
            (Level.list_type == 'platformer', 2),
            else_=3,
        ),
        Level.position.is_(None),
        Level.position,
        db.func.lower(Level.name),
    ).all()

    beaten_levels = []
    if beaten_ids:
        beaten_levels = Level.query.filter(Level.id.in_(list(beaten_ids))).all()

    beaten_tags = set()
    for lv in beaten_levels:
        raw = str(getattr(lv, 'categories', '') or '')
        for p in raw.split(','):
            t = p.strip().lower()
            if t:
                beaten_tags.add(t)

    rec_level = None
    rec_ratings = {'deco': None, 'gameplay': None, 'enjoyment': None}
    if beaten_tags:
        candidates = Level.query.filter(Level.is_verified.is_(True)).all()
        scored = []
        for lv in candidates:
            if lv.id in beaten_ids:
                continue
            if lv.id in skipped_ids:
                continue
            tags = set([t.strip().lower() for t in str(getattr(lv, 'categories', '') or '').split(',') if t.strip()])
            overlap = len(tags & beaten_tags)
            if overlap <= 0:
                continue
            pos = int(getattr(lv, 'position', 999999) or 999999)
            scored.append((overlap, -pos, lv.id, lv))
        if scored:
            scored.sort(reverse=True)
            rec_level = scored[0][3]
    if rec_level is None:
        rec_q = Level.query.filter(Level.is_verified.is_(True), or_(~Level.id.in_(list(beaten_ids)) if beaten_ids else True))
        rec_q = rec_q.filter(~Level.id.in_(list(skipped_ids)) if skipped_ids else True)
        if list_type != 'all':
            rec_q = rec_q.filter(Level.list_type == list_type)
        rec_level = rec_q.order_by(Level.position.asc().nullslast(), Level.id.asc()).first()

    if skip_now and rec_level is not None:
        try:
            skipped_ids.add(int(rec_level.id))
            session['rec_skip_ids'] = sorted(list(skipped_ids))[-500:]
        except Exception:
            pass
        return redirect(url_for('unbeaten_levels'))

    if rec_level:
        rec_rows = Record.query.filter(Record.level_id == rec_level.id, Record.status == 'approved').all()
        def _avg(vals):
            nums = [float(v) for v in vals if v is not None]
            return round(sum(nums) / len(nums), 1) if nums else None
        rec_ratings = {
            'deco': _avg([getattr(r, 'rating_deco', None) for r in rec_rows]),
            'gameplay': _avg([getattr(r, 'rating_gameplay', None) for r in rec_rows]),
            'enjoyment': _avg([getattr(r, 'rating_difficulty', None) for r in rec_rows]),
        }

    return render_template('unbeaten_recommended.html', recommended=rec_level, rec_ratings=rec_ratings, unbeaten_levels=unbeaten_levels_list, list_type=list_type)


@app.route('/recommended')
@login_required
def recommended_levels():
    return redirect(url_for('unbeaten_levels'))

@app.route('/level/<int:level_id>')
def level_detail(level_id):
    level = Level.query.get_or_404(level_id)
    records = Record.query.filter_by(level_id=level_id, status='approved').all()
    def _avg(nums):
        vals = [float(x) for x in (nums or []) if x is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)
    avg_deco = _avg([getattr(r, 'rating_deco', None) for r in records])
    avg_gameplay = _avg([getattr(r, 'rating_gameplay', None) for r in records])
    avg_difficulty = _avg([getattr(r, 'rating_difficulty', None) for r in records])
    return render_template('level.html', level=level, records=records, avg_deco=avg_deco, avg_gameplay=avg_gameplay, avg_difficulty=avg_difficulty)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    if request.method == 'POST':
        if request.form.get('website'):
            flash('Bot detected.', 'danger')
            return redirect(url_for('register'))
        if not (request.form.get('agree_guidelines') or '').strip():
            flash('You must agree to the guidelines to create an account.', 'danger')
            return redirect(url_for('register'))
        provider = _get_captcha_provider()
        captcha_ok = _verify_captcha()
        if captcha_ok is False:
            flash('Invalid hCaptcha.' if provider == 'hcaptcha' else 'Invalid reCAPTCHA.', 'danger')
            return redirect(url_for('register'))
        if captcha_ok is None:
            try:
                user_answer = int(request.form.get('captcha_answer', ''))
            except ValueError:
                user_answer = None
            if user_answer != session.get('captcha_answer'):
                flash('Invalid CAPTCHA answer.', 'danger')
                return redirect(url_for('register'))
        username = (request.form.get('username') or '').strip()
        password = _trim_pw(request.form.get('password', ''))
        pw_len = len(password or '')
        has_alpha = any(c.isalpha() for c in password or '')
        has_digit = any(c.isdigit() for c in password or '')
        if not username:
            flash('Username is required', 'danger')
            return redirect(url_for('register'))
        if len(username) > 32:
            flash('Username must be at most 32 characters long', 'danger')
            return redirect(url_for('register'))
        if pw_len < 8 or not (has_alpha and has_digit):
            flash('Password must be at least 8 characters and include letters and numbers.', 'danger')
            return redirect(url_for('register'))
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, password=hashed_pw, guidelines_accepted_at=datetime.utcnow(), register_ip=None)
        pending = session.get('pending_discord') or {}
        try:
            pending_id = str(pending.get('discord_id') or '').strip()
        except Exception:
            pending_id = ''
        if pending_id:
            exists = User.query.filter(User.discord_id == pending_id).first()
            if exists:
                flash('That Discord account is already linked to another user.', 'warning')
            else:
                user.discord_id = pending_id
                user.discord_username = (pending.get('discord_username') or '')[:100]
                user.discord_avatar = (pending.get('discord_avatar') or '')[:100]
                if not user.discord_tag:
                    user.discord_tag = (pending.get('discord_tag') or '')[:100]
        db.session.add(user)
        db.session.commit()
        add_site_log('account_create', f'Account created: {user.username}', actor_user_id=user.id)
        if pending_id and not User.query.filter(User.discord_id == pending_id, User.id != user.id).first():
            try:
                session.pop('pending_discord', None)
            except Exception:
                pass
            add_site_log('account_link', f'Discord linked during registration for {user.username}.', actor_user_id=user.id)
        flash('Account created! You can now log in.', 'success')
        return redirect(url_for('login'))
    provider = _get_captcha_provider()
    site_key = _get_captcha_site_key(provider)
    if provider and site_key:
        return render_template('register.html', captcha_provider=provider, captcha_site_key=site_key, guidelines_url=_get_guidelines_url())
    import random
    a, b = random.randint(1, 9), random.randint(1, 9)
    session['captcha_answer'] = a + b
    return render_template('register.html', captcha_a=a, captcha_b=b, captcha_provider='', captcha_site_key='', guidelines_url=_get_guidelines_url())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    if request.method == 'POST':
        if request.form.get('website'):
            flash('Bot detected.', 'danger')
            return redirect(url_for('login'))
        provider = _get_captcha_provider()
        captcha_ok = _verify_captcha()
        if captcha_ok is False:
            flash('Invalid hCaptcha.' if provider == 'hcaptcha' else 'Invalid reCAPTCHA.', 'danger')
            return redirect(url_for('login'))
        if captcha_ok is None:
            try:
                user_answer = int(request.form.get('captcha_answer', ''))
            except ValueError:
                user_answer = None
            if user_answer != session.get('captcha_answer'):
                flash('Invalid CAPTCHA answer.', 'danger')
                return redirect(url_for('login'))
        username = request.form.get('username')
        password = _trim_pw(request.form.get('password', ''))
        user = User.query.filter_by(username=username).first()
        if user and bool(getattr(user, 'is_disabled', False)):
            add_site_log('login', f'Login failed (disabled): {username}', actor_user_id=getattr(user, 'id', None))
            flash('Your account has been disabled.', 'danger')
            return redirect(url_for('login'))
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            add_site_log('login', f'Login success: {user.username}', actor_user_id=user.id)
            if _needs_guidelines_acceptance(user):
                return redirect(url_for('guidelines'))
            return redirect(url_for('home'))
        else:
            add_site_log('login', f'Login failed: {username}', actor_user_id=(user.id if user else None))
            flash('Login Unsuccessful. Please check username and password', 'danger')
    provider = _get_captcha_provider()
    site_key = _get_captcha_site_key(provider)
    if provider and site_key:
        return render_template('login.html', captcha_provider=provider, captcha_site_key=site_key, discord_oauth_ready=_discord_oauth_ready())
    import random
    a, b = random.randint(1, 9), random.randint(1, 9)
    session['captcha_answer'] = a + b
    return render_template('login.html', captcha_a=a, captcha_b=b, captcha_provider='', captcha_site_key='', discord_oauth_ready=_discord_oauth_ready())

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/guidelines', methods=['GET', 'POST'])
@login_required
def guidelines():
    if request.method == 'POST':
        if not (request.form.get('agree') or '').strip():
            flash('You must agree to continue.', 'danger')
            return redirect(url_for('guidelines', next=request.args.get('next') or ''))
        current_user.guidelines_accepted_at = datetime.utcnow()
        db.session.commit()
        add_site_log('guidelines_accept', f'{current_user.username} accepted guidelines.', actor_user_id=current_user.id)
        nxt = (request.args.get('next') or '').strip()
        if not nxt.startswith('/'):
            nxt = url_for('home')
        return redirect(nxt)
    nxt = (request.args.get('next') or '').strip()
    if nxt and not nxt.startswith('/'):
        nxt = ''
    return render_template('guidelines_accept.html', guidelines_url=_get_guidelines_url(), next=nxt)

def _get_discord_client_id() -> str:
    return (_get_setting('discord_client_id') or app.config.get('DISCORD_CLIENT_ID') or '').strip()

def _get_discord_client_secret() -> str:
    return (_get_setting('discord_client_secret') or app.config.get('DISCORD_CLIENT_SECRET') or '').strip()

def _get_discord_redirect_uri() -> str:
    return (_get_setting('discord_redirect_uri') or app.config.get('DISCORD_REDIRECT_URI') or '').strip()

def _discord_oauth_ready():
    return bool(_get_discord_client_id() and _get_discord_client_secret() and _get_discord_redirect_uri())

@app.route('/discord/login')
def discord_login():
    if getattr(current_user, 'is_authenticated', False):
        return redirect(url_for('home'))
    if not _discord_oauth_ready():
        flash('Discord login is not configured.', 'danger')
        return redirect(url_for('login'))
    state = uuid.uuid4().hex
    session['discord_oauth_state'] = state
    session['discord_oauth_mode'] = 'login'
    session['discord_oauth_next'] = (request.args.get('next') or url_for('home'))
    params = {
        'client_id': _get_discord_client_id(),
        'redirect_uri': _get_discord_redirect_uri(),
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
        'prompt': 'consent'
    }
    url = 'https://discord.com/api/oauth2/authorize?' + urllib.parse.urlencode(params)
    return redirect(url)

@app.route('/discord/link')
def discord_link():
    mode = (request.args.get('mode') or '').strip().lower() or 'profile'
    if not getattr(current_user, 'is_authenticated', False) and mode != 'register':
        return redirect(url_for('login'))
    if not _discord_oauth_ready():
        flash('Discord linking is not configured.', 'danger')
        return redirect(request.referrer or url_for('home'))
    state = uuid.uuid4().hex
    session['discord_oauth_state'] = state
    session['discord_oauth_mode'] = mode
    session['discord_oauth_next'] = (request.args.get('next') or request.referrer or url_for('home'))
    params = {
        'client_id': _get_discord_client_id(),
        'redirect_uri': _get_discord_redirect_uri(),
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
        'prompt': 'consent'
    }
    url = 'https://discord.com/api/oauth2/authorize?' + urllib.parse.urlencode(params)
    return redirect(url)

@app.route('/discord/callback')
def discord_callback():
    err = (request.args.get('error') or '').strip()
    if err:
        flash('Discord linking cancelled.', 'warning')
        return redirect(session.get('discord_oauth_next') or url_for('home'))
    code = (request.args.get('code') or '').strip()
    state = (request.args.get('state') or '').strip()
    want_state = (session.get('discord_oauth_state') or '').strip()
    if not code or not state or state != want_state:
        try:
            app.logger.warning('Discord linking failed: state mismatch or missing code (has_code=%s, has_state=%s, has_want_state=%s)', bool(code), bool(state), bool(want_state))
        except Exception:
            pass
        try:
            add_site_log('account_link_fail', 'Discord linking failed: state mismatch or missing code.', actor_user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None))
        except Exception:
            pass
        flash('Discord linking failed.', 'danger')
        return redirect(url_for('home'))
    session.pop('discord_oauth_state', None)
    mode = (session.get('discord_oauth_mode') or '').strip().lower() or 'profile'
    nxt = session.get('discord_oauth_next') or url_for('home')
    session.pop('discord_oauth_mode', None)
    session.pop('discord_oauth_next', None)
    try:
        use_requests = False
        try:
            import requests  # type: ignore
            use_requests = True
        except Exception:
            use_requests = False

        base_headers = {
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (compatible; site/1.0; +https://discord.com)',
        }

        if use_requests:
            token_data = {
                'client_id': _get_discord_client_id(),
                'client_secret': _get_discord_client_secret(),
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': _get_discord_redirect_uri(),
                'scope': 'identify',
            }
            tok_resp = requests.post(
                'https://discord.com/api/oauth2/token',
                data=token_data,
                headers={**base_headers, 'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=8,
            )
            if tok_resp.status_code >= 400:
                try:
                    app.logger.warning('Discord linking failed: token HTTP %s %s', tok_resp.status_code, (tok_resp.text or '')[:500])
                except Exception:
                    pass
                flash('Discord linking failed.', 'danger')
                return redirect(nxt)
            token_json = tok_resp.json() if tok_resp.text else {}
            access = token_json.get('access_token')
            if not access:
                try:
                    app.logger.warning('Discord linking failed: no access_token in response')
                except Exception:
                    pass
                try:
                    add_site_log('account_link_fail', 'Discord linking failed: no access token.', actor_user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None))
                except Exception:
                    pass
                flash('Discord linking failed.', 'danger')
                return redirect(nxt)
            me_resp = requests.get(
                'https://discord.com/api/users/@me',
                headers={**base_headers, 'Authorization': f'Bearer {access}'},
                timeout=8,
            )
            if me_resp.status_code >= 400:
                try:
                    app.logger.warning('Discord linking failed: me HTTP %s %s', me_resp.status_code, (me_resp.text or '')[:500])
                except Exception:
                    pass
                flash('Discord linking failed.', 'danger')
                return redirect(nxt)
            me = me_resp.json() if me_resp.text else {}
        else:
            token_payload = urllib.parse.urlencode({
                'client_id': _get_discord_client_id(),
                'client_secret': _get_discord_client_secret(),
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': _get_discord_redirect_uri(),
                'scope': 'identify'
            }).encode('utf-8')
            token_req = urllib.request.Request(
                'https://discord.com/api/oauth2/token',
                data=token_payload,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    **base_headers,
                }
            )
            with urllib.request.urlopen(token_req, timeout=8) as resp:
                token_raw = resp.read().decode('utf-8', 'ignore')
            token_json = json.loads(token_raw or '{}')
            access = token_json.get('access_token')
            if not access:
                try:
                    app.logger.warning('Discord linking failed: no access_token in response')
                except Exception:
                    pass
                try:
                    add_site_log('account_link_fail', 'Discord linking failed: no access token.', actor_user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None))
                except Exception:
                    pass
                flash('Discord linking failed.', 'danger')
                return redirect(nxt)
            me_req = urllib.request.Request(
                'https://discord.com/api/users/@me',
                headers={
                    'Authorization': f'Bearer {access}',
                    **base_headers,
                }
            )
            with urllib.request.urlopen(me_req, timeout=8) as resp:
                me_raw = resp.read().decode('utf-8', 'ignore')
            me = json.loads(me_raw or '{}')
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'ignore')
        except Exception:
            body = ''
        try:
            app.logger.warning('Discord linking failed: HTTPError %s %s', getattr(e, 'code', ''), (body[:500] if body else ''))
        except Exception:
            pass
        try:
            add_site_log('account_link_fail', f'Discord linking failed: HTTP error {getattr(e, "code", "")}.', actor_user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None))
        except Exception:
            pass
        flash('Discord linking failed.', 'danger')
        return redirect(nxt)
    except Exception:
        try:
            app.logger.exception('Discord linking failed: exception during OAuth callback')
        except Exception:
            pass
        try:
            add_site_log('account_link_fail', 'Discord linking failed: exception during OAuth callback.', actor_user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None))
        except Exception:
            pass
        flash('Discord linking failed.', 'danger')
        return redirect(nxt)

    discord_id = str(me.get('id') or '').strip()
    username = str(me.get('username') or '').strip()
    discrim = str(me.get('discriminator') or '').strip()
    avatar = str(me.get('avatar') or '').strip()
    if not discord_id:
        try:
            app.logger.warning('Discord linking failed: missing discord_id in /users/@me response')
        except Exception:
            pass
        try:
            add_site_log('account_link_fail', 'Discord linking failed: missing discord_id.', actor_user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None))
        except Exception:
            pass
        flash('Discord linking failed.', 'danger')
        return redirect(nxt)
    tag = username
    if discrim and discrim != '0':
        tag = f'{username}#{discrim}'

    if mode == 'login':
        u = User.query.filter(User.discord_id == discord_id).first()
        if u:
            if bool(getattr(u, 'is_disabled', False)):
                flash('Your account has been disabled.', 'danger')
                return redirect(url_for('login'))
            login_user(u)
            add_site_log('login', f'{u.username} logged in with Discord.', actor_user_id=u.id)
            flash('Logged in with Discord.', 'success')
            if _needs_guidelines_acceptance(u):
                return redirect(url_for('guidelines', next=nxt or url_for('home')))
            return redirect(nxt or url_for('home'))
        session['pending_discord'] = {
            'discord_id': discord_id,
            'discord_username': username[:100],
            'discord_avatar': avatar[:100],
            'discord_tag': tag[:100]
        }
        flash('No account is linked to that Discord. Register to link it.', 'warning')
        return redirect(url_for('register'))

    if getattr(current_user, 'is_authenticated', False):
        other = User.query.filter(User.discord_id == discord_id, User.id != current_user.id).first()
        if other:
            flash('That Discord account is already linked to another user.', 'warning')
            return redirect(url_for('edit_profile'))
        current_user.discord_id = discord_id
        current_user.discord_username = username[:100]
        current_user.discord_avatar = avatar[:100]
        if not current_user.discord_tag:
            current_user.discord_tag = tag[:100]
        db.session.commit()
        add_site_log('account_link', f'{current_user.username} linked Discord {tag}.', actor_user_id=current_user.id)
        flash('Discord linked.', 'success')
        return redirect(url_for('edit_profile'))

    session['pending_discord'] = {
        'discord_id': discord_id,
        'discord_username': username[:100],
        'discord_avatar': avatar[:100],
        'discord_tag': tag[:100]
    }
    flash('Discord linked for registration. Complete signup to save it.', 'success')
    if mode == 'register':
        return redirect(url_for('register'))
    return redirect(nxt)

@app.route('/docs/discord-oauth')
def discord_oauth_tutorial():
    if not current_user.is_authenticated:
        abort(404)
    if not _has_perm(current_user, 'access_admin'):
        abort(404)
    return render_template('discord_oauth_tutorial.html')

@app.route('/discord/unlink', methods=['POST'])
@login_required
def discord_unlink():
    if current_user.discord_id:
        old = current_user.discord_id
        current_user.discord_id = None
        current_user.discord_username = None
        current_user.discord_avatar = None
        db.session.commit()
        add_site_log('account_unlink', f'{current_user.username} unlinked Discord {old}.', actor_user_id=current_user.id)
    flash('Discord unlinked.', 'success')
    return redirect(url_for('edit_profile'))

@app.route('/submit/record', methods=['GET', 'POST'])
@login_required
def submit_record():
    levels = Level.query.order_by(Level.list_type.asc(), db.func.coalesce(Level.position, 0).asc()).all()
    if request.method == 'POST':
        if request.form.get('website'):
            flash('Bot detected.', 'danger')
            return redirect(url_for('submit_record'))
        provider = _get_captcha_provider()
        captcha_ok = _verify_captcha()
        if captcha_ok is False:
            flash('Invalid hCaptcha.' if provider == 'hcaptcha' else 'Invalid reCAPTCHA.', 'danger')
            return redirect(url_for('submit_record'))
        if captcha_ok is None:
            try:
                user_answer = int(request.form.get('captcha_answer', ''))
            except ValueError:
                user_answer = None
            if user_answer != session.get('captcha_answer'):
                flash('Invalid CAPTCHA answer.', 'danger')
                return redirect(url_for('submit_record'))
        try:
            level_id = int(request.form.get('level_id') or 0)
        except Exception:
            level_id = 0
        selected_lt = (request.form.get('list_type') or 'main').strip().lower()
        if selected_lt == 'challange':
            selected_lt = 'challenge'
        if selected_lt not in ('main', 'challenge', 'platformer'):
            flash('Please choose a list.', 'danger')
            return redirect(url_for('submit_record'))
        video_url = (request.form.get('video_url') or '').strip()
        device = request.form.get('device')
        comment = request.form.get('comment')
        beaten_on_vc = request.form.get('beaten_on_vc') == '1'
        if not level_id:
            flash('Please select a level.', 'danger')
            return redirect(url_for('submit_record'))
        try:
            lv = db.session.get(Level, level_id)
            level_ok = bool(lv)
        except Exception:
            level_ok = False
        if not level_ok:
            flash('Invalid level selection.', 'danger')
            return redirect(url_for('submit_record'))

        lt = (getattr(lv, 'list_type', '') or 'main').strip().lower()
        if lt not in ('main', 'challenge', 'platformer'):
            lt = 'main'
        if lt != selected_lt:
            flash('That level is not on the selected list.', 'danger')
            return redirect(url_for('submit_record'))
        pos = 0
        try:
            pos = int(getattr(lv, 'position', 0) or 0)
        except Exception:
            pos = 0
        top_n = _record_proof_top_n_for_list(lt)
        require_video = bool(lt == 'platformer' or (top_n > 0 and pos > 0 and pos <= top_n))
        require_image = bool(lt in ('main', 'challenge') and top_n > 0 and pos > top_n)
        if require_video and not video_url:
            if lt == 'platformer':
                flash('Video proof is required for the platformer list.', 'danger')
            else:
                flash(f'Video proof is required for Top {top_n} levels.', 'danger')
            return redirect(url_for('submit_record'))
        def _parse_rating(key: str):
            raw = (request.form.get(key) or '').strip()
            if not raw:
                return None
            try:
                v = int(float(raw))
            except Exception:
                return None
            if v < 1 or v > 10:
                return None
            return int(v)
        rating_deco = _parse_rating('rating_deco')
        rating_gameplay = _parse_rating('rating_gameplay')
        rating_difficulty = _parse_rating('rating_difficulty')
        if rating_deco is None or rating_gameplay is None or rating_difficulty is None:
            flash('Deco, Gameplay, and Enjoyment ratings are required.', 'danger')
            return redirect(url_for('submit_record'))
        vc_proof_filename = None
        proof_image_filename = None
        if require_image:
            f = request.files.get('proof_image')
            if not f or not f.filename:
                flash(f'Screenshot proof image is required for levels below Top {top_n}.', 'danger')
                return redirect(url_for('submit_record'))
            unique, err = _save_uploaded_file(f, app.config['RECORD_PROOF_UPLOAD_FOLDER'], _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('Screenshot proof must be a valid image file.', 'danger')
                return redirect(url_for('submit_record'))
            proof_image_filename = unique
        if beaten_on_vc:
            f = request.files.get('vc_proof')
            if not f or not f.filename:
                flash('VC proof image is required when beaten on VC.', 'danger')
                return redirect(url_for('submit_record'))
            unique, err = _save_uploaded_file(f, app.config['VC_PROOF_UPLOAD_FOLDER'], _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('VC proof must be a valid image file.', 'danger')
                return redirect(url_for('submit_record'))
            vc_proof_filename = unique
        
        # Check if already submitted
        existing = Record.query.filter_by(user_id=current_user.id, level_id=level_id).first()
        if existing:
            flash('You have already submitted a record for this level.', 'warning')
            return redirect(url_for('submit_record'))
            
        record = Record(
            user_id=current_user.id,
            level_id=level_id,
            video_url=video_url,
            device=device,
            comment=comment,
            beaten_on_vc=beaten_on_vc,
            vc_proof_filename=vc_proof_filename,
            proof_image_filename=proof_image_filename,
            rating_deco=rating_deco,
            rating_gameplay=rating_gameplay,
            rating_difficulty=rating_difficulty
        )
        record.status = 'pending'
        db.session.add(record)
        db.session.commit()
        try:
            add_site_log('record_submit', f'{current_user.username} submitted a record for "{lv.name}" by {lv.creator} ({lt}).', actor_user_id=current_user.id)
        except Exception:
            pass
        flash('Record submitted successfully! Waiting for approval.', 'success')
        return redirect(url_for('profile'))
    provider = _get_captcha_provider()
    site_key = _get_captcha_site_key(provider)
    proof_by_list = {
        'main': _record_proof_top_n_for_list('main'),
        'challenge': _record_proof_top_n_for_list('challenge'),
        'platformer': _record_proof_top_n_for_list('platformer'),
    }
    if provider and site_key:
        return render_template('submit_record.html', levels=levels, captcha_provider=provider, captcha_site_key=site_key, record_proof_top_n_by_list=proof_by_list)
    import random
    a, b = random.randint(1, 9), random.randint(1, 9)
    session['captcha_answer'] = a + b
    return render_template('submit_record.html', levels=levels, captcha_a=a, captcha_b=b, captcha_provider='', captcha_site_key='', record_proof_top_n_by_list=proof_by_list)

@app.route('/submit/level', methods=['GET', 'POST'])
@login_required
def submit_level():
    if request.method == 'POST':
        if request.form.get('website'):
            flash('Bot detected.', 'danger')
            return redirect(url_for('submit_level'))
        provider = _get_captcha_provider()
        captcha_ok = _verify_captcha()
        if captcha_ok is False:
            flash('Invalid hCaptcha.' if provider == 'hcaptcha' else 'Invalid reCAPTCHA.', 'danger')
            return redirect(url_for('submit_level'))
        if captcha_ok is None:
            try:
                user_answer = int(request.form.get('captcha_answer', ''))
            except ValueError:
                user_answer = None
            if user_answer != session.get('captcha_answer'):
                flash('Invalid CAPTCHA answer.', 'danger')
                return redirect(url_for('submit_level'))
        name = (request.form.get('name') or '').strip()
        creator = (getattr(current_user, 'username', '') or '').strip()
        video_url = (request.form.get('video_url') or '').strip()
        gd_level_id = (request.form.get('gd_level_id') or '').strip()
        collaborators = (request.form.get('collaborators') or '').strip()
        is_verified = (request.form.get('is_verified') == 'on')
        verified_video_url = (request.form.get('verified_video_url') or '').strip()
        if is_verified and not verified_video_url:
            flash('Verification link is required when marked verified.', 'danger')
            return redirect(url_for('submit_level'))
        raw_pos = (request.form.get('proposed_position') or '').strip()
        proposed_position = None
        if raw_pos:
            try:
                proposed_position = int(raw_pos)
                if proposed_position < 1:
                    proposed_position = None
            except Exception:
                proposed_position = None
        lt = (request.form.get('list_type') or 'main').strip().lower()
        if lt == 'challange':
            lt = 'challenge'
        if lt not in ('main', 'challenge', 'platformer'):
            lt = 'main'
        thumb_file = request.files.get('thumbnail')
        thumb_unique = None
        if thumb_file and thumb_file.filename:
            folder = os.path.join(app.root_path, 'static', 'level_thumbs')
            thumb_unique, err = _save_uploaded_file(thumb_file, folder, _ALLOWED_IMAGE_EXTS, 'image/')
            if not thumb_unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('Thumbnail must be a valid image (png/jpg/webp).', 'danger')
                return redirect(url_for('submit_level'))
        
        submission = LevelSubmission(
            user_id=current_user.id,
            name=name,
            creator=creator,
            video_url=video_url,
            list_type=lt,
            gd_level_id=gd_level_id or None,
            proposed_position=proposed_position,
            is_verified=bool(is_verified),
            verified_video_url=verified_video_url or None,
            collaborators=collaborators or None,
            thumbnail_filename=thumb_unique or None,
        )
        db.session.add(submission)
        db.session.commit()
        try:
            add_site_log('level_submit', f'{current_user.username} submitted level "{submission.name}" ({lt}).', actor_user_id=current_user.id)
        except Exception:
            pass
        flash('Level submitted successfully!', 'success')
        return redirect(url_for('profile'))
    provider = _get_captcha_provider()
    site_key = _get_captcha_site_key(provider)
    if provider and site_key:
        return render_template('submit_level.html', captcha_provider=provider, captcha_site_key=site_key)
    import random
    a, b = random.randint(1, 9), random.randint(1, 9)
    session['captcha_answer'] = a + b
    return render_template('submit_level.html', captcha_a=a, captcha_b=b, captcha_provider='', captcha_site_key='')

@app.route('/profile')
@login_required
def profile():
    user = current_user
    records = Record.query.filter_by(user_id=user.id).all()
    submissions = LevelSubmission.query.filter_by(user_id=user.id).all()
    display_points = _sum_approved_points_for_user(user.id)
    by_list = _sum_approved_points_by_list_for_user(user.id)
    main_points = float(by_list.get('main') or 0.0)
    challenge_points = float(by_list.get('challenge') or 0.0)
    platformer_points = float(by_list.get('platformer') or 0.0)
    
    total_records_count = Record.query.filter_by(user_id=user.id).count()
    approved_records_count = Record.query.filter_by(user_id=user.id, status='approved').count()
    record_completion_percentage = round((approved_records_count / total_records_count * 100) if total_records_count > 0 else 0, 1)

    beaten_level_ids = {r.level_id for r in records if str(r.status or '') == 'approved'}
    beaten_levels = []
    if beaten_level_ids:
        try:
            beaten_levels = Level.query.filter(Level.id.in_(list(beaten_level_ids))).all()
        except Exception:
            beaten_levels = []

    beaten_main = [l for l in beaten_levels if l.list_type == 'main' and l.is_verified and l.position is not None]
    beaten_challenge = [l for l in beaten_levels if l.list_type == 'challenge' and l.is_verified and l.position is not None]
    beaten_platformer = [l for l in beaten_levels if l.list_type == 'platformer' and l.is_verified and l.position is not None]

    total_main = Level.query.filter(Level.is_verified == True, Level.list_type == 'main', Level.position.isnot(None)).count()
    total_challenge = Level.query.filter(Level.is_verified == True, Level.list_type == 'challenge', Level.position.isnot(None)).count()
    total_platformer = Level.query.filter(Level.is_verified == True, Level.list_type == 'platformer', Level.position.isnot(None)).count()

    main_percent = round((len(beaten_main) / total_main * 100) if total_main > 0 else 0, 1)
    challenge_percent = round((len(beaten_challenge) / total_challenge * 100) if total_challenge > 0 else 0, 1)
    platformer_percent = round((len(beaten_platformer) / total_platformer * 100) if total_platformer > 0 else 0, 1)

    packs = []
    try:
        packs = MapPack.query.all()
    except Exception:
        packs = []

    beaten_packs = []
    for p in packs:
        try:
            ids = [int(i.strip()) for i in (p.level_ids or '').split(',') if i.strip().isdigit()]
            if not ids:
                continue
            if all(lid in beaten_level_ids for lid in ids):
                beaten_packs.append(p)
        except Exception:
            continue
    total_pack_count = len(packs)
    beaten_pack_count = len(beaten_packs)
    pack_percent = round((beaten_pack_count / total_pack_count * 100) if total_pack_count > 0 else 0, 1)

    total_submissions_count = LevelSubmission.query.filter_by(user_id=user.id).count()
    approved_submissions_count = LevelSubmission.query.filter_by(user_id=user.id, status='approved').count()
    submission_success_rate = round((approved_submissions_count / total_submissions_count * 100) if total_submissions_count > 0 else 0, 1)
    
    recent_activity = []
    for record in records:
        recent_activity.append({
            'type': 'record',
            'item': record,
            'date': record.submitted_at,
            'status': record.status
        })
    for submission in submissions:
        recent_activity.append({
            'type': 'submission', 
            'item': submission,
            'date': submission.submitted_at,
            'status': submission.status
        })
    approved_record_notifs = []
    rejected_record_notifs = []
    approved_submission_notifs = []
    rejected_submission_notifs = []
    try:
        approved_records_list = [r for r in records if str(r.status or '') == 'approved']
        approved_records_list.sort(key=lambda r: r.submitted_at or datetime.min, reverse=True)
        approved_records_list = approved_records_list[:40]
        for r in approved_records_list:
            level = None
            try:
                level = r.level
            except Exception:
                level = None
            approved_record_notifs.append({
                'id': int(getattr(r, 'id', 0) or 0),
                'level_name': str(getattr(level, 'name', '') or ''),
                'level_creator': str(getattr(level, 'creator', '') or '')
            })
    except Exception:
        approved_record_notifs = []
    try:
        rejected_records_list = [r for r in records if str(r.status or '') == 'rejected']
        rejected_records_list.sort(key=lambda r: r.submitted_at or datetime.min, reverse=True)
        rejected_records_list = rejected_records_list[:40]
        for r in rejected_records_list:
            level = None
            try:
                level = r.level
            except Exception:
                level = None
            rejected_record_notifs.append({
                'id': int(getattr(r, 'id', 0) or 0),
                'level_name': str(getattr(level, 'name', '') or ''),
                'level_creator': str(getattr(level, 'creator', '') or '')
            })
    except Exception:
        rejected_record_notifs = []
    try:
        approved_subs_list = [s for s in submissions if str(s.status or '') == 'approved']
        approved_subs_list.sort(key=lambda s: s.submitted_at or datetime.min, reverse=True)
        approved_subs_list = approved_subs_list[:40]
        for s in approved_subs_list:
            approved_submission_notifs.append({
                'id': int(getattr(s, 'id', 0) or 0),
                'name': str(getattr(s, 'name', '') or ''),
                'creator': str(getattr(s, 'creator', '') or '')
            })
    except Exception:
        approved_submission_notifs = []
    try:
        rejected_subs_list = [s for s in submissions if str(s.status or '') == 'rejected']
        rejected_subs_list.sort(key=lambda s: s.submitted_at or datetime.min, reverse=True)
        rejected_subs_list = rejected_subs_list[:40]
        for s in rejected_subs_list:
            rejected_submission_notifs.append({
                'id': int(getattr(s, 'id', 0) or 0),
                'name': str(getattr(s, 'name', '') or ''),
                'creator': str(getattr(s, 'creator', '') or '')
            })
    except Exception:
        rejected_submission_notifs = []
    recent_activity.sort(key=lambda x: (x['date'] or datetime.min), reverse=True)
    recent_activity = recent_activity[:10]
    
    return render_template('profile.html', 
                          user=user, 
                          records=records, 
                          submissions=submissions,
                          display_points=display_points,
                          main_points=main_points,
                          challenge_points=challenge_points,
                          platformer_points=platformer_points,
                          record_completion_percentage=record_completion_percentage,
                          submission_success_rate=submission_success_rate,
                          approved_records_count=approved_records_count,
                          total_records_count=total_records_count,
                          approved_submissions_count=approved_submissions_count,
                          total_submissions_count=total_submissions_count,
                          beaten_main_count=len(beaten_main),
                          total_main_count=total_main,
                          main_percent=main_percent,
                          beaten_challenge_count=len(beaten_challenge),
                          total_challenge_count=total_challenge,
                          challenge_percent=challenge_percent,
                          beaten_platformer_count=len(beaten_platformer),
                          total_platformer_count=total_platformer,
                          platformer_percent=platformer_percent,
                          beaten_pack_count=beaten_pack_count,
                          total_pack_count=total_pack_count,
                          pack_percent=pack_percent,
                          beaten_packs=beaten_packs,
                          recent_activity=recent_activity,
                          approved_record_notifs=approved_record_notifs,
                          rejected_record_notifs=rejected_record_notifs,
                          approved_submission_notifs=approved_submission_notifs,
                          rejected_submission_notifs=rejected_submission_notifs)

@app.route('/user/<int:user_id>')
def user_profile(user_id):
    user = User.query.get_or_404(user_id)
    records = Record.query.filter_by(user_id=user.id, status='approved').all()
    submissions = LevelSubmission.query.filter_by(user_id=user.id).all()
    display_points = _sum_approved_points_for_user(user.id)
    by_list = _sum_approved_points_by_list_for_user(user.id)
    main_points = float(by_list.get('main') or 0.0)
    challenge_points = float(by_list.get('challenge') or 0.0)
    platformer_points = float(by_list.get('platformer') or 0.0)
    
    total_records = Record.query.filter_by(user_id=user.id).count()
    approved_records = len(records)
    record_completion_percentage = round((approved_records / total_records * 100) if total_records > 0 else 0, 1)
    
    # Separate beaten levels by list type
    beaten_level_ids = {r.level_id for r in records}
    beaten_levels = []
    if beaten_level_ids:
        try:
            beaten_levels = Level.query.filter(Level.id.in_(list(beaten_level_ids))).all()
        except Exception:
            beaten_levels = []
    
    beaten_main = [l for l in beaten_levels if l.list_type == 'main' and l.is_verified and l.position is not None]
    beaten_challenge = [l for l in beaten_levels if l.list_type == 'challenge' and l.is_verified and l.position is not None]
    beaten_platformer = [l for l in beaten_levels if l.list_type == 'platformer' and l.is_verified and l.position is not None]
    
    total_main = Level.query.filter(Level.is_verified == True, Level.list_type == 'main', Level.position.isnot(None)).count()
    total_challenge = Level.query.filter(Level.is_verified == True, Level.list_type == 'challenge', Level.position.isnot(None)).count()
    total_platformer = Level.query.filter(Level.is_verified == True, Level.list_type == 'platformer', Level.position.isnot(None)).count()
    
    main_percent = round((len(beaten_main) / total_main * 100) if total_main > 0 else 0, 1)
    challenge_percent = round((len(beaten_challenge) / total_challenge * 100) if total_challenge > 0 else 0, 1)
    platformer_percent = round((len(beaten_platformer) / total_platformer * 100) if total_platformer > 0 else 0, 1)

    # Map Pack stats
    packs = []
    try:
        packs = MapPack.query.all()
    except Exception:
        # Fallback if DB column is still missing despite migration
        packs = []
        
    beaten_packs = []
    for p in packs:
        try:
            ids = [int(i.strip()) for i in (p.level_ids or '').split(',') if i.strip().isdigit()]
            if not ids: continue
            if all(lid in beaten_level_ids for lid in ids):
                beaten_packs.append(p)
        except Exception:
            continue
    
    pack_count = len(packs)
    beaten_pack_count = len(beaten_packs)
    pack_percent = round((beaten_pack_count / pack_count * 100) if pack_count > 0 else 0, 1)

    total_submissions = len(submissions)
    approved_submissions = len([s for s in submissions if s.status == 'approved'])
    submission_success_rate = round((approved_submissions / total_submissions * 100) if total_submissions > 0 else 0, 1)
    
    recent_activity = []
    all_user_records = Record.query.filter_by(user_id=user.id).all()
    
    for record in all_user_records:
        recent_activity.append({
            'type': 'record',
            'item': record,
            'date': record.submitted_at,
            'status': record.status
        })
    for submission in submissions:
        recent_activity.append({
            'type': 'submission', 
            'item': submission,
            'date': submission.submitted_at,
            'status': submission.status
        })
    
    recent_activity.sort(key=lambda x: (x['date'] or datetime.min), reverse=True)
    recent_activity = recent_activity[:10]

    approved_record_notifs = []
    rejected_record_notifs = []
    approved_submission_notifs = []
    rejected_submission_notifs = []
    try:
        if getattr(current_user, 'is_authenticated', False) and int(getattr(current_user, 'id', 0) or 0) == int(user.id):
            try:
                approved_rows = (
                    db.session.query(Record, Level)
                    .join(Level, Level.id == Record.level_id)
                    .filter(Record.user_id == user.id, Record.status == 'approved')
                    .order_by(Record.submitted_at.desc())
                    .limit(40)
                    .all()
                )
                for r, lv in approved_rows:
                    approved_record_notifs.append({
                        'id': int(getattr(r, 'id', 0) or 0),
                        'level_name': str(getattr(lv, 'name', '') or ''),
                        'level_creator': str(getattr(lv, 'creator', '') or ''),
                    })
            except Exception:
                approved_record_notifs = []
            try:
                rejected_rows = (
                    db.session.query(Record, Level)
                    .join(Level, Level.id == Record.level_id)
                    .filter(Record.user_id == user.id, Record.status == 'rejected')
                    .order_by(Record.submitted_at.desc())
                    .limit(40)
                    .all()
                )
                for r, lv in rejected_rows:
                    rejected_record_notifs.append({
                        'id': int(getattr(r, 'id', 0) or 0),
                        'level_name': str(getattr(lv, 'name', '') or ''),
                        'level_creator': str(getattr(lv, 'creator', '') or ''),
                    })
            except Exception:
                rejected_record_notifs = []
            try:
                approved_subs = (
                    LevelSubmission.query
                    .filter(LevelSubmission.user_id == user.id, LevelSubmission.status == 'approved')
                    .order_by(LevelSubmission.submitted_at.desc())
                    .limit(40)
                    .all()
                )
                for s in approved_subs:
                    approved_submission_notifs.append({
                        'id': int(getattr(s, 'id', 0) or 0),
                        'name': str(getattr(s, 'name', '') or ''),
                        'creator': str(getattr(s, 'creator', '') or ''),
                    })
            except Exception:
                approved_submission_notifs = []
            try:
                rejected_subs = (
                    LevelSubmission.query
                    .filter(LevelSubmission.user_id == user.id, LevelSubmission.status == 'rejected')
                    .order_by(LevelSubmission.submitted_at.desc())
                    .limit(40)
                    .all()
                )
                for s in rejected_subs:
                    rejected_submission_notifs.append({
                        'id': int(getattr(s, 'id', 0) or 0),
                        'name': str(getattr(s, 'name', '') or ''),
                        'creator': str(getattr(s, 'creator', '') or ''),
                    })
            except Exception:
                rejected_submission_notifs = []
    except Exception:
        pass
    
    return render_template('profile.html', 
                          user=user, 
                          records=records, 
                          submissions=submissions,
                          display_points=display_points,
                          main_points=main_points,
                          challenge_points=challenge_points,
                          platformer_points=platformer_points,
                          record_completion_percentage=record_completion_percentage,
                          approved_records_count=approved_records,
                          total_records_count=total_records,
                          submission_success_rate=submission_success_rate,
                          approved_submissions_count=approved_submissions,
                          total_submissions_count=total_submissions,
                          beaten_main_count=len(beaten_main),
                          total_main_count=total_main,
                          main_percent=main_percent,
                          beaten_challenge_count=len(beaten_challenge),
                          total_challenge_count=total_challenge,
                          challenge_percent=challenge_percent,
                          beaten_platformer_count=len(beaten_platformer),
                          total_platformer_count=total_platformer,
                          platformer_percent=platformer_percent,
                          beaten_pack_count=beaten_pack_count,
                          total_pack_count=pack_count,
                          pack_percent=pack_percent,
                          beaten_packs=beaten_packs,
                          recent_activity=recent_activity,
                          approved_record_notifs=approved_record_notifs,
                          rejected_record_notifs=rejected_record_notifs,
                          approved_submission_notifs=approved_submission_notifs,
                          rejected_submission_notifs=rejected_submission_notifs)

@app.route('/stats')
@login_required
def stats():
    return redirect(url_for('user_stats', user_id=current_user.id))

@app.route('/me/export', methods=['GET'])
@login_required
def export_my_data():
    recs = (
        Record.query
        .filter(Record.user_id == current_user.id)
        .order_by(Record.submitted_at.desc())
        .all()
    )
    subs = (
        LevelSubmission.query
        .filter(LevelSubmission.user_id == current_user.id)
        .order_by(LevelSubmission.submitted_at.desc())
        .all()
    )
    payload = {
        'user': _user_profile_json(current_user),
        'records': [],
        'submissions': [],
    }
    for r in recs:
        lv = None
        try:
            lv = r.level
        except Exception:
            lv = None
        payload['records'].append(_record_json(r, lv))
    for s in subs:
        payload['submissions'].append({
            'id': s.id,
            'name': s.name,
            'creator': s.creator,
            'video_url': s.video_url,
            'status': s.status,
            'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None,
        })
    data = json.dumps(payload, default=str)
    return Response(data, mimetype='application/json', headers={'Content-Disposition': 'attachment; filename="my_data_export.json"'})

@app.route('/user/<int:user_id>/stats')
def user_stats(user_id):
    user = User.query.get_or_404(user_id)
    is_self = False
    try:
        is_self = bool(getattr(current_user, 'is_authenticated', False)) and int(getattr(current_user, 'id', 0) or 0) == int(user.id)
    except Exception:
        is_self = False

    rec_all_q = Record.query.filter(Record.user_id == user.id)
    sub_all_q = LevelSubmission.query.filter(LevelSubmission.user_id == user.id)

    rec_vis_q = rec_all_q
    sub_vis_q = sub_all_q
    if not is_self:
        rec_vis_q = rec_vis_q.filter(Record.status == 'approved')
        sub_vis_q = sub_vis_q.filter(LevelSubmission.status == 'approved')

    total_main_levels_count = Level.query.filter(Level.is_verified == True, Level.list_type == 'main', Level.position.isnot(None)).count()
    total_challenge_levels_count = Level.query.filter(Level.is_verified == True, Level.list_type == 'challenge', Level.position.isnot(None)).count()
    total_platformer_levels_count = Level.query.filter(Level.is_verified == True, Level.list_type == 'platformer', Level.position.isnot(None)).count()

    rec_status_rows = db.session.query(Record.status, db.func.count(Record.id)).filter(Record.user_id == user.id).group_by(Record.status).all() if is_self else [('approved', rec_vis_q.count())]
    rec_status_counts = {str(s or ''): int(c or 0) for (s, c) in rec_status_rows if str(s or '')}

    sub_status_rows = db.session.query(LevelSubmission.status, db.func.count(LevelSubmission.id)).filter(LevelSubmission.user_id == user.id).group_by(LevelSubmission.status).all() if is_self else [('approved', sub_vis_q.count())]
    sub_status_counts = {str(s or ''): int(c or 0) for (s, c) in sub_status_rows if str(s or '')}

    records_total = int(sum(rec_status_counts.values()) or 0)
    records_approved = int(rec_status_counts.get('approved', 0) or 0)
    submissions_total = int(sum(sub_status_counts.values()) or 0)
    submissions_approved = int(sub_status_counts.get('approved', 0) or 0)

    record_success_rate = round((records_approved / records_total * 100) if records_total > 0 else 0, 1)
    submission_success_rate = round((submissions_approved / submissions_total * 100) if submissions_total > 0 else 0, 1)

    beaten_rows = db.session.query(Record.level_id).filter(Record.user_id == user.id)
    if is_self:
        beaten_rows = beaten_rows.filter(Record.status == 'approved')
    beaten_rows = beaten_rows.distinct().all()
    beaten_level_ids = {int(lid or 0) for (lid,) in beaten_rows if int(lid or 0)}

    beaten_main_levels_count = 0
    beaten_challenge_levels_count = 0
    beaten_platformer_levels_count = 0
    try:
        if beaten_level_ids:
            levels_for_beaten = Level.query.filter(Level.id.in_(list(beaten_level_ids))).all()
            for lv in levels_for_beaten:
                if not getattr(lv, 'is_verified', False) or lv.position is None:
                    continue
                if lv.list_type == 'main':
                    beaten_main_levels_count += 1
                elif lv.list_type == 'challenge':
                    beaten_challenge_levels_count += 1
                elif lv.list_type == 'platformer':
                    beaten_platformer_levels_count += 1
    except Exception:
        beaten_main_levels_count = 0
        beaten_challenge_levels_count = 0
        beaten_platformer_levels_count = 0

    beaten_main_levels_percentage = round((beaten_main_levels_count / total_main_levels_count * 100) if total_main_levels_count > 0 else 0, 1)
    beaten_challenge_levels_percentage = round((beaten_challenge_levels_count / total_challenge_levels_count * 100) if total_challenge_levels_count > 0 else 0, 1)
    beaten_platformer_levels_percentage = round((beaten_platformer_levels_count / total_platformer_levels_count * 100) if total_platformer_levels_count > 0 else 0, 1)

    # Map Pack stats
    packs = MapPack.query.all()
    beaten_pack_count = 0
    for p in packs:
        ids = [int(i.strip()) for i in (p.level_ids or '').split(',') if i.strip().isdigit()]
        if ids and all(lid in beaten_level_ids for lid in ids):
            beaten_pack_count += 1
    total_pack_count = len(packs)
    pack_percent = round((beaten_pack_count / total_pack_count * 100) if total_pack_count > 0 else 0, 1)

    total_site_levels_count = int(total_main_levels_count or 0)
    beaten_levels_count = int(beaten_main_levels_count or 0)
    beaten_levels_percentage = beaten_main_levels_percentage

    recent_records = rec_vis_q.order_by(Record.submitted_at.desc()).limit(40).all()
    recent_submissions = sub_vis_q.order_by(LevelSubmission.submitted_at.desc()).limit(40).all()

    device_rows = db.session.query(Record.device, db.func.count(Record.id)).filter(Record.user_id == user.id)
    if not is_self:
        device_rows = device_rows.filter(Record.status == 'approved')
    device_rows = device_rows.group_by(Record.device).order_by(db.func.count(Record.id).desc()).all()
    device_breakdown = [{'device': (d or 'unknown'), 'count': int(c or 0)} for (d, c) in device_rows]

    top_beaten_levels = []
    try:
        if beaten_level_ids:
            top_beaten_levels = Level.query.filter(Level.id.in_(list(beaten_level_ids))).order_by(Level.points_reward.desc()).limit(15).all()
    except Exception:
        top_beaten_levels = []

    graphs = {}
    try:
        q = db.session.query(db.func.date(Record.submitted_at), db.func.count(Record.id)).filter(Record.user_id == user.id)
        if not is_self:
            q = q.filter(Record.status == 'approved')
        rows = q.group_by(db.func.date(Record.submitted_at)).order_by(db.func.date(Record.submitted_at)).all()
        graphs['records'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for d, c in rows if d]}
    except Exception:
        graphs['records'] = {'labels': [], 'data': []}

    try:
        q = (
            db.session.query(db.func.date(Record.submitted_at), db.func.sum(Level.points_reward))
            .select_from(Record)
            .join(Level, Level.id == Record.level_id)
            .filter(Record.user_id == user.id, Record.status == 'approved')
        )
        rows = q.group_by(db.func.date(Record.submitted_at)).order_by(db.func.date(Record.submitted_at)).all()
        graphs['points_gained'] = {'labels': [str(d) for d, _ in rows if d], 'data': [float(s or 0.0) for d, s in rows if d]}
    except Exception:
        graphs['points_gained'] = {'labels': [], 'data': []}

    if is_self:
        try:
            rows = db.session.query(db.func.date(Record.submitted_at), db.func.count(Record.id)).filter(Record.user_id == user.id, Record.status == 'approved').group_by(db.func.date(Record.submitted_at)).order_by(db.func.date(Record.submitted_at)).all()
            graphs['records_approved'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for d, c in rows if d]}
        except Exception:
            graphs['records_approved'] = {'labels': [], 'data': []}

    try:
        q = db.session.query(db.func.date(LevelSubmission.submitted_at), db.func.count(LevelSubmission.id)).filter(LevelSubmission.user_id == user.id)
        if not is_self:
            q = q.filter(LevelSubmission.status == 'approved')
        rows = q.group_by(db.func.date(LevelSubmission.submitted_at)).order_by(db.func.date(LevelSubmission.submitted_at)).all()
        graphs['submissions'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for d, c in rows if d]}
    except Exception:
        graphs['submissions'] = {'labels': [], 'data': []}

    if is_self:
        try:
            rows = db.session.query(db.func.date(LevelSubmission.submitted_at), db.func.count(LevelSubmission.id)).filter(LevelSubmission.user_id == user.id, LevelSubmission.status == 'approved').group_by(db.func.date(LevelSubmission.submitted_at)).order_by(db.func.date(LevelSubmission.submitted_at)).all()
            graphs['submissions_approved'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for d, c in rows if d]}
        except Exception:
            graphs['submissions_approved'] = {'labels': [], 'data': []}

    return render_template(
        'stats.html',
        user=user,
        is_self=is_self,
        total_site_levels_count=total_site_levels_count,
        beaten_levels_count=beaten_levels_count,
        beaten_levels_percentage=beaten_levels_percentage,
        total_main_levels_count=total_main_levels_count,
        total_challenge_levels_count=total_challenge_levels_count,
        total_platformer_levels_count=total_platformer_levels_count,
        beaten_main_levels_count=beaten_main_levels_count,
        beaten_challenge_levels_count=beaten_challenge_levels_count,
        beaten_platformer_levels_count=beaten_platformer_levels_count,
        beaten_main_levels_percentage=beaten_main_levels_percentage,
        beaten_challenge_levels_percentage=beaten_challenge_levels_percentage,
        beaten_platformer_levels_percentage=beaten_platformer_levels_percentage,
        records_total=records_total,
        records_approved=records_approved,
        submissions_total=submissions_total,
        submissions_approved=submissions_approved,
        record_success_rate=record_success_rate,
        submission_success_rate=submission_success_rate,
        rec_status_counts=rec_status_counts,
        sub_status_counts=sub_status_counts,
        recent_records=recent_records,
        recent_submissions=recent_submissions,
        device_breakdown=device_breakdown,
        top_beaten_levels=top_beaten_levels,
        graphs=graphs,
    )

@app.route('/admin/users/<int:user_id>/media/delete', methods=['POST'])
@login_required
def admin_delete_user_media(user_id):
    _require_perm('manage_users')
    kind = (request.form.get('kind') or '').strip().lower()
    if kind not in ('avatar', 'banner'):
        flash('Invalid media kind.', 'danger')
        return redirect(url_for('admin_dashboard'))
    u = db.session.get(User, user_id)
    if not u:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_dashboard'))
    fname = None
    if kind == 'avatar':
        fname = (getattr(u, 'avatar_filename', '') or '').strip()
        u.avatar_filename = None
    else:
        fname = (getattr(u, 'banner_filename', '') or '').strip()
        u.banner_filename = None
    db.session.commit()
    if fname:
        try:
            path = os.path.join(app.root_path, 'static', 'profile_media', fname)
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
    add_site_log('admin_action', f'{current_user.username} deleted {kind} image for {u.username}.', actor_user_id=current_user.id)
    flash(f'{kind.title()} removed.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/levels/<int:level_id>/thumbnail/delete', methods=['POST'])
@login_required
def admin_delete_level_thumbnail(level_id):
    _require_perm('manage_levels')
    lv = db.session.get(Level, level_id)
    if not lv:
        flash('Level not found.', 'danger')
        return redirect(url_for('admin_dashboard'))
    fname = (getattr(lv, 'thumbnail_filename', '') or '').strip()
    lv.thumbnail_filename = None
    db.session.commit()
    if fname:
        try:
            path = os.path.join(app.root_path, 'static', 'level_thumbs', fname)
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
    add_site_log('admin_action', f'{current_user.username} deleted thumbnail for level "{lv.name}".', actor_user_id=current_user.id)
    flash('Thumbnail removed.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/recap')
@login_required
def recap():
    if not _year_recap_enabled():
        abort(404)

    now = datetime.utcnow()
    year = int(now.year)

    def _recap_for_year(y: int):
        start = datetime(y, 1, 1)
        end = datetime(y + 1, 1, 1)
        month_labels = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        groups = [
            {'key': 'main_verified', 'title': 'Main List', 'list_type': 'main', 'is_verified': True},
            {'key': 'challenge_verified', 'title': 'Challenge List', 'list_type': 'challenge', 'is_verified': True},
            {'key': 'platformer_verified', 'title': 'Platformer List', 'list_type': 'platformer', 'is_verified': True},
            {'key': 'main_unverified', 'title': 'Unverified (Main)', 'list_type': 'main', 'is_verified': False},
            {'key': 'challenge_unverified', 'title': 'Unverified (Challenge)', 'list_type': 'challenge', 'is_verified': False},
            {'key': 'platformer_unverified', 'title': 'Unverified (Platformer)', 'list_type': 'platformer', 'is_verified': False},
        ]

        out = []
        for g in groups:
            total_levels = Level.query.filter(
                Level.list_type == g['list_type'],
                Level.is_verified == bool(g['is_verified']),
                Level.position.isnot(None),
            ).count()

            beaten_count = 0
            try:
                beaten_count = int(
                    db.session.query(db.func.count(db.func.distinct(Record.level_id)))
                    .select_from(Record)
                    .join(Level, Level.id == Record.level_id)
                    .filter(
                        Record.user_id == current_user.id,
                        Record.status == 'approved',
                        Record.submitted_at >= start,
                        Record.submitted_at < end,
                        Level.list_type == g['list_type'],
                        Level.is_verified == bool(g['is_verified']),
                        Level.position.isnot(None),
                    )
                    .scalar()
                    or 0
                )
            except Exception:
                beaten_count = 0

            percent = round((beaten_count / total_levels * 100) if total_levels > 0 else 0, 1)
            out.append(
                {
                    'key': g['key'],
                    'title': g['title'],
                    'beaten': beaten_count,
                    'total': int(total_levels or 0),
                    'percent': percent,
                }
            )

        total_beaten = 0
        records_total = 0
        records_approved = 0
        submissions_total = 0
        submissions_approved = 0
        points_gained = 0.0
        monthly = {'labels': [], 'beaten': [], 'points': []}
        monthly_rows = []
        best_month = {'month': '', 'beaten': 0, 'points': 0.0}
        device_breakdown = []
        top_beaten_levels = []
        highlights = []
        quotes = []
        record_status_counts = []
        submission_status_counts = []
        list_breakdown = []
        position_buckets = []
        top_creators = []
        top_tags = []
        streaks = {}
        time_breakdown = {}
        rating_avgs = {}
        first_record = None
        first_mappack = None
        pack_stats = {'beaten': 0, 'total': 0, 'percent': 0.0}

        try:
            # First record of the year
            first_rec_row = (
                Record.query.filter(Record.user_id == current_user.id, Record.submitted_at >= start, Record.submitted_at < end)
                .order_by(Record.submitted_at.asc())
                .first()
            )
            if first_rec_row:
                first_record = {
                    'level_name': first_rec_row.level.name,
                    'date': first_rec_row.submitted_at.strftime('%Y-%m-%d'),
                    'status': first_rec_row.status
                }
        except Exception:
            pass

        try:
            all_packs = MapPack.query.all()
            pack_stats['total'] = len(all_packs)
            earliest_rows = (
                db.session.query(Record.level_id, db.func.min(Record.submitted_at))
                .filter(Record.user_id == current_user.id, Record.status == 'approved')
                .group_by(Record.level_id)
                .all()
            )
            earliest_by_level = {int(lid): dt for (lid, dt) in (earliest_rows or []) if lid and dt}
            beaten_packs_this_year = []
            for p in all_packs:
                ids = [int(i.strip()) for i in (p.level_ids or '').split(',') if i.strip().isdigit()]
                if not ids:
                    continue
                ok = True
                finish_date = None
                for lid in ids:
                    dt = earliest_by_level.get(int(lid))
                    if not dt:
                        ok = False
                        break
                    if finish_date is None or dt > finish_date:
                        finish_date = dt
                if not ok or not finish_date:
                    continue
                if start <= finish_date < end:
                    beaten_packs_this_year.append({'pack': p, 'date': finish_date})
            pack_stats['beaten'] = len(beaten_packs_this_year)
            pack_stats['percent'] = round((pack_stats['beaten'] / pack_stats['total'] * 100) if pack_stats['total'] > 0 else 0, 1)
            if beaten_packs_this_year:
                beaten_packs_this_year.sort(key=lambda x: x['date'])
                first_mappack = {'name': beaten_packs_this_year[0]['pack'].name, 'date': beaten_packs_this_year[0]['date'].strftime('%Y-%m-%d')}
        except Exception:
            pass

        last_record = None
        global_rank = {}
        clan_stats = None
        closest_rival = None

        try:
            records_total = int(
                db.session.query(db.func.count(Record.id))
                .filter(Record.user_id == current_user.id, Record.submitted_at >= start, Record.submitted_at < end)
                .scalar()
                or 0
            )
        except Exception:
            records_total = 0

        try:
            records_approved = int(
                db.session.query(db.func.count(Record.id))
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .scalar()
                or 0
            )
        except Exception:
            records_approved = 0

        try:
            rows = (
                db.session.query(Record.status, db.func.count(Record.id))
                .filter(Record.user_id == current_user.id, Record.submitted_at >= start, Record.submitted_at < end)
                .group_by(Record.status)
                .order_by(db.func.count(Record.id).desc())
                .all()
            )
            record_status_counts = [{'status': (s or 'unknown'), 'count': int(c or 0)} for s, c in (rows or [])]
        except Exception:
            record_status_counts = []

        try:
            submissions_total = int(
                db.session.query(db.func.count(LevelSubmission.id))
                .filter(LevelSubmission.user_id == current_user.id, LevelSubmission.submitted_at >= start, LevelSubmission.submitted_at < end)
                .scalar()
                or 0
            )
        except Exception:
            submissions_total = 0

        try:
            submissions_approved = int(
                db.session.query(db.func.count(LevelSubmission.id))
                .filter(
                    LevelSubmission.user_id == current_user.id,
                    LevelSubmission.status == 'approved',
                    LevelSubmission.submitted_at >= start,
                    LevelSubmission.submitted_at < end,
                )
                .scalar()
                or 0
            )
        except Exception:
            submissions_approved = 0

        try:
            rows = (
                db.session.query(LevelSubmission.status, db.func.count(LevelSubmission.id))
                .filter(LevelSubmission.user_id == current_user.id, LevelSubmission.submitted_at >= start, LevelSubmission.submitted_at < end)
                .group_by(LevelSubmission.status)
                .order_by(db.func.count(LevelSubmission.id).desc())
                .all()
            )
            submission_status_counts = [{'status': (s or 'unknown'), 'count': int(c or 0)} for s, c in (rows or [])]
        except Exception:
            submission_status_counts = []

        try:
            total_beaten = int(
                db.session.query(db.func.count(db.func.distinct(Record.level_id)))
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .scalar()
                or 0
            )
        except Exception:
            total_beaten = 0

        try:
            rows = (
                db.session.query(Record.submitted_at, Record.level_id, Level.points_reward)
                .select_from(Record)
                .join(Level, Level.id == Record.level_id)
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .all()
            )
        except Exception:
            rows = []

        seen_levels = set()
        month_beaten = {m: 0 for m in range(1, 13)}
        month_points = {m: 0.0 for m in range(1, 13)}
        for dt, lvl_id, pts in rows:
            try:
                mid = int(getattr(dt, 'month', 0) or 0)
            except Exception:
                mid = 0
            if mid < 1 or mid > 12:
                continue
            try:
                lvl_id = int(lvl_id or 0)
            except Exception:
                lvl_id = 0
            if not lvl_id or lvl_id in seen_levels:
                continue
            seen_levels.add(lvl_id)
            month_beaten[mid] += 1
            try:
                month_points[mid] += float(pts or 0.0)
            except Exception:
                month_points[mid] += 0.0

        points_gained = round(sum(month_points.values()) if month_points else 0.0, 2)
        monthly['labels'] = month_labels
        monthly['beaten'] = [int(month_beaten[m]) for m in range(1, 13)]
        monthly['points'] = [round(float(month_points[m]), 2) for m in range(1, 13)]
        monthly_rows = [
            {'month': month_labels[m - 1], 'beaten': int(month_beaten[m]), 'points': round(float(month_points[m]), 2)}
            for m in range(1, 13)
        ]
        try:
            best = max(range(1, 13), key=lambda m: (int(month_points[m]), int(month_beaten[m])))
            best_month = {
                'month': month_labels[best - 1],
                'beaten': int(month_beaten[best]),
                'points': round(float(month_points[best]), 2),
            }
        except Exception:
            best_month = {'month': '', 'beaten': 0, 'points': 0.0}

        try:
            device_rows = (
                db.session.query(Record.device, db.func.count(Record.id))
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .group_by(Record.device)
                .order_by(db.func.count(Record.id).desc())
                .all()
            )
            device_breakdown = [{'device': (d or 'unknown'), 'count': int(c or 0)} for (d, c) in device_rows]
        except Exception:
            device_breakdown = []

        try:
            if seen_levels:
                top_beaten_levels = (
                    Level.query.filter(Level.id.in_(list(seen_levels)))
                    .order_by(Level.points_reward.desc())
                    .limit(8)
                    .all()
                )
        except Exception:
            top_beaten_levels = []

        beaten_levels = []
        if seen_levels:
            try:
                beaten_levels = Level.query.filter(Level.id.in_(list(seen_levels))).all()
            except Exception:
                beaten_levels = []

        try:
            by_lt = {}
            for lv in (beaten_levels or []):
                lt = (getattr(lv, 'list_type', '') or 'main').strip().lower() or 'main'
                if lt not in ('main', 'challenge', 'platformer'):
                    lt = 'main'
                by_lt.setdefault(lt, {'list_type': lt, 'beaten': 0, 'points': 0.0})
                by_lt[lt]['beaten'] += 1
                try:
                    by_lt[lt]['points'] += float(getattr(lv, 'points_reward', 0.0) or 0.0)
                except Exception:
                    by_lt[lt]['points'] += 0.0
            list_breakdown = sorted(
                [{'list_type': k, 'beaten': int(v['beaten']), 'points': round(float(v['points']), 2)} for k, v in by_lt.items()],
                key=lambda r: (float(r.get('points') or 0.0), int(r.get('beaten') or 0)),
                reverse=True,
            )
        except Exception:
            list_breakdown = []

        try:
            buckets = [
                (1, 10, 'Top 10'),
                (11, 25, '11–25'),
                (26, 50, '26–50'),
                (51, 100, '51–100'),
                (101, 200, '101–200'),
                (201, 999999, '201+'),
            ]
            counts = {label: 0 for _, _, label in buckets}
            for lv in (beaten_levels or []):
                try:
                    pos = int(getattr(lv, 'position', 0) or 0)
                except Exception:
                    pos = 0
                if pos <= 0:
                    continue
                for a, b, label in buckets:
                    if pos >= a and pos <= b:
                        counts[label] += 1
                        break
            position_buckets = [{'label': label, 'count': int(counts[label])} for _, _, label in buckets]
        except Exception:
            position_buckets = []

        try:
            creator_counts = {}
            for lv in (beaten_levels or []):
                c = (getattr(lv, 'creator', '') or '').strip() or 'Unknown'
                creator_counts[c] = creator_counts.get(c, 0) + 1
            top_creators = [
                {'creator': k, 'count': int(v)}
                for k, v in sorted(creator_counts.items(), key=lambda kv: (-int(kv[1]), kv[0].lower()))
            ][:12]
        except Exception:
            top_creators = []

        try:
            tag_counts = {}
            for lv in (beaten_levels or []):
                raw = str(getattr(lv, 'categories', '') or '')
                if not raw:
                    continue
                parts = [p.strip() for p in raw.split(',')]
                for p in parts:
                    if not p:
                        continue
                    tag_counts[p] = tag_counts.get(p, 0) + 1
            top_tags = [
                {'tag': k, 'count': int(v)}
                for k, v in sorted(tag_counts.items(), key=lambda kv: (-int(kv[1]), kv[0].lower()))
            ][:20]
        except Exception:
            top_tags = []

        try:
            dts = (
                db.session.query(Record.submitted_at)
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .all()
            )
            days = sorted({dt.date() for (dt,) in (dts or []) if dt})
            active_days = len(days)
            longest = 0
            current = 0
            prev = None
            for d in days:
                if prev and (d - prev).days == 1:
                    current += 1
                else:
                    current = 1
                longest = max(longest, current)
                prev = d
            streaks = {'active_days': int(active_days), 'longest_streak_days': int(longest)}
        except Exception:
            streaks = {}

        try:
            dts = [dt for (dt,) in (dts or []) if dt] if 'dts' in locals() else []
            dow_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            by_dow = {k: 0 for k in dow_labels}
            by_hour = {h: 0 for h in range(24)}
            for dt in dts:
                try:
                    by_dow[dow_labels[int(dt.weekday())]] += 1
                except Exception:
                    pass
                try:
                    by_hour[int(dt.hour)] += 1
                except Exception:
                    pass
            time_breakdown = {
                'by_dow': [{'label': k, 'count': int(by_dow[k])} for k in dow_labels],
                'by_hour': [{'label': f'{h:02d}:00', 'count': int(by_hour[h])} for h in range(24)],
            }
        except Exception:
            time_breakdown = {}

        try:
            av = (
                db.session.query(
                    db.func.avg(Record.rating_deco),
                    db.func.avg(Record.rating_gameplay),
                    db.func.avg(Record.rating_difficulty),
                )
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .first()
            )
            if av:
                rating_avgs = {
                    'deco': round(float(av[0]), 2) if av[0] is not None else None,
                    'gameplay': round(float(av[1]), 2) if av[1] is not None else None,
                    'difficulty': round(float(av[2]), 2) if av[2] is not None else None,
                }
        except Exception:
            rating_avgs = {}

        try:
            r = (
                db.session.query(Record, Level)
                .join(Level, Level.id == Record.level_id)
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .order_by(Record.submitted_at.asc())
                .first()
            )
            if r and r[0] and r[1]:
                first_record = {
                    'date': r[0].submitted_at,
                    'level_id': int(r[1].id),
                    'level_name': r[1].name,
                    'creator': r[1].creator,
                    'points': float(r[1].points_reward or 0.0),
                    'position': int(r[1].position or 0),
                    'list_type': (r[1].list_type or 'main'),
                }
        except Exception:
            first_record = None

        try:
            r = (
                db.session.query(Record, Level)
                .join(Level, Level.id == Record.level_id)
                .filter(
                    Record.user_id == current_user.id,
                    Record.status == 'approved',
                    Record.submitted_at >= start,
                    Record.submitted_at < end,
                )
                .order_by(Record.submitted_at.desc())
                .first()
            )
            if r and r[0] and r[1]:
                last_record = {
                    'date': r[0].submitted_at,
                    'level_id': int(r[1].id),
                    'level_name': r[1].name,
                    'creator': r[1].creator,
                    'points': float(r[1].points_reward or 0.0),
                    'position': int(r[1].position or 0),
                    'list_type': (r[1].list_type or 'main'),
                }
        except Exception:
            last_record = None

        try:
            subq = (
                db.session.query(
                    Record.user_id.label('uid'),
                    db.func.coalesce(db.func.sum(Level.points_reward), 0.0).label('pts'),
                )
                .select_from(Record)
                .join(Level, Level.id == Record.level_id)
                .filter(Record.status == 'approved', Record.submitted_at >= start, Record.submitted_at < end)
                .group_by(Record.user_id)
                .subquery()
            )
            my_pts = (
                db.session.query(db.func.coalesce(subq.c.pts, 0.0))
                .select_from(subq)
                .filter(subq.c.uid == current_user.id)
                .scalar()
            )
            my_pts = float(my_pts or 0.0)
            higher = int(db.session.query(db.func.count()).select_from(subq).filter(subq.c.pts > my_pts).scalar() or 0)
            total = int(db.session.query(db.func.count()).select_from(subq).scalar() or 0)
            global_rank = {'points_rank': int(higher + 1), 'points_total': int(total), 'points_gained': round(my_pts, 2)}
            if total > 1:
                rival_uid = None
                rival_pts = None
                rival_diff = None
                try:
                    rows = (
                        db.session.query(subq.c.uid, subq.c.pts)
                        .select_from(subq)
                        .order_by(subq.c.pts.asc())
                        .all()
                    )
                except Exception:
                    rows = []
                for uid, pts in (rows or []):
                    try:
                        uid_int = int(uid or 0)
                    except Exception:
                        uid_int = 0
                    if uid_int == int(current_user.id or 0):
                        continue
                    try:
                        pts_val = float(pts or 0.0)
                    except Exception:
                        pts_val = 0.0
                    diff = abs(pts_val - my_pts)
                    if rival_uid is None or rival_diff is None or diff < rival_diff:
                        rival_uid = uid_int
                        rival_pts = pts_val
                        rival_diff = diff
                if rival_uid and rival_pts is not None:
                    try:
                        rival_user = db.session.get(User, rival_uid)
                    except Exception:
                        rival_user = None
                    if rival_user:
                        direction = 'ahead'
                        if rival_pts < my_pts:
                            direction = 'behind'
                        closest_rival = {
                            'username': rival_user.username,
                            'user_id': int(rival_user.id),
                            'points_gained': round(float(rival_pts), 2),
                            'diff_points': round(float(rival_diff or 0.0), 2),
                            'direction': direction,
                        }
        except Exception:
            global_rank = {}

        try:
            if getattr(current_user, 'clan_id', None):
                clan_id = int(current_user.clan_id)
                clan = db.session.get(Clan, clan_id)
                if clan:
                    member_count = int(User.query.filter(User.clan_id == clan_id).count() or 0)
                    total_points = (
                        db.session.query(db.func.coalesce(db.func.sum(User.points), 0.0))
                        .filter(User.clan_id == clan_id)
                        .scalar()
                    )
                    total_points = float(total_points or 0.0)
                    clan_subq = (
                        db.session.query(
                            Record.user_id.label('uid'),
                            db.func.coalesce(db.func.sum(Level.points_reward), 0.0).label('pts'),
                        )
                        .select_from(Record)
                        .join(Level, Level.id == Record.level_id)
                        .filter(Record.status == 'approved', Record.submitted_at >= start, Record.submitted_at < end)
                        .group_by(Record.user_id)
                        .subquery()
                    )
                    clan_year_points = (
                        db.session.query(db.func.coalesce(db.func.sum(clan_subq.c.pts), 0.0))
                        .select_from(clan_subq)
                        .join(User, User.id == clan_subq.c.uid)
                        .filter(User.clan_id == clan_id)
                        .scalar()
                    )
                    clan_year_points = float(clan_year_points or 0.0)
                    clan_stats = {
                        'name': clan.name,
                        'member_count': member_count,
                        'total_points': round(total_points, 2),
                        'year_points': round(clan_year_points, 2),
                    }
        except Exception:
            clan_stats = None

        try:
            best_device = ''
            if device_breakdown:
                best_device = str(device_breakdown[0].get('device') or '')
            main_v = next((g for g in out if g.get('key') == 'main_verified'), None) or {}
            chall_v = next((g for g in out if g.get('key') == 'challenge_verified'), None) or {}
            plat_v = next((g for g in out if g.get('key') == 'platformer_verified'), None) or {}
            if total_beaten > 0:
                quotes.append(f'You beat {total_beaten} unique levels in {y}.')
            if points_gained > 0:
                quotes.append(f'You gained {points_gained} points this year — that’s a lot of grind.')
            if best_month.get('month') and (best_month.get('beaten') or best_month.get('points')):
                quotes.append(f'Your biggest month was {best_month["month"]}: {best_month["beaten"]} beaten, {best_month["points"]} points.')
            if best_device:
                quotes.append(f'Your go-to device was {best_device}.')
            if float(main_v.get('percent') or 0) >= 50:
                quotes.append('Main list progress is looking serious. Keep going.')
            if float(chall_v.get('percent') or 0) >= 50:
                quotes.append('Challenge list progress is crazy. Respect.')
            if float(plat_v.get('percent') or 0) >= 50:
                quotes.append('Platformer progress is looking strong.')
            if records_approved >= 25:
                quotes.append('25+ approvals in a year is wild consistency.')
            if points_gained >= 1000:
                quotes.append('That point total could buy a whole lot of imaginary gold.')
            if streaks and int(streaks.get('longest_streak_days') or 0) >= 7:
                quotes.append(f'You hit a {int(streaks.get("longest_streak_days") or 0)}-day approval streak. Insane.')
        except Exception:
            quotes = quotes or []

        try:
            highlights = [
                {'label': 'Unique levels beaten', 'value': int(total_beaten)},
                {'label': 'Points gained', 'value': float(points_gained)},
                {'label': 'Approved records', 'value': int(records_approved)},
                {'label': 'Total records submitted', 'value': int(records_total)},
                {'label': 'Approved submissions', 'value': int(submissions_approved)},
                {'label': 'Total submissions', 'value': int(submissions_total)},
            ]
        except Exception:
            highlights = []

        return {
            'year': y,
            'groups': out,
            'total_beaten': total_beaten,
            'records_total': records_total,
            'records_approved': records_approved,
            'submissions_total': submissions_total,
            'submissions_approved': submissions_approved,
            'points_gained': points_gained,
            'monthly': monthly,
            'monthly_rows': monthly_rows,
            'best_month': best_month,
            'device_breakdown': device_breakdown,
            'top_beaten_levels': top_beaten_levels,
            'highlights': highlights,
            'quotes': quotes,
            'record_status_counts': record_status_counts,
            'submission_status_counts': submission_status_counts,
            'list_breakdown': list_breakdown,
            'position_buckets': position_buckets,
            'top_creators': top_creators,
            'top_tags': top_tags,
            'streaks': streaks,
            'time_breakdown': time_breakdown,
            'rating_avgs': rating_avgs,
            'first_record': first_record,
            'first_mappack': first_mappack,
            'pack_stats': pack_stats,
            'last_record': last_record,
            'global_rank': global_rank,
            'clan_stats': clan_stats,
            'closest_rival': closest_rival,
        }

    this_year = _recap_for_year(year)
    last_year = _recap_for_year(year - 1)
    return render_template('recap.html', this_year=this_year, last_year=last_year)

@app.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        old_bio = str(getattr(current_user, 'bio', '') or '')
        old_youtube = str(getattr(current_user, 'youtube_link', '') or '')
        old_avatar = str(getattr(current_user, 'avatar_filename', '') or '')
        old_banner = str(getattr(current_user, 'banner_filename', '') or '')

        current_user.bio = request.form.get('bio')
        current_user.youtube_link = request.form.get('youtube_link')
        avatar_file = request.files.get('avatar_file')
        if avatar_file and getattr(avatar_file, 'filename', None):
            unique, err = _save_uploaded_file(avatar_file, app.config['PROFILE_MEDIA_UPLOAD_FOLDER'], _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('Avatar must be a valid image file.', 'danger')
                return redirect(url_for('edit_profile'))
            try:
                old = str(getattr(current_user, 'avatar_filename', '') or '')
                if old:
                    base = os.path.realpath(app.config['PROFILE_MEDIA_UPLOAD_FOLDER'])
                    path = os.path.realpath(os.path.join(base, old))
                    if path.startswith(base + os.sep) and os.path.exists(path):
                        os.remove(path)
            except Exception:
                pass
            current_user.avatar_filename = unique
        banner_file = request.files.get('banner_file')
        if banner_file and getattr(banner_file, 'filename', None):
            unique, err = _save_uploaded_file(banner_file, app.config['PROFILE_MEDIA_UPLOAD_FOLDER'], _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('Background must be a valid image file.', 'danger')
                return redirect(url_for('edit_profile'))
            try:
                old = str(getattr(current_user, 'banner_filename', '') or '')
                if old:
                    base = os.path.realpath(app.config['PROFILE_MEDIA_UPLOAD_FOLDER'])
                    path = os.path.realpath(os.path.join(base, old))
                    if path.startswith(base + os.sep) and os.path.exists(path):
                        os.remove(path)
            except Exception:
                pass
            current_user.banner_filename = unique
        db.session.commit()
        changed = []
        try:
            if str(getattr(current_user, 'bio', '') or '') != old_bio:
                changed.append('bio')
        except Exception:
            pass
        try:
            if str(getattr(current_user, 'youtube_link', '') or '') != old_youtube:
                changed.append('youtube')
        except Exception:
            pass
        try:
            if str(getattr(current_user, 'avatar_filename', '') or '') != old_avatar:
                changed.append('avatar')
        except Exception:
            pass
        try:
            if str(getattr(current_user, 'banner_filename', '') or '') != old_banner:
                changed.append('background')
        except Exception:
            pass
        suffix = f' Changed: {", ".join(changed)}.' if changed else ''
        add_site_log('profile_edit', f'{current_user.username} edited profile.{suffix}', actor_user_id=current_user.id)
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile'))
    return render_template('edit_profile.html')

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'regen_api_token':
            tok = secrets.token_urlsafe(32)
            try:
                current_user.api_token = tok
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                flash('Could not regenerate API token. Try again.', 'danger')
                return redirect(url_for('settings'))
            add_site_log('settings_edit', f'{current_user.username} regenerated API token.', actor_user_id=current_user.id)
            flash('API token regenerated.', 'success')
            return redirect(url_for('settings'))
        old_ui = str(getattr(current_user, 'ui_version', '') or '')
        old_accent = str(getattr(current_user, 'theme_accent', '') or '')
        old_accent_bg = str(getattr(current_user, 'theme_accent_bg', '') or '')
        ui_version = (request.form.get('ui_version') or '').strip().lower()
        if ui_version not in ('new', 'new2', 'custom'):
            ui_version = 'new'
        current_user.ui_version = ui_version
        accent = (request.form.get('theme_accent') or '').strip()
        if accent and not re.match(r'^#[0-9a-fA-F]{6}$', accent):
            accent = ''
        current_user.theme_accent = accent or None
        accent_bg = (request.form.get('theme_accent_bg') or '').strip()
        if accent_bg and not re.match(r'^#[0-9a-fA-F]{6}$', accent_bg):
            accent_bg = ''
        current_user.theme_accent_bg = accent_bg or None
        if ui_version == 'custom':
            custom_source = (request.form.get('custom_source') or 'user').strip().lower()
            market_slug = (request.form.get('market_slug') or '').strip()
            mode = (request.form.get('mode') or 'easy').strip().lower()
            if custom_source == 'market' and market_slug:
                current_user.ui_template_slug = market_slug
                current_user.ui_custom_mode = None
                current_user.ui_custom_script = None
            else:
                tpl_dir = os.path.join(app.root_path, 'templates', 'ui_users')
                try:
                    os.makedirs(tpl_dir, exist_ok=True)
                except Exception:
                    pass
                current_user.ui_custom_mode = mode
                if mode == 'advanced':
                    text = (request.form.get('template_html') or '').strip()
                    if len(text) < 1 or len(text) > 20000 or ('{{ content_html' not in text):
                        flash('Paste a valid template that includes {{ content_html }}.', 'danger')
                        return redirect(url_for('settings'))
                    current_user.ui_custom_script = text
                else:
                    script = (request.form.get('easy_script') or '').strip()
                    current_user.ui_custom_script = script
                    params = {}
                    for line in script.splitlines():
                        if '=' in line:
                            k, v = line.split('=', 1)
                            params[k.strip().lower()] = v.strip()
                    
                    brand = params.get('brand_text', 'The List')
                    tagline = params.get('tagline_text', '')
                    try:
                        roundness = int(params.get('roundness', '16'))
                    except Exception:
                        roundness = 16
                    roundness = max(0, min(200, roundness))
                    
                    def _is_on(k):
                        val = params.get(k, 'on').lower()
                        return val in ('on', 'true', '1', 'yes')

                    nav_home = _is_on('nav_home')
                    nav_challenge = _is_on('nav_challenge')
                    nav_platformer = _is_on('nav_platformer')
                    nav_unverified = _is_on('nav_unverified')
                    nav_leaderboard = _is_on('nav_leaderboard')
                    nav_submit = _is_on('nav_submit')
                    links = []
                    if nav_home: links.append("<a href=\"{{ url_for('home') }}\" class=\"block nav-link px-3 py-2 rounded-md text-sm\">Main List</a>")
                    if nav_challenge: links.append("<a href=\"{{ url_for('challenge_list') }}\" class=\"block nav-link px-3 py-2 rounded-md text-sm\">Challenge</a>")
                    if nav_platformer: links.append("<a href=\"{{ url_for('platformer_list') }}\" class=\"block nav-link px-3 py-2 rounded-md text-sm\">Platformer</a>")
                    if nav_unverified: links.append("<a href=\"{{ url_for('unverified_list') }}\" class=\"block nav-link px-3 py-2 rounded-md text-sm\">Unverified</a>")
                    if nav_leaderboard: links.append("<a href=\"{{ url_for('leaderboard') }}\" class=\"block nav-link px-3 py-2 rounded-md text-sm\">Leaderboard</a>")
                    submit_btns = """
{% if current_user.is_authenticated %}
    <a href="{{ url_for('submit_record') }}" class="bg-green-600 hover:bg-green-700 text-white px-3 py-2 rounded-md text-sm"><span class="sm:hidden">Record</span><span class="hidden sm:inline">Submit Record</span></a>
    <a href="{{ url_for('submit_level') }}" class="bg-green-600 hover:bg-green-700 text-white px-3 py-2 rounded-md text-sm"><span class="sm:hidden">Level</span><span class="hidden sm:inline">Submit Level</span></a>
{% endif %}
""".strip() if nav_submit else ""
                    color = params.get('color', '').strip()
                    style_rules = [f"border-radius: {roundness}px !important;"]
                    if color and re.match(r'^#[0-9a-fA-F]{3,6}$', color):
                        style_rules.append(f"--accent-color: {color} !important;")
                        style_rules.append(f"color: {color} !important;")
                    
                    style = f"<style>.glass-panel, .rounded, .rounded-lg, .nav-link {{ {' '.join(style_rules)} }}</style>"
                    text = f"""
{style}
<nav class="glass-panel sticky top-0 z-50 shadow-lg">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="flex items-center justify-between h-16">
      <div class="flex items-center gap-2">
        <span class="nav-link px-3 py-2 rounded-md text-sm font-bold text-white">{brand}</span>
        <details class="relative lg:hidden">
          <summary class="list-none cursor-pointer nav-link px-3 py-2 rounded-md text-sm font-medium">Menu</summary>
          <div class="absolute left-0 mt-2 w-64 glass-panel rounded shadow-lg p-2">
            <div class="px-3 py-2 text-xs text-gray-400">Navigate</div>
            {''.join(links)}
          </div>
        </details>
        <div class="hidden lg:flex items-center gap-2">
          {''.join(links)}
        </div>
      </div>
      <div class="flex items-center gap-2">
        {submit_btns}
      </div>
    </div>
  </div>
</nav>
<main class="flex-grow container mx-auto px-4 sm:px-6 lg:px-8 py-8">
  {"<div class='text-xs text-gray-400 mb-2'>" + tagline + "</div>" if tagline else ""}
  {{ messages_html|safe }}
  {{ content_html|safe }}
</main>
""".strip()
                try:
                    fname = f"{current_user.id}.html"
                    with open(os.path.join(tpl_dir, fname), 'w', encoding='utf-8') as f:
                        f.write(text)
                    current_user.ui_template_slug = f"user/{current_user.id}"
                except Exception:
                    flash('Could not save your custom UI.', 'danger')
                    return redirect(url_for('settings'))
        db.session.commit()
        changed = []
        try:
            if str(getattr(current_user, 'ui_version', '') or '') != old_ui:
                changed.append('ui')
        except Exception:
            pass
        try:
            if str(getattr(current_user, 'theme_accent', '') or '') != old_accent:
                changed.append('theme')
        except Exception:
            pass
        try:
            if str(getattr(current_user, 'theme_accent_bg', '') or '') != old_accent_bg:
                changed.append('background')
        except Exception:
            pass
        suffix = f' Changed: {", ".join(changed)}.' if changed else ''
        add_site_log('settings_edit', f'{current_user.username} edited settings.{suffix}', actor_user_id=current_user.id)
        flash('Settings saved.', 'success')
        resp = redirect(url_for('settings'))
        try:
            want = bool(request.form.get('disable_thumbnails'))
            resp.set_cookie('disable_thumbnails', '1' if want else '0', max_age=365 * 24 * 60 * 60, samesite='Lax')
        except Exception:
            pass
        return resp
    market_templates = []
    market_preview = ''
    user_preview = ''
    try:
        d = os.path.join(app.root_path, 'templates', 'ui_marketplace')
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith('.html'):
                    market_templates.append(name[:-5])
        slug = str(getattr(current_user, 'ui_template_slug', '') or '')
        if slug and not slug.startswith('user/'):
            p = os.path.join(d, f'{slug}.html')
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    market_preview = f.read()
    except Exception:
        market_templates = market_templates or []
    try:
        ud = os.path.join(app.root_path, 'templates', 'ui_users')
        p = os.path.join(ud, f'{current_user.id}.html')
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                user_preview = f.read()
    except Exception:
        user_preview = ''
    
    # Populate easy_script and template_html for the form
    easy_script = ''
    template_html = ''
    if current_user.ui_custom_mode == 'easy':
        easy_script = current_user.ui_custom_script or ''
    elif current_user.ui_custom_mode == 'advanced':
        template_html = current_user.ui_custom_script or ''
    
    return render_template('settings.html', 
                         market_templates=market_templates, 
                         market_preview=market_preview, 
                         user_preview=user_preview,
                         easy_script=easy_script,
                         template_html=template_html)

@app.route('/clans', methods=['GET', 'POST'])
@login_required
def clans():
    if request.method == 'POST':
        if current_user.clan_id:
            flash('Leave your current clan before creating a new one.', 'danger')
            return redirect(url_for('clans'))
        name = (request.form.get('name') or '').strip()[:80]
        desc = (request.form.get('description') or '').strip()[:500] or None
        if not name:
            flash('Clan name is required.', 'danger')
            return redirect(url_for('clans'))
        if 'raid' in name.lower():
            flash('Clan name cannot include "raid".', 'danger')
            return redirect(url_for('clans'))
        existing = Clan.query.filter(db.func.lower(Clan.name) == name.lower()).first()
        if existing:
            flash('A clan with that name already exists.', 'danger')
            return redirect(url_for('clans'))
        clan = Clan(name=name, description=desc, owner_user_id=int(current_user.id))
        db.session.add(clan)
        db.session.flush()
        current_user.clan_id = int(clan.id)
        db.session.commit()
        add_site_log('clan_create', f'{current_user.username} created clan "{clan.name}".', actor_user_id=current_user.id)
        flash('Clan created.', 'success')
        return redirect(url_for('clan_detail', clan_id=clan.id))
    clans = Clan.query.filter(~db.func.lower(Clan.name).like('%raid%')).order_by(Clan.created_at.desc()).all()
    return render_template('clans.html', clans=clans)

@app.route('/clan/<int:clan_id>', methods=['GET', 'POST'])
@login_required
def clan_detail(clan_id):
    clan = Clan.query.get_or_404(clan_id)
    if 'raid' in str(clan.name or '').lower():
        abort(404)
    is_owner = int(getattr(clan, 'owner_user_id', 0) or 0) == int(current_user.id)
    is_member = int(getattr(current_user, 'clan_id', 0) or 0) == int(clan.id)
    pending_req = None
    try:
        pending_req = ClanJoinRequest.query.filter_by(clan_id=int(clan.id), user_id=int(current_user.id)).first()
    except Exception:
        pending_req = None
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'request_join':
            if current_user.clan_id:
                flash('Leave your current clan before joining another.', 'danger')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            if pending_req:
                flash('Join request already pending.', 'warning')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            jr = ClanJoinRequest(clan_id=int(clan.id), user_id=int(current_user.id))
            db.session.add(jr)
            db.session.commit()
            add_site_log('clan_join_request', f'{current_user.username} requested to join "{clan.name}".', actor_user_id=current_user.id)
            flash('Join request sent.', 'success')
            return redirect(url_for('clan_detail', clan_id=clan.id))
        if action == 'cancel_request':
            if pending_req:
                db.session.delete(pending_req)
                db.session.commit()
                flash('Join request cancelled.', 'success')
            return redirect(url_for('clan_detail', clan_id=clan.id))
        if action == 'leave':
            if not is_member:
                return redirect(url_for('clan_detail', clan_id=clan.id))
            if is_owner:
                flash('Transfer ownership or disband the clan before leaving.', 'danger')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            current_user.clan_id = None
            db.session.commit()
            add_site_log('clan_leave', f'{current_user.username} left "{clan.name}".', actor_user_id=current_user.id)
            flash('Left clan.', 'success')
            return redirect(url_for('clans'))
        if not is_owner:
            abort(403)
        if action == 'edit_description':
            desc = (request.form.get('description') or '').strip()[:500] or None
            clan.description = desc
            db.session.commit()
            flash('Clan updated.', 'success')
            return redirect(url_for('clan_detail', clan_id=clan.id))
        if action == 'accept_request':
            try:
                req_id = int(request.form.get('request_id') or 0)
            except Exception:
                req_id = 0
            jr = ClanJoinRequest.query.get(req_id) if req_id else None
            if not jr or int(jr.clan_id) != int(clan.id):
                flash('Request not found.', 'danger')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            u = db.session.get(User, int(jr.user_id))
            if not u:
                db.session.delete(jr)
                db.session.commit()
                flash('User not found.', 'danger')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            if u.clan_id:
                db.session.delete(jr)
                db.session.commit()
                flash('User is already in a clan.', 'danger')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            u.clan_id = int(clan.id)
            db.session.query(ClanJoinRequest).filter(ClanJoinRequest.user_id == int(u.id)).delete()
            db.session.commit()
            add_site_log('clan_join_accept', f'{current_user.username} accepted {u.username} into "{clan.name}".', actor_user_id=current_user.id)
            flash('Member added.', 'success')
            return redirect(url_for('clan_detail', clan_id=clan.id))
        if action == 'deny_request':
            try:
                req_id = int(request.form.get('request_id') or 0)
            except Exception:
                req_id = 0
            jr = ClanJoinRequest.query.get(req_id) if req_id else None
            if jr and int(jr.clan_id) == int(clan.id):
                db.session.delete(jr)
                db.session.commit()
                flash('Request denied.', 'success')
            return redirect(url_for('clan_detail', clan_id=clan.id))
        if action == 'kick_member':
            try:
                user_id = int(request.form.get('user_id') or 0)
            except Exception:
                user_id = 0
            if not user_id:
                return redirect(url_for('clan_detail', clan_id=clan.id))
            if int(user_id) == int(clan.owner_user_id or 0):
                flash('Cannot kick the owner.', 'danger')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            u = db.session.get(User, int(user_id))
            if u and int(getattr(u, 'clan_id', 0) or 0) == int(clan.id):
                u.clan_id = None
                db.session.commit()
                add_site_log('clan_kick', f'{current_user.username} kicked {u.username} from "{clan.name}".', actor_user_id=current_user.id)
                flash('Member removed.', 'success')
            return redirect(url_for('clan_detail', clan_id=clan.id))
        if action == 'transfer_owner':
            try:
                user_id = int(request.form.get('user_id') or 0)
            except Exception:
                user_id = 0
            u = db.session.get(User, int(user_id)) if user_id else None
            if not u or int(getattr(u, 'clan_id', 0) or 0) != int(clan.id):
                flash('User must be a clan member.', 'danger')
                return redirect(url_for('clan_detail', clan_id=clan.id))
            clan.owner_user_id = int(u.id)
            db.session.commit()
            add_site_log('clan_transfer', f'{current_user.username} transferred "{clan.name}" to {u.username}.', actor_user_id=current_user.id)
            flash('Ownership transferred.', 'success')
            return redirect(url_for('clan_detail', clan_id=clan.id))
        if action == 'disband':
            db.session.query(User).filter(User.clan_id == int(clan.id)).update({User.clan_id: None})
            db.session.query(ClanJoinRequest).filter(ClanJoinRequest.clan_id == int(clan.id)).delete()
            db.session.delete(clan)
            db.session.commit()
            add_site_log('clan_disband', f'{current_user.username} disbanded a clan.', actor_user_id=current_user.id)
            flash('Clan disbanded.', 'success')
            return redirect(url_for('clans'))
        return redirect(url_for('clan_detail', clan_id=clan.id))

    members = User.query.filter_by(clan_id=int(clan.id)).order_by(User.username.asc()).all()
    join_requests = []
    if is_owner:
        join_requests = ClanJoinRequest.query.filter_by(clan_id=int(clan.id)).order_by(ClanJoinRequest.created_at.asc()).all()
    return render_template('clan_detail.html', clan=clan, is_owner=is_owner, is_member=is_member, pending_req=pending_req, members=members, join_requests=join_requests)

import random

@app.route('/forgot-password')
def forgot_password():
    return render_template('forgot_password.html')

@app.route('/icon.png')
def icon_png():
    return send_from_directory(os.path.join(app.root_path, 'templates'), 'icon.png')

@app.route('/tick.mp3')
def tick_mp3():
    return send_from_directory(os.path.join(app.root_path, 'templates'), 'tick.mp3')

@app.route('/songs/<filename>')
def serve_song(filename):
    name = secure_filename(filename)
    if not name or name != filename or '/' in filename or '\\' in filename:
        raise NotFound()
    base = os.path.realpath(app.config['SONG_UPLOAD_FOLDER'])
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise NotFound()
    return send_from_directory(base, name)

@app.route('/pending-songs/<filename>')
def serve_pending_song(filename):
    name = secure_filename(filename)
    if not name or name != filename or '/' in filename or '\\' in filename:
        raise NotFound()
    pending_folder = os.path.join(app.root_path, 'static', 'pending_songs')
    base = os.path.realpath(pending_folder)
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise NotFound()
    return send_from_directory(base, name)

@app.route('/roulette')
def roulette():
    pool = session.get('roulette_pool')
    if not pool:
        session['roulette_pool'] = [lv.id for lv in Level.query.filter_by(is_verified=True).all()]
        pool = session['roulette_pool']
    if not pool:
        flash('No levels available for roulette!', 'warning')
        return redirect(url_for('home'))
    auth_user_id = None
    auth_username = ''
    try:
        auth_user_id = int(getattr(current_user, 'id', 0) or 0) or None
        auth_username = str(getattr(current_user, 'username', '') or '')
    except Exception:
        auth_user_id = None
        auth_username = ''
    return render_template('roulette.html', auth_user_id=auth_user_id, auth_username=auth_username)

def _init_roulette_pool():
    ids = [lv.id for lv in Level.query.filter_by(is_verified=True).all()]
    session['roulette_pool'] = ids
    return ids

ROULETTE_ROOMS = {}
ROULETTE_ROOMS_LOCK = threading.Lock()

ROULETTE_TAGS = [
    '2.1',
    '2.2',
    'Medium',
    'Long',
    'XL',
    'XXL+',
    'Ship',
    'Cube',
    'Wave',
    'Robot',
    'UFO',
    'Spider',
    'Swing Copter',
    'Dual',
    'Timings',
    'Memory',
    'Nerve Control',
    'Slow Paced',
    'Fast Paced',
    'High CPS',
    'NONG',
    'Chokepoints',
    'buffer click abuse',
    'buffer click',
    'ball',
    'spam',
    'unballanced'
]

def _roulette_parse_tags(raw):
    if not raw:
        return []
    out = []
    for t in raw:
        try:
            s = str(t).strip()
        except Exception:
            continue
        if not s:
            continue
        out.append(s)
    allowed = {c.lower(): c for c in ROULETTE_TAGS}
    norm = []
    seen = set()
    for t in out:
        key = t.lower()
        if key in allowed and key not in seen:
            norm.append(allowed[key])
            seen.add(key)
    return norm

def _roulette_level_tag_set(level):
    src = (level.categories or '')
    parts = []
    try:
        parts = src.split(',')
    except Exception:
        parts = []
    tags = set()
    for p in parts:
        s = (p or '').strip()
        if not s:
            continue
        tags.add(s.lower())
    try:
        if getattr(level, 'uses_nong', False):
            tags.add('nong')
    except Exception:
        pass
    return tags

def _roulette_ids_for_room(category, verified_mode, tags, top_n=None):
    category = (category or 'all').lower()
    if category not in ('all', 'main', 'challenge'):
        category = 'all'
    verified_mode = (verified_mode or 'verified').lower()
    if verified_mode not in ('verified', 'unverified', 'both'):
        verified_mode = 'verified'
    try:
        top_n = int(top_n or 0)
    except Exception:
        top_n = 0
    if top_n < 0:
        top_n = 0
    q = Level.query
    if category != 'all':
        q = q.filter_by(list_type=category)
    if verified_mode == 'verified':
        q = q.filter_by(is_verified=True)
    elif verified_mode == 'unverified':
        q = q.filter_by(is_verified=False)
    if top_n > 0:
        q = q.filter(Level.position.isnot(None), Level.position <= top_n)
    levels = q.all()
    norm_tags = _roulette_parse_tags(tags)
    if norm_tags:
        want = {t.lower() for t in norm_tags}
        levels = [lv for lv in levels if _roulette_level_tag_set(lv) & want]
    return [lv.id for lv in levels]

def _roulette_single_settings_key(category, verified_mode, tags, top_n):
    category = (category or 'all').lower()
    verified_mode = (verified_mode or 'verified').lower()
    try:
        top_n = int(top_n or 0)
    except Exception:
        top_n = 0
    norm_tags = _roulette_parse_tags(tags)
    tag_key = ','.join(sorted([t.lower() for t in norm_tags]))
    return f'{category}|{verified_mode}|{top_n}|{tag_key}'

def _roulette_touch_player(room, player_id, player_name, now_ts):
    pid = (player_id or '').strip()[:64]
    if not pid:
        pid = uuid.uuid4().hex
    name = (player_name or '').strip()[:32] or 'Player'
    players = list(room.get('players') or [])
    found = False
    for p in players:
        if (p.get('id') or '') == pid:
            p['name'] = name
            p['last_seen'] = now_ts
            found = True
            break
    if not found:
        players.append({'id': pid, 'name': name, 'last_seen': now_ts})
    cutoff = now_ts - (30 * 60)
    players = [p for p in players if float(p.get('last_seen') or 0) >= cutoff]
    if len(players) > 64:
        players = players[-64:]
    room['players'] = players
    return pid

def _roulette_request_player(payload_player_id, payload_player_name):
    try:
        if getattr(current_user, 'is_authenticated', False):
            pid = f"user:{int(getattr(current_user, 'id', 0) or 0)}"
            name = str(getattr(current_user, 'username', '') or '')
            return (pid[:64], name[:32])
    except Exception:
        pass
    pid = (payload_player_id or '').strip()[:64]
    name = (payload_player_name or '').strip()[:32]
    return (pid, name)

def _roulette_clean_kicked(room, now_ts):
    kicked = dict(room.get('kicked') or {})
    if not kicked:
        room['kicked'] = {}
        return {}
    out = {}
    for pid, until in kicked.items():
        try:
            exp = float(until or 0)
        except Exception:
            exp = 0
        if exp > now_ts:
            out[str(pid)] = exp
    room['kicked'] = out
    return out

def _roulette_is_kicked(room, player_id, now_ts):
    pid = (player_id or '').strip()[:64]
    if not pid:
        return False
    kicked = _roulette_clean_kicked(room, now_ts)
    return pid in kicked

def _roulette_public_players(room):
    owner_pid = room.get('owner_player_id') or ''
    out = []
    for p in (room.get('players') or []):
        pid = p.get('id') or ''
        out.append({'id': pid, 'name': p.get('name') or 'Player', 'is_owner': pid == owner_pid})
    return out

def _roulette_ids_for_category(category):
    if category and category != 'all':
        return [lv.id for lv in Level.query.filter_by(is_verified=True, list_type=category).all()]
    return [lv.id for lv in Level.query.filter_by(is_verified=True).all()]

def _roulette_levels_for_ids(ids):
    if not ids:
        return []
    levels = Level.query.filter(Level.id.in_(ids)).all()
    by_id = {lv.id: lv for lv in levels}
    return [by_id[i] for i in ids if i in by_id]

def _roulette_room_cleanup(now_ts):
    cutoff = now_ts - (6 * 60 * 60)
    to_delete = [code for code, room in ROULETTE_ROOMS.items() if room.get('updated_at', 0) < cutoff]
    for code in to_delete:
        del ROULETTE_ROOMS[code]

def _roulette_new_room_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(secrets.choice(alphabet) for _ in range(6))
        if code not in ROULETTE_ROOMS:
            return code

@app.route('/api/roulette/pool', methods=['GET'])
def roulette_pool():
    category = request.args.get('category', 'all')
    verified_mode = (request.args.get('verified_mode') or 'verified').lower()
    top_n = request.args.get('top_n')
    no_repeat = str(request.args.get('no_repeat') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    tags_raw = (request.args.get('tags') or '').strip()
    tags = [t for t in tags_raw.split(',') if (t or '').strip()] if tags_raw else []
    try:
        top_n_val = int(top_n or 0)
    except Exception:
        top_n_val = 0
    key = _roulette_single_settings_key(category, verified_mode, tags, top_n_val)
    data = session.get('roulette_pool_v2') or {}
    pool_key = data.get('key') or ''
    pool_present = 'pool' in data
    pool = list(data.get('pool') or [])
    if pool_key != key or not pool_present or (not pool and not no_repeat):
        pool = _roulette_ids_for_room(category, verified_mode, tags, top_n_val)
        session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    levels = _roulette_levels_for_ids(pool)
    out = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]
    return jsonify({'levels': out})

@app.route('/api/roulette/spin', methods=['POST'])
def roulette_spin():
    category = request.args.get('category', 'all')
    verified_mode = (request.args.get('verified_mode') or 'verified').lower()
    top_n = request.args.get('top_n')
    no_repeat = str(request.args.get('no_repeat') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    tags_raw = (request.args.get('tags') or '').strip()
    tags = [t for t in tags_raw.split(',') if (t or '').strip()] if tags_raw else []
    try:
        top_n_val = int(top_n or 0)
    except Exception:
        top_n_val = 0
    key = _roulette_single_settings_key(category, verified_mode, tags, top_n_val)
    data = session.get('roulette_pool_v2') or {}
    pool_key = data.get('key') or ''
    pool_present = 'pool' in data
    pool = list(data.get('pool') or [])
    if pool_key != key or not pool_present:
        pool = _roulette_ids_for_room(category, verified_mode, tags, top_n_val)
        session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    elif not pool and not no_repeat:
        pool = _roulette_ids_for_room(category, verified_mode, tags, top_n_val)
        session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    if not pool:
        return jsonify({'error': 'empty_pool'}), 400
    chosen_id = random.choice(pool)
    lv = db.session.get(Level, chosen_id)
    if not lv:
        session['roulette_pool_v2'] = {'key': key, 'pool': [i for i in pool if i != chosen_id]}
        return jsonify({'error': 'invalid_level'}), 404
    return jsonify({
        'level': {
            'id': lv.id,
            'gd_level_id': lv.gd_level_id,
            'name': lv.name,
            'creator': lv.creator,
            'video_url': lv.video_url,
            'position': lv.position
        },
        'pool_size': len(pool)
    })

@app.route('/api/roulette/skip', methods=['POST'])
def roulette_skip():
    try:
        payload = request.get_json(silent=True) or {}
        level_id = int(payload.get('level_id'))
    except Exception:
        return jsonify({'error': 'bad_request'}), 400
    category = request.args.get('category', 'all')
    verified_mode = (request.args.get('verified_mode') or 'verified').lower()
    top_n = request.args.get('top_n')
    no_refill = str(request.args.get('no_refill') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    tags_raw = (request.args.get('tags') or '').strip()
    tags = [t for t in tags_raw.split(',') if (t or '').strip()] if tags_raw else []
    try:
        top_n_val = int(top_n or 0)
    except Exception:
        top_n_val = 0
    key = _roulette_single_settings_key(category, verified_mode, tags, top_n_val)
    data = session.get('roulette_pool_v2') or {}
    pool_key = data.get('key') or ''
    pool = list(data.get('pool') or [])
    if pool_key != key or not pool:
        pool = _roulette_ids_for_room(category, verified_mode, tags, top_n_val)
    if level_id in pool:
        pool = [i for i in pool if i != level_id]
        session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    if not pool:
        if no_refill:
            session['roulette_pool_v2'] = {'key': key, 'pool': pool}
        else:
            pool = _roulette_ids_for_room(category, verified_mode, tags, top_n_val)
            session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    return jsonify({'pool_size': len(pool)})

@app.route('/api/roulette/reset', methods=['POST'])
def roulette_reset():
    try:
        session.pop('roulette_pool_v2', None)
    except Exception:
        pass
    return jsonify({'ok': True})

@app.route('/api/roulette/room/create', methods=['POST'])
def roulette_room_create():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    payload = request.get_json(silent=True) or {}
    category = (payload.get('category') or 'all').lower()
    if category not in ('all', 'main', 'challenge'):
        category = 'all'
    verified_mode = (payload.get('verified_mode') or 'verified').lower()
    tags = _roulette_parse_tags(payload.get('tags') or [])
    is_public = bool(payload.get('is_public') or False)
    try:
        top_n = int(payload.get('top_n') or 0)
    except Exception:
        top_n = 0
    if top_n < 0:
        top_n = 0
    ids = _roulette_ids_for_room(category, verified_mode, tags, top_n)
    if not ids:
        return jsonify({'error': 'empty_pool'}), 400
    now_ts = time.time()
    owner_token = uuid.uuid4().hex
    player_id, player_name = _roulette_request_player(payload.get('player_id'), payload.get('player_name'))
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        owner_pid_raw = (player_id or '').strip()[:64]
        for existing_code, existing_room in ROULETTE_ROOMS.items():
            try:
                existing_owner = (existing_room.get('owner_player_id') or '').strip()[:64]
                is_closed = bool(existing_room.get('closed') or False)
            except Exception:
                existing_owner = ''
                is_closed = False
            if existing_owner and existing_owner == owner_pid_raw and not is_closed:
                return jsonify({'error': 'room_limit'}), 400
        code = _roulette_new_room_code()
        room = {
            'pool': ids,
            'category': category,
            'verified_mode': verified_mode,
            'tags': tags,
            'top_n': top_n,
            'is_public': is_public,
            'closed': False,
            'last_level_id': None,
            'updated_at': now_ts,
            'created_at': now_ts,
            'owner_token': owner_token,
            'owner_player_id': '',
            'max_rounds': 15,
            'max_attempts': 10,
            'current_round': 0,
            'require_proof': False,
            'no_repeat': False,
            'results': []
        }
        owner_pid = _roulette_touch_player(room, player_id, player_name, now_ts)
        room['owner_player_id'] = owner_pid
        room['kicked'] = {}
        ROULETTE_ROOMS[code] = room
        players = _roulette_public_players(room)
    levels = _roulette_levels_for_ids(ids)
    data = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]
    return jsonify({'code': code, 'category': category, 'verified_mode': verified_mode, 'tags': tags, 'top_n': top_n, 'is_public': is_public, 'closed': False, 'levels': data, 'pool_size': len(ids), 'owner_token': owner_token, 'max_rounds': 15, 'max_attempts': 10, 'current_round': 0, 'require_proof': False, 'no_repeat': False, 'results': [], 'players': players, 'owner_player_id': owner_pid})

@app.route('/api/roulette/room/join', methods=['POST'])
def roulette_room_join():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _roulette_request_player(payload.get('player_id'), payload.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if bool(room.get('closed') or False):
            player_ids = {(p.get('id') or '') for p in (room.get('players') or [])}
            if player_id and player_id in player_ids:
                pass
            else:
                is_staff = False
                try:
                    is_staff = bool(getattr(current_user, 'is_authenticated', False)) and _get_user_rank(current_user) in ('mod', 'elder_moderator', 'admin')
                except Exception:
                    is_staff = False
                if not is_staff:
                    return jsonify({'error': 'closed'}), 403
        if player_id and _roulette_is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        _roulette_touch_player(room, player_id, player_name, now_ts)
        room['updated_at'] = now_ts
        ids = list(room.get('pool') or [])
        category = room.get('category') or 'all'
        verified_mode = room.get('verified_mode') or 'verified'
        tags = list(room.get('tags') or [])
        top_n = int(room.get('top_n') or 0)
        is_public = bool(room.get('is_public') or False)
        closed = bool(room.get('closed') or False)
        last_level_id = room.get('last_level_id')
        max_rounds = int(room.get('max_rounds') or 15)
        max_attempts = int(room.get('max_attempts') or 10)
        current_round = int(room.get('current_round') or 0)
        require_proof = bool(room.get('require_proof') or False)
        no_repeat = bool(room.get('no_repeat') or False)
        results = list(room.get('results') or [])[-200:]
        players = _roulette_public_players(room)
        owner_player_id = room.get('owner_player_id') or ''
    levels = _roulette_levels_for_ids(ids)
    data = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]
    last_level = db.session.get(Level, last_level_id) if last_level_id else None
    last_payload = None
    if last_level:
        last_payload = {'id': last_level.id, 'gd_level_id': last_level.gd_level_id, 'name': last_level.name, 'creator': last_level.creator, 'position': last_level.position}
    return jsonify({'code': code, 'category': category, 'verified_mode': verified_mode, 'tags': tags, 'top_n': top_n, 'is_public': is_public, 'closed': closed, 'levels': data, 'pool_size': len(ids), 'last_level': last_payload, 'max_rounds': max_rounds, 'max_attempts': max_attempts, 'current_round': current_round, 'require_proof': require_proof, 'no_repeat': no_repeat, 'results': results, 'players': players, 'owner_player_id': owner_player_id})

@app.route('/api/roulette/room/state', methods=['GET'])
def roulette_room_state():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _roulette_request_player(request.args.get('player_id'), request.args.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if player_id and _roulette_is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _roulette_touch_player(room, player_id, player_name, now_ts)
        room['updated_at'] = now_ts
        ids = list(room.get('pool') or [])
        category = room.get('category') or 'all'
        verified_mode = room.get('verified_mode') or 'verified'
        tags = list(room.get('tags') or [])
        top_n = int(room.get('top_n') or 0)
        is_public = bool(room.get('is_public') or False)
        closed = bool(room.get('closed') or False)
        last_level_id = room.get('last_level_id')
        max_rounds = int(room.get('max_rounds') or 15)
        max_attempts = int(room.get('max_attempts') or 10)
        current_round = int(room.get('current_round') or 0)
        require_proof = bool(room.get('require_proof') or False)
        no_repeat = bool(room.get('no_repeat') or False)
        results = list(room.get('results') or [])[-200:]
        players = _roulette_public_players(room)
        owner_player_id = room.get('owner_player_id') or ''
    levels = _roulette_levels_for_ids(ids)
    data = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]
    last_level = db.session.get(Level, last_level_id) if last_level_id else None
    last_payload = None
    if last_level:
        last_payload = {'id': last_level.id, 'gd_level_id': last_level.gd_level_id, 'name': last_level.name, 'creator': last_level.creator, 'position': last_level.position}
    return jsonify({'code': code, 'category': category, 'verified_mode': verified_mode, 'tags': tags, 'top_n': top_n, 'is_public': is_public, 'closed': closed, 'levels': data, 'pool_size': len(ids), 'last_level': last_payload, 'max_rounds': max_rounds, 'max_attempts': max_attempts, 'current_round': current_round, 'require_proof': require_proof, 'no_repeat': no_repeat, 'results': results, 'players': players, 'owner_player_id': owner_player_id})

@app.route('/api/roulette/room/spin', methods=['POST'])
def roulette_room_spin():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _roulette_request_player(request.args.get('player_id'), request.args.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        is_staff = False
        try:
            is_staff = bool(getattr(current_user, 'is_authenticated', False)) and _get_user_rank(current_user) in ('mod', 'elder_moderator', 'admin')
        except Exception:
            is_staff = False
        owner_pid = (room.get('owner_player_id') or '').strip()
        if not is_staff and (not player_id or (player_id or '').strip() != owner_pid):
            return jsonify({'error': 'forbidden'}), 403
        if player_id and _roulette_is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _roulette_touch_player(room, player_id, player_name, now_ts)
        pool = list(room.get('pool') or [])
        category = room.get('category') or 'all'
        verified_mode = room.get('verified_mode') or 'verified'
        tags = list(room.get('tags') or [])
        top_n = int(room.get('top_n') or 0)
        max_rounds = int(room.get('max_rounds') or 15)
        max_attempts = int(room.get('max_attempts') or 10)
        current_round = int(room.get('current_round') or 0)
        require_proof = bool(room.get('require_proof') or False)
        no_repeat = bool(room.get('no_repeat') or False)
        if current_round > 0 and room.get('last_level_id'):
            round_results = list(room.get('results') or [])
            if not any(int((r or {}).get('round_no') or 0) == current_round for r in round_results):
                return jsonify({'error': 'round_incomplete'}), 400
        if current_round >= max_rounds:
            return jsonify({'error': 'finished'}), 400
        if not pool:
            if no_repeat:
                return jsonify({'error': 'empty_pool'}), 400
            pool = _roulette_ids_for_room(category, verified_mode, tags, top_n)
            room['pool'] = pool
        if not pool:
            return jsonify({'error': 'empty_pool'}), 400
        chosen_id = random.choice(pool)
        room['last_level_id'] = chosen_id
        room['current_round'] = current_round + 1
        room['updated_at'] = now_ts
    lv = db.session.get(Level, chosen_id)
    if not lv:
        return jsonify({'error': 'invalid_level'}), 404
    return jsonify({'code': code, 'level': {'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position}, 'pool_size': len(pool), 'current_round': current_round + 1, 'max_rounds': max_rounds, 'max_attempts': max_attempts, 'require_proof': require_proof})

@app.route('/api/roulette/room/skip', methods=['POST'])
def roulette_room_skip():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    payload = request.get_json(silent=True) or {}
    no_refill = str(request.args.get('no_refill') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    keep_round = str(request.args.get('keep_round') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    player_id, player_name = _roulette_request_player(request.args.get('player_id'), request.args.get('player_name'))
    try:
        level_id = int(payload.get('level_id'))
    except Exception:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        is_staff = False
        try:
            is_staff = bool(getattr(current_user, 'is_authenticated', False)) and _get_user_rank(current_user) in ('mod', 'elder_moderator', 'admin')
        except Exception:
            is_staff = False
        owner_pid = (room.get('owner_player_id') or '').strip()
        if not is_staff and (not player_id or (player_id or '').strip() != owner_pid):
            return jsonify({'error': 'forbidden'}), 403
        if player_id and _roulette_is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _roulette_touch_player(room, player_id, player_name, now_ts)
        pool = list(room.get('pool') or [])
        if level_id in pool:
            pool = [i for i in pool if i != level_id]
        category = room.get('category') or 'all'
        verified_mode = room.get('verified_mode') or 'verified'
        tags = list(room.get('tags') or [])
        top_n = int(room.get('top_n') or 0)
        if not pool and not no_refill:
            pool = _roulette_ids_for_room(category, verified_mode, tags, top_n)
        room['pool'] = pool
        if not keep_round and room.get('last_level_id') == level_id:
            old_round = int(room.get('current_round') or 0)
            room['last_level_id'] = None
            room['current_round'] = max(0, old_round - 1)
            if old_round > 0:
                rows = list(room.get('results') or [])
                room['results'] = [r for r in rows if int((r or {}).get('round_no') or 0) != old_round]
        room['updated_at'] = now_ts
    return jsonify({'code': code, 'pool_size': len(pool)})

@app.route('/api/roulette/room/config', methods=['POST'])
def roulette_room_config():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    owner_token = (payload.get('owner_token') or '').strip()
    if not code or not owner_token:
        return jsonify({'error': 'bad_request'}), 400
    try:
        max_rounds = int(payload.get('max_rounds'))
    except Exception:
        max_rounds = None
    try:
        max_attempts = int(payload.get('max_attempts'))
    except Exception:
        max_attempts = None
    require_proof = bool(payload.get('require_proof')) if 'require_proof' in payload else None
    no_repeat = bool(payload.get('no_repeat')) if 'no_repeat' in payload else None
    category = (payload.get('category') or '').lower().strip() if 'category' in payload else None
    verified_mode = (payload.get('verified_mode') or '').lower().strip() if 'verified_mode' in payload else None
    is_public = bool(payload.get('is_public') or False) if 'is_public' in payload else None
    tags = _roulette_parse_tags(payload.get('tags') or []) if 'tags' in payload else None
    try:
        top_n = int(payload.get('top_n')) if 'top_n' in payload else None
    except Exception:
        top_n = None
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if (room.get('owner_token') or '') != owner_token:
            return jsonify({'error': 'forbidden'}), 403
        if max_rounds is not None:
            room['max_rounds'] = max(1, min(200, max_rounds))
            if int(room.get('current_round') or 0) > int(room.get('max_rounds') or 15):
                room['current_round'] = int(room.get('max_rounds') or 15)
        if max_attempts is not None:
            room['max_attempts'] = max(1, min(50, max_attempts))
        if require_proof is not None:
            room['require_proof'] = bool(require_proof)
        if no_repeat is not None:
            room['no_repeat'] = bool(no_repeat)
        changed_pool = False
        if category is not None:
            if category not in ('all', 'main', 'challenge'):
                category = 'all'
            if (room.get('category') or 'all') != category:
                room['category'] = category
                changed_pool = True
        if verified_mode is not None:
            if verified_mode not in ('verified', 'unverified', 'both'):
                verified_mode = 'verified'
            if (room.get('verified_mode') or 'verified') != verified_mode:
                room['verified_mode'] = verified_mode
                changed_pool = True
        if is_public is not None:
            if bool(room.get('closed') or False):
                room['is_public'] = False
            else:
                room['is_public'] = bool(is_public)
        if tags is not None:
            if list(room.get('tags') or []) != tags:
                room['tags'] = tags
                changed_pool = True
        if top_n is not None:
            if top_n < 0:
                top_n = 0
            if int(room.get('top_n') or 0) != int(top_n or 0):
                room['top_n'] = int(top_n or 0)
                changed_pool = True
        if changed_pool:
            ids = _roulette_ids_for_room(room.get('category') or 'all', room.get('verified_mode') or 'verified', list(room.get('tags') or []), int(room.get('top_n') or 0))
            room['pool'] = ids
            if room.get('last_level_id') and room.get('last_level_id') not in set(ids):
                room['last_level_id'] = None
        room['updated_at'] = now_ts
        resp = {
            'code': code,
            'category': room.get('category') or 'all',
            'verified_mode': room.get('verified_mode') or 'verified',
            'tags': list(room.get('tags') or []),
            'top_n': int(room.get('top_n') or 0),
            'is_public': bool(room.get('is_public') or False),
            'closed': bool(room.get('closed') or False),
            'max_rounds': int(room.get('max_rounds') or 15),
            'max_attempts': int(room.get('max_attempts') or 10),
            'current_round': int(room.get('current_round') or 0),
            'require_proof': bool(room.get('require_proof') or False),
            'no_repeat': bool(room.get('no_repeat') or False)
        }
    return jsonify(resp)

@app.route('/api/roulette/room/reset', methods=['POST'])
def roulette_room_reset():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _roulette_request_player(request.args.get('player_id'), request.args.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        is_staff = False
        try:
            is_staff = bool(getattr(current_user, 'is_authenticated', False)) and _get_user_rank(current_user) in ('mod', 'elder_moderator', 'admin')
        except Exception:
            is_staff = False
        owner_pid = (room.get('owner_player_id') or '').strip()
        if not is_staff and (not player_id or (player_id or '').strip() != owner_pid):
            return jsonify({'error': 'forbidden'}), 403
        if player_id and _roulette_is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _roulette_touch_player(room, player_id, player_name, now_ts)
        category = room.get('category') or 'all'
        verified_mode = room.get('verified_mode') or 'verified'
        tags = list(room.get('tags') or [])
        top_n = int(room.get('top_n') or 0)
        ids = _roulette_ids_for_room(category, verified_mode, tags, top_n)
        room['pool'] = ids
        room['updated_at'] = now_ts
    return jsonify({'code': code, 'pool_size': len(ids)})

@app.route('/api/roulette/room/close', methods=['POST'])
def roulette_room_close():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    owner_token = (payload.get('owner_token') or '').strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        allowed = False
        if owner_token and owner_token == (room.get('owner_token') or ''):
            allowed = True
        else:
            is_staff = False
            try:
                is_staff = bool(getattr(current_user, 'is_authenticated', False)) and _get_user_rank(current_user) in ('mod', 'elder_moderator', 'admin')
            except Exception:
                is_staff = False
            allowed = is_staff
        if not allowed:
            return jsonify({'error': 'forbidden'}), 403
        room['closed'] = True
        room['is_public'] = False
        room['updated_at'] = now_ts
        resp = {'code': code, 'closed': True, 'is_public': False}
    return jsonify(resp)

@app.route('/api/roulette/room/leave', methods=['POST'])
def roulette_room_leave():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    player_id, _ = _roulette_request_player(payload.get('player_id'), payload.get('player_name'))
    if not code or not player_id:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        players = [p for p in (room.get('players') or []) if (p.get('id') or '') != player_id]
        room['players'] = players
        room['updated_at'] = now_ts
    return jsonify({'code': code})

@app.route('/api/roulette/rooms/public', methods=['GET'])
def roulette_public_rooms():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        rooms = []
        for code, room in ROULETTE_ROOMS.items():
            if not room.get('is_public'):
                continue
            if bool(room.get('closed') or False):
                continue
            players = list(room.get('players') or [])
            owner_pid = room.get('owner_player_id') or ''
            owner_name = ''
            for p in players:
                if (p.get('id') or '') == owner_pid:
                    owner_name = p.get('name') or ''
                    break
            rooms.append({
                'code': code,
                'category': room.get('category') or 'all',
                'verified_mode': room.get('verified_mode') or 'verified',
                'tags': list(room.get('tags') or []),
                'top_n': int(room.get('top_n') or 0),
                'owner_name': owner_name or 'Owner',
                'players_count': len(players),
                'created_at': int(room.get('created_at') or now_ts)
            })
    rooms.sort(key=lambda r: (-(r.get('players_count') or 0), -(r.get('created_at') or 0)))
    return jsonify({'rooms': rooms[:50]})

@app.route('/api/roulette/room/kick', methods=['POST'])
def roulette_room_kick():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or request.args.get('code') or '').upper().strip()
    owner_token = (payload.get('owner_token') or '').strip()
    target_player_id = (payload.get('player_id') or payload.get('target_player_id') or '').strip()[:64]
    if not code or not target_player_id:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        allowed = False
        if owner_token and owner_token == (room.get('owner_token') or ''):
            allowed = True
        elif getattr(current_user, 'is_authenticated', False) and (getattr(current_user, 'is_admin', False) or getattr(current_user, 'is_mod', False)):
            allowed = True
        if not allowed:
            return jsonify({'error': 'forbidden'}), 403
        if target_player_id == (room.get('owner_player_id') or '') and not (getattr(current_user, 'is_authenticated', False) and getattr(current_user, 'is_admin', False)):
            return jsonify({'error': 'forbidden'}), 403
        players = [p for p in (room.get('players') or []) if (p.get('id') or '') != target_player_id]
        room['players'] = players
        kicked = dict(room.get('kicked') or {})
        kicked[target_player_id] = now_ts + (30 * 60)
        room['kicked'] = kicked
        room['updated_at'] = now_ts
        out_players = _roulette_public_players(room)
    return jsonify({'code': code, 'players': out_players})

@app.route('/api/roulette/room/result', methods=['POST'])
def roulette_room_result():
    user, err = _roulette_require_user()
    if err:
        code, status = err
        return jsonify({'error': code}), status
    code = (request.args.get('code') or request.form.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _roulette_request_player(
        request.form.get('player_id') or request.args.get('player_id'),
        request.form.get('player_name')
    )
    if not player_name:
        player_name = 'Player'
    try:
        level_id = int(request.form.get('level_id'))
        round_no = int(request.form.get('round_no'))
        first_try_percent = float(request.form.get('first_try_percent'))
        best_percent = float(request.form.get('best_percent'))
        attempts_used = int(request.form.get('attempts_used'))
    except Exception:
        return jsonify({'error': 'bad_request'}), 400
    first_try_percent = max(0.0, min(100.0, first_try_percent))
    best_percent = max(0.0, min(100.0, best_percent))
    if best_percent < first_try_percent:
        best_percent = first_try_percent
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _roulette_room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if player_id and _roulette_is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _roulette_touch_player(room, player_id, player_name, now_ts)
        room['updated_at'] = now_ts
        max_attempts = int(room.get('max_attempts') or 10)
        require_proof = bool(room.get('require_proof') or False)
    if attempts_used < 1 or attempts_used > max_attempts:
        return jsonify({'error': 'bad_request'}), 400
    proof_url = None
    f = request.files.get('proof')
    if require_proof:
        if not f or not f.filename:
            return jsonify({'error': 'proof_required'}), 400
    if f and f.filename:
        folder = os.path.join(app.root_path, 'static', 'roulette_proofs')
        unique, err = _save_uploaded_file(f, folder, _ALLOWED_IMAGE_EXTS, 'image/')
        if not unique:
            if err == 'rate_limited':
                return jsonify({'error': 'rate_limited'}), 429
            return jsonify({'error': 'bad_file'}), 400
        proof_url = url_for('static', filename=f'roulette_proofs/{unique}')
    lv = db.session.get(Level, level_id)
    if not lv:
        return jsonify({'error': 'invalid_level'}), 404
    entry = {
        'player_name': player_name,
        'level': {'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position},
        'round_no': round_no,
        'first_try_percent': first_try_percent,
        'best_percent': best_percent,
        'attempts_used': attempts_used,
        'proof_url': proof_url,
        'submitted_at': int(now_ts)
    }
    with ROULETTE_ROOMS_LOCK:
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        results = list(room.get('results') or [])
        results.append(entry)
        room['results'] = results[-500:]
        room['updated_at'] = now_ts
        max_rounds = int(room.get('max_rounds') or 15)
        current_round = int(room.get('current_round') or 0)
        require_proof = bool(room.get('require_proof') or False)
        max_attempts = int(room.get('max_attempts') or 10)
        out = {'code': code, 'results': room['results'][-200:], 'max_rounds': max_rounds, 'current_round': current_round, 'require_proof': require_proof, 'max_attempts': max_attempts}
    return jsonify(out)


@app.route('/api/levels', methods=['GET'])
def api_levels():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    from sqlalchemy import or_
    raw_list = request.args.get('list')
    raw_status = request.args.get('status')
    list_type = (raw_list or '').strip().lower()
    if list_type not in ('', 'all', 'main', 'challenge', 'platformer'):
        return jsonify({'error': 'bad_request'}), 400
    status = (raw_status or '').strip().lower()
    if not status:
        status = 'verified'
        try:
            ep = str(request.endpoint or '').strip()
        except Exception:
            ep = ''
        if ep == 'api_levels_unverified':
            status = 'unverified'
    if not list_type:
        try:
            ep = str(request.endpoint or '').strip()
        except Exception:
            ep = ''
        if ep == 'api_levels_main':
            list_type = 'main'
        elif ep == 'api_levels_challenge':
            list_type = 'challenge'
        elif ep == 'api_levels_platformer':
            list_type = 'platformer'
    if status not in ('verified', 'unverified', 'all'):
        return jsonify({'error': 'bad_request'}), 400
    include_unlisted = str(request.args.get('include_unlisted') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    order = (request.args.get('order') or '').strip().lower() or 'position'
    search_q = (request.args.get('q') or '').strip()
    tags_raw = (request.args.get('tag') or request.args.get('tags') or '').strip()
    min_points = request.args.get('min_points')
    max_points = request.args.get('max_points')
    min_position = request.args.get('min_position')
    max_position = request.args.get('max_position')

    q = Level.query
    if list_type and list_type != 'all':
        q = q.filter(Level.list_type == list_type)
    if status == 'verified':
        q = q.filter(Level.is_verified == True)
    elif status == 'unverified':
        q = q.filter(Level.is_verified == False)

    if search_q:
        like = f'%{search_q}%'
        q = q.filter(or_(Level.name.ilike(like), Level.creator.ilike(like)))

    if tags_raw:
        wanted = {t.strip().lower() for t in tags_raw.split(',') if t.strip()}
        if wanted:
            q = q.filter(Level.categories.isnot(None))
            q = q.filter(
                or_(
                    *[
                        Level.categories.ilike(f'%{t}%')
                        for t in wanted
                    ]
                )
            )

    try:
        if min_points is not None and str(min_points).strip() != '':
            min_points_val = float(min_points)
            q = q.filter(Level.points_reward >= min_points_val)
    except Exception:
        pass
    try:
        if max_points is not None and str(max_points).strip() != '':
            max_points_val = float(max_points)
            q = q.filter(Level.points_reward <= max_points_val)
    except Exception:
        pass

    try:
        if min_position is not None and str(min_position).strip() != '':
            min_pos_val = int(min_position)
            q = q.filter(Level.position.isnot(None), Level.position >= min_pos_val)
    except Exception:
        pass
    try:
        if max_position is not None and str(max_position).strip() != '':
            max_pos_val = int(max_position)
            q = q.filter(Level.position.isnot(None), Level.position <= max_pos_val)
    except Exception:
        pass

    if not include_unlisted:
        q = q.filter(Level.position.isnot(None))

    if order == 'position':
        q = q.order_by(Level.list_type.asc(), Level.position.asc().nullslast(), Level.id.asc())
    elif order == 'points':
        q = q.order_by(Level.points_reward.desc(), Level.id.asc())
    elif order == 'id':
        q = q.order_by(Level.id.asc())
    else:
        q = q.order_by(Level.id.asc())

    levels = q.all()
    data = []
    for lv in levels:
        data.append(
            {
                'id': lv.id,
                'gd_level_id': lv.gd_level_id,
                'name': lv.name,
                'creator': lv.creator,
                'list_type': lv.list_type,
                'is_verified': bool(lv.is_verified),
                'position': lv.position,
                'points_reward': float(lv.points_reward or 0.0),
            }
        )
    return jsonify({'levels': data})


@app.route('/api/levels/main', methods=['GET'])
def api_levels_main():
    return api_levels()


@app.route('/api/levels/challenge', methods=['GET'])
def api_levels_challenge():
    return api_levels()


@app.route('/api/levels/platformer', methods=['GET'])
def api_levels_platformer():
    return api_levels()


@app.route('/api/levels/unverified', methods=['GET'])
def api_levels_unverified():
    return api_levels()


@app.route('/api/levels/random', methods=['GET'])
def api_levels_random():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    base = api_levels().get_json() or {}
    levels = list(base.get('levels') or [])
    if not levels:
        return jsonify({'error': 'not_found'}), 404
    import random
    return jsonify({'level': random.choice(levels)})


@app.route('/api/health', methods=['GET'])
def api_health():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    return jsonify({'ok': True})


@app.route('/api/stats', methods=['GET'])
def api_stats():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    try:
        levels_total = int(db.session.query(db.func.count(Level.id)).scalar() or 0)
    except Exception:
        levels_total = 0
    try:
        users_total = int(db.session.query(db.func.count(User.id)).scalar() or 0)
    except Exception:
        users_total = 0
    try:
        clans_total = int(db.session.query(db.func.count(Clan.id)).scalar() or 0)
    except Exception:
        clans_total = 0
    try:
        records_total = int(db.session.query(db.func.count(Record.id)).scalar() or 0)
    except Exception:
        records_total = 0
    return jsonify(
        {
            'levels_total': levels_total,
            'players_total': users_total,
            'clans_total': clans_total,
            'records_total': records_total,
        }
    )


@app.route('/api/tags', methods=['GET'])
def api_tags():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    rows = db.session.query(Level.categories).filter(Level.categories.isnot(None)).all()
    counts = {}
    for (cats,) in rows:
        if not cats:
            continue
        for raw in str(cats).split(','):
            t = raw.strip()
            if not t:
                continue
            key = t
            counts[key] = counts.get(key, 0) + 1
    tags = sorted(counts.keys(), key=lambda k: (-counts[k], k.lower()))
    return jsonify({'tags': tags})


@app.route('/api/tags/<string:tag>/levels', methods=['GET'])
def api_tag_levels(tag):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    t = (tag or '').strip()
    if not t:
        return jsonify({'error': 'bad_request'}), 400
    like = f'%{t}%'
    q = Level.query.filter(Level.categories.isnot(None), Level.categories.ilike(like))
    levels = q.order_by(Level.list_type.asc(), Level.position.asc().nullslast(), Level.id.asc()).all()
    out = []
    for lv in levels:
        out.append(
            {
                'id': lv.id,
                'gd_level_id': lv.gd_level_id,
                'name': lv.name,
                'creator': lv.creator,
                'list_type': lv.list_type,
                'is_verified': bool(lv.is_verified),
                'position': lv.position,
                'points_reward': float(lv.points_reward or 0.0),
            }
        )
    return jsonify({'levels': out})


@app.route('/api/tags/popular', methods=['GET'])
def api_tags_popular():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    rows = db.session.query(Level.categories).filter(Level.categories.isnot(None)).all()
    counts = {}
    for (cats,) in rows:
        if not cats:
            continue
        for raw in str(cats).split(','):
            t = raw.strip()
            if not t:
                continue
            key = t
            counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    out = [{'tag': k, 'count': int(v or 0)} for (k, v) in items]
    return jsonify({'tags': out})


@app.route('/api/bottom-index', methods=['GET'])
@app.route('/api/bl-index', methods=['GET'])
def api_bottom_index():
    """NONG index endpoint matching Song File Hub / Auto Nong format exactly."""
    import time
    import uuid
    
    site_url = str(_get_setting('site_url') or request.host_url or '').strip()
    if site_url and not site_url.endswith('/'):
        site_url += '/'
    discord_url = str(_get_setting('footer_discord_url') or 'https://discord.gg/maSgd4zpEF')

    # Build hosted NONG entries
    hosted = {}
    
    # Query all NONGs (verified and unverified)
    nongs = (
        LevelNong.query
        .join(Level)
        .all()
    )

    # Find latest update time for manifest
    latest_update = 0
    if nongs:
        # Use created_at as a proxy for update time since we don't have updated_at yet
        latest_update = int(max(n.created_at.timestamp() for n in nongs if n.created_at))
    else:
        latest_update = int(time.time())

    for nong in nongs:
        lv = nong.level
        if not lv:
            continue
        
        # Verify the file actually exists
        file_path = os.path.join(app.root_path, 'static', 'songs', nong.song_filename)
        if not os.path.exists(file_path):
            # Skip NONGs with missing files
            continue
        
        # Get the song ID to replace - first from LevelNong, then from Level
        song_id = None
        
        # Try LevelNong's song_id first (stored when submission was approved)
        if nong.song_id is not None:
            try:
                song_id = int(nong.song_id)
            except (ValueError, TypeError):
                pass
        
        # Fallback to Level's song_id/official_song_id
        if song_id is None and lv.song_id is not None:
            try:
                song_id = int(lv.song_id)
            except (ValueError, TypeError):
                pass
        
        if song_id is None and lv.official_song_id is not None:
            try:
                song_id = int(lv.official_song_id)
            except (ValueError, TypeError):
                pass
        
        # Skip if we can't determine the song ID
        if song_id is None:
            continue

        # Generate UUID if not set
        if not nong.uuid:
            nong.uuid = str(uuid.uuid4())
            db.session.commit()

        song_url = url_for('serve_song', filename=nong.song_filename, _external=True)

        # Build verifiedLevelIDs array - GD level IDs where this NONG is verified
        verified_level_ids = []
        if nong.is_verified and lv.gd_level_id:
            try:
                gd_id_int = int(lv.gd_level_id)
                verified_level_ids = [gd_id_int]
            except (ValueError, TypeError):
                pass

        hosted[nong.uuid] = {
            'name': nong.song_name or lv.name,
            'artist': nong.artist_name or lv.creator,
            'startOffset': nong.start_offset or 0,
            'url': song_url,
            'songs': [song_id],  # The song ID to replace (from GD level data)
            'verifiedLevelIDs': verified_level_ids,  # GD level IDs where this NONG is the verified one
        }

    response = jsonify({
        'manifest': 1,
        'name': 'Bottom List NONG Index',
        'id': 'bottom-index',
        'description': 'Bottom List Official NONG Index',
        'lastUpdate': latest_update,
        'links': {
            'discord': discord_url,
        },
        'features': {
            'submit': {
                'supportedSongTypes': ['hosted'],
                'preSubmitMessage': 'Submit songs via the Bottom List Discord Bot or website.\n\nOnly mp3/ogg files are supported.',
                'requestParams': {
                    'url': discord_url,
                    'params': False,
                },
            },
        },
        'nongs': {
            'hosted': hosted,
            'youtube': {},
        },
    })
    
    # Add cache-busting headers
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response


@app.route('/api/clans', methods=['GET'])
def api_clans():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    q = Clan.query
    name_query = (request.args.get('q') or '').strip()
    if name_query:
        like = f'%{name_query}%'
        q = q.filter(Clan.name.ilike(like))
    q = q.order_by(Clan.name.asc())
    clans = q.all()

    out = []
    for c in clans:
        try:
            members_count = len(list(c.members or []))
        except Exception:
            members_count = 0
        owner_username = None
        try:
            if c.owner:
                owner_username = c.owner.username
        except Exception:
            owner_username = None
        out.append(
            {
                'id': c.id,
                'name': c.name,
                'description': c.description,
                'owner_user_id': c.owner_user_id,
                'owner_username': owner_username,
                'member_count': members_count,
                'created_at': c.created_at.isoformat() if c.created_at else None,
            }
        )
    return jsonify({'clans': out})


@app.route('/api/players', methods=['GET'])
def api_players():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    q = (request.args.get('q') or '').strip()
    sort_by = (request.args.get('sort') or 'points').strip().lower()
    limit = request.args.get('limit')
    try:
        limit_val = int(limit or 100)
    except Exception:
        limit_val = 100
    if limit_val <= 0:
        limit_val = 1
    if limit_val > 500:
        limit_val = 500
    query = User.query
    if q:
        like = f'%{q}%'
        query = query.filter(User.username.ilike(like))
    if sort_by == 'username':
        query = query.order_by(User.username.asc())
    else:
        query = query.order_by(User.points.desc(), User.username.asc())
    users = query.limit(limit_val).all()
    out = []
    for u in users:
        clan_id = getattr(u, 'clan_id', None)
        clan_name = None
        try:
            if clan_id:
                rel = getattr(u, 'clan', None)
                if rel and getattr(rel, 'name', None):
                    clan_name = rel.name
        except Exception:
            clan_name = None
        out.append(
            {
                'id': u.id,
                'username': u.username,
                'points': float(getattr(u, 'points', 0.0) or 0.0),
                'rank': getattr(u, 'rank', None),
                'clan_id': clan_id,
                'clan_name': clan_name,
            }
        )
    return jsonify({'players': out})

def _user_profile_json(u):
    try:
        avatar = (getattr(u, 'avatar_filename', '') or '').strip()
    except Exception:
        avatar = ''
    try:
        banner = (getattr(u, 'banner_filename', '') or '').strip()
    except Exception:
        banner = ''
    try:
        avatar_url = url_for('static', filename=f'profile_media/{avatar}') if avatar else None
    except Exception:
        avatar_url = None
    try:
        banner_url = url_for('static', filename=f'profile_media/{banner}') if banner else None
    except Exception:
        banner_url = None
    return {
        'id': u.id,
        'username': u.username,
        'points': float(getattr(u, 'points', 0.0) or 0.0),
        'rank': getattr(u, 'rank', None),
        'avatar_url': avatar_url,
        'banner_url': banner_url,
    }

def _record_json(r, lv):
    try:
        img = (getattr(r, 'proof_image_filename', '') or '').strip()
    except Exception:
        img = ''
    try:
        vc = (getattr(r, 'vc_proof_filename', '') or '').strip()
    except Exception:
        vc = ''
    try:
        img_url = url_for('static', filename=f'record_proofs/{img}') if img else None
    except Exception:
        img_url = None
    try:
        vc_url = url_for('static', filename=f'vc_proofs/{vc}') if vc else None
    except Exception:
        vc_url = None
    return {
        'id': r.id,
        'status': r.status,
        'device': getattr(r, 'device', None),
        'video_url': getattr(r, 'video_url', None),
        'beaten_on_vc': bool(getattr(r, 'beaten_on_vc', False)),
        'proof_image_url': img_url,
        'vc_proof_url': vc_url,
        'submitted_at': r.submitted_at.isoformat() if r.submitted_at else None,
        'level': {
            'id': getattr(lv, 'id', None),
            'name': getattr(lv, 'name', None),
            'list_type': getattr(lv, 'list_type', None),
            'position': getattr(lv, 'position', None),
            'points_reward': float(getattr(lv, 'points_reward', 0.0) or 0.0),
        } if lv else None
    }

@app.route('/api/users/<int:user_id>/profile', methods=['GET'])
def api_user_profile(user_id):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'user': _user_profile_json(u)})

@app.route('/api/users/<int:user_id>/records', methods=['GET'])
def api_user_records(user_id):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    status = (request.args.get('status') or 'approved').strip().lower()
    if status not in ('approved', 'pending', 'rejected', 'all'):
        return jsonify({'error': 'bad_request'}), 400
    list_type = (request.args.get('list') or '').strip().lower()
    if list_type and list_type not in ('all', 'main', 'challenge', 'platformer'):
        return jsonify({'error': 'bad_request'}), 400
    limit = request.args.get('limit')
    try:
        limit_val = int(limit or 200)
    except Exception:
        limit_val = 200
    if limit_val <= 0:
        limit_val = 1
    if limit_val > 1000:
        limit_val = 1000
    q = Record.query.filter(Record.user_id == u.id)
    if status != 'all':
        q = q.filter(Record.status == status)
    q = q.join(Level, Level.id == Record.level_id)
    if list_type and list_type != 'all':
        q = q.filter(Level.list_type == list_type)
    rows = q.order_by(Record.submitted_at.desc()).limit(limit_val).all()
    out = []
    for r in rows:
        try:
            lv = r.level
        except Exception:
            lv = None
        out.append(_record_json(r, lv))
    return jsonify({'records': out})

@app.route('/api/me/profile', methods=['GET'])
def api_me_profile():
    user, auth = _api_auth()
    if auth:
        return auth
    return jsonify({'user': _user_profile_json(user)})

@app.route('/api/me/records', methods=['GET'])
def api_me_records():
    user, auth = _api_auth()
    if auth:
        return auth
    with app.app_context():
        request_args = request.args
    with app.test_request_context(query_string=request_args):
        return api_user_records(user.id)

@app.route('/api/me/export', methods=['GET'])
def api_me_export():
    user, auth = _api_auth()
    if auth:
        return auth
    fmt = (request.args.get('fmt') or 'json').strip().lower()
    limit = request.args.get('limit')
    try:
        limit_val = int(limit or 20000)
    except Exception:
        limit_val = 20000
    if limit_val <= 0:
        limit_val = 1
    if limit_val > 200000:
        limit_val = 200000

    recs = (
        Record.query
        .filter(Record.user_id == user.id)
        .order_by(Record.submitted_at.desc())
        .limit(limit_val)
        .all()
    )
    subs = (
        LevelSubmission.query
        .filter(LevelSubmission.user_id == user.id)
        .order_by(LevelSubmission.submitted_at.desc())
        .limit(limit_val)
        .all()
    )
    if fmt == 'csv':
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['type', 'id', 'status', 'submitted_at', 'level_id', 'level_name', 'level_creator', 'video_url', 'device', 'comment'])
        for r in recs:
            lv = None
            try:
                lv = r.level
            except Exception:
                lv = None
            w.writerow([
                'record',
                getattr(r, 'id', None),
                getattr(r, 'status', ''),
                getattr(r, 'submitted_at', None),
                getattr(r, 'level_id', None),
                getattr(lv, 'name', '') if lv else '',
                getattr(lv, 'creator', '') if lv else '',
                getattr(r, 'video_url', ''),
                getattr(r, 'device', ''),
                getattr(r, 'comment', ''),
            ])
        for s in subs:
            w.writerow([
                'submission',
                getattr(s, 'id', None),
                getattr(s, 'status', ''),
                getattr(s, 'submitted_at', None),
                '',
                getattr(s, 'name', ''),
                getattr(s, 'creator', ''),
                getattr(s, 'video_url', ''),
                '',
                '',
            ])
        data = buf.getvalue()
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="my_data_export.csv"'})

    payload = {
        'user': _user_profile_json(user),
        'records': [],
        'submissions': [],
    }
    for r in recs:
        lv = None
        try:
            lv = r.level
        except Exception:
            lv = None
        payload['records'].append(_record_json(r, lv))
    for s in subs:
        payload['submissions'].append({
            'id': s.id,
            'name': s.name,
            'creator': s.creator,
            'video_url': s.video_url,
            'status': s.status,
            'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None,
        })
    resp = jsonify(payload)
    resp.headers['Content-Disposition'] = 'attachment; filename="my_data_export.json"'
    return resp


def _serialize_event(ev: Event) -> dict:
    return {
        'id': ev.id,
        'title': ev.title,
        'body': ev.body,
        'starts_at': ev.starts_at.isoformat() if ev.starts_at else None,
        'ends_at': ev.ends_at.isoformat() if ev.ends_at else None,
        'is_published': bool(ev.is_published),
        'created_at': ev.created_at.isoformat() if ev.created_at else None,
    }


@app.route('/api/events', methods=['GET'])
def api_events():
    user, auth_err = _api_auth()
    if auth_err:
        return auth_err
    status = (request.args.get('status') or 'published').strip().lower()
    if status not in ('published', 'all'):
        return jsonify({'error': 'bad_request'}), 400
    if status == 'all' and not _has_perm(user, 'manage_events'):
        return jsonify({'error': 'forbidden'}), 403
    limit = request.args.get('limit')
    try:
        limit_val = int(limit or 100)
    except Exception:
        limit_val = 100
    if limit_val <= 0:
        limit_val = 1
    if limit_val > 500:
        limit_val = 500
    q = Event.query
    if status == 'published':
        q = q.filter(Event.is_published.is_(True))
    q = q.order_by(Event.starts_at.desc().nullslast(), Event.created_at.desc())
    events = q.limit(limit_val).all()
    return jsonify({'events': [_serialize_event(ev) for ev in events]})


@app.route('/api/events/<int:event_id>', methods=['GET'])
def api_event_detail(event_id):
    user, auth_err = _api_auth()
    if auth_err:
        return auth_err
    ev = Event.query.get_or_404(event_id)
    if not bool(getattr(ev, 'is_published', True)) and not _has_perm(user, 'manage_events'):
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'event': _serialize_event(ev)})


@app.route('/admin/add_level', methods=['GET', 'POST'])
@login_required
def add_level():
    _require_perm('manage_levels')
        
    if request.method == 'POST':
        name = request.form.get('name')
        creator = request.form.get('creator')
        video_url = request.form.get('video_url')
        description = request.form.get('description')
        gd_level_id = request.form.get('gd_level_id')
        
        song_id = None
        try:
            sid = request.form.get('song_id', '').strip()
            if sid: song_id = int(sid)
        except Exception:
            pass
            
        official_song_id = None
        try:
            osid = request.form.get('official_song_id', '').strip()
            if osid: official_song_id = int(osid)
        except Exception:
            pass
            
        try:
            points_reward = float(request.form.get('points_reward', 0))
        except ValueError:
            points_reward = 0.0
            
        is_verified = True if request.form.get('is_verified') == 'on' else False
        list_type = (request.form.get('list_type') or 'main').strip().lower()
        if list_type not in ('main', 'challenge', 'platformer'):
            list_type = 'main'
        difficulty = (request.form.get('difficulty') or '').strip()[:40] or None
        uses_nong = True if request.form.get('uses_nong') == 'on' else False
        categories = request.form.getlist('categories')
        categories_str = ",".join(categories)

        insert_pos = None
        raw_pos = (request.form.get('position') or '').strip()
        if raw_pos:
            try:
                insert_pos = int(raw_pos)
            except Exception:
                insert_pos = None

        song_filename = None
        if uses_nong:
            file = request.files.get('nong_file')
            if file and file.filename:
                unique, err = _save_uploaded_file(file, app.config['SONG_UPLOAD_FOLDER'], _ALLOWED_AUDIO_EXTS, '')
                if not unique:
                    if err == 'rate_limited':
                        flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                    else:
                        flash('NONG song must be a valid audio file (mp3/wav/ogg).', 'danger')
                    return redirect(url_for('add_level'))
                song_filename = unique
        thumb_unique = None
        thumb_file = request.files.get('thumbnail')
        if thumb_file and thumb_file.filename:
            folder = os.path.join(app.root_path, 'static', 'level_thumbs')
            thumb_unique, err = _save_uploaded_file(thumb_file, folder, _ALLOWED_IMAGE_EXTS, 'image/')
            if not thumb_unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('Thumbnail must be a valid image (png/jpg/webp).', 'danger')
                return redirect(url_for('add_level'))
        new_level = Level(
            name=name,
            creator=creator,
            video_url=video_url,
            description=description,
            gd_level_id=gd_level_id,
            song_id=song_id,
            official_song_id=official_song_id,
            points_reward=points_reward,
            is_verified=is_verified,
            list_type=list_type,
            difficulty=difficulty,
            position=None,
            uses_nong=uses_nong,
            song_filename=song_filename,
            categories=categories_str,
            thumbnail_filename=thumb_unique or None
        )
        
        db.session.add(new_level)

        affected = Level.query.filter(
            Level.list_type == list_type,
            Level.is_verified == bool(is_verified),
            Level.position.isnot(None)
        ).order_by(Level.position).all()

        if insert_pos is None:
            insert_pos = len(affected) + 1
        if insert_pos < 1:
            insert_pos = 1
        if insert_pos > len(affected) + 1:
            insert_pos = len(affected) + 1

        ordered = list(affected)
        ordered.insert(insert_pos - 1, new_level)
        for idx, lv in enumerate(ordered, start=1):
            lv.position = idx

        if _auto_points_applies_to(list_type):
            rules = _parse_auto_points_rules(_get_auto_points_rules_text_for(list_type, bool(is_verified)))
            for lv in ordered:
                lv.points_reward = _points_for_position(int(lv.position or 0), rules)

        db.session.commit()
        if _auto_points_applies_to(list_type):
            try:
                _recalc_all_user_points()
            except Exception:
                pass
        try:
            vlabel = 'verified' if bool(is_verified) else 'unverified'
            add_site_log(
                'level_create',
                f'{current_user.username} created level "{(name or "")[:150]}" by {(creator or "")[:150]} ({list_type}, {vlabel}) at #{int(new_level.position or 0)}. Points: {float(new_level.points_reward or 0.0)}.',
                actor_user_id=current_user.id
            )
        except Exception:
            pass
        
        status_msg = "Verified List" if is_verified else "Unverified List"
        flash(f'Level "{name}" added successfully to {status_msg} ({list_type})!', 'success')
        return redirect(url_for('admin_dashboard'))
        
    return render_template('add_level.html')

@app.route('/admin/ui-marketplace', methods=['GET', 'POST'])
@login_required
def admin_ui_marketplace():
    _require_perm('manage_settings')
    tpl_dir = os.path.join(app.root_path, 'templates', 'ui_marketplace')
    os.makedirs(tpl_dir, exist_ok=True)
    templates = []
    try:
        for name in os.listdir(tpl_dir):
            if name.endswith('.html'):
                templates.append(name[:-5])
    except Exception:
        templates = []
    if request.method == 'POST':
        action = (request.form.get('action') or 'add').strip().lower()
        slug = (request.form.get('slug') or '').strip().lower()
        
        if action == 'delete':
            try:
                p = os.path.join(tpl_dir, f'{slug}.html')
                if os.path.exists(p):
                    os.remove(p)
                add_site_log('admin_action', f'{current_user.username} deleted marketplace template "{slug}".', actor_user_id=current_user.id)
                flash('Marketplace template deleted.', 'success')
            except Exception:
                flash('Could not delete template.', 'danger')
            return redirect(url_for('admin_ui_marketplace'))
            
        body = (request.form.get('template_html') or '').strip()
        if not re.match(r'^[a-z0-9_-]{3,80}$', slug or ''):
            flash('Slug must be 3-80 chars: a-z, 0-9, -, _.', 'danger')
            return redirect(url_for('admin_ui_marketplace'))
        if len(body) < 1 or len(body) > 20000 or ('{{ content_html' not in body):
            flash('Template must include {{ content_html }} and be under 20k characters.', 'danger')
            return redirect(url_for('admin_ui_marketplace'))
        try:
            with open(os.path.join(tpl_dir, f'{slug}.html'), 'w', encoding='utf-8') as f:
                f.write(body)
            if action == 'edit':
                add_site_log('admin_action', f'{current_user.username} edited marketplace template "{slug}".', actor_user_id=current_user.id)
                flash('Marketplace template updated.', 'success')
            else:
                add_site_log('admin_action', f'{current_user.username} added marketplace template "{slug}".', actor_user_id=current_user.id)
                flash('Marketplace template added.', 'success')
        except Exception:
            flash('Could not save template.', 'danger')
        return redirect(url_for('admin_ui_marketplace'))
    
    # Load body for edit if slug provided
    edit_slug = request.args.get('edit')
    edit_body = ''
    if edit_slug:
        try:
            p = os.path.join(tpl_dir, f'{edit_slug}.html')
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    edit_body = f.read()
        except Exception:
            pass
            
    return render_template('admin_ui_marketplace.html', templates=templates, edit_slug=edit_slug, edit_body=edit_body)
@app.route('/admin')
@login_required
def admin_dashboard():
    _require_perm('access_admin')
    
    q = (request.args.get('q') or '').strip().lower()
    t = (request.args.get('type') or 'all').lower()
    levels_sort = (request.args.get('levels_sort') or 'position').strip().lower()
    if levels_sort not in ('position', 'name', 'creator', 'list', 'verified', 'newest'):
        levels_sort = 'position'
    from sqlalchemy.orm import joinedload
    pending_records = Record.query.options(joinedload(Record.user), joinedload(Record.level)).filter_by(status='pending').all()
    approved_page = int(request.args.get('approved_page', 1) or 1)
    try:
        approved_per_page = int(request.args.get('approved_per_page', 100) or 100)
    except Exception:
        approved_per_page = 100
    if approved_per_page < 25:
        approved_per_page = 25
    if approved_per_page > 500:
        approved_per_page = 500
    approved_base_q = (
        Record.query
        .options(joinedload(Record.user), joinedload(Record.level))
        .filter_by(status='approved')
        .order_by(Record.submitted_at.desc())
    )
    approved_pagination = approved_base_q.paginate(page=approved_page, per_page=approved_per_page, error_out=False)
    approved_records = approved_pagination.items
    pending_levels = LevelSubmission.query.options(joinedload(LevelSubmission.user)).filter_by(status='pending').all()
    if levels_sort == 'name':
        levels = Level.query.order_by(Level.name.asc()).all()
    elif levels_sort == 'creator':
        levels = Level.query.order_by(Level.creator.asc(), Level.name.asc()).all()
    elif levels_sort == 'list':
        levels = Level.query.order_by(Level.list_type.asc(), Level.name.asc()).all()
    elif levels_sort == 'verified':
        levels = Level.query.order_by(Level.is_verified.desc(), Level.list_type.asc(), db.func.coalesce(Level.position, 999999).asc(), Level.name.asc()).all()
    elif levels_sort == 'newest':
        levels = Level.query.order_by(Level.created_at.desc()).all()
    else:
        levels = Level.query.order_by(Level.list_type.asc(), db.func.coalesce(Level.position, 999999).asc(), Level.name.asc()).all()
    total_users_count = User.query.count()
    total_levels_count = Level.query.count()
    users = []
    if _has_perm(current_user, 'manage_users'):
        users = User.query.order_by(User.username.asc()).limit(200).all()
    def _match(text):
        return q in (text or '').lower()
    if q:
        if t in ('all','records'):
            like = f"%{q}%"
            base = (
                Record.query
                .join(User, Record.user_id == User.id)
                .join(Level, Record.level_id == Level.id)
                .filter(
                    db.func.lower(User.username).like(like)
                    | db.func.lower(Level.name).like(like)
                    | db.func.lower(Record.device).like(like)
                )
            )
            pending_records = (
                base.filter(Record.status == 'pending')
                .order_by(Record.submitted_at.desc())
                .all()
            )
            approved_base_q = (
                base.filter(Record.status == 'approved')
                .options(joinedload(Record.user), joinedload(Record.level))
                .order_by(Record.submitted_at.desc())
            )
            approved_pagination = approved_base_q.paginate(page=approved_page, per_page=approved_per_page, error_out=False)
            approved_records = approved_pagination.items
        if t in ('all','levels'):
            levels = [lv for lv in levels if _match(lv.name) or _match(lv.creator)]
        if t in ('all','submissions'):
            like = f"%{q}%"
            base = (
                LevelSubmission.query
                .join(User, LevelSubmission.user_id == User.id)
                .filter(
                    db.func.lower(LevelSubmission.name).like(like)
                    | db.func.lower(LevelSubmission.creator).like(like)
                    | db.func.lower(User.username).like(like)
                )
            )
            pending_levels = (
                base.order_by(LevelSubmission.submitted_at.desc())
                .limit(500)
                .all()
            )
        if t in ('all','users') and _has_perm(current_user, 'manage_users'):
            users = [u for u in users if _match(u.username)]
        
    news_posts = []
    webhook_url = ''
    log_webhook_url = ''
    site_launch_webhook_url = ''
    discord_client_id = ''
    discord_redirect_uri = ''
    discord_client_secret_set = False
    captcha_provider = ''
    recaptcha_site_key = ''
    recaptcha_secret_set = False
    hcaptcha_site_key = ''
    hcaptcha_secret_set = False
    guidelines_url = ''
    hide_categories_on_main_list = False
    record_proof_top_n = 30
    record_proof_top_n_main = 30
    record_proof_top_n_challenge = 30
    record_proof_top_n_platformer = 10000
    auto_points_enabled = False
    auto_points_scope = 'both'
    auto_points_rules_main_verified = ''
    auto_points_rules_main_unverified = ''
    auto_points_rules_challenge_verified = ''
    auto_points_rules_challenge_unverified = ''
    auto_points_rules_platformer_verified = ''
    auto_points_rules_platformer_unverified = ''
    active_users_count = 0
    logs = []
    api_logs = []
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=15)
        active_users_count = User.query.filter(User.last_seen_at.isnot(None), User.last_seen_at >= cutoff).count()
    except Exception:
        active_users_count = 0
    try:
        logs = (
            SiteLog.query
            .filter(SiteLog.event_type != 'api_call')
            .order_by(SiteLog.created_at.desc())
            .limit(100)
            .all()
        )
    except Exception:
        logs = []
    try:
        api_logs = (
            SiteLog.query
            .filter(SiteLog.event_type == 'api_call')
            .order_by(SiteLog.created_at.desc())
            .limit(100)
            .all()
        )
    except Exception:
        api_logs = []
    if _has_perm(current_user, 'manage_news'):
        news_posts = NewsPost.query.order_by(NewsPost.created_at.desc()).limit(20).all()
    if _has_perm(current_user, 'manage_settings'):
        webhook_url = _get_setting('discord_webhook_url') or WEBHOOK_URL or ''
        log_webhook_url = _get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL or ''
        site_launch_webhook_url = _get_setting('discord_site_launch_webhook_url') or ''
        discord_client_id = _get_discord_client_id()
        discord_redirect_uri = _get_discord_redirect_uri()
        discord_client_secret_set = bool(_get_discord_client_secret())
        captcha_provider = _get_captcha_provider()
        recaptcha_site_key = _get_recaptcha_site_key()
        recaptcha_secret_set = bool(_get_recaptcha_secret_key())
        hcaptcha_site_key = _get_hcaptcha_site_key()
        hcaptcha_secret_set = bool(_get_hcaptcha_secret_key())
        guidelines_url = _get_guidelines_url()
        hide_categories_on_main_list = str(_get_setting('hide_categories_on_main_list') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        record_proof_top_n = _record_proof_top_n()
        record_proof_top_n_main = _record_proof_top_n_for_list('main')
        record_proof_top_n_challenge = _record_proof_top_n_for_list('challenge')
        record_proof_top_n_platformer = _record_proof_top_n_for_list('platformer')
        auto_points_enabled = _auto_points_enabled()
        auto_points_scope = _auto_points_scope()
        auto_points_rules_main_verified = _get_auto_points_rules_text_for('main', True)
        auto_points_rules_main_unverified = _get_auto_points_rules_text_for('main', False)
        auto_points_rules_challenge_verified = _get_auto_points_rules_text_for('challenge', True)
        auto_points_rules_challenge_unverified = _get_auto_points_rules_text_for('challenge', False)
        auto_points_rules_platformer_verified = _get_auto_points_rules_text_for('platformer', True)
        auto_points_rules_platformer_unverified = _get_auto_points_rules_text_for('platformer', False)
        announce_selected = _get_event_filter_set('discord_webhook_events')
        log_selected = _get_event_filter_set('discord_log_webhook_events')
        announce_known = set([k for k, _ in _ANNOUNCE_WEBHOOK_EVENT_CHOICES])
        log_known = set([k for k, _ in _LOG_WEBHOOK_EVENT_CHOICES])
        announce_custom = ','.join(sorted([v for v in announce_selected if v not in announce_known]))
        log_custom = ','.join(sorted([v for v in log_selected if v not in log_known]))
    else:
        announce_selected = set()
        log_selected = set()
        announce_custom = ''
        log_custom = ''

    graphs = {}
    try:
        sub_rows = db.session.query(db.func.date(LevelSubmission.submitted_at), db.func.count(LevelSubmission.id)).group_by(db.func.date(LevelSubmission.submitted_at)).order_by(db.func.date(LevelSubmission.submitted_at)).all()
        labels = []
        vals = []
        for d, c in sub_rows:
            if not d:
                continue
            labels.append(str(d))
            vals.append(int(c or 0))
        graphs['submissions'] = {'labels': labels, 'data': vals}
    except Exception:
        graphs['submissions'] = {'labels': [], 'data': []}

    try:
        rec_rows = db.session.query(db.func.date(Record.submitted_at), db.func.count(Record.id)).group_by(db.func.date(Record.submitted_at)).order_by(db.func.date(Record.submitted_at)).all()
        labels = []
        vals = []
        for d, c in rec_rows:
            if not d:
                continue
            labels.append(str(d))
            vals.append(int(c or 0))
        graphs['records'] = {'labels': labels, 'data': vals}
    except Exception:
        graphs['records'] = {'labels': [], 'data': []}

    try:
        lvl_rows = db.session.query(db.func.date(Level.created_at), db.func.count(Level.id)).filter(Level.created_at.isnot(None)).group_by(db.func.date(Level.created_at)).order_by(db.func.date(Level.created_at)).all()
        labels = []
        daily = []
        for d, c in lvl_rows:
            if not d:
                continue
            labels.append(str(d))
            daily.append(int(c or 0))
        cum = []
        run = 0
        for v in daily:
            run += v
            cum.append(run)
        graphs['levels_total'] = {'labels': labels, 'data': cum}
    except Exception:
        graphs['levels_total'] = {'labels': [], 'data': []}

    return render_template('admin.html', 
                          pending_records=pending_records, 
                          approved_records=approved_records, 
                          approved_pagination=approved_pagination,
                          approved_page=approved_page,
                          approved_per_page=approved_per_page,
                          pending_levels=pending_levels, 
                          levels=levels,
                          levels_sort=levels_sort,
                          users=users,
                          admin_q=q,
                          admin_type=t,
                          news_posts=news_posts,
                          webhook_url=webhook_url,
                          log_webhook_url=log_webhook_url,
                          site_launch_webhook_url=site_launch_webhook_url,
                          announce_event_choices=_ANNOUNCE_WEBHOOK_EVENT_CHOICES,
                          log_event_choices=_LOG_WEBHOOK_EVENT_CHOICES,
                          announce_events_selected=announce_selected,
                          log_events_selected=log_selected,
                          announce_events_custom=announce_custom,
                          log_events_custom=log_custom,
                          discord_client_id=discord_client_id,
                          discord_redirect_uri=discord_redirect_uri,
                          discord_client_secret_set=discord_client_secret_set,
                          captcha_provider=captcha_provider,
                          recaptcha_site_key=recaptcha_site_key,
                          recaptcha_secret_set=recaptcha_secret_set,
                          hcaptcha_site_key=hcaptcha_site_key,
                          hcaptcha_secret_set=hcaptcha_secret_set,
                          guidelines_url=guidelines_url,
                          hide_categories_on_main_list=hide_categories_on_main_list,
                          record_proof_top_n=record_proof_top_n,
                          record_proof_top_n_main=record_proof_top_n_main,
                          record_proof_top_n_challenge=record_proof_top_n_challenge,
                          record_proof_top_n_platformer=record_proof_top_n_platformer,
                          auto_points_enabled=auto_points_enabled,
                          auto_points_scope=auto_points_scope,
                          auto_points_rules_main_verified=auto_points_rules_main_verified,
                          auto_points_rules_main_unverified=auto_points_rules_main_unverified,
                          auto_points_rules_challenge_verified=auto_points_rules_challenge_verified,
                          auto_points_rules_challenge_unverified=auto_points_rules_challenge_unverified,
                          auto_points_rules_platformer_verified=auto_points_rules_platformer_verified,
                          auto_points_rules_platformer_unverified=auto_points_rules_platformer_unverified,
                          active_users_count=active_users_count,
                          total_users_count=total_users_count,
                          total_levels_count=total_levels_count,
                          graphs=graphs,
                          logs=logs,
                          api_logs=api_logs)

@app.route('/admin/toggle_mod/<int:user_id>')
@login_required
def toggle_mod(user_id):
    if _get_user_rank(current_user) != 'admin':
        abort(403)
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own role.', 'warning')
        return redirect(url_for('admin_dashboard'))
        
    user.is_mod = not bool(getattr(user, 'is_mod', False))
    if user.is_mod and not bool(getattr(user, 'is_admin', False)):
        user.rank = 'mod'
    elif not user.is_mod and (str(getattr(user, 'rank', '') or '').strip().lower() == 'mod'):
        user.rank = 'member'
    db.session.commit()
    status = "Mod" if user.is_mod else "User"
    add_site_log('admin_action', f'{current_user.username} set mod={user.is_mod} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} is now a {status}.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle_admin/<int:user_id>')
@login_required
def toggle_admin(user_id):
    if _get_user_rank(current_user) != 'admin':
        abort(403)
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own admin status.', 'warning')
        return redirect(url_for('admin_dashboard'))
    user.is_admin = not user.is_admin
    if user.is_admin:
        user.rank = 'admin'
    else:
        user.rank = 'mod' if bool(getattr(user, 'is_mod', False)) else 'member'
    db.session.commit()
    status = "Admin" if user.is_admin else ("Mod" if user.is_mod else "User")
    add_site_log('admin_action', f'{current_user.username} set admin={user.is_admin} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} is now {status}.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle_disabled/<int:user_id>')
@login_required
def toggle_disabled(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot disable your own account.', 'warning')
        return redirect(url_for('admin_dashboard'))
    user.is_disabled = not bool(getattr(user, 'is_disabled', False))
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} set disabled={bool(user.is_disabled)} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} is now {"disabled" if user.is_disabled else "enabled"}.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/set_rank/<int:user_id>', methods=['POST'])
@login_required
def admin_set_user_rank(user_id):
    if _get_user_rank(current_user) != 'admin':
        abort(403)
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own rank.', 'warning')
        return redirect(url_for('admin_dashboard'))
    rank = (request.form.get('rank') or '').strip().lower()
    if rank not in RANKS:
        flash('Invalid rank.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if rank == 'admin' and _get_user_rank(current_user) != 'admin':
        abort(403)
    user.rank = rank
    if rank == 'admin':
        user.is_admin = True
        user.is_mod = False
    elif rank in ('mod', 'elder_moderator'):
        user.is_admin = False
        user.is_mod = True
    else:
        user.is_admin = False
        user.is_mod = False
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} set rank={rank} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} rank updated to { _rank_label(rank) }.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/rank_permissions', methods=['POST'])
@login_required
def admin_set_rank_permissions():
    if _get_user_rank(current_user) != 'admin':
        abort(403)
    allowed = {k for k, _ in PERMISSIONS}
    out = {}
    for r in RANKS:
        if r == 'admin':
            continue
        picked = request.form.getlist(f'perms_{r}')
        cleaned = []
        for p in picked:
            key = str(p or '').strip()
            if key in allowed and key not in cleaned:
                cleaned.append(key)
        out[r] = cleaned
    _set_setting('rank_permissions', json.dumps(out))
    add_site_log('admin_action', f'{current_user.username} updated rank permissions.', actor_user_id=current_user.id)
    flash('Rank permissions updated.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/news/create', methods=['POST'])
@login_required
def admin_news_create():
    _require_perm('manage_news')
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    if not title or not body:
        flash('News title and body are required.', 'danger')
        return redirect(url_for('admin_dashboard'))
    post = NewsPost(title=title[:200], body=body)
    db.session.add(post)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} created news "{title[:100]}".', actor_user_id=current_user.id)
    flash('News posted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/news/<int:post_id>/delete', methods=['POST'])
@login_required
def admin_news_delete(post_id):
    _require_perm('manage_news')
    post = NewsPost.query.get_or_404(post_id)
    title = post.title
    db.session.delete(post)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted news "{(title or "")[:100]}".', actor_user_id=current_user.id)
    flash('News deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/page/<slug>')
def view_page(slug):
    s = (slug or '').strip()
    page = Page.query.filter(Page.slug == s).first()
    if not page:
        abort(404)
    if bool(getattr(page, 'is_html', False)):
        return Response(page.body or '', mimetype='text/html')
    return render_template('page.html', page=page)

@app.route('/admin/pages')
@login_required
def admin_pages():
    _require_perm('manage_pages')
    pages = Page.query.order_by(Page.updated_at.desc()).all()
    return render_template('admin_pages.html', pages=pages)

@app.route('/admin/pages/create', methods=['POST'])
@login_required
def admin_pages_create():
    _require_perm('manage_pages')
    slug = (request.form.get('slug') or '').strip()
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    is_html = request.form.get('is_html') == 'on'
    cleaned = ''.join(ch for ch in slug.lower() if ch.isalnum() or ch in ('-', '_'))
    if not cleaned or cleaned != slug.lower():
        flash('Slug must be lowercase and only use letters, numbers, - or _.', 'danger')
        return redirect(url_for('admin_pages'))
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin_pages'))
    existing = Page.query.filter(Page.slug == cleaned).first()
    if existing:
        flash('Slug already exists.', 'danger')
        return redirect(url_for('admin_pages'))
    now = datetime.utcnow()
    page = Page(slug=cleaned, title=title[:200], body=body, is_html=is_html, created_at=now, updated_at=now)
    db.session.add(page)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} created page "{cleaned}".', actor_user_id=current_user.id)
    flash('Page created.', 'success')
    return redirect(url_for('admin_pages'))

@app.route('/admin/pages/<int:page_id>/update', methods=['POST'])
@login_required
def admin_pages_update(page_id):
    _require_perm('manage_pages')
    page = Page.query.get_or_404(page_id)
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    is_html = request.form.get('is_html') == 'on'
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin_pages'))
    page.title = title[:200]
    page.body = body
    page.is_html = is_html
    page.updated_at = datetime.utcnow()
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} updated page "{page.slug}".', actor_user_id=current_user.id)
    flash('Page updated.', 'success')
    return redirect(url_for('admin_pages'))

@app.route('/admin/pages/<int:page_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_pages_edit(page_id):
    _require_perm('manage_pages')
    page = Page.query.get_or_404(page_id)
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        body = (request.form.get('body') or '').strip()
        is_html = request.form.get('is_html') == 'on'
        if not title or not body:
            flash('Title and body are required.', 'danger')
            return redirect(url_for('admin_pages_edit', page_id=page_id))
        page.title = title[:200]
        page.body = body
        page.is_html = is_html
        page.updated_at = datetime.utcnow()
        db.session.commit()
        add_site_log('admin_action', f'{current_user.username} updated page "{page.slug}".', actor_user_id=current_user.id)
        flash('Page updated.', 'success')
        return redirect(url_for('admin_pages_edit', page_id=page_id))
    return render_template('admin_page_edit.html', page=page)

@app.route('/admin/pages/<int:page_id>/delete', methods=['POST'])
@login_required
def admin_pages_delete(page_id):
    _require_perm('manage_pages')
    page = Page.query.get_or_404(page_id)
    slug = page.slug
    db.session.delete(page)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted page "{slug}".', actor_user_id=current_user.id)
    flash('Page deleted.', 'success')
    return redirect(url_for('admin_pages'))

@app.route('/admin/pages/<int:page_id>/delete', methods=['GET'])
@login_required
def admin_pages_delete_confirm(page_id):
    _require_perm('manage_pages')
    page = Page.query.get_or_404(page_id)
    return render_template('admin_page_delete.html', page=page)

@app.route('/events')
def events_list():
    events = Event.query.filter_by(is_published=True).order_by(Event.starts_at.desc().nullslast(), Event.created_at.desc()).all()
    return render_template('events.html', events=events)

@app.route('/event/<int:event_id>')
def event_detail(event_id):
    event = Event.query.get_or_404(event_id)
    if not bool(getattr(event, 'is_published', True)) and not _has_perm(current_user, 'manage_events'):
        abort(404)
    return render_template('event.html', event=event)

@app.route('/admin/events')
@login_required
def admin_events():
    _require_perm('manage_events')
    events = Event.query.order_by(Event.created_at.desc()).all()
    return render_template('admin_events.html', events=events)

def _parse_dt_local(raw: str):
    s = (raw or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

@app.route('/admin/events/create', methods=['POST'])
@login_required
def admin_events_create():
    _require_perm('manage_events')
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin_events'))
    starts_at = _parse_dt_local(request.form.get('starts_at') or '')
    ends_at = _parse_dt_local(request.form.get('ends_at') or '')
    is_published = request.form.get('is_published') == 'on'
    ev = Event(title=title[:200], body=body, starts_at=starts_at, ends_at=ends_at, is_published=is_published, created_at=datetime.utcnow())
    db.session.add(ev)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} created event "{title[:100]}".', actor_user_id=current_user.id)
    flash('Event created.', 'success')
    return redirect(url_for('admin_events'))

@app.route('/admin/events/<int:event_id>/update', methods=['POST'])
@login_required
def admin_events_update(event_id):
    _require_perm('manage_events')
    ev = Event.query.get_or_404(event_id)
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin_events'))
    ev.title = title[:200]
    ev.body = body
    ev.starts_at = _parse_dt_local(request.form.get('starts_at') or '')
    ev.ends_at = _parse_dt_local(request.form.get('ends_at') or '')
    ev.is_published = request.form.get('is_published') == 'on'
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} updated event #{ev.id}.', actor_user_id=current_user.id)
    flash('Event updated.', 'success')
    return redirect(url_for('admin_events'))

@app.route('/admin/events/<int:event_id>/delete', methods=['POST'])
@login_required
def admin_events_delete(event_id):
    _require_perm('manage_events')
    ev = Event.query.get_or_404(event_id)
    title = ev.title
    db.session.delete(ev)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted event "{(title or "")[:100]}".', actor_user_id=current_user.id)
    flash('Event deleted.', 'success')
    return redirect(url_for('admin_events'))

@app.route('/admin/settings/webhook', methods=['POST'])
@login_required
def admin_set_webhook():
    _require_perm('manage_settings')
    url = (request.form.get('webhook_url') or '').strip()
    _set_setting('discord_webhook_url', url)
    flash('Webhook updated.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/webhook/test', methods=['POST'])
@login_required
def admin_test_webhook():
    _require_perm('manage_settings')
    url = (_get_setting('discord_webhook_url') or WEBHOOK_URL or '').strip()
    if not url:
        flash('Webhook is not set.', 'danger')
        return redirect(url_for('admin_dashboard'))
    ok, err = _post_discord_webhook(url, f'Test message from admin panel by {current_user.username}.')
    if ok:
        add_site_log('admin_action', f'Main webhook test sent by {current_user.username}.', actor_user_id=current_user.id)
        flash('Test message sent.', 'success')
    else:
        add_site_log('admin_action', f'Main webhook test failed: {err}', actor_user_id=current_user.id, send_to_discord=False)
        flash(f'Test failed: {err}', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/webhook/events', methods=['POST'])
@login_required
def admin_set_webhook_events():
    _require_perm('manage_settings')
    announce = [str(v or '').strip().lower() for v in request.form.getlist('announce_events')]
    announce_custom = (request.form.get('announce_events_custom') or '').strip()
    log = [str(v or '').strip().lower() for v in request.form.getlist('log_events')]
    log_custom = (request.form.get('log_events_custom') or '').strip()

    announce_extra = _parse_event_filter_text(announce_custom)
    log_extra = _parse_event_filter_text(log_custom)

    _save_event_filter('discord_webhook_events', (announce or []) + (announce_extra or []))
    _save_event_filter('discord_log_webhook_events', (log or []) + (log_extra or []))

    flash('Webhook event settings updated.', 'success')
    add_site_log('admin_action', f'{current_user.username} updated webhook event settings.', actor_user_id=current_user.id, send_to_discord=False)
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/log_webhook', methods=['POST'])
@login_required
def admin_set_log_webhook():
    _require_perm('manage_settings')
    url = (request.form.get('log_webhook_url') or '').strip()
    _set_setting('discord_logs_webhook_url', url)
    flash('Log webhook updated.', 'success')
    add_site_log('admin_action', f'Log webhook updated by {current_user.username}.', actor_user_id=current_user.id, send_to_discord=False)
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/site_launch_webhook', methods=['POST'])
@login_required
def admin_set_site_launch_webhook():
    _require_perm('manage_settings')
    url = (request.form.get('site_launch_webhook_url') or '').strip()
    _set_setting('discord_site_launch_webhook_url', url)
    flash('Site launch webhook updated.', 'success')
    add_site_log('admin_action', f'Site launch webhook updated by {current_user.username}.', actor_user_id=current_user.id, send_to_discord=False)
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/site_launch_webhook/test', methods=['POST'])
@login_required
def admin_test_site_launch_webhook():
    _require_perm('manage_settings')
    url = (_get_setting('discord_site_launch_webhook_url') or '').strip()
    if not url:
        flash('Site launch webhook is not set.', 'danger')
        return redirect(url_for('admin_dashboard'))
    payload = {
        "embeds": [
            {
                "title": "Site launch test",
                "description": f"Test message from admin panel by {current_user.username}.",
                "color": 0x22C55E,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        ]
    }
    ok, err = _post_discord_webhook_payload(url, payload)
    if ok:
        add_site_log('admin_action', f'Site launch webhook test sent by {current_user.username}.', actor_user_id=current_user.id, send_to_discord=False)
        flash('Site launch test sent.', 'success')
    else:
        add_site_log('admin_action', f'Site launch webhook test failed: {err}', actor_user_id=current_user.id, send_to_discord=False)
        flash(f'Site launch test failed: {err}', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/log_webhook/test', methods=['POST'])
@login_required
def admin_test_log_webhook():
    _require_perm('manage_settings')
    raw = (_get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL or '').strip()
    fallback = (_get_setting('discord_webhook_url') or WEBHOOK_URL or '').strip()
    url = raw or fallback
    if not url:
        flash('Logs webhook is not set.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if not _event_allowed('discord_log_webhook_events', 'test'):
        flash('Log test is blocked by webhook event settings.', 'warning')
        return redirect(url_for('admin_dashboard'))
    payload = _discord_log_payload('test', f'Test log message from admin panel by {current_user.username}.', actor_user_id=current_user.id)
    ok, err = _post_discord_webhook_payload(url, payload)
    if ok:
        add_site_log('test', f'Test log message from admin panel by {current_user.username}.', actor_user_id=current_user.id, send_to_discord=False)
        flash('Test log sent.', 'success')
    else:
        add_site_log('test', f'Log webhook test failed: {err}', actor_user_id=current_user.id, send_to_discord=False)
        flash(f'Log test failed: {err}', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/discord_oauth', methods=['POST'])
@login_required
def admin_set_discord_oauth():
    _require_perm('manage_settings')
    client_id = (request.form.get('client_id') or '').strip()
    redirect_uri = (request.form.get('redirect_uri') or '').strip()
    client_secret = (request.form.get('client_secret') or '').strip()
    _set_setting('discord_client_id', client_id)
    _set_setting('discord_redirect_uri', redirect_uri)
    if client_secret:
        _set_setting('discord_client_secret', client_secret)
    elif not client_id and not redirect_uri:
        _set_setting('discord_client_secret', '')
    add_site_log('admin_action', f'{current_user.username} updated Discord OAuth settings.', actor_user_id=current_user.id, send_to_discord=False)
    flash('Discord OAuth settings updated.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/guidelines', methods=['POST'])
@login_required
def admin_set_guidelines():
    _require_perm('manage_settings')
    url = (request.form.get('guidelines_url') or '').strip()
    _set_setting('guidelines_url', url)
    _set_setting('guidelines_reset_ts', str(int(time.time())))
    add_site_log('admin_action', f'{current_user.username} updated guidelines URL and required re-acceptance.', actor_user_id=current_user.id, send_to_discord=False)
    flash('Guidelines updated. Everyone must accept again.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/recaptcha', methods=['POST'])
@login_required
def admin_set_recaptcha():
    _require_perm('manage_settings')
    provider = (request.form.get('captcha_provider') or '').strip().lower()
    if provider not in ('', 'recaptcha', 'hcaptcha'):
        provider = ''
    _set_setting('captcha_provider', provider)

    recaptcha_site_key = (request.form.get('recaptcha_site_key') or request.form.get('site_key') or '').strip()
    recaptcha_secret_key = (request.form.get('recaptcha_secret_key') or request.form.get('secret_key') or '').strip()
    _set_setting('recaptcha_site_key', recaptcha_site_key)
    if recaptcha_secret_key:
        _set_setting('recaptcha_secret_key', recaptcha_secret_key)
    elif not recaptcha_site_key:
        _set_setting('recaptcha_secret_key', '')

    hcaptcha_site_key = (request.form.get('hcaptcha_site_key') or '').strip()
    hcaptcha_secret_key = (request.form.get('hcaptcha_secret_key') or '').strip()
    _set_setting('hcaptcha_site_key', hcaptcha_site_key)
    if hcaptcha_secret_key:
        _set_setting('hcaptcha_secret_key', hcaptcha_secret_key)
    elif not hcaptcha_site_key:
        _set_setting('hcaptcha_secret_key', '')

    flash('CAPTCHA settings updated.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/ui', methods=['POST'])
@login_required
def admin_set_ui_settings():
    _require_perm('manage_settings')
    hide_categories_on_main_list = '1' if request.form.get('hide_categories_on_main_list') == 'on' else '0'
    _set_setting('hide_categories_on_main_list', hide_categories_on_main_list)
    year_recap_enabled = '1' if request.form.get('year_recap_enabled') == 'on' else '0'
    _set_setting('year_recap_enabled', year_recap_enabled)
    april_fools_enabled = '1' if request.form.get('april_fools_enabled') == 'on' else '0'
    _set_setting('april_fools_enabled', april_fools_enabled)
    footer_discord_url = (request.form.get('footer_discord_url') or '').strip()
    _set_setting('footer_discord_url', footer_discord_url)
    def _parse_top_n(form_key: str, default: int) -> int:
        raw = str(request.form.get(form_key) or '').strip()
        try:
            n = int(float(raw))
        except Exception:
            n = default
        if n < 0:
            n = 0
        if n > 10000:
            n = 10000
        return int(n)
    n_main = _parse_top_n('record_proof_top_n_main', _record_proof_top_n_for_list('main'))
    n_chall = _parse_top_n('record_proof_top_n_challenge', _record_proof_top_n_for_list('challenge'))
    n_plat = _parse_top_n('record_proof_top_n_platformer', _record_proof_top_n_for_list('platformer'))
    _set_setting('record_proof_top_n_main', str(n_main))
    _set_setting('record_proof_top_n_challenge', str(n_chall))
    _set_setting('record_proof_top_n_platformer', str(n_plat))
    _set_setting('record_proof_top_n', str(n_main))
    flash('UI settings updated.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/maintenance', methods=['POST'])
@login_required
def admin_maintenance():
    _require_perm('manage_settings')
    action = (request.form.get('action') or '').strip().lower()
    if action == 'recalc_points':
        try:
            _recalc_all_user_points()
            add_site_log('admin_action', f'{current_user.username} recalculated all user points.', actor_user_id=current_user.id)
            flash('Recalculated all user points.', 'success')
        except Exception:
            flash('Failed to recalculate user points.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if action == 'ensure_schema':
        try:
            ensure_schema()
            add_site_log('admin_action', f'{current_user.username} ran ensure_schema.', actor_user_id=current_user.id)
            flash('Schema check complete.', 'success')
        except Exception:
            flash('Schema check failed.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if action == 'expire_pending':
        try:
            days = int(float(str(request.form.get('days') or '0').strip() or '0'))
        except Exception:
            days = 0
        if days <= 0:
            flash('Days must be greater than 0.', 'danger')
            return redirect(url_for('admin_dashboard'))
        cutoff = datetime.utcnow() - timedelta(days=days)
        expire_records = request.form.get('expire_records') == 'on'
        expire_submissions = request.form.get('expire_submissions') == 'on'
        did = []
        try:
            if expire_records:
                n = (
                    db.session.query(Record)
                    .filter(Record.status == 'pending', Record.submitted_at.isnot(None), Record.submitted_at < cutoff)
                    .update({Record.status: 'rejected'}, synchronize_session=False)
                )
                did.append(f'{int(n or 0)} pending records')
            if expire_submissions:
                n = (
                    db.session.query(LevelSubmission)
                    .filter(LevelSubmission.status == 'pending', LevelSubmission.submitted_at.isnot(None), LevelSubmission.submitted_at < cutoff)
                    .update({LevelSubmission.status: 'rejected'}, synchronize_session=False)
                )
                did.append(f'{int(n or 0)} pending submissions')
            if did:
                db.session.commit()
                add_site_log('admin_action', f'{current_user.username} expired {", ".join(did)} older than {days} days.', actor_user_id=current_user.id)
                flash(f'Expired {", ".join(did)} older than {days} days.', 'success')
            else:
                flash('Select at least one thing to expire.', 'warning')
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash('Failed to expire pending items.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if action == 'clear_proof_range':
        try:
            from_str = request.form.get('from_date')
            to_str = request.form.get('to_date')
            if not from_str or not to_str:
                flash('Both dates are required.', 'danger')
                return redirect(url_for('admin_dashboard'))
            
            from_dt = datetime.strptime(from_str, '%Y-%m-%d')
            to_dt = datetime.strptime(to_str, '%Y-%m-%d') + timedelta(days=1) # inclusive of the to_date
            
            n = (
                db.session.query(Record)
                .filter(Record.submitted_at >= from_dt, Record.submitted_at < to_dt)
                .update({Record.video_url: ''}, synchronize_session=False)
            )
            db.session.commit()
            add_site_log('admin_action', f'{current_user.username} cleared video URLs for {int(n or 0)} records between {from_str} and {to_str}.', actor_user_id=current_user.id)
            flash(f'Cleared video URLs for {int(n or 0)} record(s).', 'success')
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash(f'Failed to clear proof: {str(e)}', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    if action == 'prune_logs':
        try:
            days = int(float(str(request.form.get('log_days') or '0').strip() or '0'))
        except Exception:
            days = 0
        if days <= 0:
            flash('Days must be greater than 0.', 'danger')
            return redirect(url_for('admin_dashboard'))
        cutoff = datetime.utcnow() - timedelta(days=days)
        try:
            n = db.session.query(SiteLog).filter(SiteLog.created_at.isnot(None), SiteLog.created_at < cutoff).delete(synchronize_session=False)
            db.session.commit()
            add_site_log('admin_action', f'{current_user.username} pruned {int(n or 0)} logs older than {days} days.', actor_user_id=current_user.id)
            flash(f'Pruned {int(n or 0)} logs older than {days} days.', 'success')
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash('Failed to prune logs.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if action == 'backup_now':
        try:
            dst_dir = os.path.join(app.root_path, 'backups')
            os.makedirs(dst_dir, exist_ok=True)

            uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            if uri.startswith('sqlite:///'):
                candidate = uri.replace('sqlite:///', '', 1)
                src = candidate if os.path.isabs(candidate) else os.path.join(app.instance_path, candidate)
            else:
                src = os.path.join(app.instance_path, 'database.db')

            if not os.path.exists(src):
                flash('Database file not found.', 'danger')
                return redirect(url_for('admin_dashboard'))

            ts = datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')
            nonce = uuid.uuid4().hex[:8]
            dst = os.path.join(dst_dir, f'manual-{ts}-{nonce}.db')
            shutil.copy2(src, dst)
            add_site_log('admin_action', f'{current_user.username} created a manual backup ({os.path.basename(dst)}).', actor_user_id=current_user.id, send_to_discord=False)
            flash('Backup created.', 'success')
        except Exception:
            flash('Failed to create backup.', 'danger')
        return redirect(url_for('admin_dashboard'))
    flash('Unknown maintenance action.', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
def reset_password(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.is_admin and _get_user_rank(current_user) != 'admin':
        flash('Cannot reset password for admins via panel.', 'warning')
        return redirect(url_for('admin_dashboard'))
    new_password = _trim_pw(request.form.get('new_password', '').strip())
    if not new_password:
        flash('New password cannot be empty.', 'danger')
        return redirect(url_for('admin_dashboard'))
    hashed = bcrypt.generate_password_hash(new_password).decode('utf-8')
    user.password = hashed
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} reset password for {user.username}.', actor_user_id=current_user.id)
    flash(f'Password reset for {user.username}.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_user_points/<int:user_id>', methods=['POST'])
@login_required
def update_user_points(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    old_points = float(user.points or 0)
    try:
        points = float(request.form.get('points', user.points))
    except ValueError:
        flash('Invalid points value.', 'danger')
        return redirect(url_for('admin_dashboard'))
    user.points = max(0.0, points)
    db.session.commit()
    add_site_log('points_change', f'{current_user.username} changed points for {user.username}: {old_points} -> {user.points}.', actor_user_id=current_user.id)
    flash(f'Points updated for {user.username}.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Cannot delete your own account from admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    uname = user.username
    db.session.delete(user)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted user {uname}.', actor_user_id=current_user.id)
    flash(f'User {user.username} deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_level_order', methods=['POST'])
@login_required
def update_level_order():
    _require_perm('move_levels')
        
    level_id = request.form.get('level_id')
    new_position = request.form.get('position')
    
    level = Level.query.get_or_404(level_id)
    level_name = level.name
    level_author = level.creator
    try:
        if not new_position:
            level.position = None
            affected = Level.query.filter(
                Level.list_type == level.list_type,
                Level.is_verified == bool(level.is_verified),
                Level.id != level.id,
                Level.position.isnot(None)
            ).order_by(Level.position).all()
            for idx, lv in enumerate(affected, start=1):
                lv.position = idx
            did_auto_points = False
            if _auto_points_applies_to(level.list_type):
                rules = _parse_auto_points_rules(_get_auto_points_rules_text_for(level.list_type, bool(level.is_verified)))
                for lv in affected:
                    lv.points_reward = _points_for_position(int(lv.position or 0), rules)
                did_auto_points = True
            db.session.commit()
            if did_auto_points:
                _recalc_all_user_points()
            try:
                vlabel = 'verified' if bool(level.is_verified) else 'unverified'
                lt = (level.list_type or 'main')
            except Exception:
                vlabel = 'unknown'
                lt = 'main'
            add_site_log('level_move', f'{current_user.username} cleared position for "{level_name}" by {level_author} ({lt}, {vlabel}).', actor_user_id=current_user.id)
            flash('Level removed from ordered list.', 'success')
        else:
            new_pos = int(new_position)
            # Work within same list_type and verification status
            affected = Level.query.filter(
                Level.list_type == level.list_type,
                Level.is_verified == bool(level.is_verified),
                Level.id != level.id,
                Level.position.isnot(None)
            ).order_by(Level.position).all()
            # Build ordered list and insert at new_pos
            ordered = affected
            # Clamp new_pos to range 1..len(affected)+1
            if new_pos < 1:
                new_pos = 1
            if new_pos > len(affected) + 1:
                new_pos = len(affected) + 1
            ordered.insert(new_pos - 1, level)
            # Reassign positions
            for idx, lv in enumerate(ordered, start=1):
                lv.position = idx
            did_auto_points = False
            if _auto_points_applies_to(level.list_type):
                rules = _parse_auto_points_rules(_get_auto_points_rules_text_for(level.list_type, bool(level.is_verified)))
                for lv in ordered:
                    lv.points_reward = _points_for_position(int(lv.position or 0), rules)
                did_auto_points = True
            db.session.commit()
            if did_auto_points:
                _recalc_all_user_points()
            send_discord_message(f'"{level_name}" by {level_author} was moved to place {new_pos} by {current_user.username}', event_type='level_moved')
            add_site_log('level_move', f'{current_user.username} moved "{level_name}" by {level_author} to place {new_pos}.', actor_user_id=current_user.id)
            flash('Level position updated with list reordering.', 'success')
    except ValueError:
        flash('Invalid position value.', 'danger')
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/settings/auto_points', methods=['POST'])
@login_required
def admin_set_auto_points():
    _require_perm('manage_settings')
    enabled = '1' if request.form.get('auto_points_enabled') == 'on' else '0'
    _set_setting('auto_points_enabled', enabled)
    scope = (request.form.get('auto_points_scope') or '').strip().lower()
    if scope not in ('main', 'challenge', 'platformer', 'both', 'all'):
        scope = 'both'
    _set_setting('auto_points_scope', scope)
    main_verified = (request.form.get('auto_points_rules_main_verified') or '').strip()
    main_unverified = (request.form.get('auto_points_rules_main_unverified') or '').strip()
    chall_verified = (request.form.get('auto_points_rules_challenge_verified') or '').strip()
    chall_unverified = (request.form.get('auto_points_rules_challenge_unverified') or '').strip()
    plat_verified = (request.form.get('auto_points_rules_platformer_verified') or '').strip()
    plat_unverified = (request.form.get('auto_points_rules_platformer_unverified') or '').strip()
    _set_setting(_auto_points_rules_key('main', True), main_verified)
    _set_setting(_auto_points_rules_key('main', False), main_unverified)
    _set_setting(_auto_points_rules_key('challenge', True), chall_verified)
    _set_setting(_auto_points_rules_key('challenge', False), chall_unverified)
    _set_setting(_auto_points_rules_key('platformer', True), plat_verified)
    _set_setting(_auto_points_rules_key('platformer', False), plat_unverified)
    _set_setting('auto_points_rules', main_verified)
    if request.form.get('recalc_now') == 'on' and _auto_points_enabled():
        _auto_points_recalc_all()
    add_site_log('admin_action', f'{current_user.username} updated auto points settings.', actor_user_id=current_user.id, send_to_discord=False)
    flash('Auto points settings updated.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/approve_record/<int:record_id>')
@login_required
def approve_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    old_status = record.status
    record.status = 'approved'
    level = db.session.get(Level, record.level_id)
    old_points = float(record.user.points or 0)
    if old_status != 'approved' and level and level.points_reward:
        record.user.points += level.points_reward
        
    db.session.commit()
    try:
        _recalc_user_points_for_users([record.user_id])
    except Exception:
        pass
    if old_status != 'approved' and level:
        proof = (record.video_url or '').strip()
        if not proof:
            try:
                fn = str(getattr(record, 'proof_image_filename', '') or '').strip()
                if fn:
                    proof = url_for('static', filename=f'record_proofs/{fn}', _external=True)
            except Exception:
                proof = ''
        send_discord_message(f'Proof approved by {current_user.username} for "{level.name}" by {level.creator}. Proof: {proof}', event_type='record_approved')
        add_site_log('points_change', f'{current_user.username} approved record for {record.user.username} on "{level.name}". Points: {old_points} -> {record.user.points}.', actor_user_id=current_user.id)
    flash('Record approved', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/approve_records_bulk', methods=['POST'])
@login_required
def bulk_approve_records():
    _require_perm('moderate_records')
    action = (request.form.get('bulk_action') or 'approve').strip().lower()
    raw_ids = request.form.getlist('record_ids')
    ids = []
    for r in (raw_ids or []):
        try:
            v = int(r)
        except Exception:
            continue
        if v > 0 and v not in ids:
            ids.append(v)
    if not ids:
        flash('No records selected.', 'warning')
        return redirect(url_for('admin_dashboard'))

    records = Record.query.filter(Record.id.in_(ids)).all()
    affected_user_ids = set()
    if action == 'approve':
        changed = 0
        for rec in records:
            if rec.status != 'approved':
                rec.status = 'approved'
                changed += 1
            try:
                affected_user_ids.add(int(rec.user_id))
            except Exception:
                pass
        db.session.commit()
        try:
            _recalc_user_points_for_users(sorted(list(affected_user_ids)))
        except Exception:
            pass
        add_site_log('admin_action', f'{current_user.username} bulk approved {changed} records.', actor_user_id=current_user.id)
        flash(f'Approved {changed} record(s).', 'success')
        return redirect(url_for('admin_dashboard'))

    if action == 'reject':
        changed = 0
        for rec in records:
            if rec.status != 'rejected':
                rec.status = 'rejected'
                changed += 1
            try:
                affected_user_ids.add(int(rec.user_id))
            except Exception:
                pass
        db.session.commit()
        try:
            _recalc_user_points_for_users(sorted(list(affected_user_ids)))
        except Exception:
            pass
        add_site_log('admin_action', f'{current_user.username} bulk rejected {changed} records.', actor_user_id=current_user.id)
        flash(f'Rejected {changed} record(s).', 'success')
        return redirect(url_for('admin_dashboard'))

    if action == 'clear_video':
        cleared = 0
        for rec in records:
            if (rec.video_url or ''):
                rec.video_url = ''
                cleared += 1
        db.session.commit()
        add_site_log('admin_action', f'{current_user.username} bulk cleared video for {cleared} records.', actor_user_id=current_user.id)
        flash(f'Cleared video for {cleared} record(s).', 'success')
        return redirect(url_for('admin_dashboard'))
    
    if action == 'delete':
        deleted = 0
        for rec in records:
            try:
                affected_user_ids.add(int(rec.user_id))
            except Exception:
                pass
            db.session.delete(rec)
            deleted += 1
        db.session.commit()
        try:
            _recalc_user_points_for_users(sorted(list(affected_user_ids)))
        except Exception:
            pass
        add_site_log('admin_action', f'{current_user.username} bulk deleted {deleted} records.', actor_user_id=current_user.id)
        flash(f'Deleted {deleted} record(s).', 'success')
        return redirect(url_for('admin_dashboard'))

    flash('Invalid bulk action.', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/clear_record_video/<int:record_id>')
@login_required
def clear_record_video(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    level = db.session.get(Level, record.level_id)
    level_name = level.name if level else ''
    record.video_url = ''
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} cleared record video for {record.user.username} on "{level_name or "level"}".', actor_user_id=current_user.id)
    flash('Record video cleared.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/edit_record/<int:record_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    if request.method == 'POST':
        old_status = record.status
        record.device = (request.form.get('device') or 'pc').strip().lower()
        record.status = (request.form.get('status') or 'pending').strip().lower()
        record.video_url = (request.form.get('video_url') or '').strip()
        record.comment = (request.form.get('comment') or '').strip()
        record.beaten_on_vc = bool(request.form.get('beaten_on_vc'))
        
        db.session.commit()
        
        # If status changed, recalc points
        if record.status != old_status or record.status == 'approved':
            try:
                _recalc_user_points_for_users([int(record.user_id)])
            except Exception:
                pass
        
        add_site_log('admin_action', f'{current_user.username} edited record {record.id} for {record.user.username}.', actor_user_id=current_user.id)
        flash('Record updated.', 'success')
        return redirect(url_for('admin_dashboard'))
    
    return render_template('admin_edit_record.html', record=record)

@app.route('/admin/reject_record/<int:record_id>')
@login_required
def reject_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    record.status = 'rejected'
    db.session.commit()
    level = db.session.get(Level, record.level_id)
    add_site_log('admin_action', f'{current_user.username} rejected record for {record.user.username} on "{(level.name if level else "") or "level"}".', actor_user_id=current_user.id)
    flash('Record rejected', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/approve_level_submission/<int:sub_id>')
@login_required
def approve_level_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = LevelSubmission.query.get_or_404(sub_id)
    if sub.status != 'approved':
        sub.status = 'approved'
        lt = (getattr(sub, 'list_type', '') or 'main').strip().lower()
        if lt == 'challange':
            lt = 'challenge'
        if lt not in ('main', 'challenge', 'platformer'):
            lt = 'main'
        # Build creator string with collaborators if provided
        creator = (sub.creator or '').strip()
        collab = (sub.collaborators or '').strip()
        if collab:
            # Normalize comma-separated list
            extra = [s.strip() for s in collab.split(',') if s.strip()]
            if extra:
                creator = ', '.join([creator] + extra) if creator else ', '.join(extra)
        video_url = (sub.verified_video_url or sub.video_url or '').strip()
        is_verified = bool(getattr(sub, 'is_verified', False))
        new_level = Level(
            name=sub.name,
            creator=creator or sub.creator,
            video_url=video_url or sub.video_url,
            is_verified=is_verified,
            list_type=lt
        )
        # Copy optional metadata if present
        if getattr(sub, 'gd_level_id', None):
            new_level.gd_level_id = sub.gd_level_id
        if getattr(sub, 'proposed_position', None):
            try:
                pos = int(sub.proposed_position)
                if pos > 0:
                    new_level.position = pos
            except Exception:
                pass
        if getattr(sub, 'thumbnail_filename', None):
            new_level.thumbnail_filename = sub.thumbnail_filename
        db.session.add(new_level)
        db.session.commit()
        send_discord_message(f'Level submission accepted: "{new_level.name}" by {new_level.creator}, approved by {current_user.username}. Level showcase: {new_level.video_url}', event_type='level_submission_approved')
        add_site_log('admin_action', f'{current_user.username} approved level submission "{new_level.name}" by {new_level.creator}.', actor_user_id=current_user.id)
        msg = 'Level submission approved and added to list' + (' as verified' if is_verified else ' (unverified)')
        flash(msg, 'success')
    else:
        flash('Submission already approved.', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/reject_level_submission/<int:sub_id>')
@login_required
def reject_level_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = LevelSubmission.query.get_or_404(sub_id)
    sub.status = 'rejected'
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} rejected level submission "{sub.name}" by {sub.creator}.', actor_user_id=current_user.id)
    flash('Level submission rejected', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_record/<int:record_id>')
@login_required
def delete_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    
    # If approved, remove points from user
    old_points = float(record.user.points or 0)
    level_name = ''
    if record.status == 'approved':
        level = db.session.get(Level, record.level_id)
        if level:
            level_name = level.name
        if level and level.points_reward:
            record.user.points -= level.points_reward
            if record.user.points < 0: record.user.points = 0
            
    uname = record.user.username
    user_id = int(record.user_id)
    new_points = float(record.user.points or 0)
    db.session.delete(record)
    db.session.commit()
    try:
        _recalc_user_points_for_users([user_id])
        new_points = float(User.query.get(user_id).points or 0)
    except Exception:
        pass
    add_site_log('admin_action', f'{current_user.username} deleted record #{record_id} ({level_name or "level"}) for {uname}. Points: {old_points} -> {new_points}.', actor_user_id=current_user.id)
    flash('Record deleted', 'success')
    return redirect(url_for('admin_dashboard'))

# Simple database editor (admin only)
@app.route('/admin/db')
@login_required
def admin_db():
    _require_perm('admin_db')
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'SongSubmission': SongSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    models = []
    for name, model in sorted(model_map.items(), key=lambda x: x[0].lower()):
        try:
            count = int(model.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(model, '__tablename__', name.lower())})
    return render_template('admin_db.html', models=models, selected=None, active_tab='structure', db_server='localhost', db_name='site')

@app.route('/admin/db/<model_name>')
@login_required
def admin_db_table(model_name):
    _require_perm('admin_db')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 25))
    if per_page < 5:
        per_page = 5
    if per_page > 200:
        per_page = 200
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    q = (request.args.get('q') or '').strip()
    tab = (request.args.get('tab') or 'browse').strip().lower()
    if tab not in ('browse', 'search'):
        tab = 'browse'
    query = model.query
    if q:
        from sqlalchemy import or_
        cols = []
        try:
            cols = [c.name for c in model.__table__.columns]
        except Exception:
            cols = []
        filt = []
        for c in cols:
            try:
                col = getattr(model, c)
            except Exception:
                continue
            try:
                if str(getattr(col.type, 'python_type', '')).endswith('str') or 'CHAR' in str(col.type).upper() or 'TEXT' in str(col.type).upper():
                    filt.append(col.ilike(f'%{q}%'))
            except Exception:
                continue
        if filt:
            query = query.filter(or_(*filt))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    rows = pagination.items
    columns = [c.name for c in model.__table__.columns]
    rows_data = []
    for r in rows:
        rows_data.append({col: getattr(r, col) for col in columns})
    models = []
    for name, m in sorted(model_map.items(), key=lambda x: x[0].lower()):
        try:
            count = int(m.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(m, '__tablename__', name.lower())})
    return render_template('admin_db.html', selected=model_name, columns=columns, rows=rows_data, pagination=pagination, models=models, active_tab=tab, q=q, per_page=per_page, db_server='localhost', db_name='site')

@app.route('/admin/db/<model_name>/structure')
@login_required
def admin_db_structure(model_name):
    _require_perm('admin_db')
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    models = []
    for name, m in sorted(model_map.items(), key=lambda x: x[0].lower()):
        try:
            count = int(m.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(m, '__tablename__', name.lower())})
    cols = []
    try:
        for c in model.__table__.columns:
            cols.append({
                'name': c.name,
                'type': str(c.type),
                'nullable': bool(c.nullable),
                'primary_key': bool(c.primary_key),
                'default': str(c.default.arg) if getattr(c, 'default', None) is not None and getattr(c.default, 'arg', None) is not None else '',
            })
    except Exception:
        cols = []
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='structure', structure_cols=cols, db_server='localhost', db_name='site')

@app.route('/admin/db/<model_name>/export')
@login_required
def admin_db_export(model_name):
    _require_perm('admin_db')
    fmt = (request.args.get('fmt') or 'csv').strip().lower()
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    limit = int(request.args.get('limit', 20000))
    if limit < 1:
        limit = 1
    if limit > 200000:
        limit = 200000
    cols = [c.name for c in model.__table__.columns]
    rows = model.query.limit(limit).all()
    if fmt == 'json':
        out = []
        for r in rows:
            out.append({c: getattr(r, c) for c in cols})
        resp = jsonify({'table': model_name, 'rows': out})
        resp.headers['Content-Disposition'] = f'attachment; filename="{model_name}.json"'
        return resp
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([getattr(r, c) for c in cols])
    data = buf.getvalue()
    return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename="{model_name}.csv"'})

@app.route('/admin/db/<model_name>/sql', methods=['GET', 'POST'])
@login_required
def admin_db_sql(model_name):
    _require_perm('admin_db')
    from sqlalchemy import text
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    models = []
    for name, m in sorted(model_map.items(), key=lambda x: x[0].lower()):
        try:
            count = int(m.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(m, '__tablename__', name.lower())})
    sql_text = ''
    result_cols = []
    result_rows = []
    error = ''
    if request.method == 'POST':
        sql_text = (request.form.get('sql') or '').strip()
        s = sql_text.strip()
        low = s.lower()
        banned = ['insert', 'update', 'delete', 'drop', 'alter', 'create', 'replace', 'vacuum', 'attach', 'detach', 'pragma writable_schema']
        is_ok = bool(low.startswith('select') or low.startswith('pragma') or low.startswith('with'))
        if not is_ok:
            error = 'Only SELECT/PRAGMA queries are allowed here.'
        else:
            for b in banned:
                if b in low:
                    error = 'This query contains a blocked keyword.'
                    break
        if not error:
            parts = [p for p in s.split(';') if p.strip()]
            if len(parts) > 1:
                error = 'Only one SQL statement is allowed.'
        if not error:
            try:
                res = db.session.execute(text(s))
                try:
                    result_cols = list(res.keys())
                except Exception:
                    result_cols = []
                fetched = res.fetchmany(500)
                for r in fetched:
                    try:
                        result_rows.append(list(r))
                    except Exception:
                        result_rows.append([str(r)])
            except Exception:
                error = 'SQL failed.'
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='sql', sql_text=sql_text, sql_error=error, sql_cols=result_cols, sql_rows=result_rows, db_server='localhost', db_name='site')

@app.route('/admin/db/<model_name>/operations', methods=['GET', 'POST'])
@login_required
def admin_db_operations(model_name):
    _require_perm('admin_db')
    from sqlalchemy import text
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    models = []
    for name, m in sorted(model_map.items(), key=lambda x: x[0].lower()):
        try:
            count = int(m.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(m, '__tablename__', name.lower())})
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        tbl = getattr(model, '__tablename__', '')
        if action == 'empty':
            try:
                db.session.execute(text(f'DELETE FROM {tbl}'))
                db.session.commit()
                add_site_log('admin_action', f'{current_user.username} emptied table {tbl}.', actor_user_id=current_user.id)
                flash('Table emptied.', 'success')
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                flash('Could not empty table.', 'danger')
        elif action == 'vacuum':
            try:
                db.session.execute(text('VACUUM'))
                db.session.commit()
                add_site_log('admin_action', f'{current_user.username} ran VACUUM.', actor_user_id=current_user.id)
                flash('VACUUM completed.', 'success')
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                flash('VACUUM failed.', 'danger')
        elif action == 'drop':
            confirm = (request.form.get('confirm') or '').strip()
            confirm2 = (request.form.get('confirm2') or '').strip()
            if confirm == 'DROP' and confirm2 == tbl:
                try:
                    db.session.execute(text(f'DROP TABLE {tbl}'))
                    db.session.commit()
                    add_site_log('admin_action', f'{current_user.username} dropped table {tbl}.', actor_user_id=current_user.id)
                    flash('Table dropped.', 'success')
                    return redirect(url_for('admin_db'))
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                    flash('Drop failed.', 'danger')
            else:
                flash('Drop confirmation failed.', 'danger')
        return redirect(url_for('admin_db_operations', model_name=model_name))
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='operations', db_server='localhost', db_name='site')

@app.route('/admin/db/<model_name>/insert', methods=['GET', 'POST'])
@login_required
def admin_db_insert(model_name):
    _require_perm('admin_db')
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    models = []
    for name, m in sorted(model_map.items(), key=lambda x: x[0].lower()):
        try:
            count = int(m.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(m, '__tablename__', name.lower())})
    cols = []
    try:
        for c in model.__table__.columns:
            cols.append({
                'name': c.name,
                'type': str(c.type),
                'nullable': bool(c.nullable),
                'primary_key': bool(c.primary_key),
            })
    except Exception:
        cols = []
    if request.method == 'POST':
        obj = model()
        for c in cols:
            if c.get('primary_key'):
                continue
            k = c.get('name')
            v = request.form.get(k)
            if v is None:
                continue
            v = v.strip()
            if v == '':
                continue
            try:
                col = getattr(model, k)
                tname = str(getattr(col.type, 'python_type', '')).lower()
                if 'int' in tname:
                    v2 = int(v)
                elif 'float' in tname:
                    v2 = float(v)
                elif 'bool' in tname:
                    v2 = v.lower() in ('1', 'true', 'yes', 'on')
                else:
                    v2 = v
            except Exception:
                v2 = v
            try:
                setattr(obj, k, v2)
            except Exception:
                pass
        try:
            db.session.add(obj)
            db.session.commit()
            add_site_log('admin_action', f'{current_user.username} inserted row into {model_name}.', actor_user_id=current_user.id)
            flash('Row inserted.', 'success')
            return redirect(url_for('admin_db_table', model_name=model_name))
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash('Insert failed.', 'danger')
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='insert', insert_cols=cols, db_server='localhost', db_name='site')

@app.route('/admin/db/<model_name>/import', methods=['GET', 'POST'])
@login_required
def admin_db_import(model_name):
    _require_perm('admin_db')
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    models = []
    for name, m in sorted(model_map.items(), key=lambda x: x[0].lower()):
        try:
            count = int(m.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(m, '__tablename__', name.lower())})
    columns = [c.name for c in model.__table__.columns]
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not file.filename:
            flash('Select a CSV file.', 'danger')
            return redirect(url_for('admin_db_import', model_name=model_name))
        try:
            import csv
            import io
            raw = file.read().decode('utf-8', errors='ignore')
            reader = csv.DictReader(io.StringIO(raw))
            if not reader.fieldnames:
                flash('CSV must include headers.', 'danger')
                return redirect(url_for('admin_db_import', model_name=model_name))
            fields = [f.strip() for f in reader.fieldnames if f and f.strip()]
            bad = [f for f in fields if f not in columns]
            if bad:
                flash('CSV has unknown columns: ' + ', '.join(bad), 'danger')
                return redirect(url_for('admin_db_import', model_name=model_name))
            inserted = 0
            for row in reader:
                if inserted >= 5000:
                    break
                obj = model()
                for f in fields:
                    if f == 'id':
                        continue
                    v = row.get(f)
                    if v is None:
                        continue
                    v = str(v).strip()
                    if v == '':
                        continue
                    try:
                        col = getattr(model, f)
                        tname = str(getattr(col.type, 'python_type', '')).lower()
                        if 'int' in tname:
                            v2 = int(v)
                        elif 'float' in tname:
                            v2 = float(v)
                        elif 'bool' in tname:
                            v2 = v.lower() in ('1', 'true', 'yes', 'on')
                        else:
                            v2 = v
                    except Exception:
                        v2 = v
                    try:
                        setattr(obj, f, v2)
                    except Exception:
                        pass
                db.session.add(obj)
                inserted += 1
            db.session.commit()
            add_site_log('admin_action', f'{current_user.username} imported {inserted} row(s) into {model_name}.', actor_user_id=current_user.id)
            flash(f'Imported {inserted} row(s).', 'success')
            return redirect(url_for('admin_db_table', model_name=model_name))
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash('Import failed.', 'danger')
            return redirect(url_for('admin_db_import', model_name=model_name))
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='import', import_columns=columns, db_server='localhost', db_name='site')

@app.route('/admin/db/<model_name>/<int:row_id>/delete')
@login_required
def admin_db_delete(model_name, row_id):
    _require_perm('admin_db')
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    row = model.query.get_or_404(row_id)
    # Prevent deleting self via DB editor
    if model_name == 'User' and row.id == current_user.id:
        flash('Cannot delete your own account.', 'danger')
        return redirect(url_for('admin_db_table', model_name=model_name))
    db.session.delete(row)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted {model_name} #{row_id} via DB editor.', actor_user_id=current_user.id)
    flash(f'{model_name} #{row_id} deleted.', 'success')
    return redirect(url_for('admin_db_table', model_name=model_name))

@app.route('/admin/db/<model_name>/<int:row_id>/edit', methods=['POST'])
@login_required
def admin_db_edit(model_name, row_id):
    _require_perm('admin_db')
    model_map = {
        'User': User,
        'Level': Level,
        'Record': Record,
        'LevelSubmission': LevelSubmission,
        'Page': Page,
        'Event': Event,
        'Clan': Clan,
        'ClanJoinRequest': ClanJoinRequest,
        'NewsPost': NewsPost,
        'Setting': Setting,
        'SiteLog': SiteLog,
        'CustomList': CustomList,
        'MapPack': MapPack,
    }
    model = model_map.get(model_name)
    if not model:
        abort(404)
    row = model.query.get_or_404(row_id)
    # Update basic text/number fields safely
    for key, value in request.form.items():
        if key in ('id','password','submitted_at'):
            continue
        if hasattr(row, key):
            cur = getattr(row, key)
            new_val = value
            try:
                if isinstance(cur, bool):
                    new_val = value.lower() in ('1','true','yes','on')
                elif isinstance(cur, int):
                    new_val = int(value)
                elif isinstance(cur, float):
                    new_val = float(value)
            except Exception:
                pass
            setattr(row, key, new_val)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} edited {model_name} #{row_id} via DB editor.', actor_user_id=current_user.id)
    flash(f'{model_name} #{row_id} updated.', 'success')
    return redirect(url_for('admin_db_table', model_name=model_name))

@app.route('/admin/delete_level_submission/<int:sub_id>')
@login_required
def delete_level_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = LevelSubmission.query.get_or_404(sub_id)
    sub_name = sub.name
    sub_creator = sub.creator
    db.session.delete(sub)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted level submission "{sub_name}" by {sub_creator}.', actor_user_id=current_user.id)
    flash('Level submission deleted', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_level/<int:level_id>')
@login_required
def delete_level(level_id):
    _require_perm('manage_levels')
    level = Level.query.get_or_404(level_id)
    level_name = level.name
    level_creator = level.creator
    
    # Remove points from all users who have records on this level
    for record in level.records:
        if record.status == 'approved':
            record.user.points -= level.points_reward
            if record.user.points < 0: record.user.points = 0
            
    db.session.delete(level)
    db.session.commit()
    send_discord_message(f'"{level_name}" by {level.creator} was deleted by {current_user.username}', event_type='level_deleted')
    add_site_log('admin_action', f'{current_user.username} deleted level "{level_name}" by {level_creator}.', actor_user_id=current_user.id)
    flash('Level deleted', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/download-db')
@login_required
def download_db():
    _require_perm('admin_db')
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    path = None
    if uri.startswith('sqlite:///'):
        candidate = uri.replace('sqlite:///', '', 1)
        if os.path.isabs(candidate):
            base = os.path.realpath(app.instance_path)
            p = os.path.realpath(candidate)
            if not p.startswith(base + os.sep):
                raise NotFound()
            path = p
        else:
            path = os.path.join(app.instance_path, candidate)
    else:
        path = os.path.join(app.instance_path, 'database.db')
    if not os.path.exists(path):
        raise NotFound()
    return send_file(path, as_attachment=True, download_name='database.db')
@app.route('/admin/edit_level/<int:level_id>', methods=['GET', 'POST'])
@login_required
def edit_level(level_id):
    _require_perm('manage_levels')
    level = Level.query.get_or_404(level_id)
    
    if request.method == 'POST':
        old_verified = level.is_verified
        old_list_type = level.list_type
        old_points_reward = float(getattr(level, 'points_reward', 0.0) or 0.0)
        old_position = getattr(level, 'position', None)
        level.name = request.form.get('name')
        level.creator = request.form.get('creator')
        level.video_url = request.form.get('video_url')
        level.description = request.form.get('description')
        level.gd_level_id = request.form.get('gd_level_id')
        
        try:
            sid = request.form.get('song_id', '').strip()
            level.song_id = int(sid) if sid else None
        except Exception:
            pass
            
        try:
            osid = request.form.get('official_song_id', '').strip()
            level.official_song_id = int(osid) if osid else None
        except Exception:
            pass
            
        level.points_reward = float(request.form.get('points_reward', 0))
        lt = (request.form.get('list_type', 'main') or 'main').strip().lower()
        if lt not in ('main', 'challenge', 'platformer'):
            lt = 'main'
        level.list_type = lt
        level.difficulty = (request.form.get('difficulty') or '').strip()[:40] or None
        if request.form.get('is_verified') == 'on':
            level.is_verified = True
        else:
            level.is_verified = False
        raw_pos = (request.form.get('position') or '').strip()
        new_position = None
        if raw_pos:
            try:
                new_position = int(float(raw_pos))
            except Exception:
                new_position = None
        level.uses_nong = True if request.form.get('uses_nong') == 'on' else False
        cats = request.form.getlist('categories')
        level.categories = ",".join(cats)
        file = request.files.get('nong_file')
        if level.uses_nong and file and file.filename:
            unique, err = _save_uploaded_file(file, app.config['SONG_UPLOAD_FOLDER'], _ALLOWED_AUDIO_EXTS, '')
            if not unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('NONG song must be a valid audio file (mp3/wav/ogg).', 'danger')
                return redirect(url_for('edit_level', level_id=level.id))
            level.song_filename = unique
        thumb_file = request.files.get('thumbnail')
        if thumb_file and thumb_file.filename:
            folder = os.path.join(app.root_path, 'static', 'level_thumbs')
            unique, err = _save_uploaded_file(thumb_file, folder, _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                if err == 'rate_limited':
                    flash('Upload rate limit exceeded. Please wait a minute and try again.', 'danger')
                else:
                    flash('Thumbnail must be a valid image (png/jpg/webp).', 'danger')
                return redirect(url_for('edit_level', level_id=level.id))
            level.thumbnail_filename = unique
        if not level.is_verified:
            if old_position is not None and (old_list_type or 'main') in ('main', 'challenge', 'platformer'):
                try:
                    Level.query.filter(
                        Level.list_type == (old_list_type or 'main'),
                        Level.is_verified == True,
                        Level.position.isnot(None),
                        Level.position > int(old_position),
                        Level.id != level.id,
                    ).update({Level.position: Level.position - 1}, synchronize_session=False)
                except Exception:
                    pass
            level.position = None
        else:
            if new_position is None:
                try:
                    max_pos = (
                        db.session.query(db.func.max(Level.position))
                        .filter(Level.is_verified == True, Level.list_type == level.list_type, Level.position.isnot(None), Level.id != level.id)
                        .scalar()
                    )
                except Exception:
                    max_pos = None
                try:
                    max_pos_int = int(max_pos or 0)
                except Exception:
                    max_pos_int = 0
                new_position = max_pos_int + 1

            if (old_list_type or 'main') != (level.list_type or 'main'):
                if old_position is not None and (old_list_type or 'main') in ('main', 'challenge', 'platformer'):
                    try:
                        Level.query.filter(
                            Level.list_type == (old_list_type or 'main'),
                            Level.is_verified == True,
                            Level.position.isnot(None),
                            Level.position > int(old_position),
                            Level.id != level.id,
                        ).update({Level.position: Level.position - 1}, synchronize_session=False)
                    except Exception:
                        pass
                try:
                    Level.query.filter(
                        Level.list_type == level.list_type,
                        Level.is_verified == True,
                        Level.position.isnot(None),
                        Level.position >= int(new_position),
                        Level.id != level.id,
                    ).update({Level.position: Level.position + 1}, synchronize_session=False)
                except Exception:
                    pass
                level.position = int(new_position)
            else:
                if old_position is None:
                    try:
                        Level.query.filter(
                            Level.list_type == level.list_type,
                            Level.is_verified == True,
                            Level.position.isnot(None),
                            Level.position >= int(new_position),
                            Level.id != level.id,
                        ).update({Level.position: Level.position + 1}, synchronize_session=False)
                    except Exception:
                        pass
                    level.position = int(new_position)
                else:
                    try:
                        op = int(old_position)
                    except Exception:
                        op = None
                    try:
                        np = int(new_position)
                    except Exception:
                        np = None
                    if op is None or np is None:
                        level.position = int(new_position)
                    elif np < op:
                        try:
                            Level.query.filter(
                                Level.list_type == level.list_type,
                                Level.is_verified == True,
                                Level.position.isnot(None),
                                Level.position >= np,
                                Level.position < op,
                                Level.id != level.id,
                            ).update({Level.position: Level.position + 1}, synchronize_session=False)
                        except Exception:
                            pass
                        level.position = np
                    elif np > op:
                        try:
                            Level.query.filter(
                                Level.list_type == level.list_type,
                                Level.is_verified == True,
                                Level.position.isnot(None),
                                Level.position <= np,
                                Level.position > op,
                                Level.id != level.id,
                            ).update({Level.position: Level.position - 1}, synchronize_session=False)
                        except Exception:
                            pass
                        level.position = np
                    else:
                        level.position = op
        db.session.commit()
        _cache_bust_prefixes(['levels:', 'lb:'])
        if old_verified != level.is_verified:
            if level.is_verified:
                send_discord_message(f'"{level.name}" by {level.creator} was verified by {current_user.username}. Video: {level.video_url or "N/A"}', event_type='level_verified')
            else:
                send_discord_message(f'"{level.name}" by {level.creator} was set to unverified by {current_user.username}', event_type='level_unverified')
        changes = []
        try:
            if bool(old_verified) != bool(level.is_verified):
                changes.append(f'verified: {bool(old_verified)} -> {bool(level.is_verified)}')
        except Exception:
            pass
        try:
            if (old_list_type or 'main') != (level.list_type or 'main'):
                changes.append(f'list: {(old_list_type or "main")} -> {(level.list_type or "main")}')
        except Exception:
            pass
        suffix = f' Changes: {", ".join(changes)}.' if changes else ''
        add_site_log('admin_action', f'{current_user.username} edited level "{level.name}" by {level.creator}.{suffix}', actor_user_id=current_user.id)
        try:
            if float(getattr(level, 'points_reward', 0.0) or 0.0) != float(old_points_reward or 0.0) or bool(old_verified) != bool(level.is_verified) or (old_list_type or 'main') != (level.list_type or 'main'):
                user_ids = [
                    int(uid)
                    for (uid,) in (
                        db.session.query(Record.user_id)
                        .filter(Record.level_id == level.id, Record.status == 'approved')
                        .distinct()
                        .all()
                    )
                    if uid
                ]
                if user_ids:
                    _recalc_user_points_for_users(user_ids)
        except Exception:
            pass
        flash('Level updated successfully', 'success')
        return redirect(url_for('admin_dashboard'))
        
    return render_template('edit_level.html', level=level)


@app.route('/profile/delete', methods=['POST'])
@login_required
def delete_account():
    try:
        user_obj = current_user._get_current_object()
    except Exception:
        user_obj = None
    if not user_obj or not getattr(user_obj, 'id', None):
        flash('You must be logged in to delete your account.', 'danger')
        return redirect(url_for('login'))
    uid = int(user_obj.id)
    uname = str(getattr(user_obj, 'username', '') or '')
    logout_user()
    try:
        u = db.session.get(User, uid)
    except Exception:
        u = None
    if not u:
        flash('Account not found.', 'danger')
        return redirect(url_for('home'))
    try:
        if bool(getattr(u, 'is_admin', False)) or str(getattr(u, 'rank', '') or '') == 'admin':
            flash('Admin accounts cannot be deleted from here.', 'danger')
            return redirect(url_for('home'))
    except Exception:
        pass
    try:
        db.session.delete(u)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        flash('Could not delete your account. Try again.', 'danger')
        return redirect(url_for('home'))
    add_site_log('account_delete', f'Account deleted for {uname}.', actor_user_id=None)
    flash('Your account has been deleted.', 'success')
    return redirect(url_for('home'))



@app.route('/profile/change_username', methods=['POST'])
@login_required
def change_username():
    new_username = request.form.get('new_username')
    if not new_username or new_username.strip() == '':
        flash('Username cannot be empty.', 'danger')
        return redirect(url_for('profile'))
    if User.query.filter_by(username=new_username).first():
        flash('Username already taken.', 'danger')
        return redirect(url_for('profile'))
    old_username = current_user.username
    current_user.username = new_username
    db.session.commit()
    add_site_log('profile_edit', f'Username changed: {old_username} -> {new_username}.', actor_user_id=current_user.id)
    flash('Username updated.', 'success')
    return redirect(url_for('profile'))

def ensure_schema():
    from sqlalchemy import text, inspect
    inspector = None
    try:
        inspector = inspect(db.engine)
    except Exception:
        inspector = None
    try:
        db.create_all()
    except Exception:
        pass
    # Add device column to Record if missing
    try:
        info = db.session.execute(text("PRAGMA table_info(record)")).fetchall()
        cols = [row[1] for row in info]
        if 'device' not in cols:
            db.session.execute(text("ALTER TABLE record ADD COLUMN device TEXT"))
            db.session.commit()
        if 'beaten_on_vc' not in cols:
            db.session.execute(text("ALTER TABLE record ADD COLUMN beaten_on_vc INTEGER DEFAULT 0"))
            db.session.commit()
        if 'vc_proof_filename' not in cols:
            db.session.execute(text("ALTER TABLE record ADD COLUMN vc_proof_filename TEXT"))
            db.session.commit()
        if 'proof_image_filename' not in cols:
            db.session.execute(text("ALTER TABLE record ADD COLUMN proof_image_filename TEXT"))
            db.session.commit()
        if 'rating_deco' not in cols:
            db.session.execute(text("ALTER TABLE record ADD COLUMN rating_deco INTEGER"))
            db.session.commit()
        if 'rating_gameplay' not in cols:
            db.session.execute(text("ALTER TABLE record ADD COLUMN rating_gameplay INTEGER"))
            db.session.commit()
        if 'rating_difficulty' not in cols:
            db.session.execute(text("ALTER TABLE record ADD COLUMN rating_difficulty INTEGER"))
            db.session.commit()
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_status_submitted_at ON record(status, submitted_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_user_status ON record(user_id, status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_level_status ON record(level_id, status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_record_user_submitted_at ON record(user_id, submitted_at)"))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    try:
        info = db.session.execute(text("PRAGMA table_info(user)")).fetchall()
        cols = [row[1] for row in info]
        if 'discord_id' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN discord_id TEXT"))
            db.session.commit()
        if 'discord_username' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN discord_username TEXT"))
            db.session.commit()
        if 'discord_avatar' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN discord_avatar TEXT"))
            db.session.commit()
        if 'avatar_filename' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN avatar_filename TEXT"))
            db.session.commit()
        if 'banner_filename' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN banner_filename TEXT"))
            db.session.commit()
        if 'api_token' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN api_token TEXT"))
            db.session.commit()
        if 'register_ip' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN register_ip TEXT"))
            db.session.commit()
        if 'clan_id' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN clan_id INTEGER"))
            db.session.commit()
        if 'ui_version' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN ui_version TEXT"))
            db.session.commit()
        if 'theme_accent' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN theme_accent TEXT"))
            db.session.commit()
        if 'theme_accent_bg' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN theme_accent_bg TEXT"))
            db.session.commit()
        if 'last_seen_at' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN last_seen_at DATETIME"))
            db.session.commit()
        if 'guidelines_accepted_at' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN guidelines_accepted_at DATETIME"))
            db.session.commit()
        if 'is_disabled' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN is_disabled INTEGER DEFAULT 0"))
            db.session.commit()
        if 'rank' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN rank TEXT DEFAULT 'member'"))
            db.session.commit()
            try:
                db.session.execute(text("UPDATE user SET rank='admin' WHERE is_admin=1"))
                db.session.execute(text("UPDATE user SET rank='mod' WHERE is_admin=0 AND is_mod=1"))
                db.session.execute(text("UPDATE user SET rank='member' WHERE rank IS NULL OR TRIM(rank)=''"))
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
        if 'ui_template_slug' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN ui_template_slug TEXT"))
            db.session.commit()
        if 'ui_custom_mode' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN ui_custom_mode TEXT"))
            db.session.commit()
        if 'ui_custom_script' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN ui_custom_script TEXT"))
            db.session.commit()
        info_mp = db.session.execute(text("PRAGMA table_info(map_pack)")).fetchall()
        if info_mp:
            mp_cols = [row[1] for row in info_mp]
            if 'thumbnail_filename' not in mp_cols:
                db.session.execute(text("ALTER TABLE map_pack ADD COLUMN thumbnail_filename TEXT"))
                db.session.commit()
            db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_map_pack_created_at ON map_pack(created_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_username ON user(username)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_last_seen ON user(last_seen_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_points ON user(points)"))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    try:
        info = db.session.execute(text("PRAGMA table_info(clan)")).fetchall()
        cols = [row[1] for row in info]
        if info and 'owner_user_id' not in cols:
            db.session.execute(text("ALTER TABLE clan ADD COLUMN owner_user_id INTEGER"))
            db.session.commit()
            cols.append('owner_user_id')
        if info and 'owner_user_id' in cols:
            try:
                db.session.execute(text("UPDATE clan SET owner_user_id=(SELECT MIN(id) FROM user WHERE user.clan_id=clan.id) WHERE owner_user_id IS NULL"))
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
    except Exception:
        pass
    try:
        info = db.session.execute(text("PRAGMA table_info(clan_join_request)")).fetchall()
        if not info:
            db.create_all()
    except Exception:
        pass
    try:
        info = db.session.execute(text("PRAGMA table_info(level)")).fetchall()
        cols = [row[1] for row in info]
        if 'created_at' not in cols:
            db.session.execute(text("ALTER TABLE level ADD COLUMN created_at DATETIME"))
            db.session.commit()
            try:
                db.session.execute(text("UPDATE level SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL"))
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
        if 'categories' not in cols:
            db.session.execute(text("ALTER TABLE level ADD COLUMN categories TEXT"))
            db.session.commit()
        if 'uses_nong' not in cols:
            db.session.execute(text("ALTER TABLE level ADD COLUMN uses_nong INTEGER DEFAULT 0"))
            db.session.commit()
        if 'song_filename' not in cols:
            db.session.execute(text("ALTER TABLE level ADD COLUMN song_filename TEXT"))
            db.session.commit()
        if 'difficulty' not in cols:
            db.session.execute(text("ALTER TABLE level ADD COLUMN difficulty TEXT"))
            db.session.commit()
        if 'thumbnail_filename' not in cols:
            db.session.execute(text("ALTER TABLE level ADD COLUMN thumbnail_filename TEXT"))
            db.session.commit()
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_list_type_position ON level(list_type, position)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_verified_list_type ON level(is_verified, list_type)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_created_at ON level(created_at)"))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    try:
        info = db.session.execute(text("PRAGMA table_info(page)")).fetchall()
        if not info:
            db.create_all()
        else:
            cols = [row[1] for row in info]
            if 'is_html' not in cols:
                db.session.execute(text("ALTER TABLE page ADD COLUMN is_html INTEGER DEFAULT 0"))
                db.session.commit()
    except Exception:
        pass
    try:
        info = db.session.execute(text("PRAGMA table_info(event)")).fetchall()
        if not info:
            db.create_all()
    except Exception:
        pass
    try:
        info = db.session.execute(text("PRAGMA table_info(level_submission)")).fetchall()
        cols = [row[1] for row in info]
        if info and 'list_type' not in cols:
            db.session.execute(text("ALTER TABLE level_submission ADD COLUMN list_type TEXT DEFAULT 'main'"))
            db.session.commit()
            try:
                db.session.execute(text("UPDATE level_submission SET list_type='main' WHERE list_type IS NULL OR TRIM(list_type)=''"))
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
        if info and 'gd_level_id' not in cols:
            db.session.execute(text("ALTER TABLE level_submission ADD COLUMN gd_level_id TEXT"))
            db.session.commit()
        if info and 'proposed_position' not in cols:
            db.session.execute(text("ALTER TABLE level_submission ADD COLUMN proposed_position INTEGER"))
            db.session.commit()
        if info and 'is_verified' not in cols:
            db.session.execute(text("ALTER TABLE level_submission ADD COLUMN is_verified INTEGER DEFAULT 0"))
            db.session.commit()
        if info and 'verified_video_url' not in cols:
            db.session.execute(text("ALTER TABLE level_submission ADD COLUMN verified_video_url TEXT"))
            db.session.commit()
        if info and 'collaborators' not in cols:
            db.session.execute(text("ALTER TABLE level_submission ADD COLUMN collaborators TEXT"))
            db.session.commit()
        if info and 'thumbnail_filename' not in cols:
            db.session.execute(text("ALTER TABLE level_submission ADD COLUMN thumbnail_filename TEXT"))
            db.session.commit()
        if info:
            db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_submission_status_submitted_at ON level_submission(status, submitted_at)"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_submission_user_status ON level_submission(user_id, status)"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS idx_level_submission_list_type ON level_submission(list_type)"))
            db.session.commit()
    except Exception:
        pass

    # Data Migration: Sync approved song submissions to NONG index
    try:
        import uuid
        approved_subs = SongSubmission.query.filter_by(status='approved').all()
        for sub in approved_subs:
            level = sub.level
            if not level and sub.gd_level_id:
                level = Level.query.filter_by(gd_level_id=sub.gd_level_id).first()
            
            if not level:
                continue
                
            # Check if this level already has a verified NONG
            has_verified = LevelNong.query.filter_by(level_id=level.id, is_verified=True).first()
            if not has_verified:
                # Check if this specific file is already in LevelNong (even if not verified)
                existing_nong = LevelNong.query.filter_by(level_id=level.id, song_filename=sub.song_filename).first()
                if existing_nong:
                    existing_nong.is_verified = True
                else:
                    # Create new verified NONG entry
                    nong = LevelNong(
                        uuid=str(uuid.uuid4()),
                        level_id=level.id,
                        song_name=sub.song_name or level.name,
                        artist_name=sub.artist_name or level.creator,
                        song_filename=sub.song_filename,
                        original_filename=sub.original_filename,
                        file_size=sub.file_size,
                        is_verified=True,
                        created_by_user_id=sub.reviewed_by_user_id
                    )
                    db.session.add(nong)
                
                # Update level flags
                level.uses_nong = True
                level.song_filename = sub.song_filename
        
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

@app.route('/api-docs')
def api_docs():
    api_token = None
    try:
        if getattr(current_user, 'is_authenticated', False):
            api_token = _ensure_api_token_for(current_user)
    except Exception:
        api_token = None
    return render_template('api_docs.html', api_token=api_token)


def _leaderboard_rows(view: str, list_type: str, sort_by: str, filter_device: str, filter_status: str):
    from sqlalchemy import case

    view = (view or 'users').strip().lower()
    if view not in ('users', 'clans'):
        view = 'users'
    lt = (list_type or 'main').strip().lower()
    if lt not in ('main', 'challenge', 'platformer', 'all'):
        lt = 'main'
    cache_key = f'lb:{view}:{lt}:{(sort_by or "")}:{(filter_device or "")}:{(filter_status or "")}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[0], cached[1], cached[2], cached[3]

    record_join = db.and_(Record.user_id == User.id, Record.status == 'approved')
    if filter_device:
        record_join = db.and_(record_join, Record.device == filter_device)

    if lt in ('main', 'challenge', 'platformer'):
        level_join = db.and_(Level.id == Record.level_id, Level.list_type == lt)
        records_count_expr = db.func.count(Level.id)
        points_expr = db.func.coalesce(db.func.sum(Level.points_reward), 0.0)
        recent_expr = db.func.max(case((Level.id.isnot(None), Record.submitted_at), else_=None))
    else:
        level_join = Level.id == Record.level_id
        records_count_expr = db.func.count(Record.id)
        points_expr = db.func.coalesce(db.func.sum(Level.points_reward), 0.0)
        recent_expr = db.func.max(Record.submitted_at)

    users = []
    clans = []

    if view == 'clans':
        query = (
            db.session.query(
                Clan,
                points_expr.label('lb_points'),
                records_count_expr.label('lb_records'),
                recent_expr.label('lb_recent')
            )
            .outerjoin(User, User.clan_id == Clan.id)
            .outerjoin(Record, record_join)
            .outerjoin(Level, level_join)
            .group_by(Clan.id)
        )

        if filter_status == 'active':
            query = query.having(records_count_expr > 0)
        elif filter_status == 'verified':
            query = query.having(points_expr > 0)

        if sort_by == 'records':
            query = query.order_by(records_count_expr.desc(), points_expr.desc())
        elif sort_by in ('username', 'name'):
            query = query.order_by(Clan.name.asc())
        elif sort_by == 'recent':
            query = query.order_by(recent_expr.desc().nullslast(), points_expr.desc())
        else:
            query = query.order_by(points_expr.desc(), records_count_expr.desc())

        rows = query.all()
        for c, pts, recs, _recent in rows:
            try:
                c.lb_points = float(pts or 0.0)
            except Exception:
                c.lb_points = 0.0
            try:
                c.lb_records = int(recs or 0)
            except Exception:
                c.lb_records = 0
            clans.append(c)
    else:
        query = (
            db.session.query(
                User,
                points_expr.label('lb_points'),
                records_count_expr.label('lb_records'),
                recent_expr.label('lb_recent')
            )
            .outerjoin(Record, record_join)
            .outerjoin(Level, level_join)
            .group_by(User.id)
        )

        if filter_status == 'active':
            query = query.having(records_count_expr > 0)
        elif filter_status == 'verified':
            query = query.having(points_expr > 0)

        if sort_by == 'records':
            query = query.order_by(records_count_expr.desc(), points_expr.desc())
        elif sort_by == 'username':
            query = query.order_by(User.username.asc())
        elif sort_by == 'recent':
            query = query.order_by(recent_expr.desc().nullslast(), points_expr.desc())
        else:
            query = query.order_by(points_expr.desc(), records_count_expr.desc())

        rows = query.all()
        for u, pts, recs, _recent in rows:
            try:
                u.lb_points = float(pts or 0.0)
            except Exception:
                u.lb_points = 0.0
            try:
                u.lb_records = int(recs or 0)
            except Exception:
                u.lb_records = 0
            if u.lb_points <= 0 and u.lb_records <= 0:
                continue
            users.append(u)

    out = (users, clans, view, lt)
    _cache_set(cache_key, out, 20)
    return out


@app.route('/api/leaderboard/users', methods=['GET'])
def api_leaderboard_users():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    sort_by = request.args.get('sort', 'points')
    filter_device = request.args.get('device', '')
    filter_status = request.args.get('status', '')
    list_type = (request.args.get('list') or 'main').strip().lower()
    users, _clans, _view, list_type = _leaderboard_rows('users', list_type, sort_by, filter_device, filter_status)
    data = []
    for u in users:
        clan_id = getattr(u, 'clan_id', None)
        clan_name = None
        try:
            if clan_id:
                rel = getattr(u, 'clan', None)
                if rel and getattr(rel, 'name', None):
                    clan_name = rel.name
                else:
                    c = db.session.get(Clan, int(clan_id))
                    if c and getattr(c, 'name', None):
                        clan_name = c.name
        except Exception:
            clan_name = None
        data.append(
            {
                'id': u.id,
                'username': u.username,
                'points': float(getattr(u, 'lb_points', 0.0) or 0.0),
                'records': int(getattr(u, 'lb_records', 0) or 0),
                'clan_id': clan_id,
                'clan_name': clan_name,
                'list': list_type,
            }
        )
    return jsonify({'users': data})


@app.route('/api/leaderboard/clans', methods=['GET'])
def api_leaderboard_clans():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    sort_by = request.args.get('sort', 'points')
    filter_device = request.args.get('device', '')
    filter_status = request.args.get('status', '')
    list_type = (request.args.get('list') or 'main').strip().lower()
    _users, clans, _view, list_type = _leaderboard_rows('clans', list_type, sort_by, filter_device, filter_status)
    data = []
    for c in clans:
        try:
            members_count = len(list(c.members or []))
        except Exception:
            members_count = 0
        data.append(
            {
                'id': c.id,
                'name': c.name,
                'points': float(getattr(c, 'lb_points', 0.0) or 0.0),
                'records': int(getattr(c, 'lb_records', 0) or 0),
                'member_count': members_count,
                'list': list_type,
            }
        )
    return jsonify({'clans': data})

@app.route('/leaderboard/user/<int:user_id>/details', methods=['GET'])
def leaderboard_user_details(user_id):
    list_type = (request.args.get('list') or 'main').strip().lower()
    if list_type not in ('main', 'challenge', 'platformer', 'all'):
        list_type = 'main'
    try:
        limit = int(request.args.get('limit') or 40)
    except Exception:
        limit = 40
    if limit < 5:
        limit = 5
    if limit > 200:
        limit = 200

    rec_q = (
        db.session.query(Record, Level)
        .join(Level, Level.id == Record.level_id)
        .filter(Record.user_id == user_id, Record.status == 'approved')
    )
    if list_type in ('main', 'challenge', 'platformer'):
        rec_q = rec_q.filter(Level.list_type == list_type)
    rec_rows = rec_q.order_by(Record.submitted_at.desc()).limit(limit).all()

    recs = []
    for r, lv in rec_rows:
        recs.append({
            'id': r.id,
            'level_id': r.level_id,
            'level_name': lv.name,
            'creator': lv.creator,
            'submitted_at': r.submitted_at.isoformat() if r.submitted_at else None,
            'video_url': r.video_url or '',
            'device': r.device or '',
        })

    subs_q = LevelSubmission.query.filter(LevelSubmission.user_id == user_id).order_by(LevelSubmission.submitted_at.desc()).limit(limit).all()
    subs = []
    for s in subs_q:
        subs.append({
            'id': s.id,
            'name': s.name,
            'creator': s.creator,
            'status': s.status,
            'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None,
            'video_url': s.video_url or '',
        })

    return jsonify({'records': recs, 'submissions': subs, 'list': list_type})


@app.route('/leaderboard')
def leaderboard():
    sort_by = request.args.get('sort', 'points')
    filter_device = request.args.get('device', '')
    filter_status = request.args.get('status', '')
    view = (request.args.get('view') or 'users').strip().lower()
    list_type = (request.args.get('list') or 'main').strip().lower()
    users, clans, view, list_type = _leaderboard_rows(view, list_type, sort_by, filter_device, filter_status)
    return render_template(
        'leaderboard.html',
        users=users,
        clans=clans,
        view=view,
        sort_by=sort_by,
        filter_device=filter_device,
        filter_status=filter_status,
        list_type=list_type,
    )
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        ensure_schema()
        try:
            _announce_site_launch()
        except Exception:
            pass
        existing_admin = User.query.filter_by(is_admin=True).first()
        if not existing_admin:
            admin_pw = os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(12)
            hashed_pw = bcrypt.generate_password_hash(admin_pw).decode('utf-8')
            username = 'admin'
            if User.query.filter_by(username=username).first():
                username = f'admin-{secrets.token_hex(3)}'
            admin = User(username=username, password=hashed_pw, is_admin=True, rank='admin')
            db.session.add(admin)
            db.session.commit()
            print("Admin account created.")
            print(f"Username: {username}")
            print(f"Password: {admin_pw}")
            print("PLEASE SAVE THIS PASSWORD.")
        
        # Add sample level if empty
        has_any_level = False
        try:
            has_any_level = bool(Level.query.first())
        except OperationalError:
            try:
                ensure_schema()
            except Exception:
                pass
            has_any_level = bool(Level.query.first())
        if not has_any_level:
            sample = Level(
                name='Bottom Heavy',
                creator='Unknown',
                video_url='https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                position=1,
                is_verified=True,
                points_reward=100.0
            )
            db.session.add(sample)
            db.session.commit()
    
    def _backup_once():
        dst_dir = os.path.join(app.root_path, 'backups')
        os.makedirs(dst_dir, exist_ok=True)

        now = datetime.now()
        try:
            newest_mtime = None
            for entry in os.scandir(dst_dir):
                if not entry.is_file():
                    continue
                if not entry.name.lower().endswith('.db'):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except Exception:
                    continue
                if newest_mtime is None or mtime > newest_mtime:
                    newest_mtime = mtime
            if newest_mtime is not None and (time.time() - newest_mtime) < (24 * 60 * 60):
                return
        except Exception:
            pass

        date_part = now.strftime('%Y-%m-%d')
        hour_part = now.strftime('%H')
        marker_path = os.path.join(dst_dir, f'{date_part}-{hour_part}.marker')

        marker_fd = None
        try:
            marker_fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        except Exception:
            return

        try:
            uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            if uri.startswith('sqlite:///'):
                candidate = uri.replace('sqlite:///', '', 1)
                base = candidate if os.path.isabs(candidate) else os.path.join(app.instance_path, candidate)
            else:
                base = os.path.join(app.instance_path, 'database.db')

            if not os.path.exists(base):
                return

            prefix = f'{date_part}-{hour_part}.database('
            copy_no = 1
            try:
                for name in os.listdir(dst_dir):
                    if not name.startswith(prefix) or not name.endswith(').db'):
                        continue
                    num_str = name[len(prefix):-len(').db')]
                    try:
                        num = int(num_str)
                    except Exception:
                        continue
                    if num >= copy_no:
                        copy_no = num + 1
            except Exception:
                pass

            dst = os.path.join(dst_dir, f'{date_part}-{hour_part}.database({copy_no}).db')
            shutil.copy2(base, dst)
            try:
                now_ts = int(time.time())
                last_ts = int(float(_get_setting('last_data_save_log_ts') or 0))
                if not last_ts or (now_ts - last_ts) >= (14 * 24 * 60 * 60):
                    add_site_log('data_save', f'Database saved ({os.path.basename(dst)}).', actor_user_id=None)
                    _set_setting('last_data_save_log_ts', str(now_ts))
            except Exception:
                pass
        except Exception:
            try:
                os.unlink(marker_path)
            except Exception:
                pass
        finally:
            try:
                if marker_fd is not None:
                    os.close(marker_fd)
            except Exception:
                pass
@app.route('/admin/song-submissions')
@login_required
def admin_song_submissions():
    _require_perm('moderate_submissions')
    status = (request.args.get('status') or 'pending').strip().lower()
    if status not in ('pending', 'approved', 'rejected', 'all'):
        status = 'pending'
    q = SongSubmission.query
    if status != 'all':
        q = q.filter_by(status=status)
    submissions = q.order_by(SongSubmission.submitted_at.desc()).all()
    return render_template('admin_song_submissions.html', submissions=submissions, status=status)


@app.route('/admin/song-submissions/<int:sub_id>/approve', methods=['POST'])
@login_required
def approve_song_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    if sub.status == 'pending':
        # Get or create the level
        level = sub.level
        if not level and sub.gd_level_id:
            # Try to find level by GD level ID
            level = Level.query.filter_by(gd_level_id=sub.gd_level_id).first()

        if not level:
            flash('Cannot approve: no associated level found.', 'error')
            return redirect(url_for('admin_song_submissions'))

        # Move file from pending_songs to songs folder
        src_path = os.path.join(app.root_path, 'static', 'pending_songs', sub.song_filename)
        dst_path = os.path.join(app.root_path, 'static', 'songs', sub.song_filename)

        try:
            if os.path.exists(src_path):
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.move(src_path, dst_path)
        except Exception as e:
            flash(f'Error moving file: {str(e)[:100]}', 'error')
            return redirect(url_for('admin_song_submissions'))

        # Check if should be verified (from form checkbox)
        should_verify = request.form.get('set_verified_on_approve') == 'on'
        
        # If the level already has a verified NONG, unverify it if we are setting this one as verified
        if should_verify:
            existing_verified = LevelNong.query.filter_by(level_id=level.id, is_verified=True).first()
            if existing_verified:
                existing_verified.is_verified = False

        import uuid
        nong = LevelNong(
            uuid=str(uuid.uuid4()),  # Generate UUID for Song File Hub
            level_id=level.id,
            song_name=sub.song_name or level.name,
            artist_name=sub.artist_name or level.creator,
            song_id=sub.song_id,  # Save the song ID (Newgrounds/official) that this NONG replaces
            song_filename=sub.song_filename,
            original_filename=sub.original_filename,
            file_size=sub.file_size,
            start_offset=0,
            is_verified=should_verify,
            created_by_user_id=current_user.id
        )
        db.session.add(nong)
        
        # ALSO update the Level.song_filename for the UI if it's verified
        if should_verify:
            level.song_filename = sub.song_filename
            level.uses_nong = True

        # Update the submission status
        sub.status = 'approved'
        sub.reviewed_at = datetime.utcnow()
        sub.reviewed_by_user_id = current_user.id
        db.session.commit()

        if should_verify:
            flash(f'Song submission approved and set as verified NONG for {level.name}: {sub.original_filename}', 'success')
        else:
            flash(f'Song submission approved (not verified) for {level.name}: {sub.original_filename}', 'success')
    else:
        flash('Submission already processed.', 'info')
    return redirect(url_for('admin_song_submissions'))


@app.route('/admin/song-submissions/<int:sub_id>/reject', methods=['POST'])
@login_required
def reject_song_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    if sub.status == 'pending':
        sub.status = 'rejected'
        sub.reviewed_at = datetime.utcnow()
        sub.reviewed_by_user_id = current_user.id
        db.session.commit()
        # Optionally delete the file
        try:
            file_path = os.path.join(app.root_path, 'static', 'pending_songs', sub.song_filename)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        flash(f'Song submission rejected: {sub.original_filename}', 'success')
    else:
        flash('Submission already processed.', 'info')
    return redirect(url_for('admin_song_submissions'))


@app.route('/admin/song-submissions/<int:sub_id>/edit', methods=['POST'])
@login_required
def edit_song_submission(sub_id):
    """Edit a song submission's metadata."""
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    
    # Update song name
    if request.form.get('song_name'):
        sub.song_name = request.form.get('song_name')
    
    # Update artist name
    if request.form.get('artist_name'):
        sub.artist_name = request.form.get('artist_name')
    
    # Update song ID (the Newgrounds/official song ID this NONG replaces)
    new_song_id = request.form.get('song_id')
    if new_song_id:
        sub.song_id = new_song_id
    
    # Update level association
    new_level_id = request.form.get('level_id')
    if new_level_id and new_level_id.isdigit():
        level = Level.query.get(int(new_level_id))
        if level:
            sub.level_id = level.id
            sub.gd_level_id = level.gd_level_id
    
    # Update GD level ID directly
    new_gd_id = request.form.get('gd_level_id')
    if new_gd_id:
        sub.gd_level_id = new_gd_id
        # Also try to find matching level
        level = Level.query.filter_by(gd_level_id=new_gd_id).first()
        if level:
            sub.level_id = level.id
    
    db.session.commit()
    flash(f'Song submission updated: {sub.original_filename}', 'success')
    return redirect(url_for('admin_song_submissions'))


@app.route('/admin/song-submissions/<int:sub_id>/delete', methods=['POST'])
@login_required
def delete_song_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    # Delete the file
    try:
        file_path = os.path.join(app.root_path, 'static', 'pending_songs', sub.song_filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass
    db.session.delete(sub)
    db.session.commit()
    flash('Song submission deleted.', 'success')
    return redirect(url_for('admin_song_submissions'))


@app.route('/admin/level/<int:level_id>/nongs')
@login_required
def admin_level_nongs(level_id):
    """Manage NONGs for a specific level."""
    _require_perm('manage_levels')
    level = Level.query.get_or_404(level_id)
    return render_template('admin_level_nongs.html', level=level)


@app.route('/admin/level/<int:level_id>/nongs/add', methods=['POST'])
@login_required
def add_level_nong(level_id):
    """Add a new NONG to a level via file upload."""
    _require_perm('manage_levels')
    level = Level.query.get_or_404(level_id)

    if 'song_file' not in request.files:
        flash('No file provided.', 'error')
        return redirect(url_for('admin_level_nongs', level_id=level_id))

    file = request.files['song_file']
    if not file or file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('admin_level_nongs', level_id=level_id))

    # Validate file type
    filename = file.filename.lower()
    if not (filename.endswith('.mp3') or filename.endswith('.ogg')):
        flash('Only mp3 and ogg files are allowed.', 'error')
        return redirect(url_for('admin_level_nongs', level_id=level_id))

    # Save file
    import uuid
    unique_filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    save_path = os.path.join(app.root_path, 'static', 'songs', unique_filename)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    file.save(save_path)

    # Create LevelNong entry
    make_verified = request.form.get('is_verified') == 'on'

    # If making this verified, unverify existing
    if make_verified:
        existing = LevelNong.query.filter_by(level_id=level_id, is_verified=True).first()
        if existing:
            existing.is_verified = False

    nong = LevelNong(
        uuid=str(uuid.uuid4()),  # Generate UUID for Song File Hub
        level_id=level_id,
        song_name=request.form.get('song_name') or level.name,
        artist_name=request.form.get('artist_name') or level.creator,
        song_filename=unique_filename,
        original_filename=file.filename,
        file_size=os.path.getsize(save_path),
        start_offset=int(request.form.get('start_offset') or 0),
        is_verified=make_verified,
        created_by_user_id=current_user.id
    )
    db.session.add(nong)
    db.session.commit()

    flash(f'NONG added: {file.filename} (UUID: {nong.uuid})', 'success')
    return redirect(url_for('admin_level_nongs', level_id=level_id))


@app.route('/admin/level/nongs/<int:nong_id>/edit', methods=['POST'])
@login_required
def edit_level_nong(nong_id):
    """Edit a NONG's metadata."""
    _require_perm('manage_levels')
    nong = LevelNong.query.get_or_404(nong_id)
    
    nong.song_name = request.form.get('song_name') or nong.song_name
    nong.artist_name = request.form.get('artist_name') or nong.artist_name
    nong.start_offset = int(request.form.get('start_offset') or 0)
    
    # Handle verified status toggle
    is_verified = request.form.get('is_verified') == 'on'
    if is_verified and not nong.is_verified:
        # Making this verified - unverify any existing verified NONG for this level
        existing = LevelNong.query.filter_by(level_id=nong.level_id, is_verified=True).first()
        if existing:
            existing.is_verified = False
        nong.is_verified = True
        flash('NONG marked as verified and will appear in /api/bl-index', 'success')
    elif not is_verified and nong.is_verified:
        # Unverifying this NONG
        nong.is_verified = False
        flash('NONG unverified - will not appear in /api/bl-index', 'warning')
    else:
        flash('NONG updated.', 'success')
    
    db.session.commit()
    return redirect(url_for('admin_level_nongs', level_id=nong.level_id))


@app.route('/admin/level/nongs/<int:nong_id>/verify', methods=['POST'])
@login_required
def verify_level_nong(nong_id):
    """Mark a specific NONG as the verified one for its level."""
    _require_perm('manage_levels')
    nong = LevelNong.query.get_or_404(nong_id)

    # Unverify all other NONGs for this level
    LevelNong.query.filter_by(level_id=nong.level_id).update({'is_verified': False})

    # Verify this one
    nong.is_verified = True
    db.session.commit()

    flash(f'NONG marked as verified for {nong.level.name if nong.level else "level"}.', 'success')
    return redirect(url_for('admin_level_nongs', level_id=nong.level_id))


@app.route('/admin/level/<int:level_id>/nongs/<int:nong_id>/unverify', methods=['POST'])
@login_required
def unverify_level_nong(level_id, nong_id):
    """Unverify a specific NONG (remove its verified status)."""
    _require_perm('manage_levels')
    nong = LevelNong.query.get_or_404(nong_id)
    
    # Only unverify this specific NONG
    nong.is_verified = False
    db.session.commit()
    
    flash(f'NONG unverified for {nong.level.name if nong.level else "level"}.', 'success')
    
    # Redirect back to referring page or nongs page
    referer = request.headers.get('Referer')
    if referer and 'song-submissions' in referer:
        return redirect(url_for('admin_song_submissions', status='all'))
    return redirect(url_for('admin_level_nongs', level_id=nong.level_id))


@app.route('/admin/level/nongs/<int:nong_id>/delete', methods=['POST'])
@login_required
def delete_level_nong(nong_id):
    """Delete a NONG from a level."""
    _require_perm('manage_levels')
    nong = LevelNong.query.get_or_404(nong_id)
    level_id = nong.level_id

    # Delete file
    try:
        file_path = os.path.join(app.root_path, 'static', 'songs', nong.song_filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass

    db.session.delete(nong)
    db.session.commit()

    flash('NONG deleted.', 'success')
    return redirect(url_for('admin_level_nongs', level_id=level_id))


if __name__ == '__main__':
    def _start_backup_scheduler():
        def _loop():
            while True:
                with app.app_context():
                    _backup_once()
                time.sleep(60 * 60)
        threading.Thread(target=_loop, daemon=True).start()
    if os.environ.get('WERKZEUG_RUN_MAIN') in (None, 'true'):
        _start_backup_scheduler()
    # Bind to all interfaces
    host = os.environ.get('HOST', '0.0.0.0')
    try:
        port = int(os.environ.get('PORT', '6890'))
    except Exception:
        port = 6890
    debug = str(os.environ.get('FLASK_DEBUG') or os.environ.get('DEBUG') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    app.run(host=host, debug=debug, port=port)
