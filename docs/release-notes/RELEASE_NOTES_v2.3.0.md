# AutoBuddy v2.3.0 发布说明

## 核心新增：智能路由（第 7 核心功能，对标 Magpie routing group）

Magpie 的路由核心是 **routing group + smart failover**：把多个账号/模型编成一组，请求按顺序尝试，遇到额度耗尽或报错就自动切到下一个，调用方全程无感。AutoBuddy 内部已有账号池轮询、能力矩阵换号、可用性巡检这套“黑盒自动换号”能力，本版本把它**前台化 + 可编排化**。

### 功能要点

1. **路由规则**：按 `model` / `api_key` / `client_tag` 匹配请求，命中后分流到指定**路由组** / **指定账号** / **指定模型**。支持 `eq`（精确）与 `contains`（子串），大小写不敏感。多规则按 `priority` 升序，首条命中即胜出。
2. **路由组**：把多个账号编成有序成员列表，支持三种模式：
   - `smart`：按成员顺序优先（组内故障转移由既有换号重试循环天然承担）
   - `round_robin`：组内按顺序轮转，均衡负载
   - `order`：严格按成员顺序
   
   兜底策略：
   - `global_pool`：组内无可用成员时退回全局账号池（默认，最稳）
   - `error`：严格按编排，组内耗尽即向上游返回真实错误，绝不偷偷绕到全局账号
3. **透明集成**：路由决策层在 `select_account` 选号**之前**插入，复用既有换号重试、能力矩阵学习与可用性巡检——故障转移对调用方完全透明。未命中任何规则时照常走全局账号池，**零侵入**。
4. **WebUI**：顶栏新增第四字选项卡「**智能路由**」，与数据总览/每日任务同一套 hash 视图与移动端收纳面板机制，复用现有 1180px 居中容器与 CSS 变量主题。规则/组列表纯展示，**不内联任何账号或密钥明文**。
5. **后端 API**（统一 `require_user` 鉴权门）：
   - `GET /routing/groups` / `POST /routing/groups` / `PUT /routing/groups/{id}` / `DELETE /routing/groups/{id}`
   - `GET /routing/rules` / `POST /routing/rules` / `PUT /routing/rules/{id}` / `DELETE /routing/rules/{id}`
   - `GET /routing/resolve`：预览一次路由解析结果（不实际选号）
6. **持久化**：路由组（`routing_groups`）与规则（`routing_rules`）落 SQLite，随 `/data` 卷持久化，容器升级/重建数据不丢。

### 与现有六大功能的关系

- **账号池 / 能力矩阵 / 可用性巡检** → 作为路由组的“成员来源”与“健康依据”
- **每日任务** → 不参与路由，但路由组成员状态可在拓扑中体现
- **API 网关** → 在 `/v1/chat/completions` 入口加路由解析层，对调用方透明
- **界面** → 新 tab 复用同一套顶栏/主题/响应式

### 升级说明

- 仅新增模块与表结构，无破坏性变更；旧版数据兼容。
- 升级后首次启动自动建表（`routing_groups` / `routing_rules`），无需手动迁移。
- 未配置任何路由规则时，全部请求行为与旧版完全一致（默认走全局账号池）。
