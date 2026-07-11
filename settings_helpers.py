"""
settings_helpers.py – Database-backed site settings, rank/permission helpers,
auto-points logic, guidelines helpers.
"""
import json
from typing import Optional

# Avoid circular imports: db is imported lazily inside functions where needed.

RANKS = ['member', 'active_member', 'mod', 'elder_moderator', 'admin']

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

DEFAULT_AUTO_POINTS_RULES = """1=500\n2-5=250\n6-10=100\n11-20=50\n"""


# ── Settings CRUD ─────────────────────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    from config import db
    from models import Setting
    try:
        row = db.session.get(Setting, key)
        return row.value if row else None
    except Exception as e:
        print(f'get_setting error for {key}: {e}')
        return None


def set_setting(key: str, value: str):
    from config import db
    from models import Setting
    row = db.session.get(Setting, key)
    if not row:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


def setting_truthy(key: str) -> bool:
    return str(get_setting(key) or '').strip().lower() in ('1', 'true', 'yes', 'on')


# ── Rank / permission helpers ─────────────────────────────────────────────────

def rank_label(rank: str) -> str:
    r = (rank or '').strip().lower()
    labels = {
        'admin': 'Admin',
        'elder_moderator': 'Elder Moderator',
        'mod': 'Mod',
        'active_member': 'Active Member',
    }
    return labels.get(r, 'Member')


def get_user_rank(user) -> str:
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


def default_rank_permissions() -> dict:
    return {
        'member': [],
        'active_member': [],
        'mod': ['access_admin', 'manage_levels', 'moderate_records', 'moderate_submissions'],
        'elder_moderator': [
            'access_admin', 'manage_levels', 'move_levels',
            'moderate_records', 'moderate_submissions', 'manage_pages', 'manage_events'
        ],
        'admin': [p[0] for p in PERMISSIONS],
    }


def get_rank_permissions() -> dict:
    defaults = default_rank_permissions()
    raw = (get_setting('rank_permissions') or '').strip()
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
        perms = [str(item or '').strip() for item in (v if isinstance(v, list) else []) if str(item or '').strip()]
        out[rk] = perms
    out['admin'] = [p[0] for p in PERMISSIONS]
    return out


def has_perm(user, perm: str) -> bool:
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    r = get_user_rank(user)
    if r == 'admin':
        return True
    return perm in set(get_rank_permissions().get(r) or [])


def require_perm(perm: str):
    from flask import abort
    from flask_login import current_user
    if not has_perm(current_user, perm):
        abort(403)


# ── Guidelines helpers ────────────────────────────────────────────────────────

GUIDELINES_URL_DEFAULT = '/guidelines-site'


def get_guidelines_url() -> str:
    return (get_setting('guidelines_url') or GUIDELINES_URL_DEFAULT or '').strip()


def get_guidelines_reset_ts() -> int:
    from datetime import datetime
    raw = str(get_setting('guidelines_reset_ts') or '').strip()
    try:
        return int(float(raw))
    except Exception:
        return 0


def needs_guidelines_acceptance(user) -> bool:
    from datetime import datetime
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
        reset_dt = datetime.utcfromtimestamp(reset_ts)
    except Exception:
        return False
    return accepted_at < reset_dt


# ── Auto-points helpers ───────────────────────────────────────────────────────

def auto_points_enabled() -> bool:
    return setting_truthy('auto_points_enabled')


def auto_points_scope() -> str:
    raw = str(get_setting('auto_points_scope') or '').strip().lower()
    if raw in ('main', 'challenge', 'platformer', 'both', 'all'):
        return raw
    return 'both'


def auto_points_applies_to(list_type: str) -> bool:
    if not auto_points_enabled():
        return False
    scope = auto_points_scope()
    lt = (list_type or '').strip().lower() or 'main'
    if lt not in ('main', 'challenge', 'platformer'):
        lt = 'main'
    if scope == 'all':
        return True
    if scope == 'both':
        return lt in ('main', 'challenge')
    return scope == lt


def auto_points_rules_key(list_type: str, is_verified: bool) -> str:
    lt = (list_type or '').strip().lower() or 'main'
    if lt not in ('main', 'challenge', 'platformer'):
        lt = 'main'
    return f'auto_points_rules_{lt}_{"verified" if bool(is_verified) else "unverified"}'


def get_auto_points_rules_text_for(list_type: str, is_verified: bool) -> str:
    key = auto_points_rules_key(list_type, is_verified)
    raw = (get_setting(key) or '').strip()
    if raw:
        return raw
    lt = (list_type or '').strip().lower() or 'main'
    if bool(is_verified) and lt == 'main':
        legacy = (get_setting('auto_points_rules') or '').strip()
        if legacy:
            return legacy
    return DEFAULT_AUTO_POINTS_RULES


def parse_auto_points_rules(text: str):
    rules = []
    for line in (text or '').splitlines():
        s = (line or '').strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        left, right = s.split('=', 1)
        left = left.strip()
        right = right.strip()
        if not left or not right:
            continue
        try:
            pts = float(right)
        except Exception:
            continue
        if '-' in left:
            a, b = left.split('-', 1)
            try:
                start, end = int(a.strip()), int(b.strip())
            except Exception:
                continue
        else:
            try:
                start = end = int(left)
            except Exception:
                continue
        if start < 1:
            start = 1
        if end < start:
            end = start
        rules.append((start, end, pts))
    rules.sort(key=lambda x: (x[0], x[1]))
    return rules


def points_for_position(pos: int, rules) -> float:
    if not pos:
        return 0.0
    for start, end, pts in (rules or []):
        if start <= pos <= end:
            return float(pts or 0.0)
    return 0.0


def record_proof_top_n() -> int:
    raw = str(get_setting('record_proof_top_n') or '').strip()
    try:
        n = int(float(raw))
    except Exception:
        n = 30
    return max(0, min(10000, n))


def record_proof_top_n_for_list(list_type: str) -> int:
    lt = (list_type or '').strip().lower() or 'main'
    if lt not in ('main', 'challenge', 'platformer'):
        lt = 'main'
    raw = str(get_setting(f'record_proof_top_n_{lt}') or '').strip()
    if raw:
        try:
            n = int(float(raw))
        except Exception:
            n = 30
        return max(0, min(10000, n))
    if lt == 'platformer':
        return 10000
    return record_proof_top_n()


def year_recap_enabled() -> bool:
    return setting_truthy('year_recap_enabled')
