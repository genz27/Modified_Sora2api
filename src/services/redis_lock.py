"""Redis-based distributed lock manager for concurrent request handling"""
import asyncio
from typing import Optional
from ..core.config import config
from ..core.redis_manager import get_redis_manager

# Redis client (lazy initialization) - kept for backward compatibility
_redis_client = None


async def get_redis_client():
    """Get or create Redis client - now uses RedisManager"""
    manager = get_redis_manager()
    if not manager._initialized:
        await manager.initialize()
    return manager._client


class RedisLock:
    """Distributed lock using Redis"""
    
    def __init__(self, key: str, timeout: int = None):
        self.key = key
        self.timeout = timeout or config.redis_lock_timeout
        self._lock_value = None
    
    async def acquire(self, blocking: bool = True, timeout: float = None) -> bool:
        """Acquire the lock"""
        manager = get_redis_manager()
        if not manager._initialized:
            await manager.initialize()
        
        wait_timeout = timeout or self.timeout
        self._lock_value = await manager.acquire_lock(
            self.key, 
            timeout=self.timeout, 
            blocking=blocking, 
            wait_timeout=wait_timeout
        )
        return self._lock_value is not None
    
    async def release(self):
        """Release the lock"""
        if self._lock_value is None:
            return
        
        manager = get_redis_manager()
        await manager.release_lock(self.key, self._lock_value)
        self._lock_value = None
    
    async def __aenter__(self):
        await self.acquire()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


class RedisCFLock:
    """Distributed lock for Cloudflare credential refresh"""
    
    CF_LOCK_KEY = "cf:refresh:lock"
    CF_REFRESHING_KEY = "cf:refreshing"
    
    @classmethod
    async def is_refreshing(cls) -> bool:
        """Check if CF credentials are being refreshed"""
        manager = get_redis_manager()
        if not manager._initialized:
            await manager.initialize()
        return await manager.is_cf_refreshing()
    
    @classmethod
    async def set_refreshing(cls, value: bool, ttl: int = 60):
        """Set CF refreshing status"""
        manager = get_redis_manager()
        if not manager._initialized:
            await manager.initialize()
        await manager.set_cf_refreshing(value, ttl)
    
    @classmethod
    async def acquire_lock(cls, timeout: int = 60) -> bool:
        """Acquire CF refresh lock"""
        manager = get_redis_manager()
        if not manager._initialized:
            await manager.initialize()
        return await manager.acquire_cf_lock(timeout)
    
    @classmethod
    async def release_lock(cls):
        """Release CF refresh lock"""
        manager = get_redis_manager()
        if not manager._initialized:
            await manager.initialize()
        await manager.release_cf_lock()


async def close_redis():
    """Close Redis connection - now handled by RedisManager"""
    from ..core.redis_manager import close_redis as close_redis_manager
    await close_redis_manager()
