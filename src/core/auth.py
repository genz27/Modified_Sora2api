"""Authentication module"""
import bcrypt
from typing import Optional
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .config import config

security = HTTPBearer()

# Password hash cache to avoid repeated hashing
_password_hash_cache: dict = {}


class AuthManager:
    """Authentication manager"""

    @staticmethod
    def verify_api_key(api_key: str) -> bool:
        """Verify API key"""
        return api_key == config.api_key

    @staticmethod
    def verify_admin(username: str, password: str) -> bool:
        """Verify admin credentials using secure comparison

        Uses bcrypt for password verification when a hashed password is available,
        falls back to constant-time comparison for plain text passwords.
        """
        import hmac

        if username != config.admin_username:
            return False

        stored_password = config.admin_password

        # Check if stored password is a bcrypt hash (starts with $2b$ or $2a$)
        if stored_password and (stored_password.startswith('$2b$') or stored_password.startswith('$2a$')):
            try:
                return bcrypt.checkpw(password.encode(), stored_password.encode())
            except Exception:
                return False

        # For plain text passwords, use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(password, stored_password)

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using bcrypt"""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify password against bcrypt hash"""
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception:
            return False

    @staticmethod
    def is_password_hashed(password: str) -> bool:
        """Check if a password string is already a bcrypt hash"""
        return password and (password.startswith('$2b$') or password.startswith('$2a$'))

async def verify_api_key_header(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """Verify API key from Authorization header"""
    api_key = credentials.credentials
    if not AuthManager.verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
    return api_key
