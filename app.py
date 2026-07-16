"""
app.py – Application entry point.

Initialises Flask, registers all blueprints, attaches before/after-request
hooks, Jinja2 globals, error handlers and the startup / shutdown lifecycle.
"""
import atexit
import logging
import os
import shutil
import sys
import time
import uuid

from flask import (
    redirect, url_for, request, flash, abort, session
)
from flask_login import current_user, logout_user
from jinja2 import pass_context
from markupsafe import Markup

from config import create_app, db, login_manager
from schema import ensure_schema
from settings_helpers import (
    get_setting, has_perm, get_user_rank, rank_label,
    RANKS, PERMISSIONS, get_rank_permissions,
    get_guidelines_url, get_guidelines_reset_ts,
)
from models import UiTemplate
from discord_webhooks import announce_site_launch, announce_site_shutdown, add_site_log
from utils.security import (
    get_csrf_token, validate_csrf, add_security_headers,
    get_captcha_provider, get_captcha_site_key,
)

# ── Create app ─────────────────────────────────────────────────────────────────
app = create_app()
application = app          # WSGI alias

# ── Logging setup ──────────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_log_dir, exist_ok=True)

_file_handler = logging.FileHandler(os.path.join(_log_dir, 'app.log'), encoding='utf-8')
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
app.logger.addHandler(_file_handler)
app.logger.setLevel(logging.WARNING)
logging.getLogger('werkzeug').addHandler(_file_handler)
logging.getLogger('waitress').addHandler(_file_handler)
# Suppress waitress queue-depth spam (logged every 5s when backlog > 0)
logging.getLogger('waitress').setLevel(logging.ERROR)


def _notify_discord_error(title: str, body: str):
    """Send an error summary to the Discord logs webhook (best-effort)."""
    try:
        from discord_webhooks import send_discord_log_message
        send_discord_log_message(f'[{title}] {body}'[:1900])
    except Exception:
        pass

# ── Blueprints ─────────────────────────────────────────────────────────────────
from blueprints.auth       import auth_bp
from blueprints.main       import main_bp
from blueprints.profile    import profile_bp
from blueprints.admin      import admin_bp
from blueprints.api        import api_bp
from blueprints.roulette   import roulette_bp
from blueprints.leaderboard import leaderboard_bp
from blueprints.shop import shop_bp

app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
app.register_blueprint(profile_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(api_bp)
app.register_blueprint(roulette_bp)
app.register_blueprint(leaderboard_bp)
app.register_blueprint(shop_bp)

# Register recap blueprint only if year recap is conceptually possible
# (the route itself guards with _year_recap_enabled())
from blueprints.recap import recap_bp
app.register_blueprint(recap_bp)

# ── Schema migrations ──────────────────────────────────────────────────────────
with app.app_context():
    from config import db as _db
    import sqlalchemy as _sa
    from models import MarketListing
    _db.create_all()
    inspector = _sa.inspect(_db.engine)
    shop_cols = {c['name'] for c in inspector.get_columns('shop_item')}
    if 'is_limited' not in shop_cols:
        _db.session.execute(_sa.text('ALTER TABLE shop_item ADD COLUMN is_limited BOOLEAN DEFAULT 0'))
        _db.session.commit()
    if 'expires_at' not in shop_cols:
        _db.session.execute(_sa.text('ALTER TABLE shop_item ADD COLUMN expires_at DATETIME'))
        _db.session.commit()
    ls_cols = {c['name'] for c in inspector.get_columns('level_submission')}
    for col, col_type in [('song_name', 'TEXT'), ('artist_name', 'TEXT'), ('song_id', 'INTEGER'), ('official_song_id', 'INTEGER'), ('thumbnail_filename', 'TEXT')]:
        if col not in ls_cols:
            _db.session.execute(_sa.text(f'ALTER TABLE level_submission ADD COLUMN {col} {col_type}'))
            _db.session.commit()
    user_cols = {c['name'] for c in inspector.get_columns('user')}
    if 'is_donor' not in user_cols:
        _db.session.execute(_sa.text('ALTER TABLE user ADD COLUMN is_donor BOOLEAN DEFAULT 0'))
        _db.session.commit()
    if 'scheduled_deletion_at' not in user_cols:
        _db.session.execute(_sa.text('ALTER TABLE user ADD COLUMN scheduled_deletion_at DATETIME'))
        _db.session.commit()

# ── Login manager ──────────────────────────────────────────────────────────────
from models import User

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


@login_manager.unauthorized_handler
def _unauthorized():
    if request.path.startswith('/api/'):
        abort(401)
    flash('Please log in to access this page.', 'warning')
    return redirect(url_for('auth.login', next=request.url))


# ── DB bootstrap (once per process) ───────────────────────────────────────────
_db_bootstrapped = False

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
        announce_site_launch(app)
    except Exception:
        pass
    _db_bootstrapped = True


# ── Auto Database Backup ──────────────────────────────────────────────────────

_last_backup_check_ts = 0

def _do_auto_backup():
    """Create an automatic database backup if enough time has passed."""
    try:
        from settings_helpers import get_setting, set_setting
        interval_hours = int(float(str(get_setting('auto_backup_interval_hours') or '24').strip() or '0'))
    except Exception:
        interval_hours = 24
    if interval_hours <= 0:
        return
    try:
        last_ts = float(str(get_setting('last_auto_backup_ts') or '0').strip() or '0')
    except Exception:
        last_ts = 0
    now_ts = time.time()
    if last_ts and (now_ts - last_ts) < interval_hours * 3600:
        return
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
            return
        from datetime import datetime
        ts = datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')
        dst = os.path.join(dst_dir, f'auto-{ts}-{uuid.uuid4().hex[:8]}.db')
        shutil.copy2(src, dst)
        set_setting('last_auto_backup_ts', str(now_ts))
        try:
            add_site_log('admin_action', 'Auto-backup created.', actor_user_id=None, send_to_discord=False)
        except Exception:
            pass
    except Exception:
        pass

@app.before_request
def _try_auto_backup():
    global _last_backup_check_ts
    now = time.time()
    if now - _last_backup_check_ts < 3600:
        return
    _last_backup_check_ts = now
    try:
        _do_auto_backup()
    except Exception:
        pass


# ── Rate limiting ──────────────────────────────────────────────────────────────
from utils.security import rate_limited

@app.before_request
def _rate_limit_requests():
    if request.path.startswith('/api/'):
        if rate_limited('api', limit=60, window_seconds=60):
            abort(429)
        return
    if request.method == 'POST':
        if request.endpoint in ('auth.login', 'auth.register'):
            if rate_limited(f'ep:{request.endpoint}', limit=15, window_seconds=60):
                abort(429)
        else:
            if rate_limited('post', limit=90, window_seconds=60):
                abort(429)


# ── CSRF ───────────────────────────────────────────────────────────────────────
@app.before_request
def _enforce_csrf():
    if request.method == 'POST':
        if not validate_csrf():
            try:
                flash('Security check failed. Please refresh the page and try again.', 'danger')
                return redirect(request.referrer or url_for('main.home'))
            except Exception:
                abort(403)


# ── Security headers ───────────────────────────────────────────────────────────
@app.after_request
def _security_headers(resp):
    return add_security_headers(resp)


# ── Request timing ─────────────────────────────────────────────────────────────
@app.before_request
def _start_timer():
    request._start_time = time.monotonic()

@app.after_request
def _log_request(resp):
    if getattr(request, '_start_time', None):
        elapsed = time.monotonic() - request._start_time
        if elapsed > 5.0 or resp.status_code >= 500:
            app.logger.warning('%s %s %s %.2fs', request.method, request.path, resp.status_code, elapsed)
    return resp


# ── Last-seen heartbeat ────────────────────────────────────────────────────────
from datetime import datetime, timezone

@app.before_request
def _touch_last_seen_at():
    if request.endpoint in ('static',):
        return
    if not getattr(current_user, 'is_authenticated', False):
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        last = float(session.get('last_seen_ping_ts') or 0)
    except Exception:
        last = 0
    if last and (now.timestamp() - last) < 120:
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


# ── Guidelines / account-disabled enforcement ──────────────────────────────────
def _needs_guidelines_acceptance(user) -> bool:
    try:
        accepted_at = getattr(user, 'guidelines_accepted_at', None)
    except Exception:
        accepted_at = None
    if not accepted_at:
        return True
    reset_ts = get_guidelines_reset_ts()
    if not reset_ts:
        return False
    try:
        reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return False
    return accepted_at < reset_dt


# ── Process scheduled account deletions (throttled) ────────────────────────────

_last_deletion_check_ts = 0

@app.before_request
def _process_scheduled_deletions():
    global _last_deletion_check_ts
    now = time.time()
    if now - _last_deletion_check_ts < 300:
        return
    _last_deletion_check_ts = now
    from models import User
    from datetime import datetime as _dt
    now_dt = _dt.utcnow()
    try:
        overdue = User.query.filter(
            User.scheduled_deletion_at.isnot(None),
            User.scheduled_deletion_at <= now_dt
        ).all()
        for u in overdue:
            uname = str(getattr(u, 'username', '') or '')
            try:
                db.session.delete(u)
                db.session.commit()
                add_site_log('account_delete', f'Scheduled deletion processed for {uname}.', actor_user_id=None)
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


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
            return redirect(url_for('auth.login'))
    except Exception:
        pass
    if not _needs_guidelines_acceptance(current_user):
        return
    if request.endpoint in ('auth.guidelines', 'auth.logout'):
        return
    nxt = request.full_path if (request.query_string or b'') else request.path
    return redirect(url_for('auth.guidelines', next=nxt))


# ── Jinja2 globals & context processors ───────────────────────────────────────
def get_ui_template(slug: str):
    if slug in ('new', 'new2'):
        return None
    try:
        return UiTemplate.query.filter_by(slug=slug, is_active=True).first()
    except Exception:
        return None

app.jinja_env.globals['csrf_token']          = get_csrf_token
app.jinja_env.globals['get_setting']         = get_setting
app.jinja_env.globals['get_captcha_provider'] = get_captcha_provider
app.jinja_env.globals['get_captcha_site_key'] = get_captcha_site_key
app.jinja_env.globals['get_ui_template']     = get_ui_template

@app.template_filter('evaluate')
@pass_context
def evaluate_template(context, val):
    from jinja2 import Template
    t = Template(val)
    try:
        return Markup(t.render(context))
    except Exception:
        return Markup('')


def _setting_truthy(key: str) -> bool:
    raw = str(get_setting(key) or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


@app.context_processor
def _inject_rank_helpers():
    try:
        disable_thumbs = str(
            request.cookies.get('disable_thumbnails') or ''
        ).strip().lower() in ('1', 'true', 'yes', 'on')
    except Exception:
        disable_thumbs = False
    return {
        'has_perm':           has_perm,
        'user_rank':          get_user_rank,
        'rank_label':         rank_label,
        'rank_permissions':   get_rank_permissions(),
        'permission_defs':    PERMISSIONS,
        'rank_list':          [(r, rank_label(r)) for r in RANKS],
        'year_recap_enabled': _setting_truthy('year_recap_enabled'),
        'disable_thumbnails': disable_thumbs,
        'footer_discord_url': str(get_setting('footer_discord_url') or '').strip(),
        'april_fools_enabled': _setting_truthy('april_fools_enabled'),
    }


# ── Error handlers ─────────────────────────────────────────────────────────────
from flask import render_template
from werkzeug.exceptions import RequestEntityTooLarge


@app.errorhandler(RequestEntityTooLarge)
def _handle_too_large(_e):
    flash('File too large (max 10MB).', 'danger')
    try:
        return redirect(request.referrer or url_for('main.home'))
    except Exception:
        return 'File too large (max 10MB).', 413


@app.errorhandler(404)
def _handle_404(_e):
    return render_template('error.html', error_code=404, error_title='Page Not Found', error_message="The page you're looking for doesn't exist."), 404


@app.errorhandler(403)
def _handle_403(_e):
    return render_template('error.html', error_code=403, error_title='Access Denied', error_message="You don't have permission to access this page."), 403


@app.errorhandler(500)
def _handle_500(_e):
    try:
        db.session.rollback()
    except Exception:
        pass
    import traceback, sys
    tb = traceback.format_exc()
    app.logger.error('500 error: %s', tb)
    _notify_discord_error('500 Server Error', f'`{request.method} {request.path}`\n```{tb[-1500:]}```')
    return render_template('error.html', error_code=500, error_title='Server Error', error_message='Something went wrong on our end. Please try again later.'), 500


@app.errorhandler(Exception)
def _handle_uncaught(exc):
    try:
        db.session.rollback()
    except Exception:
        pass
    import traceback, sys
    tb = traceback.format_exc()
    app.logger.error('Unhandled exception: %s', tb)
    _notify_discord_error('Unhandled Exception', f'`{request.method} {request.path}`\n```{tb[-1500:]}```')
    return render_template('error.html', error_code=500, error_title='Server Error', error_message='Something went wrong on our end. Please try again later.'), 500


@app.errorhandler(429)
def _handle_429(_e):
    return render_template('error.html', error_code=429, error_title='Too Many Requests', error_message='Please slow down and try again shortly.'), 429


@app.errorhandler(405)
def _handle_405(_e):
    return render_template('error.html', error_code=405, error_title='Method Not Allowed', error_message='This action is not supported.'), 405


# ── Shutdown hook ──────────────────────────────────────────────────────────────
import functools
atexit.register(functools.partial(announce_site_shutdown, app))


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import os
    _workers = os.environ.get('WAITRESS_WORKERS', '4')
    try:
        _nworkers = max(1, int(_workers))
    except Exception:
        _nworkers = 4
    import waitress
    waitress.serve(app, host='0.0.0.0', port=6890, threads=_nworkers, channel_timeout=30)
