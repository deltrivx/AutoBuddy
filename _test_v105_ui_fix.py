#!/usr/bin/env python3
"""v1.0.5 交互缺陷修复回归测试（哥哥 2026-10-20 交办第 2 件）。

三处缺陷（账号页点「启用/停用」时）：
1. **页面抖动**：toggle/pin 成功回调 refreshPool() 把**全部**卡片控制条拆掉
   再异步 fetch 重建 —— 全表 DOM 拆建不但闪动，还会惊动 MutationObserver
   触发整页 run() 重注入，页面在多个状态间重排。
2. **启用相关按钮跑位**：重建的 bar 一律 appendChild 到 section 末尾，
   与官方 React 异步追加内容竞争位置，出现「按钮跑到卡片底部」。
3. **移动端页面直接刷新**：注入的 <button> 没写 type="button"，落在官方
   <form> 内时默认 type=submit，点一下即表单提交整页刷新。

修复方式：
- 抽出 wbBuildPoolBar(acc, data, cardEl)：单卡片控制条构建器，
  toggle / pin 成功后就地 replaceWith 重绘**那一根** bar（原子替换，
  不拆整表、不再二次 fetch —— toggle 与 savePool 响应体本就带最新 status）。
- wbToggleAccount 的 done 回调扩展为 done(ok, status)；
- wbEl 里 button 一律显式 type="button"；裸 createElement("button")
  的 14 处逐一补 .type = "button"。

不依赖 fastapi / httpx / 浏览器 —— 静态断言注入 JS 的结构契约
（与 _test_webui_panels.py 同一手法）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent
SRC = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")

# 提取注入 JS（单个 <script> 块）
blocks = re.findall(r"<script>(.*?)</script>", SRC, re.S)
assert blocks, "注入 script 块不存在"
JS = max(blocks, key=len)

VENDOR = (ROOT / "gateway" / "vendor" / "workbuddy_daily.py").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(label, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label} {detail}")


print("[1] 单卡片就地重绘：wbBuildPoolBar 存在且被三条路径复用")
check("构建器 wbBuildPoolBar 已定义", "function wbBuildPoolBar(acc, data, cardEl)" in JS)
check("首次注入走构建器", JS.count("wbBuildPoolBar(") >= 3,
      f"调用次数 {JS.count('wbBuildPoolBar(')}")
check("就地重绘工具 wbReplacePoolBar 已定义", "function wbReplacePoolBar(" in JS)
check("重绘定位按 accountId 找卡片（不靠位置猜测）",
      'article[data-wb-acc="' in JS and 'setAttribute("data-wb-acc"' in JS)

print("\n[2] toggle / pin 成功后不再全表拆建")
check("toggle 回调不再调用 refreshPool()",
      "wbToggleAccount(acc.id, false, function (ok, status) {" in JS)
check("toggle 响应 status 透传给回调", "done(true, res.body && res.body.status)" in JS)
check("toggle 成功后写缓存", "wbPoolCache = res.body.status" in JS)
check("toggle 路径走 wbReplacePoolBar（原子替换）",
      re.search(r"wbToggleAccount\(acc\.id, false, function \(ok, status\) \{[^}]*wbReplacePoolBar", JS, re.S) is not None)
check("savePool 路径走 wbReplacePoolBar（原子替换）",
      re.search(r"savePool\(\s*[^,]+,\s*function \(body\) \{[^}]*wbReplacePoolBar", JS, re.S) is not None)
count_rpb = len(re.findall(r"wbReplacePoolBar\(", JS))
check("pin（设为首选）路径也走就地重绘", count_rpb >= 3,
      f"出现次数 {count_rpb}")
check("refreshPool 仍保留（切页兜底用），但 toggle/pin 不再调它",
      "function refreshPool()" in JS)

print("\n[3] 注入按钮一律显式 type=\"button\"（防 form 内 submit 整页刷新）")
check("wbEl 对 button 统一设 type", re.search(r'tag === "button"\s*\)\s*el\.type = "button"', JS) is not None)
# 裸 createElement("button") 后必须紧跟 .type = "button"
bare = re.findall(r'(\w+) = document\.createElement\("button"\);\n( +)\1\.type = "button"', JS)
check("14 处裸 createElement button 均已补 .type（含头像上传/模型恢复/控制条等）",
      len(bare) >= 14, f"got {len(bare)}")
# 反向断言：不允许再有「建了 button 但下一行不是 .type」的漏网
# （JS 里写入的换行与源码一致；缩进后必须紧跟 <var>.type = "button"）
leak = re.findall(r'\b(\w+) = document\.createElement\("button"\);\n(?! +\1\.type = "button")', JS)
check("无漏补的裸 button", not leak, f"漏了 {leak[:3]}")

print("\n[4] 旧 bug 形状不得回流")
check("不再有「全表 remove 后重建」的 toggle 回调",
      "function () { wbPoolCache = null; refreshPool(); }" not in JS)
check("wbPinAccountBlocks 仍在（控制条/模型区顺序恒定的既有保障）",
      "function wbPinAccountBlocks()" in JS)

print("\n[5] vendor 脚本为 v3.14（含 500 积分补偿）")
check("vendor 自报 v3.14", "v3.14" in VENDOR[:600], "头部版本行")
check("已下线补偿接口被安全跳过（需求6：claim-compensation 报 400，不再请求）",
      "claim-compensation 接口已彻底下线" in VENDOR and
      "url = BASE + \"/billing/meter/claim-compensation\"" not in VENDOR and
      "s.post(CBDOMAIN + \"/billing/meter/claim-compensation\"" not in VENDOR)
check("旧 v3.7 备份保留", (ROOT / "gateway/vendor").glob and
      any("bak-v3.7" in p.name for p in (ROOT / "gateway" / "vendor").iterdir()))

print("\n" + "=" * 50)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)
