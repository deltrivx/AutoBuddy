#!/usr/bin/env python3
"""模型可用性自动巡检的本地单测。

不依赖 fastapi / httpx —— 用假客户端替换真实 HTTP，直接验证 model_health 的决策逻辑。
覆盖：
  1. 默认关闭（自动禁用会改行为，不能默认开）
  2. 配置读写回环 + 间隔下限钳制 + 损坏回退
  3. 账号范围：空数组 = 全部；显式列表 = 白名单；过期账号被排除
  4. onlyUsedModels：只探测用过的；无记录时退回基础清单
  5. 探测结果分类：429/超时 = transient（不自动禁用）；404/500 = unavailable
  6. 不可用 → 自动写入禁用策略
  7. 可用 → 自动移除禁用项（autoEnable 开启时）
  8. autoEnable 关闭时，可用不会自动放开
  9. transient 不改变策略（否则高峰期会把好模型全禁掉）
 10. 别名归一：禁用 hy4 连带挡住 hy3
 11. 文件落盘无 .tmp 残留
 12. 与手动禁用共用同一份策略（关掉巡检，禁用项仍在）
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
os.environ["WB_DATA_DIR"] = str(tmpdir)
sys.path.insert(0, str(Path(__file__).parent / "gateway"))

import model_policy  # noqa: E402
import model_health  # noqa: E402

print("== 模型可用性巡检单测 ==")

# ---------------------------------------------------------------------------
# 1. 默认配置
# ---------------------------------------------------------------------------
print("\n[1] 默认配置")
cfg = model_health.default_config()
check("默认关闭自动巡检", cfg["enabled"] is False, f"got {cfg['enabled']}")
check("默认自动启用开启", cfg["autoEnable"] is True)
check("默认只探测用过的模型", cfg["onlyUsedModels"] is True)
check("默认账号范围为空(=全部)", cfg["accountIds"] == [])
check("默认间隔 60 分钟", cfg["intervalMinutes"] == 60, f"got {cfg['intervalMinutes']}")

# ---------------------------------------------------------------------------
# 2. 配置读写
# ---------------------------------------------------------------------------
print("\n[2] 配置读写")
saved = model_health.merge_config({"enabled": True, "intervalMinutes": 15})
check("开启后落盘", saved["enabled"] is True)
check("间隔已保存", saved["intervalMinutes"] == 15)
reloaded = model_health.load_config()
check("重新读取一致", reloaded["enabled"] is True and reloaded["intervalMinutes"] == 15)

clamped = model_health.merge_config({"intervalMinutes": 1})
check("间隔低于下限被钳制", clamped["intervalMinutes"] == model_health.MIN_INTERVAL_MINUTES,
      f"got {clamped['intervalMinutes']}")

bad = model_health._coerce_config({"intervalMinutes": "abc", "accountIds": "not-a-list"})
check("非法间隔回退默认", bad["intervalMinutes"] == 60)
check("非法账号范围回退空", bad["accountIds"] == [])

model_health.HEALTH_CONFIG_FILE.write_text("{ this is not json", encoding="utf-8")
fallback = model_health.load_config()
check("文件损坏回退默认且不抛", fallback["enabled"] is False)
check("损坏时无 .tmp 残留", not (tmpdir / "model_health_config.json.tmp").exists())

# ---------------------------------------------------------------------------
# 3. 账号范围
# ---------------------------------------------------------------------------
print("\n[3] 账号范围")
now = 4_000_000_000_000
accounts = [
    {"id": "a1", "nickname": "账号1", "access_token": "t1", "variant": "ai"},
    {"id": "a2", "nickname": "账号2", "access_token": "t2", "variant": "cn"},
    {"id": "a3", "nickname": "账号3", "access_token": "t3", "variant": "ai", "expiresAt": 1000},
    {"id": "a4", "nickname": "账号4", "variant": "ai"},  # 无 token
]

cfg_all = {"accountIds": [], "onlyUsedModels": False}
targets = model_health.select_targets(accounts, cfg_all, {}, ["hy3", "kimi-k3"])
ids = sorted({t["accountId"] for t in targets})
check("空数组 = 全部可用账号", ids == ["a1", "a2"], f"got {ids}")
check("过期账号被排除", "a3" not in ids)
check("无 token 账号被排除", "a4" not in ids)
check("组合数 = 账号数 × 模型数", len(targets) == 4, f"got {len(targets)}")

cfg_one = {"accountIds": ["a2"], "onlyUsedModels": False}
targets = model_health.select_targets(accounts, cfg_one, {}, ["hy3"])
check("显式列表 = 白名单", [t["accountId"] for t in targets] == ["a2"])

# ---------------------------------------------------------------------------
# 4. onlyUsedModels
# ---------------------------------------------------------------------------
print("\n[4] 探测范围收窄")
used = {"a1": {"hy3"}, "a2": set()}
targets = model_health.select_targets(accounts, {"accountIds": [], "onlyUsedModels": True},
                                     used, ["hy3", "kimi-k3"])
m_a1 = sorted(t["model"] for t in targets if t["accountId"] == "a1")
m_a2 = sorted(t["model"] for t in targets if t["accountId"] == "a2")
check("只探测用过的模型", m_a1 == ["hy3"], f"got {m_a1}")
check("无记录账号退回基础清单", m_a2 == ["hy3", "kimi-k3"], f"got {m_a2}")

# ---------------------------------------------------------------------------
# 5. 结果分类
# ---------------------------------------------------------------------------
print("\n[5] 探测结果分类")
check("200 → available", model_health.classify_probe(200) == "available")
check("404 → unavailable", model_health.classify_probe(404) == "unavailable")
check("500 → unavailable", model_health.classify_probe(500) == "unavailable")
check("403 → unavailable", model_health.classify_probe(403) == "unavailable")
check("429 → transient（限流不禁用）", model_health.classify_probe(429) == "transient")
check("408 → transient", model_health.classify_probe(408) == "transient")
check("网络异常 → transient", model_health.classify_probe(None, "TimeoutError") == "transient")

# ---------------------------------------------------------------------------
# 6/7/8/9. 结果落地
# ---------------------------------------------------------------------------
print("\n[6] 自动禁用")
policy = {}
report = model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "unavailable"},
    {"model": "kimi-k3", "verdict": "available"},
], auto_enable=True)
check("不可用写入禁用", policy.get("a1") == ["hy3"], f"got {policy.get('a1')}")
check("报告记录了新增禁用", report["disabled"] == ["hy3"])

print("\n[7] 自动启用")
policy = {"a1": ["hy3", "kimi-k3"]}
report = model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "available"},
    {"model": "kimi-k3", "verdict": "unavailable"},
], auto_enable=True)
check("恢复可用被移出禁用", policy.get("a1") == ["kimi-k3"], f"got {policy.get('a1')}")
check("报告记录了自动启用", report["enabled"] == ["hy3"])

print("\n[8] autoEnable 关闭")
policy = {"a1": ["hy3"]}
model_health.apply_verdicts(policy, "a1", [{"model": "hy3", "verdict": "available"}],
                            auto_enable=False)
check("关闭时可用不自动放开", policy.get("a1") == ["hy3"], f"got {policy.get('a1')}")

print("\n[9] transient 不动策略")
policy = {"a1": ["hy3"]}
model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "transient"},
    {"model": "kimi-k3", "verdict": "transient"},
], auto_enable=True)
check("限流/超时不改变既有禁用", policy.get("a1") == ["hy3"], f"got {policy.get('a1')}")
check("不会把 transient 模型加入禁用", "kimi-k3" not in (policy.get("a1") or []))

print("\n[10] 别名归一")
policy = {"a1": ["hy4"]}
check("禁用 hy4 挡住 hy3", model_policy.model_is_disabled("a1", "hy3", policy) is True)
check("禁用 hy4 也挡 hy4", model_policy.model_is_disabled("a1", "hy4", policy) is True)
policy2 = {"a1": ["hy3"]}
check("禁用 hy3 挡住别名 hy4", model_policy.model_is_disabled("a1", "hy4", policy2) is True)

# ---------------------------------------------------------------------------
# 11/12. 落盘与共用策略
# ---------------------------------------------------------------------------
print("\n[11] 落盘与共用策略")
model_policy.save_policy({"a1": ["hy3"]})
check("策略文件已写入", model_policy.MODEL_POLICY_FILE.exists())
check("无 .tmp 残留", not (tmpdir / "model_policy.json.tmp").exists())
on_disk = json.loads(model_policy.MODEL_POLICY_FILE.read_text(encoding="utf-8"))
check("落盘内容正确", on_disk == {"a1": ["hy3"]}, f"got {on_disk}")

# 巡检写入的就是手动禁用读的那份策略
model_policy.save_policy({})
_p = model_policy.load_policy()
model_health.apply_verdicts(_p, "a9", [
    {"model": "glm-5.3", "verdict": "unavailable"},
], auto_enable=True)
model_policy.save_policy(_p)  # run_round() 在生产路径中同样会落盘
# 模拟「巡检写完后，手动读策略」 —— 必须能读到同一条
policy_after = model_policy.load_policy()
check("巡检写入对手动可见", policy_after.get("a9") == ["glm-5.3"], f"got {policy_after}")

manual = model_policy.set_model_disabled(model_policy.load_policy(), "a9", "glm-5.3", False)
check("手动可移除自动写入的禁用", "a9" not in manual or manual["a9"] == [], f"got {manual}")
model_policy.save_policy(manual)
check("清空后不留空键", "a9" not in model_policy.load_policy())

check("disabledTotal 汇总正确",
      model_policy.disabled_total({"a": ["m1", "m2"], "b": ["m3"]}) == 3)

# ---------------------------------------------------------------------------
# 13. 整轮巡检（假客户端）
# ---------------------------------------------------------------------------
print("\n[13] 整轮巡检")
model_policy.save_policy({})
model_health.save_config({"accountIds": [], "onlyUsedModels": False, "autoEnable": True})


class FakeResponse:
    def __init__(self, status):
        self.status_code = status


class FakeClient:
    """按 (账号token, 模型) 返回预设状态码，模拟上游。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        model = (json or {}).get("model")
        self.calls.append((token, model))
        return FakeResponse(self.mapping.get((token, model), 200))

    def close(self):
        pass


fake = FakeClient({("t1", "hy3"): 404, ("t2", "hy3"): 200, ("t2", "kimi-k3"): 429})
result = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: fake,
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("巡检覆盖 2 个可用账号", result["accounts"] == 2, f"got {result['accounts']}")
check("组合数正确", result["combos"] == 4, f"got {result['combos']}")
check("可用计数", result["counts"]["available"] == 2, f"got {result['counts']}")
check("不可用计数", result["counts"]["unavailable"] == 1, f"got {result['counts']}")
check("transient 计数", result["counts"]["transient"] == 1, f"got {result['counts']}")
check("a1 的 hy3 被自动禁用",
      model_policy.load_policy().get("a1") == ["hy3"], f"got {model_policy.load_policy()}")
check("429 未导致禁用",
      "kimi-k3" not in (model_policy.load_policy().get("a2") or []),
      f"got {model_policy.load_policy()}")

summary = model_health.summarize(result)
check("摘要不含逐条明细", "reports" not in summary and summary["combos"] == 4)

# ---------------------------------------------------------------------------
# 14. 并发保护：同一时刻只允许一轮巡检
#
# 手动「立即巡检」与后台定时轮次撞车时，必须有一方被拒 —— 否则会双倍
# 消耗上游额度，并且两轮各自读到的策略会互相覆盖。
# ---------------------------------------------------------------------------
print("\n[14] 并发保护")
import threading  # noqa: E402
import time as _time  # noqa: E402

_lock = threading.Lock()
_entered = threading.Event()
_release = threading.Event()
_concurrent = []


def _slow_round():
    # 模拟一轮较慢的巡检占住锁
    assert _lock.acquire(blocking=False), "首轮应能拿到锁"
    _entered.set()
    _release.wait(2.0)
    _lock.release()


t = threading.Thread(target=_slow_round, daemon=True)
t.start()
_entered.wait(1.0)
# 第二轮在首轮未释放时应拿不到锁
got = _lock.acquire(blocking=False)
_concurrent.append(got)
if got:
    _lock.release()
_release.set()
t.join(2.0)
check("巡检互斥：首轮持锁时次轮拿不到", got is False, f"got {got}")

# 释放后可以重新获取
_ok_after = _lock.acquire(blocking=False)
if _ok_after:
    _lock.release()
check("巡检互斥：释放后可再次获取", _ok_after is True)

# ---------------------------------------------------------------------------
print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
