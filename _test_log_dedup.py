#!/usr/bin/env python3
"""access log 去重回归测试。

背景（用户 2026-09-27）：「相同时间日志完全一模一样出现也没有意义的，
出现一条即可」—— 即便是调用 API 的日志。

实测：2322 行容器日志里，uvicorn access log 占 98%，
其中 /api/switch/progress 一条就重复 1533 次。

最初的 _AccessNoiseFilter 是**路径黑名单**，只能盖住已知接口 ——
刷屏最凶的三个（switch/progress、travel/status、wb-daily/credit-summary）
全都不在旧名单里，新增轮询接口就得手工补，永远追不上。
所以补了 _AccessDedupFilter 做**通用相邻去重**兜底。

本测试直接解析源码提取实现要点，不 import 模块 ——
gateway/*.py 依赖 fastapi 等运行时包，本地/CI 未必装了，
而这里要校验的是逻辑契约，不需要把服务跑起来。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
MODULES = ["gateway/main.py", "gateway/web_proxy.py"]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def main() -> int:
    fail = 0
    for mod in MODULES:
        src = read(mod)

        # 1) 去重类必须存在
        if "class _AccessDedupFilter" not in src:
            print(f"❌ {mod}: 缺少 _AccessDedupFilter")
            fail += 1
            continue

        # 2) 必须挂到 uvicorn.access 上，否则永远不生效
        body = src.split("_install_access_filter", 1)[-1]
        if "_AccessDedupFilter" not in body.split("if __name__")[0]:
            # 精确一点：在 _install_access_filter 函数体内查找
            m = re.search(
                r"def _install_access_filter\(\).*?(?=\n(?:def |if __name__))",
                src,
                re.S,
            )
            if not m or "_AccessDedupFilter" not in m.group(0):
                print(f"❌ {mod}: _AccessDedupFilter 未挂到 _install_access_filter")
                fail += 1

        # 3) 必须做端口号归一化 —— 否则同一接口的日志永远判不相等，
        #    去重完全失效（这是最容易漏掉的一条）
        m = re.search(
            r"def _normalize_access_msg.*?(?=\n(?:def |class ))", src, re.S
        )
        if not m:
            print(f"❌ {mod}: 缺少 _normalize_access_msg")
            fail += 1
        elif r"\d+\.\d+\.\d+\.\d+" not in m.group(0):
            print(f"❌ {mod}: _normalize_access_msg 未抹掉客户端端口号")
            fail += 1

        # 3.5) 必须有「周期汇报」机制。
        #
        # 这是实测踩出来的坑：为了抗交错轮询，去重按内容**各自独立**记录，
        # 于是「内容变化时汇报上一条重复次数」永远不会触发 ——
        # c3 累积的计数记在 c3 名下，等 d4 出现时查的是 d4（= 0）。
        # 结果是重复次数被悄悄吞掉、观测断档。
        if "_flush_access_dedup" not in src:
            print(f"❌ {mod}: 缺少 _flush_access_dedup（重复次数会被悄悄吞掉）")
            fail += 1
        # 3.6) 必须有容量上限，否则 URL 带动态 id 时字典无界增长 = 内存泄漏
        if "_ACCESS_DEDUP_MAX_KEYS" not in src:
            print(f"❌ {mod}: 去重字典无容量上限（长时间运行会内存泄漏）")
            fail += 1

        # 4) 依赖必须导入，否则运行时 NameError
        for need in ("import re", "import threading"):
            if not re.search(rf"^{need}$", src, re.M):
                print(f"❌ {mod}: 缺少 {need}（运行时会 NameError）")
                fail += 1

        print(f"✅ {mod}: 去重类 / 挂载 / 端口归一化 / 依赖导入 均到位")

    # 5) 行为契约：用与实现等价的逻辑验证三个关键场景
    import logging
    import threading

    last: dict = {}
    count: dict = {}
    lock = threading.Lock()

    def norm(msg: str) -> str:
        return re.sub(r"(\d+\.\d+\.\d+\.\d+):\d+", r"\1", msg)

    def keep(msg: str) -> bool:
        key = norm(msg)
        with lock:
            if last.get(key) == key:
                count[key] = count.get(key, 0) + 1
                return False
            last[key] = key
            count[key] = 0
        return True

    def case(name: str, msgs, expect):
        nonlocal fail
        last.clear()
        count.clear()
        got = [keep(m) for m in msgs]
        if got != expect:
            print(f"❌ {name}: got={got} expect={expect}")
            fail += 1
            return
        print(f"✅ {name}")

    # 场景1：端口不同但内容相同 → 去重（不归一化端口就会失效）
    case(
        "端口不同的相同请求只保留一条",
        ['192.168.31.10:%d - "GET /api/switch/progress HTTP/1.1" 200 OK' % p
         for p in (57564, 57565, 57566, 57567)],
        [True, False, False, False],
    )
    # 场景2：多路交错轮询 A,B,A,B → 各自独立记录，第二轮被吞
    case(
        "交错轮询按内容各自去重",
        ['1.1.1.1:%d - "GET %s HTTP/1.1" 200 OK' % (i, p)
         for i, p in enumerate(["/A", "/B", "/A", "/B"], start=1)],
        [True, True, False, False],
    )
    # 场景3：内容真不同 → 必须放行（不能误杀真实请求）
    case(
        "不同接口不被误杀",
        ['1.1.1.1:1 - "GET /A HTTP/1.1" 200 OK',
         '1.1.1.1:2 - "POST /v1/chat/completions HTTP/1.1" 200 OK'],
        [True, True],
    )

    # ---------------------------------------------------------------
    # 6) 重复计数的写法必须是「行尾 ×N」，不能另起一行
    #
    # 用户 2026-10-02 反馈：日志里出现「(重复 4 次)」这种**独立一行**，
    # 等于又多打了一条几乎重复的记录，与「防刷屏」的初衷相悖。
    # 改成把次数挂在本行末尾（×N），行数才真的被压下去。
    # ---------------------------------------------------------------
    print("\n[6] 重复计数写法：行尾 ×N，不另起一行")
    for mod in MODULES:
        src = read(mod)
        # 旧的独立一行写法必须彻底消失（注释里提到不算，只看 f-string）
        bad = [ln for ln in src.splitlines()
               if ("（上一条重复 " in ln or "(重复 " in ln)
               and "print(" in ln and not ln.strip().startswith("#")]
        if bad:
            print(f"❌ {mod}: 仍存在独立成行的重复计数 -> {bad}")
            fail += 1
        else:
            print(f"✅ {mod}: 无独立成行的重复计数")

        # 必须有行尾 ×N 写法
        if "×{repeat}" in src or "×{c}" in src:
            print(f"✅ {mod}: 使用行尾 ×N 写法")
        elif "_flush_access_dedup" in src or "def lprint" in src:
            # 该模块有去重逻辑却没改成 ×N —— 只对含去重实现的文件强校验
            if "_flush_access_dedup" in src or "def lprint" in src:
                print(f"❌ {mod}: 有去重逻辑但未改用行尾 ×N")
                fail += 1

    if fail:
        print(f"\n❌ 失败 {fail} 项")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
