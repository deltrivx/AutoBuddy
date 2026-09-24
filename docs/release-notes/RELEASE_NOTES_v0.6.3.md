# AutoBuddy v0.6.3

> 修复新账号卡片上「参与调用 · 点击停用 / 设为首选 / 检测账号 / 已调用 N 次」整条控件不出现的问题。

## 问题

注入的账号池控制条用**卡片标题文字**（账号昵称）去官方 `/api/account-pool` 的返回列表里反查条目。而官方只在 `selectionCounts` 里返回**已有调用记录**的账号名——新账号（刚添加、从未被调用过）在那里既没有昵称也没有邮箱。

标题对不上任何键 → 那张卡片的整条控制条不渲染 → 用户看到的就是「新账号缺失功能按钮」。

## 修复

### 后端：`/api/account-pool` 补字段
在官方原始响应上**补充**（不改动原有字段）：

- `nickname` / `email`：名字兜底顺序 `nickname → email → 账号 ID`
- `disabled` / `disabledSource`：账号级停用状态与来源（manual / auto）
- `enabled`：是否参与调用的明确布尔值
- 把账号池里**未出现在官方列表**的新账号一并补进 `accounts`

业务语义（`mode` / `enabledAccountIds` / `selectionCounts` / `allEnabledByDefault`）原样透传。官方原始响应保留在 `/api/account-pool-official` 供排查对比。

### 前端：多重索引 + 位置兜底
`name` / `id` / `nickname` / `email` 四个键都建立索引；标题仍对不上时按卡片位置取账号，不再轻易放弃渲染。

## 踩过的坑（值得记住）

先写的补字段路由与原有的 `/api/account-pool` **同名**。FastAPI 对同名路由只认**先注册**的那个，于是补字段逻辑静默失效——`py_compile` 语法检查通过、61 项面板测试全绿，**没有一个能在改前发现它**。

教训：改响应层路由后必须**实际请求一次**确认新版逻辑生效（本版已按此验收）；路由同名属于静默覆盖，不报错、不告警。
