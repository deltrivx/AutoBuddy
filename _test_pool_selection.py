#!/usr/bin/env python3
"""账号池参与范围与能力矩阵选号收窄的回归测试。

覆盖两个真实故障（用户 2026-09-29 反馈「新加的账号一次都没被调用」）：

**故障一：新账号永远进不了候选（冷启动死循环）**

选号时曾用「正向白名单」收窄：

    known_yes = [a for a in candidates
                 if capability_state(a_id, model) == "yes"]
    if known_yes:
        candidates = known_yes      # ← 只留"明确支持"的

``capability_state`` 对**从未探测过**的账号返回 None，于是新账号被排除。
而巡检不会补上这个键：新账号 0 调用 → ``used_models`` 为空 → 只探
``BASE_MODELS`` 基模型清单 → 清单里没有自动发现的新模型（如
``hy4-preview-f``）→ 永远探不到 → 永远不进矩阵 → 永远不被选中。

    没调用 → 不被探测 → 矩阵缺键 → 不被选中 → 没调用

实测：4 个新 cn 账号 token 有效、确实在 enabledAccountIds 里，
但 selection_logs 中 0 次选中，老账号 24~28 次。

修正：只排除**明确不支持**（state == "no"），未知（None）保留参与。

**故障二：巡检探池外账号**

巡检传 ``_load_accounts()``（两份账号文件全量合并），而日常调用走
``_enabled_accounts()``（白名单 + 停用策略）。两套集合不一致，
表现为「账号已移出池子，巡检还在探它、还在报错」。
实测一轮探了 15 个账号，远多于池内数量。

修正：巡检改为传 ``_enabled_accounts(_load_accounts(), _load_pool_config())``。

不依赖 fastapi / httpx —— 用临时目录隔离数据目录，直接验证判定逻辑。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

FAIL = 0
PASS = 0


def check(name, cond, extra=""):
    global FAIL, PASS
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


tmpdir = Path(tempfile.mkdtemp())
os.environ["AB_DATA_DIR"] = str(tmpdir)
sys.path.insert(0, str(Path(__file__).parent / "gateway"))

# main.py 依赖 fastapi 等运行时包，本地/CI 未必装了；
# 而这里要校验的是判定契约，不需要把服务跑起来。
# 因此直接解析源码提取实现要点（与 _test_log_dedup.py 同一手法）。
ROOT = Path(__file__).parent
MAIN = ROOT / "gateway" / "main.py"


def read() -> str:
    return MAIN.read_text(encoding="utf-8")


print("== 账号池参与范围与能力矩阵收窄 ==")
src = read()

# ---------------------------------------------------------------------------
# 1. 选号收窄：必须「排除 no」而不是「只留 yes」
# ---------------------------------------------------------------------------
print("\n[1] 选号收窄不得排除未知账号")

# 两处收窄点（主选号 + 换号重试）都必须用 != "no"
n_not_no = src.count('capability_state(_account_id(a), model) != "no"')
check("两处收窄均改为「排除 no」（保留未知）", n_not_no == 2, f"实际 {n_not_no} 处")

# 旧的「只留 yes」写法必须彻底消失，否则新账号仍会被排除
n_yes_only = src.count('capability_state(_account_id(a), model) == "yes"')
check("旧的「只留 yes」写法已清除", n_yes_only == 0, f"仍残留 {n_yes_only} 处")

# ---------------------------------------------------------------------------
# 2. 行为契约：用等价逻辑验证三种能力状态
# ---------------------------------------------------------------------------
print("\n[2] 能力状态三种取值的选中契约")


def selected(cap_map, model):
    """复刻修正后的收窄逻辑：只排除明确 no。

    ⚠️ 必须连 `if not_no:` 守卫一起复刻。生产代码是：

        not_no = [a for a in candidates if state != "no"]
        if not_no:
            candidates = not_no

    收窄结果为空时（例如全部明确 no）**不覆盖** candidates，
    保持原候选集 —— 设计意图是「宁可把请求打到上游拿真实错误，
    也不在网关层编一个『无账号』」。

    早期版本的 helper 漏了这层守卫，导致「全为 no 时应保留原集」
    这条契约被误判为失败；修正的是 helper，不是生产代码。
    """
    not_no = [aid for aid, st in cap_map.items() if st != "no"]
    # 守卫：收窄结果为空则保留原候选集（全量）
    return not_no if not_no else list(cap_map.keys())


# 场景1：新账号（未知 None）+ 老账号（yes）→ 新账号必须保留
caps = {"new-1": None, "old-1": "yes"}
got = selected(caps, "hy4-preview-f")
check("未知账号与已知支持账号共存时，未知账号保留",
      "new-1" in got and "old-1" in got, f"got={got}")

# 场景2：明确 no 的账号必须被排除
caps = {"bad-1": "no", "old-1": "yes"}
got = selected(caps, "hy4-preview-f")
check("明确不支持的账号被排除",
      "bad-1" not in got and "old-1" in got, f"got={got}")

# 场景3：全是未知（纯新账号池）→ 全部保留，不能空
caps = {"new-a": None, "new-b": None}
got = selected(caps, "hy4-preview-f")
check("全为未知时全部保留（不得返回空）", len(got) == 2, f"got={got}")

# 场景4：全是 no → 保留（上游返回真实错误，不在网关层编「无账号」）
caps = {"bad-a": "no", "bad-b": "no"}
got = selected(caps, "hy4-preview-f")
check("全为 no 时不返回空（让上游给真实错误）", len(got) == 2, f"got={got}")

# ---------------------------------------------------------------------------
# 3. 巡检账号来源：必须走账号池白名单
# ---------------------------------------------------------------------------
print("\n[3] 巡检只探账号池内账号")

check("巡检传入 _enabled_accounts(...) 过滤后的账号",
      "_enabled_accounts(_load_accounts()" in src,
      "未找到池过滤调用")

# 不得再直接把全量账号喂给巡检
import re

m = re.search(r"run_round\(\s*accounts=(.*?),\s*\n", src, re.S)
if m:
    arg = m.group(1)
    check("run_round 的 accounts 不再是裸 _load_accounts()",
          "_enabled_accounts" in arg, f"实际: {arg.strip()[:60]}")
else:
    check("能定位 run_round 的 accounts 入参", False, "未匹配到 run_round 调用")

# _enabled_accounts 必须同时叠加白名单与停用策略
fn_start = src.find("def _enabled_accounts")
fn_body = src[fn_start:fn_start + 1200] if fn_start >= 0 else ""
check("_enabled_accounts 叠加 enabledAccountIds 白名单",
      "enabledAccountIds" in fn_body)
check("_enabled_accounts 叠加账号级停用策略",
      "account_policy.disabled_ids()" in fn_body)

# ---------------------------------------------------------------------------
# 4. 巡检不应绕过这个过滤（overrides 显式指定时仍由 select_targets 收窄）
# ---------------------------------------------------------------------------
print("\n[4] 手动指定账号时的收窄仍在")

check("model_health.select_targets 仍按 accountIds 收窄",
      'wanted = set(config.get("accountIds") or [])' in
      (ROOT / "gateway" / "model_health.py").read_text(encoding="utf-8"))

if FAIL:
    print(f"\n❌ 失败 {FAIL} 项（通过 {PASS}）")
    sys.exit(1)
print(f"\n全部通过（{PASS} 项）")
sys.exit(0)
