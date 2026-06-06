# Fund Insight Product Context

## Register
product

## Product Purpose
Fund Insight 是基金博主分析系统，用于追踪和分析博主在天天基金等平台发布的基金投资预测。用户手动粘贴帖子内容，系统通过 LLM 提取预测观点、关联基金，并对比基金净值验证预测准确率。

## Users
主要用户是需要长期跟踪基金观点、管理博主、核验预测表现的个人投资研究者或项目维护者。界面服务于高频查看、录入、分析、验证和清理数据。

## Strategic Principles
- 优先清晰、稳定、可快速扫描，不追求营销式视觉冲击。
- 关键数字和状态要易读，避免装饰压过数据本身。
- Render 部署资源有限，前端美化不能引入构建链路或重依赖。
- 当前前端是原生 HTML/JS + Vue CDN，改动应低风险、可直接部署。

## Anti-references
- 不做金融终端式高压暗黑界面。
- 不做 SaaS  landing page 风格的大渐变和夸张指标英雄区。
- 不使用复杂动画、重图片或外部字体依赖。
