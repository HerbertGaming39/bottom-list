"""
utils/cache.py – Lightweight thread-safe TTL in-memory cache.
"""
import time
import threading

_TTL_CACHE: dict = {}
_TTL_CACHE_LOCK = threading.Lock()


def cache_get(key: str):
    now = time.time()
    with _TTL_CACHE_LOCK:
        item = _TTL_CACHE.get(key)
        if not item:
            return None
        exp, val = item
        try:
            if exp and float(exp) < now:
                _TTL_CACHE.pop(key, None)
                return None
        except Exception:
            _TTL_CACHE.pop(key, None)
            return None
        return val


def cache_set(key: str, val, ttl_seconds: int):
    try:
        ttl = int(ttl_seconds or 0)
    except Exception:
        ttl = 0
    if ttl <= 0:
        return
    exp = time.time() + ttl
    with _TTL_CACHE_LOCK:
        _TTL_CACHE[key] = (exp, val)


def cache_bust_prefixes(prefixes):
    if not prefixes:
        return
    prefs = tuple([str(p or '') for p in prefixes if str(p or '')])
    if not prefs:
        return
    with _TTL_CACHE_LOCK:
        keys = list(_TTL_CACHE.keys())
        for k in keys:
            try:
                if any(str(k).startswith(p) for p in prefs):
                    _TTL_CACHE.pop(k, None)
            except Exception:
                continue
