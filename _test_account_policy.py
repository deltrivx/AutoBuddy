"""账号级停用策略（account_policy）的单元测试。

这个模块有两个容易出错的地方，各配一条正向断言：
1. ``set_disabled`` 不能在「已停用」时改写来源 —— 否则手动停用的账号
   会被巡检改成 auto，然后下一轮探测通过就被自动放开；
2. ``normalize_policy`` 要能吃下三种历史形状（完整文件 / 裸映射 / 数组），
   数组形状曾被误当成裸映射处理，产生 ``{'disabled': 'manual'}`` 这种鬼条目。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["AB_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(Path(__file__).parent / "gateway"))

import account_policy  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("[1] 归一化：三种历史形状")
check("空输入 -> 空字典", account_policy.normalize_policy(None) == {})
check("完整文件形状", account_policy.normalize_policy(
    {"version": 1, "disabled": {"a1": "auto"}}) == {"a1": "auto"})
check("裸映射形状", account_policy.normalize_policy({"a1": "manual"}) == {"a1": "manual"})
# 数组形状曾被误当裸映射：{'disabled': ['a1']} -> 把 list 当 body 遍历，
# 产出 {'disabled': 'manual'}，账号名变成了字段名。
check("数组形状", account_policy.normalize_policy({"disabled": ["a1", "a2"]})
      == {"a1": "manual", "a2": "manual"})
check("非法来源被归一为 manual",
      account_policy.normalize_policy({"a1": "whatever"}) == {"a1": "manual"})
check("结构体形状取 source",
      account_policy.normalize_policy({"a1": {"source": "auto"}}) == {"a1": "auto"})

print("\n[2] set_disabled 的来源保护")
pol = {}
check("首次停用返回已变更", account_policy.set_disabled(pol, "a1", True,
                                          account_policy.SOURCE_MANUAL) is True)
check("来源记为 manual", pol.get("a1") == "manual", str(pol))
# 正向断言：这是修复前会失败的那条。
check("已停用时重复停用不产生变更",
      account_policy.set_disabled(pol, "a1", True, account_policy.SOURCE_MANUAL) is False)
check("巡检停用不改写已有的 manual 来源",
      account_policy.set_disabled(pol, "a1", True, account_policy.SOURCE_AUTO) is False)
check("manual 来源被保住了", pol.get("a1") == "manual", str(pol))

check("auto 项可被巡检再确认（值不变即无变更）",
      account_policy.set_disabled(pol, "a2", True, account_policy.SOURCE_AUTO) is True)

print("\n[3] 反过来：auto 能升级成 manual，manual 不能降级成 auto")
check("auto -> manual 允许（人的决定更强）",
      account_policy.set_disabled(pol, "a2", True, account_policy.SOURCE_MANUAL) is True)
check("来源已变成 manual", pol.get("a2") == "manual", str(pol))

print("\n[4] 解除停用")
check("解除已停用的返回变更",
      account_policy.set_disabled(pol, "a1", False) is True)
check("解除后不在表里", "a1" not in pol, str(pol))
check("解除未停用的返回无变更",
      account_policy.set_disabled(pol, "zzz", False) is False)

print("\n[5] 落盘与读取（往返一致）")
account_policy.save_policy({})
account_policy.save_policy({"a1": "auto", "a2": "manual"})
raw = json.loads(account_policy.ACCOUNT_POLICY_FILE.read_text(encoding="utf-8"))
check("落盘带 version", raw.get("version") == 1, str(raw))
check("落盘结构为 disabled 映射", raw.get("disabled") == {"a1": "auto", "a2": "manual"},
      str(raw))
check("读回一致", account_policy.load_policy() == {"a1": "auto", "a2": "manual"})

print("\n[6] disabled_ids / as_source_map")
check("disabled_ids 不区分来源",
      account_policy.disabled_ids() == {"a1", "a2"})
check("as_source_map 保留来源",
      account_policy.as_source_map() == {"a1": "auto", "a2": "manual"})

print("\n[7] 文件损坏时降级为空（不能让网关起不来）")
account_policy.ACCOUNT_POLICY_FILE.write_text("{ 这不是 json", encoding="utf-8")
check("损坏文件读成空字典", account_policy.load_policy() == {})

print("\n[8] 备份")
account_policy.save_policy({"a1": "manual"})
backup = account_policy.backup_policy(".bak")
check("留底文件生成", backup is not None and backup.exists(), str(backup))
check("留底内容与原文一致",
      backup is not None and backup.read_text(encoding="utf-8")
      == account_policy.ACCOUNT_POLICY_FILE.read_text(encoding="utf-8"))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
