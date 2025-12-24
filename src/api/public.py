"""Public API routes - Sora data access endpoints"""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from typing import Optional
import random
import aiohttp
import base64
from ..core.auth import verify_api_key_header
from ..services.token_manager import TokenManager
from ..services.webdav_manager import WebDAVManager
from ..core.database import Database

router = APIRouter()

# Dependency injection
token_manager: TokenManager = None
db: Database = None
generation_handler = None
webdav_manager: WebDAVManager = None

def set_dependencies(tm: TokenManager, database: Database, gh=None, wm: WebDAVManager = None):
    """Set dependencies"""
    global token_manager, db, generation_handler, webdav_manager
    token_manager = tm
    db = database
    generation_handler = gh
    webdav_manager = wm


# ============================================================
# Stats API Endpoints
# ============================================================

@router.get("/v1/stats")
async def get_public_stats(api_key: str = Depends(verify_api_key_header)):
    """Get system statistics
    
    Returns:
        Token counts and generation statistics
    """
    try:
        tokens = await db.get_all_tokens()
        stats = await db.get_stats()
        
        total_tokens = len(tokens)
        active_tokens = len([t for t in tokens if t.is_active])
        
        return {
            "success": True,
            "stats": {
                "total_tokens": total_tokens,
                "active_tokens": active_tokens,
                "today_images": stats.get("today_images", 0),
                "total_images": stats.get("total_images", 0),
                "today_videos": stats.get("today_videos", 0),
                "total_videos": stats.get("total_videos", 0),
                "today_errors": stats.get("today_errors", 0),
                "total_errors": stats.get("total_errors", 0)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")


@router.get("/v1/invite-codes")
async def get_random_invite_code(api_key: str = Depends(verify_api_key_header)):
    """Get a random invite code from tokens with remaining Sora2 quota
    
    Returns:
        A random invite code from an active token with available Sora2 quota
    """
    try:
        tokens = await db.get_all_tokens()
        
        # Filter tokens that have Sora2 support and remaining quota
        available_tokens = []
        for t in tokens:
            if t.is_active and t.sora2_supported and t.sora2_invite_code:
                remaining = (t.sora2_total_count or 0) - (t.sora2_redeemed_count or 0)
                if remaining > 0:
                    available_tokens.append({
                        "token_id": t.id,
                        "email": t.email,
                        "invite_code": t.sora2_invite_code,
                        "remaining_count": remaining,
                        "total_count": t.sora2_total_count,
                        "redeemed_count": t.sora2_redeemed_count
                    })
        
        if not available_tokens:
            return {
                "success": False,
                "message": "No available invite codes with remaining quota"
            }
        
        # Randomly select one
        selected = random.choice(available_tokens)
        
        return {
            "success": True,
            "invite_code": selected["invite_code"],
            "remaining_count": selected["remaining_count"],
            "total_count": selected["total_count"],
            "email": selected["email"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get invite code: {str(e)}")


# ============================================================
# Public API Endpoints (API Key authentication)
# ============================================================

@router.get("/v1/profiles/{username}")
async def get_user_profile(
    username: str,
    token_id: int = None,
    api_key: str = Depends(verify_api_key_header)
):
    """Get user profile by username via Sora API
    
    Args:
        username: Username to lookup
        token_id: Optional token ID to use (uses first available if not specified)
    
    Returns:
        User profile data
    """
    try:
        # Get a token to use
        if token_id:
            token_obj = await token_manager.get_token_by_id(token_id)
            if not token_obj:
                raise HTTPException(status_code=404, detail="Token not found")
        else:
            tokens = await db.get_all_tokens()
            active_tokens = [t for t in tokens if t.is_active]
            if not active_tokens:
                raise HTTPException(status_code=404, detail="No active tokens available")
            token_obj = active_tokens[0]
        
        # Get profile via Sora API
        result = await generation_handler.sora_client.get_user_profile(username, token_obj.token)
        
        return {
            "success": True,
            "profile": result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get user profile: {str(e)}")


@router.get("/v1/users/{user_id}/feed")
async def get_user_feed(
    user_id: str,
    limit: int = 8,
    cursor: str = None,
    token_id: int = None,
    api_key: str = Depends(verify_api_key_header)
):
    """Get user's published posts by user_id via Sora API
    
    Args:
        user_id: User ID (e.g., user-4qluo8ATzeEsuvCpOUAfAZY0)
        limit: Number of items to fetch (default 8)
        cursor: Pagination cursor for next page
        token_id: Optional token ID to use (uses first available if not specified)
    
    Returns:
        User's feed data with items array and cursor for pagination
    """
    try:
        # Get a token to use
        if token_id:
            token_obj = await token_manager.get_token_by_id(token_id)
            if not token_obj:
                raise HTTPException(status_code=404, detail="Token not found")
        else:
            tokens = await db.get_all_tokens()
            active_tokens = [t for t in tokens if t.is_active]
            if not active_tokens:
                raise HTTPException(status_code=404, detail="No active tokens available")
            token_obj = active_tokens[0]
        
        # Get user feed via Sora API
        result = await generation_handler.sora_client.get_user_feed(user_id, token_obj.token, limit, cursor)
        
        return {
            "success": True,
            "user_id": user_id,
            "feed": result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get user feed: {str(e)}")


@router.get("/v1/characters/search")
async def search_characters(
    username: str,
    intent: str = "users",
    token_id: int = None,
    limit: int = 10,
    api_key: str = Depends(verify_api_key_header)
):
    """Search for characters by username via Sora API
    
    Args:
        username: Username to search for
        intent: Search intent - 'users' for all users, 'cameo' for users that can be used in video generation
        token_id: Optional token ID to use for the search (uses first available if not specified)
        limit: Number of results to return (default 10)
    
    Returns:
        Simplified character search results with essential fields
    """
    try:
        # Validate intent
        if intent not in ["users", "cameo"]:
            raise HTTPException(status_code=400, detail="Invalid intent. Must be 'users' or 'cameo'")
        
        # Get a token to use for the search
        if token_id:
            token_obj = await token_manager.get_token_by_id(token_id)
            if not token_obj:
                raise HTTPException(status_code=404, detail="Token not found")
        else:
            # Use first available active token
            tokens = await db.get_all_tokens()
            active_tokens = [t for t in tokens if t.is_active]
            if not active_tokens:
                raise HTTPException(status_code=404, detail="No active tokens available")
            token_obj = active_tokens[0]
        
        # Search via Sora API
        try:
            result = await generation_handler.sora_client.search_character(username, token_obj.token, limit, intent)
        except Exception as e:
            # If search fails (e.g., no results), return empty results
            return {
                "success": True,
                "query": username,
                "count": 0,
                "results": []
            }
        
        # Extract and simplify the results
        items = result.get("items", [])
        simplified_results = []
        for item in items:
            profile = item.get("profile", {})
            owner = profile.get("owner_profile", {})
            simplified_results.append({
                "user_id": profile.get("user_id"),
                "username": profile.get("username"),
                "display_name": profile.get("display_name"),
                "profile_picture_url": profile.get("profile_picture_url"),
                "permalink": profile.get("permalink"),
                "can_cameo": profile.get("can_cameo"),
                "verified": profile.get("verified"),
                "follower_count": profile.get("follower_count"),
                "token": item.get("token"),  # e.g., "<@ch_693b0192af888191ac8b3af188acebce>"
                "owner": {
                    "user_id": owner.get("user_id") if owner else None,
                    "username": owner.get("username") if owner else None,
                    "display_name": owner.get("display_name") if owner else None
                } if owner else None
            })
        
        return {
            "success": True,
            "query": username,
            "count": len(simplified_results),
            "results": simplified_results
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search characters: {str(e)}")


@router.get("/v1/feed")
async def get_public_feed(
    limit: int = 8,
    cut: str = "nf2_latest",
    cursor: str = None,
    token_id: int = None,
    api_key: str = Depends(verify_api_key_header)
):
    """Get public feed from Sora
    
    Args:
        limit: Number of items to fetch (default 8)
        cut: Feed type - 'nf2_latest' for latest, 'nf2_top' for top posts
        cursor: Pagination cursor for next page
        token_id: Optional token ID to use (uses first available if not specified)
    
    Returns:
        Simplified feed with essential fields
    """
    try:
        # Get a token to use
        if token_id:
            token_obj = await token_manager.get_token_by_id(token_id)
            if not token_obj:
                raise HTTPException(status_code=404, detail="Token not found")
        else:
            tokens = await db.get_all_tokens()
            active_tokens = [t for t in tokens if t.is_active]
            if not active_tokens:
                raise HTTPException(status_code=404, detail="No active tokens available")
            token_obj = active_tokens[0]
        
        # Get feed via Sora API
        result = await generation_handler.sora_client.get_public_feed(token_obj.token, limit, cut, cursor)
        
        # Simplify the response
        items = result.get("items", [])
        simplified_items = []
        for item in items:
            post = item.get("post", {})
            profile = item.get("profile", {})
            attachments = post.get("attachments", [])
            attachment = attachments[0] if attachments else {}
            
            simplified_items.append({
                "id": post.get("id"),
                "text": post.get("text"),
                "permalink": post.get("permalink"),
                "preview_image_url": post.get("preview_image_url"),
                "posted_at": post.get("posted_at"),
                "like_count": post.get("like_count"),
                "view_count": post.get("view_count"),
                "remix_count": post.get("remix_count"),
                "attachment": {
                    "kind": attachment.get("kind"),
                    "url": attachment.get("url"),
                    "downloadable_url": attachment.get("downloadable_url"),
                    "width": attachment.get("width"),
                    "height": attachment.get("height"),
                    "n_frames": attachment.get("n_frames"),
                    "duration_seconds": attachment.get("n_frames", 0) / 30 if attachment.get("n_frames") else None,
                    "thumbnail_url": attachment.get("encodings", {}).get("thumbnail", {}).get("path")
                } if attachment else None,
                "author": {
                    "user_id": profile.get("user_id"),
                    "username": profile.get("username"),
                    "display_name": profile.get("display_name"),
                    "profile_picture_url": profile.get("profile_picture_url"),
                    "permalink": profile.get("permalink"),
                    "verified": profile.get("verified"),
                    "follower_count": profile.get("follower_count")
                }
            })
        
        return {
            "success": True,
            "cut": cut,
            "count": len(simplified_items),
            "cursor": result.get("cursor"),
            "items": simplified_items
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get public feed: {str(e)}")


@router.get("/v1/tokens/{token_id}/profile-feed")
async def get_token_profile_feed(
    token_id: int,
    limit: int = 8,
    api_key: str = Depends(verify_api_key_header)
):
    """Get profile feed (published posts) for a specific token"""
    try:
        # Get the token
        token_obj = await token_manager.get_token_by_id(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")
        
        # Get profile feed from Sora API
        feed = await generation_handler.sora_client.get_profile_feed(token_obj.token, limit=limit)
        
        return {
            "success": True,
            "token_id": token_id,
            "feed": feed
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get profile feed: {str(e)}")


@router.get("/v1/tokens/{token_id}/pending-tasks")
async def get_token_pending_tasks(
    token_id: int,
    api_key: str = Depends(verify_api_key_header)
):
    """Get pending video generation tasks for a specific token (v1)
    
    Args:
        token_id: Token ID to query
    
    Returns:
        List of pending tasks with progress information
    """
    try:
        token_obj = await token_manager.get_token_by_id(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")
        
        tasks = await generation_handler.sora_client.get_pending_tasks(token_obj.token)
        
        return {
            "success": True,
            "token_id": token_id,
            "count": len(tasks),
            "tasks": tasks
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get pending tasks: {str(e)}")


@router.get("/v1/tokens/{token_id}/pending-tasks-v2")
async def get_token_pending_tasks_v2(
    token_id: int,
    api_key: str = Depends(verify_api_key_header)
):
    """Get pending video generation tasks for a specific token (v2)
    
    Args:
        token_id: Token ID to query
    
    Returns:
        List of pending tasks with progress information (v2 format)
    """
    try:
        token_obj = await token_manager.get_token_by_id(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")
        
        tasks = await generation_handler.sora_client.get_pending_tasks_v2(token_obj.token)
        
        return {
            "success": True,
            "token_id": token_id,
            "count": len(tasks),
            "tasks": tasks
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get pending tasks v2: {str(e)}")


@router.get("/v1/test/tokens/{token_id}/pending-tasks")
async def test_get_token_pending_tasks(
    token_id: int,
    api_key: str = Depends(verify_api_key_header)
):
    """[TEST] Get pending video generation tasks for a specific token (v1)
    
    Args:
        token_id: Token ID to query
    
    Returns:
        List of pending tasks with progress information
    """
    try:
        token_obj = await token_manager.get_token_by_id(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")
        
        tasks = await generation_handler.sora_client.get_pending_tasks(token_obj.token)
        
        return {
            "success": True,
            "token_id": token_id,
            "count": len(tasks),
            "tasks": tasks
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get pending tasks: {str(e)}")


@router.get("/v1/test/tokens/{token_id}/pending-tasks-v2")
async def test_get_token_pending_tasks_v2(
    token_id: int,
    api_key: str = Depends(verify_api_key_header)
):
    """[TEST] Get pending video generation tasks for a specific token (v2)
    
    Args:
        token_id: Token ID to query
    
    Returns:
        List of pending tasks with progress information (v2 format)
    """
    try:
        token_obj = await token_manager.get_token_by_id(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")
        
        tasks = await generation_handler.sora_client.get_pending_tasks_v2(token_obj.token)
        
        return {
            "success": True,
            "token_id": token_id,
            "count": len(tasks),
            "tasks": tasks
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get pending tasks v2: {str(e)}")


@router.get("/v1/tokens/{token_id}/tasks/{task_id}")
async def get_task_progress(
    token_id: int,
    task_id: str,
    api_key: str = Depends(verify_api_key_header)
):
    """Get video generation task progress by task ID
    
    Args:
        token_id: Token ID to use for query
        task_id: Task ID (e.g., task_01kcybbj56fp7vctvpmx0drrw1)
    
    Returns:
        Task progress info:
        - id: task ID
        - status: task status (running/completed/failed)
        - prompt: generation prompt
        - title: task title
        - progress_pct: progress percentage (0.0-1.0)
        - generations: list of generated videos
    """
    try:
        token_obj = await token_manager.get_token_by_id(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")
        
        task = await generation_handler.sora_client.get_task_progress(task_id, token_obj.token)
        
        if not task:
            raise HTTPException(status_code=404, detail="Task not found or already completed")
        
        return {
            "success": True,
            "task": task
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get task progress: {str(e)}")


# ============================================================
# WebDAV Proxy Download Endpoint (No authentication required)
# ============================================================

@router.get("/video/{path:path}")
async def proxy_video_download(path: str, redirect: bool = True):
    """Get real download URL from WebDAV server
    
    This endpoint uses WebDAV credentials to get the real download URL (302 redirect target)
    and either redirects the user or returns the URL as JSON.
    
    Args:
        path: Video path on WebDAV server (e.g., task_01kcxp6ezvfh4rhx0v85ejp4rq.mp4)
        redirect: If True (default), redirect to the real URL. If False, return JSON with URL.
    
    Returns:
        302 redirect to real download URL, or JSON with download_url
    
    Example:
        GET /video/task_01kcxp6ezvfh4rhx0v85ejp4rq.mp4
        -> 302 redirect to real download URL
        
        GET /video/task_01kcxp6ezvfh4rhx0v85ejp4rq.mp4?redirect=false
        -> {"success": true, "download_url": "https://..."}
    """
    from fastapi.responses import RedirectResponse
    
    try:
        if not webdav_manager:
            raise HTTPException(status_code=500, detail="WebDAV manager not initialized")
        
        # Get WebDAV config
        config = await webdav_manager.get_config()
        if not config.webdav_enabled:
            raise HTTPException(status_code=503, detail="WebDAV is not enabled")
        
        if not config.webdav_url:
            raise HTTPException(status_code=503, detail="WebDAV URL is not configured")
        
        # Build full WebDAV URL
        upload_path = config.webdav_upload_path or "/video"
        
        # If path doesn't start with upload_path, prepend it
        if not path.startswith(upload_path.lstrip('/')):
            full_path = f"{upload_path.rstrip('/')}/{path}"
        else:
            full_path = f"/{path}"
        
        webdav_url = f"{config.webdav_url.rstrip('/')}{full_path}"
        
        # Create Basic Auth header
        auth_str = f"{config.webdav_username}:{config.webdav_password}"
        auth_bytes = base64.b64encode(auth_str.encode()).decode()
        
        # Request WebDAV URL without following redirects to get the real download URL
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Basic {auth_bytes}"
            }
            # allow_redirects=False to capture the redirect URL
            async with session.get(webdav_url, headers=headers, allow_redirects=False) as response:
                if response.status == 404:
                    raise HTTPException(status_code=404, detail="Video not found")
                if response.status == 401:
                    raise HTTPException(status_code=500, detail="WebDAV authentication failed")
                
                # Check for redirect (302, 301, 307, 308)
                if response.status in (301, 302, 307, 308):
                    download_url = response.headers.get("Location")
                    if download_url:
                        if redirect:
                            return RedirectResponse(url=download_url, status_code=302)
                        else:
                            return {
                                "success": True,
                                "download_url": download_url
                            }
                    else:
                        raise HTTPException(status_code=500, detail="No redirect URL found")
                
                # If no redirect, the WebDAV server returns the file directly
                # In this case, we need to stream it
                if response.status == 200:
                    # For direct file response, we need to stream it
                    async def stream_video():
                        async for chunk in response.content.iter_chunked(65536):
                            yield chunk
                    
                    content_type = "video/mp4"
                    if path.endswith(".webm"):
                        content_type = "video/webm"
                    elif path.endswith(".mov"):
                        content_type = "video/quicktime"
                    
                    filename = path.split("/")[-1]
                    
                    return StreamingResponse(
                        stream_video(),
                        media_type=content_type,
                        headers={
                            "Content-Disposition": f'inline; filename="{filename}"',
                            "Accept-Ranges": "bytes"
                        }
                    )
                
                raise HTTPException(status_code=response.status, detail=f"Failed to fetch video: HTTP {response.status}")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get video URL: {str(e)}")
