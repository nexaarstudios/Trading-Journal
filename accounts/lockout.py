import hashlib
import time

from django.conf import settings
from django.core.cache import cache


def _key(kind, identifier):
    digest = hashlib.sha256(str(identifier).strip().lower().encode()).hexdigest()
    return f"{kind}:{digest}"


def seconds_locked(identifier):
    """How many seconds this identifier is still locked out for, or 0."""
    until = cache.get(_key("lock", identifier))
    if not until:
        return 0
    return max(int(until - time.time()), 0)


def register_failure(identifier):
    """Count one failed attempt. Returns True if this one locked the identifier."""
    key = _key("fail", identifier)
    cache.add(key, 0, timeout=settings.LOGIN_LOCKOUT_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:  # the counter expired between the two calls
        cache.set(key, 1, timeout=settings.LOGIN_LOCKOUT_SECONDS)
        count = 1
    if count >= settings.LOGIN_MAX_FAILURES:
        cache.set(
            _key("lock", identifier), time.time() + settings.LOGIN_LOCKOUT_SECONDS,
            timeout=settings.LOGIN_LOCKOUT_SECONDS,
        )
        cache.delete(key)
        return True
    return False


def clear(identifier):
    cache.delete_many([_key("fail", identifier), _key("lock", identifier)])