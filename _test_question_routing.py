#!/usr/bin/env python3
"""疑问句路由回归（2026-10-14 用户反馈：「我只是问他今天执行过任务吗？为何就直接执行了」）。

### 实测根因

复现 5 种问法，**全部命中写操作**：

```
今天执行过任务吗  -> _cmd_run()   ← 用户遇到的
任务跑完了吗      -> _cmd_run()
任务进度          -> _cmd_run()
执行任务          -> _cmd_run()（这条本就该执行）
任务              -> _cmd_run()
```

根因：`if "任务" in text` 是**子串匹配**，分不清「问状态」与「下令执行」。
用户问一句「今天执行过任务吗」，网关就直接把 22 个账号的任务跑了一遍。

### 用户指出的设计缺陷

> 「交互是交互，智能助手是智能助手，可以包含交互菜单命令，
> 而不是交互菜单不够了才调用智能助手。」

即：菜单与助手应**平级**，助手不是「命令没命中时的兜底」。

### 修复

1. **疑问句优先交助手**：`_looks_like_question()` 识别疑问句，作为**一等公民入口**
   放在命令分支**之前** —— 助手能用只读工具回答的，绝不被命令分支拦截；
2. **写操作只认显式精确命令**：`执行任务` / `启动任务` / `跑任务` / `运行任务` /
   `run`、`签到` / `全部签到` / `checkin`，且必须先剥掉首尾标点再精确匹配；
3. **只读查询仍允许关键词命中**（误命中也只是多返回数据，无害）；
4. **助手不可用时降级到只读兜底**：`_readonly_fallback()` 只走查询命令，
   红线是**绝不调用写操作** —— 疑问句永远是查询，助手挂了也不能把用户的任务执行了；
5. 顺带修 `_tool_task`：原先只读内存态 `jobs`（容器重启即丢），
   回答不了「今天跑过吗」这种历史问题；现补 `runs`（SQLite 落库）并统计今日轮次。

本测试锁定上述契约，重点防「问一句跑一轮」。
"""

import ast
import os
import re as _re0
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
SRC_NT = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
SRC_AS = (ROOT / "gateway" / "assistant.py").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(name, cond, extra=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


print("[1] 源码契约（notify.py）")

check("_looks_like_question 存在", "def _looks_like_question(" in SRC_NT)
check("_readonly_fallback 存在", "def _readonly_fallback(" in SRC_NT)
check("疑问句分支在命令分支之前",
      SRC_NT.index("if _looks_like_question(text):")
      < SRC_NT.index('if low.startswith("notify"):'),
      "疑问句必须最先判定，否则仍会被命令分支拦截")
check("疑问句优先调助手（一等公民，非兜底）",
      "if _looks_like_question(text):" in SRC_NT
      and "reply = _assistant_fallback(text, session_id)" in SRC_NT)
check("能力问句在疑问句内最先判定（不打 LLM，规避企微 5 秒红线）",
      "if _looks_like_capability_question(text, low):" in SRC_NT)
check("能力问句先于 LLM 延后判定",
      SRC_NT.index("if _looks_like_capability_question(text, low):")
      < SRC_NT.index("if allow_defer and _assistant_available():"))
check("写操作只认精确命令（剥标点后 in 元组）",
      '_cmd in ("执行任务", "启动任务", "跑任务", "运行任务")' in SRC_NT)
check("写操作 run 精确匹配", 'orig == "run"' in SRC_NT)
check("写操作 checkin 精确匹配", 'orig == "checkin"' in SRC_NT)
check("只读任务查询单独分支（关键词允许）",
      'if "任务进度" in text or orig == "daily":' in SRC_NT)
check("旧的宽松写匹配已移除（'任务' in text 直接 run）",
      'if "任务" in text or orig in ("daily", "run"):\n            if orig == "daily":' not in SRC_NT)
check("旧宽松签到匹配已移除",
      'if "签到" in text or orig in ("checkin",):' not in SRC_NT)

# 写操作必须**只**出现在精确匹配分支里（且被 `_cmd in (...)` 或 `orig == ...` 守卫）。
# 用 AST 精确定位：wecom_command_reply 内对 _cmd_run/_cmd_checkin 的调用，
# 必须位于一个 if 语句的 body 中（而不是裸调或宽松关键词分支）。
_tree = ast.parse(SRC_NT)
_fn = next(n for n in _tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "wecom_command_reply")


def _calls_in_if_body(fn_node, target: str) -> bool:
    for node in ast.walk(fn_node):
        if isinstance(node, ast.If):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == target):
                    return True
    return False


check("_cmd_run 只在 if 分支内被调用（精确匹配守卫）",
      _calls_in_if_body(_fn, "_cmd_run"))
check("_cmd_checkin 只在 if 分支内被调用（精确匹配守卫）",
      _calls_in_if_body(_fn, "_cmd_checkin"))
check("写操作调用受 _cmd in (...) 精确匹配守卫",
      '_cmd in ("执行任务", "启动任务", "跑任务", "运行任务")' in SRC_NT
      and '_cmd in ("签到", "全部签到")' in SRC_NT)
# `_readonly_fallback` 只允许调**只读**命令（_cmd_accounts/_cmd_models/
# _cmd_credits/_cmd_status/_cmd_daily）；`_cmd_run`/`_cmd_checkin` 只应出现在
# docstring 的「设计红线」说明里。所以剥离子串时要同时去掉 `#` 注释**和
# docstring**（三段引号包裹的内容），否则误把说明文字当成代码。
_ro_body = SRC_NT[SRC_NT.index("def _readonly_fallback"):
                 SRC_NT.index("def wecom_command_reply")]
_ro_code = _re0.sub(r'""".*?"""', '', _ro_body, flags=_re0.S)   # 去 docstring
_ro_code = "\n".join(l for l in _ro_code.splitlines()
                     if not l.lstrip().startswith("#"))          # 去行注释
check("_readonly_fallback 执行代码不含写操作",
      "_cmd_run()" not in _ro_code and "_cmd_checkin()" not in _ro_code,
      f"code:\n{_ro_code}")  # 带括号精确匹配：避免 _cmd_checkin_status（只读）被误判为写操作 _cmd_checkin()
check("_readonly_fallback 任务疑问只走 _cmd_daily",
      "_cmd_daily()" in _ro_code)
check("疑问句标记包含「吗/呢/多少/是否/有没有」等",
      all(m in SRC_NT for m in ('"吗"', '"呢"', '"多少"', '"是否"', '"有没有"')))
check("疑问句标记包含「是不是/跑完/跑过/执行过」",
      all(m in SRC_NT for m in ('"是不是"', '"跑完"', '"跑过"', '"执行过"')))
check("疑问句助手不可用时有明确提示",
      "这个问题我暂时答不上来" in SRC_NT)

# 旧的宽松写匹配（"任务" in text → 直接 run）必须已移除。
# 该串含换行，用变量承载避免多行字符串字面量。
_OLD_LOOSE_TASK = (
    'if "任务" in text or orig in ("daily", "run"):'
    '\n            if orig == "daily":'
)
_fn_body = SRC_NT[SRC_NT.index("def wecom_command_reply"):]
_fn_body = _fn_body[:_fn_body.index("\n\n\n")]
check("旧的宽松写匹配已移除", _OLD_LOOSE_TASK not in SRC_NT)

print("\n[2] 助手工具：任务历史（assistant.py）")

check("_tool_task 读 runs（落库历史）", "/api/wb-daily/runs" in SRC_AS)
check("_tool_task 统计今日轮次", "today_runs" in SRC_AS)
check("_tool_task 能回答「今天没有执行记录」",
      "没有执行记录" in SRC_AS)
check("_tool_task 仍读 jobs（实时进度）", "/api/wb-daily/jobs" in SRC_AS)

print("\n[3] 疑问句判定行为（真实函数）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
sys.path.insert(0, str(ROOT))
from gateway import notify as N  # noqa: E402

# 用户反馈的场景 + 同类问法：必须判为疑问句
question_cases = [
    "今天执行过任务吗",      # ← 用户原始反馈
    "任务跑完了吗",
    "执行过任务吗",
    "今天有跑任务吗",
    "任务进度",              # 无问号但语义是问状态（靠「进度」外的标记兜不住，
                             # 由只读分支接管，见 [4]）
    "签到了吗",
    "有没有签到",
    "现在积分多少",
    "模型有哪些",
    "网关状态怎么样",
    "能执行任务吗",
    "任务是不是失败了",
]
for q in question_cases:
    got = N._looks_like_question(q)
    # 「任务进度」不含疑问标记，允许为 False（由只读分支接管，不触发写操作）
    expect = q != "任务进度"
    check(f"_looks_like_question({q!r}) == {expect}", got == expect, f"got {got}")

# 显式命令：绝不能被误判成疑问句（否则点菜单按钮也会被助手接走）
command_cases = [
    "执行任务", "启动任务", "跑任务", "运行任务", "run",
    "签到", "全部签到", "checkin",
    "账号余额", "模型", "状态", "任务进度", "daily",
    "帮助", "help", "通知 on", "通知 off",
]
for c in command_cases:
    got = N._looks_like_question(c)
    check(f"_looks_like_question({c!r}) == False", got is False, f"got {got}")

print("\n[4] 写操作闸门（真实路由，助手关闭以隔离 LLM）")

# 关闭助手：疑问句必须降级到只读兜底，绝不能触发写操作
_old_env = os.environ.get("AB_ASSISTANT_ENABLED")
os.environ["AB_ASSISTANT_ENABLED"] = "0"

# assistant 模块级常量在 import 时求值，需 reload 才生效
import importlib  # noqa: E402
from gateway import assistant as A  # noqa: E402
importlib.reload(A)
importlib.reload(N)

fired = []
N._cmd_run = lambda *a, **k: fired.append("RUN") or "已启动一轮每日任务"
N._cmd_checkin = lambda *a, **k: fired.append("CHECKIN") or "签到已提交"
N._cmd_daily = lambda *a, **k: fired.append("DAILY") or "任务进度查询结果"

# 疑问句 → 不得触发 RUN / CHECKIN
for q in ("今天执行过任务吗", "任务跑完了吗", "执行过任务吗", "签到了吗"):
    fired.clear()
    N.wecom_command_reply(q)
    bad = [f for f in fired if f in ("RUN", "CHECKIN")]
    check(f"疑问句 {q!r} 不触发写操作", not bad, f"fired={fired}")

# 「任务进度」无疑问标记 → 由只读分支接管（DAILY 查询，不是 RUN）
fired.clear()
N.wecom_command_reply("任务进度")
check("「任务进度」走查询而非执行",
      fired == ["DAILY"], f"fired={fired}")

# 显式命令 → 阶段 5 起改为**两段式确认**：先返回待确认提示，
# 不立即执行；用户回「确认」才真正触发写操作。
# （旧契约是「立即触发 RUN」，v0.9.73 起有confirmed —— 这是防误触发的核心设计。）
fired.clear()
r_cmd = N.wecom_command_reply("执行任务")
check("「执行任务」先要确认（不立即执行）",
      fired == [] and bool(r_cmd) and "待确认" in r_cmd, f"fired={fired} reply={r_cmd[:40]}")

fired.clear()
r_ci = N.wecom_command_reply("签到")
check("「签到」先要确认（不立即执行）",
      fired == [] and bool(r_ci) and "待确认" in r_ci, f"fired={fired} reply={r_ci[:40]}")

# 带标点的命令也要认（用户手打常带句号）
fired.clear()
r_dot = N.wecom_command_reply("执行任务。")
check("「执行任务。」（带句号）仍进确认流程",
      fired == [] and bool(r_dot) and "待确认" in r_dot, f"fired={fired} reply={r_dot[:40]}")

# 英文命令（Telegram 菜单）
fired.clear()
r_run = N.wecom_command_reply("run")
check("「run」仍进确认流程",
      fired == [] and bool(r_run) and "待确认" in r_run, f"fired={fired} reply={r_run[:40]}")

fired.clear()
r_ci2 = N.wecom_command_reply("checkin")
check("「checkin」仍进确认流程",
      fired == [] and bool(r_ci2) and "待确认" in r_ci2, f"fired={fired} reply={r_ci2[:40]}")

# 确认后才真正执行（走 assistant 的执行器，不再经过 notify 的 _cmd_*）
try:
    try:
        from gateway import assistant as _A5
    except ImportError:
        import assistant as _A5  # type: ignore
    sid5 = "qr-confirm-test"
    _A5.clear_session(sid5)
    _A5.try_write_flow(sid5, "执行任务")      # 下令 → 待确认
    pend = _A5._pending_write(sid5)
    check("下令后待确认态已建立", pend is not None and pend.get("action") == "run_daily",
          f"pend={pend}")
    # 取消 → 不执行
    rc = _A5.try_write_flow(sid5, "取消")
    check("取消后不执行且清状态",
          bool(rc) and "已取消" in rc and _A5._pending_write(sid5) is None)
except Exception as _e5:
    check("确认流程可跑通", False, f"{type(_e5).__name__}: {_e5}")

# 恢复环境 + reload 回默认状态
if _old_env is None:
    os.environ.pop("AB_ASSISTANT_ENABLED", None)
else:
    os.environ["AB_ASSISTANT_ENABLED"] = _old_env
importlib.reload(A)
importlib.reload(N)

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
