> 修复统计页「加载失败/卡死」——签到状态接口上游劣化到 5~9 秒拖垮整页；改为 stale-while-revalidate 让用户请求永不等上游。同时把误导性报错里的旧品牌名 `workbuddy-switch` 改为 AutoBuddy。

## 用户反馈

> 「统计加载失败 / 无法连接 workbuddy-switch 服务（http://192.168.31.2:18090），
> 请先运行 `workbuddy-switch` / 卡死或者重启报错，
> workbuddy-switch 是硬编码还是可以修改的，因为也已经改为 autobuddy 了」

## 实测根因

### 问题 1：加载失败/卡死

**官方后端 `/api/checkin/status` 本身就要 4.9~8.9 秒**（逐个账号实时查；账号从 17 涨到 22+ 后从早期的 1.46s 劣化至此）。

原缓存 TTL 只有 60 秒，挡不住。前端 `Promise.all` 等它 → 整页卡死 → fetch 被中断 → 走 catch → 抛「无法连接 workbuddy-switch 服务」。

**关键**：服务其实活着。并发实测（绕过缓存，从 Unraid 宿主）：

```
/api/checkin/status       200    7.72s     ← 慢
/api/status               200    0.00s     ← 未被拖累
/api/credits/stats        200    0.02s     ← 未被拖累
```

⇒ 后端可并发，是前端被单个慢接口阻塞。报错文案是**误导性**的。

### 问题 2：品牌名是否硬编码

**是硬编码，但可以改。** 该文案是官方前端 JS 里的模板字符串：

```js
} catch { throw new Error(`无法连接 workbuddy-switch 服务（${zb}），请先运行 \`workbuddy-switch\``) }
```

其中 `zb = window.location.origin` 是**动态**的（跟着页面地址走），不是硬编码 IP。

我们早前就有响应改写层（`clean_mac_content`）负责品牌名统一，只是**漏了这条报错文案**——此前只覆盖了 `workbuddy-switch.app` 和 `WorkBuddy Switch`（带空格），没覆盖裸的 `workbuddy-switch`（小写连字符）形态。

## 修复

### 1. stale-while-revalidate：用户请求永远不等上游

```
有数据（哪怕陈旧）→ 立即返回 + 后台单工刷新
无数据（首启）    → 同步等一次；失败返回空结构而非 5xx
容器启动          → 后台预热一次，让第一个用户就能命中缓存
```

- 新鲜度阈值 120s（超过才触发后台刷新）
- 后台刷新**单工**：上游要 5~9 秒，多请求同时触发会把它打爆；且签到状态分钟级陈旧完全可接受
- 响应头带 `x-autobuddy-cache: hit|stale|miss|error` + `x-autobuddy-cache-age`，便于排查

### 2. 品牌名替换（只改用户可见文案）

| 原文案 | 新文案 |
|---|---|
| 无法连接 workbuddy-switch 服务 | 无法连接 AutoBuddy 服务 |
| 请先运行 \`workbuddy-switch\` | 请检查 AutoBuddy 容器是否正常运行 |

**不动** `jye="workbuddy-switch"` 拼成的 GitHub 仓库地址（`github.com/changexbc/workbuddy-switch`）——那是真实上游，改了会 404。

项目地址本身早前就已可配（`AB_PROJECT_URL`，默认 `https://github.com/deltrivx/AutoBuddy`），与官方仓库名是两回事。

## 验证

替换规则对**线上真实 JS** 验证：

```
规则 1: 匹配 1 处 ✅   规则 2: 匹配 1 处 ✅
替换后 JS 语法有效 ✅   GitHub 仓库名未被动 ✅
报错文案预览：无法连接 AutoBuddy 服务（${zb}），请检查 AutoBuddy 容器是否正常运行
```

## 测试

新增 `_test_stats_page_fix.py`（**29 项**）：

- **stale-while-revalidate 契约**：`_checkin_status_serve` 返回二元组、后台刷新单工 + 守护线程、新鲜度/陈旧阈值、`_checkin_age`、容器启动预热；
- **请求路径永不等待**：走 serve 而非旧 cached、带 age 头、区分 hit/stale、无数据才同步取、失败返回空结构非 5xx、旧注释数据已更新；
- **品牌名替换**：两条新规则在位、新文案指向 AutoBuddy 容器、旧规则保留、注释说明 GitHub 仓库名必须保留；
- **对真实 JS 有效**：规则命中数、替换后 `node --check` 通过、仓库名未动、旧文案消失；
- **项目地址可配**：`AB_PROJECT_URL` 存在且默认指向 AutoBuddy。

全量 **45 个测试文件全绿**；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器即可。`/data` 数据不受影响。
