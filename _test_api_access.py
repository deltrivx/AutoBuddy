"""本地单测：API 访问密钥与 /v1 鉴权（v0.4.0）。

本机没有 fastapi / httpx，用 stub 顶掉后导入 gateway/main.py，
再验证 gateway/api_keys.py 的持久化、掩码、增删改与 _gateway_auth 的放行规则。

跑法：python3 _test_api_access.py
"""
import importlib
import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent
# 仓库根目录放在 gateway/ 之前：让 main 走 `from gateway import api_keys` 这条包导入路径，
# 与下面测试导入的是**同一个**模块对象。
# 否则 `import api_keys` 与 `gateway.api_keys` 会各自持有一份状态，
# 测试就会测到跟线上不同的那一个（这里踩过一次）。
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "gateway"))

FAILED = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


# ---------- stub fastapi / httpx ----------
class _App:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        def deco(*a, **k):
            if len(a) == 1 and callable(a[0]) and not k:
                return a[0]
            return lambda f: f
        return deco


class _HTTPException(Exception):
    """对齐 FastAPI 的真实行为：status_code / detail 都是可读属性。

    只写 `super().__init__(*a)` 会让 detail 丢掉，断言门锁防线时就无从检查拒绝原因。
    """

    def __init__(self, *a, **k):
        self.status_code = k.get("status_code", a[0] if a else None)
        self.detail = k.get("detail", a[1] if len(a) > 1 else None)
        super().__init__(str(self.detail))


_fastapi = types.ModuleType("fastapi")
_fastapi.FastAPI = _App
_fastapi.HTTPException = _HTTPException
_fastapi.Request = type("Request", (), {})
_fastapi.Response = type("Response", (), {})
sys.modules["fastapi"] = _fastapi

_resp = types.ModuleType("fastapi.responses")
_resp.StreamingResponse = type("StreamingResponse", (), {})
sys.modules["fastapi.responses"] = _resp

_httpx = types.ModuleType("httpx")
_httpx.AsyncClient = type("AsyncClient", (), {})
sys.modules["httpx"] = _httpx

# ---------- 用临时目录当 DATA_DIR ----------
tmpdir = tempfile.mkdtemp(prefix="wbs-apikey-")
os.environ["AB_DATA_DIR"] = tmpdir

main = importlib.import_module("main")
api_keys = importlib.import_module("gateway.api_keys")
assert main.api_keys is api_keys, "main 与测试用到了两个不同的 api_keys 模块实例"

KEYS_FILE = Path(tmpdir) / "api_keys.json"


class _Client:
    def __init__(self, host):
        self.host = host


class _Req:
    """_gateway_auth 只用到 request.client.host 与 request.headers。"""

    def __init__(self, host, headers=None):
        self.client = _Client(host)
        self.headers = headers or {}


def _req(host="203.0.113.9", key=None):
    headers = {}
    if key is not None:
        headers["authorization"] = f"Bearer {key}"
    return _Req(host, headers)


print("=== 1. 初始状态：无文件 -> 校验关闭、零密钥，且不抛 ===")
check("requireKey 默认 False", api_keys.get_state()["requireKey"] is False)
check("密钥列表为空", api_keys.get_config()["keys"] == [])
check("此时磁盘无文件", not KEYS_FILE.exists())
check("开启校验被拒（无可用密钥会把自己锁在门外）",
      api_keys.get_config()["stats"]["enabled"] == 0)

print("\n=== 2. 新建密钥 -> 格式、掩码、落盘 ===")
rec, err = api_keys.create_key("Sub2API")
check("创建成功", rec is not None and err is None, str(err))
check("前缀为 sk-ab-", rec["key"].startswith("sk-ab-"), rec["key"][:12])
check("密钥主体 32 位十六进制", len(rec["key"]) == len("sk-ab-") + 32, str(len(rec["key"])))
check("名称已保存", rec["name"] == "Sub2API")
check("默认启用", rec["enabled"] is True)
check("掩码只露头尾", rec["maskedKey"].startswith("sk-ab-") and "•" in rec["maskedKey"],
      rec["maskedKey"])
check("掩码不含完整明文", rec["maskedKey"] != rec["key"])
check("磁盘文件已生成", KEYS_FILE.exists())
on_disk = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
check("磁盘结构正确", on_disk["requireKey"] is False and len(on_disk["keys"]) == 1)
if os.name == "posix":
    mode = stat.S_IMODE(KEYS_FILE.stat().st_mode)
    check("文件权限 0600", mode == 0o600, oct(mode))
else:
    print("  SKIP  文件权限 0600（Windows 无 POSIX 权限位）")

print("\n=== 3. 密钥值唯一 + 自动命名不撞车 ===")
rec2, _ = api_keys.create_key(None)
rec3, _ = api_keys.create_key(None)
check("两次生成不重复", rec["key"] != rec2["key"] != rec3["key"])
check("第一个自动名为「默认密钥」", rec2["name"] == "默认密钥", rec2["name"])
check("第二个自动名带序号", rec3["name"] == "默认密钥 2", rec3["name"])

print("\n=== 4. 校验关闭时：任何请求都放行 ===")
check("无密钥放行", api_keys.authenticate(None)["ok"] is True)
check("错误密钥也放行", api_keys.authenticate("sk-ab-deadbeef")["ok"] is True)
check("模式为 open", api_keys.authenticate(None)["mode"] == "open")

print("\n=== 5. 开启校验后：缺失 / 错误 / 正确 ===")
api_keys.set_require_key(True)
check("配置已落盘", json.loads(KEYS_FILE.read_text(encoding="utf-8"))["requireKey"] is True)
bad = api_keys.authenticate(None)
check("缺密钥被拒", bad["ok"] is False and bad["mode"] == "missing")
check("缺密钥时有中文提示", "密钥" in bad.get("detail", ""))
check("错误密钥被拒", api_keys.authenticate("sk-ab-0000")["mode"] == "invalid")
good = api_keys.authenticate(rec["key"])
check("正确密钥放行", good["ok"] is True and good["mode"] == "key")
check("返回 keyId 用于归因", good["keyId"] == rec["id"], str(good.get("keyId")))
check("密钥首尾空格被容忍", api_keys.authenticate("  " + rec["key"] + "  ")["ok"] is True)

print("\n=== 6. 停用 / 启用 / 改名 ===")
api_keys.update_key(rec["id"], {"enabled": False})
check("停用后拒绝", api_keys.authenticate(rec["key"])["mode"] == "disabled")
api_keys.update_key(rec["id"], {"enabled": True, "name": "Sub2API 主"})
check("重新启用后放行", api_keys.authenticate(rec["key"])["ok"] is True)
check("改名生效", api_keys.get_config()["keys"][0]["name"] == "Sub2API 主")
check("未知 id 返回错误", api_keys.update_key("knope", {"enabled": True})[1] == "key not found")
check("空名字不覆盖原值", api_keys.update_key(rec["id"], {"name": "   "})[0]["name"] == "Sub2API 主")

print("\n=== 7. 调用计数与最近使用 ===")
api_keys.mark_used(rec["id"])
api_keys.mark_used(rec["id"])
stats = api_keys.get_config()
row = [k for k in stats["keys"] if k["id"] == rec["id"]][0]
check("计数累加", row["callCount"] == 2, str(row["callCount"]))
check("记录最近使用时间", row["lastUsedAt"] is not None)
check("未知 keyId 不抛", api_keys.mark_used("knope") is None)
check("mark_used(None) 不抛", api_keys.mark_used(None) is None)

print("\n=== 8. /v1 鉴权：loopback 免校验、外部必须带密钥 ===")
check("开启校验后 127.0.0.1 放行", main._gateway_auth(_req("127.0.0.1"))["ok"] is True)
check("loopback 模式标记为 internal", main._gateway_auth(_req("127.0.0.1"))["mode"] == "internal")
check("::1 放行", main._gateway_auth(_req("::1"))["ok"] is True)
check("外部无密钥 401", main._gateway_auth(_req("203.0.113.9"))["ok"] is False)
check("外部带正确密钥放行",
      main._gateway_auth(_req("203.0.113.9", rec["key"]))["ok"] is True)
check("外部带错误密钥拒绝",
      main._gateway_auth(_req("203.0.113.9", "sk-ab-bad"))["ok"] is False)
check("x-api-key 头同样有效",
      main._gateway_auth(_Req("203.0.113.9", {"x-api-key": rec["key"]}))["ok"] is True)
check("Bearer 大小写不敏感",
      main._gateway_auth(_Req("203.0.113.9", {"authorization": "bearer " + rec["key"]}))["ok"] is True)
api_keys.set_require_key(False)
check("关闭校验后外部也放行", main._gateway_auth(_req("203.0.113.9"))["ok"] is True)

print("\n=== 9. 删除 / 清空 ===")
api_keys.set_require_key(True)
check("删除存在的密钥返回 True", api_keys.delete_key(rec2["id"]) is True)
check("删除不存在的返回 False", api_keys.delete_key("knope") is False)
check("剩余 2 个", len(api_keys.get_config()["keys"]) == 2)
check("已删密钥不再通过校验", api_keys.authenticate(rec2["key"])["mode"] == "invalid")
check("清空返回删除数", api_keys.delete_all_keys() == 2)
check("清空后无密钥", api_keys.get_config()["keys"] == [])
check("清空后磁盘同步", json.loads(KEYS_FILE.read_text(encoding="utf-8"))["keys"] == [])

print("\n=== 9b. 门锁防线：任何操作都不能把所有人关在门外 ===")


def _raises(fn, *a, **k):
    try:
        fn(*a, **k)
        return None
    except main.HTTPException as exc:
        return exc


exc = _raises(main.api_keys_config, {"requireKey": True})
check("零密钥时开启校验被拒（endpoint 层）", exc is not None)
check("拒绝原因可读", exc is not None and "密钥" in str(exc.detail), str(exc.detail) if exc else "")

r = main.api_keys_create({"name": "唯一密钥"})
sole = r["key"]
check("新建接口返回 key 与 status", "key" in r and "status" in r)
exc = _raises(main.api_keys_config, {"requireKey": True})
check("有密钥后可以开启校验", exc is None)
check("开启后状态为真", api_keys.get_state()["requireKey"] is True)

exc = _raises(main.api_keys_update, {"id": sole["id"], "enabled": False})
check("停用最后一个启用中的密钥被拒", exc is not None)
check("拒绝原因可读", exc is not None and "最后一个" in str(exc.detail), str(exc.detail) if exc else "")

exc = _raises(main.api_keys_delete, {"id": sole["id"]})
check("删除最后一个启用中的密钥被拒", exc is not None)

exc = _raises(main.api_keys_delete_all)
check("开启校验时清空全部被拒", exc is not None)

r2 = main.api_keys_create({"name": "第二个"})
check("再建一个后仍有 2 个", len(api_keys.get_config()["keys"]) == 2)
exc = _raises(main.api_keys_update, {"id": sole["id"], "enabled": False})
check("有备用密钥后即可停用", exc is None)

check("缺 id 时报错", _raises(main.api_keys_update, {"enabled": True}) is not None)
check("无可更新字段时报错", _raises(main.api_keys_update, {"id": sole["id"]}) is not None)
exc = _raises(main.api_keys_update, {"id": "knope", "name": "x"})
check("未知 id 返回 404", exc is not None and getattr(exc, "status_code", None) == 404)

# 收尾：关掉校验并清空，方便后续小节从干净状态开始
api_keys.set_require_key(False)
api_keys.delete_all_keys()
check("收尾后回到零密钥", api_keys.get_config()["keys"] == [])

print("\n=== 10. 配置损坏 -> 回退安全默认，绝不抛 ===")
KEYS_FILE.write_text("{ this is not json", encoding="utf-8")
api_keys._load_locked()
check("坏文件不抛且回退为空", api_keys.get_config()["keys"] == [])
check("坏文件回退为关闭校验", api_keys.get_state()["requireKey"] is False)
KEYS_FILE.write_text(json.dumps({"requireKey": True, "keys": "not-a-list"}), encoding="utf-8")
api_keys._load_locked()
check("keys 非 list 被容忍", api_keys.get_config()["keys"] == [])
check("合法 requireKey 仍被读取", api_keys.get_state()["requireKey"] is True)
KEYS_FILE.write_text(json.dumps({"requireKey": True, "keys": [{"no_key": 1}, {"key": "sk-ab-x"}]}),
                     encoding="utf-8")
api_keys._load_locked()
check("缺 key 字段的条目被过滤", len(api_keys.get_config()["keys"]) == 1)
KEYS_FILE.write_text(json.dumps({"requireKey": False, "keys": []}), encoding="utf-8")
api_keys._load_locked()

print("\n=== 11. 无 .tmp 残留 ===")
check("没有 api_keys.json.tmp 残留", not (Path(tmpdir) / "api_keys.json.tmp").exists())

print("\n=== 12. 连接信息接口自洽 ===")
info = main._gateway_info()
check("返回 basePath /v1", info["basePath"] == "/v1")
check("给出两个端点", set(info["endpoints"]) >= {"chatCompletions", "models"})
check("鉴权字段完整", set(info["auth"]) >= {"requireKey", "header", "keysTotal", "keysEnabled"})
check("模型计数为正整数", isinstance(info["models"]["count"], int) and info["models"]["count"] > 0,
      str(info["models"]["count"]))
check("账号统计存在", set(info["accounts"]) >= {"total", "usable", "inPool", "mode"})

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：")
    for name in FAILED:
        print("   -", name)
    sys.exit(1)
print("✅ 全部断言通过")
