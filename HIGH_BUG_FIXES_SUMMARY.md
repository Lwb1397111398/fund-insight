# High 级别 Bug 修复总结

> 历史记录说明：本文记录一次 High 级别 Bug 修复，不代表当前完整架构或全部已知问题。当前项目入口请优先阅读 `AGENTS.md` / `CLAUDE.md`，整体架构请阅读 `ARCHITECTURE.md`。

## 修复概述

已成功修复 3 个服务文件中的 High 级别 bug，涉及并发安全、数据一致性和内存管理。

---

## 1. src/services/fund_service.py

### 1.1 修复竞态条件 - increment_predictions() 和 decrement_predictions()

**问题描述：**
原始代码使用读取-修改-写模式，存在典型的竞态条件：
```python
# 旧代码（存在竞态条件）
fund = self.get_by_code(fund_code)  # 步骤1: 读取
if fund:
    fund.active_predictions = (fund.active_predictions or 0) + 1  # 步骤2: 修改
    self.db.commit()  # 步骤3: 写入
```

在多线程/并发场景下，两个线程可能同时读取相同的 `active_predictions` 值，导致更新丢失。

**修复方案：**
使用数据库级别的原子操作 `UPDATE ... SET active_predictions = active_predictions + 1`：

```python
from sqlalchemy import update as sql_update

# 新代码（原子操作，无竞态条件）
stmt = sql_update(FundInfo).where(
    FundInfo.fund_code == fund_code
).values(
    active_predictions=FundInfo.active_predictions + 1,
    can_delete=False
)
result = self.db.execute(stmt)
self.db.commit()
```

**优势：**
- 原子性：数据库保证操作的原子性，无需应用层锁
- 并发安全：即使多个请求同时执行，也不会丢失更新
- 性能更好：减少了一次 SELECT 查询

### 1.2 添加多进程部署警告 - update_all_funds()

**问题描述：**
原始代码使用全局变量 `_is_updating` 和 `threading.Lock` 防止重复执行，但在多进程部署（如 Gunicorn with multiple workers）时无效：

```python
# 原始代码（仅单进程有效）
_update_lock = threading.Lock()
_is_updating = False

def update_all_funds(self):
    global _is_updating
    if _is_updating:  # 这个检查在多进程中无效
        return ...
```

**修复方案：**
添加清晰的注释说明这是单进程保护，多进程需要数据库锁：

```python
# 基金更新锁，防止重复执行
# ⚠️ 注意：这是单进程保护机制，多进程部署时无效
# 多进程场景需要使用数据库级别的锁（如 PostgreSQL 的 advisory lock）或分布式锁（如 Redis）
_update_lock = threading.Lock()
_is_updating = False
```

**建议后续改进：**
- 使用 PostgreSQL 的 `pg_try_advisory_lock()` 实现数据库级锁
- 或使用 Redis 的 `SETNX` 命令实现分布式锁

---

## 2. src/services/post_service.py

### 2.1 添加缺失的 import json

**问题描述：**
代码中使用了 `json.dumps()` 但未导入 `json` 模块，导致运行时 `NameError`。

**修复方案：**
在文件顶部添加 `import json`：

```python
import re
import logging
import json  # 新增导入
```

### 2.2 统一返回字典结构 - create_post_with_analysis()

**问题描述：**
`create_post_with_analysis()` 方法的多个返回分支返回的字典结构不一致，缺少 `success` 字段：

```python
# 异步模式返回（缺少 success）
return {
    "id": db_post.id,
    "title": db_post.title,
    "auto_titled": auto_titled,
    "analyzed": False,
    "predictions_created": 0,
    "message": "帖子已添加，请手动点击分析"
}

# 分析失败返回（有 success）
return {
    "success": False,
    "message": "分析失败：LLM未能提取有效预测",
    "predictions_created": 0
}
```

**修复方案：**
确保所有返回分支都包含 `success` 字段，并保持一致的字典结构：

```python
# 异步模式返回（现在包含 success）
return {
    "success": True,
    "id": db_post.id,
    "title": db_post.title,
    "auto_titled": auto_titled,
    "analyzed": False,
    "predictions_created": 0,
    "message": "帖子已添加，请手动点击分析"
}

# 分析失败返回（保持一致性）
return {
    "success": False,
    "id": db_post.id,
    "title": db_post.title,
    "auto_titled": auto_titled,
    "analyzed": False,
    "predictions_created": 0,
    "message": "分析失败：LLM未能提取有效预测"
}
```

### 2.3 事务一致性 - 同步模式分析

**问题描述：**
同步模式下，LLM 分析成功但后续创建预测失败会导致数据不一致：
- 帖子已标记为 `analyzed = True`
- 但实际没有创建任何预测
- 用户看到"分析成功"但预测列表为空

**修复方案：**
将整个操作包裹在单个事务中，失败时回滚：

```python
try:
    # LLM 分析
    result = llm_analyzer.analyze_post(...)
    
    # 创建预测（如果失败会触发异常）
    for pred in result.get("predictions", []):
        prediction = Prediction(...)
        self.db.add(prediction)
        predictions_created += 1
    
    # 所有操作成功后才提交
    self.db.commit()

except Exception as e:
    # 回滚事务，保持数据一致性
    self.db.rollback()
    
    # 更新帖子状态为失败
    db_post.analyzed = False
    db_post.analysis_result = json.dumps({"predictions": [], "summary": f"分析失败: {str(e)[:100]}"})
    self.db.commit()
```

**优势：**
- 原子性：要么全部成功，要么全部回滚
- 一致性：不会出现"分析成功但无预测"的中间状态
- 可追溯：失败时记录错误信息到 `analysis_result`

---

## 3. src/services/prediction_verify_service.py

### 3.1 LRU 缓存机制 - _nav_cache

**问题描述：**
`_nav_cache` 是实例级缓存，没有大小限制，在批量验证大量预测时可能导致内存溢出：

```python
# 原始代码（无大小限制）
self._nav_cache: Dict = {}

def get_nav_by_date(self, fund_code: str, target_date: date):
    cache_key = (fund_code, target_date.isoformat())
    if cache_key in self._nav_cache:
        return self._nav_cache[cache_key]
    # ... 添加到缓存（无限制）
    self._nav_cache[cache_key] = nav_record.nav
```

**修复方案：**
实现 LRU（Least Recently Used）缓存淘汰机制，限制最大条目数：

```python
class PredictionVerifyService:
    # 缓存最大条目数，防止内存溢出
    MAX_CACHE_SIZE = 10000

    def __init__(self, db: Session):
        self._nav_cache: Dict = {}
        self._cache_order: list = []  # 记录缓存插入顺序，用于 LRU 淘汰

    def _add_to_cache(self, key, value):
        """添加条目到缓存，使用 LRU 淘汰策略"""
        # 如果 key 已存在，先删除旧的顺序记录
        if key in self._nav_cache:
            self._cache_order.remove(key)
        # 如果缓存已满，淘汰最早的条目
        elif len(self._cache_order) >= self.MAX_CACHE_SIZE:
            oldest_key = self._cache_order.pop(0)
            del self._nav_cache[oldest_key]

        # 添加新条目
        self._nav_cache[key] = value
        self._cache_order.append(key)

    def get_nav_by_date(self, fund_code: str, target_date: date):
        cache_key = (fund_code, target_date.isoformat())
        if cache_key in self._nav_cache:
            # 更新 LRU 顺序
            self._cache_order.remove(cache_key)
            self._cache_order.append(cache_key)
            return self._nav_cache[cache_key]
        # ... 使用 _add_to_cache() 添加新条目
```

**优势：**
- 内存安全：缓存大小有上限，不会无限增长
- LRU 策略：自动淘汰最近最少使用的条目，保留热点数据
- 透明使用：调用方无需关心缓存细节

**性能考虑：**
- `MAX_CACHE_SIZE = 10000` 可支持约 100 个基金 × 100 天的缓存
- 对于批量验证场景，预热缓存后命中率很高
- LRU 淘汰使用列表，时间复杂度 O(n)，但对于 10000 条目规模可接受

---

## 测试建议

### 1. 并发测试（fund_service.py）
```python
import threading
import concurrent.futures

def test_concurrent_increment():
    """测试并发 increment_predictions"""
    def increment_task(fund_code):
        service = FundService(db_session)
        service.increment_predictions(fund_code)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(increment_task, "000001") for _ in range(100)]
        concurrent.futures.wait(futures)
    
    # 验证 active_predictions 是否正确增加了 100
```

### 2. 事务回滚测试（post_service.py）
```python
def test_transaction_rollback():
    """测试分析失败时事务是否正确回滚"""
    # 模拟 LLM 分析成功但创建预测失败的情况
    # 验证帖子状态是否回滚为 analyzed=False
```

### 3. 缓存淘汰测试（prediction_verify_service.py）
```python
def test_cache_eviction():
    """测试 LRU 缓存淘汰机制"""
    service = PredictionVerifyService(db_session)
    
    # 添加超过 MAX_CACHE_SIZE 个条目
    for i in range(11000):
        service._add_to_cache(f"key_{i}", f"value_{i}")
    
    # 验证缓存大小不超过 MAX_CACHE_SIZE
    assert len(service._nav_cache) == 10000
    assert len(service._cache_order) == 10000
```

---

## 影响范围

- ✅ **并发安全**：修复了 `increment_predictions()` 和 `decrement_predictions()` 的竞态条件
- ✅ **数据一致性**：确保 LLM 分析和预测创建的原子性
- ✅ **内存管理**：防止 `_nav_cache` 无限增长导致内存溢出
- ✅ **代码质量**：统一返回结构，添加必要的导入和注释

## 向后兼容性

所有修复都是向后兼容的：
- 数据库表结构无需修改
- API 接口签名不变
- 返回值结构保持一致（只是添加了 `success` 字段）

---

**修复完成时间：** 2026-06-14
**修复工程师：** Claude Code
