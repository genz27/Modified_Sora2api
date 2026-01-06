"""Proxy management module"""
from typing import Optional, List, Dict
from pathlib import Path
import asyncio
import aiohttp
from datetime import datetime
from ..core.database import Database
from ..core.models import ProxyConfig

class ProxyManager:
    """Proxy configuration manager with pool rotation support"""
    
    def __init__(self, db: Database):
        self.db = db
        self._proxy_pool: List[str] = []
        self._pool_index: int = 0
        self._pool_lock = asyncio.Lock()
        self._proxy_file_path = Path(__file__).parent.parent.parent / "data" / "proxy.txt"
        self._proxy_status: Dict[str, dict] = {}  # 代理状态缓存
    
    def _load_proxy_pool(self) -> List[str]:
        """Load proxy list from data/proxy.txt"""
        proxies = []
        if self._proxy_file_path.exists():
            try:
                with open(self._proxy_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            # Convert to standard proxy URL format
                            proxy_url = self._parse_proxy_line(line)
                            if proxy_url:
                                proxies.append(proxy_url)
            except Exception as e:
                print(f"⚠️ Failed to load proxy pool: {e}")
        return proxies
    
    def _parse_proxy_line(self, line: str) -> Optional[str]:
        """Parse proxy line and convert to standard URL format
        
        Supported formats:
        - http://user:pass@host:port
        - https://user:pass@host:port
        - socks5://user:pass@host:port
        - socks5h://user:pass@host:port
        - socks5://host:port:user:pass (协议://IP:端口:用户名:密码)
        - host:port (assumes http, no auth)
        - host:port:user:pass (IP:端口:用户名:密码 format, assumes http)
        """
        line = line.strip()
        if not line:
            return None
        
        # Check if it's a URL format with protocol
        if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
            # Check if it's already in standard format (has @)
            if "@" in line:
                return line
            
            # Handle format: protocol://host:port:user:pass
            # e.g., socks5://38.134.216.107:13611:helX01iJa8:jbMXPCMoja
            try:
                protocol_end = line.index("://") + 3
                protocol = line[:protocol_end]  # e.g., "socks5://"
                rest = line[protocol_end:]  # e.g., "38.134.216.107:13611:helX01iJa8:jbMXPCMoja"
                
                parts = rest.split(":")
                if len(parts) >= 4:
                    host = parts[0]
                    port = parts[1]
                    user = parts[2]
                    # Password might contain colons, join remaining parts
                    password = ":".join(parts[3:])
                    if port.isdigit():
                        return f"{protocol}{user}:{password}@{host}:{port}"
                elif len(parts) == 2:
                    # Just host:port, no auth
                    return line
            except Exception as e:
                print(f"⚠️ Failed to parse proxy URL: {line}, error: {e}")
            
            return line
        
        # Count colons to determine format
        colon_count = line.count(":")
        
        # Format: host:port (exactly 1 colon)
        if colon_count == 1:
            host, port = line.split(":")
            if port.isdigit():
                return f"http://{host}:{port}"
            print(f"⚠️ Invalid proxy format (port not numeric): {line}")
            return None
        
        # Format: host:port:user:pass (3+ colons)
        # Split from the right to handle passwords with colons
        if colon_count >= 3:
            parts = line.split(":")
            if len(parts) >= 4:
                host = parts[0]
                port = parts[1]
                user = parts[2]
                # Password might contain colons, join remaining parts
                password = ":".join(parts[3:])
                if port.isdigit():
                    return f"http://{user}:{password}@{host}:{port}"
        
        # Unknown format
        print(f"⚠️ Unknown proxy format: {line}")
        return None
    
    async def get_proxy_url(self, token_id: Optional[int] = None) -> Optional[str]:
        """Get proxy URL if enabled, with pool rotation support
        
        Args:
            token_id: Optional token ID for token-specific proxy (reserved for future use)
        """
        config = await self.db.get_proxy_config()
        
        if not config.proxy_enabled:
            return None
        
        # If proxy pool is enabled, rotate through proxies
        if config.proxy_pool_enabled:
            async with self._pool_lock:
                # Reload proxy pool if empty
                if not self._proxy_pool:
                    self._proxy_pool = self._load_proxy_pool()
                
                if self._proxy_pool:
                    # Get current proxy and rotate index
                    proxy = self._proxy_pool[self._pool_index]
                    self._pool_index = (self._pool_index + 1) % len(self._proxy_pool)
                    return proxy
                else:
                    # Fallback to single proxy if pool is empty
                    return config.proxy_url if config.proxy_url else None
        
        # Use single proxy
        return config.proxy_url if config.proxy_url else None
    
    async def update_proxy_config(self, enabled: bool, proxy_url: Optional[str], proxy_pool_enabled: bool = False):
        """Update proxy configuration"""
        await self.db.update_proxy_config(enabled, proxy_url, proxy_pool_enabled)
        
        # Reset proxy pool when config changes
        async with self._pool_lock:
            self._proxy_pool = []
            self._pool_index = 0
    
    async def get_proxy_config(self) -> ProxyConfig:
        """Get proxy configuration"""
        return await self.db.get_proxy_config()
    
    async def get_proxy_pool_count(self) -> int:
        """Get the number of proxies in the pool"""
        proxies = self._load_proxy_pool()
        return len(proxies)
    
    async def reload_proxy_pool(self):
        """Force reload proxy pool from file"""
        async with self._pool_lock:
            self._proxy_pool = self._load_proxy_pool()
            self._pool_index = 0
            if self._proxy_pool:
                print(f"✅ Proxy pool reloaded: {len(self._proxy_pool)} proxies")
                # Print first proxy as example (masked for security)
                first_proxy = self._proxy_pool[0]
                if "@" in first_proxy:
                    # Mask credentials
                    parts = first_proxy.split("@")
                    print(f"   Example format: {parts[0][:15]}...@{parts[1]}")
                else:
                    print(f"   Example format: {first_proxy}")
        return len(self._proxy_pool)

    async def test_single_proxy(self, proxy_url: str, timeout: int = 10) -> dict:
        """Test a single proxy by connecting to sora.chatgpt.com
        
        Args:
            proxy_url: Proxy URL to test
            timeout: Request timeout in seconds
            
        Returns:
            dict with valid, latency, error fields
        """
        start_time = datetime.now()
        test_url = "https://sora.chatgpt.com/"
        
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    test_url,
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=True
                ) as response:
                    latency = (datetime.now() - start_time).total_seconds() * 1000
                    # 200 或 403 都表示代理可以连接到目标
                    if response.status in [200, 403]:
                        return {
                            "valid": True,
                            "latency": round(latency, 2),
                            "error": None
                        }
                    else:
                        return {
                            "valid": False,
                            "latency": None,
                            "error": f"HTTP {response.status}"
                        }
        except asyncio.TimeoutError:
            return {
                "valid": False,
                "latency": None,
                "error": "连接超时"
            }
        except aiohttp.ClientProxyConnectionError as e:
            return {
                "valid": False,
                "latency": None,
                "error": f"代理连接失败: {str(e)}"
            }
        except Exception as e:
            return {
                "valid": False,
                "latency": None,
                "error": str(e)
            }

    async def test_all_proxies(self, remove_invalid: bool = False) -> dict:
        """Test all proxies in the pool
        
        Args:
            remove_invalid: If True, remove invalid proxies from the pool file
            
        Returns:
            dict with total, valid, invalid counts and details
        """
        proxies = self._load_proxy_pool()
        if not proxies:
            return {
                "total": 0,
                "valid": 0,
                "invalid": 0,
                "removed": 0,
                "results": []
            }
        
        results = []
        valid_proxies = []
        
        for proxy in proxies:
            result = await self.test_single_proxy(proxy)
            result["proxy"] = self._mask_proxy(proxy)
            result["proxy_full"] = proxy  # 保留完整代理用于后续处理
            results.append(result)
            
            if result["valid"]:
                valid_proxies.append(proxy)
            
            # 更新状态缓存
            self._proxy_status[proxy] = {
                "valid": result["valid"],
                "latency": result["latency"],
                "error": result["error"],
                "tested_at": datetime.now().isoformat()
            }
        
        removed_count = 0
        if remove_invalid and len(valid_proxies) < len(proxies):
            # 重写代理池文件，只保留有效代理
            removed_count = len(proxies) - len(valid_proxies)
            await self._save_proxy_pool(valid_proxies)
            # 重新加载代理池
            await self.reload_proxy_pool()
        
        # 清理结果中的完整代理信息
        for r in results:
            del r["proxy_full"]
        
        return {
            "total": len(proxies),
            "valid": len(valid_proxies),
            "invalid": len(proxies) - len(valid_proxies),
            "removed": removed_count,
            "results": results
        }

    def _mask_proxy(self, proxy_url: str) -> str:
        """Mask proxy credentials for display"""
        if "@" in proxy_url:
            parts = proxy_url.split("@")
            protocol_and_creds = parts[0]
            host_part = parts[1]
            # 只显示协议和主机部分
            if "://" in protocol_and_creds:
                protocol = protocol_and_creds.split("://")[0]
                return f"{protocol}://***@{host_part}"
            return f"***@{host_part}"
        return proxy_url

    async def _save_proxy_pool(self, proxies: List[str]):
        """Save proxy list to file"""
        try:
            with open(self._proxy_file_path, "w", encoding="utf-8") as f:
                f.write("# 代理池配置文件\n")
                f.write("# 每行一个代理地址，支持 HTTP 和 SOCKS5 格式\n")
                f.write("# 以 # 开头的行为注释\n\n")
                for proxy in proxies:
                    f.write(f"{proxy}\n")
            print(f"✅ Proxy pool saved: {len(proxies)} proxies")
        except Exception as e:
            print(f"⚠️ Failed to save proxy pool: {e}")

    def get_proxy_status(self) -> Dict[str, dict]:
        """Get cached proxy status"""
        return self._proxy_status
