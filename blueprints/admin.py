"""
blueprints/admin.py – Admin panel: dashboard, level management, record moderation,
user management, site settings, pages, events, song submissions,
database editor, webhooks, auto-points, maintenance.
"""
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, abort, jsonify, send_file, Response, current_app)
from flask_login import login_required, current_user
from sqlalchemy.exc import OperationalError

from config import db
from models import (User, Level, Record, LevelSubmission, SiteLog, Page, Event,
                    Setting, CustomList, MapPack, NewsPost, SongSubmission,
                     Clan, ClanJoinRequest, ShopItem, UserPurchase, SparkyImage,
                     MarketListing, Setting, GuessGameScore)
from auxiliary.settings_helpers import (
    has_perm, get_user_rank, rank_label, RANKS, PERMISSIONS,
    get_rank_permissions, get_guidelines_url, record_proof_top_n,
    record_proof_top_n_for_list, auto_points_enabled, auto_points_scope,
    get_auto_points_rules_text_for, auto_points_rules_key,
    parse_auto_points_rules, points_for_position, get_setting, set_setting
)
from auxiliary.discord_webhooks import (
    add_site_log, send_discord_message, post_discord_webhook,
    post_discord_webhook_payload, get_event_filter_set, save_event_filter,
    discord_log_payload, get_discord_client_id, get_discord_client_secret,
    get_discord_redirect_uri, discord_oauth_ready,
    _ANNOUNCE_WEBHOOK_EVENT_CHOICES, _LOG_WEBHOOK_EVENT_CHOICES,
    WEBHOOK_URL, LOG_WEBHOOK_URL
)
from auxiliary.points import (recalc_all_user_points, recalc_user_points_for_users,
                    auto_points_recalc_all)
from utils.security import (save_uploaded_file, _ALLOWED_IMAGE_EXTS, _ALLOWED_AUDIO_EXTS,
                             get_captcha_provider, get_captcha_site_key,
                             get_recaptcha_site_key, get_recaptcha_secret_key,
                             get_hcaptcha_site_key, get_hcaptcha_secret_key,
                             safe_int)

admin_bp = Blueprint('admin', __name__)


def _require_perm(perm: str):
    if not has_perm(current_user, perm):
        abort(403)


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


# ── Dashboard ─────────────────────────────────────────────────────────────────

@admin_bp.route('/admin', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    _require_perm('access_admin')

    # Handle POST (forms that used to 405)
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action:
            # Delegate to maintenance handler
            return admin_maintenance()
        # Bulk record approval via record_ids checkboxes
        record_ids = request.form.getlist('record_ids')
        if record_ids:
            return bulk_approve_records()
        flash('Unknown form action.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    q = (request.args.get('q') or '').strip().lower()
    t = (request.args.get('type') or 'all').lower()
    levels_sort = (request.args.get('levels_sort') or 'position').strip().lower()
    if levels_sort not in ('position', 'name', 'creator', 'list', 'verified', 'newest'):
        levels_sort = 'position'

    from sqlalchemy.orm import joinedload
    pending_records = Record.query.options(joinedload(Record.user), joinedload(Record.level)).filter_by(status='pending').all()

    approved_page = safe_int(request.args.get('approved_page', 1), default=1)
    try:
        approved_per_page = max(25, min(500, int(request.args.get('approved_per_page', 100) or 100)))
    except Exception:
        approved_per_page = 100
    approved_base_q = Record.query.options(joinedload(Record.user), joinedload(Record.level)).filter_by(status='approved').order_by(Record.submitted_at.desc())
    approved_pagination = approved_base_q.paginate(page=approved_page, per_page=approved_per_page, error_out=False)
    approved_records = approved_pagination.items

    rejected_records = []
    if has_perm(current_user, 'moderate_records'):
        rejected_records = Record.query.options(joinedload(Record.user), joinedload(Record.level)).filter_by(status='rejected').order_by(Record.submitted_at.desc()).limit(200).all()

    pending_levels = LevelSubmission.query.options(joinedload(LevelSubmission.user)).filter_by(status='pending').all()

    sort_map = {
        'name': Level.name.asc(),
        'creator': (Level.creator.asc(), Level.name.asc()),
        'list': (Level.list_type.asc(), Level.name.asc()),
        'verified': (Level.is_verified.desc(), Level.list_type.asc(), db.func.coalesce(Level.position, 999999).asc(), Level.name.asc()),
        'newest': Level.created_at.desc(),
    }
    if levels_sort in sort_map:
        order = sort_map[levels_sort]
        levels = Level.query.order_by(*([order] if not isinstance(order, tuple) else order)).all()
    else:
        levels = Level.query.order_by(Level.list_type.asc(), db.func.coalesce(Level.position, 999999).asc(), Level.name.asc()).all()

    total_users_count = User.query.count()
    total_levels_count = Level.query.count()
    users = []
    if has_perm(current_user, 'manage_users'):
        users = User.query.order_by(User.username.asc()).limit(200).all()

    if q:
        if t in ('all', 'records'):
            like = f'%{q}%'
            base = (Record.query.join(User, Record.user_id == User.id).join(Level, Record.level_id == Level.id)
                    .filter(db.func.lower(User.username).like(like) | db.func.lower(Level.name).like(like) | db.func.lower(Record.device).like(like)))
            pending_records = base.filter(Record.status == 'pending').order_by(Record.submitted_at.desc()).all()
            approved_base_q = base.filter(Record.status == 'approved').options(joinedload(Record.user), joinedload(Record.level)).order_by(Record.submitted_at.desc())
            approved_pagination = approved_base_q.paginate(page=approved_page, per_page=approved_per_page, error_out=False)
            approved_records = approved_pagination.items
            if has_perm(current_user, 'moderate_records'):
                rejected_records = base.filter(Record.status == 'rejected').options(joinedload(Record.user), joinedload(Record.level)).order_by(Record.submitted_at.desc()).limit(200).all()
        if t in ('all', 'levels'):
            levels = [lv for lv in levels if q in (lv.name or '').lower() or q in (lv.creator or '').lower()]
        if t in ('all', 'submissions'):
            like = f'%{q}%'
            pending_levels = (LevelSubmission.query.join(User, LevelSubmission.user_id == User.id)
                              .filter(db.func.lower(LevelSubmission.name).like(like) | db.func.lower(LevelSubmission.creator).like(like) | db.func.lower(User.username).like(like))
                              .order_by(LevelSubmission.submitted_at.desc()).limit(500).all())
        if t in ('all', 'users') and has_perm(current_user, 'manage_users'):
            users = [u for u in users if q in (u.username or '').lower()]

    news_posts = []
    webhook_url = log_webhook_url = site_launch_webhook_url = ''
    discord_client_id = discord_redirect_uri = ''
    discord_client_secret_set = False
    captcha_provider = recaptcha_site_key = hcaptcha_site_key = ''
    recaptcha_secret_set = hcaptcha_secret_set = False
    guidelines_url = ''
    hide_categories_on_main_list = False
    auto_points_rules = {}
    active_users_count = 0
    logs = []
    api_logs = []
    announce_selected = log_selected = set()
    announce_custom = log_custom = ''

    try:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(minutes=15)
        active_users_count = User.query.filter(User.last_seen_at.isnot(None), User.last_seen_at >= cutoff).count()
    except Exception:
        pass
    try:
        logs = SiteLog.query.filter(SiteLog.kind != 'api_call').order_by(SiteLog.created_at.desc()).limit(100).all()
    except Exception:
        pass
    try:
        api_logs = SiteLog.query.filter(SiteLog.kind == 'api_call').order_by(SiteLog.created_at.desc()).limit(100).all()
    except Exception:
        pass

    if has_perm(current_user, 'manage_news'):
        news_posts = NewsPost.query.order_by(NewsPost.created_at.desc()).limit(20).all()

    if has_perm(current_user, 'manage_settings'):
        webhook_url = get_setting('discord_webhook_url') or WEBHOOK_URL or ''
        log_webhook_url = get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL or ''
        site_launch_webhook_url = get_setting('discord_site_launch_webhook_url') or ''
        discord_client_id = get_discord_client_id()
        discord_redirect_uri = get_discord_redirect_uri()
        discord_client_secret_set = bool(get_discord_client_secret())
        captcha_provider = get_captcha_provider()
        recaptcha_site_key = get_recaptcha_site_key()
        recaptcha_secret_set = bool(get_recaptcha_secret_key())
        hcaptcha_site_key = get_hcaptcha_site_key()
        hcaptcha_secret_set = bool(get_hcaptcha_secret_key())
        guidelines_url = get_guidelines_url()
        hide_categories_on_main_list = str(get_setting('hide_categories_on_main_list') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        announce_selected = get_event_filter_set('discord_webhook_events')
        log_selected = get_event_filter_set('discord_log_webhook_events')
        announce_known = {k for k, _ in _ANNOUNCE_WEBHOOK_EVENT_CHOICES}
        log_known = {k for k, _ in _LOG_WEBHOOK_EVENT_CHOICES}
        announce_custom = ','.join(sorted(v for v in announce_selected if v not in announce_known))
        log_custom = ','.join(sorted(v for v in log_selected if v not in log_known))

    # Graphs
    graphs = {}
    try:
        rows = db.session.query(db.func.date(LevelSubmission.submitted_at), db.func.count(LevelSubmission.id)).group_by(db.func.date(LevelSubmission.submitted_at)).order_by(db.func.date(LevelSubmission.submitted_at)).all()
        graphs['submissions'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for _, c in rows if _]}
    except Exception:
        graphs['submissions'] = {'labels': [], 'data': []}
    try:
        rows = db.session.query(db.func.date(Record.submitted_at), db.func.count(Record.id)).group_by(db.func.date(Record.submitted_at)).order_by(db.func.date(Record.submitted_at)).all()
        graphs['records'] = {'labels': [str(d) for d, _ in rows if d], 'data': [int(c or 0) for _, c in rows if _]}
    except Exception:
        graphs['records'] = {'labels': [], 'data': []}
    try:
        rows = db.session.query(db.func.date(Level.created_at), db.func.count(Level.id)).filter(Level.created_at.isnot(None)).group_by(db.func.date(Level.created_at)).order_by(db.func.date(Level.created_at)).all()
        daily = [int(c or 0) for _, c in rows if _]
        cum = []
        run = 0
        for v in daily:
            run += v
            cum.append(run)
        graphs['levels_total'] = {'labels': [str(d) for d, _ in rows if d], 'data': cum}
    except Exception:
        graphs['levels_total'] = {'labels': [], 'data': []}

    return render_template('admin.html',
                           pending_records=pending_records,
                           approved_records=approved_records,
                           rejected_records=rejected_records,
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
                           record_proof_top_n=record_proof_top_n(),
                           record_proof_top_n_main=record_proof_top_n_for_list('main'),
                           record_proof_top_n_challenge=record_proof_top_n_for_list('challenge'),
                           record_proof_top_n_platformer=record_proof_top_n_for_list('platformer'),
                           auto_points_enabled=auto_points_enabled(),
                           auto_points_scope=auto_points_scope(),
                           auto_points_rules_main_verified=get_auto_points_rules_text_for('main', True),
                           auto_points_rules_main_unverified=get_auto_points_rules_text_for('main', False),
                           auto_points_rules_challenge_verified=get_auto_points_rules_text_for('challenge', True),
                           auto_points_rules_challenge_unverified=get_auto_points_rules_text_for('challenge', False),
                           auto_points_rules_platformer_verified=get_auto_points_rules_text_for('platformer', True),
                           auto_points_rules_platformer_unverified=get_auto_points_rules_text_for('platformer', False),
                           active_users_count=active_users_count,
                           total_users_count=total_users_count,
                           total_levels_count=total_levels_count,
                           graphs=graphs,
                           logs=logs,
                           api_logs=api_logs)


# ── Level management ──────────────────────────────────────────────────────────

@admin_bp.route('/admin/add_level', methods=['GET', 'POST'])
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
            if sid:
                song_id = int(sid)
        except Exception:
            pass
        official_song_id = None
        try:
            osid = request.form.get('official_song_id', '').strip()
            if osid:
                official_song_id = int(osid)
        except Exception:
            pass
        try:
            points_reward = float(request.form.get('points_reward', 0))
        except ValueError:
            points_reward = 0.0
        is_verified = request.form.get('is_verified') == 'on'
        lt = (request.form.get('list_type') or 'main').strip().lower()
        if lt not in ('main', 'challenge', 'platformer'):
            lt = 'main'
        difficulty = (request.form.get('difficulty') or '').strip()[:40] or None
        categories_str = ",".join(request.form.getlist('categories'))
        insert_pos = None
        raw_pos = (request.form.get('position') or '').strip()
        if raw_pos:
            try:
                insert_pos = int(raw_pos)
            except Exception:
                pass

        thumb_unique = None
        thumb_file = request.files.get('thumbnail')
        if thumb_file and thumb_file.filename:
            folder = os.path.join(current_app.root_path, 'static', 'level_thumbs')
            thumb_unique, err = save_uploaded_file(thumb_file, folder, _ALLOWED_IMAGE_EXTS, 'image/')
            if not thumb_unique:
                flash('Upload rate limit exceeded.' if err == 'rate_limited' else 'Thumbnail must be a valid image.', 'danger')
                return redirect(url_for('admin.add_level'))

        new_level = Level(name=name, creator=creator, video_url=video_url, description=description,
                          gd_level_id=gd_level_id, song_id=song_id, official_song_id=official_song_id,
                          points_reward=points_reward, is_verified=is_verified, list_type=lt,
                          difficulty=difficulty, position=None, categories=categories_str,
                          thumbnail_filename=thumb_unique or None)
        db.session.add(new_level)

        affected = Level.query.filter(Level.list_type == lt, Level.is_verified == bool(is_verified), Level.position.isnot(None)).order_by(Level.position).all()
        if insert_pos is None:
            insert_pos = len(affected) + 1
        insert_pos = max(1, min(insert_pos, len(affected) + 1))
        ordered = list(affected)
        ordered.insert(insert_pos - 1, new_level)
        for idx, lv in enumerate(ordered, start=1):
            lv.position = idx

        from auxiliary.settings_helpers import auto_points_applies_to
        if auto_points_applies_to(lt):
            rules = parse_auto_points_rules(get_auto_points_rules_text_for(lt, bool(is_verified)))
            for lv in ordered:
                lv.points_reward = points_for_position(int(lv.position or 0), rules)

        db.session.commit()
        if auto_points_applies_to(lt):
            try:
                recalc_all_user_points()
            except Exception:
                pass
        add_site_log('level_create', f'{current_user.username} created level "{name}" by {creator} ({lt}) at #{int(new_level.position or 0)}.', actor_user_id=current_user.id)
        flash(f'Level "{name}" added to {"Verified" if is_verified else "Unverified"} ({lt}) list!', 'success')
        return redirect(url_for('admin.admin_dashboard'))
    return render_template('add_level.html')


@admin_bp.route('/admin/edit_level/<int:level_id>', methods=['GET', 'POST'])
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
        level.is_verified = request.form.get('is_verified') == 'on'
        level.categories = ",".join(request.form.getlist('categories'))

        raw_pos = (request.form.get('position') or '').strip()
        new_position = None
        if raw_pos:
            try:
                new_position = int(float(raw_pos))
            except Exception:
                pass

        # Handle thumbnail
        thumb_file = request.files.get('thumbnail')
        if thumb_file and thumb_file.filename:
            folder = os.path.join(current_app.root_path, 'static', 'level_thumbs')
            unique, err = save_uploaded_file(thumb_file, folder, _ALLOWED_IMAGE_EXTS, 'image/')
            if not unique:
                flash('Upload rate limit exceeded.' if err == 'rate_limited' else 'Thumbnail must be a valid image.', 'danger')
                return redirect(url_for('admin.edit_level', level_id=level.id))
            level.thumbnail_filename = unique

        # Position management with proper shifting: inserting at a spot
        # pushes later levels down instead of creating duplicate positions.
        if not level.is_verified:
            level.position = None
        else:
            siblings = Level.query.filter(
                Level.list_type == lt,
                Level.is_verified == True,
                Level.id != level.id,
                Level.position.isnot(None),
            ).order_by(Level.position).all()
            if new_position is None:
                new_position = len(siblings) + 1
            try:
                new_position = int(new_position)
            except Exception:
                new_position = len(siblings) + 1
            new_position = max(1, min(new_position, len(siblings) + 1))
            ordered = list(siblings)
            ordered.insert(new_position - 1, level)
            for idx, lv in enumerate(ordered, start=1):
                lv.position = idx

        db.session.commit()
        from utils.cache import cache_bust_prefixes
        cache_bust_prefixes(['levels:', 'lb:'])

        if old_verified != level.is_verified:
            event_type = 'level_verified' if level.is_verified else 'level_unverified'
            send_discord_message(f'"{level.name}" was {"verified" if level.is_verified else "set to unverified"} by {current_user.username}', event_type=event_type)

        add_site_log('admin_action', f'{current_user.username} edited level "{level.name}" by {level.creator}.', actor_user_id=current_user.id)
        try:
            user_ids = [int(uid) for (uid,) in db.session.query(Record.user_id).filter(Record.level_id == level.id, Record.status == 'approved').distinct().all() if uid]
            if user_ids:
                recalc_user_points_for_users(user_ids)
        except Exception:
            pass
        flash('Level updated successfully', 'success')
        return redirect(url_for('admin.admin_dashboard'))
    return render_template('edit_level.html', level=level)


@admin_bp.route('/admin/delete_level/<int:level_id>')
@login_required
def delete_level(level_id):
    _require_perm('manage_levels')
    level = Level.query.get_or_404(level_id)
    level_name = level.name
    for record in level.records:
        if record.status == 'approved':
            record.user.points = max(0, record.user.points - (level.points_reward or 0))
    db.session.delete(level)
    db.session.commit()
    send_discord_message(f'"{level_name}" was deleted by {current_user.username}', event_type='level_deleted')
    add_site_log('admin_action', f'{current_user.username} deleted level "{level_name}".', actor_user_id=current_user.id)
    flash('Level deleted', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/update_level_order', methods=['POST'])
@login_required
def update_level_order():
    _require_perm('move_levels')
    level_id = request.form.get('level_id')
    new_position = (request.form.get('position') or '').strip()
    level = Level.query.get_or_404(level_id)
    level_name = level.name
    level_author = level.creator
    try:
        affected = Level.query.filter(Level.list_type == level.list_type, Level.is_verified == bool(level.is_verified), Level.id != level.id, Level.position.isnot(None)).order_by(Level.position).all()
        if not new_position:
            level.position = None
            for idx, lv in enumerate(affected, start=1):
                lv.position = idx
        else:
            new_pos = max(1, min(int(new_position), len(affected) + 1))
            ordered = list(affected)
            ordered.insert(new_pos - 1, level)
            for idx, lv in enumerate(ordered, start=1):
                lv.position = idx
            send_discord_message(f'"{level_name}" by {level_author} moved to place {new_pos} by {current_user.username}', event_type='level_moved')
            add_site_log('level_move', f'{current_user.username} moved "{level_name}" to place {new_pos}.', actor_user_id=current_user.id)

        from auxiliary.settings_helpers import auto_points_applies_to
        if auto_points_applies_to(level.list_type):
            all_in_list = Level.query.filter(Level.list_type == level.list_type, Level.is_verified == bool(level.is_verified), Level.position.isnot(None)).order_by(Level.position).all()
            rules = parse_auto_points_rules(get_auto_points_rules_text_for(level.list_type, bool(level.is_verified)))
            for lv in all_in_list:
                lv.points_reward = points_for_position(int(lv.position or 0), rules)

        db.session.commit()
        from utils.cache import cache_bust_prefixes
        cache_bust_prefixes(['levels:', 'lb:'])
        from auxiliary.settings_helpers import auto_points_applies_to
        if auto_points_applies_to(level.list_type):
            recalc_all_user_points()
        flash('Level position updated.', 'success')
    except ValueError:
        flash('Invalid position value.', 'danger')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/auto_number_levels', methods=['POST'])
@login_required
def auto_number_levels():
    _require_perm('move_levels')
    list_type = request.form.get('list_type', 'main')
    is_verified_raw = request.form.get('is_verified', '1')
    is_verified = is_verified_raw == '1'
    levels = Level.query.filter(
        Level.list_type == list_type,
        Level.is_verified == is_verified,
    ).order_by(db.func.coalesce(Level.position, 999999).asc(), Level.id.asc()).all()
    from auxiliary.settings_helpers import auto_points_applies_to, parse_auto_points_rules, get_auto_points_rules_text_for, points_for_position
    for idx, lv in enumerate(levels, start=1):
        lv.position = idx
    db.session.commit()
    if auto_points_applies_to(list_type):
        rules = parse_auto_points_rules(get_auto_points_rules_text_for(list_type, is_verified))
        for lv in levels:
            lv.points_reward = points_for_position(int(lv.position or 0), rules)
        db.session.commit()
        recalc_all_user_points()
    from utils.cache import cache_bust_prefixes
    cache_bust_prefixes(['levels:', 'lb:'])
    send_discord_message(f'{current_user.username} renumbered {len(levels)} {list_type} levels (verified={is_verified}).', event_type='level_moved')
    add_site_log('level_move', f'{current_user.username} renumbered {len(levels)} {list_type} levels (verified={is_verified}).', actor_user_id=current_user.id)
    flash(f'Renumbered {len(levels)} {list_type} levels (1–{len(levels)}).', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Record moderation ─────────────────────────────────────────────────────────

@admin_bp.route('/admin/approve_record/<int:record_id>')
@login_required
def approve_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    old_status = record.status
    record.status = 'approved'
    db.session.commit()
    try:
        recalc_user_points_for_users([record.user_id])
    except Exception:
        pass
    if old_status != 'approved':
        level = db.session.get(Level, record.level_id)
        proof = record.video_url or ''
        if not proof and getattr(record, 'proof_image_filename', ''):
            try:
                proof = url_for('static', filename=f'record_proofs/{record.proof_image_filename}', _external=True)
            except Exception:
                pass
        send_discord_message(f'Proof approved by {current_user.username} for "{level.name if level else "level"}". Proof: {proof}', event_type='record_approved')
        add_site_log('points_change', f'{current_user.username} approved record for {record.user.username} on "{level.name if level else "level"}".', actor_user_id=current_user.id)
        # Award bcoins for the beaten level
        if level:
            bcoin_amt = max(1, int(float(level.points_reward or 0) / 2))
            user = db.session.get(User, record.user_id)
            if user:
                user.bcoins = (user.bcoins or 0) + bcoin_amt
                user.total_bcoins_earned = (user.total_bcoins_earned or 0) + bcoin_amt
                db.session.commit()
    flash('Record approved', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/reject_record/<int:record_id>')
@login_required
def reject_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    record.status = 'rejected'
    db.session.commit()
    level = db.session.get(Level, record.level_id)
    add_site_log('admin_action', f'{current_user.username} rejected record for {record.user.username} on "{level.name if level else "level"}".', actor_user_id=current_user.id)
    flash('Record rejected', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/reapprove_record/<int:record_id>')
@login_required
def reapprove_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    record.status = 'approved'
    db.session.commit()
    try:
        recalc_user_points_for_users([record.user_id])
    except Exception:
        pass
    level = db.session.get(Level, record.level_id)
    add_site_log('points_change', f'{current_user.username} re-approved rejected record for {record.user.username} on "{level.name if level else "level"}".', actor_user_id=current_user.id)
    if level:
        bcoin_amt = max(1, int(float(level.points_reward or 0) / 2))
        user = db.session.get(User, record.user_id)
        if user:
            user.bcoins = (user.bcoins or 0) + bcoin_amt
            user.total_bcoins_earned = (user.total_bcoins_earned or 0) + bcoin_amt
            db.session.commit()
    flash('Record re-approved', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/delete_record/<int:record_id>')
@login_required
def delete_record(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    uname = record.user.username
    user_id = int(record.user_id)
    level = db.session.get(Level, record.level_id)
    level_name = level.name if level else ''
    db.session.delete(record)
    db.session.commit()
    try:
        recalc_user_points_for_users([user_id])
    except Exception:
        pass
    add_site_log('admin_action', f'{current_user.username} deleted record #{record_id} ({level_name}) for {uname}.', actor_user_id=current_user.id)
    flash('Record deleted', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/clear_record_video/<int:record_id>')
@login_required
def clear_record_video(record_id):
    _require_perm('moderate_records')
    record = Record.query.get_or_404(record_id)
    record.video_url = ''
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} cleared record video for {record.user.username}.', actor_user_id=current_user.id)
    flash('Record video cleared.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/edit_record/<int:record_id>', methods=['GET', 'POST'])
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
        if record.status != old_status:
            try:
                recalc_user_points_for_users([int(record.user_id)])
            except Exception:
                pass
        add_site_log('admin_action', f'{current_user.username} edited record {record.id} for {record.user.username}.', actor_user_id=current_user.id)
        flash('Record updated.', 'success')
        return redirect(url_for('admin.admin_dashboard'))
    return render_template('admin_edit_record.html', record=record)


@admin_bp.route('/admin/approve_records_bulk', methods=['POST'])
@login_required
def bulk_approve_records():
    _require_perm('moderate_records')
    action = (request.form.get('bulk_action') or 'approve').strip().lower()
    ids = [int(r) for r in request.form.getlist('record_ids') if str(r).strip().isdigit()]
    if not ids:
        flash('No records selected.', 'warning')
        return redirect(url_for('admin.admin_dashboard'))
    records = Record.query.filter(Record.id.in_(ids)).all()
    affected_user_ids = set()
    if action in ('approve', 'reject'):
        new_status = 'approved' if action == 'approve' else 'rejected'
        changed = sum(1 for rec in records if rec.status != new_status)
        for rec in records:
            rec.status = new_status
            try:
                affected_user_ids.add(int(rec.user_id))
            except Exception:
                pass
        db.session.commit()
        try:
            recalc_user_points_for_users(sorted(list(affected_user_ids)))
        except Exception:
            pass
        add_site_log('admin_action', f'{current_user.username} bulk {action}d {changed} records.', actor_user_id=current_user.id)
        flash(f'{action.title()}d {changed} record(s).', 'success')
    elif action == 'clear_video':
        cleared = sum(1 for rec in records if rec.video_url)
        for rec in records:
            rec.video_url = ''
        db.session.commit()
        flash(f'Cleared video for {cleared} record(s).', 'success')
    elif action == 'delete':
        for rec in records:
            try:
                affected_user_ids.add(int(rec.user_id))
            except Exception:
                pass
            db.session.delete(rec)
        db.session.commit()
        try:
            recalc_user_points_for_users(sorted(list(affected_user_ids)))
        except Exception:
            pass
        flash(f'Deleted {len(ids)} record(s).', 'success')
    else:
        flash('Invalid bulk action.', 'danger')
    return redirect(url_for('admin.admin_dashboard'))


# ── Level submissions ─────────────────────────────────────────────────────────

@admin_bp.route('/admin/approve_level_submission/<int:sub_id>')
@login_required
def approve_level_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = LevelSubmission.query.get_or_404(sub_id)
    if sub.status != 'approved':
        sub.status = 'approved'
        new_level = Level(name=sub.name, creator=sub.creator, video_url=sub.video_url)
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
        send_discord_message(f'Level submission accepted: "{new_level.name}" by {new_level.creator}, approved by {current_user.username}.', event_type='level_submission_approved')
        add_site_log('admin_action', f'{current_user.username} approved level submission "{new_level.name}".', actor_user_id=current_user.id)
        flash('Level submission approved.', 'success')
    else:
        flash('Submission already approved.', 'info')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/reject_level_submission/<int:sub_id>')
@login_required
def reject_level_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = LevelSubmission.query.get_or_404(sub_id)
    sub.status = 'rejected'
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} rejected level submission "{sub.name}".', actor_user_id=current_user.id)
    flash('Level submission rejected', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/delete_level_submission/<int:sub_id>')
@login_required
def delete_level_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = LevelSubmission.query.get_or_404(sub_id)
    sub_name = sub.name
    db.session.delete(sub)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted level submission "{sub_name}".', actor_user_id=current_user.id)
    flash('Level submission deleted', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── User management ───────────────────────────────────────────────────────────

@admin_bp.route('/admin/toggle_mod/<int:user_id>')
@login_required
def toggle_mod(user_id):
    if get_user_rank(current_user) != 'admin':
        abort(403)
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own role.', 'warning')
        return redirect(url_for('admin.admin_dashboard'))
    user.is_mod = not bool(getattr(user, 'is_mod', False))
    if user.is_mod and not bool(getattr(user, 'is_admin', False)):
        user.rank = 'mod'
    elif not user.is_mod and str(getattr(user, 'rank', '') or '').strip().lower() == 'mod':
        user.rank = 'member'
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} set mod={user.is_mod} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} is now a {"Mod" if user.is_mod else "User"}.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/toggle_admin/<int:user_id>')
@login_required
def toggle_admin(user_id):
    if get_user_rank(current_user) != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own admin status.', 'warning')
        return redirect(url_for('admin.admin_dashboard'))
    user.is_admin = not user.is_admin
    user.rank = 'admin' if user.is_admin else ('mod' if bool(getattr(user, 'is_mod', False)) else 'member')
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} set admin={user.is_admin} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} admin status updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Admin user edit page (dedicated) ───────────────────────────────────────────

@admin_bp.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_user_edit(user_id):
    _require_perm('manage_users')
    from config import bcrypt
    from utils.security import trim_pw
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        new_username = (request.form.get('username') or '').strip()
        new_password = trim_pw(request.form.get('new_password') or '')
        new_password2 = request.form.get('new_password2') or ''
        new_rank = (request.form.get('rank') or '').strip()
        new_points = request.form.get('points', '').strip()
        new_bcoins = request.form.get('bcoins', '').strip()
        new_discord_tag = (request.form.get('discord_tag') or '').strip()
        new_bio = (request.form.get('bio') or '').strip()
        new_youtube = (request.form.get('youtube_link') or '').strip()
        new_disabled = request.form.get('is_disabled') == '1'
        new_donor = request.form.get('is_donor') == '1'

        if new_username:
            existing = User.query.filter(User.username == new_username, User.id != user.id).first()
            if existing:
                flash('Username already taken.', 'danger')
            else:
                old = user.username
                user.username = new_username
                add_site_log('admin_action', f'{current_user.username} changed username for {old} -> {new_username}.', actor_user_id=current_user.id)

        if new_password:
            if new_password != new_password2:
                flash('Passwords do not match.', 'danger')
            elif get_user_rank(user) in ('admin', 'elder_moderator') and get_user_rank(current_user) != 'admin':
                flash('Only admins can change passwords for staff accounts.', 'danger')
            else:
                user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
                add_site_log('admin_action', f'{current_user.username} changed password for {user.username}.', actor_user_id=current_user.id)
                flash('Password updated.', 'success')

        valid_ranks = ['member', 'mod', 'admin']
        from auxiliary.settings_helpers import get_user_rank as _gur
        if new_rank in valid_ranks:
            if new_rank == 'admin' and _gur(current_user) != 'admin':
                flash('Only admins can grant the admin rank.', 'danger')
            else:
                user.rank = new_rank

        if new_points:
            try:
                user.points = max(0.0, float(new_points))
            except ValueError:
                flash('Invalid points value.', 'danger')

        if new_bcoins:
            try:
                user.bcoins = max(0, int(new_bcoins))
            except ValueError:
                flash('Invalid bcoins value.', 'danger')

        user.discord_tag = new_discord_tag or None
        user.bio = new_bio or None
        user.youtube_link = new_youtube or None
        user.is_disabled = new_disabled
        user.is_donor = new_donor

        try:
            db.session.commit()
            flash(f'User {user.username} updated.', 'success')
        except Exception:
            db.session.rollback()
            flash('Error saving changes.', 'danger')

        return redirect(url_for('admin.admin_user_edit', user_id=user.id))

    return render_template('admin_user_edit.html', user=user, valid_ranks=['member', 'mod', 'admin'])


@admin_bp.route('/admin/toggle_disabled/<int:user_id>')
@login_required
def toggle_disabled(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot disable your own account.', 'warning')
        return redirect(url_for('admin.admin_dashboard'))
    user.is_disabled = not bool(getattr(user, 'is_disabled', False))
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} set disabled={bool(user.is_disabled)} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} is now {"disabled" if user.is_disabled else "enabled"}.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/toggle_donor/<int:user_id>', methods=['POST'])
@login_required
def toggle_donor(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    user.is_donor = not bool(getattr(user, 'is_donor', False))
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} set donor={user.is_donor} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} is now {"a donor" if user.is_donor else "not a donor"}.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/set_rank/<int:user_id>', methods=['POST'])
@login_required
def admin_set_user_rank(user_id):
    if get_user_rank(current_user) != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own rank.', 'warning')
        return redirect(url_for('admin.admin_dashboard'))
    rank = (request.form.get('rank') or '').strip().lower()
    if rank not in RANKS:
        flash('Invalid rank.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    user.rank = rank
    user.is_admin = rank == 'admin'
    user.is_mod = rank in ('mod', 'elder_moderator')
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} set rank={rank} for {user.username}.', actor_user_id=current_user.id)
    flash(f'User {user.username} rank updated to {rank_label(rank)}.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
def reset_password(user_id):
    _require_perm('manage_users')
    from config import bcrypt
    from utils.security import trim_pw
    user = User.query.get_or_404(user_id)
    # Rank hierarchy: cannot reset the password of an equal-or-higher rank
    if get_user_rank(user) in ('admin', 'elder_moderator') and get_user_rank(current_user) != 'admin':
        flash('Only admins can reset passwords for staff accounts.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    new_password = trim_pw(request.form.get('new_password', '').strip())
    if not new_password:
        flash('New password cannot be empty.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} reset password for {user.username}.', actor_user_id=current_user.id)
    flash(f'Password reset for {user.username}.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/update_user_points/<int:user_id>', methods=['POST'])
@login_required
def update_user_points(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    old_points = float(user.points or 0)
    try:
        points = float(request.form.get('points', user.points))
    except ValueError:
        flash('Invalid points value.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    user.points = max(0.0, points)
    db.session.commit()
    add_site_log('points_change', f'{current_user.username} changed points for {user.username}: {old_points} -> {user.points}.', actor_user_id=current_user.id)
    flash(f'Points updated for {user.username}.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    _require_perm('manage_users')
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Cannot delete your own account from admin panel.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    # Rank hierarchy: only admins may delete staff accounts
    if get_user_rank(user) in ('admin', 'elder_moderator') and get_user_rank(current_user) != 'admin':
        flash('Only admins can delete staff accounts.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    uname = user.username
    db.session.delete(user)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted user {uname}.', actor_user_id=current_user.id)
    flash(f'User {uname} deleted.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/users/<int:user_id>/media/delete', methods=['POST'])
@login_required
def admin_delete_user_media(user_id):
    _require_perm('manage_users')
    kind = (request.form.get('kind') or '').strip().lower()
    if kind not in ('avatar', 'banner'):
        flash('Invalid media kind.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    u = db.session.get(User, user_id)
    if not u:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    fname = getattr(u, f'{kind}_filename', None) or ''
    setattr(u, f'{kind}_filename', None)
    db.session.commit()
    if fname:
        try:
            path = os.path.join(current_app.root_path, 'static', 'profile_media', fname)
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
    add_site_log('admin_action', f'{current_user.username} deleted {kind} for {u.username}.', actor_user_id=current_user.id)
    flash(f'{kind.title()} removed.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/levels/<int:level_id>/thumbnail/delete', methods=['POST'])
@login_required
def admin_delete_level_thumbnail(level_id):
    _require_perm('manage_levels')
    lv = db.session.get(Level, level_id)
    if not lv:
        flash('Level not found.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    fname = (getattr(lv, 'thumbnail_filename', '') or '').strip()
    lv.thumbnail_filename = None
    db.session.commit()
    if fname:
        try:
            path = os.path.join(current_app.root_path, 'static', 'level_thumbs', fname)
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
    add_site_log('admin_action', f'{current_user.username} deleted thumbnail for level "{lv.name}".', actor_user_id=current_user.id)
    flash('Thumbnail removed.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Rank permissions ──────────────────────────────────────────────────────────

@admin_bp.route('/admin/settings/rank_permissions', methods=['POST'])
@login_required
def admin_set_rank_permissions():
    if get_user_rank(current_user) != 'admin':
        abort(403)
    allowed = {k for k, _ in PERMISSIONS}
    out = {}
    for r in RANKS:
        if r == 'admin':
            continue
        picked = [str(p or '').strip() for p in request.form.getlist(f'perms_{r}') if str(p or '').strip() in allowed]
        out[r] = list(dict.fromkeys(picked))
    set_setting('rank_permissions', json.dumps(out))
    add_site_log('admin_action', f'{current_user.username} updated rank permissions.', actor_user_id=current_user.id)
    flash('Rank permissions updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── News ──────────────────────────────────────────────────────────────────────

@admin_bp.route('/admin/news/create', methods=['POST'])
@login_required
def admin_news_create():
    _require_perm('manage_news')
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    if not title or not body:
        flash('News title and body are required.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    post = NewsPost(title=title[:200], body=body)
    db.session.add(post)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} created news "{title[:100]}".', actor_user_id=current_user.id)
    flash('News posted.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/news/<int:post_id>/delete', methods=['POST'])
@login_required
def admin_news_delete(post_id):
    _require_perm('manage_news')
    post = NewsPost.query.get_or_404(post_id)
    title = post.title
    db.session.delete(post)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted news "{title[:100]}".', actor_user_id=current_user.id)
    flash('News deleted.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Webhook settings ──────────────────────────────────────────────────────────

@admin_bp.route('/admin/settings/webhook', methods=['POST'])
@login_required
def admin_set_webhook():
    _require_perm('manage_settings')
    set_setting('discord_webhook_url', (request.form.get('webhook_url') or '').strip())
    flash('Webhook updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/webhook/test', methods=['POST'])
@login_required
def admin_test_webhook():
    _require_perm('manage_settings')
    url = (get_setting('discord_webhook_url') or WEBHOOK_URL or '').strip()
    if not url:
        flash('Webhook is not set.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    ok, err = post_discord_webhook(url, f'Test message from admin panel by {current_user.username}.')
    if ok:
        flash('Test message sent.', 'success')
    else:
        flash(f'Test failed: {err}', 'danger')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/log_webhook', methods=['POST'])
@login_required
def admin_set_log_webhook():
    _require_perm('manage_settings')
    set_setting('discord_logs_webhook_url', (request.form.get('log_webhook_url') or '').strip())
    flash('Log webhook updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/log_webhook/test', methods=['POST'])
@login_required
def admin_test_log_webhook():
    _require_perm('manage_settings')
    url = (get_setting('discord_logs_webhook_url') or LOG_WEBHOOK_URL or get_setting('discord_webhook_url') or WEBHOOK_URL or '').strip()
    if not url:
        flash('Logs webhook is not set.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    payload = discord_log_payload('test', f'Test log message from admin panel by {current_user.username}.', actor_user_id=current_user.id)
    ok, err = post_discord_webhook_payload(url, payload)
    flash('Test log sent.' if ok else f'Log test failed: {err}', 'success' if ok else 'danger')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/site_launch_webhook', methods=['POST'])
@login_required
def admin_set_site_launch_webhook():
    _require_perm('manage_settings')
    set_setting('discord_site_launch_webhook_url', (request.form.get('site_launch_webhook_url') or '').strip())
    flash('Site launch webhook updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/site_launch_webhook/test', methods=['POST'])
@login_required
def admin_test_site_launch_webhook():
    _require_perm('manage_settings')
    url = (get_setting('discord_site_launch_webhook_url') or '').strip()
    if not url:
        flash('Site launch webhook is not set.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))
    payload = {"embeds": [{"title": "Site launch test", "description": f"Test by {current_user.username}.", "color": 0x22C55E, "timestamp": datetime.utcnow().isoformat() + "Z"}]}
    ok, err = post_discord_webhook_payload(url, payload)
    flash('Site launch test sent.' if ok else f'Failed: {err}', 'success' if ok else 'danger')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/webhook/events', methods=['POST'])
@login_required
def admin_set_webhook_events():
    _require_perm('manage_settings')
    from auxiliary.discord_webhooks import _parse_event_filter_text
    announce = [str(v or '').strip().lower() for v in request.form.getlist('announce_events')]
    announce_extra = _parse_event_filter_text((request.form.get('announce_events_custom') or '').strip())
    log = [str(v or '').strip().lower() for v in request.form.getlist('log_events')]
    log_extra = _parse_event_filter_text((request.form.get('log_events_custom') or '').strip())
    save_event_filter('discord_webhook_events', announce + announce_extra)
    save_event_filter('discord_log_webhook_events', log + log_extra)
    flash('Webhook event settings updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Other settings ────────────────────────────────────────────────────────────

@admin_bp.route('/admin/settings/discord_oauth', methods=['POST'])
@login_required
def admin_set_discord_oauth():
    _require_perm('manage_settings')
    set_setting('discord_client_id', (request.form.get('client_id') or '').strip())
    set_setting('discord_redirect_uri', (request.form.get('redirect_uri') or '').strip())
    if (client_secret := (request.form.get('client_secret') or '').strip()):
        set_setting('discord_client_secret', client_secret)
    add_site_log('admin_action', f'{current_user.username} updated Discord OAuth settings.', actor_user_id=current_user.id, send_to_discord=False)
    flash('Discord OAuth settings updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/recaptcha', methods=['POST'])
@login_required
def admin_set_recaptcha():
    _require_perm('manage_settings')
    provider = (request.form.get('captcha_provider') or '').strip().lower()
    if provider not in ('', 'recaptcha', 'hcaptcha'):
        provider = ''
    set_setting('captcha_provider', provider)
    for prefix in ('recaptcha', 'hcaptcha'):
        site_key = (request.form.get(f'{prefix}_site_key') or '').strip()
        secret_key = (request.form.get(f'{prefix}_secret_key') or '').strip()
        set_setting(f'{prefix}_site_key', site_key)
        if secret_key:
            set_setting(f'{prefix}_secret_key', secret_key)
    flash('CAPTCHA settings updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/guidelines', methods=['POST'])
@login_required
def admin_set_guidelines():
    _require_perm('manage_settings')
    import time
    set_setting('guidelines_url', (request.form.get('guidelines_url') or '').strip())
    set_setting('guidelines_reset_ts', str(int(time.time())))
    add_site_log('admin_action', f'{current_user.username} updated guidelines URL.', actor_user_id=current_user.id, send_to_discord=False)
    flash('Guidelines updated. Everyone must accept again.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/ui', methods=['POST'])
@login_required
def admin_set_ui_settings():
    _require_perm('manage_settings')
    set_setting('hide_categories_on_main_list', '1' if request.form.get('hide_categories_on_main_list') == 'on' else '0')
    set_setting('year_recap_enabled', '1' if request.form.get('year_recap_enabled') == 'on' else '0')
    set_setting('april_fools_enabled', '1' if request.form.get('april_fools_enabled') == 'on' else '0')
    set_setting('footer_discord_url', (request.form.get('footer_discord_url') or '').strip())
    for lt in ('main', 'challenge', 'platformer'):
        raw = str(request.form.get(f'record_proof_top_n_{lt}') or '').strip()
        try:
            n = max(0, min(10000, int(float(raw))))
        except Exception:
            n = record_proof_top_n_for_list(lt)
        set_setting(f'record_proof_top_n_{lt}', str(n))
    set_setting('record_proof_top_n', str(max(0, min(10000, int(float(str(request.form.get('record_proof_top_n_main') or '30')))))))
    flash('UI settings updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/settings/auto_points', methods=['POST'])
@login_required
def admin_set_auto_points():
    _require_perm('manage_settings')
    set_setting('auto_points_enabled', '1' if request.form.get('auto_points_enabled') == 'on' else '0')
    scope = (request.form.get('auto_points_scope') or '').strip().lower()
    if scope not in ('main', 'challenge', 'platformer', 'both', 'all'):
        scope = 'both'
    set_setting('auto_points_scope', scope)
    for lt in ('main', 'challenge', 'platformer'):
        for verified in (True, False):
            key_suffix = 'verified' if verified else 'unverified'
            field = f'auto_points_rules_{lt}_{key_suffix}'
            set_setting(auto_points_rules_key(lt, verified), (request.form.get(field) or '').strip())
    if request.form.get('recalc_now') == 'on' and auto_points_enabled():
        auto_points_recalc_all()
    add_site_log('admin_action', f'{current_user.username} updated auto points settings.', actor_user_id=current_user.id, send_to_discord=False)
    flash('Auto points settings updated.', 'success')
    return redirect(url_for('admin.admin_dashboard'))


# ── Maintenance ───────────────────────────────────────────────────────────────

@admin_bp.route('/admin/maintenance', methods=['POST'])
@login_required
def admin_maintenance():
    _require_perm('manage_settings')
    action = (request.form.get('action') or '').strip().lower()

    if action == 'recalc_points':
        try:
            recalc_all_user_points()
            add_site_log('admin_action', f'{current_user.username} recalculated all user points.', actor_user_id=current_user.id)
            flash('Recalculated all user points.', 'success')
        except Exception:
            flash('Failed to recalculate user points.', 'danger')

    elif action == 'ensure_schema':
        try:
            from auxiliary.schema import ensure_schema
            ensure_schema()
            flash('Schema check complete.', 'success')
        except Exception:
            flash('Schema check failed.', 'danger')

    elif action == 'prune_logs':
        try:
            days = max(1, int(float(str(request.form.get('log_days') or '0').strip() or '0')))
            cutoff = datetime.utcnow() - timedelta(days=days)
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

    elif action == 'backup_now':
        try:
            dst_dir = os.path.join(current_app.root_path, 'backups')
            os.makedirs(dst_dir, exist_ok=True)
            uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
            if uri.startswith('sqlite:///'):
                candidate = uri.replace('sqlite:///', '', 1)
                src = candidate if os.path.isabs(candidate) else os.path.join(current_app.instance_path, candidate)
            else:
                src = os.path.join(current_app.instance_path, 'database.db')
            if not os.path.exists(src):
                flash('Database file not found.', 'danger')
                return redirect(url_for('admin.admin_dashboard'))
            ts = datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')
            dst = os.path.join(dst_dir, f'manual-{ts}-{uuid.uuid4().hex[:8]}.db')
            shutil.copy2(src, dst)
            add_site_log('admin_action', f'{current_user.username} created a manual backup.', actor_user_id=current_user.id, send_to_discord=False)
            flash('Backup created.', 'success')
        except Exception:
            flash('Failed to create backup.', 'danger')

    elif action == 'set_auto_backup':
        try:
            hours = request.form.get('auto_backup_interval_hours', '24').strip()
            hours = max(0, min(720, int(float(hours))))
            from auxiliary.settings_helpers import set_setting
            set_setting('auto_backup_interval_hours', str(hours))
            add_site_log('admin_action', f'{current_user.username} set auto-backup interval to {hours}h.', actor_user_id=current_user.id, send_to_discord=False)
            flash(f'Auto-backup interval set to {hours} hours.', 'success')
        except Exception:
            flash('Invalid interval value.', 'danger')

    elif action == 'expire_pending':
        try:
            days = max(1, int(float(str(request.form.get('days') or '0').strip() or '0')))
            cutoff = datetime.utcnow() - timedelta(days=days)
            did = []
            if request.form.get('expire_records') == 'on':
                n = db.session.query(Record).filter(Record.status == 'pending', Record.submitted_at.isnot(None), Record.submitted_at < cutoff).update({Record.status: 'rejected'}, synchronize_session=False)
                did.append(f'{int(n or 0)} pending records')
            if request.form.get('expire_submissions') == 'on':
                n = db.session.query(LevelSubmission).filter(LevelSubmission.status == 'pending', LevelSubmission.submitted_at.isnot(None), LevelSubmission.submitted_at < cutoff).update({LevelSubmission.status: 'rejected'}, synchronize_session=False)
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

    elif action == 'backfill_bcoins':
        try:
            from models import User, Record, Level
            total = 0
            users = User.query.all()
            for u in users:
                earned = 0
                approved = Record.query.filter_by(user_id=u.id, status='approved').all()
                for r in approved:
                    lv = db.session.get(Level, r.level_id)
                    if lv:
                        earned += max(1, int(float(lv.points_reward or 0) / 2))
                if earned:
                    u.bcoins = max(u.bcoins or 0, earned)
                    u.total_bcoins_earned = max(u.total_bcoins_earned or 0, earned)
                    total += 1
            db.session.commit()
            flash(f'Backfilled bcoins for {total} users based on their approved records.', 'success')
        except Exception:
            try: db.session.rollback()
            except: pass
            flash('Failed to backfill bcoins.', 'danger')

    elif action == 'clear_proof_range':
        try:
            from_dt = datetime.strptime(request.form.get('from_date'), '%Y-%m-%d')
            to_dt = datetime.strptime(request.form.get('to_date'), '%Y-%m-%d') + timedelta(days=1)
            n = db.session.query(Record).filter(Record.submitted_at >= from_dt, Record.submitted_at < to_dt).update({Record.video_url: ''}, synchronize_session=False)
            db.session.commit()
            flash(f'Cleared video URLs for {int(n or 0)} record(s).', 'success')
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash(f'Failed to clear proof: {str(e)[:100]}', 'danger')

    else:
        flash('Unknown maintenance action.', 'danger')

    return redirect(url_for('admin.admin_dashboard'))


# ── Pages ─────────────────────────────────────────────────────────────────────

@admin_bp.route('/admin/pages')
@login_required
def admin_pages():
    _require_perm('manage_pages')
    pages = Page.query.order_by(Page.updated_at.desc()).all()
    return render_template('admin_pages.html', pages=pages)


@admin_bp.route('/admin/pages/create', methods=['POST'])
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
        return redirect(url_for('admin.admin_pages'))
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin.admin_pages'))
    if Page.query.filter(Page.slug == cleaned).first():
        flash('Slug already exists.', 'danger')
        return redirect(url_for('admin.admin_pages'))
    now = datetime.utcnow()
    # SECURITY: raw-HTML pages are an XSS vector - only top-level admins
    # may enable is_html; everyone else's pages render escaped.
    if is_html and get_user_rank(current_user) != 'admin':
        is_html = False
    page = Page(slug=cleaned, title=title[:200], body=body, is_html=is_html, created_at=now, updated_at=now)
    db.session.add(page)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} created page "{cleaned}".', actor_user_id=current_user.id)
    flash('Page created.', 'success')
    return redirect(url_for('admin.admin_pages'))


@admin_bp.route('/admin/pages/<int:page_id>/update', methods=['POST'])
@login_required
def admin_pages_update(page_id):
    _require_perm('manage_pages')
    page = Page.query.get_or_404(page_id)
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    is_html = request.form.get('is_html') == 'on'
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin.admin_pages'))
    page.title = title[:200]
    page.body = body
    # SECURITY: only top-level admins may change raw-HTML rendering
    if get_user_rank(current_user) == 'admin':
        page.is_html = is_html
    page.updated_at = datetime.utcnow()
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} updated page "{page.slug}".', actor_user_id=current_user.id)
    flash('Page updated.', 'success')
    return redirect(url_for('admin.admin_pages'))


@admin_bp.route('/admin/pages/<int:page_id>/edit', methods=['GET', 'POST'])
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
            return redirect(url_for('admin.admin_pages_edit', page_id=page_id))
        page.title = title[:200]
        page.body = body
        page.is_html = is_html
        page.updated_at = datetime.utcnow()
        db.session.commit()
        add_site_log('admin_action', f'{current_user.username} updated page "{page.slug}".', actor_user_id=current_user.id)
        flash('Page updated.', 'success')
        return redirect(url_for('admin.admin_pages_edit', page_id=page_id))
    return render_template('admin_page_edit.html', page=page)


@admin_bp.route('/admin/pages/<int:page_id>/delete', methods=['POST', 'GET'])
@login_required
def admin_pages_delete(page_id):
    _require_perm('manage_pages')
    page = Page.query.get_or_404(page_id)
    if request.method == 'GET':
        return render_template('admin_page_delete.html', page=page)
    slug = page.slug
    db.session.delete(page)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted page "{slug}".', actor_user_id=current_user.id)
    flash('Page deleted.', 'success')
    return redirect(url_for('admin.admin_pages'))


# ── Events ────────────────────────────────────────────────────────────────────

@admin_bp.route('/admin/events')
@login_required
def admin_events():
    _require_perm('manage_events')
    events = Event.query.order_by(Event.created_at.desc()).all()
    return render_template('admin_events.html', events=events)


@admin_bp.route('/admin/events/create', methods=['POST'])
@login_required
def admin_events_create():
    _require_perm('manage_events')
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin.admin_events'))
    ev = Event(title=title[:200], body=body,
               starts_at=_parse_dt_local(request.form.get('starts_at') or ''),
               ends_at=_parse_dt_local(request.form.get('ends_at') or ''),
               is_published=request.form.get('is_published') == 'on',
               created_at=datetime.utcnow())
    db.session.add(ev)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} created event "{title[:100]}".', actor_user_id=current_user.id)
    flash('Event created.', 'success')
    return redirect(url_for('admin.admin_events'))


@admin_bp.route('/admin/events/<int:event_id>/update', methods=['POST'])
@login_required
def admin_events_update(event_id):
    _require_perm('manage_events')
    ev = Event.query.get_or_404(event_id)
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    if not title or not body:
        flash('Title and body are required.', 'danger')
        return redirect(url_for('admin.admin_events'))
    ev.title = title[:200]
    ev.body = body
    ev.starts_at = _parse_dt_local(request.form.get('starts_at') or '')
    ev.ends_at = _parse_dt_local(request.form.get('ends_at') or '')
    ev.is_published = request.form.get('is_published') == 'on'
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} updated event #{ev.id}.', actor_user_id=current_user.id)
    flash('Event updated.', 'success')
    return redirect(url_for('admin.admin_events'))


@admin_bp.route('/admin/events/<int:event_id>/delete', methods=['POST'])
@login_required
def admin_events_delete(event_id):
    _require_perm('manage_events')
    ev = Event.query.get_or_404(event_id)
    title = ev.title
    db.session.delete(ev)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted event "{title[:100]}".', actor_user_id=current_user.id)
    flash('Event deleted.', 'success')
    return redirect(url_for('admin.admin_events'))


# ── User management ───────────────────────────────────────────────────────────

@admin_bp.route('/admin/users')
@login_required
def admin_users():
    _require_perm('manage_users')
    q = (request.args.get('q') or '').strip().lower()
    page = safe_int(request.args.get('page', 1), default=1)
    per_page = 50
    base = User.query
    if q:
        like = f'%{q}%'
        base = base.filter(db.func.lower(User.username).like(like) | db.func.lower(db.func.coalesce(User.discord_tag, '')).like(like))
    pagination = base.order_by(User.username.asc()).paginate(page=page, per_page=per_page, error_out=False)
    users = pagination.items
    return render_template('admin_users.html', users=users, pagination=pagination, admin_q=q)


# ── Custom lists / Map packs ──────────────────────────────────────────────────

@admin_bp.route('/admin/custom-lists', methods=['GET', 'POST'])
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
                db.session.add(CustomList(name=name, spreadsheet_url=url))
                db.session.commit()
                flash('Custom list added.', 'success')
        elif action == 'delete':
            cid = request.form.get('id')
            cl = CustomList.query.get(cid)
            if cl:
                db.session.delete(cl)
                db.session.commit()
                flash('Custom list deleted.', 'success')
        return redirect(url_for('admin.admin_custom_lists'))
    lists = CustomList.query.order_by(CustomList.created_at.desc()).all()
    return render_template('admin_custom_lists.html', lists=lists)


@admin_bp.route('/admin/map-packs', methods=['GET', 'POST'])
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
                        tdir = os.path.join(current_app.root_path, 'static', 'mappack_thumbs')
                        os.makedirs(tdir, exist_ok=True)
                        file.save(os.path.join(tdir, thumb_filename))
            if not name:
                flash('Name is required.', 'danger')
            else:
                db.session.add(MapPack(name=name, level_ids=ids, thumbnail_filename=thumb_filename))
                db.session.commit()
                flash('Map pack added.', 'success')
        elif action == 'delete':
            mid = request.form.get('id')
            mp = MapPack.query.get(mid)
            if mp:
                if mp.thumbnail_filename:
                    try:
                        os.remove(os.path.join(current_app.root_path, 'static', 'mappack_thumbs', mp.thumbnail_filename))
                    except Exception:
                        pass
                db.session.delete(mp)
                db.session.commit()
                flash('Map pack deleted.', 'success')
        return redirect(url_for('admin.admin_map_packs'))
    packs = MapPack.query.order_by(MapPack.created_at.desc()).all()
    return render_template('admin_map_packs.html', packs=packs)


# ── Song submissions ──────────────────────────────────────────────────────────

@admin_bp.route('/admin/song-submissions')
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


@admin_bp.route('/admin/song-submissions/<int:sub_id>/approve', methods=['POST'])
@login_required
def approve_song_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    if sub.status == 'pending':
        level = sub.level or (Level.query.filter_by(gd_level_id=sub.gd_level_id).first() if sub.gd_level_id else None)
        if not level:
            flash('Cannot approve: no associated level found.', 'error')
            return redirect(url_for('admin.admin_song_submissions'))
        src_path = os.path.join(current_app.root_path, 'static', 'pending_songs', sub.song_filename)
        dst_path = os.path.join(current_app.root_path, 'static', 'songs', sub.song_filename)
        try:
            if os.path.exists(src_path):
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.move(src_path, dst_path)
        except Exception as e:
            flash(f'Error moving file: {str(e)[:100]}', 'error')
            return redirect(url_for('admin.admin_song_submissions'))
        sub.status = 'approved'
        sub.reviewed_at = datetime.utcnow()
        sub.reviewed_by_user_id = current_user.id
        db.session.commit()
        flash(f'Song submission approved for {level.name}.', 'success')
    else:
        flash('Submission already processed.', 'info')
    return redirect(url_for('admin.admin_song_submissions'))


@admin_bp.route('/admin/song-submissions/<int:sub_id>/reject', methods=['POST'])
@login_required
def reject_song_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    if sub.status == 'pending':
        sub.status = 'rejected'
        sub.reviewed_at = datetime.utcnow()
        sub.reviewed_by_user_id = current_user.id
        db.session.commit()
        try:
            file_path = os.path.join(current_app.root_path, 'static', 'pending_songs', sub.song_filename)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        flash(f'Song submission rejected: {sub.original_filename}', 'success')
    else:
        flash('Submission already processed.', 'info')
    return redirect(url_for('admin.admin_song_submissions'))


@admin_bp.route('/admin/song-submissions/<int:sub_id>/edit', methods=['POST'])
@login_required
def edit_song_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    if request.form.get('song_name'):
        sub.song_name = request.form.get('song_name')
    if request.form.get('artist_name'):
        sub.artist_name = request.form.get('artist_name')
    if request.form.get('song_id'):
        sub.song_id = request.form.get('song_id')
    new_gd_id = request.form.get('gd_level_id')
    if new_gd_id:
        sub.gd_level_id = new_gd_id
        level = Level.query.filter_by(gd_level_id=new_gd_id).first()
        if level:
            sub.level_id = level.id
    db.session.commit()
    flash(f'Song submission updated: {sub.original_filename}', 'success')
    return redirect(url_for('admin.admin_song_submissions'))


@admin_bp.route('/admin/song-submissions/<int:sub_id>/delete', methods=['POST'])
@login_required
def delete_song_submission(sub_id):
    _require_perm('moderate_submissions')
    sub = SongSubmission.query.get_or_404(sub_id)
    try:
        file_path = os.path.join(current_app.root_path, 'static', 'pending_songs', sub.song_filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass
    db.session.delete(sub)
    db.session.commit()
    flash('Song submission deleted.', 'success')
    return redirect(url_for('admin.admin_song_submissions'))


# ── Shop editor ───────────────────────────────────────────────────────────────

@admin_bp.route('/admin/shop')
@login_required
def admin_shop():
    _require_perm('manage_settings')
    items = ShopItem.query.order_by(ShopItem.created_at.desc()).all()
    return render_template('admin_shop.html', items=items)


@admin_bp.route('/admin/shop/create', methods=['POST'])
@login_required
def admin_shop_create():
    _require_perm('manage_settings')
    name = (request.form.get('name') or '').strip()[:200]
    price = int(request.form.get('price', 0))
    item_type = (request.form.get('item_type') or 'profile_deco').strip().lower()
    description = (request.form.get('description') or '').strip()
    if not name or price <= 0:
        flash('Name and price are required.', 'danger')
        return redirect(url_for('admin.admin_shop'))
    file = request.files.get('image')
    image_filename = None
    if file and file.filename:
        from utils.security import save_uploaded_file
        folder = os.path.join(current_app.root_path, 'static', 'shop_images')
        unique, err = save_uploaded_file(file, folder, {'png', 'jpg', 'jpeg', 'webp', 'gif'}, 'image/')
        if not unique:
            flash(f'Image upload failed: {err}', 'danger')
            return redirect(url_for('admin.admin_shop'))
        image_filename = unique
    is_limited = request.form.get('is_limited') == 'on'
    expires_at = None
    if is_limited:
        expires_raw = (request.form.get('expires_at') or '').strip()
        if expires_raw:
            try:
                expires_at = datetime.strptime(expires_raw, '%Y-%m-%dT%H:%M')
            except ValueError:
                flash('Invalid expiry date format.', 'danger')
                return redirect(url_for('admin.admin_shop'))
    item = ShopItem(name=name, price=price, item_type=item_type, description=description,
                    image_filename=image_filename, is_limited=is_limited, expires_at=expires_at)
    db.session.add(item)
    db.session.commit()
    flash('Shop item created.', 'success')
    return redirect(url_for('admin.admin_shop'))


@admin_bp.route('/admin/shop/<int:item_id>/edit', methods=['POST'])
@login_required
def admin_shop_edit(item_id):
    _require_perm('manage_settings')
    item = ShopItem.query.get_or_404(item_id)
    item.name = (request.form.get('name') or '').strip()[:200]
    item.price = max(0, int(request.form.get('price', 0)))
    item.item_type = (request.form.get('item_type') or 'profile_deco').strip().lower()
    item.description = (request.form.get('description') or '').strip()
    item.is_available = request.form.get('is_available') == 'on'
    item.is_limited = request.form.get('is_limited') == 'on'
    expires_raw = (request.form.get('expires_at') or '').strip()
    if item.is_limited and expires_raw:
        try:
            item.expires_at = datetime.strptime(expires_raw, '%Y-%m-%dT%H:%M')
        except ValueError:
            flash('Invalid expiry date format.', 'danger')
    elif not item.is_limited:
        item.expires_at = None
    file = request.files.get('image')
    if file and file.filename:
        from utils.security import save_uploaded_file
        folder = os.path.join(current_app.root_path, 'static', 'shop_images')
        unique, err = save_uploaded_file(file, folder, {'png', 'jpg', 'jpeg', 'webp', 'gif'}, 'image/')
        if not unique:
            flash(f'Image upload failed: {err}', 'danger')
        else:
            item.image_filename = unique
    db.session.commit()
    flash('Shop item updated.', 'success')
    return redirect(url_for('admin.admin_shop'))


@admin_bp.route('/admin/shop/<int:item_id>/delete', methods=['POST'])
@login_required
def admin_shop_delete(item_id):
    _require_perm('manage_settings')
    item = ShopItem.query.get_or_404(item_id)
    db.session.query(UserPurchase).filter_by(item_id=item.id).delete()
    db.session.delete(item)
    db.session.commit()
    flash('Shop item deleted.', 'success')
    return redirect(url_for('admin.admin_shop'))


@admin_bp.route('/admin/shop/award', methods=['GET', 'POST'])
@login_required
def admin_shop_award():
    _require_perm('manage_settings')
    if request.method == 'POST':
        user_id = int(request.form.get('user_id', 0))
        item_id = int(request.form.get('item_id', 0))
        if not user_id or not item_id:
            flash('User and item are required.', 'danger')
            return redirect(url_for('admin.admin_shop_award'))
        user = User.query.get(user_id)
        item = ShopItem.query.get(item_id)
        if not user or not item:
            flash('User or item not found.', 'danger')
            return redirect(url_for('admin.admin_shop_award'))
        purchase = UserPurchase(user_id=user.id, item_id=item.id)
        db.session.add(purchase)
        db.session.commit()
        flash(f'Awarded "{item.name}" to {user.username}.', 'success')
        return redirect(url_for('admin.admin_shop_award'))
    users = User.query.order_by(User.username).all()
    items = ShopItem.query.order_by(ShopItem.name).all()
    return render_template('admin_shop_award.html', users=users, items=items)


# ── IDBBL / Sparky ────────────────────────────────────────────────────────────

@admin_bp.route('/admin/idbbl')
@login_required
def admin_idbbl():
    _require_perm('manage_settings')
    images = SparkyImage.query.order_by(SparkyImage.created_at.desc()).all()
    levels = Level.query.order_by(Level.list_type, Level.position).all()
    return render_template('admin_idbbl.html', images=images, levels=levels)


@admin_bp.route('/admin/idbbl/upload', methods=['POST'])
@login_required
def admin_idbbl_upload():
    _require_perm('manage_settings')
    base_name = (request.form.get('name') or '').strip()
    level_id = request.form.get('level_id', type=int) or None
    if not base_name:
        flash('Name is required.', 'danger')
        return redirect(url_for('admin.admin_idbbl'))
    files = request.files.getlist('images')
    if not files or all(not f.filename for f in files):
        flash('At least one image file is required.', 'danger')
        return redirect(url_for('admin.admin_idbbl'))
    sparky_dir = os.path.join(current_app.root_path, 'static', 'sparky')
    os.makedirs(sparky_dir, exist_ok=True)
    uploaded = 0
    for i, file in enumerate(files):
        if not file or not getattr(file, 'filename', None):
            continue
        ext = (file.filename.rsplit('.', 1)[-1] if '.' in file.filename else '').lower()
        if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
            continue
        unique, err = save_uploaded_file(file, sparky_dir, {'png', 'jpg', 'jpeg', 'gif', 'webp'}, 'image/')
        if not unique:
            continue
        img_name = f'{base_name} #{i+1}' if len(files) > 1 else base_name
        img = SparkyImage(name=img_name, filename=unique, level_id=level_id,
                          is_approved=True, submitted_by_user_id=current_user.id)
        db.session.add(img)
        uploaded += 1
    db.session.commit()
    if uploaded:
        add_site_log('sparky', f'{uploaded} sparky image(s) uploaded.', actor_user_id=current_user.id)
        flash(f'{uploaded} sparky image(s) uploaded.', 'success')
    else:
        flash('No valid images were uploaded.', 'danger')
    return redirect(url_for('admin.admin_idbbl'))


@admin_bp.route('/admin/idbbl/<int:img_id>/delete', methods=['POST'])
@login_required
def admin_idbbl_delete(img_id):
    _require_perm('manage_settings')
    img = SparkyImage.query.get_or_404(img_id)
    path = os.path.join(current_app.root_path, 'static', 'sparky', img.filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(img)
    db.session.commit()
    add_site_log('sparky', f'Sparky image "{img.name}" deleted.', actor_user_id=current_user.id)
    flash(f'Sparky image "{img.name}" deleted.', 'success')
    return redirect(url_for('admin.admin_idbbl'))


@admin_bp.route('/admin/idbbl/<int:img_id>/toggle', methods=['POST'])
@login_required
def admin_idbbl_toggle(img_id):
    _require_perm('manage_settings')
    img = SparkyImage.query.get_or_404(img_id)
    img.is_approved = not img.is_approved
    db.session.commit()
    flash(f'Sparky image "{img.name}" {"approved" if img.is_approved else "unapproved"}.', 'success')
    return redirect(url_for('admin.admin_idbbl'))


# ── Database editor ───────────────────────────────────────────────────────────

MODEL_MAP = {
    'User': User, 'Level': Level, 'Record': Record, 'LevelSubmission': LevelSubmission,
    'Page': Page, 'Event': Event, 'Clan': Clan, 'ClanJoinRequest': ClanJoinRequest,
    'NewsPost': NewsPost, 'Setting': Setting, 'SiteLog': SiteLog,
    'CustomList': CustomList, 'MapPack': MapPack,
    'ShopItem': ShopItem, 'UserPurchase': UserPurchase, 'SparkyImage': SparkyImage, 'GuessGameScore': GuessGameScore,
}


def _build_model_list():
    models = []
    for name, m in sorted(MODEL_MAP.items(), key=lambda x: x[0].lower()):
        try:
            count = int(m.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(m, '__tablename__', name.lower())})
    return models


@admin_bp.route('/admin/db')
@login_required
def admin_db():
    _require_perm('admin_db')
    models = []
    for name, model in sorted(MODEL_MAP.items(), key=lambda x: x[0].lower()):
        try:
            count = int(model.query.count() or 0)
        except Exception:
            count = 0
        models.append({'name': name, 'count': count, 'table': getattr(model, '__tablename__', name.lower())})
    return render_template('admin_db.html', models=models, selected=None, active_tab='structure', db_server='localhost', db_name='site')


@admin_bp.route('/admin/db/<model_name>')
@login_required
def admin_db_table(model_name):
    _require_perm('admin_db')
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    page = safe_int(request.args.get('page', 1), default=1)
    per_page = max(5, min(200, int(request.args.get('per_page', 25) or 25) if str(request.args.get('per_page', '25')).isdigit() else 25))
    q = (request.args.get('q') or '').strip()
    tab = (request.args.get('tab') or 'browse').strip().lower()
    query = model.query
    if q:
        from sqlalchemy import or_
        cols = [c.name for c in model.__table__.columns]
        filt = []
        for c in cols:
            try:
                col = getattr(model, c)
                if 'CHAR' in str(col.type).upper() or 'TEXT' in str(col.type).upper():
                    filt.append(col.ilike(f'%{q}%'))
            except Exception:
                continue
        if filt:
            query = query.filter(or_(*filt))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    columns = [c.name for c in model.__table__.columns]
    rows_data = [{col: getattr(r, col) for col in columns} for r in pagination.items]
    models = [{'name': n, 'count': int(m.query.count() or 0), 'table': getattr(m, '__tablename__', n.lower())} for n, m in sorted(MODEL_MAP.items(), key=lambda x: x[0].lower())]
    return render_template('admin_db.html', selected=model_name, columns=columns, rows=rows_data, pagination=pagination, models=models, active_tab=tab, q=q, per_page=per_page, db_server='localhost', db_name='site')


@admin_bp.route('/admin/db/<model_name>/structure')
@login_required
def admin_db_structure(model_name):
    _require_perm('admin_db')
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    models = [{'name': n, 'count': int(m.query.count() or 0), 'table': getattr(m, '__tablename__', n.lower())} for n, m in sorted(MODEL_MAP.items(), key=lambda x: x[0].lower())]
    cols = [{'name': c.name, 'type': str(c.type), 'nullable': bool(c.nullable), 'primary_key': bool(c.primary_key), 'default': str(c.default.arg) if getattr(c, 'default', None) and getattr(c.default, 'arg', None) else ''} for c in model.__table__.columns]
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='structure', structure_cols=cols, db_server='localhost', db_name='site')


@admin_bp.route('/admin/db/<model_name>/export')
@login_required
def admin_db_export(model_name):
    _require_perm('admin_db')
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    fmt = (request.args.get('fmt') or 'csv').strip().lower()
    limit = max(1, min(200000, int(request.args.get('limit', 20000))))
    cols = [c.name for c in model.__table__.columns]
    rows = model.query.limit(limit).all()
    if fmt == 'json':
        out = [{c: getattr(r, c) for c in cols} for r in rows]
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
    return Response(buf.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename="{model_name}.csv"'})


@admin_bp.route('/admin/db/<model_name>/sql', methods=['GET', 'POST'])
@login_required
def admin_db_sql(model_name):
    _require_perm('admin_db')
    from sqlalchemy import text
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    models = [{'name': n, 'count': int(m.query.count() or 0), 'table': getattr(m, '__tablename__', n.lower())} for n, m in sorted(MODEL_MAP.items(), key=lambda x: x[0].lower())]
    sql_text = ''
    result_cols = []
    result_rows = []
    error = ''
    if request.method == 'POST':
        sql_text = (request.form.get('sql') or '').strip()
        low = sql_text.strip().lower()
        # SECURITY: read-only enforcement via a real SQLite read-only connection
        # (keyword blocklists are bypassable). Falls back to strict checks.
        import sqlite3 as _sq3
        ro_conn = None
        try:
            from flask import current_app as _ca
            _uri = _ca.config.get('SQLALCHEMY_DATABASE_URI', '')
            if 'sqlite:///' in _uri:
                _p = _uri.split('sqlite:///')[-1]
                if not _p or not _p[1:2] == ':':
                    import os as _os
                    _p = _os.path.join(_ca.instance_path, _p)
                ro_conn = _sq3.connect(f'file:{_p}?mode=ro', uri=True)
                cur = ro_conn.execute(sql_text)
                result_cols = [d[0] for d in cur.description] if cur.description else []
                result_rows = [list(r) for r in cur.fetchmany(500)]
            else:
                raise RuntimeError('non-sqlite')
        except _sq3.Error as e:
            error = f'SQL error: {e}'
        except Exception:
            # Fallback for non-sqlite backends (kept restrictive on purpose)
            if not (low.startswith('select') or low.startswith('with')):
                error = 'Only SELECT queries are allowed here.'
            elif any(b in low for b in ['insert', 'update', 'delete', 'drop', 'alter', 'create', 'pragma', 'attach']):
                error = 'This query contains a blocked keyword.'
            elif ';' in sql_text.rstrip(';'):
                error = 'Only one statement at a time.'
            else:
                try:
                    res = db.session.execute(text(sql_text))
                    try:
                        result_cols = list(res.keys())
                    except Exception:
                        result_cols = []
                    result_rows = [list(r) for r in res.fetchmany(500)]
                except Exception:
                    error = 'SQL failed.'
        finally:
            if ro_conn is not None:
                try:
                    ro_conn.close()
                except Exception:
                    pass
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='sql', sql_text=sql_text, sql_error=error, sql_cols=result_cols, sql_rows=result_rows, db_server='localhost', db_name='site')


@admin_bp.route('/admin/db/<model_name>/operations', methods=['GET', 'POST'])
@login_required
def admin_db_operations(model_name):
    _require_perm('admin_db')
    from sqlalchemy import text as sa_text
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    models = _build_model_list()
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        tbl = getattr(model, '__tablename__', '')
        if action == 'empty':
            try:
                db.session.execute(sa_text(f'DELETE FROM {tbl}'))
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
                db.session.execute(sa_text('VACUUM'))
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
                    db.session.execute(sa_text(f'DROP TABLE {tbl}'))
                    db.session.commit()
                    add_site_log('admin_action', f'{current_user.username} dropped table {tbl}.', actor_user_id=current_user.id)
                    flash('Table dropped.', 'success')
                    return redirect(url_for('admin.admin_db'))
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                    flash('Drop failed.', 'danger')
            else:
                flash('Drop confirmation failed.', 'danger')
        return redirect(url_for('admin.admin_db_operations', model_name=model_name))
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='operations', db_server='localhost', db_name='site')


@admin_bp.route('/admin/db/<model_name>/insert', methods=['GET', 'POST'])
@login_required
def admin_db_insert(model_name):
    _require_perm('admin_db')
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    models = _build_model_list()
    cols = []
    try:
        for c in model.__table__.columns:
            cols.append({'name': c.name, 'type': str(c.type), 'nullable': bool(c.nullable), 'primary_key': bool(c.primary_key)})
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
            return redirect(url_for('admin.admin_db_table', model_name=model_name))
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash('Insert failed.', 'danger')
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='insert', insert_cols=cols, db_server='localhost', db_name='site')


@admin_bp.route('/admin/db/<model_name>/import', methods=['GET', 'POST'])
@login_required
def admin_db_import(model_name):
    _require_perm('admin_db')
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    models = _build_model_list()
    columns = [c.name for c in model.__table__.columns]
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not file.filename:
            flash('Select a CSV file.', 'danger')
            return redirect(url_for('admin.admin_db_import', model_name=model_name))
        try:
            import csv
            import io
            raw = file.read().decode('utf-8', errors='ignore')
            reader = csv.DictReader(io.StringIO(raw))
            if not reader.fieldnames:
                flash('CSV must include headers.', 'danger')
                return redirect(url_for('admin.admin_db_import', model_name=model_name))
            fields = [f.strip() for f in reader.fieldnames if f and f.strip()]
            bad = [f for f in fields if f not in columns]
            if bad:
                flash('CSV has unknown columns: ' + ', '.join(bad), 'danger')
                return redirect(url_for('admin.admin_db_import', model_name=model_name))
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
            return redirect(url_for('admin.admin_db_table', model_name=model_name))
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            flash('Import failed.', 'danger')
            return redirect(url_for('admin.admin_db_import', model_name=model_name))
    return render_template('admin_db.html', selected=model_name, models=models, active_tab='import', import_columns=columns, db_server='localhost', db_name='site')


@admin_bp.route('/admin/db/<model_name>/<int:row_id>/delete')
@login_required
def admin_db_delete(model_name, row_id):
    _require_perm('admin_db')
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    row = model.query.get_or_404(row_id)
    if model_name == 'User' and row.id == current_user.id:
        flash('Cannot delete your own account.', 'danger')
        return redirect(url_for('admin.admin_db_table', model_name=model_name))
    db.session.delete(row)
    db.session.commit()
    add_site_log('admin_action', f'{current_user.username} deleted {model_name} #{row_id} via DB editor.', actor_user_id=current_user.id)
    flash(f'{model_name} #{row_id} deleted.', 'success')
    return redirect(url_for('admin.admin_db_table', model_name=model_name))


@admin_bp.route('/admin/db/<model_name>/<int:row_id>/edit', methods=['POST'])
@login_required
def admin_db_edit(model_name, row_id):
    _require_perm('admin_db')
    model = MODEL_MAP.get(model_name)
    if not model:
        abort(404)
    row = model.query.get_or_404(row_id)
    # SECURITY: never editable via the DB editor - privilege/credential fields
    _PROTECTED_COLS = {
        'id', 'password', 'submitted_at', '_csrf_token',
        'is_admin', 'is_mod', 'rank', 'api_token', 'discord_id',
    }
    for key, value in request.form.items():
        if key in _PROTECTED_COLS:
            continue
        if hasattr(row, key):
            cur = getattr(row, key)
            new_val = value
            try:
                if isinstance(cur, bool):
                    new_val = value.lower() in ('1', 'true', 'yes', 'on')
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
    return redirect(url_for('admin.admin_db_table', model_name=model_name))


@admin_bp.route('/admin/download-db')
@login_required
def download_db():
    _require_perm('admin_db')
    from werkzeug.exceptions import NotFound
    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if uri.startswith('sqlite:///'):
        candidate = uri.replace('sqlite:///', '', 1)
        path = candidate if os.path.isabs(candidate) else os.path.join(current_app.instance_path, candidate)
    else:
        path = os.path.join(current_app.instance_path, 'database.db')
    if not os.path.exists(path):
        raise NotFound()
    return send_file(path, as_attachment=True, download_name='database.db')


@admin_bp.route('/admin/shutdown', methods=['POST'])
@login_required
def admin_shutdown():
    _require_perm('manage_settings')
    func = request.environ.get('werkzeug.server.shutdown')
    if not func:
        return jsonify({'ok': False, 'error': 'shutdown_not_supported'}), 503
    add_site_log('admin_action', f'{current_user.username} triggered shutdown.', actor_user_id=current_user.id)
    func()
    return jsonify({'ok': True})


@admin_bp.route('/docs/discord-oauth')
@login_required
def discord_oauth_tutorial():
    if not has_perm(current_user, 'access_admin'):
        abort(404)
    return render_template('discord_oauth_tutorial.html')
