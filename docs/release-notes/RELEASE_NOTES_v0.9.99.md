> 修复 v0.9.94–v0.9.98 连续五个版本 CI 构建失败的问题 —— 根因是两处源码契约测试没跟着代码一起改，导致 `test` job 失败、镜像从未推送。

## 一、问题

你让我检查 0.9.98 是否正常发布、GitHub Actions 是否有问题。查下来**确实有大问题**：

```
v0.9.98  push     completed failure
v0.9.98  release  completed failure
v0.9.97  push     completed failure
v0.9.97  release  completed failure
v0.9.96  push     completed failure
v0.9.96  release  completed failure
v0.9.95  push     completed failure
v0.9.95  release  completed failure
v0.9.94  push     completed failure
v0.9.94  release  completed failure
```

**从 v0.9.94 到 v0.9.98，连续五个版本的 CI 全部失败**，`test` job 失败导致 `build-and-push` job 被跳过 —— 这五个版本的 Docker 镜像**从未推送到 ghcr.io**。

（本地容器是我手工 scp + `docker restart` 部署的，所以功能一直是好的，但镜像仓库里没有。）

## 二、根因

CI 的 `test` job 跑的是：

```yaml
for f in _test_*.py; do
  python3 "$f"
done
```

本地复现，48 个测试文件里有 2 个失败，都是**源码契约测试没跟着代码一起改**：

### 1. `_test_assistant.py`（2 项失败）

v0.9.95 我把助手的第二轮从「单次请求 + 失败回落」改成了「双变体重试」（`tool_choice=none` / 完全不带 tools），但断言还在匹配旧写法：

| 断言 | 旧写法（已不存在） | 新写法（实际代码） |
|---|---|---|
| tool_echo 判为无效输出 | `if _is_tool_echo(final):` | `if cand and not _is_tool_echo(cand):` |
| 第二轮又发起工具调用 | `if not (msg2.get(\"tool_calls\") or []):` | `if msg2.get(\"tool_calls\"):` + `continue` |

### 2. `_test_orphan_runs.py`（1 项失败）

v0.9.92 我在 `lifespan` 里加了巡检循环 `asyncio.create_task(_patrol_loop())`，但断言仍匹配这段**连续文本**：

```
asyncio.create_task(_scheduler_loop())\n    _reconcile_orphan_runs()
```

中间插了一行巡检，连续文本匹配不上了。已改为三个独立子串判定。

## 三、修复

两个测试文件的断言同步到当前代码。本地全量复跑：

```
通过 48 个文件，失败 0 个
```

## 四、教训

这是我自己的流程漏洞：**改了实现却没同步源码契约测试**。这类测试的用途就是「锁定代码形态」，代码变了它们必须跟着变，否则 CI 会在你最不想看到的时候（发版时）红掉。

而且这次连错五个版本都没发现 —— 因为我一直用「本地 scp + docker restart」的方式部署验证，绕过了 CI，CI 红了也不知道。**发布检查不能只验证功能，必须确认 CI 绿灯**。
