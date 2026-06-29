# 安全修复报告

## 修复的 High 级别安全 Bug

### 1. SQL 注入风险 (import_database 路由)
**位置**: `src/api/main.py` 第 538-540 行
**问题**: 使用字符串拼接构造 SQL 查询 `f"SELECT * FROM {table_name}"`，table_name 来自用户可控的 orm_map
**修复**: 添加表名白名单验证 `ALLOWED_IMPORT_TABLES`，只允许已知的 19 个表名

```python
# 允许导入的表名白名单（防止 SQL 注入）
ALLOWED_IMPORT_TABLES = {
    'bloggers', 'posts', 'predictions', 'viewpoints', 'fund_info',
    'fund_history', 'sector_fund_mapping', 'investment_advice',
    'crawler_article_records', 'prediction_groups', 'batch_analysis_tasks',
    'user_fund_bindings', 'sync_logs', 'fund_holdings', 'market_data',
    'policy_data', 'sentiment_data', 'sector_fund_flow'
}

# 验证所有表名都在白名单中（防御性编程）
for table_name, _ in orm_map:
    if table_name not in ALLOWED_IMPORT_TABLES:
        raise HTTPException(status_code=400, detail=f"未授权的表名: {table_name}")
```

### 2. 文件上传未验证 (import_database 路由)
**位置**: `src/api/main.py` 第 467-485 行
**问题**: 文件上传后未对文件类型和内容进行验证，可能导致任意文件上传
**修复**: 三重验证机制

```python
# 1. 验证文件扩展名（必须为 .db）
if not file.filename or not file.filename.endswith('.db'):
    raise HTTPException(status_code=400, detail="仅支持 .db 文件")

# 2. 验证文件大小（最大 100MB）
max_size = 100 * 1024 * 1024  # 100MB
file.file.seek(0, 2)
file_size = file.file.tell()
file.file.seek(0)
if file_size > max_size:
    raise HTTPException(status_code=400, detail="文件大小超过限制（最大 100MB）")

# 3. 验证文件头魔术字节（SQLite 数据库验证）
with open(temp_file, "rb") as f:
    header = f.read(16)
    if header != b"SQLite format 3\x00":
        os.remove(temp_file)
        os.rmdir(temp_dir)
        raise HTTPException(status_code=400, detail="无效的 SQLite 数据库文件")
```

### 3. 异步阻塞 (import_database 路由)
**位置**: `src/api/main.py` 第 467-468 行
**问题**: 路由定义为 `async def`，但内部全是同步阻塞操作（文件 I/O、数据库操作），会阻塞事件循环
**修复**: 将路由改为同步函数（`def`），因为这是后台管理接口，不需要异步

```python
@app.post("/api/import-database")
def import_database(file: UploadFile = File(...), request: Request = None):
    # 同步函数，适合后台管理接口
```

### 4. SQL 注入风险 (_run_startup_migrations 函数)
**位置**: `src/api/main.py` 第 115-117 行
**问题**: 虽然 idx_name 来自硬编码列表，但为了安全，添加白名单验证
**修复**: 添加表名白名单验证 `ALLOWED_INDEX_TABLES`

```python
# 允许创建索引的表名白名单（防止 SQL 注入）
ALLOWED_INDEX_TABLES = {"posts", "predictions", "viewpoints", "bloggers"}

# 验证所有表名都在白名单中（防御性编程）
for idx_name, table, columns in indexes:
    if table not in ALLOWED_INDEX_TABLES:
        logger.warning(f"[Startup] 跳过未授权的表 {table} 的索引创建")
        indexes.remove((idx_name, table, columns))
```

## 修复原则

1. **白名单验证**: 对所有用户可控的输入（表名、文件名）进行白名单验证
2. **深度防御**: 文件上传采用三重验证（扩展名、大小、魔术字节）
3. **最小权限**: 只允许访问预定义的表名，拒绝未知表名
4. **同步/异步分离**: 根据实际场景选择合适的函数类型
5. **防御性编程**: 即使输入来自硬编码列表，也进行验证

## 测试建议

1. 验证无法上传非 .db 文件
2. 验证无法上传超过 100MB 的文件
3. 验证无法上传伪造的 SQLite 文件
4. 验证 import_database 路由在同步模式下正常工作
5. 验证 _run_startup_migrations 正常创建索引
