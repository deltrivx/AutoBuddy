## 智能路由页：路由规则恢复 + 分流错判修复、magpie 残留清除、实时网关真实标识、移动端居中

本版回应反馈，把 v2.3.8 下线的「路由规则」卡片恢复，并修掉了它真正的 bug——不是没效果，而是匹配逻辑写错了；同时清掉移植残留的 magpie 标识、让实时网关左右两侧显示真实智能体/API、移动端顶端与多账号区居中对齐。

### 改了什么

- **路由规则卡片恢复**：按反馈恢复前端 `id="wb-rt-rules"` 整段展示、`new-rule`/`import-rule` 按钮与 `data-ract`/`data-re` 规则编辑器事件分支，并还原 `wbRenderRules`/`wbRuleEditForm`/`openRuleEditor`/`saveRuleEditor` 函数；规则编辑器匹配字段默认改为 `variant`（cn/ai）。
- **路由分流错判修复（关键）**：旧自动生成规则按 `client_tag contains "ai"`/`"cn"` 匹配，而 `client_tag` 是客户端 user-agent（如 `OpenAI/JS 7.28.0`），`"ai"` 是 `"OpenAI"` 的子串，导致**每条请求都误命中「国际版账号分流」**，国内版账号却显示命中国际版规则。现改为按请求模型的真实上游变体 `variant`（cn/ai）做 `eq` 匹配：`main.py` 新增 `_model_variant()` 按模型目录 `owned_by` 解析（tencent-codebuddy 系为 cn，其余为 ai），`routing._rule_matches` 新增 `match_field="variant"` 支持，`auto_generate` 重新生成时清理历史遗留的 `client_tag` 坏规则。
- **实时网关左右两侧标识失真修复**：左侧来源节点在 `caller_name=="gateway-local"`（客户端未带 `x-autobuddy-client` 头）时，回退到真实 `client_tag`（user-agent）并归一化显示真实智能体（OpenClaw/DeepSeek/ZCode 等）；右侧焦点框改为显示「API 名：模型」（新增 `key_name` 字段，后端从 `gate.get("keyName")` 取密钥可读名，前端 `api` 优先用 `key_name`）。
- **路由页残留 magpie 标识清除**：网关 hub 节点 `<small>magpie</small>` 改为 `<small>AutoBuddy</small>`。
- **移动端顶端居中**：`@media (max-width:760px)` 下 `.am-head` 改为纵向居中、`.rt-top`/`.rt-stage` 居中堆叠，修复移植 Magpie 且取消连线后顶部未居中的违和感。
- **移动端多账号 / 多 KEY 留白修复**：`.wb-rt-prov-card` 标题与分组头居中、`.am-g`/`.am-gh`/`.wb-rt-cfg-row`/`.am-seg` 窄屏居中堆叠，消除右侧大片空白。

### 影响面

- 改变智能路由页的可见规则卡片与实时网关的左右标识含义；分流逻辑从「按 user-agent 子串」改为「按模型变体」，国内版/国际版账号现在能正确分流。
- 重新点一次「⚡ 重新生成默认编排」即可把生产环境里遗留的坏规则替换成新的 variant 规则。
- 图标与前端随镜像构建打包，运行时不依赖外网。
