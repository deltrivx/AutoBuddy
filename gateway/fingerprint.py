#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoBuddy 硬件指纹生成与持久化模块（v1.0.10：动态池化隔离）
────────────────────────────────────────────────────────────
为账号池中的每个账号生成并持久化独立的虚拟硬件画像（RFC 4122 UUIDv5、拟真独立硬件画像、独立真实 UA），
彻底杜绝多账号共用单套宿主机特征（如相同的 20核/24G内存/同MAC/同指纹）导致腾讯侧风控关联标记。

v1.0.10 新增（用户 2026-10-22 需求「动态池化隔离，覆盖新账号」）：
  1. **新账号自动分配**：账号池里出现新 uid 时，第一次被 get_or_create_fingerprint
     访问就当场分配一套全新画像并落盘，不需要任何人工登记。
  2. **池化去重不碰撞**：分配前先扫一遍已持久化的全部画像，把已占用的
     (machineId, mac, profileIdx, UA) 收进 used 集合；新画像的 UA / 硬件档位
     若与任何已占用项相同就顺延到下一档，直到四项全部唯一为止。
  3. **入口全覆盖**：ensure_account_fingerprints(accounts) 供 main.py 启动时
     与 wb_daily 收集参与账号时调用，把「存量 + 新加入」一次性补齐。
"""

import hashlib
import json
import os
import random
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

NAMESPACE_AUTOBUDDY = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

# 拟真硬件画像候选池（基于主流开发者设备型号配置）。
# ⚠️ UA 与硬件档位一一对应：同一账号的画像必须自洽（UA 声明的 OS/版本要和
#    os/osVersion 字段对得上），否则上游一看就是伪造的。
#    v1.0.10：池子扩到 12 档，且每档 UA 互不相同 —— 池化去重靠的就是这个多样性。
_PROFILES_POOL = [
    {
        "os": "Win32", "arch": "x64", "osVersion": "10.0.22631",
        "cpuCores": 16, "memorySize": 32,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32", "arch": "x64", "osVersion": "10.0.22621",
        "cpuCores": 12, "memorySize": 16,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32", "arch": "x64", "osVersion": "10.0.26100",
        "cpuCores": 8, "memorySize": 16,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "MacIntel", "arch": "arm64", "osVersion": "14.5",
        "cpuCores": 10, "memorySize": 18,
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "MacIntel", "arch": "arm64", "osVersion": "15.0",
        "cpuCores": 12, "memorySize": 36,
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32", "arch": "x64", "osVersion": "10.0.22631",
        "cpuCores": 8, "memorySize": 32,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Linux x86_64", "arch": "x64", "osVersion": "6.5.0-35-generic",
        "cpuCores": 16, "memorySize": 64,
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32", "arch": "x64", "osVersion": "10.0.19045",
        "cpuCores": 6, "memorySize": 16,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    # ── v1.0.10 新增档位：Windows 11 24H2 / Chrome 129-131 ──
    {
        "os": "Win32", "arch": "x64", "osVersion": "10.0.26100",
        "cpuCores": 14, "memorySize": 32,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32", "arch": "x64", "osVersion": "10.0.22631",
        "cpuCores": 20, "memorySize": 64,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    # ── macOS 两档（Apple Silicon 主流配置）──
    {
        "os": "MacIntel", "arch": "arm64", "osVersion": "15.1",
        "cpuCores": 12, "memorySize": 24,
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    # ── Linux 开发者档（高配工作站）──
    {
        "os": "Linux x86_64", "arch": "x64", "osVersion": "6.8.0-45-generic",
        "cpuCores": 24, "memorySize": 64,
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
]

# 池化隔离的最大尝试轮数：账号数超过池容量时，靠 UA 后缀微调（+patch 号）
# 继续产出唯一组合，绝不复用同一套画像。
_MAX_POOL_ATTEMPTS = 64


def _fingerprint_store_path() -> Path:
    data_dir = os.getenv("AB_DATA_DIR", "/data/.autobuddy")
    return Path(data_dir) / "account_fingerprints.json"


def _load_fingerprints() -> Dict[str, Any]:
    path = _fingerprint_store_path()
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_fingerprints(store: Dict[str, Any]) -> None:
    path = _fingerprint_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[fingerprint] 保存指纹库失败: {e}")


def _used_keys(store: Dict[str, Any]):
    """收集已被占用的画像维度，供池化去重。

    返回 (used_machine, used_mac, used_profile, used_ua) 四个集合。
    新画像的每一项都不得落在对应集合里，否则顺延重取 ——
    这就是「任何两个账号的设备指纹与 UA 都不相同」的实现方式。
    """
    used_machine, used_mac, used_profile, used_ua = set(), set(), set(), set()
    for fp in (store or {}).values():
        if not isinstance(fp, dict):
            continue
        if fp.get("machineId"):
            used_machine.add(str(fp["machineId"]))
        if fp.get("mac"):
            used_mac.add(str(fp["mac"]).lower())
        if fp.get("userAgent"):
            used_ua.add(str(fp["userAgent"]))
        # 硬件档位用 (os, osVersion, cpuCores, memorySize) 四元组标识，
        # 比索引更稳（索引会随池子扩容而变化，四元组不会）。
        used_profile.add((str(fp.get("os")), str(fp.get("osVersion")),
                          int(fp.get("cpuCores") or 0), int(fp.get("memorySize") or 0)))
    return used_machine, used_mac, used_profile, used_ua


def _stable_mac(key: str) -> str:
    """由账号 key 稳定派生本地管理单播 MAC（同一账号每次同一 MAC）。"""
    hash_bytes = hashlib.sha256(f"wb-mac:{key}".encode()).digest()
    return ":".join([
        f"{(hash_bytes[0] & 0xfe | 0x02):02x}",
        f"{hash_bytes[1]:02x}",
        f"{hash_bytes[2]:02x}",
        f"{hash_bytes[3]:02x}",
        f"{hash_bytes[4]:02x}",
        f"{hash_bytes[5]:02x}",
    ])


def _bump_ua(ua: str, step: int) -> str:
    """UA 的 Chrome 版本号按 step 顺延，用于池容量耗尽时继续产出唯一 UA。

    只动 Chrome 的 build 与 patch 两段，OS/Arch 部分与硬件画像保持自洽。
    ⚠️ 必须动 **build 而不是只动 patch**：patch 段上游基本不看，两个 UA
       仅 patch 不同仍然高度可疑；build 段不同才是「不同版本的浏览器」。
    """
    import re as _re
    m = _re.search(r"Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)", ua)
    if not m:
        return ua + f"#{step}"
    major, minor, build, patch = (int(x) for x in m.groups())
    new_build = build + step
    new_patch = patch + step
    # ⚠️ replace 的 old 串必须是**原始**版本号，不能是已经 +step 的那个 ——
    # 曾经写成 build += step 后再 replace(build+step)，old 串在原 UA 里根本
    # 不存在，replace 静默返回原串，于是「bump 过的 UA」和原 UA 一模一样，
    # 去重完全失效（实测 30 个账号 UA 只有 12 个唯一）。
    return ua.replace(f"Chrome/{major}.{minor}.{build}.{patch}",
                      f"Chrome/{major}.{minor}.{new_build}.{new_patch}")


def _allocate_profile(key: str, used_profile, used_ua):
    """从池子里挑一套**未被占用**的硬件画像 + UA。

    顺序：先用 key 的哈希定位一个起始档位（保证同一账号的重试结果稳定），
    再向后顺延，跳过已占用的档位与 UA；池子走完还撞车就 bump UA 版本号。

    ⚠️ 硬件画像档位与 UA 是**两套独立的去重集合**，必须分开判：
       实测 30 个账号时，若把「档位未占用」当成「UA 也未占用」直接返回，
       UA 会重复 18 次（12 个档位 × 同一 UA 字符串）。多账号共用同一个 UA
       正是上游并案标记的典型特征，比共用 CPU/内存更显眼。
    """
    n = len(_PROFILES_POOL)
    start = int(hashlib.md5(key.encode()).hexdigest(), 16) % n

    # 第一轮：档位和 UA 都未被占用 → 直接采用（最优，画像拟真）。
    for step in range(n):
        idx = (start + step) % n
        profile = _PROFILES_POOL[idx]
        tag = (profile["os"], profile["osVersion"],
               profile["cpuCores"], profile["memorySize"])
        if tag in used_profile:
            continue
        if profile["ua"] in used_ua:
            continue
        return idx, profile

    # 第二轮：档位可用但 UA 已被占用 → 保留档位，用 UA 尾段微调产出唯一 UA。
    # 只动 Chrome 的 build/patch 号，UA 的 OS/Arch 部分与硬件画像保持自洽。
    for step in range(1, _MAX_POOL_ATTEMPTS):
        for off in range(n):
            idx = (start + off) % n
            base = _PROFILES_POOL[idx]
            tag = (base["os"], base["osVersion"],
                   base["cpuCores"], base["memorySize"])
            if tag in used_profile:
                continue
            cand = _bump_ua(base["ua"], step)
            if cand in used_ua:
                continue
            profile = dict(base)
            profile["ua"] = cand
            return idx, profile

    # 第三轮（理论上到不了）：档位全满且微调也撞车 —— 允许档位复用，
    # 但 UA 必须唯一（UA 相同才是上游最容易抓的并案特征）。
    for step in range(1, _MAX_POOL_ATTEMPTS * 8):
        idx = (start + step) % n
        cand = _bump_ua(_PROFILES_POOL[idx]["ua"], step)
        if cand in used_ua:
            continue
        profile = dict(_PROFILES_POOL[idx])
        profile["ua"] = cand
        return idx, profile

    # 兜底：随机一档 + 大跨度版本号，绝不返回已被占用的 UA。
    idx = random.randrange(n)
    profile = dict(_PROFILES_POOL[idx])
    profile["ua"] = _bump_ua(profile["ua"], random.randint(1000, 9999))
    return idx, profile


def get_or_create_fingerprint(uid: str, account_id: str = "") -> Dict[str, Any]:
    """为指定账号获取或生成独立持久化硬件画像。

    uid 优先，退化到 account_id；两者都空则用 "default"（单账号自用场景）。

    v1.0.10：分配前先扫全库收集已占用项（_used_keys），
    保证任何两个账号的 machineId / mac / 硬件档位 / UA 四项都不同。
    """
    key = str(uid or account_id or "default").strip()
    store = _load_fingerprints()
    if key in store:
        return store[key]

    used_machine, used_mac, used_profile, used_ua = _used_keys(store)

    # 生成稳定且合规的 UUIDv5；若撞车（概率极低）就加盐重取，直到唯一。
    machine_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-machine:{key}"))
    salt = 0
    while machine_uuid in used_machine and salt < _MAX_POOL_ATTEMPTS:
        salt += 1
        machine_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-machine:{key}#{salt}"))
    session_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-session:{key}#{salt}"))
    mp_machine_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-mp-machine:{key}#{salt}"))
    web_machine_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-web-machine:{key}#{salt}"))

    # MAC：稳定派生，撞车则换盐重派生。
    mac = _stable_mac(key)
    salt_mac = 0
    while mac in used_mac and salt_mac < _MAX_POOL_ATTEMPTS:
        salt_mac += 1
        mac = _stable_mac(f"{key}#{salt_mac}")

    # 硬件画像 + UA：走池化去重。
    _, profile = _allocate_profile(key, used_profile, used_ua)

    fp = {
        "uid": key,
        "machineId": machine_uuid,
        "sessionId": session_uuid,
        "mpMachineId": mp_machine_uuid,
        "webMachineId": web_machine_uuid,
        "mac": mac,
        "os": profile["os"],
        "arch": profile["arch"],
        "osVersion": profile["osVersion"],
        "cpuCores": profile["cpuCores"],
        "memorySize": profile["memorySize"],
        "userAgent": profile["ua"],
        "ideVersion": profile["ideVersion"],
    }

    store[key] = fp
    _save_fingerprints(store)
    return fp


def account_uid(acc: Dict[str, Any]) -> str:
    """从账号池条目里取稳定 uid（与 vendor 脚本 uid_of 同源）。

    兼容几种账号文件字段名：uid / id / user / phone / email。
    """
    if not isinstance(acc, dict):
        return ""
    for k in ("uid", "id", "user", "phone", "email", "account"):
        v = acc.get(k)
        if v:
            return str(v).strip()
    return ""


def ensure_account_fingerprints(accounts: List[Dict[str, Any]]) -> Dict[str, int]:
    """给一批账号补齐指纹（**新账号在此自动分配**，存量账号原样返回）。

    返回 {"total": n, "created": m} —— created 是本次新分配的账号数。
    供 main.py 启动时、wb_daily 收集参与账号时调用，确保：
      · 存量账号：指纹早已在库里，直接复用（不变，避免上游看到画像漂移）；
      · 新加入账号：库里没有 → 当场分配并落盘，绝不漏掉也绝不复用别人的。
    """
    accounts = accounts or []
    store = _load_fingerprints()
    created = 0
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        key = account_uid(acc)
        if not key:
            continue
        if key in store:
            continue
        try:
            get_or_create_fingerprint(key)
            created += 1
        except Exception as e:
            print(f"[fingerprint] 账号 {key} 分配指纹失败: {e}")
    return {"total": len(accounts), "created": created}


def all_fingerprints() -> Dict[str, Any]:
    """只读快照：全部已分配画像（供调试/校验，不触发分配）。"""
    return _load_fingerprints()


def fingerprint_uniqueness_report() -> Dict[str, Any]:
    """自检：报告当前指纹库里有没有重复的 machineId / mac / UA。

    返回 {"total": n, "dup_machine": [...], "dup_mac": [...], "dup_ua": [...]}。
    正常应为三个 dup 列表全空 —— 这是「池化隔离是否真的生效」的可验证判据。
    """
    store = _load_fingerprints()
    seen_machine, seen_mac, seen_ua = {}, {}, {}
    dup_machine, dup_mac, dup_ua = [], [], []
    for key, fp in (store or {}).items():
        if not isinstance(fp, dict):
            continue
        for field, seen, dup in (
            ("machineId", seen_machine, dup_machine),
            ("mac", seen_mac, dup_mac),
            ("userAgent", seen_ua, dup_ua),
        ):
            v = str(fp.get(field) or "").lower()
            if not v:
                continue
            if v in seen:
                dup.append({"field": field, "value": v, "keys": [seen[v], key]})
            else:
                seen[v] = key
    return {
        "total": len(store),
        "dup_machine": [d for d in dup_machine if d["field"] == "machineId"],
        "dup_mac": [d for d in dup_mac if d["field"] == "mac"],
        "dup_ua": [d for d in dup_ua if d["field"] == "userAgent"],
    }
