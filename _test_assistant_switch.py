#!/usr/bin/env python3
"""助手配置页卡死修复回归（2026-10-14 用户反馈）。

### 用户反馈

> 「智能助手的配置页面，点击 OpenAI 兼容接口直接卡死，还没配置就已经切换了」

### 实测根因（两个缺陷叠加）

复现 `PUT /api/assistant`（切 provider 走的就是它）：

```
耗时: 102.1 秒
probe = {"ok": false, "error": "未收到回答（检查地址、密钥与模型名）"}
```

1. **卡死**：`assistant_put()` 保存后**同步跑一次 LLM 探测**，
   实测 **102.1 秒**（探测要跑两轮 LLM：45s 超时 ×2 + 工具调用）。
   前端 await 同一个请求，页面看着就像卡死；
   **总开关按钮也走这个 PUT**，等于点一下开关也要等 100 秒。

2. **未配置就切换**：`_save_config()` 在探测**之前**执行 —— provider
   已被切成 `openai`，但密钥是空的，助手进入坏状态。
   （探测失败只影响返回的 `probe` 字段，救不回已经落盘的配置。）

### 修复

1. **PUT 不再自动探测**：保存只落盘，想验证连通性请用「测试连通性」按钮
   （`POST /api/assistant/test`，用户主动发起、有明确等待预期）。
   保留 ``probe: true`` 显式开关（向后兼容旧调用方）。
2. **新增轻量切换接口** `POST /api/assistant/provider`：只换 provider，
   零网络开销、即时返回。
3. **切到需要密钥的 provider 但没填密钥时给出 warn**：
   前端 toast 提示去补配置，而不是静默生效一个坏配置。

本测试锁定上述契约。
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
SRC_WP = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

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


print("[1] PUT 不再自动探测（卡死根因）")

# 取出 assistant_put 函数体
_put_start = SRC_WP.index("async def assistant_put(request: Request):")
_put_end = SRC_WP.index("async def _assistant_probe(")
_PUT_BODY = SRC_WP[_put_start:_put_end]

check("PUT 处理器存在", _put_start > 0)
check("PUT 不再无条件调用探测",
      "probe = await _assistant_probe(payload)\n" not in _PUT_BODY.replace(
          "                probe = await _assistant_probe(payload)\n", ""),
      "PUT 仍可能在无条件下探测")
check("PUT 只在显式 probe:true 时探测",
      'payload.get("probe") is True' in _PUT_BODY)
check("PUT 保存逻辑在探测之前（保存永不被探测阻塞）",
      _PUT_BODY.index("_assistant._save_config(payload)")
      < _PUT_BODY.index("_assistant_probe"))
check("PUT 返回 warn 字段（缺密钥时提示）", '"warn": warn' in _PUT_BODY)
check("PUT 注释记录 102.1 秒根因", "102.1" in _PUT_BODY)

print("\n[2] 轻量切换接口")

check("POST /api/assistant/provider 存在",
      '@app.post("/api/assistant/provider")' in SRC_WP)
_SW_START = SRC_WP.index("async def assistant_switch_provider")
_SW_END = SRC_WP.index('@app.post("/api/assistant/test")')
_SW_BODY = SRC_WP[_SW_START:_SW_END]
check("切换接口只落盘不探测",
      '_assistant._save_config({"provider": key})' in _SW_BODY
      and "assistant_reply" not in _SW_BODY)
check("切换接口校验未知 provider",
      "if key not in _assistant._PROVIDER_BY_KEY:" in SRC_WP)
check("切换接口返回 warn", '"warn": warn' in SRC_WP)
check("_PROVIDER_NAME 映射存在（提示用）",
      "_PROVIDER_NAME = {p[\"key\"]: p.get(\"name\") for p in _assistant.PROVIDERS}" in SRC_WP)

print("\n[3] 前端接线")

check("provider 标签改走轻量接口",
      'fetch("/api/assistant/provider"' in SRC_WP)
check("切换用 POST + JSON body",
      'body: JSON.stringify({ provider: p.key })' in SRC_WP)
check("切换有禁用态（防连点）", "tag.disabled = true;" in SRC_WP)
check("切换完成恢复禁用态", "tag.disabled = false;" in SRC_WP)
check("切换区分 ok / warn 两种 toast",
      'wbToast(d.warn ? d.warn : ("已切换到 " + p.name), d.warn ? "warn" : "ok");' in SRC_WP)
check("切换失败有兜底提示", "切换失败" in SRC_WP)
check("provider 标签不再走 PUT（卡死路径已弃用）",
      'wbApiAction("/api/assistant", "PUT", { provider: p.key }' not in SRC_WP)
check("总开关仍走 PUT 但不触发探测",
      'wbApiAction("/api/assistant", "PUT", { enabled: !cfg.enabled }' in SRC_WP)
check("collect() 不带 probe 字段（保存不探测）",
      'payload.providers[curKey] = pv;' in SRC_WP
      and "probe:" not in SRC_WP[SRC_WP.index("function collect()"):
                                  SRC_WP.index("function collect()") + 400])

print("\n[4] 测试连通性接口保持独立（用户主动发起）")

check("POST /api/assistant/test 仍存在",
      '@app.post("/api/assistant/test")' in SRC_WP)
check("test 接口仍走探测（这是它该做的事）",
      "return await _assistant_probe(payload.get(\"config\"))" in SRC_WP)
check("test 接口的 save 参数仍可用（先存再测）",
      'if payload.get("save"):' in SRC_WP)

print("\n[5] 配置层 secret 语义未被破坏（assistant.py）")

SRC_AS = (ROOT / "gateway" / "assistant.py").read_text(encoding="utf-8")
check("_save_config 仍是合并式", "def _save_config(" in SRC_AS)
check("secret 三态语义保留（未提交=保留）",
      "if k not in pv:" in SRC_AS and "# 未提交 → 保留" in SRC_AS
      and "# 保留" in SRC_AS,
      "未提交=保留 / None=保留 / 空串=清除 三态必须都在")
check("secret 空串=清除", 'slot[k] = str(v or "").strip()' in SRC_AS)
check("_runtime 仍解析 api_key", '"api_key": str(slot.get("api_key") or "").strip()' in SRC_AS)
check("PROVIDERS 注册表未动（两个 provider 仍在）",
      '"key": "gateway"' in SRC_AS and '"key": "openai"' in SRC_AS)

print("\n[6] 行为验证（真实配置层，不联网）")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
os.environ["AB_ASSISTANT_WEBUI"] = "http://127.0.0.1:1"
os.environ["AB_ASSISTANT_GATEWAY"] = "http://127.0.0.1:1"
sys.path.insert(0, str(ROOT))
from gateway import assistant as A  # noqa: E402

# 模拟「切到 openai 但没填密钥」→ _runtime 应能识别出缺密钥
A._save_config({"provider": "openai"})
rt = A._runtime()
check("切到 openai 后 runtime.provider 正确", rt.get("provider") == "openai")
check("未填密钥时 api_key 为空（warn 判据成立）", rt.get("api_key") == "")
check("缺密钥场景可被前端识别",
      rt.get("provider") != "gateway" and not rt.get("api_key"))

# 填了密钥就不再是缺密钥场景
A._save_config({"providers": {"openai": {"api_key": "sk-test-123"}}})
rt2 = A._runtime()
check("填密钥后 api_key 读到", rt2.get("api_key") == "sk-test-123")
check("有密钥时不触发 warn",
      not (rt2.get("provider") != "gateway" and not rt2.get("api_key")))

# 切回 gateway：即使 openai 有密钥，也不该 warn（gateway 不需要）
A._save_config({"provider": "gateway"})
rt3 = A._runtime()
check("切回 gateway 后 provider 正确", rt3.get("provider") == "gateway")

# provider 名校映射可用（前端提示要用）
names = {p["key"]: p.get("name") for p in A.PROVIDERS}
check("provider 名映射含两个 provider",
      names.get("gateway") and names.get("openai"), f"got {names}")

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
