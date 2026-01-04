# 性能优化指南

## 已实施的优化

### 1. 数据库连接池 (`src/core/db_pool.py`)
- 实现了读写分离的 SQLite 连接池
- 启用 WAL 模式提高并发性能
- 写操作使用独占锁确保数据一致性
- 读操作使用连接池并发处理

**使用方法：**
```python
from src.core.db_pool import init_pool, get_pool

# 初始化（在应用启动时）
await init_pool(db_path, pool_size=5)

# 读操作
pool = get_pool()
async with pool.read_connection() as conn:
    cursor = await conn.execute("SELECT * FROM tokens")
    rows = await cursor.fetchall()

# 写操作
async with pool.write_connection() as conn:
    await conn.execute("INSERT INTO ...")
    await conn.commit()
```

### 2. Token 缓存 (`src/services/token_cache.py`)
- 内存缓存活跃 Token 列表
- 30 秒 TTL 自动过期
- 读操作无锁，高并发友好
- 支持增量更新

### 3. 负载均衡器优化 (`src/services/load_balancer.py`)
- Token 自动刷新检查移到后台任务
- 限制刷新检查频率（5分钟一次）
- Sora2 配额刷新异步执行，不阻塞请求

### 4. 批量测试并发控制 (`src/services/token_manager.py`)
- 使用信号量限制并发数（默认 5）
- 减少测试间隔（0.5s → 0.3s）
- 并发执行提高效率

### 5. Cloudflare 状态锁优化 (`src/services/cloudflare_solver.py`)
- 使用 `threading.RLock` 替代 `threading.Lock`
- 避免同一线程重入死锁

### 6. 自适应轮询系统 (`src/services/generation_handler.py`)
- 根据生成进度动态调整轮询间隔
- 初始阶段 (0-30%): 5 秒间隔
- 中间阶段 (30-70%): 3 秒间隔
- 最终阶段 (70-100%): 2 秒间隔
- 停滞检测：连续 3 次无进度变化时增加间隔
- 最大间隔限制：10 秒

### 7. 批量操作并发控制
- 批量测试 Token：最大 5 并发
- 批量激活 Sora2：最大 3 并发
- 批量添加 Token：顺序处理，带重复检测

## 待优化项

### 高优先级

1. **数据库操作迁移到连接池**
   - 修改 `Database` 类使用连接池
   - 需要逐步迁移所有数据库操作

2. **Token 管理器使用缓存**
   - `get_active_tokens()` 使用缓存
   - `get_all_tokens()` 使用缓存
   - 写操作后更新缓存

### 中优先级

3. **HTTP Session 清理**
   - `SoraClient._sessions` 添加 LRU 清理机制
   - 限制最大 session 数量

4. **日志优化**
   - 减少高频日志输出
   - 使用异步日志写入

### 低优先级

5. **配置缓存**
   - 缓存数据库配置（proxy_config, watermark_free_config 等）
   - 减少配置查询频率

## 性能监控建议

1. **添加请求计时**
```python
import time

start = time.time()
# ... 操作 ...
duration = time.time() - start
if duration > 1.0:  # 超过 1 秒记录警告
    logger.warning(f"Slow operation: {duration:.2f}s")
```

2. **监控数据库连接**
```python
# 在连接池中添加统计
pool.stats()  # 返回 {active: 3, idle: 2, waiting: 0}
```

3. **监控锁等待时间**
```python
# 在关键锁上添加等待时间统计
async with timed_lock(self._lock, "token_lock") as wait_time:
    if wait_time > 0.1:
        logger.warning(f"Lock wait: {wait_time:.3f}s")
```

## 配置建议

### SQLite 优化
```sql
PRAGMA journal_mode=WAL;      -- 启用 WAL 模式
PRAGMA synchronous=NORMAL;    -- 平衡性能和安全
PRAGMA cache_size=10000;      -- 增加缓存大小
PRAGMA temp_store=MEMORY;     -- 临时表存内存
```

### 并发配置
- 数据库连接池大小：5-10（根据并发量调整）
- 批量测试并发数：5（避免触发 API 限流）
- Token 缓存 TTL：30 秒（平衡实时性和性能）
