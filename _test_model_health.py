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
import account_policy  # noqa: E402

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
    """假响应。``text`` 用于测「靠响应体文案识别错误类别」的分支 ——
    内容审查（11140）之类经常没有可用的错误码，只能看文案。"""

    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text or ""


class FakeClient:
    """按 (账号token, 模型) 返回预设状态码，模拟上游。未命中则用 default。

    ``account_default`` 是**账号级探测**（不带真实模型名的那种）的默认响应。
    两者必须能分开预设：账号探测故意用一个不存在的模型名，
    如果混进模型映射里，就分不清「这条是探凭据」还是「探某个真模型」了。

    ``account_codes`` 用于按 token 单独指定账号级探测的状态码
    （例如让 t1 的凭据探测回 401，而它的模型探测回别的）。
    账号级探测是凭据结论的**唯一**来源，所以测试必须能独立控制它 ——
    否则「凭据失效」这个场景根本造不出来。
    """

    def __init__(self, mapping, default=200, account_default=None,
                 account_codes=None, account_bodies=None, bodies=None):
        self.mapping = mapping
        self.default = default
        self.account_default = account_default if account_default is not None else default
        self.account_codes = account_codes or {}
        self.account_bodies = account_bodies or {}
        # (token, model) → 响应体。用于测那些**必须看文案**才能分类的响应
        # （内容审查经常没有可用的错误码，只有一句 safety review）。
        self.bodies = bodies or {}
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        model = (json or {}).get("model")
        self.calls.append((token, model))
        if str(model).startswith("__wb_probe"):
            # 账号级探测：按 token 单独预设，未预设时用 account_default。
            status = self.account_codes.get(token, self.account_default)
            body = self.account_bodies.get(token)
            return FakeResponse(status, body)
        return FakeResponse(self.mapping.get((token, model), self.default),
                            self.bodies.get((token, model)))

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
# 16c. 巡检查到一半时用户点下的手动禁用不能被覆盖
#
# 一轮巡检要跑几分钟（几百次探测）。早期实现是「开场读入策略 → 结尾整份写回」，
# 于是这几分钟里用户在卡片上点下的手动禁用会被那份旧快照**静默抹掉** ——
# 实测中真的丢过一次（巡检 10:57 开始，10:58 点下的禁用到巡检结束时不见了）。
# 现在落盘前重新读盘，只把本轮的判定叠加到最新策略上。
# ---------------------------------------------------------------------------
print("\n[16c] 巡检期间的并发手动禁用不被覆盖")
model_policy.save_policy({"a1": {"hy3": "auto"}})


class _ConcurrentToggleClient(FakeClient):
    """第一次探测时往策略文件里写一条手动禁用，模拟用户在巡检期间点卡片。"""

    def __init__(self):
        super().__init__({("t1", "hy3"): 404}, default=200)
        self.toggled = False

    def post(self, url, json=None, headers=None, timeout=None):
        if not self.toggled:
            self.toggled = True
            pol = model_policy.load_policy()
            model_policy.set_model_disabled(pol, "a1", "kimi-k3", True)
            model_policy.save_policy(pol)
        return super().post(url, json=json, headers=headers, timeout=timeout)


concurrent = _ConcurrentToggleClient()
model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: concurrent,
    base_url_for=lambda variant: "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
after_race = model_policy.load_policy()
check("巡检期间点下的手动禁用活了下来",
      model_policy.source_of(after_race, "a1", "kimi-k3") == "manual", f"got {after_race}")
check("巡检期间点下的手动禁用没被降级成 auto",
      model_policy.source_of(after_race, "a1", "kimi-k3") != "auto", f"got {after_race}")
check("同一轮里探测到的不可用照常写入",
      model_policy.source_of(after_race, "a1", "hy3") == "auto", f"got {after_race}")

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
# a1 凭据整体失效（账号级探测回 401），a2 一切正常。
#
# 关键变化：凭据结论**只由账号级探测裁定**，不再从模型级结果反推。
# 这里刻意让 a1 的模型探测也回 401，用来验证「模型级失败不再被当成凭据问题」——
# 若哪次改动把这条推断加回来，下面「凭据失效不写禁用」会立刻失败。
auth_round = model_health.run_round(
    accounts=accounts,
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient(
        {("t1", "hy3"): 401, ("t1", "kimi-k3"): 401, ("t1", "glm-5.3"): 401},
        account_codes={"t1": 401},
    ),
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
# 凭据状态单独成字段，界面只认它，不必再去模型结果里猜。
_a1_cred = next(r["credential"] for r in auth_round["reports"] if r["accountId"] == "a1")
check("凭据失效在 credential 里标为 invalid",
      _a1_cred["state"] == "invalid", f"got {_a1_cred}")

# 模型级失败**不**反推凭据失效：账号级探测说有效，就按有效处理，
# 那些失败只在该模型维度上生效（该禁则禁）。
model_policy.save_policy({})
mixed = model_health.run_round(
    accounts=[accounts[0]],
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient(
        {("t1", "hy3"): 403, ("t1", "kimi-k3"): 200},
        account_codes={"t1": 400},
    ),
    base_url_for=lambda variant="ai": "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("模型级 403 不再反推为凭据失效", not (mixed.get("authFailed") or []),
      f"got {mixed.get('authFailed')}")
check("凭据有效时模型失败照常按模型维度处理",
      (model_policy.load_policy().get("a1") or {}).get("hy3") == "auto",
      f"got {model_policy.load_policy()}")

# 内容审查（11140）：凭据有效但请求被拦，既不算凭据失效，也不禁用模型。
model_policy.save_policy({})
restricted_round = model_health.run_round(
    accounts=[accounts[0]],
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient(
        {("t1", "hy3"): 403, ("t1", "kimi-k3"): 403},
        account_codes={"t1": 403},
        account_bodies={"t1": '{"code":11140,"msg":"request illegal"}'},
        bodies={("t1", "hy3"): '{"code":11140,"msg":"request illegal"}',
                ("t1", "kimi-k3"): '{"code":11140,"msg":"request illegal"}'},
    ),
    base_url_for=lambda variant="ai": "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("内容审查不算凭据失效", not (restricted_round.get("authFailed") or []),
      f"got {restricted_round.get('authFailed')}")
_r_cred = restricted_round["reports"][0]["credential"]
check("内容审查标为 restricted 且凭据有效",
      _r_cred["state"] == "restricted", f"got {_r_cred}")
check("内容审查被单独点名", len(restricted_round.get("restricted") or []) == 1,
      f"got {restricted_round.get('restricted')}")
check("内容审查计入 counts.restricted",
      restricted_round["counts"]["restricted"] > 0, f"got {restricted_round['counts']}")
check("内容审查不写禁用（凭据没问题）", model_policy.load_policy() == {},
      f"got {model_policy.load_policy()}")
check("内容审查时不提示重新登录",
      "重新登录" not in (_r_cred.get("detail") or ""), f"got {_r_cred.get('detail')}")

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
print("\n[22] 账号级探测：凭据失效与模型失效要分开")
account_policy.save_policy({})
model_policy.save_policy({})


class AccountAwareClient(FakeClient):
    """账号探测（模型名不存在）与模型探测用**不同的**响应。

    ``account_status`` 控制「探凭据」那一条，``mapping`` 控制其余模型探测。
    这样才能验证「先探凭据、探到失效就跳过模型」这条优化真的生效
    （而不是碰巧因为模型探测也失败）。
    """

    def __init__(self, mapping, default=200, account_status=400):
        super().__init__(mapping, default=default)
        self.account_status = account_status

    def post(self, url, json=None, headers=None, timeout=None):
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        model = (json or {}).get("model")
        self.calls.append((token, model))
        if str(model).startswith("__wb_probe"):
            status = self.account_status.get(token, 400) \
                if isinstance(self.account_status, dict) else self.account_status
            return FakeResponse(status)
        return FakeResponse(self.mapping.get((token, model), self.default))


accounts2 = [
    {"id": "a1", "nickname": "Acc1", "access_token": "t1", "variant": "ai"},
    {"id": "a2", "nickname": "Acc2", "access_token": "t2", "variant": "ai"},
]

# 场景一：a2 凭据失效（401），a1 正常。a2 名下的模型**不该**被探测，
# 也不该被写进模型策略 —— 坏的是凭据不是模型。
client2 = AccountAwareClient(
    {("t1", "hy3"): 200, ("t1", "kimi-k3"): 200, ("t2", "hy3"): 401, ("t2", "kimi-k3"): 401},
    account_status={"t1": 400, "t2": 401})
res2 = model_health.run_round(
    accounts=accounts2,
    config={**model_health.default_config(), "checkAccounts": True,
            "autoDisableAccounts": True, "autoEnable": True},
    client_factory=lambda: client2,
    base_url_for=lambda v: "https://example.invalid",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("a2 被识别为凭据失效",
      "a2" in [x["id"] for x in res2["authFailed"]], str(res2["authFailed"]))
# 关键：a2 的模型一次都没被探 —— 这正是「先探凭据」省下来的成本。
probed_a2_models = [m for (tk, m) in client2.calls
                    if tk == "t2" and not str(m).startswith("__wb_probe")]
check("凭据失效账号的模型不再逐个探测", probed_a2_models == [], str(probed_a2_models))
# 也没有一条模型级禁用来自 a2（不能把凭据问题写成模型问题）。
a2_disabled = [m for m in res2["disabled"] if m in ("hy3", "kimi-k3")]
check("模型策略不因凭据失效而被写入", a2_disabled == [], str(a2_disabled))
check("a2 被自动停用（账号级）", "a2" in res2["accountsDisabled"], str(res2["accountsDisabled"]))
check("账号策略里 a2 来源为 auto",
      account_policy.load_policy().get("a2") == "auto", str(account_policy.load_policy()))
check("a1 的可用模型正常判定",
      res2["counts"]["available"] == 2, str(res2["counts"]))

# 场景二：探不到结论（transient，比如上游 5xx）不该停用账号。
account_policy.save_policy({})
client3 = AccountAwareClient({("t1", "hy3"): 200, ("t2", "hy3"): 200},
                             account_status=503)
res3 = model_health.run_round(
    accounts=accounts2,
    config={**model_health.default_config(), "checkAccounts": True,
            "autoDisableAccounts": True},
    client_factory=lambda: client3,
    base_url_for=lambda v: "https://example.invalid",
    used_models={},
    base_models=["hy3"],
)
check("探测未得结论时不自动停用账号",
      res3["accountsDisabled"] == [], str(res3["accountsDisabled"]))
check("探测未得结论时不写账号策略",
      account_policy.load_policy() == {}, str(account_policy.load_policy()))

# 场景三：checkAccounts 关掉后不再发账号探测请求（给用户一个省流量的开关）。
account_policy.save_policy({})
client4 = AccountAwareClient({("t1", "hy3"): 200, ("t2", "hy3"): 200})
res4 = model_health.run_round(
    accounts=accounts2,
    config={**model_health.default_config(), "checkAccounts": False},
    client_factory=lambda: client4,
    base_url_for=lambda v: "https://example.invalid",
    used_models={},
    base_models=["hy3"],
)
probe_calls = [c for c in client4.calls if str(c[1]).startswith("__wb_probe")]
check("关掉 checkAccounts 后不发账号探测", probe_calls == [], str(probe_calls))
check("关掉后账号策略不被写", account_policy.load_policy() == {},
      str(account_policy.load_policy()))

# 场景四：账号级探测的判定函数本身。
check("401 判为凭据失效",
      model_health.classify_account_probe(401, None) == "auth_failed")
# 403 **不再**一律当凭据失效 —— 这是本次统一口径的核心修正。
# 403 至少对应三种原因：token 被吊销、内容审查、无模型权限。
# 只看状态码必然误判，必须看错误码。
check("403 不再一律判为凭据失效（裸 403 交给语义表或兜底逻辑）",
      model_health.classify_account_probe(403, None) != "auth_failed",
      f"got {model_health.classify_account_probe(403, None)}")
check("403 + 11140（内容审查）判为账号受限，而非凭据失效",
      model_health.classify_account_probe(
          403, None, '{"code":11140,"msg":"request illegal"}', 11140) == "restricted")
check("403 + 无错误码但有审查文案，同样判为账号受限",
      model_health.classify_account_probe(
          403, None, '{"msg":"The content did not pass the safety review."}') == "restricted")
check("403 + 11100（token 失效）才是凭据失效",
      model_health.classify_account_probe(
          403, None, '{"code":11100}', 11100) == "auth_failed")
check("401 无论有无错误码都判凭据失效（401 只有这一种含义）",
      model_health.classify_account_probe(401, None, "") == "auth_failed")
# 探测形态错误：非流式请求被上游拒绝（11101）。
# 这类必须判 probe_defect，**不能**当凭据有效 ——
# 那是「探测没到达判定点」，把失效账号读成好账号比误报更危险。
check("11101（非流式不支持）判为探测形态问题，不当作凭据有效",
      model_health.classify_account_probe(
          400, None, '{"code":11101,"msg":"Non-stream chat request is not supported"}',
          11101) == "probe_defect")
check("11102（模型不存在）才判凭据有效",
      model_health.classify_account_probe(
          400, None, '{"code":11102,"msg":"model service info not found"}',
          11102) == "available")
# 关键：400/404 是好消息 —— 上游能说「这个模型不存在」，说明它认下了这个 token。
check("400 判为凭据有效（上游认得出 token）",
      model_health.classify_account_probe(400, None) == "available")
check("404 判为凭据有效",
      model_health.classify_account_probe(404, None) == "available")
check("5xx 不下结论",
      model_health.classify_account_probe(503, None) == "transient")
check("网络异常不下结论",
      model_health.classify_account_probe(None, "ConnectTimeout") == "transient")

# 场景五：行动建议。restricted 与 invalid 都是 403，长得像但处理方式完全相反：
# 一个要重登，一个重登多少次都没用。这里把两者的区别钉死。
check("凭据失效的提示已含「请重新登录」",
      "重新登录" in (model_health.credential_state(
          {"verdict": "auth_failed"}) or {}).get("userMessage", ""),
      str(model_health.credential_state({"verdict": "auth_failed"})))
check("凭据失效不再另给一句重复的建议",
      not (model_health.credential_state(
          {"verdict": "auth_failed"}) or {}).get("action"),
      str(model_health.credential_state({"verdict": "auth_failed"})))
_r_action = (model_health.credential_state(
    {"verdict": "restricted", "semantic": "restricted"}) or {}).get("action", "")
check("账号受限明确说明重新登录没用",
      "重新登录没用" in _r_action, _r_action)
check("凭据有效不给多余建议",
      not model_health.credential_state(
          {"verdict": "available", "semantic": "model_missing"}).get("action"))
check("探测形态错误归为未得出结论",
      model_health.credential_state(
          {"verdict": "probe_defect"}).get("state") == "unknown")

# 场景六：11101「不支持非流式」是探测自身的形态问题，不是「凭据有效」的证据。
# 线上实测就栽在这里：probe_account 曾写死 stream:False，上游回 11101 被
# 当成「模型不存在」而判为 available —— 一个从没测到凭据的假阳性。
check("非流式不支持识别为探测形态问题",
      model_health.classify_semantic(11101) == "probe_defect",
      str(model_health.classify_semantic(11101)))
check("非流式不支持不判为凭据有效",
      model_health.classify_account_probe(
          400, None,
          '{"code":11101,"msg":"Non-stream chat request is currently not supported"}'
      ) == "probe_defect",
      model_health.classify_account_probe(
          400, None,
          '{"code":11101,"msg":"Non-stream chat request is currently not supported"}'))

# 场景七：账号探测必须与真实调用同构（走 stream），否则上游直接拒绝，
# 探测根本到不了判定点。
_acct_src = open("gateway/model_health.py", encoding="utf-8").read()
_probe_acct = _acct_src[_acct_src.index("def probe_account("):
                        _acct_src.index("def credential_state(")]
check("账号探测不再写死非流式（必须与真实调用同构）",
      '"stream": False' not in _probe_acct, "probe_account 里仍有 stream:False")
check("账号探测复用统一的请求构造",
      "build_probe_body" in _probe_acct, "未复用 build_probe_body")

# 场景四点五：探测接口对外只给**结论**，不给原始状态码。
# 探测刻意用一个不存在的模型名，上游回 400/404 正是「认下了 token」的证据；
# 把 400 原样透给界面，会被读成「探测失败了」，与同一条响应里的「凭据有效」自相矛盾。
_probe_fn = main_src[main_src.index("def probe_account_credentials"):
                     main_src.index("@app.post", main_src.index("def probe_account_credentials"))]
check("探测接口不下发原始状态码",
      '"status":' not in _probe_fn and "result.get(\"status\")" not in _probe_fn)
check("探测接口给出判定依据",
      '"evidence"' in _probe_fn)

# 场景五：手动停用不受巡检影响（阳性对照）。
account_policy.save_policy({})
pol5 = account_policy.load_policy()
account_policy.set_disabled(pol5, "a2", True, account_policy.SOURCE_MANUAL)
account_policy.save_policy(pol5)
client5 = AccountAwareClient({("t1", "hy3"): 200, ("t2", "hy3"): 200},
                             account_status={"t1": 400, "t2": 401})
model_health.run_round(
    accounts=accounts2,
    config={**model_health.default_config(), "checkAccounts": True,
            "autoDisableAccounts": True},
    client_factory=lambda: client5,
    base_url_for=lambda v: "https://example.invalid",
    used_models={},
    base_models=["hy3"],
)
check("已有的手动停用不被改写成 auto",
      account_policy.load_policy().get("a2") == "manual",
      str(account_policy.load_policy()))
account_policy.save_policy({})

check("checkAccounts 可被配置覆盖",
      model_health._coerce_config({"checkAccounts": False})["checkAccounts"] is False)
check("autoDisableAccounts 可被配置覆盖",
      model_health._coerce_config({"autoDisableAccounts": False})["autoDisableAccounts"] is False)

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 统一判定标准：把线上实测抓到的**真实响应体**固化在这里。
# 这些字符串不是编的 —— 每一条都对应一次真实的线上排查结论，
# 改动判定逻辑时它们会立刻告诉你有没有把既有结论改坏。
# ---------------------------------------------------------------------------
UPSTREAM_SAMPLES = {
    "ai_model_missing": (400, '{"code":11102,"msg":"model service info not found"}'),
    "cn_content_review": (403, '{"code":11140,"msg":"request illegal",'
                               '"displayMsg":"The content did not pass the safety review."}'),
    "nonstream_unsupported": (400, '{"code":11101,"msg":"Non-stream chat request is currently not supported"}'),
    "auth_expired": (401, '{"code":401,"msg":"unauthorized"}'),
    "rate_limited": (429, '{"code":429,"msg":"too many requests"}'),
}

_ai_sn = UPSTREAM_SAMPLES["ai_model_missing"][1]
_cn_sn = UPSTREAM_SAMPLES["cn_content_review"][1]
_ns_sn = UPSTREAM_SAMPLES["nonstream_unsupported"][1]

# 账号级：凭据有效性的唯一裁定者
check("实测·ai 版模型不存在 → 凭据有效",
      model_health.credential_state({
          "verdict": model_health.classify_account_probe(400, None, _ai_sn),
          "semantic": "model_missing"})["state"] == "valid")
check("实测·cn 版内容审查 → 账号受限而非凭据失效",
      model_health.credential_state({
          "verdict": model_health.classify_account_probe(403, None, _cn_sn),
          "semantic": "restricted"})["state"] == "restricted")
check("实测·非流式不支持 → 未得出结论而非凭据有效",
      model_health.credential_state({
          "verdict": model_health.classify_account_probe(400, None, _ns_sn),
          "semantic": "probe_defect"})["state"] == "unknown")
check("实测·401 → 凭据失效",
      model_health.credential_state({
          "verdict": model_health.classify_account_probe(
              401, None, UPSTREAM_SAMPLES["auth_expired"][1])})["state"] == "invalid")
check("实测·429 → 未得出结论",
      model_health.credential_state({
          "verdict": model_health.classify_account_probe(
              429, None, UPSTREAM_SAMPLES["rate_limited"][1])})["state"] == "unknown")

# 模型级：只判模型好坏，不反推凭据
check("实测·模型级 11140 判 restricted 而非 unavailable",
      model_health.classify_probe(403, None, 11140, _cn_sn) == "restricted",
      model_health.classify_probe(403, None, 11140, _cn_sn))
check("实测·模型级 11101 判 probe_defect",
      model_health.classify_probe(400, None, 11101, _ns_sn) == "probe_defect")
check("实测·模型级 11102 判 unavailable",
      model_health.classify_probe(400, None, 11102, _ai_sn) == "unavailable",
      model_health.classify_probe(400, None, 11102, _ai_sn))

# 策略写入：只有确凿的不可用才允许落盘
for _v, _expect in (("restricted", False), ("probe_defect", False),
                    ("transient", False), ("unavailable", True)):
    _rep = model_health.apply_verdicts({}, "acc-samp", [
        {"model": "hy3", "verdict": _v,
          "code": 11140 if _v == "restricted" else None}], auto_enable=True)
    check(f"实测·{_v} → {'写禁用' if _expect else '不写禁用'}",
          bool(_rep["disabled"]) == _expect, str(_rep["disabled"]))

# ---------------------------------------------------------------------------
# 账号被风控时，模型级自动启用必须被拦住
#
# 场景：账号整体被风控（账号探测 restricted），但某个模型这一轮偶然探测通过。
# 旧行为只看模型维度，于是把它放回轮询 —— 下一轮又失败，来回抖动。
# 风控是**账号级**状态，个别模型偶然通过不代表能用。
# ---------------------------------------------------------------------------
_blocked = model_health.apply_verdicts(
    {"acc-b": {"hy3": "auto"}}, "acc-b",
    [{"model": "hy3", "verdict": "available"}],
    auto_enable=True, account_blocked=True)
check("实测·账号风控时模型不被自动启用",
      "hy3" not in _blocked["enabled"], str(_blocked["enabled"]))
check("实测·账号风控时模型仍留在禁用策略里",
      "hy3" in _blocked["disabledAfter"], str(_blocked["disabledAfter"]))
# 对照：账号正常时同样输入应当被放回 —— 证明「不启用」确实由 account_blocked 引起，
# 而不是别的什么原因恰好让它没启用。
_free = model_health.apply_verdicts(
    {"acc-b": {"hy3": "auto"}}, "acc-b",
    [{"model": "hy3", "verdict": "available"}],
    auto_enable=True, account_blocked=False)
check("实测·账号正常时同输入会被自动启用（对照）",
      _free["enabled"] == ["hy3"], str(_free["enabled"]))

# ---------------------------------------------------------------------------
# 账号级：风控也自动停用；只有「正常」才自动放回
#
# 账号 id 用待测账号自己的（accounts[0]），不要另编一个 —— 探测结果按真实 id 归集，
# 写错 id 会让断言读到空列表，看起来像「逻辑没生效」。
# ---------------------------------------------------------------------------
_AID = str(accounts[0].get("id") or "")
# FakeClient 按 **token** 索引映射与账号级预设，账号 id 是另一回事 ——
# 混用会让所有预设都落空，断言读到空列表而误判成「逻辑没生效」。
_TOK = str(accounts[0].get("access_token") or "")
model_policy.save_policy({})
account_policy.save_policy({})
_rc_round = model_health.run_round(
    accounts=[accounts[0]],
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient(
        {(_TOK, "hy3"): 403, (_TOK, "kimi-k3"): 403},
        account_codes={_TOK: 403},
        account_bodies={_TOK: '{"code":11140,"msg":"request illegal"}'},
        bodies={(_TOK, "hy3"): '{"code":11140,"msg":"request illegal"}',
                (_TOK, "kimi-k3"): '{"code":11140,"msg":"request illegal"}'},
    ),
    base_url_for=lambda variant="ai": "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("实测·账号被风控 → 该账号被自动停用",
      _AID in (_rc_round.get("accountsDisabled") or []),
      f"got {_rc_round.get('accountsDisabled')}")
check("实测·被停用的风控账号计入 restricted 列表",
      any(r.get("id") == _AID for r in (_rc_round.get("restricted") or [])),
      f"got {_rc_round.get('restricted')}")
check("实测·风控停用写的是 auto 来源（可被自愈放回）",
      account_policy.load_policy().get(_AID) == "auto",
      f"got {account_policy.load_policy()}")

# 已被停用的账号，下一轮探测恢复正常 → 自动放回轮询
account_policy.save_policy({_AID: "auto"})
_ok_round = model_health.run_round(
    accounts=[accounts[0]],
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient({(_TOK, "hy3"): 200, (_TOK, "kimi-k3"): 200}),
    base_url_for=lambda variant="ai": "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("实测·账号恢复正常 → 被自动放回轮询",
      _AID in (_ok_round.get("accountsEnabled") or []),
      f"got {_ok_round.get('accountsEnabled')}")
check("实测·放回后策略里不再有该账号",
      _AID not in account_policy.load_policy(),
      f"got {account_policy.load_policy()}")

# 人工停用的账号，巡检无权替人放开
account_policy.save_policy({_AID: "manual"})
_man_round = model_health.run_round(
    accounts=[accounts[0]],
    config={"accountIds": [], "onlyUsedModels": False, "autoEnable": True},
    client_factory=lambda: FakeClient({(_TOK, "hy3"): 200, (_TOK, "kimi-k3"): 200}),
    base_url_for=lambda variant="ai": "https://example.test",
    used_models={},
    base_models=["hy3", "kimi-k3"],
)
check("实测·人工停用的账号不被巡检自动放回",
      _AID not in (_man_round.get("accountsEnabled") or [])
      and account_policy.load_policy().get(_AID) == "manual",
      f"enabled={_man_round.get('accountsEnabled')} policy={account_policy.load_policy()}")

# ---------------------------------------------------------------------------
# 面向用户的文案：只讲「能不能用」，不讲探测细节
# ---------------------------------------------------------------------------
for _verdict, _sem, _want in (
        ("available", "model_missing", "账号正常"),
        ("auth_failed", "auth", "重新登录"),
        ("restricted", "restricted", "风控"),
        ("transient", "transient", "重试"),
):
    _st = model_health.credential_state({"verdict": _verdict, "semantic": _sem})
    _um = _st.get("userMessage") or ""
    check(f"实测·{_verdict} 的用户提示含「{_want}」", _want in _um, _um)
    check(f"实测·{_verdict} 的用户提示不含探测细节",
          not any(w in _um for w in ("不存在", "模型名", "凭据", "状态码", "上游以")),
          _um)
    # 提示要短。界面把 userMessage 单独显示，不再拼 evidence / action ——
    # 但那两个字段本身也不该写成一段话：它们会出现在自检面板与悬停说明里。
    check(f"实测·{_verdict} 的用户提示够短（≤ 20 字）", len(_um) <= 20, f"{len(_um)} 字：{_um}")
    _act = _st.get("action") or ""
    check(f"实测·{_verdict} 的建议够短（≤ 20 字）", len(_act) <= 20, f"{len(_act)} 字：{_act}")
    check(f"实测·{_verdict} 的建议不含分号（不是把多句拼起来）",
          "；" not in _act, _act)

check("实测·正常账号的提示是肯定句（可以放心使用）",
      "可以放心使用" in (model_health.credential_state(
          {"verdict": "available", "semantic": "model_missing"}).get("userMessage") or ""),
      model_health.credential_state(
          {"verdict": "available", "semantic": "model_missing"}).get("userMessage"))
check("实测·正常账号不附带建议（无事需处理）",
      model_health.credential_state(
          {"verdict": "available", "semantic": "model_missing"}).get("action") is None,
      model_health.credential_state(
          {"verdict": "available", "semantic": "model_missing"}).get("action"))
check("实测·风控提示明说重登无效",
      "重新登录没用" in (model_health.credential_state(
          {"verdict": "restricted", "semantic": "restricted"}).get("action") or ""),
      model_health.credential_state(
          {"verdict": "restricted", "semantic": "restricted"}).get("action"))

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
