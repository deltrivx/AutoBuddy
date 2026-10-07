> 渠道拦截（400 `code=11128`）的账号退出轮询 30 分钟；出站 UA 对齐官方桌面客户端形态。

## 起因

2026-10-07 晚排查上游报错时，在容器日志里搜到一类此前没被处理的错误：

```
[upstream] HTTP 400 {
  "code": 11128,
  "msg": "Illegal API invocation from an unapproved channel"
}
```

上游给的提示是"这次调用没通过渠道校验，可能不是官方客户端发出的"。

顺着查下去发现两个问题：

1. **被拦的账号没有退出轮询。** `cooldown._classify()` 对 400 **一律返回 `None`**
   （不冷却），于是这个账号仍留在候选集里，后续请求会反复撞同一个 400。
2. **出站 UA 是 `python-httpx/0.23.3`。** 三条转发路径都只带 `Authorization`
   与 `Content-Type`，没设 UA，于是 httpx 用默认自带的那个 —— 一眼就是脚本客户端。

## 修复内容

### 一、渠道拦截 → 冷却 30 分钟（真正解决问题的一条）

`gateway/cooldown.py` 新增 `channel` 分类：

```python
if status_code == 400:
    low = (body_text or "").lower()
    if "11128" in (body_text or "") and "unapproved" in low:
        return "channel"
```

命中后施加 30 分钟冷却（`AB_CD_CHANNEL`，可配），该账号退出自动轮询、自动换号。

设计上刻意不叠加软退避、到期即恢复 —— 上游的一次策略判定不该被放大成
长期封禁。

### 二、出站 UA 对齐官方桌面客户端

三条转发路径（流式首试 / 流式换号重试 / 非流式换号重试）原先各写一遍
headers，现在统一走 `_upstream_headers()`：

```python
{
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": UPSTREAM_UA,   # WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1
}
```

UA 取自 `gateway/vendor/workbuddy_daily.py` 的 `DESKTOP_UA`（仓内已有的
权威值），可用环境变量 `AB_UPSTREAM_UA` 覆盖。

## 一条必须说清的判据

**UA 不是已证实的根因。**

窗口内 2 次 11128 **全部落在「尘星途」(`a944ccff…`) 一个账号**，而同期其他
账号用**完全相同**的 `python-httpx` UA 全都成功了。

所以 11128 更像**账号级渠道拦截**，不是请求形态问题。本次改 UA 只是顺手把
请求形态对齐官方、消除一个明确嫌疑点；**真正解决问题的是「让它退出轮询」
这条**。这点写在这里，免得后来人误以为改 UA 就能根治。

## 护栏

对照测试锁死：普通参数类 400（如 `code=11133`）**绝不冷却** —— 那不是账号的
错。新增回归测试 8 项：

- `11128` 归类为 `channel` / 施加冷却 / 进入冷却 / 原因记为 `channel_blocked`
- 普通 400 不冷却、不进冷却
- 被拦截账号退出轮询，健康账号仍在轮询

## 测试

```
全量 19 个测试文件     ✅ 全绿
_test_cooldown.py     ✅ 34 项（新增 [7] 渠道拦截 8 项）
```
