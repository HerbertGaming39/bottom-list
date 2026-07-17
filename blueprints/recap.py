"""
blueprints/recap.py – Year-in-review recap page for the logged-in user.
"""
from datetime import datetime

from flask import Blueprint, render_template, abort
from flask_login import login_required, current_user

from config import db
from models import Level, Record, LevelSubmission, MapPack, User, Clan
from auxiliary.settings_helpers import get_setting

recap_bp = Blueprint('recap', __name__)


def _year_recap_enabled() -> bool:
    raw = str(get_setting('year_recap_enabled') or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _recap_for_year(y: int) -> dict:
    start = datetime(y, 1, 1)
    end = datetime(y + 1, 1, 1)
    month_labels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    groups = [
        {'key': 'main_verified',         'title': 'Main List',              'list_type': 'main',        'is_verified': True},
        {'key': 'challenge_verified',     'title': 'Challenge List',         'list_type': 'challenge',   'is_verified': True},
        {'key': 'platformer_verified',    'title': 'Platformer List',        'list_type': 'platformer',  'is_verified': True},
        {'key': 'main_unverified',        'title': 'Unverified (Main)',      'list_type': 'main',        'is_verified': False},
        {'key': 'challenge_unverified',   'title': 'Unverified (Challenge)', 'list_type': 'challenge',   'is_verified': False},
        {'key': 'platformer_unverified',  'title': 'Unverified (Platformer)','list_type': 'platformer',  'is_verified': False},
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
                ).scalar() or 0
            )
        except Exception:
            beaten_count = 0

        percent = round((beaten_count / total_levels * 100) if total_levels > 0 else 0, 1)
        out.append({
            'key':     g['key'],
            'title':   g['title'],
            'beaten':  beaten_count,
            'total':   int(total_levels or 0),
            'percent': percent,
        })

    # ── defaults ───────────────────────────────────────────────────────────
    total_beaten = records_total = records_approved = 0
    submissions_total = submissions_approved = 0
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
    first_record = last_record = None
    first_mappack = None
    pack_stats = {'beaten': 0, 'total': 0, 'percent': 0.0}
    global_rank = {}
    clan_stats = None
    closest_rival = None

    # ── pack stats ─────────────────────────────────────────────────────────
    try:
        all_packs = MapPack.query.all()
        pack_stats['total'] = len(all_packs)
        earliest_rows = (
            db.session.query(Record.level_id, db.func.min(Record.submitted_at))
            .filter(Record.user_id == current_user.id, Record.status == 'approved')
            .group_by(Record.level_id).all()
        )
        earliest_by_level = {int(lid): dt for lid, dt in (earliest_rows or []) if lid and dt}
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
        pack_stats['percent'] = round(
            (pack_stats['beaten'] / pack_stats['total'] * 100) if pack_stats['total'] > 0 else 0, 1)
        if beaten_packs_this_year:
            beaten_packs_this_year.sort(key=lambda x: x['date'])
            first_mappack = {
                'name': beaten_packs_this_year[0]['pack'].name,
                'date': beaten_packs_this_year[0]['date'].strftime('%Y-%m-%d'),
            }
    except Exception:
        pass

    # ── record / submission counts ─────────────────────────────────────────
    try:
        records_total = int(db.session.query(db.func.count(Record.id))
            .filter(Record.user_id == current_user.id,
                    Record.submitted_at >= start, Record.submitted_at < end).scalar() or 0)
    except Exception:
        pass
    try:
        records_approved = int(db.session.query(db.func.count(Record.id))
            .filter(Record.user_id == current_user.id, Record.status == 'approved',
                    Record.submitted_at >= start, Record.submitted_at < end).scalar() or 0)
    except Exception:
        pass
    try:
        rows = (db.session.query(Record.status, db.func.count(Record.id))
            .filter(Record.user_id == current_user.id,
                    Record.submitted_at >= start, Record.submitted_at < end)
            .group_by(Record.status).order_by(db.func.count(Record.id).desc()).all())
        record_status_counts = [{'status': s or 'unknown', 'count': int(c or 0)} for s, c in rows]
    except Exception:
        pass
    try:
        submissions_total = int(db.session.query(db.func.count(LevelSubmission.id))
            .filter(LevelSubmission.user_id == current_user.id,
                    LevelSubmission.submitted_at >= start,
                    LevelSubmission.submitted_at < end).scalar() or 0)
    except Exception:
        pass
    try:
        submissions_approved = int(db.session.query(db.func.count(LevelSubmission.id))
            .filter(LevelSubmission.user_id == current_user.id,
                    LevelSubmission.status == 'approved',
                    LevelSubmission.submitted_at >= start,
                    LevelSubmission.submitted_at < end).scalar() or 0)
    except Exception:
        pass
    try:
        rows2 = (db.session.query(LevelSubmission.status, db.func.count(LevelSubmission.id))
            .filter(LevelSubmission.user_id == current_user.id,
                    LevelSubmission.submitted_at >= start,
                    LevelSubmission.submitted_at < end)
            .group_by(LevelSubmission.status).all())
        submission_status_counts = [{'status': s or 'unknown', 'count': int(c or 0)} for s, c in rows2]
    except Exception:
        pass

    # ── unique levels beaten + monthly breakdown ───────────────────────────
    try:
        total_beaten = int(db.session.query(db.func.count(db.func.distinct(Record.level_id)))
            .filter(Record.user_id == current_user.id, Record.status == 'approved',
                    Record.submitted_at >= start, Record.submitted_at < end).scalar() or 0)
    except Exception:
        pass

    seen_levels = set()
    dts_list = []
    try:
        rows3 = (db.session.query(Record.submitted_at, Record.level_id, Level.points_reward)
            .select_from(Record).join(Level, Level.id == Record.level_id)
            .filter(Record.user_id == current_user.id, Record.status == 'approved',
                    Record.submitted_at >= start, Record.submitted_at < end).all())
    except Exception:
        rows3 = []

    month_beaten = {m: 0 for m in range(1, 13)}
    month_points = {m: 0.0 for m in range(1, 13)}
    for dt, lvl_id, pts in rows3:
        try:
            mid = int(getattr(dt, 'month', 0) or 0)
        except Exception:
            mid = 0
        if not 1 <= mid <= 12:
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
            pass
        if dt:
            dts_list.append(dt)

    points_gained = round(sum(month_points.values()), 2)
    monthly['labels'] = month_labels
    monthly['beaten'] = [int(month_beaten[m]) for m in range(1, 13)]
    monthly['points'] = [round(float(month_points[m]), 2) for m in range(1, 13)]
    monthly_rows = [
        {'month': month_labels[m - 1], 'beaten': int(month_beaten[m]),
         'points': round(float(month_points[m]), 2)}
        for m in range(1, 13)
    ]
    try:
        best = max(range(1, 13), key=lambda m: (month_points[m], month_beaten[m]))
        best_month = {'month': month_labels[best - 1], 'beaten': int(month_beaten[best]),
                      'points': round(float(month_points[best]), 2)}
    except Exception:
        pass

    # ── device breakdown ───────────────────────────────────────────────────
    try:
        dev_rows = (db.session.query(Record.device, db.func.count(Record.id))
            .filter(Record.user_id == current_user.id, Record.status == 'approved',
                    Record.submitted_at >= start, Record.submitted_at < end)
            .group_by(Record.device).order_by(db.func.count(Record.id).desc()).all())
        device_breakdown = [{'device': d or 'unknown', 'count': int(c or 0)} for d, c in dev_rows]
    except Exception:
        pass

    # ── top levels / creators / tags ───────────────────────────────────────
    beaten_levels = []
    if seen_levels:
        try:
            beaten_levels = Level.query.filter(Level.id.in_(list(seen_levels))).all()
            top_beaten_levels = (Level.query.filter(Level.id.in_(list(seen_levels)))
                .order_by(Level.points_reward.desc()).limit(8).all())
        except Exception:
            pass

    try:
        by_lt: dict = {}
        for lv in beaten_levels:
            lt = (getattr(lv, 'list_type', '') or 'main').strip().lower() or 'main'
            if lt not in ('main', 'challenge', 'platformer'):
                lt = 'main'
            entry = by_lt.setdefault(lt, {'list_type': lt, 'beaten': 0, 'points': 0.0})
            entry['beaten'] += 1
            try:
                entry['points'] += float(getattr(lv, 'points_reward', 0.0) or 0.0)
            except Exception:
                pass
        list_breakdown = sorted(
            [{'list_type': k, 'beaten': int(v['beaten']), 'points': round(float(v['points']), 2)}
             for k, v in by_lt.items()],
            key=lambda r: (float(r.get('points') or 0), int(r.get('beaten') or 0)), reverse=True)
    except Exception:
        pass

    try:
        buckets = [(1, 10, 'Top 10'), (11, 25, '11–25'), (26, 50, '26–50'),
                   (51, 100, '51–100'), (101, 200, '101–200'), (201, 999999, '201+')]
        counts = {label: 0 for _, _, label in buckets}
        for lv in beaten_levels:
            pos = int(getattr(lv, 'position', 0) or 0)
            if pos <= 0:
                continue
            for a, b, label in buckets:
                if a <= pos <= b:
                    counts[label] += 1
                    break
        position_buckets = [{'label': label, 'count': int(counts[label])} for _, _, label in buckets]
    except Exception:
        pass

    try:
        creator_counts: dict = {}
        for lv in beaten_levels:
            c = (getattr(lv, 'creator', '') or '').strip() or 'Unknown'
            creator_counts[c] = creator_counts.get(c, 0) + 1
        top_creators = [{'creator': k, 'count': int(v)}
                        for k, v in sorted(creator_counts.items(),
                                           key=lambda kv: (-kv[1], kv[0].lower()))][:12]
    except Exception:
        pass

    try:
        tag_counts: dict = {}
        for lv in beaten_levels:
            for p in (str(getattr(lv, 'categories', '') or '')).split(','):
                p = p.strip()
                if p:
                    tag_counts[p] = tag_counts.get(p, 0) + 1
        top_tags = [{'tag': k, 'count': int(v)}
                    for k, v in sorted(tag_counts.items(),
                                       key=lambda kv: (-kv[1], kv[0].lower()))][:20]
    except Exception:
        pass

    # ── streaks / time breakdown ───────────────────────────────────────────
    try:
        days = sorted({dt.date() for dt in dts_list if dt})
        active_days = len(days)
        longest = current_streak = 0
        prev = None
        for d in days:
            if prev and (d - prev).days == 1:
                current_streak += 1
            else:
                current_streak = 1
            longest = max(longest, current_streak)
            prev = d
        streaks = {'active_days': int(active_days), 'longest_streak_days': int(longest)}
    except Exception:
        pass

    try:
        dow_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        by_dow = {k: 0 for k in dow_labels}
        by_hour = {h: 0 for h in range(24)}
        for dt in dts_list:
            try:
                by_dow[dow_labels[int(dt.weekday())]] += 1
            except Exception:
                pass
            try:
                by_hour[int(dt.hour)] += 1
            except Exception:
                pass
        time_breakdown = {
            'by_dow':  [{'label': k, 'count': int(by_dow[k])} for k in dow_labels],
            'by_hour': [{'label': f'{h:02d}:00', 'count': int(by_hour[h])} for h in range(24)],
        }
    except Exception:
        pass

    # ── rating averages ────────────────────────────────────────────────────
    try:
        av = (db.session.query(
                db.func.avg(Record.rating_deco),
                db.func.avg(Record.rating_gameplay),
                db.func.avg(Record.rating_difficulty))
            .filter(Record.user_id == current_user.id, Record.status == 'approved',
                    Record.submitted_at >= start, Record.submitted_at < end).first())
        if av:
            rating_avgs = {
                'deco':       round(float(av[0]), 2) if av[0] is not None else None,
                'gameplay':   round(float(av[1]), 2) if av[1] is not None else None,
                'difficulty': round(float(av[2]), 2) if av[2] is not None else None,
            }
    except Exception:
        pass

    # ── first / last record ────────────────────────────────────────────────
    for order, attr in [(Record.submitted_at.asc(), 'first_record'),
                        (Record.submitted_at.desc(), 'last_record')]:
        try:
            row = (db.session.query(Record, Level).join(Level, Level.id == Record.level_id)
                .filter(Record.user_id == current_user.id, Record.status == 'approved',
                        Record.submitted_at >= start, Record.submitted_at < end)
                .order_by(order).first())
            if row and row[0] and row[1]:
                val = {
                    'date':       row[0].submitted_at,
                    'level_id':   int(row[1].id),
                    'level_name': row[1].name,
                    'creator':    row[1].creator,
                    'points':     float(row[1].points_reward or 0.0),
                    'position':   int(row[1].position or 0),
                    'list_type':  row[1].list_type or 'main',
                }
                if attr == 'first_record':
                    first_record = val
                else:
                    last_record = val
        except Exception:
            pass

    # ── global rank ────────────────────────────────────────────────────────
    try:
        subq = (db.session.query(
                    Record.user_id.label('uid'),
                    db.func.coalesce(db.func.sum(Level.points_reward), 0.0).label('pts'))
            .select_from(Record).join(Level, Level.id == Record.level_id)
            .filter(Record.status == 'approved',
                    Record.submitted_at >= start, Record.submitted_at < end)
            .group_by(Record.user_id).subquery())
        my_pts = float(db.session.query(db.func.coalesce(subq.c.pts, 0.0))
            .filter(subq.c.uid == current_user.id).scalar() or 0.0)
        higher = int(db.session.query(db.func.count()).select_from(subq)
            .filter(subq.c.pts > my_pts).scalar() or 0)
        total = int(db.session.query(db.func.count()).select_from(subq).scalar() or 0)
        global_rank = {'points_rank': higher + 1, 'points_total': total,
                       'points_gained': round(my_pts, 2)}
        if total > 1:
            rows_r = (db.session.query(subq.c.uid, subq.c.pts)
                .order_by(subq.c.pts.asc()).all())
            rival_uid = rival_pts = rival_diff = None
            for uid, pts in rows_r:
                if int(uid or 0) == int(current_user.id):
                    continue
                pts_val = float(pts or 0.0)
                diff = abs(pts_val - my_pts)
                if rival_diff is None or diff < rival_diff:
                    rival_uid, rival_pts, rival_diff = int(uid), pts_val, diff
            if rival_uid and rival_pts is not None:
                rival_user = db.session.get(User, rival_uid)
                if rival_user:
                    closest_rival = {
                        'username':     rival_user.username,
                        'user_id':      int(rival_user.id),
                        'points_gained': round(float(rival_pts), 2),
                        'diff_points':  round(float(rival_diff or 0.0), 2),
                        'direction':    'ahead' if rival_pts >= my_pts else 'behind',
                    }
    except Exception:
        pass

    # ── clan stats ─────────────────────────────────────────────────────────
    try:
        if getattr(current_user, 'clan_id', None):
            clan_id = int(current_user.clan_id)
            clan = db.session.get(Clan, clan_id)
            if clan:
                member_count = int(User.query.filter(User.clan_id == clan_id).count() or 0)
                total_points = float(
                    db.session.query(db.func.coalesce(db.func.sum(User.points), 0.0))
                    .filter(User.clan_id == clan_id).scalar() or 0.0)
                clan_subq = (db.session.query(
                        Record.user_id.label('uid'),
                        db.func.coalesce(db.func.sum(Level.points_reward), 0.0).label('pts'))
                    .select_from(Record).join(Level, Level.id == Record.level_id)
                    .filter(Record.status == 'approved',
                            Record.submitted_at >= start, Record.submitted_at < end)
                    .group_by(Record.user_id).subquery())
                clan_year_points = float(
                    db.session.query(db.func.coalesce(db.func.sum(clan_subq.c.pts), 0.0))
                    .join(User, User.id == clan_subq.c.uid)
                    .filter(User.clan_id == clan_id).scalar() or 0.0)
                clan_stats = {
                    'name':         clan.name,
                    'member_count': member_count,
                    'total_points': round(total_points, 2),
                    'year_points':  round(clan_year_points, 2),
                }
    except Exception:
        pass

    # ── quotes / highlights ────────────────────────────────────────────────
    try:
        main_v  = next((g for g in out if g.get('key') == 'main_verified'), {})
        chall_v = next((g for g in out if g.get('key') == 'challenge_verified'), {})
        plat_v  = next((g for g in out if g.get('key') == 'platformer_verified'), {})
        best_device = str(device_breakdown[0].get('device') or '') if device_breakdown else ''
        if total_beaten > 0:
            quotes.append(f'You beat {total_beaten} unique levels in {y}.')
        if points_gained > 0:
            quotes.append(f"You gained {points_gained} points this year \u2014 that's a lot of grind.")
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
            quotes.append(f'You hit a {int(streaks["longest_streak_days"])}-day approval streak. Insane.')
    except Exception:
        pass

    highlights = [
        {'label': 'Unique levels beaten',     'value': int(total_beaten)},
        {'label': 'Points gained',            'value': float(points_gained)},
        {'label': 'Approved records',         'value': int(records_approved)},
        {'label': 'Total records submitted',  'value': int(records_total)},
        {'label': 'Approved submissions',     'value': int(submissions_approved)},
        {'label': 'Total submissions',        'value': int(submissions_total)},
    ]

    return {
        'year':                    y,
        'groups':                  out,
        'total_beaten':            total_beaten,
        'records_total':           records_total,
        'records_approved':        records_approved,
        'submissions_total':       submissions_total,
        'submissions_approved':    submissions_approved,
        'points_gained':           points_gained,
        'monthly':                 monthly,
        'monthly_rows':            monthly_rows,
        'best_month':              best_month,
        'device_breakdown':        device_breakdown,
        'top_beaten_levels':       top_beaten_levels,
        'highlights':              highlights,
        'quotes':                  quotes,
        'record_status_counts':    record_status_counts,
        'submission_status_counts': submission_status_counts,
        'list_breakdown':          list_breakdown,
        'position_buckets':        position_buckets,
        'top_creators':            top_creators,
        'top_tags':                top_tags,
        'streaks':                 streaks,
        'time_breakdown':          time_breakdown,
        'rating_avgs':             rating_avgs,
        'first_record':            first_record,
        'first_mappack':           first_mappack,
        'pack_stats':              pack_stats,
        'last_record':             last_record,
        'global_rank':             global_rank,
        'clan_stats':              clan_stats,
        'closest_rival':           closest_rival,
    }


@recap_bp.route('/recap')
@login_required
def recap():
    if not _year_recap_enabled():
        abort(404)
    year = int(datetime.utcnow().year)
    this_year = _recap_for_year(year)
    last_year = _recap_for_year(year - 1)
    return render_template('recap.html', this_year=this_year, last_year=last_year)
