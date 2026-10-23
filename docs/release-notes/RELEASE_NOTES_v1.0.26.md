修整轮任务状态判定——`intl_accounts` 条件本应只决定执行路径，却被误用来写 `status`，导致账号池全为国内版时每一轮都被判 `failed`。

## 一、问题现象

面板长期显示「最近一轮任务失败」，但积分照常入账。用户多次反馈「任务明明失败了，怎么还有积分」。

## 二、根因

`gateway/wb_daily.py` 的 `_execute()` 中，这个 `if` 被赋予了两个职责：

```python
if intl_accounts and not job.single_task:      # 本应只决定「跑不跑国际版脚本」
    ...
    job.finish("done", {"rc": 0, ...})         # 却在这里写了 status
else:
    job.log_line(f"脚本退出码 {rc}，请查看日志定位失败账号")
    job.finish("failed", {"rc": rc, ...})      # 也在这里写了 status
```

账号池里 5 个账号全部是 `variant: "cn"`（国内版），`build_intl_account_store()`
只返回 `variant == "ai"` 的账号，因此 `intl_accounts` **恒为空列表**，条件
**恒为 False**，于是**每一轮必然走 else、必然 `failed`**。

`rc`（脚本退出码）完全不参与这个判断。第 1118 行另有一个正确的
`if rc == 0:` 分支，但它只做补登记 / 补领，**没有写 status**——作者本意是以
`rc` 决定状态，这个孤立分支是遗留线索。

## 三、实证

| 指标 | 值 |
| :--- | :--- |
| `wb_daily_runs` 总运行数 | 59 |
| `status = failed` | **58** |
| `status = aborted` | 1（唯一走 `rc < 0` 分支的，判定正确） |
| `status = done` | **0** |

反证样本（`rc=0`、零失败任务、却记 `failed`）：

| job_id | rc | 任务数 | 任务分布 |
| :--- | :--- | :--- | :--- |
| `7d77847fdcd0` | 0 | 80 | `{already: 10, info: 70}` |
| `b5f86aaa1089` | 0 | 80 | `{already: 10, info: 70}` |
| `7ff7e2735e52` | 0 | 14 | `{info: 14}` |

这些运行里**一条失败任务都没有**，`rc` 也是 `0`，仍被整轮标成 `failed`。
这同时排除了「`rc != 0` 才 failed」与「有失败任务就整轮 failed」两个假设。

`summary` 里如实记录的 `"rc": 0` 与 `status: "failed"` 自相矛盾，日志还会打印出
「脚本退出码 0，请查看日志定位失败账号」这种句子。

## 四、修复

执行路径与状态判定解耦：

```python
if intl_accounts and not job.single_task:      # 只决定执行路径
    ... 跑国际版脚本 ...

# 两条路径都会走到这里，status 只按 rc 判定
if rc == 0:
    job.finish("done", {"rc": 0, ...})
    ... 完成通知 ...
else:
    job.log_line(f"脚本退出码 {rc}，请查看日志定位失败账号")
    job.finish("failed", {"rc": rc, ...})
```

## 五、状态走向对照

| 账号池 | `rc` | 改动前 | 改动后 |
| :--- | :--- | :--- | :--- |
| 有国际版账号 | `rc = 0` | `done` | `done` |
| 有国际版账号 | `rc != 0` | `done`（错） | `failed` |
| **无国际版账号（当前）** | **`rc = 0`** | **`failed`（错）** | **`done`** |
| 无国际版账号（当前） | `rc != 0` | `failed`（碰巧对） | `failed` |
| 任意 | `rc < 0` | `aborted` | `aborted`（未改动） |

改动前有两个场景判错（且是双向的：该 done 的判 failed、该 failed 的判 done）。

## 六、影响

- 面板「任务失败」告警停止误报。
- 此前「任务失败但有积分」的现象随之消失——**积分统计本就是对的**，是整轮状态标记错了。
- 历史 58 条 `failed` 记录未回填（保留原始痕迹），仅新运行按新逻辑判定。

## 七、验证

- 既有契约测试全部保持通过：`_test_re_accept.py`（补登记仅在 `rc == 0` 后）、
  `_test_wait_claim.py`（补领仅在 `rc == 0` 后）、`_test_broadcast_analysis.py`
  （`job.status != "done"` 出口与 `_persist_run` 顺序）。
- `rc < 0` 的 `aborted` 分支未改动，历史行为保持。
