# AutoBuddy v0.9.26

> 修复工具调用透传丢失 tool_calls、日志重复计数改为行尾 ×N、词元统计只剩两天。

## 1. 工具调用透传损坏（严重）

### 现象

用户 2026-10-02 反馈：「所有模型都返回 finish_reason=tool_calls 却不携带 tool_calls 数组」。

实测确认：

```
finish_reason : tool_calls
message       : {"role": "assistant", "content": ""}
tool_calls    : null          ← 没有
```

**危险之处在于它不报错**：模型「说要调工具」，但调用内容一个字都没传下去，
Agent 侧的 function calling 链路整条断掉，而空 content 看起来像一条正常回复，
排查时极易误判成「模型不支持工具」。

### 根因：非流式路径只挑了 content

网关把上游流式帧重组成 assistant message 时：

```python
delta = choices[0].get("delta", {})
if "content" in delta and delta["content"]:
    collected_content += delta["content"]      # ← 只挑了 content
```

而 tool calling **恰恰不走 content**，它走 `delta["tool_calls"]`。
于是那一半消息被静默丢弃，最终拼出的 message 只剩 role/content。

### 修复

新增 `accumulate_delta()`，把整块 delta 增量合并进 assistant 消息：

| 字段 | 处理 |
| :--- | :--- |
| `content` / `reasoning_content` | 字符串累加 |
| `tool_calls` | 按 `index` 定位同一条，再**拼接** arguments |
| `role` | 只在首帧携带，取到就写、取不到不动 |

⚠️ **arguments 必须拼接而非覆盖** —— 上游是把 JSON 参数**逐片**下发的：

```
{"tz"  →  :"Asia/  →  Shanghai"}
```

每片都 append 一条新 tool_call 会变成一串残缺调用，必须按 index 归位。

组装时不再只塞 `content`，而是把累积结果整体放进 `message`（真·透传）。

## 2. 日志重复计数改为行尾 ×N

原先数数是**另起一行**，等于又多打了一条几乎重复的记录，与「防刷屏」初衷相悖：

```
改前:  [pool]  msg
       [pool]  (上一条重复 4 次)      ← 多一行
改后:  [pool]  msg ×4                ← 挂在行尾
```

三处一并改：`lprint` 通用日志、access log 周期汇报（main + web_proxy）。

## 3. 词元统计只剩最近两天

### 根因

明细日志是**滑动窗口**，上限 3000 条，而实测网关每天产生约 2000 条调用：

```
按天分布: 10-01: 2025 条, 10-02: 975 条   ← 不到两天写满
最密时段: 00 时 418 条 / 21 时 358 条
```

窗口写满后最老记录被**直接丢掉**，页面永远只剩一两天。

### 为什么不能简单调大窗口

明细**每请求都要整文件读+写一次**，3000 条已是 1.28MB 一轮 I/O，
放大到几万条会把网关拖垮。

### 修复：明细保近期、历史转聚合

滑出窗口的记录按「天 x 模型 x 账号 x variant」折算成极小一行存
`token_stats_rollup.json`；趋势图与汇总把明细与 rollup 一起算，
请求明细仍只列真实调用记录。

每请求的成本从此与「累积了多久」无关。

### 伴随修正的四处计数缺陷（均由新测试暴露）

- 各维度桶的 `records` 一律 `+1` → 改为按 `_rec_count()` 累加
  （聚合行代表 N 次调用，算 1 会把历史量算少）；
- `requests` / `sessions` 误把 rollup 聚合行混进去，变成一堆没有 id 的
  `req-0` 假记录 → 改为只遍历真实明细；
- 分流子集（国内/国际版）同源问题一并修。

## 关于「积分统计页最近更新停在 9-24」

**这一项网关侧无法修复**，如实说明：

`official_usage_cache.json` 的 `collectedAt` 停在 `09-23 04:44`、
`rangeEnd` 停在 `2026-09-24`。它是**官方闭源底层**
（`autobuddy-engine`）的采集产物 —— 引擎对该环节只字不提（静默跳过），
直连 `:57890/api/credits/stats` 也不触发重写。

Web 代理层已做的日程校正（`_reconcile_official_usage`）不受影响，
顶层 `daily` 仍是新鲜的（最新 2026-10-02，今日消耗 586.64）。

## 测试

新增 2 个测试文件共 37 项：

```
_test_tool_call_passthrough.py   16 项   逐片拼接、按 index 归位、真透传
_test_token_stats_history.py     21 项   历史折算、records 计数、文件损坏容错
```

`_test_log_dedup.py` 增补第 6 组断言：强制「行尾 ×N」写法，
禁止再出现独立成行的重复计数。

```
全量 17 个测试文件               ✅ 全绿
五模块 AST                       ✅ 通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

- 新增可选环境变量 `AB_TOKEN_DETAIL_MAX`（默认 3000，明细窗口上限）；
- 新增数据文件 `/data/.autobuddy/token_stats_rollup.json`（历史聚合，
  首次写入时自动生成，无需手工初始化）；
- 无数据库结构变更；
- 行为变化：非流式 chat 响应在模型要求调工具时会带上 `tool_calls`，
  Agent 的 function calling 链路恢复。
