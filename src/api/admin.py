"""Admin routes - Management endpoints"""
from fastapi import APIRouter, HTTPException, Depends, Header
from typing import List, Optional
from datetime import datetime
import secrets
from pydantic import BaseModel
from ..core.auth import AuthManager
from ..core.config import config
from ..services.token_manager import TokenManager
from ..services.proxy_manager import ProxyManager
from ..services.concurrency_manager import ConcurrencyManager
from ..core.database import Database
from ..core.models import Token, AdminConfig, ProxyConfig

router = APIRouter()

# Dependency injection
token_manager: TokenManager = None
proxy_manager: ProxyManager = None
db: Database = None
generation_handler = None
concurrency_manager: ConcurrencyManager = None

# Store active admin tokens (in production, use Redis or database)
active_admin_tokens = set()

def set_dependencies(tm: TokenManager, pm: ProxyManager, database: Database, gh=None, cm: ConcurrencyManager = None):
    """Set dependencies"""
    global token_manager, proxy_manager, db, generation_handler, concurrency_manager
    token_manager = tm
    proxy_manager = pm
    db = database
    generation_handler = gh
    concurrency_manager = cm

def verify_admin_token(authorization: str = Header(None)):
    """Verify admin token from Authorization header"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    # Support both "Bearer token" and "token" formats
    token = authorization
    if authorization.startswith("Bearer "):
        token = authorization[7:]

    if token not in active_admin_tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return token

# Request/Response models
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    message: Optional[str] = None

class AddTokenRequest(BaseModel):
    token: str  # Access Token (AT)
    st: Optional[str] = None  # Session Token (optional, for storage)
    rt: Optional[str] = None  # Refresh Token (optional, for storage)
    client_id: Optional[str] = None  # Client ID (optional)
    proxy_url: Optional[str] = None  # Proxy URL (optional)
    remark: Optional[str] = None
    image_enabled: bool = True  # Enable image generation
    video_enabled: bool = True  # Enable video generation
    image_concurrency: int = -1  # Image concurrency limit (-1 for no limit)
    video_concurrency: int = -1  # Video concurrency limit (-1 for no limit)

class ST2ATRequest(BaseModel):
    st: str  # Session Token

class RT2ATRequest(BaseModel):
    rt: str  # Refresh Token

class UpdateTokenStatusRequest(BaseModel):
    is_active: bool

class UpdateTokenRequest(BaseModel):
    token: Optional[str] = None  # Access Token
    st: Optional[str] = None
    rt: Optional[str] = None
    client_id: Optional[str] = None  # Client ID
    proxy_url: Optional[str] = None  # Proxy URL
    remark: Optional[str] = None
    image_enabled: Optional[bool] = None  # Enable image generation
    video_enabled: Optional[bool] = None  # Enable video generation
    image_concurrency: Optional[int] = None  # Image concurrency limit
    video_concurrency: Optional[int] = None  # Video concurrency limit

class ImportTokenItem(BaseModel):
    email: str  # Email (primary key)
    access_token: str  # Access Token (AT)
    session_token: Optional[str] = None  # Session Token (ST)
    refresh_token: Optional[str] = None  # Refresh Token (RT)
    proxy_url: Optional[str] = None  # Proxy URL (optional)
    remark: Optional[str] = None  # Remark (optional)
    is_active: bool = True  # Active status
    image_enabled: bool = True  # Enable image generation
    video_enabled: bool = True  # Enable video generation
    image_concurrency: int = -1  # Image concurrency limit
    video_concurrency: int = -1  # Video concurrency limit

class ImportTokensRequest(BaseModel):
    tokens: List[ImportTokenItem]

class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: int

class UpdateProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    proxy_pool_enabled: bool = False

class UpdateAdminPasswordRequest(BaseModel):
    old_password: str
    new_password: str
    username: Optional[str] = None  # Optional: new username

class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str

class UpdateDebugConfigRequest(BaseModel):
    enabled: bool

class UpdateCacheTimeoutRequest(BaseModel):
    timeout: int  # Cache timeout in seconds

class UpdateCacheBaseUrlRequest(BaseModel):
    base_url: str  # Cache base URL (e.g., https://yourdomain.com)

class UpdateGenerationTimeoutRequest(BaseModel):
    image_timeout: Optional[int] = None  # Image generation timeout in seconds
    video_timeout: Optional[int] = None  # Video generation timeout in seconds

class UpdateWatermarkFreeConfigRequest(BaseModel):
    watermark_free_enabled: bool
    parse_method: Optional[str] = "third_party"  # "third_party" or "custom"
    custom_parse_url: Optional[str] = None
    custom_parse_token: Optional[str] = None

class UpdateCloudflareSolverConfigRequest(BaseModel):
    solver_enabled: bool
    solver_api_url: Optional[str] = None

# Auth endpoints
@router.post("/api/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Admin login"""
    if AuthManager.verify_admin(request.username, request.password):
        # Generate simple token
        token = f"admin-{secrets.token_urlsafe(32)}"
        # Store token in active tokens
        active_admin_tokens.add(token)
        return LoginResponse(success=True, token=token, message="Login successful")
    else:
        return LoginResponse(success=False, message="Invalid credentials")

@router.post("/api/logout")
async def logout(token: str = Depends(verify_admin_token)):
    """Admin logout"""
    # Remove token from active tokens
    active_admin_tokens.discard(token)
    return {"success": True, "message": "Logged out successfully"}

# Token management endpoints
@router.get("/api/tokens")
async def get_tokens(token: str = Depends(verify_admin_token)) -> List[dict]:
    """Get all tokens with statistics"""
    tokens = await token_manager.get_all_tokens()
    result = []

    for token in tokens:
        stats = await db.get_token_stats(token.id)
        result.append({
            "id": token.id,
            "token": token.token,  # 完整的Access Token
            "st": token.st,  # 完整的Session Token
            "rt": token.rt,  # 完整的Refresh Token
            "client_id": token.client_id,  # Client ID
            "proxy_url": token.proxy_url,  # Proxy URL
            "email": token.email,
            "name": token.name,
            "remark": token.remark,
            "expiry_time": token.expiry_time.isoformat() if token.expiry_time else None,
            "is_active": token.is_active,
            "cooled_until": token.cooled_until.isoformat() if token.cooled_until else None,
            "created_at": token.created_at.isoformat() if token.created_at else None,
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "use_count": token.use_count,
            "image_count": stats.image_count if stats else 0,
            "video_count": stats.video_count if stats else 0,
            "error_count": stats.error_count if stats else 0,
            # 订阅信息
            "plan_type": token.plan_type,
            "plan_title": token.plan_title,
            "subscription_end": token.subscription_end.isoformat() if token.subscription_end else None,
            # Sora2信息
            "sora2_supported": token.sora2_supported,
            "sora2_invite_code": token.sora2_invite_code,
            "sora2_redeemed_count": token.sora2_redeemed_count,
            "sora2_total_count": token.sora2_total_count,
            "sora2_remaining_count": token.sora2_remaining_count,
            "sora2_cooldown_until": token.sora2_cooldown_until.isoformat() if token.sora2_cooldown_until else None,
            # 功能开关
            "image_enabled": token.image_enabled,
            "video_enabled": token.video_enabled,
            # 并发限制
            "image_concurrency": token.image_concurrency,
            "video_concurrency": token.video_concurrency
        })

    return result

@router.post("/api/tokens")
async def add_token(request: AddTokenRequest, token: str = Depends(verify_admin_token)):
    """Add a new Access Token"""
    try:
        new_token = await token_manager.add_token(
            token_value=request.token,
            st=request.st,
            rt=request.rt,
            client_id=request.client_id,
            proxy_url=request.proxy_url,
            remark=request.remark,
            update_if_exists=False,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency
        )
        # Initialize concurrency counters for the new token
        if concurrency_manager:
            await concurrency_manager.reset_token(
                new_token.id,
                image_concurrency=request.image_concurrency,
                video_concurrency=request.video_concurrency
            )
        return {"success": True, "message": "Token 添加成功", "token_id": new_token.id}
    except ValueError as e:
        # Token already exists
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"添加 Token 失败: {str(e)}")

@router.post("/api/tokens/st2at")
async def st_to_at(request: ST2ATRequest, token: str = Depends(verify_admin_token)):
    """Convert Session Token to Access Token (only convert, not add to database)"""
    try:
        result = await token_manager.st_to_at(request.st)
        return {
            "success": True,
            "message": "ST converted to AT successfully",
            "access_token": result["access_token"],
            "email": result.get("email"),
            "expires": result.get("expires")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/tokens/rt2at")
async def rt_to_at(request: RT2ATRequest, token: str = Depends(verify_admin_token)):
    """Convert Refresh Token to Access Token (only convert, not add to database)"""
    try:
        result = await token_manager.rt_to_at(request.rt)
        return {
            "success": True,
            "message": "RT converted to AT successfully",
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token"),
            "expires_in": result.get("expires_in")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/api/tokens/{token_id}/status")
async def update_token_status(
    token_id: int,
    request: UpdateTokenStatusRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token status"""
    try:
        await token_manager.update_token_status(token_id, request.is_active)

        # Reset error count when enabling token
        if request.is_active:
            await token_manager.record_success(token_id)

        return {"success": True, "message": "Token status updated"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/tokens/{token_id}/enable")
async def enable_token(token_id: int, token: str = Depends(verify_admin_token)):
    """Enable a token and reset error count"""
    try:
        await token_manager.enable_token(token_id)
        return {"success": True, "message": "Token enabled", "is_active": 1, "error_count": 0}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/tokens/{token_id}/disable")
async def disable_token(token_id: int, token: str = Depends(verify_admin_token)):
    """Disable a token"""
    try:
        await token_manager.disable_token(token_id)
        return {"success": True, "message": "Token disabled", "is_active": 0}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/tokens/{token_id}/test")
async def test_token(token_id: int, token: str = Depends(verify_admin_token)):
    """Test if a token is valid and refresh Sora2 info"""
    try:
        result = await token_manager.test_token(token_id)
        response = {
            "success": True,
            "status": "success" if result["valid"] else "failed",
            "message": result["message"],
            "email": result.get("email"),
            "username": result.get("username")
        }

        # Include Sora2 info if available
        if result.get("valid"):
            response.update({
                "sora2_supported": result.get("sora2_supported"),
                "sora2_invite_code": result.get("sora2_invite_code"),
                "sora2_redeemed_count": result.get("sora2_redeemed_count"),
                "sora2_total_count": result.get("sora2_total_count"),
                "sora2_remaining_count": result.get("sora2_remaining_count")
            })

        return response
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/api/tokens/batch-delete-disabled")
async def batch_delete_disabled_tokens(token: str = Depends(verify_admin_token)):
    """Delete all disabled tokens"""
    try:
        all_tokens = await db.get_all_tokens()
        deleted_count = 0
        
        for t in all_tokens:
            if not t.is_active:
                await token_manager.delete_token(t.id)
                deleted_count += 1
        
        return {
            "success": True,
            "message": f"已删除 {deleted_count} 个禁用账号",
            "deleted_count": deleted_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量删除失败: {str(e)}")


@router.delete("/api/tokens/{token_id}")
async def delete_token(token_id: int, token: str = Depends(verify_admin_token)):
    """Delete a token"""
    try:
        await token_manager.delete_token(token_id)
        return {"success": True, "message": "Token deleted"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/tokens/batch-test")
async def batch_test_tokens(
    only_active: bool = True,
    only_disabled: bool = False,
    token: str = Depends(verify_admin_token)
):
    """Batch test all tokens

    - only_active=True: Test only active tokens, auto-disable 401 tokens
    - only_disabled=True: Test only disabled tokens, auto-enable valid tokens
    - Both False: Test all tokens
    """
    try:
        result = await token_manager.batch_test_tokens(
            only_active=only_active,
            only_disabled=only_disabled
        )
        return {
            "success": True,
            "message": f"测试完成: {result['valid']} 有效, {result['invalid']} 无效, "
                      f"{result['auto_disabled']} 已自动禁用, {result['auto_enabled']} 已自动启用",
            **result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量测试失败: {str(e)}")


@router.post("/api/tokens/batch-enable")
async def batch_enable_tokens(token: str = Depends(verify_admin_token)):
    """Enable all disabled tokens"""
    try:
        all_tokens = await db.get_all_tokens()
        enabled_count = 0
        
        for t in all_tokens:
            if not t.is_active:
                await token_manager.enable_token(t.id)
                enabled_count += 1
        
        return {
            "success": True,
            "message": f"已启用 {enabled_count} 个账号",
            "enabled_count": enabled_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量启用失败: {str(e)}")


@router.post("/api/tokens/batch-disable")
async def batch_disable_tokens(token: str = Depends(verify_admin_token)):
    """Disable all enabled tokens"""
    try:
        all_tokens = await db.get_all_tokens()
        disabled_count = 0
        
        for t in all_tokens:
            if t.is_active:
                await token_manager.disable_token(t.id)
                disabled_count += 1
        
        return {
            "success": True,
            "message": f"已禁用 {disabled_count} 个账号",
            "disabled_count": disabled_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量禁用失败: {str(e)}")


@router.post("/api/tokens/import")
async def import_tokens(request: ImportTokensRequest, token: str = Depends(verify_admin_token)):
    """Import tokens in append mode (update if exists, add if not)"""
    try:
        added_count = 0
        updated_count = 0

        for import_item in request.tokens:
            # Check if token with this email already exists
            existing_token = await db.get_token_by_email(import_item.email)

            if existing_token:
                # Update existing token
                await token_manager.update_token(
                    token_id=existing_token.id,
                    token=import_item.access_token,
                    st=import_item.session_token,
                    rt=import_item.refresh_token,
                    proxy_url=import_item.proxy_url,
                    remark=import_item.remark,
                    image_enabled=import_item.image_enabled,
                    video_enabled=import_item.video_enabled,
                    image_concurrency=import_item.image_concurrency,
                    video_concurrency=import_item.video_concurrency
                )
                # Update active status
                await token_manager.update_token_status(existing_token.id, import_item.is_active)
                # Reset concurrency counters
                if concurrency_manager:
                    await concurrency_manager.reset_token(
                        existing_token.id,
                        image_concurrency=import_item.image_concurrency,
                        video_concurrency=import_item.video_concurrency
                    )
                updated_count += 1
            else:
                # Add new token
                new_token = await token_manager.add_token(
                    token_value=import_item.access_token,
                    st=import_item.session_token,
                    rt=import_item.refresh_token,
                    proxy_url=import_item.proxy_url,
                    remark=import_item.remark,
                    update_if_exists=False,
                    image_enabled=import_item.image_enabled,
                    video_enabled=import_item.video_enabled,
                    image_concurrency=import_item.image_concurrency,
                    video_concurrency=import_item.video_concurrency
                )
                # Set active status
                if not import_item.is_active:
                    await token_manager.disable_token(new_token.id)
                # Initialize concurrency counters
                if concurrency_manager:
                    await concurrency_manager.reset_token(
                        new_token.id,
                        image_concurrency=import_item.image_concurrency,
                        video_concurrency=import_item.video_concurrency
                    )
                added_count += 1

        return {
            "success": True,
            "message": f"Import completed: {added_count} added, {updated_count} updated",
            "added": added_count,
            "updated": updated_count
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {str(e)}")

@router.put("/api/tokens/{token_id}")
async def update_token(
    token_id: int,
    request: UpdateTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token (AT, ST, RT, proxy_url, remark, image_enabled, video_enabled, concurrency limits)"""
    try:
        await token_manager.update_token(
            token_id=token_id,
            token=request.token,
            st=request.st,
            rt=request.rt,
            client_id=request.client_id,
            proxy_url=request.proxy_url,
            remark=request.remark,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency
        )
        # Reset concurrency counters if they were updated
        if concurrency_manager and (request.image_concurrency is not None or request.video_concurrency is not None):
            await concurrency_manager.reset_token(
                token_id,
                image_concurrency=request.image_concurrency if request.image_concurrency is not None else -1,
                video_concurrency=request.video_concurrency if request.video_concurrency is not None else -1
            )
        return {"success": True, "message": "Token updated"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Admin config endpoints
@router.get("/api/admin/config")
async def get_admin_config(token: str = Depends(verify_admin_token)) -> dict:
    """Get admin configuration"""
    admin_config = await db.get_admin_config()
    return {
        "error_ban_threshold": admin_config.error_ban_threshold,
        "api_key": config.api_key,
        "admin_username": config.admin_username,
        "debug_enabled": config.debug_enabled
    }

@router.post("/api/admin/config")
async def update_admin_config(
    request: UpdateAdminConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin configuration"""
    try:
        # Get current admin config to preserve username and password
        current_config = await db.get_admin_config()

        # Update only the error_ban_threshold, preserve username and password
        current_config.error_ban_threshold = request.error_ban_threshold

        await db.update_admin_config(current_config)
        return {"success": True, "message": "Configuration updated"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/admin/password")
async def update_admin_password(
    request: UpdateAdminPasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin password and/or username"""
    try:
        # Verify old password
        if not AuthManager.verify_admin(config.admin_username, request.old_password):
            raise HTTPException(status_code=400, detail="Old password is incorrect")

        # Get current admin config from database
        admin_config = await db.get_admin_config()

        # Update password in database
        admin_config.admin_password = request.new_password

        # Update username if provided
        if request.username:
            admin_config.admin_username = request.username

        # Update in database
        await db.update_admin_config(admin_config)

        # Update in-memory config
        config.set_admin_password_from_db(request.new_password)
        if request.username:
            config.set_admin_username_from_db(request.username)

        # Invalidate all admin tokens (force re-login)
        active_admin_tokens.clear()

        return {"success": True, "message": "Password updated successfully. Please login again."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update password: {str(e)}")

@router.post("/api/admin/apikey")
async def update_api_key(
    request: UpdateAPIKeyRequest,
    token: str = Depends(verify_admin_token)
):
    """Update API key"""
    try:
        # Update in-memory config
        config.api_key = request.new_api_key

        return {"success": True, "message": "API key updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update API key: {str(e)}")

@router.post("/api/admin/debug")
async def update_debug_config(
    request: UpdateDebugConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update debug configuration"""
    try:
        # Update in-memory config
        config.set_debug_enabled(request.enabled)

        status = "enabled" if request.enabled else "disabled"
        return {"success": True, "message": f"Debug mode {status}", "enabled": request.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")

# Proxy config endpoints
@router.get("/api/proxy/config")
async def get_proxy_config(token: str = Depends(verify_admin_token)) -> dict:
    """Get proxy configuration"""
    proxy_config = await proxy_manager.get_proxy_config()
    pool_count = await proxy_manager.get_proxy_pool_count()
    return {
        "proxy_enabled": proxy_config.proxy_enabled,
        "proxy_url": proxy_config.proxy_url,
        "proxy_pool_enabled": proxy_config.proxy_pool_enabled,
        "proxy_pool_count": pool_count
    }

@router.post("/api/proxy/config")
async def update_proxy_config(
    request: UpdateProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration"""
    try:
        await proxy_manager.update_proxy_config(
            request.proxy_enabled, 
            request.proxy_url,
            request.proxy_pool_enabled
        )
        return {"success": True, "message": "Proxy configuration updated"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Watermark-free config endpoints
@router.get("/api/watermark-free/config")
async def get_watermark_free_config(token: str = Depends(verify_admin_token)) -> dict:
    """Get watermark-free mode configuration"""
    config_obj = await db.get_watermark_free_config()
    return {
        "watermark_free_enabled": config_obj.watermark_free_enabled,
        "parse_method": config_obj.parse_method,
        "custom_parse_url": config_obj.custom_parse_url,
        "custom_parse_token": config_obj.custom_parse_token
    }

@router.post("/api/watermark-free/config")
async def update_watermark_free_config(
    request: UpdateWatermarkFreeConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update watermark-free mode configuration"""
    try:
        await db.update_watermark_free_config(
            request.watermark_free_enabled,
            request.parse_method,
            request.custom_parse_url,
            request.custom_parse_token
        )

        # Update in-memory config
        from ..core.config import config
        config.set_watermark_free_enabled(request.watermark_free_enabled)

        return {"success": True, "message": "Watermark-free mode configuration updated"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Cloudflare Solver config endpoints
@router.get("/api/cloudflare/config")
async def get_cloudflare_solver_config(token: str = Depends(verify_admin_token)) -> dict:
    """Get Cloudflare Solver configuration"""
    # Ensure table has a row
    await db.ensure_cloudflare_solver_config_row()
    config_obj = await db.get_cloudflare_solver_config()
    return {
        "success": True,
        "config": {
            "solver_enabled": config_obj.solver_enabled,
            "solver_api_url": config_obj.solver_api_url
        }
    }

@router.post("/api/cloudflare/config")
async def update_cloudflare_solver_config(
    request: UpdateCloudflareSolverConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update Cloudflare Solver configuration"""
    try:
        from ..core.config import config
        
        # Ensure table has a row
        await db.ensure_cloudflare_solver_config_row()
        
        # Update database
        await db.update_cloudflare_solver_config(
            request.solver_enabled,
            request.solver_api_url
        )
        
        # Also update in-memory config for immediate effect
        config.set_cf_enabled(request.solver_enabled)
        if request.solver_api_url:
            config.set_cf_api_url(request.solver_api_url)
        
        return {"success": True, "message": "Cloudflare Solver configuration updated"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Cloudflare State endpoints
@router.get("/api/cloudflare/state")
async def get_cloudflare_state(token: str = Depends(verify_admin_token)) -> dict:
    """Get current Cloudflare credentials state"""
    from ..services.cloudflare_solver import get_cloudflare_state
    cf_state = get_cloudflare_state()
    return {
        "success": True,
        "state": cf_state.get_status()
    }

@router.post("/api/cloudflare/refresh")
async def refresh_cloudflare_credentials(token: str = Depends(verify_admin_token)) -> dict:
    """Manually refresh Cloudflare credentials"""
    import sys
    print("🔄 [API] 收到获取凭据请求", flush=True)
    sys.stdout.flush()
    
    from ..services.cloudflare_solver import solve_cloudflare_challenge, get_cloudflare_state
    from ..core.config import config
    
    print(f"🔄 [API] Solver启用: {config.cf_enabled}, URL: {config.cf_api_url}", flush=True)
    sys.stdout.flush()
    
    # 检查是否启用了 Cloudflare Solver
    if not config.cf_enabled:
        print("⚠️ [API] Solver未启用", flush=True)
        raise HTTPException(status_code=400, detail="Cloudflare Solver 未启用，请先在配置中启用")
    
    if not config.cf_api_url:
        print("⚠️ [API] Solver URL未配置", flush=True)
        raise HTTPException(status_code=400, detail="Cloudflare Solver API 地址未配置")
    
    try:
        print("🔄 [API] 开始调用 solve_cloudflare_challenge", flush=True)
        sys.stdout.flush()
        result = await solve_cloudflare_challenge(force_refresh=True)
        print(f"🔄 [API] solve_cloudflare_challenge 返回: {result is not None}", flush=True)
        sys.stdout.flush()
        if result:
            print("🔄 [API] 获取 cf_state", flush=True)
            sys.stdout.flush()
            cf_state = get_cloudflare_state()
            print("🔄 [API] 调用 get_status()", flush=True)
            sys.stdout.flush()
            status = cf_state.get_status()
            print(f"🔄 [API] get_status() 返回: {status}", flush=True)
            sys.stdout.flush()
            response = {
                "success": True,
                "message": "Cloudflare credentials refreshed successfully",
                "state": status
            }
            print(f"🔄 [API] 准备返回响应", flush=True)
            sys.stdout.flush()
            return response
        else:
            raise HTTPException(status_code=500, detail="CF 凭据获取失败，请检查 Solver 服务")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"❌ [API] 异常: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        raise HTTPException(status_code=500, detail=f"获取凭据失败: {str(e)}")

@router.post("/api/cloudflare/clear")
async def clear_cloudflare_credentials(token: str = Depends(verify_admin_token)) -> dict:
    """Clear Cloudflare credentials"""
    from ..services.cloudflare_solver import get_cloudflare_state
    cf_state = get_cloudflare_state()
    cf_state.clear()
    return {
        "success": True,
        "message": "Cloudflare credentials cleared",
        "state": cf_state.get_status()
    }

# Statistics endpoints
@router.get("/api/stats")
async def get_stats(token: str = Depends(verify_admin_token)):
    """Get system statistics"""
    tokens = await token_manager.get_all_tokens()
    active_tokens = await token_manager.get_active_tokens()

    total_images = 0
    total_videos = 0
    total_errors = 0
    today_images = 0
    today_videos = 0
    today_errors = 0

    for token in tokens:
        stats = await db.get_token_stats(token.id)
        if stats:
            total_images += stats.image_count
            total_videos += stats.video_count
            total_errors += stats.error_count
            today_images += stats.today_image_count
            today_videos += stats.today_video_count
            today_errors += stats.today_error_count

    return {
        "total_tokens": len(tokens),
        "active_tokens": len(active_tokens),
        "total_images": total_images,
        "total_videos": total_videos,
        "today_images": today_images,
        "today_videos": today_videos,
        "total_errors": total_errors,
        "today_errors": today_errors
    }

# Username activation endpoint
@router.post("/api/tokens/{token_id}/activate-username")
async def activate_username(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Activate username for a token (auto-generate and set username if not set)"""
    try:
        # Get token
        token_obj = await db.get_token(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")

        # Get user info to check current username
        print(f"🔍 [activate-username] Getting user info for token {token_id}...")
        try:
            user_info = await token_manager.get_user_info(token_obj.token)
        except Exception as e:
            print(f"❌ [activate-username] Failed to get user info: {str(e)}")
            raise HTTPException(status_code=500, detail=f"获取用户信息失败: {str(e)}")
        
        current_username = user_info.get("username")
        print(f"📋 [activate-username] Current username: {current_username}")

        if current_username:
            return {
                "success": True,
                "message": f"用户名已存在: {current_username}",
                "username": current_username,
                "already_set": True
            }

        # Generate and set random username
        print(f"🔄 [activate-username] Username is null, generating random username...")
        max_attempts = 5
        for attempt in range(max_attempts):
            generated_username = token_manager._generate_random_username()
            print(f"🔄 [activate-username] Attempt {attempt + 1}/{max_attempts}: trying username '{generated_username}'")

            # Check if username is available
            try:
                is_available = await token_manager.check_username_available(token_obj.token, generated_username)
            except Exception as e:
                print(f"❌ [activate-username] Failed to check username availability: {str(e)}")
                if attempt == max_attempts - 1:
                    raise HTTPException(status_code=500, detail=f"检查用户名可用性失败: {str(e)}")
                continue
            
            if is_available:
                # Set the username
                try:
                    result = await token_manager.set_username(token_obj.token, generated_username)
                    print(f"✅ [activate-username] Username set successfully: {generated_username}")
                    return {
                        "success": True,
                        "message": f"用户名设置成功: {generated_username}",
                        "username": generated_username,
                        "already_set": False
                    }
                except Exception as e:
                    print(f"❌ [activate-username] Failed to set username: {str(e)}")
                    if attempt == max_attempts - 1:
                        raise HTTPException(status_code=500, detail=f"用户名设置失败: {str(e)}")
            else:
                print(f"⚠️ [activate-username] Username '{generated_username}' is not available")

        raise HTTPException(status_code=500, detail="无法找到可用的用户名，请稍后重试")

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [activate-username] Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"激活用户名失败: {str(e)}")

# Sora2 endpoints
@router.post("/api/tokens/{token_id}/sora2/activate")
async def activate_sora2(
    token_id: int,
    invite_code: str,
    token: str = Depends(verify_admin_token)
):
    """Activate Sora2 with invite code"""
    try:
        # Get token
        token_obj = await db.get_token(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")

        # Activate Sora2
        result = await token_manager.activate_sora2_invite(token_obj.token, invite_code)

        if result.get("success"):
            # Get new invite code after activation
            sora2_info = await token_manager.get_sora2_invite_code(token_obj.token)

            # Get remaining count
            sora2_remaining_count = 0
            try:
                remaining_info = await token_manager.get_sora2_remaining_count(token_obj.token)
                if remaining_info.get("success"):
                    sora2_remaining_count = remaining_info.get("remaining_count", 0)
            except Exception as e:
                print(f"Failed to get Sora2 remaining count: {e}")

            # Update database
            await db.update_token_sora2(
                token_id,
                supported=True,
                invite_code=sora2_info.get("invite_code"),
                redeemed_count=sora2_info.get("redeemed_count", 0),
                total_count=sora2_info.get("total_count", 0),
                remaining_count=sora2_remaining_count
            )

            return {
                "success": True,
                "message": "Sora2 activated successfully",
                "already_accepted": result.get("already_accepted", False),
                "invite_code": sora2_info.get("invite_code"),
                "redeemed_count": sora2_info.get("redeemed_count", 0),
                "total_count": sora2_info.get("total_count", 0),
                "sora2_remaining_count": sora2_remaining_count
            }
        else:
            return {
                "success": False,
                "message": "Failed to activate Sora2"
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to activate Sora2: {str(e)}")

# Logs endpoints
@router.get("/api/logs")
async def get_logs(limit: int = 100, token: str = Depends(verify_admin_token)):
    """Get recent logs with token email and task progress"""
    logs = await db.get_recent_logs(limit)
    result = []
    for log in logs:
        log_data = {
            "id": log.get("id"),
            "token_id": log.get("token_id"),
            "token_email": log.get("token_email"),
            "token_username": log.get("token_username"),
            "operation": log.get("operation"),
            "status_code": log.get("status_code"),
            "duration": log.get("duration"),
            "created_at": log.get("created_at"),
            "request_body": log.get("request_body"),
            "response_body": log.get("response_body"),
            "task_id": log.get("task_id")
        }

        # If task_id exists and status is in-progress, get task progress
        if log.get("task_id") and log.get("status_code") == -1:
            task = await db.get_task(log.get("task_id"))
            if task:
                log_data["progress"] = task.progress
                log_data["task_status"] = task.status

        result.append(log_data)

    return result

# Cache config endpoints
@router.post("/api/cache/config")
async def update_cache_timeout(
    request: UpdateCacheTimeoutRequest,
    token: str = Depends(verify_admin_token)
):
    """Update cache timeout"""
    try:
        if request.timeout < 60:
            raise HTTPException(status_code=400, detail="Cache timeout must be at least 60 seconds")

        if request.timeout > 86400:
            raise HTTPException(status_code=400, detail="Cache timeout cannot exceed 24 hours (86400 seconds)")

        # Update in-memory config
        config.set_cache_timeout(request.timeout)

        # Update database
        await db.update_cache_config(timeout=request.timeout)

        # Update file cache timeout
        if generation_handler:
            generation_handler.file_cache.set_timeout(request.timeout)

        return {
            "success": True,
            "message": f"Cache timeout updated to {request.timeout} seconds",
            "timeout": request.timeout
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update cache timeout: {str(e)}")

@router.post("/api/cache/base-url")
async def update_cache_base_url(
    request: UpdateCacheBaseUrlRequest,
    token: str = Depends(verify_admin_token)
):
    """Update cache base URL"""
    try:
        # Validate base URL format (optional, can be empty)
        base_url = request.base_url.strip()
        if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise HTTPException(
                status_code=400,
                detail="Base URL must start with http:// or https://"
            )

        # Remove trailing slash
        if base_url:
            base_url = base_url.rstrip('/')

        # Update in-memory config
        config.set_cache_base_url(base_url)

        # Update database
        await db.update_cache_config(base_url=base_url)

        return {
            "success": True,
            "message": f"Cache base URL updated to: {base_url or 'server address'}",
            "base_url": base_url
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update cache base URL: {str(e)}")

@router.get("/api/cache/config")
async def get_cache_config(token: str = Depends(verify_admin_token)):
    """Get cache configuration"""
    return {
        "success": True,
        "config": {
            "enabled": config.cache_enabled,
            "timeout": config.cache_timeout,
            "base_url": config.cache_base_url,  # 返回实际配置的值，可能为空字符串
            "effective_base_url": config.cache_base_url or f"http://{config.server_host}:{config.server_port}"  # 实际生效的值
        }
    }

@router.post("/api/cache/enabled")
async def update_cache_enabled(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache enabled status"""
    try:
        enabled = request.get("enabled", True)

        # Update in-memory config
        config.set_cache_enabled(enabled)

        # Update database
        await db.update_cache_config(enabled=enabled)

        return {
            "success": True,
            "message": f"Cache {'enabled' if enabled else 'disabled'} successfully",
            "enabled": enabled
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update cache enabled status: {str(e)}")

# Generation timeout config endpoints
@router.get("/api/generation/timeout")
async def get_generation_timeout(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    return {
        "success": True,
        "config": {
            "image_timeout": config.image_timeout,
            "video_timeout": config.video_timeout
        }
    }

@router.post("/api/generation/timeout")
async def update_generation_timeout(
    request: UpdateGenerationTimeoutRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    try:
        # Validate timeouts
        if request.image_timeout is not None:
            if request.image_timeout < 60:
                raise HTTPException(status_code=400, detail="Image timeout must be at least 60 seconds")
            if request.image_timeout > 3600:
                raise HTTPException(status_code=400, detail="Image timeout cannot exceed 1 hour (3600 seconds)")

        if request.video_timeout is not None:
            if request.video_timeout < 60:
                raise HTTPException(status_code=400, detail="Video timeout must be at least 60 seconds")
            if request.video_timeout > 7200:
                raise HTTPException(status_code=400, detail="Video timeout cannot exceed 2 hours (7200 seconds)")

        # Update in-memory config
        if request.image_timeout is not None:
            config.set_image_timeout(request.image_timeout)
        if request.video_timeout is not None:
            config.set_video_timeout(request.video_timeout)

        # Update database
        await db.update_generation_config(
            image_timeout=request.image_timeout,
            video_timeout=request.video_timeout
        )

        # Update TokenLock timeout if image timeout was changed
        if request.image_timeout is not None and generation_handler:
            generation_handler.load_balancer.token_lock.set_lock_timeout(config.image_timeout)

        return {
            "success": True,
            "message": "Generation timeout configuration updated",
            "config": {
                "image_timeout": config.image_timeout,
                "video_timeout": config.video_timeout
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update generation timeout: {str(e)}")

# AT auto refresh config endpoints
@router.get("/api/token-refresh/config")
async def get_at_auto_refresh_config(token: str = Depends(verify_admin_token)):
    """Get AT auto refresh configuration"""
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": config.at_auto_refresh_enabled
        }
    }

@router.post("/api/token-refresh/enabled")
async def update_at_auto_refresh_enabled(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update AT auto refresh enabled status"""
    try:
        enabled = request.get("enabled", False)

        # Update in-memory config
        config.set_at_auto_refresh_enabled(enabled)

        # Update database
        await db.update_token_refresh_config(enabled)

        return {
            "success": True,
            "message": f"AT auto refresh {'enabled' if enabled else 'disabled'} successfully",
            "enabled": enabled
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update AT auto refresh enabled status: {str(e)}")

# Character (角色卡) management endpoints
class CharacterUpdateRequest(BaseModel):
    instruction_set: Optional[str] = None
    safety_instruction_set: Optional[str] = None
    visibility: Optional[str] = None

@router.get("/api/characters")
async def list_characters(token: str = Depends(verify_admin_token)):
    """List all characters"""
    try:
        characters = await db.get_all_characters()
        return {
            "success": True,
            "characters": [char.model_dump() for char in characters]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list characters: {str(e)}")

@router.get("/api/characters/by-token/{token_id}")
async def list_characters_by_token(token_id: int, token: str = Depends(verify_admin_token)):
    """List characters for a specific token"""
    try:
        characters = await db.get_characters_by_token_id(token_id)
        return {
            "success": True,
            "characters": [char.model_dump() for char in characters]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list characters: {str(e)}")

@router.get("/api/characters/{cameo_id}")
async def get_character(cameo_id: str, token: str = Depends(verify_admin_token)):
    """Get character by cameo_id"""
    try:
        character = await db.get_character_by_cameo_id(cameo_id)
        if not character:
            raise HTTPException(status_code=404, detail="Character not found")
        return {
            "success": True,
            "character": character.model_dump()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get character: {str(e)}")

@router.post("/api/characters/{cameo_id}/update")
async def update_character_instructions(
    cameo_id: str,
    request: CharacterUpdateRequest,
    token: str = Depends(verify_admin_token)
):
    """Update character instruction_set and safety_instruction_set via Sora API"""
    try:
        # Get character from database to find associated token
        character = await db.get_character_by_cameo_id(cameo_id)
        if not character:
            raise HTTPException(status_code=404, detail="Character not found in database")
        
        # Get the token for this character
        token_obj = await token_manager.get_token_by_id(character.token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Associated token not found")
        
        # Call Sora API to update character
        result = await generation_handler.sora_client.update_character_instructions(
            cameo_id=cameo_id,
            token=token_obj.token,
            instruction_set=request.instruction_set,
            safety_instruction_set=request.safety_instruction_set,
            visibility=request.visibility
        )
        
        # Update local database
        update_fields = {}
        if request.instruction_set is not None:
            update_fields['instruction_set'] = request.instruction_set
        if request.safety_instruction_set is not None:
            update_fields['safety_instruction_set'] = request.safety_instruction_set
        if request.visibility is not None:
            update_fields['visibility'] = request.visibility
        
        if update_fields:
            await db.update_character(cameo_id, **update_fields)
        
        return {
            "success": True,
            "message": "Character updated successfully",
            "result": result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update character: {str(e)}")

@router.delete("/api/characters/{cameo_id}")
async def delete_character(cameo_id: str, token: str = Depends(verify_admin_token)):
    """Delete character from database (does not delete from Sora)"""
    try:
        success = await db.delete_character(cameo_id)
        if not success:
            raise HTTPException(status_code=404, detail="Character not found")
        return {
            "success": True,
            "message": "Character deleted from database"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete character: {str(e)}")

# Token characters endpoint (get characters from Sora API)
@router.get("/api/tokens/{token_id}/sora-characters")
async def get_token_sora_characters(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Get characters from Sora API for a specific token"""
    try:
        # Get the token
        token_obj = await token_manager.get_token_by_id(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")
        
        # Get characters from Sora API (via profile feed or dedicated endpoint if available)
        # For now, we return local database characters
        characters = await db.get_characters_by_token_id(token_id)
        
        return {
            "success": True,
            "token_id": token_id,
            "characters": [char.model_dump() for char in characters]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get characters: {str(e)}")


# Proxy pool file endpoints
@router.get("/api/proxy/pool")
async def get_proxy_pool(token: str = Depends(verify_admin_token)):
    """Get proxy pool content from data/proxy.txt"""
    try:
        import os
        proxy_file = os.path.join("data", "proxy.txt")
        if os.path.exists(proxy_file):
            with open(proxy_file, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = ""
        return {
            "success": True,
            "content": content
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read proxy pool: {str(e)}")

@router.post("/api/proxy/pool")
async def update_proxy_pool(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update proxy pool content in data/proxy.txt"""
    try:
        import os
        content = request.get("content", "")
        proxy_file = os.path.join("data", "proxy.txt")
        
        # Ensure data directory exists
        os.makedirs("data", exist_ok=True)
        
        with open(proxy_file, "w", encoding="utf-8") as f:
            f.write(content)
        
        # Reload proxy pool in proxy manager
        if proxy_manager:
            await proxy_manager.reload_proxy_pool()
        
        return {
            "success": True,
            "message": "Proxy pool updated successfully"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update proxy pool: {str(e)}")


# Proxy pool test endpoints
@router.post("/api/proxy/test")
async def test_all_proxies(
    request: dict = None,
    token: str = Depends(verify_admin_token)
):
    """Test all proxies in the pool
    
    Args:
        remove_invalid: If True, remove invalid proxies from the pool file
    """
    try:
        if not proxy_manager:
            raise HTTPException(status_code=500, detail="Proxy manager not initialized")
        
        remove_invalid = request.get("remove_invalid", False) if request else False
        result = await proxy_manager.test_all_proxies(remove_invalid=remove_invalid)
        
        return {
            "success": True,
            "message": f"测试完成: {result['valid']} 有效, {result['invalid']} 无效" + 
                      (f", 已移除 {result['removed']} 个无效代理" if result['removed'] > 0 else ""),
            **result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to test proxies: {str(e)}")

@router.post("/api/proxy/test-single")
async def test_single_proxy(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Test a single proxy"""
    try:
        if not proxy_manager:
            raise HTTPException(status_code=500, detail="Proxy manager not initialized")
        
        proxy_url = request.get("proxy_url")
        if not proxy_url:
            raise HTTPException(status_code=400, detail="proxy_url is required")
        
        result = await proxy_manager.test_single_proxy(proxy_url)
        
        return {
            "success": True,
            **result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to test proxy: {str(e)}")
