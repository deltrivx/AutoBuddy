# AutoBuddy v0.9.25

> 一致性自检页补上「额度用尽」的显示档位，不再与「未检测」混淆。

## 背景

v0.9.24 已把额度耗尽正确判为 `quota_exhausted`。但界面上仍看不到——
它显示为「没检测出结果」。

## 先说定位结论：**判定是对的**

实测（对额度耗尽的账号发一次探测请求）：

```
HTTP 429
{"error":{"data":{"code":14018,"msg":"Credits exhausted. ..."}}}

code      = 14018
semantic  = quota_exhausted
verdict   = quota_exhausted
state     = quota_exhausted
```

对照旧代码：`14018` 不在语义表里 → `429` 属 `TRANSIENT_STATUS`
→ `transient` → 健康映射 `"transient": "unknown"`。

**所以界面显示的正是「没测出结果」** —— 与你看到的现象完全吻合。
这不是定位错误，是判定修好后**显示层还差一档**。

## 修复：补 audit 页的显示档位

检测接口 `/api/account-health/probe` 现在返回正确：

```json
{
  "verdict": "quota_exhausted",
  "state": "quota_exhausted",
  "message": "额度已用尽",
  "userMessage": "额度已用尽，已自动停用",
  "action": "充值或等待额度重置",
  "code": 14018
}
```

但一致性自检页（audit）的状态着色只认三档：

```js
if (st === "invalid") cls += " wb-audit-bad";
else if (st === "restricted") cls += " wb-audit-warn";
else if (st === "valid") cls += " wb-audit-ok";
```

`quota_exhausted` 一档不落 → 掉进「默认无着色」→
与「未检测」在视觉上无法区分。

现补上：

```js
else if (st === "quota_exhausted") cls += " wb-audit-warn";
```

配色与 `restricted` 同为琥珀：**两者都不是凭据坏了**，
用红色会让人跑去重新登录，而重登对这两种情况毫无帮助。

## 一处说明：「检测账号」按钮无需改动

那条路径（`/api/account-health/probe` 的渲染，2654 行）本来就是三分支：

```js
var tone = state === "valid" ? "ok"
         : (state === "invalid" ? "error" : "warn");
```

`quota_exhausted` 落在琥珀 `warn` 档，**已经是对的**。
本次只补 audit 页。

## 验证

- 容器实测接口返回：`state=quota_exhausted`、
  `userMessage=额度已用尽，已自动停用`、`code=14018`
- 对照正常账号：`HTTP 400 code=11102`（模型不存在）→
  `verdict=available` → `state=valid`，确认未误伤

```
15 项单测                      ✅ 全绿
五模块 AST                     ✅ 通过
注入 JS node --check           ✅ 通过
```

## 升级说明

重建容器即可，`/data` 数据不受影响。

- 无新增或删除环境变量；
- 无数据库结构变更；
- 纯前端显示修复，无判定逻辑变更。
