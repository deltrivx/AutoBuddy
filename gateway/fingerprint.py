#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoBuddy 硬件指纹生成与持久化模块
────────────────────────────────────────────────────────────
为账号池中的每个账号生成并持久化独立的虚拟硬件画像（RFC 4122 UUIDv5、拟真独立硬件画像、独立真实 UA），
彻底杜绝多账号共用单套宿主机特征（如相同的 20核/24G内存/同MAC/同指纹）导致腾讯侧风控关联标记。
"""

import hashlib
import json
import os
import random
import uuid
from pathlib import Path
from typing import Dict, Any

NAMESPACE_AUTOBUDDY = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

# 拟真硬件画像候选池（基于主流开发者设备型号配置）
_PROFILES_POOL = [
    {
        "os": "Win32",
        "arch": "x64",
        "osVersion": "10.0.22631",
        "cpuCores": 16,
        "memorySize": 32,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32",
        "arch": "x64",
        "osVersion": "10.0.22621",
        "cpuCores": 12,
        "memorySize": 16,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32",
        "arch": "x64",
        "osVersion": "10.0.26100",
        "cpuCores": 8,
        "memorySize": 16,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "MacIntel",
        "arch": "arm64",
        "osVersion": "14.5",
        "cpuCores": 10,
        "memorySize": 18,
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "MacIntel",
        "arch": "arm64",
        "osVersion": "15.0",
        "cpuCores": 12,
        "memorySize": 36,
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32",
        "arch": "x64",
        "osVersion": "10.0.22631",
        "cpuCores": 8,
        "memorySize": 32,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Linux x86_64",
        "arch": "x64",
        "osVersion": "6.5.0-35-generic",
        "cpuCores": 16,
        "memorySize": 64,
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    },
    {
        "os": "Win32",
        "arch": "x64",
        "osVersion": "10.0.19045",
        "cpuCores": 6,
        "memorySize": 16,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "ideVersion": "5.5.6",
    }
]

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

def get_or_create_fingerprint(uid: str, account_id: str = "") -> Dict[str, Any]:
    """为指定账号获取或生成独立持久化硬件画像。
    
    使用 RFC 4122 标准 UUIDv5，以稳定 seed 结合账号标识派生；
    绑定独立的 machineId、mac、独立虚拟 CPU/内存/OS 与真实独立 UA。
    """
    key = str(uid or account_id or "default").strip()
    store = _load_fingerprints()
    if key in store:
        return store[key]

    # 生成稳定且合规的 UUIDv5
    machine_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-machine:{key}"))
    session_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-session:{key}"))
    mp_machine_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-mp-machine:{key}"))
    web_machine_uuid = str(uuid.uuid5(NAMESPACE_AUTOBUDDY, f"wb-web-machine:{key}"))

    # 生成拟真稳定的 MAC 地址（单播、本地管理）
    hash_bytes = hashlib.sha256(f"wb-mac:{key}".encode()).digest()
    mac = ":".join([
        f"{(hash_bytes[0] & 0xfe | 0x02):02x}",
        f"{hash_bytes[1]:02x}",
        f"{hash_bytes[2]:02x}",
        f"{hash_bytes[3]:02x}",
        f"{hash_bytes[4]:02x}",
        f"{hash_bytes[5]:02x}"
    ])

    # 选择硬件档案
    idx = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(_PROFILES_POOL)
    profile = dict(_PROFILES_POOL[idx])

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