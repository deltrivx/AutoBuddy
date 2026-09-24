# AutoBuddy v0.6.2

> 修复 v0.6.1 引入的 `/api/credits/stats` 500 错误（缺少 `time` 导入）。

## 问题

v0.6.1 新增的 `_reconcile_official_usage()` 会调用 `time.strftime()` 取当天日期，用于从 `daily` 数组重算「今日消耗」，但 `gateway/web_proxy.py` 此前从未需要过时间函数，导入清单里没有 `time`。

结果：`/api/credits/stats` 直接抛 `NameError: name 'time' is not defined`，返回 500，**积分统计页整个打不开**——比修复前的「今日消耗显示 0」更严重。

## 修复

补齐 `import time`。

## 教训

响应层模块（`web_proxy.py`）从未用过时间函数，新加带日期比较的逻辑时极易漏导入。CI 的 `py_compile` 语法检查**拦不住这类运行期 NameError**——只验证「能不能编译」，不验证「跑起来对不对」。

所以：改了响应层逻辑后，必须**实际请求一次接口**确认返回，不能只看编译通过。本版就是按这条补做的验收。
