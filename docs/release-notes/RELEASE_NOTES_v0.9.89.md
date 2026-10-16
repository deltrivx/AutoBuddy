> 修掉 v0.9.88 的一个**严重回归**：巡检功能其实完全没生效，白跑了 8 轮以上；另修顶栏选项卡键盘焦点不可见。

## 一、巡检完全失效（v0.9.88 引入的回归）

### 1. 现象

发布 v0.9.88 后看巡检日志，每 45 分钟固定刷一条错误：

```
[wb-daily] 巡检：发现中断的任务 3b40929af14e（failed），以 task 轮立即恢复
[wb-daily] 巡检异常: DailyJob.__init__() got an unexpected keyword argument 'kind'
```

中断的任务**一次都没恢复成**，已经连续失败 8 次以上。

### 2. 根因：两个 `__init__`，后者覆盖前者

`DailyJob` 类里出现了两个同名构造器 —— 我在类开头加了一个带 `kind` 参数的，但**不知道类的中后段原本就有一个**：

```python
class DailyJob:
    def __init__(self, jid, mode, kind="task"):     # 我加的（361 行）
        ...

    def __init__(self, job_id, mode):               # 原有的（402 行）
        ...
```

Python 的类体是顺序执行的，**后定义者覆盖先定义者**。所以生效的一直是原有那个（只有 `jid` 和 `mode`），我加的那个是死代码 —— 从签名上看不出来，只有真去调用才会炸。

调用方 `start_job(mode, kind)` 一直在传 `kind=`，于是每轮必抛 `TypeError`。

### 3. 为什么发布前没抓到

发布前的校验只做了**语法检查**（`ast.parse`）和**面板渲染测试**（62 项），这两类都验证不到「同名方法被覆盖」——语法完全合法，`DailyJob` 也能正常构造（只要不传 `kind`）。真正调用巡检路径才会暴露。

教训：给既有类加构造器参数时，必须先确认类里没有第二个 `__init__`。本次补了一条 AST 级校验（见下文「验证情况」）。

### 4. 修复

删掉重复的那个，把 `kind` 合进唯一的构造器：

```python
def __init__(self, job_id: str, mode: str, kind: str = "task"):
    """``kind`` = checkin / task（签到 / 任务调度拆分）。"""
    self.id = job_id
    self.mode = mode
    self.kind = kind if kind in ("checkin", "task") else "task"
    ...
```

实测：

```
DailyJob("x", "manual", kind="checkin").kind  → "checkin"
DailyJob("y", "manual").kind                  → "task"
start_job 签名                                 → (mode, kind='task')
```

### 5. 顺带清掉一个部署陷阱

容器里的 `__pycache__/wb_daily.cpython-312.pyc` 是旧字节码。当时源码 md5 明明和本地一致，但 `inspect.signature()` 读出来还是没有 `kind` —— 一度以为是缓存问题，实际是**同名方法覆盖**，pycache 只是干扰了判断。

本次部署显式 `rm -rf __pycache__` 后重启，排除这个变量。

## 二、顶栏选项卡键盘焦点不可见

### 1. 现象

用键盘 Tab 切换顶部 6 个选项卡时，**完全看不到焦点在哪哪个上**。

### 2. 根因

官方导航链接的 className 里带 Tailwind 的 `outline-none`：

```html
<a class="flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm
          outline-none transition-colors text-muted-foreground ...">
```

实测 6 个官方项 + 我注入的「每日任务」**全都带**。于是：

| 实测项 | 修复前 | 修复后 |
| :--- | :--- | :--- |
| `outlineStyle` | `none` | `solid` |
| `outlineWidth` | `2.67px`（有宽度无样式） | `2px` |
| `outlineColor` | 主题色（未生效） | `oklch(0.696 0.17 162.48)` |
| `:focus-visible` | 无可见样式 | 匹配 |

鼠标操作有 hover/active 兜底，看不出问题；纯键盘操作等于盲人摸象。

### 3. 修复

给顶栏内所有可聚焦元素补 `:focus-visible` 焦点环：

```css
#wb-topbar-inner nav a:focus-visible,
#wb-topbar-inner button:focus-visible,
#wb-theme-mini:focus-visible,
#wb-nav-toggler:focus-visible {
  outline: 2px solid var(--ring, #3b82f6) !important;
  outline-offset: 2px !important;
}
```

- 用官方 `--ring` 变量，深浅主题自动跟随
- `:focus-visible` 只在**键盘聚焦**时出现，鼠标点击不触发，不影响既有视觉
- 实测 PC 端 6 个选项卡 + 3 个按钮、移动端主题与收纳按钮全部生效

## 三、巡检不再把「正在执行」当欠账

`start_job()` 一开始就落一条 running 行，所以「正在跑」和「中断留下」在表里是同一条记录的两种状态。原判定只排除 `done`，于是巡检每次醒来都把正在执行的那轮当成欠账 —— health 的 `patrol.pending_run_id` 显示为当前运行中的 job，面板上看起来「巡检一直在报同一件事没处理」，实际它已经拉起来了。

现在显式排除 `running`，只有 `failed` / `aborted` 才算需要恢复的中断。

## 四、验证情况

- 语法：5 个 gateway 模块 `ast.parse` 全过；CSS 括号与注释配平；`node --check` 过
- 面板渲染测试：**62 项全部通过**
- 巡检构造器：AST 校验 `DailyJob` 内 `__init__` **数量为 1** 且参数含 `kind`；运行时实测三种入参
- 焦点环：CDP 实测 PC 端 9 个元素 + 移动端 2 个按钮，`outlineStyle` 全部 `solid`
- 巡检实跑：日志无异常，`patrol.pending` 为 `None`（不再误报），中断任务已能正常拉起
- 容器 md5 与本地一致
