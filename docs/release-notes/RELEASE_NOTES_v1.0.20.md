修自动留底清理范围的通配符 bug —— 人工命名的留证（32 条干净源、污染态留证、手工备份）原来会被当旧垃圾删掉。

## 问题

`_prune_accounts_backups()` 用 `glob(f"{primary.name}.bak-*")` 挑要清理的文件。那个通配符会匹配**所有** `accounts.json.bak-` 开头的文件，远不止它自己生成的时间戳形态。

隔离实测确认了匹配范围：构造 11 个对应本体真实形态的文件，glob 命中全部 11 个，其中 **9 个是人工命名的留证**：

| 会被误删的 | 是什么 |
| :--- | :--- |
| `accounts.json.bak-rmmrh-20261020-200536` | **唯一的 32 条干净源**（国内 27 + 国际 5） |
| `accounts.json.bak-before-recovery-*` | 污染态留证 |
| `accounts.json.bak-manual-*` | 手工固化的备份 |
| `bak-deljunk-*` / `bak-prerestore-*` / `bak-before-*` / `bak-dead-*` | 历次治理的退路 |

一旦自动留底的份数堆过 `_ACCOUNTS_BAK_KEEP`（20），这些**不可再生的人工留证就会按 mtime 被当"旧垃圾"删掉** —— 而它们往往是唯一一份能回到某个历史状态的凭据。

当时目录里正好 11 份、未超上限，所以侥幸没删。但这是悬在留证头上的刀：账号池最高频的写入是「刷新令牌」（一次刷新就落盘一次），自动留底每天能攒出几十份，超线是迟早的事。

## 修法

改为按形态过滤，只认自动留底自己那一种：

```python
stamp_rx = re.compile(
    r"^" + re.escape(primary.name) + r"\.bak-\d{8}-\d{6}(-\d+)?$"
)
baks = [
    p for p in primary.parent.iterdir()
    if p.is_file() and stamp_rx.match(p.name)
]
```

- `^...$` 全串锚定：`accounts.json.bak-20261021-060000.evil` 这种带尾巴的混不进来
- `\d{8}-\d{6}`：只认 `YYYYMMDD-HHMMSS`，与 `_backup_accounts()` 里 `time.strftime("%Y%m%d-%H%M%S")` 的产物严格对齐
- `(-\d+)?`：同秒多次留底的 `-1` / `-2` 后缀仍被认可，不会把自己刚生成的漏掉
- 带文件名锚定：`refresh_state.json.bak-*`、`model_policy.json.bak-*` 这些别的文件的备份一律不碰

自动留底仍受 20 份上限约束，该清的还是清 —— 修的是**范围**，不是把清理关掉。

## 顺带修掉一个测试盲区

测试文件 exec `main.py` 片段时的命名空间漏了 `re`。`_prune_accounts_backups()` 里 `re.compile` 抛的 NameError 被它自己的 `except Exception: pass` 吞掉，表现为「prune 一个都不删」且无任何报错。

这暴露了留底机制「失败一律吞掉」的设计盲区：**静默失败与成功在外部无法区分**。已在测试里补上 `re`，并加一条断言检查切片命名空间完整性，防止再漏。

## 验证

新增 `_test_accounts_bak_prune.py`，**22 项断言全部通过**，其中真跑函数（不是静态查字符串）：

| 场景 | 结果 |
| :--- | :--- |
| 9 个人工命名留证 + 25 份自动留底一起跑 prune | 人工留证 **9/9 幸存**，内容未被改动 |
| 最老的那份 32 条干净源 | 还在 |
| 污染态留证 | 还在 |
| 自动留底 | 压到 20 份，删掉的是 mtime 最小的 5 个 |
| 同秒带 `-1`/`-2` 后缀的自动留底 | 不被漏删 |
| `_backup_accounts()` 自己生成的 | 能被自己识别，不自杀 |
| 别的文件的 bak | 一个不碰 |
| 空目录 / primary 不存在 | 不抛异常 |

全仓回归：**60 个测试文件全绿**。

## 升级

```bash
docker pull ghcr.io/deltrivx/autobuddy:1.0.20
php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/rebuild_container AutoBuddy
curl -s http://127.0.0.1:18091/health   # version 应为 1.0.20
```

账号数据不受影响，本次只改清理逻辑。
