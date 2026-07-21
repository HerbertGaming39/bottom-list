"""
blueprints/leaderboard.py – Leaderboard logic and API endpoints.
"""
from flask import Blueprint, request, jsonify

from config import db
from models import User, Clan, Level, Record, UserBlock

from utils.cache import cache_get, cache_set

leaderboard_bp = Blueprint('leaderboard', __name__)


def leaderboard_rows(view: str, list_type: str, sort_by: str, filter_device: str, filter_status: str):
    view = (view or 'users').strip().lower()
    if view not in ('users', 'clans'):
        view = 'users'
    lt = (list_type or 'main').strip().lower()
    if lt not in ('main', 'challenge', 'platformer', 'all'):
        lt = 'main'
    cache_key = f'lb:{view}:{lt}:{sort_by or ""}:{filter_device or ""}:{filter_status or ""}'
    cached = cache_get(cache_key)
    if cached is not None:
        return cached[0], cached[1], cached[2], cached[3]

    record_join = db.and_(Record.user_id == User.id, Record.status == 'approved')
    if filter_device:
        record_join = db.and_(record_join, Record.device == filter_device)

    if lt in ('main', 'challenge', 'platformer'):
        from sqlalchemy import case
        level_join = db.and_(Level.id == Record.level_id, Level.list_type == lt)
        records_count_expr = db.func.count(Level.id)
        points_expr = db.func.coalesce(db.func.sum(Level.points_reward), 0.0)
    else:
        level_join = Level.id == Record.level_id
        records_count_expr = db.func.count(Record.id)
        points_expr = db.func.coalesce(db.func.sum(Level.points_reward), 0.0)

    users = []
    clans = []

    if view == 'clans':
        query = (
            db.session.query(Clan, points_expr.label('lb_points'), records_count_expr.label('lb_records'))
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
        else:
            query = query.order_by(points_expr.desc(), records_count_expr.desc())
        for c, pts, recs in query.all():
            c.lb_points = float(pts or 0.0)
            c.lb_records = int(recs or 0)
            clans.append(c)
    else:
        query = (
            db.session.query(User, points_expr.label('lb_points'), records_count_expr.label('lb_records'))
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
            recent_expr = db.func.max(Record.submitted_at)
            query = query.order_by(recent_expr.desc().nullslast(), points_expr.desc())
        else:
            query = query.order_by(points_expr.desc(), records_count_expr.desc())
        for u, pts, recs in query.all():
            u.lb_points = float(pts or 0.0)
            u.lb_records = int(recs or 0)
            if u.lb_points <= 0 and u.lb_records <= 0:
                continue
            users.append(u)

    out = (users, clans, view, lt)
    cache_set(cache_key, out, 20)
    return out


def filter_blocked_from_leaderboard(users, current_user_id):
    """Remove users blocked with hide_leaderboard from a leaderboard user list."""
    if not current_user_id:
        return users
    import json
    blocks = UserBlock.query.filter_by(blocker_id=current_user_id).all()
    hide_ids = set()
    for b in blocks:
        try:
            opts = json.loads(b.block_options or '{}')
        except Exception:
            opts = {}
        if opts.get('hide_leaderboard', True):
            hide_ids.add(b.blocked_id)
    if not hide_ids:
        return users
    return [u for u in users if u.id not in hide_ids]


@leaderboard_bp.route('/leaderboard/user/<int:user_id>/details')
def leaderboard_user_details(user_id):
    list_type = (request.args.get('list') or 'main').strip().lower()
    if list_type not in ('main', 'challenge', 'platformer', 'all'):
        list_type = 'main'
    limit = min(max(5, int(request.args.get('limit') or 40)), 200)

    rec_q = (
        db.session.query(Record, Level)
        .filter(Record.user_id == user_id, Record.status == 'approved')
    )
    if list_type in ('main', 'challenge', 'platformer'):
        rec_q = rec_q.join(Level, db.and_(Level.id == Record.level_id, Level.list_type == list_type))
    else:
        rec_q = rec_q.outerjoin(Level, Level.id == Record.level_id)
    recs = [
        {'id': r.id, 'level_id': r.level_id, 'level_name': lv.name if lv else 'Deleted level', 'creator': lv.creator if lv else 'Unknown',
         'submitted_at': r.submitted_at.isoformat() if r.submitted_at else None,
         'video_url': r.video_url or '', 'device': r.device or ''}
        for r, lv in rec_q.order_by(Record.submitted_at.desc()).limit(limit).all()
    ]

    from models import LevelSubmission
    subs = [
        {'id': s.id, 'name': s.name, 'creator': s.creator, 'status': s.status,
         'submitted_at': s.submitted_at.isoformat() if s.submitted_at else None, 'video_url': s.video_url or ''}
        for s in LevelSubmission.query.filter(LevelSubmission.user_id == user_id).order_by(LevelSubmission.submitted_at.desc()).limit(limit).all()
    ]

    return jsonify({'records': recs, 'submissions': subs, 'list': list_type})


@leaderboard_bp.route('/api/leaderboard/users')
def api_leaderboard_users():
    from blueprints.api import _api_auth
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    sort_by = request.args.get('sort', 'points')
    filter_device = request.args.get('device', '')
    filter_status = request.args.get('status', '')
    list_type = (request.args.get('list') or 'main').strip().lower()
    users, _clans, _view, list_type = leaderboard_rows('users', list_type, sort_by, filter_device, filter_status)
    from flask_login import current_user
    if getattr(current_user, 'is_authenticated', False):
        users = filter_blocked_from_leaderboard(users, current_user.id)
    data = []
    for u in users:
        clan_id = getattr(u, 'clan_id', None)
        clan_name = None
        try:
            if clan_id:
                rel = getattr(u, 'clan', None)
                clan_name = getattr(rel, 'name', None) if rel else None
        except Exception:
            pass
        data.append({'id': u.id, 'username': u.username, 'points': float(getattr(u, 'lb_points', 0.0) or 0.0),
                     'records': int(getattr(u, 'lb_records', 0) or 0), 'clan_id': clan_id, 'clan_name': clan_name, 'list': list_type})
    return jsonify({'users': data})


@leaderboard_bp.route('/api/leaderboard/clans')
def api_leaderboard_clans():
    from blueprints.api import _api_auth
    _, auth_err = _api_auth()
    if auth_err:
        return auth_err
    sort_by = request.args.get('sort', 'points')
    filter_device = request.args.get('device', '')
    filter_status = request.args.get('status', '')
    list_type = (request.args.get('list') or 'main').strip().lower()
    _users, clans, _view, list_type = leaderboard_rows('clans', list_type, sort_by, filter_device, filter_status)
    data = []
    for c in clans:
        try:
            member_count = len(list(c.members or []))
        except Exception:
            member_count = 0
        data.append({'id': c.id, 'name': c.name, 'points': float(getattr(c, 'lb_points', 0.0) or 0.0),
                     'records': int(getattr(c, 'lb_records', 0) or 0), 'member_count': member_count, 'list': list_type})
    return jsonify({'clans': data})
