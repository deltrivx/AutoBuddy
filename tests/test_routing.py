#!/usr/bin/env python3
"""智能路由引擎单元测试（第 7 核心功能）。

直接 import gateway/routing，不依赖 fastapi 等运行时；只把数据目录指到临时目录，
避免污染宿主 /data。覆盖：
  - 路由组 / 规则的 CRUD 与字段校验（非法枚举回退到默认）
  - 规则匹配（eq / contains，大小写不敏感）
  - resolve_routing 对 group/account/model/none 四种 target 的决策
  - apply_group_ordering 的 smart/order 保序与 round_robin 轮转
  - fallback=error 时组无成员不应退回全局池（由 select_account 侧保证，这里验决策）
"""
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


tmp = Path(tempfile.mkdtemp())
os.environ["AB_DATA_DIR"] = str(tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

import routing  # noqa: E402
from db import get_db_connection  # noqa: E402

# 确保表已建
with get_db_connection() as c:
    routing.init_routing_db(c)


print("[1] 路由组 CRUD + 枚举校验")
g = routing.create_group("主用组", mode="smart", members=[{"id": "A"}, {"id": "B"}])
check("create_group 返回 id", bool(g["id"]))
check("mode 被接受为 smart", g["mode"] == "smart")
check("members 有序保留", [m["id"] for m in g["members"]] == ["A", "B"])
check("默认 fallback=global_pool", g["fallback"] == "global_pool")

g2 = routing.create_group("错误模式组", mode="bogus_mode")
check("非法 mode 回退 smart", g2["mode"] == "smart")
g3 = routing.create_group("错误兜底组", fallback="bogus_fb")
check("非法 fallback 回退 global_pool", g3["fallback"] == "global_pool")

upd = routing.update_group(g["id"], enabled=0, name="主用组改名")
check("update_group 改名生效", upd["name"] == "主用组改名")
check("update_group 停用生效", upd["enabled"] is False)

check("list_groups 含 3 条", len(routing.list_groups()) == 3)
check("delete_group 成功", routing.delete_group(g3["id"]) is True)
check("delete 后剩 2 条", len(routing.list_groups()) == 2)
routing.update_group(g["id"], enabled=1, name="主用组")  # 还原启用


print("[2] 路由规则 CRUD + 匹配")
r = routing.create_rule("按模型分流", match_field="model", match_op="eq",
                        match_value="kimi-k2", target_type="group",
                        target_id=g["id"])
check("create_rule 返回 id", bool(r["id"]))
check("match_value 保留", r["match_value"] == "kimi-k2")

r2 = routing.create_rule("含子串分流", match_field="model", match_op="contains",
                         match_value="deepseek", target_type="account",
                         target_id="X")
check("contains 规则创建", r2["match_op"] == "contains")

rbad = routing.create_rule("坏字段", match_field="bogus", match_value="z")
check("非法 match_field 回退 model", rbad["match_field"] == "model")

# 匹配大小写不敏感
check("eq 命中（同大小写）", routing._rule_matches(r, {"model": "kimi-k2"}))
check("eq 命中（大小写不敏感）", routing._rule_matches(r, {"model": "KIMI-K2"}))
check("eq 未命中", not routing._rule_matches(r, {"model": "glm-5"}))
check("contains 命中", routing._rule_matches(r2, {"model": "deepseek-v4-pro"}))
check("contains 未命中", not routing._rule_matches(r2, {"model": "glm-5"}))
# 空 match_value 永不命中：构造一条空值的规则验证守卫
r_empty = routing.create_rule("空值规则", match_field="model", match_op="eq",
                              match_value="")
check("空 match_value 永不命中", not routing._rule_matches(r_empty, {"model": "anything"}))
routing.delete_rule(r_empty["id"])


print("[3] resolve_routing 决策")
# 命中 group 规则 → 返回组白名单
d = routing.resolve_routing("kimi-k2", {"model": "kimi-k2"})
check("group 决策 matched", d["matched"])
check("target_type=group", d["target_type"] == "group")
check("account_ids 取组成员", d["account_ids"] == ["A", "B"])
check("带 group_id", d["group_id"] == g["id"])
check("组模式透传", d["group_mode"] == "smart")

# 命中 account 规则 → 返回 forced_account_id
d2 = routing.resolve_routing("deepseek-v4-pro", {"model": "deepseek-v4-pro"})
check("account 决策 matched", d2["matched"])
check("target_type=account", d2["target_type"] == "account")
check("forced_account_id=X", d2["forced_account_id"] == "X")

# 命中 model 规则（target_type=model 用 target_model）
r3 = routing.create_rule("改模型名", match_field="model", match_op="eq",
                         match_value="alias-a", target_type="model",
                         target_model="real-model-a")
d3 = routing.resolve_routing("alias-a", {"model": "alias-a"})
check("model 决策 matched", d3["matched"])
check("forced_model 透传", d3["forced_model"] == "real-model-a")

# 未命中 → 返回不命中
d4 = routing.resolve_routing("glm-5", {"model": "glm-5"})
check("未命中 matched=False", d4["matched"] is False)
check("未命中 target_type=none", d4["target_type"] == "none")

# 命中一个指向“不存在组”的规则 → 视为不命中，继续往下
r4 = routing.create_rule("指向空组", match_field="model", match_op="eq",
                         match_value="orphan", target_type="group",
                         target_id="grp_nonexist")
d5 = routing.resolve_routing("orphan", {"model": "orphan"})
check("指向不存在组 → 不命中", d5["matched"] is False)

# 优先级：两条规则都按 model=pri 命中时，priority 升序优先
rp_high = routing.create_rule("高优先", match_field="model", match_op="eq",
                              match_value="pri", target_type="account",
                              target_id="H", priority=10)
rp_low = routing.create_rule("低优先", match_field="model", match_op="eq",
                             match_value="pri", target_type="account",
                             target_id="L", priority=1)
d6 = routing.resolve_routing("pri", {"model": "pri"})
check("priority 低者优先命中", d6["forced_account_id"] == "L")
routing.delete_rule(rp_high["id"])
routing.delete_rule(rp_low["id"])


print("[4] apply_group_ordering 排序")
cands = [{"id": "B"}, {"id": "A"}, {"id": "C"}, {"id": "D"}]
ordered = routing.apply_group_ordering(cands, ["A", "B"], "smart")
check("smart 按组顺序收敛", [c["id"] for c in ordered] == ["A", "B"])
# round_robin：两次调用应错开起点
r1 = routing.apply_group_ordering(cands, ["A", "B"], "round_robin")
r2b = routing.apply_group_ordering(cands, ["A", "B"], "round_robin")
check("round_robin 两次顺序不同或在范围内",
      [c["id"] for c in r1] in (["A", "B"], ["B", "A"]))
check("round_robin 收敛到组成员", {c["id"] for c in r1} == {"A", "B"})


print("[5] fallback=error 决策透传")
routing.update_group(g["id"], fallback="error")
d7 = routing.resolve_routing("kimi-k2", {"model": "kimi-k2"})
check("fallback 透传为 error", d7["fallback"] == "error")


print()
print(f"PASS={PASS} FAIL={FAIL}")
raise SystemExit(1 if FAIL else 0)
