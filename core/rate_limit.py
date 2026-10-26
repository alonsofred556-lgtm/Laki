"""Redis-only rate limiting for security-sensitive and upstream proxy paths."""

from functools import lru_cache

from django.conf import settings


class RateLimitUnavailable(Exception):
    pass


@lru_cache(maxsize=1)
def _client():
    url = getattr(settings, "CONI_REDIS_URL", "")
    if not url:
        return None
    try:
        import redis
        return redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2, health_check_interval=30, decode_responses=True)
    except Exception as exc:
        raise RateLimitUnavailable("Redis client is unavailable") from exc


def allow(bucket, key, limit=10, window_seconds=60):
    """Return (allowed, remaining). Fail closed when Redis is required."""
    client = _client()
    if client is None:
        if getattr(settings, "CONI_REDIS_REQUIRED", True):
            raise RateLimitUnavailable("Redis rate limiting is required")
        return True, limit
    redis_key = f"coni:ratelimit:{bucket}:{key}"
    try:
        pipe = client.pipeline()
        pipe.incr(redis_key)
        pipe.ttl(redis_key)
        count, ttl = pipe.execute()
        if count == 1 or ttl < 0:
            client.expire(redis_key, window_seconds)
        return count <= limit, max(0, limit - count)
    except Exception as exc:
        raise RateLimitUnavailable("Redis rate limiting failed") from exc
