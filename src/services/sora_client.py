"""Sora API client module"""
import base64
import io
import time
import random
import string
import re
from typing import Optional, Dict, Any, Tuple
from curl_cffi.requests import AsyncSession
from curl_cffi import CurlMime
from .proxy_manager import ProxyManager
from ..core.config import config
from ..core.logger import debug_logger

class SoraClient:
    """Sora API client with proxy support"""

    def __init__(self, proxy_manager: ProxyManager):
        self.proxy_manager = proxy_manager
        self.base_url = config.sora_base_url
        self.timeout = config.sora_timeout
        # 持久化 session 字典，按 token 分组维护 cookie
        self._sessions: Dict[str, AsyncSession] = {}

    @staticmethod
    def _generate_sentinel_token() -> str:
        """
        生成 openai-sentinel-token
        根据测试文件的逻辑，传入任意随机字符即可
        生成10-20个字符的随机字符串（字母+数字）
        """
        length = random.randint(10, 20)
        random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=length))
        return random_str

    @staticmethod
    def is_storyboard_prompt(prompt: str) -> bool:
        """检测提示词是否为分镜模式格式

        格式: [time]prompt 或 [time]prompt\n[time]prompt
        例如: [5.0s]猫猫从飞机上跳伞 [5.0s]猫猫降落

        Args:
            prompt: 用户输入的提示词

        Returns:
            True if prompt matches storyboard format
        """
        if not prompt:
            return False
        # 匹配格式: [数字s] 或 [数字.数字s]
        pattern = r'\[\d+(?:\.\d+)?s\]'
        matches = re.findall(pattern, prompt)
        # 至少包含一个时间标记才认为是分镜模式
        return len(matches) >= 1

    @staticmethod
    def format_storyboard_prompt(prompt: str) -> str:
        """将分镜格式提示词转换为API所需格式

        输入: 猫猫的奇妙冒险\n[5.0s]猫猫从飞机上跳伞 [5.0s]猫猫降落
        输出: current timeline:\nShot 1:...\n\ninstructions:\n猫猫的奇妙冒险

        Args:
            prompt: 原始分镜格式提示词

        Returns:
            格式化后的API提示词
        """
        # 匹配 [时间]内容 的模式
        pattern = r'\[(\d+(?:\.\d+)?)s\]\s*([^\[]+)'
        matches = re.findall(pattern, prompt)

        if not matches:
            return prompt

        # 提取总述(第一个[时间]之前的内容)
        first_bracket_pos = prompt.find('[')
        instructions = ""
        if first_bracket_pos > 0:
            instructions = prompt[:first_bracket_pos].strip()

        # 格式化分镜
        formatted_shots = []
        for idx, (duration, scene) in enumerate(matches, 1):
            scene = scene.strip()
            shot = f"Shot {idx}:\nduration: {duration}sec\nScene: {scene}"
            formatted_shots.append(shot)

        timeline = "\n\n".join(formatted_shots)

        # 如果有总述,添加instructions部分
        if instructions:
            return f"current timeline:\n{timeline}\n\ninstructions:\n{instructions}"
        else:
            return timeline

    async def _get_session(self, token: str) -> AsyncSession:
        """获取或创建持久化 session，维护 Cloudflare cookie"""
        if token not in self._sessions:
            self._sessions[token] = AsyncSession(impersonate="chrome120")
        return self._sessions[token]

    async def _make_request(self, method: str, endpoint: str, token: str,
                           json_data: Optional[Dict] = None,
                           multipart: Optional[Dict] = None,
                           add_sentinel_token: bool = False,
                           max_retries: int = 3,
                           infinite_retry_429: bool = False) -> Dict[str, Any]:
        """Make HTTP request with proxy support and 429 retry

        Args:
            method: HTTP method (GET/POST)
            endpoint: API endpoint
            token: Access token
            json_data: JSON request body
            multipart: Multipart form data (for file uploads)
            add_sentinel_token: Whether to add openai-sentinel-token header (only for generation requests)
            max_retries: Maximum number of retries for 429 errors (ignored if infinite_retry_429=True)
            infinite_retry_429: If True, retry 429 errors infinitely until success
        """
        import asyncio
        
        proxy_url = await self.proxy_manager.get_proxy_url()

        # 完整的 Chrome 浏览器请求头
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Origin": "https://sora.chatgpt.com",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Referer": "https://sora.chatgpt.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        # 只在生成请求时添加 sentinel token
        if add_sentinel_token:
            headers["openai-sentinel-token"] = self._generate_sentinel_token()

        if not multipart:
            headers["Content-Type"] = "application/json"

        url = f"{self.base_url}{endpoint}"
        
        # 使用持久化 session 维护 cookie
        session = await self._get_session(token)
        
        attempt = 0
        while True:
            # Check if we should stop retrying (only for non-infinite mode)
            if not infinite_retry_429 and attempt > max_retries:
                break
                
            kwargs = {
                "headers": headers,
                "timeout": self.timeout,
            }

            if proxy_url:
                kwargs["proxy"] = proxy_url

            if json_data:
                kwargs["json"] = json_data

            if multipart:
                kwargs["multipart"] = multipart

            # Log request
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=json_data,
                files=multipart,
                proxy=proxy_url
            )

            # Record start time
            start_time = time.time()

            # Make request
            if method == "GET":
                response = await session.get(url, **kwargs)
            elif method == "POST":
                response = await session.post(url, **kwargs)
            else:
                raise ValueError(f"Unsupported method: {method}")

            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            # Parse response
            try:
                response_json = response.json()
            except:
                response_json = None

            # Log response
            debug_logger.log_response(
                status_code=response.status_code,
                headers=dict(response.headers),
                body=response_json if response_json else response.text,
                duration_ms=duration_ms
            )

            # Handle 429 rate limit with retry
            if response.status_code == 429:
                # Check if it's a Cloudflare challenge (fake 429)
                is_cf_challenge = 'cf-mitigated' in response.headers or 'Just a moment' in response.text
                
                if infinite_retry_429 or attempt < max_retries:
                    # Get retry-after header or use exponential backoff
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait_time = int(retry_after)
                        except ValueError:
                            wait_time = min((attempt + 1) * 2, 30)  # Cap at 30 seconds
                    else:
                        wait_time = min((attempt + 1) * 2, 30)  # Exponential backoff, cap at 30s
                    
                    retry_msg = "infinite" if infinite_retry_429 else f"{attempt + 1}/{max_retries}"
                    cf_msg = " (Cloudflare challenge)" if is_cf_challenge else ""
                    print(f"⚠️ 429 Rate limit{cf_msg}, retrying in {wait_time}s (attempt {retry_msg})")
                    debug_logger.log_info(f"429 Rate limit{cf_msg}, waiting {wait_time}s before retry {retry_msg}")
                    await asyncio.sleep(wait_time)
                    attempt += 1
                    continue
                else:
                    error_msg = f"Rate limit exceeded after {max_retries} retries"
                    debug_logger.log_error(
                        error_message=error_msg,
                        status_code=429,
                        response_text=response.text
                    )
                    raise Exception(error_msg)

            # Check status
            if response.status_code not in [200, 201]:
                # Try to extract error message from response JSON
                error_detail = None
                if response_json and isinstance(response_json, dict):
                    error_obj = response_json.get("error", {})
                    if isinstance(error_obj, dict):
                        error_detail = error_obj.get("message")
                
                # Use extracted error message or fall back to raw response
                if error_detail:
                    error_msg = f"{error_detail}"
                else:
                    error_msg = f"API request failed: {response.status_code} - {response.text}"
                
                # Check for non-retryable errors (401, insufficient balance, etc.)
                is_auth_error = response.status_code == 401
                is_balance_error = any(keyword in error_msg.lower() for keyword in [
                    'insufficient', 'balance', 'quota', 'limit exceeded', 'no credits',
                    'out of', 'exhausted', 'remaining', '余额', '次数'
                ])
                
                # Print error to console
                print(f"❌ [SoraClient] {method} {url} failed: {response.status_code}")
                print(f"   Response: {response.text[:500] if response.text else 'No response body'}")
                
                debug_logger.log_error(
                    error_message=error_msg,
                    status_code=response.status_code,
                    response_text=response.text
                )
                
                # Don't retry auth errors or balance errors
                if is_auth_error or is_balance_error:
                    raise Exception(error_msg)
                
                # For other errors in infinite retry mode, retry
                if infinite_retry_429 and response.status_code >= 500:
                    wait_time = min((attempt + 1) * 2, 30)
                    print(f"⚠️ Server error {response.status_code}, retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    attempt += 1
                    continue
                
                raise Exception(error_msg)

            return response_json if response_json else response.json()
    
    async def get_user_info(self, token: str) -> Dict[str, Any]:
        """Get user information"""
        return await self._make_request("GET", "/me", token)

    async def get_profile_feed(self, token: str, limit: int = 8) -> Dict[str, Any]:
        """Get user's profile feed (published posts)

        Args:
            token: Access token
            limit: Number of items to fetch (default 8)

        Returns:
            Profile feed data with items array
        """
        return await self._make_request("GET", f"/project_y/profile_feed/me?limit={limit}&cut=nf2", token)

    async def get_user_profile(self, username: str, token: str) -> Dict[str, Any]:
        """Get user profile by username
        
        Args:
            username: Username to lookup
            token: Access token
            
        Returns:
            User profile data
        """
        return await self._make_request("GET", f"/project_y/profile/username/{username}", token)

    async def get_user_feed(self, user_id: str, token: str, limit: int = 8, cursor: str = None) -> Dict[str, Any]:
        """Get user's published posts by user_id
        
        Args:
            user_id: User ID (e.g., user-4qluo8ATzeEsuvCpOUAfAZY0)
            token: Access token
            limit: Number of items to fetch (default 8)
            cursor: Pagination cursor for next page
            
        Returns:
            User's feed data with items array and cursor
        """
        url = f"/project_y/profile_feed/{user_id}?limit={limit}&cut=nf2"
        if cursor:
            url = f"/project_y/profile_feed/{user_id}?cursor={cursor}&limit={limit}&cut=nf2"
        return await self._make_request("GET", url, token)

    async def search_character(self, username: str, token: str, limit: int = 10, intent: str = "users") -> Dict[str, Any]:
        """Search for character/user by username

        Args:
            username: Username to search for
            token: Access token
            limit: Number of results to return (default 10)
            intent: Search intent - 'users' for all users, 'cameo' for users that can be used in video generation

        Returns:
            Search results with profile information
        """
        return await self._make_request("GET", f"/project_y/profile/search_mentions?username={username}&intent={intent}&limit={limit}", token)

    async def get_public_feed(self, token: str, limit: int = 8, cut: str = "nf2_latest", cursor: str = None) -> Dict[str, Any]:
        """Get public feed (latest or top posts)

        Args:
            token: Access token
            limit: Number of items to fetch (default 8)
            cut: Feed type - 'nf2_latest' for latest, 'nf2_top' for top posts, 'nf2' for default
            cursor: Pagination cursor for next page

        Returns:
            Feed data with items array and cursor for pagination
        """
        url = f"/project_y/feed?limit={limit}&cut={cut}"
        if cursor:
            url = f"/project_y/feed?cursor={cursor}&limit={limit}&cut={cut}"
        return await self._make_request("GET", url, token)
    
    async def upload_image(self, image_data: bytes, token: str, filename: str = "image.png") -> str:
        """Upload image and return media_id

        使用 CurlMime 对象上传文件（curl_cffi 的正确方式）
        参考：https://curl-cffi.readthedocs.io/en/latest/quick_start.html#uploads
        """
        # 检测图片类型
        mime_type = "image/png"
        if filename.lower().endswith('.jpg') or filename.lower().endswith('.jpeg'):
            mime_type = "image/jpeg"
        elif filename.lower().endswith('.webp'):
            mime_type = "image/webp"

        # 创建 CurlMime 对象
        mp = CurlMime()

        # 添加文件部分
        mp.addpart(
            name="file",
            content_type=mime_type,
            filename=filename,
            data=image_data
        )

        # 添加文件名字段
        mp.addpart(
            name="file_name",
            data=filename.encode('utf-8')
        )

        result = await self._make_request("POST", "/uploads", token, multipart=mp)
        return result["id"]
    
    async def generate_image(self, prompt: str, token: str, width: int = 360,
                            height: int = 360, media_id: Optional[str] = None) -> str:
        """Generate image (text-to-image or image-to-image)"""
        operation = "remix" if media_id else "simple_compose"

        inpaint_items = []
        if media_id:
            inpaint_items = [{
                "type": "image",
                "frame_index": 0,
                "upload_media_id": media_id
            }]

        json_data = {
            "type": "image_gen",
            "operation": operation,
            "prompt": prompt,
            "width": width,
            "height": height,
            "n_variants": 1,
            "n_frames": 1,
            "inpaint_items": inpaint_items
        }

        # 生成请求需要添加 sentinel token，429 无限重试
        result = await self._make_request("POST", "/video_gen", token, json_data=json_data, add_sentinel_token=True, infinite_retry_429=True)
        return result["id"]
    
    async def generate_video(self, prompt: str, token: str, orientation: str = "landscape",
                            media_id: Optional[str] = None, n_frames: int = 450,
                            style_id: Optional[str] = None) -> str:
        """Generate video (text-to-video or image-to-video)
        
        Args:
            prompt: Generation prompt
            token: Access token
            orientation: Video orientation (portrait/landscape)
            media_id: Optional media ID for image-to-video
            n_frames: Number of frames (150=5s, 300=10s, 450=15s, 600=20s)
            style_id: Optional style ID (festive, retro, news, selfie, handheld, anime, comic, golden, vintage)
        """
        inpaint_items = []
        if media_id:
            inpaint_items = [{
                "kind": "upload",
                "upload_id": media_id
            }]

        json_data = {
            "kind": "video",
            "prompt": prompt,
            "orientation": orientation,
            "size": "small",
            "n_frames": n_frames,
            "model": "sy_8",
            "inpaint_items": inpaint_items
        }
        
        # Add style_id if provided
        if style_id:
            json_data["style_id"] = style_id.lower()

        # 生成请求需要添加 sentinel token，429 无限重试
        result = await self._make_request("POST", "/nf/create", token, json_data=json_data, add_sentinel_token=True, infinite_retry_429=True)
        return result["id"]
    
    async def get_image_tasks(self, token: str, limit: int = 20) -> Dict[str, Any]:
        """Get recent image generation tasks"""
        return await self._make_request("GET", f"/v2/recent_tasks?limit={limit}", token)
    
    async def get_video_drafts(self, token: str, limit: int = 15) -> Dict[str, Any]:
        """Get recent video drafts"""
        return await self._make_request("GET", f"/project_y/profile/drafts?limit={limit}", token)

    async def get_pending_tasks(self, token: str) -> list:
        """Get pending video generation tasks (v1)

        Returns:
            List of pending tasks with progress information
        """
        result = await self._make_request("GET", "/nf/pending", token)
        # The API returns a list directly
        return result if isinstance(result, list) else []

    async def get_pending_tasks_v2(self, token: str) -> list:
        """Get pending video generation tasks (v2)

        Returns:
            List of pending tasks with progress information
        """
        result = await self._make_request("GET", "/nf/pending/v2", token)
        # The API returns a list directly
        return result if isinstance(result, list) else []

    async def get_task_progress(self, task_id: str, token: str) -> Optional[Dict[str, Any]]:
        """Get video generation task progress by task ID

        Args:
            task_id: Task ID (e.g., task_01kcybbj56fp7vctvpmx0drrw1)
            token: Access token

        Returns:
            Task progress info with fields:
            - id: task ID
            - status: task status (running/completed/failed)
            - prompt: generation prompt
            - title: task title
            - progress_pct: progress percentage (0.0-1.0)
            - generations: list of generated videos
            Returns None if task not found
        """
        pending_tasks = await self.get_pending_tasks_v2(token)
        for task in pending_tasks:
            if task.get("id") == task_id:
                return task
        return None

    async def post_video_for_watermark_free(self, generation_id: str, prompt: str, token: str) -> str:
        """Post video to get watermark-free version

        Args:
            generation_id: The generation ID (e.g., gen_01k9btrqrnen792yvt703dp0tq)
            prompt: The original generation prompt
            token: Access token

        Returns:
            Post ID (e.g., s_690ce161c2488191a3476e9969911522)
        """
        json_data = {
            "attachments_to_create": [
                {
                    "generation_id": generation_id,
                    "kind": "sora"
                }
            ],
            "post_text": ""
        }

        # 发布请求需要添加 sentinel token
        result = await self._make_request("POST", "/project_y/post", token, json_data=json_data, add_sentinel_token=True)

        # 返回 post.id
        return result.get("post", {}).get("id", "")

    async def delete_post(self, post_id: str, token: str) -> bool:
        """Delete a published post

        Args:
            post_id: The post ID (e.g., s_690ce161c2488191a3476e9969911522)
            token: Access token

        Returns:
            True if deletion was successful
        """
        proxy_url = await self.proxy_manager.get_proxy_url()

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Origin": "https://sora.chatgpt.com",
            "Pragma": "no-cache",
            "Referer": "https://sora.chatgpt.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        session = await self._get_session(token)
        url = f"{self.base_url}/project_y/post/{post_id}"

        kwargs = {
            "headers": headers,
            "timeout": self.timeout,
        }

        if proxy_url:
            kwargs["proxy"] = proxy_url

        # Log request
        debug_logger.log_request(
            method="DELETE",
            url=url,
            headers=headers,
            body=None,
            files=None,
            proxy=proxy_url
        )

        # Record start time
        start_time = time.time()

        # Make DELETE request
        response = await session.delete(url, **kwargs)

        # Calculate duration
        duration_ms = (time.time() - start_time) * 1000

        # Log response
        debug_logger.log_response(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response.text if response.text else "No content",
            duration_ms=duration_ms
        )

        # Check status (DELETE typically returns 204 No Content or 200 OK)
        if response.status_code not in [200, 204]:
            error_msg = f"Delete post failed: {response.status_code} - {response.text}"
            debug_logger.log_error(
                error_message=error_msg,
                status_code=response.status_code,
                response_text=response.text
            )
            raise Exception(error_msg)

        return True

    async def get_watermark_free_url_custom(self, parse_url: str, parse_token: str, post_id: str) -> str:
        """Get watermark-free video URL from custom parse server

        Args:
            parse_url: Custom parse server URL (e.g., http://example.com)
            parse_token: Access token for custom parse server
            post_id: Post ID to parse (e.g., s_690c0f574c3881918c3bc5b682a7e9fd)

        Returns:
            Download link from custom parse server

        Raises:
            Exception: If parse fails or token is invalid
        """
        proxy_url = await self.proxy_manager.get_proxy_url()

        # Construct the share URL
        share_url = f"https://sora.chatgpt.com/p/{post_id}"

        # Prepare request
        json_data = {
            "url": share_url,
            "token": parse_token
        }

        kwargs = {
            "json": json_data,
            "timeout": 30,
            "impersonate": "chrome"
        }

        if proxy_url:
            kwargs["proxy"] = proxy_url

        try:
            async with AsyncSession() as session:
                # Record start time
                start_time = time.time()

                # Make POST request to custom parse server
                response = await session.post(f"{parse_url}/get-sora-link", **kwargs)

                # Calculate duration
                duration_ms = (time.time() - start_time) * 1000

                # Log response
                debug_logger.log_response(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=response.text if response.text else "No content",
                    duration_ms=duration_ms
                )

                # Check status
                if response.status_code != 200:
                    error_msg = f"Custom parse failed: {response.status_code} - {response.text}"
                    debug_logger.log_error(
                        error_message=error_msg,
                        status_code=response.status_code,
                        response_text=response.text
                    )
                    raise Exception(error_msg)

                # Parse response
                result = response.json()

                # Check for error in response
                if "error" in result:
                    error_msg = f"Custom parse error: {result['error']}"
                    debug_logger.log_error(
                        error_message=error_msg,
                        status_code=401,
                        response_text=str(result)
                    )
                    raise Exception(error_msg)

                # Extract download link
                download_link = result.get("download_link")
                if not download_link:
                    raise Exception("No download_link in custom parse response")

                debug_logger.log_info(f"Custom parse successful: {download_link}")
                return download_link

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Custom parse request failed: {str(e)}",
                status_code=500,
                response_text=str(e)
            )
            raise

    # ==================== Character Creation Methods ====================

    async def upload_character_video(self, video_data: bytes, token: str, timestamps: str = None) -> str:
        """Upload character video and return cameo_id

        Args:
            video_data: Video file bytes
            token: Access token
            timestamps: Optional custom timestamps (e.g., "0,3" or "1,5"), defaults to "0,3"

        Returns:
            cameo_id
        """
        mp = CurlMime()
        mp.addpart(
            name="file",
            content_type="video/mp4",
            filename="video.mp4",
            data=video_data
        )
        # Use custom timestamps if provided, otherwise default to "0,3"
        ts_value = timestamps if timestamps else "0,3"
        mp.addpart(
            name="timestamps",
            data=ts_value.encode('utf-8')
        )

        result = await self._make_request("POST", "/characters/upload", token, multipart=mp)
        return result.get("id")

    async def get_cameo_status(self, cameo_id: str, token: str) -> Dict[str, Any]:
        """Get character (cameo) processing status

        Args:
            cameo_id: The cameo ID returned from upload_character_video
            token: Access token

        Returns:
            Dictionary with status, display_name_hint, username_hint, profile_asset_url, instruction_set_hint
        """
        return await self._make_request("GET", f"/project_y/cameos/in_progress/{cameo_id}", token)

    async def download_character_image(self, image_url: str) -> bytes:
        """Download character image from URL

        Args:
            image_url: The profile_asset_url from cameo status

        Returns:
            Image file bytes
        """
        proxy_url = await self.proxy_manager.get_proxy_url()

        kwargs = {
            "timeout": self.timeout,
            "impersonate": "chrome"
        }

        if proxy_url:
            kwargs["proxy"] = proxy_url

        async with AsyncSession() as session:
            response = await session.get(image_url, **kwargs)
            if response.status_code != 200:
                raise Exception(f"Failed to download image: {response.status_code}")
            return response.content

    async def finalize_character(self, cameo_id: str, username: str, display_name: str,
                                profile_asset_pointer: str, instruction_set, token: str,
                                safety_instruction_set: str = None) -> str:
        """Finalize character creation

        Args:
            cameo_id: The cameo ID
            username: Character username
            display_name: Character display name
            profile_asset_pointer: Asset pointer from upload_character_image
            instruction_set: Character instruction set text (optional, will be wrapped in proper format)
            token: Access token
            safety_instruction_set: Safety instruction set text (optional, will be wrapped in proper format)

        Returns:
            character_id
        """
        # Format instruction_set if provided
        formatted_instruction_set = None
        if instruction_set:
            formatted_instruction_set = {
                "value": [{"type": "text", "value": instruction_set}]
            }
        
        # Format safety_instruction_set if provided
        formatted_safety_instruction_set = None
        if safety_instruction_set:
            formatted_safety_instruction_set = {
                "value": [{"type": "text", "value": safety_instruction_set}]
            }
        
        json_data = {
            "cameo_id": cameo_id,
            "username": username,
            "display_name": display_name,
            "profile_asset_pointer": profile_asset_pointer,
            "instruction_set": formatted_instruction_set,
            "safety_instruction_set": formatted_safety_instruction_set
        }

        result = await self._make_request("POST", "/characters/finalize", token, json_data=json_data)
        return result.get("character", {}).get("character_id")

    async def check_username_available(self, username: str, token: str) -> bool:
        """Check if username is available

        Args:
            username: Username to check
            token: Access token

        Returns:
            True if username is available, False otherwise
        """
        json_data = {"username": username}
        result = await self._make_request("POST", "/project_y/profile/username/check", token, json_data=json_data)
        return result.get("available", False)

    async def set_character_public(self, cameo_id: str, token: str) -> bool:
        """Set character as public

        Args:
            cameo_id: The cameo ID
            token: Access token

        Returns:
            True if successful
        """
        json_data = {"visibility": "public"}
        await self._make_request("POST", f"/project_y/cameos/by_id/{cameo_id}/update_v2", token, json_data=json_data)
        return True

    async def update_character_instructions(self, cameo_id: str, token: str,
                                           instruction_set: str = None,
                                           safety_instruction_set: str = None,
                                           visibility: str = None) -> Dict[str, Any]:
        """Update character instruction_set and safety_instruction_set

        Args:
            cameo_id: The cameo ID
            token: Access token
            instruction_set: Instruction set text (will be wrapped in proper format)
            safety_instruction_set: Safety instruction set text (will be wrapped in proper format)
            visibility: Visibility setting (public/private)

        Returns:
            Updated character info from API
        """
        json_data = {}
        
        if visibility:
            json_data["visibility"] = visibility
        
        if instruction_set is not None:
            json_data["instruction_set"] = {
                "value": [{"type": "text", "value": instruction_set}]
            }
        
        if safety_instruction_set is not None:
            json_data["safety_instruction_set"] = {
                "value": [{"type": "text", "value": safety_instruction_set}]
            }
        
        result = await self._make_request("POST", f"/project_y/cameos/by_id/{cameo_id}/update_v2", token, json_data=json_data)
        return result

    async def upload_character_image(self, image_data: bytes, token: str) -> str:
        """Upload character image and return asset_pointer

        Args:
            image_data: Image file bytes
            token: Access token

        Returns:
            asset_pointer
        """
        mp = CurlMime()
        mp.addpart(
            name="file",
            content_type="image/webp",
            filename="profile.webp",
            data=image_data
        )
        mp.addpart(
            name="use_case",
            data=b"profile"
        )

        result = await self._make_request("POST", "/project_y/file/upload", token, multipart=mp)
        return result.get("asset_pointer")

    async def delete_character(self, character_id: str, token: str) -> bool:
        """Delete a character

        Args:
            character_id: The character ID
            token: Access token

        Returns:
            True if successful
        """
        proxy_url = await self.proxy_manager.get_proxy_url()

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Origin": "https://sora.chatgpt.com",
            "Pragma": "no-cache",
            "Referer": "https://sora.chatgpt.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        session = await self._get_session(token)
        url = f"{self.base_url}/project_y/characters/{character_id}"

        kwargs = {
            "headers": headers,
            "timeout": self.timeout,
        }

        if proxy_url:
            kwargs["proxy"] = proxy_url

        response = await session.delete(url, **kwargs)
        if response.status_code not in [200, 204]:
            raise Exception(f"Failed to delete character: {response.status_code}")
        return True

    async def remix_video(self, remix_target_id: str, prompt: str, token: str,
                         orientation: str = "portrait", n_frames: int = 450) -> str:
        """Generate video using remix (based on existing video)

        Args:
            remix_target_id: The video ID from Sora share link (e.g., s_690d100857248191b679e6de12db840e)
            prompt: Generation prompt
            token: Access token
            orientation: Video orientation (portrait/landscape)
            n_frames: Number of frames

        Returns:
            task_id
        """
        json_data = {
            "kind": "video",
            "prompt": prompt,
            "inpaint_items": [],
            "remix_target_id": remix_target_id,
            "cameo_ids": [],
            "cameo_replacements": {},
            "model": "sy_8",
            "orientation": orientation,
            "n_frames": n_frames
        }

        result = await self._make_request("POST", "/nf/create", token, json_data=json_data, add_sentinel_token=True, infinite_retry_429=True)
        return result.get("id")

    async def generate_storyboard(self, prompt: str, token: str, orientation: str = "landscape",
                                 media_id: Optional[str] = None, n_frames: int = 450) -> str:
        """Generate video using storyboard mode

        Args:
            prompt: Formatted storyboard prompt (Shot 1:\nduration: 5.0sec\nScene: ...)
            token: Access token
            orientation: Video orientation (portrait/landscape)
            media_id: Optional image media_id for image-to-video
            n_frames: Number of frames

        Returns:
            task_id
        """
        inpaint_items = []
        if media_id:
            inpaint_items = [{
                "kind": "upload",
                "upload_id": media_id
            }]

        json_data = {
            "kind": "video",
            "prompt": prompt,
            "title": "Draft your video",
            "orientation": orientation,
            "size": "small",
            "n_frames": n_frames,
            "storyboard_id": None,
            "inpaint_items": inpaint_items,
            "remix_target_id": None,
            "model": "sy_8",
            "metadata": None,
            "style_id": None,
            "cameo_ids": None,
            "cameo_replacements": None,
            "audio_caption": None,
            "audio_transcript": None,
            "video_caption": None
        }

        result = await self._make_request("POST", "/nf/create/storyboard", token, json_data=json_data, add_sentinel_token=True, infinite_retry_429=True)
        return result.get("id")
