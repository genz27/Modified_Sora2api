"""HTTP utilities - Common HTTP headers and request helpers"""
from typing import Optional

# Chrome Mobile 浏览器请求头模板 (iPhone)
CHROME_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Origin": "https://sora.chatgpt.com",
    "Pragma": "no-cache",
    "Priority": "u=1, i",
    "Referer": "https://sora.chatgpt.com/",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?1",
    "Sec-Ch-Ua-Platform": '"iOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# iPhone 15 Pro Max + Chrome 131 Mobile UA
DEFAULT_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1"


def build_sora_headers(
    token: str,
    user_agent: Optional[str] = None,
    content_type: Optional[str] = None,
    sentinel_token: Optional[str] = None
) -> dict:
    """构建 Sora API 请求头
    
    Args:
        token: Access token
        user_agent: 自定义 User-Agent
        content_type: Content-Type (默认 application/json)
        sentinel_token: openai-sentinel-token (仅生成请求需要)
    
    Returns:
        完整的请求头字典
    """
    headers = {
        **CHROME_HEADERS,
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
    }
    
    if content_type:
        headers["Content-Type"] = content_type
    
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    
    return headers


def build_simple_headers(token: str) -> dict:
    """构建简单的 API 请求头
    
    Args:
        token: Access token
    
    Returns:
        简单请求头字典
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Origin": "https://sora.chatgpt.com",
        "Referer": "https://sora.chatgpt.com/"
    }
