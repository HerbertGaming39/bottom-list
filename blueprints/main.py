"""
blueprints/main.py – Public-facing routes: home list, challenge list, platformer list,
unverified list, level detail, leaderboard, roulette page, search, events, clans,
map packs, custom lists, misc static pages.
"""
import random

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, abort, session, jsonify, send_file, send_from_directory, Response, make_response, send_file as flask_send_file)
from flask_login import login_required, current_user

from config import db
from models import (Level, Record, User, Clan, NewsPost, Event, MapPack,
                    CustomList, ClanJoinRequest, LevelSubmission)
from settings_helpers import has_perm, get_user_rank, rank_label, year_recap_enabled
from utils.cache import cache_get, cache_set, cache_bust_prefixes
from discord_webhooks import add_site_log
import os

main_bp = Blueprint('main', __name__)


# ── Home / Main list ──────────────────────────────────────────────────────────

@main_bp.route('/')
def home():
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 40
    query = request.args.get('q')
    selected_cats = request.args.getlist('cat')
    banned = {'sonic wave brother', 'nugget', 'hey lois do you remember the time i was in the bottom list'}
    all_categories = [
        '2.1', '2.2', 'Medium', 'Long', 'XL', 'XXL+', 'Ship', 'Cube', 'Wave',
        'Robot', 'UFO', 'Spider', 'Swing Copter', 'Dual', 'Timings', 'Memory',
        'Nerve Control', 'Slow Paced', 'Fast Paced', 'High CPS',
        'Chokepoints', 'buffer click abuse', 'buffer click',
        'Ball', 'Spam', 'Unbalanced', 'Precision', 'Flow', 'Learny', 'Sync',
        'Technical', 'Consistency', 'Endurance', 'Straight Fly', 'Click Pattern',
        'Low Refresh Friendly',
    ]
    from settings_helpers import get_setting
    hide_categories = str(get_setting('hide_categories_on_main_list') or '').strip().lower() in ('1', 'true', 'yes', 'on')

    def has_all_categories(level, cats):
        src = (level.categories or '').lower()
        for c in cats:
            if c.lower() not in src:
                return False
        for b in banned:
            if b in src:
                return False
        return True

    is_search = bool(query or selected_cats)

    if query:
        q = query.strip()
        from sqlalchemy import case
        base = Level.query.filter(Level.name.ilike(f'%{q}%') | Level.creator.ilike(f'%{q}%')).all()
        if selected_cats:
            base = [lv for lv in base if has_all_categories(lv, selected_cats)]
        users = (User.query
                 .filter(User.username.ilike(f'%{q}%') | db.func.coalesce(User.discord_username, '').ilike(f'%{q}%'))
                 .order_by(case((db.func.lower(User.username) == q.lower(), 0), else_=1), db.func.lower(User.username))
                 .limit(25).all())
        clans = Clan.query.filter(Clan.name.ilike(f'%{q}%'), ~db.func.lower(Clan.name).like('%raid%')).limit(25).all()
        map_packs_res = MapPack.query.filter(MapPack.name.ilike(f'%{q}%')).limit(25).all()
        news_posts = NewsPost.query.order_by(NewsPost.created_at.desc()).limit(5).all()
        events = Event.query.filter_by(is_published=True).order_by(Event.starts_at.desc().nullslast(), Event.created_at.desc()).limit(3).all()
        resp = make_response(render_template('index.html', levels=base, users=users, clans=clans, map_packs=map_packs_res,
                               search_query=q, is_search=is_search, all_categories=all_categories,
                               selected_cats=selected_cats, news_posts=news_posts, events=events,
                               hide_categories_on_main_list=hide_categories, pagination=None))
        return resp

    levels = cache_get('levels:main:verified')
    if levels is None:
        levels = Level.query.filter_by(is_verified=True, list_type='main').order_by(Level.position).all()
        cache_set('levels:main:verified', levels, 20)
    if selected_cats:
        levels = [lv for lv in levels if has_all_categories(lv, selected_cats)]
    news_posts = cache_get('news:latest5')
    if news_posts is None:
        news_posts = NewsPost.query.order_by(NewsPost.created_at.desc()).limit(5).all()
        cache_set('news:latest5', news_posts, 20)
    events = Event.query.filter_by(is_published=True).order_by(Event.starts_at.desc().nullslast(), Event.created_at.desc()).limit(3).all()
    total = len(levels)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    levels_page = levels[start:start + per_page]
    pagination = {'page': page, 'pages': pages, 'has_prev': page > 1, 'has_next': page < pages,
                  'prev_num': page - 1, 'next_num': page + 1}
    resp = make_response(render_template('index.html', levels=levels_page, users=[], clans=[], map_packs=[],
                           is_search=is_search, all_categories=all_categories, selected_cats=selected_cats,
                           news_posts=news_posts, events=events, hide_categories_on_main_list=hide_categories,
                           pagination=pagination))
    return resp


@main_bp.route('/challenges')
def challenge_list():
    page = max(1, request.args.get('page', 1, type=int))
    pagination = Level.query.filter_by(is_verified=True, list_type='challenge').order_by(Level.position).paginate(page=page, per_page=40, error_out=False)
    return render_template('challenge_list.html', levels=pagination.items, pagination=pagination)


@main_bp.route('/platformer')
def platformer_list():
    page = max(1, request.args.get('page', 1, type=int))
    pagination_obj = Level.query.filter_by(is_verified=True, list_type='platformer').order_by(Level.position).paginate(page=page, per_page=40, error_out=False)
    return render_template('index.html', levels=pagination_obj.items, is_search=False, all_categories=[],
                           selected_cats=[], news_posts=[], hide_categories_on_main_list=True,
                           show_home_header=False, list_title='Platformer List',
                           empty_message='No platformer levels verified yet.', list_accent='purple',
                           pagination=pagination_obj)


@main_bp.route('/unverified')
def unverified_list():
    page = max(1, request.args.get('page', 1, type=int))
    pagination = (Level.query.filter_by(is_verified=False)
                  .order_by(Level.list_type.asc(), Level.position.asc().nullslast(), Level.id.asc())
                  .paginate(page=page, per_page=40, error_out=False))
    levels = list(pagination.items or [])
    wr_by_level = {}
    try:
        level_ids = [int(lv.id) for lv in levels if getattr(lv, 'id', None)]
        if level_ids:
            from sqlalchemy.orm import joinedload
            recs = (Record.query.options(joinedload(Record.user))
                    .filter(Record.status == 'approved', Record.level_id.in_(level_ids))
                    .order_by(Record.level_id.asc(), Record.submitted_at.asc().nullslast(), Record.id.asc())
                    .all())
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


@main_bp.route('/level/<int:level_id>')
def level_detail(level_id):
    level = Level.query.get_or_404(level_id)
    from sqlalchemy.orm import joinedload
    records = Record.query.options(joinedload(Record.user)).filter_by(level_id=level_id, status='approved').all()
    def avg(nums):
        vals = [float(x) for x in (nums or []) if x is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    avg_deco = avg([getattr(r, 'rating_deco', None) for r in records])
    avg_gameplay = avg([getattr(r, 'rating_gameplay', None) for r in records])
    avg_difficulty = avg([getattr(r, 'rating_difficulty', None) for r in records])
    return render_template('level.html', level=level, records=records, avg_deco=avg_deco,
                           avg_gameplay=avg_gameplay, avg_difficulty=avg_difficulty)


# ── Guidelines / static pages ─────────────────────────────────────────────────

@main_bp.route('/guidelines-site')
def guidelines_site():
    return render_template('guidelines_site.html')


@main_bp.route('/privacy')
def privacy():
    return render_template('privacy.html')


@main_bp.route('/page/<slug>')
def view_page(slug):
    from models import Page
    page = Page.query.filter(Page.slug == slug.strip()).first()
    if not page:
        abort(404)
    if bool(getattr(page, 'is_html', False)):
        return Response(page.body or '', mimetype='text/html')
    return render_template('page.html', page=page)


# ── Events ────────────────────────────────────────────────────────────────────

@main_bp.route('/events')
def events_list():
    events = Event.query.filter_by(is_published=True).order_by(Event.starts_at.desc().nullslast(), Event.created_at.desc()).all()
    return render_template('events.html', events=events)


@main_bp.route('/event/<int:event_id>')
def event_detail(event_id):
    event = Event.query.get_or_404(event_id)
    if not bool(getattr(event, 'is_published', True)) and not has_perm(current_user, 'manage_events'):
        abort(404)
    return render_template('event.html', event=event)


# ── Clans ─────────────────────────────────────────────────────────────────────

@main_bp.route('/clans', methods=['GET', 'POST'])
@login_required
def clans():
    if request.method == 'POST':
        if current_user.clan_id:
            flash('Leave your current clan before creating a new one.', 'danger')
            return redirect(url_for('main.clans'))
        name = (request.form.get('name') or '').strip()[:80]
        desc = (request.form.get('description') or '').strip()[:500] or None
        if not name:
            flash('Clan name is required.', 'danger')
            return redirect(url_for('main.clans'))
        if 'raid' in name.lower():
            flash('Clan name cannot include "raid".', 'danger')
            return redirect(url_for('main.clans'))
        existing = Clan.query.filter(db.func.lower(Clan.name) == name.lower()).first()
        if existing:
            flash('A clan with that name already exists.', 'danger')
            return redirect(url_for('main.clans'))
        clan = Clan(name=name, description=desc, owner_user_id=int(current_user.id))
        db.session.add(clan)
        db.session.flush()
        current_user.clan_id = int(clan.id)
        db.session.commit()
        add_site_log('clan_create', f'{current_user.username} created clan "{clan.name}".', actor_user_id=current_user.id)
        flash('Clan created.', 'success')
        return redirect(url_for('main.clan_detail', clan_id=clan.id))
    clans_list = Clan.query.filter(~db.func.lower(Clan.name).like('%raid%')).order_by(Clan.created_at.desc()).all()
    return render_template('clans.html', clans=clans_list)


@main_bp.route('/clan/<int:clan_id>', methods=['GET', 'POST'])
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
        pass
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'request_join':
            if current_user.clan_id:
                flash('Leave your current clan before joining another.', 'danger')
            elif pending_req:
                flash('Join request already pending.', 'warning')
            else:
                jr = ClanJoinRequest(clan_id=int(clan.id), user_id=int(current_user.id))
                db.session.add(jr)
                db.session.commit()
                flash('Join request sent.', 'success')
            return redirect(url_for('main.clan_detail', clan_id=clan.id))
        if action == 'cancel_request' and pending_req:
            db.session.delete(pending_req)
            db.session.commit()
            flash('Join request cancelled.', 'success')
            return redirect(url_for('main.clan_detail', clan_id=clan.id))
        if action == 'leave' and is_member:
            if is_owner:
                flash('Transfer ownership or disband the clan before leaving.', 'danger')
            else:
                current_user.clan_id = None
                db.session.commit()
                flash('Left clan.', 'success')
                return redirect(url_for('main.clans'))
            return redirect(url_for('main.clan_detail', clan_id=clan.id))
        if not is_owner:
            abort(403)
        # Owner-only actions handled inline for brevity
        if action == 'edit_description':
            clan.description = (request.form.get('description') or '').strip()[:500] or None
            db.session.commit()
            flash('Clan updated.', 'success')
        elif action == 'accept_request':
            try:
                req_id = int(request.form.get('request_id') or 0)
            except Exception:
                req_id = 0
            jr = ClanJoinRequest.query.get(req_id) if req_id else None
            if jr and int(jr.clan_id) == int(clan.id):
                u = db.session.get(User, int(jr.user_id))
                if u and not u.clan_id:
                    u.clan_id = int(clan.id)
                    db.session.query(ClanJoinRequest).filter(ClanJoinRequest.user_id == int(u.id)).delete()
                    db.session.commit()
                    flash('Member added.', 'success')
        elif action == 'deny_request':
            try:
                req_id = int(request.form.get('request_id') or 0)
            except Exception:
                req_id = 0
            jr = ClanJoinRequest.query.get(req_id) if req_id else None
            if jr and int(jr.clan_id) == int(clan.id):
                db.session.delete(jr)
                db.session.commit()
                flash('Request denied.', 'success')
        elif action == 'kick_member':
            try:
                user_id = int(request.form.get('user_id') or 0)
            except Exception:
                user_id = 0
            if user_id and user_id != int(clan.owner_user_id or 0):
                u = db.session.get(User, user_id)
                if u and int(getattr(u, 'clan_id', 0) or 0) == int(clan.id):
                    u.clan_id = None
                    db.session.commit()
                    flash('Member removed.', 'success')
        elif action == 'disband':
            db.session.query(User).filter(User.clan_id == int(clan.id)).update({User.clan_id: None})
            db.session.query(ClanJoinRequest).filter(ClanJoinRequest.clan_id == int(clan.id)).delete()
            db.session.delete(clan)
            db.session.commit()
            flash('Clan disbanded.', 'success')
            return redirect(url_for('main.clans'))
        return redirect(url_for('main.clan_detail', clan_id=clan.id))

    members = User.query.filter_by(clan_id=int(clan.id)).order_by(User.username.asc()).all()
    join_requests = ClanJoinRequest.query.filter_by(clan_id=int(clan.id)).order_by(ClanJoinRequest.created_at.asc()).all() if is_owner else []
    return render_template('clan_detail.html', clan=clan, is_owner=is_owner, is_member=is_member,
                           pending_req=pending_req, members=members, join_requests=join_requests)


# ── Map Packs ─────────────────────────────────────────────────────────────────

@main_bp.route('/map-packs')
def map_packs():
    packs = MapPack.query.order_by(MapPack.created_at.desc()).all()
    beaten_ids = set()
    if current_user.is_authenticated:
        try:
            beaten_ids = set(lid for (lid,) in db.session.query(Record.level_id).filter(Record.user_id == current_user.id, Record.status == 'approved').distinct().all() if lid)
        except Exception:
            beaten_ids = set()

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
            level_map = {l.id: l for l in Level.query.filter(Level.id.in_(all_level_ids)).all()}
        except Exception:
            pass
    resolved_packs = []
    for p in packs:
        ids = per_pack_ids.get(p.id) or []
        sorted_levels = [level_map[i] for i in ids if i in level_map]
        beaten_count = sum(1 for l in sorted_levels if l.id in beaten_ids)
        total_count = len(sorted_levels)
        percent = round(beaten_count / total_count * 100, 1) if total_count else 0
        resolved_packs.append({'pack': p, 'levels': sorted_levels, 'beaten_count': beaten_count, 'total_count': total_count, 'percent': percent, 'beaten_ids': beaten_ids})
    return render_template('map_packs.html', packs=resolved_packs)


# ── Custom Lists ──────────────────────────────────────────────────────────────

@main_bp.route('/custom-lists')
def custom_lists():
    lists = CustomList.query.order_by(CustomList.created_at.desc()).all()
    return render_template('custom_lists.html', lists=lists, show_warning=True)


# ── Leaderboard ───────────────────────────────────────────────────────────────

@main_bp.route('/leaderboard')
def leaderboard():
    from blueprints.leaderboard import leaderboard_rows
    sort_by = request.args.get('sort', 'points')
    filter_device = request.args.get('device', '')
    filter_status = request.args.get('status', '')
    view = (request.args.get('view') or 'users').strip().lower()
    list_type = (request.args.get('list') or 'main').strip().lower()
    users, clans, view, list_type = leaderboard_rows(view, list_type, sort_by, filter_device, filter_status)
    return render_template('leaderboard.html', users=users, clans=clans, view=view, sort_by=sort_by,
                           filter_device=filter_device, filter_status=filter_status, list_type=list_type)


# ── Roulette page ─────────────────────────────────────────────────────────────

@main_bp.route('/roulette')
def roulette():
    auth_user_id = None
    auth_username = ''
    try:
        auth_user_id = int(getattr(current_user, 'id', 0) or 0) or None
        auth_username = str(getattr(current_user, 'username', '') or '')
    except Exception:
        pass
    return render_template('roulette.html', auth_user_id=auth_user_id, auth_username=auth_username)


# ── Misc ──────────────────────────────────────────────────────────────────────

@main_bp.route('/forgot-password')
def forgot_password():
    return render_template('forgot_password.html')


@main_bp.route('/icon.png')
def icon_png():
    from flask import current_app
    return send_from_directory(os.path.join(current_app.root_path, 'templates'), 'icon.png')


@main_bp.route('/favicon.png')
def favicon_png():
    from flask import current_app
    return send_from_directory(os.path.join(current_app.root_path, 'templates'), 'favicon.png')


@main_bp.route('/bcoin.png')
def bcoin_png():
    from flask import current_app
    return send_from_directory(os.path.join(current_app.root_path, 'templates'), 'bcoin.png')


@main_bp.route('/tick.mp3')
def tick_mp3():
    from flask import current_app
    return send_from_directory(os.path.join(current_app.root_path, 'templates'), 'tick.mp3')


@main_bp.route('/googlea5c67195c29a4405.html')
def google_verify():
    from flask import current_app
    return send_from_directory(os.path.join(current_app.root_path, 'templates'), 'googlea5c67195c29a4405.html')


@main_bp.route('/assets/discordicon.png')
def discord_icon_png():
    from flask import current_app
    p = os.path.join(current_app.root_path, 'templates', 'discordicon.png')
    try:
        return send_file(p, mimetype='image/png')
    except Exception:
        abort(404)


@main_bp.route('/songs/<filename>')
def serve_song(filename):
    from flask import current_app
    from werkzeug.utils import secure_filename
    from werkzeug.exceptions import NotFound
    name = secure_filename(filename)
    if not name or name != filename or '/' in filename or '\\' in filename:
        raise NotFound()
    base = os.path.realpath(current_app.config['SONG_UPLOAD_FOLDER'])
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise NotFound()
    return send_from_directory(base, name)


@main_bp.route('/pending-songs/<filename>')
def serve_pending_song(filename):
    from flask import current_app
    from werkzeug.utils import secure_filename
    from werkzeug.exceptions import NotFound
    name = secure_filename(filename)
    if not name or name != filename or '/' in filename or '\\' in filename:
        raise NotFound()
    pending_folder = os.path.join(current_app.root_path, 'static', 'pending_songs')
    base = os.path.realpath(pending_folder)
    path = os.path.realpath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise NotFound()
    return send_from_directory(base, name)


