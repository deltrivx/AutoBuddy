#!/usr/bin/env python3
"""助手 provider 切换语义修正回归（2026-10-15 用户反馈）。

### 用户反馈（两轮，指向同一处设计错误）

> 「点击 OpenAI 兼容接口直接卡死，还没配置就已经切换了」（v0.9.77 修）
> 「切换逻辑有问题，切换又没保存，提示我干嘛，仔细思考应该怎么处理」

### 演进与最终定论

| 版本 | 做法 | 问题 |
|---|---|---|
| v0.9.69 前 | 切 provider 走 PUT，PUT 保存后**同步跑 LLM 探测** | 实测 **102.1 秒**，页面像卡死；且总开关也走 PUT |
| v0.9.77 | 新增 `POST /api/assistant/provider`，切即落盘 + 提示缺密钥 | **切换又没保存**——用户以为只是选一下，实际已提交；空手切换也弹「没填 API Key」纯属噪音 |

### 最终模型（本次修正）

「切 provider」不是提交动作，而是**选择要编辑哪一组字段**——
这是配置页的标准语义。因此：

1. **切换只改本地选中态 `selKey` + 重渲染字段表单**，零网络请求、不落盘；
2. **只有「保存」才提交**（PUT，含选中的 provider）；
3. **草稿保持**：每个 provider 各自记住本次编辑内容，切走再切回不丢
   （v0.9.77 的 `state` 在 renderFields 内，切回会把用户刚填的地址/密钥全丢）；
4. **提示策略**：空手切换不打扰；只有**已动过表单**时才提醒「有未保存的修改」；
5. `collect()` 按**当前选中**的 provider 定义收集字段
   （原先读闭包里的初始 `cur`，切到 openai 后保存会漏 base_url/api_key）；
6. 死接口 `POST /api/assistant/provider` 已删除（切换不再需要服务端参与）。

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


print("[1] 切换 = 选编辑目标（不落盘、零网络）")

check("选中态 selKey 存在", "var selKey = curKey;" in SRC_WP)
check("provider 标签 onclick 不发请求",
      'fetch("/api/assistant/provider"' not in SRC_WP
      and 'wbApiAction("/api/assistant", "PUT", { provider: p.key }' not in SRC_WP,
      "切换仍是落盘动作")
check("切换只重渲染字段表单",
      'var box = document.getElementById("wb-asst-fields");' in SRC_WP
      and "renderFields(box, p);" in SRC_WP)
check("字段容器有固定 id（wb-asst-fields）", 'fieldsBox.id = "wb-asst-fields";' in SRC_WP)
check("renderFields 可重渲染（函数签名带 box/prov）",
      "function renderFields(box, prov) {" in SRC_WP)
check("死接口已删除（不再有 provider 专用端点）",
      '@app.post("/api/assistant/provider")' not in SRC_WP
      and "assistant_switch_provider" not in SRC_WP)

print("\n[2] 提示策略（不打扰）")

check("切换不再无条件弹「已切换到…」",
      'wbToast("已切换到 " + p.name' not in SRC_WP)
check("只有动过表单才提醒未保存",
      "var touched = !!(draft[p.key] && Object.keys(draft[p.key]).length);" in SRC_WP)
check("提醒文案说明「有未保存的修改」",
      "有未保存的修改，记得点「保存」" in SRC_WP)

print("\n[3] 草稿保持（切走切回不丢内容）")

check("draft 字典存在", "var draft = {};" in SRC_WP)
check("state 提升到外层（不在 renderFields 内声明）",
      SRC_WP.index("var state = {") < SRC_WP.index("function renderFields(box, prov) {"))
check("base_url 输入记草稿", 'draft[prov.key].base_url = baseInput.value;' in SRC_WP)
check("api_key 输入记草稿", 'draft[prov.key].api_key = keyInput.value;' in SRC_WP)
check("model 输入记草稿", 'draft[prov.key].model = modelInput.value;' in SRC_WP)
check("systemPrompt 输入记草稿", 'draft[prov.key].systemPrompt = promptInput.value;' in SRC_WP)
check("渲染时恢复草稿（d.base_url !== undefined）",
      "d.base_url !== undefined" in SRC_WP)
check("无草稿时回落到已保存值/默认值",
      "(slot.base_url || cur.default_base_url || \"\")" in SRC_WP)

print("\n[4] collect 用当前选中的 provider")

check("collect 的 provider 用 selKey", "provider: selKey," in SRC_WP)
check("collect 用 providers[selKey]", "payload.providers[selKey] = pv;" in SRC_WP)
check("collect 按选中态取 provider 定义",
      'var prov = reg.filter(function (x) { return x.key === selKey; })[0] || {};' in SRC_WP)
check("collect 不再读闭包里的旧 cur",
      "if (prov.base_url_editable !== false)" in SRC_WP
      and "if (prov.supports_api_key && state.api_key)" in SRC_WP)

print("\n[5] 保存仍是唯一提交点")

check("保存按钮走 PUT", 'wbApiAction("/api/assistant", "PUT", collect()' in SRC_WP)
check("PUT 保存不自动探测（v0.9.77 的修复不回退）",
      'payload.get("probe") is True' in SRC_WP)
check("PUT 保存缺密钥时返回 warn（保存时才提示，合理）",
      '"warn": warn' in SRC_WP)
check("测试连通性仍是独立接口",
      '@app.post("/api/assistant/test")' in SRC_WP)

print("\n[6] 行为验证：写意图识别不被破坏（assistant.py）")

SRC_AS = (ROOT / "gateway" / "assistant.py").read_text(encoding="utf-8")
check("6 个写操作仍在注册",
      all(f'_register_write_op("{op}"' in SRC_AS
          for op in ("run_daily", "checkin_all", "abort_daily",
                     "probe_models", "notify_on", "notify_off")))
check("写意图识别函数仍在", "def _write_intent(" in SRC_AS)
check("两段式确认仍在", "def try_write_flow(" in SRC_AS)
check("疑问句双保险仍在", "_nt._looks_like_question(text)" in SRC_AS)

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp
os.environ["AB_ASSISTANT_WEBUI"] = "http://127.0.0.1:1"
os.environ["AB_ASSISTANT_GATEWAY"] = "http://127.0.0.1:1"
sys.path.insert(0, str(ROOT))
from gateway import assistant as A  # noqa: E402

# 写意图仍可用（切换语义改动不能影响它）
write_cases = [("执行任务", "run_daily"), ("签到", "checkin_all"),
               ("触发巡检", "probe_models"), ("开启通知", "notify_on"),
               ("中止任务", "abort_daily")]
for text, want in write_cases:
    got = A._write_intent(text)
    check(f"写意图 {text!r} -> {want}", got == want, f"got {got}")

# 疑问句仍不触发写（防回归）
for q in ("今天执行过任务吗", "任务跑完了吗", "签到了吗"):
    check(f"疑问句 {q!r} 不触发写", A._write_intent(q) is None)

# 配置层仍可用
A._save_config({"provider": "openai"})
rt = A._runtime()
check("配置层 provider 可切换（服务端能力保留）", rt.get("provider") == "openai")
check("未填密钥时 api_key 为空（PUT 保存时 warn 判据）",
      rt.get("api_key") == "")
A._save_config({"provider": "gateway"})

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
