#!/usr/bin/env python3
"""模型可用性自动巡检的本地单测。

不依赖 fastapi / httpx —— 用假客户端替换真实 HTTP，直接验证 model_health 的决策逻辑。
覆盖：
  1. 默认关闭（自动禁用会改行为，不能默认开）
  2. 配置读写回环 + 间隔下限钳制 + 损坏回退
  3. 账号范围：空数组 = 全部；显式列表 = 白名单；过期账号被排除
  4. onlyUsedModels：只探测用过的；无记录时退回基础清单
  5. **探测请求体形态**：流式 + 首条为 system + 不带 max_tokens（上游有最小值校验）
  6. 探测结果分类：429/超时 = transient；404/500 = unavailable；
     参数被上游校验拒绝 = probe_defect（与模型可用性无关，绝不写策略）
  7. 不可用 → 自动写入禁用，并标记来源 auto
  8. 可用 → 自动移除**仅限 auto 项**
  9. 手动禁用（manual）受保护：探测可用也不会被放开
 10. autoEnable 关闭时，auto 项也不自动放开
 11. transient 不改变策略（否则高峰期会把好模型全禁掉）
 12. 别名归一：禁用 hy4 连带挡住 hy3
 13. v1（数组）策略兼容：一律读作手动禁用
 14. 落盘无 .tmp 残留；落盘内容为 v2 结构
 15. 与手动禁用共用同一份策略（关掉巡检，禁用项仍在）
 16. 整轮误判保护：全部不可用时不写策略
 17. 探测被上游参数校验拒绝：逐条跳过；全部被拒时整轮作废
 18. 账号凭据整体失效：整账号跳过，不写禁用（坏的是凭据不是模型）
 19. 生产代码的重入锁与「不阻塞事件循环」、问题提示文案
 20. 上次巡检摘要落盘与回读
 21. 巡检改动前自动留底
"""
import json
import os
import sys
import tempfile
import types
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
# 5. 探测请求体形态
#
# 这是**最容易致命**的一环：上游只接受流式请求，且要求首条消息是 system prompt
# （非流式回 400 `Non-stream chat request is currently not supported`，
#   首条非 system 回 400 `first message is not system prompt`）。
# 一旦形态不对，整轮探测会把所有组合判成「不可用」，进而成批误禁好模型。
# ---------------------------------------------------------------------------
print("\n[5] 探测请求体形态")
body = model_health.build_probe_body("hy3")
check("必须是流式请求（上游拒绝非流式）", body["stream"] is True, f"got {body['stream']}")
check("首条消息是 system", (body.get("messages") or [{}])[0].get("role") == "system",
      f"got {body.get('messages')}")
check("带有 user 消息", len(body["messages"]) >= 2 and body["messages"][1].get("role") == "user")
# 上游对 max_tokens 有最小值校验，且最小值随模型系列变化（曾回 400
# integer_below_min_value，让整批模型被误判为不可用）。
# 因此探测请求体里**不能**出现这个字段 —— 不传才与普通客户端请求一致。
check("不携带 max_tokens（上游有最小值校验，会误判为模型不可用）",
      "max_tokens" not in body, f"got {body.get('max_tokens')}")
check("只带必需字段", sorted(body) == ["messages", "model", "stream"], f"got {sorted(body)}")

_sent = {}


class _CapturingClient:
    def post(self, url, json=None, headers=None, timeout=None):
        _sent["url"] = url
        _sent["body"] = json or {}
        _sent["headers"] = headers or {}
        return types.SimpleNamespace(status_code=200)

    def close(self):
        pass


model_health.probe_once(_CapturingClient(), "https://example.test", "tok", "hy3")
check("探测实际发出的 stream 为 True", (_sent.get("body") or {}).get("stream") is True,
      f"got {(_sent.get('body') or {}).get('stream')}")
check("探测实际首条为 system",
      ((_sent.get("body") or {}).get("messages") or [{}])[0].get("role") == "system")
check("拼接的是上游 chat/completions 路径",
      str(_sent.get("url", "")).endswith("/chat/completions"), f"got {_sent.get('url')}")
check("带上账号 Bearer 凭据",
      "Bearer tok" in str((_sent.get("headers") or {}).get("Authorization", "")))


class _StreamingResponse:
    """模拟流式响应：状态码 + 若干帧。"""

    def __init__(self, status, lines):
        self.status_code = status
        self._lines = lines
        self.read_calls = 0

    def iter_lines(self):
        for line in self._lines:
            yield line

    def read(self):
        self.read_calls += 1
        return b"".join(x.encode("utf-8") for x in self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StreamingClient:
    """支持 stream() 的假客户端，记录实际读了多少帧。"""

    def __init__(self, status, lines=("data: {\"choices\":[]}\n",)):
        self.status = status
        self.lines = lines
        self.responses = []
        self.saw_body = None

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.saw_body = json or {}
        res = _StreamingResponse(self.status, list(self.lines))
        self.responses.append(res)
        return res

    def close(self):
        pass


_stream_client = _StreamingClient(200)
item = model_health.probe_once(_stream_client, "https://example.test", "tok", "hy3")
check("流式客户端下同样判定为 available", item["verdict"] == "available", f"got {item}")
check("流式路径也发出 stream=True",
      (_stream_client.saw_body or {}).get("stream") is True)
check("流式路径不携带 max_tokens", "max_tokens" not in (_stream_client.saw_body or {}))
check("成功时只读第一帧就断开（省额度）",
      _stream_client.responses and _stream_client.responses[0].read_calls == 0)

_err_client = _StreamingClient(400, ['{"code":11102,"msg":"model not found"}'])
item = model_health.probe_once(_err_client, "https://example.test", "tok", "hy3")
check("失败时读出错误体以便诊断", _err_client.responses[0].read_calls == 1)
check("解析出上游错误码", item["code"] == 11102, f"got {item.get('code')}")
check("模型不存在 → unavailable", item["verdict"] == "unavailable", f"got {item}")

# ---------------------------------------------------------------------------
# 6. 结果分类
# ---------------------------------------------------------------------------
print("\n[6] 探测结果分类")
check("200 → available", model_health.classify_probe(200) == "available")
check("404 → unavailable", model_health.classify_probe(404) == "unavailable")
check("500 → unavailable", model_health.classify_probe(500) == "unavailable")
check("403 → unavailable", model_health.classify_probe(403) == "unavailable")
check("429 → transient（限流不禁用）", model_health.classify_probe(429) == "transient")
check("408 → transient", model_health.classify_probe(408) == "transient")
check("网络异常 → transient", model_health.classify_probe(None, "TimeoutError") == "transient")

# 「请求被上游参数校验拒绝」与模型可用性无关，不能算模型坏。
# 这是上一版最致命的一处：min 值校验把整批好模型判成了不可用。
check("上游参数校验码 → probe_defect",
      model_health.classify_probe(400, None, 11133, "") == "probe_defect")
check("响应体里的取值越界提示 → probe_defect",
      model_health.classify_probe(
          400, None, None, '{"extError":{"code":"integer_below_min_value"}}') == "probe_defect")
check("probe_defect 优先于 unavailable 判定（同为 400）",
      model_health.classify_probe(400, None, 11133, "") != "unavailable")
check("模型不存在的错误码仍判 unavailable",
      model_health.classify_probe(400, None, 11102, '{"msg":"model not found"}') == "unavailable")
check("2xx 优先于 probe_defect：成功就是成功",
      model_health.classify_probe(200, None, 11133, "") == "available")

# ---------------------------------------------------------------------------
# 7. 自动禁用 + 来源标记
# ---------------------------------------------------------------------------
print("\n[7] 自动禁用与来源标记")
policy = {}
report = model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "unavailable"},
    {"model": "kimi-k3", "verdict": "available"},
], auto_enable=True)
check("不可用写入禁用", sorted(policy.get("a1") or {}) == ["hy3"], f"got {policy.get('a1')}")
check("来源标记为 auto", model_policy.source_of(policy, "a1", "hy3") == "auto",
      f"got {model_policy.source_of(policy, 'a1', 'hy3')}")
check("可用且未禁用的模型不写入", "kimi-k3" not in (policy.get("a1") or {}))
check("报告记录了新增禁用", report["disabled"] == ["hy3"])

# 已禁用的 auto 项再判不可用：不重复计为变更
again = model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "unavailable"},
], auto_enable=True)
check("重复判不可用不产生新变更", again["disabled"] == [] and again["changed"] is False)

# ---------------------------------------------------------------------------
# 8. 自动启用只动 auto 项
# ---------------------------------------------------------------------------
print("\n[8] 自动启用（只放开 auto 项）")
policy = {"a1": {"hy3": "auto", "kimi-k3": "auto"}}
report = model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "available"},
    {"model": "kimi-k3", "verdict": "unavailable"},
], auto_enable=True)
check("auto 项恢复可用后被放开", "hy3" not in (policy.get("a1") or {}), f"got {policy.get('a1')}")
check("报告记录了自动启用", report["enabled"] == ["hy3"])
check("被重新判不可用的 auto 项仍在", model_policy.source_of(policy, "a1", "kimi-k3") == "auto")

# ---------------------------------------------------------------------------
# 9. 手动禁用受保护（本版的核心）
# ---------------------------------------------------------------------------
print("\n[9] 手动禁用受保护")
policy = {"a1": {"hy3": "manual", "kimi-k3": "auto"}}
report = model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "available"},     # 明明探测通了
    {"model": "kimi-k3", "verdict": "available"},
], auto_enable=True)
check("探测可用也不放手动禁用的模型", "hy3" in (policy.get("a1") or {}), f"got {policy.get('a1')}")
check("手动项来源未被改写", model_policy.source_of(policy, "a1", "hy3") == "manual")
check("同一轮里 auto 项照常放开", "kimi-k3" not in (policy.get("a1") or {}))
check("报告列出被保护的手动项", report["protected"] == ["hy3"], f"got {report['protected']}")

# 手动禁用的模型真被上游下线时，来源保持 manual（不降级为可自愈的 auto）
policy = {"a1": {"hy3": "manual"}}
model_health.apply_verdicts(policy, "a1", [{"model": "hy3", "verdict": "unavailable"}])
check("手动项不会因探测不可用而变成 auto",
      model_policy.source_of(policy, "a1", "hy3") == "manual")

# 用户手动把 auto 项也点一遍：显式操作应升级为 manual（由接口层写入，这里验证语义）
pol = {"a1": {"hy3": "auto"}}
model_policy.set_model_disabled(pol, "a1", "hy3", True)
check("手动禁用升级来源为 manual", model_policy.source_of(pol, "a1", "hy3") == "manual")

# ---------------------------------------------------------------------------
# 10. autoEnable 关闭
# ---------------------------------------------------------------------------
print("\n[10] autoEnable 关闭")
policy = {"a1": {"hy3": "auto"}}
model_health.apply_verdicts(policy, "a1", [{"model": "hy3", "verdict": "available"}],
                            auto_enable=False)
check("关闭时 auto 项也不自动放开", "hy3" in (policy.get("a1") or {}), f"got {policy.get('a1')}")

# ---------------------------------------------------------------------------
# 11. transient 不动策略
# ---------------------------------------------------------------------------
print("\n[11] transient 不动策略")
policy = {"a1": {"hy3": "manual"}}
model_health.apply_verdicts(policy, "a1", [
    {"model": "hy3", "verdict": "transient"},
    {"model": "kimi-k3", "verdict": "transient"},
], auto_enable=True)
check("限流/超时不改变既有禁用", "hy3" in (policy.get("a1") or {}), f"got {policy.get('a1')}")
check("不会把 transient 模型加入禁用", "kimi-k3" not in (policy.get("a1") or {}))

# ---------------------------------------------------------------------------
# 12. 别名归一
# ---------------------------------------------------------------------------
print("\n[12] 别名归一")
check("禁用 hy4 挡住 hy3", model_policy.model_is_disabled("a1", "hy3", {"a1": {"hy4": "manual"}}))
check("禁用 hy4 也挡 hy4", model_policy.model_is_disabled("a1", "hy4", {"a1": {"hy4": "manual"}}))
check("禁用 hy3 挡住别名 hy4", model_policy.model_is_disabled("a1", "hy4", {"a1": {"hy3": "manual"}}))

# ---------------------------------------------------------------------------
# 13. v1 兼容迁移
# ---------------------------------------------------------------------------
print("\n[13] v1 策略兼容")
model_policy.MODEL_POLICY_FILE.write_text(
    json.dumps({"a1": ["hy3", "kimi-k3"], "a2": []}), encoding="utf-8")
legacy = model_policy.load_policy()
check("v1 数组读为 v2 结构",
      legacy == {"a1": {"hy3": "manual", "kimi-k3": "manual"}}, f"got {legacy}")
check("v1 条目一律视为手动禁用（不被自愈放开）",
      model_policy.source_of(legacy, "a1", "hy3") == "manual")
check("空数组账号不产生空键", "a2" not in legacy)
check("v1 读取不抛异常且不影响 model_is_disabled",
      model_policy.model_is_disabled("a1", "hy3", legacy) is True)

# ---------------------------------------------------------------------------
# 14/15. 落盘与共用策略
# ---------------------------------------------------------------------------
print("\n[14] 落盘")
model_policy.save_policy({"a1": {"hy3": "auto"}})
check("策略文件已写入", model_policy.MODEL_POLICY_FILE.exists())
check("无 .tmp 残留", not (tmpdir / "model_policy.json.tmp").exists())
on_disk = json.loads(model_policy.MODEL_POLICY_FILE.read_text(encoding="utf-8"))
check("落盘带版本号", on_disk.get("version") == 2, f"got {on_disk}")
check("落盘内容为 v2 disabled 结构",
      on_disk.get("disabled") == {"a1": {"hy3": "auto"}}, f"got {on_disk}")
check("兼容传数组写入（按 manual 记）",
      model_policy.as_source_map(model_policy.save_policy({"z": ["m1"]})) == {"z": {"m1": "manual"}})

print("\n[15] 与手动禁用共用同一份策略")
model_policy.save_policy({})
_p = model_policy.load_policy()
model_health.apply_verdicts(_p, "a9", [{"model": "glm-5.3", "verdict": "unavailable"}],
                            auto_enable=True)
model_policy.save_policy(_p)  # run_round() 在生产路径中同样会落盘
policy_after = model_policy.load_policy()
check("巡检写入对手动可见", policy_after.get("a9") == {"glm-5.3": "auto"}, f"got {policy_after}")

model_policy.set_model_disabled(policy_after, "a9", "glm-5.3", False)
model_policy.save_policy(policy_after)
check("手动可移除自动写入的禁用", "a9" not in model_policy.load_policy())

check("按来源统计正确",
      model_policy.count_by_source({"a": {"m": "manual", "n": "auto"}, "b": {"x": "manual"}})
      == {"manual": 2, "auto": 1})
check("disabledTotal 兼容数组入参",
      model_policy.disabled_total({"a": ["m1", "m2"], "b": ["m3"]}) == 3)

# ---------------------------------------------------------------------------
# 16. 整轮巡检（假客户端）
# ---------------------------------------------------------------------------
print("\n[16] 整轮巡检")
model_policy.save_policy({})


class FakeResponse:
    def __init__(self, status):
        self.status_code = status


class FakeClient:
    """按 (账号token, 模型) 返回预设状态码，模拟上游。未命中则用 default。"""

    def __init__(self, mapping, default=200):
        self.mapping = mapping
        self.default = default
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        model = (json or {}).get("model")
        self.calls.append((token, model))
        return FakeResponse(self.mapping.get((token, model), self.default))

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
check("未触发整轮保护", result["aborted"] is False)
check("a1 的 hy3 被自动禁用",
      model_policy.load_policy().get("a1") == {"hy3": "auto"},
      f"got {model_policy.load_policy()}")
check("429 未导致禁用",
      "kimi-k3" not in (model_policy.load_policy().get("a2") or {}),
      f"got {model_policy.load_policy()}")

summary = model_health.summarize(result)
check("摘要不含逐条明细", "reports" not in summary and summary["combos"] == 4)
check("摘要带 aborted 标记", summary["aborted"] is False)

# 摘要会**落盘**，进程重启后界面直接读它回显。凭据失效名单与被拒错误码只存在于
# 那一轮里，重跑补不回来 —— 曾经这里有两份同名 summarize，后一份把这两个字段丢掉，
# Python 只保留最后一份，于是落盘的摘要长期缺字段、重启后提示不再出现。
_summary = model_health.summarize({
    "checkedAt": 1, "combos": 3, "accounts": 1, "skippedManual": 2,
    "counts": {"probe_defect": 1}, "aborted": False,
    "authFailed": [{"id": "a1", "name": "acc-1"}], "defectCodes": [11133],
})
check("摘要只保留一份定义（字段不再被同名函数覆盖丢失）",
      model_health.summarize.__code__.co_argcount == 1,
      f"got {model_health.summarize.__code__.co_argcount}")
check("摘要保留凭据失效名单",
      _summary.get("authFailed") == [{"id": "a1", "name": "acc-1"}], f"got {_summary}")
check("摘要保留被拒错误码", _summary.get("defectCodes") == [11133], f"got {_summary}")
check("摘要带手动禁用跳过数", _summary.get("skippedManual") == 2, f"got {_summary}")

# ---------------------------------------------------------------------------
# 16b. 手动禁用的组合不参与探测
#
# 手动禁用是人的明确决定，探测它得不到任何有用的结论，只会白花上游额度，
# 并让「手动禁用的 N 项」与「巡检禁用的 M 项」在界面上混成一锅。
# auto 项**必须**继续探测 —— 自愈正是靠这轮探测发现模型恢复可用。
# ---------------------------------------------------------------------------
print("\n[16b] 手动禁用不参与探测")
model_policy.save_policy({"a1": {"hy3": "manual"}, "a2": {"kimi-k3": "auto"}})
skip_client = FakeClient({("t1", "hy3"): 404, ("t2", "kimi-k3"): 200})
skip_round = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: skip_client,
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
probed = set(skip_client.calls)
check("手动禁用的组合根本没被探测", ("t1", "hy3") not in probed, f"got {sorted(probed)}")
check("auto 项照常探测（自愈靠它）", ("t2", "kimi-k3") in probed, f"got {sorted(probed)}")
check("报告里说明跳过了多少组合", skip_round.get("skippedManual") == 1,
      f"got {skip_round.get('skippedManual')}")
check("跳过的手动项来源与状态都不动",
      model_policy.source_of(model_policy.load_policy(), "a1", "hy3") == "manual",
      f"got {model_policy.load_policy()}")
check("auto 项照常按探测结果自愈",
      "kimi-k3" not in (model_policy.load_policy().get("a2") or {}),
      f"got {model_policy.load_policy()}")
check("跳过数不计入任何探测计数",
      sum(int(v) for v in skip_round["counts"].values()) == skip_round["combos"],
      f"got {skip_round['counts']} combos={skip_round['combos']}")

# ---------------------------------------------------------------------------
# 17. 整轮误判保护
#
# 一个可用的都没有，几乎可以肯定是探测机制失效（凭据 / 请求形态 / 上游整体故障），
# 而不是所有模型同时坏掉。此时必须整轮作废，不能把好模型成批误禁。
# ---------------------------------------------------------------------------
print("\n[17] 整轮误判保护")
# 组合数要够到保护门槛（2 个可用账号 × 3 个模型 = 6 ≥ GUARD_MIN_UNAVAILABLE）
guard_models = ["hy3", "kimi-k3", "glm-5.3"]
model_policy.save_policy({"a1": {"hy3": "manual"}})
before = model_policy.load_policy()
all_bad = FakeClient({}, default=400)
aborted = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: all_bad,
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=guard_models,
)
check("组合数达到保护门槛", aborted["combos"] >= model_health.GUARD_MIN_UNAVAILABLE,
      f"got {aborted['combos']}")
check("全部不可用时判定为整轮作废", aborted["aborted"] is True, f"got {aborted['counts']}")
check("作废时给出原因", bool(aborted.get("reason")), f"got {aborted.get('reason')}")
check("作废时不写入任何禁用", model_policy.load_policy() == before,
      f"got {model_policy.load_policy()}")
check("作废时报告不谎报变更",
      all(not r["disabled"] and not r["enabled"] for r in aborted["reports"]))

# 边界：只要有 1 个可用就不算作废。
# 注意可用的那个必须是**真的会被探测**的组合：a1/hy3 上面被设成了手动禁用，
# 巡检根本不会探测它，拿它当「可用样本」是测不到东西的。
mixed = FakeClient({("t2", "hy3"): 200}, default=400)
notaborted = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: mixed,
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=guard_models,
)
check("有一个可用即正常落盘", notaborted["aborted"] is False
      and notaborted["counts"]["available"] == 1, f"got {notaborted['counts']}")
check("手动禁用的组合被排除在探测之外（组合数 = 总数 - 手动项）",
      notaborted["combos"] == 5 and notaborted["skippedManual"] == 1,
      f"got combos={notaborted['combos']} skipped={notaborted.get('skippedManual')}")

# ---------------------------------------------------------------------------
# 17b. 探测被上游参数校验拒绝（probe_defect）
#
# 与「模型不可用」必须分开：请求参数被上游拒了，说明探测形态不对，
# 我们对模型可用性**一无所知**。这类结果一律不写策略。
# ---------------------------------------------------------------------------
print("\n[17b] 探测请求被上游拒绝")


class FakeDefectResponse:
    """400 + 参数校验错误体的假响应。"""

    def __init__(self):
        self.status_code = 400
        self.text = '{"code":11133,"msg":"request parameters rejected",' \
                    '"extError":{"code":"integer_below_min_value"}}'


class FakeDefectClient(FakeClient):
    """除 ``ok_models`` 里的模型外，一律返回「请求参数被上游拒绝」。"""

    def __init__(self, ok_models=()):
        super().__init__({})
        self.ok_models = set(ok_models)

    def post(self, url, json=None, headers=None, timeout=None):
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        model = (json or {}).get("model")
        self.calls.append((token, model))
        # 一部分组合照常返回 200，用来证明「不是整轮作废，而是逐条跳过」
        if model in self.ok_models:
            return FakeResponse(200)
        return FakeDefectResponse()


model_policy.save_policy({"a1": {"hy3": "manual"}})
before_defect = model_policy.load_policy()
defect_round = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeDefectClient(ok_models=["kimi-k3"]),
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=guard_models,
)
check("参数被拒计入 probe_defect", defect_round["counts"]["probe_defect"] > 0,
      f"got {defect_round['counts']}")
check("参数被拒不计入 unavailable", defect_round["counts"]["unavailable"] == 0,
      f"got {defect_round['counts']}")
check("参数被拒时不写任何禁用", model_policy.load_policy() == before_defect,
      f"got {model_policy.load_policy()}")
check("报告里也没有谎报变更",
      all(not r["disabled"] for r in defect_round["reports"]))
check("记录被拒的错误码用于排查", defect_round["defectCodes"] == [11133],
      f"got {defect_round.get('defectCodes')}")
check("有组合可用时不整轮作废", defect_round["aborted"] is False)

# 全部组合都被参数拒绝：整轮作废，并且原因要说清是「探测形态」而不是「模型坏了」
all_defect = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeDefectClient(),
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=guard_models,
)
check("全部被拒时整轮作废", all_defect["aborted"] is True, f"got {all_defect['counts']}")
check("作废原因点明参数校验被拒",
      "参数校验" in (all_defect.get("reason") or "") and "11133" in (all_defect.get("reason") or ""),
      f"got {all_defect.get('reason')}")
# ---------------------------------------------------------------------------
# 17c. 账号凭据整体失效
#
# token 过期会让一个账号名下所有模型一起 401 —— 坏的是凭据不是模型。
# 若按「不可用」处理，一次过期就会自动禁掉整个账号的模型清单。
# ---------------------------------------------------------------------------
print("\n[17c] 账号凭据失效")
model_policy.save_policy({})
# a1 凭据整体失效（全 401），a2 一切正常。若把 a1 的 401 当「模型不可用」，
# 就会一次性自动禁用掉 a1 名下的整个模型清单 —— 这正是要避免的。
auth_round = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient({("t1", "hy3"): 401, ("t1", "kimi-k3"): 401,
                                       ("t1", "glm-5.3"): 401}),
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=guard_models,
)
check("凭据失效的账号被点名",
      [a["id"] for a in (auth_round.get("authFailed") or [])] == ["a1"],
      f"got {auth_round.get('authFailed')}")
check("凭据失效不写禁用", model_policy.load_policy() == {},
      f"got {model_policy.load_policy()}")
check("凭据失效的账号报告里没有变更",
      all(not r["disabled"] and not r["enabled"] for r in auth_round["reports"]))
check("凭据失效的账号带 authFailed 标记",
      [r["accountId"] for r in auth_round["reports"] if r.get("authFailed")] == ["a1"])
check("健康账号照常参与且不误判", auth_round["aborted"] is False
      and auth_round["counts"]["available"] == len(guard_models), f"got {auth_round['counts']}")

# 只有一个模型、无法区分「凭据坏了」还是「这个模型坏了」时，按普通规则处理
single = model_health.run_round(
    accounts=[accounts[0]],
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient({("t1", "hy3"): 401, ("t1", "kimi-k3"): 200}),
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("有可用模型时不误判为凭据失效", not (single.get("authFailed") or []),
      f"got {single.get('authFailed')}")
check("混合状态下 401 的模型照常禁用",
      (model_policy.load_policy().get("a1") or {}).get("hy3") == "auto",
      f"got {model_policy.load_policy()}")

# ---------------------------------------------------------------------------
# 18. 上次巡检摘要落盘与回读
# ---------------------------------------------------------------------------
print("\n[18] 巡检摘要落盘")
model_health.save_last_run({"checkedAt": 123, "combos": 7, "aborted": False})
check("摘要可回读", (model_health.load_last_run() or {}).get("combos") == 7)
model_health.LAST_RUN_FILE.write_text("{ broken", encoding="utf-8")
check("摘要损坏时返回 None 且不抛", model_health.load_last_run() is None)

# ---------------------------------------------------------------------------
# 18b. 巡检改动前自动留底
#
# 一次巡检可能同时改动上百个条目，判定万一有误，用户需要的是整份回退。
# ---------------------------------------------------------------------------
print("\n[18b] 巡检改动前留底")
model_policy.save_policy({"a1": {"hy3": "manual"}})
backup_path = tmpdir / f"model_policy.json.{model_health.BACKUP_SUFFIX}"
if backup_path.exists():
    backup_path.unlink()
changed = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient({("t1", "kimi-k3"): 404}, default=200),
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("本轮确实产生了改动", bool(changed["disabled"]), f"got {changed['disabled']}")
check("改动前留下备份文件", backup_path.exists())
check("备份内容 = 改动前的策略",
      json.loads(backup_path.read_text(encoding="utf-8")).get("disabled")
      == {"a1": {"hy3": "manual"}},
      f"got {backup_path.read_text(encoding='utf-8')}")

# 无改动的一轮不应覆盖已有备份
before_text = backup_path.read_text(encoding="utf-8")
model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient({}, default=200),
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=["hy3"],
)
check("无改动时不覆盖备份", backup_path.read_text(encoding="utf-8") == before_text)

# 作废的一轮同样不该覆盖备份
model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient({}, default=400),
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=guard_models,
)
check("整轮作废时也不动备份", backup_path.read_text(encoding="utf-8") == before_text)

# ---------------------------------------------------------------------------
# 19. 生产代码：重入锁与不阻塞事件循环
#
# 直接从 gateway/main.py 切出 run_model_health_check 的源码执行，
# 避免为了一个断言把整个 FastAPI 应用拉起来。
# ---------------------------------------------------------------------------
print("\n[19] 生产代码的重入保护")
main_src = (Path(__file__).parent / "gateway" / "main.py").read_text(encoding="utf-8")

# run_model_health_check() 会调用同模块的 _health_problem_note()，
# 切片执行时必须把这个依赖一起注入 —— 注入真实实现（而非 lambda 桩），
# 顺带把它的分支也覆盖掉。
note_seg = main_src[main_src.index("def _health_problem_note("):
                    main_src.index("def health_config_snapshot(")]
seg = main_src[main_src.index("def run_model_health_check("):
               main_src.index("async def model_health_loop(")]
ns = {
    "json": json, "os": os, "print": print, "Path": Path,
    "Any": __import__("typing").Any,
    "Dict": __import__("typing").Dict,
    "List": __import__("typing").List,
    "Optional": __import__("typing").Optional,
    "threading": __import__("threading"),
    "httpx": types.SimpleNamespace(Client=lambda **k: None),
    "model_health": model_health,
    "model_policy": model_policy,
    "_HEALTH_RUNTIME": {"running": False, "lastRunAt": None, "lastResult": None, "lastError": None},
    "_HEALTH_RUN_LOCK": __import__("threading").Lock(),
    "_load_accounts": lambda: [],
    "_health_base_url": lambda variant: "https://example.test",
    "_used_models_by_account": lambda: {},
    "_base_model_ids": lambda: [],
}
exec(compile(note_seg, "health_problem_note_fn", "exec"), ns)
exec(compile(seg, "health_check_fn", "exec"), ns)
run_check = ns["run_model_health_check"]
problem_note = ns["_health_problem_note"]

# 「本轮结果不可信」的原因必须说清楚，否则界面上看起来跟「什么都没变」一样。
check("一切正常时没有问题提示",
      problem_note({"aborted": False, "counts": {"available": 5, "unavailable": 1}}) is None)
check("整轮作废时回传作废原因",
      problem_note({"aborted": True, "reason": "R", "counts": {}}) == "R")
check("探测被上游拒绝时给出提示",
      "11133" in (problem_note({
          "aborted": False,
          "counts": {"probe_defect": 3},
          "defectCodes": [11133],
      }) or ""))
check("凭据失效时提示重新登录",
      "凭据" in (problem_note({
          "aborted": False,
          "counts": {"probe_defect": 0},
          "authFailed": [{"id": "a1", "name": "acc-1"}],
      }) or ""))

import threading  # noqa: E402


class _BlockingRound:
    """首轮进入后卡住，用来复现「手动触发」与「定时轮次」撞车。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **kwargs):
        self.entered.set()
        self.release.wait(5.0)
        return {"checkedAt": 1, "combos": 0, "accounts": 0,
                "counts": {"available": 0, "unavailable": 0, "transient": 0},
                "disabled": [], "enabled": [], "protected": [], "reports": [],
                "aborted": False}


blocking = _BlockingRound()
original_round = model_health.run_round
model_health.run_round = blocking
try:
    first = {}
    t = threading.Thread(target=lambda: first.update(run_check()), daemon=True)
    t.start()
    blocking.entered.wait(3.0)
    second = run_check()
    blocking.release.set()
    t.join(5.0)
finally:
    model_health.run_round = original_round

check("首轮正常完成", "error" not in first, f"got {first}")
check("撞车时次轮被拒", second.get("skipped") is True, f"got {second}")
check("被拒时给出可读原因", "巡检" in str(second.get("error") or ""), f"got {second.get('error')}")
check("释放后可再次获取锁", run_check().get("skipped") is None)

check("定时轮次丢到线程执行、不阻塞事件循环",
      "asyncio.to_thread(run_model_health_check)" in main_src)

# ---------------------------------------------------------------------------
print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
