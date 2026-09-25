#!/usr/bin/env python3
"""测试账号删除联动与幽灵账号彻底清除机制"""
import os, sys, json, tempfile

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

tmp = tempfile.mkdtemp()
os.environ["AB_DATA_DIR"] = tmp

from gateway import main

# 模拟 3 个账号
acc1 = {"id": "acc-1", "name": "账号1", "variant": "ai"}
acc2 = {"id": "acc-2", "name": "账号2", "variant": "ai"}
acc3 = {"id": "acc-3", "name": "账号3", "variant": "cn"}
main._save_accounts([acc1, acc2, acc3])

# 初始化账号池配置，开启白名单
main._save_pool_config({
    "mode": "manual",
    "manualAccountId": "acc-2",
    "enabledAccountIds": ["acc-1", "acc-2", "acc-3"]
})

print("[1] 测试删除账号时的联动清理")
# 执行删除 acc-2
res = main.delete_account_api({"accountId": "acc-2"})
check("删除接口成功", res.get("ok") is True)

cfg = main._load_pool_config()
check("enabledAccountIds 中已被剔除 acc-2", "acc-2" not in cfg["enabledAccountIds"])
check("剩余白名单正确", cfg["enabledAccountIds"] == ["acc-1", "acc-3"])
check("manualAccountId 原为 acc-2，已被自动重置为 None", cfg["manualAccountId"] is None)
check("模式自动退回 auto", cfg["mode"] == "auto")

print("\n[2] 测试 _load_pool_config 的被动自愈机制")
# 手动往配置文件写入一个未知的幽灵 ID
cfg_corrupt = {
    "mode": "manual",
    "manualAccountId": "ghost-999",
    "enabledAccountIds": ["acc-1", "ghost-999", "acc-3"]
}
main._save_pool_config(cfg_corrupt)

# 读取时应被自愈
healed = main._load_pool_config()
check("读取配置自动剔除 ghost-999", "ghost-999" not in healed["enabledAccountIds"])
check("白名单修复为已知账号", healed["enabledAccountIds"] == ["acc-1", "acc-3"])
check("首选账号是幽灵账号时被自动重置", healed["manualAccountId"] is None)
check("模式从 manual 恢复为 auto", healed["mode"] == "auto")

print("\n[3] 测试 update_account_pool 更新时的容错与通用性")
# 前端即便由于缓存带着 ghost 提交，也不会报 400，并自动剔除
res_update = main.update_account_pool({
    "mode": "manual",
    "manualAccountId": "acc-1",
    "enabledAccountIds": ["acc-1", "ghost-888"]
})
check("更新未报错 400", res_update.get("ok") is True)
check("保存结果中不含幽灵 ID", "ghost-888" not in res_update["config"]["enabledAccountIds"])
check("首选账号正常设为 acc-1", res_update["config"]["manualAccountId"] == "acc-1")

print("\n" + "=" * 50)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
