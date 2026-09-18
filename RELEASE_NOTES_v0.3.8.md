# v0.3.8 - 全局巡检收尾：把「半成品面板」和「说不清的状态」一次清掉

本版不是加功能，而是把项目从「能用但有半成品」推到「完整可交付」的一次系统性排查。

---

## 1️⃣ 账号卡片：清掉冗余提示

删除卡片上的 `自动分配中，并发请求会分摊到 N 个已启用账号` 文案与 `清零统计` 按钮。
保留 `已调用 N 次` 计数徽章与 `参与调用` / `设为首选` 控件。

> 观测入口没丢，仍在 `GET /account-pool/selections`，只是不再往每张卡片上塞按钮。

---

## 2️⃣ 「当前 CLI 账号」为什么只有一个？

**原因**：设置页显示的「当前 CLI 账号」来自官方底层 `/api/codebuddy-cli/status`，
官方只维护**一个** `activeAccountId` —— 它本质是「CodeBuddy CLI 助手绑定哪个账号」，是单账号概念，
所以永远只有一张卡对应得上。

**处理**：

- 设置页「CodeBuddy CLI 自动轮换」区块顶部注入范围说明：

  > 说明：此处「当前 CLI 账号」由官方底层服务（:57890）维护，全局只有一个，仅决定 CodeBuddy CLI 助手绑定哪个账号；网关的 API 调用不受它影响，会按账号池在全部「参与调用」的账号之间分摊。

- 账号卡片给被绑定的那张卡打 `官方 CLI 绑定` 虚线标记，不再看起来「只有它有状态」。

---

## 3️⃣ 其他账号看不到模型调用提示 —— 根因修复

**根因**：`/api/account-models` 只读上游 `official_usage_cache.json`，
而上游**只对「当前账号」返回模型明细**，其余账号的 `daily[].models` 恒为空。
实测 5 个账号里只有 1～2 个有记录，看起来像「账号池没生效」。

**修复**：网关每次 API 调用都把**实际服务该请求的账号**写进 `token_stats_logs.json`
（新增 `accountId` / `accountName` / `variant`），`/api/account-models` 改为
**以网关归因为主数据源、上游缓存为补充**。

实测（5 个账号）修复后：

```
samailamuhammadk774@gmail.com | used: 1 | gatewayCalls: 4 | ['hy3']
angelwized@gmail.com          | used: 23| gatewayCalls: 4 | ['claude-opus-4.6', ...]
palmesewooters371@gmail.com   | used: 5 | gatewayCalls: 4 | ['deepseek-v4.1-flash', ...]
w769600627@gmail.com          | used: 1 | gatewayCalls: 4 | ['hy3']
一杯美式                       | used: 1 | gatewayCalls: 4 | ['hy3']
```

卡片标题也全部从「暂无调用记录」变为「已调用 N」。

---

## 4️⃣ Token 统计页两个面板恒为空 —— 根因修复

**根因**：后端 `make_source_obj()` 里这三个字段**写死为空数组**：

```python
"dailyByModel": {},   # 趋势图模型筛选 → 永远没有模型可选
"projects": [],       # 用量分布（默认 Tab）→ 永远「暂无统计数据」
"sessions": [],       # 消耗最高的会话 → 永远「暂无统计数据」
```

前端 `用量分布` 默认读 `source.projects`、`消耗最高的调用` 读 `source.sessions`。
另外 `models[]` 缺少前端构建下拉所需的 `key` 字段，切到「按模型」也会渲染成空白。

**修复**：

| 字段 | 修复后语义 |
| :--- | :--- |
| `projects` | 按**账号**聚合（网关没有「项目」概念） |
| `sessions` | 按**单次调用**的 Token 降序（每条网关请求就是一次独立调用） |
| `dailyByModel` | 按「模型 × 日期」填充，趋势图的模型筛选可用 |
| `models[].key` | 补齐，供前端构建下拉 |

**文案同步**（代理层等长字节替换，不动二进制）：

| 原文 | 新文案 |
| :--- | :--- |
| 按项目 | 按账号 |
| 按项目汇总本地 Token 用量。 | 按账号汇总本地 Token 用量。 |
| 消耗最高的会话 | 消耗最高的调用 |
| 按本地聚合 Token 从高到低排列。 | 按单次调用 Token 从高到低排列。 |

**顺带修掉一个归因缺口**：云端流水合并时把 `accountId` / `accountName` 丢掉了，
导致所有云端记录落进「未归属账号」并占据 100%。现在带上了；无法回溯归因的历史记录
改标为 `未归因（升级前记录）`。

修复后实测：

```
用量分布 · 按账号
  01  未归因（升级前记录）          3.6M   52.4%
  02  palmesewooters371@gmail.com  3.2M   45.6%
  03  angelwized@gmail.com        137.6K   2.0%
  04  一杯美式                         44   0.0%
  05  w769600627@gmail.com             42   0.0%
  06  samailamuhammadk774@gmail.com    40   0.0%

消耗最高的调用
  01  gpt-5.6-luna  palmesewooters371@gmail.com  1.7M  24.8%
  02  glm-5.2       palmesewooters371@gmail.com  540.1K  7.8%
  ...
```

> **口径提醒**：网关自身记录的是实测估算 Token（按文本长度 / 3.5 估算）；
> 从官方云端流水合并进来的记录只有「积分」，其 Token 是按固定比例**折算的估算值**。
> 判断绝对量级时请注意这一差异。

---

## 5️⃣ 主页文案按容器真实能力重写

| | 文案 |
| :--- | :--- |
| 原 | 统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态。 |
| 新 | 统一管理 WorkBuddy、账号池与 OpenAI 兼容网关服务、积分和签到状态。 |

容器里 IDE / CLI 能力已物理移除，原措辞会让用户去找不存在的入口。

---

## 6️⃣ 链路讲清楚 + 缓存修掉

### 三层账号调度链路（互补，不冲突）

| 层 | 机制 | 作用范围 | 默认间隔 |
| :--- | :--- | :--- | :--- |
| ① **账号池** `select_account()` | 每次 API 请求独立选账号，round-robin | **决定 API 实际用哪个账号** | 每请求 |
| ② **网关健康巡检** `rotate_once()` | 当前账号失效时才切换，维护 `rotate/state.json` | 仅供健康状态展示与兼容旧接口 | 30 分钟 |
| ③ **官方 CLI 自动轮换**（设置页） | 切换 CodeBuddy CLI 助手的绑定账号 | **只影响 CLI 助手**，与 API 无关 | 5 分钟 |

①是权威；②③维护的是官方底层的单账号标记，容器里没有真实 CLI 会话，因此不参与 API 调度。

### 响应禁缓存

`assets/index-*.js` 文件名带 hash，**不会随内容改动变化**。此前浏览器可能一直使用旧副本，
表现为「改了却没生效」——这也很可能是历史上「按钮明明移除了却还在」的帮凶之一。
现在代理对 HTML / JS 响应强制 `cache-control: no-store, must-revalidate`。

---

## 📦 升级方式

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> Unraid 请通过容器模板重建，不要手工拼接 `docker run`。
