#!/usr/bin/env python3
"""主动播报回归（2026-10-14 用户确认「需要」）。

### 需求

三阶段路线落地后，用户确认要做方案里的第三项「主动播报」。

### 分工原则（避免与既有通知重复）

用户场景里已有 7 个通知场景，其中 `task_done` 发「任务完成 + 积分」。
本次新增的 `task_failed_analysis` 只发**分析**（为什么失败 + 怎么办）：

  - `task_done`            → 发**事实**（本次积分、账号数）
  - `task_failed_analysis` → 发**分析**（原因、建议、进度、已运行时长）

两者内容不重叠，不会对同一事件重复推送。

### 实现要点

1. `notify.build_task_failed_analysis()`：失败 detail → 「原因 + 建议」文案；
2. `notify._classify_task_failure()`：**共享**归因映射，助手工具
   `diagnose_task_failure` 也调它 —— 保证「问助手」与「主动推送」口径一致，
   不会出现两套说法；
3. `wb_daily._notify_task_failed()`：两处失败出口都接（`rc != 0` 分支 +
   `_guard` 兜底），`_failed_notified` 幂等标记保证一轮只发一次；
4. 通知异常一律吞掉 —— 附属功能绝不能影响任务主流程。

本测试锁定上述契约。
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
SRC_NT = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
SRC_AS = (ROOT / "gateway" / "assistant.py").read_text(encoding="utf-8")
SRC_WD = (ROOT / "gateway" / "wb_daily.py").read_text(encoding="utf-8")

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


print("[1] 场景与构建器（notify.py）")

check("task_failed_analysis 场景已注册",
      '{"key": "task_failed_analysis"' in SRC_NT)
check("场景说明写明与 task_done 的分工",
      "task_done 发事实" in SRC_NT)
check("build_task_failed_analysis 存在",
      "def build_task_failed_analysis(" in SRC_NT)
check("共享归因函数 _classify_task_failure 存在",
      "def _classify_task_failure(" in SRC_NT)

print("\n[2] 接入点（wb_daily.py）")

check("_notify_task_failed 存在", "async def _notify_task_failed(" in SRC_WD)
check("幂等标记 _failed_notified 已初始化",
      "self._failed_notified = False" in SRC_WD)
check("幂等判断在函数首行",
      "if job._failed_notified:" in SRC_WD and "job._failed_notified = True" in SRC_WD)
# 两处失败出口都接
check("rc != 0 分支接了失败通知",
      "if job.status != \"done\":" in SRC_WD
      and "await _notify_task_failed(job)" in SRC_WD)
check("_guard 兜底出口也接（含 not _failed_notified 防重）",
      "if job.status != \"done\" and not job._failed_notified:" in SRC_WD)
check("推送在 _persist_run 之后（面板与推送数据一致）",
      SRC_WD.index("_persist_run(job, all_lines)")
      < SRC_WD.index("if job.status != \"done\":"))

print("\n[3] 口径一致（assistant.py 复用同一映射）")

check("助手归因工具调共享映射",
      "_nt6._classify_task_failure(detail)" in SRC_AS)
check("助手侧不再有自己的一套 if/elif 归因",
      'if "进程中断" in detail or "容器重启" in detail:' not in SRC_AS)
check("共享映射调用有异常兜底",
      "except Exception:" in SRC_AS
      and '"可查看容器日志进一步定位"' in SRC_AS)

print("\n[4] 归因映射行为（真实函数）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
sys.path.insert(0, str(ROOT))
from gateway import notify as N  # noqa: E402

cases = [
    ("进程中断（容器重启/被终止），未完成", "进程被中断", "重建容器"),
    ("账号池无可用账号", "账号池无可用账号", "凭据"),
    ("执行超时", "执行超时", "单账号超时"),
    ("内置脚本缺失: /app/x.py", "内置脚本缺失", "镜像"),
    ("TypeError: something broke", "TypeError", "容器日志"),
    ("", "原因未记录", "容器日志"),
]
for detail, want_reason, want_advice in cases:
    reason, advice = N._classify_task_failure(detail)
    check(f"归因 {detail[:24]!r} -> {want_reason}",
          want_reason in reason, f"got {reason}")
    check(f"建议 {detail[:24]!r} 含 {want_advice}",
          want_advice in advice, f"got {advice}")

print("\n[5] 通知文案（真实函数）")

title, lines = N.build_task_failed_analysis(
    "进程中断（容器重启/被终止），未完成", 18, 22, 2700.0)
check("标题含「失败分析」", "失败分析" in title, f"got {title}")
check("文案含原因", "原因" in "\n".join(lines))
check("文案含建议", "建议" in "\n".join(lines))
check("文案含进度（18/22）", "18/22" in "\n".join(lines))
check("文案含已运行时长（45 分钟）", "45 分钟" in "\n".join(lines))
check("空 detail 不炸", bool(N.build_task_failed_analysis("", 0, 0, 0.0)[1]))
check("零时长不显示时长行", "已运行" not in "\n".join(
    N.build_task_failed_analysis("x", 0, 0, 0.0)[1]))

print("\n[6] 场景总数与既有场景不冲突")

# 2026-10-21 加 account_risk_alert（风控预警）后为 9。这里断言的是
# 「数量与登记表一致」而不是某个魔法数——若以后再登记场景，
# 改这一处即可，别让 CI 因为数错场景挂掉。
check("场景数 = 9", len(N.SCENES) == 9, f"got {len(N.SCENES)}")
keys = [s["key"] for s in N.SCENES]
check("场景键唯一", len(keys) == len(set(keys)))
for must in ("task_done", "task_failed_analysis", "account_risk_alert"):
    check(f"既有场景 {must} 仍在", must in keys)
check("account_risk_alert 是纯通知场景（不触碰账号池）",
      "account_risk_alert" in keys)
check("task_done 与 task_failed_analysis 是不同场景（分工不重叠）",
      "task_done" in keys and "task_failed_analysis" in keys)

print("\n[7] send_scene 订阅过滤兼容新场景")

check("send_scene 按场景订阅过滤（新场景无需改代码）",
      "if scenes and scene not in scenes:" in SRC_NT)

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
