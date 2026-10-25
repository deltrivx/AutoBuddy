#!/usr/bin/env python3
"""v2.1.0 并入补丁回归测试（源：另一设备 v1.0.16 补丁镜像 autobuddy-patched-20261024）。

背景：另一设备曾对 v1.0.16 镜像打过 7 项补丁。经比对：
  · wb_daily 刷屏根因修复 —— 上游已重构并入（只按 rc 判定），无需重复；
  · anchor_pilot 锚点轮 —— 补丁自身未接任何 API/UI，死代码，忽略；
  · 其余 5 项有效补丁在此并入，本测试逐项锁死，防回归。

源码驱动（与 test_text_replacements 同思路）：直接读源码断言，
不 import 运行时模块。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
NOTIFY = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
MH = (ROOT / "gateway" / "model_health.py").read_text(encoding="utf-8")
RG = (ROOT / "gateway" / "refresh_guard.py").read_text(encoding="utf-8")
WBD = (ROOT / "gateway" / "wb_daily.py").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(label, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label} {detail}")


print("[1] 账号池就绪探针（补丁 优化9）")
check("main.py 定义 account_pool_ready_loop",
      "async def account_pool_ready_loop()" in MAIN)
check("lifespan 注册探针任务并在 finally 取消",
      "pool_probe_task = asyncio.create_task(account_pool_ready_loop())" in MAIN
      and "pool_probe_task.cancel()" in MAIN)
check("只在「上一轮>0 且本轮=0」下降沿推送（send_scene 场景 account_pool_ready）",
      '"account_pool_ready"' in MAIN and "prev_usable > 0 and count == 0" in MAIN)
check("notify 注册 account_pool_ready 场景（设置页可开关）",
      '"account_pool_ready"' in NOTIFY)

print("\n[2] 流式首帧 TTFB 埋点（补丁 优化7）")
check("reqlog.set_status 在流式首帧被调用",
      re.search(r"async for chunk in res\.aiter_bytes\(\):.*?reqlog\.set_status\(rid, res\.status_code",
                MAIN, re.S) is not None)
check("TTFB = 首帧时刻 - 请求开始时刻", "(time.time() - start_time) * 1000.0" in MAIN)

print("\n[3] model_health autoEnableAccounts 白名单（补丁 优化6 补字段）")
check("default_config 含 autoEnableAccounts（默认开，与既有行为一致）",
      '"autoEnableAccounts": True' in MH)
check("_coerce_config 接受 autoEnableAccounts（旧版隐性常开 bug 修复）",
      re.search(r'if "autoEnableAccounts" in raw:', MH) is not None)
check("merge_config 键表含 autoEnableAccounts（设置页可改）",
      '"checkAccounts", "autoDisableAccounts", "autoEnableAccounts"):' in MH)
check("使用点缺键默认 False（旧配置文件不隐性开启）",
      'config.get("autoEnableAccounts", False)' in MH)

print("\n[4] refresh_guard _save 加锁（补丁 优化7）")
check("_save 整体在 _LOCK 内（并发撕裂保护）",
      re.search(r"def _save.*?with _LOCK:\s*\n\s*try:", RG, re.S) is not None)

print("\n[5] 签到推送补今日新增/消耗（补丁「院长需求」）")
check("build_checkin_done 签名含可选 today_task / usage_today",
      re.search(r"def build_checkin_done\(.*?today_task: float = 0\.0,\s*\n\s*usage_today: float = None", NOTIFY, re.S) is not None)
check("推送正文含「今日新增」与「今日消耗」行",
      "今日新增" in NOTIFY and "今日消耗" in NOTIFY)
check("usage_today 取不到显示 —（区别于真实 0）",
      '"—" if usage_today is None' in NOTIFY)
check("wb_daily 调用传入 today_task / usage_today",
      "today_task=today_task, usage_today=usage_today" in WBD)
check("今日消耗取数走 WebUI 代理校准口径（PORT 默认 18090，非官方 57890）",
      'os.getenv("PORT", "18090")' in WBD
      and 'http://127.0.0.1:%d/api/credits/stats' in WBD
      and ":57890" not in WBD)

print("\n[6] 判定记录（防误删）")
check("anchor_pilot 锚点轮未并入（死代码，补丁里无任何 API/UI 接线）",
      "anchor_pilot" not in WBD and "anchor_pilot" not in MAIN)

print(f"\n{_ok} passed, {len(_fail)} failed")
sys.exit(1 if _fail else 0)
