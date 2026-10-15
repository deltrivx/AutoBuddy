#!/usr/bin/env python3
"""阶段 5：写操作两段式确认回归（2026-10-14）。

### 动因（用户原话）

> 「我只是问他今天执行过任务吗？为何就直接执行了……交互是交互，
> 智能助手是智能助手，可以包含交互菜单命令，而不是交互菜单不够了才调用智能助手。」

v0.9.71 已把「疑问句被命令拦截」修掉；本阶段解决另一半：
**写操作永不被自然语言直接触发**，必须先经一次显式确认。

### 结构性设计（不靠提示词，靠代码路径）

1. `try_write_flow()` 在 LLM **之前**拦截（notify 在疑问句之后、写分支之前调用）；
2. 意图识别是**确定性**的：关键词表 + 精确匹配，且双保险——疑问句永远不进写意图；
3. 确认态与会话同生命周期，TTL 5 分钟（短于对话 30 分钟，确认是即时决策）；
4. 每个写操作带 `impact`（影响范围）+ `estimate`（耗时预估），确认前可见；
5. 审计日志记录每次实际执行（时间/会话/操作/参数/结果）。

### 危险操作排除（面板可做，助手不做）

登录/改密、导入导出、删除全部 Key、账号永久删除 —— 涉及凭据与数据主权。

本测试锁定上述契约。
"""

import ast
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
SRC_AS = (ROOT / "gateway" / "assistant.py").read_text(encoding="utf-8")
SRC_NT = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")

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


print("[1] 源码契约（assistant.py）")

check("try_write_flow 存在", "def try_write_flow(" in SRC_AS)
check("WRITE_CONFIRM_ENABLED 开关", "WRITE_CONFIRM_ENABLED = " in SRC_AS)
check("WRITE_CONFIRM_TTL 存在", "WRITE_CONFIRM_TTL = " in SRC_AS)
check("_write_intent 确定性识别", "def _write_intent(" in SRC_AS)
check("_confirm_intent 确认/取消识别", "def _confirm_intent(" in SRC_AS)
check("_pending_write / _set_pending_write / _clear_pending_write",
      all(f"def {n}(" in SRC_AS for n in
          ("_pending_write", "_set_pending_write", "_clear_pending_write")))
check("审计日志 _audit_write + write_audit_log",
      "def _audit_write(" in SRC_AS and "def write_audit_log(" in SRC_AS)
check("写操作注册表 _WRITE_OPS", "_WRITE_OPS: Dict[str, Dict[str, Any]] = {}" in SRC_AS)
check("_register_write_op 装饰器", "def _register_write_op(" in SRC_AS)

# 双保险：疑问句永不进写意图
check("_write_intent 内调用 _looks_like_question（双保险）",
      "_nt._looks_like_question(text)" in SRC_AS)
check("_write_intent 自带疑问标记兜底",
      'for m in ("吗", "呢", "？", "?"):' in SRC_AS)

# 6 个写操作全部注册
for op in ("run_daily", "checkin_all", "abort_daily", "probe_models",
           "notify_on", "notify_off"):
    check(f"写操作 {op} 已注册", f'_register_write_op("{op}"' in SRC_AS)

# 每个写操作都带 impact + estimate（确认前可见）
# 每个写操作都带 impact + estimate（确认前可见）。
# ⚠️ 不能用 `SRC_AS.count("impact") >= 6` 这种源码计数：注册时是**位置传参**
# （`_register_write_op(action, desc, impact, estimate)`），源码里 impact/estimate
# 只出现 5 次（定义 1 + 转发 1 + 注释 1 + 提示 2）。真正可靠的判据是
# **遍历注册表**（见 [4] 末尾的循环，那里逐个校验 6 个 op 的字段）。
check("写操作带 impact（影响预告）",
      '"desc": desc, "impact": impact, "estimate": estimate,' in SRC_AS)
check("写操作带 estimate（耗时预估）",
      'op.get("estimate") or "—"' in SRC_AS)
check("待确认提示含影响与预计",
      "op.get(\"impact\")" in SRC_AS and "op.get(\"estimate\")" in SRC_AS)

# 危险操作排除。
# 2026-10-15 修订：断言原本扫全文，而 assistant.py 新增的「边界说明」注释块里
# 引用了这些端点名（为了说清楚「为什么不给工具」）。注释不是代码，直接用
# 全文匹配会误报。改为**剥除注释后**再扫，才是真正有效的判据。
import re as _re_wc
_SRC_AS_CODE = _re_wc.sub(r'#[^\n]*', '', SRC_AS)          # 去行注释
_SRC_AS_CODE = _re_wc.sub(r'""".*?"""', '', _SRC_AS_CODE, flags=_re_wc.S)  # 去 docstring

check("不碰登录/改密", "auth/login" not in _SRC_AS_CODE and "auth/password" not in _SRC_AS_CODE)
check("不碰导入导出", "api/import" not in _SRC_AS_CODE and "export-accounts" not in _SRC_AS_CODE)
check("不碰删除全部 Key", "delete-all" not in _SRC_AS_CODE)
check("不碰账号删除", "api/delete" not in _SRC_AS_CODE)

print("\n[2] 集成契约（notify.py）")

check("_write_confirm_flow 存在", "def _write_confirm_flow(" in SRC_NT)
check("命令路由调用确认流程", "reply = _write_confirm_flow(text, session_id)" in SRC_NT)
# 位置必须在疑问句之后、写操作分支之前
_q_idx = SRC_NT.index("if _looks_like_question(text):")
_w_idx = SRC_NT.index("reply = _write_confirm_flow(text, session_id)")
_run_idx = SRC_NT.index("return _cmd_run()")
check("确认流程在疑问句之后", _q_idx < _w_idx)
check("确认流程在写操作分支之前", _w_idx < _run_idx,
      "否则精确命令会绕过确认直接执行")

print("\n[3] 意图识别行为（真实函数）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
os.environ["AB_ASSISTANT_WEBUI"] = "http://127.0.0.1:1"   # 本组不联网
os.environ["AB_ASSISTANT_GATEWAY"] = "http://127.0.0.1:1"
sys.path.insert(0, str(ROOT))
from gateway import assistant as A  # noqa: E402
from gateway import notify as N  # noqa: E402

write_cases = [
    ("执行任务", "run_daily"), ("启动任务", "run_daily"), ("跑任务", "run_daily"),
    ("运行任务", "run_daily"), ("run", "run_daily"),
    ("把每日任务跑一轮", "run_daily"),
    ("签到", "checkin_all"), ("全部签到", "checkin_all"), ("checkin", "checkin_all"),
    ("去签到", "checkin_all"),
    ("中止任务", "abort_daily"), ("停止任务", "abort_daily"),
    ("触发巡检", "probe_models"), ("立即巡检", "probe_models"),
    ("开启通知", "notify_on"), ("关闭通知", "notify_off"),
]
for text, want in write_cases:
    got = A._write_intent(text)
    check(f"写意图 {text!r} -> {want}", got == want, f"got {got}")

# 疑问句/闲聊：绝不能识别成写意图（这是用户踩坑的核心）
none_cases = [
    "今天执行过任务吗", "任务跑完了吗", "任务进度", "执行过任务吗",
    "签到了吗", "有没有签到", "现在积分多少", "模型有哪些",
    "网关状态怎么样", "你好", "你会什么", "帮我订机票",
]
for text in none_cases:
    got = A._write_intent(text)
    check(f"非写意图 {text!r} -> None", got is None, f"got {got}")

print("\n[4] 两段式流程（真实会话状态，不联网）")

sid = "stage5-test"
A.clear_session(sid)

# 第一段：下令 → 待确认，且**未执行**
r1 = A.try_write_flow(sid, "执行任务")
check("下令返回待确认提示", bool(r1) and "待确认" in r1)
check("待确认提示含影响范围", "影响" in (r1 or ""))
check("待确认提示含耗时预估", "预计" in (r1 or ""))
check("待确认提示含确认/取消指引",
      "确认" in (r1 or "") and "取消" in (r1 or ""))
check("待确认态已记录", A._pending_write(sid) is not None)

# 取消 → 不执行、清状态
r2 = A.try_write_flow(sid, "取消")
check("取消有回执", bool(r2) and "已取消" in r2)
check("取消后待确认清除", A._pending_write(sid) is None)

# 待确认期间说别的 → 视为放弃，按新输入继续（不执行）
A.try_write_flow(sid, "执行任务")
r3 = A.try_write_flow(sid, "现在积分多少")
check("待确认期间提问不会执行写操作", r3 is None,
      "应返回 None 让上层走正常问答")
check("换话题后待确认已清除", A._pending_write(sid) is None)

# 确认词覆盖
for w in ("确认", "确定", "是的", "好", "好的", "ok", "yes", "y", "执行"):
    check(f"确认词 {w!r}", A._confirm_intent(w) == "confirm", f"got {A._confirm_intent(w)}")
for w in ("取消", "算了", "不用", "否", "不", "no", "n", "放弃"):
    check(f"取消词 {w!r}", A._confirm_intent(w) == "cancel", f"got {A._confirm_intent(w)}")

# TTL 过期
A._set_pending_write(sid, "run_daily", {})
with A._SESSIONS_LOCK:
    A._SESSIONS[A._session_key(sid)]["pending_write"]["expires"] = time.time() - 1
check("过期待确认自动失效", A._pending_write(sid) is None)
check("失效后确认不再执行", A.try_write_flow(sid, "确认") is None)

# 开关关闭 → 整个流程旁路
_old = A.WRITE_CONFIRM_ENABLED
A.WRITE_CONFIRM_ENABLED = False
check("关闭确认流程后写意图不被拦截", A.try_write_flow(sid, "执行任务") is None)
A.WRITE_CONFIRM_ENABLED = _old

# 审计日志结构
A._AUDIT.clear()
A._audit_write(sid, "run_daily", {"x": "y"}, "ok-result")
log = A.write_audit_log(5)
check("审计日志已记录", len(log) == 1)
check("审计含时间/会话/操作/结果",
      all(k in log[0] for k in ("ts", "session", "action", "args", "ok", "result")))
A._audit_write(sid, "checkin_all", {}, "失败：xxx")
check("审计能标记失败", A.write_audit_log(5)[-1]["ok"] is False)

# 写操作 impact/estimate 完整性
for k, v in A._WRITE_OPS.items():
    check(f"{k} 有 desc/impact/estimate",
          bool(v.get("desc")) and bool(v.get("impact")) and bool(v.get("estimate")))
    check(f"{k} 有可调用执行器", callable(v.get("fn")))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
