> 修掉通知里两处**标题语义重复**、巡检「本轮检测：? 个组合」的问号 bug、日报「昨日消耗合计」的**假数据**；并按 `www.baidu.com` 移动端做法给全站做**全屏适配**。

## 一、通知文案修复（三处）

### 1. 标题语义重复（两处）

推送出来长这样，同一个意思出现两次：

```
🎯 每日任务完成
🎯 每日任务执行完成          ← 删掉这行
```

```
🔍 巡检结果
🔍 模型可用性巡检完成        ← 删掉这行
```

两处都删掉正文那行重复标题，正文直接从数据开始。

### 2. 巡检通知「本轮检测：? 个组合」永远是问号

根因是**字段名对不上**：

```python
# gateway/notify.py 读的是 checked
checked = (result or {}).get("checked")

# gateway/model_health.py 的 summarize() 实际返回的是 combos
return {"combos": round_result.get("combos", 0), ...}
```

所以 `checked` 永远取不到 → 永远 None → 永远显示问号。

改为读 `combos` 并兼容旧字段名，顺带补上「参与账号」一行。修复后：

```
🔍 模型可用性巡检完成
  · 本轮检测：748 个组合
  · 参与账号：22 个
  · 新禁用：1 · 恢复启用：0
```

### 3. 日报「昨日消耗合计」是假数据

原实现把 `checkin + task`（**获得**的积分）当成「消耗」报出去：

```
昨日签到积分：0
昨日任务积分：3,000
昨日消耗合计：3,000     ← 这就是上面那个 3000，是挣的，不是花的
```

数字恰好等于签到+任务，但语义完全相反。用户反馈「消耗数据应该是假的」——确实是。

现改为从**官方底层**取真实按日消耗（与页面「官方积分消耗」图表同源）：

```python
GET http://127.0.0.1:57890/api/credits/stats
  → accounts[].daily[] = [{date, usage}, ...]
昨日消耗 = 所有账号中 date == 昨日 的 usage 之和
```

实测 2026-10-15 真实消耗 **8,590.68**（原报 3,000）。

另外：取不到数据时显示 `—` 而不是 `0`，避免把「没数据」误读成「没消耗」。

---

## 二、移动端全屏适配（参考 www.baidu.com）

### 百度移动端的三条核心做法

| 做法 | 百度 | AutoBuddy 原状 |
| :--- | :--- | :--- |
| 禁双指缩放 | `minimum-scale=maximum-scale=1.0` + `user-scalable=no` | 只有 `initial-scale=1.0`，允许缩放 |
| 延伸到刘海区 | `viewport-fit=cover` | 无，刘海区域被当安全区外 |
| 去点击蓝框 | `-webkit-tap-highlight-color` | 未处理，每次点一下出一块蓝 |

### 本次改动

**viewport 服务端改写**（不是 JS 注入）：

```
width=device-width, initial-scale=1.0, minimum-scale=1.0,
maximum-scale=1.0, user-scalable=no, viewport-fit=cover
```

为什么在响应阶段改而不是 JS：viewport 必须**首屏渲染前**就位，事后用 JS 改会触发一次可见的重排/缩放跳动。

**safe-area 让开刘海与 Home 条**：

```css
aside  { padding-top: max(6px,  env(safe-area-inset-top, 0px)) }
main   { padding-bottom: max(16px, env(safe-area-inset-bottom, 0px)) }
```

**原生 App 手感**：

- `-webkit-tap-highlight-color: transparent` — 去掉点击蓝框
- `-webkit-touch-callout: none` — 禁掉长按弹出的「保存图片/拷贝」菜单
- `user-select: none` — 防止滑动时误选中文字
- ⚠️ 输入框/文本域显式放回 `user-select: auto`，否则设置页填不了 API Key

**滚动优化**：

- `100dvh` 动态视口 —— 地址栏显隐不再把内容顶出可见区（原来的 `h-screen` 用的是 layout viewport）
- `-webkit-overflow-scrolling: touch` — iOS 惯性滚动
- `overscroll-behavior: contain` — 掐断滚动链，只让 main 滚
- `touch-action: pan-y` — 明确只响应纵向手势

### 验证（CDP 多尺寸实测）

| 尺寸 | 容器高度 | 滚动 | 顶栏吸附 | 横向溢出 | 输入框可选 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| iPhone 竖屏 390×844 | 844px (dvh) | ✅ | ✅ y=0 | 无 | ✅ |
| iPhone 横屏 844×390 | 390px (dvh) | ✅ | ✅ | 无 | ✅ |
| 极窄屏 360×640 | 640px (dvh) | ✅ | ✅ | 无 | ✅ |
| 平板竖屏 820×1180 | 1180px (dvh) | ✅ | ✅ | 无 | ✅ |

滑到底部：`scrollIntoView` 后最后一项 `lastVisible: true`、距视口底部 `gapToBottom: 0`，内容不被裁、顶栏始终吸附。

---

## 三、每日任务子标题更新

原文案：

> WorkBuddy 成长中心自动化：积分与成长查询、成长任务、互动玩法、**开学季活动**与自动领奖（内置 WorkBuddy-Daily 脚本，账号池国内版账号只读共用凭据）。

「开学季活动」早已不成立 —— 上游 v3.x 已把开学季代码归档移除（v0.9.84 升级时记录在案）。按 vendor v3.7 真实能力改写：

> WorkBuddy 成长中心自动化：每日签到、旅行、抽奖、盲盒、Buddy、连签兑换、补签卡、礼包补偿、徽章共 8 类任务，附积分/用量/成长查询与自动领奖（内置 WorkBuddy-Daily v3.7 脚本，账号池国内版账号只读共用凭据）。

---

## 四、验证汇总

| 检查项 | 结果 |
| :--- | :--- |
| 单元测试 | 47 个文件全部通过，0 失败 |
| 巡检通知（真实摘要） | 问号消失、🔍 只出现 1 次、748 组合/22 账号 |
| 任务通知 | 🎯 只出现 1 次 |
| 日报消耗 | 昨日 8,590.68（官方底层同源） |
| viewport meta | 已改写为百度式禁缩放 + cover |
| 每日任务子标题 | 已更新为 v3.7 真实 8 类任务 |
| 容器状态 | Up (healthy)，健康检查 200 |

---

## 升级说明

```bash
docker compose pull
docker compose up -d
```

本次无新增环境变量、无配置变更、无数据库迁移。
