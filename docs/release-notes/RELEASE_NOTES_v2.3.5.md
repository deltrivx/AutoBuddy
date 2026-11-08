# AutoBuddy v2.3.5 发布说明

> 对比基准：[v2.3.4](https://github.com/deltrivx/AutoBuddy/compare/v2.3.4...v2.3.5)

## 新增

- **智能路由页按 API（api_key）动态分类来源**：原先左侧来源节点写死显示「你的 Agent」，无法体现「有几个 API 在发请求」。现参考 Magpie 的动态来源显示方案（`client.js` 按 `r.agent` 去重、多个来源加 `.many` 类、名字从映射表查），改为扫描最近 60 条请求 trace，按 `api_key`（keyId）去重，每个在发请求的密钥渲染成一个 source 节点并显示其名字（如 `DeltrivX`、`我的iOS App`），无请求时回退显示「你的 Agent」。多来源时给 `.rt-srcs` 容器加 `.many` 类切换为横向并列布局。
- **账号行内嵌 API 标签**：右侧每个候选账号行末尾新增 `<span class="rt-api-tag">` 小标签，显示「该账号最近一次被哪个密钥（API）用过」，按 ts 取最新一次请求的 `api_key` 归因。
- **密钥名字映射**：进入路由页时拉取 `/api/api-keys` 建立 `id → name` 字典（`__wbRtKeyNames`），将 keyId 翻译成可读名字；映射缺失时回退显示 keyId 前 8 位。
- **多来源连线**：舞台连线从「只连第一个 source」改为遍历所有 source 节点，每个 API 都连到 hub，当前在用的那个高亮。
- **历史列表「按密钥」→「按 API」**：分组切换标签文案对齐用户的 API 维度说法，语义仍为按 `api_key` 聚合。
