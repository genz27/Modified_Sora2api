"""HTTP utilities - Common HTTP headers and request helpers"""
import random
from typing import Optional

# 手机指纹列表（curl_cffi 只支持这些手机指纹）
MOBILE_FINGERPRINTS = [
    "safari17_2_ios",
    "safari18_0_ios",
]

# 手机 UA 列表 (20个)
MOBILE_USER_AGENTS = [
    # iPhone Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    # iPhone Chrome
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/130.0.6723.90 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/129.0.6668.69 Mobile/15E148 Safari/604.1",
    # iPad Safari
    "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    # Android Chrome
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.81 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.102 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.81 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.102 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.100 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-A536B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.127 Mobile Safari/537.36",
    # Android Samsung Browser
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.6167.178 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/24.0 Chrome/117.0.5938.60 Mobile Safari/537.36",
    # Xiaomi
    "Mozilla/5.0 (Linux; Android 14; 2312DRA50G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.81 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; 22081212G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.102 Mobile Safari/537.36",
]

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

# 默认 UA (从列表中随机选择)
DEFAULT_USER_AGENT = MOBILE_USER_AGENTS[0]


def get_random_fingerprint() -> str:
    """获取随机手机指纹"""
    return random.choice(MOBILE_FINGERPRINTS)


def get_random_user_agent() -> str:
    """获取随机手机 UA"""
    return random.choice(MOBILE_USER_AGENTS)


def build_sora_headers(
    token: str,
    user_agent: Optional[str] = None,
    content_type: Optional[str] = None,
    sentinel_token: Optional[str] = None
) -> dict:
    """构建 Sora API 请求头
    
    Args:
        token: Access token
        user_agent: 自定义 User-Agent (默认随机选择)
        content_type: Content-Type (默认 application/json)
        sentinel_token: openai-sentinel-token (仅生成请求需要)
    
    Returns:
        完整的请求头字典
    """
    headers = {
        **CHROME_HEADERS,
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent or get_random_user_agent(),
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
        "Referer": "https://sora.chatgpt.com/",
        "User-Agent": get_random_user_agent(),
    }
