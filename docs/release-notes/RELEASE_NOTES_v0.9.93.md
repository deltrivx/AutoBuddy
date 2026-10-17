> 修掉一个刚引入的真 bug：带 `?查询串` 打开时视图 hash 判定失效，每日任务页会退回官方页、导航高亮丢失。

## 一、问题

v0.9.92 发版后我复验时发现：用带查询串的 URL 打开每日任务页

```
/#/wb-daily?O=1792200220
```

页面**没有进入每日任务**，而是退回官方 `/` 显示成账号管理，导航高亮也丢了。

同一次实测里 `/` `/token-stats` `/settings` 都正常，只有两个 hash 视图页中招。

## 二、根因

`window.location.hash` 对 `/#/accounts?O=1792200207` 返回的是：

```
#/accounts?O=1792200207
```

而不是 `#/accounts`。代码里全是字面量全等比较：

```js
if (hash === "#/accounts") mode = "accounts";
```

查询串一进来，比较永远落空 → 落到 `official` 分支 → 恢复官方子节点 → 显示成账号管理。

**为什么之前没暴露**：v0.9.92 的验收用的是不带查询串的 URL。而 `?O=` 是页面自己加的缓存破坏参数（我自己的测试脚本在用，用户的浏览器/书签/外链同样可能带），与视图状态无关，却把视图判定整个打翻了。

## 三、修复

新增归一化函数，剥掉查询串与末尾斜杠：

```js
function wbViewHash() {
  var h = window.location.hash || "";
  var q = h.indexOf("?");
  if (q >= 0) h = h.slice(0, q);
  if (h.length > 1 && h.charAt(h.length - 1) === "/") h = h.slice(0, -1);
  return h;
}
```

`wbApplyMainView()`、`wbSyncNavActive()` 及两处 `WB_VIEW_HASHES` 比较全部改用它。

## 四、三项需求的最终交付状态

你之前提的三项，现在都实测通过了：

### ① 部署前先 abort 当前任务

新增 `scripts/deploy.sh`，部署前软中止正在执行的每日任务，等它真正结束（最多 30 秒）再重启：

```bash
./scripts/deploy.sh            # abort + 重启
./scripts/deploy.sh --sync     # 额外同步 gateway/*.py 并清 __pycache__
```

实测成功 abort 了一个执行中的任务（`3573be2067c6`）并完成重启。软中止走 `CancelledError` 分支正常收尾（落库 + 标记 aborted），巡检稍后自动从剩余账号续跑。

### ② 「统计自 v0.9.90 起」这类提示已取消

```
Default | 累计调用45.9K 次  今日调用337 次  今日词元181.27M
          累计词元181.27M  输入/输出180.95M / 314.4K
iMohican | 累计调用4.2K 次  今日调用与词元消耗暂无明细
Chkris | 累计调用8 次      今日调用与词元消耗暂无明细
```

代码里 `统计自 v0.9` 字样 0 命中，tooltip 也只说明口径不提版本号。

### ③ 数据总览 = 默认主页，账号管理加后缀

| 打开 | 结果 |
|---|---|
| `/` | 数据总览 ✅ |
| `/#/accounts?O=xxx` | 账号管理 ✅（修复前失败） |
| `/#/wb-daily?O=xxx` | 每日任务 ✅（修复前失败） |
| `/token-stats` | 词元统计 ✅ |
| `/settings` | 设置 ✅ |
| `/#/accounts` 刷新 | 仍是账号管理 ✅ |
| `/` 刷新 | 仍是数据总览 ✅ |

高亮、H1、视图显隐全部正确。

## 五、验证情况

- 语法：`web_proxy.py` / `main.py` `ast.parse` 通过；CSS 配平；`node --check` 通过
- 面板渲染测试：**62 项全部通过**
- 路由实测：CDP 覆盖 6 个 URL + 2 次刷新，全部正确
