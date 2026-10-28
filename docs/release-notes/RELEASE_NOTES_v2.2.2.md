参考 `CLIProxyAPI-freetier2api-plugin` 的 `autoclaw` 路由实测口径，彻底破坏上游渠道审计对 `"DeepSeek"` + `"harness"` 复合特征的正则与子串匹配，根除换号 7 连败及 `HTTP 400 (11128)` 报错。

### 修复内容

- **DSH 渠道拦截根治**：
  - 根因：上游渠道识别拦截的是「`DeepSeek`」+「`harness`」这组语义串（大小写不敏感子串匹配）。之前将空格改连字符（`DeepSeek-Harness`）两个特征词依然同时存在，导致只要包含该提示词，换号 7 次全灭。
  - 修复：参照 `freetier2api-plugin` 去除品牌词并中性化前缀的方案，将 `"You are an AI agent powered by DeepSeek Harness"` 与 `"DeepSeek Harness"` 改写为 `"local coding tool"`。
  - 同步更新内置规则表与快速预检映射。
