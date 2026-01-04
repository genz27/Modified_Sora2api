"""Redis manager for distributed locking and caching"""
import asyncio
from typing import Optional, Any
from .config import config


class RedisManager:
    """Redis connection manager with fallback to local operations"""
    
    def __init__(self):
        self._client = None
        self._initialized = False
        self._local_locks: dict = {}  # Fallback local locks
        self._local_cache: dict = {}  # Fallback local cache
        self._local_lock = asyncio.Lock()
    
    async def initialize(self) -> bool:
        """Initialize Redis connection if enabled"""
        if not config.redis_enabled:
            print("ℹ️ Redis disabled, using local locks and cache")
            self._initialized = True
            return False
        
        try:
            import redis.asyncio as redis
            self._client = redis.Redis(
                host=config.redis_host,
                port=config.redis_port,
                password=config.redis_password or None,
                db=config.redis_db,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # Test connection
            await self._client.ping()
            self._initialized = True
            print(f"✅ Redis connected: {config.redis_host}:{config.redis_port}")
            return True
        except Exception as e:
            print(f"⚠️ Redis connection failed: {e}, falling back to local locks")
            self._client = None
            self._initialized = True
            return False
    
    async def close(self):
        """Close Redis connection"""
        if self._client:
            await self._client.close()
            self._client = None
        self._initialized = False
    
    @property
    def is_connected(self) -> bool:
        """Check if Redis is connected"""
        return self._client is not None
    
    # ==================== Distributed Lock Operations ====================
    
    async def acquire_lock(self, key: str, timeout: int = None, blocking: bool = True, 
                          wait_timeout: float = None) -> Optional[str]:
        """
        Acquire a distributed lock
        
        Args:
            key: Lock key
            timeout: Lock expiration time in seconds
            blocking: Whether to wait for lock
            wait_timeout: Maximum time to wait for lock
            
        Returns:
            Lock value (UUID) if acquired, None otherwise
        """
        import uuid
        lock_key = f"lock:{key}"
        lock_value = str(uuid.uuid4())
        timeout = timeout or config.redis_lock_timeout
        wait_timeout = wait_timeout or timeout
        
        if self._client:
            # Use Redis
            start_time = asyncio.get_event_loop().time()
            while True:
                acquired = await self._client.set(
                    lock_key, lock_value, nx=True, ex=timeout
                )
                if acquired:
                    return lock_value
                
                if not blocking:
                    return None
                
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= wait_timeout:
                    return None
                
                await asyncio.sleep(0.1)
        else:
            # Fallback to local lock
            async with self._local_lock:
                if lock_key in self._local_locks:
                    if not blocking:
                        return None
                    # Wait for lock (simplified)
                    start_time = asyncio.get_event_loop().time()
                    while lock_key in self._local_locks:
                        await asyncio.sleep(0.1)
                        if asyncio.get_event_loop().time() - start_time >= wait_timeout:
                            return None
                
                self._local_locks[lock_key] = lock_value
                # Schedule auto-release
                asyncio.create_task(self._auto_release_local_lock(lock_key, timeout))
                return lock_value
    
    async def _auto_release_local_lock(self, key: str, timeout: int):
        """Auto-release local lock after timeout"""
        await asyncio.sleep(timeout)
        async with self._local_lock:
            if key in self._local_locks:
                del self._local_locks[key]
    
    async def release_lock(self, key: str, lock_value: str) -> bool:
        """
        Release a distributed lock
        
        Args:
            key: Lock key
            lock_value: Lock value returned by acquire_lock
            
        Returns:
            True if released, False otherwise
        """
        lock_key = f"lock:{key}"
        
        if self._client:
            # Use Lua script to ensure atomic release
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            try:
                result = await self._client.eval(lua_script, 1, lock_key, lock_value)
                return result == 1
            except Exception as e:
                print(f"⚠️ Failed to release Redis lock: {e}")
                return False
        else:
            # Fallback to local lock
            async with self._local_lock:
                if self._local_locks.get(lock_key) == lock_value:
                    del self._local_locks[lock_key]
                    return True
                return False
    
    async def is_locked(self, key: str) -> bool:
        """Check if a key is locked"""
        lock_key = f"lock:{key}"
        
        if self._client:
            return await self._client.exists(lock_key) > 0
        else:
            return lock_key in self._local_locks
    
    # ==================== Cache Operations ====================
    
    async def get(self, key: str) -> Optional[str]:
        """Get a value from cache"""
        if self._client:
            return await self._client.get(key)
        else:
            return self._local_cache.get(key)
    
    async def set(self, key: str, value: str, ex: int = None) -> bool:
        """Set a value in cache"""
        if self._client:
            return await self._client.set(key, value, ex=ex)
        else:
            self._local_cache[key] = value
            if ex:
                asyncio.create_task(self._auto_expire_local_cache(key, ex))
            return True
    
    async def _auto_expire_local_cache(self, key: str, ex: int):
        """Auto-expire local cache after timeout"""
        await asyncio.sleep(ex)
        if key in self._local_cache:
            del self._local_cache[key]
    
    async def delete(self, key: str) -> bool:
        """Delete a key from cache"""
        if self._client:
            return await self._client.delete(key) > 0
        else:
            if key in self._local_cache:
                del self._local_cache[key]
                return True
            return False
    
    async def exists(self, key: str) -> bool:
        """Check if a key exists"""
        if self._client:
            return await self._client.exists(key) > 0
        else:
            return key in self._local_cache
    
    # ==================== Token Lock Operations ====================
    
    async def acquire_token_lock(self, token_id: int, lock_type: str = "image", 
                                 timeout: int = 300) -> Optional[str]:
        """
        Acquire a lock for a specific token
        
        Args:
            token_id: Token ID
            lock_type: Lock type (image/video)
            timeout: Lock timeout in seconds
            
        Returns:
            Lock value if acquired, None otherwise
        """
        key = f"token:{token_id}:{lock_type}"
        return await self.acquire_lock(key, timeout=timeout, blocking=False)
    
    async def release_token_lock(self, token_id: int, lock_type: str, lock_value: str) -> bool:
        """Release a token lock"""
        key = f"token:{token_id}:{lock_type}"
        return await self.release_lock(key, lock_value)
    
    async def is_token_locked(self, token_id: int, lock_type: str = "image") -> bool:
        """Check if a token is locked"""
        key = f"token:{token_id}:{lock_type}"
        return await self.is_locked(key)
    
    # ==================== Cloudflare Lock Operations ====================
    
    async def acquire_cf_lock(self, timeout: int = 60) -> bool:
        """Acquire Cloudflare refresh lock"""
        lock_value = await self.acquire_lock("cf:refresh", timeout=timeout, blocking=False)
        return lock_value is not None
    
    async def release_cf_lock(self):
        """Release Cloudflare refresh lock"""
        # For CF lock, we just delete the key
        if self._client:
            await self._client.delete("lock:cf:refresh")
        else:
            async with self._local_lock:
                if "lock:cf:refresh" in self._local_locks:
                    del self._local_locks["lock:cf:refresh"]
    
    async def is_cf_refreshing(self) -> bool:
        """Check if CF credentials are being refreshed"""
        return await self.exists("cf:refreshing")
    
    async def set_cf_refreshing(self, value: bool, ttl: int = 60):
        """Set CF refreshing status"""
        if value:
            await self.set("cf:refreshing", "1", ex=ttl)
        else:
            await self.delete("cf:refreshing")
    
    # ==================== Concurrency Counter Operations ====================
    
    async def get_concurrency(self, token_id: int, lock_type: str) -> int:
        """Get current concurrency count for a token"""
        key = f"concurrency:{token_id}:{lock_type}"
        if self._client:
            value = await self._client.get(key)
            return int(value) if value else 0
        else:
            return self._local_cache.get(key, 0)
    
    async def increment_concurrency(self, token_id: int, lock_type: str) -> int:
        """Increment concurrency count"""
        key = f"concurrency:{token_id}:{lock_type}"
        if self._client:
            return await self._client.incr(key)
        else:
            current = self._local_cache.get(key, 0)
            self._local_cache[key] = current + 1
            return current + 1
    
    async def decrement_concurrency(self, token_id: int, lock_type: str) -> int:
        """Decrement concurrency count"""
        key = f"concurrency:{token_id}:{lock_type}"
        if self._client:
            result = await self._client.decr(key)
            if result < 0:
                await self._client.set(key, 0)
                return 0
            return result
        else:
            current = self._local_cache.get(key, 0)
            new_value = max(0, current - 1)
            self._local_cache[key] = new_value
            return new_value


# Global Redis manager instance
_redis_manager: Optional[RedisManager] = None


def get_redis_manager() -> RedisManager:
    """Get the global Redis manager"""
    global _redis_manager
    if _redis_manager is None:
        _redis_manager = RedisManager()
    return _redis_manager


async def init_redis() -> RedisManager:
    """Initialize the global Redis manager"""
    manager = get_redis_manager()
    await manager.initialize()
    return manager


async def close_redis():
    """Close the global Redis manager"""
    global _redis_manager
    if _redis_manager:
        await _redis_manager.close()
        _redis_manager = None
