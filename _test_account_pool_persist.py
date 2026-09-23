"""本地单测：账号池「保留式更新」与模型「一键恢复」（v0.4.15）。

## 为什么要专门测这两件事

两个缺陷都属于**静默降级**型 —— 不报错、不抛异常，界面也看不出异常，
只在用户日后排查时表现为「我设置的东西没生效」：

1. **设为首选会清空白名单**。前端「设为首选」只提交 `{mode, manualAccountId}`，
   后端旧实现用 `config.get("enabledAccountIds", [])` 取值 —— 缺字段被读成空数组
   并覆盖落盘，用户勾选的账号白名单整份丢失。空数组在业务上表示「全部启用」，
   所以现场毫无异样。

2. **一键恢复**是新增能力，需要确认它**真的**移除了 `manual` 与 `auto` 两种来源，
   而不是只清掉其中一种（漏掉 manual 会让「全部恢复」名不副实且无提示）。

跑法：python3 _test_account_pool_persist.py
"""
import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "gateway"))

FAILED = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


# ---------- stub fastapi ----------
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

tmpdir = tempfile.mkdtemp(prefix="wbs-pool-")
os.environ["AB_DATA_DIR"] = tmpdir

main = importlib.import_module("main")
model_policy = importlib.import_module("model_policy")

POOL_FILE = Path(tmpdir) / "account_pool_config.json"
POLICY_FILE = Path(tmpdir) / "model_policy.json"

ACC_A = "aaaa1111-0000-0000-0000-000000000001"
ACC_B = "bbbb2222-0000-0000-0000-000000000002"
ACC_C = "cccc3333-0000-0000-0000-000000000003"

# 用假账号池顶掉真实 accounts.json 读取（本测试只关心配置读写）
_FAKE_ACCOUNTS = [
    {"id": ACC_A, "email": "a@test", "access_token": "tok-a"},
    {"id": ACC_B, "email": "b@test", "access_token": "tok-b"},
    {"id": ACC_C, "email": "c@test", "access_token": "tok-c"},
]
main._load_accounts = lambda: list(_FAKE_ACCOUNTS)


def pool_on_disk():
    return json.loads(POOL_FILE.read_text(encoding="utf-8"))


# ===========================================================================
print("=== 1. 初始状态：白名单写入并落盘 ===")
main.update_account_pool({
    "mode": "auto",
    "enabledAccountIds": [ACC_A, ACC_B, ACC_C],
    "manualAccountId": None,
})
disk = pool_on_disk()
check("白名单三项已落盘", disk["enabledAccountIds"] == [ACC_A, ACC_B, ACC_C],
      str(disk["enabledAccountIds"]))
check("mode 为 auto", disk["mode"] == "auto", disk["mode"])

# ===========================================================================
print("\n=== 2. 核心回归：设为首选（不带白名单）不得清空白名单 ===")
# 这正是界面上「设为首选」发出的 payload —— 只改 mode 与 manualAccountId。
main.update_account_pool({"mode": "manual", "manualAccountId": ACC_B})
disk = pool_on_disk()
check("★ 白名单未被清空（本次修复的核心）",
      disk["enabledAccountIds"] == [ACC_A, ACC_B, ACC_C],
      f"got {disk['enabledAccountIds']}  ← 旧实现会变成 []")
check("mode 已切到 manual", disk["mode"] == "manual", disk["mode"])
check("首选账号已记下", disk["manualAccountId"] == ACC_B, disk["manualAccountId"])

# 再切回自动分配，同样不得动白名单
main.update_account_pool({"mode": "auto", "manualAccountId": None})
disk = pool_on_disk()
check("★ 切回自动分配后白名单仍在",
      disk["enabledAccountIds"] == [ACC_A, ACC_B, ACC_C],
      str(disk["enabledAccountIds"]))
check("首选账号已清除", disk["manualAccountId"] is None, str(disk["manualAccountId"]))

# ===========================================================================
print("\n=== 3. 显式提交白名单时仍能正常收窄 ===")
main.update_account_pool({
    "mode": "auto", "enabledAccountIds": [ACC_A, ACC_C],
})
disk = pool_on_disk()
check("白名单按提交值收窄", disk["enabledAccountIds"] == [ACC_A, ACC_C],
      str(disk["enabledAccountIds"]))

# ===========================================================================
print("\n=== 4. 只改白名单、不提交首选账号：不应报错 ===")
ok = True
try:
    main.update_account_pool({"enabledAccountIds": [ACC_A]})
except Exception as e:
    ok = False
    print(f"      异常：{e}")
check("缺 mode / manualAccountId 的请求可以成功", ok)
disk = pool_on_disk()
check("白名单更新为一项", disk["enabledAccountIds"] == [ACC_A], str(disk["enabledAccountIds"]))
check("mode 沿用原值", disk["mode"] in ("auto", "manual"), disk["mode"])

# ===========================================================================
print("\n=== 5. 切 manual 但没给首选账号：退回 auto 而不是崩 ===")
main.update_account_pool({"mode": "manual", "enabledAccountIds": [ACC_A]})
disk = pool_on_disk()
check("无首选账号时不会停在 manual（否则请求无处可去）",
      disk["mode"] == "auto", f"got {disk['mode']}")
check("白名单保留", disk["enabledAccountIds"] == [ACC_A], str(disk["enabledAccountIds"]))

# ===========================================================================
print("\n=== 6. 未知账号仍会被拒绝 ===")
rejected = False
try:
    main.update_account_pool({
        "mode": "auto", "enabledAccountIds": [ACC_A, "nope-9999"],
    })
except _HTTPException as e:
    rejected = "nope-9999" in json.dumps(e.detail)
check("白名单里的未知账号被拒", rejected)

# ===========================================================================
print("\n=== 7. 一键恢复：清掉 manual 与 auto 两种来源 ===")
main.update_account_pool({
    "mode": "auto", "enabledAccountIds": [ACC_A, ACC_B, ACC_C],
})
model_policy.save_policy({
    ACC_A: {"hy3": "manual", "kimi-k3": "auto", "glm-4": "manual"},
})

res = main.restore_account_models({"accountId": ACC_A, "scope": "all"})
check("恢复 3 个模型", res["restoredCount"] == 3, str(res["restored"]))
check("★ manual 来源也被恢复（全放回）",
      "hy3" in res["restored"] and "glm-4" in res["restored"], str(res["restored"]))
check("★ auto 来源也被恢复", "kimi-k3" in res["restored"], str(res["restored"]))
check("恢复后该账号禁用列表为空", res["disabledModels"] == [], str(res["disabledModels"]))
check("恢复后来源表为空", res["disabledSources"] == {}, str(res["disabledSources"]))
check("落盘确认：策略里该账号已无条目",
      ACC_A not in model_policy.load_policy(), str(model_policy.load_policy()))

# ===========================================================================
print("\n=== 8. scope=auto：只放回巡检禁用的，保留人工决策 ===")
model_policy.save_policy({
    ACC_A: {"hy3": "manual", "kimi-k3": "auto"},
})
res = main.restore_account_models({"accountId": ACC_A, "scope": "auto"})
check("只恢复了 auto 那一个", res["restored"] == ["kimi-k3"], str(res["restored"]))
check("★ 手动禁用的 hy3 仍在", "hy3" in res["disabledModels"], str(res["disabledModels"]))
check("hy3 来源仍为 manual",
      res["disabledSources"].get("hy3") == "manual", str(res["disabledSources"]))

# ===========================================================================
print("\n=== 9. 恢复不存在的账号 / 非法 scope 应被拒 ===")
bad_acc = False
try:
    main.restore_account_models({"accountId": "nope-9999"})
except _HTTPException:
    bad_acc = True
check("未知账号被拒", bad_acc)

bad_scope = False
try:
    main.restore_account_models({"accountId": ACC_A, "scope": "everything"})
except _HTTPException:
    bad_scope = True
check("非法 scope 被拒", bad_scope)

# ===========================================================================
print("\n=== 10. 无禁用项时恢复是空操作（不报错）===")
model_policy.save_policy({})
res = main.restore_account_models({"accountId": ACC_A, "scope": "all"})
check("空策略下返回 0", res["restoredCount"] == 0, str(res))
check("空策略下仍返回 ok", res["ok"] is True)

# ===========================================================================
print("\n=== 11. 恢复前留底（改动可回溯）===")
model_policy.save_policy({ACC_B: {"hy3": "manual"}})
main.restore_account_models({"accountId": ACC_B, "scope": "all"})
backup = POLICY_FILE.parent / (POLICY_FILE.name + ".before-restore")
check("生成了恢复前备份", backup.exists(), str(backup))
if backup.exists():
    saved = json.loads(backup.read_text(encoding="utf-8"))
    raw = json.dumps(saved)
    check("备份里保留了被恢复的那一条", "hy3" in raw, raw[:160])

# ===========================================================================
print("\n=== 12. 其它账号的策略不受影响 ===")
model_policy.save_policy({
    ACC_A: {"hy3": "manual"},
    ACC_C: {"glm-4": "manual"},
})
main.restore_account_models({"accountId": ACC_A, "scope": "all"})
policy = model_policy.load_policy()
check("★ 只动了目标账号", ACC_A not in policy, str(policy))
check("★ 别的账号策略原封不动", policy.get(ACC_C) == {"glm-4": "manual"},
      str(policy.get(ACC_C)))

print(f"\n{'=' * 46}")
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("全部通过 ✅")
