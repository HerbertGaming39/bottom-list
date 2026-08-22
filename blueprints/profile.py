"""
blueprints/profile.py – User profile, edit profile, settings, stats, recap,
record/level submission, unbeaten levels, export.
"""
import os
import re
import secrets
from datetime import datetime

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, session, Response, abort, jsonify, current_app)
from flask_login import login_required, current_user

from config import db
from models import User, Level, Record, LevelSubmission, MapPack, Clan, ShopItem, UserBlock
from auxiliary.settings_helpers import (has_perm, get_user_rank, rank_label, year_recap_enabled,
                               record_proof_top_n_for_list,
                               auto_points_applies_to, get_auto_points_rules_text_for,
                               parse_auto_points_rules, points_for_position)
from auxiliary.discord_webhooks import add_site_log, send_discord_message
from utils.security import (verify_captcha, get_captcha_provider, get_captcha_site_key,
                             trim_pw, save_uploaded_file,
                             _ALLOWED_IMAGE_EXTS, _ALLOWED_AUDIO_EXTS)
from auxiliary.points import (sum_approved_points_for_user, sum_approved_points_by_list_for_user,
                    recalc_user_points_for_users)

profile_bp = Blueprint('profile', __name__)


def utcnow():
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Profile view ──────────────────────────────────────────────────────────────

@profile_bp.route('/profile')
@login_required
def profile():
    return redirect(url_for('profile.user_profile', user_id=current_user.id))


@profile_bp.route('/user/<int:user_id>')
def user_profile(user_id):
    from utils.security import rate_limited, client_ip
    if not getattr(current_user, 'is_authenticated', False):
        if rate_limited('profile_view', limit=30, window_seconds=60):
            abort(429)
    user = User.query.get_or_404(user_id)
    is_self = getattr(current_user, 'is_authenticated', False) and int(getattr(current_user, 'id', 0)) == int(user.id)

    records = Record.query.filter_by(user_id=user.id).all() if is_self else Record.query.filter_by(user_id=user.id, status='approved').all()
    submissions = LevelSubmission.query.filter_by(user_id=user.id).all()
    display_points = sum_approved_points_for_user(user.id)
    by_list = sum_approved_points_by_list_for_user(user.id)
    main_points = float(by_list.get('main') or 0.0)
    challenge_points = float(by_list.get('challenge') or 0.0)
    platformer_points = float(by_list.get('platformer') or 0.0)

    beaten_level_ids = {r.level_id for r in records if str(r.status or '') == 'approved'}
    beaten_levels = Level.query.filter(Level.id.in_(list(beaten_level_ids))).all() if beaten_level_ids else []

    def beaten_for(lt):
        return [l for l in beaten_levels if l.list_type == lt and l.is_verified and l.position is not None]

    beaten_main = beaten_for('main')
    beaten_challenge = beaten_for('challenge')
    beaten_platformer = beaten_for('platformer')

    def total_for(lt):
        return Level.query.filter(Level.is_verified == True, Level.list_type == lt, Level.position.isnot(None)).count()

    total_main = total_for('main')
    total_challenge = total_for('challenge')
    total_platformer = total_for('platformer')

    def pct(a, b):
        return round(a / b * 100, 1) if b else 0.0

    packs = []
    try:
        packs = MapPack.query.all()
    except Exception:
        pass
    beaten_packs = [p for p in packs if (ids := [int(i.strip()) for i in (p.level_ids or '').split(',') if i.strip().isdigit()]) and all(lid in beaten_level_ids for lid in ids)]

    total_records = Record.query.filter_by(user_id=user.id).count()
    approved_records = len([r for r in records if str(r.status or '') == 'approved'])
    total_submissions = len(submissions)
    approved_submissions = len([s for s in submissions if s.status == 'approved'])

    recent_activity = sorted(
        [{'type': 'record', 'item': r, 'date': r.submitted_at, 'status': r.status} for r in records] +
        [{'type': 'submission', 'item': s, 'date': s.submitted_at, 'status': s.status} for s in submissions],
        key=lambda x: (x['date'] or datetime.min), reverse=True
    )[:10]

    # Notifications (self only)
    approved_record_notifs = rejected_record_notifs = []
    approved_submission_notifs = rejected_submission_notifs = []
    if is_self:
        def _record_notif(status):
            rows = (db.session.query(Record, Level).join(Level, Level.id == Record.level_id)
                    .filter(Record.user_id == user.id, Record.status == status)
                    .order_by(Record.submitted_at.desc()).limit(40).all())
            return [{'id': int(r.id), 'level_name': lv.name, 'level_creator': lv.creator} for r, lv in rows]
        def _sub_notif(status):
            rows = LevelSubmission.query.filter(LevelSubmission.user_id == user.id, LevelSubmission.status == status).order_by(LevelSubmission.submitted_at.desc()).limit(40).all()
            return [{'id': int(s.id), 'name': s.name, 'creator': s.creator} for s in rows]
        try:
            approved_record_notifs = _record_notif('approved')
            rejected_record_notifs = _record_notif('rejected')
            approved_submission_notifs = _sub_notif('approved')
            rejected_submission_notifs = _sub_notif('rejected')
        except Exception:
            pass

    equipped_ids = [id for id in [user.equipped_avatar_item_id, user.equipped_background_item_id, user.equipped_deco_item_id] if id]
    equipped_items = {i.id: i for i in ShopItem.query.filter(ShopItem.id.in_(equipped_ids)).all()} if equipped_ids else {}
    blocked_user_ids = set()
    profile_blurred = False
    if getattr(current_user, 'is_authenticated', False):
        blocked_user_ids = {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=current_user.id).all()}
        block_entry = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user.id).first()
        if block_entry:
            profile_blurred = block_entry.has_option('hide_profile')
    return render_template('profile.html',
                           user=user,
                           records=records,
                           submissions=submissions,
                           equipped_items=equipped_items,
                           display_points=display_points,
                           main_points=main_points,
                           challenge_points=challenge_points,
                           platformer_points=platformer_points,
                           record_completion_percentage=pct(approved_records, total_records),
                           submission_success_rate=pct(approved_submissions, total_submissions),
                           approved_records_count=approved_records,
                           total_records_count=total_records,
                           approved_submissions_count=approved_submissions,
                           total_submissions_count=total_submissions,
                           beaten_main_count=len(beaten_main),
                           total_main_count=total_main,
                           main_percent=pct(len(beaten_main), total_main),
                           beaten_challenge_count=len(beaten_challenge),
                           total_challenge_count=total_challenge,
                           challenge_percent=pct(len(beaten_challenge), total_challenge),
                           beaten_platformer_count=len(beaten_platformer),
                           total_platformer_count=total_platformer,
                           platformer_percent=pct(len(beaten_platformer), total_platformer),
                           beaten_pack_count=len(beaten_packs),
                           total_pack_count=len(packs),
                           pack_percent=pct(len(beaten_packs), len(packs)),
                           beaten_packs=beaten_packs,
                           recent_activity=recent_activity,
                           approved_record_notifs=approved_record_notifs,
                           rejected_record_notifs=rejected_record_notifs,
                           approved_submission_notifs=approved_submission_notifs,
                           rejected_submission_notifs=rejected_submission_notifs,
                            blocked_user_ids=blocked_user_ids,
                            profile_blurred=profile_blurred)


# ── Edit profile ──────────────────────────────────────────────────────────────

@profile_bp.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        from utils.security import validate_url_scheme, validate_username
        # Display name: cosmetic, freely changeable
        new_display = (request.form.get('display_name') or '').strip()
        if len(new_display) > 32:
            flash('Display name must be at most 32 characters.', 'danger')
            return redirect(url_for('profile.edit_profile'))
        current_user.display_name = new_display or None
        new_bio = (request.form.get('bio') or '').strip()
        if len(new_bio) > 500:
            flash('Bio must be at most 500 characters.', 'danger')
            return redirect(url_for('profile.edit_profile'))
        current_user.bio = new_bio
        yt_error = validate_url_scheme(request.form.get('youtube_link'))
        if yt_error:
            flash(yt_error, 'danger')
            return redirect(url_for('profile.edit_profile'))
        current_user.youtube_link = (request.form.get('youtube_link') or '').strip()
        for field, config_key, attr in [
            ('avatar_file', 'PROFILE_MEDIA_UPLOAD_FOLDER', 'avatar_filename'),
            ('banner_file', 'PROFILE_MEDIA_UPLOAD_FOLDER', 'banner_filename'),
        ]:
            f = request.files.get(field)
            if f and getattr(f, 'filename', None):
                unique, err = save_uploaded_file(f, current_app.config[config_key], _ALLOWED_IMAGE_EXTS, 'image/')
                if not unique:
                    flash('Upload rate limit exceeded. Please wait a minute.' if err == 'rate_limited' else f'{attr.split("_")[0].title()} must be a valid image file.', 'danger')
                    return redirect(url_for('profile.edit_profile'))
                old = str(getattr(current_user, attr, '') or '')
                if old:
                    try:
                        os.remove(os.path.join(current_app.config[config_key], old))
                    except Exception:
                        pass
                setattr(current_user, attr, unique)
        db.session.commit()
        add_site_log('profile_edit', f'{current_user.username} edited profile.', actor_user_id=current_user.id)
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile.user_profile', user_id=current_user.id))
    equipped_ids = [id for id in [current_user.equipped_avatar_item_id, current_user.equipped_background_item_id, current_user.equipped_deco_item_id] if id]
    equipped_items = ShopItem.query.filter(ShopItem.id.in_(equipped_ids)).all() if equipped_ids else []
    return render_template('edit_profile.html', equipped_items=equipped_items)


# ── Username change ───────────────────────────────────────────────────────────

@profile_bp.route('/profile/change_username', methods=['POST'])
@login_required
def change_username():
    from datetime import timedelta
    from utils.security import validate_username
    # Cooldown: prevent identity rotation to evade moderation
    if current_user.username_changed_at:
        try:
            if (utcnow() - current_user.username_changed_at) < timedelta(days=30):
                flash('You can only change your username once every 30 days.', 'danger')
                return redirect(url_for('profile.user_profile', user_id=current_user.id))
        except Exception:
            pass
    new_username = (request.form.get('new_username') or '').strip()
    uname_error = validate_username(new_username)
    if uname_error:
        flash(uname_error, 'danger')
        return redirect(url_for('profile.user_profile', user_id=current_user.id))
    if User.query.filter_by(username=new_username).first():
        flash('Username already taken.', 'danger')
        return redirect(url_for('profile.user_profile', user_id=current_user.id))
    old_username = current_user.username
    current_user.username = new_username
    current_user.username_changed_at = utcnow()
    db.session.commit()
    add_site_log('profile_edit', f'Username changed: {old_username} -> {new_username}.', actor_user_id=current_user.id)
    flash('Username updated.', 'success')
    return redirect(url_for('profile.user_profile', user_id=current_user.id))


# ── Settings ──────────────────────────────────────────────────────────────────

@profile_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()

        if action == 'change_password':
            from config import bcrypt
            from utils.security import trim_pw, rate_limited as _rl
            from datetime import timezone
            if _rl('pw_change', limit=5, window_seconds=300):
                flash('Too many attempts. Please wait a few minutes.', 'danger')
                return redirect(url_for('profile.settings'))
            current_pw = trim_pw(request.form.get('current_password', ''))
            new_pw = trim_pw(request.form.get('new_password', ''))
            new_pw2 = request.form.get('new_password2', '')
            if not current_user.password:
                flash('Your account has no password set (Discord-only). Set one via Discord link flow.', 'danger')
                return redirect(url_for('profile.settings'))
            if not bcrypt.check_password_hash(current_user.password, current_pw):
                flash('Current password is incorrect.', 'danger')
                return redirect(url_for('profile.settings'))
            if len(new_pw) < 8 or not (any(c.isalpha() for c in new_pw) and any(c.isdigit() for c in new_pw)):
                flash('New password must be at least 8 characters with letters and numbers.', 'danger')
                return redirect(url_for('profile.settings'))
            if new_pw != new_pw2:
                flash('New passwords do not match.', 'danger')
                return redirect(url_for('profile.settings'))
            current_user.password = bcrypt.generate_password_hash(new_pw).decode('utf-8')
            # Invalidate all other sessions (this request's session is refreshed below)
            current_user.password_changed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.commit()
            session['login_ts'] = utcnow().timestamp()
            add_site_log('password_change', f'{current_user.username} changed password.', actor_user_id=current_user.id)
            flash('Password changed. Other sessions were logged out.', 'success')
            return redirect(url_for('profile.settings'))

        if action == '2fa_begin':
            from utils.security import totp_generate_secret
            session['pending_totp_secret'] = totp_generate_secret()
            flash('Scan the code with your authenticator app, then enter a code to confirm.', 'info')
            return redirect(url_for('profile.settings'))

        if action == '2fa_enable':
            from utils.security import totp_verify
            secret = session.get('pending_totp_secret') or ''
            code = (request.form.get('totp_code') or '').strip()
            if not secret:
                flash('Start the 2FA setup first.', 'warning')
                return redirect(url_for('profile.settings'))
            if not totp_verify(secret, code):
                flash('That code was not valid. Try again.', 'danger')
                return redirect(url_for('profile.settings'))
            current_user.totp_secret = secret
            current_user.password_changed_at = utcnow()  # log out other sessions too
            db.session.commit()
            session.pop('pending_totp_secret', None)
            session['login_ts'] = utcnow().timestamp()
            add_site_log('security_2fa', f'{current_user.username} enabled two-factor auth.', actor_user_id=current_user.id)
            flash('Two-factor authentication is now enabled!', 'success')
            return redirect(url_for('profile.settings'))

        if action == '2fa_disable':
            from utils.security import totp_verify, trim_pw
            from config import bcrypt
            code = (request.form.get('totp_code') or '').strip()
            pw = trim_pw(request.form.get('current_password', ''))
            ok_pw = bool(current_user.password) and bcrypt.check_password_hash(current_user.password, pw)
            if not (ok_pw and current_user.totp_secret and totp_verify(current_user.totp_secret, code)):
                flash('Password or 2FA code incorrect.', 'danger')
                return redirect(url_for('profile.settings'))
            current_user.totp_secret = None
            db.session.commit()
            add_site_log('security_2fa', f'{current_user.username} disabled two-factor auth.', actor_user_id=current_user.id)
            flash('Two-factor authentication disabled.', 'success')
            return redirect(url_for('profile.settings'))

        db.session.commit()
        add_site_log('settings_edit', f'{current_user.username} edited settings.', actor_user_id=current_user.id)
        flash('Settings saved.', 'success')
        resp = redirect(url_for('profile.settings'))
        try:
            want = bool(request.form.get('disable_thumbnails'))
            resp.set_cookie('disable_thumbnails', '1' if want else '0', max_age=365 * 24 * 60 * 60, samesite='Lax')
        except Exception:
            pass
        return resp

    pending_totp = session.get('pending_totp_secret')
    totp_uri = None
    if pending_totp:
        from utils.security import totp_provisioning_uri
        totp_uri = totp_provisioning_uri(pending_totp, current_user.username)
    return render_template('settings.html', totp_uri=totp_uri)


# ── Record submission ─────────────────────────────────────────────────────────

@profile_bp.route('/submit/record', methods=['GET', 'POST'])
@login_required
def submit_record():
    levels = Level.query.order_by(Level.list_type.asc(), db.func.coalesce(Level.position, 0).asc()).all()
    if request.method == 'POST':
        if request.form.get('website'):
            flash('Bot detected.', 'danger')
            return redirect(url_for('profile.submit_record'))
        provider = get_captcha_provider()
        captcha_ok = verify_captcha()
        if captcha_ok is False:
            flash('Invalid hCaptcha.' if provider == 'hcaptcha' else 'Invalid reCAPTCHA.', 'danger')
            return redirect(url_for('profile.submit_record'))
        if captcha_ok is None:
            try:
                user_answer = int(request.form.get('captcha_answer', ''))
            except ValueError:
                user_answer = None
            if user_answer != session.get('captcha_answer'):
                flash('Invalid CAPTCHA answer.', 'danger')
                return redirect(url_for('profile.submit_record'))

        try:
            level_id = int(request.form.get('level_id') or 0)
        except Exception:
            level_id = 0
        selected_lt = (request.form.get('list_type') or 'main').strip().lower()
        if selected_lt == 'challange':
            selected_lt = 'challenge'
        if selected_lt not in ('main', 'challenge', 'platformer'):
            flash('Please choose a list.', 'danger')
            return redirect(url_for('profile.submit_record'))
        video_url = (request.form.get('video_url') or '').strip()
        if video_url:
            from utils.security import validate_url_scheme
            _verr = validate_url_scheme(video_url)
            if _verr:
                flash(_verr, 'danger')
                return redirect(url_for('profile.submit_record'))
        device = request.form.get('device')
        comment = request.form.get('comment')
        beaten_on_vc = request.form.get('beaten_on_vc') == '1'
        if not level_id:
            flash('Please select a level.', 'danger')
            return redirect(url_for('profile.submit_record'))
        lv = db.session.get(Level, level_id)
        if not lv:
            flash('Invalid level selection.', 'danger')
            return redirect(url_for('profile.submit_record'))
        lt = (getattr(lv, 'list_type', '') or 'main').strip().lower()
        if lt != selected_lt:
            flash('That level is not on the selected list.', 'danger')
            return redirect(url_for('profile.submit_record'))
        pos = int(getattr(lv, 'position', 0) or 0)
        top_n = record_proof_top_n_for_list(lt)
        require_video = bool(lt == 'platformer' or (top_n > 0 and pos > 0 and pos <= top_n))
        require_image = bool(lt in ('main', 'challenge') and top_n > 0 and pos > top_n)
        if require_video and not video_url:
            flash('Video proof is required for the platformer list.' if lt == 'platformer' else f'Video proof is required for Top {top_n} levels.', 'danger')
            return redirect(url_for('profile.submit_record'))

        def parse_rating(key: str):
            raw = (request.form.get(key) or '').strip()
            if not raw:
                return None
            try:
                v = int(float(raw))
                return v if 1 <= v <= 10 else None
            except Exception:
                return None

        rating_deco = parse_rating('rating_deco')
        rating_gameplay = parse_rating('rating_gameplay')
        rating_difficulty = parse_rating('rating_difficulty')
        if rating_deco is None or rating_gameplay is None or rating_difficulty is None:
            flash('Deco, Gameplay, and Enjoyment ratings are required.', 'danger')
            return redirect(url_for('profile.submit_record'))

        vc_proof_filename = proof_image_filename = None
        if require_image:
            f = request.files.get('proof_image')
            if not f or not f.filename:
                flash(f'Screenshot proof image is required for levels below Top {top_n}.', 'danger')
                return redirect(url_for('profile.submit_record'))
            unique, err = save_uploaded_file(f, current_app.config['RECORD_PROOF_UPLOAD_FOLDER'], _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                flash('Upload rate limit exceeded.' if err == 'rate_limited' else 'Screenshot proof must be a valid image file.', 'danger')
                return redirect(url_for('profile.submit_record'))
            proof_image_filename = unique
        if beaten_on_vc:
            f = request.files.get('vc_proof')
            if not f or not f.filename:
                flash('VC proof image is required when beaten on VC.', 'danger')
                return redirect(url_for('profile.submit_record'))
            unique, err = save_uploaded_file(f, current_app.config['VC_PROOF_UPLOAD_FOLDER'], _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                flash('Upload rate limit exceeded.' if err == 'rate_limited' else 'VC proof must be a valid image file.', 'danger')
                return redirect(url_for('profile.submit_record'))
            vc_proof_filename = unique

        existing = Record.query.filter_by(user_id=current_user.id, level_id=level_id).first()
        if existing:
            flash('You have already submitted a record for this level.', 'warning')
            return redirect(url_for('profile.submit_record'))

        record = Record(user_id=current_user.id, level_id=level_id, video_url=video_url, device=device,
                        comment=comment, beaten_on_vc=beaten_on_vc, vc_proof_filename=vc_proof_filename,
                        proof_image_filename=proof_image_filename, rating_deco=rating_deco,
                        rating_gameplay=rating_gameplay, rating_difficulty=rating_difficulty, status='pending')
        db.session.add(record)
        db.session.commit()
        add_site_log('record_submit', f'{current_user.username} submitted a record for "{lv.name}" ({lt}).', actor_user_id=current_user.id)
        flash('Record submitted successfully! Waiting for approval.', 'success')
        return redirect(url_for('profile.user_profile', user_id=current_user.id))

    import random
    provider = get_captcha_provider()
    site_key = get_captcha_site_key(provider)
    proof_by_list = {lt: record_proof_top_n_for_list(lt) for lt in ('main', 'challenge', 'platformer')}
    if provider and site_key:
        return render_template('submit_record.html', levels=levels, captcha_provider=provider, captcha_site_key=site_key, record_proof_top_n_by_list=proof_by_list)
    a, b = random.randint(1, 9), random.randint(1, 9)
    session['captcha_answer'] = a + b
    return render_template('submit_record.html', levels=levels, captcha_a=a, captcha_b=b, captcha_provider='', captcha_site_key='', record_proof_top_n_by_list=proof_by_list)


# ── Level submission ──────────────────────────────────────────────────────────

@profile_bp.route('/submit/level', methods=['GET', 'POST'])
@login_required
def submit_level():
    if request.method == 'POST':
        if request.form.get('website'):
            flash('Bot detected.', 'danger')
            return redirect(url_for('profile.submit_level'))
        provider = get_captcha_provider()
        captcha_ok = verify_captcha()
        if captcha_ok is False:
            flash('Invalid hCaptcha.' if provider == 'hcaptcha' else 'Invalid reCAPTCHA.', 'danger')
            return redirect(url_for('profile.submit_level'))
        if captcha_ok is None:
            try:
                user_answer = int(request.form.get('captcha_answer', ''))
            except ValueError:
                user_answer = None
            if user_answer != session.get('captcha_answer'):
                flash('Invalid CAPTCHA answer.', 'danger')
                return redirect(url_for('profile.submit_level'))

        name = (request.form.get('name') or '').strip()
        creator = (getattr(current_user, 'username', '') or '').strip()
        video_url = (request.form.get('video_url') or '').strip()
        gd_level_id = (request.form.get('gd_level_id') or '').strip()
        collaborators = (request.form.get('collaborators') or '').strip()
        from utils.security import validate_url_scheme as _vus
        for _label, _url in (('Video link', video_url), ('Verification link', request.form.get('verified_video_url') or '')):
            if _url:
                _err = _vus(_url)
                if _err:
                    flash(f'{_label}: {_err}', 'danger')
                    return redirect(url_for('profile.submit_level'))
        verified_video_url = (request.form.get('verified_video_url') or '').strip()
        is_verified = request.form.get('is_verified') == 'on'
        if is_verified and not verified_video_url:
            flash('Verification link is required when marked verified.', 'danger')
            return redirect(url_for('profile.submit_level'))
        raw_pos = (request.form.get('proposed_position') or '').strip()
        proposed_position = None
        if raw_pos:
            try:
                proposed_position = max(1, int(raw_pos))
            except Exception:
                pass
        lt = (request.form.get('list_type') or 'main').strip().lower()
        if lt == 'challange':
            lt = 'challenge'
        if lt not in ('main', 'challenge', 'platformer'):
            lt = 'main'
        thumb_unique = None
        thumb_file = request.files.get('thumbnail')
        if thumb_file and thumb_file.filename:
            folder = os.path.join(current_app.root_path, 'static', 'level_thumbs')
            thumb_unique, err = save_uploaded_file(thumb_file, folder, _ALLOWED_IMAGE_EXTS, 'image/')
            if not thumb_unique:
                flash('Upload rate limited.' if err == 'rate_limited' else 'Thumbnail must be a valid image.', 'danger')
                return redirect(url_for('profile.submit_level'))

        song_id_raw = (request.form.get('song_id') or '').strip()
        official_song_id_raw = (request.form.get('official_song_id') or '').strip()

        submission = LevelSubmission(user_id=current_user.id, name=name, creator=creator,
                                     video_url=video_url, gd_level_id=gd_level_id or None,
                                     proposed_position=proposed_position, thumbnail_filename=thumb_unique or None)
        db.session.add(submission)
        db.session.commit()
        add_site_log('level_submit', f'{current_user.username} submitted level "{submission.name}" ({lt}).', actor_user_id=current_user.id)
        flash('Level submitted successfully!', 'success')
        return redirect(url_for('profile.user_profile', user_id=current_user.id))

    import random
    provider = get_captcha_provider()
    site_key = get_captcha_site_key(provider)
    if provider and site_key:
        return render_template('submit_level.html', captcha_provider=provider, captcha_site_key=site_key)
    a, b = random.randint(1, 9), random.randint(1, 9)
    session['captcha_answer'] = a + b
    return render_template('submit_level.html', captcha_a=a, captcha_b=b, captcha_provider='', captcha_site_key='')


# ── Unbeaten / recommended ────────────────────────────────────────────────────

@profile_bp.route('/unbeaten')
@login_required
def unbeaten_levels():
    list_type = (request.args.get('list_type') or 'all').strip().lower()
    if list_type not in ('all', 'main', 'challenge', 'platformer'):
        list_type = 'all'
    beaten_ids = set()
    try:
        beaten_ids = {r[0] for r in db.session.query(Record.level_id).filter(Record.user_id == current_user.id, Record.status == 'approved').distinct().all() if r and r[0]}
    except Exception:
        pass
    from sqlalchemy import case
    unbeaten_q = Level.query.filter(Level.is_verified.is_(True))
    if list_type != 'all':
        unbeaten_q = unbeaten_q.filter(Level.list_type == list_type)
    if beaten_ids:
        unbeaten_q = unbeaten_q.filter(~Level.id.in_(list(beaten_ids)))
    unbeaten_levels_list = unbeaten_q.order_by(
        case((Level.list_type == 'main', 0), (Level.list_type == 'challenge', 1), (Level.list_type == 'platformer', 2), else_=3),
        Level.position.is_(None), Level.position, db.func.lower(Level.name)
    ).all()

    skip_now = str(request.args.get('skip') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    skipped_ids = set()
    try:
        skipped_ids = {int(x) for x in (session.get('rec_skip_ids') or []) if str(x).strip().isdigit()}
    except Exception:
        pass

    rec_level = None
    beaten_levels = Level.query.filter(Level.id.in_(list(beaten_ids))).all() if beaten_ids else []
    beaten_tags = {t.strip().lower() for lv in beaten_levels for t in str(getattr(lv, 'categories', '') or '').split(',') if t.strip()}

    if beaten_tags:
        candidates = Level.query.filter(Level.is_verified.is_(True)).all()
        scored = []
        for lv in candidates:
            if lv.id in beaten_ids or lv.id in skipped_ids:
                continue
            tags = {t.strip().lower() for t in str(getattr(lv, 'categories', '') or '').split(',') if t.strip()}
            overlap = len(tags & beaten_tags)
            if overlap > 0:
                scored.append((overlap, -(int(getattr(lv, 'position', 999999) or 999999)), lv.id, lv))
        if scored:
            scored.sort(reverse=True)
            rec_level = scored[0][3]

    if rec_level is None:
        from sqlalchemy import or_
        rec_q = Level.query.filter(Level.is_verified.is_(True))
        if beaten_ids:
            rec_q = rec_q.filter(~Level.id.in_(list(beaten_ids)))
        if skipped_ids:
            rec_q = rec_q.filter(~Level.id.in_(list(skipped_ids)))
        if list_type != 'all':
            rec_q = rec_q.filter(Level.list_type == list_type)
        rec_level = rec_q.order_by(Level.position.asc().nullslast(), Level.id.asc()).first()

    if skip_now and rec_level is not None:
        skipped_ids.add(int(rec_level.id))
        session['rec_skip_ids'] = sorted(list(skipped_ids))[-500:]
        return redirect(url_for('profile.unbeaten_levels'))

    rec_ratings = {'deco': None, 'gameplay': None, 'enjoyment': None}
    if rec_level:
        rec_rows = Record.query.filter(Record.level_id == rec_level.id, Record.status == 'approved').all()
        def avg(vals):
            nums = [float(v) for v in vals if v is not None]
            return round(sum(nums) / len(nums), 1) if nums else None
        rec_ratings = {
            'deco': avg([getattr(r, 'rating_deco', None) for r in rec_rows]),
            'gameplay': avg([getattr(r, 'rating_gameplay', None) for r in rec_rows]),
            'enjoyment': avg([getattr(r, 'rating_difficulty', None) for r in rec_rows]),
        }

    return render_template('unbeaten_recommended.html', recommended=rec_level, rec_ratings=rec_ratings,
                           unbeaten_levels=unbeaten_levels_list, list_type=list_type)


@profile_bp.route('/recommended')
@login_required
def recommended_levels():
    return redirect(url_for('profile.unbeaten_levels'))


# ── Stats ─────────────────────────────────────────────────────────────────────

@profile_bp.route('/stats')
@login_required
def stats():
    return redirect(url_for('profile.user_stats', user_id=current_user.id))


@profile_bp.route('/user/<int:user_id>/stats')
def user_stats(user_id):
    user = User.query.get_or_404(user_id)
    is_self = getattr(current_user, 'is_authenticated', False) and int(getattr(current_user, 'id', 0)) == int(user.id)

    def total_lvls(lt):
        return Level.query.filter(Level.is_verified == True, Level.list_type == lt, Level.position.isnot(None)).count()

    total_main = total_lvls('main')
    total_challenge = total_lvls('challenge')
    total_platformer = total_lvls('platformer')

    rec_q = Record.query.filter(Record.user_id == user.id)
    sub_q = LevelSubmission.query.filter(LevelSubmission.user_id == user.id)
    if not is_self:
        rec_q = rec_q.filter(Record.status == 'approved')
        sub_q = sub_q.filter(LevelSubmission.status == 'approved')

    records_approved = rec_q.filter(Record.status == 'approved').count() if is_self else rec_q.count()
    records_total = Record.query.filter(Record.user_id == user.id).count() if is_self else records_approved
    submissions_approved = sub_q.filter(LevelSubmission.status == 'approved').count() if is_self else sub_q.count()
    submissions_total = LevelSubmission.query.filter(LevelSubmission.user_id == user.id).count() if is_self else submissions_approved

    beaten_rows = db.session.query(Record.level_id).filter(Record.user_id == user.id, Record.status == 'approved').distinct().all()
    beaten_level_ids = {int(lid) for (lid,) in beaten_rows if lid}

    beaten_main = beaten_challenge = beaten_platformer = 0
    if beaten_level_ids:
        for lv in Level.query.filter(Level.id.in_(list(beaten_level_ids))).all():
            if not getattr(lv, 'is_verified', False) or lv.position is None:
                continue
            if lv.list_type == 'main':
                beaten_main += 1
            elif lv.list_type == 'challenge':
                beaten_challenge += 1
            elif lv.list_type == 'platformer':
                beaten_platformer += 1

    packs = MapPack.query.all()
    beaten_pack_count = sum(1 for p in packs
                            if (ids := [int(i.strip()) for i in (p.level_ids or '').split(',') if i.strip().isdigit()])
                            and all(lid in beaten_level_ids for lid in ids))

    def pct(a, b): return round(a / b * 100, 1) if b else 0.0

    device_rows = db.session.query(Record.device, db.func.count(Record.id)).filter(Record.user_id == user.id)
    if not is_self:
        device_rows = device_rows.filter(Record.status == 'approved')
    device_breakdown = [{'device': d or 'unknown', 'count': int(c or 0)} for d, c in device_rows.group_by(Record.device).order_by(db.func.count(Record.id).desc()).all()]

    top_beaten = Level.query.filter(Level.id.in_(list(beaten_level_ids))).order_by(Level.points_reward.desc()).limit(15).all() if beaten_level_ids else []

    graphs = {}
    try:
        rows = rec_q.with_entities(db.func.date(Record.submitted_at), db.func.count(Record.id)).group_by(db.func.date(Record.submitted_at)).order_by(db.func.date(Record.submitted_at)).all()
        graphs['records'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for _, c in rows if _]}
    except Exception:
        graphs['records'] = {'labels': [], 'data': []}

    try:
        rows = (db.session.query(db.func.date(Record.submitted_at), db.func.sum(Level.points_reward))
                .select_from(Record).join(Level, Level.id == Record.level_id)
                .filter(Record.user_id == user.id, Record.status == 'approved')
                .group_by(db.func.date(Record.submitted_at)).order_by(db.func.date(Record.submitted_at)).all())
        graphs['points_gained'] = {'labels': [str(d) for d, _ in rows if d], 'data': [float(s or 0.0) for _, s in rows if _]}
    except Exception:
        graphs['points_gained'] = {'labels': [], 'data': []}

    try:
        rows = sub_q.with_entities(db.func.date(LevelSubmission.submitted_at), db.func.count(LevelSubmission.id)).group_by(db.func.date(LevelSubmission.submitted_at)).order_by(db.func.date(LevelSubmission.submitted_at)).all()
        graphs['submissions'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for _, c in rows if _]}
    except Exception:
        graphs['submissions'] = {'labels': [], 'data': []}

    rec_status_counts = {}
    sub_status_counts = {}
    if is_self:
        try:
            for s, c in db.session.query(Record.status, db.func.count(Record.id)).filter(Record.user_id == user.id).group_by(Record.status).all():
                rec_status_counts[str(s or '')] = int(c or 0)
            for s, c in db.session.query(LevelSubmission.status, db.func.count(LevelSubmission.id)).filter(LevelSubmission.user_id == user.id).group_by(LevelSubmission.status).all():
                sub_status_counts[str(s or '')] = int(c or 0)
        except Exception:
            pass

    recent_records = rec_q.order_by(Record.submitted_at.desc()).limit(40).all()
    recent_submissions = sub_q.order_by(LevelSubmission.submitted_at.desc()).limit(40).all()

    return render_template('stats.html', user=user, is_self=is_self,
                           total_site_levels_count=total_main,
                           beaten_levels_count=beaten_main,
                           beaten_levels_percentage=pct(beaten_main, total_main),
                           total_main_levels_count=total_main,
                           total_challenge_levels_count=total_challenge,
                           total_platformer_levels_count=total_platformer,
                           beaten_main_levels_count=beaten_main,
                           beaten_challenge_levels_count=beaten_challenge,
                           beaten_platformer_levels_count=beaten_platformer,
                           beaten_main_levels_percentage=pct(beaten_main, total_main),
                           beaten_challenge_levels_percentage=pct(beaten_challenge, total_challenge),
                           beaten_platformer_levels_percentage=pct(beaten_platformer, total_platformer),
                           records_total=records_total, records_approved=records_approved,
                           submissions_total=submissions_total, submissions_approved=submissions_approved,
                           record_success_rate=pct(records_approved, records_total),
                           submission_success_rate=pct(submissions_approved, submissions_total),
                           rec_status_counts=rec_status_counts, sub_status_counts=sub_status_counts,
                           recent_records=recent_records, recent_submissions=recent_submissions,
                           device_breakdown=device_breakdown, top_beaten_levels=top_beaten,
                           beaten_pack_count=beaten_pack_count, total_pack_count=len(packs),
                           pack_percent=pct(beaten_pack_count, len(packs)), graphs=graphs)


# ── Data export ───────────────────────────────────────────────────────────────

@profile_bp.route('/me/export')
@login_required
def export_my_data():
    import json
    from blueprints.api import _user_profile_json, _record_json
    recs = Record.query.filter(Record.user_id == current_user.id).order_by(Record.submitted_at.desc()).all()
    subs = LevelSubmission.query.filter(LevelSubmission.user_id == current_user.id).order_by(LevelSubmission.submitted_at.desc()).all()
    payload = {'user': _user_profile_json(current_user), 'records': [], 'submissions': []}
    for r in recs:
        try:
            lv = r.level
        except Exception:
            lv = None
        payload['records'].append(_record_json(r, lv))
    for s in subs:
        payload['submissions'].append({'id': s.id, 'name': s.name, 'creator': s.creator, 'video_url': s.video_url, 'status': s.status, 'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None})
    data = json.dumps(payload, default=str)
    return Response(data, mimetype='application/json', headers={'Content-Disposition': 'attachment; filename="my_data_export.json"'})


@profile_bp.route('/user/<int:user_id>/block', methods=['POST'])
@login_required
def block_user(user_id):
    import json
    from utils.security import validate_csrf
    validate_csrf()
    if user_id == current_user.id:
        flash('You cannot block yourself.', 'warning')
        return redirect(url_for('profile.user_profile', user_id=user_id))
    target = User.query.get_or_404(user_id)
    options = json.dumps({
        'hide_levels': bool(request.form.get('hide_levels')),
        'hide_leaderboard': bool(request.form.get('hide_leaderboard')),
        'hide_profile': bool(request.form.get('hide_profile')),
    })
    existing = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user_id).first()
    if existing:
        existing.block_options = options
        db.session.commit()
        flash(f'Block settings updated for {target.username}.', 'success')
    else:
        db.session.add(UserBlock(blocker_id=current_user.id, blocked_id=user_id, block_options=options))
        db.session.commit()
        flash(f'Blocked {target.username}.', 'success')
    return redirect(url_for('profile.user_profile', user_id=user_id))


@profile_bp.route('/user/<int:user_id>/unblock', methods=['POST'])
@login_required
def unblock_user(user_id):
    from utils.security import validate_csrf
    validate_csrf()
    block = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user_id).first()
    if block:
        db.session.delete(block)
        db.session.commit()
        flash('User unblocked.', 'success')
    return redirect(url_for('profile.user_profile', user_id=user_id))


@profile_bp.route('/user/<int:user_id>/update-block', methods=['POST'])
@login_required
def update_block(user_id):
    import json
    from utils.security import validate_csrf
    validate_csrf()
    block = UserBlock.query.filter_by(blocker_id=current_user.id, blocked_id=user_id).first()
    if block:
        block.block_options = json.dumps({
            'hide_levels': bool(request.form.get('hide_levels')),
            'hide_leaderboard': bool(request.form.get('hide_leaderboard')),
            'hide_profile': bool(request.form.get('hide_profile')),
        })
        db.session.commit()
        flash('Block settings updated.', 'success')
    return redirect(url_for('profile.blocked_users'))


@profile_bp.route('/settings/blocked')
@login_required
def blocked_users():
    blocks = UserBlock.query.filter_by(blocker_id=current_user.id).all()
    blocked_users_list = [{'user': b.blocked, 'options': b.options} for b in blocks]
    all_users = [entry['user'] for entry in blocked_users_list] + [current_user]
    equipped_ids = [u.equipped_avatar_item_id for u in all_users if u.equipped_avatar_item_id]
    equipped_items = {i.id: i for i in ShopItem.query.filter(ShopItem.id.in_(equipped_ids)).all()} if equipped_ids else {}
    return render_template('blocked_users.html', blocked_users_list=blocked_users_list, equipped_items=equipped_items)
