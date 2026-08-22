"""
blueprints/auth.py – Authentication: login, register, logout, guidelines, Discord OAuth.
"""
import uuid
import secrets
import urllib.parse
import urllib.request
import urllib.error
import json
from datetime import datetime

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, session, abort)
from flask_login import login_user, logout_user, login_required, current_user

from config import db, bcrypt
from models import User
from auxiliary.settings_helpers import (
    needs_guidelines_acceptance, get_guidelines_url,
    get_guidelines_reset_ts
)
from auxiliary.discord_webhooks import add_site_log, discord_oauth_ready
from auxiliary.discord_webhooks import (
    get_discord_client_id, get_discord_client_secret, get_discord_redirect_uri
)
from utils.security import (
    get_csrf_token, validate_csrf, verify_captcha, trim_pw,
    get_captcha_provider, get_captcha_site_key
)

auth_bp = Blueprint('auth', __name__)


# ── Login ─────────────────────────────────────────────────────────────────────

@auth_bp.route('/login', methods=['GET'])
def login():
    """Discord-only login. Password authentication has been retired."""
    if current_user.is_authenticated:
        return redirect(url_for('main.home'))
    return render_template('login.html', captcha_provider='', captcha_site_key='', discord_oauth_ready=discord_oauth_ready())


@auth_bp.route('/register', methods=['GET'])
def register():
    """Registration happens automatically through Discord OAuth."""
    return redirect(url_for('auth.discord_login') if discord_oauth_ready() else url_for('auth.login'))


@auth_bp.route('/login/2fa', methods=['GET', 'POST'])
def verify_2fa():
    uid = session.get('pending_2fa_user_id')
    if not uid:
        return redirect(url_for('auth.login'))
    user = db.session.get(User, int(uid))
    if not user or not getattr(user, 'totp_secret', None):
        session.pop('pending_2fa_user_id', None)
        return redirect(url_for('auth.login'))
    if request.method == 'POST':
        from utils.security import totp_verify
        code = (request.form.get('totp_code') or '').strip()
        if totp_verify(user.totp_secret, code):
            if bool(getattr(user, 'is_disabled', False)):
                flash('Your account has been disabled.', 'danger')
                return redirect(url_for('auth.login'))
            session.clear()
            login_user(user)
            import time as _t
            session['login_ts'] = _t.time()
            session.pop('pending_2fa_user_id', None)
            add_site_log('login', f'{user.username} logged in with Discord + 2FA.', actor_user_id=user.id)
            return redirect(url_for('main.home'))
        flash('Invalid authenticator code.', 'danger')
        return redirect(url_for('auth.verify_2fa'))
    return render_template('verify_2fa.html')


# ── Logout ────────────────────────────────────────────────────────────────────

@auth_bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.home'))


# ── Guidelines ────────────────────────────────────────────────────────────────

@auth_bp.route('/guidelines', methods=['GET', 'POST'])
@login_required
def guidelines():
    if request.method == 'POST':
        if not (request.form.get('agree') or '').strip():
            flash('You must agree to continue.', 'danger')
            return redirect(url_for('auth.guidelines', next=request.args.get('next') or ''))
        current_user.guidelines_accepted_at = datetime.utcnow()
        db.session.commit()
        add_site_log('guidelines_accept', f'{current_user.username} accepted guidelines.', actor_user_id=current_user.id)
        nxt = (request.args.get('next') or '').strip()
        if not nxt.startswith('/'):
            nxt = url_for('main.home')
        return redirect(nxt)
    nxt = (request.args.get('next') or '').strip()
    if nxt and not nxt.startswith('/'):
        nxt = ''
    return render_template('guidelines_accept.html', guidelines_url=get_guidelines_url(), next=nxt)


# ── Discord OAuth ─────────────────────────────────────────────────────────────

@auth_bp.route('/discord/login')
def discord_login():
    if getattr(current_user, 'is_authenticated', False):
        return redirect(url_for('main.home'))
    if not discord_oauth_ready():
        flash('Discord login is not configured.', 'danger')
        return redirect(url_for('auth.login'))
    state = uuid.uuid4().hex
    session['discord_oauth_state'] = state
    session['discord_oauth_mode'] = 'login'
    session['discord_oauth_next'] = (request.args.get('next') or url_for('main.home'))
    params = {
        'client_id': get_discord_client_id(),
        'redirect_uri': get_discord_redirect_uri(),
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
        'prompt': 'consent'
    }
    url = 'https://discord.com/api/oauth2/authorize?' + urllib.parse.urlencode(params)
    return redirect(url)


@auth_bp.route('/discord/link')
def discord_link():
    mode = (request.args.get('mode') or '').strip().lower() or 'profile'
    if not getattr(current_user, 'is_authenticated', False) and mode != 'register':
        return redirect(url_for('auth.login'))
    if not discord_oauth_ready():
        flash('Discord linking is not configured.', 'danger')
        return redirect(request.referrer or url_for('main.home'))
    state = uuid.uuid4().hex
    session['discord_oauth_state'] = state
    session['discord_oauth_mode'] = mode
    session['discord_oauth_next'] = (request.args.get('next') or request.referrer or url_for('main.home'))
    params = {
        'client_id': get_discord_client_id(),
        'redirect_uri': get_discord_redirect_uri(),
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
        'prompt': 'consent'
    }
    url = 'https://discord.com/api/oauth2/authorize?' + urllib.parse.urlencode(params)
    return redirect(url)


@auth_bp.route('/discord/callback')
def discord_callback():
    err = (request.args.get('error') or '').strip()
    if err:
        flash('Discord linking cancelled.', 'warning')
        return redirect(session.get('discord_oauth_next') or url_for('main.home'))
    code = (request.args.get('code') or '').strip()
    state = (request.args.get('state') or '').strip()
    want_state = (session.get('discord_oauth_state') or '').strip()
    if not code or not state or state != want_state:
        add_site_log('account_link_fail', 'Discord linking failed: state mismatch or missing code.',
                     actor_user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None))
        flash('Discord linking failed.', 'danger')
        return redirect(url_for('main.home'))
    session.pop('discord_oauth_state', None)
    mode = (session.get('discord_oauth_mode') or '').strip().lower() or 'profile'
    nxt = session.get('discord_oauth_next') or url_for('main.home')
    session.pop('discord_oauth_mode', None)
    session.pop('discord_oauth_next', None)
    try:
        me = _exchange_discord_code(code)
    except Exception:
        flash('Discord linking failed.', 'danger')
        return redirect(nxt)
    if not me:
        flash('Discord linking failed.', 'danger')
        return redirect(nxt)

    discord_id = str(me.get('id') or '').strip()
    username = str(me.get('username') or '').strip()
    discrim = str(me.get('discriminator') or '').strip()
    avatar = str(me.get('avatar') or '').strip()
    if not discord_id:
        flash('Discord linking failed.', 'danger')
        return redirect(nxt)
    tag = username if not discrim or discrim == '0' else f'{username}#{discrim}'

    if mode == 'login':
        u = User.query.filter(User.discord_id == discord_id).first()
        if u:
            if bool(getattr(u, 'is_disabled', False)):
                flash('Your account has been disabled.', 'danger')
                return redirect(url_for('auth.login'))
            # Two-factor verification applies to Discord login too
            if getattr(u, 'totp_secret', None):
                from utils.security import totp_verify
                totp_code = (request.args.get('totp_code') or request.form.get('totp_code') or '').strip()
                if not totp_verify(u.totp_secret, totp_code):
                    session['pending_2fa_user_id'] = u.id  # retry via password form hint
                    flash('Enter your authenticator code to finish logging in.', 'warning')
                    return redirect(url_for('auth.verify_2fa'))
            login_user(u)
            import time as _t2
            session['login_ts'] = _t2.time()
            add_site_log('login', f'{u.username} logged in with Discord.', actor_user_id=u.id)
            flash('Logged in with Discord.', 'success')
            if needs_guidelines_acceptance(u):
                return redirect(url_for('auth.guidelines', next=nxt or url_for('main.home')))
            return redirect(nxt or url_for('main.home'))
        # AUTO-REGISTRATION: no account linked to this Discord -> create one
        # instantly. Original username derived from the Discord name (made
        # unique); user can change it later in profile settings.
        base_name = ''.join(ch for ch in username if ch.isalnum() or ch in ('_', '.', '-'))[:24] or 'player'
        candidate = base_name
        suffix = 1
        while User.query.filter_by(username=candidate).first():
            candidate = f'{base_name}{suffix}'
            suffix += 1
            if suffix > 999:
                candidate = f"{base_name}{secrets.token_hex(3)}"
                break
        new_user = User(
            username=candidate,
            password='',  # Discord-only account: no password
            discord_id=discord_id,
            discord_username=username[:100],
            discord_avatar=avatar[:100],
            discord_tag=tag[:100],
            bcoins=50,
        )
        db.session.add(new_user)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Account creation failed. Please try again.', 'danger')
            return redirect(url_for('auth.login'))
        add_site_log('account_create', f'Account auto-created via Discord: {new_user.username}.', actor_user_id=new_user.id)
        login_user(new_user)
        import time as _t3
        session['login_ts'] = _t3.time()
        flash(f'Welcome! Your account "{new_user.username}" is ready. You can change your display name in Settings.', 'success')
        return redirect(url_for('auth.guidelines', next=url_for('profile.edit_profile')))

    # Logged-in user manually linking a Discord account (profile settings)
    if getattr(current_user, 'is_authenticated', False):
        other = User.query.filter(User.discord_id == discord_id, User.id != current_user.id).first()
        if other:
            flash('That Discord account is already linked to another user.', 'warning')
            return redirect(url_for('profile.edit_profile'))
        current_user.discord_id = discord_id
        current_user.discord_username = username[:100]
        current_user.discord_avatar = avatar[:100]
        if not current_user.discord_tag:
            current_user.discord_tag = tag[:100]
        db.session.commit()
        add_site_log('account_link', f'{current_user.username} linked Discord {tag}.', actor_user_id=current_user.id)
        flash('Discord linked.', 'success')
        return redirect(url_for('profile.edit_profile'))

    flash('Discord linking failed.', 'danger')
    return redirect(url_for('auth.login'))


@auth_bp.route('/discord/unlink', methods=['POST'])
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
    return redirect(url_for('profile.edit_profile'))


def _exchange_discord_code(code: str) -> dict:
    """Exchange an OAuth2 code for user info. Returns the /users/@me dict or None."""
    base_headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    token_payload = urllib.parse.urlencode({
        'client_id': get_discord_client_id(),
        'client_secret': get_discord_client_secret(),
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': get_discord_redirect_uri(),
        'scope': 'identify'
    }).encode('utf-8')
    token_req = urllib.request.Request(
        'https://discord.com/api/oauth2/token',
        data=token_payload,
        headers={'Content-Type': 'application/x-www-form-urlencoded', **base_headers}
    )
    with urllib.request.urlopen(token_req, timeout=8) as resp:
        token_json = json.loads(resp.read().decode('utf-8', 'ignore') or '{}')
    access = token_json.get('access_token')
    if not access:
        return None
    me_req = urllib.request.Request(
        'https://discord.com/api/users/@me',
        headers={'Authorization': f'Bearer {access}', **base_headers}
    )
    with urllib.request.urlopen(me_req, timeout=8) as resp:
        return json.loads(resp.read().decode('utf-8', 'ignore') or '{}')


# ── Account deletion (soft-delete with grace period) ──────────────────────────

from datetime import timedelta

@auth_bp.route('/profile/delete', methods=['GET', 'POST'])
@login_required
def delete_account():
    try:
        user_obj = current_user._get_current_object()
    except Exception:
        user_obj = None
    if not user_obj or not getattr(user_obj, 'id', None):
        flash('You must be logged in to delete your account.', 'danger')
        return redirect(url_for('auth.login'))
    uid = int(user_obj.id)
    uname = str(getattr(user_obj, 'username', '') or '')
    u = db.session.get(User, uid)
    if not u:
        flash('Account not found.', 'danger')
        return redirect(url_for('main.home'))
    if bool(getattr(u, 'is_admin', False)) or str(getattr(u, 'rank', '') or '') == 'admin':
        flash('Admin accounts cannot be deleted from here.', 'danger')
        return redirect(url_for('main.home'))
    if request.method == 'GET':
        return render_template('confirm_delete.html', username=uname)
    confirm = (request.form.get('confirm_username') or '').strip()
    if confirm != uname:
        flash('Username did not match. Account deletion cancelled.', 'danger')
        return redirect(url_for('profile.edit_profile'))
    grace_days = 7
    u.scheduled_deletion_at = datetime.utcnow() + timedelta(days=grace_days)
    u.is_disabled = True
    try:
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        flash('Could not schedule account deletion. Try again.', 'danger')
        return redirect(url_for('main.home'))
    logout_user()
    add_site_log('account_delete', f'Account deletion scheduled for {uname} (grace period {grace_days}d).', actor_user_id=None)
    flash(f'Your account has been disabled and will be permanently deleted in {grace_days} days. Log in within that time to cancel.', 'success')
    return redirect(url_for('main.home'))


@auth_bp.route('/profile/delete/cancel', methods=['POST'])
@login_required
def cancel_delete():
    try:
        user_obj = current_user._get_current_object()
    except Exception:
        user_obj = None
    if not user_obj or not getattr(user_obj, 'id', None):
        flash('You must be logged in to cancel deletion.', 'danger')
        return redirect(url_for('auth.login'))
    u = db.session.get(User, int(user_obj.id))
    if not u or not u.scheduled_deletion_at:
        flash('No deletion is scheduled for your account.', 'info')
        return redirect(url_for('profile.edit_profile'))
    u.scheduled_deletion_at = None
    u.is_disabled = False
    try:
        db.session.commit()
        add_site_log('account_delete', f'Account deletion cancelled for {u.username}.', actor_user_id=u.id)
        flash('Account deletion has been cancelled.', 'success')
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        flash('Could not cancel deletion. Try again.', 'danger')
    return redirect(url_for('profile.edit_profile'))
