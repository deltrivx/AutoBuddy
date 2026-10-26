修复换号重试未过滤冷却账号导致的 400 渠道拦截死循环。

## 修复

### 换号重试不再反复撞同一个 400 渠道拦截

`gateway/main.py` 的 `_retry_candidates()`。

v0.9.35 起，上游 400 + `code=11128`「Illegal API invocation from an unapproved channel」
会被 `cooldown._classify()` 归类为 `channel` 并给该账号 30 分钟冷却。
但换号重试走的 `_retry_candidates()` **只排除本次已试过的账号，没有过滤冷却中的账号** ——
刚被上游拦下的号会被立刻选回来重试，再次撞同一个 400，重试链形同虚设。

实测（v2.1.0，Unraid 容器）：日志中 `[retry] 换号重试第 1/2/3/4 次` 反复落到
`尘星途` / `158****4203` / `190****1183` / `181****6005`，而这些账号正处于
`channel_blocked` 冷却期内（冷却状态文件可见 `"reason": "channel_blocked"`）。

现在 `_retry_candidates()` 补上与 `select_account()` 一致的
`cooldown.filter_cooling()` 过滤；兜底行为相同 —— 全部都在冷却时回退原集合，
宁可让上游返回真实错误，也不在网关层编一个「无账号」。

## 说明

### `/api/import-local` 的 400 不在本次修复范围

该路由由官方二进制 `autobuddy-engine` 内建
（`import_local:{method:"POST",path:"/api/import-local"}`），容器内缺少其依赖的
宿主机客户端文件，属官方后端行为；`patch/patch_binary.py` 只隐藏前端按钮，
不改变后端路由。前端按钮已隐藏，残留 400 不影响功能。

## 升级

```bash
docker pull ghcr.io/deltrivx/autobuddy:latest
docker compose up -d
```
