"""Token lock manager for image generation"""
import asyncio
import time
from typing import Dict, Optional
from ..core.logger import debug_logger
from ..core.config import config


class TokenLock:
    """Token lock manager for image generation (supports Redis for distributed mode)"""
    
    def __init__(self, lock_timeout: int = 300):
        """
        Initialize token lock manager
        
        Args:
            lock_timeout: Lock timeout in seconds (default: 300s = 5 minutes)
        """
        self.lock_timeout = lock_timeout
        self._locks: Dict[int, float] = {}  # token_id -> lock_timestamp (local fallback)
        self._lock_values: Dict[int, str] = {}  # token_id -> lock_value (for Redis)
        self._lock = asyncio.Lock()  # Protect _locks dict
        self._redis_manager = None
    
    async def _get_redis_manager(self):
        """Get Redis manager if available"""
        if self._redis_manager is None:
            try:
                from ..core.redis_manager import get_redis_manager
                self._redis_manager = get_redis_manager()
                if not self._redis_manager._initialized:
                    await self._redis_manager.initialize()
            except:
                self._redis_manager = None
        return self._redis_manager
    
    async def acquire_lock(self, token_id: int) -> bool:
        """
        Try to acquire lock for image generation
        
        Args:
            token_id: Token ID
            
        Returns:
            True if lock acquired, False if already locked
        """
        redis_mgr = await self._get_redis_manager()
        
        if redis_mgr and redis_mgr.is_connected:
            # Use Redis distributed lock
            lock_value = await redis_mgr.acquire_token_lock(token_id, "image", self.lock_timeout)
            if lock_value:
                self._lock_values[token_id] = lock_value
                debug_logger.log_info(f"Token {token_id} lock acquired (Redis)")
                return True
            else:
                debug_logger.log_info(f"Token {token_id} is locked (Redis)")
                return False
        else:
            # Fallback to local lock
            async with self._lock:
                current_time = time.time()
                
                if token_id in self._locks:
                    lock_time = self._locks[token_id]
                    
                    if current_time - lock_time > self.lock_timeout:
                        debug_logger.log_info(f"Token {token_id} lock expired, releasing")
                        del self._locks[token_id]
                    else:
                        remaining = self.lock_timeout - (current_time - lock_time)
                        debug_logger.log_info(f"Token {token_id} is locked, remaining: {remaining:.1f}s")
                        return False
                
                self._locks[token_id] = current_time
                debug_logger.log_info(f"Token {token_id} lock acquired (local)")
                return True
    
    async def release_lock(self, token_id: int):
        """
        Release lock for token
        
        Args:
            token_id: Token ID
        """
        redis_mgr = await self._get_redis_manager()
        
        if redis_mgr and redis_mgr.is_connected and token_id in self._lock_values:
            # Release Redis lock
            lock_value = self._lock_values.pop(token_id, None)
            if lock_value:
                await redis_mgr.release_token_lock(token_id, "image", lock_value)
                debug_logger.log_info(f"Token {token_id} lock released (Redis)")
        else:
            # Release local lock
            async with self._lock:
                if token_id in self._locks:
                    del self._locks[token_id]
                    debug_logger.log_info(f"Token {token_id} lock released (local)")
    
    async def is_locked(self, token_id: int) -> bool:
        """
        Check if token is locked
        
        Args:
            token_id: Token ID
            
        Returns:
            True if locked, False otherwise
        """
        redis_mgr = await self._get_redis_manager()
        
        if redis_mgr and redis_mgr.is_connected:
            return await redis_mgr.is_token_locked(token_id, "image")
        else:
            async with self._lock:
                if token_id not in self._locks:
                    return False
                
                current_time = time.time()
                lock_time = self._locks[token_id]
                
                if current_time - lock_time > self.lock_timeout:
                    del self._locks[token_id]
                    return False
                
                return True
    
    async def cleanup_expired_locks(self):
        """Clean up expired locks (local only, Redis handles expiration automatically)"""
        async with self._lock:
            current_time = time.time()
            expired_tokens = []
            
            for token_id, lock_time in self._locks.items():
                if current_time - lock_time > self.lock_timeout:
                    expired_tokens.append(token_id)
            
            for token_id in expired_tokens:
                del self._locks[token_id]
                debug_logger.log_info(f"Cleaned up expired lock for token {token_id}")
            
            if expired_tokens:
                debug_logger.log_info(f"Cleaned up {len(expired_tokens)} expired locks")
    
    def get_locked_tokens(self) -> list:
        """Get list of currently locked token IDs (local only)"""
        return list(self._locks.keys())

    def set_lock_timeout(self, timeout: int):
        """Set lock timeout in seconds"""
        self.lock_timeout = timeout
        debug_logger.log_info(f"Lock timeout updated to {timeout} seconds")
