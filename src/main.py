"""Main application entry point"""
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path

# Import modules
from .core.config import config
from .core.database import Database
from .core.db_pool import init_pool, close_pool
from .core.db_adapter import init_adapter, close_adapter
from .core.redis_manager import init_redis, close_redis
from .services.token_manager import TokenManager
from .services.proxy_manager import ProxyManager
from .services.load_balancer import LoadBalancer
from .services.sora_client import SoraClient
from .services.generation_handler import GenerationHandler
from .services.concurrency_manager import ConcurrencyManager
from .services.token_cache import get_token_cache
from .api import routes as api_routes
from .api import admin as admin_routes
from .api import public as public_routes
from .api import openai_compat as openai_routes
from .api import sora_compat as sora_routes

# Initialize FastAPI app
app = FastAPI(
    title="Sora2API",
    description="OpenAI compatible API for Sora",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize components
db = Database()
token_manager = TokenManager(db)
proxy_manager = ProxyManager(db)
concurrency_manager = ConcurrencyManager()
load_balancer = LoadBalancer(token_manager, concurrency_manager)
sora_client = SoraClient(proxy_manager)
generation_handler = GenerationHandler(sora_client, token_manager, load_balancer, db, proxy_manager, concurrency_manager)

# Set dependencies for route modules
api_routes.set_generation_handler(generation_handler)
admin_routes.set_dependencies(token_manager, proxy_manager, db, generation_handler, concurrency_manager)
public_routes.set_dependencies(token_manager, db, generation_handler)
openai_routes.set_generation_handler(generation_handler)
sora_routes.set_generation_handler(generation_handler)

# Include routers
app.include_router(api_routes.router)
app.include_router(admin_routes.router)
app.include_router(public_routes.router)
app.include_router(openai_routes.router)
app.include_router(sora_routes.router)

# Static files
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Cache files (tmp directory)
tmp_dir = Path(__file__).parent.parent / "tmp"
tmp_dir.mkdir(exist_ok=True)
app.mount("/tmp", StaticFiles(directory=str(tmp_dir)), name="tmp")

# Frontend routes
@app.get("/", response_class=HTMLResponse)
async def root():
    """Redirect to login page"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta http-equiv="refresh" content="0; url=/login">
    </head>
    <body>
        <p>Redirecting to login...</p>
    </body>
    </html>
    """

@app.get("/login", response_class=FileResponse)
async def login_page():
    """Serve login page"""
    return FileResponse(str(static_dir / "login.html"))

@app.get("/manage", response_class=FileResponse)
async def manage_page():
    """Serve management page"""
    return FileResponse(str(static_dir / "manage.html"))

@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    # Get config from setting.toml
    config_dict = config.get_raw_config()

    # Initialize database adapter (SQLite or MySQL based on config)
    adapter = await init_adapter()
    print(f"✓ Database adapter initialized ({config.db_type})")

    # Initialize Redis manager
    redis_mgr = await init_redis()
    if redis_mgr.is_connected:
        print("✓ Redis manager initialized (distributed mode)")
    else:
        print("✓ Redis manager initialized (local mode)")

    # Check if database exists (for SQLite)
    is_first_startup = not db.db_exists() if config.db_type == "sqlite" else False

    # Initialize database tables
    await db.init_db()

    # Initialize database connection pool for better concurrency (SQLite only)
    if config.db_type == "sqlite":
        await init_pool(db.db_path, read_pool_size=20)
        print("✓ Database connection pool initialized (read_pool_size=20, WAL mode)")

    # Handle database initialization based on startup type
    if is_first_startup:
        print("🎉 First startup detected. Initializing database and configuration from setting.toml...")
        await db.init_config_from_toml(config_dict, is_first_startup=True)
        print("✓ Database and configuration initialized successfully.")
    else:
        # 跳过迁移检查，直接加载配置
        pass

    # Load admin credentials from database
    admin_config = await db.get_admin_config()
    config.set_admin_username_from_db(admin_config.admin_username)
    config.set_admin_password_from_db(admin_config.admin_password)

    # Load cache configuration from database
    cache_config = await db.get_cache_config()
    config.set_cache_enabled(cache_config.cache_enabled)
    config.set_cache_timeout(cache_config.cache_timeout)
    config.set_cache_base_url(cache_config.cache_base_url or "")

    # Load generation configuration from database
    generation_config = await db.get_generation_config()
    config.set_image_timeout(generation_config.image_timeout)
    config.set_video_timeout(generation_config.video_timeout)

    # Load token refresh configuration from database
    token_refresh_config = await db.get_token_refresh_config()
    config.set_at_auto_refresh_enabled(token_refresh_config.at_auto_refresh_enabled)

    # Load Cloudflare Solver configuration from database
    await db.ensure_cloudflare_solver_config_row(config_dict)
    cloudflare_config = await db.get_cloudflare_solver_config()
    config.set_cf_enabled(cloudflare_config.solver_enabled)
    config.set_cf_api_url(cloudflare_config.solver_api_url)

    # Initialize concurrency manager with all tokens
    all_tokens = await db.get_all_tokens()
    await concurrency_manager.initialize(all_tokens)
    print(f"✓ Concurrency manager initialized with {len(all_tokens)} tokens")

    # Initialize token cache
    token_cache = get_token_cache()
    await token_cache.refresh(db)
    print(f"✓ Token cache initialized")

    # Start file cache cleanup task
    await generation_handler.file_cache.start_cleanup_task()

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    await generation_handler.file_cache.stop_cleanup_task()
    # Close database connection pool (SQLite only)
    if config.db_type == "sqlite":
        await close_pool()
        print("✓ Database connection pool closed")
    # Close database adapter
    await close_adapter()
    print("✓ Database adapter closed")
    # Close Redis manager
    await close_redis()
    print("✓ Redis manager closed")

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=config.server_host,
        port=config.server_port,
        reload=False
    )
