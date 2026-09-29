# AutoBuddy v0.9.23

> 修复新加的账号永远不被调用，以及巡检探测账号池外的账号。

## 修复一：新账号一次都没被调用

### 现象

新加进账号池的账号，token 有效、确实在 `enabledAccountIds` 里，
但**一次都没被调用过**。

`selection_logs.json` 实测（09-29 23:43 ~ 09-30 00:36，200 条选号记录）：

```
palmesewooters371@gmail.com   28 次
尘星途                        28 次
Mr.Chen                       27 次
13035639603                   26 次
18981122864                   26 次
18011628363                   25 次
13558746529                   24 次
13666135560                   16 次
----------------------------------------
15573657770                    0 次   ← 新账号
13111877992                    0 次
15228718439                    0 次
15198254808                    0 次
```

### 根因：收窄逻辑把「未知」当成了「不支持」

选号时有一层「正向能力矩阵」收窄：

```python
known_yes = [a for a in candidates
             if capability_state(_account_id(a), model) == "yes"]
if known_yes:
    candidates = known_yes      # ← 只留"明确支持"的
```

`capability_state()` 对**从未探测过**的账号返回 `None`，
于是新账号和"明确不支持"一样被排除。

配合巡检的冷启动缺口，形成死循环：

```
新账号 0 调用
   ↓
巡检 onlyUsedModels=true → used_models 为空
   ↓
只探 BASE_MODELS 基模型清单
   ↓
清单里没有自动发现的新模型（如 hy4-preview-f）
   ↓
能力矩阵永远缺这个键 → state 永远是 None
   ↓
收窄时被排除
   ↓
又是 0 调用  ← 回到起点
```

对比数据印证了机制：

| 账号 | `hy4-preview-f` 键 | 结果 |
| :--- | :--- | :--- |
| 6 个老账号 | `yes` | 进候选，24~28 次 |
| 4 个新账号 | **键不存在** | 出局，0 次 |

**为什么 `13666135560` 没键也能被调用？**

它 261 次调用全是 `deepseek-v4.1-flash`（不是 `hy4-preview-f`）。
收窄是**按当前请求的模型**查键的 —— 这恰好反证：
不是账号被禁，而是「该账号 × 该模型」这个组合没被探过。

### 修复

只排除**明确不支持**，未知保留参与：

```python
not_no = [a for a in candidates
          if capability_state(_account_id(a), model) != "no"]
if not_no:
    candidates = not_no
```

两处收窄点均已修正：主选号 `_pick_account`、换号重试 `_retry_candidates`。

真不支持的账号仍会被 `_learn_model_unavailable` 即时学习成 `no`，
下一轮自然出局 —— **不牺牲原有选号质量**。

保留 `if not_no:` 守卫：全部明确 `no` 时不覆盖候选集，
让上游返回真实错误，而不在网关层编一个「无账号」。

## 修复二：巡检探测账号池外的账号

### 现象

账号已经移出账号池，巡检**还在探它、还在报错**。

实测：一轮巡检探了 **15 个账号**，远多于池内数量；
`model_health_last.json` 里出现了已移出池的账号。

### 根因：两套账号集合

| 路径 | 账号来源 |
| :--- | :--- |
| 日常调用 `/v1/chat/completions` | `_enabled_accounts()`（白名单 + 停用策略） |
| **模型巡检** | `_load_accounts()`（两份账号文件**全量合并**） |

巡检那条完全没过池白名单。

### 修复

```python
raw = model_health.run_round(
    accounts=_enabled_accounts(_load_accounts(), _load_pool_config()),
    ...
)
```

与日常调用同源。这符合项目既有设计原则 ——
`build_account_store()` 的注释写着：

> 账号来源与账号池其它功能保持同一个出处，
> 避免「账号页看得到、每日任务里没有」

界面手动「只探这几个账号」（`overrides.accountIds`）时，
仍由 `model_health.select_targets` 按该清单收窄，不受影响。

## 测试

- **新增 `_test_pool_selection.py`（11 项）**：锁定「不得排除未知账号」契约，
  防止日后又改回 `state == "yes"`；同时校验巡检的池过滤。
- **修正 `_test_model_health.py`**：该测试把 `run_model_health_check`
  切片到隔离命名空间执行，巡检新增的两个依赖需补桩
  （`_load_pool_config` / `_enabled_accounts`）。
  **生产代码没有问题**，是测试桩缺失。

```
15 项单测                      ✅ 全绿
四模块 AST                     ✅ 通过
注入 JS node --check           ✅ 通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

- 无新增或删除环境变量；
- 无数据库结构变更；
- 行为变化：
  - 新账号加入池后可立即参与轮转（原先需先被巡检探到该模型）；
  - 巡检只探池内账号，探测次数下降，不再报池外账号的错。

## 遗留事项（非本次范围）

- 国际版账号额度耗尽时会被正常选中但调用失败，建议移出账号池
  （本次未改：这是账号状态问题，不是选号逻辑问题）。
- 上游 429 的响应体未落盘，无法区分 `6004`（模型级限流）与
  `14018`（额度耗尽）。如需精确诊断，可另加日志。
