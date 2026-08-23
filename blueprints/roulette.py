"""
blueprints/roulette.py – Roulette single-player and multiplayer room API endpoints.
"""
import secrets as _secrets
import secrets
import string
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, session, url_for
from flask_login import current_user

from config import db
from models import Level
from utils.security import save_uploaded_file, _ALLOWED_IMAGE_EXTS

roulette_bp = Blueprint('roulette', __name__)

ROULETTE_ROOMS: dict = {}
ROULETTE_ROOMS_LOCK = threading.Lock()

ROULETTE_TAGS = [
    '2.1', '2.2', 'Medium', 'Long', 'XL', 'XXL+', 'Ship', 'Cube', 'Wave',
    'Robot', 'UFO', 'Spider', 'Swing Copter', 'Dual', 'Timings', 'Memory',
    'Nerve Control', 'Slow Paced', 'Fast Paced', 'High CPS',
    'Chokepoints', 'buffer click abuse', 'buffer click', 'ball', 'spam', 'unballanced'
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_user():
    if not getattr(current_user, 'is_authenticated', False):
        return None, ('login_required', 401)
    if bool(getattr(current_user, 'is_disabled', False)):
        return None, ('forbidden', 403)
    return current_user, None


def _parse_tags(raw):
    out = []
    allowed = {c.lower(): c for c in ROULETTE_TAGS}
    seen = set()
    for t in (raw or []):
        key = str(t or '').strip().lower()
        if key in allowed and key not in seen:
            out.append(allowed[key])
            seen.add(key)
    return out


def _level_tag_set(level):
    tags = set()
    for p in str(level.categories or '').split(','):
        s = p.strip()
        if s:
            tags.add(s.lower())
    return tags


def _ids_for_room(category, verified_mode, tags, top_n=None):
    category = (category or 'all').lower()
    if category not in ('all', 'main', 'challenge'):
        category = 'all'
    verified_mode = (verified_mode or 'verified').lower()
    if verified_mode not in ('verified', 'unverified', 'both'):
        verified_mode = 'verified'
    try:
        top_n = int(top_n or 0)
    except Exception:
        top_n = 0
    q = Level.query
    if category != 'all':
        q = q.filter_by(list_type=category)
    if verified_mode == 'verified':
        q = q.filter_by(is_verified=True)
    elif verified_mode == 'unverified':
        q = q.filter_by(is_verified=False)
    if top_n > 0:
        q = q.filter(Level.position.isnot(None), Level.position <= top_n)
    levels = q.all()
    norm_tags = _parse_tags(tags)
    if norm_tags:
        want = {t.lower() for t in norm_tags}
        levels = [lv for lv in levels if _level_tag_set(lv) & want]
    return [lv.id for lv in levels]


def _levels_for_ids(ids):
    if not ids:
        return []
    levels = Level.query.filter(Level.id.in_(ids)).all()
    by_id = {lv.id: lv for lv in levels}
    return [by_id[i] for i in ids if i in by_id]


def _single_settings_key(category, verified_mode, tags, top_n):
    category = (category or 'all').lower()
    verified_mode = (verified_mode or 'verified').lower()
    try:
        top_n = int(top_n or 0)
    except Exception:
        top_n = 0
    norm_tags = _parse_tags(tags)
    tag_key = ','.join(sorted([t.lower() for t in norm_tags]))
    return f'{category}|{verified_mode}|{top_n}|{tag_key}'


def _request_player(payload_player_id, payload_player_name):
    try:
        if getattr(current_user, 'is_authenticated', False):
            return f"user:{int(getattr(current_user, 'id', 0) or 0)}", str(getattr(current_user, 'username', '') or '')
    except Exception:
        pass
    return (payload_player_id or '').strip()[:64], (payload_player_name or '').strip()[:32]


def _new_room_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(secrets.choice(alphabet) for _ in range(6))
        if code not in ROULETTE_ROOMS:
            return code


def _touch_player(room, player_id, player_name, now_ts):
    pid = (player_id or '').strip()[:64] or uuid.uuid4().hex
    name = (player_name or '').strip()[:32] or 'Player'
    players = list(room.get('players') or [])
    found = False
    for p in players:
        if p.get('id') == pid:
            p['name'] = name
            p['last_seen'] = now_ts
            found = True
            break
    if not found:
        players.append({'id': pid, 'name': name, 'last_seen': now_ts})
    cutoff = now_ts - 30 * 60
    room['players'] = [p for p in players if float(p.get('last_seen') or 0) >= cutoff][:64]
    return pid


def _public_players(room):
    owner_pid = room.get('owner_player_id') or ''
    return [{'id': p.get('id') or '', 'name': p.get('name') or 'Player', 'is_owner': p.get('id') == owner_pid}
            for p in (room.get('players') or [])]


def _clean_kicked(room, now_ts):
    kicked = {pid: until for pid, until in (room.get('kicked') or {}).items() if float(until or 0) > now_ts}
    room['kicked'] = kicked
    return kicked


def _is_kicked(room, player_id, now_ts):
    return (player_id or '').strip() in _clean_kicked(room, now_ts)


def _room_cleanup(now_ts):
    cutoff = now_ts - 6 * 60 * 60
    to_delete = [code for code, room in ROULETTE_ROOMS.items() if room.get('updated_at', 0) < cutoff]
    for code in to_delete:
        del ROULETTE_ROOMS[code]


def _is_staff():
    try:
        return bool(getattr(current_user, 'is_authenticated', False)) and getattr(current_user, 'rank', '') in ('mod', 'elder_moderator', 'admin')
    except Exception:
        return False


# ── Single-player endpoints ───────────────────────────────────────────────────

def api_roulette_pool():
    if not getattr(current_user, 'is_authenticated', False):
        return jsonify({'error': 'login_required'}), 401
    category = request.args.get('category', 'all')
    verified_mode = (request.args.get('verified_mode') or 'verified').lower()
    top_n = request.args.get('top_n')
    no_repeat = str(request.args.get('no_repeat') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    tags_raw = (request.args.get('tags') or '').strip()
    tags = [t for t in tags_raw.split(',') if t.strip()] if tags_raw else []
    try:
        top_n_val = int(top_n or 0)
    except Exception:
        top_n_val = 0
    key = _single_settings_key(category, verified_mode, tags, top_n_val)
    data = session.get('roulette_pool_v2') or {}
    pool_key = data.get('key') or ''
    pool = list(data.get('pool') or [])
    if pool_key != key or 'pool' not in data or (not pool and not no_repeat):
        pool = _ids_for_room(category, verified_mode, tags, top_n_val)
        session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    levels = _levels_for_ids(pool)
    return jsonify({'levels': [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]})


def api_roulette_spin():
    if not getattr(current_user, 'is_authenticated', False):
        return jsonify({'error': 'login_required'}), 401
    category = request.args.get('category', 'all')
    verified_mode = (request.args.get('verified_mode') or 'verified').lower()
    top_n = request.args.get('top_n')
    no_repeat = str(request.args.get('no_repeat') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    tags_raw = (request.args.get('tags') or '').strip()
    tags = [t for t in tags_raw.split(',') if t.strip()] if tags_raw else []
    try:
        top_n_val = int(top_n or 0)
    except Exception:
        top_n_val = 0
    key = _single_settings_key(category, verified_mode, tags, top_n_val)
    data = session.get('roulette_pool_v2') or {}
    pool_key = data.get('key') or ''
    pool = list(data.get('pool') or [])
    if pool_key != key or 'pool' not in data:
        pool = _ids_for_room(category, verified_mode, tags, top_n_val)
        session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    elif not pool and not no_repeat:
        pool = _ids_for_room(category, verified_mode, tags, top_n_val)
        session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    if not pool:
        return jsonify({'error': 'empty_pool'}), 400
    chosen_id = _secrets.choice(pool)
    lv = db.session.get(Level, chosen_id)
    if not lv:
        session['roulette_pool_v2'] = {'key': key, 'pool': [i for i in pool if i != chosen_id]}
        return jsonify({'error': 'invalid_level'}), 404
    # no_repeat mode: consume the drawn level so it isn't spun again
    if no_repeat:
        remaining = [i for i in pool if i != chosen_id]
        session['roulette_pool_v2'] = {'key': key, 'pool': remaining}
    return jsonify({'level': {'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'video_url': lv.video_url, 'position': lv.position}, 'pool_size': len(pool)})


def api_roulette_skip():
    if not getattr(current_user, 'is_authenticated', False):
        return jsonify({'error': 'login_required'}), 401
    try:
        payload = request.get_json(silent=True) or {}
        level_id = int(payload.get('level_id'))
    except Exception:
        return jsonify({'error': 'bad_request'}), 400
    category = request.args.get('category', 'all')
    verified_mode = (request.args.get('verified_mode') or 'verified').lower()
    top_n = request.args.get('top_n')
    no_refill = str(request.args.get('no_refill') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    tags_raw = (request.args.get('tags') or '').strip()
    tags = [t for t in tags_raw.split(',') if t.strip()] if tags_raw else []
    try:
        top_n_val = int(top_n or 0)
    except Exception:
        top_n_val = 0
    key = _single_settings_key(category, verified_mode, tags, top_n_val)
    data = session.get('roulette_pool_v2') or {}
    pool = list(data.get('pool') or []) if data.get('key') == key else _ids_for_room(category, verified_mode, tags, top_n_val)
    if level_id in pool:
        pool = [i for i in pool if i != level_id]
    if not pool and not no_refill:
        pool = _ids_for_room(category, verified_mode, tags, top_n_val)
    session['roulette_pool_v2'] = {'key': key, 'pool': pool}
    return jsonify({'pool_size': len(pool)})


def api_roulette_reset():
    if not getattr(current_user, 'is_authenticated', False):
        return jsonify({'error': 'login_required'}), 401
    try:
        session.pop('roulette_pool_v2', None)
    except Exception:
        pass
    return jsonify({'ok': True})


# ── Room endpoints ────────────────────────────────────────────────────────────

@roulette_bp.route('/api/roulette/room/create', methods=['POST'])
def roulette_room_create():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    category = (payload.get('category') or 'all').lower()
    if category not in ('all', 'main', 'challenge'):
        category = 'all'
    verified_mode = (payload.get('verified_mode') or 'verified').lower()
    tags = _parse_tags(payload.get('tags') or [])
    is_public = bool(payload.get('is_public') or False)
    try:
        top_n = max(0, int(payload.get('top_n') or 0))
    except Exception:
        top_n = 0
    ids = _ids_for_room(category, verified_mode, tags, top_n)
    if not ids:
        return jsonify({'error': 'empty_pool'}), 400
    now_ts = time.time()
    owner_token = uuid.uuid4().hex
    player_id, player_name = _request_player(payload.get('player_id'), payload.get('player_name'))
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        owner_pid_raw = (player_id or '').strip()[:64]
        for existing_code, existing_room in ROULETTE_ROOMS.items():
            if (existing_room.get('owner_player_id') or '').strip() == owner_pid_raw and not existing_room.get('closed'):
                return jsonify({'error': 'room_limit'}), 400
        code = _new_room_code()
        room = {
            'pool': ids, 'category': category, 'verified_mode': verified_mode, 'tags': tags,
            'top_n': top_n, 'is_public': is_public, 'closed': False, 'last_level_id': None,
            'updated_at': now_ts, 'created_at': now_ts, 'owner_token': owner_token,
            'owner_player_id': '', 'max_rounds': 15, 'max_attempts': 10, 'current_round': 0,
            'require_proof': False, 'no_repeat': False, 'results': [], 'players': [], 'kicked': {}
        }
        owner_pid = _touch_player(room, player_id, player_name, now_ts)
        room['owner_player_id'] = owner_pid
        ROULETTE_ROOMS[code] = room
        players = _public_players(room)
    levels = _levels_for_ids(ids)
    data = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]
    return jsonify({'code': code, 'category': category, 'verified_mode': verified_mode, 'tags': tags,
                   'top_n': top_n, 'is_public': is_public, 'closed': False, 'levels': data,
                   'pool_size': len(ids), 'owner_token': owner_token, 'max_rounds': 15,
                   'max_attempts': 10, 'current_round': 0, 'require_proof': False, 'no_repeat': False,
                   'results': [], 'players': players, 'owner_player_id': owner_pid})


@roulette_bp.route('/api/roulette/room/join', methods=['POST'])
def roulette_room_join():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _request_player(payload.get('player_id'), payload.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if room.get('closed') and not _is_staff():
            return jsonify({'error': 'closed'}), 403
        if player_id and _is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        _touch_player(room, player_id, player_name, now_ts)
        room['updated_at'] = now_ts
        ids = list(room.get('pool') or [])
        players = _public_players(room)
        last_level_id = room.get('last_level_id')
    levels = _levels_for_ids(ids)
    data = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]
    last_level = db.session.get(Level, last_level_id) if last_level_id else None
    last_payload = {'id': last_level.id, 'gd_level_id': last_level.gd_level_id, 'name': last_level.name, 'creator': last_level.creator, 'position': last_level.position} if last_level else None
    return jsonify({**_room_state_dict(code, room), 'levels': data, 'pool_size': len(ids), 'last_level': last_payload, 'players': players})


@roulette_bp.route('/api/roulette/room/state')
def roulette_room_state():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _request_player(request.args.get('player_id'), request.args.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if player_id and _is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _touch_player(room, player_id, player_name, now_ts)
        room['updated_at'] = now_ts
        ids = list(room.get('pool') or [])
        players = _public_players(room)
        last_level_id = room.get('last_level_id')
    levels = _levels_for_ids(ids)
    data = [{'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position} for lv in levels]
    last_level = db.session.get(Level, last_level_id) if last_level_id else None
    last_payload = {'id': last_level.id, 'gd_level_id': last_level.gd_level_id, 'name': last_level.name, 'creator': last_level.creator, 'position': last_level.position} if last_level else None
    return jsonify({**_room_state_dict(code, room), 'levels': data, 'pool_size': len(ids), 'last_level': last_payload, 'players': players})


@roulette_bp.route('/api/roulette/room/spin', methods=['POST'])
def roulette_room_spin():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _request_player(request.args.get('player_id'), request.args.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        owner_pid = (room.get('owner_player_id') or '').strip()
        if not _is_staff() and (not player_id or (player_id or '').strip() != owner_pid):
            return jsonify({'error': 'forbidden'}), 403
        if player_id and _is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _touch_player(room, player_id, player_name, now_ts)
        pool = list(room.get('pool') or [])
        category = room.get('category') or 'all'
        verified_mode = room.get('verified_mode') or 'verified'
        tags = list(room.get('tags') or [])
        top_n = int(room.get('top_n') or 0)
        max_rounds = int(room.get('max_rounds') or 15)
        max_attempts = int(room.get('max_attempts') or 10)
        current_round = int(room.get('current_round') or 0)
        require_proof = bool(room.get('require_proof') or False)
        no_repeat = bool(room.get('no_repeat') or False)
        if current_round >= max_rounds:
            return jsonify({'error': 'finished'}), 400
        if not pool:
            if no_repeat:
                return jsonify({'error': 'empty_pool'}), 400
            pool = _ids_for_room(category, verified_mode, tags, top_n)
            room['pool'] = pool
        if not pool:
            return jsonify({'error': 'empty_pool'}), 400
        chosen_id = _secrets.choice(pool)
        room['last_level_id'] = chosen_id
        room['current_round'] = current_round + 1
        room['updated_at'] = now_ts
    lv = db.session.get(Level, chosen_id)
    if not lv:
        return jsonify({'error': 'invalid_level'}), 404
    return jsonify({'code': code, 'level': {'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position},
                   'pool_size': len(pool), 'current_round': current_round + 1, 'max_rounds': max_rounds,
                   'max_attempts': max_attempts, 'require_proof': require_proof})


@roulette_bp.route('/api/roulette/room/skip', methods=['POST'])
def roulette_room_skip():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    payload = request.get_json(silent=True) or {}
    player_id, player_name = _request_player(request.args.get('player_id'), request.args.get('player_name'))
    try:
        level_id = int(payload.get('level_id'))
    except Exception:
        return jsonify({'error': 'bad_request'}), 400
    no_refill = str(request.args.get('no_refill') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    keep_round = str(request.args.get('keep_round') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        owner_pid = (room.get('owner_player_id') or '').strip()
        if not _is_staff() and (not player_id or (player_id or '').strip() != owner_pid):
            return jsonify({'error': 'forbidden'}), 403
        if player_id and _is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _touch_player(room, player_id, player_name, now_ts)
        pool = [i for i in (room.get('pool') or []) if i != level_id]
        if not pool and not no_refill:
            pool = _ids_for_room(room.get('category') or 'all', room.get('verified_mode') or 'verified', list(room.get('tags') or []), int(room.get('top_n') or 0))
        room['pool'] = pool
        if not keep_round and room.get('last_level_id') == level_id:
            old_round = int(room.get('current_round') or 0)
            room['last_level_id'] = None
            room['current_round'] = max(0, old_round - 1)
            if old_round > 0:
                room['results'] = [r for r in (room.get('results') or []) if int((r or {}).get('round_no') or 0) != old_round]
        room['updated_at'] = now_ts
    return jsonify({'code': code, 'pool_size': len(pool)})


@roulette_bp.route('/api/roulette/room/config', methods=['POST'])
def roulette_room_config():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    owner_token = (payload.get('owner_token') or '').strip()
    if not code or not owner_token:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if (room.get('owner_token') or '') != owner_token:
            return jsonify({'error': 'forbidden'}), 403
        changed_pool = False
        for field, valid in [('category', ('all', 'main', 'challenge')), ('verified_mode', ('verified', 'unverified', 'both'))]:
            if field in payload:
                v = (payload.get(field) or '').lower()
                if v not in valid:
                    v = valid[0]
                if room.get(field) != v:
                    room[field] = v
                    changed_pool = True
        for int_field in ('max_rounds', 'max_attempts'):
            if int_field in payload:
                try:
                    val = int(payload[int_field])
                    room[int_field] = max(1, min(200 if int_field == 'max_rounds' else 50, val))
                except Exception:
                    pass
        for bool_field in ('require_proof', 'no_repeat'):
            if bool_field in payload:
                room[bool_field] = bool(payload[bool_field])
        if 'is_public' in payload:
            room['is_public'] = False if room.get('closed') else bool(payload['is_public'])
        if 'tags' in payload:
            tags = _parse_tags(payload['tags'])
            if list(room.get('tags') or []) != tags:
                room['tags'] = tags
                changed_pool = True
        if 'top_n' in payload:
            try:
                top_n = max(0, int(payload['top_n']))
                if int(room.get('top_n') or 0) != top_n:
                    room['top_n'] = top_n
                    changed_pool = True
            except Exception:
                pass
        if changed_pool:
            ids = _ids_for_room(room.get('category') or 'all', room.get('verified_mode') or 'verified', list(room.get('tags') or []), int(room.get('top_n') or 0))
            room['pool'] = ids
        room['updated_at'] = now_ts
        resp = _room_state_dict(code, room)
    return jsonify(resp)


@roulette_bp.route('/api/roulette/room/reset', methods=['POST'])
def roulette_room_reset():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    code = (request.args.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _request_player(request.args.get('player_id'), request.args.get('player_name'))
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        owner_pid = (room.get('owner_player_id') or '').strip()
        if not _is_staff() and (not player_id or (player_id or '').strip() != owner_pid):
            return jsonify({'error': 'forbidden'}), 403
        ids = _ids_for_room(room.get('category') or 'all', room.get('verified_mode') or 'verified', list(room.get('tags') or []), int(room.get('top_n') or 0))
        room['pool'] = ids
        room['updated_at'] = now_ts
    return jsonify({'code': code, 'pool_size': len(ids)})


@roulette_bp.route('/api/roulette/room/close', methods=['POST'])
def roulette_room_close():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    owner_token = (payload.get('owner_token') or '').strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if not (owner_token and owner_token == room.get('owner_token')) and not _is_staff():
            return jsonify({'error': 'forbidden'}), 403
        room['closed'] = True
        room['is_public'] = False
        room['updated_at'] = now_ts
    return jsonify({'code': code, 'closed': True, 'is_public': False})


@roulette_bp.route('/api/roulette/room/leave', methods=['POST'])
def roulette_room_leave():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or '').upper().strip()
    player_id, _ = _request_player(payload.get('player_id'), payload.get('player_name'))
    if not code or not player_id:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        room = ROULETTE_ROOMS.get(code)
        if room:
            room['players'] = [p for p in (room.get('players') or []) if p.get('id') != player_id]
            room['updated_at'] = now_ts
    return jsonify({'code': code})


@roulette_bp.route('/api/roulette/rooms/public')
def roulette_public_rooms():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    now_ts = time.time()
    rooms = []
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        for code, room in ROULETTE_ROOMS.items():
            if not room.get('is_public') or room.get('closed'):
                continue
            players = list(room.get('players') or [])
            owner_pid = room.get('owner_player_id') or ''
            owner_name = next((p.get('name') or '' for p in players if p.get('id') == owner_pid), '') or 'Owner'
            rooms.append({'code': code, 'category': room.get('category') or 'all', 'verified_mode': room.get('verified_mode') or 'verified', 'tags': list(room.get('tags') or []), 'top_n': int(room.get('top_n') or 0), 'owner_name': owner_name, 'players_count': len(players), 'created_at': int(room.get('created_at') or now_ts)})
    rooms.sort(key=lambda r: (-(r.get('players_count') or 0), -(r.get('created_at') or 0)))
    return jsonify({'rooms': rooms[:50]})


@roulette_bp.route('/api/roulette/room/kick', methods=['POST'])
def roulette_room_kick():
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    code = (payload.get('code') or request.args.get('code') or '').upper().strip()
    owner_token = (payload.get('owner_token') or '').strip()
    target_player_id = (payload.get('player_id') or payload.get('target_player_id') or '').strip()[:64]
    if not code or not target_player_id:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        allowed = (owner_token and owner_token == room.get('owner_token')) or _is_staff()
        if not allowed:
            return jsonify({'error': 'forbidden'}), 403
        room['players'] = [p for p in (room.get('players') or []) if p.get('id') != target_player_id]
        kicked = dict(room.get('kicked') or {})
        kicked[target_player_id] = now_ts + 30 * 60
        room['kicked'] = kicked
        room['updated_at'] = now_ts
        out_players = _public_players(room)
    return jsonify({'code': code, 'players': out_players})


@roulette_bp.route('/api/roulette/room/result', methods=['POST'])
def roulette_room_result():
    import os as _os
    from flask import current_app
    user, err = _require_user()
    if err:
        return jsonify({'error': err[0]}), err[1]
    code = (request.args.get('code') or request.form.get('code') or '').upper().strip()
    if not code:
        return jsonify({'error': 'bad_request'}), 400
    player_id, player_name = _request_player(request.form.get('player_id') or request.args.get('player_id'), request.form.get('player_name'))
    if not player_name:
        player_name = 'Player'
    try:
        level_id = int(request.form.get('level_id'))
        round_no = int(request.form.get('round_no'))
        first_try_percent = min(100.0, max(0.0, float(request.form.get('first_try_percent'))))
        best_percent = min(100.0, max(first_try_percent, float(request.form.get('best_percent'))))
        attempts_used = int(request.form.get('attempts_used'))
    except Exception:
        return jsonify({'error': 'bad_request'}), 400
    now_ts = time.time()
    with ROULETTE_ROOMS_LOCK:
        _room_cleanup(now_ts)
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        if player_id and _is_kicked(room, player_id, now_ts):
            return jsonify({'error': 'kicked'}), 403
        if player_id or player_name:
            _touch_player(room, player_id, player_name, now_ts)
        max_attempts = int(room.get('max_attempts') or 10)
        require_proof = bool(room.get('require_proof') or False)
    if attempts_used < 1 or attempts_used > max_attempts:
        return jsonify({'error': 'bad_request'}), 400
    proof_url = None
    f = request.files.get('proof')
    if require_proof and (not f or not f.filename):
        return jsonify({'error': 'proof_required'}), 400
    if f and f.filename:
        folder = _os.path.join(current_app.root_path, 'static', 'roulette_proofs')
        unique, err_str = save_uploaded_file(f, folder, _ALLOWED_IMAGE_EXTS, 'image/')
        if not unique:
            return jsonify({'error': 'rate_limited' if err_str == 'rate_limited' else 'bad_file'}), (429 if err_str == 'rate_limited' else 400)
        proof_url = url_for('static', filename=f'roulette_proofs/{unique}')
    lv = db.session.get(Level, level_id)
    if not lv:
        return jsonify({'error': 'invalid_level'}), 404
    entry = {'player_name': player_name, 'level': {'id': lv.id, 'gd_level_id': lv.gd_level_id, 'name': lv.name, 'creator': lv.creator, 'position': lv.position}, 'round_no': round_no, 'first_try_percent': first_try_percent, 'best_percent': best_percent, 'attempts_used': attempts_used, 'proof_url': proof_url, 'submitted_at': int(now_ts)}
    with ROULETTE_ROOMS_LOCK:
        room = ROULETTE_ROOMS.get(code)
        if not room:
            return jsonify({'error': 'not_found'}), 404
        # INTEGRITY: result must match the level actually spun this round and
        # the submitter must be a room participant.
        spun_id = int(room.get('last_level_id') or 0)
        if spun_id != int(lv.id):
            return jsonify({'error': 'level_mismatch'}), 400
        participants = {p.get('player_id') for p in (room.get('players') or [])}
        if not player_id or player_id not in participants:
            return jsonify({'error': 'not_in_room'}), 403
        results = list(room.get('results') or [])
        # INTEGRITY: one result per player per round - duplicates rejected.
        for r in results:
            try:
                if int(r.get('round_no') or 0) == int(round_no) and (r.get('player_id') or '') == (player_id or ''):
                    return jsonify({'error': 'already_submitted'}), 409
            except Exception:
                pass
        entry['player_id'] = player_id
        results.append(entry)
        room['results'] = results[-500:]
        room['updated_at'] = now_ts
        out = {'code': code, 'results': room['results'][-200:], 'max_rounds': room.get('max_rounds', 15), 'current_round': room.get('current_round', 0), 'require_proof': bool(room.get('require_proof')), 'max_attempts': room.get('max_attempts', 10)}
    return jsonify(out)


# ── Helper ────────────────────────────────────────────────────────────────────

def _room_state_dict(code, room):
    return {
        'code': code,
        'category': room.get('category') or 'all',
        'verified_mode': room.get('verified_mode') or 'verified',
        'tags': list(room.get('tags') or []),
        'top_n': int(room.get('top_n') or 0),
        'is_public': bool(room.get('is_public') or False),
        'closed': bool(room.get('closed') or False),
        'max_rounds': int(room.get('max_rounds') or 15),
        'max_attempts': int(room.get('max_attempts') or 10),
        'current_round': int(room.get('current_round') or 0),
        'require_proof': bool(room.get('require_proof') or False),
        'no_repeat': bool(room.get('no_repeat') or False),
        'results': list(room.get('results') or [])[-200:],
        'owner_player_id': room.get('owner_player_id') or '',
    }

# Solo-mode routes (previously aliased through the removed API blueprint)
roulette_bp.add_url_rule('/api/roulette/pool', view_func=api_roulette_pool, methods=['GET'])
roulette_bp.add_url_rule('/api/roulette/spin', view_func=api_roulette_spin, methods=['POST'])
roulette_bp.add_url_rule('/api/roulette/skip', view_func=api_roulette_skip, methods=['POST'])
roulette_bp.add_url_rule('/api/roulette/reset', view_func=api_roulette_reset, methods=['POST'])
