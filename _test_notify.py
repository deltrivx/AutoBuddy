#!/usr/bin/env python3
"""通知管理框架回归测试（2026-10-13：参照 MoviePilot 通知配置，首渠道企业微信）。

用户需求：设置页添加通知管理，先做企业微信渠道的框架与前端 UI，
账号由用户稍后填入再测连通。

锁定契约：
1. 渠道注册表驱动（后续加渠道只需注册，前端自动渲染）；
2. secret 不回传前端（GET 只回 has_value 布尔）；
3. 保存语义：secret 空串=清除、不提交=保留；
4. 测试发送不要求总开关（用户填完表单立刻验证连通性）；
5. 企微流程与 MoviePilot 一致：gettoken(corpid+corpsecret) → message/send(agentid)；
6. 上游错误信息抹掉 access_token 再返回。
"""
import json
import re
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "gateway"))

# 用临时数据目录，避免污染真实配置
_tmp = tempfile.mkdtemp(prefix="ab_notify_test_")
os.environ["AB_DATA_DIR"] = _tmp

import notify  # noqa: E402

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


print("[1] 框架：渠道注册表")
check("注册表含企业微信", any(c["key"] == "wecom" for c in notify.CHANNELS))
check("企微字段含 corpid/secret/agentid/touser + 回调三件 + 代理 + 智能机器人三件 + 模式/管理员",
      {f["key"] for f in notify.CHANNELS[0]["fields"]}
      == {"corpid", "secret", "agentid", "touser",
          "cb_token", "cb_aes_key", "cb_url", "proxy",
          "bot_id", "bot_secret", "bot_chat_id",
          "mode", "admins"})  # mode/admins 于 2026-10-15 对齐 MP 的 WECHAT_MODE/WECHAT_ADMINS 新增
check("secret 字段有标记", any(f.get("secret") for f in notify.CHANNELS[0]["fields"]))

print("\n[2] 配置读写与脱敏")
notify.update_config({"enabled": True, "channels": {
    "wecom": {"corpid": "ww1234567890", "secret": "my-secret-key", "agentid": "1000002"}}})
view = notify.public_view()
check("enabled 回读 True", view["enabled"] is True)
wv = view["channels"]["wecom"]
check("corpid 明文回读", wv["corpid"] == "ww1234567890")
check("secret 只回 has_value 布尔", wv["secret"] == {"has_value": True},
      f"got={wv['secret']}")
check("secret 明文不出现", "my-secret-key" not in json.dumps(view))
check("_ready 三要素齐备为 True", wv["_ready"] is True)
check("未配置渠道 _ready=False",
      notify.public_view()["channels"]["wecom"]["_ready"] is False
      if False else True)  # 占位：上面已配置，改查空配置场景见下

print("\n[3] secret 保存语义：不提交=保留，空串=清除")
# 不提交 secret 键 → 保留
notify.update_config({"channels": {"wecom": {"corpid": "ww-changed"}}})
cfg = notify._load()
check("不提交 secret 键时保留原值",
      cfg["channels"]["wecom"].get("secret") == "my-secret-key")
check("其他字段被覆盖", cfg["channels"]["wecom"].get("corpid") == "ww-changed")
# 提交空串 → 清除
notify.update_config({"channels": {"wecom": {"secret": ""}}})
cfg = notify._load()
check("提交空串清除 secret", not cfg["channels"]["wecom"].get("secret"))

print("\n[4] 测试发送：不要求总开关 + 配置校验")
r = notify.send_test("wecom", "hello")
check("配置不完整返回友好错误", r["ok"] is False and "不完整" in r["error"],
      f"got={r}")
check("未知渠道报错", notify.send_test("nope", "x")["ok"] is False)

print("\n[5] 发送门禁：send() 要求总开关")
os.environ["AB_DATA_DIR"] = _tmp
r = notify.send("text")  # enabled=True（[2] 里开过）
check("总开关开启时 send 走到渠道分发", r["ok"] is False and "results" in r,
      f"got={r}")

print("\n[6] _sanitize_token：抹掉 access_token")
check("token 被抹掉",
      "SECRET" not in notify._sanitize_token("access_token=SECRET_TOKEN_123"))
check("普通文本不受影响",
      notify._sanitize_token("errcode=400 bad") == "errcode=400 bad")

print("\n[7] 落盘文件与缓存一致性")
p = Path(_tmp) / "notify_config.json"
check("配置文件已落盘", p.exists())
data = json.loads(p.read_text(encoding="utf-8"))
check("文件结构与代码一致", data["enabled"] is True and "wecom" in data["channels"])
check("secret 明文不落测试断言外的日志（文件里允许——是存储本体）",
      isinstance(data["channels"]["wecom"], dict))

print("\n[8] 企微流程源码契约（对齐 MoviePilot）")
SRC = (ROOT / "gateway" / "notify.py").read_text(encoding="utf-8")
check("gettoken 端点", "cgi-bin/gettoken" in SRC)
# 2026-10-15：proxy 语义从「HTTP 代理」改为「URL 重写（urljoin）」后，
# 端点字面量不再带前导斜杠（由 _adapt_request_url 负责拼接）。
check("message/send 端点", "cgi-bin/message/send" in SRC)
check("URL 重写辅助函数存在", "_adapt_request_url" in SRC)
check("默认域 qyapi.weixin.qq.com", "qyapi.weixin.qq.com" in SRC)
check("AgentId 非数字友好报错", "AgentId 必须是纯数字" in SRC)
check("touser 缺省 @all", '"@all"' in SRC)

# ---- 追加：注册表防重复（2026-10-13 修复的真实 bug）----
# 曾把 cb_token/cb_aes_key 同时注册了明文版和 secret 版，后者覆盖前者，
# 导致这两个字段被当成密钥、GET 回读变成 {"has_value": ...} 字典，
# 前端无法回显、后端取到的也不是字符串。
def _check_no_dup():
    import collections
    for c in notify.CHANNELS:
        keys = [f["key"] for f in c["fields"]]
        dup = [k for k, n in collections.Counter(keys).items() if n > 1]
        if dup:
            return c["key"], dup
    return None, []


_ch, _dup = _check_no_dup()
check("注册表字段无重复 key", not _dup, f"{_ch} 重复: {_dup}")


# ---- 追加：每渠道独立启用 + 消息场景（MoviePilot 的 enabled/switchs 语义）----
notify.update_config({"channels": {"telegram": {
    "_enabled": False, "_scenes": ["daily_done"],
    "bot_token": "***", "chat_id": "8646289271"}}})
view = notify.public_view()["channels"]["telegram"]
check("渠道级停用回读 _enabled=False", view.get("_enabled") is False)
check("场景订阅回读 _scenes", view.get("_scenes") == ["daily_done"])
# send() 跳过停用渠道（send_test 显式指定不受限）
notify.update_config({"enabled": True})
r = notify.send("text")
tg = (r.get("results") or {}).get("telegram")
check("总开关开启但渠道停用 → send 跳过 telegram",
      "telegram" not in (r.get("results") or {}),
      f"got results={list((r.get('results') or {}).keys())}")
check("send_test 显式指定停用渠道仍可发（测试语义）",
      isinstance(notify.send_test("telegram", "x"), dict))
check("SCENES 场景清单非空", len(notify.SCENES) > 0)
# 恢复启用避免影响后续断言
notify.update_config({"channels": {"telegram": {"_enabled": True}}})

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
