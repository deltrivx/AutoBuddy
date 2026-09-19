## 🔍 上一版漏了什么

v0.3.4 的发布说明写「账号卡片三个产品槽位已物理移除」，但实际上官方 UI 里这组槽位**存在两份**：

| 位置 | 形态 | v0.3.4 状态 |
| :--- | :--- | :--- |
| 卡片**底部 footer** | 带文字标签的三个槽位 | ✅ 已移除 |
| 卡片**头部账号名右侧** | 紧凑图标行 `div.ml-auto.flex.shrink-0.items-center.gap-1` | ❌ **漏掉了** |

实际界面上「国际版账号后面一个 CodeBuddy IDE 和 CodeBuddy CLI 按钮」，指的正是**头部图标行**。v0.3.4 之后它依然在，所以问题看起来「没修好」。

### 为什么上一版没验出来

v0.3.4 的验证用的是固定字面量断言：

```
grep -c 'aria-label":"设为 CodeBuddy CLI 当前账号"'
```

但真实 bundle 里这些 aria-label 是**三元表达式动态拼接**的：

```js
"aria-label": b ? "正在切换 CodeBuddy CLI 当前账号" : "设为 CodeBuddy CLI 当前账号"
```

所以这个字面量在**原始** bundle 里出现次数本来就是 0 —— 断言 `0 → 0`，等于什么都没验。这类「用错误的关键词做通过性断言」比不做断言更危险。

## 🛠️ 本次改动

头部图标行内含三个槽位，每个都有「已接入→徽章 / 未接入→按钮」两个分支，共六个 JSX 表达式：

```jsx
m.jsxs("div",{className:"ml-auto flex shrink-0 items-center gap-1",children:[
  h ? <WorkBuddy 当前账号徽章>      : <按钮 aria-label="设为 WorkBuddy 当前账号">,
  A ? <CodeBuddy IDE 当前账号徽章>  : <按钮 aria-label="切换到 CodeBuddy IDE">,
  x ? <CodeBuddy CLI 当前账号徽章>  : <按钮 aria-label="设为 CodeBuddy CLI 当前账号">
]})
```

整行 `div` 一次性物理移除（**2790 字节**，等长填充，二进制大小不变），父节点 `children` 第三项变为 `null`，React 渲染正常。

## ✅ 这次怎么验的

改用**真实浏览器加载页面后查 DOM**，不再依赖字符串断言：

```js
// 修复前
{ cards: 4, 当前账号按钮/徽章: 3/卡片 }
// 修复后
{ cards: 4, 当前账号按钮/徽章: 0, poolBars: 4, poolBtns: 8 }
```

断言 `article [aria-label*="当前账号"]` 数量为 **0**，并逐张卡片确认图标行已消失。

## 📦 升级方式

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> Unraid 请通过容器模板重建，不要手工拼接 `docker run`。
