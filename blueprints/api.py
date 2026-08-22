"""
blueprints/api.py – REST API endpoints (/api/*).
Authentication, levels, players, clans, events, roulette pool/spin/rooms.
"""
import os
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, url_for, Response, render_template, abort
from flask_login import login_required, current_user

from config import db
from models import (User, Level, Record, LevelSubmission, Clan, ClanJoinRequest,
                     Event, MapPack, CustomList, ShopItem, MarketListing,
                     SongSubmission, NewsPost, UserPurchase, Page, Clue, SiteLog)
from auxiliary.settings_helpers import has_perm, get_setting
from auxiliary.discord_webhooks import add_site_log

api_bp = Blueprint('api', __name__, url_prefix='/api')

# ── API auth ──────────────────────────────────────────────────────────────────

def _ensure_api_token_for(user):
    tok = (getattr(user, 'api_token', '') or '').strip()
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
    from flask_login import current_user
    if getattr(current_user, 'is_authenticated', False) and not bool(getattr(current_user, 'is_disabled', False)):
        return current_user, None

    # SECURITY: header-only. Query-string tokens leak into access logs,
    # browser history and Referer headers (removed request.args fallback).
    token = (request.headers.get('X-API-Token') or '').strip()
    if not token:
        return None, ('login_required', 401)

    user = User.query.filter_by(api_token=token).first()
    if not user or bool(getattr(user, 'is_disabled', False)):
        return None, ('invalid_token', 403)

    return user, None


def _api_auth():
    user, err = _api_require_user()
    if err:
        key, status = err
        return None, (jsonify({'error': key}), status)
    try:
        msg = f'{request.method} {request.path} via token for {getattr(user, "username", "unknown")}'
        add_site_log('api_call', msg, actor_user_id=getattr(user, 'id', None), send_to_discord=False)
    except Exception:
        pass
    return user, None


def _limit_param(default=100, maximum=500):
    try:
        return max(1, min(maximum, int(request.args.get('limit') or default)))
    except Exception:
        return default


# ── Serialization helpers ─────────────────────────────────────────────────────

def _user_profile_json(u):
    avatar = (getattr(u, 'avatar_filename', '') or '').strip()
    banner = (getattr(u, 'banner_filename', '') or '').strip()
    try:
        avatar_url = url_for('static', filename=f'profile_media/{avatar}') if avatar else None
    except Exception:
        avatar_url = None
    try:
        banner_url = url_for('static', filename=f'profile_media/{banner}') if banner else None
    except Exception:
        banner_url = None
    return {'id': u.id, 'username': u.username, 'points': float(getattr(u, 'points', 0.0) or 0.0),
            'rank': getattr(u, 'rank', None), 'avatar_url': avatar_url, 'banner_url': banner_url}


def _record_json(r, lv):
    img = (getattr(r, 'proof_image_filename', '') or '').strip()
    vc = (getattr(r, 'vc_proof_filename', '') or '').strip()
    try:
        img_url = url_for('static', filename=f'record_proofs/{img}') if img else None
    except Exception:
        img_url = None
    try:
        vc_url = url_for('static', filename=f'vc_proofs/{vc}') if vc else None
    except Exception:
        vc_url = None
    return {
        'id': r.id, 'status': r.status, 'device': getattr(r, 'device', None),
        'video_url': getattr(r, 'video_url', None), 'beaten_on_vc': bool(getattr(r, 'beaten_on_vc', False)),
        'proof_image_url': img_url, 'vc_proof_url': vc_url,
        'submitted_at': r.submitted_at.isoformat() if r.submitted_at else None,
        'level': {'id': getattr(lv, 'id', None), 'name': getattr(lv, 'name', None),
                  'list_type': getattr(lv, 'list_type', None), 'position': getattr(lv, 'position', None),
                  'points_reward': float(getattr(lv, 'points_reward', 0.0) or 0.0)} if lv else None
    }


# ── Health / stats ────────────────────────────────────────────────────────────

@api_bp.route('/health')
def api_health():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    return jsonify({'ok': True})


@api_bp.route('/stats')
def api_stats():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    return jsonify({
        'levels_total': int(db.session.query(db.func.count(Level.id)).scalar() or 0),
        'players_total': int(db.session.query(db.func.count(User.id)).scalar() or 0),
        'clans_total': int(db.session.query(db.func.count(Clan.id)).scalar() or 0),
        'records_total': int(db.session.query(db.func.count(Record.id)).scalar() or 0),
    })


# ── Levels ────────────────────────────────────────────────────────────────────

def _build_levels_query():
    from sqlalchemy import or_
    raw_list = request.args.get('list')
    raw_status = request.args.get('status')
    list_type = (raw_list or '').strip().lower()
    if list_type not in ('', 'all', 'main', 'challenge', 'platformer'):
        return None, jsonify({'error': 'bad_request'}), 400
    status = (raw_status or 'verified').strip().lower()
    if status not in ('verified', 'unverified', 'all'):
        return None, jsonify({'error': 'bad_request'}), 400
    include_unlisted = str(request.args.get('include_unlisted') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    order = (request.args.get('order') or 'position').strip().lower()
    search_q = (request.args.get('q') or '').strip()
    tags_raw = (request.args.get('tag') or request.args.get('tags') or '').strip()
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
            q = q.filter(or_(*[Level.categories.ilike(f'%{t}%') for t in wanted]))
    for param, column, cast in [('min_points', Level.points_reward, float), ('max_points', Level.points_reward, float),
                                  ('min_position', Level.position, int), ('max_position', Level.position, int)]:
        val = request.args.get(param)
        if val is not None and str(val).strip():
            try:
                v = cast(val)
                if param.startswith('min'):
                    q = q.filter(column.isnot(None) if 'position' in param else True, column >= v)
                else:
                    q = q.filter(column.isnot(None) if 'position' in param else True, column <= v)
            except Exception:
                pass
    if not include_unlisted:
        q = q.filter(Level.position.isnot(None))
    if order == 'position':
        q = q.order_by(Level.list_type.asc(), Level.position.asc().nullslast(), Level.id.asc())
    elif order == 'points':
        q = q.order_by(Level.points_reward.desc(), Level.id.asc())
    else:
        q = q.order_by(Level.id.asc())
    return q, None, None


@api_bp.route('/levels')
@api_bp.route('/levels/main')
@api_bp.route('/levels/challenge')
@api_bp.route('/levels/platformer')
@api_bp.route('/levels/unverified')
def api_levels():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    q, err_resp, err_code = _build_levels_query()
    if err_resp:
        return err_resp, err_code
    levels = q.all()
    data = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator,
             'list_type': lv.list_type, 'is_verified': bool(lv.is_verified), 'position': lv.position,
             'points_reward': float(lv.points_reward or 0.0)} for lv in levels]
    return jsonify({'levels': data})


@api_bp.route('/levels/random')
def api_levels_random():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    q, err_resp, err_code = _build_levels_query()
    if err_resp:
        return err_resp, err_code
    levels = q.all()
    if not levels:
        return jsonify({'error': 'not_found'}), 404
    lv = random.choice(levels)
    return jsonify({'level': {'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator,
                              'list_type': lv.list_type, 'is_verified': bool(lv.is_verified),
                              'position': lv.position, 'points_reward': float(lv.points_reward or 0.0)}})


# ── Tags ──────────────────────────────────────────────────────────────────────

@api_bp.route('/tags')
def api_tags():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    rows = db.session.query(Level.categories).filter(Level.categories.isnot(None)).all()
    counts = {}
    for (cats,) in rows:
        for raw in str(cats or '').split(','):
            t = raw.strip()
            if t:
                counts[t] = counts.get(t, 0) + 1
    tags = sorted(counts.keys(), key=lambda k: (-counts[k], k.lower()))
    return jsonify({'tags': tags})


@api_bp.route('/tags/popular')
def api_tags_popular():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    rows = db.session.query(Level.categories).filter(Level.categories.isnot(None)).all()
    counts = {}
    for (cats,) in rows:
        for raw in str(cats or '').split(','):
            t = raw.strip()
            if t:
                counts[t] = counts.get(t, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return jsonify({'tags': [{'tag': k, 'count': int(v or 0)} for k, v in items]})


@api_bp.route('/tags/<string:tag>/levels')
def api_tag_levels(tag):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    t = (tag or '').strip()
    if not t:
        return jsonify({'error': 'bad_request'}), 400
    q = Level.query.filter(Level.categories.isnot(None), Level.categories.ilike(f'%{t}%'))
    levels = q.order_by(Level.list_type.asc(), Level.position.asc().nullslast(), Level.id.asc()).all()
    return jsonify({'levels': [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator,
                                 'list_type': lv.list_type, 'is_verified': bool(lv.is_verified),
                                 'position': lv.position, 'points_reward': float(lv.points_reward or 0.0)} for lv in levels]})


# ── Clans / Players ───────────────────────────────────────────────────────────

@api_bp.route('/clans')
def api_clans():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    q = Clan.query
    name_query = (request.args.get('q') or '').strip()
    if name_query:
        q = q.filter(Clan.name.ilike(f'%{name_query}%'))
    clans = q.order_by(Clan.name.asc()).all()
    out = []
    for c in clans:
        try:
            members_count = len(list(c.members or []))
        except Exception:
            members_count = 0
        out.append({'id': c.id, 'name': c.name, 'description': c.description, 'owner_user_id': c.owner_user_id,
                    'owner_username': getattr(getattr(c, 'owner', None), 'username', None),
                    'member_count': members_count, 'created_at': c.created_at.isoformat() if c.created_at else None})
    return jsonify({'clans': out})


@api_bp.route('/players')
def api_players():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    q = (request.args.get('q') or '').strip()
    sort_by = (request.args.get('sort') or 'points').strip().lower()
    try:
        limit_val = max(1, min(500, int(request.args.get('limit') or 100)))
    except Exception:
        limit_val = 100
    query = User.query
    if q:
        query = query.filter(User.username.ilike(f'%{q}%'))
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
                clan_name = getattr(rel, 'name', None) if rel else None
        except Exception:
            pass
        out.append({'id': u.id, 'username': u.username, 'points': float(getattr(u, 'points', 0.0) or 0.0),
                    'rank': getattr(u, 'rank', None), 'clan_id': clan_id, 'clan_name': clan_name})
    return jsonify({'players': out})


# ── User profile / records ────────────────────────────────────────────────────

@api_bp.route('/users/<int:user_id>/profile')
def api_user_profile(user_id):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'user': _user_profile_json(u)})


@api_bp.route('/users/<int:user_id>/records')
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
    try:
        limit_val = max(1, min(1000, int(request.args.get('limit') or 200)))
    except Exception:
        limit_val = 200
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


@api_bp.route('/me/profile')
def api_me_profile():
    user, auth = _api_auth()
    if auth:
        return auth
    return jsonify({'user': _user_profile_json(user)})


@api_bp.route('/me/records')
def api_me_records():
    user, auth = _api_auth()
    if auth:
        return auth
    return api_user_records(user.id)


@api_bp.route('/me/export')
def api_me_export():
    user, auth = _api_auth()
    if auth:
        return auth
    import json as _json
    fmt = (request.args.get('fmt') or 'json').strip().lower()
    try:
        limit_val = max(1, min(200000, int(request.args.get('limit') or 20000)))
    except Exception:
        limit_val = 20000
    recs = Record.query.filter(Record.user_id == user.id).order_by(Record.submitted_at.desc()).limit(limit_val).all()
    subs = LevelSubmission.query.filter(LevelSubmission.user_id == user.id).order_by(LevelSubmission.submitted_at.desc()).limit(limit_val).all()
    if fmt == 'csv':
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['type', 'id', 'status', 'submitted_at', 'level_id', 'level_name', 'level_creator', 'video_url', 'device', 'comment'])
        for r in recs:
            try:
                lv = r.level
            except Exception:
                lv = None
            w.writerow(['record', r.id, r.status, r.submitted_at, r.level_id, getattr(lv, 'name', ''), getattr(lv, 'creator', ''), r.video_url, r.device, r.comment])
        for s in subs:
            w.writerow(['submission', s.id, s.status, s.submitted_at, '', s.name, s.creator, s.video_url, '', ''])
        return Response(buf.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="my_data_export.csv"'})

    payload = {'user': _user_profile_json(user), 'records': [], 'submissions': []}
    for r in recs:
        try:
            lv = r.level
        except Exception:
            lv = None
        payload['records'].append(_record_json(r, lv))
    for s in subs:
        payload['submissions'].append({'id': s.id, 'name': s.name, 'creator': s.creator, 'video_url': s.video_url, 'status': s.status, 'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None})
    resp = jsonify(payload)
    resp.headers['Content-Disposition'] = 'attachment; filename="my_data_export.json"'
    return resp


# ── Events ────────────────────────────────────────────────────────────────────

@api_bp.route('/events')
def api_events():
    user, auth_err = _api_auth()
    if auth_err:
        return auth_err
    status = (request.args.get('status') or 'published').strip().lower()
    if status not in ('published', 'all'):
        return jsonify({'error': 'bad_request'}), 400
    if status == 'all' and not has_perm(user, 'manage_events'):
        return jsonify({'error': 'forbidden'}), 403
    try:
        limit_val = max(1, min(500, int(request.args.get('limit') or 100)))
    except Exception:
        limit_val = 100
    q = Event.query
    if status == 'published':
        q = q.filter(Event.is_published.is_(True))
    events = q.order_by(Event.starts_at.desc().nullslast(), Event.created_at.desc()).limit(limit_val).all()

    def _serialize(ev):
        return {'id': ev.id, 'title': ev.title, 'body': ev.body,
                'starts_at': ev.starts_at.isoformat() if ev.starts_at else None,
                'ends_at': ev.ends_at.isoformat() if ev.ends_at else None,
                'is_published': bool(ev.is_published),
                'created_at': ev.created_at.isoformat() if ev.created_at else None}

    return jsonify({'events': [_serialize(ev) for ev in events]})


@api_bp.route('/events/<int:event_id>')
def api_event_detail(event_id):
    user, auth_err = _api_auth()
    if auth_err:
        return auth_err
    ev = Event.query.get_or_404(event_id)
    if not bool(getattr(ev, 'is_published', True)) and not has_perm(user, 'manage_events'):
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'event': {'id': ev.id, 'title': ev.title, 'body': ev.body,
                               'starts_at': ev.starts_at.isoformat() if ev.starts_at else None,
                               'ends_at': ev.ends_at.isoformat() if ev.ends_at else None,
                               'is_published': bool(ev.is_published),
                               'created_at': ev.created_at.isoformat() if ev.created_at else None}})


# ── Single level ─────────────────────────────────────────────────────────────

@api_bp.route('/levels/<int:level_id>')
def api_level_detail(level_id):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    lv = db.session.get(Level, level_id)
    if not lv:
        return jsonify({'error': 'not_found'}), 404
    cats = [c.strip() for c in (lv.categories or '').split(',') if c.strip()] if lv.categories else []
    records_count = 0
    try:
        records_count = Record.query.filter_by(level_id=lv.id, status='approved').count()
    except Exception:
        pass
    data = {'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator,
            'list_type': lv.list_type, 'position': lv.position, 'points_reward': float(lv.points_reward or 0.0),
            'is_verified': bool(lv.is_verified), 'difficulty': lv.difficulty,
            'video_url': lv.video_url, 'description': lv.description,
            'categories': cats, 'records_count': records_count,
            'thumbnail_url': url_for('static', filename=f'level_thumbs/{lv.thumbnail_filename}', _external=True) if lv.thumbnail_filename else None,
            'created_at': lv.created_at.isoformat() if lv.created_at else None}
    return jsonify({'level': data})


# ── Leaderboard ───────────────────────────────────────────────────────────────

@api_bp.route('/leaderboard')
@api_bp.route('/leaderboard/<string:list_type>')
def api_leaderboard(list_type=None):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    try:
        limit_val = max(1, min(500, int(request.args.get('limit') or 100)))
    except Exception:
        limit_val = 100
    if list_type and list_type not in ('main', 'challenge', 'platformer', 'unverified'):
        return jsonify({'error': 'bad_request'}), 400

    users = User.query.order_by(User.points.desc(), User.username.asc()).limit(limit_val).all()
    out = []
    for rank, u in enumerate(users, start=1):
        out.append({'rank': rank, 'id': u.id, 'username': u.username, 'points': float(u.points or 0.0),
                    'rank_title': u.rank, 'clan_name': getattr(getattr(u, 'clan', None), 'name', None)})
    return jsonify({'leaderboard': out})


@api_bp.route('/leaderboard/rank/<int:user_id>')
def api_leaderboard_rank(user_id):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    above = User.query.filter(User.points > (u.points or 0.0)).count()
    rank = above + 1
    total = User.query.count()
    return jsonify({'rank': rank, 'total': total, 'username': u.username, 'points': float(u.points or 0.0)})


# ── Records ───────────────────────────────────────────────────────────────────

@api_bp.route('/records')
def api_records():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    try:
        limit_val = max(1, min(500, int(request.args.get('limit') or 100)))
    except Exception:
        limit_val = 100
    status = (request.args.get('status') or 'approved').strip().lower()
    if status not in ('approved', 'pending', 'rejected', 'all'):
        return jsonify({'error': 'bad_request'}), 400
    level_id = request.args.get('level_id', type=int)
    user_id = request.args.get('user_id', type=int)
    q = Record.query
    if status != 'all':
        q = q.filter(Record.status == status)
    if level_id:
        q = q.filter(Record.level_id == level_id)
    if user_id:
        q = q.filter(Record.user_id == user_id)
    rows = q.order_by(Record.submitted_at.desc()).limit(limit_val).all()
    out = []
    for r in rows:
        try:
            lv = r.level
        except Exception:
            lv = None
        j = _record_json(r, lv)
        try:
            ru = r.user
            j['user'] = {'id': ru.id, 'username': ru.username} if ru else None
        except Exception:
            pass
        out.append(j)
    return jsonify({'records': out})


# ── Users / beaten levels ─────────────────────────────────────────────────────

@api_bp.route('/users/<int:user_id>/beaten')
def api_user_beaten(user_id):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    list_type = (request.args.get('list') or '').strip().lower()
    q = Record.query.filter(Record.user_id == u.id, Record.status == 'approved')
    if list_type and list_type != 'all':
        q = q.join(Level, Level.id == Record.level_id).filter(Level.list_type == list_type)
    records = q.order_by(Record.submitted_at.desc()).all()
    out = []
    for r in records:
        try:
            lv = r.level
            out.append({'level_id': lv.id, 'level_name': lv.name, 'list_type': lv.list_type,
                        'position': lv.position, 'points_reward': float(lv.points_reward or 0.0),
                        'video_url': r.video_url, 'submitted_at': r.submitted_at.isoformat() if r.submitted_at else None})
        except Exception:
            pass
    return jsonify({'beaten': out})


# ── Custom lists ──────────────────────────────────────────────────────────────

@api_bp.route('/custom-lists')
def api_custom_lists():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    lists = CustomList.query.order_by(CustomList.name.asc()).all()
    out = []
    for cl in lists:
        out.append({'id': cl.id, 'name': cl.name, 'description': cl.description,
                    'creator_user_id': cl.creator_user_id,
                    'creator_username': getattr(getattr(cl, 'creator', None), 'username', None),
                    'created_at': cl.created_at.isoformat() if cl.created_at else None})
    return jsonify({'custom_lists': out})


@api_bp.route('/custom-lists/<int:list_id>')
def api_custom_list_detail(list_id):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    cl = db.session.get(CustomList, list_id)
    if not cl:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'custom_list': {'id': cl.id, 'name': cl.name, 'description': cl.description,
                                     'creator_user_id': cl.creator_user_id,
                                     'creator_username': getattr(getattr(cl, 'creator', None), 'username', None),
                                     'created_at': cl.created_at.isoformat() if cl.created_at else None}})


# ── Map packs ─────────────────────────────────────────────────────────────────

@api_bp.route('/map-packs')
def api_map_packs():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    packs = MapPack.query.order_by(MapPack.name.asc()).all()
    out = []
    for mp in packs:
        level_ids = []
        try:
            if mp.level_ids:
                level_ids = [int(x.strip()) for x in mp.level_ids.split(',') if x.strip()]
        except Exception:
            pass
        out.append({'id': mp.id, 'name': mp.name, 'description': mp.description,
                    'level_ids': level_ids,
                    'created_at': mp.created_at.isoformat() if mp.created_at else None})
    return jsonify({'map_packs': out})


# ── Shop ──────────────────────────────────────────────────────────────────────

@api_bp.route('/shop/items')
def api_shop_items():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    items = ShopItem.query.filter_by(is_available=True).order_by(ShopItem.price.asc()).all()
    out = []
    for i in items:
        out.append({'id': i.id, 'name': i.name, 'description': i.description,
                    'price': i.price, 'item_type': i.item_type,
                    'image_url': url_for('static', filename=f'shop/{i.image_filename}', _external=True) if i.image_filename else None,
                    'is_limited': bool(i.is_limited), 'is_expired': i.is_expired,
                    'created_at': i.created_at.isoformat() if i.created_at else None})
    return jsonify({'items': out})


@api_bp.route('/shop/market')
def api_shop_market():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    listings = MarketListing.query.filter_by(is_sold=False).order_by(MarketListing.price.asc()).all()
    out = []
    for m in listings:
        out.append({'id': m.id, 'price': m.price,
                    'item_name': getattr(getattr(m, 'item', None), 'name', None),
                    'seller_username': getattr(getattr(m, 'seller', None), 'username', None),
                    'created_at': m.created_at.isoformat() if m.created_at else None})
    return jsonify({'listings': out})


# ── News ──────────────────────────────────────────────────────────────────────

@api_bp.route('/news')
def api_news():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    try:
        limit_val = max(1, min(100, int(request.args.get('limit') or 20)))
    except Exception:
        limit_val = 20
    posts = NewsPost.query.order_by(NewsPost.created_at.desc()).limit(limit_val).all()
    out = []
    for p in posts:
        out.append({'id': p.id, 'title': p.title, 'body': p.body,
                    'author_username': getattr(getattr(p, 'author', None), 'username', None),
                    'created_at': p.created_at.isoformat() if p.created_at else None})
    return jsonify({'news': out})


# ── Pages ─────────────────────────────────────────────────────────────────────

@api_bp.route('/pages')
def api_pages():
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    pages = Page.query.filter_by(is_published=True).order_by(Page.title.asc()).all()
    return jsonify({'pages': [{'id': p.id, 'title': p.title, 'slug': p.slug,
                                'created_at': p.created_at.isoformat() if p.created_at else None,
                                'updated_at': p.updated_at.isoformat() if p.updated_at else None} for p in pages]})


@api_bp.route('/pages/<string:slug>')
def api_page_detail(slug):
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    p = Page.query.filter_by(slug=slug, is_published=True).first()
    if not p:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'page': {'id': p.id, 'title': p.title, 'slug': p.slug, 'body': p.body, 'is_html': bool(p.is_html),
                              'created_at': p.created_at.isoformat() if p.created_at else None,
                              'updated_at': p.updated_at.isoformat() if p.updated_at else None}})


# ── API token management ──────────────────────────────────────────────────────

@api_bp.route('/token', methods=['GET'])
def api_get_token():
    user, auth = _api_auth()
    if auth:
        return auth
    tok = _ensure_api_token_for(user)
    return jsonify({'api_token': tok, 'username': user.username})


@api_bp.route('/token/regenerate', methods=['POST'])
def api_regenerate_token():
    user, auth = _api_auth()
    if auth:
        return auth
    user.api_token = secrets.token_urlsafe(32)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'server_error'}), 500
    return jsonify({'api_token': user.api_token, 'username': user.username})


# ── Auth: change password ──────────────────────────────────────────────────────

@api_bp.route('/auth/change-password', methods=['POST'])
def api_change_password():
    user, auth = _api_auth()
    if auth:
        return auth
    from utils.security import rate_limited, trim_pw
    if rate_limited('change_password', limit=5, window_seconds=3600):
        return jsonify({'error': 'rate_limited', 'message': 'Too many password change attempts. Try again later.'}), 429
    from config import bcrypt
    data = request.get_json(silent=True) or {}
    current_pw = trim_pw(data.get('current_password') or '')
    new_pw = trim_pw(data.get('new_password') or '')
    new_pw2 = data.get('new_password2') or ''
    if not current_pw or not new_pw:
        return jsonify({'error': 'bad_request', 'message': 'current_password and new_password required'}), 400
    if new_pw != new_pw2:
        return jsonify({'error': 'bad_request', 'message': 'Passwords do not match'}), 400
    try:
        if not bcrypt.check_password_hash(user.password, current_pw):
            return jsonify({'error': 'invalid_credentials'}), 403
    except Exception:
        return jsonify({'error': 'invalid_credentials'}), 403
    user.password = bcrypt.generate_password_hash(new_pw).decode('utf-8')
    try:
        db.session.commit()
        add_site_log('password_change', f'{user.username} changed password via API.', actor_user_id=user.id)
        return jsonify({'success': True})
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'server_error'}), 500


@api_bp.route('/auth/set-password', methods=['POST'])
def api_set_password():
    user, auth = _api_auth()
    if auth:
        return auth
    from config import bcrypt
    from utils.security import trim_pw
    data = request.get_json(silent=True) or {}
    new_pw = trim_pw(data.get('new_password') or '')
    new_pw2 = data.get('new_password2') or ''
    current_pw = data.get('current_password') or ''
    if not current_pw or not bcrypt.check_password_hash(user.password, trim_pw(current_pw)):
        return jsonify({'error': 'forbidden', 'message': 'current_password required and must match'}), 403
    if not new_pw:
        return jsonify({'error': 'bad_request', 'message': 'new_password required'}), 400
    if new_pw != new_pw2:
        return jsonify({'error': 'bad_request', 'message': 'Passwords do not match'}), 400
    user.password = bcrypt.generate_password_hash(new_pw).decode('utf-8')
    try:
        db.session.commit()
        add_site_log('password_change', f'{user.username} set password via API.', actor_user_id=user.id)
        return jsonify({'success': True})
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'server_error'}), 500


# ── Profile update ─────────────────────────────────────────────────────────────

@api_bp.route('/profile/update', methods=['POST'])
def api_profile_update():
    user, auth = _api_auth()
    if auth:
        return auth
    data = request.get_json(silent=True) or {}
    bio = (data.get('bio') or '').strip()
    youtube = (data.get('youtube_link') or '').strip()
    discord_tag = (data.get('discord_tag') or '').strip()
    update_username = (data.get('username') or '').strip()
    if bio:
        user.bio = bio[:500]
    if youtube:
        from utils.security import validate_url_scheme
        _err = validate_url_scheme(youtube)
        if _err:
            return jsonify({'error': 'bad_request', 'message': _err}), 400
        user.youtube_link = youtube[:200]
    if discord_tag:
        user.discord_tag = discord_tag[:100]
    if update_username:
        from utils.security import rate_limited
        if rate_limited('username_change', limit=2, window_seconds=3600):
            return jsonify({'error': 'rate_limited', 'message': 'You can change your username at most twice per hour.'}), 429
        existing = User.query.filter(User.username == update_username, User.id != user.id).first()
        if existing:
            return jsonify({'error': 'username_taken'}), 409
        user.username = update_username
    try:
        db.session.commit()
        return jsonify({'success': True, 'username': user.username})
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'server_error'}), 500


# ── Songs (submissions) ────────────────────────────────────────────────────────

@api_bp.route('/songs')
def api_songs():
    _, auth = _api_auth()
    if auth:
        return auth
    limit = _limit_param(100, 500)
    status = (request.args.get('status') or '').strip().lower()
    q = SongSubmission.query
    if status in ('approved', 'pending', 'rejected'):
        q = q.filter(SongSubmission.status == status)
    rows = q.order_by(SongSubmission.submitted_at.desc()).limit(limit).all()
    out = []
    for s in rows:
        try:
            u = s.submitter
            submitter = {'id': u.id, 'username': u.username} if u else None
        except Exception:
            submitter = None
        out.append({'id': s.id, 'song_name': s.song_name, 'artist': s.artist, 'song_id': s.song_id,
                     'status': s.status, 'submitter': submitter,
                     'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None})
    return jsonify({'songs': out})


# ── Submit a record ────────────────────────────────────────────────────────────

@api_bp.route('/levels/<int:level_id>/submit-record', methods=['POST'])
def api_submit_record(level_id):
    user, auth = _api_auth()
    if auth:
        return auth
    lv = db.session.get(Level, level_id)
    if not lv:
        return jsonify({'error': 'not_found'}), 404
    data = request.get_json(silent=True) or {}
    video_url = (data.get('video_url') or '').strip()
    if not video_url:
        return jsonify({'error': 'bad_request', 'message': 'video_url required'}), 400
    from utils.security import validate_url_scheme
    _verr = validate_url_scheme(video_url)
    if _verr:
        return jsonify({'error': 'bad_request', 'message': _verr}), 400
    existing = Record.query.filter_by(user_id=user.id, level_id=lv.id).first()
    if existing:
        return jsonify({'error': 'already_submitted', 'record_id': existing.id}), 409
    record = Record(user_id=user.id, level_id=lv.id, video_url=video_url,
                     status='pending', submitted_at=datetime.utcnow())
    db.session.add(record)
    try:
        db.session.commit()
        add_site_log('record_submit', f'{user.username} submitted record for {lv.name} via API.', actor_user_id=user.id)
        return jsonify({'success': True, 'record_id': record.id}), 201
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'server_error'}), 500


# ── Site logs ──────────────────────────────────────────────────────────────────

@api_bp.route('/site-logs')
def api_site_logs():
    user, auth = _api_auth()
    if auth:
        return auth
    if not has_perm(user, 'manage_users'):
        return jsonify({'error': 'forbidden'}), 403
    limit = _limit_param(50, 500)
    kind = (request.args.get('kind') or '').strip()
    q = SiteLog.query
    if kind:
        q = q.filter(SiteLog.kind == kind)
    rows = q.order_by(SiteLog.created_at.desc()).limit(limit).all()
    out = []
    for log in rows:
        actor = None
        try:
            a = log.actor
            actor = {'id': a.id, 'username': a.username} if a else None
        except Exception:
            pass
        out.append({'id': log.id, 'kind': log.kind, 'body': log.body,
                     'actor': actor,
                     'created_at': log.created_at.isoformat() if log.created_at else None})
    return jsonify({'logs': out})


# ── Admin: update any user ─────────────────────────────────────────────────────

@api_bp.route('/admin/users/<int:user_id>/update', methods=['POST'])
def api_admin_update_user(user_id):
    user, auth = _api_auth()
    if auth:
        return auth
    if not has_perm(user, 'manage_users'):
        return jsonify({'error': 'forbidden'}), 403
    from config import bcrypt
    from utils.security import trim_pw
    target = db.session.get(User, user_id)
    if not target:
        return jsonify({'error': 'not_found'}), 404
    data = request.get_json(silent=True) or {}
    new_username = (data.get('username') or '').strip()
    new_password = trim_pw(data.get('new_password') or '')
    new_rank = (data.get('rank') or '').strip()
    new_points = data.get('points')
    new_bcoins = data.get('bcoins')
    new_discord_tag = (data.get('discord_tag') or '').strip()
    new_bio = (data.get('bio') or '').strip()
    new_youtube = (data.get('youtube_link') or '').strip()
    new_disabled = data.get('is_disabled')
    new_donor = data.get('is_donor')

    if new_username:
        existing = User.query.filter(User.username == new_username, User.id != target.id).first()
        if existing:
            return jsonify({'error': 'username_taken'}), 409
        target.username = new_username
    if new_password:
        target.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
    if new_rank in ('member', 'mod', 'admin'):
        from auxiliary.settings_helpers import get_user_rank as _gur
        if new_rank == 'admin' and _gur(user) != 'admin':
            return jsonify({'error': 'forbidden', 'message': 'only admins can grant the admin rank'}), 403
        target.rank = new_rank
    if new_points is not None:
        try:
            target.points = max(0.0, float(new_points))
        except (ValueError, TypeError):
            return jsonify({'error': 'bad_request', 'message': 'invalid points'}), 400
    if new_bcoins is not None:
        try:
            target.bcoins = max(0, int(new_bcoins))
        except (ValueError, TypeError):
            return jsonify({'error': 'bad_request', 'message': 'invalid bcoins'}), 400
    if new_discord_tag:
        target.discord_tag = new_discord_tag
    if new_bio:
        target.bio = new_bio
    if new_youtube:
        target.youtube_link = new_youtube
    if isinstance(new_disabled, bool):
        target.is_disabled = new_disabled
    if isinstance(new_donor, bool):
        target.is_donor = new_donor
    try:
        db.session.commit()
        add_site_log('admin_action', f'{user.username} updated user {target.username} via API.', actor_user_id=user.id)
        return jsonify({'success': True})
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'server_error'}), 500


# ── API docs page ─────────────────────────────────────────────────────────────

@api_bp.route('/docs')
@login_required
def api_docs_page():
    return render_template('api_docs.html')


# ── Roulette single-player ────────────────────────────────────────────────────
# These are thin wrappers – actual pool logic lives in blueprints/roulette.py

from blueprints.roulette import (
    api_roulette_pool as _pool,
    api_roulette_spin as _spin,
    api_roulette_skip as _skip,
    api_roulette_reset as _reset,
)

api_bp.add_url_rule('/roulette/pool', view_func=_pool, methods=['GET'])
api_bp.add_url_rule('/roulette/spin', view_func=_spin, methods=['POST'])
api_bp.add_url_rule('/roulette/skip', view_func=_skip, methods=['POST'])
api_bp.add_url_rule('/roulette/reset', view_func=_reset, methods=['POST'])

# Room endpoints are registered directly from blueprints/roulette.py
