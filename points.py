"""
points.py – User points recalculation helpers.
"""
from config import db


def recalc_all_user_points():
    """Recompute points for every user from scratch."""
    from models import User, Record, Level
    rows = (
        db.session.query(Record.user_id, db.func.coalesce(db.func.sum(Level.points_reward), 0.0))
        .join(Level, Level.id == Record.level_id)
        .filter(Record.status == 'approved')
        .group_by(Record.user_id)
        .all()
    )
    by_user = {int(uid): float(pts or 0.0) for uid, pts in (rows or []) if uid is not None}
    for u in User.query.all():
        u.points = max(0.0, float(by_user.get(int(u.id), 0.0)))
    db.session.commit()


def recalc_user_points_for_users(user_ids):
    """Recompute points only for the given user IDs."""
    from models import User, Record, Level
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
    for u in User.query.filter(User.id.in_(cleaned)).all():
        u.points = max(0.0, float(by_user.get(int(u.id), 0.0)))
    db.session.commit()


def sum_approved_points_for_user(user_id: int) -> float:
    from models import Record, Level
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


def sum_approved_points_by_list_for_user(user_id: int) -> dict:
    from models import Record, Level
    try:
        uid = int(user_id)
    except Exception:
        return {'main': 0.0, 'challenge': 0.0, 'platformer': 0.0}
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
    out = {'main': 0.0, 'challenge': 0.0, 'platformer': 0.0}
    for lt, pts in (rows or []):
        key = (lt or 'main').strip().lower() or 'main'
        if key in out:
            try:
                out[key] = float(pts or 0.0)
            except Exception:
                out[key] = 0.0
    return out


def auto_points_recalc_all():
    """Recalculate level points_reward for all lists according to configured rules."""
    from models import Level
    from settings_helpers import (
        auto_points_scope, get_auto_points_rules_text_for,
        parse_auto_points_rules, points_for_position
    )
    scope = auto_points_scope()
    q = Level.query.filter(Level.position.isnot(None))
    if scope in ('main', 'challenge'):
        q = q.filter(Level.list_type == scope)
    levels = q.order_by(Level.list_type, Level.is_verified.desc(), Level.position).all()
    cache = {}
    for lv in levels:
        key = ((lv.list_type or 'main'), bool(lv.is_verified))
        rules = cache.get(key)
        if rules is None:
            rules = parse_auto_points_rules(get_auto_points_rules_text_for(key[0], key[1]))
            cache[key] = rules
        lv.points_reward = points_for_position(int(lv.position or 0), rules)
    db.session.commit()
    try:
        recalc_all_user_points()
    except Exception:
        pass
