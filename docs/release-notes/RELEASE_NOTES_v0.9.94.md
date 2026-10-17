> 修掉数据总览改为默认主页后引入的真 bug：从数据总览点「词元统计」「系统设置」时 URL 和高亮都变了，页面却不动。

## 一、问题

复验路由时抓到的。用真实鼠标点击，从数据总览点「词元统计」：

```
点「词元统计」→ URL=/token-stats  高亮=['词元统计']  页面=数据总览  ← 错
```

URL 对了、高亮对了，但**页面仍显示数据总览**，官方内容被整个盖住。点「系统设置」同样。

同一次实测里 `/#/accounts`、`/#/wb-daily`、直接打开各 URL 都正常，只有「从数据总览点官方页」这一条路径中招。

## 二、根因

官方导航是 React Router 的 `history.pushState` 跳转，**既不触发 `hashchange` 也不触发 `popstate`**。

而视图应用的状态机是这样的：

| 触发源 | 会调用 `wbApplyMainView()` 吗 |
|---|---|
| `hashchange` 事件 | ✅ |
| 视图链接点击（概览/每日任务/账号管理） | ✅（`wbPushUrl()` 里显式调） |
| **官方导航点击** | ❌ |

官方导航的点击处理器里只调了 `wbSyncNavActive()` —— 那函数只管高亮，不管视图显隐。

**为什么之前没暴露**：数据总览占用默认主页之前，`/` 是账号管理（官方页）。那时从任何官方页点到另一个官方页，路径变化不涉及注入视图的显隐，所以只同步高亮就够。现在默认主页本身就是注入视图（数据总览），路径一变就必须重新应用视图状态 —— 这条路径此前从未被走过。

## 三、修复

两处，都是小改动：

**① 导航点击处理器改调 `wbUpdateOverviewView()`**（内部是 `wbApplyMainView()` + `wbSyncNavActive()`，视图状态和高亮一起同步）：

```js
// 改之前
requestAnimationFrame(function () { wbSyncNavActive(); });
setTimeout(wbSyncNavActive, 120);
setTimeout(wbSyncNavActive, 400);

// 改之后
requestAnimationFrame(function () { wbUpdateOverviewView(); });
setTimeout(wbUpdateOverviewView, 120);
setTimeout(wbUpdateOverviewView, 400);
```

**② 补 `popstate` 监听**，覆盖浏览器前进/后退（`pushState` 本身不触发它，但用户点后退按钮会）：

```js
window.addEventListener("hashchange", wbUpdateOverviewView);
window.addEventListener("popstate", wbUpdateOverviewView);
```

## 四、修复后实测

CDP 真实鼠标点击，六个选项卡来回切：

```
打开 /              → 数据总览  ✅
点「账号管理」        → /#/accounts   账号管理  ✅
点「数据总览」        → /             数据总览  ✅
点「词元统计」        → /token-stats  词元统计  ✅   ← 修复前这里失败
点「数据总览」        → /             数据总览  ✅
点「每日任务」        → /#/wb-daily   每日任务  ✅
点「数据总览」        → /             数据总览  ✅
点「系统设置」        → /settings     设置     ✅   ← 修复前这里失败
点「数据总览」        → /             数据总览  ✅
点「账号管理」        → /#/accounts   账号管理  ✅
```

带查询串直接打开也正常：

```
/#/accounts?O=1  → 账号管理  ✅
/#/wb-daily?O=1  → 每日任务  ✅
/?O=1            → 数据总览  ✅
```

## 五、顺带确认的另两项

**① 部署脚本**：`scripts/deploy.sh` 已就位（101 行），本次收尾部署就是用它跑的 —— `--sync` 同步 12 个 gateway 文件、清 `__pycache__`、abort 步骤正常走完、重启就绪。

**② 版本号提示**：四个密钥行的统计文案实测如下，无版本号字样：

```
Default  | 累计调用46.0K 次  今日调用449 次  今日词元224.09M
           累计词元224.09M  输入/输出223.73M / 361.6K
iMohican | 累计调用4.2K 次  今日调用与词元消耗暂无明细
Chkris   | 累计调用8 次    今日调用与词元消耗暂无明细
.        | 累计调用0 次    今日调用与词元消耗暂无明细
```

代码里 `统计自` 字样 0 命中。

## 六、验证情况

- 语法：`web_proxy.py` / `main.py` `ast.parse` 通过；CSS 括号与注释配平；`node --check` 通过
- 面板渲染测试：**62 项全部通过**
- 路由实测：CDP 真实鼠标点击 10 步 + 3 个带查询串 URL，全部正确
