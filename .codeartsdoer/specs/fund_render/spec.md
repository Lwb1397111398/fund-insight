# **1. 组件定位**

## **1.1 核心职责**

本组件负责修复 Fund Insight 系统在 Render 平台部署后无法访问的问题，并增加 PostgreSQL 数据库支持和本地数据上传到云端网站的完整能力。

## **1.2 核心输入**

1. 用户通过浏览器访问 Render 部署的 Fund Insight 网站
2. 用户上传本地 SQLite 数据库文件到云端
3. 环境变量 `DATABASE_URL` 指定 PostgreSQL 连接字符串
4. 前端页面发起的 API 请求（需携带访问密码）

## **1.3 核心输出**

1. 正常可访问的 Web 界面和 API 接口
2. PostgreSQL 数据库中存储的业务数据
3. 数据导入结果（成功/失败/导入记录数）

## **1.4 职责边界**

1. 不负责 Render 平台的基础设施运维
2. 不负责 PostgreSQL 数据库服务器的创建和运维（由 Render 托管）
3. 不负责 LLM API 密钥的管理

# **2. 领域术语**

**Render 部署**
: 将应用部署到 Render.com 云平台，通过公网 URL 提供服务。

**PostgreSQL**
: 开源关系型数据库，Render 平台提供的托管数据库服务，支持通过 `DATABASE_URL` 环境变量连接。

**SQLite**
: 轻入式文件数据库，用于本地开发和数据存储，不支持云端多实例并发访问。

**数据迁移**
: 将本地 SQLite 数据库中的业务数据导入到云端 PostgreSQL 数据库的过程。

**访问密码中间件**
: HTTP 请求中间件，验证请求头 `X-Access-Password` 是否匹配环境变量 `ACCESS_PASSWORD`，未匹配则返回 401。

# **3. 角色与边界**

## **3.1 核心角色**

- 网站管理员：拥有访问密码，可管理博主、帖子、预测等数据，可执行数据导入操作
- 普通访客：访问 Render 部署的网站首页

## **3.2 外部系统**

- Render.com：云平台，提供 Web 服务托管和 PostgreSQL 数据库
- 火山引擎/硅基流动：LLM API 提供商
- 天天基金：基金数据源 API

## **3.3 交互上下文**

```plantuml
@startuml
left to right direction

actor "网站管理员" as admin
actor "普通访客" as visitor

rectangle "Fund Insight" as app {
}

cloud "Render.com" as render
database "PostgreSQL" as pg
cloud "LLM API" as llm
cloud "天天基金" as fund

admin --> app : 管理数据/导入数据
visitor --> app : 访问网站
app --> render : 部署运行
app --> pg : 读写数据
app --> llm : AI分析
app --> fund : 基金数据
@enduml
```

# **4. DFX约束**

## **4.1 性能**

1. 网站首页加载时间必须 ≤ 5 秒
2. API 接口响应时间必须 ≤ 3 秒（不含 LLM 调用）
3. 数据导入接口支持 ≤ 50MB 的 SQLite 文件上传

## **4.2 可靠性**

1. 数据库连接失败时必须返回明确的错误信息，不能导致应用崩溃
2. SQLite 到 PostgreSQL 的数据导入必须支持部分成功（某表失败不影响其他表）
3. 应用启动时必须自动创建所有数据库表

## **4.3 安全性**

1. 所有 API 接口（除健康检查外）必须验证访问密码
2. 前端页面必须配置 axios 默认携带访问密码请求头
3. `DATABASE_URL` 等敏感配置必须通过环境变量注入，不能硬编码

## **4.4 可维护性**

1. 数据库引擎选择逻辑必须集中在一处，通过 `DATABASE_URL` 环境变量自动切换
2. 日志必须输出当前使用的数据库类型和连接状态

## **4.5 兼容性**

1. 未配置 `DATABASE_URL` 时必须回退到 SQLite，保持本地开发兼容
2. 数据导入接口必须兼容现有 SQLite 数据库的表结构

# **5. 核心能力**

## **5.1 Render 部署修复**

### **5.1.1 业务规则**

1. **前端静态资源路径必须正确**：部署到 Render 后，前端页面引用的 JS/CSS 资源路径必须可访问
   - 验收条件：[访问 Render 部署的网站根路径] → [返回完整的 HTML 页面，JS/CSS 资源可加载]

2. **前端 API 请求必须携带访问密码**：所有 axios 请求必须默认携带 `X-Access-Password` 请求头
   - 验收条件：[前端发起任何 API 请求] → [请求头包含 X-Access-Password]

3. **CORS 配置必须允许前端跨域访问**：Render 部署后前后端可能不同源
   - 验收条件：[浏览器发起跨域 API 请求] → [请求成功，不被 CORS 阻止]

4. **render.yaml 必须包含所有必要的环境变量配置**
   - 验收条件：[Render 自动部署] → [应用正常启动，所有功能可用]

5. **禁止项**：前端不能硬编码 localhost 地址
   - 验收条件：[前端代码搜索 localhost/127.0.0.1] → [无匹配结果]

### **5.1.2 交互流程**

```plantuml
@startuml
actor "用户" as user
participant "Render" as render
participant "Fund Insight" as app
database "PostgreSQL" as db

user -> render : 访问 https://fund-insight.onrender.com
render -> app : 路由请求
app -> db : 初始化连接
db --> app : 连接成功
app --> render : 返回 HTML 页面
render --> user : 渲染页面
@enduml
```

### **5.1.3 异常场景**

1. **数据库连接失败**
   - 触发条件：`DATABASE_URL` 配置错误或 PostgreSQL 服务不可用
   - 系统行为：输出详细错误日志，尝试回退到 SQLite
   - 用户感知：健康检查接口返回 `{"status": "error", "detail": "数据库连接失败"}`

2. **访问密码未配置**
   - 触发条件：Render 环境变量未设置 `ACCESS_PASSWORD`
   - 系统行为：使用默认密码，输出警告日志
   - 用户感知：使用默认密码可正常访问

## **5.2 PostgreSQL 数据库支持**

### **5.2.1 业务规则**

1. **数据库引擎自动选择**：当 `DATABASE_URL` 环境变量以 `postgresql` 开头时，必须使用 PostgreSQL；否则回退到 SQLite
   - 验收条件：[设置 DATABASE_URL=postgresql://...] → [使用 PostgreSQL 引擎]
   - 验收条件：[未设置 DATABASE_URL] → [使用 SQLite 引擎]

2. **PostgreSQL 连接池配置**：使用 PostgreSQL 时必须配置连接池，避免连接泄漏
   - 验收条件：[高并发请求] → [数据库连接数不超过池大小]

3. **自动建表**：应用启动时必须自动创建所有数据库表（无论 PostgreSQL 还是 SQLite）
   - 验收条件：[首次启动连接空数据库] → [所有业务表自动创建]

4. **init_db 函数必须兼容两种数据库**：打印日志时必须显示当前使用的数据库类型
   - 验收条件：[使用 PostgreSQL 启动] → [日志显示 "已初始化: PostgreSQL"]
   - 验收条件：[使用 SQLite 启动] → [日志显示 "已初始化: SQLite: /path/to/db"]

### **5.2.2 交互流程**

```plantuml
@startuml
participant "应用启动" as app
database "数据库" as db

app -> app : 读取 DATABASE_URL 环境变量
alt DATABASE_URL 以 postgresql 开头
    app -> db : 创建 PostgreSQL 引擎（含连接池）
else
    app -> db : 创建 SQLite 引擎
end
app -> db : Base.metadata.create_all()
db --> app : 表创建完成
app -> app : 输出数据库类型日志
@enduml
```

### **5.2.3 异常场景**

1. **PostgreSQL 驱动缺失**
   - 触发条件：`psycopg2-binary` 未安装但配置了 PostgreSQL
   - 系统行为：输出安装提示，回退到 SQLite
   - 用户感知：应用仍可启动，使用 SQLite 模式

## **5.3 本地数据上传到云端**

### **5.3.1 业务规则**

1. **数据导入接口必须存在**：提供 HTTP POST 接口，接受 SQLite 文件上传，将数据导入到当前连接的数据库
   - 验收条件：[POST /api/import-database 上传 .db 文件] → [数据导入到 PostgreSQL]

2. **导入必须支持所有核心业务表**：至少包含 bloggers、posts、predictions、viewpoints、fund_info、fund_history、sector_fund_mapping、investment_advice、crawler_article_records
   - 验收条件：[上传包含所有表数据的 SQLite 文件] → [所有表数据成功导入]

3. **导入前必须清空目标表**：防止主键冲突和数据重复
   - 验收条件：[重复导入同一文件] → [目标表数据为最新导入的数据]

4. **导入结果必须返回详细统计**：每张表的导入记录数
   - 验收条件：[导入完成] → [返回 {"success": true, "imported": {"bloggers": 5, "posts": 20, ...}}]

5. **数据导入页面必须可访问**：提供 Web 界面供用户上传文件
   - 验收条件：[访问 /import-data.html] → [显示文件上传界面]

6. **禁止项**：导入过程中不能跳过自增 ID 处理，必须让目标数据库自动生成 ID
   - 验收条件：[导入含 ID 的数据] → [目标表 ID 为自动生成，不使用源表 ID]

### **5.3.2 交互流程**

```plantuml
@startuml
actor "管理员" as admin
participant "Fund Insight" as app
database "SQLite(本地)" as sqlite
database "PostgreSQL(云端)" as pg

admin -> app : 访问 /import-data.html
app --> admin : 显示上传界面
admin -> app : 上传 fund_insight.db 文件
app -> sqlite : 读取源数据库各表数据
sqlite --> app : 返回行数据
app -> pg : 清空目标表
app -> pg : INSERT 数据（不含 ID）
pg --> app : 导入完成
app --> admin : 返回导入统计结果
@enduml
```

### **5.3.3 异常场景**

1. **上传文件格式错误**
   - 触发条件：上传的文件不是 .db 或 .sqlite 格式
   - 系统行为：拒绝处理
   - 用户感知：提示"请选择 .db 或 .sqlite 格式的数据库文件"

2. **源数据库表不存在**
   - 触发条件：上传的 SQLite 文件缺少某些业务表
   - 系统行为：跳过该表，继续导入其他表
   - 用户感知：该表导入计数为 0

3. **数据类型不兼容**
   - 触发条件：SQLite 中的数据类型与 PostgreSQL 列类型不匹配
   - 系统行为：跳过该行，记录错误日志
   - 用户感知：该表导入计数可能少于源表行数

# **6. 数据约束**

## **6.1 数据库连接配置**

1. **DATABASE_URL**：PostgreSQL 连接字符串，格式为 `postgresql://user:password@host:port/dbname`
2. **DB_PATH**：SQLite 数据库文件路径，仅在未配置 DATABASE_URL 时使用
3. **ACCESS_PASSWORD**：API 访问密码，未配置时使用默认值

## **6.2 导入数据约束**

1. **上传文件大小**：必须 ≤ 50MB
2. **支持的文件扩展名**：.db、.sqlite
3. **导入表列表**：bloggers、posts、predictions、viewpoints、fund_info、fund_history、sector_fund_mapping、investment_advice、crawler_article_records
