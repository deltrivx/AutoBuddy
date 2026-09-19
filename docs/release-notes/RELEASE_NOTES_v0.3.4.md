## 🎯 本次解决的两个问题

### 1. 国际版账号后面的 CodeBuddy IDE / CodeBuddy CLI 按钮，这次是**真的没了**

之前两个版本尝试过两种「遮掩」方案，都不成立：

| 版本 | 方案 | 结果 |
| :--- | :--- | :--- |
| v0.3.2 | `MutationObserver` 按 `aria-label` 加隐藏 class | 用户反馈「按钮依然存在」 |
| v0.3.3 | CSS 属性选择器 `[role="status"][aria-label="..."]` | 用户反馈「按钮依然存在」 |

原因是这两个按钮和徽章由 React 在异步数据到达后才渲染，遮掩的时机永远赌不赢。

**v0.3.4 改为在二进制层物理移除**：

- 那三个槽位（WorkBuddy / CodeBuddy IDE / CodeBuddy CLI）其实是同一个 `m.jsxs("footer", ...)` 容器的三个孩子。整个 `footer` 表达式被原地抹掉（等长填充），DOM 里从此不会生成这个节点。
- 其他页面复用的 `Kb`「当前账号」徽章组件，返回值被清空（保留 `function Kb({product:e,compact:t=!1}){...;return null}` 的声明结构，只清返回值，避免破坏闭合括号）。
- `web_proxy.py` 里所有与之相关的遮掩代码（`WB_UNSUPPORTED_LABELS`、`WB_UNSUPPORTED_STATUS`、`aria-label` 匹配、右上角 `statusIcons` 块、对应 CSS）**一并删除**，不再双份维护。

**验证结果**（在真实 bundle 上跑补丁 + 字节断言）：

```
✓ remove account-card WorkBuddy/IDE/CLI footer: 1656 bytes
✓ remove Kb current-account badge component: 420 bytes
✓ remove desktop runtime status icon group: 2038 bytes

size equal: True  (1059826 → 1059826)
footer container      orig=1  new=0
status icon group     orig=1  new=0
Kb function decl      orig=1  new=1   ← 结构保留
node --check: exit 0
```

### 2. 多账号调用：从「单账号自动轮换」升级为「并行 + 可手动」

原来的逻辑是 `get_active_account()` 取 `rotate/state.json` 里的 `activeAccountId`，**所有请求都打同一个账号**。现在的模型是**请求级选账号**：

```
请求 → 指定 header/body 账号?  ── 有 ──→ 用指定账号（不存在/被停用则 409）
                              └─ 无 ──→ mode=manual? ── 是 ──→ 固定 manualAccountId
                                                    └─ 否 ──→ 在已启用账号中 round-robin
```

关键设计取舍：**一条对话请求仍由单个账号完成**（不把同一个请求拆到两个账号，否则上下文与计费都会乱），但**多个同时到达的独立请求会分摊到不同账号**——这正是并发场景下想要的并行。

账号池配置（`/data/.wb-switch/account_pool_config.json`）：

```json
{
  "mode": "auto",
  "enabledAccountIds": [],
  "manualAccountId": null,
  "updatedAt": null
}
```

- `enabledAccountIds` 为**空数组 = 全部启用**（默认值）；一旦保存成明确列表，就变成白名单。
- 候选账号还会再过一层可用性过滤：token 缺失或 `expiresAt` 已过期的直接排除。

## 🖱️ WebUI 上的操作方式

每个账号卡片下方会注入一行控制条（不需要懂 API）：

- **`参与调用 · 点击停用`** / **`已停用 · 点击启用`** —— 切换该账号是否进入账号池。绿色描边表示启用。
- **`设为首选`** / **`首选账号 · 点击改回自动分配`** —— 蓝色描边表示当前固定使用本账号。停用首选账号时会自动回退到自动分配，不会留下悬空配置。

## 🔌 新增接口

| 接口 | 端口 | 说明 |
| :--- | :--- | :--- |
| `GET /account-pool/status` | 18091 | 账号池状态：模式、启用列表、首选账号、各账号 `enabled` / `usable` / `active` |
| `PUT /account-pool/config` | 18091 | 更新 `mode` / `enabledAccountIds` / `manualAccountId`（含未知账号 ID 与首选未启用的校验） |
| `GET /api/account-pool` | 18090 | Web 控制台转发到网关的 `status` |
| `PUT /api/account-pool` | 18090 | Web 控制台转发到网关的 `config` |

单次请求指定账号（不影响全局配置）：

```bash
curl -X POST http://<IP>:18091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-WorkBuddy-Account-Id: <account-id>" \
  -d '{"model":"deepseek-v3","messages":[{"role":"user","content":"hi"}]}'
```

也支持放在请求体里：`{"account_id": "<account-id>", ...}`（网关会先把它从 body 里摘掉再转发上游，不会污染上游请求）。

## 📦 升级方式

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> Unraid 请通过容器模板重建，不要手工拼接 `docker run`。
