"""Database adapter layer - supports SQLite and MySQL"""
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, List, Any, Dict
from contextlib import asynccontextmanager
from pathlib import Path

from .config import config


class DatabaseAdapter(ABC):
    """Abstract database adapter interface"""
    
    @abstractmethod
    async def initialize(self):
        """Initialize the database connection/pool"""
        pass
    
    @abstractmethod
    async def close(self):
        """Close all connections"""
        pass
    
    @abstractmethod
    @asynccontextmanager
    async def connection(self, readonly: bool = False):
        """Get a database connection"""
        pass
    
    @abstractmethod
    async def execute(self, sql: str, params: tuple = None) -> Any:
        """Execute a write operation"""
        pass
    
    @abstractmethod
    async def execute_many(self, sql: str, params_list: List[tuple]) -> None:
        """Execute multiple write operations"""
        pass
    
    @abstractmethod
    async def fetchone(self, sql: str, params: tuple = None) -> Optional[Dict]:
        """Fetch a single row"""
        pass
    
    @abstractmethod
    async def fetchall(self, sql: str, params: tuple = None) -> List[Dict]:
        """Fetch all rows"""
        pass
    
    @abstractmethod
    async def table_exists(self, table_name: str) -> bool:
        """Check if a table exists"""
        pass
    
    @abstractmethod
    async def column_exists(self, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        pass
    
    @abstractmethod
    def get_placeholder(self) -> str:
        """Get the parameter placeholder (? for SQLite, %s for MySQL)"""
        pass
    
    @abstractmethod
    def get_auto_increment(self) -> str:
        """Get the auto increment syntax"""
        pass
    
    @abstractmethod
    def get_current_timestamp(self) -> str:
        """Get the current timestamp function"""
        pass


class SQLiteAdapter(DatabaseAdapter):
    """SQLite database adapter with WAL mode and high concurrency optimizations"""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            data_dir = Path(__file__).parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "hancat.db")
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._initialized = False
    
    async def initialize(self):
        """Initialize SQLite with WAL mode"""
        if self._initialized:
            return
        
        import aiosqlite
        # Test connection and set up WAL mode
        conn = await aiosqlite.connect(self.db_path, timeout=60.0)
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout=60000")
            await conn.execute("PRAGMA cache_size=-64000")
            await conn.execute("PRAGMA mmap_size=268435456")
            await conn.execute("PRAGMA temp_store=MEMORY")
            await conn.commit()
        finally:
            await conn.close()
        
        self._initialized = True
        print(f"✅ SQLite adapter initialized (WAL mode, path: {self.db_path})")
    
    async def close(self):
        """Close SQLite connections"""
        self._initialized = False
    
    @asynccontextmanager
    async def connection(self, readonly: bool = False):
        """Get a SQLite connection with optimized settings"""
        import aiosqlite
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
    
    async def execute(self, sql: str, params: tuple = None, max_retries: int = 5) -> Any:
        """Execute a write operation with retry logic"""
        import aiosqlite
        for attempt in range(max_retries):
            try:
                async with self._write_lock:
                    async with self.connection() as conn:
                        if params:
                            cursor = await conn.execute(sql, params)
                        else:
                            cursor = await conn.execute(sql)
                        await conn.commit()
                        return cursor.lastrowid
            except aiosqlite.OperationalError as e:
                error_msg = str(e).lower()
                if ("database is locked" in error_msg or "unable to open" in error_msg) and attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 0.2 + (attempt * 0.1)
                    print(f"⚠️ SQLite locked, retrying in {wait_time:.1f}s... (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                else:
                    raise
    
    async def execute_many(self, sql: str, params_list: List[tuple]) -> None:
        """Execute multiple write operations"""
        async with self._write_lock:
            async with self.connection() as conn:
                await conn.executemany(sql, params_list)
                await conn.commit()
    
    async def fetchone(self, sql: str, params: tuple = None) -> Optional[Dict]:
        """Fetch a single row"""
        async with self.connection(readonly=True) as conn:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None
    
    async def fetchall(self, sql: str, params: tuple = None) -> List[Dict]:
        """Fetch all rows"""
        async with self.connection(readonly=True) as conn:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in SQLite"""
        result = await self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return result is not None
    
    async def column_exists(self, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a SQLite table"""
        try:
            rows = await self.fetchall(f"PRAGMA table_info({table_name})")
            return any(row.get('name') == column_name for row in rows)
        except:
            return False
    
    def get_placeholder(self) -> str:
        return "?"
    
    def get_auto_increment(self) -> str:
        return "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    def get_current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP"
    
    def db_exists(self) -> bool:
        """Check if database file exists"""
        return Path(self.db_path).exists()


class MySQLAdapter(DatabaseAdapter):
    """MySQL database adapter with connection pooling"""
    
    def __init__(self):
        self._pool = None
        self._initialized = False
    
    async def initialize(self):
        """Initialize MySQL connection pool"""
        if self._initialized:
            return
        
        import aiomysql
        
        self._pool = await aiomysql.create_pool(
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
        
        self._initialized = True
        print(f"✅ MySQL adapter initialized (pool_size: {config.mysql_pool_size}, host: {config.mysql_host})")
    
    async def close(self):
        """Close MySQL connection pool"""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        self._initialized = False
    
    @asynccontextmanager
    async def connection(self, readonly: bool = False):
        """Get a MySQL connection from pool"""
        if not self._initialized:
            await self.initialize()
        
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                yield cursor
    
    async def execute(self, sql: str, params: tuple = None) -> Any:
        """Execute a write operation"""
        if not self._initialized:
            await self.initialize()
        
        # Convert SQLite placeholders to MySQL
        sql = sql.replace("?", "%s")
        
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                if params:
                    await cursor.execute(sql, params)
                else:
                    await cursor.execute(sql)
                await conn.commit()
                return cursor.lastrowid
    
    async def execute_many(self, sql: str, params_list: List[tuple]) -> None:
        """Execute multiple write operations"""
        if not self._initialized:
            await self.initialize()
        
        sql = sql.replace("?", "%s")
        
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(sql, params_list)
                await conn.commit()
    
    async def fetchone(self, sql: str, params: tuple = None) -> Optional[Dict]:
        """Fetch a single row"""
        if not self._initialized:
            await self.initialize()
        
        sql = sql.replace("?", "%s")
        
        import aiomysql
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                if params:
                    await cursor.execute(sql, params)
                else:
                    await cursor.execute(sql)
                row = await cursor.fetchone()
                return row
    
    async def fetchall(self, sql: str, params: tuple = None) -> List[Dict]:
        """Fetch all rows"""
        if not self._initialized:
            await self.initialize()
        
        sql = sql.replace("?", "%s")
        
        import aiomysql
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                if params:
                    await cursor.execute(sql, params)
                else:
                    await cursor.execute(sql)
                rows = await cursor.fetchall()
                return list(rows)
    
    async def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in MySQL"""
        result = await self.fetchone(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (config.mysql_database, table_name)
        )
        return result is not None
    
    async def column_exists(self, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a MySQL table"""
        result = await self.fetchone(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (config.mysql_database, table_name, column_name)
        )
        return result is not None
    
    def get_placeholder(self) -> str:
        return "%s"
    
    def get_auto_increment(self) -> str:
        return "INT AUTO_INCREMENT PRIMARY KEY"
    
    def get_current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP"
    
    def db_exists(self) -> bool:
        """For MySQL, always return True (database should be created externally)"""
        return True


# Global adapter instance
_adapter: Optional[DatabaseAdapter] = None


def get_adapter() -> DatabaseAdapter:
    """Get the global database adapter"""
    global _adapter
    if _adapter is None:
        raise RuntimeError("Database adapter not initialized. Call init_adapter() first.")
    return _adapter


async def init_adapter() -> DatabaseAdapter:
    """Initialize the global database adapter based on config"""
    global _adapter
    
    if _adapter is not None:
        return _adapter
    
    db_type = config.db_type.lower()
    
    if db_type == "mysql":
        _adapter = MySQLAdapter()
    else:
        # Default to SQLite
        _adapter = SQLiteAdapter(config.sqlite_path)
    
    await _adapter.initialize()
    return _adapter


async def close_adapter():
    """Close the global database adapter"""
    global _adapter
    if _adapter:
        await _adapter.close()
        _adapter = None
