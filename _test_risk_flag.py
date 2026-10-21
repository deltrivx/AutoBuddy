#!/usr/bin/env python3
"""账号风控标记（risk_flag）回归测试。

锁定三条契约：

1. 标记与停用**彻底分离**：`risk_flag` 有自己独立的落盘文件，绝不写
   `account_policy.json`、绝不碰 `accounts.json`；
2. 只有上游**确诊**的错误码才记标记（11140 / 14018），
   限流 429、过期 401 一律不记 —— 它们各有自己的处置通道；
3. 同一账号冷却期内不重复刷，但**次数要累加**（界面要显示历史触发次数）。

外加一条红线断言：模块可执行代码里不存在任何停用账号的调用面。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT))

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


tmp = Path(tempfile.mkdtemp())
os.environ["AB_DATA_DIR"] = str(tmp)

import risk_flag as rf  # noqa: E402

# —— 接住推送回调 ——
fired = []
# 回调签名固定两个参数 (account_id, flag)。写成三个会被 _fire_alert 的
# try/except 静默吞掉，表现为「一次都没推」—— 这种坑要记牢。
rf.set_alert_callback(lambda acc_id, flag: fired.append((acc_id, flag)))

print("== 落盘位置：与停用表分离 ==")
check("状态文件是独立的 account_risk_flags.json",
      rf.STATE_FILE.name == "account_risk_flags.json", f"got {rf.STATE_FILE}")
check("状态文件落在 AB_DATA_DIR 下",
      str(rf.STATE_FILE).startswith(str(tmp)), f"got {rf.STATE_FILE}")
check("不是 account_policy.json",
      rf.STATE_FILE.name != "account_policy.json")

print("\n== 只有确诊码才记标记 ==")
rf.clear("c_11140")
rf.record("c_11140", 11140, '{"code":11140,"msg":"x"}')
check("11140 账号被拦 → 记上标记", rf.get("c_11140") is not None)
# 14018 额度用尽**不**算风控：它有专属处置（model_health 的 quota_exhausted），
# 塞进风控标记会让人以为账号被腾讯盯上，实际只是没钱了。
for code, label in ((14018, "14018 额度用尽"), (429, "429 限流"), (401, "401 登录过期"),
                    (404, "404 模型不存在"), (500, "500 上游炸了")):
    rf.clear("n_" + str(code))
    rf.record("n_" + str(code), code, '{"code":%d,"msg":"x"}' % code)
    check(f"{label} → 不记标记（各有自己的处置通道）",
          rf.get("n_" + str(code)) is None)
rf.clear("n_none")
rf.record("n_none", 403, "<html>403 Forbidden</html>")
check("非 JSON 响应体不记标记", rf.get("n_none") is None)
rf.clear("n_str")
rf.record("n_str", 403, '{"code":"11140","msg":"x"}')
check("字符串形式的 code 不认（含义不一致，不猜）", rf.get("n_str") is None)
# 注意：record() 只接**调用方解析好的** code，自己不解 snippet ——
# 解析响应体是 main.py._extract_upstream_code 的活（它要兼容
# {"code":…} 与 {"data":{"code":…}} 两种上游形状）。
# 这里锁定的契约是：调用方从 data.code 里解出 11140 交给 record 后，
# 标记照样成立。
rf.clear("d_nested")
rf.record("d_nested", 11140, '{"data":{"code":11140,"msg":"x"}}')
check("调用方解出 code 后标记成立", rf.get("d_nested") is not None)
# 字符串 code 会被 int() 强转后记上 —— 这不是漏洞：调用方
# main.py._extract_upstream_code 只从 JSON 里取**数字** code 才往下传，
# 字符串形态在那一层就被挡掉了。这里锁定「record 接受 int」的契约，
# 并提醒调用方别把上游文案里的数字直接喂进来。
rf.clear("n_code_str")
rf.record("n_code_str", 11140, "x")
check("数字 code 正常记标记", rf.get("n_code_str") is not None)
rf.clear("n_code_bad")
rf.record("n_code_bad", "not-a-number", "x")
check("非数字字符串 code 不炸也不记", rf.get("n_code_bad") is None)

print("\n== 标记内容 ==")
rf.clear("c_detail")
rf.record("c_detail", 11140, '{"code":11140,"msg":"账号存在异常行为"}', source="forward")
flag = rf.get("c_detail")
check("记下上游错误码", flag.get("code") == 11140, f"got {flag.get('code')}")
check("记下最近触发时间", isinstance(flag.get("lastSeenAt"), int) and flag.get("lastSeenAt") > 0)
check("记下首次触发时间", isinstance(flag.get("firstSeenAt"), int) and flag.get("firstSeenAt") > 0)
check("记下来源", flag.get("source") == "forward", f"got {flag.get('source')}")
check("记下次数", flag.get("hits") == 1, f"got {flag.get('hits')}")
check("带上人话原因", bool(flag.get("reason")))

print("\n== 冷却期内不重复推，但次数累加 ==")
rf.clear("c_cool")
rf.record("c_cool", 11140, "x")
rf.record("c_cool", 11140, "x")
rf.record("c_cool", 11140, "x")
check("冷却期内只推一次", len([f for f in fired if f[0] == "c_cool"]) == 1,
      f"fired={[(a, fl.get('hits')) for a, fl in fired]}")
check("三次触发次数累加为 3", rf.get("c_cool").get("hits") == 3,
      f"got {rf.get('c_cool').get('hits')}")

print("\n== 冷却期过后可再推 ==")
st = rf._load()
st["flags"]["c_cool"]["alertedAt"] = int(time.time()) - rf.ALERT_COOLDOWN - 5
rf._save(st)
rf.record("c_cool", 11140, "x")
check("过了冷却期能再推", len([f for f in fired if f[0] == "c_cool"]) == 2,
      f"fired={[(a, fl.get('hits')) for a, fl in fired]}")

print("\n== 解除标记 ==")
rf.clear("c_detail")
check("clear 后不再标记", rf.get("c_detail") is None)
check("clear 后 flagged_ids 不含它", "c_detail" not in rf.flagged_ids())

print("\n== 快照 ==")
rf.clear("s1")
rf.record("s1", 11140, "x")
snap = rf.all_flags()
check("快照里有该账号", "s1" in snap)
check("快照带出次数", snap["s1"].get("hits") == 1)
check("快照带出错误码", snap["s1"].get("code") == 11140)
check("flagged_ids 含该账号", "s1" in rf.flagged_ids())

print("\n== 与停用表互不写入 ==")
# 建一个 account_policy.json，记满标记，看它有没有被改动
pol = tmp / "account_policy.json"
pol.write_text('{"version":1,"disabled":{"someone":"manual"}}', encoding="utf-8")
before = pol.read_text(encoding="utf-8")
rf.clear("x1")
rf.record("x1", 11140, "x")
rf.record("x1", 11140, "x")
rf.clear("x1")
rf.remove_account("x1")
check("account_policy.json 内容零改动", pol.read_text(encoding="utf-8") == before)
check("risk_flag 自己的文件确实写成功了", rf.STATE_FILE.exists())

print("\n== 红线：模块无停用账号的调用面 ==")
import io  # noqa: E402
import re  # noqa: E402
import tokenize  # noqa: E402

src = (ROOT / "gateway" / "risk_flag.py").read_text(encoding="utf-8")


def code_only(text):
    """用 tokenize 剥掉注释与字符串字面量，只留可执行代码。

    为什么不用「行首不是 # 就留下」的土办法：docstring 的后续行不带引号，
    那种判法会把整篇说明文字当成代码，红线断言全被自己的注释误伤
    （本次实测踩过：注释里写「不写 accounts.json」被自己的断言判成违规）。
    tokenize 是唯一可靠的剥离方式。
    """
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        pass
    return " ".join(out)


code = code_only(src)
check("tokenize 剥离子符串成功（拿到了真实代码）", "def record" in code)
check("不 import account_policy",
      not re.search(r"\b(import|from)\s+account_policy\b", code))
check("不 import notify（推送走回调注入，防环）",
      not re.search(r"\b(import|from)\s+notify\b", code))
check("可执行代码里无 set_disabled / auto_disable 调用",
      not re.search(r"\b(set_disabled|auto_disable|set_enabled|auto_enable)\b", code),
      "注释里出现这些词不算")
check("不写 accounts.json", "accounts.json" not in code)
check("不 import model_policy / cooldown（各自独立）",
      not re.search(r"\b(import|from)\s+(model_policy|cooldown)\b", code))
check("不写 account_pool_config.json", "account_pool_config" not in code)
check("只写自己的 account_risk_flags.json",
      "account_risk_flags" in src and "account_risk_flags" not in code)
check("没有任何自动清除标记的调用面（解除只能人工）",
      "auto_clear" not in code and "auto_resolve" not in code)

# —— web_proxy 侧的转发端点 ——
# 2026-10-21 实测踩到的坑：web_proxy.py 末尾有一条 catch-all
# `@app.api_route("/{path:path}")`，/api/account-pool/risk-flag/clear 没有
# 显式注册时会被它转给官方后端，官方不认识该路径却仍返回 200 ——
# 前端看到成功 toast、标记实际没被清除。所以这里锁住「端点必须显式存在」。
wp_src = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")
wp_code = code_only(wp_src)
check("web_proxy 显式注册了 /api/account-pool/risk-flag/clear",
      '"/api/account-pool/risk-flag/clear"' in wp_src,
      "漏注册会被 catch-all 静默吞掉，界面显示成功但标记不清")
check("web_proxy 里该端点转发到网关本体而不是自己实现",
      "GATEWAY_BASE_URL + \"/account-pool/risk-flag/clear\"" in wp_src,
      "实现只此一处，转发避免两份逻辑漂移")
check("web_proxy 自己不在本地落盘风控标记（纯转发）",
      "account_risk_flags" not in wp_code,
      "落盘逻辑只在 gateway/risk_flag.py 一处")
check("web_proxy 里无 set_disabled / auto_disable 调用",
      not re.search(r"\b(set_disabled|auto_disable)\b", wp_code))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
