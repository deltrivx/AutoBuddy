> 超时判定改为**按账号**（原来是 30 分钟总时间，账号一多必炸）。

## 用户反馈

> 超时判定有问题，你应该单独按照每个账号判定，而不是总时间，如果我以后账号变多，你岂不是又炸了

## 原实现的缺陷

```python
deadline = time.monotonic() + 1800   # 写死 30 分钟【总】上限
```

账号池涨到 22 个后，单轮耗时必然超过 30 分钟 → **每轮都被强杀、永远 `failed`**。
且账号越多炸得越狠：这是设计缺陷，不是参数问题——调大 1800 只是延后爆炸。

## 修复：按账号判定

```python
per_account_timeout = float(os.getenv("WB_DAILY_ACCOUNT_TIMEOUT", "180") or 180)
last_activity = time.monotonic()
while True:
    idle = time.monotonic() - last_activity
    if idle >= per_account_timeout:
        raise TimeoutError()
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=per_account_timeout - idle)
    last_activity = time.monotonic()   # 有产出 = 账号在推进，重置计时
```

语义：**单个账号**超过 `WB_DAILY_ACCOUNT_TIMEOUT`（默认 180 秒）没有产出新日志行，才算卡住 → 杀进程。

⇒ **总轮次时长 = 账号数 × 每账号耗时**，随账号数自然伸缩，不再有总上限。
⇒ 单个账号真的卡死（上游不返回 / 网络黑洞）仍会被切掉，不会拖死整轮。

顺带修正：`proc.wait()` 原先也挂在总 deadline 上，长轮次收尾时可能被误杀；现改为无限等待（子进程自然退出）。

## 测试

新增 `_test_wb_daily_timeout.py`（16 项）：

- **旧缺陷已除**：无总 deadline 常量、无 30 分钟文案、`proc.wait` 不再被掐；
- **按账号判定已实现**：可配置、默认 180s、idle 计时、有产出即重置、文案口径；
- **等价算法验证**：22 账号 × 80s（总 1760s）不杀、50 账号 × 80s（总 4000s）不杀、
  单账号 200s 无产出则杀、每 179s 有产出不算卡、阈值可配置（60s 阈值下 80s 间隔该杀）。

全量 33 个测试文件全绿。

## 升级说明

重建容器即可。新增环境变量 `WB_DAILY_ACCOUNT_TIMEOUT`（默认 180 秒，按需调整）。
`/data` 数据不受影响。
