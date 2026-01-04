"""Database storage layer"""
import aiosqlite
import asyncio
import json
from datetime import datetime
from typing import Optional, List
from pathlib import Path
from contextlib import asynccontextmanager
from .models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, WatermarkFreeConfig, CacheConfig, GenerationConfig, TokenRefreshConfig, CloudflareSolverConfig, Character, WebDAVConfig, VideoRecord, UploadLog
from .db_pool import get_db_connection
from .config import config

class Database:
    """Database manager with support for SQLite and MySQL"""

    def __init__(self, db_path: str = None):
        # Determine database type from config
        self.db_type = config.db_type.lower()
        
        if self.db_type == "mysql":
            self.db_path = None
            self._mysql_pool = None
        else:
            # SQLite
            if db_path is None:
                data_dir = Path(__file__).parent.parent.parent / "data"
                data_dir.mkdir(exist_ok=True)
                # Use config path or default
                sqlite_path = config.sqlite_path if config.sqlite_path else "hancat.db"
                # If path is relative, make it relative to project root
                if not Path(sqlite_path).is_absolute():
                    db_path = str(Path(__file__).parent.parent.parent / sqlite_path)
                else:
                    db_path = sqlite_path
            self.db_path = db_path
        
        self._write_lock = asyncio.Lock()  # 写操作锁

    def db_exists(self) -> bool:
        """Check if database file exists (SQLite only)"""
        if self.db_type == "mysql":
            return True  # MySQL database should be created externally
        return Path(self.db_path).exists()

    async def _get_mysql_pool(self):
        """Get or create MySQL connection pool"""
        if self._mysql_pool is None:
            import aiomysql
            self._mysql_pool = await aiomysql.create_pool(
                host=config.mysql_host,
                port=config.mysql_port,
                user=config.mysql_user,
                password=config.mysql_password,
                db=config.mysql_database,
                minsize=5,
                maxsize=config.mysql_pool_size,
                autocommit=False,
                charset='utf8mb4',
                cursorclass=aiomysql.DictCursor,
                connect_timeout=60
            )
            print(f"✅ MySQL pool initialized (host: {config.mysql_host}, pool_size: {config.mysql_pool_size})")
        return self._mysql_pool

    @asynccontextmanager
    async def _connect(self, readonly: bool = False):
        """Get a database connection with proper settings (context manager)"""
        if self.db_type == "mysql":
            import aiomysql
            pool = await self._get_mysql_pool()
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    yield _MySQLConnectionWrapper(conn, cursor)
        else:
            # SQLite with high concurrency optimizations
            conn = await aiosqlite.connect(self.db_path, timeout=60.0)
            try:
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA busy_timeout=60000")
                await conn.execute("PRAGMA cache_size=-64000")
                await conn.execute("PRAGMA mmap_size=268435456")
                await conn.execute("PRAGMA temp_store=MEMORY")
                if readonly:
                    await conn.execute("PRAGMA query_only=ON")
                conn.row_factory = aiosqlite.Row
                yield conn
            finally:
                await conn.close()

    async def _get_connection(self, readonly: bool = False):
        """Get a database connection with proper settings (non-context manager)"""
        if self.db_type == "mysql":
            import aiomysql
            pool = await self._get_mysql_pool()
            conn = await pool.acquire()
            return _MySQLConnectionWrapper(conn, await conn.cursor(aiomysql.DictCursor), pool=pool)
        else:
            conn = await aiosqlite.connect(self.db_path, timeout=60.0)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout=60000")
            await conn.execute("PRAGMA cache_size=-64000")
            await conn.execute("PRAGMA mmap_size=268435456")
            await conn.execute("PRAGMA temp_store=MEMORY")
            if readonly:
                await conn.execute("PRAGMA query_only=ON")
            conn.row_factory = aiosqlite.Row
            return conn


class _MySQLConnectionWrapper:
    """Wrapper to make MySQL cursor behave like SQLite connection"""
    
    def __init__(self, conn, cursor, pool=None):
        self._conn = conn
        self._cursor = cursor
        self._pool = pool
        self.row_factory = None
    
    async def execute(self, sql: str, params: tuple = None):
        # Convert SQLite placeholders to MySQL
        sql = sql.replace("?", "%s")
        # Handle SQLite-specific syntax
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "INT AUTO_INCREMENT PRIMARY KEY")
        sql = sql.replace("AUTOINCREMENT", "AUTO_INCREMENT")
        sql = sql.replace("INSERT OR IGNORE", "INSERT IGNORE")
        
        if params:
            await self._cursor.execute(sql, params)
        else:
            await self._cursor.execute(sql)
        return self._cursor
    
    async def executemany(self, sql: str, params_list: list):
        sql = sql.replace("?", "%s")
        await self._cursor.executemany(sql, params_list)
    
    async def commit(self):
        await self._conn.commit()
    
    async def close(self):
        await self._cursor.close()
        if self._pool:
            self._pool.release(self._conn)
        else:
            self._conn.close()
    
    async def fetchone(self):
        return await self._cursor.fetchone()
    
    async def fetchall(self):
        return await self._cursor.fetchall()
    
    @property
    def lastrowid(self):
        return self._cursor.lastrowid


# Continue with the rest of the Database class methods...

    async def _execute_with_retry(self, operation, max_retries: int = 5):
        """Execute database operation with retry logic for locked database
        
        Args:
            operation: Async function that takes a connection and performs DB operations
            max_retries: Maximum retry attempts (default 5 for high concurrency)
        """
        for attempt in range(max_retries):
            try:
                async with self._write_lock:
                    async with self._connect() as conn:
                        result = await operation(conn)
                        await conn.commit()
                        return result
            except aiosqlite.OperationalError as e:
                error_msg = str(e).lower()
                if ("database is locked" in error_msg or "unable to open" in error_msg) and attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 0.2 + (attempt * 0.1)  # 递增等待
                    print(f"⚠️ Database error: {e}, retrying in {wait_time:.1f}s... (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                else:
                    raise

    async def _table_exists(self, db, table_name: str) -> bool:
        """Check if a table exists in the database"""
        if self.db_type == "mysql":
            cursor = await db.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (config.mysql_database, table_name)
            )
        else:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
        result = await cursor.fetchone()
        return result is not None

    async def _column_exists(self, db, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        try:
            if self.db_type == "mysql":
                cursor = await db.execute(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                    (config.mysql_database, table_name, column_name)
                )
                result = await cursor.fetchone()
                return result is not None
            else:
                cursor = await db.execute(f"PRAGMA table_info({table_name})")
                columns = await cursor.fetchall()
                return any(col[1] == column_name for col in columns)
        except:
            return False

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
        """
        # Ensure admin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM admin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            # Get admin credentials from config_dict if provided, otherwise use defaults
            admin_username = "admin"
            admin_password = "admin"
            error_ban_threshold = 3

            if config_dict:
                global_config = config_dict.get("global", {})
                admin_username = global_config.get("admin_username", "admin")
                admin_password = global_config.get("admin_password", "admin")

                admin_config = config_dict.get("admin", {})
                error_ban_threshold = admin_config.get("error_ban_threshold", 3)

            await db.execute("""
                INSERT INTO admin_config (id, admin_username, admin_password, error_ban_threshold)
                VALUES (1, ?, ?, ?)
            """, (admin_username, admin_password, error_ban_threshold))

        # Ensure proxy_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM proxy_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            # Get proxy config from config_dict if provided, otherwise use defaults
            proxy_enabled = False
            proxy_url = None

            if config_dict:
                proxy_config = config_dict.get("proxy", {})
                proxy_enabled = proxy_config.get("proxy_enabled", False)
                proxy_url = proxy_config.get("proxy_url", "")
                # Convert empty string to None
                proxy_url = proxy_url if proxy_url else None

            await db.execute("""
                INSERT INTO proxy_config (id, proxy_enabled, proxy_url)
                VALUES (1, ?, ?)
            """, (proxy_enabled, proxy_url))

        # Ensure watermark_free_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM watermark_free_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            # Get watermark-free config from config_dict if provided, otherwise use defaults
            watermark_free_enabled = False
            parse_method = "third_party"
            custom_parse_url = None
            custom_parse_token = None

            if config_dict:
                watermark_config = config_dict.get("watermark_free", {})
                watermark_free_enabled = watermark_config.get("watermark_free_enabled", False)
                parse_method = watermark_config.get("parse_method", "third_party")
                custom_parse_url = watermark_config.get("custom_parse_url", "")
                custom_parse_token = watermark_config.get("custom_parse_token", "")

                # Convert empty strings to None
                custom_parse_url = custom_parse_url if custom_parse_url else None
                custom_parse_token = custom_parse_token if custom_parse_token else None

            await db.execute("""
                INSERT INTO watermark_free_config (id, watermark_free_enabled, parse_method, custom_parse_url, custom_parse_token)
                VALUES (1, ?, ?, ?, ?)
            """, (watermark_free_enabled, parse_method, custom_parse_url, custom_parse_token))

        # Ensure cache_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM cache_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            # Get cache config from config_dict if provided, otherwise use defaults
            cache_enabled = False
            cache_timeout = 600
            cache_base_url = None

            if config_dict:
                cache_config = config_dict.get("cache", {})
                cache_enabled = cache_config.get("enabled", False)
                cache_timeout = cache_config.get("timeout", 600)
                cache_base_url = cache_config.get("base_url", "")
                # Convert empty string to None
                cache_base_url = cache_base_url if cache_base_url else None

            await db.execute("""
                INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                VALUES (1, ?, ?, ?)
            """, (cache_enabled, cache_timeout, cache_base_url))

        # Ensure generation_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM generation_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            # Get generation config from config_dict if provided, otherwise use defaults
            image_timeout = 300
            video_timeout = 1500

            if config_dict:
                generation_config = config_dict.get("generation", {})
                image_timeout = generation_config.get("image_timeout", 300)
                video_timeout = generation_config.get("video_timeout", 1500)

            await db.execute("""
                INSERT INTO generation_config (id, image_timeout, video_timeout)
                VALUES (1, ?, ?)
            """, (image_timeout, video_timeout))

        # Ensure token_refresh_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM token_refresh_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            # Get token refresh config from config_dict if provided, otherwise use defaults
            at_auto_refresh_enabled = False

            if config_dict:
                token_refresh_config = config_dict.get("token_refresh", {})
                at_auto_refresh_enabled = token_refresh_config.get("at_auto_refresh_enabled", False)

            await db.execute("""
                INSERT INTO token_refresh_config (id, at_auto_refresh_enabled)
                VALUES (1, ?)
            """, (at_auto_refresh_enabled,))

        # Ensure cloudflare_solver_config has a row
        if await self._table_exists(db, "cloudflare_solver_config"):
            cursor = await db.execute("SELECT COUNT(*) FROM cloudflare_solver_config")
            count = await cursor.fetchone()
            if count[0] == 0:
                # Get cloudflare solver config from config_dict if provided, otherwise use defaults
                solver_enabled = False
                solver_api_url = "http://localhost:8000/v1/challenge"

                if config_dict:
                    cloudflare_config = config_dict.get("cloudflare", {})
                    solver_enabled = cloudflare_config.get("solver_enabled", False)
                    solver_api_url = cloudflare_config.get("solver_api_url", "http://localhost:8000/v1/challenge")

                await db.execute("""
                    INSERT INTO cloudflare_solver_config (id, solver_enabled, solver_api_url)
                    VALUES (1, ?, ?)
                """, (solver_enabled, solver_api_url))


    async def check_and_migrate_db(self, config_dict: dict = None):
        """Check database integrity and perform migrations if needed
        
        使用版本号机制，只在版本变化时执行完整迁移检查
        """
        CURRENT_DB_VERSION = 5  # 增加此版本号以触发迁移
        
        db = await self._get_connection()
        try:
            # 检查版本表是否存在
            if not await self._table_exists(db, "db_version"):
                await db.execute("""
                    CREATE TABLE db_version (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        version INTEGER DEFAULT 1,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("INSERT INTO db_version (id, version) VALUES (1, 1)")
                await db.commit()
            
            # 获取当前版本
            cursor = await db.execute("SELECT version FROM db_version WHERE id = 1")
            row = await cursor.fetchone()
            db_version = row[0] if row else 1
            
            # 如果版本相同，跳过迁移检查
            if db_version >= CURRENT_DB_VERSION:
                await self._ensure_config_rows(db, config_dict)
                await db.commit()
                return
            
            # 执行迁移
            await self._run_migrations(db, db_version, CURRENT_DB_VERSION, config_dict)
            
            # 更新版本号
            await db.execute("UPDATE db_version SET version = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1", 
                           (CURRENT_DB_VERSION,))
            await db.commit()
        finally:
            await db.close()

    async def _run_migrations(self, db, from_version: int, to_version: int, config_dict: dict = None):
        """执行数据库迁移"""
        print(f"  📦 Migrating database from v{from_version} to v{to_version}...")
        
        # 快速批量添加列（不逐个检查）
        migrations = [
            # (表名, 列名, 类型)
            ("tokens", "sora2_supported", "BOOLEAN"),
            ("tokens", "sora2_invite_code", "TEXT"),
            ("tokens", "sora2_redeemed_count", "INTEGER DEFAULT 0"),
            ("tokens", "sora2_total_count", "INTEGER DEFAULT 0"),
            ("tokens", "sora2_remaining_count", "INTEGER DEFAULT 0"),
            ("tokens", "sora2_cooldown_until", "TIMESTAMP"),
            ("tokens", "image_enabled", "BOOLEAN DEFAULT 1"),
            ("tokens", "video_enabled", "BOOLEAN DEFAULT 1"),
            ("tokens", "image_concurrency", "INTEGER DEFAULT -1"),
            ("tokens", "video_concurrency", "INTEGER DEFAULT -1"),
            ("tokens", "client_id", "TEXT"),
            ("tokens", "proxy_url", "TEXT"),
            ("token_stats", "consecutive_error_count", "INTEGER DEFAULT 0"),
            ("admin_config", "admin_username", "TEXT DEFAULT 'admin'"),
            ("admin_config", "admin_password", "TEXT DEFAULT 'admin'"),
            ("watermark_free_config", "parse_method", "TEXT DEFAULT 'third_party'"),
            ("watermark_free_config", "custom_parse_url", "TEXT"),
            ("watermark_free_config", "custom_parse_token", "TEXT"),
            ("proxy_config", "proxy_pool_enabled", "BOOLEAN DEFAULT 0"),
            ("request_logs", "task_id", "TEXT"),
            ("request_logs", "updated_at", "TIMESTAMP"),
        ]
        
        for table, col, col_type in migrations:
            if await self._table_exists(db, table):
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                except:
                    pass  # 列已存在，忽略
        
        # 创建新表
        new_tables = [
            ("cloudflare_solver_config", """
                CREATE TABLE IF NOT EXISTS cloudflare_solver_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    solver_enabled BOOLEAN DEFAULT 0,
                    solver_api_url TEXT DEFAULT 'http://localhost:8000/v1/challenge',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """),
            ("webdav_config", """
                CREATE TABLE IF NOT EXISTS webdav_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    webdav_enabled BOOLEAN DEFAULT 0,
                    webdav_url TEXT,
                    webdav_username TEXT,
                    webdav_password TEXT,
                    webdav_upload_path TEXT DEFAULT '/video',
                    auto_delete_enabled BOOLEAN DEFAULT 0,
                    auto_delete_days INTEGER DEFAULT 30,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """),
            ("video_records", """
                CREATE TABLE IF NOT EXISTS video_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    token_id INTEGER NOT NULL,
                    original_url TEXT NOT NULL,
                    watermark_free_url TEXT,
                    webdav_path TEXT,
                    webdav_url TEXT,
                    file_size INTEGER,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    uploaded_at TIMESTAMP,
                    deleted_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """),
            ("upload_logs", """
                CREATE TABLE IF NOT EXISTS upload_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_record_id INTEGER,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    duration FLOAT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (video_record_id) REFERENCES video_records(id)
                )
            """),
        ]
        
        for table_name, create_sql in new_tables:
            if not await self._table_exists(db, table_name):
                await db.execute(create_sql)
        
        # 创建索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_video_record_task_id ON video_records(task_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_video_record_status ON video_records(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_upload_log_video_record_id ON upload_logs(video_record_id)")
        
        # 确保配置行存在
        await self._ensure_config_rows(db, config_dict)
        
        print(f"  ✓ Migration completed")

    async def init_db(self):
        """Initialize database tables - creates all tables and ensures data integrity"""
        db = await self._get_connection()
        try:
            # Tokens table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    username TEXT NOT NULL,
                    name TEXT NOT NULL,
                    st TEXT,
                    rt TEXT,
                    client_id TEXT,
                    proxy_url TEXT,
                    remark TEXT,
                    expiry_time TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    cooled_until TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    plan_type TEXT,
                    plan_title TEXT,
                    subscription_end TIMESTAMP,
                    sora2_supported BOOLEAN,
                    sora2_invite_code TEXT,
                    sora2_redeemed_count INTEGER DEFAULT 0,
                    sora2_total_count INTEGER DEFAULT 0,
                    sora2_remaining_count INTEGER DEFAULT 0,
                    sora2_cooldown_until TIMESTAMP,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT -1
                )
            """)

            # Token stats table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress FLOAT DEFAULT 0,
                    result_urls TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Request logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    task_id TEXT,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Admin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    admin_username TEXT DEFAULT 'admin',
                    admin_password TEXT DEFAULT 'admin',
                    error_ban_threshold INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Proxy config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    proxy_enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Watermark-free config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS watermark_free_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    watermark_free_enabled BOOLEAN DEFAULT 0,
                    parse_method TEXT DEFAULT 'third_party',
                    custom_parse_url TEXT,
                    custom_parse_token TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cache config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 600,
                    cache_base_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Generation config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Token refresh config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_refresh_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    at_auto_refresh_enabled BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cloudflare Solver config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cloudflare_solver_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    solver_enabled BOOLEAN DEFAULT 0,
                    solver_api_url TEXT DEFAULT 'http://localhost:8000/v1/challenge',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Characters table (角色�?
            await db.execute("""
                CREATE TABLE IF NOT EXISTS characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cameo_id TEXT UNIQUE NOT NULL,
                    character_id TEXT,
                    token_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    profile_url TEXT,
                    instruction_set TEXT,
                    safety_instruction_set TEXT,
                    visibility TEXT DEFAULT 'private',
                    status TEXT DEFAULT 'finalized',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # WebDAV config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS webdav_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    webdav_enabled BOOLEAN DEFAULT 0,
                    webdav_url TEXT,
                    webdav_username TEXT,
                    webdav_password TEXT,
                    webdav_upload_path TEXT DEFAULT '/video',
                    auto_delete_enabled BOOLEAN DEFAULT 0,
                    auto_delete_days INTEGER DEFAULT 30,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Video records table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS video_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    token_id INTEGER NOT NULL,
                    original_url TEXT NOT NULL,
                    watermark_free_url TEXT,
                    webdav_path TEXT,
                    webdav_url TEXT,
                    file_size INTEGER,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    uploaded_at TIMESTAMP,
                    deleted_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Upload logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS upload_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_record_id INTEGER,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    duration FLOAT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (video_record_id) REFERENCES video_records(id)
                )
            """)

            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON tasks(task_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_active ON tokens(is_active)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_character_cameo_id ON characters(cameo_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_character_token_id ON characters(token_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_video_record_task_id ON video_records(task_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_video_record_status ON video_records(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_upload_log_video_record_id ON upload_logs(video_record_id)")

            # Migration: Add daily statistics columns if they don't exist
            if not await self._column_exists(db, "token_stats", "today_image_count"):
                await db.execute("ALTER TABLE token_stats ADD COLUMN today_image_count INTEGER DEFAULT 0")
            if not await self._column_exists(db, "token_stats", "today_video_count"):
                await db.execute("ALTER TABLE token_stats ADD COLUMN today_video_count INTEGER DEFAULT 0")
            if not await self._column_exists(db, "token_stats", "today_error_count"):
                await db.execute("ALTER TABLE token_stats ADD COLUMN today_error_count INTEGER DEFAULT 0")
            if not await self._column_exists(db, "token_stats", "today_date"):
                await db.execute("ALTER TABLE token_stats ADD COLUMN today_date DATE")

            await db.commit()
        finally:
            await db.close()

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, only update if row doesn't exist. If False, always update.
        """
        async with self._connect() as db:
            # On first startup, ensure all config rows exist with values from setting.toml
            if is_first_startup:
                await self._ensure_config_rows(db, config_dict)

            # Initialize admin config
            admin_config = config_dict.get("admin", {})
            error_ban_threshold = admin_config.get("error_ban_threshold", 3)

            # Get admin credentials from global config
            global_config = config_dict.get("global", {})
            admin_username = global_config.get("admin_username", "admin")
            admin_password = global_config.get("admin_password", "admin")

            if not is_first_startup:
                # On upgrade, update the configuration
                await db.execute("""
                    UPDATE admin_config
                    SET admin_username = ?, admin_password = ?, error_ban_threshold = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (admin_username, admin_password, error_ban_threshold))

            # Initialize proxy config
            proxy_config = config_dict.get("proxy", {})
            proxy_enabled = proxy_config.get("proxy_enabled", False)
            proxy_url = proxy_config.get("proxy_url", "")
            # Convert empty string to None
            proxy_url = proxy_url if proxy_url else None

            if is_first_startup:
                await db.execute("""
                    INSERT OR IGNORE INTO proxy_config (id, proxy_enabled, proxy_url)
                    VALUES (1, ?, ?)
                """, (proxy_enabled, proxy_url))
            else:
                await db.execute("""
                    UPDATE proxy_config
                    SET proxy_enabled = ?, proxy_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (proxy_enabled, proxy_url))

            # Initialize watermark-free config
            watermark_config = config_dict.get("watermark_free", {})
            watermark_free_enabled = watermark_config.get("watermark_free_enabled", False)
            parse_method = watermark_config.get("parse_method", "third_party")
            custom_parse_url = watermark_config.get("custom_parse_url", "")
            custom_parse_token = watermark_config.get("custom_parse_token", "")

            # Convert empty strings to None
            custom_parse_url = custom_parse_url if custom_parse_url else None
            custom_parse_token = custom_parse_token if custom_parse_token else None

            if is_first_startup:
                await db.execute("""
                    INSERT OR IGNORE INTO watermark_free_config (id, watermark_free_enabled, parse_method, custom_parse_url, custom_parse_token)
                    VALUES (1, ?, ?, ?, ?)
                """, (watermark_free_enabled, parse_method, custom_parse_url, custom_parse_token))
            else:
                await db.execute("""
                    UPDATE watermark_free_config
                    SET watermark_free_enabled = ?, parse_method = ?, custom_parse_url = ?,
                        custom_parse_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (watermark_free_enabled, parse_method, custom_parse_url, custom_parse_token))

            # Initialize cache config
            cache_config = config_dict.get("cache", {})
            cache_enabled = cache_config.get("enabled", False)
            cache_timeout = cache_config.get("timeout", 600)
            cache_base_url = cache_config.get("base_url", "")
            # Convert empty string to None
            cache_base_url = cache_base_url if cache_base_url else None

            if is_first_startup:
                await db.execute("""
                    INSERT OR IGNORE INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                    VALUES (1, ?, ?, ?)
                """, (cache_enabled, cache_timeout, cache_base_url))
            else:
                await db.execute("""
                    UPDATE cache_config
                    SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (cache_enabled, cache_timeout, cache_base_url))

            # Initialize generation config
            generation_config = config_dict.get("generation", {})
            image_timeout = generation_config.get("image_timeout", 300)
            video_timeout = generation_config.get("video_timeout", 1500)

            if is_first_startup:
                await db.execute("""
                    INSERT OR IGNORE INTO generation_config (id, image_timeout, video_timeout)
                    VALUES (1, ?, ?)
                """, (image_timeout, video_timeout))
            else:
                await db.execute("""
                    UPDATE generation_config
                    SET image_timeout = ?, video_timeout = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (image_timeout, video_timeout))

            # Initialize token refresh config
            token_refresh_config = config_dict.get("token_refresh", {})
            at_auto_refresh_enabled = token_refresh_config.get("at_auto_refresh_enabled", False)

            if is_first_startup:
                await db.execute("""
                    INSERT OR IGNORE INTO token_refresh_config (id, at_auto_refresh_enabled)
                    VALUES (1, ?)
                """, (at_auto_refresh_enabled,))
            else:
                await db.execute("""
                    UPDATE token_refresh_config
                    SET at_auto_refresh_enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (at_auto_refresh_enabled,))

            await db.commit()

    # Token operations
    async def add_token(self, token: Token) -> int:
        """Add a new token"""
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO tokens (token, email, username, name, st, rt, client_id, proxy_url, remark, expiry_time, is_active,
                                   plan_type, plan_title, subscription_end, sora2_supported, sora2_invite_code,
                                   sora2_redeemed_count, sora2_total_count, sora2_remaining_count, sora2_cooldown_until,
                                   image_enabled, video_enabled, image_concurrency, video_concurrency)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (token.token, token.email, "", token.name, token.st, token.rt, token.client_id, token.proxy_url,
                  token.remark, token.expiry_time, token.is_active,
                  token.plan_type, token.plan_title, token.subscription_end,
                  token.sora2_supported, token.sora2_invite_code,
                  token.sora2_redeemed_count, token.sora2_total_count,
                  token.sora2_remaining_count, token.sora2_cooldown_until,
                  token.image_enabled, token.video_enabled,
                  token.image_concurrency, token.video_concurrency))
            await db.commit()
            token_id = cursor.lastrowid

            # Create stats entry
            await db.execute("""
                INSERT INTO token_stats (token_id) VALUES (?)
            """, (token_id,))
            await db.commit()

            return token_id
    
    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None
    
    async def get_token_by_value(self, token: str) -> Optional[Token]:
        """Get token by value"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE token = ?", (token,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_token_by_email(self, email: str) -> Optional[Token]:
        """Get token by email"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE email = ?", (email,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None
    
    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens (enabled, not cooled down, not expired)"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM tokens
                WHERE is_active = 1
                AND (cooled_until IS NULL OR cooled_until < CURRENT_TIMESTAMP)
                AND expiry_time > CURRENT_TIMESTAMP
                ORDER BY last_used_at ASC NULLS FIRST
            """)
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]
    
    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]
    
    async def update_token_usage(self, token_id: int):
        """Update token usage"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE tokens 
                SET last_used_at = CURRENT_TIMESTAMP, use_count = use_count + 1
                WHERE id = ?
            """, (token_id,))
            await db.commit()
    
    async def update_token_status(self, token_id: int, is_active: bool):
        """Update token status"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE tokens SET is_active = ? WHERE id = ?
            """, (is_active, token_id))
            await db.commit()
    
    async def update_token_sora2(self, token_id: int, supported: bool, invite_code: Optional[str] = None,
                                redeemed_count: int = 0, total_count: int = 0, remaining_count: int = 0):
        """Update token Sora2 support info"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE tokens
                SET sora2_supported = ?, sora2_invite_code = ?, sora2_redeemed_count = ?, sora2_total_count = ?, sora2_remaining_count = ?
                WHERE id = ?
            """, (supported, invite_code, redeemed_count, total_count, remaining_count, token_id))
            await db.commit()

    async def update_token_sora2_remaining(self, token_id: int, remaining_count: int):
        """Update token Sora2 remaining count"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE tokens SET sora2_remaining_count = ? WHERE id = ?
            """, (remaining_count, token_id))
            await db.commit()

    async def update_token_sora2_cooldown(self, token_id: int, cooldown_until: Optional[datetime]):
        """Update token Sora2 cooldown time"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE tokens SET sora2_cooldown_until = ? WHERE id = ?
            """, (cooldown_until, token_id))
            await db.commit()

    async def update_token_cooldown(self, token_id: int, cooled_until: datetime):
        """Update token cooldown"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE tokens SET cooled_until = ? WHERE id = ?
            """, (cooled_until, token_id))
            await db.commit()
    
    async def delete_token(self, token_id: int):
        """Delete token"""
        async with self._connect() as db:
            await db.execute("DELETE FROM token_stats WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            await db.commit()

    async def update_token(self, token_id: int,
                          token: Optional[str] = None,
                          st: Optional[str] = None,
                          rt: Optional[str] = None,
                          client_id: Optional[str] = None,
                          proxy_url: Optional[str] = None,
                          remark: Optional[str] = None,
                          expiry_time: Optional[datetime] = None,
                          plan_type: Optional[str] = None,
                          plan_title: Optional[str] = None,
                          subscription_end: Optional[datetime] = None,
                          image_enabled: Optional[bool] = None,
                          video_enabled: Optional[bool] = None,
                          image_concurrency: Optional[int] = None,
                          video_concurrency: Optional[int] = None):
        """Update token (AT, ST, RT, client_id, proxy_url, remark, expiry_time, subscription info, image_enabled, video_enabled)"""
        async with self._connect() as db:
            # Build dynamic update query
            updates = []
            params = []

            if token is not None:
                updates.append("token = ?")
                params.append(token)

            if st is not None:
                updates.append("st = ?")
                params.append(st)

            if rt is not None:
                updates.append("rt = ?")
                params.append(rt)

            if client_id is not None:
                updates.append("client_id = ?")
                params.append(client_id)

            if proxy_url is not None:
                updates.append("proxy_url = ?")
                params.append(proxy_url)

            if remark is not None:
                updates.append("remark = ?")
                params.append(remark)

            if expiry_time is not None:
                updates.append("expiry_time = ?")
                params.append(expiry_time)

            if plan_type is not None:
                updates.append("plan_type = ?")
                params.append(plan_type)

            if plan_title is not None:
                updates.append("plan_title = ?")
                params.append(plan_title)

            if subscription_end is not None:
                updates.append("subscription_end = ?")
                params.append(subscription_end)

            if image_enabled is not None:
                updates.append("image_enabled = ?")
                params.append(image_enabled)

            if video_enabled is not None:
                updates.append("video_enabled = ?")
                params.append(video_enabled)

            if image_concurrency is not None:
                updates.append("image_concurrency = ?")
                params.append(image_concurrency)

            if video_concurrency is not None:
                updates.append("video_concurrency = ?")
                params.append(video_concurrency)

            if updates:
                params.append(token_id)
                query = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    # Token stats operations
    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None
    
    async def increment_image_count(self, token_id: int):
        """Increment image generation count"""
        from datetime import date
        async with self._connect() as db:
            today = str(date.today())
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset today's count
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count"""
        from datetime import date
        async with self._connect() as db:
            today = str(date.today())
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset today's count
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()
    
    async def increment_error_count(self, token_id: int):
        """Increment error count (both total and consecutive)"""
        from datetime import date
        async with self._connect() as db:
            today = str(date.today())
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset today's error count
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment all counters
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()
    
    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (keep total error_count)"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """, (token_id,))
            await db.commit()
    
    # Task operations
    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO tasks (task_id, token_id, model, prompt, status, progress)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task.task_id, task.token_id, task.model, task.prompt, task.status, task.progress))
            await db.commit()
            return cursor.lastrowid
    
    async def update_task(self, task_id: str, status: str, progress: float, 
                         result_urls: Optional[str] = None, error_message: Optional[str] = None):
        """Update task status"""
        async with self._connect() as db:
            completed_at = datetime.now() if status in ["completed", "failed"] else None
            await db.execute("""
                UPDATE tasks 
                SET status = ?, progress = ?, result_urls = ?, error_message = ?, completed_at = ?
                WHERE task_id = ?
            """, (status, progress, result_urls, error_message, completed_at, task_id))
            await db.commit()
    
    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                return Task(**dict(row))
            return None
    
    # Request log operations
    async def log_request(self, log: RequestLog) -> int:
        """Log a request and return log ID"""
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO request_logs (token_id, task_id, operation, request_body, response_body, status_code, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (log.token_id, log.task_id, log.operation, log.request_body, log.response_body,
                  log.status_code, log.duration))
            await db.commit()
            return cursor.lastrowid

    async def update_request_log(self, log_id: int, response_body: Optional[str] = None,
                                 status_code: Optional[int] = None, duration: Optional[float] = None):
        """Update request log with completion data"""
        async with self._connect() as db:
            updates = []
            params = []

            if response_body is not None:
                updates.append("response_body = ?")
                params.append(response_body)
            if status_code is not None:
                updates.append("status_code = ?")
                params.append(status_code)
            if duration is not None:
                updates.append("duration = ?")
                params.append(duration)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(log_id)
                query = f"UPDATE request_logs SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    async def get_recent_logs(self, limit: int = 100) -> List[dict]:
        """Get recent logs with token email and task progress"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT
                    rl.id,
                    rl.token_id,
                    rl.task_id,
                    rl.operation,
                    rl.request_body,
                    rl.response_body,
                    rl.status_code,
                    rl.duration,
                    rl.created_at,
                    t.email as token_email,
                    t.username as token_username
                FROM request_logs rl
                LEFT JOIN tokens t ON rl.token_id = t.id
                ORDER BY rl.created_at DESC
                LIMIT ?
            """, (limit,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    # Admin config operations
    async def get_admin_config(self) -> AdminConfig:
        """Get admin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return AdminConfig(**dict(row))
            # If no row exists, return a default config with placeholder values
            # This should not happen in normal operation as _ensure_config_rows should create it
            return AdminConfig(admin_username="admin", admin_password="admin")
    
    async def update_admin_config(self, config: AdminConfig):
        """Update admin configuration"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE admin_config
                SET admin_username = ?, admin_password = ?, error_ban_threshold = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (config.admin_username, config.admin_password, config.error_ban_threshold))
            await db.commit()
    
    # Proxy config operations
    async def get_proxy_config(self) -> ProxyConfig:
        """Get proxy configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return ProxyConfig(**dict(row))
            # If no row exists, return a default config
            # This should not happen in normal operation as _ensure_config_rows should create it
            return ProxyConfig(proxy_enabled=False)
    
    async def update_proxy_config(self, enabled: bool, proxy_url: Optional[str], proxy_pool_enabled: bool = False):
        """Update proxy configuration"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE proxy_config
                SET proxy_enabled = ?, proxy_url = ?, proxy_pool_enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (enabled, proxy_url, proxy_pool_enabled))
            await db.commit()

    # Watermark-free config operations
    async def get_watermark_free_config(self) -> WatermarkFreeConfig:
        """Get watermark-free configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM watermark_free_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return WatermarkFreeConfig(**dict(row))
            # If no row exists, return a default config
            # This should not happen in normal operation as _ensure_config_rows should create it
            return WatermarkFreeConfig(watermark_free_enabled=False, parse_method="third_party")

    async def update_watermark_free_config(self, enabled: bool, parse_method: str = None,
                                          custom_parse_url: str = None, custom_parse_token: str = None):
        """Update watermark-free configuration"""
        async with self._connect() as db:
            if parse_method is None and custom_parse_url is None and custom_parse_token is None:
                # Only update enabled status
                await db.execute("""
                    UPDATE watermark_free_config
                    SET watermark_free_enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (enabled,))
            else:
                # Update all fields
                await db.execute("""
                    UPDATE watermark_free_config
                    SET watermark_free_enabled = ?, parse_method = ?, custom_parse_url = ?,
                        custom_parse_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (enabled, parse_method or "third_party", custom_parse_url, custom_parse_token))
            await db.commit()

    # Cache config operations
    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CacheConfig(**dict(row))
            # If no row exists, return a default config
            # This should not happen in normal operation as _ensure_config_rows should create it
            return CacheConfig(cache_enabled=False, cache_timeout=600)

    async def update_cache_config(self, enabled: bool = None, timeout: int = None, base_url: Optional[str] = None):
        """Update cache configuration"""
        async with self._connect() as db:
            # Get current config first
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Update only provided fields
                new_enabled = enabled if enabled is not None else current.get("cache_enabled", False)
                new_timeout = timeout if timeout is not None else current.get("cache_timeout", 600)
                new_base_url = base_url if base_url is not None else current.get("cache_base_url")
            else:
                new_enabled = enabled if enabled is not None else False
                new_timeout = timeout if timeout is not None else 600
                new_base_url = base_url

            # Convert empty string to None
            new_base_url = new_base_url if new_base_url else None

            await db.execute("""
                UPDATE cache_config
                SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (new_enabled, new_timeout, new_base_url))
            await db.commit()

    # Generation config operations
    async def get_generation_config(self) -> GenerationConfig:
        """Get generation configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return GenerationConfig(**dict(row))
            # If no row exists, return a default config
            # This should not happen in normal operation as _ensure_config_rows should create it
            return GenerationConfig(image_timeout=300, video_timeout=1500)

    async def update_generation_config(self, image_timeout: int = None, video_timeout: int = None):
        """Update generation configuration"""
        async with self._connect() as db:
            # Get current config first
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Update only provided fields
                new_image_timeout = image_timeout if image_timeout is not None else current.get("image_timeout", 300)
                new_video_timeout = video_timeout if video_timeout is not None else current.get("video_timeout", 1500)
            else:
                new_image_timeout = image_timeout if image_timeout is not None else 300
                new_video_timeout = video_timeout if video_timeout is not None else 1500

            await db.execute("""
                UPDATE generation_config
                SET image_timeout = ?, video_timeout = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (new_image_timeout, new_video_timeout))
            await db.commit()

    # Token refresh config operations
    async def get_token_refresh_config(self) -> TokenRefreshConfig:
        """Get token refresh configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_refresh_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return TokenRefreshConfig(**dict(row))
            # If no row exists, return a default config
            # This should not happen in normal operation as _ensure_config_rows should create it
            return TokenRefreshConfig(at_auto_refresh_enabled=False)

    async def update_token_refresh_config(self, at_auto_refresh_enabled: bool):
        """Update token refresh configuration"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE token_refresh_config
                SET at_auto_refresh_enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (at_auto_refresh_enabled,))
            await db.commit()

    # Cloudflare Solver config operations
    async def get_cloudflare_solver_config(self) -> CloudflareSolverConfig:
        """Get Cloudflare Solver configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cloudflare_solver_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CloudflareSolverConfig(**dict(row))
            # If no row exists, return a default config
            return CloudflareSolverConfig(solver_enabled=False, solver_api_url="http://localhost:8000/v1/challenge")

    async def update_cloudflare_solver_config(self, solver_enabled: bool, solver_api_url: str = None):
        """Update Cloudflare Solver configuration"""
        async with self._connect() as db:
            if solver_api_url is not None:
                await db.execute("""
                    UPDATE cloudflare_solver_config
                    SET solver_enabled = ?, solver_api_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (solver_enabled, solver_api_url))
            else:
                await db.execute("""
                    UPDATE cloudflare_solver_config
                    SET solver_enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (solver_enabled,))
            await db.commit()

    async def ensure_cloudflare_solver_config_row(self, config_dict: dict = None):
        """Ensure cloudflare_solver_config table has a row"""
        async with self._connect() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM cloudflare_solver_config")
            count = await cursor.fetchone()
            if count[0] == 0:
                solver_enabled = False
                solver_api_url = "http://localhost:8000/v1/challenge"
                if config_dict:
                    cloudflare_config = config_dict.get("cloudflare", {})
                    solver_enabled = cloudflare_config.get("solver_enabled", False)
                    solver_api_url = cloudflare_config.get("solver_api_url", solver_api_url)
                await db.execute("""
                    INSERT INTO cloudflare_solver_config (id, solver_enabled, solver_api_url)
                    VALUES (1, ?, ?)
                """, (solver_enabled, solver_api_url))
                await db.commit()

    # Character (角色�? operations
    async def create_character(self, character: Character) -> int:
        """Create a new character record"""
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO characters (cameo_id, character_id, token_id, username, display_name, 
                                       profile_url, instruction_set, safety_instruction_set, 
                                       visibility, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (character.cameo_id, character.character_id, character.token_id, 
                  character.username, character.display_name, character.profile_url,
                  character.instruction_set, character.safety_instruction_set,
                  character.visibility, character.status))
            await db.commit()
            return cursor.lastrowid

    async def get_character_by_cameo_id(self, cameo_id: str) -> Optional[Character]:
        """Get character by cameo_id"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM characters WHERE cameo_id = ?", (cameo_id,)
            )
            row = await cursor.fetchone()
            if row:
                return Character(**dict(row))
            return None

    async def get_character_by_id(self, character_db_id: int) -> Optional[Character]:
        """Get character by database id"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM characters WHERE id = ?", (character_db_id,)
            )
            row = await cursor.fetchone()
            if row:
                return Character(**dict(row))
            return None

    async def get_characters_by_token_id(self, token_id: int) -> List[Character]:
        """Get all characters for a specific token"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM characters WHERE token_id = ? ORDER BY created_at DESC", (token_id,)
            )
            rows = await cursor.fetchall()
            return [Character(**dict(row)) for row in rows]

    async def get_all_characters(self) -> List[Character]:
        """Get all characters"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM characters ORDER BY created_at DESC"
            )
            rows = await cursor.fetchall()
            return [Character(**dict(row)) for row in rows]

    async def update_character(self, cameo_id: str, **kwargs) -> bool:
        """Update character fields by cameo_id"""
        if not kwargs:
            return False
        
        # Build dynamic update query
        set_clauses = []
        values = []
        for key, value in kwargs.items():
            if key in ['character_id', 'username', 'display_name', 'profile_url', 
                       'instruction_set', 'safety_instruction_set', 'visibility', 'status']:
                set_clauses.append(f"{key} = ?")
                values.append(value)
        
        if not set_clauses:
            return False
        
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(cameo_id)
        
        async with self._connect() as db:
            await db.execute(
                f"UPDATE characters SET {', '.join(set_clauses)} WHERE cameo_id = ?",
                values
            )
            await db.commit()
            return True

    async def delete_character(self, cameo_id: str) -> bool:
        """Delete character by cameo_id"""
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM characters WHERE cameo_id = ?", (cameo_id,)
            )
            await db.commit()
            return cursor.rowcount > 0


    # WebDAV config operations
    async def get_webdav_config(self) -> WebDAVConfig:
        """Get WebDAV configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM webdav_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return WebDAVConfig(**dict(row))
            # If no row exists, return a default config
            return WebDAVConfig(webdav_enabled=False)

    async def update_webdav_config(self, enabled: bool = None, url: str = None, 
                                   username: str = None, password: str = None,
                                   upload_path: str = None, auto_delete_enabled: bool = None,
                                   auto_delete_days: int = None):
        """Update WebDAV configuration"""
        async with self._connect() as db:
            # Check if row exists
            cursor = await db.execute("SELECT COUNT(*) FROM webdav_config WHERE id = 1")
            count = await cursor.fetchone()
            
            if count[0] == 0:
                # Insert new row
                await db.execute("""
                    INSERT INTO webdav_config (id, webdav_enabled, webdav_url, webdav_username, 
                                              webdav_password, webdav_upload_path, auto_delete_enabled, auto_delete_days)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """, (enabled or False, url, username, password, upload_path or '/video', 
                      auto_delete_enabled or False, auto_delete_days or 30))
            else:
                # Build dynamic update query
                updates = []
                params = []
                
                if enabled is not None:
                    updates.append("webdav_enabled = ?")
                    params.append(enabled)
                if url is not None:
                    updates.append("webdav_url = ?")
                    params.append(url)
                if username is not None:
                    updates.append("webdav_username = ?")
                    params.append(username)
                if password is not None:
                    updates.append("webdav_password = ?")
                    params.append(password)
                if upload_path is not None:
                    updates.append("webdav_upload_path = ?")
                    params.append(upload_path)
                if auto_delete_enabled is not None:
                    updates.append("auto_delete_enabled = ?")
                    params.append(auto_delete_enabled)
                if auto_delete_days is not None:
                    updates.append("auto_delete_days = ?")
                    params.append(auto_delete_days)
                
                if updates:
                    updates.append("updated_at = CURRENT_TIMESTAMP")
                    query = f"UPDATE webdav_config SET {', '.join(updates)} WHERE id = 1"
                    await db.execute(query, params)
            
            await db.commit()

    # Video record operations
    async def create_video_record(self, record: VideoRecord) -> int:
        """Create a new video record"""
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO video_records (task_id, token_id, original_url, watermark_free_url, 
                                          webdav_path, webdav_url, file_size, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (record.task_id, record.token_id, record.original_url, record.watermark_free_url,
                  record.webdav_path, record.webdav_url, record.file_size, record.status, record.error_message))
            await db.commit()
            return cursor.lastrowid

    async def get_video_record(self, record_id: int) -> Optional[VideoRecord]:
        """Get video record by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM video_records WHERE id = ?", (record_id,))
            row = await cursor.fetchone()
            if row:
                return VideoRecord(**dict(row))
            return None

    async def get_video_record_by_task_id(self, task_id: str) -> Optional[VideoRecord]:
        """Get video record by task ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM video_records WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                return VideoRecord(**dict(row))
            return None

    async def get_all_video_records(self, limit: int = 100, status: str = None) -> List[VideoRecord]:
        """Get all video records with optional status filter"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if status:
                cursor = await db.execute(
                    "SELECT * FROM video_records WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM video_records ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
            rows = await cursor.fetchall()
            return [VideoRecord(**dict(row)) for row in rows]

    async def get_video_records_for_auto_delete(self, days: int) -> List[VideoRecord]:
        """Get video records older than specified days for auto deletion"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM video_records 
                WHERE status = 'uploaded' 
                AND uploaded_at < datetime('now', ? || ' days')
            """, (f"-{days}",))
            rows = await cursor.fetchall()
            return [VideoRecord(**dict(row)) for row in rows]

    async def update_video_record(self, record_id: int, **kwargs):
        """Update video record fields"""
        if not kwargs:
            return
        
        async with self._connect() as db:
            updates = []
            params = []
            
            for key, value in kwargs.items():
                if key in ['watermark_free_url', 'webdav_path', 'webdav_url', 'file_size', 
                          'status', 'error_message', 'uploaded_at', 'deleted_at']:
                    updates.append(f"{key} = ?")
                    params.append(value)
            
            if updates:
                params.append(record_id)
                query = f"UPDATE video_records SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    async def delete_video_record(self, record_id: int):
        """Delete video record"""
        async with self._connect() as db:
            await db.execute("DELETE FROM upload_logs WHERE video_record_id = ?", (record_id,))
            await db.execute("DELETE FROM video_records WHERE id = ?", (record_id,))
            await db.commit()

    async def delete_all_video_records(self):
        """Delete all video records and upload logs"""
        async with self._connect() as db:
            await db.execute("DELETE FROM upload_logs")
            await db.execute("DELETE FROM video_records")
            await db.commit()

    async def get_video_records_stats(self) -> dict:
        """Get video records statistics"""
        async with self._connect() as db:
            cursor = await db.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'uploaded' THEN 1 ELSE 0 END) as uploaded,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                    SUM(COALESCE(file_size, 0)) as total_size
                FROM video_records
            """)
            row = await cursor.fetchone()
            return {
                "total": row[0] or 0,
                "uploaded": row[1] or 0,
                "pending": row[2] or 0,
                "failed": row[3] or 0,
                "deleted": row[4] or 0,
                "total_size": row[5] or 0
            }

    # Upload log operations
    async def create_upload_log(self, log: UploadLog) -> int:
        """Create a new upload log"""
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO upload_logs (video_record_id, operation, status, message, duration)
                VALUES (?, ?, ?, ?, ?)
            """, (log.video_record_id, log.operation, log.status, log.message, log.duration))
            await db.commit()
            return cursor.lastrowid

    async def get_upload_logs(self, limit: int = 100) -> List[dict]:
        """Get recent upload logs with video record info"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT 
                    ul.*,
                    vr.task_id,
                    vr.webdav_path
                FROM upload_logs ul
                LEFT JOIN video_records vr ON ul.video_record_id = vr.id
                ORDER BY ul.created_at DESC
                LIMIT ?
            """, (limit,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_all_upload_logs(self):
        """Delete all upload logs"""
        async with self._connect() as db:
            await db.execute("DELETE FROM upload_logs")
            await db.commit()

    async def ensure_webdav_config_row(self):
        """Ensure webdav_config table has a default row"""
        async with self._connect() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM webdav_config")
            count = await cursor.fetchone()
            if count[0] == 0:
                await db.execute("""
                    INSERT INTO webdav_config (id, webdav_enabled, webdav_upload_path, auto_delete_enabled, auto_delete_days)
                    VALUES (1, 0, '/video', 0, 30)
                """)
                await db.commit()
