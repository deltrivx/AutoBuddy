# v0.3.14 - 二轮验收：账号页副标题还在宣称管理 CodeBuddy IDE 与 CLI

> 承接 v0.3.13。这轮换了个更严的筛法，又找出 **1 处真问题**（已修），并**修正了上一版对 `AZ` 弹窗的错误判定依据**。

## 1. 为什么上一轮漏了

v0.3.13 的普查是按「桌面 / CLI **关键词**」筛的。这个筛法有个盲区：**把已移除的产品当成功能卖点、写进正常句子的文案**，不含任何关键词，直接漏网。

这轮改成：把 patched 二进制前端 JS 区段里**所有**中文界面串抽出来（173 条），再对账号页 `kZ`、账号卡片 `$X`、设置页、导航**逐个组件全量过**。

## 2. 账号管理页副标题（唯一真问题，已修）

```jsx
<p className="mt-2 text-sm leading-6 text-muted-foreground">
  统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态。
</p>
```

这句在账号页顶部**常驻渲染**，是用户打开页面看到的第一句话。它把 `CodeBuddy IDE` / `CodeBuddy CLI` 说成本页的管理对象——而这两个桌面端产品早在 v0.3.11 就被物理移除，容器里根本不存在。

| | 文案 | 字节 |
| :--- | :--- | ---: |
| 改前 | 统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态。 | 90 |
| 改后 | 统一管理 WorkBuddy 账号、积分与签到状态，并为 API 网关提供账号。 | 88 |

新文案只描述容器里真实存在的两件事：账号/积分/签到管理 + 为 API 网关供号。

## 3. 修正上一版的一处判定依据

上一版把权限引导弹窗 `AZ` 记作「**没有任何调用点**」。这轮查实**该结论不准确**：

```js
p.jsx(AZ,{open:C!==null, onOpenChange:le=>{le||N(null)}, account:C, onDone:()=>{…}})
```

它确实被渲染。真正的死因是状态恒假——`[C,N]=j.useState(null)`，而全文件 `N(` **只被调用一次，且是 `N(null)`**，没有任何地方写入非空账号 → `open` 永远是 `false`。

结论（死代码）不变，但**依据要改**：这类弹窗不能只看「有没有调用点」，必须查到 setter 的**写入值**。同法复核另两处：

| 文案 | 宿主 | 死因 |
| :--- | :--- | :--- |
| 复制会话到目标账号 / 如何授权（只需 3 步）/ 在 Finder 中显示 / 立即检测 | `AZ` 弹窗 | `N` 只被写入 `null` → `open` 恒假 |
| 切换 CodeBuddy CLI / 关闭 CLI 并切换 | `Ka` 弹窗 | `Ot` 只由 `An` 写入，而 `An` 仅作为 `onSwitchCodebuddyCli` prop 传给卡片，卡片上那个按钮已抹除 → 无调用者 |
| 将注入凭证并重启 CodeBuddy IDE | `pi` | 仅作为 `onSwitchCodebuddyCnIde` prop 传递 → 无调用者 |

## 4. 「模型限额」徽章：确认容器内不可达

账号卡片仍接收 `rateLimits` prop，卡片上也还留着 `DX()` 渲染的 `模型限额：…` 徽章（`variant:"warning"`），一度怀疑「限额监听」有可见残留。查实：

```js
async function ds(le){ if(J!==!0)return; … const ze=await LF();
  const bt={}; for(const tt of ze.accounts??[]) tt.limited?.length&&(bt[tt.accountId]=tt.limited); q(bt) }
```

轮询确实在跑（`J` 由 `/api/rate-limits/config` 的 `enabled:true` 置真），但 `LF()` 即 `/api/rate-limits` 在容器里恒返回 `{"accounts":[]}`——它扫的是**宿主机 IDE 日志**，容器内没有。映射表恒为空 → `DX(undefined,t)` 走 `n.length===0` 直接 `return null`。**徽章不可能渲染**。

## 5. 复核其余界面（全部干净）

- 导航 4 项：账号管理 / Token 统计 / 积分统计 / 设置
- 设置页仅剩 2 个 section：`settings-auto-checkin`（自动签到）、`settings-appearance`（外观）
- 账号卡片可见文案：签到状态、积分包、`建议优先使用`、`N 个工具正在使用` —— 均为活功能
- `未登录` / `尚未接入` / `未检测到` / `未安装` 只是被已抹除的桌面状态区使用的局部变量
- 容器挂载只剩 `/data`（`/workspace` 已在早前移除）

## 6. 校验

- 补丁数 **18 → 19**，pristine 上 `applied: 19 failed: 0`
- **新增本地基线复现**：用补丁脚本对 npm 拉回的 pristine 跑一遍，产物 md5 与线上部署二进制**逐字节一致**（`2d46309d2a6dcefcadc2f8291d41e950`）→ 证明本地验证环境可信，不再「本地过了线上没过」
- 新旧产物 diff 仅副标题那 90 字节区间（67 字节实际变化），无副作用
- 字节数 12861680 不变；总校验 `FAILED: 0`

## 7. 仍然待你决定的两项（与 v0.3.12 相同）

- `/data/.codebuddy/`（18 MB，CLI 运行时残留）+ `/data/.codebuddy-rotate/`（helper.cjs + state.json）——删掉即可回收。
- `.wb-switch/auto_travel_config.json`（`enabled: true`）是**官方后端**的「自动出行」，无任何 UI，与「自动签到」同属积分保活家族；关掉可能减少积分收益，故未擅自处理。
