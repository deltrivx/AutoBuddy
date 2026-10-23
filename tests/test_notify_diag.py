#!/usr/bin/env python3
"""日报失败日志 / 延后推送留痕 / 助手人格化（2026-10-15 用户反馈）。

### 用户反馈（三条）

> 「查看日志，有提示日报发送失败，当前我问过今天执行过任务吗？也只是回复
> 我正在查询，但是没有结果，我问你能做什么，回复的也比较机械，而不是那种
> 我是一个智能助手，我能做……，没有那种智能助手的智能体回复，感觉都是写死的回复」

### 实测根因

**问题 1 & 2 同根：企微可信 IP 失效。**

容器出网 IP 实测 `112.194.50.154`（企微报错里还见过 `.151`、`.1` —— NAT 池），
而企微后台配的可信 IP 还是旧的 `112.194.50.104`。ISP 换 IP 后：

```
message/send → 200  errcode=60020 not allow to access from your ip,
               from ip: 112.194.50.154
```

⇒ 所有企微**主动推送**（日报、任务通知、助手延后答复）全部被拒。

两个表现：
1. 日报失败日志只打 `None` —— 因为 `send_scene` 失败时顶层 `error` 常为 None，
   真实错误在每个渠道的 `results.<channel>.error` 里；
2. 延后推送失败被 worker 静默吞掉，用户只看到「正在查询」然后永远没下文。

**问题 3 独立：能力问句恒返回写死清单。**

`_looks_like_capability_question()` 命中后直接 `return _cmd_assistant_help()`，
那是一段写死的功能列表 —— 所以「你能做什么」永远得到机械回复，
没有「我是…我能…」的智能体感。

### 修复

1. `main._scene_err()`：从 `send_scene` 返回值提取**可读**失败原因；
2. `notify._push_to_channel()`：失败必须留痕；`_wecom_ip_hint()` 对
   errcode=60020 给出**可操作**指引（该加哪个 IP、去哪加）；
3. 延后 worker：拿不到答复也记日志，不再完全静默；
4. 能力问句改为**模型优先**（智能体感），失败才回落本地清单；
5. `SYSTEM_PROMPT` 加入人格设定：身份、口语化、「不要罗列功能清单」。

⚠️ **IP 本身需要人工处理**：到企微管理后台 → 应用管理 → 可信 IP，
添加当前出口 IP（NAT 池可能多个，建议把整个段加上）。

本测试锁定上述代码契约。
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_M = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
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


print("[1] 日报失败日志 surface 真实错误（main.py）")

check("_scene_err 辅助函数存在", "def _scene_err(" in SRC_M)
check("日报失败改用它", 'print("[report] 日报发送失败: %s" % _scene_err(r))' in SRC_M)
check("不再直接打印顶层 error（那往往是 None）",
      'print("[report] 日报发送失败: %s" % r.get("error"))' not in SRC_M)
check("_scene_err 遍历 results 提取渠道错误",
      "for ch, res in (r.get(\"results\") or {}).items():" in SRC_M)
check("_scene_err 有兜底（无 results 时用顶层 error）",
      "r.get(\"error\") or \"未知原因\"" in SRC_M)

print("\n[2] 延后推送失败留痕（notify.py）")

check("_push_to_channel 失败打印日志",
      'print("[assistant] 助手答复推送失败（%s）：%s"' in SRC_NT)
check("_push_to_channel 未知渠道也留痕",
      'print("[assistant] 推送失败：未知渠道 %s"' in SRC_NT)
check("_push_to_channel 异常留痕（不再静默）",
      'print("[assistant] 推送异常（%s）：%s: %s"' in SRC_NT)
check("_wecom_ip_hint 存在", "def _wecom_ip_hint(" in SRC_NT)
check("60020 时给出 IP 提示", "_wecom_ip_hint(err)" in SRC_NT)
check("提示里含「可信 IP」关键词", "可信 IP" in SRC_NT)
check("提示里含自查命令", "api.ipify.org" in SRC_NT)
check("延后 worker 拿不到答复也记日志",
      'print("[assistant] 延后问答未得到答复' in SRC_NT)
check("延后 worker 异常记日志",
      'print("[assistant] 延后问答异常：%s: %s"' in SRC_NT)

print("\n[3] 能力问句模型优先（智能体感）")

check("能力问句不再恒返回写死清单",
      "if _looks_like_capability_question(text, low):\n            return _cmd_assistant_help()" not in SRC_NT)
check("助手可用时先走模型",
      "if _assistant_available():" in SRC_NT
      and "r = _assistant_fallback(text, session_id)" in SRC_NT)
check("模型不可用时回落本地清单",
      "return _cmd_assistant_help()" in SRC_NT)
check("企微场景走延后（5 秒硬限制）",
      "if allow_defer and _defer_assistant_reply(text, session_id):" in SRC_NT)
check("延后回执文案更自然",
      "让我整理一下能做什么" in SRC_NT)

print("\n[4] 系统提示词人格化（assistant.py）")

check("身份设定：智能管家", "智能管家" in SRC_AS)
check("明确「不是命令解释器」", "不是命令解释器" in SRC_AS)
check("口语化指引", "口语化" in SRC_AS)
check("禁止罗列功能清单", "不要罗列功能清单" in SRC_AS)
check("给出自我介绍示例", "我是 AutoBuddy 的智能助手" in SRC_AS)
check("边界：不做高危操作", "不执行登录/改密/删除数据这类高危操作" in SRC_AS)

print("\n[5] 行为验证（真实函数，不联网）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
os.environ["AB_ASSISTANT_WEBUI"] = "http://127.0.0.1:1"
os.environ["AB_ASSISTANT_GATEWAY"] = "http://127.0.0.1:1"
sys.path.insert(0, str(ROOT))
from gateway import notify as N  # noqa: E402

# 5a. _wecom_ip_hint：60020 → 可操作提示；其他 → 空
hint = N._wecom_ip_hint(
    "发送失败：errcode=60020 not allow to access from your ip, "
    "hint: [1792020413], from ip: 112.194.50.154, more info at ...")
check("60020 提示非空", bool(hint))
check("60020 提示含实际 IP", "112.194.50.154" in hint, f"got {hint[:120]}")
check("60020 提示含修复动作（添加）", "添加" in hint)
check("60020 提示指明位置（可信 IP）", "可信 IP" in hint)
check("非 60020 返回空串", N._wecom_ip_hint("errcode=40001 invalid credential") == "")
check("空错误不炸", N._wecom_ip_hint("") == "")
check("None 不炸", N._wecom_ip_hint(None) == "")

# 5b. _push_to_channel：未知渠道返回 False 且不抛
check("推送未知渠道返回 False", N._push_to_channel("__nonexistent__", "hi") is False)

# 5c. 能力问句：助手关闭时回落本地清单（行为不回退）
_old = os.environ.get("AB_ASSISTANT_ENABLED")
os.environ["AB_ASSISTANT_ENABLED"] = "0"
import importlib  # noqa: E402
importlib.reload(sys.modules["gateway.assistant"]) if "gateway.assistant" in sys.modules else None
from gateway import assistant as _A5  # noqa: E402
_A5.ASSISTANT_ENABLED = False
r = N.wecom_command_reply("你可以做什么", session_id="cap-test")
check("助手关闭时能力问句回落本地清单",
      bool(r) and "智能助手" in r, f"got {(r or '')[:60]}")
check("回落清单仍含用法示例", "现在积分多少" in (r or ""))
_A5.ASSISTANT_ENABLED = True
if _old is None:
    os.environ.pop("AB_ASSISTANT_ENABLED", None)
else:
    os.environ["AB_ASSISTANT_ENABLED"] = _old

# 5d. 人格化提示词可读
check("SYSTEM_PROMPT 含身份段落", "## 你是谁" in _A5.SYSTEM_PROMPT)
check("SYSTEM_PROMPT 含说话风格段落", "## 怎么说话" in _A5.SYSTEM_PROMPT)

# 5e. 日报错误提取逻辑（复刻 _scene_err 语义做等价验证）
def scene_err(r):
    if not isinstance(r, dict):
        return str(r)[:120]
    errs = []
    for ch, res in (r.get("results") or {}).items():
        if isinstance(res, dict) and res.get("error"):
            errs.append("%s: %s" % (ch, str(res["error"])[:200]))
        elif isinstance(res, dict) and not res.get("ok"):
            errs.append("%s: 未知失败" % ch)
    if not errs:
        return str(r.get("error") or "未知原因")[:200]
    return " | ".join(errs)

r1 = {"ok": False, "error": None,
      "results": {"wecom": {"ok": False, "error": "发送失败：errcode=60020 not allow to access"}}}
e1 = scene_err(r1)
check("能提出 60020（不再是 None）", "60020" in e1 and e1 != "None", f"got {e1[:80]}")
check("错误带渠道名", "wecom" in e1)
r2 = {"ok": False, "error": None, "results": {}}
check("无 results 时兜底", scene_err(r2) == "未知原因")
r3 = {"ok": False, "error": "通知总开关未开启"}
check("顶层 error 仍可用", "总开关未开启" in scene_err(r3))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
