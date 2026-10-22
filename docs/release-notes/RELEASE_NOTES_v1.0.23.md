修积分统计页「官方用量暂不可用」—— 重建 officialUsage 时漏改 status，前端准入校验把整份实时数据丢弃。

## 一、问题表现

积分统计页面顶部出现黄色警告条：

> **官方用量暂不可用**
> 今日、近 7 天和本月消耗将使用本地观察口径；官方接口恢复后刷新即可重新同步。

用户同时提出怀疑：是否之前把数据目录改成 `.autobuddy` 导致，需要改回 `.workbuddy-switch`。

## 二、根因

**不是目录名问题** —— 已实证排除：引擎二进制与官方 JS bundle 两处均只引用 `.wb-switch`，全文无一处 `.workbuddy-switch`。

**真因是一个漏改的字段**：

1. 官方闭源引擎的用量采集早已停更，`official_usage_cache.json` 里 `status` 是引擎留下的 `"unavailable"`；
2. 网关的 `_reconcile_official_usage()` 早就接管了这份数据 —— 用实时快照重建 `accounts` / `daily` / `summary` / `collectedAt` / `errors`，并把 `source` 标成 `gateway-realtime`；
3. **但重建收尾处漏改 `status`** —— 它仍是 `"unavailable"`；
4. 官方前端用 `status` 做整份数据的准入校验（bundle 内实证）：

   ```js
   function py(e){return e?.status==="complete"||e?.status==="partial"}
   ```

5. `status` 是 `unavailable` → `py()` 返回 false → 前端把这份**已经由网关实时重建好的 officialUsage 整份丢弃**，页面回退到本地观察口径并弹出警告。

也就是说：数据明明是实时的（10 个账号、31 天按日明细、`collectedAt` 为当前时刻、`ageDays=0`），只因为一个字段没同步，就被前端判为不可用。

### 修前线上实证

```
status        = unavailable      ← 漏改
source        = gateway-realtime ← 已标实时
collectedAt   = 1792711920870    ← 当前时刻
accounts 数   = 10
daily 天数    = 31
errors        = []
官方 JS py() 准入 = ❌ 被丢弃
```

`collectedAt` 是新的、`source` 是实时的，唯独 `status` 是旧的 —— 一处漏改，整份作废。

## 三、修复

在重建收尾处（原 `ou["collectedAt"]` / `ou["errors"] = []` 之后）同步：

```python
ou["status"] = "partial"
ou["source"] = "gateway-realtime"
```

**为什么取 `partial` 而不是 `complete`**：账号明细与按日明细是实时重建的，但「官方请求级原始用量」（`requests`）仍来自已停更的采集。`partial` 如实表达「部分实时」，与 `errors=[]` 不矛盾；若填 `complete` 会让前端把停更的请求级数据也当成权威展示。

## 四、修后实测（Unraid 线上，2026-10-23 07:31）

| 字段 | 修前 | 修后 |
| :--- | :--- | :--- |
| `status` | `unavailable` | **`partial`** |
| `source` | `gateway-realtime` | `gateway-realtime` |
| `accounts` 数 | 10（被丢弃） | 10（被采用） |
| `daily` 天数 | 31（被丢弃） | 31（被采用） |
| `ageDays` | — | **0.0** |
| 官方 JS `py()` 准入 | ❌ 被丢弃 | **✅ 通过** |

容器已重启并确认 `Up (healthy)`、`version=1.0.22`、39 个模型。

## 五、顺带核实：Mac-mini DSH 400 的现状

同日用户反馈「400 依然存在」。臣做了针对性实验：

- **A/B 对照**（5 个变体 × 4 次，共 20 次，从 Mac-mini 用 DSH 真密钥发出）：baseline、+`conversationId`、+`X-User-Id`（假 uid）、+`X-User-Id`（真 uid）、+全套桌面端头族 —— **20 次全部未复现 11128**。
- **大请求实验**：42 万 token 的请求成功返回 200（44.8 秒），证明与请求体量无关。
- **画像键校验**：账号顶层确有 `uid` 字段，取到的正是 fingerprint 库中的真实 uid，未产生幽灵条目，桌面端画像逻辑无误。
- **近期线上**：最近 40 分钟 chat/completions **零 400、零上游错误**，仅 2 个账号处于正常限流冷却。

结论：11128 属**上游间歇性账号级拦截**，与请求特征无关；v1.0.22 的换号重试与粘性冷却避让已能在其上兜底（实测换号重试跑到第 9 次）。

## 六、升级说明

常规升级，无破坏性变更、无配置迁移、无数据格式变化。账号数据与统计流水原样保留。Unraid 用户更新镜像并重建容器即可，模板无需改动。
