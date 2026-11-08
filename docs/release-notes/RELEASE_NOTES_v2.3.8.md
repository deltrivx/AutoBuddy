## 智能路由页：上游智能体名字归一化、移动端连线与留白修复、路由规则卡片下线、图标补全

本版在 v2.3.7「上游智能体图标化」基础上，按反馈修正了名字归一化、移动端残留连线与留白，并移除了没有实际收益的「路由规则」卡片，同时补全了一批常见智能体图标。

### 改了什么

- **上游智能体名字归一化**：`deepseek*` 开头的调用方（如 `deepseek-harness`、`DeepSeek-API`、`deepseek/api`）统一显示为 **DeepSeek** 并匹配 `deepseek.png` 图标，解决「deepseek harness 名字不符合预期」的问题。
- **移动端连接线残留移除**：`@media (max-width:760px)` 中 `.rt-sky / .rt-wires / .rt-flows` 设为 `display:none !important`，移动端不再显示来源→hub 的舞台连线。
- **移动端路由页底部留白修复**：`.rt-tools` 由 `width:100%; justify-content:flex-end` 改为 `width:auto; flex:0 0 auto` 内联到标题区，并补全 `.wb-rt-card / .wb-rt-row-head / .wb-rt-cols / .rt-reqs-tabs / .rt-tab` 窄屏适配，消除「路由页面后面按钮未适配导致大面积留白」。
- **移除「路由规则」卡片**：用户确认该卡片无实际收益、未展示任何有价值信息，移除前端 `id="wb-rt-rules"` 整段展示、`refreshGroups` 改为仅拉取 `/api/routing/groups`、删除 `new-rule`/`import-rule` 顶层按钮与 `data-ract`/`data-re` 规则编辑器事件分支，并清理 `wbRenderRules`/`wbRuleEditForm`/`openRuleEditor`/`saveRuleEditor` 等死代码（后端 `routing_rules` 表与分流逻辑保留，不影响网关分流）。
- **补全常见智能体图标**：`gateway/icons/` 新增智谱（zhipu/glm）、腾讯混元（hunyuan）、MiniMax（minimax）、百度文心（baidu/ernie）、百川（baichuan）、Ollama（ollama）、Perplexity（perplexity）、Poe（poe）、Mistral（mistral）、讯飞星火（spark）等官方/品牌图标，未知客户端统一回退 `generic-app.svg`；`manifest.json` 与前端 `wbRtCallerIcon` 映射表同步更新。

### 影响面

- 仅影响智能路由页视觉呈现与前端交互，不改变网关路由分流的核心逻辑。
- 图标随镜像构建打包进 `/app/gateway/icons/`，运行时不依赖外网拉取。
