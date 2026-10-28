import logging
import time
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# DECISION: The release lock Lua script compares the token before DEL, registered once per client.
RELEASE_LOCK_LUA = """-- release only if we still own the lock
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# DECISION: Rate limit Lua script from SPEC §14 table (Redis fixed window, returns count).
RATE_LIMIT_LUA = """-- rate_limit.lua
-- KEYS[1] = rl:{scope}:{ip}:{window_start}, ARGV[1] = window ms; returns count
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
return n
"""


class RedisClient:
    def __init__(self, client: aioredis.Redis):
        self.client = client
        self._release_script = client.register_script(RELEASE_LOCK_LUA)
        self._rate_limit_script = client.register_script(RATE_LIMIT_LUA)

    async def aclose(self) -> None:
        try:
            await self.client.aclose()
        except Exception as exc:
            logger.warning("Error closing Redis client: %s", exc)

    def pubsub(self):
        return self.client.pubsub()

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning("Redis ping failed: %s", exc)
            return False

    async def publish(self, channel: str, message: str) -> bool:
        try:
            await self.client.publish(channel, message)
            return True
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning("Redis publish failed on channel %s: %s", channel, exc)
            return False

    async def set_lock(self, key: str, token: str, px: int = 120000) -> bool:
        try:
            res = await self.client.set(key, token, nx=True, px=px)
            return bool(res)
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning("Redis set lock failed on %s: %s", key, exc)
            # DECISION: Fail open with True when Redis is down so sweep runs without lock.
            return True

    async def release_lock(self, key: str, token: str) -> bool:
        try:
            res = await self._release_script(keys=[key], args=[token])
            return bool(res)
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning("Redis release lock failed on %s: %s", key, exc)
            return False

    async def get(self, key: str) -> Any:
        try:
            return await self.client.get(key)
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning("Redis get failed on %s: %s", key, exc)
            return None

    async def set(self, key: str, value: str, **kwargs) -> bool:
        try:
            res = await self.client.set(key, value, **kwargs)
            return bool(res)
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning("Redis set failed on %s: %s", key, exc)
            return False

    async def check_rate_limit(
        self, scope: str, ip: str, limit: int, window_seconds: int
    ) -> tuple[bool, int]:
        now = int(time.time())
        window_start = (now // window_seconds) * window_seconds
        key = f"rl:{scope}:{ip}:{window_start}"
        window_ms = window_seconds * 1000
        retry_after = max(1, (window_start + window_seconds) - now)
        try:
            count = await self._rate_limit_script(keys=[key], args=[window_ms])
            if count > limit:
                return False, retry_after
            return True, retry_after
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning("Redis rate limit check failed on %s: %s", key, exc)
            # DECISION: Fail open when Redis is unavailable so requests succeed without limits.
            return True, 0


def make_redis(url: str) -> RedisClient:
    # DECISION: Use short socket timeouts (1.0s) so Redis failures fail open without blocking.
    raw_client = aioredis.from_url(
        url,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        decode_responses=True,
    )
    return RedisClient(raw_client)
