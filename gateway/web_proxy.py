import db
try:
    from paths import get_switch_file          # gateway/ 在 sys.path 上
except ImportError:                            # 仓库根在 sys.path 上
    from gateway.paths import get_switch_file
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, Response
import httpx
import uvicorn

try:
    from gateway import account_policy
except ImportError:
    import account_policy

try:
    from gateway import refresh_guard
except ImportError:  # pragma: no cover - 取决于运行方式
    import refresh_guard  # type: ignore[no-redef]

try:
    from gateway import notify as _notify
except ImportError:
    import notify as _notify  # type: ignore[no-redef]

# 风控标记的清除走转发到网关本体（gateway/main.py 的
# /account-pool/risk-flag/clear），不在这里另写一份实现 ——
# 状态文件读写逻辑只有一处，避免两份实现漂移。
try:
    from gateway import risk_flag as _risk_flag
except ImportError:  # pragma: no cover - 取决于运行方式
    import risk_flag as _risk_flag  # type: ignore[no-redef]

try:
    from gateway.token_tracker import (get_aggregated_token_stats,
                                       load_tracker_records)
except ImportError:
    from token_tracker import (get_aggregated_token_stats,
                               load_tracker_records)

app = FastAPI()

# ---------------------------------------------------------------------------
# 容器内的内部调用（WebUI 代理 -> 官方后端 57890 / -> 网关 18091）全部走 loopback。
#
# httpx 默认 trust_env=True，会读取宿主机的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
# NAS 上为了下载或科学上网设了全局代理的环境很常见，一旦被代理接管，内部调用会被
# 转发到代理上并返回 404 —— 表现为「UI 打不开 / 模型清单为空」，但网关本身是好的。
# 内部调用一律 trust_env=False，绕开代理环境变量。
#
# 注意：网关（main.py）访问上游模型服务的客户端**不能**这样改，
# 那里恰恰需要代理环境变量才能出网。
# ---------------------------------------------------------------------------
def _internal_client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, trust_env=False)



BACKEND_URL = "http://127.0.0.1:57890"
ICON_PATH = Path("/app/icon.png")

COLLAPSE_SCRIPT = r"""
<style>
  /* ⚠️ 这里**不能**再强制 position: relative ——
     新设计把 aside 改成 position: sticky 的顶部横条（见下面「顶栏」块），
     而旧规则 `position: relative !important` 特异性 (0,0,1) + !important
     与 sticky 那条同级，后声明者胜；但两条分散在文件里、顺序脆弱，
     实测出现过面板 absolute 锚点错乱（面板跑到右上角 y=-73）。
     结论：旧规则只保留 transition，position 完全交给新设计那一条管。 */
  aside {
    transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1), padding 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
  }
  #wb-collapse-btn {
    position: absolute;
    right: -12px;
    top: 20px;
    z-index: 50;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 1px solid rgba(120, 120, 120, 0.2);
    background-color: var(--background, #ffffff);
    color: var(--foreground, #374151);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    box-shadow: 0 1px 4px rgba(0,0,0,0.12);
    transition: all 0.2s ease;
    padding: 0;
  }
  #wb-collapse-btn:hover {
    background-color: rgba(120, 120, 120, 0.1);
    transform: scale(1.08);
  }
  /* 切换视图时纵向滚动条出现/消失会让内容区宽度突变，视觉上就是「页面横向漂移」。
     scrollbar-gutter: stable 为滚动条预留固定槽位，消除这个抖动。 */
  html { scrollbar-gutter: stable; }

  /* 右下角用户球（头像 + 昵称，窄屏只留头像）与弹出层 */
  #wb-user-orb {
    position: fixed;
    right: 18px;
    bottom: 18px;
    z-index: 9800;
    display: flex;
    align-items: center;
    gap: 0;
    padding: 0;
    width: 44px;
    height: 44px;
    justify-content: center;
    border-radius: 50%;
    background: var(--card, #ffffff);
    border: 1px solid var(--border, rgba(120,120,120,0.25));
    box-shadow: 0 6px 18px rgba(0,0,0,0.14);
    cursor: pointer;
    user-select: none;
    -webkit-user-select: none;
    overflow: hidden;
    transition: width 0.18s ease, border-radius 0.18s ease, padding 0.18s ease, gap 0.18s ease;
  }
  /* 展开态：改回胶囊并露出昵称（点击或悬浮均可） */
  #wb-user-orb.wb-orb-open {
    width: auto;
    height: 44px;
    padding: 0 14px 0 5px;
    gap: 8px;
    border-radius: 999px;
  }
  #wb-user-orb img {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    object-fit: cover;
    display: block;
    flex-shrink: 0;
    margin: 0;
  }
  #wb-user-orb .wb-orb-nick {
    display: none;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--foreground, #0f172a);
  }
  /* 平时只显示头像；点击展开浮窗时才露出昵称 */
  #wb-user-orb.wb-orb-open .wb-orb-nick { display: inline; }
  @media (max-width: 720px) {
    #wb-user-orb { width: 40px; height: 40px; right: 14px; bottom: 14px; }
    #wb-user-orb img { width: 32px; height: 32px; }
  }
  /* 官方图标是方形 App 图标：后端已用圆形 SVG 出口兜底，前端只负责保持正圆 */
  img.wb-avatar-round {
    border-radius: 50% !important;
    object-fit: cover;
    background: var(--muted, rgba(120,120,120,0.12));
    box-sizing: border-box;
  }
  /* 右下角浮窗：有意义的信息行（剩余积分 / 今日消耗 / 账号数量） */
  .wb-pop-stats { margin-top: 10px; display: flex; flex-direction: column; gap: 7px; }
  /* 密码掩码：等宽字体保证 * 等宽，视觉上能反映真实位数 */
  .wb-pwd-mask { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; letter-spacing: 2px; color: var(--muted-foreground, #64748b); }
  /* 统计行：左侧标签严格左对齐、右侧数值严格右对齐，
     并用 baseline 让文字基线在同一条线上（此前 align-items:center 在
     标签/数值字号不同时会视觉错位 —— 用户 2026-09-27 反馈「左侧内容未对齐」）。 */
  .wb-pop-stat { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; font-size: 12px; padding: 0; }
  .wb-pop-stat-k { color: var(--muted-foreground, #64748b); flex: 0 0 auto; text-align: left; }
  .wb-pop-stat-v { font-weight: 600; font-variant-numeric: tabular-nums; flex: 0 0 auto; text-align: right; margin-left: auto; }
  /* Buddy 旅行三态徽章：同一枚官方飛機图标，仅以「线型 + 颜色」区分状态。
     无 Buddy = 虚线灰 / 未旅行 = 实线中性 / 旅行中 = 实线主题色 */
  .wb-nobuddy-badge {
    display: inline-flex !important;
    align-items: center;
    gap: 4px;
    border: none !important;
    border-radius: 999px;
    padding: 2px 4px;
    vertical-align: middle;
  }
  /* 无 Buddy：虚线描边（未领养，无伙伴）——灰调 */
  .wb-buddy-badge { display: inline-flex !important; align-items: center; gap: 4px; vertical-align: middle; }
  .wb-buddy-badge svg { flex-shrink: 0; }

  /* 账号卡片右上角的两个状态图标（旅行 / 签到）—— 移动端强制与 PC 一致。
     根因（用户 2026-09-27 反馈「PC 正常、移动端都不显示」）：
     官方前端给这两个容器加了 `hidden ... min-[420px]:flex`，
     即默认 display:none、只有视口 ≥420px 才 flex。
     而手机竖屏宽度普遍是 390 / 375 / 360，全都低于 420，
     于是官方自己的响应式规则把它们藏了 —— 并非我们注入的代码有问题。
     实测：1280px 下 display=flex（可见），390px 下 display=none（不可见）。
     这里无条件覆盖为 flex，使移动端与 PC 显示同步。 */
  [class*="min-[420px]:flex"] { display: flex !important; }

  /* ---------------- 每日任务页「积分统计」：1:1 复刻官方积分统计页 ----------------
     官方实测结构/取值（/credit-stats，1280px 视口）：
       卡片  rounded-2xl(16px) + border 1px + bg-card/70 + overflow:hidden（flex-col）
       网格  grid grid-cols-1 sm:grid-cols-4 + sm:py-5（20px 0）
       单项  flex flex-col items-center justify-center px-4 py-5 sm:py-3 text-center
             非首项 sm:border-l 分隔
       标签  13px / font-weight 500 / leading-20px / muted-foreground
       数值  mt-3(12px) 26px / 600 / leading-32px / tracking -0.025em / tabular-nums
     -- 之前用的是自造的 flex-wrap + 18px 方块 + 各自着色，并非官方结构。 */
  .wb-dl-credit-card {
    display: flex; flex-direction: column; min-width: 0;
    border: 1px solid var(--border, rgba(120,120,120,.25));
    border-radius: 16px; overflow: hidden;
    /* 背景必须跟随主题变量，不能写死白色。

       此前这里是 background: rgba(255,255,255,.7) —— 硬编码白色，
       于是深色主题下页面整体变暗、文字与边框都跟着变了，
       唯独这个四项数据框仍是一块刺眼的白底（用户 2026-09-28 反馈）。

       实测（1280px，深色主题）：
         --card      = oklch(27.9% .041 260.031)   ← 主题变量本身是正常的
         body 背景    = oklch(0.208 0.042 265.755)
         本框背景    = rgba(255,255,255,0.7)        ← 没跟上，就是它

       注意 --card 是 **oklch** 而非 hex，所以兜底值不能用
       color-mix(srgb ...) 配 hex 的老写法，直接用 color-mix 取 70% 即可；
       变量缺失时退回半透明中性灰，避免又变回固定白。 */
    background: color-mix(in oklab, var(--card, #ffffff) 70%, transparent);
  }
  .wb-dl-credit-grid { display: grid; grid-template-columns: minmax(0, 1fr); padding: 0; }
  .wb-dl-credit-item {
    display: flex; flex-direction: column; min-width: 0;
    align-items: center; justify-content: center;
    padding: 20px 16px; text-align: center;
  }
  .wb-dl-credit-k {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    max-width: 100%; font-size: 13px; font-weight: 500; line-height: 20px;
    color: var(--muted-foreground, #64748b); text-align: center;
  }
  .wb-dl-credit-icon {
    width: 16px; height: 16px; flex: 0 0 auto;
    color: var(--muted-foreground, #64748b); stroke: currentColor;
  }
  .wb-dl-credit-v {
    margin-top: 12px; max-width: 100%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-size: 26px; font-weight: 600; line-height: 32px; letter-spacing: -0.025em;
    font-variant-numeric: tabular-nums; color: var(--foreground, #0f172a);
  }
  @media (min-width: 640px) {
    .wb-dl-credit-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); padding: 20px 0; }
    .wb-dl-credit-item { padding: 12px 16px; border-left: 1px solid rgba(120,120,120,.18); }
    .wb-dl-credit-item:first-child { border-left: 0; }
  }
  .wb-buddy-nobuddy { color: #94a3b8; }
  .wb-buddy-nobuddy svg { stroke-dasharray: 3 2.4; }
  /* 未旅行：实线（已领养，伙伴在家待命） */
  .wb-buddy-idle { color: var(--muted-foreground, #64748b); }
  /* 旅行中：实线 + 主题色（伙伴出门旅行） */
  .wb-buddy-traveling { color: var(--primary, #3b82f6); }
  #wb-user-pop {
    position: fixed;
    right: 18px;
    bottom: 64px;
    z-index: 9801;
    width: 264px;
    background: var(--card, #ffffff);
    color: var(--foreground, #0f172a);
    border: 1px solid var(--border, rgba(120,120,120,0.25));
    border-radius: 14px;
    box-shadow: 0 14px 34px rgba(0,0,0,0.2);
    padding: 14px;
    font-size: 12.5px;
  }
  @media (max-width: 720px) { #wb-user-pop { right: 12px; bottom: 60px; } }
  .wb-pop-head { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--border, rgba(120,120,120,0.18)); }
  .wb-pop-head img { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; }
  .wb-pop-menu { display: flex; flex-direction: column; gap: 2px; margin-top: 8px; }
  /* 退出登录等菜单项：视觉样式保持不变，但文字左边缘必须与上方统计行的
     标签左边缘对齐 —— 原来 padding 8px 10px 让文字比上面缩进 10px
     （用户 2026-09-27 反馈「退出登录文字未和上方文字左边对齐」）。 */
  .wb-pop-item { display: block; width: 100%; text-align: left; background: transparent; border: none; padding: 8px 0; border-radius: 8px; font-size: 12.5px; color: inherit; cursor: pointer; }
  .wb-pop-item:hover:not(:disabled) { background: var(--muted, rgba(120,120,120,0.12)); }
  .wb-pop-item:disabled { opacity: 0.5; cursor: not-allowed; }
  .wb-pop-item.danger { color: #ef4444; }
  .wb-modal-input { width: 100%; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border, rgba(120,120,120,0.3)); background: var(--background, #ffffff); color: inherit; font-size: 13px; box-sizing: border-box; }
  .wb-modal-btn { width: 100%; padding: 9px; border-radius: 8px; background: #3b82f6; color: #ffffff; font-size: 13px; font-weight: 600; border: none; cursor: pointer; }
  .wb-modal-btn:disabled { opacity: 0.6; cursor: default; }
  .wb-modal-err { color: #ef4444; font-size: 12px; display: none; }
  /* 头像预览独占一行并放大（用户 2026-09-26）：
     之前预览与选择器并排，52px 挤在左侧，看不清换成什么样。
     现在预览居中独占一行 88px，选择器整行排在下方，视线从上到下走。 */
  .wb-avatar-pick {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
  }
  .wb-avatar-pick img {
    width: 88px;
    height: 88px;
    border-radius: 50%;
    object-fit: cover;
    border: 2px solid var(--border, rgba(120,120,120,0.3));
    box-shadow: 0 4px 14px rgba(0,0,0,0.10);
    flex-shrink: 0;
  }
  /* 选择器整行：预览下方，桌面端居中排布 */
  .wb-avatar-pick .wb-avatar-grid { justify-content: center; }
  .wb-avatar-grid { display: flex; flex-wrap: wrap; gap: 8px; }
  /* 头像选择器与「上传自定义头像」共用同一行：
     官方 + 6 个预设 = 7 个 42px 圆钮，加上上传钮刚好排满一行，
     上传钮不再被压到选择器下方单独占一行。
     wrap 保留：窄屏（手机）放不下时自然换行，不会挤出容器。 */
  .wb-avatar-grid-inline { flex: 1; min-width: 0; align-items: center; }
  .wb-avatar-upload {
    width: 42px !important;
    height: 42px !important;
    border-radius: 50% !important;
    padding: 0 !important;
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    line-height: 1;
    color: var(--muted-foreground, #64748b);
    border: 2px dashed var(--border, rgba(120,120,120,0.45)) !important;
    background: transparent !important;
    cursor: pointer;
    box-sizing: border-box;
    flex-shrink: 0;
    transition: color .15s, border-color .15s;
  }
  .wb-avatar-upload:hover {
    color: var(--primary, #3b82f6);
    border-color: var(--primary, #3b82f6) !important;
  }
  .wb-avatar-opt { width: 42px; height: 42px; border-radius: 50%; border: 2px solid transparent; padding: 0; background: transparent; cursor: pointer; overflow: hidden; box-sizing: border-box; flex-shrink: 0; }
  .wb-avatar-opt img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; display: block; }
  .wb-avatar-opt.active { border-color: #3b82f6; }
  .wb-avatar-pick button { line-height: 1; }
  /* 隐藏桌面端专有无法在容器执行的操作按钮与完全磁盘访问提示块 */
  .wb-mac-btn-hide,
  .wb-mac-block-hide {
    display: none !important;
  }
  /* 账号池控制条：启用开关 + 设为首选 */
  .wb-pool-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
  }
  /* 2026-10-21 工单第 2 件：「账号卡片启用时按钮跑到卡片底部」。
     根因是这条控制条的 flex-wrap:wrap 遇上**启用前后按钮文案长度变化**：
     「不在账号池 · 点击启用」(9 字) → 「参与调用 · 点击停用」(8 字)，
     「已停用 · 点击启用」(7 字) → 同上，宽度一改，折行点跟着改，
     后面的按钮/计数就被甩到下一行 —— 用户看到的就是「按钮跑到卡片底部」。
     移动端（390px 视口、约 310px 内容宽）上 4 个元素本来就要折行，
     这个效应最明显。三层修法（都不动 DOM 结构）：
       ① 按钮一律 flex:0 0 auto + nowrap：不被压缩变形，也不自行断行；
       ② toggle 按钮给 min-width：三种状态宽度取齐，折行点不随状态跳；
       ③ 计数/「最近调用」改 margin-left:auto 独占右侧：换行只发生在
          「按钮组 ↔ 统计」之间，按钮组三个的相对位置永远不变。 */
  .wb-pool-btn {
    font-size: 11px;
    line-height: 1.8;
    padding: 1px 10px;
    border-radius: 999px;
    border: 1px solid rgba(120, 120, 120, 0.35);
    background: transparent;
    color: var(--foreground, #374151);
    cursor: pointer;
    /* ① 不参与压缩、不自行断行（前述工单） */
    flex: 0 0 auto;
    white-space: nowrap;
  }
  /* ② 启用/停用主按钮：三种文案（不在账号池·点击启用 / 已停用·点击启用 /
     参与调用·点击停用）统一到一个最小宽度，切换状态时该按钮宽度不变，
     后面的按钮与计数不会被推着换行。取 118px：够放最长的
     「不在账号池 · 点击启用」，PC 与移动端一致（移动端靠字号微调补）。 */
  .wb-pool-bar > .wb-pool-btn:first-child {
    min-width: 118px;
    text-align: center;
  }
  /* 「需要重新登录」提示文字（官方积分包区 401 错误块替换用）：标题 + 说明句。
     异常信息直接居中展示在原积分展示区域，保持与正常卡片相同的区域高度与排版节奏。 */
  .wb-pool-relogin-title {
    font-size: 13px;
    font-weight: 600;
    color: #b91c1c;
    line-height: 1.4;
  }
  .wb-pool-relogin-desc {
    font-size: 11px;
    color: #7f1d1d;
    line-height: 1.45;
    min-width: 0;
  }
  /* 官方积分包区 401 错误文本的替换样式：
     位置在操作按钮上方，占满原积分包展示区域。
     使其与正常卡片上方积分展示区的高度完全对齐一致，使整张卡片上下排版严格对称。 */
  .wb-official-relogin {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px dashed rgba(239, 68, 68, 0.5);
    background: rgba(239, 68, 68, 0.07);
    flex: 0 0 auto;
    height: auto !important;
    min-height: 140px;
    box-sizing: border-box;
    margin-top: 12px;
    word-break: break-word !important;
    overflow-wrap: break-word !important;
  }
  /* 移动端与全端自适应：确保卡片与内框高度不被官方样式截断，异常卡片各区域完整展开 */
  article {
    height: auto !important;
    min-height: auto !important;
    max-height: none !important;
  }
  article > section {
    height: auto !important;
    min-height: auto !important;
    max-height: none !important;
    overflow: visible !important;
  }
  /* 需重登卡片的按钮区/模型区禁用态（四校）：灰化 + 不可操作，
     参照旅行图标的灰色处理方式。 */
  .wb-relogin-disabled {
    opacity: 0.45;
    filter: grayscale(1);
    pointer-events: none;
    user-select: none;
  }
  /* 异常卡内框与正常卡对齐（2026-10-21 工单：「内部区域高度未对齐正常卡片」）。
     实测差异（同 334px 内容宽）：
       模型区标题行  异常 18px  vs  正常 24px   ← 异常态没有「已禁用/巡检」徽标
                                                  与「全部恢复」按钮，行被压矮
       模型区总高    异常 330px vs  正常 336px  ← 上面那 6px 差额的直接来源
     正常卡顶部的「积分/用量标题行 + 副标题行 + 积分包内容区」共 136px（含间距），
     异常卡这三块被隐藏，只留一枚登录提醒块 —— 卡片整体因此比正常卡矮一截。
     这里不硬填那 136px（内容差异就是差异，填了反而是假的），只把**同名区域**
     逐项对齐：标题行给足 24px 行高。模型区的 min-height 不写死像素 ——
     由 JS 在同页正常卡上量实际高度再设（见 wbApplyReloginDisabled），
     PC 与移动端各自适配，不拍脑袋。 */
  .wb-relogin-disabled .wb-am-title {
    min-height: 24px;
  }
  /* 顶部外框官方「需重新登录」badge 隐藏（四校）：提示只出现在内框。
     只遮 header 里的 badge 行（span 容器），不动右侧两个按钮。 */
  .wb-header-badge-hidden span:not(:has(button)):not(:has(svg[class*=chevron])) {
    visibility: hidden;
  }
  /* 账号管理搜索框（v1.0.36：进「账号」行 + 移动端图标弹窗，用户方案）
     ──────────────────────────────────────────────────────────
     v1.0.34 及以前：搜索框寄生在官方「档位容器」里（内联改写官方 flex-col
     → flex-row），需要一整套清理机器，官方一改版最先坏。
     v1.0.35：独立过滤行插在页头行之后（现保留为官方改版时的兜底形态）。
     v1.0.36（当前，用户 2026-10-25 方案）：
       · PC（≥640px）：搜索框插进「账号」行右侧图标按钮容器的**首位** ——
         即「账号」标题之后、自动签到/自动执行等图标按钮之前，与图标的
         间距 = 容器自身 gap（与两个图标间距一致）；框高 36px 与图标对齐。
       · 移动端（<640px）：内联框隐藏，改显**同款风格的搜索图标按钮**
         （克隆旁边图标按钮的 class，外观完全一致）；点击弹出搜索弹窗，
         16px 输入框（防 iOS 聚焦自动放大）+ 计数徽章 + 关闭按钮，
         输入即过滤；过滤生效时图标点亮提醒「列表当前处于过滤态」。
     过滤状态在 PC 内联框与移动端弹窗之间双向同步（同一 state.q）。 */
  .wb-acc-search-row {
    display: flex;
    justify-content: flex-end;    /* 兜底形态：搜索框靠右 */
    align-items: center;
    margin: 8px 0 4px;
  }
  .wb-acc-search-wrap {
    display: inline-flex;
    align-items: center;
    height: 36px;                 /* 与「账号」行图标按钮 36×36 对齐 */
    box-sizing: border-box;
    flex: 0 1 auto;
    max-width: 100%;
  }
  /* v1.0.38：「账号」行右侧控件的间距归一化（用户要求全行间距一致）。
     官方结构实测：容器 gap-1（4px），但中间的分隔线自带 mx-2（8+1+8），
     使「自动执行」与「视图切换」之间出现 25px 大间隙；PC 4 控件 / 移动端
     5 控件间距不一。归一化 = 分隔线水平 margin 归零，全行统一 4px 间距
     （分隔线本体保留，仍是那个 1px 竖线，只是不再撑出额外空隙）。
     React 重渲染会把内联样式/class 冲掉，所以归一化在每次 run() 都重放
     （wbNormalizeAccountRowActions，幂等），撤销时按 dataset 记录还原。 */
  /* 移动端搜索图标按钮：默认隐藏（PC 不显示）；class 主体克隆自旁边的
     官方图标按钮（wbInjectAccountSearch 里 dynamic copy），这里只补
     「没克隆到兄弟 class」时的兜底外观与激活态。 */
  .wb-acc-search-iconbtn {
    display: none;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: 10px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.25));
    /* v1.0.39：兜底背景从 --card 改 --accent —— 本规则在官方 Tailwind 之后
       加载，同优先级会盖掉克隆来的 bg-accent（浅色下露出纯白，与旁边按钮
       两张皮）。改用 --accent 后：盖掉与被盖两个值同源，两主题必然一致。 */
    background: var(--accent, #f4f4f5);
    color: var(--muted-foreground, #6b7280);
    cursor: pointer;
    padding: 0;
  }
  .dark .wb-acc-search-iconbtn,
  html.dark .wb-acc-search-iconbtn {
    border-color: rgba(148, 163, 184, 0.28);
  }
  .wb-acc-search-iconbtn svg { width: 16px; height: 16px; }
  /* 过滤生效中：图标点亮，提醒移动端用户当前列表处于过滤态 */
  .wb-acc-search-icon-on {
    border-color: rgba(59, 130, 246, 0.65) !important;
    color: #3b82f6 !important;
  }
  /* v1.0.38：刷新按钮官方是透明 ghost 形态（lucide-refresh-cw，无 bg-accent），
     与旁边自动签到/自动执行/视图切换三个按钮不一致（用户要求「加一样的背景」）。
     用官方 --accent 变量补背景 —— 与 bg-accent 同源，两个主题自动跟随。 */
  button.wb-acc-btn-flat {
    background: var(--accent, #f4f4f5);
  }
  .wb-acc-search-box {
    display: flex;
    align-items: center;
    gap: 6px;
    height: 36px;                 /* 与「账号」行图标按钮 36×36 对齐 */
    box-sizing: border-box;
    /* v1.0.37：配色与「账号」行图标按钮完全一致（用户要求「背景相同、
       两个主题颜色同步」）。按钮实测用的是官方 bg-accent + --border，
       这里同款取值 —— 两个主题由 --accent / --border 变量自动跟随，
       不再像旧版那样浅色用 --card（纯白）、深色叠硬编码边框。
       圆角同按钮 rounded-lg = 12px。 */
    background: var(--accent, #f4f4f5);
    border: 1px solid var(--border, rgba(120, 120, 120, 0.25));
    border-radius: 12px;
    padding: 0 10px;
    transition: border-color 0.15s ease, background 0.15s ease;
  }
  .wb-acc-search-box:focus-within {
    border-color: rgba(59, 130, 246, 0.65);
  }
  .wb-acc-search-icon {
    width: 14px;
    height: 14px;
    color: var(--foreground, #374151);   /* 与按钮图标同色（v1.0.37） */
    flex-shrink: 0;
  }
  .wb-acc-search-input {
    border: none;
    background: transparent;
    font-size: 12px;              /* 对齐官方 text-xs */
    font-weight: 500;             /* 对齐切换按钮 font-medium */
    line-height: 34px;
    color: var(--foreground, #374151);
    outline: none;
    width: 170px;
    min-width: 0;
  }
  /* 移动端搜索弹窗（v1.0.36 用户方案）：点搜索图标弹出，输入即过滤。
     输入框 16px 起步 —— iOS Safari 对 <16px 的 input 聚焦时自动放大
     整页且不回弹；40px 触控高度。 */
  .wb-acc-search-modal {
    position: fixed;
    inset: 0;
    z-index: 9999;
  }
  .wb-acc-search-modal[hidden] { display: none; }
  .wb-acc-search-backdrop {
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
  }
  .wb-acc-search-sheet {
    position: absolute;
    left: 50%;
    top: 96px;
    transform: translateX(-50%);
    width: min(92vw, 420px);
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    background: var(--card, #ffffff);
    border: 1px solid var(--border, rgba(120, 120, 120, 0.25));
    border-radius: 14px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.28);
  }
  .dark .wb-acc-search-sheet,
  html.dark .wb-acc-search-sheet {
    background: var(--popover, #1f2430);
    border-color: rgba(148, 163, 184, 0.28);
  }
  .wb-acc-search-minput {
    flex: 1;
    min-width: 0;
    border: none;
    background: transparent;
    outline: none;
    font-size: 16px;              /* ≥16px：防 iOS Safari 聚焦自动放大 */
    height: 40px;
    line-height: 40px;
    color: var(--foreground, #374151);
  }
  .wb-acc-search-close {
    border: none;
    background: transparent;
    color: var(--muted-foreground, #6b7280);
    font-size: 18px;
    line-height: 1;
    cursor: pointer;
    padding: 8px 6px;
    flex-shrink: 0;
  }
  /* 移动端（<640px）：内联搜索框隐藏，改显同款风格搜索图标（点击弹窗）。
     兜底独立行（wb-account-search-row，官方改版时才会出现）保持吸顶。 */
  @media (max-width: 640px) {
    .wb-acc-search-wrap { display: none; }
    .wb-acc-search-iconbtn { display: inline-flex; }
    .wb-acc-search-row {
      position: sticky;
      top: 0;
      z-index: 10;
      justify-content: stretch;
      margin: 4px 0;
      padding: 4px 0;
      background: var(--background, #f7f7f8);
    }
    .dark .wb-acc-search-row,
    html.dark .wb-acc-search-row {
      background: var(--background, #0b0e14);
    }
    .wb-acc-search-row .wb-acc-search-wrap {
      display: inline-flex;       /* 兜底行里内联框仍要显示 */
      width: 100%; height: 40px;
    }
    .wb-acc-search-row .wb-acc-search-box { width: 100%; height: 40px; }
    .wb-acc-search-row .wb-acc-search-input { width: 100%; font-size: 16px; line-height: 40px; }
    .wb-acc-search-icon { width: 16px; height: 16px; }
    /* 移动端登录提醒块：占满对应积分区域高度并自适应居中 */
    .wb-official-relogin { padding: 10px 12px; gap: 3px; min-height: 130px; }
    .wb-pool-relogin-title { font-size: 12px; line-height: 1.3; }
    .wb-pool-relogin-desc {
      font-size: 10.5px;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    /* 账号池控制条（2026-10-21 工单第 2 件「启用时按钮跑到卡片底部」）：
       窄屏约 310px 内容宽，4~5 个元素必然折行。上面已用
       flex:0 0 auto + min-width + margin-left:auto 保证「按钮组 ↔ 统计」
       分开换行、按钮组内部不错位；这里再把间距与主按钮最小宽度收一档，
       让按钮组三个尽量留在同一行。 */
    .wb-pool-bar { gap: 6px; }
    .wb-pool-bar > .wb-pool-btn:first-child { min-width: 104px; }
    .wb-pool-btn { padding: 1px 8px; }
  }
  .wb-acc-search-count {
    font-size: 11px;
    color: var(--muted-foreground, #6b7280);
    background: rgba(120, 120, 120, 0.15);
    padding: 1px 6px;
    border-radius: 999px;
    flex: 0 0 auto;
  }
  /* 主按钮宽度取齐规则见上方 .wb-pool-bar > .wb-pool-btn:first-child 的注释 */
  .wb-pool-btn:hover { background: rgba(120, 120, 120, 0.12); }
  .wb-pool-btn-on {
    border-color: rgba(34, 197, 94, 0.55);
    background: rgba(34, 197, 94, 0.14);
    color: #15803d;
  }
  .wb-pool-btn-primary {
    border-color: rgba(59, 130, 246, 0.55);
    background: rgba(59, 130, 246, 0.14);
    color: #1d4ed8;
    font-weight: 600;
  }
  .wb-pool-btn-auto {
    border-color: rgba(217, 119, 6, 0.55);
    background: rgba(217, 119, 6, 0.14);
    color: #b45309;
  }
  /* ---------------- 账号卡片「风控标记」徽章（2026-10-21）----------------
     位置：账号卡片右上角，插在原有旅行 / 签到两个图标的**前方**。

     ⚠️ 刻意与「已停用」做足区分，不许看混：
       停用（.wb-pool-btn-auto）= 琥珀色、细边框、淡底、文字「已停用」
       风控标记（本类）        = 实心红、白字、盾牌感叹号图标、无边框
     上一次的教训就是把「被上游风控」和「本地停用」混为一谈，一眼看混
     就等于这个功能白做。所以形状（盾牌 vs 圆角矩形）、填充方式
     （实心 vs 淡底）、配色（红 vs 琥珀）三者全都不同。

     语义：标记 ≠ 停用。一个号可以「被标记但仍在跑」，也可以「本地停用
     但没被标记」—— 徽章只回答「上游有没有判过它被拦」，不回答别的。 */
  .wb-risk-badge {
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
    gap: 3px;
    flex: 0 0 auto;
    height: 18px;
    min-width: 18px;
    padding: 0 4px;
    border-radius: 999px;
    background: #dc2626;
    color: #ffffff;
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    /* 与官方那排图标对齐 */
    vertical-align: middle;
    -webkit-tap-highlight-color: transparent;
  }
  .wb-risk-badge:hover { background: #b91c1c; }
  .wb-risk-badge svg {
    width: 11px;
    height: 11px;
    flex-shrink: 0;
    /* 图标用 currentColor 跟着文字色（白）走，不单独上色 */
    stroke: currentColor;
  }
  /* 深色主题下实心红会发闷，提一档亮度保证对比度 */
  @media (prefers-color-scheme: dark) {
    .wb-risk-badge { background: #ef4444; }
    .wb-risk-badge:hover { background: #dc2626; }
  }
  /* 点击后的详情浮层。挂在徽章下方，绝对定位避免推开同行图标。
     max-width 用 calc 而非固定 px：移动端竖屏很窄，写死会溢出屏幕。 */
  .wb-risk-pop {
    position: absolute;
    z-index: 60;
    top: calc(100% + 6px);
    right: 0;
    width: max-content;
    max-width: min(280px, calc(100vw - 32px));
    padding: 10px 12px;
    border-radius: 10px;
    border: 1px solid rgba(220, 38, 38, 0.45);
    background: color-mix(in oklab, var(--popover, var(--card, #ffffff)) 96%, transparent);
    color: var(--foreground, #111827);
    font-size: 12px;
    line-height: 1.6;
    text-align: left;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
    cursor: default;
  }
  .wb-risk-pop-title {
    display: flex;
    align-items: center;
    gap: 5px;
    font-weight: 700;
    color: #dc2626;
    margin-bottom: 4px;
  }
  .wb-risk-pop-row { display: flex; gap: 6px; }
  .wb-risk-pop-k { color: var(--muted-foreground, #6b7280); flex: 0 0 auto; }
  .wb-risk-pop-v { min-width: 0; word-break: break-all; }
  .wb-risk-pop-note {
    margin-top: 6px;
    padding-top: 6px;
    border-top: 1px dashed var(--border, rgba(120, 120, 120, 0.25));
    color: var(--muted-foreground, #6b7280);
    font-size: 11px;
  }
  /* 浮层里的「解除标记」按钮。刻意做成**低调的描边按钮**而不是实心红 ——
     浮层本身已经是红边框，按钮再实心红会让人以为点了会发生什么大事，
     而它其实只是从界面上撤掉一条记录，账号本身毫发无损。 */
  .wb-risk-pop-btn {
    display: block;
    width: 100%;
    margin-top: 8px;
    padding: 5px 10px;
    border-radius: 8px;
    border: 1px solid rgba(220, 38, 38, 0.4);
    background: transparent;
    color: #dc2626;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
  }
  .wb-risk-pop-btn:hover { background: rgba(220, 38, 38, 0.08); }
  .wb-risk-pop-btn:disabled { opacity: 0.55; cursor: default; }
  /* 一致性自检的结果块。每行 = 一个账号的「凭据状态 + 姓名 + 一致性结论 + 建议」。
     窄屏时靠 flex-wrap 自然折行，不写死列宽 —— 写死会让长建议被裁掉。 */
  .wb-audit-box { gap: 8px; }
  .wb-audit-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border, rgba(120, 120, 120, 0.15));
    min-width: 0;
  }
  .wb-audit-row:last-child { border-bottom: 0; }
  .wb-audit-state {
    flex: 0 0 auto;
    font-size: 11px;
    font-weight: 600;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.35));
  }
  .wb-audit-ok { color: #047857; border-color: rgba(4, 120, 87, 0.45); background: rgba(4, 120, 87, 0.1); }
  .wb-audit-warn { color: #b45309; border-color: rgba(217, 119, 6, 0.5); background: rgba(217, 119, 6, 0.12); }
  .wb-audit-bad { color: #b91c1c; border-color: rgba(185, 28, 28, 0.5); background: rgba(185, 28, 28, 0.1); }
  .wb-audit-name { flex: 0 0 auto; font-weight: 600; min-width: 0; }
  .wb-audit-consistency { flex: 0 0 auto; font-size: 12px; }
  .wb-audit-detail {
    flex: 1 1 100%;
    min-width: 0;
    font-size: 11px;
    opacity: 0.72;
    line-height: 1.5;
  }
  .wb-audit-advice {
    flex: 1 1 240px;
    min-width: 0;
    font-size: 11px;
    opacity: 0.75;
    line-height: 1.5;
  }
  .wb-audit-criteria { font-size: 11px; opacity: 0.65; }
  .wb-pool-hint {
    font-size: 11px;
    color: var(--muted-foreground, #6b7280);
  }
  /* 标记最近一次 API 请求实际使用的账号 */
  .wb-pool-last {
    font-size: 11px;
    line-height: 1.8;
    padding: 1px 9px;
    border-radius: 999px;
    border: 1px dashed rgba(120, 120, 120, 0.45);
    color: var(--muted-foreground, #6b7280);
    flex: 0 0 auto;
    white-space: nowrap;
    /* 2026-10-21 工单第 2 件：与「已调用 N 次」一起靠最右，二者之间用
       auto 均分剩余空间。这样窄屏换行只发生在「按钮组 ↔ 统计」之间，
       按钮组三个的相对位置不受影响。 */
    margin-left: auto;
  }
  /* 该账号累计被分摊到的请求次数（进程启动后累计） */
  .wb-pool-count {
    font-size: 11px;
    line-height: 1.8;
    padding: 1px 9px;
    border-radius: 999px;
    background: rgba(120, 120, 120, 0.14);
    color: var(--muted-foreground, #6b7280);
    font-variant-numeric: tabular-nums;
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .wb-pool-count-hot {
    background: rgba(59, 130, 246, 0.16);
    color: #1d4ed8;
    font-weight: 600;
  }
  /* 账号卡片下方动态展示的「可用模型」区域 */
  .wb-am-box {
    margin-top: 12px;
    border-top: 1px dashed rgba(120, 120, 120, 0.25);
    padding-top: 10px;
  }
  .wb-am-title {
    font-size: 11px;
    line-height: 1.6;
    color: var(--muted-foreground, #6b7280);
    margin-bottom: 6px;
    /* flex + wrap：徽标与「全部恢复」按钮同排，按钮靠 margin-left:auto 右对齐。
       窄屏放不下时整行折行，不会把按钮挤出卡片。 */
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 2px 0;
  }
  .wb-am-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .wb-am-tag {
    font-size: 10px;
    line-height: 1.7;
    padding: 1px 7px;
    border-radius: 6px;
    background: rgba(120, 120, 120, 0.12);
    color: var(--foreground, #374151);
    white-space: nowrap;
  }
  /* 该账号已经真实调用过的模型，高亮以区分「可用」与「已用」 */
  .wb-am-tag-used {
    background: rgba(59, 130, 246, 0.16);
    color: var(--primary, #1d4ed8);
    font-weight: 600;
  }
  /* 模型标签可点击：点一下禁用该模型对该账号的调用，再点恢复。
     默认态刻意不加边框/下划线，避免一排标签看起来像按钮墙；
     只有悬停与键盘聚焦时才显形，暗示「这是可以点的」。 */
  .wb-am-tag-click {
    cursor: pointer;
    user-select: none;
    transition: background 0.12s ease, color 0.12s ease, box-shadow 0.12s ease;
  }
  .wb-am-tag-click:hover {
    box-shadow: inset 0 0 0 1px rgba(120, 120, 120, 0.55);
  }
  .wb-am-tag-click:focus-visible {
    outline: 2px solid rgba(59, 130, 246, 0.65);
    outline-offset: 1px;
  }
  /* 禁用态：压暗 + 删除线。用「灰掉」而不是「红掉」——
     红色读起来像出错告警，而这里是用户主动的选择，不是故障。 */
  .wb-am-tag-off {
    background: rgba(120, 120, 120, 0.10);
    color: var(--muted-foreground, #9ca3af);
    text-decoration: line-through;
    font-weight: 400;
  }
  .wb-am-tag-off:hover {
    box-shadow: inset 0 0 0 1px rgba(120, 120, 120, 0.35);
  }
  /* 被**手动**禁用的模型数。只数人点的那些：巡检写的另有 .wb-am-offcount-auto，
     两个数互不重叠，免得同一批模型被数两遍。 */
  .wb-am-offcount {
    font-size: 10px;
    line-height: 1.7;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px dashed rgba(120, 120, 120, 0.45);
    color: var(--muted-foreground, #6b7280);
    margin-left: 6px;
    font-variant-numeric: tabular-nums;
  }
  /* 巡检自动禁用：与手动禁用一样「不参与调用」，但来源不同 ——
     它由可用性巡检写入、模型恢复后会自行解除，所以用琥珀色虚线区分，
     让用户一眼看出「这不是我关的」。计数上同样与手动禁用分开。 */
  .wb-am-tag-auto {
    background: rgba(245, 158, 11, 0.14);
    color: #b45309;
    text-decoration: line-through;
    font-weight: 400;
    box-shadow: inset 0 0 0 1px rgba(245, 158, 11, 0.32);
  }
  .wb-am-tag-auto:hover {
    box-shadow: inset 0 0 0 1px rgba(245, 158, 11, 0.6);
  }
  .wb-am-offcount-auto {
    border-color: rgba(245, 158, 11, 0.5);
    color: #b45309;
  }
  /* 一键恢复：把该账号被禁用的模型全部放回轮询。
     做成文字按钮而非图标 —— 它改变的是路由行为，值得让人先看清「恢复几个」。
     与左侧两个徽标同一行右对齐，作用域天然读作「这张卡片」。 */
  .wb-am-restore {
    margin-left: auto;
    font: inherit;
    font-size: 11px;
    line-height: 1.6;
    padding: 2px 10px;
    border-radius: 6px;
    cursor: pointer;
    color: var(--primary, #1d4ed8);
    background: transparent;
    border: 1px solid rgba(29, 78, 216, 0.35);
    transition: background .15s ease, border-color .15s ease;
    /* 2026-10-21 工单第 2 件：标题行是 flex-wrap，计数徽标数量会随
       「已禁用 / 巡检」增减而变化。按钮若不锁定自身宽度，徽标一多一少
       就把它挤到下一行 —— 与账号池控制条同一个病。 */
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .wb-am-restore:hover:not(:disabled) {
    background: rgba(29, 78, 216, 0.08);
    border-color: rgba(29, 78, 216, 0.6);
  }
  .wb-am-restore:disabled { opacity: .6; cursor: default; }
  /* ---------------- 设置页：API 接入面板 ---------------- */
  /* ---------------- 每日任务页：容器 / 标题对齐官方页面 ----------------
     实测官方（账号管理 / 积分统计 / Token 统计）统一使用：
       mx-auto w-full max-w-[1180px] px-4 py-6，≥640px 时 sm:px-8 sm:py-9
       H1 28px / 600 / line-height 42px
     本页原先是自造的 max-width 960px + 24px 20px + 20px/700 的标题。 */
  .wb-daily-wrap {
    margin: 0 auto;
    width: 100%;
    max-width: 1180px;
    min-width: 0;
    padding: 24px 16px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    box-sizing: border-box;
  }
  .wb-daily-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 14px;
  }
  .wb-daily-h1 {
    font-size: 28px;
    font-weight: 600;
    line-height: 42px;
    margin: 0;
    color: var(--foreground, #0f172a);
  }
  .wb-daily-sub {
    font-size: 13.5px;
    margin: 4px 0 0 0;
    color: var(--muted-foreground, #64748b);
    max-width: 720px;
    line-height: 20px;
  }
  @media (min-width: 640px) {
    .wb-daily-wrap { padding: 36px 32px; }
  }
  @media (max-width: 560px) {
    /* 窄屏：标题不该再和右侧状态徽章抢同一行。
       ⚠️ 不再缩小 H1 字号 —— 2026-10-17 体检发现移动端六个页面的 H1
       出现 22/24/28px 三档，官方页是 28px，注入页却缩到 22/24px。
       统一为 28px（390px 宽度下 4 字标题约 112px，放得下）。 */
    .wb-daily-head { flex-direction: column; align-items: stretch; }
  }

  /* 区块结构对齐官方（用户 2026-09-27 反馈）。

     官方积分统计页实测骨架：
       SECTION
         ├─ H2  二级标题（如「官方积分消耗」）= 13px / 500 / line-height 20px
         └─ CARD（rounded-2xl，内部只放内容）

     每日任务页原骨架（自造，与官方不一致）：
       CARD
         └─ 行：三级标题(.wb-api-label) + 副标题(.wb-api-desc) + 内容
     即**标题被包进了框里**，且层级是三级标题。

     这里把每个卡片的标题**外提**为卡片外的二级标题 H2，
     使结构与官方一致：SECTION > [H2, CARD(内容)]。 */
  .wb-daily-section { display: block; margin: 0; }
  .wb-daily-h2 {
    font-size: 14.5px;
    font-weight: 500;
    line-height: 20px;
    color: var(--foreground, #0f172a);
    margin: 0 0 8px 0;
  }

  /* 本页控件统一到官方规格。注意：.wb-api-btn 是跨页面共用的类
     （设置页/账号卡片等 36 处都在用），因此这里必须用 .wb-daily-wrap
     作用域限定，只改本页，避免波及其它页面。

     官方实测（积分统计 / Token 统计 / 账号管理）：
       主按钮 14px / 500 / radius 10px / 高 32px / border 1px
       次按钮 12px / 500 / radius 10px / 高 28px
     本页原有：11px / 400 / radius 999px（胶囊）/ 高 25px —— 明显偏小。 */
  .wb-daily-wrap .wb-api-btn {
    font-size: 14px;
    font-weight: 500;
    line-height: 1.35;
    padding: 0 10px;
    min-height: 32px;
    border-radius: 10px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.3));
    background: var(--card, #ffffff);
    color: var(--foreground, #0f172a);
  }
  .wb-daily-wrap .wb-api-btn:hover { background: rgba(120, 120, 120, 0.1); }
  /* 次要按钮（刷新等）沿用官方的 12px / 28px 一档 */
  .wb-daily-wrap .wb-api-btn-primary {
    background: rgba(59, 130, 246, 0.14);
    border-color: rgba(59, 130, 246, 0.35);
    color: #1d4ed8;
    font-weight: 500;
  }
  .wb-daily-wrap .wb-api-btn-danger {
    background: rgba(239, 68, 68, 0.1);
    border-color: rgba(239, 68, 68, 0.35);
    color: #dc2626;
  }

  /* 输入框 / 下拉：与官方一致 —— 12px、radius 10px、高 32px */
  .wb-daily-wrap input[type="number"],
  .wb-daily-wrap input[type="text"],
  .wb-daily-wrap select {
    font-size: 12px;
    line-height: 1.4;
    padding: 6px 10px;
    min-height: 32px;
    border-radius: 10px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.3));
    background: var(--card, #ffffff);
    color: var(--foreground, #0f172a);
    box-sizing: border-box;
  }
  /* 表格：与官方一致 —— 12px、th 弱化、单元格 padding 统一 6px 10px */
  .wb-daily-wrap table { font-size: 12px; width: 100%; border-collapse: collapse; }
  .wb-daily-wrap th {
    font-size: 12px; font-weight: 500;
    color: var(--muted-foreground, #64748b);
    text-align: left; padding: 6px 10px;
    white-space: nowrap;
  }
  .wb-daily-wrap td { font-size: 12px; padding: 6px 10px; }

  .wb-api-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--card, #ffffff);
    color: var(--card-foreground, #0f172a);
    border: 1px solid var(--border, rgba(120, 120, 120, 0.25));
    border-radius: 16px;
    overflow: hidden;
  }
  .wb-api-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
    margin: 0 16px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border, rgba(120, 120, 120, 0.2));
  }
  @media (min-width: 640px) { .wb-api-row { margin: 0 20px; } }
  /* ⚠️ 每日任务页：.wb-api-row 自带 `margin: 0 20px`（≥640px），那是为
     「卡片内部的字段行」设计的左右留白。但本页的「账号签到与凭据」那一行
     是 .wb-daily-wrap 的**直接子元素**（它的卡片被提前闭合了，见
     wbNormalizeDailyLayout 注释），于是比同一层的其它行多内缩 20px ——
     用户 2026-10-17 反馈「部分内容左右宽度未对齐」，实测这一行
     x=446 w=1076，而同层其它行 x=426 w=1116，左右各差 20px。
     这里把 wrap 直接子级的 row 边距归零，让它与同层元素严格对齐。
     只作用于本页（.wb-daily-wrap 作用域），不影响设置页等其它页面。 */
  .wb-daily-wrap > .wb-api-row { margin: 0 !important; }
  /* 卡片内「游离说明文字」的缩进：必须与 .wb-api-row 用**同一套**左右边距，
     否则它作为 card 直接子节点时会比字段行凸出 16/20px。
     搜索引擎区的引擎说明（desc + 申请地址）用它。 */
  .wb-sr-note {
    margin: 0 16px 8px;
  }
  @media (min-width: 640px) { .wb-sr-note { margin: 0 20px 8px; } }
  @media (max-width: 560px) { .wb-sr-note { margin: 0 12px 8px; } }
  .wb-api-row:last-child { border-bottom: 0; }
  .wb-api-row-stack { flex-direction: column; align-items: stretch; }
  /* 窄屏下横排会把左边的说明文字压成一条竖线（实测 13px 宽），
     因为右侧的按钮/代码块不肯让位。改成纵向堆叠，各自吃满整行。 */
  @media (max-width: 560px) {
    .wb-api-row {
      flex-direction: column;
      align-items: stretch;
      gap: 8px;
    }
    .wb-api-row > * { max-width: 100%; }
    /* 窄屏竖屏：状态徽章允许换行、飞机图标保持不缩，避免被挤出可视区 */
    .wb-api-badge { white-space: normal; }
    .wb-buddy-badge, .wb-nobuddy-badge { flex-shrink: 0; }
    .wb-buddy-badge svg { width: 12px; height: 12px; }
  }
  .wb-api-main { min-width: 0; flex: 1 1 auto; }
  .wb-api-label {
    font-size: 14px;
    line-height: 1.5;
    font-weight: 500;
    color: var(--foreground, #0f172a);
  }
  .wb-api-desc {
    margin-top: 2px;
    font-size: 12.5px;
    line-height: 1.6;
    color: var(--muted-foreground, #64748b);
  }
  .wb-api-mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12.5px;
  }
  .wb-api-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 100%;
    padding: 3px 8px;
    border-radius: 6px;
    background: var(--muted, rgba(120, 120, 120, 0.12));
    color: var(--foreground, #0f172a);
  }
  .wb-api-chip > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wb-api-input {
    flex: 1 1 auto;
    min-width: 0;
    padding: 5px 9px;
    border-radius: 6px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.35));
    background: var(--background, #ffffff);
    color: var(--foreground, #0f172a);
    font-size: 12px;
  }
  .wb-api-input:focus { outline: 2px solid var(--primary, #1d4ed8); outline-offset: 1px; }
  .wb-api-btn {
    flex: 0 0 auto;
    font-size: 12.5px;
    line-height: 1.9;
    padding: 1px 10px;
    border-radius: 999px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.35));
    background: transparent;
    color: var(--foreground, #374151);
    cursor: pointer;
    white-space: nowrap;
  }
  .wb-api-btn:hover { background: rgba(120, 120, 120, 0.12); }
  .wb-api-btn-primary {
    border-color: rgba(59, 130, 246, 0.55);
    background: rgba(59, 130, 246, 0.14);
    color: #1d4ed8;
    font-weight: 600;
  }
  .wb-api-btn-danger {
    border-color: rgba(239, 68, 68, 0.5);
    background: rgba(239, 68, 68, 0.1);
    color: #b91c1c;
  }
  .wb-api-btn-on {
    border-color: rgba(34, 197, 94, 0.55);
    background: rgba(34, 197, 94, 0.14);
    color: #15803d;
    font-weight: 600;
  }
  .wb-api-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    line-height: 1.9;
    padding: 1px 9px;
    border-radius: 999px;
    background: var(--muted, rgba(120, 120, 120, 0.14));
    color: var(--muted-foreground, #64748b);
    white-space: nowrap;
  }
  .wb-api-badge-ok { background: rgba(34, 197, 94, 0.16); color: #15803d; font-weight: 600; }
  .wb-api-badge-warn { background: rgba(245, 158, 11, 0.18); color: #b45309; font-weight: 600; }
  .wb-api-keyrow {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex-wrap: wrap;
    padding: 8px 0;
    border-bottom: 1px dashed var(--border, rgba(120, 120, 120, 0.2));
  }
  .wb-api-keyrow:last-child { border-bottom: 0; }
  /* 密钥统计子行：单独一行横排五个指标（用户 2026-10-17 需求）。
     用 flex-basis:100% 强制换行，把统计与上面的密钥名/按钮行分开；
     不用再嵌一张卡片 —— 那张卡已经是一行一个密钥了，再套会变成框中框。 */
  .wb-api-keystat {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 14px;
    flex-basis: 100%;
    margin-top: 2px;
    padding-left: 2px;
  }
  .wb-api-keystat-cell { display: inline-flex; align-items: baseline; gap: 4px; }
  .wb-api-keystat-k {
    font-size: 11.5px;
    color: var(--muted-foreground, #64748b);
    white-space: nowrap;
  }
  .wb-api-keystat-v {
    font-size: 13px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--foreground, #0f172a);
    white-space: nowrap;
  }
  .wb-api-keystat-empty {
    font-size: 12px;
    color: var(--muted-foreground, #64748b);
  }
  .wb-api-keyname {
    font-size: 12px;
    font-weight: 500;
    color: var(--foreground, #0f172a);
    max-width: 190px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wb-api-code {
    margin-top: 6px;
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--muted, rgba(120, 120, 120, 0.12));
    color: var(--foreground, #0f172a);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11.5px;
    line-height: 1.7;
    white-space: pre;
    overflow-x: auto;
  }
  .wb-api-empty {
    padding: 10px 0;
    font-size: 12px;
    line-height: 1.7;
    color: var(--muted-foreground, #64748b);
  }
  .wb-api-toast {
    position: fixed;
    left: 50%;
    bottom: 28px;
    z-index: 9999;
    transform: translateX(-50%);
    padding: 8px 16px;
    /* 圆角不再用 999px：结论文案是多行的（账号名 + 结论 + 依据 + 建议），
       胶囊形在多行下两端会被拉出怪异的弧形，移动端尤其明显。 */
    border-radius: 10px;
    font-size: 12.5px;
    line-height: 1.6;
    /* 必须限制宽度并允许换行。此前只有 padding 没有上限，
       长文案会把 toast 撑得比视口还宽，两端被裁掉、整块像贴歪的色带。 */
    max-width: min(560px, calc(100vw - 32px));
    box-sizing: border-box;
    white-space: normal;
    overflow-wrap: anywhere;
    text-align: left;
    /* 默认档（纯告知）跟随主题前景/背景，而不是写死深色 ——
       写死深色在浅色主题下就是一块突兀的黑条。 */
    color: var(--background, #ffffff);
    background: color-mix(in srgb, var(--foreground, #0f172a) 92%, transparent);
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.25);
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.2s ease, transform 0.2s ease;
  }
  .wb-api-toast-show { opacity: 1; transform: translateX(-50%) translateY(-4px); }
  /* 三档语义配色。此前只有「普通 / 红色错误」两档，「账号受限」被并进红色档，
     在语义上错误地暗示了「凭据坏了」—— 而它的凭据其实是好的。
     颜色只用于区分**该不该动手**，不用于表达严重程度：绿=无需处理，
     琥珀=需要关注但重登没用，红=需要你介入（重新登录）。

     选择器同时列 err 与 error 两种写法：调用方历史上混用过
     （`wbToast(msg, "error")` 与 `wb-api-toast-err`），只写一个会让另一个
     静默落到默认档 —— 红色提示看起来像普通提示，正是「背景很奇怪」的来源。 */
  .wb-api-toast-ok {
    color: #ffffff;
    background: rgba(4, 120, 87, 0.95);
  }
  .wb-api-toast-warn {
    color: #ffffff;
    background: rgba(180, 83, 9, 0.95);
  }
  .wb-api-toast-err,
  .wb-api-toast-error { background: rgba(185, 28, 28, 0.95); }
  @media (max-width: 560px) {
    .wb-api-toast {
      bottom: 16px;
      padding: 9px 13px;
      font-size: 12px;
      /* 窄屏再收一点边距，避免贴边；圆角同步收小更好看。 */
      max-width: calc(100vw - 20px);
      border-radius: 9px;
    }
  }
  /* ---------------- 设置页：关于面板 ----------------
     这一块是设置页的最后一段：容器是自建项目，用户需要知道它是什么、
     跑的是哪个镜像、出问题去哪里看，否则只能靠翻文档回忆。 */
  .wb-about-head {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 2px;
  }
  .wb-about-name {
    font-size: 13px;
    font-weight: 600;
    line-height: 1.6;
    color: var(--foreground, #374151);
  }
  .wb-about-links {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 2px;
  }
  .wb-about-link {
    font-size: 11px;
    line-height: 1.7;
    padding: 1px 9px;
    border-radius: 999px;
    border: 1px solid rgba(120, 120, 120, 0.35);
    color: var(--foreground, #374151);
    text-decoration: none;
    white-space: nowrap;
    transition: background 0.12s ease, border-color 0.12s ease;
  }
  .wb-about-link:hover {
    background: rgba(120, 120, 120, 0.12);
    border-color: rgba(120, 120, 120, 0.55);
  }
  /* .wb-about-note 的样式随「说明」那一项一起下线（2026-10-22 用户要求），
     这里不再保留规则 —— 留着只会让以后读代码的人以为还有地方在用。 */
  /* 「运行位置」用两列网格而不是一句带分隔符的长文本 ——
     路径很长而端口很短，挤在一行里换行后必然对不齐，读起来像排版坏了。
     网格的标签列取 max-content，值列吃掉剩余宽度，天然左对齐。 */
  .wb-about-grid {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    align-items: center;
    column-gap: 14px;
    row-gap: 6px;
    margin-top: 4px;
  }
  /* 窄屏时 `max-content` 的键列会把值列挤得放不下（「数据目录」四个字加路径），
     改成键值上下堆叠，键小字灰色、值换行显示。 */
  @media (max-width: 560px) {
    .wb-about-grid {
      grid-template-columns: minmax(0, 1fr);
      row-gap: 2px;
    }
    .wb-about-k { margin-top: 6px; }
  }
  .wb-about-k {
    font-size: 11.5px;
    line-height: 1.6;
    color: var(--muted-foreground, #64748b);
    white-space: nowrap;
  }
  .wb-about-v {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    font-size: 12px;
    line-height: 1.6;
    color: var(--foreground, #0f172a);
  }
  .wb-about-v > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }





  /* ==================================================================
     主界面重新设计 v5（2026-10-16 第五版）—— 顶栏中央选项卡
     废弃 v4 的左侧边栏布局。用户需求：
       1) PC 端选项卡固定到正上方中央
       2) 移动端选项卡以收纳形式放在右上角
       3) 主题切换以单个图标放在收纳按钮前方
       4) 「概览」二字改为四字（与其他选项卡对齐）→ 数据总览
       5) 概览页增加更多重要数据

     布局（PC >=861px）：
       ┌────────────────────────────────────────────────────────┐
       │ logo   [账号][词元][积分][任务][设置]      主题(三段)      │  56px sticky
       ├────────────────────────────────────────────────────────┤
       │  内容区（官方页 或 数据总览视图）                        │
       └────────────────────────────────────────────────────────┘
       用 3 列网格保证选项卡真正居中（brand 左 / nav 中 / 主题右）。

     布局（移动端 <=860px）：
       ┌────────────────────────────────────────────────────────┐
       │ logo                          [☾ 主题][☰ 收纳]          │  48px
       ├────────────────────────────────────────────────────────┤
       │  内容区                                                │
       └────────────────────────────────────────────────────────┘
       点收纳按钮弹出下拉面板（6 个选项卡纵向列表）。
     ================================================================== */

  /* —— 0. 容器：column（顶栏在上，内容在下）—— */
  body > div#root > div,
  div.flex.h-screen {
    flex-direction: column !important;
  }

  /* —— ① 顶栏：外层只做「贴顶 + 底部细线」，内层才是真正的盒子 —— */
  /* 用户需求（2026-10-16）：「网页整体包含顶部以盒子形式展示，
     顶部不要两边无限延伸」。
     实测改前：顶栏 w=1425 / w=1905 通铺全宽，而内容区限宽 1240px 居中 —— 
     顶栏两侧「无限延伸」，视觉上头部和内容对不齐。
     改法：外层 aside 保留 sticky 与下边线，内层 #wb-topbar-inner 限宽
     1240px 居中 + 圆角盒子底 + 独立边框，与内容区左右严格对齐。 */
  aside {
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100% !important;
    height: auto !important;
    flex: 0 0 auto !important;
    display: block !important;
    padding: 0 !important;
    border-right: 0 !important;
    border-bottom: 1px solid var(--sidebar-border, rgba(120,120,120,0.16)) !important;
    background: color-mix(in oklab, var(--sidebar, #1b1e27) 88%, transparent) !important;
    -webkit-backdrop-filter: blur(20px) saturate(150%);
    backdrop-filter: blur(20px) saturate(150%);
    position: sticky !important;
    top: 0 !important;
    z-index: 9000 !important;
    overflow: visible !important;
  }

  /* 顶栏内盒子：与内容区严格同宽同左边缘。
     ⚠️ 为什么不用 max-width + margin:auto：
     实测 aside 可用宽 1425、而 main 里的内容 wrap 受官方 mx-auto 上下文影响，
     两者居中基点不同，用 max-width:1240 + margin:0 auto 会差 8px
     （内盒 x=93 而内容 x=85，「差一点没对齐」）。
     改用 width:100% + 内部再套一层限宽网格的方式，让内盒与 aside 同宽，
     再让真正的三列网格限宽 1240 居中 —— 这样 logo 左边缘由内层 grid 的
     padding 决定，与内容 wrap 的 padding 完全一致（都是 24px），严格对齐。 */
  #wb-topbar-inner {
    display: block !important;
    width: 100% !important;
    max-width: none !important;
    margin: 0 !important;
    padding: 0 !important;
    height: 72px !important;
  }

  /* 真正承载三列的那一层：盒模型与官方内容 wrap 完全一致
     （w-full + max-width:1180px + 左右 padding 24px），
     这样顶栏左边缘与内容左边缘严格相等，不会差几个像素。 */
  #wb-topbar-inner > .wb-tb-grid {
    display: grid !important;
    grid-template-columns: 1fr auto 1fr !important;
    align-items: center !important;
    gap: 12px !important;
    /* ⚠️ margin / width / max-width 都不能加 !important：
       wbSyncTopbarAlign() 要用内联样式把顶栏宽度钉成「当前页内容区实测宽度」
       （设置页是 max-w-3xl 768px，与默认 1180px 差 412px），
       !important 会盖过内联样式，导致顶栏永远比内容宽一截。
       2026-10-16 实测踩过：加了 !important 时设置页 grid 恒为 1180px。
       这里给普通声明作默认值，JS 内联样式优先级更高，可正常覆盖。 */
    width: 100%;
    max-width: 1180px;
    margin: 0 auto;
    padding: 0 24px !important;
    height: 72px !important;
  }

  /* aside 的直接子元素都改成 grid 项，避免 block 布局下堆叠 */
  aside > * { grid-column: auto !important; }

  /* 收纳态在 v5 无意义（顶栏不可折叠），防御性清掉 */
  aside.wb-collapsed { width: 100% !important; }

  /* —— ② logo 区（内盒子左上角）—— */
  .wb-tb-grid > div:first-child {
    grid-column: 1 !important;
    justify-self: start !important;
    display: flex !important;
    align-items: center !important;
    gap: 13px !important;
    padding: 0 !important;
    margin: 0 !important;
    border-right: 0 !important;
    border-bottom: 0 !important;
    width: auto !important;
    height: auto !important;
    cursor: default !important;
    background: transparent !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    transition: none !important;
    min-width: 0 !important;
  }
  .wb-tb-grid > div:first-child span {
    /* 品牌图标：随顶栏一起放大。32→38px（2026-10-17 用户要求「继续放大」）。 */
    width: 38px !important;
    height: 38px !important;
    flex-shrink: 0 !important;
  }
  /* 品牌文字「AutoBuddy」：官方原生 15px，随顶栏一起放到 18px。 */
  .wb-tb-grid > div:first-child div.min-w-0 > div {
    font-size: 18px !important;
    font-weight: 650 !important;
    line-height: 1.2 !important;
  }
  .wb-tb-grid > div:first-child div.min-w-0 { min-width: 0 !important; }

  /* —— ③ 选项卡：内盒子正上方中央 —— */
  .wb-tb-grid nav {
    grid-column: 2 !important;
    justify-self: center !important;
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    gap: 2px !important;
    width: auto !important;
    height: auto !important;
    padding: 0 !important;
    overflow: visible !important;
    min-width: 0 !important;
  }
  .wb-tb-grid nav a {
    flex: 0 0 auto !important;
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    gap: 8px !important;
    height: 46px !important;
    padding: 0 15px !important;
    border-radius: 11px !important;
    font-size: 14.5px !important;
    line-height: 1.2 !important;
    white-space: nowrap !important;
    color: var(--muted-foreground, #64748b) !important;
    text-decoration: none !important;
    transition: background 0.15s ease, color 0.15s ease !important;
    position: relative !important;
  }
  .wb-tb-grid nav a:hover {
    background: var(--accent, rgba(120,120,120,0.1)) !important;
    color: var(--foreground, #0f172a) !important;
  }
  /* 选中态：主色淡底 + 主色文字 + 底部短横条（顶栏用底条比左侧竖条自然）。
     ⚠️ 选择器必须用专用类 .wb-nav-active，不能用官方的 bg-foreground ——
     后者是 React 的状态类，会随 SPA 重渲染被重新分配，导致
     「离开页面后仍高亮 / 多项同时高亮」（2026-10-16 用户反馈）。 */
  .wb-tb-grid nav a.wb-nav-active {
    background: color-mix(in oklab, var(--primary, #22c55e) 13%, transparent) !important;
    color: var(--primary, #22c55e) !important;
    font-weight: 600 !important;
  }
  .wb-tb-grid nav a.wb-nav-active::before {
    content: "" !important;
    position: absolute !important;
    left: 12px !important;
    right: 12px !important;
    bottom: 3px !important;
    height: 2px !important;
    border-radius: 1px !important;
    background: var(--primary, #22c55e) !important;
  }
  .wb-tb-grid nav a svg { width: 20px !important; height: 20px !important; flex-shrink: 0 !important; }
  .wb-tb-grid nav a span.wb-nav-label { display: block !important; font-size: 15.5px !important; }

  /* —— ④ 主题三段控件：PC 端右上角 —— */
  #wb-theme-switch {
    grid-column: 3 !important;
    justify-self: end !important;
    display: flex !important;
    align-items: center !important;
    gap: 2px !important;
    padding: 3px !important;
    border-radius: 10px !important;
    background: var(--muted, rgba(120,120,120,0.12)) !important;
    border: 1px solid var(--border, rgba(120,120,120,0.14)) !important;
    margin: 0 !important;
  }
  #wb-theme-switch .wb-ts-btn {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    width: 38px !important;
    height: 34px !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 9px !important;
    background: transparent !important;
    color: var(--muted-foreground, #64748b) !important;
    cursor: pointer !important;
    transition: background 0.15s ease, color 0.15s ease !important;
  }
  #wb-theme-switch .wb-ts-btn:hover {
    background: var(--accent, rgba(120,120,120,0.1)) !important;
    color: var(--foreground, #0f172a) !important;
  }
  #wb-theme-switch .wb-ts-btn.wb-ts-on {
    background: var(--primary, #22c55e) !important;
    color: var(--primary-foreground, #ffffff) !important;
  }
  #wb-theme-switch .wb-ts-btn svg { width: 18px !important; height: 18px !important; }

  /* —— ⑤ 移动端专用：主题单图标 + 收纳按钮（PC 端隐藏）—— */
  #wb-theme-mini, #wb-nav-toggler { display: none !important; }

  /* —— ⑥ 主内容区 —— */
  main {
    flex: 1 1 auto !important;
    width: 100% !important;
    min-width: 0 !important;
    background: var(--background, #f6f7fb) !important;
  }
  /* 内容区限宽：**不**覆盖官方 wrap 的 max-width。
     官方 wrap 自带 mx-auto + w-full + max-w-[1180px] + px-6/sm:px-8，
     浏览器据此算出 margin-left:85px 并固定。我们若用 !important 改
     max-width，宽度变了但那个 85px 不重算，顶栏就与内容差 8px。
     所以这里只保留居中与内边距的兜底，max-width 交给官方 —— 
     顶栏网格（#wb-topbar-inner > .wb-tb-grid）用同一套盒模型对齐。 */
  /* 内容区统一限宽：**六个页面全部 1180px**。
     用户需求（2026-10-16）：「以账号管理/词元统计/积分统计三个页面为标准，
     剩下三个页面的宽度和横向位置对齐，6个页面宽度、横向起始位置全部统一」。
     官方设置页用 max-w-3xl（768px，x=321），与标准页 1180px（x=115）
     左右都差一截；数据总览/每日任务两个 hash 视图也要跟同一套。
     这里直接把 max-width 压成 1180px，居中由 margin auto 保证。 */
  main > div, main > section {
    max-width: 1180px !important;
    margin-left: auto !important;
    margin-right: auto !important;
    /* padding-top 36px = 官方 sm:py-9。不补的话设置页（原生 py-8=32px）
       的 H1 顶位置比其余五页高 4px —— 2026-10-17 六页体检实测。 */
    padding-top: 36px !important;
    padding-left: 24px !important;
    padding-right: 24px !important;
  }
  /* 主标题统一 28px：官方设置页原生是 24px，其余五页都是 28px，
     用户要求「六个页面样式完全统一」（2026-10-17）。
     账号/词元/积分三页原生即 28px，这条只影响设置页。 */
  main h1 { font-size: 28px !important; }

  /* ⚠️ 两个注入视图（数据总览 / 每日任务）本身也是 main 的直接子 div，
     会被上面的统一规则套上 1180px + 24px 左右 padding，
     于是它们自带的 .wb-ov-wrap / .wb-daily-wrap 只剩 1180-48=1132px 可用，
     实测总览页 wrap 变成 x=139 / w=1132，与官方页 x=115 / w=1180 对不上
     （2026-10-16 实测）。
     这两个视图自带限宽 wrap（1180px + 16px 左右 padding，与官方 px-4 一致），
     外层必须是通铺、无内边距，让 wrap 自己居中。 */
  #wb-overview-view, #wb-daily-view {
    max-width: none !important;
    width: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
  }

  /* —— ⑦ 数据总览页 —— */
  /* 与其他五个页面统一：1180px 居中（官方标准页就是 1180px）。 */
  /* 与其他五个页面统一：1180px 居中、左右内边距 16px（与官方 max-w-[1180px] 的
     px-4 完全一致），这样总览页内容区的**实测宽度与左边缘**和官方页逐像素相等。 */
  #wb-overview-view .wb-ov-wrap {
    max-width: 1180px;
    margin: 0 auto;
    /* padding-top 36px 与官方页 sm:py-9 一致 —— H1 顶位置才能对齐
       （2026-10-17 体检：原 28px 时概览 H1 y=93，官方页 y=101）。 */
    padding: 36px 16px 40px;
  }
  .wb-ov-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }
  .wb-ov-h1 {
    font-size: 28px;
    font-weight: 600;
    line-height: 1.25;
    letter-spacing: -.02em;
    color: var(--foreground, #0f172a);
    margin: 0 0 4px;
  }
  .wb-ov-sub {
    font-size: 13px;
    color: var(--muted-foreground, #64748b);
    margin: 0;
  }
  .wb-ov-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .wb-ov-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 32px;
    padding: 0 13px;
    border-radius: 8px;
    border: 1px solid var(--border, rgba(120,120,120,0.18));
    background: var(--card, #ffffff);
    color: var(--foreground, #0f172a);
    font-size: 12.5px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s ease;
  }
  .wb-ov-btn:hover { background: var(--accent, rgba(120,120,120,0.1)); }
  .wb-ov-btn svg { width: 14px; height: 14px; }
  .wb-ov-btn-primary {
    background: var(--primary, #22c55e);
    border-color: transparent;
    color: var(--primary-foreground, #ffffff);
    font-weight: 600;
  }
  .wb-ov-btn-primary:hover { filter: brightness(1.06); background: var(--primary, #22c55e); }
  .wb-ov-btn:disabled { opacity: 0.6; cursor: default; }

  /* KPI 网格：两行各 4 张 */
  #wb-ov-kpi-row, #wb-ov-kpi-row2 {
    display: grid !important;
    grid-template-columns: repeat(4, 1fr) !important;
    gap: 12px !important;
    margin: 0 0 12px 0 !important;
  }
  .wb-kpi {
    background: var(--card, #ffffff) !important;
    border: 1px solid var(--border, rgba(120,120,120,0.16)) !important;
    border-radius: 12px !important;
    padding: 12px 14px 11px !important;
    min-width: 0 !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
  }
  .wb-kpi-label {
    font-size: 12.5px !important;
    color: var(--muted-foreground, #64748b) !important;
    margin-bottom: 3px !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
  }
  .wb-kpi-value {
    font-size: 26px !important;
    font-weight: 700 !important;
    line-height: 1.15 !important;
    color: var(--foreground, #0f172a) !important;
    font-variant-numeric: tabular-nums !important;
    letter-spacing: -.02em !important;
  }
  .wb-kpi-badge {
    display: inline-flex !important;
    align-items: center !important;
    gap: 3px !important;
    margin-top: 5px !important;
    height: 18px !important;
    padding: 0 7px !important;
    border-radius: 9px !important;
    font-size: 11.5px !important;
    font-weight: 600 !important;
    white-space: nowrap !important;
  }
  .wb-kpi-badge-up { background: color-mix(in oklab, #22c55e 15%, transparent) !important; color: #15803d !important; }
  .wb-kpi-badge-down { background: color-mix(in oklab, #f59e0b 16%, transparent) !important; color: #b45309 !important; }
  .wb-kpi-badge-flat { background: var(--muted, rgba(120,120,120,0.12)) !important; color: var(--muted-foreground, #64748b) !important; }

  /* 中栏：趋势图 + 任务状态 */
  #wb-ov-mid-row {
    display: grid !important;
    grid-template-columns: 2fr 1fr !important;
    gap: 12px !important;
    margin: 0 0 12px 0 !important;
  }
  /* 底栏：账号池 + 最近执行 */
  /* 底栏只有「最近执行」一张卡片（账号池相关内容已移除，
     用户 2026-10-17 要求），所以从两列网格改回单列。 */
  #wb-ov-bottom-row {
    display: grid !important;
    grid-template-columns: 1fr !important;
    gap: 12px !important;
    margin: 0 !important;
  }
  .wb-panel {
    background: var(--card, #ffffff) !important;
    border: 1px solid var(--border, rgba(120,120,120,0.16)) !important;
    border-radius: 12px !important;
    padding: 14px 16px !important;
    min-width: 0 !important;
  }
  .wb-panel-head {
    display: flex !important;
    align-items: baseline !important;
    justify-content: space-between !important;
    gap: 10px !important;
    margin-bottom: 10px !important;
  }
  .wb-panel-title {
    font-size: 14px !important;
    font-weight: 650 !important;
    color: var(--foreground, #0f172a) !important;
  }
  .wb-panel-sub {
    font-size: 12px !important;
    color: var(--muted-foreground, #64748b) !important;
    margin-top: 1px !important;
  }
  .wb-panel-chip {
    font-size: 11.5px !important;
    color: var(--muted-foreground, #64748b) !important;
    background: var(--muted, rgba(120,120,120,0.1)) !important;
    border-radius: 8px !important;
    padding: 2px 8px !important;
    white-space: nowrap !important;
  }
  #wb-trend-svg { display: block !important; width: 100% !important; height: 130px !important; }
  #wb-trend-svg .wb-trend-area { fill: color-mix(in oklab, var(--primary, #22c55e) 18%, transparent) !important; }
  #wb-trend-svg .wb-trend-line { fill: none !important; stroke: var(--primary, #22c55e) !important; stroke-width: 2 !important; }
  #wb-trend-svg .wb-trend-axis { stroke: var(--border, rgba(120,120,120,0.2)) !important; stroke-width: 1 !important; }
  #wb-trend-svg text { font-size: 8.5px !important; fill: var(--muted-foreground, #64748b) !important; }
  #wb-trend-svg .wb-trend-dot { fill: var(--primary, #22c55e) !important; }
  .wb-task-bar {
    height: 8px !important;
    border-radius: 4px !important;
    background: var(--muted, rgba(120,120,120,0.14)) !important;
    overflow: hidden !important;
    margin: 4px 0 8px !important;
  }
  .wb-task-bar > div {
    height: 100% !important;
    border-radius: 4px !important;
    background: var(--primary, #22c55e) !important;
    transition: width 0.4s ease !important;
  }
  .wb-task-row {
    display: flex !important;
    align-items: baseline !important;
    justify-content: space-between !important;
    gap: 10px !important;
    font-size: 12.5px !important;
    padding: 4px 0 !important;
  }
  .wb-task-row span { color: var(--muted-foreground, #64748b) !important; }
  .wb-task-row b { color: var(--foreground, #0f172a) !important; font-variant-numeric: tabular-nums !important; }

  /* 最近执行列表 */
  #wb-ov-runs { display: flex; flex-direction: column; gap: 6px; }
  .wb-run-row {
    display: flex; align-items: center; gap: 9px;
    font-size: 12.5px; padding: 7px 9px;
    border-radius: 8px; background: var(--muted, rgba(120,120,120,0.07));
  }
  .wb-run-dot {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
    background: var(--muted-foreground, #64748b);
  }
  .wb-run-dot.ok { background: #22c55e; }
  .wb-run-dot.err { background: #ef4444; }
  .wb-run-mode {
    flex: 0 0 auto; font-size: 11.5px; padding: 2px 7px; border-radius: 6px;
    background: var(--muted, rgba(120,120,120,0.14)); color: var(--muted-foreground, #64748b);
  }
  .wb-run-time { flex: 0 0 auto; color: var(--muted-foreground, #64748b); font-variant-numeric: tabular-nums; }
  .wb-run-detail {
    flex: 1 1 auto; min-width: 0; text-align: right;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: var(--foreground, #0f172a);
  }
  /* 行可点：给一点可点击暗示（用户 2026-10-16 要求点击展开明细） */
  .wb-run-clickable { cursor: pointer; transition: background 0.15s ease; }
  .wb-run-clickable:hover { background: var(--accent, rgba(120,120,120,0.13)); }
  .wb-run-clickable:focus-visible { outline: 2px solid var(--ring, #3b82f6); outline-offset: 1px; }

  /* 顶栏导航焦点环（2026-10-17 修复「顶部选项卡焦点问题」）。
     根因：官方导航链接的 className 里带 Tailwind 的 `outline-none`
     （实测 6 个官方项 + 注入的「每日任务」全都带），
     于是键盘用户按 Tab 切换选项卡时**完全看不到焦点在哪** ——
     outlineStyle 实测为 none，:focus-visible 也无任何可见样式。
     鼠标点击有 hover/active 兜底看不出问题，纯键盘操作就等于盲人摸象。
     这里给顶栏内所有可聚焦元素补一个 :focus-visible 焦点环，
     用官方 --ring 变量，深浅主题自动跟随；
     只在键盘聚焦时出现（:focus-visible 不会因鼠标点击而触发），
     不影响既有视觉。 */
  #wb-topbar-inner nav a:focus-visible,
  #wb-topbar-inner button:focus-visible,
  #wb-theme-mini:focus-visible,
  #wb-nav-toggler:focus-visible {
    outline: 2px solid var(--ring, #3b82f6) !important;
    outline-offset: 2px !important;
  }
  /* 展开指示箭头：默认朝下，展开时翻转朝上 */
  .wb-run-caret {
    flex: 0 0 auto; font-size: 11px; line-height: 1; margin-left: 2px;
    color: var(--muted-foreground, #64748b);
    transition: transform 0.18s ease;
  }
  .wb-run-open .wb-run-caret { transform: rotate(180deg); }
  .wb-run-open { background: var(--accent, rgba(120,120,120,0.13)); }

  /* 每日任务页「执行记录」行的展开箭头（与概览页同一套交互：
     点行展开明细、再点一次收纳，详见 wbToggleRunDetail）。 */
  .wb-dl-run-caret {
    flex: 0 0 auto; font-size: 11px; line-height: 1; margin-left: 2px;
    color: var(--muted-foreground, #64748b);
    transition: transform 0.18s ease;
  }
  .wb-dl-run-open .wb-dl-run-caret { transform: rotate(180deg); }
  .wb-dl-run-open { background: var(--accent, rgba(120,120,120,0.13)); }
  /* 行内明细容器：插在被点行的正下方，左右撑满列表，
     不与上面的行共享内边距（明细自带 padding）。 */
  .wb-dl-run-detail {
    flex-basis: 100%;
    width: 100%;
    padding: 4px 8px 8px;
    border-left: 2px solid var(--border, rgba(120,120,120,0.25));
    margin: 2px 0 4px;
  }
  .wb-dl-run-detail::-webkit-scrollbar { width: 5px; height: 5px; }
  .wb-dl-run-detail::-webkit-scrollbar-thumb {
    background: rgba(120,120,120,0.35); border-radius: 3px;
  }

  /* 行下方明细卡：插在被点行的正下方，宽度撑满列表。
     ⚠️ 固定 max-height + 内部滚动（用户 2026-10-17 要求：「点击任务显示对应
     信息在下方符合预期，但是不应该撑大卡片大小，显示不下的内容应该加一个
     滑动条，用户可以自行滑动查看」）。账号多时明细很长，不限制高度会把
     「最近执行」卡片撑爆，把 KPI 和趋势图全部挤下去。 */
  .wb-run-detailbox {
    margin: 2px 0 2px; padding: 10px 12px;
    border-radius: 8px;
    border: 1px dashed var(--border, rgba(120,120,120,0.22));
    background: var(--background, #ffffff);
    font-size: 12.5px;
    max-height: 260px;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-width: thin;
  }
  .wb-run-detailbox::-webkit-scrollbar { width: 5px; }
  .wb-run-detailbox::-webkit-scrollbar-thumb {
    background: rgba(120,120,120,0.35); border-radius: 3px;
  }
  /* 明细里的逐账号表：横向滚动由表格容器负责，这里只保证不溢出
     （账号名/积分列都很长，390px 窄屏必然超宽）。 */
  .wb-run-detailbox .wb-rund-tbl { overflow-x: auto; }
  .wb-run-detail-loading { color: var(--muted-foreground, #64748b); font-size: 12.5px; }
  .wb-rund-h { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
  .wb-rund-h b { font-size: 12.5px; font-variant-numeric: tabular-nums; color: var(--foreground, #0f172a); }
  .wb-rund-st { font-size: 11.5px; padding: 2px 8px; border-radius: 6px; flex: 0 0 auto; }
  .wb-rund-st-ok { background: rgba(34,197,94,.14); color: #047857; }
  .wb-rund-st-run { background: rgba(59,130,246,.14); color: #1d4ed8; }
  .wb-rund-st-err { background: rgba(239,68,68,.14); color: #b91c1c; }
  .wb-rund-kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; align-items: baseline; }
  .wb-rund-kv span { color: var(--muted-foreground, #64748b); }
  .wb-rund-kv b { color: var(--foreground, #0f172a); font-variant-numeric: tabular-nums; font-weight: 600; }
  .wb-rund-tbl { margin-top: 8px; border-radius: 6px; overflow: hidden; }
  .wb-rund-tr {
    display: grid; grid-template-columns: 1.5fr .7fr .5fr .5fr; gap: 6px;
    padding: 3px 6px; font-size: 10.5px; align-items: center;
  }
  .wb-rund-tr:nth-child(odd) { background: var(--muted, rgba(120,120,120,0.06)); }
  .wb-rund-th { color: var(--muted-foreground, #64748b); font-weight: 600; }
  .wb-rund-acc { font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wb-rund-more { margin-top: 4px; color: var(--muted-foreground, #64748b); font-size: 10px; }
  .wb-rund-note { margin-top: 6px; color: var(--muted-foreground, #64748b); font-size: 10.5px; line-height: 1.5; }
  .wb-rund-acts { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
  .wb-rund-acts span {
    font-size: 9.5px; padding: 1px 6px; border-radius: 5px;
    background: var(--muted, rgba(120,120,120,0.12)); color: var(--muted-foreground, #64748b);
  }

  /* 设置页小标题间距 */
  h2[id^="settings-"] {
    font-size: 12.5px !important;
    font-weight: 600 !important;
    letter-spacing: 0.03em !important;
    color: var(--muted-foreground, #64748b) !important;
  }

  /* —— ⑧ 收纳下拉面板（PC 端不用，移动端专用）—— */
  #wb-nav-panel {
    display: none;
    position: absolute !important;
    top: calc(100% + 8px) !important;
    right: 8px !important;
    left: auto !important;
    z-index: 9500 !important;
    min-width: 176px !important;
    max-width: min(80vw, 280px) !important;
    padding: 4px !important;
    border-radius: 12px !important;
    border: 1px solid var(--border, rgba(120,120,120,0.18)) !important;
    background: color-mix(in oklab, var(--popover, var(--card, #ffffff)) 94%, transparent) !important;
    -webkit-backdrop-filter: blur(20px) saturate(150%);
    backdrop-filter: blur(20px) saturate(150%);
    box-shadow: 0 10px 34px rgba(0,0,0,0.18) !important;
    flex-direction: column !important;
    gap: 2px !important;
  }
  #wb-nav-panel.wb-nav-panel-open { display: flex !important; }
  #wb-nav-panel .wb-nav-panel-item {
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    gap: 10px !important;
    padding: 10px 12px !important;
    border-radius: 9px !important;
    font-size: 13px !important;
    white-space: nowrap !important;
    color: var(--foreground, #0f172a) !important;
    text-decoration: none !important;
    cursor: pointer !important;
    transition: background 0.14s ease !important;
  }
  #wb-nav-panel .wb-nav-panel-item:hover { background: var(--accent, rgba(120,120,120,0.1)) !important; }
  #wb-nav-panel .wb-nav-panel-item svg { width: 16px !important; height: 16px !important; flex-shrink: 0 !important; }
  #wb-nav-panel .wb-nav-panel-item.wb-nav-panel-active {
    background: color-mix(in oklab, var(--primary, #22c55e) 13%, transparent) !important;
    color: var(--primary, #22c55e) !important;
    font-weight: 600 !important;
  }
  #wb-nav-panel .wb-nav-panel-empty {
    padding: 10px 12px !important;
    font-size: 12px !important;
    color: var(--muted-foreground, #64748b) !important;
  }

  /* —— ⑨ 移动端（<=860px）：选项卡收纳到右上角 —— */
  @media (max-width: 860px) {
    /* 外层仍是贴顶条；内盒子退化为 3 列网格 */
    aside { display: block !important; padding: 0 !important; }
    /* ⚠️ 必须写成 ID 选择器：桌面规则 `#wb-topbar-inner > .wb-tb-grid` 是
       (1,1,0) 优先级，裸类选择器 `.wb-tb-grid` 是 (0,1,0)，
       媒体查询再靠后也赢不了它 —— 2026-10-16 实测移动端列数仍是桌面的
       143px 32px 143px、padding 仍是 24px、height 仍是 56px（溢出 48px inner）。
       主题图标与收纳按钮因此被 1fr 第三列摊开、散在中间，正是「排版混乱」。 */
    #wb-topbar-inner > .wb-tb-grid {
      grid-template-columns: 1fr auto auto !important;
      max-width: none !important;
      height: 62px !important;
      /* 与 main > div 的左右 padding 一致（16px），保证 logo 与内容左对齐 */
      padding: 0 16px !important;
      gap: 8px !important;
    }
    #wb-topbar-inner { height: 62px !important; }
    /* logo 只留图标（窄屏放不下名字） */
    .wb-tb-grid > div:first-child { grid-column: 1 !important; }
    .wb-tb-grid > div:first-child div.min-w-0 { display: none !important; }
    /* 选项卡：PC 的横排导航整体隐藏，改走收纳面板 */
    .wb-tb-grid nav { display: none !important; }
    /* 主题三段控件隐藏，改用单图标 */
    #wb-theme-switch { display: none !important; }
    /* 主题单图标 + 收纳按钮：右上角，顺序为「主题 → 收纳」 */
    #wb-theme-mini {
      display: inline-flex !important;
      grid-column: 2 !important;
      justify-self: center !important;
      align-items: center !important;
      justify-content: center !important;
      width: 42px !important;
      height: 42px !important;
      padding: 0 !important;
      border: 1px solid var(--border, rgba(120,120,120,0.16)) !important;
      border-radius: 11px !important;
      background: var(--muted, rgba(120,120,120,0.1)) !important;
      color: var(--muted-foreground, #64748b) !important;
      cursor: pointer !important;
      transition: background 0.15s ease, color 0.15s ease !important;
    }
    #wb-theme-mini:hover { background: var(--accent, rgba(120,120,120,0.14)) !important; }
    #wb-theme-mini svg { width: 20px !important; height: 20px !important; }
    #wb-nav-toggler {
      display: inline-flex !important;
      grid-column: 3 !important;
      justify-self: center !important;
      align-items: center !important;
      justify-content: center !important;
      width: 42px !important;
      height: 42px !important;
      padding: 0 !important;
      border: 1px solid var(--border, rgba(120,120,120,0.16)) !important;
      border-radius: 11px !important;
      background: var(--muted, rgba(120,120,120,0.1)) !important;
      color: var(--muted-foreground, #64748b) !important;
      cursor: pointer !important;
      transition: background 0.15s ease, color 0.15s ease !important;
    }
    #wb-nav-toggler:hover { background: var(--accent, rgba(120,120,120,0.14)) !important; }
    #wb-nav-toggler svg { width: 20px !important; height: 20px !important; }
    /* 收纳面板在顶栏下方、右侧弹出 */
    #wb-nav-panel {
      top: calc(100% + 6px) !important;
      right: 8px !important;
      left: auto !important;
      bottom: auto !important;
    }
    /* 内容区留出底部空间（底部已无常驻栏） */
    /* padding-top 24px = 官方 py-6。账号页原生是 py-8(32px)，
       不补的话移动端它的 H1 顶位置比其余五页高 8px。 */
    main > div, main > section { padding-top: 24px !important; padding-bottom: 24px !important; }
    /* padding-top 24px 与官方页 py-6 一致（H1 顶位置对齐）。 */
    #wb-overview-view .wb-ov-wrap { padding: 24px 14px 24px !important; }
    /* KPI 2×2 */
    #wb-ov-kpi-row, #wb-ov-kpi-row2 { grid-template-columns: repeat(2, 1fr) !important; gap: 10px !important; }
    .wb-kpi-value { font-size: 19px !important; }
    /* 中栏/底栏堆叠 */
    #wb-ov-mid-row, #wb-ov-bottom-row { grid-template-columns: 1fr !important; gap: 10px !important; }
    /* 右下角用户球避开顶部不影响，保持在右下 */
    #wb-user-orb { bottom: 16px !important; right: 12px !important; }
  }

  /* 极窄屏（<=560px） */
  @media (max-width: 560px) {
    #wb-topbar-inner > .wb-tb-grid { padding: 0 12px !important; }
    main > div, main > section { padding-left: 12px !important; padding-right: 12px !important; }
    #wb-overview-view .wb-ov-wrap { padding-left: 12px !important; padding-right: 12px !important; }
    .wb-kpi { padding: 10px 11px 9px !important; }
    .wb-kpi-value { font-size: 17px !important; }
    #wb-theme-mini, #wb-nav-toggler { width: 40px !important; height: 40px !important; }
  }

  /* PC 端中等宽度：选项卡压缩一点，避免与 logo/主题挤在一起 */
  @media (min-width: 861px) and (max-width: 1080px) {
    #wb-topbar-inner > .wb-tb-grid { padding: 0 12px !important; gap: 8px !important; }
    .wb-tb-grid nav a { padding: 0 10px !important; font-size: 13.5px !important; gap: 6px !important; }
    .wb-tb-grid nav a svg { width: 17px !important; height: 17px !important; }
    .wb-tb-grid > div:first-child span { width: 32px !important; height: 32px !important; }
  }
</style>
<script>
(function() {

  /* ------------------- 右上角实时进度指示器 -------------------
     参考 OutlookRegister 的做法：把「当前正在跑什么、到哪一步、进度百分之几」
     常驻在右上角，切到别的页面也能看到。

     ⚠️ 曾经反复闪烁的根因与现在约法：
     旧实现每次轮询（2s 一次）都往 document 里 append 一个新的 pill，
     并且每次非运行态都重开一个 6s 淡出定时器 —— 于是看见的就是「不断重新出现 / 消失」。
     现在的规则：
       1) 节点只在首次创建，后续只改文本与颜色（不再重建 DOM）；
       2) 淡出定时器全局唯一，同一轮只计一次；
       3) 内容未发生变化时连样式计算都不做，彻底避免无谓重绘。 */
  var __wbPillState = { key: "", fadeTimer: null };

  function wbEnsureProgressPill() {
    var existing = document.getElementById("wb-progress-pill");
    if (existing) return existing;
    var el = document.createElement("div");
    el.id = "wb-progress-pill";
    el.style.cssText = [
      "position:fixed", "top:14px", "right:16px", "z-index:99998",
      "display:none", "align-items:center", "gap:8px",
      "padding:6px 12px", "border-radius:999px",
      "background:var(--card,#ffffff)", "color:var(--foreground,#0f172a)",
      "border:1px solid var(--border,rgba(120,120,120,0.28))",
      "box-shadow:0 4px 14px rgba(15,23,42,0.14)",
      "font-size:12px", "line-height:1.5", "max-width:min(320px,60vw)",
      "cursor:pointer"
    ].join(";");
    el.innerHTML =
      '<span id="wb-progress-spin" style="width:10px;height:10px;border-radius:50%;flex:0 0 auto;background:#3b82f6;"></span>' +
      '<span id="wb-progress-text" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">准备中…</span>' +
      '<span id="wb-progress-pct" style="font-variant-numeric:tabular-nums;font-weight:600;flex:0 0 auto;">0%</span>';
    document.body.appendChild(el);
    return el;
  }

  /* 渲染进度胶囊。
     status: running / pending / done / failed / aborted / idle(或 none 表示隐藏)
     同一 (status, progress, stepText) 组合只渲染一次，重复轮询不会产生任何 DOM 写入。 */
  function wbRenderProgressPill(status, progress, stepText) {
    var el = wbEnsureProgressPill();
    if (!el) return;
    // idle / none：隐藏并清掉淡出定时器，不重建节点
    if (status === "idle" || status === "none") {
      if (__wbPillState.fadeTimer) { clearTimeout(__wbPillState.fadeTimer); __wbPillState.fadeTimer = null; }
      if (el.style.display !== "none") el.style.display = "none";
      __wbPillState.key = "";
      return;
    }

    var running = (status === "running" || status === "pending");
    var pct = progress || "0%";
    var text = stepText || "注册进行中";
    var key = status + "|" + pct + "|" + text;

    // 内容无变化：不碰 DOM、不重置定时器（这就是闪烁的根治点）
    if (key === __wbPillState.key && el.style.display === "inline-flex") return;
    __wbPillState.key = key;

    if (el.style.display !== "inline-flex") el.style.display = "inline-flex";
    var pctEl = document.getElementById("wb-progress-pct");
    var txtEl = document.getElementById("wb-progress-text");
    var dotEl = document.getElementById("wb-progress-spin");
    if (pctEl && pctEl.textContent !== pct) pctEl.textContent = pct;
    if (txtEl && txtEl.textContent !== text) txtEl.textContent = text;
    if (dotEl) {
      var color = running ? "#3b82f6" : (status === "done" ? "#10b981" : "#f59e0b");
      if (dotEl.style.background !== color) dotEl.style.background = color;
    }

    // 结束后 8 秒自动淡出；定时器全局唯一，重复渲染不会续期
    if (!running) {
      if (__wbPillState.fadeTimer) return;
      __wbPillState.fadeTimer = setTimeout(function () {
        var node = document.getElementById("wb-progress-pill");
        if (node) node.style.display = "none";
        __wbPillState.fadeTimer = null;
        __wbPillState.key = "";
      }, 8000);
    }
  }

  /* 侧边栏左上角品牌文字实时校准。
     官方产物里旧名有「WorkBuddy Switch」与不含 Switch 的裸「WorkBuddy」两种形态，
     后者与业务文案里的 WorkBuddy（账号池指代）同形，所以只处理 aside 内的品牌节点：
     div.truncate 是侧边栏标题容器，业务文案不会出现在这里。 */
  function sanitizeSidebarBrand() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    var truncates = aside.querySelectorAll("div.truncate");
    truncates.forEach(function(el) {
      if (el.textContent && (el.textContent.indexOf("WorkBuddy") !== -1 || el.textContent.indexOf("Switch") !== -1)) {
        el.textContent = "AutoBuddy";
      }
    });
  }

  /* ------------------- WebUI 用户安全认证与登录模态框 ------------------- */
  var __wbAuthChecked = false;
  var __wbAuthChecking = false;
  var __wbCurrentUser = null;

  function wbCheckAuth() {
    if (__wbAuthChecked || __wbAuthChecking) return;
    __wbAuthChecking = true;
    fetch("/api/auth/status")
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!data.enabled) {
          __wbAuthChecked = true;
          return;
        }
        if (!data.authenticated) {
          wbShowLoginModal();
        } else {
          __wbAuthChecked = true;
          __wbCurrentUser = data.username;
          __wbAuthData = data;
          wbRenderUserOrb(data);
        }
      })
      .catch(function() {
        __wbAuthChecked = true;
      })
      .finally(function() {
        __wbAuthChecking = false;
      });
  }

  function wbShowLoginModal() {
    if (document.getElementById("wb-login-overlay")) return;
    var overlay = document.createElement("div");
    overlay.id = "wb-login-overlay";
    // 遮罩必须真正挡住后面的内容：此前 0.78 透明度 + 6px 模糊，
    // 在高对比度主题下仍能辨认出底层文字与卡片，登录框看起来像「浮在内容上面」。
    // 这里同时提高底色不透明度与模糊半径，并保留 -webkit- 前缀兼容 Safari。
    overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,0.92);backdrop-filter:blur(18px) saturate(120%);-webkit-backdrop-filter:blur(18px) saturate(120%);z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px;";
    
    var card = document.createElement("div");
    card.style.cssText = "background:var(--card,#ffffff);color:var(--foreground,#0f172a);border-radius:16px;box-shadow:0 20px 40px rgba(0,0,0,0.25);width:100%;max-width:380px;padding:28px 24px;border:1px solid var(--border,rgba(120,120,120,0.2));font-family:inherit;";
    
    card.innerHTML = 
      '<div style="text-align:center;margin-bottom:20px;">' +
        '<img src="/icon.png" style="width:56px;height:56px;border-radius:14px;box-shadow:0 4px 12px rgba(0,0,0,0.12);margin:0 auto 12px;display:block;" />' +
        '<div style="font-size:18px;font-weight:700;letter-spacing:-0.02em;">AutoBuddy</div>' +
        '<div style="font-size:12px;color:var(--muted-foreground,#64748b);margin-top:4px;">&nbsp;</div>' +
      '</div>' +
      '<div style="display:flex;flex-direction:column;gap:12px;">' +
        '<div>' +
          '<label style="font-size:12px;font-weight:500;display:block;margin-bottom:4px;">账号</label>' +
          '<input id="wb-login-user" type="text" autocomplete="off" placeholder="" style="width:100%;padding:8px 12px;border-radius:8px;border:1px solid var(--border,rgba(120,120,120,0.3));background:var(--background,#ffffff);color:inherit;font-size:13px;box-sizing:border-box;" />' +
        '</div>' +
        '<div>' +
          '<label style="font-size:12px;font-weight:500;display:block;margin-bottom:4px;">密码</label>' +
          '<input id="wb-login-pass" type="password" autocomplete="new-password" placeholder="" style="width:100%;padding:8px 12px;border-radius:8px;border:1px solid var(--border,rgba(120,120,120,0.3));background:var(--background,#ffffff);color:inherit;font-size:13px;box-sizing:border-box;" />' +
        '</div>' +
        '<div id="wb-login-slider-wrap" style="margin-top:4px;">' +
          '<div id="wb-login-slider" style="position:relative;height:38px;border-radius:8px;background:var(--muted,rgba(120,120,120,0.12));overflow:hidden;user-select:none;-webkit-user-select:none;touch-action:none;">' +
            '<div id="wb-login-slider-fill" style="position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,rgba(59,130,246,0.18),rgba(59,130,246,0.32));"></div>' +
            '<div id="wb-login-slider-text" style="position:absolute;left:0;right:0;top:0;bottom:0;display:flex;align-items:center;justify-content:center;font-size:12.5px;color:var(--muted-foreground,#64748b);pointer-events:none;letter-spacing:0.02em;">按住滑块，拖动到最右侧</div>' +
            '<div id="wb-login-slider-handle" style="position:absolute;left:2px;top:2px;width:44px;height:34px;border-radius:6px;background:#ffffff;box-shadow:0 1px 4px rgba(0,0,0,0.18);display:flex;align-items:center;justify-content:center;cursor:grab;transition:background 0.15s;">' +
              '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div id="wb-login-err" style="color:#ef4444;font-size:12px;display:none;margin-top:2px;"></div>' +
        '<button id="wb-login-submit" type="button" style="margin-top:6px;width:100%;padding:9px;border-radius:8px;background:#3b82f6;color:#ffffff;font-size:13px;font-weight:600;border:none;cursor:pointer;transition:background 0.2s;">登 录</button>' +
      '</div>';
      
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    var subBtn = document.getElementById("wb-login-submit");
    var userIn = document.getElementById("wb-login-user");
    var passIn = document.getElementById("wb-login-pass");
    var errEl = document.getElementById("wb-login-err");

    var sliderOk = false;
    var sliderBox = document.getElementById("wb-login-slider");
    var sliderHandle = document.getElementById("wb-login-slider-handle");
    var sliderFill = document.getElementById("wb-login-slider-fill");
    var sliderText = document.getElementById("wb-login-slider-text");
    var sliderDragging = false;
    var sliderStartX = 0;

    function sliderMax() {
      return Math.max(0, sliderBox.clientWidth - sliderHandle.offsetWidth - 4);
    }

    function sliderMove(clientX) {
      var max = sliderMax();
      var delta = clientX - sliderStartX;
      var left = Math.min(max, Math.max(0, delta + 2));
      sliderHandle.style.left = left + "px";
      sliderFill.style.width = left + "px";
      if (left >= max - 1) {
        sliderOk = true;
        sliderFill.style.background = "linear-gradient(90deg,rgba(16,185,129,0.22),rgba(16,185,129,0.42))";
        sliderHandle.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
        sliderText.textContent = "验证通过";
        sliderText.style.color = "#10b981";
      }
    }

    function sliderReset() {
      sliderOk = false;
      sliderDragging = false;
      sliderHandle.style.left = "2px";
      sliderFill.style.width = "0px";
      sliderFill.style.background = "linear-gradient(90deg,rgba(59,130,246,0.18),rgba(59,130,246,0.32))";
      sliderHandle.style.cursor = "grab";
      sliderHandle.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>';
      sliderText.textContent = "按住滑块，拖动到最右侧";
      sliderText.style.color = "var(--muted-foreground,#64748b)";
    }

    sliderHandle.addEventListener("pointerdown", function(e) {
      if (sliderOk) return;
      sliderDragging = true;
      sliderStartX = e.clientX;
      sliderHandle.style.cursor = "grabbing";
      try { sliderHandle.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    sliderHandle.addEventListener("pointermove", function(e) {
      if (!sliderDragging || sliderOk) return;
      sliderMove(e.clientX);
    });
    sliderHandle.addEventListener("pointerup", function(e) {
      if (!sliderDragging) return;
      sliderDragging = false;
      sliderHandle.style.cursor = "grab";
      try { sliderHandle.releasePointerCapture(e.pointerId); } catch (err) {}
      if (!sliderOk) sliderReset();
    });
    sliderHandle.addEventListener("pointercancel", function() {
      if (!sliderOk) sliderReset();
    });
    window.addEventListener("resize", function() {
      if (document.getElementById("wb-login-overlay")) sliderReset();
    });

    function doLogin() {
      var u = (userIn.value || "").trim();
      var p = (passIn.value || "").trim();
      if (!u || !p) {
        errEl.textContent = "请输入账号和密码";
        errEl.style.display = "block";
        return;
      }
      if (!sliderOk) {
        errEl.textContent = "请先完成滑动验证";
        errEl.style.display = "block";
        return;
      }
      subBtn.disabled = true;
      subBtn.textContent = "登录中…";
      errEl.style.display = "none";

      fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: u, password: p })
      })
      .then(function(r) { return r.json(); })
      .then(function(res) {
        subBtn.disabled = false;
        subBtn.textContent = "登 录";
        if (res.ok) {
          overlay.remove();
          __wbAuthChecked = true;
          __wbCurrentUser = res.username;
          wbToast("登录成功，欢迎使用 AutoBuddy！", "ok");
          window.location.reload();
        } else {
          errEl.textContent = res.error || "账号或密码错误";
          errEl.style.display = "block";
          sliderReset();
        }
      })
      .catch(function() {
        subBtn.disabled = false;
        subBtn.textContent = "登 录";
        errEl.textContent = "登录请求失败，请检查网络";
        errEl.style.display = "block";
        sliderReset();
      });
    }

    subBtn.onclick = doLogin;
    passIn.onkeydown = function(e) { if (e.key === "Enter") doLogin(); };
    userIn.onkeydown = function(e) { if (e.key === "Enter") passIn.focus(); };
    setTimeout(function() { passIn.focus(); }, 100);
  }

  var __wbAuthData = null;

  // 内置圆形头像候选：与官方图标同风格（渐变底 + 简约符号），全部为内联 SVG。
  // 选中的候选由后端存为 "preset:<key>"，/api/auth/avatar 按其返回。
  var WB_AVATAR_PRESETS = {
    moon: { name: "月牙", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M42 40a14 14 0 0 1-16-21 16 16 0 1 0 22 15 14 14 0 0 1-6 6z" fill="#fff"/></svg>' },
    star: { name: "星辰", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0ea5e9"/><stop offset="1" stop-color="#6366f1"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M32 16l4.6 10.2L48 28l-8.4 7.6L41.8 47 32 41.4 22.2 47l2.2-11.4L16 28l11.4-1.8z" fill="#fff"/></svg>' },
    leaf: { name: "新叶", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#10b981"/><stop offset="1" stop-color="#14b8a6"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M44 20c0 14-9 22-22 24 2-13 10-22 22-24z" fill="#fff"/><path d="M22 44c4-9 10-15 18-19" stroke="rgba(255,255,255,0.55)" stroke-width="2" fill="none"/></svg>' },
    sun: { name: "暖阳", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f59e0b"/><stop offset="1" stop-color="#f97316"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><circle cx="32" cy="32" r="10" fill="#fff"/><g stroke="#fff" stroke-width="3" stroke-linecap="round"><path d="M32 12v6"/><path d="M32 46v6"/><path d="M12 32h6"/><path d="M46 32h6"/><path d="M18 18l4.2 4.2"/><path d="M41.8 41.8L46 46"/><path d="M18 46l4.2-4.2"/><path d="M41.8 22.2L46 18"/></g></svg>' },
    heart: { name: "心动", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f43f5e"/><stop offset="1" stop-color="#ec4899"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M32 46s-12-7.4-12-16a7 7 0 0 1 12-4.9A7 7 0 0 1 44 30c0 8.6-12 16-12 16z" fill="#fff"/></svg>' },
    gem: { name: "宝石", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#64748b"/><stop offset="1" stop-color="#334155"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M22 22h20l6 8-16 14-16-14z" fill="#fff"/></svg>' }
  };

  function wbSvgDataUri(svg) {
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  }

  function wbAvatarSrc(kind, fresh) {
    // 预设图标：直接内联渲染，不额外请求
    if (kind && kind.indexOf("preset:") === 0) {
      var p = WB_AVATAR_PRESETS[kind.slice(7)];
      return p ? wbSvgDataUri(p.svg) : "/icon.png";
    }
    return (kind === "custom" ? "/api/auth/avatar?ts=" + (fresh ? Date.now() : "1") : "/api/auth/avatar?official=1");
  }

  // 官方 App 图标是方形：圆形硬裁会切坏内容，套底衬完整显示；预设/自定义已是圆形构图，照常 cover
  function wbApplyAvatarImg(img, kind) {
    // 所有头像（官方 / 预设 / 自定义）统一圆形。官方图标由后端 SVG 出口
    // 提供「纯圆底衬 + 居中 logo」，因此前端无需再区分 contain / cover。
    img.className = "wb-avatar-round";
  }

  // 头像选择器（官方 + 内置风格预设）：弹窗与设置卡共用
  // extraBtn：可选。传进来就作为网格内的**最后一个圆钮**参与排布
  // （上传自定义头像就挂在这里），而不是另起一行压在下面。
  function wbBuildAvatarPicker(current, onPick, extraBtn) {
    var wrap = document.createElement("div");
    wrap.className = "wb-avatar-grid wb-avatar-grid-inline";
    var picked = current || "official";
    function setActive(kind) {
      wrap.querySelectorAll(".wb-avatar-opt").forEach(function(x) { x.classList.remove("active"); });
      var hit = wrap.querySelectorAll(".wb-avatar-opt[data-av=\"" + kind + "\"]");
      if (hit.length) hit[0].classList.add("active");
    }
    function mkBtn(kind, label, src) {
      var b = document.createElement("button");
      b.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      b.className = "wb-avatar-opt" + (picked === kind ? " active" : "");
      b.setAttribute("data-av", kind);
      b.title = label;
      var im = document.createElement("img");
      im.src = src;
      im.alt = label;
      im.className = "wb-avatar-round";
      b.appendChild(im);
      b.onclick = function() { picked = kind; setActive(kind); onPick(kind); };
      wrap.appendChild(b);
    }
    mkBtn("official", "官方图标", wbAvatarSrc("official"));
    Object.keys(WB_AVATAR_PRESETS).forEach(function(k) {
      mkBtn("preset:" + k, WB_AVATAR_PRESETS[k].name, wbSvgDataUri(WB_AVATAR_PRESETS[k].svg));
    });
    if (extraBtn) wrap.appendChild(extraBtn);
    return {
      wrap: wrap,
      get: function() { return picked; },
      markNone: function() { setActive(""); }
    };
  }

  function wbOpenModal(title, buildBody) {
    var overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,0.6);z-index:9900;display:flex;align-items:center;justify-content:center;padding:16px;";
    var card = document.createElement("div");
    card.style.cssText = "background:var(--card,#ffffff);color:var(--foreground,#0f172a);border-radius:16px;box-shadow:0 20px 40px rgba(0,0,0,0.25);width:100%;max-width:360px;padding:22px 20px;border:1px solid var(--border,rgba(120,120,120,0.2));position:relative;";
    var h = document.createElement("div");
    h.style.cssText = "font-size:15px;font-weight:700;margin-bottom:14px;";
    h.textContent = title;
    card.appendChild(h);
    var closeX = document.createElement("button");
    closeX.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
    closeX.textContent = "×";
    closeX.style.cssText = "position:absolute;top:10px;right:12px;background:transparent;border:none;font-size:20px;line-height:1;color:var(--muted-foreground,#64748b);cursor:pointer;padding:2px 6px;";
    closeX.onclick = function() { overlay.remove(); };
    card.appendChild(closeX);
    buildBody(card, function() { overlay.remove(); });
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    overlay.addEventListener("click", function(e) { if (e.target === overlay) overlay.remove(); });
    return overlay;
  }

  function wbRefreshAuthOrb() {
    fetch("/api/auth/status", { cache: "no-store" })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d && d.authenticated) { __wbAuthData = d; wbRenderUserOrb(d); }
      })
      .catch(function() {});
  }

  function wbSaveProfileFlow(picked, fileData, fileMime, errEl, done) {
    var finish = function() {
      fetch("/api/auth/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ avatar: picked })
      }).then(function(r) { return r.json(); }).then(function(res) {
        if (res.ok) { done(); }
        else { errEl.textContent = res.error || "保存失败"; errEl.style.display = "block"; }
      }).catch(function() { errEl.textContent = "保存请求失败"; errEl.style.display = "block"; });
    };
    if (picked === "custom" && fileData) {
      var b64 = String(fileData).split(",")[1] || "";
      var bin = atob(b64);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      fetch("/api/auth/avatar-upload", { method: "POST", body: bytes, headers: { "Content-Type": fileMime } })
        .then(function(r) { return r.json(); })
        .then(function(up) {
          if (!up.ok) { errEl.textContent = up.error || "头像上传失败"; errEl.style.display = "block"; return; }
          finish();
        })
        .catch(function() { errEl.textContent = "头像上传失败"; errEl.style.display = "block"; });
    } else {
      finish();
    }
  }

  function wbRenderProfileSection(host, d) {
    if (!host || document.getElementById("settings-profile")) return;
    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-profile";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "账号资料");
    h2.id = "settings-profile-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-profile-title");

    var card = wbEl("div", "wb-api-card");
    var picked = d.avatar || "official";
    var fileData = null, fileMime = null;

    // ---- 1. 头像与昵称 ----
    var row1 = wbEl("div", "wb-api-row wb-api-row-stack");
    var pick = wbEl("div", "wb-avatar-pick");
    var img = document.createElement("img");
    function refreshCardImg(kind) {
      img.src = wbAvatarSrc(kind, kind === "custom");
      wbApplyAvatarImg(img, kind);
    }
    refreshCardImg(picked);
    pick.appendChild(img);
    // 上传按钮与头像圆钮排在同一行（作为网格内最后一枚圆钮），
    // 不再被压到选择器下方单独占一行 —— 用户 2026-09-26 反馈。
    // 圆钮化后与官方/预设按钮视觉等高，一行排满 8 枚刚好。
    var uploadBtn = wbEl("button", "wb-avatar-upload", "+");
    uploadBtn.type = "button";
    uploadBtn.title = "上传自定义头像（≤200KB，PNG / JPG / WebP）";
    var fileIn = document.createElement("input");
    fileIn.type = "file";
    fileIn.accept = "image/png,image/jpeg,image/webp";
    fileIn.style.display = "none";
    uploadBtn.onclick = function() { fileIn.click(); };
    var picker = wbBuildAvatarPicker(picked, function(kind) {
      picked = kind;
      fileData = null;
      refreshCardImg(kind);
    }, uploadBtn);
    fileIn.onchange = function() {
      var f = fileIn.files && fileIn.files[0];
      if (!f) return;
      if (f.size > 200 * 1024) { wbToast("图片超过 200KB，请压缩后再试", "err"); return; }
      var rd = new FileReader();
      rd.onload = function() {
        fileData = rd.result;
        fileMime = f.type || "image/png";
        picked = "custom";
        img.src = String(fileData);
        wbApplyAvatarImg(img, "custom");
        picker.markNone();
      };
      rd.readAsDataURL(f);
    };
    pick.appendChild(picker.wrap);
    pick.appendChild(fileIn);
    row1.appendChild(pick);

    // 昵称已取消（用户 2026-09-26）：显示名一律以登录用户名为准，
    // 这里只保留「保存头像」，不再出现第二套可改的显示名。
    var saveLine = wbEl("div", null);
    saveLine.style.cssText = "display:flex;align-items:center;gap:8px;margin-top:12px;";
    var saveBtn = wbEl("button", "wb-api-btn wb-api-btn-primary", "保存头像");
    var errEl = wbEl("span", null);
    errEl.style.cssText = "color:#ef4444;font-size:12px;";
    saveBtn.onclick = function() {
      saveBtn.disabled = true;
      wbSaveProfileFlow(picked, fileData, fileMime, errEl, function() {
        saveBtn.disabled = false;
        errEl.textContent = "";
        wbToast("头像已保存", "ok");
        wbRefreshAuthOrb();
      });
    };
    saveLine.appendChild(saveBtn);
    saveLine.appendChild(errEl);
    row1.appendChild(saveLine);
    var desc1 = wbEl("div", "wb-api-desc", "头像默认用官方图标，也可上传自定义图片（≤200KB，PNG / JPG / WebP）。");
    row1.appendChild(desc1);
    card.appendChild(row1);

    // ---- 2. 用户名 ----
    var row2 = wbEl("div", "wb-api-row");
    var main2 = wbEl("div", "wb-api-main");
    main2.appendChild(wbEl("div", "wb-api-label", "用户名"));
    main2.appendChild(wbEl("div", "wb-api-desc", d.username || ""));
    row2.appendChild(main2);
    if (d.usernameLocked) {
      var lockBadge = wbEl("span", "wb-api-badge", "环境变量已设，不可修改");
      lockBadge.title = "容器环境变量 AUTH_USERNAME 已设置，请修改环境变量后重建容器";
      row2.appendChild(lockBadge);
    } else {
      var userBtn = wbEl("button", "wb-api-btn", "修改");
      userBtn.onclick = function() { wbOpenUsernameModal(); };
      row2.appendChild(userBtn);
    }
    card.appendChild(row2);

    // ---- 3. 密码 ----
    var row3 = wbEl("div", "wb-api-row");
    var main3 = wbEl("div", "wb-api-main");
    main3.appendChild(wbEl("div", "wb-api-label", "登录密码"));
    // 密码掩码：已设定密码时按实际长度展示同位数 *（不泄露真实字符）
    var maskLen = d.passwordLength || (d.passwordLocked ? 8 : 0);
    if (maskLen > 0) {
      var maskEl = wbEl("div", "wb-pwd-mask");
      maskEl.textContent = new Array(Math.min(maskLen, 40) + 1).join("*");
      maskEl.title = "密码已设定（共 " + maskLen + " 位）";
      main3.appendChild(maskEl);
    }
    row3.appendChild(main3);
    if (d.passwordLocked) {
      var lockBadge2 = wbEl("span", "wb-api-badge", "环境变量已设，不可修改");
      lockBadge2.title = "容器环境变量 AUTH_PASSWORD 已设置，请修改环境变量后重建容器";
      row3.appendChild(lockBadge2);
    } else {
      var pwdBtn = wbEl("button", "wb-api-btn", "修改");
      pwdBtn.onclick = function() { wbOpenPasswordModal(); };
      row3.appendChild(pwdBtn);
    }
    card.appendChild(row3);

    section.appendChild(card);
    host.appendChild(section);
    wbSortSettingsSections();
  }

  function injectProfile() {
    var host = wbFindSettingsHost();
    if (!host || document.getElementById("settings-profile")) return;
    function render(d) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-profile")) return;
      wbRenderProfileSection(host2, d);
    }
    if (__wbAuthData && __wbAuthData.username) { render(__wbAuthData); return; }
    fetch("/api/auth/profile", { cache: "no-store" })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d && d.username) { __wbAuthData = d; render(d); }
      })
      .catch(function() {});
  }

  function wbOpenProfileModal() {
    var d = __wbAuthData || {};
    var picked = d.avatar || "official";
    var fileData = null, fileMime = null;
    wbOpenModal("修改资料", function(card, close) {
      var pick = document.createElement("div");
      pick.style.marginBottom = "14px";
      var img = document.createElement("img");
      img.style.cssText = "width:56px;height:56px;border-radius:50%;object-fit:cover;border:1px solid var(--border,rgba(120,120,120,0.3));display:block;margin:0 auto 10px;";
      function refreshModalImg(kind) {
        img.src = wbAvatarSrc(kind, kind === "custom");
        wbApplyAvatarImg(img, kind);
      }
      refreshModalImg(picked);
      pick.appendChild(img);
      // 与设置页保持一致：上传钮作为网格内最后一枚圆钮，同排展示。
      var uploadBtn = document.createElement("button");
      uploadBtn.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      uploadBtn.className = "wb-avatar-upload";
      uploadBtn.textContent = "+";
      uploadBtn.title = "上传自定义头像（≤200KB，PNG / JPG / WebP）";
      var fileIn = document.createElement("input");
      fileIn.type = "file";
      fileIn.accept = "image/png,image/jpeg,image/webp";
      fileIn.style.display = "none";
      uploadBtn.onclick = function() { fileIn.click(); };
      var picker = wbBuildAvatarPicker(picked, function(kind) {
        picked = kind;
        fileData = null;
        refreshModalImg(kind);
      }, uploadBtn);
      fileIn.onchange = function() {
        var f = fileIn.files && fileIn.files[0];
        if (!f) return;
        if (f.size > 200 * 1024) { wbToast("图片超过 200KB，请压缩后再试", "err"); return; }
        var rd = new FileReader();
        rd.onload = function() {
          fileData = rd.result;
          fileMime = f.type || "image/png";
          picked = "custom";
          img.src = String(fileData);
          wbApplyAvatarImg(img, "custom");
          picker.markNone();
        };
        rd.readAsDataURL(f);
      };
      pick.appendChild(picker.wrap);
      pick.appendChild(fileIn);
      card.appendChild(pick);

      // 昵称已取消：显示名以用户名为准，弹窗只负责换头像。
      var errEl = document.createElement("div");
      errEl.className = "wb-modal-err";
      errEl.style.margin = "8px 0 2px";
      card.appendChild(errEl);

      var save = document.createElement("button");
      save.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      save.className = "wb-modal-btn";
      save.style.marginTop = "14px";
      save.textContent = "保存头像";
      save.onclick = function() {
        save.disabled = true;
        var finish = function() {
          fetch("/api/auth/profile", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ avatar: picked })
          }).then(function(r) { return r.json(); }).then(function(res) {
            if (res.ok) {
              wbToast("头像已保存", "ok");
              close();
              wbRefreshAuthOrb();
            } else {
              save.disabled = false;
              errEl.textContent = res.error || "保存失败";
              errEl.style.display = "block";
            }
          }).catch(function() {
            save.disabled = false;
            errEl.textContent = "保存请求失败";
            errEl.style.display = "block";
          });
        };
        if (picked === "custom" && fileData) {
          // 先传原始字节（不走 multipart），服务端校验类型与大小
          var b64 = String(fileData).split(",")[1] || "";
          var bin = atob(b64);
          var bytes = new Uint8Array(bin.length);
          for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          fetch("/api/auth/avatar-upload", { method: "POST", body: bytes, headers: { "Content-Type": fileMime } })
            .then(function(r) { return r.json(); })
            .then(function(up) {
              if (!up.ok) { save.disabled = false; errEl.textContent = up.error || "头像上传失败"; errEl.style.display = "block"; return; }
              finish();
            })
            .catch(function() { save.disabled = false; errEl.textContent = "头像上传失败"; errEl.style.display = "block"; });
        } else {
          finish();
        }
      };
      card.appendChild(save);
    });
  }

  function wbOpenUsernameModal() {
    wbOpenModal("修改用户名", function(card, close) {
      var mk = function(label, type) {
        var l = document.createElement("label");
        l.style.cssText = "font-size:12px;font-weight:500;display:block;margin:10px 0 4px;";
        l.textContent = label;
        card.appendChild(l);
        var inp = document.createElement("input");
        inp.type = type;
        inp.className = "wb-modal-input";
        inp.maxLength = 32;
        card.appendChild(inp);
        return inp;
      };
      var pwdIn = mk("当前密码", "password");
      var nameIn = mk("新用户名", "text");
      var errEl = document.createElement("div");
      errEl.className = "wb-modal-err";
      errEl.style.margin = "8px 0 2px";
      card.appendChild(errEl);
      var save = document.createElement("button");
      save.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      save.className = "wb-modal-btn";
      save.style.marginTop = "14px";
      save.textContent = "保存并重新登录";
      save.onclick = function() {
        save.disabled = true;
        fetch("/api/auth/username", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ current_password: pwdIn.value, new_username: (nameIn.value || "").trim() })
        }).then(function(r) { return r.json(); }).then(function(res) {
          if (res.ok) { wbToast("用户名已更新，请重新登录", "ok"); setTimeout(function() { window.location.reload(); }, 800); }
          else { save.disabled = false; errEl.textContent = res.error || "修改失败"; errEl.style.display = "block"; }
        }).catch(function() { save.disabled = false; errEl.textContent = "请求失败"; errEl.style.display = "block"; });
      };
      card.appendChild(save);
    });
  }

  function wbOpenPasswordModal() {
    wbOpenModal("修改密码", function(card, close) {
      var mk = function(label) {
        var l = document.createElement("label");
        l.style.cssText = "font-size:12px;font-weight:500;display:block;margin:10px 0 4px;";
        l.textContent = label;
        card.appendChild(l);
        var inp = document.createElement("input");
        inp.type = "password";
        inp.className = "wb-modal-input";
        card.appendChild(inp);
        return inp;
      };
      var oldIn = mk("当前密码");
      var newIn = mk("新密码（至少 6 位）");
      var newIn2 = mk("确认新密码");
      var errEl = document.createElement("div");
      errEl.className = "wb-modal-err";
      errEl.style.margin = "8px 0 2px";
      card.appendChild(errEl);
      var save = document.createElement("button");
      save.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      save.className = "wb-modal-btn";
      save.style.marginTop = "14px";
      save.textContent = "保存并重新登录";
      save.onclick = function() {
        if (newIn.value !== newIn2.value) { errEl.textContent = "两次输入的新密码不一致"; errEl.style.display = "block"; return; }
        save.disabled = true;
        fetch("/api/auth/password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ old_password: oldIn.value, new_password: newIn.value })
        }).then(function(r) { return r.json(); }).then(function(res) {
          if (res.ok) { wbToast("密码已更新，请重新登录", "ok"); setTimeout(function() { window.location.reload(); }, 800); }
          else { save.disabled = false; errEl.textContent = res.error || "修改失败"; errEl.style.display = "block"; }
        }).catch(function() { save.disabled = false; errEl.textContent = "请求失败"; errEl.style.display = "block"; });
      };
      card.appendChild(save);
    });
  }

  function wbRenderUserOrb(data) {
    if (!data || !data.authenticated) return;
    // 昵称已取消：显示名一律用登录用户名（用户 2026-09-26）。
    var nick = data.username || "";
    var orb = document.getElementById("wb-user-orb");
    if (orb) {
      var oimg = orb.querySelector("img");
      var onick = orb.querySelector(".wb-orb-nick");
      if (oimg) {
        oimg.src = wbAvatarSrc(data.avatar, true);
        wbApplyAvatarImg(oimg, data.avatar);
      }
      if (onick) onick.textContent = nick;
      return;
    }
    orb = document.createElement("div");
    orb.id = "wb-user-orb";
    // 悬浮不再弹原生文字提示：点击展开浮窗已经能看到全部信息，
    // 重复的文字气泡是噪声（用户 2026-09-27 反馈）。
    orb.removeAttribute("title");
    var img = document.createElement("img");
    img.src = wbAvatarSrc(data.avatar, data.avatar === "custom");
    img.alt = "用户头像";
    wbApplyAvatarImg(img, data.avatar);
    orb.appendChild(img);
    var nickEl = document.createElement("span");
    nickEl.className = "wb-orb-nick";
    nickEl.textContent = nick;
    orb.appendChild(nickEl);
    document.body.appendChild(orb);

    var pop = null;
    function closePop() { if (pop) { pop.remove(); pop = null; orb.classList.remove("wb-orb-open"); document.removeEventListener("click", onDoc); } }
    function onDoc() { closePop(); }
    function openPop() {
      if (pop) { closePop(); return; }
      pop = document.createElement("div");
      pop.id = "wb-user-pop";
      var head = document.createElement("div");
      head.className = "wb-pop-head";
      var himg = document.createElement("img");
      himg.src = wbAvatarSrc(data.avatar, true);
      himg.alt = "用户头像";
      wbApplyAvatarImg(himg, data.avatar);
      head.appendChild(himg);
      var hmain = document.createElement("div");
      hmain.style.cssText = "min-width:0;";
      var nrow = document.createElement("div");
      nrow.style.cssText = "font-weight:700;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      nrow.textContent = nick;
      hmain.appendChild(nrow);
      // 不再单列一行 "@用户名"：显示名就是用户名，重复一遍是噪声。
      var srow = document.createElement("div");
      srow.style.cssText = "display:flex;align-items:center;gap:5px;font-size:11px;color:#10b981;margin-top:2px;";
      var dot = document.createElement("span");
      dot.style.cssText = "width:6px;height:6px;border-radius:50%;background:#10b981;display:inline-block;";
      srow.appendChild(dot);
      srow.appendChild(document.createTextNode("在线"));
      hmain.appendChild(srow);
      head.appendChild(hmain);
      pop.appendChild(head);

      // 有意义的信息行：剩余积分 / 今日消耗 / 账号数量。
      // 有效期倒计时已移除——过期了重新登录即可，倒计时只会制造焦虑。
      var stats = document.createElement("div");
      stats.className = "wb-pop-stats";
      function statRow(k, v) {
        var row = document.createElement("div");
        row.className = "wb-pop-stat";
        var kk = document.createElement("span");
        kk.className = "wb-pop-stat-k";
        kk.textContent = k;
        var vv = document.createElement("span");
        vv.className = "wb-pop-stat-v";
        vv.textContent = v;
        row.appendChild(kk);
        row.appendChild(vv);
        return row;
      }
      var creditRow = statRow("总积分", "…");
      var todayRow = statRow("今日消耗", "…");
      var gainedRow = statRow("今日积分", "…");
      var acctRow = statRow("账号池", "…");

      // 右侧数值按语义着色 —— 一眼分辨「攒了多少 / 用了多少 / 赚了多少 / 可用占比」
      //   - 总积分：中性主色（看余额本身）
      //   - 今日消耗：消耗为 0 用中性灰，>0 用橙（在花钱）
      //   - 今日积分：赚了用绿，为 0 用中性灰
      //   - 账号池：全部启用用绿，有未启用用橙（提示有账号闲置）
      // 四行各用一种亮色，彼此可区分（要求「四个颜色各不相同，尽量亮一点」）。
      // 此前今日消耗与账号池同为橙色，无法分辨。
      //   总积分 蓝   今日消耗 橙   今日积分 绿   账号池 紫
      var C_NEUTRAL = "var(--muted-foreground,#64748b)";
      var C_MAIN = "#2563eb";
      var C_SPEND = "#ea580c";
      var C_EARN = "#16a34a";
      var C_POOL = "#9333ea";
      function setVal(row, text, color) {
        var el = row.querySelector(".wb-pop-stat-v");
        if (!el) return;
        el.textContent = text;
        if (color) el.style.color = color;
      }
      stats.appendChild(creditRow);
      stats.appendChild(todayRow);
      stats.appendChild(gainedRow);
      stats.appendChild(acctRow);

      // 今日积分：每日任务（签到 + 任务）今天获得的积分，
      // 与每日任务页「积分统计」的「今日积分」同源。
      fetch("/api/wb-daily/credit-summary", { cache: "no-store" })
        .then(function(r) { return r.ok ? r.json() : {}; })
        .then(function(d) {
          var g = (d && d.todayCredits != null) ? d.todayCredits : null;
          setVal(gainedRow, g == null ? "—" : (Math.round(g * 100) / 100 + ""),
            g == null ? C_NEUTRAL : (g > 0 ? C_EARN : C_NEUTRAL));
        })
        .catch(function() {
          setVal(gainedRow, "—", C_NEUTRAL);
        });
      pop.appendChild(stats);

      // 积分数据：官方 credits/stats（代理层已过滤僵尸账号并校准汇总）
      fetch("/api/credits/stats", { cache: "no-store" })
        .then(function(r) { return r.ok ? r.json() : {}; })
        .then(function(d) {
          var sum = (d && d.summary) || {};
          var rem = sum.currentRemaining;
          setVal(creditRow, rem != null ? (Math.round(rem * 100) / 100 + "") : "—",
            rem == null ? C_NEUTRAL : C_MAIN);
          var use = sum.usageToday;
          setVal(todayRow, use != null ? (Math.round(use * 100) / 100 + "") : "—",
            use == null ? C_NEUTRAL : (use > 0 ? C_SPEND : C_NEUTRAL));
        })
        .catch(function() {
          setVal(creditRow, "—", C_NEUTRAL);
          setVal(todayRow, "—", C_NEUTRAL);
        });
      // 账号池：网关信息里的账号统计
      fetch("/api/gateway-info", { cache: "no-store" })
        .then(function(r) { return r.ok ? r.json() : {}; })
        .then(function(d) {
          var acc = (d && d.accounts) || {};
          // 账号池 = 启用（真正参与调用）/ 总数。
          // 此前用的是 usable（凭据可用数）：当所有账号凭据都正常时显示成 11/11，
          // 看不出其中有 4 个并未启用（用户 2026-09-27 反馈）。
          // inPool 才是实际参与调度的账号数。
          var inPool = (acc.inPool != null ? acc.inPool : acc.usable);
          if (acc.total != null) {
            // 账号池统一用紫色：与总积分(蓝)/今日消耗(橙)/今日积分(绿)区分开。
            // 原先按「是否全部启用」在绿/橙之间切换，会与另两行撞色。
            setVal(acctRow, inPool + "/" + acc.total + " 个",
              inPool == null ? C_NEUTRAL : C_POOL);
          } else {
            setVal(acctRow, "—", C_NEUTRAL);
          }
        })
        .catch(function() {
          setVal(acctRow, "—", C_NEUTRAL);
        });

      // 资料与凭据的修改入口统一收在设置页「账号资料」，浮窗只留退出
      var menu = document.createElement("div");
      menu.className = "wb-pop-menu";
      var out = document.createElement("button");
      out.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      out.className = "wb-pop-item danger";
      out.textContent = "退出登录";
      out.onclick = function() {
        closePop();
        fetch("/api/auth/logout", { method: "POST" }).then(function() { window.location.reload(); });
      };
      menu.appendChild(out);
      pop.appendChild(menu);
      document.body.appendChild(pop);
      orb.classList.add("wb-orb-open");
      setTimeout(function() { document.addEventListener("click", onDoc); }, 0);
    }
    orb.addEventListener("click", function(e) {
      e.stopPropagation();
      if (pop) { closePop(); } else { openPop(); }
    });
    // 鼠标悬浮即自动展开（含浮窗），移开后由全局点击/悬浮判定收纳
    orb.addEventListener("mouseenter", function() {
      if (!pop) openPop();
    });
    orb.addEventListener("mouseleave", function() {
      // 延迟收纳：给鼠标移向浮窗留出路径，避免中途闪烁
      clearTimeout(orb.__wbLeaveTimer);
      orb.__wbLeaveTimer = setTimeout(function() {
        if (pop && !pop.matches(":hover")) closePop();
      }, 220);
    });
    document.addEventListener("mousemove", function(ev) {
      if (!pop) return;
      var r = orb.getBoundingClientRect(), pr = pop.getBoundingClientRect();
      var inside = (ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom)
                || (ev.clientX >= pr.left && ev.clientX <= pr.right && ev.clientY >= pr.top && ev.clientY <= pr.bottom);
      if (!inside) closePop();
    });
  }


  // 账号卡片上的 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 切换按钮与「当前账号」徽章，
  // 以及「导入本机账号」「权限检测」「启动设置」「自动更新」等桌面专有入口，
  // 都已在 patch/patch_binary.py 里从二进制物理移除（它们只会调用宿主桌面程序）。
  // 这里只做兜底：清掉少数没有稳定结构锚点、只能按文案识别的残留入口。
  function sanitizeMacUI() {
    document.querySelectorAll("button, a, [role='status']").forEach(el => {
      const text = (el.innerText || "").trim();
      // 清除无法在容器内执行的动作：Finder、完全磁盘访问
      if (text === "在 Finder 中显示" || text === "在文件管理器中显示" || text === "打开完全磁盘访问" || text === "打开 App 管理") {
        el.classList.add("wb-mac-btn-hide");
        return;
      }
    });

    // 仅精准清理包含「如何授权」或「完全磁盘访问」的引导小卡片，绝不向上寻找普通大容器
    document.querySelectorAll("div.border-l-2, div.rounded-md.border").forEach(box => {
      const text = box.innerText || "";
      if (text.includes("完全磁盘访问") || text.includes("如何授权") || text.includes("workbuddy-switch.app") || text.includes("autobuddy")) {
        box.classList.add("wb-mac-block-hide");
      }
    });
  }

  // ⚠️ 2026-10-16 v3 重设计后，本函数只剩「导航标签包装」这一件事。
  // 原折叠逻辑（wb-collapsed + 右上角小按钮）已彻底废弃：
  //   · 桌面是 64px 图标轨道，没有可折叠的纵栏
  //   · 移动端是底部标签栏，同样不需要折叠
  //   · aside.wb-collapsed { width: 68px !important } 特异性 (0,1,1)
  //     会盖掉移动端 aside { width: 100% !important } 的 (0,0,1)，
  //     导致底部条只剩 68px（实测踩过）
  // 收纳入口 = 侧边栏右缘的 #wb-collapse-btn（和原来一样），由本函数创建。
  /* 侧边栏收纳（v4，用户要求「增加侧边栏收纳，和原来一样」）。
     展开 220px（图标+文字）↔ 收起 64px（只图标 + 悬停气泡）。
     折叠按钮在侧边栏右缘（#wb-collapse-btn），状态存 localStorage。
     移动端（<=860px）CSS 会把按钮隐藏 —— 底部标签栏没有可折叠的纵栏。 */
  /* 把顶栏网格的左 margin 同步成内容 wrap 的实测左 margin。
     背景：两者父容器同宽（1425）、盒模型相同（max-width 1180 + padding 24），
     但浏览器给 wrap 算出 margin-left:115px、给 grid 算出 122.5px，差 7.5px，
     顶栏与内容因此「差一点没对齐」（2026-10-16 实测）。
     CSS 层面反复覆盖都无效（官方 mx-auto 的计算值会胜出），
     所以在 DOM 就绪后直接用 JS 取 wrap 的实测值同步过来 —— 一次到位，不猜。 */
  function wbSyncTopbarAlign() {
    try {
      var grid = document.querySelector("#wb-topbar-inner > .wb-tb-grid");
      if (!grid) return;
      /* ⚠️ 必须只认**当前可见视图**的内容容器，不能图省事写
         `main > div`（2026-10-17 修复，用户反馈「部分页面偶尔出现左右两个
         logo 和主题散开而不是和页面对齐」）。
         根因：main 下同时挂着官方容器和两个注入视图（#wb-overview-view /
         #wb-daily-view），`main > div` 命中的可能是**上一个页面**的残留节点 ——
         实测从官方页切到 hash 视图时，选中的是已 display:none 的旧官方容器
         （量到 0×0）或者尺寸不同的另一个容器，于是顶栏按错误基准对齐：
         实测 logo 块 x=146（总览/每日）与 x=139（账号/词元/积分/设置）
         两种结果交替出现，主题控件同理会左右散开。
         现在按视图状态精确取容器，取不到就退出（不猜、不套旧值）。 */
      var vis = function (el) {
        return !!el && el.style.display !== "none"
          && getComputedStyle(el).display !== "none";
      };
      var ov = document.getElementById("wb-overview-view");
      var dl = document.getElementById("wb-daily-view");
      var wrap = null;
      if (vis(ov)) wrap = ov.querySelector(".wb-ov-wrap");
      else if (vis(dl)) wrap = dl.querySelector(".wb-daily-wrap");
      else {
        // 官方页：从 main 的可见子节点里找内容容器
        var main = document.querySelector("main");
        if (main) {
          var kids = Array.prototype.slice.call(main.children);
          for (var i = 0; i < kids.length; i++) {
            if (kids[i] !== ov && kids[i] !== dl && vis(kids[i])) {
              wrap = kids[i];
              break;
            }
          }
        }
      }
      if (!wrap) return;
      var gl = grid.getBoundingClientRect(), wl = wrap.getBoundingClientRect();
      // 容器不可见（0×0）时直接退出，绝不能拿它当基准 —— 否则会把
      // margin-left 压成 0 或负值，顶栏对齐被破坏（2026-10-16 实测踩过）。
      if (wl.width <= 0 || gl.width <= 0) return;
      var dLeft = wl.x - gl.x;
      var dW = wl.width - gl.width;
      if (Math.abs(dLeft) < 0.5 && Math.abs(dW) < 0.5) return;
      // 差值过大说明页面正在切换中间态（容器还没渲染完），此时修正会抖。
      if (Math.abs(dLeft) > 1200 || Math.abs(dW) > 1200) return;
      var cur = parseFloat(getComputedStyle(grid).marginLeft) || 0;
      grid.style.marginLeft = (cur + dLeft) + "px";
      grid.style.maxWidth = wl.width + "px";
      grid.style.width = wl.width + "px";
    } catch (e) {}
  }

  /* 取顶栏内盒子（#wb-topbar-inner），不存在则退回 aside。
     所有顶栏子元素（主题控件 / 收纳按钮 / 面板）都必须挂到内盒子里，
     否则会跑到外层贴顶条上、破坏限宽居中对齐。 */
  function wbTopbarInner(aside) {
    var g = document.querySelector("#wb-topbar-inner > .wb-tb-grid");
    return g || document.getElementById("wb-topbar-inner") || aside;
  }

  /* v5：顶栏布局。侧边栏折叠概念已废弃（没有侧边栏了），
     本函数只做三件事：
       1) 把导航裸文本包成 span.wb-nav-label（CSS 与收纳面板都要用）
       2) 建 PC 端主题三段控件（wbInitThemeSwitch）
       3) 建移动端主题单图标 + 收纳按钮（wbInitMobileTopbar）
     旧的 #wb-collapse-btn 折叠逻辑整个删除。 */
  function initCollapse() {
    const aside = document.querySelector("aside");
    if (!aside) return;

    // 把 aside 的原有子元素（logo / nav / 主题控件 / 收纳按钮 / 面板）
    // 包进 #wb-topbar-inner。用户需求（2026-10-16）：「网页整体包含顶部
    // 以盒子形式展示，顶部不要两边无限延伸」。
    // 实测改前顶栏通铺 1425/1905px，而内容区限宽 1240px 居中，头部与内容
    // 左右对不齐。包一层限宽居中的内盒子后两者严格对齐。
    var inner = document.getElementById("wb-topbar-inner");
    if (!inner) {
      inner = document.createElement("div");
      inner.id = "wb-topbar-inner";
      // 再套一层 .wb-tb-grid 承载真正的三列网格（logo / 选项卡 / 主题）。
      // 分两层的原因：#wb-topbar-inner 与 aside 同宽（100%），
      // .wb-tb-grid 限宽 1240 居中 —— 与 main 里内容 wrap 的盒模型一致，
      // 这样 logo 左边缘与内容左边缘严格对齐（实测单层会差 8px）。
      var grid = document.createElement("div");
      grid.className = "wb-tb-grid";
      var kids = Array.prototype.slice.call(aside.childNodes);
      kids.forEach(function (n) { grid.appendChild(n); });
      inner.appendChild(grid);
      aside.appendChild(inner);
    }

    aside.querySelectorAll("nav a").forEach(a => {
      // ⚠️ 官方导航链接是 React Router 的 pushState 跳转，**既不触发 hashchange
      // 也不触发 popstate**，而路径页（/token-stats、/settings 等）
      // 只靠 hashchange 同步高亮就会丢。
      // 这里在捕获阶段挂 click：点下去先同步一次高亮（SPA 路由随后异步完成，
      // 再由 MutationObserver 触发 run() 兜底同步）。
      //
      // 2026-10-17 修复：必须调 wbUpdateOverviewView() 而不是只调
      // wbSyncNavActive() —— 数据总览占用默认主页（"/"）之后，
      // 从数据总览点官方页时路径变了但没有任何事件通知我们
      // （pushState 不触发 hashchange/popstate），
      // 实测页面仍显示数据总览、官方内容被盖住。
      if (!a.__wbNavBound) {
        a.__wbNavBound = true;
        a.addEventListener("click", function () {
          // 等一帧让 SPA 的 pushState 先落地，再按最新 URL 同步
          requestAnimationFrame(function () { wbUpdateOverviewView(); });
          setTimeout(wbUpdateOverviewView, 120);
          setTimeout(wbUpdateOverviewView, 400);
        }, true);
      }
      Array.from(a.childNodes).forEach(node => {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          const span = document.createElement("span");
          span.className = "wb-nav-label";
          span.innerText = node.textContent;
          node.replaceWith(span);
        }
      });
      var txt = (a.textContent || "").trim();
      if (txt) a.setAttribute("data-wb-label", txt);
    });

    // 清理旧版可能遗留的折叠按钮与 class（v4 及以前）
    var oldBtn = document.getElementById("wb-collapse-btn");
    if (oldBtn) oldBtn.remove();
    aside.classList.remove("wb-collapsed");

    // PC 端主题三段控件
    wbInitThemeSwitch(aside);
    // 移动端主题单图标 + 收纳按钮 + 下拉面板
    wbInitMobileTopbar(aside);
    // 顶栏与内容区左边缘对齐（用实测差值补偿，见 wbSyncTopbarAlign）
    wbSyncTopbarAlign();
  }

  /* 移动端顶栏右侧：主题单图标 + 收纳按钮。
     用户需求（2026-10-16 v5）：
       · 移动端选项卡以收纳形式放在右上角
       · 主题切换以单个图标放在收纳按钮前方
     PC 端这两个控件被 CSS 隐藏（display:none），只在 <=860px 显示。 */
  var WB_THEME_MINI_SVG = {
    system: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>',
    light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>',
    dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>'
  };

  function wbInitMobileTopbar(aside) {
    if (!aside) return;

    /* ---- 主题单图标：点击在 浅色/深色 之间切换 ---- */
    var mini = document.getElementById("wb-theme-mini");
    if (!mini) {
      mini = document.createElement("button");
      mini.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      mini.type = "button";
      mini.id = "wb-theme-mini";
      mini.setAttribute("aria-label", "切换主题");
      // 放在收纳按钮之前 → 必须先插入（挂到内盒子）
      wbTopbarInner(aside).appendChild(mini);
      var syncMini = function () {
        var cur = wbThemeCurrent();
        // 当前是深色就显示太阳（点了变浅色），反之显示月亮
        var dark = cur === "dark"
          || (cur === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
        mini.innerHTML = dark ? WB_THEME_MINI_SVG.light : WB_THEME_MINI_SVG.dark;
        mini.title = dark ? "切换到浅色" : "切换到深色";
      };
      mini.onclick = function (e) {
        e.stopPropagation();
        var cur = wbThemeCurrent();
        var dark = cur === "dark"
          || (cur === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
        wbThemeApply(dark ? "light" : "dark", true);
        syncMini();
      };
      // 主题变化时同步图标（PC 端三段控件切换后这里也要跟）
      window.addEventListener("wb-theme-changed", syncMini);
      syncMini();
    }

    /* ---- 收纳按钮：点开 6 个选项卡的下拉面板 ---- */
    var btn = document.getElementById("wb-nav-toggler");
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      btn.type = "button";
      btn.id = "wb-nav-toggler";
      btn.setAttribute("aria-label", "选项卡");
      btn.setAttribute("aria-expanded", "false");
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + '<line x1="3" y1="6" x2="21" y2="6"></line>'
        + '<line x1="3" y1="12" x2="21" y2="12"></line>'
        + '<line x1="3" y1="18" x2="21" y2="18"></line></svg>';
      // 收纳按钮在主题之后 → 后 append 即排在后面（挂到内盒子）
      wbTopbarInner(aside).appendChild(btn);

      var panel = document.getElementById("wb-nav-panel");
      if (!panel) {
        panel = document.createElement("div");
        panel.id = "wb-nav-panel";
        panel.setAttribute("role", "menu");
        wbTopbarInner(aside).appendChild(panel);
      }

      var rebuild = function () {
        var nav = aside.querySelector("nav");
        if (!nav) return;
        panel.innerHTML = "";
        var links = Array.prototype.slice.call(nav.querySelectorAll("a"));
        if (!links.length) {
          var empty = document.createElement("div");
          empty.className = "wb-nav-panel-empty";
          empty.textContent = "暂无导航项";
          panel.appendChild(empty);
          return;
        }
        links.forEach(function (a) {
          var item = document.createElement("a");
          item.className = "wb-nav-panel-item";
          item.href = a.getAttribute("href") || "#";
          item.setAttribute("role", "menuitem");
          item.innerHTML = a.innerHTML;
          // 选中态：用 wbSyncNavActive 写的 aria-current（专用信号），
          // 不再看官方 bg-foreground/[0.06] —— 那个类是 React 的状态类，
          // 会在 SPA 重渲染时被重新分配，导致面板里多项同时高亮。
          var isCur = a.getAttribute("aria-current") === "page"
            || a.classList.contains("wb-nav-active");
          if (isCur) item.classList.add("wb-nav-panel-active");
          /* ⚠️ 移动端下拉面板的导航项是「复制 href + 关面板」的简化实现，
             丢了原始导航项上 injectWbOverview / injectWbDaily 绑定的点击语义。
             尤其「账号管理」的官方 href 是 "/"，而 "/" 现在被数据总览占用 ——
             直接跳 href 会把用户带到数据总览，而不是账号管理。
             这里按原始导航项的 id 复刻正确语义：三个注入视图项各自 pushState
             到自己的 hash 路由，官方路径项保持默认 href 跳转（React Router 处理）。 */
          item.addEventListener("click", function (e) {
            var aid = a.id;
            if (aid === WB_OVERVIEW_NAV_ID) {
              e.preventDefault();
              wbPushUrl("/");
            } else if (aid === WB_ACCOUNTS_NAV_ID) {
              e.preventDefault();
              wbPushUrl("/#/accounts");
            } else if (aid === "wb-nav-wb-daily") {
              e.preventDefault();
              wbPushUrl(window.location.pathname + "#/wb-daily");
            }
            wbCloseNavPanel();
          });
          panel.appendChild(item);
        });
      };

      btn.onclick = function (e) {
        e.stopPropagation();
        var open = panel.classList.contains("wb-nav-panel-open");
        if (open) {
          wbCloseNavPanel();
          return;
        }
        rebuild();
        panel.classList.add("wb-nav-panel-open");
        btn.setAttribute("aria-expanded", "true");
        setTimeout(function () {
          document.addEventListener("click", wbNavPanelOutside);
          document.addEventListener("keydown", wbNavPanelEsc);
        }, 0);
      };
    }
  }

  function wbCloseNavPanel() {
    var panel = document.getElementById("wb-nav-panel");
    if (panel) panel.classList.remove("wb-nav-panel-open");
    var btn = document.getElementById("wb-nav-toggler");
    if (btn) btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", wbNavPanelOutside);
    document.removeEventListener("keydown", wbNavPanelEsc);
  }

  function wbNavPanelOutside(e) {
    var panel = document.getElementById("wb-nav-panel");
    var btn = document.getElementById("wb-nav-toggler");
    var mini = document.getElementById("wb-theme-mini");
    if (panel && !panel.contains(e.target)
        && btn && !btn.contains(e.target)
        && mini && !mini.contains(e.target)) {
      wbCloseNavPanel();
    }
  }

  function wbNavPanelEsc(e) {
    if (e.key === "Escape") wbCloseNavPanel();
  }

  /* ---------- 主题切换（顶栏右上角，2026-10-16）----------
     与官方机制完全兼容（实测确认）：
       localStorage["appearance-theme"] ∈ {system, light, dark}
       html.classList.toggle("dark", <是否为深色>)
     手动改这两个值后，官方的 React 受控组件不会回弹
     （实测：改完等 2 秒按钮文字与 html class 都保持）。
     因此侧边栏/顶栏这个开关与设置页那个官方主题控件
     读写同一份状态，两处不会各说各话。 */
  var WB_THEME_LS_KEY = "appearance-theme";

  function wbThemeCurrent() {
    var v = "";
    try { v = localStorage.getItem(WB_THEME_LS_KEY) || ""; } catch (e) {}
    return (v === "light" || v === "dark") ? v : "system";
  }

  function wbThemeApply(mode, persist) {
    var dark;
    if (mode === "light" || mode === "dark") {
      dark = mode === "dark";
    } else {
      dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    }
    document.documentElement.classList.toggle("dark", dark);
    if (persist) {
      try { localStorage.setItem(WB_THEME_LS_KEY, mode); } catch (e) {}
    }
    var box = document.getElementById("wb-theme-switch");
    if (box) {
      Array.prototype.slice.call(
        box.querySelectorAll(".wb-ts-btn")).forEach(function (b) {
        var on = b.getAttribute("data-theme") === mode;
        b.classList.toggle("wb-ts-on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    // 派发事件，让移动端主题单图标（#wb-theme-mini）同步自身图标。
    // v5 起 PC 端用三段控件、移动端用单图标，两者都调 wbThemeApply，
    // 靠这个事件保持一致（原先单图标只在初始化时同步，PC 端切换后移动端图标不跟）。
    try {
      window.dispatchEvent(new CustomEvent("wb-theme-changed", { detail: { mode: mode } }));
    } catch (e) {}
  }

  /* ==================================================================
     Tremor 式仪表盘注入（2026-10-16 v3）
     - wbInitTopbar：在 main 顶部注入「上下文顶栏」（页面标题 + 页面操作）
     - wbInitKpi：注入 4 张 KPI 汇总卡（总积分/昨日消耗/可用模型/今日调用）
     - wbInitMid：注入「近30天消耗趋势」面积图 + 「每日任务状态」卡
     - wbInitAcctHead：注入账号明细区标题
     全部只读现有接口，不新增后端依赖；失败静默降级，绝不影响官方页面。
     ================================================================== */

  var WB_KPI_IDS = ["wb-kpi-credits", "wb-kpi-used", "wb-kpi-models", "wb-kpi-calls"];

  /* ---------- 工具：安全取 JSON ---------- */
  function wbJson(url, timeout) {
    return fetch(url, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; });
  }


  /* 安全取当前路径名。
     ⚠️ 不能直接写 location.pathname —— _test_webui_panels.py 在 Node 环境里
     跑这段脚本，没有 location 全局对象，一执行就抛 ReferenceError，
     整个 run() 挂掉，面板全渲染不出来（实测踩过）。
     浏览器里 location 一定存在，这里只是给非浏览器环境兜底。 */
  function wbPath() {
    try {
      return (typeof location !== "undefined" && location.pathname) || "";
    } catch (e) {
      return "";
    }
  }

  function wbFmt(v) {
    if (v === null || v === undefined || v === "") return "—";
    var f = Number(v);
    if (isNaN(f)) return String(v);
    if (f !== 0 && Math.abs(f) < 1) return f.toFixed(2);
    if (f === Math.round(f)) return f.toLocaleString("en-US");
    return f.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  function wbPct(cur, prev) {
    if (!prev || prev <= 0 || cur === null || prev === null) return null;
    return (cur - prev) / prev * 100;
  }

  /* ---------- 上下文顶栏 ---------- */

  /* ==================================================================
     概览页（2026-10-16 v4）
     用户需求：
       1) 新增「概览」选项卡，放在第一个
       2) 之前散在首页的重要数据（KPI 卡 / 趋势图 / 任务状态）全部移到概览页
       3) 其他选项卡内容不变、各司其职

     实现方式：复用每日任务那套 hash 路由 + 视图切换机制（#/wb-overview），
     不新建路由体系。main 下挂一个 #wb-overview-view，切到概览时显示它、
     隐藏其他子节点，切走时反向。这样官方页面代码一行不用改。
     ================================================================== */

  var WB_OVERVIEW_NAV_ID = "wb-nav-wb-overview";
  /* 账号管理导航项的 id（用户 2026-10-17 要求「账户管理加页面后缀」）。
     官方那一项的 href 是 "/"，现在数据总览占用了 "/" 作为默认主页，
     账号管理改走 hash 后缀 #/accounts —— 给它一个 id 才能被
     wbSyncNavActive 精确识别并高亮。 */
  var WB_ACCOUNTS_NAV_ID = "wb-nav-accounts";
  /* 数据总览 / 每日任务在 URL 里的 hash 后缀。账号管理的后缀也在这里登记，
     点官方导航项时要把这些后缀清掉，否则会留在 URL 里造成状态错乱。 */
  var WB_VIEW_HASHES = ["#/wb-overview", "#/wb-daily", "#/accounts"];

  /* 取「视图 hash」：去掉查询串与末尾斜杠，只留 #/xxx 本体。
     ⚠️ 为什么必须归一化：`window.location.hash` 对
     `/#/accounts?O=1792200207` 返回的是 `#/accounts?O=1792200207`，
     与字面量 "#/accounts" 全等比较永远落空。
     2026-10-17 实测踩坑：带 ?O= 打开每日任务页时 hash 判定失败，
     页面退回官方 "/" 显示成账号管理，导航高亮也丢了。
     查询串是页面自己加的缓存破坏参数，与视图状态无关，必须剥掉。 */
  function wbViewHash() {
    var h = window.location.hash || "";
    var q = h.indexOf("?");
    if (q >= 0) h = h.slice(0, q);
    if (h.length > 1 && h.charAt(h.length - 1) === "/") h = h.slice(0, -1);
    return h;
  }

  /* 用 pushState 导航到指定 URL，并派发 popstate 让官方 SPA（React Router）
     响应 —— 实测仅 pushState 不派发事件时 SPA 不会重渲染，
     页面内容停在旧路由上（2026-10-17 实测确认）。 */
  function wbPushUrl(url) {
    try {
      window.history.pushState({ __wb: 1 }, "", url);
      window.dispatchEvent(new PopStateEvent("popstate", { state: { __wb: 1 } }));
    } catch (e) {}
    // 双保险：SPA 的 MutationObserver 兜底链路有时序差，这里直接同步一次
    try { wbApplyMainView(); wbSyncNavActive(); } catch (e) {}
  }

  /* ---------- 概览页导航项（插到导航最前） ---------- */
  function injectWbOverview() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    var nav = aside.querySelector("nav");
    if (!nav) return;

    var link = document.getElementById(WB_OVERVIEW_NAV_ID);
    if (!link) {
      link = document.createElement("a");
      link.id = WB_OVERVIEW_NAV_ID;
      /* href 用 "/" 而不是 hash：数据总览现在是默认主页（用户 2026-10-17
         要求「数据总览取消页面后缀作为默认主页」），鼠标悬停时地址栏预览
         也应该是真实的 "/"。万一点击处理器没接住，浏览器默认跳转的也是
         数据总览，不会跑到别的页面去。 */
      link.href = "/";
      link.innerHTML =
        '<svg class="size-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + '<rect x="3" y="3" width="7" height="9" rx="1"></rect>'
        + '<rect x="14" y="3" width="7" height="5" rx="1"></rect>'
        + '<rect x="14" y="12" width="7" height="9" rx="1"></rect>'
        + '<rect x="3" y="16" width="7" height="5" rx="1"></rect></svg>'
        + '<span class="wb-nav-label">数据总览</span>';
      link.setAttribute("data-wb-label", "数据总览");
      // 插到最前面（用户要求「概览是第一个选项卡」）
      if (nav.firstElementChild) nav.insertBefore(link, nav.firstElementChild);
      else nav.appendChild(link);

      link.addEventListener("click", function (e) {
        e.preventDefault();
        /* 数据总览 = 默认主页，统一推到 "/"（无任何 hash 后缀）。 */
        wbPushUrl("/");
      });

      // 点其他导航项时清掉 #/wb-overview hash，否则：
      //   · URL 变成 /#/wb-overview 之外还留着旧 hash
      //   · wbSyncNavActive() 判不出该高亮谁（实测从概览点「账号管理」
      //     后高亮仍停在「概览」）
      // 做法与 injectWbDaily 的清 hash 逻辑一致。
      /* 点其他导航项时清掉视图 hash 后缀，否则：
           · URL 里留着旧后缀（如 /token-stats#/accounts）
           · wbSyncNavActive() 判不出该高亮谁
         官方链接由 React Router 自己 pushState，hash 会被自然清掉；
         这里只兜「hash 没被清」的情况（例如 SPA 路由跳到了同一个路径）。 */
      /* 账号管理：官方那一项 href="/"，但 "/" 现在是数据总览的地盘。
         在捕获阶段拦下点击、自己 pushState 到 "/#/accounts"。
         ⚠️ 必须 stopPropagation：React 17+ 把 onClick 委托到根容器，
         不拦的话 React Router 仍会导航到 "/"（那是数据总览），
         随后我们再补 hash 就会闪一下概览页。 */
      /* ⚠️ 必须排除自己插入的两个视图链接：它们的 href 也可能是 "/"
         （数据总览现在就是 "/"），不排除的话 querySelector 会先命中概览链接，
         把它的 id 改成 wb-nav-accounts、拦截器绑错元素 ——
         2026-10-17 实测就是这么踩坑的：概览链接 id 被抢，
         wbSyncNavActive 的 isOv 判定全失效，六个选项卡一个都不高亮。 */
      var accLink = nav.querySelector('a[href="/"]:not(#' + WB_OVERVIEW_NAV_ID
        + '):not(#wb-nav-wb-daily)');
      if (accLink && !accLink.__wbAccBound) {
        accLink.__wbAccBound = true;
        accLink.id = WB_ACCOUNTS_NAV_ID;
        accLink.addEventListener("click", function (e) {
          e.preventDefault();
          e.stopPropagation();
          wbPushUrl("/#/accounts");
        }, true);
      }

      /* ⚠️ 必须排除**两个**注入的视图链接：它们自己的点击处理器刚刚
         把 hash 设成 #/wb-daily / #/accounts，这个通用处理器随后又把 hash
         当成「残留后缀」清掉 —— 2026-10-17 实测踩坑：点「每日任务」后
         URL 变成 "/"、高亮错成「数据总览」，刷新页面也回不到每日任务。
         只给官方链接（真实路径）挂这个清理逻辑。 */
      nav.querySelectorAll("a:not(#" + WB_OVERVIEW_NAV_ID + "):not(#wb-nav-wb-daily)")
        .forEach(function (a) {
        a.addEventListener("click", function () {
          if (WB_VIEW_HASHES.indexOf(wbViewHash()) >= 0) {
            window.history.replaceState(null, "",
              window.location.pathname + window.location.search);
          }
        });
      });
    }
  }

  /* ---------- 概览页视图 ---------- */
  function wbCreateOverviewView() {
    var v = document.createElement("div");
    v.id = "wb-overview-view";
    v.style.display = "none";
    v.innerHTML =
      '<div class="wb-ov-wrap">' +
        '<div class="wb-ov-head">' +
          '<div>' +
            '<h1 class="wb-ov-h1">数据总览</h1>' +
            '<p class="wb-ov-sub">账号池核心指标一屏可见：积分、消耗、模型、调用、账号与任务</p>' +
          '</div>' +
          '<div class="wb-ov-actions">' +
            '<button class="wb-ov-btn" data-act="checkin" type="button">' +
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v4M16 2v4M3 10h18"/><rect x="3" y="4" width="18" height="18" rx="2"/><path d="m9 16 2 2 4-4"/></svg>'
              + '全部签到</button>' +
            '<button class="wb-ov-btn wb-ov-btn-primary" data-act="task" type="button">' +
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 3 20 12 13 13 10 21"/></svg>'
              + '执行任务</button>' +
          '</div>' +
        '</div>' +

        /* 第一行 KPI：积分与消耗（4 张） */
        '<div id="wb-ov-kpi-row">' +
          '<div class="wb-kpi" id="wb-kpi-credits">' +
            '<div class="wb-kpi-label">账号池总积分</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
          '<div class="wb-kpi" id="wb-kpi-used">' +
            /* 用户需求（2026-10-16）：「昨日消耗卡片改为今日消耗，并获取对应的
               正确数据」。
               口径说明：**必须用 summary.usageToday，不能再用 accounts[].daily
               的今日累加**。
               实测同一份 /api/credits/stats 响应里两个口径就不一致：
                 summary.usageToday = 2347.97（准确）
                 sum(accounts[].daily[date=today].usage) = 2669.06（偏高）
               _reconcile_official_usage() 已经专门校准过 summary.usageToday
               （见 web_proxy.py 该函数注释：官方 officialUsage.summary.usageToday
               长期为 0 会把准确的顶层值覆盖掉），所以顶层这个值是当前最准的。
               顺带修掉一个真 bug：原来的「昨天」口径取的是 new Date() 的
               本地时间减一天，而 daily 里的 date 是后端时区的日期键，
               跨时区/跨零点时会取错一天 —— 现在直接用后端算好的值，不再自己减。 */
            '<div class="wb-kpi-label">今日消耗</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
          '<div class="wb-kpi" id="wb-kpi-today">' +
            '<div class="wb-kpi-label">今日获得积分</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
          '<div class="wb-kpi" id="wb-kpi-total">' +
            '<div class="wb-kpi-label">累计任务积分</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
        '</div>' +

        /* 第二行 KPI：模型 / 调用 / 密钥 / 近 7 日消耗（4 张） */
        '<div id="wb-ov-kpi-row2">' +
          '<div class="wb-kpi" id="wb-kpi-models">' +
            '<div class="wb-kpi-label">可用模型</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
          '<div class="wb-kpi" id="wb-kpi-calls">' +
            '<div class="wb-kpi-label">今日调用</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
          '<div class="wb-kpi" id="wb-kpi-keys">' +
            '<div class="wb-kpi-label">API 密钥</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
          '<div class="wb-kpi" id="wb-kpi-week">' +
            '<div class="wb-kpi-label">近 7 日消耗</div>' +
            '<div class="wb-kpi-value">—</div>' +
            '<div class="wb-kpi-badge wb-kpi-badge-flat">读取中…</div></div>' +
        '</div>' +

        /* 中栏：趋势图 + 任务状态 */
        '<div id="wb-ov-mid-row">' +
          '<div class="wb-panel">' +
            '<div class="wb-panel-head">' +
              '<div><div class="wb-panel-title">近 30 天积分消耗</div>' +
              '<div class="wb-panel-sub">全网按日汇总 · 与积分统计页同源</div></div>' +
              '<span class="wb-panel-chip">30 天</span></div>' +
            '<svg id="wb-trend-svg" viewBox="0 0 600 130" preserveAspectRatio="none"></svg>' +
          '</div>' +
          '<div class="wb-panel">' +
            '<div class="wb-panel-head">' +
              '<div><div class="wb-panel-title">每日任务</div>' +
              '<div class="wb-panel-sub" id="wb-task-sub">下次自动执行 · 间隔 12h</div></div>' +
              /* 执行中时这块变成「执行中 2/24」（wbOvTaskProgressTick 动态改） */
              '<span class="wb-panel-chip" id="wb-task-chip">自动</span></div>' +
            '<div class="wb-task-bar"><div id="wb-task-fill" style="width:0%"></div></div>' +
            '<div class="wb-task-row"><span>已完成账号</span><b id="wb-task-acc">—</b></div>' +
            /* 执行中：当前正在跑的账号（无任务执行时隐藏）
               用户需求（2026-10-16）：「显示对应任务进度，比如2/24，方便用户查看」 */
            '<div class="wb-task-row" id="wb-task-cur-row" style="display:none"><span>当前账号</span><b id="wb-task-cur">—</b></div>' +
            '<div class="wb-task-row"><span>今日签到积分</span><b id="wb-task-checkin">—</b></div>' +
            '<div class="wb-task-row"><span>今日任务积分</span><b id="wb-task-taskcredits">—</b></div>' +
            '<div class="wb-task-row"><span>累计任务积分</span><b id="wb-task-total">—</b></div>' +
            '<div class="wb-task-row"><span>最近完成</span><b id="wb-task-last">—</b></div>' +
          '</div>' +
        '</div>' +

        /* 底栏：账号池分布 + 最近执行 */
        /* 底栏只剩「最近执行」：账号池相关内容已移除
           （用户 2026-10-17 要求「数据总览页面下方账号管理相关内容移除，
           账号管理有单独页面处理」）。账号池分布、正在调用的账号
           这些属于账号管理职责，概览页不再重复展示。 */
        '<div id="wb-ov-bottom-row">' +
          '<div class="wb-panel">' +
            '<div class="wb-panel-head">' +
              '<div><div class="wb-panel-title">最近执行</div>' +
              '<div class="wb-panel-sub">每日任务历史</div></div>' +
              '<span class="wb-panel-chip" id="wb-ov-runs-chip">—</span></div>' +
            '<div id="wb-ov-runs"></div>' +
          '</div>' +
        '</div>' +
      '</div>';
    return v;
  }

  /* ---------- 概览页视图切换（与 wb-daily 同机制） ---------- */
  /* 统一视图管理器：main 下有三个互斥视图状态
       ① 概览（#/wb-overview）→ 显示 #wb-overview-view，隐藏其他
       ② 每日任务（#/wb-daily）→ 显示 #wb-daily-view，隐藏其他
       ③ 官方页面（其余）→ 两个注入视图都隐藏，恢复官方子节点

     ⚠️ 2026-10-16 修复：原先 wbUpdateOverviewView 与 wbUpdateWbDailyView
     各自独立判断、各自恢复子节点，两者在 hashchange 上互相打架 ——
     wbUpdateWbDailyView 的 else 分支会无条件把所有 display:none 的
     子节点恢复成 ""，把刚被概览视图隐藏的官方内容又放出来，
     实测「从概览点账号管理后概览视图仍可见、官方内容也同时可见」
     （DOM 里 #wb-overview-view 与官方容器 display 都是 block）。
     现在合成一个函数，任何状态变更都走同一条路径，杜绝竞态。 */
  function wbApplyMainView() {
    var main = document.querySelector("main");
    if (!main) return;
    var hash = wbViewHash();
    var ov = document.getElementById("wb-overview-view");
    var daily = document.getElementById("wb-daily-view");

    var path = wbPath();
    /* 视图状态指纹：hash + path 都没变就说明已经应用过，直接返回。
       两个作用：
         · 省掉重复的 DOM 显隐操作（SPA 路由变化会频繁触发本函数）；
         · 掐断「pushState → popstate → SPA 重渲染 → MutationObserver →
           run() → 本函数」这条链，否则会无限循环（2026-10-17 实测踩过）。 */
    var viewKey = window.location.hash + "|" + path;
    if (viewKey === window.__wbViewKey) return;
    window.__wbViewKey = viewKey;

    var mode = "official";
    /* 路由规则（用户 2026-10-17 要求「数据总览取消页面后缀作为默认主页，
       账户管理加页面后缀」）：
         · 数据总览 = 默认主页，URL 就是 "/"（无 hash）
         · 账号管理 = hash 后缀 #/accounts（背后是官方 "/" 页面）
         · 每日任务 = hash 后缀 #/wb-daily
         · 其余官方路径按 path 匹配
       ⚠️ 为什么账号管理不能占用 "/"：官方 SPA 只认 / /token-stats
       /credit-stats /settings 四条路由（实测 pushState 到 /accounts
       会被打回 /），要给它一个可访问的地址只能走 hash 后缀。 */
    if (hash === "#/wb-daily") mode = "daily";
    else if (hash === "#/accounts") mode = "accounts";
    else if (hash === "#/wb-overview") mode = "overview";
    else if (hash === "" || hash === "#/") {
      // 无后缀：根路径才是数据总览；停在别的官方路径上时照常显示那一页
      mode = (path === "/" || path === "") ? "overview" : "official";
    }

    if (mode === "accounts") {
      /* 账号管理的背后是官方 "/" 页面。SPA 停在别的路由时先把它推回去，
         否则「恢复官方子节点」放出来的是上一个页面的内容
         （实测从词元统计点账号管理，看到的是词元统计）。 */
      if (path !== "/") {
        wbPushUrl("/" + hash);
      }
      mode = "official";   // 与官方页同一套显隐逻辑
    }

    if (mode === "overview") {
      if (!ov) {
        ov = wbCreateOverviewView();
        main.appendChild(ov);
      }
      ov.style.display = "block";
      if (daily) daily.style.display = "none";
      Array.prototype.slice.call(main.children).forEach(function (c) {
        if (c !== ov) c.style.display = "none";
      });
      wbLoadOverviewData();
      return;
    }

    if (mode === "daily") {
      if (!daily) {
        daily = wbCreateWbDailyView();
        main.appendChild(daily);
        wbHoistDailyCardTitles(daily);
      }
      /* 每次进入本页都归一化一次（幂等）：既兜住首次创建的漏网元素，
         也兜住 hoist 之后新插进来的游离节点。 */
      wbNormalizeDailyLayout(daily);
      daily.style.display = "block";
      if (ov) ov.style.display = "none";
      Array.prototype.slice.call(main.children).forEach(function (c) {
        if (c !== daily) c.style.display = "none";
      });
      // 数据新鲜度：TTL 内不重拉（避免每次切换都重渲染造成闪屏）
      var now = Date.now();
      if (!__wbDailyLoadedAt || (now - __wbDailyLoadedAt) > WB_DAILY_DATA_TTL_MS) {
        __wbDailyLoadedAt = now;
        wbLoadWbDailyData();
      }
      return;
    }

    // official：两个注入视图都藏起来，恢复官方子节点
    wbOvTaskProgressStop();
    if (ov) ov.style.display = "none";
    if (daily) daily.style.display = "none";
    Array.prototype.slice.call(main.children).forEach(function (c) {
      if (c !== ov && c !== daily && c.style.display === "none") {
        c.style.display = "";
      }
    });
  }

  /* 兼容旧名：内部仍有人调用 wbUpdateOverviewView */
  function wbUpdateOverviewView() {
    wbApplyMainView();
    wbSyncNavActive();
  }

  /* ---------- 导航高亮统一管理 ---------- */
  /* 导航高亮统一管理。
     ⚠️ 2026-10-16 修复：原先直接给 <a> 加官方的 bg-foreground/[0.06] 类，
     但那是 React 自己管理的状态类 —— SPA 重渲染时会把它按官方逻辑重新分配，
     于是出现「离开某页后该项仍高亮」「多个项同时高亮」。
     现在改用**专用类 wb-nav-active**：React 不认识它，不会碰，
     高亮状态完全由我们控制；CSS 也改成用 .wb-nav-active 写样式。
     规则：只有当前页（或点到的 hash 视图）那一项高亮，其它一律取消。 */
  function wbSyncNavActive() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    var hash = wbViewHash();
    var path = wbPath();
    var links = aside.querySelectorAll("nav a");
    links.forEach(function (a) {
      var href = a.getAttribute("href") || "";
      var isOv = a.id === WB_OVERVIEW_NAV_ID;
      var isDaily = a.id === "wb-nav-wb-daily";
      var isAccounts = a.id === WB_ACCOUNTS_NAV_ID;
      var active = false;
      if (hash === "#/wb-overview") {
        active = isOv;                      // 概览视图：只高亮概览
      } else if (hash === "#/wb-daily") {
        active = isDaily;                   // 每日任务视图：只高亮它
      } else if (hash === "#/accounts") {
        active = isAccounts;                // 账号管理：hash 后缀路由
      } else if (hash === "" || hash === "#/") {
        /* 无后缀：根路径 = 数据总览（默认主页），其余按官方路径匹配 */
        if (path === "/" || path === "") active = isOv;
        else active = !isOv && !isDaily && !isAccounts && path === href;
      } else {
        // 普通路由页：path 精确匹配才算；hash 视图项一律不高亮
        active = !isOv && !isDaily && !isAccounts && path === href;
      }
      if (active) {
        a.classList.add("wb-nav-active");
      } else {
        a.classList.remove("wb-nav-active");
        // ⚠️ 必须同时清掉 React 官方加的高亮类 —— 它由 React Router 自己管理，
        // 我们只切自己的类的话，官方类会残留在刚离开的选项卡上，
        // 表现为「点了数据总览，系统设置还有底色/阴影」
        // （2026-10-16 用户反馈的遗留问题）。
        a.classList.remove("bg-foreground/[0.06]", "font-medium", "text-foreground");
        a.classList.add("text-muted-foreground");
      }
      a.setAttribute("aria-current", active ? "page" : "false");
    });
  }

  /* ---------- 概览页数据 ---------- */
  function wbLoadOverviewData() {
    if (!window.__wbOvLoaded || (Date.now() - window.__wbOvLoaded) > 30000) {
      window.__wbOvLoaded = Date.now();
    } else {
      return;   // TTL 内不重拉，避免每次切换都闪
    }
    // KPI + 趋势 + 任务（与原首页同一套取数逻辑，只是挂到概览视图里）
    wbKpiInit();
    wbMidInit();
    /* 账号池分布 / 正在调用账号：已移除（用户 2026-10-17 要求
       「数据总览页面下方账号管理相关内容移除，账号管理有单独页面处理」）。
       原先这里会拉 /api/account-pool 与 /api/token-stats 渲染
       「账号池」卡片（启用/停用分布 + 正在调用的账号列表）。
       那些属于账号管理职责，概览页不再重复；账号管理页本身已具备。 */

    // 最近执行列表（每日任务历史）
    // 用户需求（2026-10-16）：
    //   ① 「最近执行卡片中如果有错误的，比如就显示进程中断，未完成即可」
    //      —— 不要再显示「退出码 0」「账号 0」这种看不懂的原始字段，
    //         直接给一句人话状态。
    //   ② 「点击执行记录可以直接在点击的当前任务下方显示具体信息，
    //       比如执行了多少，什么原因失败，收益多少等等，再点击一次收纳」
    //      —— 行可点，点在当前行下方插入一张明细卡，再点一次收起。
    wbJson("/api/wb-daily/runs", 15000).then(function (r) {
      var runs = (r && r.runs) || [];
      var chip = document.getElementById("wb-ov-runs-chip");
      if (chip) chip.textContent = runs.length ? ("近 " + Math.min(runs.length, 5) + " 次") : "暂无";
      var box = document.getElementById("wb-ov-runs");
      if (!box) return;
      if (!runs.length) {
        box.innerHTML = '<div class="wb-run-row"><span class="wb-run-detail" '
          + 'style="text-align:left;color:var(--muted-foreground)">暂无执行记录</span></div>';
        return;
      }

      /* 状态人话化：把 status + summary 翻译成用户能直接读的一句话。
         顺序很重要 —— 「进程中断」要排在通用 failed 之前，
         否则 12 条中断记录会全部显示成「失败」，等于没修。 */
      function runStatusText(run, sm) {
        var st = String(run.status || "");
        var det = String(sm.detail || sm.reason || "");
        if (st === "done") return { tone: "ok", text: "执行完成" };
        if (st === "running") return { tone: "run", text: "执行中" };
        if (st === "aborted") return { tone: "err", text: "已手动中止，未完成" };
        if (det.indexOf("中断") >= 0) return { tone: "err", text: "进程中断，未完成" };
        if (det.indexOf("超时") >= 0) return { tone: "err", text: "执行超时，未完成" };
        if (st === "failed") return { tone: "err", text: "执行失败，未完成" };
        return { tone: "err", text: det || "未完成" };
      }

      var html = "";
      runs.slice(0, 5).forEach(function (run, idx) {
        var sm = run.summary || {};
        var stt = runStatusText(run, sm);
        var ok = stt.tone === "ok";
        var ts = run.finished_at || run.finishedAt || run.started_at || 0;
        var when = "—";
        try {
          var dt = new Date(Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts));
          when = (dt.getMonth() + 1) + "/" + dt.getDate() + " "
            + String(dt.getHours()).padStart(2, "0") + ":"
            + String(dt.getMinutes()).padStart(2, "0");
        } catch (e) {}
        var mode = run.mode === "scheduled" ? "定时" : (run.mode === "manual" ? "手动" : String(run.mode || "?"));
        var accs = Number(run.accounts || 0);
        html += '<div class="wb-run-row wb-run-clickable" data-job="' + run.job_id + '" data-idx="' + idx + '" role="button" tabindex="0" title="点击查看本轮明细，再点一次收起">'
          + '<span class="wb-run-dot ' + (ok ? "ok" : "err") + '"></span>'
          + '<span class="wb-run-mode">' + mode + '</span>'
          + '<span class="wb-run-time">' + when + '</span>'
          + '<span class="wb-run-detail">' + stt.text + (accs ? ' · ' + accs + ' 账号' : '') + '</span>'
          + '<span class="wb-run-caret">▾</span>'
          + '</div>'
          /* 明细卡：默认不存在，点行时才插到该行下方（wbOvRunDetail） */
          + '<div class="wb-run-detailbox" id="wb-ov-run-detail-' + idx + '" style="display:none"></div>';
      });
      box.innerHTML = html;

      /* 行点击：点哪行就在哪行下方插入/移除明细卡（再点一次收纳）。
         ⚠️ 用事件委托而不是给每行 addEventListener —— innerHTML 重渲染后
         旧监听会失效，委托挂在 box 上只需绑一次。 */
      box.querySelectorAll(".wb-run-clickable").forEach(function (row) {
        row.addEventListener("click", function () {
          wbOvToggleRunDetail(row, runs);
        });
      });
    });

    wbOvTaskProgressStart();
  }

  /* ---------- 概览页「最近执行」行明细 ---------- */
  /* 点击某一行 → 在该行正下方插入明细卡；再点一次同一行 → 收起。
     数据源 /api/wb-daily/run-accounts/{job_id}（逐账号明细）。
     用户需求（2026-10-16）：「点击执行记录可以直接在点击的当前任务下方显示
     具体信息，比如执行了多少，什么原因失败，收益多少等等」。
     ⚠️ 空明细不再显示「本轮没有账号级明细（可能是旧记录或任务未跑完）」——
        用户明确说这类信息没有任何意义。改成按这一轮的汇总信息给出
        「跑了多少账号 / 为什么停 / 收益多少」，哪怕是中断轮也有内容可看。 */
  function wbOvToggleRunDetail(row, runs) {
    var box = document.getElementById("wb-ov-runs");
    if (!box) return;
    var idx = row.getAttribute("data-idx");
    var panel = document.getElementById("wb-ov-run-detail-" + idx);
    var wasOpen = panel && panel.style.display !== "none";
    // 先收起所有已展开的（手风琴：同时只开一个，卡片高度不会被撑爆）
    box.querySelectorAll(".wb-run-detailbox").forEach(function (p) {
      p.style.display = "none";
      p.innerHTML = "";
    });
    box.querySelectorAll(".wb-run-clickable").forEach(function (r) {
      r.classList.remove("wb-run-open");
    });
    if (wasOpen || !panel) return;      // 再点一次 = 收纳
    row.classList.add("wb-run-open");
    panel.style.display = "";
    panel.innerHTML = '<div class="wb-run-detail-loading">正在加载明细…</div>';
    wbOvRenderRunDetail(panel, runs[Number(idx)] || {});
  }

  function wbOvRenderRunDetail(panel, run) {
    var jid = String(run.job_id || "");
    var sm = run.summary || {};
    var stt = (function () {
      var st = String(run.status || "");
      var det = String(sm.detail || sm.reason || "");
      if (st === "done") return { tone: "ok", text: "执行完成" };
      if (st === "running") return { tone: "run", text: "执行中" };
      if (st === "aborted") return { tone: "err", text: "已手动中止，未完成" };
      if (det.indexOf("中断") >= 0) return { tone: "err", text: "进程中断，未完成" };
      if (det.indexOf("超时") >= 0) return { tone: "err", text: "执行超时，未完成" };
      if (st === "failed") return { tone: "err", text: "执行失败，未完成" };
      return { tone: "err", text: det || "未完成" };
    })();

    function finish(accounts) {
      var n = accounts.length;
      var doneN = 0, totN = 0;
      var credits = [], actions = [];
      accounts.forEach(function (a) {
        doneN += Number(a.done || 0);
        totN += Number(a.total || 0);
        if (a.credits) credits.push(a.credits);
        (a.actions || []).forEach(function (x) { actions.push(x); });
      });
      var h = '<div class="wb-rund-h"><b>' + jid + '</b>'
        + '<span class="wb-rund-st wb-rund-st-' + stt.tone + '">' + stt.text + '</span></div>'
        + '<div class="wb-rund-kv">'
        + '<span>跑了多少</span><b>' + (n ? (n + ' 个账号') : (run.accounts ? run.accounts + ' 个账号' : '—')) + '</b>'
        + '<span>任务完成</span><b>' + (totN ? (doneN + ' / ' + totN + ' 项') : '—') + '</b>'
        + '<span>失败原因</span><b>' + (stt.tone === "ok" ? '—' : (String(sm.detail || sm.reason || stt.text))) + '</b>'
        + '<span>积分收益</span><b>' + (credits.length ? credits.length + ' 个账号有积分数据' : '—') + '</b>'
        + '</div>';
      if (n) {
        h += '<div class="wb-rund-tbl">'
          + '<div class="wb-rund-tr wb-rund-th"><span>账号</span><span>完成</span><span>等级</span><span>连签</span></div>';
        accounts.slice(0, 24).forEach(function (a) {
          h += '<div class="wb-rund-tr"><span class="wb-rund-acc">' + (a.account || "?") + '</span>'
            + '<span>' + (a.done || 0) + '/' + (a.total || 0) + '</span>'
            + '<span>' + (a.level || "—") + '</span>'
            + '<span>' + (a.streak || "—") + '</span></div>';
        });
        if (n > 24) h += '<div class="wb-rund-more">另有 ' + (n - 24) + ' 个账号…</div>';
        h += '</div>';
      } else {
        // 没有逐账号明细（中断轮很常见）：给汇总，不再说「没有账号级明细」
        h += '<div class="wb-rund-note">'
          + '本轮没有逐账号执行数据'
          + (stt.tone === "ok" ? '（脚本已正常结束）' : '（进程在中途停止，未产出账号明细）')
          + '</div>';
      }
      if (actions.length) {
        h += '<div class="wb-rund-acts">' + actions.slice(0, 6).map(function (x) {
          return '<span>' + String(x).replace(/[\[\]]/g, "") + '</span>';
        }).join("") + '</div>';
      }
      panel.innerHTML = h;
    }

    if (!jid) { panel.innerHTML = '<div class="wb-run-detail-loading">无记录</div>'; return; }
    wbJson("/api/wb-daily/run-accounts/" + jid, 15000).then(function (d) {
      finish((d && d.accounts) || []);
    }).catch(function () { finish([]); });
  }

  /* ---------- 概览页任务进度轮询 ---------- */
  /* 用户需求（2026-10-16）：「在数据总览的最近任务和积分任务那里，如果有对应的
     任务在执行，则显示对应任务进度，比如2/24，方便用户查看」。
     数据源：/api/wb-daily/jobs（wb-daily 服务的内存态任务表）。
     Job.to_dict() 返回 accounts_total / accounts_done / progress / steps，
     其中 steps 最后一条形如「▶ 当前：账号19」—— 正在跑的账号名从这里取。
     ⚠️ jobs 是**进程内存态**，容器重启即丢；所以没有 running job 时
     必须把卡片恢复成静态文案，不能停在旧进度上（否则重启后一直显示 23/24）。
     ⚠️ 进度条口径用 accounts_done/accounts_total，不用 job.progress ——
     后端那个百分比掺了脚本输出阶段（阶段权重），和用户要的「2/24」对不上。 */
  function wbOvTaskProgressTick() {
    wbJson("/api/wb-daily/jobs", 15000).then(function (d) {
      var jobs = (d && d.jobs) || [];
      var job = null;
      for (var i = 0; i < jobs.length; i++) {
        if (jobs[i] && (jobs[i].status === "running" || jobs[i].status === "pending")) {
          job = jobs[i];
          break;
        }
      }
      var chip = document.getElementById("wb-task-chip");
      var sub = document.getElementById("wb-task-sub");
      var fill = document.getElementById("wb-task-fill");
      var acc = document.getElementById("wb-task-acc");
      var curRow = document.getElementById("wb-task-cur-row");
      var cur = document.getElementById("wb-task-cur");

      if (!job) {
        // 只在「刚才还在执行中」时恢复一次，避免每次 tick 都写 DOM
        if (window.__wbOvTaskRun) {
          if (chip) chip.textContent = "自动";
          if (sub) sub.textContent = "下次自动执行 · 间隔 12h";
          if (curRow) curRow.style.display = "none";
          if (acc) acc.textContent = "全部完成";
          if (fill) fill.style.width = "100%";
          // 任务跑完了，绕开 TTL 强制重拉一次 —— 否则用户盯在本页时
          // 「最近执行」列表和积分会一直停在任务开始前的旧数据。
          window.__wbOvLoaded = 0;
          wbLoadOverviewData();
        }
        window.__wbOvTaskRun = false;
        return;
      }
      window.__wbOvTaskRun = true;

      var total = Number(job.accounts_total || 0);
      var done = Number(job.accounts_done || 0);
      var ratio = total > 0 ? Math.min(100, done / total * 100) : 0;
      if (chip) chip.textContent = total > 0 ? ("执行中 " + done + "/" + total) : "执行中";
      if (sub) sub.textContent = total > 0 ? ("本轮 " + total + " 个账号") : "正在执行…";
      if (fill) fill.style.width = ratio + "%";
      if (acc) acc.textContent = total > 0 ? (done + " / " + total) : "—";

      if (curRow && cur) {
        // steps 倒序找「▶ 当前：xxx」这一条
        var steps = job.steps || [];
        var curName = "";
        for (var k = steps.length - 1; k >= 0; k--) {
          var st = String(steps[k] || "");
          if (st.indexOf("当前") >= 0) {
            var idx = st.indexOf("：");
            curName = idx >= 0 ? st.slice(idx + 1).trim() : st.replace("▶", "").trim();
            break;
          }
        }
        if (curName) {
          cur.textContent = curName;
          curRow.style.display = "";
        } else {
          curRow.style.display = "none";
        }
      }
    });
  }

  /* 概览页任务进度轮询开关：视图可见时 3s 一次，切走就停。
     ⚠️ 必须停：否则用户停在别的页面时，这个定时器每 3s 打一次接口。 */
  function wbOvTaskProgressStart() {
    if (window.__wbOvTaskTimer) return;
    wbOvTaskProgressTick();
    window.__wbOvTaskTimer = setInterval(wbOvTaskProgressTick, 3000);
  }

  function wbOvTaskProgressStop() {
    if (window.__wbOvTaskTimer) {
      clearInterval(window.__wbOvTaskTimer);
      window.__wbOvTaskTimer = null;
    }
  }

  /* ---------- 概览页操作按钮 ---------- */
  function wbBindOverviewActions() {
    var view = document.getElementById("wb-overview-view");
    if (!view || view.dataset.wbBound === "1") return;
    view.dataset.wbBound = "1";
    var cb = view.querySelector('[data-act="checkin"]');
    var tb = view.querySelector('[data-act="task"]');
    /* ⚠️ 2026-10-16 修复两个真 bug（用户反馈「点击执行任务，失败，错误未知」）：
       1) 「全部签到」原先调 /api/checkin/run —— 这个端点**不存在**，
          请求落到 SPA 兜底路由返回 HTML，r.json() 直接抛错，
          永远走 catch 显示「签到请求失败」。
       2) 「执行任务」判的是 d.ok，但 /api/wb-daily/run 真实返回
          {job_id, status:"started"}，没有 ok 字段 → 永远走失败分支。
       现在两个按钮都走 /api/wb-daily/run（每日任务服务本来就在做签到），
       并统一按 job_id 判定成功，与每日任务页的 wbManualCheckin 一致。 */
    function wbOvRun(label, btn) {
      btn.disabled = true;
      btn.textContent = "执行中…";
      fetch("/api/wb-daily/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({})
      })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          btn.disabled = false;
          btn.textContent = label;
          if (d && d.job_id) {
            wbToast("已触发" + label + "，可在「每日任务」页看进度");
            window.__wbOvLoaded = 0;   // 让下次进概览页重拉数据
          } else {
            wbToast(label + "启动失败：" + ((d && (d.detail || d.error)) || "未知原因"), "err");
          }
        })
        .catch(function () {
          btn.disabled = false;
          btn.textContent = label;
          wbToast("请求失败，请刷新页面后重试", "err");
        });
    }
    if (cb) cb.onclick = function () { wbOvRun("全部签到", this); };
    if (tb) tb.onclick = function () { wbOvRun("执行任务", this); };
  }
  /* ---------- KPI 汇总卡 ---------- */
  /* KPI 卡填数。
     ⚠️ v4 起 KPI 卡 DOM 由概览视图（wbCreateOverviewView）建好，
     本函数只负责**填数**，不再往首页注入 —— 用户要求
     「重要数据移动到概览页面，其他选项卡内容不变」。
     DOM 不存在（不在概览页）时静默返回。 */
  /* KPI 卡填数（v5：8 张）。
     ⚠️ 只填数不注入 —— DOM 由 wbCreateOverviewView() 建好。 */
  function wbKpiInit() {
    if (!document.getElementById("wb-kpi-credits")) return;

    var cs = wbJson("/api/credits/stats", 15000);       // 总积分 / 按日消耗
    var gi = wbJson("/api/gateway-info", 15000);        // 模型数
    var csum = wbJson("/api/wb-daily/credit-summary", 15000);  // 今日/累计积分
    var keys = wbJson("/api/api-keys", 15000);          // 密钥

    Promise.all([cs, gi, csum, keys]).then(function (r) {
      var stats = r[0] || {};
      var info = r[1] || {};
      var csum2 = r[2] || {};
      var keys2 = r[3] || {};

      var summ = stats.summary || {};
      var accs = stats.accounts || [];

      var today = new Date();
      var yd = new Date(today.getTime() - 86400000);
      var byd = new Date(today.getTime() - 2 * 86400000);
      function key(d) {
        return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
          + "-" + String(d.getDate()).padStart(2, "0");
      }
      var yKey = key(yd), byKey = key(byd), tKey = key(today);
      var yUsed = null, byUsed = null, wUsed = null;
      accs.forEach(function (a) {
        (a.daily || []).forEach(function (row2) {
          var d = String(row2.date || "");
          var u = Number(row2.usage || 0);
          if (d === yKey) yUsed = (yUsed || 0) + u;
          if (d === byKey) byUsed = (byUsed || 0) + u;
          if (d >= key(new Date(today.getTime() - 6 * 86400000))) {
            wUsed = (wUsed || 0) + u;
          }
        });
      });

      // —— 第一行：积分相关 ——
      /* 账号池总积分卡的徽标 = **今日获得 − 今日消耗**（净值）。
         用户 2026-10-16 指出：「第一个账号总积分下方今日增加的数据应该是
         获得的积分-消耗积分，当前为何和消耗积分相同」。
         根因：这里原先直接用了 summ.usageToday（纯消耗），
         于是徽标永远显示成消耗值，与旁边的「今日消耗」卡一模一样。
         现在改为：
           净值 = csum2.todayCredits（今日签到+任务获得）
                 − summ.usageToday（今日消耗）
         两个数来自不同接口但都是「今日」口径，可相减：
           · csum2 来自 /api/wb-daily/credit-summary（任务台账，落库的真实收益）
           · usageToday 来自 /api/credits/stats 的 summary
             （已由 _reconcile_official_usage 校准，见该函数注释）
         净值为正 → 绿箭头（积分在涨）；为负 → 红箭头（积分在掉）。 */
      setKpi("wb-kpi-credits", wbFmt(summ.currentRemaining), function (badge) {
        var gain = (csum2 && csum2.todayCredits != null) ? Number(csum2.todayCredits) : null;
        var used = summ.usageToday != null ? Number(summ.usageToday) : 0;
        if (gain === null) {
          // 拿不到收益数据时退回显示消耗，至少不是空白
          badge.className = "wb-kpi-badge wb-kpi-badge-flat";
          badge.textContent = "今日消耗 " + wbFmt(used);
          return;
        }
        var net = gain - used;
        if (net > 0) {
          badge.className = "wb-kpi-badge wb-kpi-badge-up";
          badge.textContent = "▲ 今日 +" + wbFmt(net);
        } else if (net < 0) {
          badge.className = "wb-kpi-badge wb-kpi-badge-down";
          badge.textContent = "▼ 今日 " + wbFmt(net);
        } else {
          badge.className = "wb-kpi-badge wb-kpi-badge-flat";
          badge.textContent = "今日持平";
        }
      });

      // 今日消耗：用 summary.usageToday（已由 _reconcile_official_usage 校准的准确值）
      // 徽标改为与昨日对比，仍用 accounts[].daily 的昨日累加作参照 —— 那个维度
      // 没有更准的顶层字段可用，作为「较昨日」的百分比基准足够。
      var tUsed = summ.usageToday != null ? Number(summ.usageToday) : null;
      setKpi("wb-kpi-used", tUsed === null ? "—" : wbFmt(tUsed), function (badge) {
        var p = wbPct(tUsed, yUsed);
        if (p === null || yUsed === null) {
          badge.className = "wb-kpi-badge wb-kpi-badge-flat";
          badge.textContent = "较昨日 —";
        } else if (p >= 0) {
          badge.className = "wb-kpi-badge wb-kpi-badge-up";
          badge.textContent = "▲ 较昨日 +" + p.toFixed(0) + "%";
        } else {
          badge.className = "wb-kpi-badge wb-kpi-badge-down";
          badge.textContent = "▼ 较昨日 " + p.toFixed(0) + "%";
        }
      });

      // 今日获得积分（签到+任务）
      var todayC = (csum2 && csum2.todayCredits != null) ? Number(csum2.todayCredits) : null;
      setKpi("wb-kpi-today", todayC === null ? "—" : wbFmt(todayC), function (badge) {
        badge.className = "wb-kpi-badge "
          + (todayC !== null && todayC > 0 ? "wb-kpi-badge-up" : "wb-kpi-badge-flat");
        badge.textContent = (todayC !== null && todayC > 0) ? "▲ 任务已获得" : "今日暂无获得";
      });

      // 累计任务积分
      setKpi("wb-kpi-total", wbFmt(csum2 && csum2.totalCredits), function (badge) {
        badge.className = "wb-kpi-badge wb-kpi-badge-flat";
        badge.textContent = "台账累计";
      });

      // —— 第二行：模型 / 调用 / 密钥 / 近 7 日 ——
      // ⚠️ autoDiscovered 用接口返回值，不要自己 count 减基准
      var models = (info.models && info.models.count) || 0;
      var auto = (info.models && info.models.autoDiscovered) || 0;
      setKpi("wb-kpi-models", wbFmt(models), function (badge) {
        badge.className = "wb-kpi-badge wb-kpi-badge-flat";
        badge.textContent = "自动发现 " + auto;
      });

      // 今日调用（网关账本按日聚合）
      wbJson("/api/token-stats", 15000).then(function (agg) {
        var calls = 0;
        ((agg && agg.sources) || []).forEach(function (s2) {
          (s2.daily || []).forEach(function (row3) {
            if (String(row3.key) === tKey) calls += Number(row3.records || 0);
          });
        });
        setKpi("wb-kpi-calls", wbFmt(calls), function (badge) {
          badge.className = "wb-kpi-badge wb-kpi-badge-flat";
          badge.textContent = accs.length + " 账号在线";
        });
      });

      // API 密钥（启用数 / 总数）
      var kArr = (keys2 && keys2.keys) || [];
      var kOn = kArr.filter(function (k) { return k && k.enabled; }).length;
      setKpi("wb-kpi-keys", wbFmt(kArr.length), function (badge) {
        badge.className = "wb-kpi-badge "
          + (kArr.length ? "wb-kpi-badge-up" : "wb-kpi-badge-flat");
        badge.textContent = kArr.length ? (kOn + " 个启用") : "未创建";
      });

      // 近 7 日消耗
      setKpi("wb-kpi-week", wUsed === null ? "—" : wbFmt(wUsed), function (badge) {
        badge.className = "wb-kpi-badge wb-kpi-badge-flat";
        badge.textContent = "含今日";
      });

      // 任务状态卡（今日签到 / 今日任务 / 累计）
      var set = function (id, v) {
        var el = document.getElementById(id);
        if (el) el.textContent = v;
      };
      set("wb-task-checkin", wbFmt(csum2 && csum2.todayCheckinCredits));
      set("wb-task-taskcredits", wbFmt(csum2 && csum2.todayTaskCredits));
      set("wb-task-total", wbFmt(csum2 && csum2.totalCredits));
      var fill = document.getElementById("wb-task-fill");
      if (fill) fill.style.width = (todayC !== null && todayC > 0) ? "100%" : "0%";
      set("wb-task-acc", (todayC !== null && todayC > 0) ? "全部完成" : "未开始");
    });

    function setKpi(id, val, badgeFn) {
      var el = document.getElementById(id);
      if (!el) return;
      el.querySelector(".wb-kpi-value").textContent = val;
      var badge = el.querySelector(".wb-kpi-badge");
      if (badge && badgeFn) badgeFn(badge);
    }
  }

  /* ---------- 趋势图 + 任务状态 ---------- */
  /* 趋势图 + 任务状态填数。
     ⚠️ v4 起 DOM 由概览视图建好，本函数只填数不注入。 */
  function wbMidInit() {
    if (!document.getElementById("wb-trend-svg")) return;

    // —— 趋势图数据：官方 credits/stats 的顶层 daily（按日序列）——
    wbJson("/api/credits/stats", 15000).then(function (d) {
      var daily = (d && d.daily) || [];
      // 兼容两种形态：数组 [{date,usage}] 或对象 {date:usage}
      var arr = [];
      if (Array.isArray(daily)) {
        arr = daily.map(function (r) {
          return { d: String(r.date || r.key || ""), v: Number(r.usage || r.credit || 0) };
        });
      } else if (typeof daily === "object") {
        Object.keys(daily).forEach(function (k) { arr.push({ d: k, v: Number(daily[k] || 0) }); });
      }
      // 若顶层没有，从 accounts[].daily 聚合
      if (!arr.length && Array.isArray(d.accounts)) {
        var agg = {};
        d.accounts.forEach(function (a) {
          (a.daily || []).forEach(function (r) {
            var k = String(r.date || "");
            agg[k] = (agg[k] || 0) + Number(r.usage || 0);
          });
        });
        Object.keys(agg).forEach(function (k) { arr.push({ d: k, v: agg[k] }); });
      }
      // 固定 31 天窗口：今天往前推 30 天，**缺失日期补 0**。
      // 用户需求（2026-10-17）：「统计页面不就是 30 天的吗？数据没有的 0 就是了，
      // 后面随着时间推移不就完整了」。官方积分统计页同样是 31 天窗口
      // （见本文件 _rebuild_official_usage 里 _days 的构造方式）。
      // 上游 credits/stats 的 daily 只保留 3 天，不自己补窗口的话
      // 趋势图永远只有 3 个点，看着像「只有近 3 天」。
      var byDay = {};
      arr.forEach(function (x) { byDay[x.d] = x.v; });
      var win = [];
      var base = new Date();
      base.setHours(0, 0, 0, 0);
      for (var wi = 30; wi >= 0; wi--) {
        var wd = new Date(base.getTime() - wi * 86400000);
        var wk = wd.getFullYear() + "-" + String(wd.getMonth() + 1).padStart(2, "0")
          + "-" + String(wd.getDate()).padStart(2, "0");
        win.push({ d: wk, v: Number(byDay[wk] || 0) });
      }
      wbDrawTrend(win);
    });

    // —— 任务状态：wb-daily 汇总 ——
    wbJson("/api/wb-daily/credit-summary", 15000).then(function (d) {
      var todayC = d && d.todayCredits != null ? Number(d.todayCredits) : null;
      var totalC = d && d.totalCredits != null ? Number(d.totalCredits) : null;
      var set = function (id, v) { var el = document.getElementById(id); if (el) el.textContent = v; };
      set("wb-task-credits", wbFmt(todayC));
      set("wb-task-total", wbFmt(totalC));
      // 进度：有今日积分视为已完成（口径与任务页一致）
      var fill = document.getElementById("wb-task-fill");
      if (fill) fill.style.width = (todayC !== null && todayC > 0) ? "100%" : "0%";
      set("wb-task-acc", (todayC !== null && todayC > 0) ? "全部完成" : "未开始");
      // 最近完成时间
      wbJson("/api/wb-daily/runs", 15000).then(function (r2) {
        var runs = (r2 && r2.runs) || [];
        if (runs.length) {
          var last = runs[0];
          var ts = last.finishedAt || last.finished_at || last.startedAt || 0;
          if (ts) {
            var dt = new Date(Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts));
            set("wb-task-last", dt.getFullYear() + "-" + String(dt.getMonth() + 1).padStart(2, "0")
              + "-" + String(dt.getDate()).padStart(2, "0"));
          }
        }
      });
    });
  }

  function wbDrawTrend(arr) {
    var svg = document.getElementById("wb-trend-svg");
    if (!svg) return;
    var W = 600, H = 130, PAD_L = 4, PAD_R = 4, PAD_T = 8, PAD_B = 16;
    if (!arr.length) {
      svg.innerHTML = '<text x="' + (W / 2) + '" y="' + (H / 2) + '" text-anchor="middle">暂无消耗数据</text>';
      return;
    }
    var max = 0;
    arr.forEach(function (x) { if (x.v > max) max = x.v; });
    if (max <= 0) max = 1;
    var n = arr.length;
    var iw = W - PAD_L - PAD_R, ih = H - PAD_T - PAD_B;
    var pts = arr.map(function (x, i) {
      var px = PAD_L + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
      var py = PAD_T + ih - (x.v / max) * ih;
      return { x: px, y: py, d: x.d, v: x.v };
    });
    var line = pts.map(function (p, i) {
      return (i === 0 ? "M" : "L") + p.x.toFixed(1) + " " + p.y.toFixed(1);
    }).join(" ");
    var area = line + " L" + pts[pts.length - 1].x.toFixed(1) + " " + (H - PAD_B)
      + " L" + pts[0].x.toFixed(1) + " " + (H - PAD_B) + " Z";
    var html = '<path class="wb-trend-area" d="' + area + '"/>'
      + '<path class="wb-trend-line" d="' + line + '"/>'
      + '<line class="wb-trend-axis" x1="' + PAD_L + '" y1="' + (H - PAD_B) + '" x2="' + (W - PAD_R) + '" y2="' + (H - PAD_B) + '"/>'
      // 末点高亮
      + '<circle class="wb-trend-dot" cx="' + pts[pts.length - 1].x.toFixed(1) + '" cy="' + pts[pts.length - 1].y.toFixed(1) + '" r="3"/>';
    // 三个时间标签：首 / 中 / 末
    [0, Math.floor(n / 2), n - 1].forEach(function (idx) {
      if (!pts[idx]) return;
      var anchor = idx === 0 ? "start" : (idx === n - 1 ? "end" : "middle");
      var label = pts[idx].d.slice(5).replace("-", "/");
      html += '<text x="' + pts[idx].x.toFixed(1) + '" y="' + (H - 4) + '" text-anchor="' + anchor + '">' + label + '</text>';
    });
    // 峰值标注
    var peak = pts.reduce(function (a, b) { return b.v > a.v ? b : a; }, pts[0]);
    if (peak && peak.v > 0) {
      html += '<text x="' + Math.min(peak.x, W - 40).toFixed(1) + '" y="' + Math.max(peak.y - 4, 9).toFixed(1) + '" text-anchor="middle">峰值 ' + wbFmt(peak.v) + '</text>';
    }
    svg.innerHTML = html;
  }
  function wbInitThemeSwitch(aside) {
    if (!aside || document.getElementById("wb-theme-switch")) return;

    var box = document.createElement("div");
    box.id = "wb-theme-switch";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "界面主题");

    var MODES = [
      { id: "system", label: "跟随系统", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>' },
      { id: "light", label: "浅色", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>' },
      { id: "dark", label: "深色", svg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>' }
    ];

    MODES.forEach(function (m) {
      var b = document.createElement("button");
      b.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
      b.className = "wb-ts-btn";
      b.setAttribute("data-theme", m.id);
      b.setAttribute("aria-pressed", "false");
      b.title = m.id === "system" ? "跟随系统外观"
        : (m.id === "light" ? "始终浅色" : "始终深色");
      b.innerHTML = m.svg;
      b.onclick = function (e) {
        e.stopPropagation();
        wbThemeApply(m.id, true);
      };
      box.appendChild(b);
    });

    // v5：挂到顶栏内盒子（限宽居中的那一层），由 CSS grid 第 3 列推到右侧。
    wbTopbarInner(aside).appendChild(box);
    wbThemeApply(wbThemeCurrent(), false);



    // 系统外观变化时，只有当前处于「系统」模式才跟着变
    try {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onSysChange = function () {
        if (wbThemeCurrent() === "system") wbThemeApply("system", false);
      };
      if (mq.addEventListener) mq.addEventListener("change", onSysChange);
      else if (mq.addListener) mq.addListener(onSysChange);
    } catch (e) {}
  }

  /* 隐藏设置页里的官方「主题」项（用户 2026-10-16 反馈：
     「设置页主题取消，和右上角重复」）。

     主题已挪到顶栏右上角，两处指向同一份 localStorage，
     设置页那一项留着就是重复入口，且会让用户以为有两个状态。

     只隐藏 DOM，**不动官方状态机** —— 万一以后想放回来，
     把这里 return 掉即可，官方 React 组件仍是活的。

     官方 DOM（实测）：
       容器 SECTION（无 id）→ h2#settings-appearance「外观」
         → 内含 label「主题」的那一行（#appearance-theme 触发器在里面）
     因此要隐藏两处：主题行 + 「外观」标题（标题留着是空壳）。 */
  function wbHideSettingsTheme() {
    var btn = document.getElementById("appearance-theme");
    if (btn) {
      // 往上找含 label「主题」的那一行
      var node = btn;
      for (var i = 0; i < 6 && node; i++) {
        var label = node.querySelector ? node.querySelector("label") : null;
        if (label && (label.textContent || "").trim() === "主题") {
          node.style.display = "none";
          break;
        }
        node = node.parentElement;
      }
    }
    // 「外观」整个 SECTION 都要收掉。
    // ⚠️ 只隐藏 DIV.px-1（标题的父节点）不够 —— 官方 DOM 是
    //   SECTION.min-w-0.space-y-2.5  ← 就是这个空壳留在页面上
    //     └─ DIV.px-1
    //          └─ h2#settings-appearance
    // 隐藏 DIV.px-1 后 SECTION 内容为空，但仍占据布局：实测它塌成
    // **2px 高的横条**（y=198, h=2）卡在「账号资料」上方，
    // 用户 2026-10-16 反馈「设置页面账号资料上方有遗留横向」。
    // 因此这里一路向上找到 SECTION 本身，整块隐藏。
    var title = document.getElementById("settings-appearance");
    if (title) {
      var node = title;
      var sec = null;
      for (var i = 0; i < 5 && node; i++) {
        if (node.tagName === "SECTION") { sec = node; break; }
        node = node.parentElement;
      }
      if (sec) {
        sec.style.display = "none";
      } else {
        // 兜底：没找到 SECTION 就按老办法隐藏标题的父节点
        var wrap = title.parentElement;
        if (wrap) wrap.style.display = "none";
      }
    }
  }

  function enforceTitle() {
    // 浏览器标签标题精简为固定短名，官方标题过长会被标签栏截断
    if (document.title !== "AutoBuddy") {
      document.title = "AutoBuddy";
    }
  }

  var wbModelsCache = null;

  // 本地点按后需要立刻重绘，但重新拉一次 /api/account-models 会有可见延迟。
  // 这里在内存里维护一份「账号 -> {模型: 来源}」，点一下就地改、就地重绘，
  // 网络请求只负责落盘，不参与渲染，点按手感才是即时的。
  // 存来源而不只存布尔值，是为了区分手动禁用（我关的）与巡检禁用（它关的）。
  var wbModelPolicy = {};

  function wbPolicySet(accountId) {
    if (!wbModelPolicy[accountId]) wbModelPolicy[accountId] = {};
    return wbModelPolicy[accountId];
  }

  // "manual" | "auto" | undefined
  function wbModelSource(accountId, model) {
    var set = wbModelPolicy[accountId];
    return set ? set[model] : undefined;
  }

  function wbIsModelDisabled(accountId, model) {
    return !!wbModelSource(accountId, model);
  }

  function wbToggleModel(accountId, model, done) {
    var next = !wbIsModelDisabled(accountId, model);
    var set = wbPolicySet(accountId);
    // 人点出来的禁用一律算 manual —— 这是它后续不被巡检自动放开的依据。
    if (next) { set[model] = "manual"; } else { delete set[model]; }

    fetch("/api/account-models", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId, model: model, disabled: next })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          // 落盘失败就把内存里的乐观改动回滚，否则界面会显示一个并未生效的状态。
          if (next) { delete wbPolicySet(accountId)[model]; }
          else { wbPolicySet(accountId)[model] = "manual"; }
          var d = res.body.detail;
          wbToast(typeof d === "string" && d ? d : "操作失败，请重试", true);
          if (done) done(false);
          return;
        }
        wbModelPolicy[accountId] = {};
        wbApplySources(accountId, res.body.disabledSources, res.body.disabledModels);
        wbToast(next ? ("已禁用 " + model + " · 不再对该账号轮询") : ("已恢复 " + model));
        if (done) done(true);
      })
      .catch(function () {
        if (next) { delete wbPolicySet(accountId)[model]; }
        else { wbPolicySet(accountId)[model] = "manual"; }
        wbToast("请求失败，请刷新页面后重试", true);
        if (done) done(false);
      });
  }

  // 渲染「一致性自检」的结果：把每个账号的凭据结论与模型结论并排摆出来。
  //
  // 这块存在的全部理由是**消除口径矛盾**：凭据与模型是两个独立维度，
  // 过去界面上只有一个笼统状态，于是出现过「检测说凭据有效、巡检说凭据失效」
  // 这种让人无从判断的局面。这里把两侧各自的结论与判据一起列出，
  // 让「凭据好但被上游拦截」「某模型无权限」一眼就能分开。
  function wbRenderAuditBox(card, data) {
    var old = card.querySelector(".wb-audit-box");
    if (old && old.parentNode) old.parentNode.removeChild(old);

    var box = wbEl("div", "wb-api-row wb-api-row-stack wb-audit-box");
    var main = wbEl("div", "wb-api-main");
    main.appendChild(wbEl("div", "wb-api-label",
      "一致性自检 · " + (data.accounts || 0) + " 个账号"));

    var rows = data.rows || [];
    var needAttention = rows.filter(function (r) {
      return r.consistency && r.consistency.indexOf("一致：均正常") !== 0;
    });
    main.appendChild(wbEl("div", "wb-api-desc",
      needAttention.length
        ? "需要关注的账号 " + needAttention.length + " 个（下面标红/标黄的行）："
        : "全部账号两侧结论一致，无需处理。"));

    rows.forEach(function (r) {
      var line = wbEl("div", "wb-audit-row");
      // 状态着色：失效=红、受限=琥珀、正常=默认。
      var st = (r.credential || {}).state;
      var cls = "wb-audit-state";
      // 额度用尽与「账号受限」同为琥珀：两者都不是凭据坏了，
      // 用红色会让人跑去重新登录 —— 而重登对这两种情况毫无帮助。
      // 但它是独立一档，必须显式列出，否则会掉进「默认无着色」，
      // 与「未检测」看起来一样（用户 2026-09-30 反馈「显示没检测出结果」）。
      if (st === "invalid") cls += " wb-audit-bad";
      else if (st === "restricted") cls += " wb-audit-warn";
      else if (st === "quota_exhausted") cls += " wb-audit-warn";
      else if (st === "valid") cls += " wb-audit-ok";
      line.appendChild(wbEl("span", cls, (r.credential || {}).label || "未检测"));
      line.appendChild(wbEl("span", "wb-audit-name", r.name || r.id));
      line.appendChild(wbEl("span", "wb-audit-consistency", r.consistency || ""));
      // 判定依据必须印出来：「凭据有效」与「账号受限」的差别全在这一句里
      // （前者是上游以模型不存在应答，后者是请求被内容审查拦下）。
      // 只给一个标签，用户还是分不清该重登还是该找上游。
      var detail = (r.credential || {}).detail || "";
      if (detail) {
        line.appendChild(wbEl("span", "wb-audit-detail", detail));
      }
      // 判据与建议：这是这块最该被读到的部分 —— 它解释了「为什么是这个结论」。
      line.appendChild(wbEl("span", "wb-audit-advice", r.advice || ""));
      main.appendChild(line);
    });

    // 把判据本身也印出来，让「统一标准」是可查的而不是口头承诺。
    var crit = data.criteria || {};
    if (crit.credential) {
      main.appendChild(wbEl("div", "wb-api-desc wb-audit-criteria",
        "判据 · 凭据：" + crit.credential));
    }
    if (crit.model) {
      main.appendChild(wbEl("div", "wb-api-desc wb-audit-criteria",
        "判据 · 模型：" + crit.model));
    }
    if (crit.note) {
      main.appendChild(wbEl("div", "wb-api-desc wb-audit-criteria", "判据 · " + crit.note));
    }
    box.appendChild(main);
    card.appendChild(box);
  }

  // 账号级停用（与模型级是两回事：这条管「整个账号参不参与轮询」）。
  // 与 wbToggleModel 同一套写法：先乐观改本地状态让点按即时，再落盘，
  // 失败就把乐观改动回滚 —— 否则界面会显示一个并未生效的状态。
  // v1.0.5：done(ok, status) 把后端随响应带回的最新账号池状态传给调用方，
  // 供就地重绘单张卡片（不再全表拆建）。
  function wbToggleAccount(accountId, disabled, done) {
    fetch("/api/account-pool/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId, disabled: !!disabled })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail;
          wbToast(typeof d === "string" && d ? d : "操作失败，请重试", true);
          if (done) done(false, null);
          return;
        }
        wbToast(disabled ? "已停用该账号 · 不再参与轮询" : "已恢复该账号参与轮询");
        // 响应体自带最新 status，同步进缓存，供就地重绘用。
        if (res.body && res.body.status) { wbPoolCache = res.body.status; }
        if (done) done(true, res.body && res.body.status);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", true);
        if (done) done(false, null);
      });
  }

  // 后端下发的来源表是权威值，覆盖本地乐观副本。
  // 兼容只有 disabledModels（无来源表）的旧响应：那些一律当手动禁用。
  function wbApplySources(accountId, sources, models) {
    wbModelPolicy[accountId] = {};
    var set = wbModelPolicy[accountId];
    if (sources && typeof sources === "object") {
      Object.keys(sources).forEach(function (m) {
        set[m] = sources[m] === "auto" ? "auto" : "manual";
      });
      return;
    }
    (models || []).forEach(function (m) { set[m] = "manual"; });
  }

  function injectAccountModels() {
    // 在每个账号卡片下方动态插入「已使用模型」标签。
    // 数据来自官方 usage 记录（按 accountId + model 聚合），不硬编码模型清单。
    //
    // 每个标签可点击：点一下把该模型对该账号禁用（不参与轮询），再点恢复。
    // 「可用 / 已用 / 已禁用」三态互斥，已禁用优先于已用 —— 用户主动关闭是更强的意图。
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    if (!cards.length) return;

    var pending = cards.filter(function (c) {
      if (c.querySelector(".wb-am-box")) return false;
      if (!c.querySelector("h3")) return false;
      // 异常/需重新登录账号也正常注入模型区（保持卡片结构完整），
      // 后续由 wbApplyReloginDisabled 统一样式灰化/只读禁用处理。
      return true;
    });
    if (!pending.length) return;

    function render(data) {
      // 建立多重索引：卡片标题是账号昵称，而新账号常常没有昵称
      // （官方流水里 accountName 为空，只能回退成账号 ID）。
      // 只按昵称建索引的话，新账号卡片标题对不上任何键 → 落进 fallback →
      // 丢掉 accountId → 点击禁用 / 全部恢复这些按钮全数不渲染。
      // 所以三个键都注：name、aliases（后端下发）、id。
      var byName = {};
      var accs = (data && data.accounts) || {};
      Object.keys(accs).forEach(function (id) {
        var a = accs[id];
        if (!a) return;
        [a.name, a.id].concat(a.aliases || []).forEach(function (key) {
          if (key) byName[String(key)] = a;
        });
      });
      // 账号没有任何调用记录时（例如刚添加）也要展示网关可路由的完整清单，
      // 并带上 id —— 没有 id 就等于没有交互按钮。
      var fallback = { models: (data && data.catalog) || [], used: [] };
      if (!fallback.models.length) return;

      // 账号池顺序（后端下发），供标题对不上时按位置兜底取 id。
      var pendingIds = Object.keys(accs).map(function (k) { return accs[k] && accs[k].id ? accs[k].id : k; });

      pending.forEach(function (card, cardIndex) {
        if (card.querySelector(".wb-am-box")) return;
        var h3 = card.querySelector("h3");
        if (!h3) return;
        // 异常账号卡片也渲染模型区，随后由 wbApplyReloginDisabled 灰化只读处理。

        var entry = byName[(h3.textContent || "").trim()] || fallback;
        var models = (entry.models && entry.models.length) ? entry.models : fallback.models;
        var used = {};
        (entry.used || []).forEach(function (m) { used[m] = true; });
        if (!models.length) return;

        // 该卡片的账号 id：取自后端，避免用昵称反查导致的错配。
        // 标题查不到时（新账号/昵称被格式化过）按账号池顺序兜底，
        // 不轻易置 null —— accountId 为空会让整张卡片变成只读展示。
        var accountId = entry.id || null;
        if (!accountId && cardIndex < pendingIds.length) {
          accountId = pendingIds[cardIndex] || null;
        }

        var box = document.createElement("div");
        box.className = "wb-am-box";

        // 标题**不带**任何「已调用 N」计数。
        // 模型维度的「已调用 N」与账号池条的「已调用 N 次」用同一个词、口径却不同
        // （一个是「用过几个模型」，一个是「账号被分到几次请求」），并排出现极易混淆。
        // 模型是否被调用过，由标签高亮（wb-am-tag-used）表达即可 —— 用户 2026-09-19 反馈。
        var title = document.createElement("div");
        title.className = "wb-am-title";

        var titleText = document.createElement("span");
        titleText.textContent = "可用模型 · " + models.length;
        title.appendChild(titleText);

        // 「已禁用」只数**人点的**，「巡检」只数**巡检写的** —— 两个数互不重叠。
        // 曾经写成「已禁用 = 全部禁用（含巡检）」再单列一个「巡检 N」，
        // 同一批模型被数了两遍：巡检禁掉一个模型，用户会看到「已禁用」也跟着 +1，
        // 根本分不清哪个是自己点的、哪个是巡检写的。
        var manualCount = 0;
        var autoCount = 0;
        models.forEach(function (m) {
          if (!accountId) return;
          var src = wbModelSource(accountId, m);
          if (!src) return;
          if (src === "auto") autoCount += 1; else manualCount += 1;
        });
        if (manualCount) {
          var badge = document.createElement("span");
          badge.className = "wb-am-offcount";
          badge.textContent = "已禁用 " + manualCount;
          badge.title = "你手动禁用的模型 " + manualCount
            + " 个 —— 巡检不会自动放开，只有你点它才恢复";
          title.appendChild(badge);
        }
        if (autoCount) {
          // 巡检关的单独标出：它与手动禁用一样「不参与调用」，但会自行解除，
          // 用户需要能分辨，否则会以为是自己误操作。
          var autoBadge = document.createElement("span");
          autoBadge.className = "wb-am-offcount wb-am-offcount-auto";
          autoBadge.textContent = "巡检 " + autoCount;
          autoBadge.title = "可用性巡检自动禁用的模型 " + autoCount
            + " 个（探测到调用不通），模型恢复后会自动放开";
          title.appendChild(autoBadge);
        }

        // 「全部恢复」：一次把这张卡片下所有被禁用的模型放回轮询。
        //
        // 只在**确实有禁用项**时出现，否则按钮点了也不知道在恢复什么。
        // 覆盖手动与巡检两种来源 —— 所以文案不写「恢复巡检禁用」，
        // 那是另一件事（scope=auto 只在需要保留人工决策时用）。
        // 巡检误禁一批、或手动调完想一次性放开时，逐个点标签太碎。
        if (accountId && (manualCount + autoCount) > 0) {
          var restore = document.createElement("button");
          restore.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
          restore.className = "wb-am-restore";
          restore.textContent = "全部恢复 " + (manualCount + autoCount);
          restore.title = "把该账号被禁用的 " + (manualCount + autoCount)
            + " 个模型全部放回轮询（含手动禁用）";
          restore.onclick = function () {
            if (restore.dataset.wbBusy === "1") return;
            restore.dataset.wbBusy = "1";
            restore.disabled = true;
            var before = restore.textContent;
            restore.textContent = "恢复中…";
            fetch("/api/account-models/restore", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ accountId: accountId, scope: "all" })
            })
              .then(function (r) {
                return r.json().catch(function () { return {}; })
                  .then(function (b) { return { ok: r.ok, body: b || {} }; });
              })
              .then(function (res) {
                if (!res.ok || res.body.ok === false) {
                  var d = res.body.detail;
                  wbToast(typeof d === "string" && d ? d : "恢复失败，请重试", "error");
                  restore.textContent = before;
                  restore.disabled = false;
                  delete restore.dataset.wbBusy;
                  return;
                }
                // 策略变了：清掉缓存 + 直接重绘模型区，反馈即时。
                wbModelPolicy[accountId] = {};
                wbModelsCache = null;
                wbToast("已恢复 " + (res.body.restoredCount || 0) + " 个模型");
                box.remove();
                injectAccountModels();
              })
              .catch(function () {
                wbToast("请求失败，请刷新页面后重试", "error");
                restore.textContent = before;
                restore.disabled = false;
                delete restore.dataset.wbBusy;
              });
          };
          title.appendChild(restore);
        }

        var usage = entry.usage || {};
        var list = document.createElement("div");
        list.className = "wb-am-list";
        models.forEach(function (m) {
          var src = accountId ? wbModelSource(accountId, m) : undefined;
          var off = !!src;
          var tag = document.createElement("span");
          // 已禁用优先：禁用态压掉「已用」高亮，否则用户看不到自己刚点掉的那个。
          var stateClass = src === "auto" ? "wb-am-tag-off wb-am-tag-auto"
            : (off ? "wb-am-tag-off" : (used[m] ? "wb-am-tag-used" : ""));
          tag.className = ("wb-am-tag " + stateClass).replace(/\s+$/, "")
            + (accountId ? " wb-am-tag-click" : "");
          tag.textContent = m;
          var stat = usage[m];
          var tip = [];
          if (src === "auto") {
            tip.push("巡检发现调用不通，已自动禁用");
            tip.push("模型恢复后会自动启用");
          } else if (off) {
            tip.push("已手动禁用：该账号不再被分配到 " + m);
            tip.push("巡检不会自动放开手动禁用的模型");
          }
          if (stat) {
            // 只留「用量」类信息（Token / 积分），**不再显示调用次数**。
            // 调用次数是账号维度的指标，只保留在账号池条上的「已调用 N 次」一处。
            if (stat.tokens) tip.push("约 " + stat.tokens + " Token");
            if (stat.credit != null) tip.push("积分 " + Math.round(stat.credit * 100) / 100);
          } else if (used[m] && !off) {
            tip.push("该账号用过此模型");
          }
          if (accountId) {
            tip.push(off ? "点击恢复" : "点击禁用（不再对该账号轮询）");
            tag.onclick = function () {
              if (tag.dataset.wbBusy === "1") return;
              tag.dataset.wbBusy = "1";
              wbToggleModel(accountId, m, function () {
                // 就地重绘：只清掉本卡的模型区，用缓存重画（不再打一次网络请求），
                // 点按反馈才是即时的。缓存里的 updated 状态由 wbToggleModel 维护。
                box.remove();
                injectAccountModels();
              });
            };
            tag.setAttribute("role", "button");
            tag.setAttribute("tabindex", "0");
            tag.onkeydown = function (ev) {
              if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                tag.onclick();
              }
            };
          }
          if (tip.length) tag.title = tip.join(" · ");
          list.appendChild(tag);
        });

        box.appendChild(title);
        box.appendChild(list);
        (card.querySelector("section") || card).appendChild(box);
      });
    }

    if (wbModelsCache) {
      render(wbModelsCache);
      return;
    }
    fetch("/api/account-models", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        wbModelsCache = data;
        // 后端下发的策略是权威值，覆盖本地乐观副本，保证刷新后状态一致。
        var accs = (data && data.accounts) || {};
        Object.keys(accs).forEach(function (id) {
          if (accs[id]) wbApplySources(id, accs[id].disabledSources, accs[id].disabled);
        });
        render(data);
      })
      .catch(function () {});
  }

  var wbPoolCache = null;

  // 巡检改过禁用策略后，卡片上的模型标签需要整体重画。
  // injectAccountModels() 只处理「还没有标签区」的卡片，所以必须先移除旧的，
  // 否则界面会停留在巡检之前的状态。
  function refreshModelTags() {
    Array.prototype.slice.call(document.querySelectorAll(".wb-am-box"))
      .forEach(function (el) { el.remove(); });
    wbModelsCache = null;
    injectAccountModels();
  }

  function effectiveEnabledIds(data) {
    if (data.allEnabledByDefault) {
      return (data.accounts || []).map(function (a) { return a.id; });
    }
    return (data.enabledAccountIds || []).slice();
  }

  function savePool(payload, done) {
    // 这里必须把**失败**也告知用户。此前的写法是 r.json() 后无条件走 done
    // 回调，于是后端 400（例如 enabledAccountIds 里含已删除账号）时：
    //   · 没有 toast —— 用户完全看不到出错；
    //   · done 照常执行 —— refreshPool() 用旧缓存重绘，页面毫无变化。
    // 合起来就是「点了没任何反应」。现在把 HTTP 状态与业务 detail 都转发出去。
    fetch("/api/account-pool", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, status_code: r.status, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          var d = res.body.detail;
          var msg;
          if (d && typeof d === "object" && d.unknownAccountIds) {
            // 幽灵账号：白名单里指向了已被删除的账号。后端现已自动剔除，
            // 正常不会再走到这里；保留分支以免旧版本网关下静默失真。
            msg = "设置未生效：启用列表里有 " + d.unknownAccountIds.length + " 个已不存在的账号";
          } else if (typeof d === "string" && d) {
            msg = d;
          } else {
            msg = "设置未生效（HTTP " + res.status_code + "）";
          }
          wbToast(msg, "err");
          if (done) done(null);
          return;
        }
        if (res.body && res.body.status) { wbPoolCache = res.body.status; }
        if (done) done(res.body);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(null);
      });
  }

  // 401 时官方积分包区会把上游错误页**原始 HTML** 当文本渲染出来
  // （一长串 <html><head><title>401 Authorization Required…openresty…），
  // 出现在操作按钮上方 —— 哥哥三次反馈要把它换成一句「需要重新登录」。
  // 这块 DIV 是官方 React 渲染的（class 带 text-destructive，错误原文
  // 同时挂在内文和 title 属性上）；我们不猜结构，只按「内容是 401 错误页」
  // 这个特征识别，整块替换为可读提示。
  // 2026-10-20 四校（哥哥完整结构对比）：
  //   1. 替换块不撑满内框（高度只包内容，flex:0 0 auto，见 CSS）
  //   2. 顶部外框（header）官方自己渲染的「需重新登录」badge 隐藏 ——
  //      提示只该出现在内框里，顶部外框只留右侧两个按钮 + 功能收纳
  //   3. 需重登时：控制条（wb-pool-bar）与模型区（wb-am-box）灰化 +
  //      pointer-events:none（禁用态，参照旅行图标的灰色处理方式）
  //   4. 正常账号零变化（全程有 wbCardNeedsRelogin 守卫）
  function wbApplyReloginDisabled(card) {
    var sec = card.querySelector("section");
    if (!sec) return;
    var bar = sec.querySelector(".wb-pool-bar");
    var amBox = sec.querySelector(".wb-am-box");
    [bar, amBox].forEach(function (el) {
      if (!el) return;
      el.classList.add("wb-relogin-disabled");
      el.setAttribute("aria-disabled", "true");
    });
    // 模型区高度对齐正常卡（2026-10-21 工单：「内部区域高度未对齐正常卡片」）。
    // 异常态没有「已禁用 / 巡检」徽标与「全部恢复」按钮，标题行从 24px 塌到
    // 18px，模型区总高随之从 336px 掉到 330px —— 两张卡并排时内框下沿差一截。
    // 修法不写死像素：就地量一张**同页正常卡**的模型区实际高度，搬过来。
    // 这样 PC（约 336px）与移动端各自适配，内容变了也跟着变，不会拍脑袋定值。
    // 量不到（全页都是异常卡）就退回不设，让内容自然决定高度。
    if (amBox && !amBox.dataset.wbAmH) {
      var donor = null;
      var cards = document.querySelectorAll("article[data-wb-acc]");
      for (var i = 0; i < cards.length; i++) {
        var other = cards[i];
        if (other === card) continue;
        if (other.querySelector(".wb-relogin-disabled")) continue;
        var oam = other.querySelector(".wb-am-box");
        if (oam && oam.getBoundingClientRect().height > 0) { donor = oam; break; }
      }
      if (donor) {
        var h = Math.round(donor.getBoundingClientRect().height);
        if (h > 0) {
          amBox.style.minHeight = h + "px";
          amBox.dataset.wbAmH = String(h);
        }
      }
    }
    // 登录提醒区域高度对齐正常卡（使异常就显示在原来显示积分的区域，两边区域一样大）
    var reloginBox = sec.querySelector(".wb-official-relogin");
    if (reloginBox && !reloginBox.dataset.wbReloginH) {
      var donorCredit = null;
      var cards2 = document.querySelectorAll("article[data-wb-acc]");
      for (var j = 0; j < cards2.length; j++) {
        var other2 = cards2[j];
        if (other2 === card) continue;
        if (other2.querySelector(".wb-relogin-disabled") || other2.querySelector(".wb-official-relogin")) continue;
        var osec = other2.querySelector("section");
        if (!osec) continue;
        var pBar = osec.querySelector(".wb-pool-bar");
        var first = osec.firstElementChild;
        if (first && first !== pBar && first.getBoundingClientRect().height > 50) {
          donorCredit = first;
          break;
        }
      }
      if (donorCredit) {
        var ch = Math.round(donorCredit.getBoundingClientRect().height);
        if (ch >= 80) {
          reloginBox.style.minHeight = ch + "px";
          reloginBox.dataset.wbReloginH = String(ch);
        }
      }
    }
  }

  function wbApplyReloginEnabled(card) {
    // 恢复登录后逆向清理（巡检周期会重跑；React 重渲染也会换新节点）
    var sec = card.querySelector("section");
    if (!sec) return;
    sec.querySelectorAll(".wb-relogin-disabled").forEach(function (el) {
      el.classList.remove("wb-relogin-disabled");
      el.removeAttribute("aria-disabled");
    });
    // 一并撤掉灰化时给模型区与登录提醒区设的对齐高度
    var am = sec.querySelector(".wb-am-box");
    if (am && am.dataset.wbAmH) {
      delete am.dataset.wbAmH;
      am.style.minHeight = "";
    }
    var rbox = sec.querySelector(".wb-official-relogin");
    if (rbox && rbox.dataset.wbReloginH) {
      delete rbox.dataset.wbReloginH;
      rbox.style.minHeight = "";
    }
  }

  function wbReplaceReloginErrorBlocks() {
    // 2026-10-23：账号池缓存还没就绪时整轮跳过。
    //
    // 老代码在缓存为空时也会往下走，并对每张卡执行 else 分支的
    // wbApplyReloginEnabled(card) —— 那会**主动解除**可能正确的灰化
    // （比如上一轮刚判过 needsRelogin=true），也会让 header badge
    // 的隐藏状态被抹掉。语义上「没有数据」不该等于「一切正常」。
    //
    // 真正的首次判定已移到 injectAccountPool 的 render() 里
    // （数据到手后立刻跑），所以这里跳过不会漏判。
    if (!wbPoolCache || !wbPoolCache.accounts) return;
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    cards.forEach(function (card) {
      var sec = card.querySelector("section");
      if (!sec) return;
      // 该卡片是否处于需重登状态（横幅已在）；只有这种卡片的 401 文本
      // 才是「凭据失效」—— 正常账号偶尔的错误页文本不该被误伤。
      var relogin = wbCardNeedsRelogin(card);
      if (relogin) {
        // 顶部外框：隐藏官方渲染的「需重新登录」badge（含装饰 span，
        // 只遮不删——React 仍持有节点，删了会被重渲染回来）。
        var header = card.querySelector("header");
        if (header && header.dataset.wbBadgeHidden !== "1") {
          header.dataset.wbBadgeHidden = "1";
          header.classList.add("wb-header-badge-hidden");
        }
        // 按钮区 + 模型区灰化禁用
        wbApplyReloginDisabled(card);
      } else {
        // 正常账号（或恢复登录后）：还原 header badge 与灰化
        var header2 = card.querySelector("header");
        if (header2 && header2.dataset.wbBadgeHidden === "1") {
          header2.dataset.wbBadgeHidden = "";
          header2.classList.remove("wb-header-badge-hidden");
        }
        wbApplyReloginEnabled(card);
      }
      Array.prototype.slice.call(sec.children).forEach(function (div) {
        if (!div.classList) return;
        var txt = (div.textContent || "");
        var title = (div.getAttribute("title") || "");
        var is401Block = /401\s*(Authorization\s*Required)?/i.test(txt) && txt.indexOf("<html>") >= 0
          || /<html>[\s\S]*401[\s\S]*Authorization[\s\S]*Required/i.test(title);
        if (!is401Block) return;
        if (!relogin) return;   // 正常账号不动（官方自己会重试刷新）
        if (div.dataset.wbReloginDone === "1") return;
        div.dataset.wbReloginDone = "1";
        div.textContent = "";
        div.removeAttribute("title");
        div.className = "wb-official-relogin";
        var t1 = document.createElement("div");
        t1.className = "wb-pool-relogin-title";
        t1.textContent = "⛔ 需要重新登录";
        var t2 = document.createElement("div");
        t2.className = "wb-pool-relogin-desc";
        t2.textContent = "这个账号的登录凭据已失效（上游返回 401），积分与用量信息暂时无法读取。重新登录后自动恢复。";
        div.appendChild(t1);
        div.appendChild(t2);
      });
    });
  }

  // 判断一张账号卡片是否处于「需重新登录」状态（2026-10-20 工单）。
  // 数据源是账号池缓存（/api/account-pool 的 needsRelogin 标记）；
  // 卡片没打 data-wb-acc 标记（还没轮到控制条注入）时返回 false，
  // 让首次渲染自然跳过，等控制条注入打了标记后再生效。
  function wbCardNeedsRelogin(card) {
    if (!card) return false;
    var id = card.getAttribute("data-wb-acc");
    // 2026-10-23：缓存未就绪时**不再**直接返回 false 假装正常。
    //
    // 老逻辑 `!wbPoolCache → return false` 有个隐蔽后果：run() 首次同步
    // 调用时缓存必然为空，于是所有卡片都被判为「正常」；等 fetch 回来
    // render() 又不重跑判定（已修），中间态完全没人管。
    //
    // 现在返回 false 的语义收紧为「确实查过、确实不需要重登」——
    // 缓存没来时由调用方（wbReplaceReloginErrorBlocks）跳过本轮，
    // 等数据到了再判，不会把「不知道」当成「正常」。
    if (!id) return false;
    if (!wbPoolCache || !wbPoolCache.accounts) return false;
    var hit = null;
    wbPoolCache.accounts.forEach(function (a) {
      if (a && String(a.id) === String(id)) hit = a;
    });
    return !!(hit && hit.needsRelogin);
  }

  // 构建单张账号卡片的「账号池控制条」。既给 injectAccountPool 首次注入用，
  // 也给 toggle / 设为首选成功后就地重绘同一张卡片用（v1.0.5 交互修复的核心：
  // 此前点一次启用/停用要把全部卡片的控制条拆掉再整体重建 —— 表现为页面抖动、
  // 按钮位置漂移，重建期间按钮还会闪没；现在只换被点的那一根 bar）。
  // bar 对应的卡片由调用方通过 cardEl 传入（就地重绘时用来定位旧 bar）。
  function wbBuildPoolBar(acc, data, cardEl) {
    var bar = document.createElement("div");
    bar.className = "wb-pool-bar";
    // 注意：acc 可能是后端新下发的账号对象（就地重绘路径），
    // 也可能是首次渲染时的旧引用 —— 一律以入参为准，不闭包外层循环变量。

    // 两种「不可用」要分开说，否则用户会以为是自己点错了：
    //   - disabled：被账号级停用策略挡住（可能是人点的，也可能是巡检写的）
    //   - 池外：不在 enabledAccountIds 白名单里
    var accDisabled = !!acc.disabled;
    var inPool = !!acc.enabled;

    // v1.0.5 移动端修复：注入的按钮若落在官方 <form> 里，默认 type=submit，
    // 点击即触发表单提交 → 整页刷新。一律显式 type="button"。
    var toggle = document.createElement("button");
    toggle.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
    if (accDisabled) {
      // 来源不同措辞不同。巡检停用用琥珀色（.wb-pool-btn-auto），
      // 与手动停用的灰底区分开，让人一眼看出「这不是我点的」。
      var autoOff = acc.disabledSource === "auto";
      toggle.className = "wb-pool-btn" + (autoOff ? " wb-pool-btn-auto" : "");
      toggle.textContent = autoOff ? "已停用（巡检）· 点击启用" : "已停用 · 点击启用";
      toggle.title = autoOff
        ? "可用性巡检发现这个账号登录已过期或被上游拦截，自动停用了它。\n恢复后巡检会自己放回来，也可以点这里立即启用。"
        : "你手动停用了这个账号。点击恢复参与调用。";
    } else if (!inPool) {
      toggle.className = "wb-pool-btn";
      toggle.textContent = "不在账号池 · 点击启用";
      toggle.title = "这个账号不在账号池的启用列表里，不参与 API 调用。";
    } else {
      toggle.className = "wb-pool-btn wb-pool-btn-on";
      toggle.textContent = "参与调用 · 点击停用";
      toggle.title = "点击后这个账号不再参与自动轮询（显式指定它的请求仍可用）。";
    }
    toggle.onclick = function () {
      // 防抖：一次点击还没回来前不再受理（双击曾触发两次互反提交）。
      if (toggle.dataset.wbBusy === "1") return;
      toggle.dataset.wbBusy = "1";
      toggle.disabled = true;
      var done = function () {
        toggle.disabled = false;
        delete toggle.dataset.wbBusy;
      };
      // 账号级停用优先：红/灰状态点一下就恢复。
      if (accDisabled) {
        // toggle 响应体自带最新账号池状态 —— 就地重绘，不再全表重建。
        wbToggleAccount(acc.id, false, function (ok, status) {
          done();
          wbReplacePoolBar(acc.id, status, cardEl);
        });
        return;
      }
      // 否则走账号池白名单（旧语义，保留不变）。
      var ids = effectiveEnabledIds(data);
      var idx = ids.indexOf(acc.id);
      if (idx >= 0) { ids.splice(idx, 1); } else { ids.push(acc.id); }
      var next = { enabledAccountIds: ids };
      if (idx >= 0 && data.mode === "manual" && data.manualAccountId === acc.id) {
        next.mode = "auto";
        next.manualAccountId = null;
      }
      savePool(next, function (body) {
        done();
        // savePool 已把响应里的 status 写进 wbPoolCache，就地重绘。
        wbReplacePoolBar(acc.id, (body && body.status) || wbPoolCache, cardEl);
      });
    };

    var pinned = data.mode === "manual" && data.manualAccountId === acc.id;
    var pin = document.createElement("button");
    pin.type = "button"; // v1.0.5: 防 form 内默认 submit 整页刷新
    pin.className = "wb-pool-btn" + (pinned ? " wb-pool-btn-primary" : "");
    pin.textContent = pinned ? "首选账号 · 点击改回自动分配" : "设为首选";
    pin.onclick = function () {
      if (pin.dataset.wbBusy === "1") return;
      pin.dataset.wbBusy = "1";
      pin.disabled = true;
      // 设为首选：必须连 enabledAccountIds 一起提交，且确保目标账号在白名单内。
      // 若为新账号且尚未加入白名单，设为首选时自动将其追加进启用列表，
      // 否则后端安全校验因 manualAccountId not in pool 会退回 auto，导致设置失效。
      var ids = (data.allEnabledByDefault
        ? (data.accounts || []).map(function (a) { return a.id; })
        : (data.enabledAccountIds || [])).slice();
      if (!pinned && acc.id && ids.indexOf(acc.id) === -1) {
        ids.push(acc.id);
      }
      savePool(
        pinned
          ? { mode: "auto", manualAccountId: null, enabledAccountIds: ids }
          : { mode: "manual", manualAccountId: acc.id, enabledAccountIds: ids },
        function (body) {
          pin.disabled = false;
          delete pin.dataset.wbBusy;
          wbReplacePoolBar(acc.id, (body && body.status) || wbPoolCache, cardEl);
        }
      );
    };

    // 手动检测：发一次轻量鉴权请求，就地报告这个账号现在能不能用。
    // 只报告、不改配置 —— 要停用由用户看着结果自己决定（旁边就是停用按钮）。
    var probe = document.createElement("button");
    probe.type = "button";
    probe.className = "wb-pool-btn";
    probe.textContent = "检测账号";
    probe.title = "发一次轻量鉴权请求验证凭据是否有效，不消耗额度";
    probe.onclick = function () {
          if (probe.dataset.wbBusy === "1") return;
          probe.dataset.wbBusy = "1";
          probe.disabled = true;
          probe.textContent = "检测中…";
          fetch("/api/account-health/probe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ accountId: acc.id })
          })
            .then(function (r) { return r.json(); })
            .then(function (res) {
              if (!res || res.error) {
                wbToast("检测失败：" + ((res && res.error) || "无法访问接口"), "error");
                return;
              }
              // 结论一律以 state 为准（valid / invalid / restricted / unknown）。
              // 不再自行解释 verdict 或状态码 —— 那正是过去同一个账号
              // 在这里显示有效、在巡检里显示失效的来源。
              //
              // 配色也要跟着状态走，而不是「非 valid 就一律红」：
              // 受限账号（restricted）的凭据其实是好的，用红色会让用户以为
              // 凭据坏了、跑去重新登录 —— 而重登对这种情况毫无帮助。
              // 三档语义：valid=绿、restricted/unknown=琥珀、invalid=红。
              var state = res.state || "";
              var tone = state === "valid" ? "ok"
                       : (state === "invalid" ? "error" : "warn");
              // 提示只讲一件事：**账号现在能不能用**，外加一句该怎么办。
              //
              // 全文案来自后端下发的 userMessage（一句人话），**不再拼
              // evidence / action**。它们说的是同一件事的另外两个说法：
              //
              //   userMessage  账号被上游拦截，暂时用不了
              //   evidence     账号已被上游拦截              ← 同义重复
              //   action       已并入上面的结论，不再单独拼
              //
              // 三句拼起来是一段要读三遍的话，而用户只需要知道
              //「能不能用、要不要动手」。完整成因仍在接口里，排查时看得到。
              // 「重新扫码也没用」这类需要展开的说明放设置页，不进提示条。
              // 正常档连建议都没有：多一句话反而让人以为还有别的事要处理。
              var name = res.accountName || "账号";
              var msg = name + "：" + (res.userMessage || res.message || res.verdict);
              wbToast(msg, tone);
            })
            .catch(function (e) { wbToast("检测请求失败：" + e, "error"); })
            .then(function () {
              probe.disabled = false;
              probe.textContent = "检测账号";
              probe.dataset.wbBusy = "0";
            });
        };

    var count = document.createElement("span");
    var n = (window.__wbPoolCounts && window.__wbPoolCounts[acc.id]) || 0;
    count.className = "wb-pool-count" + (n > 0 ? " wb-pool-count-hot" : "");
    // 计数标签**始终渲染**（即使是 0）——「没有调用过」本身就是有效信息，
    // 没这个标签用户无法区分「从没调用」与「这功能不存在」。
    count.textContent = "已调用 " + n + " 次";

    bar.appendChild(toggle);
    bar.appendChild(pin);
    bar.appendChild(probe);
    bar.appendChild(count);

    if (data.lastSelectedAccountId && data.lastSelectedAccountId === acc.id) {
      var last = document.createElement("span");
      last.className = "wb-pool-last";
      last.textContent = "最近调用";
      bar.appendChild(last);
    }

    return bar;
  }

  // 就地重绘一张卡片的控制条：用最新账号池状态只换被点的那根 bar。
  // 这是 v1.0.5 修「抖动 / 按钮跑位」的关键 —— 旧实现 refreshPool() 拆全部
  // bar 再整体重建，触发的批量 DOM 变更还会惊动 MutationObserver 引发
  // -------------------------------------------------------------------------
  // 账号卡片「风控标记」徽章（2026-10-21）
  //
  // 需求（哥哥原话）：「每个账号卡片右上角两个图标前方出现风控标记，
  // 并推送通知」。
  //
  // DOM 定位（实测）：卡片 article > header > div.relative.z-10.flex...pr-10
  //   （标题行），该行子节点 [0] = h3 昵称，[1] = div...gap-1.min-[420px]:flex
  //   —— 后者就是「右上角两个图标」的容器，里面是旅行徽章与签到徽章两个 span。
  // 「前方」= 插到这个容器的最前面（insertBefore 到 children[0] 之前）。
  //
  // ⚠️ 与「已停用」必须一眼可辨：见 CSS 里 .wb-risk-badge 的注释。
  // 徽章只表达「上游判过它被拦」，不表达任何本地处置状态。
  // -------------------------------------------------------------------------
  var WB_RISK_SHIELD_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5l-8-3Z"></path><path d="M12 8v4"></path><path d="M12 16h.01"></path></svg>';

  // 徽章 hover/点击的说明文案。措辞与后端 SEMANTIC_LABELS["restricted"]
  // 保持同一口径：说「被上游拦截」，不说「风控」——那个词会把人引向
  // 内容或频率问题，实测证明两者都不是（换凭据无效、发「hi」也被拦）。
  function wbRiskReasonText(code) {
    var c = parseInt(code, 10);
    if (c === 11140) return "上游返回 11140：账号存在异常行为，请求被拦截";
    if (c === 14018) return "上游返回 14018：账号额度已用尽";
    return "上游返回 " + (isNaN(c) ? "未知" : code) + "：账号被拦截";
  }

  function wbRiskTimeText(ms) {
    var t = parseInt(ms, 10);
    if (!t) return "未知";
    var d = new Date(t);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
      + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  // 关掉已打开的浮层。点别处 / 点同一枚徽章都要收。
  function wbRiskClosePops() {
    Array.prototype.slice.call(document.querySelectorAll(".wb-risk-pop")).forEach(function (el) {
      el.remove();
    });
  }

  // 解除风控标记（人工）。成功后同步本地缓存与徽章，不整页重建 ——
  // 与账号池控制条的就地重绘同一个路子，避免页面抖动。
  // 请求写法与 wbToggleAccount 保持一致（裸 fetch + wbToast）。
  function wbUnflagAccount(accountId, done) {
    if (!accountId) { if (done) done(false); return; }
    fetch("/api/account-pool/risk-flag/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: String(accountId) })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          wbToast("解除标记失败，请重试", true);
          if (done) done(false);
          return;
        }
        wbToast("已解除风控标记");
        // 缓存里同步摘掉，否则下次 refreshPool 又把它画回来
        if (wbPoolCache && wbPoolCache.accountRiskFlags) {
          delete wbPoolCache.accountRiskFlags[String(accountId)];
        }
        if (wbPoolCache && wbPoolCache.accounts) {
          wbPoolCache.accounts.forEach(function (a) {
            if (a && String(a.id) === String(accountId)) { a.riskFlagged = false; }
          });
        }
        wbInjectRiskBadges();
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", true);
        if (done) done(false);
      });
  }

  function wbRiskOpenPop(badge, flag) {
    wbRiskClosePops();
    var pop = document.createElement("div");
    pop.className = "wb-risk-pop";
    pop.setAttribute("data-wb-risk-pop", String(flag.accountId || ""));

    var title = document.createElement("div");
    title.className = "wb-risk-pop-title";
    title.innerHTML = WB_RISK_SHIELD_SVG + "<span>账号被上游拦截</span>";
    pop.appendChild(title);

    function row(k, v) {
      var r = document.createElement("div");
      r.className = "wb-risk-pop-row";
      var ke = document.createElement("span");
      ke.className = "wb-risk-pop-k";
      ke.textContent = k;
      var ve = document.createElement("span");
      ve.className = "wb-risk-pop-v";
      ve.textContent = v;
      r.appendChild(ke);
      r.appendChild(ve);
      pop.appendChild(r);
    }
    row("原因", wbRiskReasonText(flag.lastCode));
    row("最近触发", wbRiskTimeText(flag.lastAt));
    row("历史次数", String(parseInt(flag.count, 10) || 0) + " 次");

    var note = document.createElement("div");
    note.className = "wb-risk-pop-note";
    // 这句是本次改动的核心信息，必须写进浮层：标记不是停用，
    // 网关没有因此停用它，也不允许任何人据此加自动停用。
    note.textContent = "这只是上游的风控记录，不是本地停用 —— 账号仍在正常参与调用，"
      + "系统不会自动停用它。是否停用由你决定。";
    pop.appendChild(note);

    // 解除标记按钮。**只人工可点** —— 后端同样没有任何自动清除逻辑，
    // 上游没说这个账号好了，我们就不该擅自说好了。
    var unflag = document.createElement("button");
    unflag.type = "button";   // v1.0.5 教训：官方卡片在 <form> 里，不写 type 会整页刷新
    unflag.className = "wb-risk-pop-btn";
    unflag.textContent = "解除这个标记";
    unflag.title = "确认这个账号已恢复正常，从界面上撤掉风控标记。账号本身不受影响。";
    unflag.onclick = function (ev) {
      ev.stopPropagation();
      if (unflag.dataset.wbBusy === "1") return;
      unflag.dataset.wbBusy = "1";
      unflag.disabled = true;
      wbUnflagAccount(flag.accountId, function (ok) {
        delete unflag.dataset.wbBusy;
        if (ok) { wbRiskClosePops(); return; }
        unflag.disabled = false;
        unflag.textContent = "解除失败，重试";
      });
    };
    pop.appendChild(unflag);

    // 徽章自身 position: relative，让浮层以它为定位基准
    badge.style.position = "relative";
    badge.appendChild(pop);
  }

  // 给单张卡片注入/更新风控徽章。acc 是账号池数据里的账号对象。
  function wbInjectRiskBadge(card, acc) {
    if (!card || !acc) return;
    // 标记明细在顶层 accountRiskFlags 里（按账号 id 索引），
    // 每条账号只带一个 riskFlagged 布尔。别把两份搞混：
    // 布尔用于快速判断，明细（次数/时间/错误码）只在点开浮层时用。
    var flag = null;
    if (wbPoolCache && wbPoolCache.accountRiskFlags) {
      flag = wbPoolCache.accountRiskFlags[String(acc.id)] || null;
    }
    var existing = card.querySelector(".wb-risk-badge");
    if (!flag) {
      // 没有标记：清掉可能残留的徽章（标记被解除后要立刻消失）
      if (existing) { existing.remove(); }
      return;
    }
    // 标题行：h3 的父节点。拿不到就说明卡片还没渲染完，跳过等下一轮。
    var h3 = card.querySelector("h3");
    if (!h3 || !h3.parentElement) return;
    var titleRow = h3.parentElement;
    var iconBox = titleRow.children[1];
    // 官方结构里 [1] 就是两个图标的容器；万一它不在（改版），
    // 兜底按 class 特征找，再找不到就放弃 —— 不往 h3 里塞，那会毁掉标题。
    if (!iconBox || iconBox.tagName !== "DIV") {
      iconBox = titleRow.querySelector('div[class*="gap-1"]');
    }
    if (!iconBox) return;

    var badge = existing;
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "wb-risk-badge";
      badge.innerHTML = WB_RISK_SHIELD_SVG;
      badge.setAttribute("role", "button");
      badge.setAttribute("tabindex", "0");
      badge.addEventListener("click", function (ev) {
        ev.stopPropagation();
        // 再点同一枚 = 收起（已打开的浮层就挂在它身上）
        if (badge.querySelector(".wb-risk-pop")) { wbRiskClosePops(); return; }
        wbRiskOpenPop(badge, flag);
      });
      // 键盘可达性：Enter / 空格同样打开，与点击等价
      badge.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        ev.preventDefault();
        ev.stopPropagation();
        if (badge.querySelector(".wb-risk-pop")) { wbRiskClosePops(); return; }
        wbRiskOpenPop(badge, flag);
      });
    }
    // 内容可能变（次数涨了、时间更新了），每次重刷 title 与 dataset
    badge.title = "账号被上游风控标记（" + (parseInt(flag.count, 10) || 0) + " 次）—— 点击查看详情。"
      + "注意：标记不等于停用，系统不会自动停用它。";
    badge.setAttribute("data-wb-risk-code", String(flag.lastCode || ""));
    badge.setAttribute("data-wb-risk-count", String(parseInt(flag.count, 10) || 0));
    badge.setAttribute("aria-label", badge.title);

    // 「两个图标前方」：插到图标容器的最前面
    if (iconBox.children[0] !== badge) {
      iconBox.insertBefore(badge, iconBox.children[0] || null);
    }
  }

  // 全量刷新：遍历所有已打标记的卡片。账号池数据刷新后调用。
  function wbInjectRiskBadges() {
    if (!wbPoolCache || !wbPoolCache.accounts) return;
    Array.prototype.slice.call(document.querySelectorAll("article[data-wb-acc]")).forEach(function (card) {
      var id = card.getAttribute("data-wb-acc");
      var acc = null;
      wbPoolCache.accounts.forEach(function (a) {
        if (a && String(a.id) === String(id)) acc = a;
      });
      if (acc) { wbInjectRiskBadge(card, acc); }
    });
  }

  // 点空白处收起浮层（document 级，只绑一次）
  if (!window.__wbRiskDocBound) {
    window.__wbRiskDocBound = true;
    document.addEventListener("click", function (ev) {
      var t = ev.target;
      // 点在徽章或其浮层内部 → 交给徽章自己的 handler，不插手
      if (t && t.closest && t.closest(".wb-risk-badge")) return;
      wbRiskClosePops();
    });
    // ESC 收起（浮层是模态-ish 的，键盘用户要有退出方式）
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { wbRiskClosePops(); }
    });
  }

  // 全页 run()，用户看到的就是页面抖一下、按钮位置乱掉。
  // cardEl 可选；不给时按 accountId 找（卡片上有 data-wb-acc 标记）。
  function wbReplacePoolBar(accountId, status, cardEl) {
    var data = status || wbPoolCache;
    if (!data || !data.accounts) { refreshPool(); return; }
    var acc = null;
    data.accounts.forEach(function (a) {
      if (a && String(a.id) === String(accountId)) acc = a;
    });
    if (!acc) { refreshPool(); return; }
    if (!cardEl) {
      cardEl = document.querySelector('article[data-wb-acc="' + String(accountId) + '"]');
    }
    if (!cardEl) { refreshPool(); return; }
    var sec = cardEl.querySelector("section") || cardEl;
    var old = sec.querySelector(".wb-pool-bar");
    var fresh = wbBuildPoolBar(acc, data, cardEl);
    if (old) {
      // insertBefore 再 remove：同一帧内完成替换，不留空窗。
      sec.insertBefore(fresh, old);
      old.remove();
    } else {
      sec.appendChild(fresh);
    }
    wbPinAccountBlocks();
  }

  function injectAccountPool() {
    // 官方那三个「设为当前账号 / 切换到 IDE / CLI」按钮依赖宿主桌面程序，
    // 已在二进制里物理移除。这里注入容器自己的账号池控制：
    //   [启用/停用] 决定该账号是否参与 API 调用；[设为首选] 固定只用这一个账号。
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    if (!cards.length) {
      // 2026-10-23：非账号管理页（积分统计 / 词元统计 / 设置…）走到这里。
      // 顺手把上一页遗留的账号池缓存清掉 —— 它是闭包变量，SPA 切页不重建，
      // 旧数据会让 wbCardNeedsRelogin / 排序 / 风控徽章在错误的页面上
      // 拿旧账号状态做判断（灰卡片工单的另一半成因）。
      wbPoolCache = null;
      wbRemoveAccountSearch();
      return;
    }

    var pending = cards.filter(function (c) {
      return !c.querySelector(".wb-pool-bar") && c.querySelector("h3");
    });
    if (!pending.length) return;

    function render(data) {
      if (!data || !data.accounts) return;
      // 多重索引：卡片标题是账号昵称，而新账号常常没有昵称，
      // 只能回退成账号 ID（如 a75e21b0-3ec4-...）。
      // 只按昵称建索引的话，这些卡片在账号池控制条上完全空 —— 启用/停用、
      // 设为首选、检测账号、调用次数四个控件都不出现，看上去像「功能缺失」。
      // 所以 name / id / 昵称与邮箱都要注。
      var byName = {};
      data.accounts.forEach(function (a) {
        if (!a) return;
        [a.name, a.id, a.nickname, a.email].forEach(function (key) {
          if (key) byName[String(key)] = a;
        });
      });

      // 各账号被分摊到的请求次数（网关进程启动后累计）。
      // 挂到 window 上，让 wbBuildPoolBar（含就地重绘路径）也能拿到。
      var counts = {};
      (data.selectionCounts || []).forEach(function (c) { counts[c.accountId] = c.count; });
      window.__wbPoolCounts = counts;

      var pendingIds = data.accounts.map(function (a) { return a && a.id; }).filter(Boolean);

      pending.forEach(function (card, cardIndex) {
        if (card.querySelector(".wb-pool-bar")) return;
        var h3 = card.querySelector("h3");
        if (!h3) return;
        var cardTitle = (h3.textContent || "").trim();
        var acc = byName[cardTitle];
        // 标题对不上时（账号无昵称、昵称被格式化过）按位置兜底。
        if (!acc && cardIndex < pendingIds.length) {
          acc = byName[pendingIds[cardIndex]];
        }
        // 后端已保证账号池里每个账号都带完整字段（id/enabled/disabled/…），
        // 这里拿不到就是**数据真的缺了**，不该用占位数据把它遮住。
        if (!acc) {
          console.warn("[AutoBuddy] 账号卡片在后端账号池数据里找不到条目:", cardTitle);
          return;
        }

        // 卡片标记自己的 accountId：就地重绘时按 id 直接找回，不再靠标题反查。
        if (acc.id) { card.setAttribute("data-wb-acc", String(acc.id)); }

        // 风控标记徽章（2026-10-21）：插在卡片右上角两个原有图标的前方。
        // 放在控制条注入之前，保证徽章先就位，不被后面的 appendChild 挤走。
        wbInjectRiskBadge(card, acc);

        var bar = wbBuildPoolBar(acc, data, card);
        (card.querySelector("section") || card).appendChild(bar);
      });
      wbSortAccountCards();
      // 2026-10-23：账号池数据**到达后**必须重跑一次需重登判定。
      //
      // 此前 run() 里 wbReplaceReloginErrorBlocks() 是同步跑的，那一刻
      // wbPoolCache 可能还是上一次页面遗留的旧数据（SPA 切页不重建 JS
      // 上下文，这个闭包变量跨页面存活）—— 旧缓存里若某账号
      // needsRelogin=true，卡片当场被灰化；而 fetch 回来后 render()
      // 只重建控制条，**不会**再跑一次判定，于是卡片永久停在灰态，
      // 用户看到的就是「账号卡片完全灰色无法操作」（2026-10-23 工单，
      // 其他设备报告）。线上实况佐证：refresh_state.json 不存在、
      // accounts.json 里 0 个账号带 needs_relogin —— 没有任何账号
      // 真需要重登，灰化纯属前端时序误判。
      wbReplaceReloginErrorBlocks();
      wbInjectAccountSearch();
    }

    if (wbPoolCache) { render(wbPoolCache); return; }
    fetch("/api/account-pool", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) { wbPoolCache = data; render(data); })
      .catch(function () {});
  }

  function wbRefreshAccountToken(accountId, done) {
    fetch("/api/refresh-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId, exportSecrets: false })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail || res.body.error;
          wbToast(typeof d === "string" && d ? "刷新失败：" + d : "刷新失败，请重试", "err");
          if (done) done(false);
          return;
        }
        wbToast("Token 已刷新");
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(false);
      });
  }

  function wbDeleteAccount(accountId, label, done) {
    // 删除是不可逆的，必须先确认；把账号名写进提示里，
    // 避免在长列表里点错行却按习惯直接确认。
    if (!window.confirm("确定删除账号「" + (label || accountId) + "」？\n\n"
        + "删除后该账号的登录凭据会被移除，需重新登录才能恢复；"
        + "它的模型禁用策略与停用记录会一并清理。")) {
      return;
    }
    fetch("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} }; 
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail || res.body.error;
          wbToast(typeof d === "string" && d ? "删除失败：" + d : "删除失败，请重试", "err");
          if (done) done(false);
          return;
        }
        wbToast("已删除账号");
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(false);
      });
  }

  function wbManualCheckin(accountId, done) {
    // 手动签到走每日任务服务（它本来就在做签到，不另开一条路径）。
    fetch("/api/wb-daily/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId || undefined })
    })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        if (!d || !d.job_id) {
          wbToast("签到启动失败：" + ((d && (d.detail || d.error)) || "未知原因"), "err");
          if (done) done(false);
          return;
        }
        wbToast("已开始签到，可在「每日任务」页看进度");
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(false);
      });
  }

  function refreshPool() {
    Array.prototype.slice.call(document.querySelectorAll(".wb-pool-bar")).forEach(function (el) {
      el.remove();
    });
    injectAccountPool();
    // 风控徽章跟着账号池数据一起刷：控制条重建后卡片上可能还有旧徽章，
    // 这里按最新数据补/更新/清除。
    wbInjectRiskBadges();
  }

  function wbPinAccountBlocks() {
    // 账号卡片里我们注入的两块（账号池控制条 / 可用模型）必须**固定在卡片末尾**，
    // 且顺序恒为 [控制条] → [模型区]。有两处竞态会让顺序漂移：
    //   1) 官方的「近期到期 / 积分明细」是数据到齐后才由 React 追加的。React 判定
    //      它是末节点时走的是 appendChild，于是它落到我们两块**之后** —— 同一页里
    //      就出现「积分明细一会儿在上一会儿在下」；
    //   2) 我们自己的两个 fetch（/api/account-pool 与 /api/account-models）谁先回来
    //      谁先 append，控制条与模型区也会互换位置。
    // 每次渲染后按固定顺序把我们两块挪回末尾即可。**已经是目标顺序时不动作**，
    // 否则 appendChild 会触发新的 DOM 变更，被 MutationObserver 接住后无限循环。
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    cards.forEach(function (card) {
      var sec = card.querySelector("section") || card;
      var pool = null;
      var box = null;
      Array.prototype.slice.call(sec.children).forEach(function (child) {
        if (!child.classList) return;
        if (child.classList.contains("wb-pool-bar")) pool = child;
        else if (child.classList.contains("wb-am-box")) box = child;
      });
      var tail = [];
      if (pool) tail.push(pool);
      if (box) tail.push(box);
      if (!tail.length) return;          // 官方自己的卡片，不碰

      var kids = Array.prototype.slice.call(sec.children);
      var n = tail.length;
      var pinned = n <= kids.length && tail.every(function (el, i) {
        return kids[kids.length - n + i] === el;
      });
      if (pinned) return;
      tail.forEach(function (el) { sec.appendChild(el); });
    });
  }

  // 需求 9：优化账号页面卡片排序（三原则排序法则）
  // 第一原则：正常卡片排在最前，非白名单靠后，最后是异常卡片
  // 第二原则：三种分类内部再按照积分多少排序，积分多的靠前，积分少的靠后
  // 第三原则：没有积分的卡片不用管理排序顺序
  function wbSortAccountCards() {
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    if (cards.length < 2) return;
    var parent = cards[0].parentElement;
    if (!parent) return;

    // 建立账号数据映射（id -> acc 对象）
    var accMap = {};
    if (wbPoolCache && Array.isArray(wbPoolCache.accounts)) {
      wbPoolCache.accounts.forEach(function (a) {
        if (!a) return;
        if (a.id) accMap[String(a.id)] = a;
        if (a.name) accMap[String(a.name)] = a;
        if (a.nickname) accMap[String(a.nickname)] = a;
      });
    }

    function getCardRankAndScore(card) {
      var id = card.getAttribute("data-wb-acc");
      var h3 = card.querySelector("h3");
      var title = (h3 ? h3.textContent : "").trim();
      var acc = (id && accMap[String(id)]) || (title && accMap[title]) || null;

      // 异常状态判断（401失效、停用、被上游拦截）：
      // 1. 账号对象上有 disabled 或 needsRelogin 标记；
      // 2. 卡片上有 wb-relogin-disabled 样式；
      // 3. wbCardNeedsRelogin(card) 为真；
      // 4. 卡片内含有 .wb-official-relogin 提示块。
      var isAbnormal = false;
      if (acc) {
        if (acc.disabled || acc.needsRelogin) isAbnormal = true;
      }
      if (wbCardNeedsRelogin(card)) isAbnormal = true;
      if (card.querySelector(".wb-official-relogin") || card.querySelector(".wb-relogin-disabled")) {
        isAbnormal = true;
      }

      // 白名单判断（在账号池中）：
      var inPool = false;
      if (acc) {
        inPool = !!acc.enabled;
      } else {
        var btn = card.querySelector(".wb-pool-bar > .wb-pool-btn:first-child");
        if (btn) {
          var btnTxt = (btn.textContent || "").trim();
          if (btnTxt.indexOf("参与调用") >= 0) inPool = true;
          if (btnTxt.indexOf("已停用") >= 0) isAbnormal = true;
        }
      }

      // 第一原则三层等级：0=正常优先，1=非白名单靠后，2=异常卡片垫底
      var rank = 1;
      if (isAbnormal) {
        rank = 2;
      } else if (inPool) {
        rank = 0;
      } else {
        rank = 1;
      }

      // 第二原则 & 第三原则：积分获取与排序判定
      var hasCredits = false;
      var credits = 0;
      if (acc && typeof acc.credits === "number" && !isNaN(acc.credits)) {
        credits = acc.credits;
        hasCredits = true;
      } else if (acc && typeof acc.balance === "number" && !isNaN(acc.balance)) {
        credits = acc.balance;
        hasCredits = true;
      } else {
        // 从 DOM 中提取数字兜底
        var txt = card.textContent || "";
        var m = txt.match(/(\d+(?:\.\d+)?)\s*(?:积分|credits?)/i);
        if (m) {
          credits = parseFloat(m[1]) || 0;
          hasCredits = true;
        }
      }

      return { rank: rank, hasCredits: hasCredits, credits: credits, title: title, id: id || "" };
    }

    var cardMetas = cards.map(function (c) {
      return { card: c, meta: getCardRankAndScore(c) };
    });

    cardMetas.sort(function (a, b) {
      // 第一原则：正常卡片 (0) 优先，非白名单 (1) 靠后，最后是异常卡片 (2)
      if (a.meta.rank !== b.meta.rank) {
        return a.meta.rank - b.meta.rank;
      }
      // 第二原则 & 第三原则：同分类内部，有积分的按积分降序排序；没有积分的不用参与排序（排在有积分后面或保持稳定）
      if (a.meta.hasCredits && b.meta.hasCredits) {
        if (b.meta.credits !== a.meta.credits) {
          return b.meta.credits - a.meta.credits;
        }
      } else if (a.meta.hasCredits && !b.meta.hasCredits) {
        return -1;
      } else if (!a.meta.hasCredits && b.meta.hasCredits) {
        return 1;
      }
      // 稳定排序兜底：标题或 ID
      return (a.meta.title || a.meta.id).localeCompare(b.meta.title || b.meta.id);
    });

    // 检查是否已是目标顺序，避免无意义的 DOM 重排
    var isSame = true;
    for (var i = 0; i < cards.length; i++) {
      if (cards[i] !== cardMetas[i].card) {
        isSame = false;
        break;
      }
    }
    if (isSame) return;

    cardMetas.forEach(function (item) {
      parent.appendChild(item.card);
    });
  }

  // 需求 3：账号管理页面搜索框注入与即时过滤
  // 2026-10-21 位置微调（用户要求）：放到「国内版 / 国际版」档位切换的
  // **后方、同一行、右端对齐**，尺寸与切换按钮（h-7 / rounded-lg / text-xs）对齐。
  // 2026-10-23 两处修正：
  //   (1) PC 端右端对齐：wrap 的 margin-left 由 8px 改 auto（见 CSS 注释）；
  //   (2) 页面守卫：只在**账号管理页**注入。此前只靠「页面上有 article」
  //       判定，而积分统计页 / 词元统计页同样渲染 article 卡片 ——
  //       用户实测「积分统计页面也出现搜索栏，我说了只需要在账号统计页面
  //       出现即可」。
  //       判据用 hash + path 双条件（与上方 wbResolveView 的路由规则一致）：
  //         · hash === "#/accounts"        → 账号管理（明确后缀路由）
  //         · path === "/" 且无 hash       → 官方根页 = 账号管理的背后页面
  //       其余一律不注入，安静退出（与其它注入函数「坏了也别炸」同风格）。
  // 2026-10-25（v1.0.35）锚点重构：从「寄生官方档位容器（内联改写它的
  // flex-col → flex-row）」改为「页头行之后插入独立过滤行」—— 官方 DOM
  // 零改写、零残留，移动端顺带吸顶 + 防 iOS 聚焦缩放（见 CSS 注释与
  // wbInjectAccountSearch 内注释）。
  // 官方 DOM 实测：
  //   div.flex.items-start.justify-between.gap-4      ← 页头行
  //     └ div.min-w-0
  //         └ div.flex.flex-col.mt-4.gap-0           ← 档位容器（tab 的外层）
  //             └ div[role=tablist][aria-label="WorkBuddy 档位"]
  //                 .inline-flex.h-9.w-fit.items-center.rounded-lg.bg-muted.p-1
  // 定位走「文本 + 结构」双判据，不依赖易变的 class 名：
  //   1. 先找 aria-label 含「档位」的 tablist（最稳，官方自己标了语义）；
  //   2. 找不到再退回到「含 国内版/国际版 两个按钮」的容器。
  /* 2026-10-23 CDP 实测修正：只在**账号管理页**注入。

     原实现的错误（用户实测「积分统计页面也出现搜索栏」的同类回归）：
        if ((path === "/" || path === "") && (hash === "" || hash === "#/" || hash === "#")) return true;
     把「根路径 + 无 hash」当成账号管理页。但官方路由规则（见本文件
     wbResolveView）写得很清楚：
       · path === "/" 且 hash === "" → **数据总览**（mode = "overview"）
       · hash === "#/accounts"        → 账号管理（mode = "accounts"）
     而账号管理的背后页面正是官方 "/"，SPA 切到总览时它**仍挂在 DOM 里**
     （只是被总览浮层盖住），档位 tablist 也一并存在 —— 于是那条判定
     让搜索栏被插到总览页上。CDP 实测：访问 http://…:18090/（无 hash）
     #wb-account-search-bar count=1、输入框 visible=false（被浮层盖住）；
     而 #/accounts 下 count=1、visible=true。

     修正：**只认 #/accounts 这一条判据**。根路径无 hash 一律不注入，
     积分统计 / 词元统计 / 数据总览天然全部排除。 */
  function wbIsAccountsPage() {
    // 与 wbPath() 同样的非浏览器兜底：Node 环境里没有 location。
    var hash = "";
    try {
      hash = (typeof location !== "undefined" && location.hash) || "";
    } catch (e) {}
    return hash === "#/accounts";
  }

  function wbFindVariantSwitchGroup() {
    var groups = document.querySelectorAll('[role="tablist"], [role="radiogroup"]');
    for (var i = 0; i < groups.length; i++) {
      var al = (groups[i].getAttribute("aria-label") || "");
      if (al.indexOf("档位") >= 0) return groups[i];
    }
    var btns = document.querySelectorAll("button");
    for (var j = 0; j < btns.length; j++) {
      var t = (btns[j].textContent || "").trim();
      if (t === "国内版" || t === "国际版") return btns[j].parentElement;
    }
    return null;
  }

  /* 撤掉账号管理页搜索栏（2026-10-23 新增）。
     两个调用场景：
       1. SPA 切到非账号页时（积分统计 / 词元统计 / 设置…）—— 只靠
          「不注入」不够，document 不重建，上次插的节点会一直留着；
       2. 档位容器结构找不到时（官方改版）—— 同样要收干净，不留孤儿节点。
     顺带把被搜索过滤隐藏的卡片全部放出来（否则带着 display:none 切走
     再切回来，账号会「少了一批」—— 过滤状态比 DOM 活得久）。

     v1.0.35：搜索框不再寄生官方档位容器（旧实现要把官方 flex-col 内联改成
     flex-row，还得在这里清官方节点的 dataset 标记与内联样式，官方一改版
     最先坏）。现在整条过滤行都是自己的节点（wb-account-search-row），
     撤销时把自己的两个节点删掉即可，官方 DOM 零残留。 */
  function wbRemoveAccountSearch() {
    // v1.0.36：自己的节点一共四个（PC 内联框 / 移动端图标 / 弹窗 / 兜底独立行），
    // 全部清掉；官方 DOM 从不改写，所以不存在「清理官方节点」这回事。
    ["wb-account-search-bar", "wb-account-search-icon",
     "wb-account-search-modal", "wb-account-search-row"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
    // 复位被过滤隐藏的账号卡片
    try {
      var cards = document.querySelectorAll("article");
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].style.display === "none") cards[i].style.display = "";
      }
    } catch (e) {}
    // 还原 v1.0.38 归一化改动：分隔线原始 margin、刷新按钮的补背景类
    //（「账号」行还在 DOM 里时才有得还原；切页后整行已卸载则自然无需）。
    try {
      var acts = wbFindAccountRowActions();
      if (acts) {
        var dividers = acts.querySelectorAll("div.bg-border[data-wb-orig-ml]");
        for (var j = 0; j < dividers.length; j++) {
          var dv = dividers[j];
          dv.style.marginLeft = dv.dataset.wbOrigMl || "";
          dv.style.marginRight = dv.dataset.wbOrigMr || "";
          delete dv.dataset.wbOrigMl;
          delete dv.dataset.wbOrigMr;
        }
        var rSvg = acts.querySelector("svg.lucide-refresh-cw");
        var rBtn = rSvg ? rSvg.closest("button") : null;
        if (rBtn) rBtn.classList.remove("wb-acc-btn-flat");
      }
    } catch (e3) {}
  }

  /* 定位「账号」行的右侧图标按钮容器（v1.0.36 主锚点）。
     官方结构实测（生产 DOM）：
       section.mt-7                                                  ← 账号卡片区
         └ div.mb-4.flex.flex-wrap.items-center.justify-between.gap-3 ← 「账号」行
             ├ div.flex.items-center.gap-2    ← 左：H2「账号」+ 计数徽章
             └ div.ml-auto.flex.items-center.gap-* ← 右：自动签到/自动执行/列表/刷新
                                                      四个 36×36 图标按钮
     搜索框 / 搜索图标插进右侧容器**首位** = 标题之后、图标按钮之前，
     与图标的间距天然等于容器自身的 gap（用户要求「与两个图标间距相同」）。
     定位走「H2 文本 === 账号」判据，不依赖易变的 class 名。 */
  function wbFindAccountRowActions() {
    try {
      var heads = document.querySelectorAll("h2");
      for (var i = 0; i < heads.length; i++) {
        if ((heads[i].textContent || "").trim() !== "账号") continue;
        var row = heads[i].parentElement;              // 左组 flex items-center gap-2
        var rowWrap = row ? row.parentElement : null;  // 「账号」行
        if (!rowWrap) return null;
        for (var j = 0; j < rowWrap.children.length; j++) {
          var c = rowWrap.children[j];
          if (c !== row && (c.className || "").indexOf("ml-auto") >= 0) return c;
        }
        return rowWrap.children.length > 1 ? rowWrap.lastElementChild : null;
      }
    } catch (e) {}
    return null;
  }

  /* 「账号」行右侧控件归一化（v1.0.38，幂等，每次 run() 重放）：
     ① 分隔线（div.bg-border）水平 margin 归零 —— 消除 mx-2 撑出的 25px
        大间隙，全行统一为容器 gap-1 的 4px（PC 4 控件 / 移动端 5 控件一致）；
     ② 刷新按钮（svg.lucide-refresh-cw）补同款背景 —— 官方此处是透明
        ghost 形态，与旁边三个 bg-accent 按钮不一致，用户要求统一。
     React 重渲染会冲掉内联样式与追加的 class，所以不能只在注入时做一次，
     必须每次 run() 重放；dataset.wbOrigMl/Mr 记录原始内联值供撤销还原。 */
  function wbNormalizeAccountRowActions() {
    var actions = wbFindAccountRowActions();
    if (!actions) return;
    try {
      var dividers = actions.querySelectorAll("div.bg-border");
      for (var i = 0; i < dividers.length; i++) {
        var d = dividers[i];
        if (d.dataset.wbOrigMl === undefined) d.dataset.wbOrigMl = d.style.marginLeft || "";
        if (d.dataset.wbOrigMr === undefined) d.dataset.wbOrigMr = d.style.marginRight || "";
        d.style.marginLeft = "0";
        d.style.marginRight = "0";
      }
    } catch (e1) {}
    try {
      var refreshSvg = actions.querySelector("svg.lucide-refresh-cw");
      var refreshBtn = refreshSvg ? refreshSvg.closest("button") : null;
      if (refreshBtn) refreshBtn.classList.add("wb-acc-btn-flat");
    } catch (e2) {}
  }

  function wbInjectAccountSearch() {
    // v1.0.38：归一化先于去重 early-return —— 即使搜索框已注入，
    // React 重渲染冲掉的间距/背景也要在每轮 run() 里补回来（幂等）。
    wbNormalizeAccountRowActions();

    // 避免重复注入（内联框与兜底行一起查：都是自己的节点）
    if (document.getElementById("wb-account-search-bar")) return;
    if (document.getElementById("wb-account-search-row")) return;

    // 2026-10-23：只在账号管理页注入。
    // 积分统计 / 词元统计页同样有 article 卡片，此前会把搜索框也插上去
    // （用户明确「只需要在账号统计页面出现即可」）。
    if (!wbIsAccountsPage()) {
      // 非账号页：把上一次注入留下的搜索栏撤掉再退出。
      // SPA 切页不重建 document，只靠 return 会让搜索栏残留在别的页上
      // （表现为「积分统计页也有搜索栏」的另一半成因）。
      wbRemoveAccountSearch();
      return;
    }

    var group = wbFindVariantSwitchGroup();
    // 拿不到档位容器：不在本页 / 官方结构变了。先清掉上次注入的残留，
    // 再安静退出（不清的话 SPA 切走后节点会留在别的页上）。
    // ⚠️ 第二行是被契约测试锁定的字面形态，不要把它合并进上面的清理行
    // —— 合并会让那个测试直接红。
    if (!group || !group.parentElement) wbRemoveAccountSearch();
    if (!group || !group.parentElement) return;

    var actions = wbFindAccountRowActions();

    var searchWrap = document.createElement("div");
    searchWrap.id = "wb-account-search-bar";
    searchWrap.className = "wb-acc-search-wrap";
    searchWrap.innerHTML =
      '<div class="wb-acc-search-box">' +
        '<svg class="wb-acc-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
          '<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>' +
        '</svg>' +
        // type="search"：移动端自带原生清除按钮（iOS 键盘上方也为搜索键）
        '<input type="search" id="wb-account-search-input" class="wb-acc-search-input" placeholder="搜索账号（名称 / 手机号 / ID）..." autocomplete="off" />' +
        '<span id="wb-account-search-count" class="wb-acc-search-count" style="display:none"></span>' +
      '</div>';

    // 移动端搜索图标按钮：克隆右侧容器现有图标按钮的 class，保证同款外观
    //（36×36、边框、底色、hover 全部与旁边两个图标一致）。
    var iconBtn = document.createElement("button");
    iconBtn.type = "button";
    iconBtn.id = "wb-account-search-icon";
    iconBtn.setAttribute("aria-label", "搜索账号");
    var siblingCls = actions && actions.querySelector("button")
      ? actions.querySelector("button").className : "";
    iconBtn.className = ("wb-acc-search-iconbtn " + siblingCls).trim();
    iconBtn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>' +
      '</svg>';

    // 移动端弹窗：点搜索图标弹出，输入即过滤（v1.0.36 用户方案）
    var modal = document.createElement("div");
    modal.id = "wb-account-search-modal";
    modal.hidden = true;
    modal.innerHTML =
      '<div class="wb-acc-search-backdrop"></div>' +
      '<div class="wb-acc-search-sheet">' +
        '<svg class="wb-acc-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
          '<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>' +
        '</svg>' +
        '<input type="search" id="wb-account-search-minput" class="wb-acc-search-minput" placeholder="搜索账号（名称 / 手机号 / ID）..." autocomplete="off" />' +
        '<span id="wb-account-search-mcount" class="wb-acc-search-count" style="display:none"></span>' +
        '<button type="button" id="wb-account-search-close" class="wb-acc-search-close" aria-label="关闭搜索">✕</button>' +
      '</div>';

    // 共享过滤状态：PC 内联输入框 ↔ 移动端弹窗输入框，任一变化双向同步。
    var state = { q: "" };
    function applyFilter(raw) {
      state.q = (raw || "").trim();
      var ql = state.q.toLowerCase();
      var currentCards = Array.prototype.slice.call(document.querySelectorAll("article"));
      var matched = 0;
      currentCards.forEach(function (card) {
        if (!ql) {
          card.style.display = "";
          matched++;
          return;
        }
        var text = (card.textContent || "").toLowerCase();
        var accId = (card.getAttribute("data-wb-acc") || "").toLowerCase();
        if (text.indexOf(ql) >= 0 || accId.indexOf(ql) >= 0) {
          card.style.display = "";
          matched++;
        } else {
          card.style.display = "none";
        }
      });
      // 输入框与计数位双向同步（两个输入框 + 两个计数徽章）
      var pcInput = searchWrap.querySelector("#wb-account-search-input");
      var pcCount = searchWrap.querySelector("#wb-account-search-count");
      var mInput = modal.querySelector("#wb-account-search-minput");
      var mCount = modal.querySelector("#wb-account-search-mcount");
      [[pcInput, pcCount], [mInput, mCount]].forEach(function (pair) {
        var inp = pair[0], cnt = pair[1];
        if (inp && inp.value !== state.q && document.activeElement !== inp) inp.value = state.q;
        if (cnt) {
          if (!state.q) { cnt.textContent = ""; cnt.style.display = "none"; }
          else { cnt.textContent = matched + " / " + currentCards.length; cnt.style.display = "inline-block"; }
        }
      });
      // 过滤生效中给图标按钮点亮，提醒移动端用户「当前列表是被过滤的」
      try { iconBtn.classList.toggle("wb-acc-search-icon-on", !!state.q); } catch (e2) {}
    }

    function openModal() {
      modal.hidden = false;
      var mInput = modal.querySelector("#wb-account-search-minput");
      if (mInput) { mInput.value = state.q; try { mInput.focus(); } catch (e3) {} }
    }
    function closeModal() { modal.hidden = true; }

    iconBtn.addEventListener("click", openModal);
    modal.querySelector(".wb-acc-search-backdrop").addEventListener("click", closeModal);
    var closeBtn = modal.querySelector("#wb-account-search-close");
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    // Esc 关闭：挂在弹窗自己的 keydown 上（输入框聚焦时冒泡可达），不碰 document
    modal.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeModal();
    });
    searchWrap.addEventListener("input", function (ev) {
      if (ev.target && ev.target.id === "wb-account-search-input") applyFilter(ev.target.value);
    });
    modal.addEventListener("input", function (ev) {
      if (ev.target && ev.target.id === "wb-account-search-minput") applyFilter(ev.target.value);
    });

    if (actions) {
      // 主锚点：插进「账号」行右侧图标容器首位 —— 标题之后、图标按钮之前，
      // 与图标的间距 = 容器自身 gap（与两个图标间距一致）。
      // PC 显示内联输入框、移动端显示搜索图标（CSS 按断点二选一），互不挤占。
      actions.insertBefore(searchWrap, actions.firstChild);
      actions.insertBefore(iconBtn, actions.firstChild);
    } else {
      // 兜底（官方改版找不到「账号」行时）：退回 v1.0.35 的独立过滤行形态 ——
      // 插在页头行（含标题与档位切换）紧后方，PC 右对齐、移动端吸顶。
      var header = null;
      try {
        header = group.closest("div.flex.items-start.justify-between");
      } catch (e4) {}
      if (!header || !header.parentNode) header = group.parentElement;
      var searchRow = document.createElement("div");
      searchRow.id = "wb-account-search-row";
      searchRow.className = "wb-acc-search-row";
      searchRow.appendChild(searchWrap);
      header.insertAdjacentElement("afterend", searchRow);
    }
    // 弹窗挂 body（fixed 定位不受父容器影响）
    document.body.appendChild(modal);
  }

  /* ------------------------------------------------------------------
     设置页：API 接入
     容器只做两件事 —— 账号管理/自动签到，以及对外提供 OpenAI 兼容 API。
     但接入地址与密钥此前在 UI 上完全不可见，这里把「怎么连、用什么密钥连」
     直接落到设置页，用户不用去翻 README。
     ------------------------------------------------------------------ */

  var WB_API_BASE_LS = "wb_api_base_url";

  // tone: "info"（默认，纯告知）| "ok" | "warn" | "error"
  //
  // 仍然接受**布尔**作为第二个参数（true = error），旧调用点不必全改；
  // 但新代码请传字符串档位 —— 二档不够用：「账号受限」既不是成功也不是
  // 需要你重登的错误，用红色会把人引向错误的排查方向。
  function wbToast(message, tone) {
    // tone 兼容三种输入，避免调用点写错就静默变默认档：
    //   字符串 "ok" / "info"  -> 绿（或默认告知档）
    //   字符串 "warn"         -> 琥珀
    //   字符串 "err" / "error" -> 红
    //   true                  -> 红（旧布尔签名）
    //   false / 未传           -> 默认
    var t;
    if (tone === true) t = "err";
    else if (tone === false || tone == null) t = "info";
    else if (tone === "error") t = "err";
    else t = String(tone);
    var cls = "wb-api-toast";
    if (t === "ok" || t === "warn" || t === "err") { cls += " wb-api-toast-" + t; }
    var el = document.createElement("div");
    el.className = cls;
    el.textContent = message;
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("wb-api-toast-show"); });
    // 文案越长停留越久：结论 + 依据 + 建议三行挤在一起一闪而过，等于没提示。
    var hold = 1800 + Math.max(0, String(message || "").length - 24) * 45;
    setTimeout(function () {
      el.classList.remove("wb-api-toast-show");
      setTimeout(function () { el.remove(); }, 260);
    }, Math.min(hold, 7000));
  }

  function wbCopy(text, okMessage) {
    // 局域网走的是 http://，不是安全上下文，navigator.clipboard 直接不可用，
    // 所以必须有 execCommand 兜底，否则「复制」按钮点了没反应。
    function fallback() {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-1000px";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        var ok = document.execCommand("copy");
        ta.remove();
        wbToast(ok ? (okMessage || "已复制") : "复制失败，请手动选中复制", !ok);
      } catch (e) {
        wbToast("复制失败，请手动选中复制", true);
      }
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { wbToast(okMessage || "已复制"); }, fallback);
    } else {
      fallback();
    }
  }

  // 所有写操作走这里：统一把网关返回的 detail 弹出来。
  // 后端的「最后一个密钥不能停用」「零密钥不能开启校验」这类防线必须让用户看见，
  // 否则点了按钮没反应，只会以为 UI 坏了。
  function wbApiAction(url, method, payload, okMessage) {
    return fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json()
          .catch(function () { return {}; })
          .then(function (body) { return { ok: r.ok, body: body || {} }; });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail;
          wbToast(typeof d === "string" && d ? d : (res.body.error || "操作失败"), true);
          return null;
        }
        if (okMessage) wbToast(okMessage);
        wbRefreshApi(true);
        return res.body;
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", true);
        return null;
      });
  }

  function wbApiBase() {    var saved = "";
    try { saved = localStorage.getItem(WB_API_BASE_LS) || ""; } catch (e) { saved = ""; }
    if (saved) return saved;
    var loc = window.location;
    var host = loc.hostname || "127.0.0.1";
    // IPv6 需要方括号
    if (host.indexOf(":") >= 0 && host.charAt(0) !== "[") host = "[" + host + "]";
    var scheme = loc.protocol === "https:" ? "https:" : "http:";
    return scheme + "//" + host + ":18091/v1";
  }

  function wbSetApiBase(value) {
    try {
      if (value) localStorage.setItem(WB_API_BASE_LS, value);
      else localStorage.removeItem(WB_API_BASE_LS);
    } catch (e) {}
  }

  function wbEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    // v1.0.5：注入的 button 一律显式 type="button"。
    // 官方页面有 <form>（如账号登录表单），button 不写 type 时默认 submit，
    // 我们的按钮一旦落在 form 内，点一下就触发表单提交整页刷新 ——
    // 移动端「点启用直接刷新页面」就是这么来的。
    if (tag === "button") el.type = "button";
    return el;
  }

  function wbApiRow(label, desc, control) {
    var row = wbEl("div", "wb-api-row");
    var main = wbEl("div", "wb-api-main");
    main.appendChild(wbEl("div", "wb-api-label", label));
    if (desc) main.appendChild(wbEl("div", "wb-api-desc", desc));
    row.appendChild(main);
    if (control) row.appendChild(control);
    return row;
  }

  function wbChip(text) {
    var chip = wbEl("span", "wb-api-chip wb-api-mono");
    chip.appendChild(wbEl("span", null, text));
    return chip;
  }

  /* 密钥统计子行里的一个「标签 + 数值」单元。
     单独成行展示（用户 2026-10-17 需求：调用次数单独一行，
     再加今日/全部的积分消耗与词元消耗），不复用行内的 badge ——
     badge 是给状态用的，塞五个数字会挤成一团。 */
  function wbKeyStat(label, value) {
    var cell = wbEl("span", "wb-api-keystat-cell");
    cell.appendChild(wbEl("span", "wb-api-keystat-k", label));
    cell.appendChild(wbEl("span", "wb-api-keystat-v", value));
    return cell;
  }

  function wbTime(ms) {
    if (!ms) return "从未";
    try {
      var d = new Date(ms);
      var p = function (n) { return (n < 10 ? "0" : "") + n; };
      return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
        " " + p(d.getHours()) + ":" + p(d.getMinutes());
    } catch (e) { return "—"; }
  }

  function wbFindSettingsHost() {
    // 设置页标题是唯一的 h1「设置」，用它定位比依赖路由稳定。
    var h1s = document.querySelectorAll("h1");
    for (var i = 0; i < h1s.length; i++) {
      if ((h1s[i].textContent || "").trim() !== "设置") continue;
      var header = h1s[i].closest("header");
      if (!header) continue;
      return header.nextElementSibling || header.parentElement || null;
    }
    return null;
  }

  function wbRenderApiSection(host, data) {
    var gw = data.gateway || {};
    var auth = gw.auth || {};
    var models = gw.models || {};
    var accounts = gw.accounts || {};
    var stats = data.stats || {};
    var keys = data.keys || [];
    var keyStats = data.keyStats || {};   // 按密钥的调用/词元统计（token-stats 的 byKey）
    var base = wbApiBase();

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-api-access";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "API 接入");
    h2.id = "settings-api-access-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-api-access-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 1. 对外访问地址（可改，覆盖隧道/反代场景）----
    var addrRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var addrHead = wbEl("div", null);
    addrHead.appendChild(wbEl("div", "wb-api-label", "对外访问地址"));
    addrHead.appendChild(wbEl("div", "wb-api-desc",
      "下游客户端（Sub2API、Cherry Studio、各类 Agent）填这个地址。默认按当前访问的主机名自动推导，走域名或隧道时可手动改。"));
    addrRow.appendChild(addrHead);

    var addrLine = wbEl("div", null);
    addrLine.style.cssText = "display:flex;align-items:center;gap:8px;margin-top:8px;";
    var addrInput = document.createElement("input");
    addrInput.className = "wb-api-input wb-api-mono";
    addrInput.value = base;
    addrInput.spellcheck = false;
    var addrCopy = wbEl("button", "wb-api-btn wb-api-btn-primary", "复制");
    addrCopy.onclick = function () { wbCopy(addrInput.value.trim(), "已复制接入地址"); };
    var addrReset = wbEl("button", "wb-api-btn", "恢复默认");
    addrReset.onclick = function () {
      wbSetApiBase("");
      addrInput.value = wbApiBase();
      wbToast("已恢复为自动推导地址");
    };
    addrInput.onchange = function () {
      wbSetApiBase(addrInput.value.trim());
      wbToast("已保存自定义地址");
    };
    addrLine.appendChild(addrInput);
    addrLine.appendChild(addrCopy);
    addrLine.appendChild(addrReset);
    addrRow.appendChild(addrLine);
    card.appendChild(addrRow);

    // ---- 2. 端点 ----
    var epRow = wbEl("div", "wb-api-row");
    var epMain = wbEl("div", "wb-api-main");
    epMain.appendChild(wbEl("div", "wb-api-label", "接口端点"));
    epMain.appendChild(wbEl("div", "wb-api-desc", "标准 OpenAI 协议，改 base_url 即可直接替换官方端点。"));
    epRow.appendChild(epMain);
    var epBtns = wbEl("div", null);
    epBtns.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;";
    [["POST /v1/chat/completions", "/v1/chat/completions"],
     ["GET /v1/models", "/v1/models"]].forEach(function (item) {
      var b = wbEl("button", "wb-api-btn wb-api-mono", item[0]);
      b.title = "点击复制完整地址";
      b.onclick = function () { wbCopy(addrInput.value.trim().replace(/\/+$/, "") + item[1], "已复制 " + item[1]); };
      epBtns.appendChild(b);
    });
    epRow.appendChild(epBtns);
    card.appendChild(epRow);

    // ---- 3. 运行状态 ----
    var stRow = wbEl("div", "wb-api-row");
    var stMain = wbEl("div", "wb-api-main");
    stMain.appendChild(wbEl("div", "wb-api-label", "网关状态"));
    stMain.appendChild(wbEl("div", "wb-api-desc",
      "版本 " + (gw.version || "—") + " · 账号池模式 " +
      ((accounts.mode === "manual") ? "手动指定" : "自动轮换")));
    stRow.appendChild(stMain);
    var stBadges = wbEl("div", null);
    stBadges.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;";
    stBadges.appendChild(wbEl("span", "wb-api-badge wb-api-badge-ok",
      "模型 " + (models.count || 0) + " 个"));
    stBadges.appendChild(wbEl("span", "wb-api-badge" + ((accounts.usable || 0) > 0 ? " wb-api-badge-ok" : " wb-api-badge-warn"),
      "可用账号 " + (accounts.usable || 0) + "/" + (accounts.total || 0)));
    stBadges.appendChild(wbEl("span", "wb-api-badge", "参与调用 " + (accounts.inPool || 0) + " 个"));
    stRow.appendChild(stBadges);
    card.appendChild(stRow);

    // ---- 3.5 调用分流三档开关（工单第 3 件）----
    // all=全部账号（默认，保持现有行为）；cn=只走国内；ai=只走国际版。
    // 只影响自动轮询的候选集；显式指定账号不受档位限制。
    var routingRow = wbEl("div", "wb-api-row");
    var routingMain = wbEl("div", "wb-api-main");
    routingMain.appendChild(wbEl("div", "wb-api-label", "调用分流"));
    routingMain.appendChild(wbEl("div", "wb-api-desc",
      "自动轮询时使用哪类账号。默认「全部账号」即现有行为；切换后立即生效，已显式指定账号的调用不受影响。"));
    routingRow.appendChild(routingMain);
    var routingBtns = wbEl("div", null);
    routingBtns.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;";
    var currentRouting = String((accounts.callRouting) || "all").toLowerCase();
    var routingChoices = [
      ["all", "全部账号"],
      ["cn", "国内账号"],
      ["ai", "国外账号"]
    ];
    routingChoices.forEach(function (item) {
      var on = currentRouting === item[0];
      var b = wbEl("button", "wb-api-btn" + (on ? " wb-api-btn-on" : ""), item[1]);
      if (on) b.title = "当前档位";
      b.onclick = function () {
        if (on) return;
        wbApiAction("/api/account-pool", "PUT", { callRouting: item[0] },
          "调用分流已切换为「" + item[1] + "」");
      };
      routingBtns.appendChild(b);
    });
    routingRow.appendChild(routingBtns);
    card.appendChild(routingRow);

    // ---- 4. 密钥校验开关 ----
    var authRow = wbEl("div", "wb-api-row");
    var authMain = wbEl("div", "wb-api-main");
    authMain.appendChild(wbEl("div", "wb-api-label", "强制 API 密钥校验"));
    authMain.appendChild(wbEl("div", "wb-api-desc", data.requireKey
      ? "已开启：调用 /v1/* 必须携带有效密钥，缺失或错误返回 401。"
      : "未开启：任何能访问该端口的客户端都可直接调用。仅建议在纯内网环境下保持关闭。"));
    authRow.appendChild(authMain);
    var authToggle = wbEl("button",
      "wb-api-btn" + (data.requireKey ? " wb-api-btn-on" : ""),
      data.requireKey ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    authToggle.onclick = function () {
      authToggle.disabled = true;
      wbApiAction("/api/api-keys/config", "PUT", { requireKey: !data.requireKey },
        !data.requireKey ? "已开启密钥校验" : "已关闭密钥校验")
        .then(function () { authToggle.disabled = false; });
    };
    authRow.appendChild(authToggle);
    card.appendChild(authRow);

    // （原 4.5 IP 白名单免验证区块已移除 ——
    //   它把「要不要密钥」和「这个来源能不能用」两件事混在一起，
    //   既难理解又难排查；用户 2026-10-07 反馈「方法做复杂了」。
    //   现在访问校验只保留「密钥 + loopback」两条规则。）

    // ---- 5. 密钥列表 ----
    var keyHead = wbEl("div", "wb-api-row");
    var keyMain = wbEl("div", "wb-api-main");
    keyMain.appendChild(wbEl("div", "wb-api-label", "API 密钥"));
    keyMain.appendChild(wbEl("div", "wb-api-desc",
      "共 " + (stats.total || 0) + " 个，启用 " + (stats.enabled || 0) + " 个 · 累计调用 " + (stats.calls || 0) + " 次"
      + " · 最近使用 " + wbTime(stats.lastUsedAt)));
    keyHead.appendChild(keyMain);
    var keyActions = wbEl("div", null);
    keyActions.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;";
    var addKey = wbEl("button", "wb-api-btn wb-api-btn-primary", "新建密钥");
    addKey.onclick = function () {
      var name = window.prompt("给这个密钥起个名字（便于区分调用方，可留空）：", "");
      if (name === null) return;
      wbApiAction("/api/api-keys", "POST", { name: name }).then(function (res) {
        if (res && res.key) wbCopy(res.key.key, "新密钥已生成并复制");
      });
    };
    keyActions.appendChild(addKey);
    if ((stats.total || 0) > 1) {
      var clearKeys = wbEl("button", "wb-api-btn wb-api-btn-danger", "清空全部");
      clearKeys.onclick = function () {
        if (!window.confirm("确认清空全部 " + stats.total + " 个密钥？已在使用这些密钥的客户端会立即失去访问权限。")) return;
        wbApiAction("/api/api-keys/delete-all", "POST", undefined, "已清空全部密钥");
      };
      keyActions.appendChild(clearKeys);
    }
    keyHead.appendChild(keyActions);
    card.appendChild(keyHead);

    var listRow = wbEl("div", "wb-api-row wb-api-row-stack");
    if (!keys.length) {
      listRow.appendChild(wbEl("div", "wb-api-empty",
        "还没有密钥。点「新建密钥」生成一个 —— 密钥格式为 sk-ab-…，明文保存在 /data/.autobuddy/api_keys.json（权限 0600）。"));
    } else {
      keys.forEach(function (k) {
        var line = wbEl("div", "wb-api-keyrow");
        var enabled = wbEl("span", "wb-api-badge" + (k.enabled ? " wb-api-badge-ok" : " wb-api-badge-warn"),
          k.enabled ? "启用" : "已停用");
        line.appendChild(enabled);
        line.appendChild(wbEl("span", "wb-api-keyname", k.name || "未命名"));
        line.appendChild(wbChip(k.maskedKey));

        /* 行内不再放「调用 N 次」badge：累计调用已由下面的统计子行展示，
           同一个数字出现两处只会让人觉得「是不是两个口径」。
           badge 改成只显示创建/最近使用时间，hover 有完整信息。 */
        var meta = wbEl("span", "wb-api-badge", "最近使用 " + wbTime(k.lastUsedAt));
        meta.title = "创建于 " + wbTime(k.createdAt) + " · 累计调用 "
          + (k.callCount || 0) + " 次 · 最近使用 " + wbTime(k.lastUsedAt);
        line.appendChild(meta);

        /* 按密钥的调用 / 积分 / 词元统计（用户 2026-10-17 需求：
           「可以将调用次数单独一行，并添加今日和全部的积分消耗，词元消耗」）。
           数据来自 /api/token-stats 的 byKey（record_token_usage 的 key_id 归因）。
           取不到时（该密钥还没被用于调用、或历史记录无 keyId）显示「—」，
           不编造 0 —— 「没有记录」和「真的是 0」是两件事。 */
        var ks = keyStats[k.id] || null;
        var statRow = wbEl("div", "wb-api-keystat");
        /* ⚠️ 2026-10-17 修复（用户反馈「Default 的 API 调用 45644 次，
           下面却说累计调用 62 次，原来的没取消，新的还不对」）：
           累计调用**必须用 k.callCount**（api_keys 的权威计数，从密钥诞生
           起每次成功调用都记，含全部历史），不能用 byKey.calls ——
           byKey 只统计「带了 keyId 归因」的记录，而 keyId 记账是这次才加的，
           历史记录全都没有，于是 Default 显示 62 而徽标显示 45644，自相矛盾。
           今日调用与词元消耗只有 byKey 一个来源（历史记录无 keyId，无法回溯），
           取不到时显示「—」而不是 0 —— 「没有记录」和「真的是 0」是两件事。 */
        var fmtN = function (n) {
          n = Number(n || 0);
          if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
          if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
          if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
          return String(n);
        };
        var totalCalls = Number(k.callCount || 0);
        statRow.appendChild(wbKeyStat("累计调用", fmtN(totalCalls) + " 次"));
        if (ks) {
          statRow.appendChild(wbKeyStat("今日调用", fmtN(ks.todayCalls) + " 次"));
          statRow.appendChild(wbKeyStat("今日词元", fmtN(ks.todayTokens)));
          statRow.appendChild(wbKeyStat("累计词元", fmtN(ks.totalTokens)));
          statRow.appendChild(wbKeyStat("输入 / 输出",
            fmtN(ks.inputTokens) + " / " + fmtN(ks.outputTokens)));
          statRow.title = "最后使用：" + wbTime(ks.lastAt)
            + "　缓存命中：" + fmtN(ks.cacheRead)
            + "\n累计调用为权威值（含全部历史）；今日调用与词元消耗为启用密钥归因后的统计。";
        } else {
          statRow.appendChild(wbEl("span", "wb-api-keystat-empty",
            "今日调用与词元消耗暂无明细"));
        }
        line.appendChild(statRow);

        var spacer = wbEl("span", null);
        spacer.style.cssText = "flex:1 1 auto;";
        line.appendChild(spacer);

        var copyBtn = wbEl("button", "wb-api-btn", "复制明文");
        copyBtn.onclick = function () { wbCopy(k.key, "已复制该密钥明文"); };
        line.appendChild(copyBtn);

        /* 工单第 4 件：编辑（改名）+ 重置（清统计）。
           编辑走 prompt 改名，非高危不打扰；重置是高危动作，
           与停用/删除一样必须二次确认 —— 误点一次就把累计计数抹了。 */
        var editBtn = wbEl("button", "wb-api-btn", "编辑");
        editBtn.onclick = function () {
          var name = window.prompt("修改密钥名称（便于区分调用方）：", k.name || "");
          if (name === null) return;
          name = name.trim();
          if (!name || name === (k.name || "")) { wbToast("名称未变"); return; }
          wbApiAction("/api/api-keys/update", "POST", { id: k.id, name: name }, "已重命名为「" + name + "」");
        };
        line.appendChild(editBtn);

        var resetBtn = wbEl("button", "wb-api-btn", "重置统计");
        resetBtn.title = "清零该密钥的调用计数与最近使用时间，密钥本身不变";
        resetBtn.onclick = function () {
          if (!window.confirm("确认重置密钥「" + (k.name || "未命名") + "」的统计？\n" +
              "累计调用 " + (k.callCount || 0) + " 次将被清零，且无法恢复（密钥本身不受影响）。")) return;
          wbApiAction("/api/api-keys/reset-stats", "POST", { id: k.id }, "已重置该密钥统计");
        };
        line.appendChild(resetBtn);

        var toggleBtn = wbEl("button", "wb-api-btn", k.enabled ? "停用" : "启用");
        toggleBtn.onclick = function () {
          if (k.enabled && !window.confirm("确认停用密钥「" + (k.name || "未命名") + "」？\n" +
              "使用它的客户端将立即失去访问权限（401）。")) return;
          wbApiAction("/api/api-keys/update", "POST", { id: k.id, enabled: !k.enabled },
            k.enabled ? "已停用该密钥" : "已启用该密钥");
        };
        line.appendChild(toggleBtn);

        var delBtn = wbEl("button", "wb-api-btn wb-api-btn-danger", "删除");
        delBtn.onclick = function () {
          if (!window.confirm("确认删除密钥「" + (k.name || "未命名") + "」？\n" +
              "该操作不可恢复，使用它的客户端会立即失去访问权限。")) return;
          wbApiAction("/api/api-keys/delete", "POST", { id: k.id }, "已删除");
        };
        line.appendChild(delBtn);

        listRow.appendChild(line);
      });
    }
    card.appendChild(listRow);

    // ---- 6. 自检 ----
    var selfRow = wbEl("div", "wb-api-row");
    var selfMain = wbEl("div", "wb-api-main");
    selfMain.appendChild(wbEl("div", "wb-api-label", "连通性自检"));
    var selfDesc = wbEl("div", "wb-api-desc",
      "在容器内回环实测网关健康、模型清单与密钥配置，用来确认「UI 能打开但 API 调不通」这类问题。");
    selfMain.appendChild(selfDesc);
    var selfResult = wbEl("div", "wb-api-empty");
    selfResult.style.display = "none";
    selfMain.appendChild(selfResult);
    selfRow.appendChild(selfMain);
    var selfBtn = wbEl("button", "wb-api-btn wb-api-btn-primary", "开始自检");
    selfBtn.onclick = function () {
      selfBtn.disabled = true;
      selfBtn.textContent = "检测中…";
      selfResult.style.display = "";
      selfResult.textContent = "正在检测…";
      fetch("/api/gateway-selftest", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          var lines = (res.steps || []).map(function (s) {
            return (s.ok ? "✓ " : "✗ ") + s.name + " — " + s.detail;
          });
          selfResult.textContent = lines.join("\n");
          selfResult.style.whiteSpace = "pre-wrap";
          selfResult.style.color = res.ok ? "#15803d" : "#b91c1c";
          wbToast(res.ok ? "自检通过" : "自检发现问题", !res.ok);
        })
        .catch(function () {
          selfResult.textContent = "自检失败：无法访问代理接口。";
          selfResult.style.color = "#b91c1c";
          wbToast("自检失败", true);
        })
        .finally(function () {
          selfBtn.disabled = false;
          selfBtn.textContent = "重新自检";
        });
    };
    selfRow.appendChild(selfBtn);
    card.appendChild(selfRow);

    // ---- 7. 调用示例 ----
    var sampleRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var sampleHead = wbEl("div", null);
    sampleHead.appendChild(wbEl("div", "wb-api-label", "调用示例"));
    sampleHead.appendChild(wbEl("div", "wb-api-desc",
      "把 <你的密钥> 换成上面任意一个启用中的密钥即可。"));
    sampleRow.appendChild(sampleHead);

    var sampleKey = (keys.filter(function (k) { return k.enabled; })[0] || {}).key || "<你的密钥>";
    var apiRoot = addrInput.value.trim().replace(/\/+$/, "");
    var sampleText =
      "curl " + apiRoot + "/chat/completions \\\n" +
      "  -H \"Content-Type: application/json\" \\\n" +
      "  -H \"Authorization: Bearer " + sampleKey + "\" \\\n" +
      "  -d '{\n" +
      "    \"model\": \"hy3\",\n" +
      "    \"messages\": [{\"role\": \"user\", \"content\": \"你好\"}]\n" +
      "  }'";
    sampleRow.appendChild(wbEl("div", "wb-api-code", sampleText));
    var sampleBar = wbEl("div", null);
    sampleBar.style.cssText = "display:flex;gap:6px;margin-top:8px;justify-content:flex-end;";
    var copySample = wbEl("button", "wb-api-btn", "复制示例");
    copySample.onclick = function () { wbCopy(sampleText, "已复制调用示例"); };
    sampleBar.appendChild(copySample);
    var copyAll = wbEl("button", "wb-api-btn wb-api-btn-primary", "复制接入信息");
    copyAll.onclick = function () {
      wbCopy([
        "AutoBuddy · OpenAI 兼容 API",
        "Base URL: " + apiRoot,
        "端点: POST /v1/chat/completions, GET /v1/models",
        "鉴权: Authorization: Bearer <key>" + (data.requireKey ? "（当前强制校验）" : "（当前未强制校验）"),
        "密钥: " + (keys.filter(function (k) { return k.enabled; }).map(function (k) { return k.key; }).join(" / ") || "（尚未创建）"),
        "可用模型: " + (models.count || 0) + " 个",
        "账号池: " + (accounts.inPool || 0) + " 个账号参与调用"
      ].join("\n"), "已复制完整接入信息");
    };
    sampleBar.appendChild(copyAll);
    sampleRow.appendChild(sampleBar);
    card.appendChild(sampleRow);

    section.appendChild(card);
    host.appendChild(section);
    wbSortSettingsSections();
  }

  var wbApiCache = null;

  function wbRefreshApi(force) {
    if (force) { wbApiCache = null; wbInfoCache = null; }
    var old = document.getElementById("settings-api-access");
    if (old) old.remove();
    var oldHealth = document.getElementById("settings-model-health");
    if (oldHealth) oldHealth.remove();
    var oldAbout = document.getElementById("settings-about");
    if (oldAbout) oldAbout.remove();
    var oldNotify = document.getElementById("settings-notify");
    if (oldNotify) oldNotify.remove();
    var oldAssistant = document.getElementById("settings-assistant");
    if (oldAssistant) oldAssistant.remove();
    injectApiAccess();
    injectModelHealth();
    injectAssistant();
    injectNotify();
    // 关于面板里有一行「当前规模」，禁用项增减后要跟着变，所以一并重取。
    injectAbout();
    return wbApiCache;
  }

  // ---------------------------------------------------------------------
  // 设置页：模型可用性巡检
  // 探测会真实向上游发请求，所以默认关闭；开启后按间隔逐个检测「账号 × 模型」，
  // 不可用自动禁用、恢复自动启用 —— 写的都是同一份 model_policy.json。
  // ---------------------------------------------------------------------
  function wbRenderHealthSection(host, data) {
    var cfg = data.config || {};
    var runtime = data.runtime || {};
    var accounts = data.accounts || [];
    var last = runtime.lastResult || null;

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-model-health";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "模型可用性巡检");
    h2.id = "settings-model-health-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-model-health-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 1. 总开关 ----
    var swRow = wbEl("div", "wb-api-row");
    var swMain = wbEl("div", "wb-api-main");
    swMain.appendChild(wbEl("div", "wb-api-label", "自动巡检"));
    swMain.appendChild(wbEl("div", "wb-api-desc",
      "开启后每隔一段时间检测账号的模型可用性 —— 调不通的自动禁用，恢复的自动启用。"
      + "关掉后之前自动写入的禁用项依然生效，仍可在账号卡片上手动禁用 / 启用。"));
    swRow.appendChild(swMain);
    var swBtn = wbEl("button",
      "wb-api-btn" + (cfg.enabled ? " wb-api-btn-on" : ""),
      cfg.enabled ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    swBtn.onclick = function () {
      swBtn.disabled = true;
      wbApiAction("/api/model-health", "PUT", { enabled: !cfg.enabled },
        !cfg.enabled ? "已开启自动巡检" : "已关闭自动巡检")
        .then(function () { swBtn.disabled = false; });
    };
    swRow.appendChild(swBtn);
    card.appendChild(swRow);

    // ---- 2. 巡检间隔 ----
    var itvRow = wbEl("div", "wb-api-row");
    var itvMain = wbEl("div", "wb-api-main");
    itvMain.appendChild(wbEl("div", "wb-api-label", "巡检间隔"));
    itvMain.appendChild(wbEl("div", "wb-api-desc",
      "两次巡检之间等待多久，最短 " + (data.minIntervalMinutes || 5) + " 分钟。"
      + "每个组合都要发一次真实请求，间隔太短会产生可观的上游调用量。"));
    itvRow.appendChild(itvMain);
    var itvWrap = document.createElement("div");
    itvWrap.style.cssText = "display:flex;align-items:center;gap:6px;flex:0 0 auto;";
    var itvInput = document.createElement("input");
    itvInput.className = "wb-api-input wb-api-mono";
    itvInput.style.cssText = "width:78px;flex:0 0 auto;text-align:center;";
    itvInput.type = "number";
    itvInput.min = String(data.minIntervalMinutes || 5);
    itvInput.value = String(cfg.intervalMinutes || 60);
    var itvSave = wbEl("button", "wb-api-btn", "保存");
    itvSave.onclick = function () {
      var v = parseInt(itvInput.value, 10);
      if (isNaN(v)) { wbToast("请输入分钟数", true); return; }
      itvSave.disabled = true;
      wbApiAction("/api/model-health", "PUT", { intervalMinutes: v }, "已保存巡检间隔")
        .then(function () { itvSave.disabled = false; });
    };
    itvWrap.appendChild(itvInput);
    itvWrap.appendChild(wbEl("span", "wb-api-badge", "分钟"));
    itvWrap.appendChild(itvSave);
    itvRow.appendChild(itvWrap);
    card.appendChild(itvRow);

    // ---- 3. 账号范围 ----
    var scopeRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var scopeMain = wbEl("div", "wb-api-main");
    scopeMain.appendChild(wbEl("div", "wb-api-label", "巡检账号范围"));
    scopeMain.appendChild(wbEl("div", "wb-api-desc",
      "不勾选任何账号 = 全部账号参与巡检。勾选后只有被选中的账号会被探测。"));
    scopeRow.appendChild(scopeMain);

    var picked = {};
    (cfg.accountIds || []).forEach(function (id) { picked[String(id)] = true; });
    var listWrap = wbEl("div", null);
    listWrap.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;";

    accounts.forEach(function (a) {
      var id = String(a.id);
      var on = !(cfg.accountIds || []).length || picked[id];
      var tag = wbEl("button",
        "wb-am-tag wb-am-tag-click" + (on ? " wb-am-tag-used" : ""),
        (a.name || id) + (a.variant === "ai" ? " · 国际版" : " · 国内版"));
      tag.title = on ? "已参与巡检，点击排除" : "未参与巡检，点击加入";
      tag.onclick = function () {
        var all = (cfg.accountIds || []).slice();
        var isAll = all.length === 0;
        var next;
        if (isAll) {
          // 从「全部」切到「显式列表」：先展开成全部账号，再去掉当前这个
          next = accounts.map(function (x) { return String(x.id); })
            .filter(function (x) { return x !== id; });
        } else if (all.indexOf(id) >= 0) {
          next = all.filter(function (x) { return x !== id; });
        } else {
          next = all.concat([id]);
        }
        // 显式选满全部账号时收敛回空数组，语义与「全部」等价
        if (next.length === accounts.length) next = [];
        wbApiAction("/api/model-health", "PUT", { accountIds: next }, "已更新巡检范围");
      };
      listWrap.appendChild(tag);
    });
    if (!accounts.length) {
      listWrap.appendChild(wbEl("div", "wb-api-empty", "还没有账号。"));
    }
    scopeRow.appendChild(listWrap);

    var scopeHint = wbEl("div", "wb-api-desc");
    scopeHint.style.marginTop = "8px";
    scopeHint.textContent = (cfg.accountIds || []).length
      ? "当前：指定 " + cfg.accountIds.length + " 个账号"
      : "当前：全部 " + accounts.length + " 个账号";
    scopeRow.appendChild(scopeHint);
    card.appendChild(scopeRow);

    // ---- 4. 自动启用 ----
    var aeRow = wbEl("div", "wb-api-row");
    var aeMain = wbEl("div", "wb-api-main");
    aeMain.appendChild(wbEl("div", "wb-api-label", "检测到可用时自动启用"));
    aeMain.appendChild(wbEl("div", "wb-api-desc",
      "开启时：巡检自己禁掉的模型恢复可用后会自动放开（自愈）。"
      + "你在账号卡片上手动禁用的模型不受影响 —— 手动禁用代表明确的取舍"
      + "（常见于「能跑但太贵」），不会被自愈抹掉。关闭时只做自动禁用。"));
    aeRow.appendChild(aeMain);
    var aeBtn = wbEl("button",
      "wb-api-btn" + (cfg.autoEnable ? " wb-api-btn-on" : ""),
      cfg.autoEnable ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    aeBtn.onclick = function () {
      aeBtn.disabled = true;
      wbApiAction("/api/model-health", "PUT", { autoEnable: !cfg.autoEnable },
        !cfg.autoEnable ? "已开启自动启用" : "已关闭自动启用")
        .then(function () { aeBtn.disabled = false; });
    };
    aeRow.appendChild(aeBtn);
    card.appendChild(aeRow);

    // ---- 4b. 当前禁用构成 ----
    // 把「谁关的」摊开给用户看。这两类禁用含义不同、恢复方式也不同，
    // 混在一个「已禁用 N」里会让人无法判断巡检是否已经生效。
    var srcRow = wbEl("div", "wb-api-row");
    var srcMain = wbEl("div", "wb-api-main");
    srcMain.appendChild(wbEl("div", "wb-api-label", "当前禁用构成"));
    var bySource = data.disabledBySource || {};
    var manualN = bySource.manual || 0;
    var autoN = bySource.auto || 0;
    srcMain.appendChild(wbEl("div", "wb-api-desc",
      "手动禁用 " + manualN + " 项（你点的，巡检不会自动放开）"
      + " · 巡检禁用 " + autoN + " 项（探测不通自动写的，恢复后自动放开）"));
    srcRow.appendChild(srcMain);
    card.appendChild(srcRow);

    // ---- 5. 探测范围口径 ----
    var umRow = wbEl("div", "wb-api-row");
    var umMain = wbEl("div", "wb-api-main");
    umMain.appendChild(wbEl("div", "wb-api-label", "只探测用过的模型"));
    umMain.appendChild(wbEl("div", "wb-api-desc",
      "开启时只检测该账号实际调用过的模型（新账号无记录时退回基础清单）；"
      + "关闭时会探测全部已知模型 —— 组合数量会大很多，谨慎使用。"));
    umRow.appendChild(umMain);
    var umBtn = wbEl("button",
      "wb-api-btn" + (cfg.onlyUsedModels ? " wb-api-btn-on" : ""),
      cfg.onlyUsedModels ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    umBtn.onclick = function () {
      umBtn.disabled = true;
      wbApiAction("/api/model-health", "PUT", { onlyUsedModels: !cfg.onlyUsedModels },
        !cfg.onlyUsedModels ? "已收窄探测范围" : "已放开探测范围")
        .then(function () { umBtn.disabled = false; });
    };
    umRow.appendChild(umBtn);
    card.appendChild(umRow);

    // ---- 6. 立即巡检 + 上次结果 ----
    var runRow = wbEl("div", "wb-api-row");
    var runMain = wbEl("div", "wb-api-main");
    runMain.appendChild(wbEl("div", "wb-api-label", "立即巡检一次"));
    var runDesc = wbEl("div", "wb-api-desc");
    if (runtime.running) {
      runDesc.textContent = "正在巡检中…";
    } else if (last && last.aborted) {
      // 整轮作废：一个可用的都没有，说明探测本身出了问题。
      // 这必须说得比「上次巡检完成」更显眼，否则用户会以为巡检正常但模型全坏了。
      runDesc.textContent = "上次巡检已跳过（未改动任何配置）：" + (last.reason || "探测异常");
      runDesc.style.color = "#b45309";
    } else if (runtime.lastError) {
      // 探测被上游参数校验拒绝、账号凭据失效等情况：本轮结果部分不可信，
      // 已经跳过了那些组合，必须显式说出来，否则看起来就跟「什么都没变」一样。
      runDesc.textContent = "上次巡检有情况被跳过：" + runtime.lastError;
      runDesc.style.color = "#b45309";
    } else if (last) {
      var c = last.counts || {};
      runDesc.textContent = "上次巡检 " + wbTime(last.checkedAt)
        + " · 共 " + (last.combos || 0) + " 个组合（" + (last.accounts || 0) + " 个账号）"
        + " · 可用 " + (c.available || 0)
        + " / 不可用 " + (c.unavailable || 0)
        + " / 跳过 " + (c.transient || 0);
      // 探测被上游参数校验拒绝：只在真的发生过时才显示，避免日常噪音。
      if (c.probe_defect) {
        runDesc.textContent += " / 探测被拒 " + c.probe_defect;
      }
      // 账号被上游拦截：与「不可用」分开显示。它既不是模型坏，也不是凭据失效，
      // 混进「不可用」会让用户点开一堆账号去找原因。
      if (c.restricted) {
        runDesc.textContent += " / 账号被拦截 " + c.restricted;
      }
      // 手动禁用的模型这一轮整个没探测。不说出来的话，用户只会看到组合数
      // 比「账号 × 模型」总数少，却不知道差在哪。
      if (last.skippedManual) {
        runDesc.textContent += " · 手动禁用 " + last.skippedManual + " 项未探测";
      }
      if ((last.disabled || []).length) {
        runDesc.textContent += " · 新禁用 " + last.disabled.length + " 项";
      }
      if ((last.enabled || []).length) {
        runDesc.textContent += " · 新启用 " + last.enabled.length + " 项";
      }
    } else {
      runDesc.textContent = "还没有执行过巡检。点右边按钮可以立刻跑一轮，"
        + "不改变上面的开关状态。";
    }
    runMain.appendChild(runDesc);
    runRow.appendChild(runMain);

    var runBtn = wbEl("button", "wb-api-btn wb-api-btn-primary",
      runtime.running ? "巡检中…" : "立即巡检");
    if (runtime.running) runBtn.disabled = true;
    runBtn.onclick = function () {
      runBtn.disabled = true;
      runBtn.textContent = "巡检中…";
      wbToast("已开始巡检，逐个组合探测中，请稍候…");
      fetch("/api/model-health/run", { method: "POST", body: "{}" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          var out = res && res.result ? res.result : res;
          if (!out || out.error) {
            wbToast("巡检失败：" + ((out && out.error) || "未知错误"), true);
            return;
          }
          if (out.aborted) {
            wbToast("巡检已跳过（未改动配置）：" + (out.reason || "探测异常"), true);
            return;
          }
          var cc = out.counts || {};
          var msg = "巡检完成 · 可用 " + (cc.available || 0)
            + " / 不可用 " + (cc.unavailable || 0)
            + " / 跳过 " + (cc.transient || 0);
          if (cc.probe_defect) msg += " / 探测被拒 " + cc.probe_defect;
          if (cc.restricted) msg += " / 账号被拦截 " + cc.restricted;
          if ((out.disabled || []).length) msg += " · 新禁用 " + out.disabled.length + " 项";
          if ((out.enabled || []).length) msg += " · 新启用 " + out.enabled.length + " 项";
          if ((out.protected || []).length) msg += " · 手动禁用已跳过 " + out.protected.length + " 项";
          if ((out.authFailed || []).length) {
            msg += " · " + out.authFailed.length + " 个账号凭据失效已跳过";
          }
          if ((out.restricted || []).length) {
            msg += " · " + out.restricted.length + " 个账号被上游拦截";
          }
          wbToast(msg);
        })
        .catch(function (e) { wbToast("巡检请求失败：" + e, true); })
        .then(function () {
          runBtn.disabled = false;
          runBtn.textContent = "立即巡检";
          // 重取配置与上次结果：巡检可能改了禁用策略，账号卡片与巡检面板都要跟着更新。
          wbRefreshApi(true);
          refreshModelTags();
        });
    };
    runRow.appendChild(runBtn);

    // 「一致性自检」：与「立即巡检」分开的两个动作。
    // 巡检会**改配置**（自动禁用/启用），自检只读 —— 排查问题时不该顺带动了配置。
    var auditBtn = wbEl("button", "wb-api-btn", "一致性自检");
    auditBtn.title = "逐个账号对照「凭据」与「模型」两侧的结论，不改动任何配置";
    auditBtn.onclick = function () {
      auditBtn.disabled = true;
      auditBtn.textContent = "自检中…";
      wbToast("正在逐账号对照检测，账号多时需要一会儿…");
      fetch("/api/account-health/audit")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || d.error) {
            wbToast("自检失败：" + ((d && d.error) || "无法访问接口"), true);
            return;
          }
          wbRenderAuditBox(card, d);
          wbToast("自检完成：" + Object.keys(d.summary || {}).map(function (k) {
            return k + " " + d.summary[k];
          }).join(" · "));
        })
        .catch(function (e) { wbToast("自检请求失败：" + e, true); })
        .then(function () {
          auditBtn.disabled = false;
          auditBtn.textContent = "一致性自检";
        });
    };
    runRow.appendChild(auditBtn);
    card.appendChild(runRow);

    // ---- 6b. 凭据失效被整账号跳过的名单（有才显示）----
    // 一个账号的 token 过期会让它名下所有模型一起失败，但坏的是凭据不是模型。
    // 巡检会整账号跳过并在这里点名，而不是默默禁用掉整个模型清单。
    if (last && (last.authFailed || []).length) {
      var afRow = wbEl("div", "wb-api-row wb-api-row-stack");
      var afMain = wbEl("div", "wb-api-main");
      afMain.appendChild(wbEl("div", "wb-api-label", "凭据失效、本轮已跳过的账号"));
      afMain.appendChild(wbEl("div", "wb-api-desc",
        (last.authFailed || []).map(function (a) { return a.name || a.id; }).join("、")
        + " —— 这些账号的上游鉴权已失效，坏的是凭据而不是模型，"
        + "本轮没有为它们写入任何禁用项。重新登录后再跑一轮即可。"));
      afRow.appendChild(afMain);
      card.appendChild(afRow);
    }

    // ---- 6c. 被上游拦截的账号名单 + 「重新扫码没用」的说明（有才显示）----
    //
    // 这一块存在的唯一理由是**把「重扫没用」讲清楚**。
    // 提示条只来得及说「账号被上游拦截，暂时用不了」，用户看到 403 的第一反应
    // 通常是「重新登录一下试试」—— 而实测证明那是在白费功夫：
    // 同一个账号换一张全新凭据后仍然被拦（403/11140，响应 0.4s，请求根本没进模型），
    // 说明拦的是账号本身，不是这张凭据、也不是发的内容或调用频率。
    // 这个结论需要完整几句话才讲得清，所以它归设置页，不进提示条。
    if (last && (last.restricted || []).length) {
      var rsRow = wbEl("div", "wb-api-row wb-api-row-stack");
      var rsMain = wbEl("div", "wb-api-main");
      rsMain.appendChild(wbEl("div", "wb-api-label", "被上游拦截的账号"));
      rsMain.appendChild(wbEl("div", "wb-api-desc",
        (last.restricted || []).map(function (a) { return a.name || a.id; }).join("、")
        + " —— 这些账号登录是好的，但发出的请求被上游直接拦下（403），"
        + "所以暂时用不了。"));
      rsMain.appendChild(wbEl("div", "wb-api-desc",
        "重新扫码登录解决不了：同一个账号换上新凭据后依然被拦，"
        + "上游认的是账号本身。建议直接删除这些账号，换新的账号使用。"));
      rsRow.appendChild(rsMain);
      card.appendChild(rsRow);
    }

    // ---- 7. 最近变更明细（有才显示）----
    if (last && ((last.disabled || []).length || (last.enabled || []).length
        || (last.protected || []).length)) {
      var logRow = wbEl("div", "wb-api-row wb-api-row-stack");
      var logMain = wbEl("div", "wb-api-main");
      logMain.appendChild(wbEl("div", "wb-api-label", "上次巡检的变更"));
      logMain.appendChild(wbEl("div", "wb-api-desc",
        "自动禁用：" + ((last.disabled || []).join("、") || "无")
        + "　自动启用：" + ((last.enabled || []).join("、") || "无")));
      if ((last.protected || []).length) {
        // 这一行是「来源标记」机制的可观测证据：这些模型探测得到「可用」，
        // 但因为是你手动禁用的，巡检没有动它们。
        logMain.appendChild(wbEl("div", "wb-api-desc",
          "因手动禁用而被跳过（探测可用但未放开）："
          + (last.protected || []).join("、")));
      }
      logRow.appendChild(logMain);
      card.appendChild(logRow);
    }

    section.appendChild(card);
    host.appendChild(section);
    wbSortSettingsSections();
  }

  function injectModelHealth() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-model-health")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-model-health")) return;
      wbRenderHealthSection(host2, data);
    }

    fetch("/api/model-health", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        render(data);
      })
      .catch(function () {});
  }

  function injectApiAccess() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-api-access")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-api-access")) return;
      wbRenderApiSection(host2, data);
    }

    if (wbApiCache) { render(wbApiCache); return; }
    /* 两个接口都要：/api/api-keys 给密钥清单与总调用次数，
       /api/token-stats 的 byKey 给每把密钥的今日/累计词元消耗
       （用户 2026-10-17 需求）。
       token-stats 拉失败不影响密钥列表渲染 —— 那时 keyStats 为 {}，
       各行显示「暂无调用明细」，而不是整块不显示。 */
    fetch("/api/api-keys", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        return fetch("/api/token-stats", { cache: "no-store" })
          .then(function (r2) { return r2.ok ? r2.json() : {}; })
          .catch(function () { return {}; })
          .then(function (agg) {
            var map = {};
            ((agg && agg.byKey) || []).forEach(function (b) {
              if (b && b.keyId) map[b.keyId] = b;
            });
            data.keyStats = map;
            wbApiCache = data;
            render(data);
          });
      })
      .catch(function () {});
  }

  // ---------------------------------------------------------------------
  // 设置页：通知管理（参照 MoviePilot 的通知配置模型，首渠道：企业微信）
  // 渠道表单由后端注册表驱动（registry），后续加 Telegram/钉钉/Bark 时
  // 后端注册即可，前端自动渲染，不用改这里。
  // secret 字段：已配置时显示「已配置，留空保持不变」占位，提交空串=清除、
  // 不提交=保留；GET 接口本就不回明文。
  // ---------------------------------------------------------------------
  function wbRenderNotifySection(host, data) {
    // MoviePilot 通知 UI 同款模型（AccountSettingNotification.vue +
    // NotificationChannelCard.vue）：渠道以**卡片网格**呈现（启用徽标 +
    // 就绪状态），点击卡片弹出**配置弹窗**（启用开关 / 消息场景 / 渠道字段 /
    // 底部「测试 + 确认」）。不再是一张长表单平铺所有字段。
    var cfg = data || {};
    var registry = cfg.registry || [];
    var channels = cfg.channels || {};
    var scenes = cfg.scenes || [];

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-notify";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "通知管理");
    h2.id = "settings-notify-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-notify-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 总开关 ----
    var swRow = wbEl("div", "wb-api-row");
    var swMain = wbEl("div", "wb-api-main");
    swMain.appendChild(wbEl("div", "wb-api-label", "启用通知"));
    swMain.appendChild(wbEl("div", "wb-api-desc",
      "开启后，每日任务完成、账号余额异常等事件会推送到已启用的渠道。"
      + "各渠道可单独启停与选择消息场景；关闭总开关不影响「测试」发送。"));
    swRow.appendChild(swMain);
    var swBtn = wbEl("button",
      "wb-api-btn" + (cfg.enabled ? " wb-api-btn-on" : ""),
      cfg.enabled ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    swBtn.onclick = function () {
      swBtn.disabled = true;
      wbApiAction("/api/notify", "PUT", { enabled: !cfg.enabled },
        !cfg.enabled ? "已开启通知" : "已关闭通知")
        .then(function () {
          var old = document.getElementById("settings-notify");
          if (old) old.remove();
          injectNotify();
        });
    };
    swRow.appendChild(swBtn);
    card.appendChild(swRow);

    // ---- 渠道卡片网格（MoviePilot NotificationChannelCard 同款）----
    // ⚠️ 间距修正（2026-10-13 用户反馈「配置框太靠近边框」）：
    // grid 直接做在 .wb-api-card 里，而该 card 是 border-radius:16px + overflow:hidden
    // 的容器，子元素不留内边距就会贴边（视觉上顶到边框）。
    // 参照同页其它区块的做法，网格自带四周内边距 + 与上方的间隔。
    var grid = wbEl("div", null);
    grid.style.cssText =
      "display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));" +
      "gap:10px;padding:14px 16px 16px;";
    registry.forEach(function (ch) {
      var view = channels[ch.key] || {};
      var on = view._enabled !== false;
      var chCard = wbEl("div", null);
      chCard.style.cssText =
        "cursor:pointer;border:1px solid var(--border,#e2e8f0);border-radius:10px;" +
        "padding:12px 14px;display:flex;flex-direction:column;gap:6px;min-width:0;" +
        "background:var(--card,#fff);transition:box-shadow .15s;";
      chCard.onmouseenter = function () { chCard.style.boxShadow = "0 2px 10px rgba(0,0,0,.12)"; };
      chCard.onmouseleave = function () { chCard.style.boxShadow = ""; };

      var titleRow = wbEl("div", null);
      titleRow.style.cssText = "display:flex;align-items:center;gap:7px;min-width:0";
      // 启用徽标（MP 的 VBadge dot）
      var dot = wbEl("span", null);
      dot.style.cssText = "width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:" +
        (on ? "#16a34a" : "#94a3b8");
      titleRow.appendChild(dot);
      titleRow.appendChild(wbEl("span", "wb-api-label", ch.name));
      // 就绪徽标
      var ready = wbEl("span", "wb-api-badge",
        view._ready ? "已就绪" : "未配置");
      ready.style.cssText += ";color:" + (view._ready ? "#047857" : "#b45309") +
        ";border:1px solid currentColor;background:transparent;flex:0 0 auto;";
      titleRow.appendChild(ready);
      chCard.appendChild(titleRow);

      var desc = wbEl("div", "wb-api-desc", (view._scenes && view._scenes.length)
        ? "消息场景：" + view._scenes.length + " 项"
        : "消息场景：全部");
      desc.style.cssText = ";white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
      chCard.appendChild(desc);
      chCard.appendChild(wbEl("div", "wb-api-desc",
        "点击配置（启用 / 场景 / 凭据 / 测试）"));

      chCard.onclick = function () { wbOpenNotifyDialog(ch, view, scenes); };
      grid.appendChild(chCard);
    });
    card.appendChild(grid);

    section.appendChild(card);
    host.appendChild(section);
    wbSortSettingsSections();
  }

  function wbOpenNotifyDialog(ch, view, scenes) {
    // MoviePilot NotificationChannelInfoDialog 的精简等价物：
    // 弹窗内含 启用开关 / 消息场景多选 / 渠道字段 / 底部「测试 + 确认」。
    var old = document.getElementById("wb-notify-dialog");
    if (old) old.remove();

    var overlay = wbEl("div", null);
    overlay.id = "wb-notify-dialog";
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:1200;" +
      "display:flex;align-items:center;justify-content:center;padding:16px;";
    // 点背板关闭（MP 的 VDialogCloseBtn 同效）
    overlay.onclick = function (e) { if (e.target === overlay) overlay.remove(); };

    var dlg = wbEl("div", null);
    dlg.style.cssText =
      "background:var(--card,#fff);border-radius:14px;max-width:640px;width:100%;" +
      "max-height:86vh;overflow-y:auto;box-shadow:0 10px 40px rgba(0,0,0,.25);" +
      "color:var(--foreground,#0f172a);padding:16px 18px;";
    overlay.appendChild(dlg);

    // ---- 头部：配置 + 渠道名 + 关闭 ----
    var head = wbEl("div", null);
    head.style.cssText = "display:flex;align-items:flex-start;justify-content:space-between;gap:8px;";
    var headTxt = wbEl("div", null);
    headTxt.appendChild(wbEl("div", "wb-api-label", "配置"));
    headTxt.appendChild(wbEl("div", "wb-api-desc", ch.name + "（" + (view._ready ? "已就绪" : "未配置") + "）"));
    head.appendChild(headTxt);
    var closeBtn = wbEl("button", "wb-api-btn", "关闭");
    closeBtn.onclick = function () { overlay.remove(); };
    head.appendChild(closeBtn);
    dlg.appendChild(head);

    // ---- 启用开关（每渠道独立，MP 的 NotificationConf.enabled）----
    var on = view._enabled !== false;
    var enRow = wbEl("div", "wb-api-row");
    enRow.style.marginTop = "8px";
    enRow.appendChild(wbEl("div", "wb-api-main",
      (function () { var m = wbEl("div", "wb-api-main");
        m.appendChild(wbEl("div", "wb-api-label", "启用该渠道"));
        m.appendChild(wbEl("div", "wb-api-desc",
          "关闭后事件推送跳过此渠道；「测试」发送不受影响。"));
        return m; })()));
    var enBtn = wbEl("button",
      "wb-api-btn" + (on ? " wb-api-btn-on" : ""),
      on ? "已启用 · 点击停用" : "已停用 · 点击启用");
    var enState = { on: on };
    enBtn.onclick = function () {
      enState.on = !enState.on;
      enBtn.className = "wb-api-btn" + (enState.on ? " wb-api-btn-on" : "");
      enBtn.textContent = enState.on ? "已启用 · 点击停用" : "已停用 · 点击启用";
    };
    enRow.appendChild(enBtn);
    dlg.appendChild(enRow);

    // ---- 消息场景（MP 的 switchs；全不勾 = 全部场景）----
    var sceneState = {};
    var scCard = wbEl("div", "wb-api-card");
    scCard.style.cssText = ";margin-top:10px;padding:10px 12px;border:1px solid var(--border,#e2e8f0);";
    var scMain = wbEl("div", "wb-api-main");
    scMain.appendChild(wbEl("div", "wb-api-label", "消息场景"));
    scMain.appendChild(wbEl("div", "wb-api-desc",
      "选择该渠道接收哪些事件（MoviePilot 的消息类型同款语义）；全不勾 = 接收全部。"));
    scCard.appendChild(scMain);
    var scWrap = wbEl("div", null);
    scWrap.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;";
    scenes.forEach(function (sc) {
      var checked = (view._scenes || []).indexOf(sc.key) >= 0;
      sceneState[sc.key] = checked;
      var tag = wbEl("button",
        "wb-am-tag wb-am-tag-click" + (checked ? " wb-am-tag-used" : ""), sc.name);
      tag.type = "button";
      // 场景说明：标题+说明+开关三件套（用户 2026-10-13 要求「做好标题，说明和启用开关」）。
      // 标签上只放名称（保持紧凑），说明用 tooltip 承载 —— 鼠标悬停即可看到，
      // 不撑大弹窗。
      if (sc.desc) tag.title = sc.name + "：" + sc.desc;
      tag.onclick = function () {
        sceneState[sc.key] = !sceneState[sc.key];
        tag.className = "wb-am-tag wb-am-tag-click" +
          (sceneState[sc.key] ? " wb-am-tag-used" : "");
      };
      scWrap.appendChild(tag);
    });
    scCard.appendChild(scWrap);
    dlg.appendChild(scCard);

    // ---- 渠道字段（注册表驱动）----
    var inputs = {};
    var formWrap = wbEl("div", null);
    formWrap.style.cssText =
      "display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-top:10px;";
    (ch.fields || []).forEach(function (f) {
      var cell = wbEl("div", null);
      cell.style.cssText = "display:flex;flex-direction:column;gap:4px;min-width:0";
      cell.appendChild(wbEl("span", "wb-api-badge", f.label));
      var inp = document.createElement("input");
      inp.className = "wb-api-input wb-api-mono";
      inp.type = "text";
      inp.placeholder = f.placeholder || "";
      if (f.secret) {
        var has = (view[f.key] || {}).has_value;
        inp.placeholder = has ? "已配置，留空保持不变" : (f.placeholder || "");
        inp.dataset.secret = "1";
      } else {
        inp.value = view[f.key] || "";
      }
      inputs[f.key] = inp;
      cell.appendChild(inp);
      formWrap.appendChild(cell);
    });
    dlg.appendChild(formWrap);

    function collectPayload() {
      var chPayload = {
        _enabled: enState.on,
        _scenes: Object.keys(sceneState).filter(function (k) { return sceneState[k]; })
      };
      (ch.fields || []).forEach(function (f) {
        if (f.secret) {
          var v = inputs[f.key].value.trim();
          if (v) chPayload[f.key] = v;      // 留空 = 不提交 = 保留原值
        } else {
          chPayload[f.key] = inputs[f.key].value.trim();
        }
      });
      return { channels: {} } && (function () {
        var p = { channels: {} }; p.channels[ch.key] = chPayload; return p;
      })();
    }

    // ---- 底部操作：测试 + 确认（MP 的 VCardActions 同位）----
    var actions = wbEl("div", null);
    actions.style.cssText =
      "display:flex;gap:8px;margin-top:14px;justify-content:flex-end;flex-wrap:wrap;";
    var testBtn = wbEl("button", "wb-api-btn", "测试");
    testBtn.onclick = function () {
      testBtn.disabled = true;
      testBtn.textContent = "发送中…";
      fetch("/api/notify/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: ch.key, save: true, config: collectPayload() })
      })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          testBtn.disabled = false;
          testBtn.textContent = "测试";
          if (d && d.ok) {
            wbToast(ch.name + " 测试消息已发送", "ok");
          } else {
            wbToast("测试失败：" + ((d && d.error) || "未知原因"), "err");
          }
        })
        .catch(function () {
          testBtn.disabled = false;
          testBtn.textContent = "测试";
          wbToast("请求失败，请重试", "err");
        });
    };
    var okBtn = wbEl("button", "wb-api-btn wb-api-btn-primary", "确认");
    okBtn.onclick = function () {
      okBtn.disabled = true;
      wbApiAction("/api/notify", "PUT", collectPayload(), "已保存 " + ch.name + " 配置")
        .then(function () {
          overlay.remove();
          var oldSec = document.getElementById("settings-notify");
          if (oldSec) oldSec.remove();
          injectNotify();
        });
    };
    actions.appendChild(testBtn);
    actions.appendChild(okBtn);
    dlg.appendChild(actions);

    document.body.appendChild(overlay);
  }

  // ---------------------------------------------------------------------
  // 设置页：智能助手模型配置（第三阶段）
  //
  // 用户需求（2026-10-14）：「在设置页面添加智能助手模型配置，而不是你自己写，
  // 参考 MoviePilot 的相关配置，默认自带模型提供，也可以配置其他模型」。
  //
  // 对齐 MoviePilot 的 LLMProviderInfo 模型：
  //   - provider 注册表驱动（后端 PROVIDERS，前端自动渲染，加 provider 不用改前端）
  //   - base_url_presets：预设地址下拉，免手打
  //   - base_url_editable：允许自定义地址
  //   - secret 字段：GET 不回明文，占位提示「已配置，留空保持不变」
  //   - supports_model_refresh：模型清单可刷新
  //   - 底部「测试连通性 + 保存」（MP 的 LLMTestResult 同款语义）
  //
  // 与通知渠道的差异：这里是**内联表单**（不是弹窗）—— 助手配置字段少、
  // 且是「一页一处」的设置项，内联比弹窗更直观、少一次点击。
  // ---------------------------------------------------------------------
  function wbRenderAssistantSection(host, data) {
    var reg = data.registry || [];
    var settingsReg = data.settings || [];
    var tavilyReady = !!(data.tavily && data.tavily.has_value);
    var cfg = data.config || {};
    var provs = cfg.providers || {};
    // 联网搜索引擎（2026-10-16）：注册表 + 每个引擎的当前配置。
    // searchCfg[key].api_key 是 {has_value: bool}，绝不含明文。
    var searchReg = data.searchRegistry || [];
    var searchCfg = cfg.searchProviders || {};
    var curKey = cfg.provider || (reg[0] && reg[0].key) || "gateway";
    var cur = reg.filter(function (p) { return p.key === curKey; })[0] || reg[0] || {};

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-assistant";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "智能助手");
    h2.id = "settings-assistant-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-assistant-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 1. 总开关 ----
    var swRow = wbEl("div", "wb-api-row");
    var swMain = wbEl("div", "wb-api-main");
    swMain.appendChild(wbEl("div", "wb-api-label", "启用智能助手"));
    swMain.appendChild(wbEl("div", "wb-api-desc",
      "开启后，企微 / Telegram 里未命中命令的自然语言会交给模型回答"
      + "（实时查积分、账号、模型、任务进度）。命令始终优先，助手只做兜底。"));
    swRow.appendChild(swMain);
    var swBtn = wbEl("button",
      "wb-api-btn" + (cfg.enabled ? " wb-api-btn-on" : ""),
      cfg.enabled ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    swBtn.onclick = function () {
      swBtn.disabled = true;
      wbApiAction("/api/assistant", "PUT", { enabled: !cfg.enabled },
        !cfg.enabled ? "已开启智能助手" : "已关闭智能助手")
        .then(function () {
          var old = document.getElementById("settings-assistant");
          if (old) old.remove();
          injectAssistant();
        });
    };
    swRow.appendChild(swBtn);
    card.appendChild(swRow);

    // ---- 2. Provider 选择（注册表驱动）----
    var provRow = wbEl("div", "wb-api-row");
    var provMain = wbEl("div", "wb-api-main");
    provMain.appendChild(wbEl("div", "wb-api-label", "模型提供方"));
    provMain.appendChild(wbEl("div", "wb-api-desc",
      "默认用本网关自带的账号池（无需额外密钥）；也可以配任意 OpenAI 兼容接口。"));
    provRow.appendChild(provMain);
    var provWrap = wbEl("div", null);
    provWrap.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;";
    // 选中态：provider 标签只是「选择要编辑哪一组字段」，**不是提交动作**。
    // 2026-10-15 用户反馈「切换又没保存，提示我干嘛」—— 原先切换即落盘
    // 还会弹「没填 API Key」，既制造半成品状态又是噪音。
    // 现在：切换只改本地选中态 + 重渲染表单，只有「保存」才提交。
    var selKey = curKey;
    reg.forEach(function (p) {
      var on = p.key === selKey;
      var tag = wbEl("button",
        "wb-am-tag wb-am-tag-click" + (on ? " wb-am-tag-used" : ""), p.name);
      tag.type = "button";
      tag.title = p.desc || p.name;
      tag.onclick = function () {
        if (selKey === p.key) return;      // 已是选中态，不重复渲染
        selKey = p.key;
        // 只重渲染「字段表单」区（保留页面位置，不整页刷新）
        var box = document.getElementById("wb-asst-fields");
        if (box) {
          box.innerHTML = "";
          renderFields(box, p);
        }
        // 更新标签高亮
        Array.prototype.forEach.call(provWrap.children, function (el, i) {
          var k = reg[i] && reg[i].key;
          el.className = "wb-am-tag wb-am-tag-click" + (k === selKey ? " wb-am-tag-used" : "");
        });
        // 提示策略（2026-10-15 用户反馈「切换又没保存，提示我干嘛」）：
        // 切换本身是**选编辑目标**，不该每次都弹窗说教。只有当用户已经
        // 动了表单（有未保存草稿）时，才提醒一句「记得保存」——
        // 空手切换不打扰。
        var touched = !!(draft[p.key] && Object.keys(draft[p.key]).length);
        if (touched) {
          wbToast("已切换到「" + p.name + "」，有未保存的修改，记得点「保存」", "warn");
        }
      };
      provWrap.appendChild(tag);
    });
    provRow.appendChild(provWrap);
    card.appendChild(provRow);

    // 字段表单容器：切 provider 时只重渲染它，不整页刷新。
    var fieldsBox = wbEl("div", null);
    fieldsBox.id = "wb-asst-fields";
    card.appendChild(fieldsBox);

    // ---- 3. 字段表单（按注册表渲染：地址 / 密钥 / 模型 / 提示词）----
    // ---- 3. 字段表单（可重渲染：切 provider 只刷这一块）----
    // 字段状态提升到外层：切 provider 只换「要编辑哪一组」，**已填内容要留着**。
    // 2026-10-15 用户反馈「切换又没保存」暴露的缺陷：原实现 state 在
    // renderFields 内，切走再切回会把用户刚填的地址/密钥全丢掉。
    var state = {
      base_url: "",
      api_key: "",
      model: cfg.model || "",
      systemPrompt: cfg.systemPrompt || "",
      // 照 MoviePilot 智能助手设置补齐的通用项
      maxContextK: cfg.maxContextK !== undefined ? cfg.maxContextK : 256,
      temperature: cfg.temperature !== undefined ? cfg.temperature : "",
      reasoningEffort: cfg.reasoningEffort || "auto",
      outputLanguage: cfg.outputLanguage || "zh-CN",
      userAgent: cfg.userAgent || "",
      verbose: !!cfg.verbose,
      webSearch: cfg.webSearch || "off",
      // 联网搜索引擎选择（2026-10-16）
      searchProvider: cfg.searchProvider || "tavily"
    };
    // 每个 provider 各自记住「本次编辑中的草稿」，切回来时恢复。
    var draft = {};

    // 搜索引擎的编辑草稿：key -> {base_url, api_key, extra:{}}。
    // api_key 故意**不预填**（后端不回明文），只有用户真输入了才提交，
    // 这样「不提交 = 保留原密钥」的语义天然成立。
    var searchDraft = {};
    // 用户显式点了「清除密钥」的引擎集合 —— 只有命中才提交空串，
    // 否则「不提交」就等于保留，与后端的 secret 语义对齐。
    var searchClearKey = {};
    (searchReg.length ? searchReg : []).forEach(function (sp) {
      var slot = searchCfg[sp.key] || {};
      var extra = {};
      (sp.extra_fields || []).forEach(function (ef) {
        extra[ef.key] = (slot.extra && slot.extra[ef.key]) || "";
      });
      searchDraft[sp.key] = {
        base_url: slot.base_url || sp.default_base_url || "",
        api_key: "",
        extra: extra
      };
    });

    // ---- 4. 操作：测试连通性 + 保存 ----
    // ⚠️ actRow/actMain/actBox 必须声明在**外层作用域**：
    // 原先写在 renderFields 内部（var 只提升到 renderFields），而 testBtn/
    // saveBtn 是在 renderFields 之外 append 的，一执行就抛
    // "ReferenceError: actBox is not defined"，整个助手区块被
    // .catch() 静默吞掉 —— 同样表现为「设置页没有智能助手」。
    var actRow = wbEl("div", "wb-api-row");
    var actMain = wbEl("div", "wb-api-main");
    actMain.appendChild(wbEl("div", "wb-api-label", "保存与测试"));
    actMain.appendChild(wbEl("div", "wb-api-desc",
      "「测试」会用当前填的内容真实问一句，能收到回答才算配好。"));
    actRow.appendChild(actMain);
    var actBox = wbEl("div", null);
    actBox.style.cssText = "display:flex;gap:8px;";

    // ⚠️ 必须在 state/draft 初始化之后调用：var 虽提升但赋值不提升，
    // 提前调用会让 renderFields 内的 draft[prov.key] 读到 undefined 而抛
    // "Cannot read properties of undefined (reading 'gateway')"，
    // 整个助手区块被 .catch() 静默吞掉 —— 表现为「设置页没有智能助手」。
    renderFields(fieldsBox, cur);

    function renderFields(box, prov) {
      var slot = provs[prov.key] || {};
      var cur = prov;
      // 恢复该 provider 的草稿；没有草稿则用已保存值/默认值
      var d = draft[prov.key] || {};
      state.base_url = d.base_url !== undefined
        ? d.base_url : (slot.base_url || cur.default_base_url || "");
      state.api_key = d.api_key !== undefined ? d.api_key : "";
      state.model = d.model !== undefined ? d.model : (cfg.model || "");
      state.systemPrompt = d.systemPrompt !== undefined
        ? d.systemPrompt : (cfg.systemPrompt || "");

      function fieldRow(label, desc, control) {
        var row = wbEl("div", "wb-api-row");
        var main = wbEl("div", "wb-api-main");
        main.appendChild(wbEl("div", "wb-api-label", label));
        if (desc) main.appendChild(wbEl("div", "wb-api-desc", desc));
        row.appendChild(main);
        row.appendChild(control);
        return row;
      }

      // 3.1 接口地址：预设下拉 + 可编辑输入
      if (cur.base_url_editable !== false) {
        var presets = cur.base_url_presets || [];
        var baseBox = wbEl("div", null);
        baseBox.style.cssText = "display:flex;flex-direction:column;gap:6px;min-width:260px;";
        var baseInput = wbEl("input", "wb-api-input");
        baseInput.type = "text";
        baseInput.value = state.base_url;
        baseInput.placeholder = cur.requires_base_url
          ? "必填，如 https://api.openai.com/v1" : "留空 = 用默认地址";
        baseInput.style.cssText = "width:100%;";
        baseInput.oninput = function () {
        state.base_url = baseInput.value;
        draft[prov.key] = draft[prov.key] || {};
        draft[prov.key].base_url = baseInput.value;
      };
        baseBox.appendChild(baseInput);
        if (presets.length) {
          var preWrap = wbEl("div", null);
          preWrap.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;";
          presets.forEach(function (pre) {
            var tag = wbEl("button", "wb-am-tag wb-am-tag-click", pre.label);
            tag.type = "button";
            tag.title = pre.value;
            tag.onclick = function () {
              baseInput.value = pre.value;
              state.base_url = pre.value;
            };
            preWrap.appendChild(tag);
          });
          baseBox.appendChild(preWrap);
        }
        box.appendChild(fieldRow("接口地址（Base URL）",
          presets.length ? "点下方标签快速填充，也可直接修改。" : "OpenAI 兼容的 /v1 地址。",
          baseBox));
      }

      // 3.2 API Key（secret：已配置则占位提示，空串=清除，不提交=保留）
      if (cur.supports_api_key) {
        var hasKey = (slot.api_key || {}).has_value;
        var keyInput = wbEl("input", "wb-api-input");
        keyInput.type = "password";
        keyInput.placeholder = hasKey
          ? "已配置，留空保持不变" : (cur.api_key_hint || "填写 API Key");
        keyInput.style.cssText = "width:100%;min-width:260px;";
        keyInput.oninput = function () {
        state.api_key = keyInput.value;
        draft[prov.key] = draft[prov.key] || {};
        draft[prov.key].api_key = keyInput.value;
      };
        box.appendChild(fieldRow(cur.api_key_label || "API Key",
          cur.api_key_hint || "仅本机保存，页面不回显明文。", keyInput));
      }

      // 3.3 模型：输入框 + 刷新清单
      var modelBox = wbEl("div", null);
      modelBox.style.cssText = "display:flex;gap:6px;align-items:center;min-width:260px;";
      var modelInput = wbEl("input", "wb-api-input");
      modelInput.type = "text";
      modelInput.value = state.model;
      modelInput.placeholder = "留空 = 自动挑选（推荐）";
      modelInput.style.cssText = "flex:1;";
      modelInput.oninput = function () {
        state.model = modelInput.value;
        draft[prov.key] = draft[prov.key] || {};
        draft[prov.key].model = modelInput.value;
      };
      modelBox.appendChild(modelInput);
      if (cur.supports_model_refresh !== false) {
        var refBtn = wbEl("button", "wb-api-btn", "刷新列表");
        refBtn.onclick = function () {
          refBtn.disabled = true;
          refBtn.textContent = "获取中…";
          fetch("/api/assistant/models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              base_url: state.base_url || cur.default_base_url || "",
              api_key: state.api_key
            })
          })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              refBtn.disabled = false;
              refBtn.textContent = "刷新列表";
              if (d && d.ok && d.models) {
                // 用 datalist 提供候选，不打断手输
                var dl = document.getElementById("wb-assistant-model-list");
                if (!dl) {
                  dl = wbEl("datalist", null);
                  dl.id = "wb-assistant-model-list";
                  document.body.appendChild(dl);
                }
                dl.innerHTML = "";
                d.models.forEach(function (m) {
                  var op = wbEl("option", null);
                  op.value = m;
                  dl.appendChild(op);
                });
                modelInput.setAttribute("list", "wb-assistant-model-list");
                wbToast("已获取 " + d.count + " 个模型，可直接输入或选择", "ok");
              } else {
                wbToast("获取失败：" + ((d && d.error) || "未知原因"), "err");
              }
            })
            .catch(function () {
              refBtn.disabled = false;
              refBtn.textContent = "刷新列表";
              wbToast("请求失败，请重试", "err");
            });
        };
        modelBox.appendChild(refBtn);
      }
      box.appendChild(fieldRow("模型",
        "留空则按内置优先级自动挑选；也可手填或点「刷新列表」获取该接口的模型。",
        modelBox));

      // 3.4 系统提示词（高级，可空）
      var promptBox = wbEl("div", null);
      promptBox.style.cssText = "min-width:260px;";
      var promptInput = wbEl("textarea", "wb-api-input");
      promptInput.rows = 3;
      promptInput.placeholder = "留空 = 用内置提示词（推荐）";
      promptInput.style.cssText = "width:100%;resize:vertical;";
      promptInput.value = state.systemPrompt;
      promptInput.oninput = function () {
        state.systemPrompt = promptInput.value;
        draft[prov.key] = draft[prov.key] || {};
        draft[prov.key].systemPrompt = promptInput.value;
      };
      promptBox.appendChild(promptInput);
      box.appendChild(fieldRow("系统提示词（高级）",
        "自定义助手的身份与口径；一般无需修改。", promptBox));

      // ---- 3.5 通用设置（照 MoviePilot 智能助手设置项逐项对齐，注册表驱动）----
      settingsReg.forEach(function (f) {
        if (f.key === "enabled") return;      // 总开关已在顶部单独渲染
        // webSearch 不在通用渲染里出：它必须与下面的「搜索引擎」下拉合并成
        // **一个**控件（用户 2026-10-16：「设置页出现两个搜索引擎，取消一个，
        // 用哪个配置哪个」）。合并后的下拉带 off 选项，放在 3.6 块里渲染。
        if (f.key === "webSearch") return;
        var box2 = wbEl("div", null);
        box2.style.cssText = "min-width:260px;";
        if (f.type === "bool") {
          var bBtn = wbEl("button",
            "wb-api-btn" + (state[f.key] ? " wb-api-btn-on" : ""),
            state[f.key] ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
          bBtn.onclick = function () {
            state[f.key] = !state[f.key];
            bBtn.className = "wb-api-btn" + (state[f.key] ? " wb-api-btn-on" : "");
            bBtn.textContent = state[f.key] ? "已开启 · 点击关闭" : "已关闭 · 点击开启";
          };
          box2.appendChild(bBtn);
        } else if (f.type === "select") {
          var sSel = wbEl("select", "wb-api-input");
          (f.options || []).forEach(function (o) {
            var opt = document.createElement("option");
            opt.value = o; opt.textContent = o;
            if (String(state[f.key]) === o) opt.selected = true;
            sSel.appendChild(opt);
          });
          sSel.onchange = function () { state[f.key] = sSel.value; };
          box2.appendChild(sSel);
        } else {
          var nIn = wbEl("input", "wb-api-input");
          nIn.type = f.type === "number" ? "number" : "text";
          if (f.type === "number") {
            if (f.min !== undefined) nIn.min = f.min;
            if (f.max !== undefined) nIn.max = f.max;
          }
          nIn.value = (state[f.key] === undefined || state[f.key] === null)
            ? "" : state[f.key];
          nIn.placeholder = f.type === "number" ? "留空 = 模型默认" : "";
          nIn.style.cssText = "width:100%;";
          nIn.oninput = function () { state[f.key] = nIn.value; };
          box2.appendChild(nIn);
        }
        box.appendChild(fieldRow(f.label, f.desc || "", box2));
      });

      // ---- 3.6 联网搜索引擎配置（2026-10-16）----
      // 用户诉求：「搜索引擎作为可选项，前端添加 UI 配置，用户自行配置搜索引擎，
      // 也可以用其他的，而不是把我的 key 写死到仓库里，因为这是开源的」。
      // 因此这里做成注册表驱动：引擎下拉 + 每个引擎自己的 base_url /
      // API Key / 附加字段（如 Google CSE 的 cx），全部由用户填写，
      // 只落在用户自己的配置文件里。
      var searchReg = data.searchRegistry || [];
      if (searchReg.length) {
        // ⚠️ 这里**不再**单独渲染「联网搜索引擎」小标题。
        // 用户 2026-10-16 反馈：设置页同时出现「联网搜索引擎」和「搜索引擎」
        // 两个重复项。前者就是这个 srHead，后者是下面那行 fieldRow 的标签。
        // 保留 fieldRow 的「搜索引擎」（它和同行其他行一样有左右对齐的标签+控件），
        // 删掉这个游离的小标题，说明文字改为挂到「搜索引擎」那行的 desc 上。

        // 引擎下拉
        // ⚠️ 必须包一层 min-width:260px 的盒子、让 select width:100%，
        // 与 LLM 区「接口地址」行用同一套规格（实测那行控件左=1100 右=1360 宽=260）。
        // 早期直接把 select 丢进行里 + max-width:320px，flex 下它按**内容宽度**
        // 收缩成 203px，比同卡其他行窄 57px，视觉上左右参差不齐。
        var srSel = wbEl("select", "wb-api-input");
        srSel.style.cssText = "width:100%;";
        // 合并后的唯一入口：off（关闭）+ 各引擎。
        // ⚠️ 这一个 select 同时驱动 state.webSearch 与 state.searchProvider，
        // 后端语义：webSearch="off" 表示关闭，非 off 时以 searchProvider 为准。
        // 因此 onChange 里两个状态必须同步改，否则会出现
        // 「下拉显示 bing、实际还在用 tavily」的错位。
        var offOpt = document.createElement("option");
        offOpt.value = "off";
        offOpt.textContent = "关闭（不联网检索）";
        if (String(state.webSearch) === "off") offOpt.selected = true;
        srSel.appendChild(offOpt);
        searchReg.forEach(function (sp) {
          var opt = document.createElement("option");
          opt.value = sp.key;
          opt.textContent = sp.name + (sp.supports_api_key ? "" : "（无需密钥）");
          // 选中判定：webSearch 非 off 时看 searchProvider
          if (String(state.webSearch) !== "off"
              && String(state.searchProvider) === sp.key) opt.selected = true;
          srSel.appendChild(opt);
        });
        srSel.onchange = function () {
          var v = srSel.value;
          if (v === "off") {
            state.webSearch = "off";
          } else {
            state.webSearch = v;
            state.searchProvider = v;
          }
          renderSearchFields();
        };
        var srSelRow = wbEl("div", null);
        srSelRow.style.cssText = "display:flex;flex-direction:column;gap:6px;min-width:260px;";
        srSelRow.appendChild(srSel);
        card.appendChild(fieldRow("联网搜索",
          "选一个引擎，或选「关闭」完全禁用联网检索。默认 Tavily；国内直连可选博查，"
          + "自建可选 SearXNG（无需密钥）。密钥只保存在本机配置文件中，不进仓库、不回传；"
          + "留空且已配置过 = 保持不变，明确提交空串 = 清除。", srSelRow));

        // 每个引擎自己的配置容器（切引擎时只重渲染它）
        var srFields = wbEl("div", null);
        srFields.id = "wb-asst-search-fields";
        card.appendChild(srFields);

        function renderSearchFields() {
          srFields.innerHTML = "";
          var sp = null;
          searchReg.forEach(function (x) { if (x.key === state.searchProvider) sp = x; });
          if (!sp) return;
          var slot = (searchCfg && searchCfg[sp.key]) || {};

          // 引擎说明 + 申请地址。
          // ⚠️ 必须包一层 .wb-sr-note（与 .wb-api-row 同一套左右缩进），
          // 不能直接 appendChild 到 srFields —— 那样它是 card 的直接子节点，
          // 左右各少 16/20px 缩进，实测比下面的字段行凸出一截，
          // 用户反馈「这行文字也左右未对齐」。
          if (sp.desc || sp.docs) {
            var d = wbEl("div", "wb-api-desc wb-sr-note");
            d.textContent = (sp.desc || "")
              + (sp.docs ? "　申请：" + sp.docs : "");
            srFields.appendChild(d);
          }

          // 地址
          if (sp.default_base_url) {
            var bIn = wbEl("input", "wb-api-input");
            bIn.type = "text";
            bIn.value = slot.base_url || sp.default_base_url || "";
            bIn.style.cssText = "width:100%;";
            bIn.oninput = function () { searchDraft[sp.key].base_url = bIn.value; };
            var bBox = wbEl("div", null);
            bBox.style.cssText = "min-width:260px;";
            bBox.appendChild(bIn);
            srFields.appendChild(fieldRow("接口地址", sp.default_base_url, bBox));
          }

          // API Key（需要密钥的引擎才显示）
          if (sp.supports_api_key) {
            var kIn = wbEl("input", "wb-api-input");
            kIn.type = "password";
            kIn.placeholder = (slot.api_key && slot.api_key.has_value)
              ? "已配置 · 留空表示不变，输入新值则覆盖"
              : "尚未配置 · 请填写";
            kIn.style.cssText = "width:100%;";
            kIn.oninput = function () { searchDraft[sp.key].api_key = kIn.value; };
            var kBox = wbEl("div", null);
            kBox.style.cssText = "min-width:260px;";
            kBox.appendChild(kIn);
            var kState = wbEl("div", "wb-api-desc");
            kState.textContent = (slot.api_key && slot.api_key.has_value)
              ? "当前状态：已配置"
              : "当前状态：未配置";
            kBox.appendChild(kState);
            // 「清除密钥」按钮：只在已配置时出现。点了之后本次保存会
            // **显式提交空串**（后端据此清除），而不是「不提交 = 保留」。
            if (slot.api_key && slot.api_key.has_value) {
              var kClear = wbEl("button", "wb-api-btn", "清除已保存的密钥");
              kClear.style.cssText = "margin-top:6px;";
              kClear.onclick = function () {
                if (!window.confirm("确定清除「" + sp.name + "」已保存的密钥？\n"
                    + "清除后需重新填写才能使用联网搜索。")) return;
                searchClearKey[sp.key] = true;
                searchDraft[sp.key].api_key = "";
                kIn.value = "";
                kIn.placeholder = "尚未配置 · 请填写";
                kState.textContent = "当前状态：将在保存后清除";
                kClear.disabled = true;
                kClear.textContent = "已标记清除（保存后生效）";
              };
              kBox.appendChild(kClear);
            }
            // 「测试搜索引擎」按钮：填完密钥立刻验证，不必等保存。
            // ⚠️ 必须放在这里（kBox 内，紧跟「清除已保存的密钥」之后），
            // 而不是 card 下：
            //   - card 下  = 独占一行、左右凸出，用户反馈「占满一行」
            //   - kBox 内  = 与清除按钮同一套盒子、同一套 margin-top:6px
            // 未配置密钥时也有这个按钮（清除按钮只在已配置时出现），
            // 所以用户第一次填完就能立刻测。
            kBox.appendChild(makeSrTestBtn(sp));
            srFields.appendChild(fieldRow(
              sp.api_key_label || "API Key", sp.api_key_hint || "", kBox));
          } else {
            // 不需要密钥的引擎（如 SearXNG）：没有 API Key 行可挂，
            // 就把测试按钮单独放一行，保持与其他行同样的左右缩进。
            var tBox = wbEl("div", null);
            tBox.style.cssText = "min-width:260px;";
            tBox.appendChild(makeSrTestBtn(sp));
            srFields.appendChild(fieldRow("连通性", "用当前地址发一次探针检索，验证配置是否正确。", tBox));
          }

          // 附加字段（如 Google CSE 的 cx）
          (sp.extra_fields || []).forEach(function (ef) {
            var eIn = wbEl("input", "wb-api-input");
            eIn.type = "text";
            eIn.value = (slot.extra && slot.extra[ef.key]) || "";
            eIn.style.cssText = "width:100%;";
            eIn.oninput = function () { searchDraft[sp.key].extra[ef.key] = eIn.value; };
            var eBox = wbEl("div", null);
            eBox.style.cssText = "min-width:260px;";
            eBox.appendChild(eIn);
            srFields.appendChild(fieldRow(ef.label, ef.desc || "", eBox));
          });
        }
        // 「测试搜索引擎」按钮工厂。
        // 走独立端点 /api/assistant/search-test（不落盘，测试无副作用）。
        function makeSrTestBtn(sp) {
          var b = wbEl("button", "wb-api-btn", "测试搜索引擎");
          b.style.cssText = "margin-top:6px;";
          b.onclick = function () {
            b.disabled = true;
            b.textContent = "测试中…";
            var probeCfg = collect();
            fetch("/api/assistant/search-test", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ config: probeCfg })
            })
              .then(function (r) { return r.json(); })
              .then(function (d) {
                b.disabled = false;
                b.textContent = "测试搜索引擎";
                if (d && d.ok) {
                  wbToast("连通正常（" + (d.durationMs || 0) + "ms）："
                    + (d.title || "已拿到结果"), "ok");
                } else {
                  wbToast("测试失败：" + ((d && d.error) || "未知原因"), "err");
                }
              })
              .catch(function () {
                b.disabled = false;
                b.textContent = "测试搜索引擎";
                wbToast("请求失败，请重试", "err");
              });
          };
          return b;
        }

        renderSearchFields();
      }
    }

    function collect() {
      // 用**当前选中**的 provider 定义（selKey），不是初始渲染时的 cur ——
      // 否则切到 openai 后保存，仍会按 gateway 的字段规则收集（漏 base_url/key）。
      var prov = reg.filter(function (x) { return x.key === selKey; })[0] || {};
      var payload = {
        provider: selKey,
        model: state.model,
        systemPrompt: state.systemPrompt,
        maxContextK: state.maxContextK,
        temperature: state.temperature,
        reasoningEffort: state.reasoningEffort,
        outputLanguage: state.outputLanguage,
        userAgent: state.userAgent,
        verbose: state.verbose,
        webSearch: state.webSearch,
        providers: {}
      };
      var pv = {};
      if (prov.base_url_editable !== false) pv.base_url = state.base_url;
      if (prov.supports_api_key && state.api_key) pv.api_key = state.api_key;
      payload.providers[selKey] = pv;

      // —— 联网搜索引擎（2026-10-16）——
      // 语义与 LLM provider 的 secret 完全一致：
      //   api_key 为空串 = 本次不提交该键（后端保留原值）
      //   只有用户真输入了才带上 = 覆盖
      //   要清除密钥需要显式传空串 —— 这里由 searchClearKey 集合标记
      payload.searchProvider = state.searchProvider;
      var spv = {};
      Object.keys(searchDraft).forEach(function (k) {
        var d = searchDraft[k] || {};
        var out = { base_url: d.base_url };
        if (d.api_key) out.api_key = d.api_key;
        else if (searchClearKey[k]) out.api_key = "";   // 明确提交空串 = 清除
        var ex = d.extra || {};
        var exOut = {};
        var hasExtra = false;
        Object.keys(ex).forEach(function (ek) {
          exOut[ek] = ex[ek]; hasExtra = true;
        });
        if (hasExtra) out.extra = exOut;
        spv[k] = out;
      });
      payload.searchProviders = spv;
      return payload;
    }

    var testBtn = wbEl("button", "wb-api-btn", "测试连通性");
    testBtn.onclick = function () {
      testBtn.disabled = true;
      testBtn.textContent = "测试中…";
      fetch("/api/assistant/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: collect() })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          testBtn.disabled = false;
          testBtn.textContent = "测试连通性";
          if (d && d.ok) {
            wbToast("连通正常（" + (d.durationMs || 0) + "ms）："
              + (d.replyPreview || "").slice(0, 60), "ok");
          } else {
            wbToast("测试失败：" + ((d && d.error) || "未知原因"), "err");
          }
        })
        .catch(function () {
          testBtn.disabled = false;
          testBtn.textContent = "测试连通性";
          wbToast("请求失败，请重试", "err");
        });
    };
    actBox.appendChild(testBtn);

    var saveBtn = wbEl("button", "wb-api-btn wb-api-btn-primary", "保存");
    saveBtn.onclick = function () {
      saveBtn.disabled = true;
      wbApiAction("/api/assistant", "PUT", collect(), "已保存智能助手配置")
        .then(function () {
          var old = document.getElementById("settings-assistant");
          if (old) old.remove();
          injectAssistant();
        });
    };
    actBox.appendChild(saveBtn);
    actRow.appendChild(actBox);
    card.appendChild(actRow);

    section.appendChild(card);
    host.appendChild(section);
    wbSortSettingsSections();
  }

  function injectAssistant() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-assistant")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-assistant")) return;
      wbRenderAssistantSection(host2, data);
    }

    fetch("/api/assistant", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        render(data);
      })
      .catch(function () {});
  }

  function injectNotify() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-notify")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-notify")) return;
      wbRenderNotifySection(host2, data);
    }

    fetch("/api/notify", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        render(data);
      })
      .catch(function () {});
  }

  // ---------------------------------------------------------------------
  // 设置页：关于
  // 放在设置页最底部。这是自建容器，用户需要一处能回答「我装的是哪个版本、
  // 账号长什么样、出问题去哪里看」的地方 —— 否则每次都得回去翻 README。
  // 项目地址与项目名由网关下发（可用环境变量覆盖），fork 出去的人不会把使用者
  // 引回上游作者的项目。
  //
  // 版本号来自网关常量，**与 GitHub Release 标签 / 镜像 tag 同号**
  // （曾经是两套号：面板 v1.8.0、发布页 v0.4.4，看的人根本没法判断自己是不是最新）。
  // ---------------------------------------------------------------------
  var wbInfoCache = null;

  function wbRenderAboutSection(host, data) {
    var proj = data.project || {};
    var models = data.models || {};
    var accounts = data.accounts || {};
    var ports = data.ports || {};
    var bySource = data.disabledBySource || {};

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-about";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "关于");
    h2.id = "settings-about-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-about-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 1. 项目名 + 版本 ----
    var nameRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var nameMain = wbEl("div", "wb-api-main");
    var headLine = wbEl("div", "wb-about-head");
    headLine.appendChild(wbEl("span", "wb-about-name", proj.name || "AutoBuddy"));
    var versionBadge = wbEl("span", "wb-api-badge wb-api-badge-ok", "v" + (data.version || "?"));
    versionBadge.title = "与 GitHub 发布标签、镜像 tag 同号";
    headLine.appendChild(versionBadge);
    nameMain.appendChild(headLine);
    nameMain.appendChild(wbEl("div", "wb-api-desc",
      "把 WorkBuddy / CodeBuddy 账号池变成标准 OpenAI 兼容网关：账号自动轮询、"
      + "模型级可用性自愈，容器内自带这个控制台。"));
    var linkRow = wbEl("div", "wb-about-links");
    [
      ["项目主页", proj.url],
      ["更新日志", proj.changelog],
      ["发布版本", proj.releases],
      ["问题反馈", proj.issues],
    ].forEach(function (item) {
      if (!item[1]) return;
      var a = wbEl("a", "wb-about-link", item[0]);
      a.href = item[1];
      a.target = "_blank";
      a.rel = "noreferrer noopener";
      linkRow.appendChild(a);
    });
    nameMain.appendChild(linkRow);
    nameRow.appendChild(nameMain);
    card.appendChild(nameRow);

    // ---- 2. 运行位置 ----
    // 只要「数据目录 + 两个入口地址」。镜像名不在这里重复：升级方式属于文档，
    // 而且版本号已经在上面的徽章里了，再列一次镜像名只会让同一件事出现两遍。
    var runRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var runMain = wbEl("div", "wb-api-main");
    runMain.appendChild(wbEl("div", "wb-api-label", "运行位置"));

    var scheme = window.location.protocol === "https:" ? "https:" : "http:";
    // 注意别用 `host` 这个名字：本函数的参数就叫 host（要往里挂节点的容器），
    // `var` 会把它整个顶掉，函数末尾 host.appendChild 就抛异常、整块面板静默消失。
    var hostName = window.location.hostname || "localhost";
    var consolePort = ports.console || 18090;
    var gatewayPort = ports.gateway || 18091;

    var grid = wbEl("div", "wb-about-grid");
    function addRow(key, value, mono, copyValue) {
      grid.appendChild(wbEl("span", "wb-about-k", key));
      var cell = wbEl("div", "wb-about-v");
      cell.appendChild(wbEl("span", mono ? "wb-api-mono" : null, value));
      if (copyValue) {
        var btn = wbEl("button", "wb-api-btn", "复制");
        btn.onclick = function () { wbCopy(copyValue, "已复制" + key); };
        cell.appendChild(btn);
      }
      grid.appendChild(cell);
    }
    var dir = data.dataDir || "";
    addRow("数据目录", dir || "—", true, dir);
    addRow("控制台", scheme + "//" + hostName + ":" + consolePort, false, null);
    addRow("网关 API", scheme + "//" + hostName + ":" + gatewayPort + "/v1", false, null);
    runMain.appendChild(grid);
    runMain.appendChild(wbEl("div", "wb-api-desc",
      "浏览器打开「控制台」；程序调用填「网关 API」。升级不会动数据目录里的"
      + "账号、密钥与模型策略配置。"));
    runRow.appendChild(runMain);
    card.appendChild(runRow);

    // ---- 3. 当前规模 ----
    var scaleRow = wbEl("div", "wb-api-row");
    var scaleMain = wbEl("div", "wb-api-main");
    scaleMain.appendChild(wbEl("div", "wb-api-label", "当前规模"));
    scaleMain.appendChild(wbEl("div", "wb-api-desc",
      "账号 " + (accounts.total || 0) + " 个（可用 " + (accounts.usable || 0)
      + "，参与调用 " + (accounts.inPool || 0) + "）"
      + " · 模型 " + (models.count || 0) + " 个"
      + " · 禁用组合 " + (models.disabledCombos || 0) + " 项"
      + "（手动 " + (bySource.manual || 0) + " / 巡检 " + (bySource.auto || 0) + "）"));
    scaleRow.appendChild(scaleMain);
    card.appendChild(scaleRow);

    // ---- 4. 免责说明 ----
    // 2026-10-22 用户要求：把「关于」页里的「说明」这一项及其文本整个去掉，
    // 别再显示。整段删除（而不是藏起来）—— 留着 DOM 只是浪费，
    // 而且以后有人改样式时会被这个隐藏节点绊到。
    // ⚠️ 这里不再往 card 里 appendChild noteRow，「关于」卡片到此为止
    //    （项目名+版本 / 运行位置 / 当前规模 三块）。

    section.appendChild(card);
    host.appendChild(section);
    wbSortSettingsSections();
  }

  function injectAbout() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-about")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-about")) return;
      wbRenderAboutSection(host2, data);
    }

    if (wbInfoCache) { render(wbInfoCache); return; }
    fetch("/api/gateway-info", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        wbInfoCache = data;
        render(data);
      })
      .catch(function () {});
  }

  // 设置页各区块的**固定**顺序。
  // 原先每个 inject* 都是「fetch 一回来就 appendChild」，谁的网络先返回谁就排在
  // 前面 —— 于是「API接入」和「模型巡检」会随机互换位置，用户看到的现象就是
  // 「设置页模块顺序偶发随机、要刷新才正常」。
  // 这里在每次挂载后按这张表统一重排，结果与网络时序完全无关。
  // 顺序与 run() 的调用序列一致：API接入 → 巡检 → 助手 → 通知 → 账号 → 关于。
  // 2026-10-16 用户要求：「账号资料应该放在顶部」。
  // 原顺序是 外观→API→巡检→助手→通知→账号→关于，账号资料排在倒数第二，
  // 每次要滚过半页才看到。现在提到最前。
  var WB_SETTINGS_ORDER = [
    "settings-profile",
    "settings-api-access",
    "settings-model-health",
    "settings-assistant",
    "settings-notify",
    "settings-about"
  ];

  function wbSortSettingsSections() {
    // 以「装着最多注入区块」的容器为宿主，**不要**拿 WB_SETTINGS_ORDER[0] 反推 ——
    // 官方页面把「外观」单独包在 DIV.px-1 里，拿它反推会得到错误容器，
    // 导致下面的 want 只有 1 个节点、判定「已有序」而什么都没排。
    var host = null, hostCount = 0;
    for (var i = 0; i < WB_SETTINGS_ORDER.length; i++) {
      var el0 = document.getElementById(WB_SETTINGS_ORDER[i]);
      if (!el0 || !el0.parentNode) continue;
      var p = el0.parentNode;
      var cnt = 0;
      var kids0 = p.children;
      for (var k = 0; k < kids0.length; k++) {
        if (WB_SETTINGS_ORDER.indexOf(kids0[k].id) >= 0) cnt++;
      }
      if (cnt > hostCount) { hostCount = cnt; host = p; }
    }
    // 至少要有 2 个注入区块才有排序意义；否则说明页面结构不对，安全退出。
    if (!host || hostCount < 2) return;

    var want = [];
    for (var j = 0; j < WB_SETTINGS_ORDER.length; j++) {
      var e2 = document.getElementById(WB_SETTINGS_ORDER[j]);
      if (e2 && e2.parentNode === host) want.push(e2);
    }
    if (!want.length) return;

    // 只比较「本表内的区块」，忽略容器里其他原生子节点（如官方无 id 的「外观」段）。
    var settingsKids = Array.prototype.slice.call(host.children).filter(function (el) {
      return WB_SETTINGS_ORDER.indexOf(el.id) >= 0;
    });
    if (settingsKids.length !== want.length) return;

    // 已经有序就**一个节点都不动** —— 这是避免 appendChild 产生新的 DOM 变更、
    // 被 MutationObserver 接住后打成死循环的关键。
    for (var m = 0; m < want.length; m++) {
      if (settingsKids[m] !== want[m]) {
        for (var n = 0; n < want.length; n++) host.appendChild(want[n]);
        return;
      }
    }
  }

  function wbPinAboutLast() {
    // 「关于」必须是设置页的最后一段。各面板插入顺序取决于 fetch 回来的先后，
    // 谁先到谁先 append，光靠在 run() 里排调用顺序保证不了。
    // 现在统一由 wbSortSettingsSections() 处理，这里保留仅为兼容旧调用点。
    wbSortSettingsSections();
  }


  // ---------------- 侧边栏「每日任务」入口与专属视图 ----------------
  function wbFmtTs(ts) {
    if (!ts) return "—";
    try { return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }); } catch (e) { return String(ts); }
  }

  function injectWbDaily() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    var nav = aside.querySelector("nav");
    if (!nav) return;

    var navLink = document.getElementById("wb-nav-wb-daily");
    if (!navLink) {
      navLink = document.createElement("a");
      navLink.id = "wb-nav-wb-daily";
      navLink.href = "#/wb-daily";
      navLink.className = "flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm outline-none transition-colors text-muted-foreground hover:bg-foreground/[0.04] hover:text-foreground cursor-pointer";
      navLink.innerHTML = '<svg class="size-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line><path d="M9 16l2 2 4-4"></path></svg><span class="wb-nav-label">每日任务</span>';
      // 侧边栏收纳后只剩图标，必须补 data-wb-label ——
      // 收拢态的悬浮气泡靠 CSS 的 content: attr(data-wb-label) 取值；
      // 官方导航项由 wrapNavLabels 统一设置，而每日任务是后来注入的，
      // 没设这个属性就会导致收纳后悬停不出现任何提示。
      navLink.setAttribute("data-wb-label", "每日任务");

      var settingsA = nav.querySelector('a[href="/settings"]') || nav.lastElementChild;
      if (settingsA) {
        nav.insertBefore(navLink, settingsA);
      } else {
        nav.appendChild(navLink);
      }

      navLink.addEventListener("click", function(e) {
        e.preventDefault();
        wbPushUrl(window.location.pathname + "#/wb-daily");
      });

      nav.querySelectorAll("a:not(#wb-nav-wb-daily)").forEach(function(a) {
        a.addEventListener("click", function() {
          /* 与概览同一套逻辑：点别的选项卡时把视图 hash 后缀清掉。
             ⚠️ 不能再写 window.location.hash = "" —— 那会触发一次
             hashchange，把「已经 pushState 过去的官方路由」又冲掉。 */
          if (WB_VIEW_HASHES.indexOf(wbViewHash()) >= 0) {
            window.history.replaceState(null, "",
              window.location.pathname + window.location.search);
          }
        });
      });
    }
  }

  /* 每日任务视图的数据新鲜度：切换回本页时若数据仍在 TTL 内就不重拉，
     避免每次切换都重渲染造成闪屏（实测切换瞬间 DOM mutation 上百次）。 */
  var __wbDailyLoadedAt = 0;
  var WB_DAILY_DATA_TTL_MS = 30000;

  /* 兼容旧名：内部/历史调用点仍在用 wbUpdateWbDailyView。
     视图切换已统一到 wbApplyMainView()（见概览节），这里只做委托，
     不再自己判断 hash —— 否则它和 wbApplyMainView 会在 hashchange 上
     互相恢复对方刚隐藏的节点（2026-10-16 实测踩过：概览页切走后
     #wb-overview-view 仍是 block，官方内容也同时显示）。 */
  function wbUpdateWbDailyView() {
    wbApplyMainView();
    wbSyncNavActive();
  }

  // 三个旧监听（wbUpdateWbDailyView / wbUpdateOverviewView / wbSyncNavActive）
  // 收敛成一个：那两个函数现在都是 wbApplyMainView() + wbSyncNavActive() 的
  // 薄封装，分别挂会重复跑两遍。留一个即可。
  window.addEventListener("hashchange", wbUpdateOverviewView);

  /* ⚠️ 2026-10-17 修复：官方导航是 React Router 的 pushState 跳转，
     **不触发 hashchange**。数据总览占用默认主页（"/"）之后，
     从数据总览点「词元统计」/「系统设置」这类官方页时，URL 变了但
     没有任何事件通知我们 —— 实测页面仍显示数据总览，官方内容被盖住。
     原先只挂 hashchange 是够的（那时默认主页是账号管理，
     路径变化不涉及注入视图的显隐），现在默认主页就是注入视图，
     路径变化同样要重新应用视图状态。
     popstate 覆盖 pushState + 浏览器前进/后退两条路径。 */
  window.addEventListener("popstate", wbUpdateOverviewView);

  /* 把每日任务页每个卡片的标题「外提」为卡片外的二级标题，
     结构从 CARD>三级标题+内容 改为 SECTION>[H2, CARD(内容)]，
     与官方积分统计页一致。

     改造在 DOM 层做（而非逐个重写 HTML 字符串），是因为本页卡片
     由一长串 innerHTML 拼出，逐处手改易漏且难维护；这里统一处理，
     新增卡片也会自动被纳入。 */
  function wbHoistDailyCardTitles(view) {
    if (!view || view.dataset.wbHoisted === "1") return;
    view.dataset.wbHoisted = "1";
    var cards = view.querySelectorAll(".wb-api-card");
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      if (card.dataset.wbHoistedCard === "1") continue;
      var label = card.querySelector(".wb-api-label");
      if (!label) continue;
      card.dataset.wbHoistedCard = "1";

      var section = document.createElement("section");
      section.className = "wb-daily-section";
      var h2 = document.createElement("h2");
      h2.className = "wb-daily-h2";
      h2.textContent = (label.textContent || "").trim();
      section.appendChild(h2);
      // 卡片整体移进 section，标题留在外面
      card.parentNode.insertBefore(section, card);
      section.appendChild(card);
      // 移除卡片内原来的标题节点（副标题/描述保留在卡片内，与官方一致）
      if (label.parentNode) label.parentNode.removeChild(label);
    }
  }

  /* 布局兜底：把游离在 .wb-daily-wrap 之外的全部元素移回 wrap 里。
     用户 2026-10-17 反馈「每日任务界面部分内容左右宽度未对齐」。
     DOM 实测根因：任务清单 / 任务记录 / 执行记录 三张卡片 + 一个 row
     是 #wb-daily-view 的**直接子元素**，不在 wrap 内 ——
     实测它们 x=0 w=1409（占满视口），而 wrap 内的卡片 x=147 w=1116，
     左右各差一截，看起来就是「部分内容没对齐」。
     成因是 innerHTML 的 div 嵌套层级漏了一层闭合，加上 hoist 时
     `card.parentNode.insertBefore(section, card)` 会把 section 插到
     卡片当时的父节点上 —— 父节点是 view 时就游离到了外面。
     这里不逐层去核对 HTML 嵌套（那样一改就可能波及别的内容），
     而是在视图建好后做一次幂等归一化：view 的直接子元素里
     除 wrap 自身外，全部 appendChild 进 wrap。
     重复调用安全：元素已在 wrap 内时不会被移动。 */
  function wbNormalizeDailyLayout(view) {
    if (!view) return;
    var wrap = view.querySelector(".wb-daily-wrap");
    if (!wrap) return;
    Array.prototype.slice.call(view.children).forEach(function (c) {
      if (c !== wrap) wrap.appendChild(c);
    });
  }

  function wbCreateWbDailyView() {
    var v = document.createElement("div");
    v.id = "wb-daily-view";
    v.style.display = "none";
    v.innerHTML =
      // 容器与标题对齐官方页面（实测 credit-stats / 账号管理的 computed style）：
      //   max-width 1180px + px-4 py-6（≥640px 时 sm:px-8 sm:py-9，即 32px/36px）
      //   H1 = 28px / 600 / line-height 42px（原为自造的 20px / 700 / 30px）
      '<div class="wb-daily-wrap">' +
        '<div class="wb-daily-head">' +
          '<div>' +
            '<h1 class="wb-daily-h1">每日任务</h1>' +
            '<p class="wb-daily-sub">WorkBuddy 成长中心自动化：每日签到、旅行、抽奖、盲盒、Buddy、连签兑换、补签卡、礼包补偿、徽章共 8 类任务，附积分/用量/成长查询与自动领奖（内置 WorkBuddy-Daily v3.7 脚本，账号池国内版账号只读共用凭据）。</p>' +
          '</div>' +
        '</div>' +

        /* 积分统计：四项数据**单独成为二级框**，不再套外层卡片
           （此前是「卡片里再套一个卡片」的框中框结构）。
           同时取消二级标题「积分统计」与其下的说明文案、以及右侧「刷新」按钮
           —— 刷新由定时拉取自动完成。 */
        '<div id="wb-dl-credit-box" class="wb-dl-credit-card"></div>' +

        /* 最近记录：独立成块（用户 2026-09-27 要求），与积分四项分开 */
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">最近记录</div>' +
              '<div class="wb-api-desc">最近 5 天的积分明细，便于核对累计来源</div>' +
            '</div>' +
          '</div>' +
          '<div id="wb-dl-recent" class="wb-api-row wb-api-row-stack" style="flex-direction:column;display:none"></div>' +
        '</div>' +

        /* 执行中：进度条 + 阶段明细。
           取消原先固定的「正在执行」二级标题文案 —— 这块只在任务真跑起来时
           由 wbRenderDailyProgress 显示，平时是个空壳，属于多余显示。 */
        '<div id="wb-dl-progress-card" class="wb-api-card" style="display:none">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div id="wb-dl-progress-step" class="wb-api-desc">准备中…</div>' +
            '</div>' +
            '<div id="wb-dl-progress-pct" class="wb-api-mono" style="font-size:18px;font-weight:600">0%</div>' +
          '</div>' +
          '<div style="width:100%;height:8px;border-radius:4px;background:rgba(120,120,120,.22);overflow:hidden;margin-top:8px">' +
            '<div id="wb-dl-progress-bar" style="width:0%;height:100%;background:#3b82f6;transition:width .4s ease"></div>' +
          '</div>' +
          '<div id="wb-dl-progress-accounts" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px"></div>' +
        '</div>' +

        /* ── 签到调度 / 任务调度拆分（用户 2026-10-16 需求，2026-10-22 收口）──
           「自动签到和任务调度混合，需要你在每日任务界面将其分开，用户可以分别
            配置，避免签到和任务相互影响」。
           做法：两张独立的卡片，各管自己的开关、间隔、上次/下次执行时间。
           跑的时候后端按类型给上游脚本传不同任务过滤：
             · 签到轮 → 只跑 checkin
             · 任务轮 → 跑除 checkin 以外的全部
           任一边单独停用不影响另一边；总开关关掉则两边都不自动跑。

           2026-10-22 用户再次反馈：「自动签到 / 自动执行任务的切换开关未分开，
           各自切换开关独立控制」。
           根因：页面上**三个**语义不同的开关并存，其中「启用自动签到」
           （checkin_enabled，启动时核验服务端状态、未签到账号自动补签）
           塞在「执行模式与账号保活」卡片里，与「启用签到调度」
           （checkin_schedule_enabled，定时跑签到轮）文字相近、位置分散，
           用户看到的就是「签到有两个开关、任务只有一个」—— 即「没分开」。
           收口做法：
             ① 把「启用自动签到」从执行模式卡里移出，改名为「启动补签」，
                并放进签到调度卡 —— 同一张卡里两个开关语义分层：
                  启动补签       = 容器起来时核验一次（checkin_enabled）
                  启用签到调度   = 定时跑签到轮（checkin_schedule_enabled）
             ② 任务调度卡保持「启用任务调度」一个开关（task_schedule_enabled）。
             ③ 三个字段后端**早已各自独立持久化**（DEFAULT_CONFIG 里三个 key
                分开存，/api/wb-daily/config 分开读写），本次只调整前端呈现，
                不改后端语义 —— 保存时三个值分别上报，互不影响。 */
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">签到调度</div>' +
              '<div class="wb-api-desc">只负责每日签到，独立间隔、独立开关。任务跑多久都不会影响签到的时间。</div>' +
            '</div>' +
          '</div>' +
          '<div class="wb-api-row wb-api-row-stack">' +
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;width:100%">' +
              /* 开关一：启动补签（checkin_enabled）—— 原「启用自动签到」，
                 从「执行模式与账号保活」卡移到这里，改名以免与下面的
                 「启用签到调度」混淆。 */
              '<div style="display:flex;align-items:center;gap:8px">' +
                '<input id="wb-dl-checkin" type="checkbox" style="width:16px;height:16px">' +
                '<span class="wb-api-label" style="margin:0" title="容器启动时立即核验服务端状态，未签到的账号会自动补签一次">启动补签</span>' +
              '</div>' +
              /* 开关二：启用签到调度（checkin_schedule_enabled）—— 定时跑签到轮 */
              '<div style="display:flex;align-items:center;gap:8px">' +
                '<input id="wb-dl-checkin-sched" type="checkbox" style="width:16px;height:16px">' +
                '<span class="wb-api-label" style="margin:0">启用签到调度</span>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">签到间隔（小时）</div>' +
                '<input id="wb-dl-checkin-interval" type="number" min="1" max="72" class="wb-api-input" style="width:100%;margin-top:4px" value="12">' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">签到上次执行</div>' +
                '<div id="wb-dl-checkin-lastrun" class="wb-api-input" style="display:flex;align-items:center;margin-top:4px;color:var(--muted-foreground)">—</div>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">签到下次到期</div>' +
                '<div id="wb-dl-checkin-nextdue" class="wb-api-input" style="display:flex;align-items:center;margin-top:4px;color:var(--muted-foreground)">—</div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">任务调度</div>' +
              '<div class="wb-api-desc">除每日签到以外的全部任务（成长 / 玩法 / 开学季 / 领奖）。独立间隔、独立开关，签到跑多久都不会影响任务的时间。</div>' +
            '</div>' +
          '</div>' +
          '<div class="wb-api-row wb-api-row-stack">' +
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;width:100%">' +
              '<div style="display:flex;align-items:center;gap:8px">' +
                '<input id="wb-dl-task-sched" type="checkbox" style="width:16px;height:16px">' +
                '<span class="wb-api-label" style="margin:0">启用任务调度</span>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">任务间隔（小时）</div>' +
                '<input id="wb-dl-task-interval" type="number" min="1" max="72" class="wb-api-input" style="width:100%;margin-top:4px" value="12">' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">任务上次执行</div>' +
                '<div id="wb-dl-task-lastrun" class="wb-api-input" style="display:flex;align-items:center;margin-top:4px;color:var(--muted-foreground)">—</div>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">任务下次到期</div>' +
                '<div id="wb-dl-task-nextdue" class="wb-api-input" style="display:flex;align-items:center;margin-top:4px;color:var(--muted-foreground)">—</div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        /* ── 任务巡检（用户 2026-10-16 需求）──
           背景：任务被中断（容器重启 / 被 kill / 超时）后，原先只能等下一轮
           调度（默认 12h）才恢复 —— 中断当天的剩余账号要等到第二天才跑，
           直接错过当天收益。巡检是一个更短的周期，到点就检查最近一轮是否
           中断，是则立即从剩余账号续跑。
           ⚠️ 巡检间隔必须小于上面的执行间隔才有意义 —— 否则等巡检到点时
              下一轮调度也到期了，巡检等于没做。 */
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">任务巡检</div>' +
              '<div class="wb-api-desc">任务被中断（容器重启、被终止、超时）后自动从剩余账号继续，不用等下一轮调度。巡检间隔需小于上面的执行间隔，否则没有意义。</div>' +
            '</div>' +
          '</div>' +
          '<div class="wb-api-row wb-api-row-stack">' +
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;width:100%">' +
              '<div style="display:flex;align-items:center;gap:8px">' +
                '<input id="wb-dl-patrol-enabled" type="checkbox" style="width:16px;height:16px">' +
                '<span class="wb-api-label" style="margin:0">启用任务巡检</span>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">巡检间隔（分钟）</div>' +
                '<input id="wb-dl-patrol-interval" type="number" min="5" max="1440" class="wb-api-input" style="width:100%;margin-top:4px" value="60" title="默认 60 分钟。范围 5–1440；必须小于执行间隔，否则等巡检到点时下一轮也到期了。">' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">待恢复的中断任务</div>' +
                '<div id="wb-dl-patrol-pending" class="wb-api-input" style="display:flex;align-items:center;margin-top:4px;color:var(--muted-foreground)">—</div>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">下次巡检</div>' +
                '<div id="wb-dl-patrol-next" class="wb-api-input" style="display:flex;align-items:center;margin-top:4px;color:var(--muted-foreground)">—</div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        /* 执行模式与账号保活：与调度拆分无关，仍归到一张公共卡片里。
           2026-10-22：「启用自动签到」（现名「启动补签」）已移到上面的
           签到调度卡，本卡只剩执行模式 / 保活 / 惰性刷新三个字段。 */
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">执行模式与账号保活</div>' +
              '<div class="wb-api-desc">「仅查询」模式只读积分/用量/成长，不产生任何任务状态变更。保活与惰性刷新控制账号凭据的续期节奏。</div>' +
            '</div>' +
          '</div>' +
          '<div class="wb-api-row wb-api-row-stack">' +
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;width:100%">' +
              '<div>' +
                '<div class="wb-api-label">执行模式</div>' +
                '<select id="wb-dl-mode" class="wb-api-input" style="width:100%;margin-top:4px">' +
                  '<option value="full">完整任务（签到/玩法/领奖）</option>' +
                  '<option value="query">仅查询（只读）</option>' +
                '</select>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">保活阈值（天）</div>' +
                '<input id="wb-dl-keep" type="number" min="0" max="90" class="wb-api-input" style="width:100%;margin-top:4px" value="30" title="0 表示每天无条件刷新">' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">惰性刷新（小时）</div>' +
                '<input id="wb-dl-lazy" type="number" min="1" max="72" class="wb-api-input" style="width:100%;margin-top:4px" value="6">' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        /* 操作按钮：**独立成一张卡**，紧跟三张调度卡之后。
           用户 2026-10-16 反馈：「调度和巡检为何没有保存按钮，改动之后用户
           无法保存」—— 原先这行按钮被塞进了「执行模式与账号保活」卡片的
           内部，视觉上归到了保活那一卡，用户改完调度/巡检配置后找不到
           保存入口，以为那两个区块没有保存按钮。
           现在独立成卡、紧跟调度卡，改完就能看到；且保存范围覆盖上面
           全部配置（签到调度 / 任务调度 / 巡检 / 执行模式 / 保活）。 */
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-desc">上次执行：<span id="wb-dl-lastrun" class="wb-api-mono">—</span>　下次到期：<span id="wb-dl-nextdue" class="wb-api-mono">—</span></div>' +
            '</div>' +
            '<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end">' +
              '<button id="wb-dl-abort" type="button" class="wb-api-btn wb-api-btn-danger" style="display:none">中止</button>' +
              '<button id="wb-dl-save" type="button" class="wb-api-btn">保存配置</button>' +
              '<button id="wb-dl-run" type="button" class="wb-api-btn wb-api-btn-primary">立即执行</button>' +
            '</div>' +
          '</div>' +
        '</div>' +

        /* ── 账号签到与凭据（最近 30 天）──
           2026-10-22 用户反馈：「这个板块没有背景框」。
           成因：这一行原本是**某张卡片的内部行**（`</div>` 层级写漏了一层，
           见 wbNormalizeDailyLayout 的说明），卡片被提前闭合后它就成了
           .wb-daily-wrap 的**直接子元素** —— 于是既没有卡片的外框
           （白底 / 1px 边框 / radius 16px），也没有卡片的内边距，
           看起来就是「一块没框的内容贴在页面上」。
           做法：把它**重新包回一张 .wb-api-card**，与同页其它板块
           （任务清单 / 任务记录 / 执行记录）用同一个圆角、边框、底色、内边距。
           行内原有的 border-bottom:0 / padding-top:14px 是「卡片内部行」的
           规格，包进卡片后保持原样；左右边距由既有规则
           `.wb-daily-wrap > .wb-api-row { margin: 0 !important }` 归零，
           使它的边缘与同层卡片严格对齐（实测同为 x=147 w=1116）。
           ⚠️ 标题「账号签到与凭据（最近 30 天）」**不要**加 wb-api-label 类之外
              的 hoist 干扰：wbHoistDailyCardTitles 会取卡片内第一个
              .wb-api-label 提到卡片外做 H2。这里标题用的是裸 span +
              行内 flex 排版（历史布局），不参与 hoist，视觉上仍与其它
              卡片标题一致（同为 13px/500 的 .wb-api-label 规格）。 */
        '<div class="wb-api-card" id="wb-dl-checkin-card">' +
          '<div class="wb-api-row wb-api-row-stack" style="border-bottom:0;padding-top:14px">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;width:100%;margin-bottom:6px">' +
              '<span class="wb-api-label">账号签到与凭据（最近 30 天）</span>' +
              // 去掉内联 font-size：它会压过 .wb-daily-wrap .wb-api-btn，
              // 导致这个按钮比同页其它按钮小一档（11px vs 14px）。
              // 「全部执行任务」与「全部立即签到」并列（用户 2026-10-13 反馈：
              // 有手动签到按钮但没有手动执行任务按钮）。
              '<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end">' +
                '<button id="wb-dl-tasks-all-btn" type="button" class="wb-api-btn">全部执行任务</button>' +
                '<button id="wb-dl-checkin-all-btn" type="button" class="wb-api-btn">全部立即签到</button>' +
              '</div>' +
            '</div>' +
            '<div class="wb-api-desc" style="margin-bottom:6px">自动关联账号池里的国内版（cn）账号，新增账号无需在此登记；刷新令牌只读共用（实测续期后旧令牌仍有效，无烧号风险）。</div>' +
            '<div id="wb-dl-checkin-logs-box" style="width:100%;max-height:280px;overflow-y:auto;display:flex;flex-direction:column;gap:6px">' +
              '<div class="wb-api-desc">正在加载账号签到与凭据…</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<!-- 任务清单：每日任务提供了哪些任务 -->' +
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">任务清单</div>' +
              '<div class="wb-api-desc" id="wb-dl-catalog-sum">正在加载任务列表…</div>' +
            '</div>' +
          '</div>' +
          '<div id="wb-dl-catalog" class="wb-api-row wb-api-row-stack" style="flex-direction:column"></div>' +
        '</div>' +

        '<!-- 任务记录：任务维度的台账（SQLite 持久化，逐任务项聚合） -->' +
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">任务记录</div>' +
              '<div class="wb-api-desc" id="wb-dl-tasks-sum">每个任务项最近一次执行结果与成功次数（内置 SQLite 持久化）</div>' +
            '</div>' +
            '<button id="wb-dl-refresh-tasks" type="button" class="wb-api-btn">刷新</button>' +
          '</div>' +
          '<div id="wb-dl-tasks" class="wb-api-row wb-api-row-stack" style="flex-direction:column"></div>' +
        '</div>' +

        '<!-- 执行记录（SQLite 持久化，保留最近 200 轮） -->' +
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">执行记录</div>' +
              '<div class="wb-api-desc" id="wb-dl-runs-sum">最近执行轮次（点击任一轮查看逐账号明细）</div>' +
            '</div>' +
            '<button id="wb-dl-refresh-runs" type="button" class="wb-api-btn">刷新</button>' +
          '</div>' +
          '<div id="wb-dl-runs" class="wb-api-row wb-api-row-stack" style="flex-direction:column"></div>' +
          '<div id="wb-dl-log-wrap" style="display:none">' +
            '<div class="wb-api-label" style="margin-top:10px">实时日志</div>' +
            '<div id="wb-dl-log" class="wb-api-code" style="max-height:220px;overflow-y:auto;white-space:pre-wrap"></div>' +
          '</div>' +
        '</div>' +
      /* ⚠️ 这里**只保留一个** `</div>` 收口 .wb-daily-wrap。
         早先「账号签到与凭据」那一行的闭合层级写错（多闭一层），它被踢出所属
         卡片；wbNormalizeDailyLayout 只兜底把游离节点搬回 wrap，搬不回归属的框。
         2026-10-22 把那一行重新包回卡片后，原先那两个多余的闭合会反过来把
         wb-daily-wrap 提前关掉 —— 整页样式全丢（wrap 作用域规则全部失效）。
         这里去掉多余闭合，让 wrap 由上面最后一个 `</div>` 正常收口。 */
      '</div>';

    v.querySelector("#wb-dl-save").addEventListener("click", wbDailySaveConfig);
    v.querySelector("#wb-dl-run").addEventListener("click", wbDailyRunJob);
    v.querySelector("#wb-dl-abort").addEventListener("click", wbDailyAbort);
    v.querySelector("#wb-dl-refresh-runs").addEventListener("click", function() {
      wbLoadWbDailyRuns();
      wbToast("执行记录已刷新", "ok");
    });
    var refreshTasksBtn = v.querySelector("#wb-dl-refresh-tasks");
    if (refreshTasksBtn) {
      refreshTasksBtn.addEventListener("click", function() {
        wbLoadWbDailyTasks();
        wbToast("任务记录已刷新", "ok");
      });
    }
    var refreshCreditBtn = v.querySelector("#wb-dl-credit-refresh");
    if (refreshCreditBtn) {
      refreshCreditBtn.addEventListener("click", function() {
        wbLoadWbDailyCredits();
        wbToast("积分统计已刷新", "ok");
      });
    }
    var checkinAllBtn = v.querySelector("#wb-dl-checkin-all-btn");
    // 「全部执行任务」：走每日任务服务的全量模式（签到+成长任务+玩法+领奖），
    // 与「立即执行」同源，只是入口放在账号列表区（用户 2026-10-13 需求：
    // 有手动签到按钮，缺手动执行任务按钮）。若已有任务在跑会返回 409，
    // 提示用户去「执行进度」看。
    var tasksAllBtn = v.querySelector("#wb-dl-tasks-all-btn");
    if (tasksAllBtn) {
      tasksAllBtn.addEventListener("click", function() {
        if (tasksAllBtn.disabled) return;
        tasksAllBtn.disabled = true;
        tasksAllBtn.textContent = "启动中…";
        fetch("/api/wb-daily/run", { method: "POST" })
          .then(function(r) { return r.json().catch(function() { return {}; }); })
          .then(function(d) {
            tasksAllBtn.disabled = false;
            tasksAllBtn.textContent = "全部执行任务";
            if (d && d.job_id) {
              wbToast("任务已启动，可在下方看进度", "ok");
              wbDailyPoll(d.job_id);
            } else {
              wbToast("启动失败：" + ((d && (d.detail || d.error)) || "已有任务在执行"), "err");
            }
          })
          .catch(function() {
            tasksAllBtn.disabled = false;
            tasksAllBtn.textContent = "全部执行任务";
            wbToast("请求失败，请重试", "err");
          });
      });
    }
    if (checkinAllBtn) {
      checkinAllBtn.addEventListener("click", function() {
        if (checkinAllBtn.disabled) return;
        checkinAllBtn.disabled = true;
        checkinAllBtn.textContent = "签到中…";
        fetch("/api/checkin/all", { method: "POST", headers: { "Content-Type": "application/json" } })
          .then(function(r) { return r.json().catch(function() { return {}; }); })
          .then(function(w) {
            if (w && w.status === "skipped" && w.reason === "already_running") {
              wbToast("签到任务正在进行，请稍后再试", "warn");
              return;
            }
            var accs = (w && w.accounts) || [];
            var s = accs.filter(function(e) { return e.result === "success"; }).length;
            var b = accs.filter(function(e) { return e.result === "already"; }).length;
            var o = accs.filter(function(e) { return e.result === "error"; }).length;
            var tone = o > 0 ? "err" : "ok";
            wbToast("签到完成：成功 " + s + "，已签 " + b + (o > 0 ? "，失败 " + o : ""), tone);
            wbLoadWbDailyCheckinLogs();
          })
          .catch(function(e) { wbToast("签到请求失败: " + e, "err"); })
          .then(function() {
            checkinAllBtn.disabled = false;
            checkinAllBtn.textContent = "全部立即签到";
          });
      });
    }
    return v;
  }

  function wbLoadWbDailyData() {
    fetch("/api/wb-daily/config").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var cfg = (d && d.config) || {};
      var el;
      el = document.getElementById("wb-dl-mode"); if (el) el.value = cfg.run_mode === "query" ? "query" : "full";
      el = document.getElementById("wb-dl-checkin"); if (el) el.checked = cfg.checkin_enabled !== false;
      el = document.getElementById("wb-dl-keep"); if (el) el.value = cfg.keepalive_days != null ? cfg.keepalive_days : 30;
      el = document.getElementById("wb-dl-lazy"); if (el) el.value = cfg.lazy_refresh_hours != null ? cfg.lazy_refresh_hours : 6;
      // 巡检（用户 2026-10-16 需求）
      el = document.getElementById("wb-dl-patrol-enabled"); if (el) el.checked = cfg.patrol_enabled !== false;
      el = document.getElementById("wb-dl-patrol-interval");
      if (el) el.value = (cfg.patrol_interval_minutes != null ? cfg.patrol_interval_minutes : 60);
      // 签到 / 任务调度拆分（用户 2026-10-16 需求）
      el = document.getElementById("wb-dl-checkin-sched"); if (el) el.checked = cfg.checkin_schedule_enabled !== false;
      el = document.getElementById("wb-dl-checkin-interval");
      if (el) el.value = cfg.checkin_interval_hours != null ? cfg.checkin_interval_hours : 12;
      el = document.getElementById("wb-dl-task-sched"); if (el) el.checked = cfg.task_schedule_enabled !== false;
      el = document.getElementById("wb-dl-task-interval");
      if (el) el.value = cfg.task_interval_hours != null ? cfg.task_interval_hours : 12;
    }).catch(function() {});
    fetch("/api/wb-daily/health").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var badge = document.getElementById("wb-dl-state");
      var runBtn = document.getElementById("wb-dl-run");
      var abortBtn = document.getElementById("wb-dl-abort");
      if (badge) {
        if (d && d.running) {
          badge.textContent = "执行中";
          badge.style.background = "rgba(59,130,246,.14)"; badge.style.color = "#1d4ed8";
        } else {
          badge.textContent = d && d.enabled === false ? "调度已停用" : "待机";
          badge.style.background = "rgba(16,185,129,.14)"; badge.style.color = "#047857";
        }
      }
      if (runBtn) { runBtn.disabled = !!(d && d.running); if (!(d && d.running)) runBtn.textContent = "立即执行"; }
      if (abortBtn) abortBtn.style.display = d && d.running ? "" : "none";
      var lr = document.getElementById("wb-dl-lastrun"); if (lr) lr.textContent = wbFmtTs(d && d.last_run_at);
      var nd = document.getElementById("wb-dl-nextdue"); if (nd) nd.textContent = d && d.enabled ? wbFmtTs(d.next_due_at) : "—（调度停用）";

      // 签到 / 任务拆分：各自的上次执行与下次到期（用户 2026-10-16 需求）
      var ch = (d && d.checkin_schedule) || {};
      var tk = (d && d.task_schedule) || {};
      var clr = document.getElementById("wb-dl-checkin-lastrun");
      if (clr) clr.textContent = ch.enabled === false ? "—（签到调度已停用）" : wbFmtTs(ch.last_run_at);
      var cnd = document.getElementById("wb-dl-checkin-nextdue");
      if (cnd) cnd.textContent = ch.enabled === false ? "—（签到调度已停用）" : wbFmtTs(ch.next_due_at);
      var tlr = document.getElementById("wb-dl-task-lastrun");
      if (tlr) tlr.textContent = tk.enabled === false ? "—（任务调度已停用）" : wbFmtTs(tk.last_run_at);
      var tnd = document.getElementById("wb-dl-task-nextdue");
      if (tnd) tnd.textContent = tk.enabled === false ? "—（任务调度已停用）" : wbFmtTs(tk.next_due_at);

      // 巡检状态（用户 2026-10-16 需求）
      var pt = (d && d.patrol) || {};
      var pp = document.getElementById("wb-dl-patrol-pending");
      if (pp) {
        if (pt.pending_run_id) {
          pp.textContent = pt.pending_run_id + "（"
            + (pt.pending_kind === "checkin" ? "签到轮" : "任务轮") + "，"
            + (pt.pending_status === "aborted" ? "已中止" : "中断") + "）";
          pp.style.color = "#b45309";
        } else {
          pp.textContent = "无（上一轮已正常完成）";
          pp.style.color = "";
        }
      }
      var pn = document.getElementById("wb-dl-patrol-next");
      if (pn) pn.textContent = pt.enabled === false ? "—（巡检已停用）" : wbFmtTs(pt.next_check_at);

      if (d && d.running && d.running_job_id) wbDailyPoll(d.running_job_id);
      else wbRenderDailyProgress(null);
    }).catch(function() {});
    wbLoadWbDailyCatalog();
    wbLoadWbDailyCheckinLogs();
    wbLoadWbDailyTasks();
    wbLoadWbDailyJobs();
    wbLoadWbDailyCredits();
    wbDailyStartCreditAutoRefresh();
  }

  /* 积分自动更新（用户 2026-09-27 要求）：取消手动「刷新」按钮后，
     积分必须自己动，否则数据会一直停在进入页面那一刻。

     做法：页面停留在本页且可见时，定期重拉 credit-summary 并重渲染。
     三条约束：
       · 只挂一次（__wbDailyCreditTimer 幂等），避免每次加载数据都叠一个定时器
       · document.hidden 时不拉 —— 后台标签页不该白烧请求
       · 离开本页（hash 变了）不拉 —— 用户已经不在这一页
     另外切回页面（visibilitychange）时立刻补拉一次，不用等下一个周期。 */
  var __wbDailyCreditTimer = null;
  var WB_DAILY_CREDIT_REFRESH_MS = 20000;
  function wbDailyStartCreditAutoRefresh() {
    if (__wbDailyCreditTimer) return;
    __wbDailyCreditTimer = setInterval(function() {
      if (document.hidden) return;
      if (window.location.hash !== "#/wb-daily") return;
      wbLoadWbDailyCredits();
    }, WB_DAILY_CREDIT_REFRESH_MS);
    if (!window.__wbDailyVisBound) {
      window.__wbDailyVisBound = true;
      document.addEventListener("visibilitychange", function() {
        if (document.hidden) return;
        if (window.location.hash !== "#/wb-daily") return;
        wbLoadWbDailyCredits();
      });
    }
  }

  /* 积分统计：今日 / 签到 / 任务 / 累计。
     数据来自 /api/wb-daily/credit-summary —— 后端从 wb_daily_tasks 的历史
     记录里解析「+N积分」并按天聚合，纯本地 SQLite 读，不走上游。 */
  function wbRenderDailyCredits(d) {
    var box = document.getElementById("wb-dl-credit-box");
    if (!box) return;
    if (!d || typeof d.totalCredits === "undefined") {
      box.innerHTML = '<div class="wb-api-desc">暂无积分记录</div>';
      return;
    }
    function num(v) {
      // 与官方积分统计页一致：千分位 + 两位小数
      var n = (v == null) ? 0 : v;
      return (Math.round(n * 100) / 100).toLocaleString("zh-CN", {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      });
    }
    // 四个数据项：今日积分 / 签到积分 / 任务积分 / 总积分
    var items = [
      { k: "今日积分", v: num(d.todayCredits),
        tip: "今日签到积分 + 今日任务积分" },
      { k: "签到积分", v: num(d.todayCheckinCredits),
        tip: "今日签到类任务获得的积分" },
      { k: "任务积分", v: num(d.todayTaskCredits),
        tip: "今日除签到外其他任务获得的积分" },
      { k: "总积分", v: num(d.totalCredits),
        tip: "按已有历史记录累加的总积分（往后继续累加）" }
    ];
    // 样式完全对齐官方「积分统计」页（实测 computed style）：
    //   外框：rounded-2xl + 1px 边框 + bg-card/70
    //   单项：flex-col + items-center + justify-center + px-4 py-5 + text-center
    //   数字：26px / font-weight 600 / 居中
    // 结构与官方积分统计页一致：卡片 → grid(4列) → 单项(居中) → 标签 + 数值
    // 数据标题图标：官方积分统计页每项标签都是
    // 「16px lucide 图标 + 8px gap + 13px/500 文字」，本页此前只有文字。
    var CREDIT_ICONS = {
      "今日积分": '<path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"/>',
      "签到积分": '<path d="M8 2v4"/><path d="M16 2v4"/><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M3 10h18"/><path d="m9 16 2 2 4-4"/>',
      "任务积分": '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
      "总积分": '<path d="M16 7h6v6"/><path d="m22 7-8.5 8.5-5-5L2 17"/>'
    };
    var html = '<div class="wb-dl-credit-card"><div class="wb-dl-credit-grid">';
    items.forEach(function(it) {
      html += '<div class="wb-dl-credit-item" title="' + it.tip + '">' +
        '<div class="wb-dl-credit-k">' +
          (CREDIT_ICONS[it.k] ? '<svg class="wb-dl-credit-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">' + CREDIT_ICONS[it.k] + '</svg>' : '') +
          '<span>' + it.k + '</span>' +
        '</div>' +
        '<div class="wb-dl-credit-v">' + it.v + '</div>' +
        '</div>';
    });
    html += '</div></div>';
    box.innerHTML = html;
    // 最近记录：单独渲染到它自己的容器，不混进积分四项框里
    wbRenderDailyRecentDays(d.days || []);
  }

  /* 最近记录：单独成块渲染，不再塞进积分四项框里。
     容器 #wb-dl-recent 在视图创建时已建好，这里只负责填内容。 */
  function wbRenderDailyRecentDays(days) {
    var host = document.getElementById("wb-dl-recent");
    if (!host) return;
    var list = (days || []).slice(0, 5);
    if (!list.length) {
      host.style.display = "none";
      host.innerHTML = "";
      return;
    }
    host.style.display = "";
    var html = '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
      '<tr style="color:var(--muted-foreground,#64748b)">' +
      '<th style="text-align:left;padding:6px 10px;font-weight:500">日期</th>' +
      '<th style="text-align:right;padding:6px 10px;font-weight:500">签到</th>' +
      '<th style="text-align:right;padding:6px 10px;font-weight:500">任务</th>' +
      '<th style="text-align:right;padding:6px 10px;font-weight:500">合计</th></tr>';
    list.forEach(function(x) {
      html += '<tr>' +
        '<td style="padding:6px 10px">' + (x.date || "") + '</td>' +
        '<td style="padding:6px 10px;text-align:right">' + (Number(x.checkin) || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '</td>' +
        '<td style="padding:6px 10px;text-align:right">' + (Number(x.task) || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '</td>' +
        '<td style="padding:6px 10px;text-align:right;font-weight:600">' + (Number(x.total) || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '</td>' +
        '</tr>';
    });
    html += '</table>';
    host.innerHTML = html;
  }

  function wbLoadWbDailyCredits() {
    fetch("/api/wb-daily/credit-summary", { cache: "no-store" })
      .then(function(r) { return r.ok ? r.json() : {}; })
      .catch(function() { return {}; })
      .then(function(d) { wbRenderDailyCredits(d); });
  }

  /* 任务记录：任务维度的台账。数据来自 SQLite（/api/wb-daily/task-summary），
     一任务一行，显示最近结果、最近账号与成功次数。与「执行记录」互补：
     那边是按轮次看，这边是按任务项看。 */
  function wbLoadWbDailyTasks() {
    var box = document.getElementById("wb-dl-tasks");
    if (!box) return;
    fetch("/api/wb-daily/task-summary").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var items = (d && d.items) || [];
      var sum = document.getElementById("wb-dl-tasks-sum");
      if (!items.length) {
        if (sum) sum.textContent = "还没有任务记录（执行一轮每日任务后自动累积，内置 SQLite 持久化）";
        box.innerHTML = '<div class="wb-api-desc" style="padding:8px 0;text-align:center">暂无任务记录</div>';
        return;
      }
      if (sum) sum.textContent = "共 " + items.length + " 个任务项有记录（内置 SQLite 持久化，随执行轮次累积）";
      var html = "";
      items.forEach(function(t) {
        var tone = t.last_result === "success" ? "#047857"
                 : t.last_result === "already" ? "#b45309"
                 : t.last_result === "failed" ? "#b91c1c" : "#94a3b8";
        var label = t.last_result === "success" ? "成功"
                  : t.last_result === "already" ? "已做过"
                  : t.last_result === "failed" ? "失败" : "记录";
        var extra = [];
        if (t.streak_days != null) extra.push("连签 " + t.streak_days + " 天");
        html += '<div class="wb-api-row" style="padding:6px 0">' +
          '<div class="wb-api-main" style="min-width:0">' +
            '<span class="wb-api-label" style="margin:0">' + (t.task_name || t.task_key) + '</span>' +
            '<span class="wb-api-desc" style="font-size:11px;margin-left:8px">' +
              (t.last_at ? wbFmtTs(t.last_at) : "—") +
              (t.last_account ? " · " + t.last_account : "") +
              (extra.length ? " · " + extra.join(" · ") : "") +
            '</span>' +
          '</div>' +
          '<div style="display:flex;align-items:center;gap:8px;flex:0 0 auto">' +
            '<span class="wb-api-desc" style="font-size:11px">成功 ' + (t.success || 0) + '/' + (t.total || 0) + '</span>' +
            '<span class="wb-api-badge" style="color:' + tone + ';background:transparent;border:1px solid ' + tone + '33">' + label + '</span>' +
          '</div>' +
        '</div>';
      });
      box.innerHTML = html;
    }).catch(function() {
      box.innerHTML = '<div class="wb-api-desc">任务记录加载失败</div>';
    });
  }

  /* 账号签到与凭据合并展示：一账号一行 ——
     左侧账号名（唯一来源：/api/wb-daily/accounts，与账号池同一出处），
     右侧两个状态：今日签到（/api/checkin/status，实时）+ 凭据（has_rt）。
     原「参与账号」卡片与「签到日志」两张卡重复列同一批账号，此处合并为一张。 */
  function wbLoadWbDailyCheckinLogs() {
    var box = document.getElementById("wb-dl-checkin-logs-box");
    if (!box) return;

    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function fmtTime(ts) {
      if (!ts) return "";
      try {
        return new Date(ts).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      } catch (e) { return String(ts); }
    }

    // 三个接口改成「各自到齐就渲染」：账号列表与签到日志走本地 SQLite（0.03s），
    // 签到状态要打上游（1.5~2.3s）。原先用 Promise.all 必须三个都到才画，
    // 于是飞机旅行图标秒出、签到徽章要等两秒多（用户 2026-09-26 反馈）。
    // 现在先用空签到状态渲染一版（徽章显示「查询中…」），状态到了再局部补写。
    var _acRes = null, _stRes = null, _lgRes = null, _drawn = false;

    function _tryDraw() {
      // 账号列表与签到日志到齐即可先画；签到状态可选，到了再重画补上。
      if (_drawn || !_acRes || !_lgRes) return;
      renderCheckinRows(_acRes, _stRes, _lgRes);
      _drawn = true;
    }

    fetch("/api/wb-daily/accounts").then(function(r) { return r.ok ? r.json() : {}; }).catch(function() { return {}; })
      .then(function(d) { _acRes = d; _tryDraw(); });
    fetch("/api/checkin/logs").then(function(r) { return r.ok ? r.json() : {}; }).catch(function() { return {}; })
      .then(function(d) { _lgRes = d; _tryDraw(); });
    fetch("/api/checkin/status").then(function(r) { return r.ok ? r.json() : {}; }).catch(function() { return {}; })
      .then(function(d) { _stRes = d; _drawn = false; _tryDraw(); });

    function renderCheckinRows(res0, res1, res2) {
      var accounts = (res0 && res0.accounts) || [];
      var statusList = (res1 && res1.accounts) || [];
      var logs = (res2 && res2.logs) || [];

      if (!accounts.length) {
        box.innerHTML = '<div class="wb-api-desc" style="padding:8px 0;text-align:center">暂无参与账号（账号池无国内版账号）</div>';
        return;
      }

      // 账号标识归一：账号池用 user/name，签到接口用 email/accountId。
      // 建一个多键索引，任一边的标识都能命中同一条记录。
      var statusByKey = {};
      statusList.forEach(function(s) {
        if (s.accountId) statusByKey[String(s.accountId)] = s;
        if (s.email) statusByKey[String(s.email)] = s;
      });

      // 每个账号取最近一条签到流水（logs 按时间升序返回）
      var lastLogByKey = {};
      logs.forEach(function(l) {
        if (l.accountId) lastLogByKey[String(l.accountId)] = l;
        if (l.email) lastLogByKey[String(l.email)] = l;
      });

      var html = "";
      accounts.forEach(function(a) {
        var displayName = a.name && a.name !== a.user ? (a.name + " (" + a.user + ")") : (a.user || a.name || "?");
        var st = statusByKey[String(a.id)] || statusByKey[String(a.user)] || statusByKey[String(a.name)] || null;
        var lg = lastLogByKey[String(a.id)] || lastLogByKey[String(a.user)] || lastLogByKey[String(a.name)] || null;

        // 今日签到状态
        var checkinText, checkinTone, checkinTitle = "";
        if (st && st.ok) {
          checkinText = st.todayCheckedIn ? "今日已签到" : "今日未签到";
          checkinTone = st.todayCheckedIn ? "#047857" : "#b45309";
          var exp = {};
          try { exp = (st.raw && st.raw.raw) || st.raw || {}; } catch (e) { exp = {}; }
          if (exp.streak_days != null) checkinTitle = "连签 " + exp.streak_days + " 天";
        } else if (st && st.statusUnsupported) {
          checkinText = "无签到接口";
          checkinTone = "#94a3b8";
          checkinTitle = st.error || "";
        } else if (lg) {
          var lt = lg.result === "success" ? "签到成功" : (lg.result === "already" ? "已签到" : "签到失败");
          checkinText = lt;
          checkinTone = lg.result === "success" ? "#047857" : (lg.result === "already" ? "#b45309" : "#b91c1c");
          if (lg.ts) checkinTitle = fmtTime(lg.ts);
        } else if (!_stRes) {
          // 签到状态还在路上（上游要 1.5~2.3s）：先给出占位，别让徽章看起来像坏了
          checkinText = "查询中…";
          checkinTone = "#94a3b8";
        } else {
          checkinText = "暂无记录";
          checkinTone = "#94a3b8";
        }

        // 凭据状态
        var credText = a.has_rt ? "凭据就绪" : "缺刷新令牌";
        var credTone = a.has_rt ? "#047857" : "#b91c1c";

        var lastTime = lg && lg.ts ? fmtTime(lg.ts) : "";

        html += '<div class="wb-api-row" style="padding:6px 0">' +
          '<div class="wb-api-main" style="min-width:0">' +
            '<span class="wb-api-mono" title="' + esc(displayName) + '">' + esc(displayName) + '</span>' +
            (lastTime ? '<span class="wb-api-desc" style="font-size:11px;margin-left:8px">最近 ' + esc(lastTime) + '</span>' : '') +
          '</div>' +
          /* 窄屏竖屏修复（用户 2026-09-27 反馈「移动端竖屏国内账号的旅行飞机图和签到图不显示」）
             原样式 flex:0 0 auto + 不换行：竖屏宽度不足时，飞机图标与签到/凭据徽章被挤出可视区
             （或被祖先 overflow 裁掉），看起来就是「图不见了」。
             改为可收缩 + 允许换行 + 右对齐，保证窄屏下整组状态仍然可见。 */
          '<div style="display:flex;align-items:center;gap:6px;flex:0 1 auto;flex-wrap:wrap;min-width:0;justify-content:flex-end">' +
            '<span class="wb-api-badge" style="color:' + checkinTone + ';background:transparent;border:1px solid ' + checkinTone + '33" title="' + esc(checkinTitle) + '">' + esc(checkinText) + '</span>' +
            '<span class="wb-api-badge" style="color:' + credTone + ';background:transparent;border:1px solid ' + credTone + '33">' + esc(credText) + '</span>' +
          '</div>' +
        '</div>';
      });
      box.innerHTML = html;
    }
  }

  function wbLoadWbDailyJobs() {
    wbLoadWbDailyRuns();
  }

  /* 任务清单：每日任务提供了哪些任务、安全分级、独立开关与单次手动执行 */
  function wbLoadWbDailyCatalog() {
    Promise.all([
      fetch("/api/wb-daily/catalog").then(function(r) { return r.ok ? r.json() : {}; }),
      fetch("/api/wb-daily/config").then(function(r) { return r.ok ? r.json() : {}; })
    ]).then(function(results) {
      var d = results[0];
      var cfg = (results[1] && results[1].config) || {};
      var disabledTasks = cfg.disabled_tasks || [];
      var box = document.getElementById("wb-dl-catalog");
      var sum = document.getElementById("wb-dl-catalog-sum");
      if (!box) return;
      var groups = (d && d.groups) || [];
      if (sum && d && d.total) {
        sum.textContent = "共 " + d.total + " 项任务：" + d.auto + " 项全自动、" + d.manual + " 项需人工；可按安全级别独立启闭或手动单次执行。";
      }
      if (!groups.length) { box.innerHTML = '<div class="wb-api-desc">暂无任务清单</div>'; return; }
      var html = "";
      groups.forEach(function(g) {
        var items = g.items || [];
        var autoN = items.filter(function(i) { return i.auto; }).length;
        html += '<div style="width:100%;margin-top:14px">' +
          '<div class="wb-api-label" style="font-weight:600;font-size:13px;display:flex;align-items:center;justify-content:space-between">' +
            '<span>' + g.group + '（' + autoN + '/' + items.length + ' 自动）</span>' +
          '</div>' +
          '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px;margin-top:8px">';
        items.forEach(function(i) {
          var k = i.key;
          var isDisabled = disabledTasks.indexOf(k) !== -1;
          var risk = i.risk || "Normal";
          var riskBadge = "";
          if (risk === "HighRisk") {
            riskBadge = '<span style="font-size:10px;padding:1px 5px;border-radius:4px;color:#dc2626;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.25)">高危</span>';
          } else if (risk === "Medium") {
            riskBadge = '<span style="font-size:10px;padding:1px 5px;border-radius:4px;color:#d97706;background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.25)">中危</span>';
          } else {
            riskBadge = '<span style="font-size:10px;padding:1px 5px;border-radius:4px;color:#059669;background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.25)">常规</span>';
          }
          var autoBadge = i.auto
            ? '<span style="color:#047857;font-size:10.5px">自动</span>'
            : '<span style="color:#b45309;font-size:10.5px">人工</span>';
          var opacity = isDisabled ? "opacity:0.55;" : "";
          html += '<div class="wb-api-card" style="margin:0;padding:8px 10px;display:flex;align-items:center;gap:8px;min-width:0;' + opacity + '" title="' + (i.note || i.name || "") + '">' +
            '<input type="checkbox" class="wb-dl-task-toggle" data-key="' + k + '" ' + (isDisabled ? '' : 'checked') + ' style="width:15px;height:15px;cursor:pointer" title="启用/禁用该任务">' +
            '<div style="display:flex;flex-direction:column;min-width:0;flex:1">' +
              '<div style="display:flex;align-items:center;gap:5px">' +
                '<span style="font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (i.name || k) + '</span>' +
                riskBadge +
              '</div>' +
              '<div style="font-size:10.5px;color:var(--muted-foreground);display:flex;gap:4px;align-items:center;margin-top:1px">' +
                autoBadge +
                (i.note ? '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:140px">· ' + i.note + '</span>' : '') +
              '</div>' +
            '</div>' +
            '<button type="button" class="wb-api-btn wb-dl-task-run-btn" data-key="' + k + '" style="font-size:11px;padding:2px 8px;height:24px;line-height:18px;margin-left:auto;flex-shrink:0" title="立即单次执行本任务">单次</button>' +
          '</div>';
        });
        html += '</div></div>';
      });
      box.innerHTML = html;

      // 绑定独立开关事件
      box.querySelectorAll(".wb-dl-task-toggle").forEach(function(chk) {
        chk.addEventListener("change", function() {
          var taskKey = this.getAttribute("data-key");
          var isChecked = this.checked;
          var curDisabled = disabledTasks.slice();
          if (!isChecked) {
            if (curDisabled.indexOf(taskKey) === -1) curDisabled.push(taskKey);
          } else {
            var idx = curDisabled.indexOf(taskKey);
            if (idx !== -1) curDisabled.splice(idx, 1);
          }
          disabledTasks = curDisabled;
          var parentCard = this.closest(".wb-api-card");
          if (parentCard) parentCard.style.opacity = isChecked ? "" : "0.55";
          fetch("/api/wb-daily/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ disabled_tasks: curDisabled })
          }).then(function(r) { return r.json(); }).then(function() {
            wbToast("任务 [" + taskKey + "] 已" + (isChecked ? "启用" : "禁用"), "ok");
          }).catch(function(e) {
            wbToast("保存任务开关失败: " + e, "err");
          });
        });
      });

      // 绑定单次手动执行事件
      box.querySelectorAll(".wb-dl-task-run-btn").forEach(function(btn) {
        btn.addEventListener("click", function(e) {
          e.stopPropagation();
          var taskKey = this.getAttribute("data-key");
          var b = this;
          b.disabled = true;
          b.textContent = "执行中";
          fetch("/api/wb-daily/run-task/" + taskKey, { method: "POST" })
            .then(function(r) { return r.json(); })
            .then(function(res) {
              if (res.job_id) {
                wbToast("已触发单任务 [" + taskKey + "] 执行", "ok");
                wbDailyPoll(res.job_id);
              } else {
                wbToast("启动失败: " + (res.detail || "未知错误"), "err");
              }
            })
            .catch(function(err) {
              wbToast("触发单任务失败: " + err, "err");
            })
            .finally(function() {
              setTimeout(function() {
                b.disabled = false;
                b.textContent = "单次";
              }, 2000);
            });
        });
      });

    }).catch(function() {});
  }

  /* 执行记录（SQLite 持久化）+ 逐账号明细 */
  function wbLoadWbDailyRuns() {
    fetch("/api/wb-daily/runs").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var box = document.getElementById("wb-dl-runs");
      var sum = document.getElementById("wb-dl-runs-sum");
      if (!box) return;
      var runs = (d && d.runs) || [];
      if (sum) sum.textContent = "最近执行轮次（最多保留 " + ((d && d.retain) || 200) + " 轮，点击任意一轮查看逐账号明细）";
      if (!runs.length) { box.innerHTML = '<div class="wb-api-desc">暂无执行记录</div>'; return; }
      var html = "";
      runs.forEach(function(j, idx) {
        var tone = j.status === "done" ? "#047857" : (j.status === "running" ? "#1d4ed8" : "#b45309");
        var label = j.status === "done" ? "✅ 完成" : j.status === "running" ? "⏳ 执行中"
                  : j.status === "aborted" ? "⛔ 已中止" : "❌ 失败";
        html += '<div class="wb-api-row wb-dl-run-row" data-job="' + j.job_id + '" data-idx="' + idx + '" style="padding:7px 0;cursor:pointer">' +
          '<div class="wb-api-main">' +
            '<span class="wb-api-mono">' + String(j.job_id) + '</span>' +
            '<span class="wb-api-desc" style="margin-left:8px">' + wbFmtTs(j.started_at) +
            ' · ' + (j.mode === "scheduled" ? "自动" : "手动") +
            (j.accounts ? " · " + j.accounts + " 个账号" : "") + '</span>' +
          '</div>' +
          '<span style="font-size:12px;font-weight:600;color:' + tone + '">' + label + '</span>' +
          '<span class="wb-dl-run-caret">▾</span>' +
        '</div>' +
        /* 明细就插在**这一行的正下方**（用户 2026-10-17 要求：
           「点击执行记录，相关信息应该显示在这条任务下方，而不是页面底部」）。
           带固定最大高度 + 内部滚动 —— 账号多时不会把整张卡片撑爆。 */
        '<div class="wb-dl-run-detail" id="wb-dl-run-detail-' + idx + '" style="display:none"></div>';
      });
      box.innerHTML = html;
      box.querySelectorAll(".wb-dl-run-row").forEach(function(row) {
        row.addEventListener("click", function() {
          wbToggleRunDetail(row, runs);
        });
      });
    }).catch(function() {});
  }

  function wbToggleRunDetail(row, runs) {
    var box = document.getElementById("wb-dl-runs");
    if (!box) return;
    var idx = row.getAttribute("data-idx");
    var panel = document.getElementById("wb-dl-run-detail-" + idx);
    var wasOpen = panel && panel.style.display !== "none";
    // 手风琴：先收起所有已展开的（同时只开一个，卡片高度不会被撑爆）
    box.querySelectorAll(".wb-dl-run-detail").forEach(function(p) {
      p.style.display = "none";
      p.innerHTML = "";
    });
    box.querySelectorAll(".wb-dl-run-row").forEach(function(r) {
      r.classList.remove("wb-dl-run-open");
    });
    if (wasOpen || !panel) return;      // 再点一次 = 收纳
    row.classList.add("wb-dl-run-open");
    panel.style.display = "";
    panel.innerHTML = '<div class="wb-api-desc" style="padding:8px 0">正在加载 ' + row.getAttribute("data-job") + ' 的逐账号明细…</div>';
    wbLoadWbDailyRunDetail(panel, row.getAttribute("data-job"));
  }

  function wbLoadWbDailyRunDetail(panel, jobId) {
    var wrap = panel;
    if (!wrap) return;
    wrap.innerHTML = '<div class="wb-api-desc" style="padding:8px 0">正在加载 ' + jobId + ' 的逐账号明细…</div>';
    fetch("/api/wb-daily/run-accounts/" + jobId)
      .then(function(r) { return r.ok ? r.json() : {}; })
      .then(function(d) {
        var rows = (d && d.accounts) || [];
        if (!rows.length) {
          wrap.innerHTML = '<div class="wb-api-desc" style="padding:8px 0">本轮没有账号级明细（可能是旧记录或任务未跑完）</div>';
          return;
        }
        var html = '<div class="wb-api-label" style="padding:6px 0 2px">逐账号明细：' + jobId + '</div>'
          /* 固定最大高度 + 内部滚动（用户 2026-10-17 要求：
             「显示不下的内容应该加一个滑动条，用户可以自行滑动查看」）。
             横向纵向都留滚动条，账号多、字段长都不撑大卡片。 */
          + '<div style="max-height:260px;overflow:auto;margin-top:4px">'
          + '<table style="width:100%;font-size:12px;border-collapse:collapse">' +
          '<thead><tr style="text-align:left;color:var(--muted-foreground,#64748b)">' +
          '<th style="padding:4px 6px">账号</th><th style="padding:4px 6px">完成</th>' +
          '<th style="padding:4px 6px">等级</th><th style="padding:4px 6px">连签</th>' +
          '<th style="padding:4px 6px">能量</th><th style="padding:4px 6px">积分</th></tr></thead><tbody>';
        rows.forEach(function(a) {
          html += '<tr style="border-top:1px solid var(--border,rgba(120,120,120,.15))">' +
            '<td style="padding:5px 6px" class="wb-api-mono">' + (a.account || "?") + '</td>' +
            '<td style="padding:5px 6px">' + (a.done || 0) + '/' + (a.total || 0) + '</td>' +
            '<td style="padding:5px 6px">' + (a.level || "—") + '</td>' +
            '<td style="padding:5px 6px">' + (a.streak || "—") + '</td>' +
            '<td style="padding:5px 6px">' + (a.energy || "—") + '</td>' +
            '<td style="padding:5px 6px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + ((a.credits || "").replace(/"/g, "&quot;")) + '">' + (a.credits || "—") + '</td></tr>';
          if (a.actions && a.actions.length) {
            html += '<tr><td colspan="6" style="padding:2px 6px 4px 6px;color:var(--foreground,#0f172a);font-size:11px;background:rgba(59,130,246,0.04)">已执行动作（' + a.actions.length + ' 项）：' + a.actions.join(" · ") + '</td></tr>';
          }
          if (a.rest && a.rest.length) {
            html += '<tr><td colspan="6" style="padding:0 6px 6px 6px;color:var(--muted-foreground,#64748b);font-size:11px">剩余 ' + a.rest.length + ' 项：' + a.rest.join("、") + '</td></tr>';
          }
        });
        html += '</tbody></table></div>';
        wrap.innerHTML = html;
      })
      .catch(function() {
        wrap.innerHTML = '<div class="wb-api-desc" style="padding:8px 0">明细加载失败</div>';
      });
  }

  /* 执行中进度：进度条 + 阶段文字 + 逐账号状态点。
     与右上角胶囊同一套原则：内容无变化不碰 DOM：只有值与上次不同才写。*/
  var __wbDlProgress = { key: "" };

  function wbRenderDailyProgress(d) {
    var card = document.getElementById("wb-dl-progress-card");
    if (!card) return;
    if (!d || (d.status !== "running" && d.status !== "pending")) {
      card.style.display = "none";
      __wbDlProgress.key = "";
      return;
    }
    card.style.display = "";
    var pct = d.progress || "0%";
    var steps = d.steps || [];
    var stepText = steps.length ? steps[steps.length - 1] : "正在执行…";
    var logs = d.log || [];
    var lastLog = logs.length ? logs[logs.length - 1] : "";
    var key = pct + "|" + stepText + "|" + lastLog;
    if (key === __wbDlProgress.key) return;
    __wbDlProgress.key = key;

    var pctEl = document.getElementById("wb-dl-progress-pct");
    var barEl = document.getElementById("wb-dl-progress-bar");
    var stepEl = document.getElementById("wb-dl-progress-step");
    var accEl = document.getElementById("wb-dl-progress-accounts");
    var totalAcc = d.accounts_total || 0, doneAcc = d.accounts_done || 0;
    if (pctEl) pctEl.textContent = pct + (totalAcc ? " (" + doneAcc + "/" + totalAcc + ")" : "");
    if (barEl) barEl.style.width = pct;
    if (stepEl) stepEl.textContent = lastLog || stepText;
    if (accEl) {
      var total = d.accounts_total || 0, done = d.accounts_done || 0;
      var html = "";
      for (var i = 0; i < total; i++) {
        var color = i < done ? "#10b981" : (i === done ? "#3b82f6" : "rgba(120,120,120,.35)");
        html += '<span title="账号' + (i + 1) + (i < done ? " 已完成" : (i === done ? " 执行中" : " 等待中")) + '" style="width:10px;height:10px;border-radius:50%;background:' + color + '"></span>';
      }
      if (total) html += '<span class="wb-api-desc" style="margin-left:6px">' + done + '/' + total + ' 个账号完成</span>';
      accEl.innerHTML = html;
    }
  }

  function wbDailySaveConfig() {
    // 参与账号自动关联账号池，不再有手填入口。
    // 签到 / 任务调度拆分 + 巡检（用户 2026-10-16 需求）：
    //   · 旧字段 enabled / interval_hours 保留并作为「总开关 + 兜底间隔」
    //     —— 两边各自的时间戳都为空时会退回它，保证拆分上线后不会立刻重跑。
    //   · 巡检间隔夹在 5–1440 分钟，越界时用边界值而不是 NaN。
    // 2026-10-22：三个开关**各自独立上报、独立持久化**——
    //   checkin_enabled          启动补签（容器起来核验一次）
    //   checkin_schedule_enabled 签到调度（定时跑签到轮）
    //   task_schedule_enabled    任务调度（定时跑任务轮）
    // 后端 DEFAULT_CONFIG 里三个 key 分开存、/api/wb-daily/config 分开读写，
    // 任一个改动不影响另外两个。
    var patrolMin = parseInt(document.getElementById("wb-dl-patrol-interval").value, 10);
    if (!(patrolMin > 0)) patrolMin = 60;
    patrolMin = Math.max(5, Math.min(1440, patrolMin));
    var payload = {
      run_mode: document.getElementById("wb-dl-mode").value,
      checkin_enabled: document.getElementById("wb-dl-checkin").checked,
      keepalive_days: parseInt(document.getElementById("wb-dl-keep").value, 10) || 0,
      lazy_refresh_hours: parseInt(document.getElementById("wb-dl-lazy").value, 10) || 6,
      // 巡检
      patrol_enabled: document.getElementById("wb-dl-patrol-enabled").checked,
      patrol_interval_minutes: patrolMin,
      // 签到 / 任务拆分
      checkin_schedule_enabled: document.getElementById("wb-dl-checkin-sched").checked,
      checkin_interval_hours: parseFloat(document.getElementById("wb-dl-checkin-interval").value) || 12,
      task_schedule_enabled: document.getElementById("wb-dl-task-sched").checked,
      task_interval_hours: parseFloat(document.getElementById("wb-dl-task-interval").value) || 12
    };
    // 巡检间隔若大于等于两边的执行间隔，明确告知用户「这样等于没做」
    var minIv = Math.min(payload.checkin_interval_hours, payload.task_interval_hours);
    if (payload.patrol_enabled && patrolMin >= minIv * 60) {
      wbToast("已保存，但巡检间隔 " + patrolMin + " 分钟 ≥ 执行间隔 " + minIv + " 小时，巡检不会生效", "warn");
    }
    fetch("/api/wb-daily/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function(r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function() { if (!(payload.patrol_enabled && patrolMin >= minIv * 60)) wbToast("配置已保存", "ok"); })
      .catch(function(e) { wbToast("保存失败: " + (e && e.message || e), "err"); });
  }

  function wbDailyRunJob() {
    var btn = document.getElementById("wb-dl-run");
    if (btn) btn.disabled = true;
    fetch("/api/wb-daily/run", { method: "POST" }).then(function(r) { return r.json(); }).then(function(d) {
      if (!d || !d.job_id) throw new Error((d && d.detail) || "启动失败");
      wbToast("每日任务已启动", "ok");
      wbDailyPoll(d.job_id);
    }).catch(function(e) {
      wbToast("启动失败: " + (e && e.message || e), "err");
      if (btn) btn.disabled = false;
    });
  }

  function wbDailyPoll(jobId) {
    if (window.__wbDailyPoll) clearInterval(window.__wbDailyPoll);
    var logBox = document.getElementById("wb-dl-log");
    if (logBox) logBox.style.display = "";
    window.__wbDailyPoll = setInterval(function() {
      fetch("/api/wb-daily/status/" + jobId).then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
        if (!d) return;
        if (logBox) {
          logBox.textContent = (d.log || []).join("\n");
          logBox.scrollTop = logBox.scrollHeight;
        }
        if (d.status !== "running") {
          clearInterval(window.__wbDailyPoll);
          window.__wbDailyPoll = null;
          wbToast(d.status === "done" ? "每日任务执行完成" : "每日任务结束（" + d.status + "）", d.status === "done" ? "ok" : "warn");
          wbLoadWbDailyData();
        }
      }).catch(function() {});
    }, 2000);
  }

  function wbDailyAbort() {
    fetch("/api/wb-daily/health").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var id = d && d.running_job_id;
      if (!id) { wbToast("当前没有执行中的任务", "info"); return; }
      fetch("/api/wb-daily/abort/" + id, { method: "DELETE" }).then(function() {
        wbToast("已发送中止请求", "info");
      }).catch(function() { wbToast("中止请求失败", "err"); });
    }).catch(function() { wbToast("中止失败", "err"); });
  }

  // 「无 Buddy」旅行徽章改造：官方在账号卡片右上角输出的是文字徽章「无 Buddy」，
  // 视觉重、信息重复。这里把它换成虚线描边的小飞机图标，悬浮时给出完整说明；
  // 账号领养 Buddy 后官方文案变成「未旅行 / 旅行中」等，本函数不再命中，
  // 徽章自然恢复官方原样 —— 无需任何持久化。
  // Buddy 旅行三态徽章：统一用同一枚官方「起飞飞机」图标（plane-takeoff），
  // 仅以「线型 + 颜色」区分状态，保证领养前后图标完全一致：
  //   无 Buddy = 虚线灰   未旅行 = 实线中性   旅行中 = 实线主题色
  // 官方文案落在徽章文本里，这里只替换图标与配色，不改动官方状态语义。
  var WB_PLANE_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="2" y1="22" x2="22" y2="22"></line><path d="M6.36 17.4 4 17l-2-4 1.1-.55a2 2 0 0 1 1.8 0l.17.1a2 2 0 0 0 1.8 0L8 12 5 6l.9-.45a2 2 0 0 1 2.09.2l4.02 3a2 2 0 0 0 2.1.2l4.19-2.06a2.41 2.41 0 0 1 1.73-.17L21 7a1.4 1.4 0 0 1 .87 1.99l-.38.76c-.23.46-.6.84-1.07 1.08L7.58 17.2a2 2 0 0 1-1.22.18Z"></path></svg>';
  function wbBuddyBadgeKind(text) {
    var t = (text || "").trim();
    if (!t) return null;
    // 先把「积分」类文案挡掉：它绝不是旅行状态。
    // 此前「剩余」前缀规则过于宽泛，把「剩余积分」也判成了 traveling，
    // 于是积分统计页的积分数据与浮窗第一行被替换成飞机图标
    // （用户 2026-09-27 反馈）。
    if (t.indexOf("积分") >= 0) return null;
    if (t === "无 Buddy") return "nobuddy";
    if (t === "未旅行") return "idle";
    if (t.indexOf("旅行中") === 0 || t.indexOf("即将到达") === 0) return "traveling";
    // 「剩余 37 分钟到达」才是旅行倒计时 —— 必须「剩余 + 数字 + 时间单位」，
    // 不再用裸的「剩余」前缀。
    if (/^剩余\s*\d+\s*(分钟|分|小时|时|秒)/.test(t)) return "traveling";
    return null;
  }
  function wbTransformNoBuddyBadges() {
    document.querySelectorAll("span, div").forEach(function(el) {
      if (el.classList.contains("wb-buddy-badge") || el.classList.contains("wb-nobuddy-badge")) return;
      if (el.childElementCount !== 0) return;
      var kind = wbBuddyBadgeKind(el.textContent);
      if (!kind) return;
      var cls = kind === "nobuddy" ? "wb-buddy-nobuddy"
              : kind === "traveling" ? "wb-buddy-traveling" : "wb-buddy-idle";
      el.classList.add("wb-buddy-badge", "wb-" + (kind === "nobuddy" ? "nobuddy-badge" : kind === "traveling" ? "traveling-badge" : "idle-badge"));
      if (kind === "nobuddy") el.classList.add("wb-buddy-nobuddy");
      if (kind === "traveling") el.classList.add("wb-buddy-traveling");
      if (kind === "idle") el.classList.add("wb-buddy-idle");
      el.title = kind === "nobuddy"
        ? "无 Buddy：还没有领养宠物伙伴。去官方端完成一次对话并领养 Buddy 后即可自动旅行"
        : (kind === "traveling" ? "Buddy 旅行中：伙伴正在出门旅行" : "未旅行：已领养 Buddy，当前在家待命");
      el.innerHTML = WB_PLANE_SVG;
      el.classList.add(cls);
    });
  }

  /* 侧边栏标题统一改名（用户 2026-09-27 要求）：
       Token 统计 -> 词元统计
       设置       -> 系统设置
     只按导航项的**完整文本**精确匹配后改名，不做全局字符串替换 ——
     「设置」这类短词在页面里出现太频繁，全局替换必然误伤。 */
  var WB_NAV_RENAMES = { "Token 统计": "词元统计", "Token统计": "词元统计", "设置": "系统设置" };
  function wbApplyNavRename(a, txt, next) {
    if (!next || txt === next) return false;
    // 收拢态的气泡读的是 data 属性，必须同步改，否则提示仍是旧名
    if (a.getAttribute("data-wb-label") === txt) a.setAttribute("data-wb-label", next);
    return true;
  }

  function renameNavLabels() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    aside.querySelectorAll("nav a").forEach(function(a) {
      // ⚠️ 关键：只替换**文本节点**，绝不写 a.textContent。
      //
      // 早期实现是 `var node = label || a; node.textContent = next;` ——
      // 而 renameNavLabels() 在 run() 里排在 initCollapse() **之前**，
      // 此刻官方导航项还没有 span.wb-nav-label（那是 initCollapse 才包上的），
      // 于是 node 就是 <a> 本身，赋值 textContent 会把 <a> 的**所有子节点**
      // 一起清掉 —— 包括 svg 图标。这正是「词元统计 / 系统设置图标丢失」的根因。
      //
      // 这里改成逐个文本节点改名，svg 等元素子节点原样保留。
      Array.prototype.slice.call(a.childNodes).forEach(function(node) {
        if (node.nodeType !== Node.TEXT_NODE) return;
        var raw = node.textContent || "";
        var txt = raw.trim();
        if (!txt) return;
        var next = WB_NAV_RENAMES[txt];
        if (!wbApplyNavRename(a, txt, next)) return;
        node.textContent = raw.replace(txt, next);
      });
      // 已被 initCollapse 包成 span 的情况（后续轮次 / MutationObserver 重跑）
      var label = a.querySelector("span.wb-nav-label");
      if (label) {
        var lt = (label.textContent || "").trim();
        var ln = WB_NAV_RENAMES[lt];
        if (wbApplyNavRename(a, lt, ln)) label.textContent = ln;
      }
    });
  }

  /* Token -> 词元：页面内**所有可见文案**的运行时替换。

     为什么不能用 TEXT_REPLACEMENTS（静态表）：
       Token 是 ASCII = 5 字节，词元 是 UTF-8 中文 = 6 字节，恒差 +1 字节。
       而 TEXT_REPLACEMENTS 对 JS 资源做的是**等长**字节替换 ——
       v0.9.16 正是栽在这里：少 1 字节 -> /assets/*.js 返回 500 -> 整站白屏。
       所以静态表根本配不平；更何况像「420M Token」「0 Token，0 次调用」
       这类**动态拼出来的串**根本无法预先枚举。

     为什么运行时替换是安全的：
       只改 **TEXT_NODE**，完全不碰元素属性 —— href="/api/token-stats"
       这类路由与标识符天然不受影响。而「裸 Token 全局替换会打挂接口」
       的风险，正是来自属性被改写，这里不存在这个问题。

     为什么不会和 MutationObserver 打成死循环：
       替换后文本里已经没有 "Token"，下一轮 run() 遍历时无节点命中、
       不再写 DOM，循环自然终止。若 React 重新渲染出含 Token 的文本，
       会被再次替换 —— 这正是期望行为（对抗 hydration 覆盖）。 */
  function wbCiyuanRename() {
    if (document.__wbCiyuanBusy) return;
    document.__wbCiyuanBusy = true;
    try {
      var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
      var targets = [];
      var n;
      while ((n = walker.nextNode())) {
        var t = n.nodeValue || "";
        if (t.indexOf("Token") === -1) continue;
        var p = n.parentNode;
        if (!p) continue;
        // 代码块/样式里的 Token 是源码，不是给看的文案
        var tag = p.nodeName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") continue;
        targets.push(n);
      }
      for (var i = 0; i < targets.length; i++) {
        var node = targets[i];
        var next = (node.nodeValue || "").replace(/Token/g, "词元");
        if (next !== node.nodeValue) node.nodeValue = next;
      }
    } catch (e) {
      // 替换失败不该影响页面其它功能
    } finally {
      document.__wbCiyuanBusy = false;
    }
  }

  function run() {
    sanitizeSidebarBrand();
    renameNavLabels();
    wbCiyuanRename();
    wbCheckAuth();
    initCollapse();
    injectWbOverview();
    wbUpdateOverviewView();
    wbBindOverviewActions();
    // 主题已挪到顶栏右上角，设置页那个官方主题项要藏掉（重复入口）
    wbHideSettingsTheme();
    sanitizeMacUI();
    wbTransformNoBuddyBadges();
    enforceTitle();
    injectAccountModels();
    injectAccountPool();
    wbReplaceReloginErrorBlocks();
    wbPinAccountBlocks();
    wbSortAccountCards();
    wbInjectAccountSearch();
    injectApiAccess();
    injectModelHealth();
    // ⚠️ 顺序要与 wbRefreshApi() 保持一致（API接入 → 巡检 → 助手 → 通知）。
    // 2026-10-14 修复：这里原先漏了 injectAssistant()，只加进了
    // wbRefreshApi()，导致**首次加载**（走 run()）看不到「智能助手」区块，
    // 只有手动触发刷新后才出现 —— 用户实测「设置页没有智能助手区块」。
    injectAssistant();
    injectNotify();
    injectProfile();
    injectAbout();
    wbPinAboutLast();
    injectWbDaily();
    // 顶栏与内容区对齐（宽度 + 左边缘都要跟当前页一致）。
    // 放在 run() 末尾：此时各页内容 wrap 已由 React 渲染出来，能量到真实宽度。
    wbSyncTopbarAlign();
    // 导航高亮同步（run() 每次跑都重新判一次，覆盖 SPA 重渲染后的残留）
    wbSyncNavActive();
  }

  // 去抖窗口。
  // 原先只有 1 帧（~16ms）：React 一次路由切换会分几十批渲染，每批 mutation
  // 都被放大成一轮完整 run() —— 实测一次「设置 → 账号管理」切换触发
  // 31 批 / 425 条 mutation，跑出 31 次 run()，主线程被 long task 占了
  // 1030ms，最长帧间隔 2077ms。侧边栏 DOM 本身没变（87 帧逐帧比对 0 跳变），
  // 用户看到的「闪屏」其实是主线程卡死造成的掉帧抖动。
  // 120ms 足以把同一轮渲染的全部批次合并成一次 run()，又不至于让
  // 首次挂载明显变慢。
  // ---- 路由切换静默期 ----
  // 实测（CDP 逐帧截图 + Profiler）：点侧边栏切页时 React 会分批重渲染，主内容容器
  // 在约 4.5s 内被整体换掉 3 次。我们注入的账号池控制条 / 模型区是卡片的子节点，
  // 每一批渲染都被连根拔掉，于是形成自激：
  //   React 重渲染 → 拔掉我们的节点 → observer 唤醒 run() → 我们重新注入
  //   → 注入又触发 observer → 再 run() ……
  // 实测一次「设置 → 账号管理」切换跑出 **8 次 run()、跨 4.8s**，主线程被 long task
  // 占掉 1s 以上，主内容区在 4 个状态间重排 —— 用户看到的就是「切换时页面闪/抖」。
  //
  // 解法：切页瞬间进入静默期，期间 observer 一律不唤醒 run()；静默期结束后若
  // 期间见过相关变更，补跑一次 run() 做最终注入。React 有多少批渲染都只换来
  // 一次注入，不再和它互斗。
  var WB_ROUTE_QUIET_MS = 2600;
  var __wbRouteQuietUntil = 0;
  var __wbRouteQuietDirty = false;
  var __wbRouteQuietTimer = null;

  function wbBeginRouteQuiet() {
    __wbRouteQuietUntil = performance.now() + WB_ROUTE_QUIET_MS;
    __wbRouteQuietDirty = false;
    if (__wbRouteQuietTimer) clearTimeout(__wbRouteQuietTimer);
    __wbRouteQuietTimer = setTimeout(function () {
      __wbRouteQuietTimer = null;
      __wbRouteQuietUntil = 0;
      if (__wbRouteQuietDirty) {
        __wbRouteQuietDirty = false;
        run();
      }
    }, WB_ROUTE_QUIET_MS + 40);
  }

  function wbRouteQuietActive() {
    return performance.now() < __wbRouteQuietUntil;
  }

  // 点侧边栏导航即视为一次切页（捕获阶段，早于 SPA 自己的路由逻辑）。
  document.addEventListener("click", function (e) {
    var t = e.target;
    var a = (t && t.closest) ? t.closest("aside nav a") : null;
    if (!a) return;
    wbBeginRouteQuiet();
  }, true);
  window.addEventListener("hashchange", function () { wbBeginRouteQuiet(); });
  window.addEventListener("popstate", function () { wbBeginRouteQuiet(); });

  var WB_RUN_DEBOUNCE_MS = 120;
  var __wbRunTimer = null;
  function wbDebouncedRun() {
    if (__wbRunTimer) {
      clearTimeout(__wbRunTimer);
    }
    __wbRunTimer = setTimeout(function () {
      __wbRunTimer = null;
      run();
    }, WB_RUN_DEBOUNCE_MS);
  }

  // observer 回调本身的代价也要控制：只在「本次变更可能影响我们注入的内容」时
  // 才唤醒 run()。纯官方自己高频刷的 class/aria 属性变化全部忽略。
  //
  // ⚠️ 这里**不能**用 rec.target.closest() 判断是不是我们自己注入的节点 ——
  // React 重渲染时 target 往往是高层的容器（如 body 或 MAIN），closest 命中
  // 不了我们的子节点，过滤会恒失效（2026-10-14 踩过这个坑）。
  // 正确做法是逐条看 record：addedNodes / removedNodes / attributeName。
  var WB_SKIP_ATTRS = {
    "class": 1, "aria-label": 1, "aria-pressed": 1, "disabled": 1,
    "aria-expanded": 1, "aria-hidden": 1, "data-state": 1,
    "data-slot": 1, "tabindex": 1, "role": 1, "style": 0
  };

  // 判断一个节点是不是我们自己注入的。
  // 我们的区块统一带 settings-* id 或 wb-* id / class，官方节点一个都不带
  // （实测页面 20 个带 id 的节点里，官方侧边栏 / nav / aside 全部无 id）。
  function wbIsOurNode(node) {
    if (!node) return false;
    if (node.nodeType === 3) {
      var p = node.parentElement;
      return p ? wbIsOurNode(p) : false;
    }
    if (node.nodeType !== 1) return false;
    var id = node.id || "";
    if (id.indexOf("settings-") === 0 || id.indexOf("wb-") === 0) return true;
    var cn = typeof node.className === "string" ? node.className : "";
    return cn.indexOf("wb-") >= 0;
  }

  // 判断一批 record 是否「只是我们自己注入 / 搬移自己节点」。
  //
  // 这是打破自激的关键。实测（run() 调用来源埋点）：
  //   切到账号管理页 → quiet-end → debounce → debounce → debounce → debounce
  // 每轮 run() 注入账号池控制条 / 模型区 → appendChild 触发 observer →
  // 再唤醒一轮 run()。官方 React 的重渲染只贡献了第一次，后面全是自己打自己。
  //
  // 规则（不对称，故意不对称）：
  //   新增的全是我们的节点            → 跳过（这是我们刚注入的）
  //   移除的全是我们的节点            → 跳过（这是 wbRefreshApi 换区块时自己删的）
  //   只移除、没有任何新增            → 唤醒（官方把我们注入的拔了，必须补注入）
  //   任何一侧掺了官方节点            → 唤醒（官方内容真的变了）
  function wbMutationIsSelfInflicted(recs) {
    var onlyOurs = true;
    var sawRemoval = false;
    for (var i = 0; i < recs.length; i++) {
      var r = recs[i];
      if (r.type === "attributes") {
        // 我们自己给节点加的 class / title / style
        if (r.target && wbIsOurNode(r.target)) continue;
        onlyOurs = false;
        continue;
      }
      if (r.type === "characterData") {
        if (r.target && wbIsOurNode(r.target)) continue;
        onlyOurs = false;
        continue;
      }
      var add = r.addedNodes || [];
      var rem = r.removedNodes || [];
      for (var j = 0; j < add.length; j++) {
        if (!wbIsOurNode(add[j])) { onlyOurs = false; break; }
      }
      for (var k = 0; k < rem.length; k++) {
        sawRemoval = true;
        if (!wbIsOurNode(rem[k])) { onlyOurs = false; break; }
      }
    }
    if (!onlyOurs) return false;
    // 全是我们的变更；但只要「只删不增」就仍然唤醒 —— 删掉旧区块和
    // 被官方拔掉从 record 上区分不了，保守唤醒不会丢功能。
    return !sawRemoval;
  }

  function wbMutationRelevant(recs) {
    if (wbMutationIsSelfInflicted(recs)) return false;
    for (var i = 0; i < recs.length; i++) {
      var r = recs[i];
      if (r.type === "childList") {
        // 节点增删可能带来新的导航项 / 设置区块，必须重跑
        if (r.addedNodes && r.addedNodes.length) return true;
        if (r.removedNodes && r.removedNodes.length) return true;
      } else if (r.type === "attributes") {
        var a = r.attributeName;
        if (!a) return true;
        // style 是极少数例外：我们自己的按钮靠内联 style 定位，但 React 很少改它
        if (WB_SKIP_ATTRS[a]) continue;
        return true;
      } else if (r.type === "characterData") {
        return true;
      }
    }
    return false;
  }

  const observer = new MutationObserver(function (recs) {
    // 路由切换静默期内一律不唤醒 run()，只记下「确实有相关变更」，
    // 等静默期结束补跑一次，避免和 React 的分批重渲染互相触发。
    if (wbRouteQuietActive()) {
      if (wbMutationRelevant(recs)) __wbRouteQuietDirty = true;
      return;
    }
    if (wbMutationRelevant(recs)) wbDebouncedRun();
  });
  // 只关心结构与文本；属性变化一律不订阅 —— 这是把「路由切换时 425 条
  // mutation」压到个位数的关键，比在回调里过滤更彻底。
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("DOMContentLoaded", function() {
    run();
    wbUpdateWbDailyView();
    // 兜底重跑（用户 2026-09-27 反馈「刷新后必须点设置才能点每日任务」）：
    // 刷新后 SPA 的 <nav> 往往还没挂载出来，此时 injectWbDaily() 找不到 nav
    // 就直接 return 放弃了；要等用户点「设置」引起 DOM 变化、MutationObserver
    // 再次触发 run() 才补上导航项 —— 表现就是「必须先点设置」。
    // 这里在 DOM 就绪后再补几次，确保导航项与 hash 路由都到位。
    [0, 300, 800, 1500].forEach(function(delay) {
      setTimeout(function() {
        renameNavLabels();
        injectWbDaily();
        wbUpdateWbDailyView();
      }, delay);
    });
  });
  // 注：hashchange 监听已在上面统一挂过（wbUpdateOverviewView →
  // wbApplyMainView + wbSyncNavActive），这里不再重复挂 ——
  // 挂两份会让每次切 tab 都跑两遍 wbApplyMainView()。
})();
</script>
"""

# 前端 UI 文案的**等长**改写表（只在 JS 资源上应用，见 clean_mac_content 的 is_js）。
# 都是「官方 UI 的措辞与本容器的语义不符」这类适配，不是删功能：
#   按项目 -> 按账号              ：网关没有「项目」概念，只有账号归因
#   消耗最高的会话 -> 消耗最高的调用：每条网关请求就是一次独立调用
#
# 账号页副标题（原「统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号…」）
# **不在这里**。它在 v0.3.14 已改由二进制补丁在源头修掉——留在响应层会是一条永远
# 命不中的死规则。选型原则：源头能改的结构性文案优先放二进制补丁，因为
# `_replace_padded` 要求锚点**恰好命中 1 次**，失配会让 CI 直接失败；而这里的
# `content.replace` 命不中时**静默无操作**（v0.3.10 那类事故的同一形状）。
TEXT_REPLACEMENTS = [
    ("按项目", "按账号"),
    ("消耗最高的会话", "消耗最高的调用"),
    ("按本地聚合 Token 从高到低排列。", "按单次调用词元 从高到低排列。"),
    # Token -> 词元：用户 2026-09-27 要求所有页面 token 相关字段同步改为词元。
    # 只列**完整短语**，不做裸 "Token" 全局替换 —— JS 里 /api/token-stats 这类
    # 路由与标识符含 Token，全局替换会直接把接口打挂。
    ("Token 与调用趋势", "词元与调用趋势"),
    ("Token 活动", "词元活动"),
    ("Token 统计", "词元统计"),
    ("Token 用量", "词元用量"),
    ("总 Token", "总词元"),
]

# 已告警过的「未命中」串，避免每次请求都刷屏。
_MISS_WARNED: set = set()


# 移动端全屏适配：把官方 viewport 改成「禁缩放 + 延伸到刘海区」。
# 参考 www.baidu.com 移动端：width=device-width + minimum/maximum-scale=1.0
# + user-scalable=no；额外加 viewport-fit=cover，让页面能吃满 iPhone
# 刘海/圆角区域，再靠 CSS 的 env(safe-area-inset-*) 把内容让开。
_WB_VIEWPORT = (
    'width=device-width, initial-scale=1.0, minimum-scale=1.0, '
    'maximum-scale=1.0, user-scalable=no, viewport-fit=cover'
)


def _wb_fix_viewport(content: bytes) -> bytes:
    """改写 HTML 里的 viewport meta（幂等，改不到就注入一条）。"""
    try:
        head = content[:4096]
        low = head.lower()
        i = low.find(b'<meta name="viewport"')
        if i >= 0:
            j = head.find(b">", i)
            if j > 0:
                # 原样保留其它 meta，只替换这一个标签
                return content[:i] + (
                    '<meta name="viewport" content="%s" />' % _WB_VIEWPORT
                ).encode("utf-8") + content[j + 1:]
            return content
        # 没有 viewport：注入到 <head> 之后
        k = low.find(b"<head>")
        if k >= 0:
            k2 = k + len(b"<head>")
            return content[:k2] + (
                '<meta name="viewport" content="%s" />' % _WB_VIEWPORT
            ).encode("utf-8") + content[k2:]
        return content
    except Exception:
        return content


def clean_mac_content(content: bytes, is_js: bool = False) -> bytes:
    # 兜底网：二进制补丁已把 `const X="http://127.0.0.1:57890";` 换成本页 origin，
    # 所以当前上游产物里已**不含**任何 `:57890`（实测 HTML / JS 均 0 处），这 4 条命中数
    # 正常就是 0。保留是因为补丁只认那一个 `const` 形态——上游若在别处新写一个硬编码
    # 后端地址，这里仍能把端口掰到 18090。**命中 0 属预期，不是失效。**
    # 旧品牌名兜底清除。
    #
    # ⚠️ 这里的 `content` 是 HTML/JS 响应体，不是二进制，所以**不要求等长** ——
    # 等长约束只适用于 patch/patch_binary.py 打二进制的情形（详见二进制补丁规程）。
    #
    # 以下四条的定位与边界：
    #   · `autobuddy.app` → `autobuddy`：官方文案里的 macOS 应用名，
    #     容器里没有桌面应用，去掉 .app 后缀纯为了不让用户去找一个不存在的东西。
    #   · 后三条把展示用的品牌名统一成 AutoBuddy，覆盖带引号、尖括号（JSX 文本节点）
    #     与裸串三种形态；顺序必须是「先具体后笼统」，否则后面的规则会先吃掉前面的。
    #
    # ⚠️ **不要**把 `/usr/lib/node_modules/autobuddy` 或 `autobuddy serve`
    # 这类标识符改成 AutoBuddy：那是官方 npm 包名与可执行文件名，改了容器直接起不来。
    # 展示名与标识符是两码事，详见 entrypoint.sh 第 1 步的注释。
    replacements = [
        (b"http://[IP]:57890", b""),
        (b"http://127.0.0.1:57890", b""),
        (b"http://localhost:57890", b""),
        (b":57890", b":18090"),
        (b"/icon-transparent.png", b"/icon.png"),
        (b"\xe5\x9c\xa8 Finder \xe4\xb8\xad\xe6\x98\xbe\xe7\xa4\xba", b"\xe5\x9c\xa8\xe6\x96\x87\xe4\xbb\xb6\xe7\xae\xa1\xe7\x90\x86\xe5\x99\xa8\xe4\xb8\xad\xe6\x98\xbe\xe7\xa4\xba"),
        (b"workbuddy-switch.app", b"autobuddy"),
        # 用户可见的旧品牌名（2026-10-15 用户反馈：项目已改名 AutoBuddy）。
        # JS 里 `workbuddy-switch` 共 3 处：前两处是 fetch 失败的报错文案
        #   「无法连接 workbuddy-switch 服务（${zb}），请先运行 \`workbuddy-switch\`」
        # 第三处是 `jye="workbuddy-switch"` 拼成的 GitHub 仓库地址
        #   （github.com/changexbc/workbuddy-switch）—— 真实上游，改了会 404，必须保留。
        # 所以只替换报错文案，绝不做全局裸串替换。
        # 响应体是文本不是二进制，不要求等长（见本函数顶部说明）。
        (b"\xe6\x97\xa0\xe6\xb3\x95\xe8\xbf\x9e\xe6\x8e\xa5 workbuddy-switch "
         b"\xe6\x9c\x8d\xe5\x8a\xa1",
         b"\xe6\x97\xa0\xe6\xb3\x95\xe8\xbf\x9e\xe6\x8e\xa5 AutoBuddy "
         b"\xe6\x9c\x8d\xe5\x8a\xa1"),
        (b"\xe8\xaf\xb7\xe5\x85\x88\xe8\xbf\x90\xe8\xa1\x8c \\`workbuddy-switch\\`",
         b"\xe8\xaf\xb7\xe6\xa3\x80\xe6\x9f\xa5 AutoBuddy \xe5\xae\xb9\xe5\x99\xa8"
         b"\xe6\x98\xaf\xe5\x90\xa6\xe6\xad\xa3\xe5\xb8\xb8\xe8\xbf\x90\xe8\xa1\x8c"),
        # JSX 文本节点：`>WorkBuddy Switch<` 与带引号的字符串字面量 {\"WorkBuddy Switch\"} 都要覆盖，
        # 否则侧边栏标题会留下半截旧名。
        (b'"WorkBuddy Switch"', b'"AutoBuddy"'),
        (b">WorkBuddy Switch<", b">AutoBuddy<"),
        # 笼统兜底放最后：上面两条已处理过的形态，走到这里就只剩裸串了。
        (b"WorkBuddy Switch", b"AutoBuddy"),
    ]
    for old, new in replacements:
        content = content.replace(old, new)

    # 文案适配：官方 UI 是给桌面客户端写的，容器里已无 IDE / CLI 能力。
    # 这里做**等长**字节替换，保证不破坏 JS 资源里的偏移与语法。
    # 这些是**前端 UI 文案**，只存在于 JS 资源里；HTML 外壳（几百字节）里没有它们。
    # 所以只在 is_js 时做，否则每个 HTML 请求都会误报「未命中」（v0.3.15 初版就踩了）。
    if is_js:
        for old, new in TEXT_REPLACEMENTS:
            old_b = old.encode("utf-8")
            new_b = new.encode("utf-8")
            if len(old_b) != len(new_b):
                raise ValueError(f"等长替换被破坏: {old} ({len(old_b)}) != {new} ({len(new_b)})")
            if content.count(old_b) == 0:
                # 上游改了文案就会走到这里。**不要静默通过**（v0.3.10 的教训就是静默失效），
                # 但也别让请求 500——每个串只告警一次，避免刷屏。
                if old not in _MISS_WARNED:
                    _MISS_WARNED.add(old)
                    print(f"[webui] 文案替换未命中（上游文案可能已变，请复核）: {old}")
            content = content.replace(old_b, new_b)

    return content


# ---------------------------------------------------------------------------
# 用户认证与配置持久化 API (SQLite 驱动)
# ---------------------------------------------------------------------------
from fastapi import Cookie, Depends, Response, HTTPException
from pydantic import BaseModel

class LoginReq(BaseModel):
    username: str
    password: str

class PwdChangeReq(BaseModel):
    old_password: str
    new_password: str

class ProfileReq(BaseModel):
    # 昵称已整体取消（用户 2026-09-26）：显示名一律以登录用户名为准，
    # 多一个「能改的显示名」只会让「我是谁」出现两种答案。
    avatar: Optional[str] = None  # "official" | "custom"

class UsernameReq(BaseModel):
    current_password: str
    new_username: str


def _env_user_set() -> bool:
    """模板/环境里显式设置过 AUTH_USERNAME（或别名）时，用户名锁定不可改。"""
    return bool((os.getenv("AUTH_USERNAME") or os.getenv("AUTH_USER") or os.getenv("AUTH_DEFAULT_USER") or "").strip())


def _env_pass_set() -> bool:
    """显式设置过 AUTH_PASSWORD（或别名）时，密码锁定不可改（每次启动都会被环境变量覆盖回去）。"""
    return bool((os.getenv("AUTH_PASSWORD") or os.getenv("AUTH_PASS") or os.getenv("AUTH_DEFAULT_PASS") or "").strip())


def _env_pass_len() -> Optional[int]:
    """返回环境变量密码的长度，供前端展示同位数掩码；未设置时返回 None。"""
    pwd = (os.getenv("AUTH_PASSWORD") or os.getenv("AUTH_PASS") or os.getenv("AUTH_DEFAULT_PASS") or "").strip()
    return len(pwd) if pwd else None


try:
    from gateway.official_avatar_svg import official_avatar_svg as _official_avatar_svg
except Exception:
    from official_avatar_svg import official_avatar_svg as _official_avatar_svg


# 启动即把历史超长会话压回当前默认有效期上限：
# 旧版本签发过 7 天票据，收紧到 24h 后必须让它们立即到期，
# 否则「过了有效期必须重新登录」对老会话不生效。
try:
    db.clamp_sessions_to_default()
except Exception as _clamp_err:
    print(f"[auth] 历史会话压回失败（忽略）: {_clamp_err}")

def get_current_user(autobuddy_session: Optional[str] = Cookie(None)) -> Optional[str]:
    if not autobuddy_session:
        return None
    return db.validate_session(autobuddy_session)


def require_user(user: Optional[str] = Depends(get_current_user)) -> str:
    """管理 API 的统一鉴权门（2026-10-20 安全修复）。

    此前 /api/* 转发端点除少数几个外都没有登录校验 —— 未登录请求也能
    直接调 /api/import 等写操作（线上曾出现注入测试残留账号，正是从
    这里进来的）。现在所有管理类 /api/* 端点都必须挂在有效会话上；
    未登录返回 401，由前端登录页接管。
    """
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user

@app.get("/api/auth/status")
async def auth_status(
    user: Optional[str] = Depends(get_current_user),
    autobuddy_session: Optional[str] = Cookie(None),
):
    enabled = True
    info = db.get_session_info(autobuddy_session) if user else None
    return {
        "enabled": enabled,
        "authenticated": bool(user),
        "username": user,
        "avatar": db.get_avatar() if user else "official",
        "usernameLocked": _env_user_set(),
        "passwordLocked": _env_pass_set(),
        "passwordLength": _env_pass_len(),
        "loginAt": int(info["created_at"] * 1000) if info else None,
        "expiresAt": int(info["expires_at"] * 1000) if info else None,
        "sessionHours": db.session_hours(),
    }

@app.post("/api/auth/login")
async def auth_login(req: LoginReq, response: Response):
    if not db.verify_user(req.username, req.password):
        return {"ok": False, "error": "用户名或密码错误"}
    token = db.create_session(req.username)
    hours = db.session_hours()
    response.set_cookie(
        key="autobuddy_session",
        value=token,
        max_age=hours * 3600,
        httponly=True,
        samesite="lax",
        path="/"
    )
    return {
        "ok": True,
        "username": req.username,
        "avatar": db.get_avatar(),
        "usernameLocked": _env_user_set(),
        "passwordLocked": _env_pass_set(),
        "sessionHours": hours,
    }

@app.post("/api/auth/logout")
async def auth_logout(response: Response, autobuddy_session: Optional[str] = Cookie(None)):
    if autobuddy_session:
        db.destroy_session(autobuddy_session)
    response.delete_cookie("autobuddy_session", path="/")
    return {"ok": True}

@app.post("/api/auth/password")
async def auth_change_pwd(req: PwdChangeReq, response: Response, user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    if _env_pass_set():
        return {"ok": False, "error": "AUTH_PASSWORD 已通过环境变量设置，此处不可修改（如需变更请修改容器环境变量）"}
    if not db.verify_user(user, req.old_password):
        return {"ok": False, "error": "当前密码不正确"}
    if len(req.new_password) < 6:
        return {"ok": False, "error": "新密码至少需要 6 个字符"}
    db.change_password(user, req.new_password)
    # 改密后作废该用户全部会话并清 Cookie，强制用新密码重新登录
    db.destroy_all_sessions(user)
    response.delete_cookie("autobuddy_session", path="/")
    return {"ok": True, "message": "密码修改成功，请重新登录", "relogin": True}


# ---------------- 用户资料（昵称 / 头像 / 用户名） ----------------

AVATAR_PATH = db.DATA_DIR / "avatars" / "avatar.png"
_AVATAR_MAX_BYTES = 200 * 1024
# 内置圆形头像候选的 key（与前端 WB_AVATAR_PRESETS 保持同步）
AVATAR_PRESET_KEYS = {"moon", "star", "leaf", "sun", "heart", "gem"}


def _valid_avatar_kind(kind: str) -> bool:
    if kind in ("official", "custom"):
        return True
    return isinstance(kind, str) and kind.startswith("preset:") and kind[7:] in AVATAR_PRESET_KEYS


def _avatar_mime(head: bytes) -> Optional[str]:
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.get("/api/auth/profile")
async def auth_profile_get(user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    return {
        "username": user,
        "avatar": db.get_avatar(),
        "usernameLocked": _env_user_set(),
        "passwordLocked": _env_pass_set(),
        "sessionHours": db.session_hours(),
    }


@app.put("/api/auth/profile")
async def auth_profile_put(req: ProfileReq, user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    if req.avatar is not None:
        if not _valid_avatar_kind(req.avatar):
            return {"ok": False, "error": "无效的头像类型"}
        if req.avatar == "custom" and not AVATAR_PATH.exists():
            return {"ok": False, "error": "尚未上传自定义头像"}
        db.set_avatar(req.avatar)
    return {"ok": True, "username": user, "avatar": db.get_avatar()}


@app.post("/api/auth/avatar-upload")
async def auth_avatar_upload(request: Request, user: Optional[str] = Depends(get_current_user)):
    """直接收原始图片字节（不走 multipart，避免额外依赖），限 PNG/JPG/WebP、≤200KB。"""
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    data = await request.body()
    if not data:
        return {"ok": False, "error": "未收到图片数据"}
    if len(data) > _AVATAR_MAX_BYTES:
        return {"ok": False, "error": "图片需 ≤ 200KB"}
    mime = _avatar_mime(data[:12])
    if not mime:
        return {"ok": False, "error": "仅支持 PNG / JPG / WebP 图片"}
    try:
        AVATAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        AVATAR_PATH.write_bytes(data)
    except Exception as e:
        return {"ok": False, "error": f"头像保存失败: {e}"}
    db.set_avatar("custom")
    return {"ok": True, "avatar": "custom"}


@app.get("/api/auth/avatar")
async def auth_avatar_get():
    """统一头像出口：自定义回自定义图，否则回官方图标（圆形 SVG 底衬）。"""
    if db.get_avatar() == "custom" and AVATAR_PATH.exists():
        data = AVATAR_PATH.read_bytes()
        mime = _avatar_mime(data[:12]) or "image/png"
        return Response(content=data, media_type=mime, headers={"Cache-Control": "no-cache"})
    return Response(
        content=_official_avatar_svg(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/auth/username")
async def auth_change_username(req: UsernameReq, response: Response, user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    if _env_user_set():
        return {"ok": False, "error": "AUTH_USERNAME 已通过环境变量设置，此处不可修改（如需变更请修改容器环境变量）"}
    if not db.verify_user(user, req.current_password):
        return {"ok": False, "error": "当前密码不正确"}
    new_name = (req.new_username or "").strip()
    if not new_name:
        return {"ok": False, "error": "新用户名不能为空"}
    if " " in new_name:
        return {"ok": False, "error": "用户名不能包含空格"}
    if len(new_name) > 32:
        return {"ok": False, "error": "用户名最长 32 个字符"}
    if not db.change_username(user, new_name):
        return {"ok": False, "error": "新用户名已存在或不可用"}
    db.destroy_all_sessions(new_name)
    response.delete_cookie("autobuddy_session", path="/")
    return {"ok": True, "username": new_name, "relogin": True}

@app.get("/icon.png")
@app.get("/icon-transparent.png")
@app.get("/favicon.ico")
async def get_icon():
    if ICON_PATH.exists():
        with open(ICON_PATH, "rb") as f:
            content = f.read()
        return Response(content=content, media_type="image/png")
    async with _internal_client() as client:
        r = await client.get(f"{BACKEND_URL}/icon.png")
        return Response(content=r.content, media_type="image/png")


def _gateway_models_by_account() -> dict:
    """从网关自己的高分辨率账本聚合「账号×日期 → 逐模型明细」。

    官方 ou.accounts 的 models 行模板 = {credit, model, requestCount}；
    官方那套自 9-24 停更，这里用 token_stats.db（逐请求带时间戳/账号/模型）
    重建等价数据。credit 用 cost 列（网关按 OpenRouter 牌价折算的消耗），
    与官方「credit」同义（都是折算额度）。带 TTL 缓存，读失败返回空 dict ——
    只是增强，失败必须优雅退化（此时 models 为空数组，页面不至于坏）。
    """
    now = time.time()
    hit = _MDL_CACHE.get("payload")
    if hit is not None and (now - float(_MDL_CACHE.get("at") or 0.0)) < _MDL_TTL_SEC:
        return hit
    out: Dict[str, Dict[str, list]] = {}
    db = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "token_stats.db"
    try:
        if db.exists():
            import sqlite3
            conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
            try:
                # 只取近 62 天（覆盖 31 天明细窗口 + 余量），避免全表扫
                lo = time.strftime("%Y-%m-%d", time.localtime(now - 62 * 86400))
                for aid, date, model, cnt, cost in conn.execute(
                        "SELECT account_id, date, model, COUNT(*), SUM(cost) "
                        "FROM usage_logs WHERE date >= ? "
                        "GROUP BY account_id, date, model", (lo,)):
                    if not aid or not date:
                        continue
                    out.setdefault(str(aid), {}).setdefault(str(date), []).append({
                        "model": str(model or "unknown"),
                        "requestCount": int(cnt or 0),
                        "credit": round(float(cost or 0.0), 6),
                    })
            finally:
                conn.close()
    except Exception:
        return out
    _MDL_CACHE["payload"] = out
    _MDL_CACHE["at"] = now
    return out


def _gateway_recent_requests(limit: int = 600) -> list:
    """从网关账本取最近的请求明细（官方 ou.requests 的等价替代）。

    官方模板字段：accountId / accountName / client / credit / model /
    requestId / requestTime。官方那套 9-24 起停更，这里用 token_stats.db
    （逐请求带时间戳）重建，按时间倒序取近 limit 条。
    带 TTL 缓存；读失败返回空 list —— 只是增强，必须优雅退化。
    """
    now = time.time()
    hit = _REQ_CACHE.get("recent")
    if isinstance(hit, list) and (now - float(_REQ_CACHE.get("recent_at") or 0.0)) < _REQ_TTL_SEC:
        return hit
    out = []
    db = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "token_stats.db"
    try:
        if db.exists():
            import sqlite3
            conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
            try:
                for rid, ts, aid, aname, model, cost in conn.execute(
                        "SELECT id, ts, account_id, account_name, model, cost "
                        "FROM usage_logs ORDER BY ts DESC LIMIT ?", (int(limit),)):
                    try:
                        _t = time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(float(ts) / 1000.0))
                    except Exception:
                        _t = ""
                    out.append({
                        "accountId": aid or "",
                        "accountName": aname or "",
                        "client": "gateway",
                        "credit": round(float(cost or 0.0), 6),
                        "model": str(model or "unknown"),
                        "requestId": str(rid or ""),
                        "requestTime": _t,
                    })
            finally:
                conn.close()
    except Exception:
        return out
    _REQ_CACHE["recent"] = out
    _REQ_CACHE["recent_at"] = now
    return out


def _reconcile_official_usage(data: dict, valid_account_ids) -> None:
    """校准 ``officialUsage`` 里失真的「今日消耗」。

    问题背景（实测）：官方底层的 ``officialUsage.summary.usageToday`` 长期为
    ``0.0``，各账号 ``usageToday`` 也全为 0、``currentRemaining`` 为 ``null``，
    而同一份响应顶层的 ``summary.usageToday`` 是准确的（如 1975.17）。
    官方前端优先采用 ``officialUsage``（``status=complete`` 即视为可用），
    于是把准确的顶层值覆盖成 0 —— 这就是「积分统计页今日消耗一直是 0」的根因。

    这里的做法是**不信任 officialUsage 的今日值**：
    - 顶层 summary.usageToday > 0 时，直接以它为准；
    - 顶层也为 0（或缺失）时，才回退到用 ``daily`` 里**今天**那条数据重算；
    - 各账号同样按其 daily 重算，并用顶层同账号的 currentRemaining 补 null。

    合计类维度一律以顶层为准：usageToday（2026-10-08 修复）+
    usage7Days / usageThisMonth（2026-10-09 修复，理由见文件内注释）。

    ⚠️ 下面这句是**旧结论，已被实测推翻**，保留在此作为教训：
        「usage7Days / usageThisMonth 官方是有值的，动它们反而会引入新的不一致」
    事实是 officialUsage 只覆盖 6 个账号而顶层是全量 20 个，它的所有合计类
    字段都偏小。当时只修了「今日」，留下累计维度没管，于是用户看到
    「近7日/本月 比 今日 还低」这种违反常识的数字。
    → 教训：给 external source 做对账时，「部分字段准」不等于「其余字段也准」；
      凡是被覆盖的子集，它的**合计类**字段一律不可信，必须逐个验证。
    """
    ou = data.get("officialUsage")
    if not isinstance(ou, dict) or not ou:
        return
    ou_summary = ou.get("summary")
    if not isinstance(ou_summary, dict):
        return

    top_summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    top_today = top_summary.get("usageToday")

    def _today_from_daily(daily) -> float:
        if not isinstance(daily, list):
            return 0.0
        today = time.strftime("%Y-%m-%d")
        for row in daily:
            if isinstance(row, dict) and str(row.get("date")) == today:
                try:
                    return float(row.get("usage") or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    # 先按 daily 重算各账号的今日值，并收集可用的兜底合计
    ou_accounts = ou.get("accounts")
    recomputed_total = None
    if isinstance(ou_accounts, list):
        total = 0.0
        any_value = False
        for acc in ou_accounts:
            if not isinstance(acc, dict):
                continue
            value = _today_from_daily(acc.get("daily"))
            if acc.get("usageToday") in (None, 0, 0.0) and value:
                acc["usageToday"] = round(value, 4)
            if acc.get("usageToday"):
                any_value = True
            try:
                total += float(acc.get("usageToday") or 0.0)
            except Exception:
                pass
        if any_value:
            recomputed_total = total

        # currentRemaining 为 null 时用顶层同账号的值补上（官方 officialUsage 不带该字段）
        top_by_id = {}
        for acc in (data.get("accounts") or []):
            if isinstance(acc, dict) and acc.get("accountId"):
                top_by_id[str(acc["accountId"])] = acc
        for acc in ou_accounts:
            if not isinstance(acc, dict):
                continue
            if acc.get("currentRemaining") is None:
                src = top_by_id.get(str(acc.get("accountId")))
                if src and src.get("currentRemaining") is not None:
                    acc["currentRemaining"] = src.get("currentRemaining")

    if isinstance(top_today, (int, float)) and top_today > 0:
        ou_summary["usageToday"] = round(float(top_today), 4)
    elif recomputed_total is not None:
        ou_summary["usageToday"] = round(recomputed_total, 4)

    # ---- 累计维度同样必须以顶层为准（2026-10-09 实测修正）----
    #
    # 这里原先的假设是「usage7Days / usageThisMonth 官方是有值的，动它们反而会
    # 引入新的不一致」。实测证明**该假设不成立**：officialUsage 只采集到 6 个账号，
    # 而顶层 summary 是全量 20 个账号的合计，于是前端拿到
    #   usage7Days=3597.49 / usageThisMonth=3819.01  对比  usageToday=5280.01
    # —— 累计值竟然比单日值还低，这在数学上不可能，用户一眼就能看出是错的。
    #
    # 和「今日」同一个道理：officialUsage 的账号覆盖不全，它的**合计类**字段
    # 不可信；顶层是全量，以顶层为准。
    def _pos(value):
        try:
            f = float(value or 0.0)
        except Exception:
            return 0.0
        return f if f > 0 else 0.0

    for _key in ("usage7Days", "usageThisMonth"):
        _top_val = _pos(top_summary.get(_key))
        if _top_val > 0:
            ou_summary[_key] = round(_top_val, 4)

    # 单调兜底：累计值必须 >= 今日值（今日窗口必然被包含在近7日/本月内）。
    # 若数据本身仍打架，就按已校准的今日值拉平 —— 宁可保守，也不能展示
    # 一个违反常识的数字。注意这里**不**强行让「本月 >= 近7日」：月初前几天
    # 本月窗口可能短于 7 天，那样断言反而会造出一个新的错误。
    _today_final = _pos(ou_summary.get("usageToday"))
    if _today_final > 0:
        if _pos(ou_summary.get("usage7Days")) < _today_final:
            ou_summary["usage7Days"] = round(_today_final, 4)
        if _pos(ou_summary.get("usageThisMonth")) < _today_final:
            ou_summary["usageThisMonth"] = round(_today_final, 4)

    # ---- 账号明细整体接管（2026-10-11 实测修正）----
    #
    # 官方采集停更后，ou.accounts[] 是重灾区：每个账号的 daily 全部停在
    # 2026-08-25 → 2026-09-24（31 天窗口不滚动），usage7Days / usageThisMonth
    # 是 9-24 的旧值，账号卡片和按日图表读的正是这份 —— 这就是用户看到
    # 「近 7 天和近一个月消耗就没变过」的真因。v0.9.40 只校准了 ou.summary
    # 三个数，漏了 ou.accounts[]。
    #
    # 参考同类项目 workbuddy2api-panel 的思路：它根本不消费官方的
    # officialUsage，而是自建 usage.Recorder（逐请求记录 + 窗口聚合）。
    # 本网关已有等价资产 —— 顶层 accounts 的 daily 由实时快照驱动（实测
    # 已滚动到当天）。既然官方通道停更（接口本身没坏，是采集环节停了），
    # 网关侧就全量接管：用校准后的实时账号替换 ou.accounts[]，并注明来源。
    #
    # 守恒性：顶层 accounts 在 credits_stats_proxy 里已完成重归因 + 滚动求和，
    # 直接引用同一份数据，ou 与顶层天然一致，不会再出现两栏数字打架。
    try:
        top_accounts = data.get("accounts")
        if isinstance(top_accounts, list) and top_accounts:
            # 按官方模板 schema 重构，而非直接 alias：
            #   官方 ou.accounts 字段 = accountId, accountName, daily[{date, models, usage}],
            #     detailTruncated, error, fetchedCount, models, ok, reportedTotal,
            #     requestCount, usage7Days, usageThisMonth, usageToday
            # 而 top accounts 是另一套 schema（checkedInToday / currentRemaining / variant…），
            # 直接替换会让按官方模板渲染的账号明细/模型分解缺料。
            # models 的真实逐模型明细（credit/requestCount）从网关自己的高分辨率
            # 账本（token_stats.db）按账号+日期聚合 —— 官方那套已停更。
            _models_by_acc = _gateway_models_by_account()
            rebuilt = []
            for _ta in top_accounts:
                if not isinstance(_ta, dict):
                    continue
                _mbd = _models_by_acc.get(str(_ta.get("accountId") or "")) or {}
                _new_daily = []
                _acc_models = {}
                _req_total = 0
                for _row in (_ta.get("daily") or []):
                    if not isinstance(_row, dict):
                        continue
                    _d = str(_row.get("date") or "")
                    _m = [dict(x) for x in (_mbd.get(_d) or [])]
                    _new_daily.append({"date": _d, "usage": _row.get("usage") or 0.0,
                                       "models": _m})
                    for _x in _m:
                        _acc_models.setdefault(_x["model"],
                                               {"credit": 0.0, "model": _x["model"], "requestCount": 0})
                        _acc_models[_x["model"]]["credit"] += _x.get("credit") or 0.0
                        _acc_models[_x["model"]]["requestCount"] += _x.get("requestCount") or 0
                        _req_total += _x.get("requestCount") or 0
                _usage_total = 0.0
                try:
                    _usage_total = round(sum(float(r.get("usage") or 0.0)
                                             for r in _new_daily), 4)
                except Exception:
                    pass
                # 状态字段必须**如实反映**，不能一律硬编码 ok=True / detailTruncated=True。
                #
                # 2026-10-24 实测缺陷（用户报「部分账号官方用量未同步」）：
                # 原先这里无条件写 detailTruncated=True + error=None + ok=True，
                # 与下面第 10881 行清空的 ou["errors"]=[] 自相矛盾 ——
                # 「明细被截断」却「没有任何错误」。官方前端据此判「有账号未同步」，
                # 于是页面同时显示「已同步 4/4 个当前账号」和「部分账号未同步」，
                # 而失败账号的官方数值一片「—」。实际 4 个当前账号全部同步成功。
                #
                # 修法：detailTruncated 只在**真的**没有逐模型明细时为 True；
                # ok 取决于该账号是否取到了 daily 数据；error 只在真的出错时给文案。
                # 这样官方前端拿到的是一份自洽的数据，不会误报。
                #
                # ⚠️ 2026-10-24 二次修正（上一版修错了方向，警告依然存在）：
                #
                # 上一版写的是 `_acc_has_detail = bool(_new_daily) and bool(_acc_models)`，
                # 让 ok 同时依赖两个数据源 —— 但这两件事的语义本来就不一样：
                #
                #   _new_daily  ← 官方接口返回的 daily（"这个账号的用量数据取到了吗"）
                #   _acc_models ← 网关 token_stats.db 的 usage_logs（"有逐模型明细吗"）
                #
                # usage_logs 只记录**经过网关调用过模型**的请求。一个只签到保活、
                # 从不走 API 的账号，daily 有数据但 usage_logs 是空的 —— 于是被那个
                # and 判成 ok=false，进 errors[]，官方前端就报「部分账号未同步」；
                # 而「已同步 4/4」用的是 daily（都有数据），两个提示自相矛盾。
                #
                # 官方语义里 ok 表示"数据取到了吗"，detailTruncated 表示"明细完整吗"，
                # 是两件独立的事。现在拆开：
                #   ok              只看 _new_daily（官方数据到手就算同步成功）
                #   detailTruncated 只看 _acc_models（明细缺失单独表达，不升级为失败）
                _acc_has_data = bool(_new_daily)
                _acc_has_detail = bool(_acc_models)
                rebuilt.append({
                    "accountId": _ta.get("accountId"),
                    "accountName": _ta.get("accountName"),
                    "daily": _new_daily,
                    "detailTruncated": not _acc_has_detail,
                    "error": None if _acc_has_data else "该账号无官方用量数据（上游未返回）",
                    "fetchedCount": _req_total,
                    "models": sorted(_acc_models.values(),
                                     key=lambda x: -(x.get("credit") or 0.0)),
                    "ok": _acc_has_data,
                    "reportedTotal": _usage_total,
                    "requestCount": _req_total,
                    "usage7Days": _ta.get("usage7Days"),
                    "usageThisMonth": _ta.get("usageThisMonth"),
                    "usageToday": _ta.get("usageToday"),
                })
            if rebuilt:
                ou["accounts"] = rebuilt
                ou["source"] = "gateway-realtime"
                # 范围端点跟着对齐，避免「数据截止 9-24」的误导继续挂着
                ou["rangeEnd"] = time.strftime("%Y-%m-%d")
                # ---- 顶层图表三件套同步接管（2026-10-13 实测补全）----
                # 页面「官方积分消耗」图表读的是 ou.daily（按日序列）/ ou.models
                # （逐模型聚合）/ ou.requests（请求明细），而不是 ou.accounts ——
                # v0.9.42 只接管了 accounts，图表仍停在 9-24（实测 08-25→09-24）。
                # 这三个字段全部从重建后的账号明细聚合而来，与 accounts 天然同源。
                try:
                    _g_daily = {}
                    _g_models = {}
                    _g_reqs = []
                    for _ra in rebuilt:
                        for _row in (_ra.get("daily") or []):
                            _rd = str(_row.get("date") or "")
                            if not _rd:
                                continue
                            _g_daily.setdefault(_rd, 0.0)
                            _g_daily[_rd] += float(_row.get("usage") or 0.0)
                            for _m in (_row.get("models") or []):
                                _mk = str(_m.get("model") or "unknown")
                                _g_models.setdefault(
                                    _mk, {"credit": 0.0, "model": _mk, "requestCount": 0})
                                _g_models[_mk]["credit"] += float(_m.get("credit") or 0.0)
                                _g_models[_mk]["requestCount"] += int(_m.get("requestCount") or 0)
                    # 请求明细：从网关账本取近 600 条（官方 detailLimit 同量级），
                    # 按时间倒序，字段对齐官方模板
                    _g_reqs = _gateway_recent_requests()
                    # 按日序列窗口对齐官方（31 天），从今天往前推 30 天
                    _today_d = time.strftime("%Y-%m-%d")
                    _start_d = time.strftime(
                        "%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
                    _days = []
                    _cur = _start_d
                    while _cur <= _today_d:
                        _days.append(_cur)
                        _cur = time.strftime(
                            "%Y-%m-%d", time.localtime(time.mktime(time.strptime(_cur, "%Y-%m-%d")) + 86400))
                    # 按日聚合全部账号的模型明细（图表的日粒度分解用）
                    _g_day_models = {}
                    for _ra in rebuilt:
                        for _row in (_ra.get("daily") or []):
                            _rd = str(_row.get("date") or "")
                            for _m in (_row.get("models") or []):
                                _g_day_models.setdefault(_rd, {})
                                _mk = str(_m.get("model") or "unknown")
                                _slot = _g_day_models[_rd].setdefault(
                                    _mk, {"credit": 0.0, "model": _mk, "requestCount": 0})
                                _slot["credit"] += float(_m.get("credit") or 0.0)
                                _slot["requestCount"] += int(_m.get("requestCount") or 0)
                    ou["daily"] = [{"date": _d,
                                     "models": sorted(_g_day_models.get(_d, {}).values(),
                                                       key=lambda x: -(x.get("credit") or 0.0)),
                                     "usage": round(_g_daily.get(_d, 0.0), 4)} for _d in _days]
                    ou["models"] = sorted(_g_models.values(),
                                           key=lambda x: -(x.get("credit") or 0.0))
                    if _g_reqs:
                        ou["requests"] = _g_reqs
                    ou["collectedAt"] = int(time.time() * 1000)
                    # errors 必须与 accounts 里的 ok/error 自洽：
                    # 有账号不同步就如实列出来，而不是一律清空。
                    # 上一版无条件 errors=[] 与 detailTruncated=True 互相矛盾，
                    # 正是官方前端误报「部分账号未同步」的直接原因。
                    ou["errors"] = [
                        {"accountId": _ra.get("accountId"),
                         "accountName": _ra.get("accountName"),
                         "error": _ra.get("error") or "未同步"}
                        for _ra in rebuilt if not _ra.get("ok")
                    ]
                    # 2026-10-23：status 必须与「已重建」的事实一起更新。
                    #
                    # 根因（用户实测「积分统计页面报错 · 官方用量暂不可用」）：
                    # 陈旧缓存里 status 是官方引擎留下的 "unavailable"，而上面
                    # 只重建了 accounts/daily/summary/collectedAt/errors，
                    # **漏了 status** —— 官方前端用它做整份数据的准入校验：
                    #     function py(e){return e?.status==="complete"||e?.status==="partial"}
                    # status 仍是 unavailable 时，py() 返回 false，前端把这份
                    # 已由网关实时重建的 officialUsage **整份丢弃**，页面回退
                    # 到本地观察口径并弹出「官方用量暂不可用」提示。
                    # 数据明明是实时的（source 已标 gateway-realtime），
                    # 只因一个字段没同步就被判为不可用 —— 必须一起改。
                    #
                    # 2026-10-24：requests 已由网关账本接管
                    # （_g_reqs = _gateway_recent_requests()），当初选 partial 的
                    # 前提（"requests 仍来自已停更的采集"）已不成立。账本有数据时
                    # accounts/daily/models/requests/summary 全部实时重建，应如实
                    # 标 complete；账本为空则 requests 仍是上游透传的旧值，维持
                    # partial 不谎报。
                    #
                    # 与下面 10914 行 `if _g_reqs: ou["requests"] = _g_reqs` 同一条件，逻辑自洽。
                    # 安全性：官方前端 py() 对 complete 与 partial 都返回 true，
                    # 改完不会触发「官方用量暂不可用」，只会消除「部分账号未同步」。
                    ou["status"] = "complete" if _g_reqs else "partial"
                    ou["source"] = "gateway-realtime"
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 官方用量数据的「陈旧度」标记
#
# 参考 CLIProxyAPI（53.7k stars）：它对所有缓存都显式设 TTL
# （如 AntigravityReasoningReplayCacheTTL = 1h），并用 3h ticker 自动刷新、
# 30s 最小间隔防抖。**陈旧是可知的，不是默认透明的。**
#
# 这里的情况更棘手：officialUsage 的采集产物 official_usage_cache.json
# 由官方闭源引擎 autobuddy-engine 写入，实测直连 :57890 调 credits/stats
# 也不触发重写（POST 返回 405、文件 mtime 纹丝不动）—— 那一段在闭源二进制
# 里，网关侧无法修复。
#
# 既然修不了上游，**就必须把陈旧如实暴露出来**：
# 过去页面显示「最近更新 9-24」却不说为什么，用户只能以为是网关坏了。
# 现在把采集时间、数据截止日、陈旧天数与原因一起下发，
# 让「数据没刷新」成为可见状态，而不是静默的假象。
# ---------------------------------------------------------------------------

# 超过这个天数就标记为陈旧（对齐 CLIProxyAPI 的「3 小时就该刷新」的严格度，
# 但这是每日级用量数据，按天衡量更合理）
OFFICIAL_USAGE_STALE_DAYS = int(os.getenv("AB_OFFICIAL_USAGE_STALE_DAYS", "2") or 2)


def _official_usage_freshness(data: dict) -> Dict[str, Any]:
    """从 officialUsage 里算出采集时间与陈旧程度。

    只读取、不修改业务数据 —— 它给的是「这份数据有多老」的元信息，
    与 usageToday 这类口径校正是两回事，不能互相污染。
    """
    ou = data.get("officialUsage") if isinstance(data.get("officialUsage"), dict) else {}
    out: Dict[str, Any] = {
        "collectedAt": ou.get("collectedAt"),
        "rangeEnd": ou.get("rangeEnd"),
        "ageDays": None,
        "stale": False,
        "reason": None,
    }
    ca = ou.get("collectedAt")
    if isinstance(ca, (int, float)) and ca > 0:
        # 上游给的是毫秒时间戳
        collected = ca / 1000.0 if ca > 1e11 else float(ca)
        age = max(0.0, (time.time() - collected) / 86400.0)
        out["ageDays"] = round(age, 2)
        out["stale"] = age >= OFFICIAL_USAGE_STALE_DAYS
        if out["stale"]:
            if ou.get("source") == "gateway-realtime":
                # 账号明细已由网关实时接管（_reconcile_official_usage），
                # 陈旧的只是官方原始采集那份；面板看到的数据是实时的。
                out["reason"] = (
                    "官方原始采集已停更（最近一次 %.1f 天前），但本页数据已由网关"
                    "用实时快照接管，汇总与按日明细均为实时口径。"
                    % age
                )
            else:
                out["reason"] = (
                    "官方用量数据由闭源底层引擎采集，当前未再刷新："
                    "最近一次采集距今约 %.1f 天，数据截止 %s。"
                    "该采集环节在官方二进制内部，网关侧无法触发重写；"
                    "本页的今日消耗已改用实时数据校准，不受此影响。"
                    % (age, ou.get("rangeEnd") or "未知")
                )
    elif not ou:
        out["reason"] = "官方用量数据缺失（闭源底层未返回 officialUsage）。"
    else:
        out["reason"] = "官方用量数据未携带采集时间，无法判断新鲜度。"
    return out


# ---------------------------------------------------------------------------
# 签到状态结果缓存（积分统计页/每日任务页共用的「今日签到」徽章）
#
# 需求来源（用户 2026-09-26）：刷新页面后账号的飞机旅行图标很快显示，
# 但签到图标要很久才出来。
#
# 根因（实测）：/api/checkin/status 上游要 1.46s、经反代 2.34s —— 它得逐个账号
# 去 WorkBuddy 服务端实时查询；而 travel/status、wb-daily/accounts 走本地
# SQLite 只要 0.03s。前端 Promise.all 三个都到齐才渲染，于是签到徽章被拖住。
#
# 修法之一：签到状态本来就是「今天签没签」，分钟级陈旧度完全可接受，
# 缓存 60 秒即可挡掉刷新页面的重复查询，把 2.34s 降到毫秒级。
# ---------------------------------------------------------------------------
# 签到状态结果缓存（积分统计页/每日任务页共用的「今日签到」徽章）
#
# 需求来源（用户 2026-09-26）：刷新页面后账号的飞机旅行图标很快显示，
# 但签到图标要很久才出来。
#
# 根因演进（实测数据说话）：
#   - 2026-09-26：上游 1.46s、经反代 2.34s → 加 60s 缓存；
#   - 2026-10-15：上游劣化到 **4.9~8.9s**（官方后端逐个账号实时查，账号从
#     17 涨到 22+）。用户反馈「统计加载失败 / 卡死 / 重启报错」——
#     前端 Promise.all 被它拖死，fetch 中断后抛
#     「无法连接 workbuddy-switch 服务」（服务其实活着，只是慢）。
#
# 修法（stale-while-revalidate）：
#   用户请求**永远不等上游**。有数据（哪怕陈旧）就立即返回，
#   同时后台线程去刷新；没有数据时才同步等一次（首启 unavoidable）。
#   ⇒ 页面秒开，签到徽章最多陈旧 FRESH_TTL，之后由后台保持。
# ---------------------------------------------------------------------------
_CHECKIN_STATUS_CACHE: Dict[str, Any] = {"at": 0.0, "payload": None}
_CHECKIN_STATUS_TTL_SEC = 60.0          # 兼容旧字段名（fresh 阈值）
_CHECKIN_FRESH_SEC = 120.0              # 超过它认为「不新鲜」，触发后台刷新
_CHECKIN_STALE_SEC = 1800.0             # 超过它认为「太旧」，仍先用但提高优先级
_CHECKIN_BG_LOCK = threading.Lock()
_CHECKIN_BG_RUNNING = False


def _checkin_status_cached() -> Optional[Any]:
    """命中未过期缓存则返回，否则 None（让调用方去取真值）。

    保留旧语义给「同步兜底」用；用户请求路径改用
    `_checkin_status_serve()`（stale-while-revalidate）。
    """
    hit = _CHECKIN_STATUS_CACHE.get("payload")
    if hit is not None and (time.time() - float(_CHECKIN_STATUS_CACHE.get("at") or 0.0)) < _CHECKIN_STATUS_TTL_SEC:
        return hit
    return None


def _checkin_status_store(payload: Any) -> None:
    _CHECKIN_STATUS_CACHE["payload"] = payload
    _CHECKIN_STATUS_CACHE["at"] = time.time()


def _checkin_age() -> float:
    """缓存年龄（秒）；无数据时返回 inf。"""
    if _CHECKIN_STATUS_CACHE.get("payload") is None:
        return float("inf")
    return time.time() - float(_CHECKIN_STATUS_CACHE.get("at") or 0.0)


def _checkin_bg_refresh(force: bool = False) -> None:
    """后台刷新签到状态（单工：已有一次在跑就跳过）。

    单工而非并发：上游要 5~9s，多个请求同时触发会把它打爆；
    且签到状态分钟级陈旧完全可接受，没必要追着刷。
    """
    global _CHECKIN_BG_RUNNING
    with _CHECKIN_BG_LOCK:
        if _CHECKIN_BG_RUNNING:
            return
        _CHECKIN_BG_RUNNING = True

    def _worker():
        global _CHECKIN_BG_RUNNING
        try:
            with httpx.Client(timeout=90.0, verify=False) as c:
                r = c.get(f"{BACKEND_URL}/api/checkin/status")
            if r.status_code == 200:
                _checkin_status_store(r.json())
                logging.info("[checkin] 后台刷新完成（force=%s）", force)
            else:
                logging.warning("[checkin] 后台刷新失败: HTTP %s", r.status_code)
        except Exception as e:
            logging.warning("[checkin] 后台刷新异常: %s", str(e)[:120])
        finally:
            with _CHECKIN_BG_LOCK:
                _CHECKIN_BG_RUNNING = False

    threading.Thread(target=_worker, name="ab-checkin-refresh", daemon=True).start()


def _checkin_status_serve() -> Any:
    """stale-while-revalidate：立即返回可用数据，必要时后台刷新。

    返回 ``(payload, age_sec)``；payload 为 None 表示从未拿到过数据
    （调用方需同步等一次）。
    """
    payload = _CHECKIN_STATUS_CACHE.get("payload")
    age = _checkin_age()
    if payload is not None:
        if age > _CHECKIN_FRESH_SEC:
            _checkin_bg_refresh(force=(age > _CHECKIN_STALE_SEC))
        return payload, age
    return None, age


# 容器启动即后台预热一次：让第一个用户请求就能命中缓存，
# 而不是首启那个人干等 5~9 秒。
try:
    _checkin_bg_refresh(force=True)
except Exception:
    pass


# ---------------------------------------------------------------------------
# 今日消耗重归因：把后端「今日桶」里跨天串账的部分归还给昨天
#
# 实测（2026-10-10 01:17，今天才过 1.29 小时，面板却显示 6371.68）：
# 后端快照约 5 小时一次，今日桶的基准锚在**昨晚 20:07** 那条快照上，
# 于是把 10-09 晚间约 3.5 小时（含 22:24 那轮每日任务）的消耗整段算进了 10-10。
# 交叉验证：当天仅 395 次请求，而往常 3135 次请求对应 5280.01 —— 差约 10 倍。
#
# 为什么不能只信后端：后端是闭源引擎，其 daily 桶由稀疏快照做差值得出，
# 快照间隔（~5h）远大于日界精度要求，跨天必然串账。网关侧改不了它的采集。
#
# 修法：用网关自己的**高分辨率**请求账本（token_stats.db，逐请求带时间戳）
# 把这段窗口内的消耗按「午夜前后的请求数」拆分；没有请求数据时退化为按时间
# 占比拆分。**守恒**：拆出来的部分加回昨天，总量不变。
#
# 快照约 5 小时才更新一次，所以修正结果在快照刷新前是稳定的 —— 用 TTL 缓存
# 避免每次请求都去读 1MB 快照文件。
# ---------------------------------------------------------------------------

_SNAP_CACHE = {"at": 0.0, "payload": None}
_SNAP_TTL_SEC = 120.0
_REQ_CACHE = {"at": 0.0, "payload": None}
_REQ_TTL_SEC = 60.0
_MDL_CACHE = {"at": 0.0, "payload": None}
_MDL_TTL_SEC = 120.0


def _credit_snapshot_files():
    """快照文件（两个位置都可能有，合并读取）。"""
    out = []
    for p in (get_switch_file("credit_usage_snapshots.json"),
              Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "credit_usage_snapshots.json"):
        try:
            if p.exists():
                out.append(p)
        except Exception:
            pass
    return out


def _load_credit_snapshots() -> dict:
    """读取积分快照，返回 {accountId: [(ts_ms, remaining), ...]}（按 ts 升序）。

    带 TTL 缓存：快照文件约 1MB / 5000 条，且后端约 5 小时才更新一次。
    读失败返回空 dict —— 归因只是增强，失败必须优雅退化。
    """
    now = time.time()
    hit = _SNAP_CACHE.get("payload")
    if hit is not None and (now - float(_SNAP_CACHE.get("at") or 0.0)) < _SNAP_TTL_SEC:
        return hit
    acc = {}
    for p in _credit_snapshot_files():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for r in data:
            if not isinstance(r, dict):
                continue
            aid = str(r.get("accountId") or "")
            if not aid:
                continue
            try:
                ts = float(r.get("ts"))
                rem = float(r.get("remaining"))
            except Exception:
                continue
            if ts < 1e12:      # 秒级时间戳统一到毫秒
                ts *= 1000.0
            acc.setdefault(aid, []).append((ts, rem))
    for k in acc:
        acc[k].sort()
    _SNAP_CACHE["payload"] = acc
    _SNAP_CACHE["at"] = now
    return acc


def _plan_from_snapshots(snaps: dict, midnight_ms: float) -> dict:
    """纯函数：算出每个账号需要重归因的窗口。

    返回 {accountId: (baseline_ts, latest_ts, delta)}：
      - baseline = 午夜**之前**最后一条快照（后端今日桶的基准，可能远早于午夜）
      - latest   = 最新快照
      - delta    = baseline.remaining - latest.remaining（后端记为「今日」的量）

    跳过：无基准快照、delta <= 0（配额补充导致的负消耗）、窗口全在午夜前。
    """
    plan = {}
    for aid, seq in (snaps or {}).items():
        if not seq:
            continue
        base = None
        for ts, rem in seq:
            if ts <= midnight_ms:
                base = (ts, rem)
            else:
                break
        latest = seq[-1]
        if base is None:
            continue
        delta = base[1] - latest[1]
        if delta <= 0:
            continue
        if latest[0] <= midnight_ms:
            continue        # 窗口未跨午夜，不存在串账
        plan[aid] = (base[0], latest[0], delta)
    return plan


def _split_fraction(baseline_ts: float, latest_ts: float, midnight_ms: float,
                    n_before: int, n_after: int) -> float:
    """纯函数：窗口内消耗应归给「今日」的比例。

    优先按请求数拆分（高分辨率证据）；无请求数据时退化为按时间占比。
    返回 0.0 ~ 1.0。
    """
    total = int(n_before or 0) + int(n_after or 0)
    if total > 0:
        frac = float(n_after or 0) / float(total)
    else:
        span = float(latest_ts - baseline_ts)
        if span <= 0:
            return 1.0
        frac = float(latest_ts - midnight_ms) / span
    return max(0.0, min(1.0, frac))


def _request_rows_cached(win_start_ms: float, win_end_ms: float) -> dict:
    """取窗口内的请求时间戳，返回 {account_id: [ts, ...]}。带 TTL 缓存。"""
    now = time.time()
    hit = _REQ_CACHE.get("payload")
    if hit is not None and (now - float(_REQ_CACHE.get("at") or 0.0)) < _REQ_TTL_SEC:
        return hit
    out = {}
    db = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "token_stats.db"
    try:
        if db.exists():
            import sqlite3
            conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
            try:
                # 多取一些（向前扩 24h），保证能覆盖各账号各异的 baseline
                lo = float(win_start_ms) - 86400_000.0
                for aid, ts in conn.execute(
                        "SELECT account_id, ts FROM usage_logs WHERE ts >= ? AND ts <= ?",
                        (lo, float(win_end_ms))):
                    if aid is None or ts is None:
                        continue
                    out.setdefault(str(aid), []).append(float(ts))
            finally:
                conn.close()
    except Exception:
        return out
    _REQ_CACHE["payload"] = out
    _REQ_CACHE["at"] = now
    return out


def _reattribute_today_usage(data: dict) -> float:
    """把后端今日桶里的跨天串账部分归还给昨天，返回挪走的总量。

    直接就地修正 data["accounts"] 的 usageToday 与 daily 桶（今日/昨日）。
    任何异常都吞掉并返回 0.0 —— 归因是增强，绝不能让积分页打不开。
    """
    try:
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            return 0.0
        snaps = _load_credit_snapshots()
        if not snaps:
            return 0.0

        now_ts = time.time()
        lt = time.localtime(now_ts)
        midnight_ts = time.mktime(time.struct_time(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, lt.tm_isdst)))
        midnight_ms = midnight_ts * 1000.0
        tmd = time.strftime("%Y-%m-%d", lt)
        ymd = time.strftime("%Y-%m-%d", time.localtime(midnight_ts - 86400))

        plan = _plan_from_snapshots(snaps, midnight_ms)
        if not plan:
            return 0.0

        rows = _request_rows_cached(min(p[0] for p in plan.values()),
                                    max(p[1] for p in plan.values()))

        moved = 0.0
        for a in accounts:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("accountId") or "")
            if aid not in plan:
                continue
            b_ts, l_ts, delta = plan[aid]
            nb = na = 0
            for ts in rows.get(aid, ()):
                if b_ts <= ts < midnight_ms:
                    nb += 1
                elif midnight_ms <= ts <= l_ts:
                    na += 1
            frac = _split_fraction(b_ts, l_ts, midnight_ms, nb, na)
            today_true = delta * frac
            carry = delta - today_true
            moved += carry

            a["usageToday"] = round(today_true, 4)
            dl = a.get("daily")
            if isinstance(dl, list):
                for row in dl:
                    if not isinstance(row, dict):
                        continue
                    rd = str(row.get("date") or "")
                    if rd == tmd:
                        row["usage"] = round(today_true, 4)
                    elif rd == ymd:
                        row["usage"] = round(float(row.get("usage") or 0.0) + carry, 4)
        if moved > 0:
            logging.info("[credits] 今日重归因：把 %.2f 从今日桶归还给昨日（快照稀疏导致跨天串账）",
                         moved)
        return moved
    except Exception:
        return 0.0


@app.get("/api/credits/stats")
async def credits_stats_proxy(request: Request, user: "str" = Depends(require_user)):
    """过滤官方 credits/stats 中的已删除僵尸账号，只保留当前账号池真实账号，并校准汇总指标。"""
    try:
        async with _internal_client(timeout=10.0) as client:
            r = await client.get(f"{BACKEND_URL}/api/credits/stats")
            if r.status_code != 200:
                return Response(content=r.content, status_code=r.status_code, media_type="application/json")
            data = r.json()
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502, media_type="application/json")

    # 读取当前有效的真实账号列表
    #
    # ⚠️ 修复（2026-10-08 实测）：原先「取到第一个非空就 break」，而
    # ``/data/.autobuddy/accounts.json`` 只有 5 个、``/data/.wb-switch/accounts.json``
    # 有 20 个（实测前者是后者的子集：交集 5、仅后者有 15），于是用了不完整的那份
    # 做过滤，把 15 个账号从积分统计里**误过滤**掉 —— 上游原本返回 34 个账号，
    # 过滤后只剩 5 个，但 summary 里 todayCheckedInAccounts=15，明显矛盾（那 15 个
    # 正在签到，却不在列表里）。
    #
    # 正确做法：两个来源取**并集**，谁都不漏。
    valid_account_ids = set()
    for acc_file in [Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "accounts.json",
                     get_switch_file("accounts.json")]:
        if not acc_file.exists():
            continue
        try:
            with open(acc_file, "r", encoding="utf-8") as f:
                acc_data = json.load(f)
            items = acc_data if isinstance(acc_data, list) else (
                acc_data.get("accounts") if isinstance(acc_data, dict) else None)
            if not isinstance(items, list):
                continue
            for a in items:
                if isinstance(a, dict) and a.get("id"):
                    valid_account_ids.add(str(a["id"]))
        except Exception:
            pass

    if valid_account_ids and "accounts" in data and isinstance(data["accounts"], list):
        # 仅保留真实存在的账号
        filtered_accounts = [a for a in data["accounts"] if str(a.get("accountId")) in valid_account_ids]
        data["accounts"] = filtered_accounts

        # ① 今日桶重归因：必须在汇总**之前**做，否则汇总会用上未修正的值。
        #    后端 daily 桶由约 5 小时一次的稀疏快照做差值，基准锚在昨晚上一条，
        #    于是把昨天晚间的消耗整段算进今天（实测 01:17 就显示 6371.68）。
        _reattribute_today_usage(data)

        # ② 7日 / 本月改为**由 daily 桶滚动求和**。
        #    实测后端本就是这么算的（25941.74 = 末7日求和、27807.18 = 10月求和，
        #    均精确匹配），这里显式重算的好处是：daily 桶每天自动前进一格，
        #    7日/本月就自动滚动刷新，不会像 officialUsage 那样停更 16 天；
        #    且今日桶修正后它们能立刻保持一致。
        _month_prefix = time.strftime("%Y-%m")
        for _a in filtered_accounts:
            _dl = _a.get("daily")
            if not isinstance(_dl, list):
                continue
            _by_date = {}
            for _row in _dl:
                if isinstance(_row, dict) and _row.get("date"):
                    try:
                        _by_date[str(_row["date"])] = float(_row.get("usage") or 0.0)
                    except Exception:
                        pass
            if not _by_date:
                continue
            _last7 = sorted(_by_date)[-7:]
            _a["usage7Days"] = round(sum(_by_date[d] for d in _last7), 2)
            _a["usageThisMonth"] = round(
                sum(v for d, v in _by_date.items() if d.startswith(_month_prefix)), 2)

        # 重新校准 summary
        total_remaining = sum((a.get("currentRemaining") or 0.0) for a in filtered_accounts)
        total_capacity = sum((a.get("totalCapacity") or 0.0) for a in filtered_accounts)
        usage_7d = sum((a.get("usage7Days") or 0.0) for a in filtered_accounts)
        usage_today = sum((a.get("usageToday") or 0.0) for a in filtered_accounts)
        usage_month = sum((a.get("usageThisMonth") or 0.0) for a in filtered_accounts)

        if "summary" in data and isinstance(data["summary"], dict):
            data["summary"]["currentRemaining"] = round(total_remaining, 2)
            data["summary"]["currentCapacity"] = round(total_capacity, 2)
            data["summary"]["usage7Days"] = round(usage_7d, 2)
            data["summary"]["usageToday"] = round(usage_today, 4)
            data["summary"]["usageThisMonth"] = round(usage_month, 2)

    _reconcile_official_usage(data, valid_account_ids)

    # 把「这份官方数据有多老」一并下发。
    # 采集环节在闭源引擎内、网关修不了，那就别让它静默过期 ——
    # 页面拿到 stale/reason 就能如实告诉用户，而不是显示一个
    # 谁也解释不了的「最近更新 9-24」。
    try:
        data["officialUsageFreshness"] = _official_usage_freshness(data)
    except Exception:
        # 元信息算不出来也不能连累主数据
        pass

    # 口径兜底的产物必须自己校验一遍：反代层发的 Response 不像 FastAPI 路由那样
    # 有框架兜底，一旦这里抛异常就会变成非 JSON 响应，前端只会拿到一句
    # "The string did not match the expected pattern"，看不出真因。
    # 所以出问题就把事故点带回日志，而不是顺着官方响应一起发给前端。
    if os.getenv("AB_DEBUG_RESPONSE", "").strip() in ("1", "true", "yes"):
        try:
            _chk = json.loads(json.dumps(data, ensure_ascii=False))
            _ou = ((_chk.get("officialUsage") or {}).get("summary") or {}).get("usageToday")
            _top = (_chk.get("summary") or {}).get("usageToday")
            logging.info("[credits] 口径对账 顶层=%s officialUsage=%s", _top, _ou)
        except Exception as exc:  # noqa: BLE001 - 自检不得反过来打断主流程
            logging.exception("[credits] 响应自检失败: %s", exc)

    return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")


@app.get("/api/token-stats")
async def token_stats_api(request: Request, user: "str" = Depends(require_user)):
    # 容器环境核心增强：接管 /api/token-stats，返回网关实测 Token 统计数据
    stats = get_aggregated_token_stats()
    return stats

@app.get("/api/reqstats")
async def reqstats_api(request: Request, limit: int = 50, user: "str" = Depends(require_user)):
    """请求级指标（成功率 / 耗时分位 / 最近请求）。

    转发到网关进程的 /reqlog 拿数据。

    ⚠️ 为什么必须走 HTTP 而不是 import reqlog：指标是**进程内内存态**，
    只有网关进程（18091）持有。WebUI 代理（18090）在另一个进程，
    直接 import 拿到的是另一个空实例，永远没有数据。
    与 token-stats 走 SQLite 可跨进程读不同，内存态指标必须经 HTTP 转发。

    归档只含元数据（模型 / 账号 / 耗时 / 状态 / 来源 IP 与 UA，受 AB_REQLOG_CLIENT 控制），
    不含提示词与凭证。
    """
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/reqlog", params={"limit": limit},
                                headers=_gw_headers(request))
            return Response(
                content=r.content, status_code=r.status_code,
                media_type="application/json",
            )
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}), status_code=502,
            media_type="application/json",
        )


GATEWAY_BASE_URL = os.getenv("AB_GATEWAY_BASE_URL", "http://127.0.0.1:18091")
GATEWAY_MODELS_URL = os.getenv("AB_GATEWAY_MODELS_URL", GATEWAY_BASE_URL + "/v1/models")


@app.get("/api/account-pool-official")
async def account_pool_get_official(request: Request, user: "str" = Depends(require_user)):
    """向官方网关直取的原始账号池状态（保留作为回退与排查对比入口）。

    注意：真正的 `/api/account-pool` 由下方 `account_pool_status_api()` 提供（带字段补充）。
    路由同名会让 FastAPI 只认先注册的那个，所以官方原始转发必须换个路径，
    否则补字段的逻辑永远不会生效。
    """
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/status",
                                headers=_gw_headers(request))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-pool-legacy")
async def account_pool_get_legacy(request: Request, user: "str" = Depends(require_user)):
    """官方账号池状态的原始转发（保留作为回退入口，供排查对比）。"""
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/status",
                                headers=_gw_headers(request))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-pool")
async def account_pool_get_v2(request: Request, user: "str" = Depends(require_user)):
    """替官方账号池状态补上“新账号”所需的展示字段（昵称/邮箱与账号级停用状态）。

    背景：官方 `/api/account-pool` 只在 `selectionCounts` 里回已有调用记录的
    账号名，新账号（未调用过）在那里既无昵称也无邮箱；前端只能拿账号 ID 去填
    卡片标题，而卡片标题一旦对不上任何键，启用/停用、设为首选、检测账号、
    调用次数四个控件就整块不渲染 —— 看上去就是「功能缺失」。

    这里只**补充**字段（nickname / email / disabled / disabledSource），
    原样保留官方的 mode / enabledAccountIds / selectionCounts 等，
    不动任何业务语义。同时把未入池的新账号补进 accounts 列表，
    让它们与已入池账号一样能被渲染。
    """
    try:
        async with _internal_client(timeout=8.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/status",
                                headers=_gw_headers(request))
            if r.status_code != 200:
                return Response(content=r.content, status_code=r.status_code,
                                media_type="application/json")
            data = r.json() or {}
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")

    # 账号池白名单（官方的 enabledAccountIds；空数组 + allEnabledByDefault=false
    # 在业务上等于“全部启用”，这里不重新解释，只原样透传）。
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    data["accounts"] = accounts

    # 账号池真实条目（昵称/邮箱的唯一来源）
    pool_accounts = {}
    for acc in _load_accounts_for_models():
        aid = str(acc.get("id") or acc.get("uid") or "")
        if aid:
            pool_accounts[aid] = acc

    # 账号级停用策略（手动/巡检），供卡片区分渲染
    disabled_map = {}
    try:
        disabled_map = account_policy.load_policy() or {}
    except Exception as e:
        print(f"[account-pool] 读账号停用策略失败: {e}")

    # 刷新令牌失效隔离：refresh_guard 标记「需人工重登」的账号集合，
    # 供卡片打「需重新登录」标记（与停用是两回事：停用 = 不参与轮询，
    # 需重登 = RT 已失效，但账号可能仍可用，只是要人去重新扫码）。
    needs_relogin = set()
    try:
        needs_relogin = refresh_guard.needs_relogin_ids() or set()
    except Exception as e:
        print(f"[account-pool] 读刷新失效标记失败: {e}")
    # 账号档案里自带的 needs_relogin（如官方禁用 / 重登失败写回 accounts.json
    # 的 needs_relogin_reason）也要并入 —— refresh_guard 只记「运行时刷新失败」，
    # accounts.json 里的标记是另一路来源，漏了它 Mr.H 这种号就只在角落里
    # 静悄悄挂着，横幅永远不出来（2026-10-20 工单：必须区域级醒目显示）。
    relogin_reasons = {}
    for src_acc in pool_accounts.values():
        if src_acc.get("needs_relogin"):
            aid2 = str(src_acc.get("id") or src_acc.get("uid") or "")
            if aid2:
                needs_relogin.add(aid2)
                relogin_reasons[aid2] = str(
                    src_acc.get("needs_relogin_reason") or "").strip()

    # 账号积分余额快照（供卡片排序第二法则：同状态积分降序）
    balances = {}
    try:
        from gateway import credit_floor
        balances = credit_floor._latest_balances() or {}
    except Exception:
        try:
            import credit_floor  # type: ignore
            balances = credit_floor._latest_balances() or {}
        except Exception:
            balances = {}

    enabled_ids = set(str(x) for x in (data.get("enabledAccountIds") or []))
    all_default = bool(data.get("allEnabledByDefault"))

    # 已有的账号条目：补 nickname / email / disabled / disabledSource / enabled / credits
    seen = set()
    for item in accounts:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or item.get("accountId") or "")
        if aid:
            seen.add(aid)
        src = pool_accounts.get(aid) or {}
        if not item.get("nickname"):
            item["nickname"] = (src.get("nickname") or src.get("email")
                                or item.get("name") or aid or None)
        if not item.get("email"):
            item["email"] = src.get("email") or None
        if not item.get("id") and aid:
            item["id"] = aid
        # 积分余额（支持排序第二法则）
        item["credits"] = float(balances.get(aid, 0.0) or 0.0)
        item["balance"] = item["credits"]
        # 账号级停用（普通正常账号也有 disabled=false，前端据此渲染启用态）
        item["disabled"] = aid in disabled_map
        item["disabledSource"] = disabled_map.get(aid)
        # 刷新令牌已失效（需人工重新登录）；原因一并带给前端横幅
        item["needsRelogin"] = aid in needs_relogin
        if item["needsRelogin"]:
            rr = str(src.get("needs_relogin_reason") or "").strip() \
                or relogin_reasons.get(aid, "")
            item["reloginReason"] = rr or "刷新令牌被上游拒绝"
        # 是否入池：给前端一个明确的布尔值，不必自己推白名单语义
        if all_default:
            item["enabled"] = not item["disabled"]
        else:
            item["enabled"] = (aid in enabled_ids) and not item["disabled"]

    # 未出现在账号池列表里的新账号（未入池、未调用过）全部补进去，
    # 否则它们的卡片永远拿不到昵称/邮箱，控制条也永远不出现。
    for aid, src in pool_accounts.items():
        if aid in seen:
            continue
        accounts.append({
            "id": aid,
            "accountId": aid,
            "name": src.get("nickname") or src.get("email") or aid,
            "nickname": src.get("nickname") or src.get("email") or aid,
            "email": src.get("email"),
            "variant": src.get("variant"),
            "enabled": (all_default or aid in enabled_ids) and aid not in disabled_map,
            "disabled": aid in disabled_map,
            "disabledSource": disabled_map.get(aid),
            "credits": float(balances.get(aid, 0.0) or 0.0),
            "balance": float(balances.get(aid, 0.0) or 0.0),
            "needsRelogin": aid in needs_relogin,
            "reloginReason": (str(src.get("needs_relogin_reason") or "").strip()
                              or relogin_reasons.get(aid, "")
                              or "刷新令牌被上游拒绝") if aid in needs_relogin else None,
            "count": 0,
        })

    return Response(content=json.dumps(data, ensure_ascii=False),
                    media_type="application/json")


@app.put("/api/account-pool")
async def account_pool_put(request: Request, user: "str" = Depends(require_user)):
    """转发账号池配置更新（模式 / 启用列表 / 首选账号）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.put(GATEWAY_BASE_URL + "/account-pool/config",
                                 content=body,
                                 headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-pool/selections")
async def account_pool_selections(request: Request, limit: int = 50, user: "str" = Depends(require_user)):
    """转发选账号分摊流水：各账号被分摊到的请求次数 + 最近明细。"""
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/selections",
                                 params={"limit": limit},
                                 headers=_gw_headers(request))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.put("/api/account-models")
async def account_models_put(request: Request, user: "str" = Depends(require_user)):
    """转发模型级禁用的切换请求（账号 x 模型）。

    前端点一下模型标签就走这里：网关那边只认 ``accountId`` + ``model`` + ``disabled``，
    这里不做任何业务判断，纯粹转发，把错误 detail 原样带回给 UI 弹提示。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.put(GATEWAY_BASE_URL + "/account-models/config",
                                 content=body,
                                 headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-models/restore")
async def account_models_restore(request: Request, user: "str" = Depends(require_user)):
    """转发「一键恢复」请求（把某账号被禁用的模型放回）。

    ``scope`` 由前端给：``all`` 全放回、``auto`` 只放回巡检禁的。
    同样纯转发，不做业务判断 —— 判定「哪些该恢复」是网关侧策略模块的职责。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-models/restore",
                                  content=body,
                                  headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-pool/selections/reset")
async def account_pool_selections_reset(request: Request, user: "str" = Depends(require_user)):
    """清空选账号流水，便于重新观测并发分摊。"""
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-pool/selections/reset",
                                  headers=_gw_headers(request))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/refresh-token")
async def refresh_token_proxy(request: Request, user: "str" = Depends(require_user)):
    """转发「刷新 Token」（账号卡片菜单）。

    超时给到 40s：要等上游刷新接口回包，默认值太紧会把「上游慢」
    误报成「刷新失败」。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=40.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/refresh-token",
                                  content=body,
                                  headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.post("/api/delete")
async def delete_account_proxy(request: Request, user: "str" = Depends(require_user)):
    """转发「删除账号」（账号卡片菜单）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=20.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/delete",
                                  content=body,
                                  headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.get("/api/export-accounts")
async def export_accounts_proxy(request: Request, user: "str" = Depends(require_user)):
    """转发「导出账号备份」。"""
    try:
        url = GATEWAY_BASE_URL + "/export-accounts"
        if request.url.query:
            url += f"?{request.url.query}"
        async with _internal_client(timeout=20.0) as client:
            r = await client.get(url, headers=_gw_headers(request))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.post("/api/import")
async def import_accounts_proxy(request: Request, user: "str" = Depends(require_user)):
    """转发「导入账号备份」（合并式，不覆盖未列出的账号）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=30.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/import",
                                  content=body,
                                  headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.post("/api/account-pool/toggle")
async def account_pool_toggle(request: Request, user: "str" = Depends(require_user)):
    """转发账号级停用切换（点击账号卡片上的「停用 / 启用」）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-pool/toggle",
                                  content=body,
                                  headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-pool/risk-flag/clear")
async def account_pool_risk_flag_clear(request: Request, user: "str" = Depends(require_user)):
    """转发「解除风控标记」（账号卡片浮层里的按钮）到网关本体。

    必须显式注册：文件末尾有一条 catch-all `/{path:path}`，不注册的话这个
    POST 会被它转给官方后端，官方不认识该路径却仍返回 200，前端看到成功、
    标记实际没被清除（2026-10-21 实测踩到）。

    实现只此一处（gateway/main.py），这里纯转发，避免两份逻辑漂移。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-pool/risk-flag/clear",
                                  content=body,
                                  headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-health/probe")
async def account_health_probe(request: Request, user: "str" = Depends(require_user)):
    """转发「检测账号」：发一次轻量鉴权请求，只回报结论、不改配置。

    超时给到 45s —— 探测本身要等上游回第一个字节，5s 的默认值太紧，
    会把「上游慢」误报成「检测失败」。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=45.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-health/probe",
                                  content=body,
                                  headers=_gw_headers(request, body))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-health/audit")
async def account_health_audit(request: Request, user: "str" = Depends(require_user)):
    """转发「一致性自检」：把凭据与模型两侧的结论并排给出。

    超时给到 120s —— 这个接口会对每个账号跑一次凭据探测加数次模型探测，
    账号多时耗时明显长于单账号检测。用 45s 会在账号稍多时稳定超时。
    """
    try:
        async with _internal_client(timeout=120.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-health/audit",
                                headers=_gw_headers(request))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


# ---------------------------------------------------------------------------
# API 接入信息与访问密钥的转发（WebUI 设置页用）
#
# 这些路由必须注册在文件末尾的 catch-all 之前 —— Starlette 按注册顺序匹配，
# 否则会被 `/{path:path}` 抢走并转发到官方后端 57890（那边没有这些接口）。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 转发到网关（main.py，18091）时的登录态透传
#
# 背景（v1.0.18 安全修复）：网关本体此前 37 个路由里 32 个零鉴权，
# 未登录 POST /import 即可往账号池合并写入任意账号（线上实测两次）。
# 现在这些端点统一挂了 require_user 门，与 WebUI 侧同一套会话判定。
#
# 于是这里多了一件事：**WebUI 代理转发到网关时必须把用户的会话 Cookie 带上**。
# 转发是容器内 loopback HTTP 调用，不带 Cookie 的话网关看到的就是"未登录"，
# 整个账号管理面板会全部 401 —— 那不是修好了安全，是把自家前端打断了。
#
# 判据就是浏览器那个 `autobuddy_session` Cookie（见 db.create_session）：
# 它在 WebUI 登录时种下、验证走 db.validate_session，网关侧读的是同一个 db。
# ---------------------------------------------------------------------------
GW_SESSION_COOKIE = "autobuddy_session"


def _gw_headers(request: Optional[Request], body: Optional[bytes] = None) -> Dict[str, Any]:
    """构造转发到网关的请求头：带 Content-Type 与登录态 Cookie。"""
    headers: Dict[str, Any] = {"Content-Type": "application/json"} if body else {}
    try:
        token = (request.cookies or {}).get(GW_SESSION_COOKIE) if request is not None else None
    except Exception:
        token = None
    if token:
        headers["Cookie"] = f"{GW_SESSION_COOKIE}={token}"
    return headers


async def _forward_gateway(method: str, path: str, body: Optional[bytes] = None,
                           request: Optional[Request] = None):
    """把请求转发到网关（18091）。失败返回 502 而不是让页面白屏。

    ``request`` 用于透传登录态 Cookie（见上方 _gw_headers 说明）——
    网关的管理端点现已挂 require_user 门，不带会话会被判 401。
    """
    try:
        headers = _gw_headers(request, body)
        async with _internal_client(timeout=10.0) as client:
            r = await client.request(method, GATEWAY_BASE_URL + path,
                                     content=body, headers=headers or None)
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/model-health")
async def model_health_get(request: Request, user: "str" = Depends(require_user)):
    """巡检配置 + 可选账号 + 上次运行状态（设置页面板数据源）。"""
    return await _forward_gateway("GET", "/model-health/config", request=request)


# ---------------------------------------------------------------------------
# 通知管理（参照 MoviePilot 的通知配置模型，首渠道：企业微信）。
# 数据存本地 notify_config.json，不转发网关 —— 设置页直接读写本代理。
# GET 只回脱敏视图（secret 只回 has_value 布尔，绝不回明文）。
# ---------------------------------------------------------------------------
@app.get("/api/notify")
async def notify_get(user: "str" = Depends(require_user)):
    try:
        return _notify.public_view()
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)[:120]}),
                        status_code=500, media_type="application/json")


@app.put("/api/notify")
async def notify_put(request: Request, user: "str" = Depends(require_user)):
    """保存通知配置。

    MoviePilot 同款行为：配置就绪后**自动注册企微菜单**（幂等覆盖式更新），
    成员在应用底部点按钮即可交互，不必手打命令。菜单创建失败不影响配置保存
    （菜单需要可信 IP / 应用可用等额外条件，失败只记录、返回 menu 字段供前端提示）。
    """
    try:
        payload = json.loads(await request.body() or b"{}")
        _notify.update_config(payload)
        menu = None
        try:
            wecom_cfg = _notify._load().get("channels", {}).get("wecom") or {}
            ready = bool(str(wecom_cfg.get("corpid") or "").strip()
                         and str(wecom_cfg.get("secret") or "").strip()
                         and str(wecom_cfg.get("agentid") or "").strip())
            if ready:
                menu = _notify.create_wecom_menus(wecom_cfg)
        except Exception as e:
            menu = {"ok": False, "error": str(e)[:120]}
        # Telegram：配置就绪即自动注册命令菜单（MoviePilot 的 register_commands 同款），
        # 并启动长轮询收消息 —— Telegram 走 getUpdates，**不需要公网入口**。
        tg = None
        try:
            tg_cfg = _notify._load().get("channels", {}).get("telegram") or {}
            if str(tg_cfg.get("bot_token") or "").strip():
                tg = _notify.telegram_register_commands(tg_cfg)
                started = _notify.start_telegram_polling()
                if isinstance(tg, dict):
                    tg["polling"] = bool(started)
        except Exception as e:
            tg = {"ok": False, "error": str(e)[:120]}
        return {"ok": True, "menu": menu, "telegram": tg}
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)[:120]}),
                        status_code=500, media_type="application/json")


@app.post("/api/notify/menu")
async def notify_menu(request: Request, user: "str" = Depends(require_user)):
    """手动注册/更新企微自定义菜单（幂等）。body: {"action": "create"|"delete"}"""
    try:
        payload = json.loads(await request.body() or b"{}")
        action = str(payload.get("action") or "create").strip().lower()
        if action == "delete":
            return _notify.delete_wecom_menus()
        return _notify.create_wecom_menus()
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)[:120]}),
                        status_code=500, media_type="application/json")


@app.post("/api/notify/test")
async def notify_test(request: Request, user: "str" = Depends(require_user)):
    """测试连通性：可选先保存表单再真发一条。"""
    try:
        payload = json.loads(await request.body() or b"{}")
        if payload.get("save"):
            _notify.update_config(payload.get("config") or {})
        channel = str(payload.get("channel") or "")
        text = str(payload.get("text") or "")
        r = _notify.send_test(channel, text)
        return r
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)[:120]}),
                        status_code=500, media_type="application/json")


@app.post("/api/notify/probe")
async def notify_probe(request: Request, user: "str" = Depends(require_user)):
    """渠道连通性自检（不发消息）。当前支持 telegram（getMe 验 Token）。"""
    try:
        payload = json.loads(await request.body() or b"{}")
        channel = str(payload.get("channel") or "").strip()
        if payload.get("save"):
            _notify.update_config(payload.get("config") or {})
        if channel == "telegram":
            cfg = _notify._load().get("channels", {}).get("telegram") or {}
            return _notify.telegram_get_me(cfg)
        return {"ok": False, "error": "该渠道暂无自检：%s" % (channel or "未指定")}
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)[:120]}),
                        status_code=500, media_type="application/json")


# ---------------------------------------------------------------------------
# 智能助手配置（第三阶段：设置页可视化配置，参考 MoviePilot 的 LLMProviderInfo）
#
# 用户需求（2026-10-14）：「在设置页面添加智能助手模型配置，而不是你自己写，
# 参考 MoviePilot 的相关配置，默认自带模型提供，也可以配置其他模型」。
#
# 接口分工：
#   GET  /api/assistant          注册表 + 当前配置（密钥只回 has_value）
#   PUT  /api/assistant          保存配置（secret：不提交=保留/空串=清除/有值=覆盖）
#   POST /api/assistant/models   拉取某 provider 的模型清单（弹窗里刷新用）
#   POST /api/assistant/test     连通性测试：用当前配置问一句，看能否拿到回答
# ---------------------------------------------------------------------------
try:
    from gateway import assistant as _assistant
except ImportError:
    import assistant as _assistant  # type: ignore[no-redef]


@app.get("/api/assistant")
async def assistant_get(user: "str" = Depends(require_user)):
    """助手配置视图：provider 注册表 + 当前配置（脱敏）。"""
    try:
        return _assistant.public_view()
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)[:120]}),
                        status_code=500, media_type="application/json")


@app.put("/api/assistant")
async def assistant_put(request: Request, user: "str" = Depends(require_user)):
    """保存助手配置（合并式；secret 语义与通知渠道一致）。

    ⚠️ 2026-10-14 修复（用户反馈「点击 OpenAI 兼容接口直接卡死」）：

    原实现在保存后**同步跑一次 LLM 探测**，实测耗时 **102.1 秒**
    （探测要跑两轮 LLM：45s 超时 ×2 + 工具调用）。前端 await 同一个请求，
    页面看着就像卡死；总开关按钮也走这里，等于点一下也要等 100 秒。

    现在保存只落盘、**绝不探测** —— 想验证连通性请用「测试连通性」按钮
    （POST /api/assistant/test，那是用户主动发起、有明确等待预期的动作）。

    ``probe`` 参数可选：只有前端明确传 ``probe: true`` 时才探测
    （向后兼容旧调用方），且探测失败**不影响**保存结果。
    """
    try:
        payload = json.loads(await request.body() or b"{}")
        _assistant._save_config(payload)
        # 校验当前配置是否「可用」：切到 openai 但没填密钥时明确告诉前端，
        # 让它提示用户去补配置 —— 而不是把坏配置静默生效。
        warn = None
        try:
            rt = _assistant._runtime()
            if rt.get("enabled") and rt.get("provider") != "gateway" \
                    and not rt.get("api_key"):
                warn = ("已切换到「%s」，但尚未填写 API Key —— 助手暂时无法回答，"
                        "请补配置后点「测试连通性」验证。"
                        % (_PROVIDER_NAME.get(rt.get("provider")) or rt.get("provider")))
        except Exception:
            warn = None
        probe = None
        if payload.get("probe") is True:
            try:
                probe = await _assistant_probe(payload)
            except Exception:
                probe = None
        return {"ok": True, "warn": warn, "probe": probe,
                "runtime": _assistant.session_stats()}
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)[:120]}),
                        status_code=500, media_type="application/json")


# provider key -> 中文名（保存后给前端的提示用，避免再查一次注册表）
_PROVIDER_NAME = {p["key"]: p.get("name") for p in _assistant.PROVIDERS}


async def _assistant_probe(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """用给定（或当前）配置问一句，验证能否真正拿到回答。

    与「拉模型清单」的区别：拉清单只证明**地址可达**，probe 证明
    **能完成一次对话**（密钥有效、模型名对、配额没爆）—— 后者才是用户关心的。
    """
    import time as _t
    t0 = _t.time()
    try:
        reply = _assistant.assistant_reply("probe-%d" % int(t0), "用一句话说明你是谁")
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:100])}
    if not reply:
        return {"ok": False, "error": "未收到回答（检查地址、密钥与模型名）"}
    return {"ok": True, "durationMs": int((_t.time() - t0) * 1000),
            "replyPreview": reply[:200]}


@app.post("/api/assistant/test")
async def assistant_test(request: Request, user: "str" = Depends(require_user)):
    """连通性测试（MoviePilot 的 LLMTestResult 同款语义）。"""
    try:
        payload = json.loads(await request.body() or b"{}")
        if payload.get("save"):
            _assistant._save_config(payload.get("config") or {})
        return await _assistant_probe(payload.get("config"))
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@app.post("/api/assistant/models")
async def assistant_models(request: Request, user: "str" = Depends(require_user)):
    """拉取指定 provider 的模型清单（弹窗「刷新」按钮用）。"""
    try:
        payload = json.loads(await request.body() or b"{}")
        base = str(payload.get("base_url") or "").strip().rstrip("/")
        key = str(payload.get("api_key") or "").strip()
        if not base:
            return {"ok": False, "error": "请先填写接口地址"}
        d = _assistant._http_json(base + "/v1/models", timeout=15.0, api_key=key) or {}
        ids = [str(m.get("id")) for m in (d.get("data") or []) if m.get("id")]
        if not ids:
            return {"ok": False, "error": "该地址未返回模型清单（可能不是 OpenAI 兼容接口）"}
        return {"ok": True, "models": ids, "count": len(ids)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:100])}


@app.post("/api/assistant/search-test")
async def assistant_search_test(request: Request, user: "str" = Depends(require_user)):
    """搜索引擎连通性测试（2026-10-16 新增）。

    用户诉求：「每个人可以在前端 UI 可以选择常见搜索引擎输入 API
    配置自己的搜索引擎」—— 填完密钥总得有个地方能立刻验证对错，
    否则只能等问答时才发现配错了。

    语义：用当前填的配置真发一次检索（query 固定为一句无害的探针词），
    返回能否拿到结果 + 首条标题，**不落盘**（测试不该有副作用）。
    """
    import time as _t
    t0 = _t.time()
    try:
        payload = json.loads(await request.body() or b"{}")
        cfg_in = payload.get("config") or {}
        engine = str(cfg_in.get("searchProvider") or "").strip()
        if not engine:
            return {"ok": False, "error": "请先选择搜索引擎"}
        spec = _assistant._SEARCH_BY_KEY.get(engine)
        if not spec:
            return {"ok": False, "error": "未知搜索引擎：%s" % engine}
        slot = ((cfg_in.get("searchProviders") or {}).get(engine)) or {}
        api_key = str(slot.get("api_key") or "").strip()
        base = str(slot.get("base_url") or "").strip() or spec.get("default_base_url") or ""
        extra = slot.get("extra") if isinstance(slot.get("extra"), dict) else {}
        if spec.get("supports_api_key") and not api_key:
            return {"ok": False, "error": "请先填写 %s 的 API Key"
                    % (spec.get("api_key_label") or "API Key")}
        if engine == "google_cse" and not str(extra.get("cx") or "").strip():
            return {"ok": False, "error": "Google CSE 还需要填写「搜索引擎 ID（cx）」"}
        cr = {"ok": True, "key": engine, "spec": spec,
              "base_url": base.rstrip("/"), "api_key": api_key,
              "extra": extra, "name": spec.get("name") or engine}
        adapt = _assistant._SEARCH_ADAPTERS.get(engine)
        if not adapt:
            return {"ok": False, "error": "引擎 %s 尚未实现适配器" % engine}
        d = adapt(cr, "AutoBuddy 网关", 3, "general")
        if not d:
            return {"ok": False,
                    "error": "未返回可用结果：请检查接口地址与密钥是否正确",
                    "durationMs": int((_t.time() - t0) * 1000)}
        results = [r for r in (d.get("results") or []) if isinstance(r, dict)]
        first = results[0] if results else None
        if not first:
            return {"ok": False, "error": "接口可达但没有搜索结果",
                    "durationMs": int((_t.time() - t0) * 1000)}
        return {"ok": True, "engine": spec.get("name") or engine,
                "title": str(first.get("title") or "")[:80],
                "url": str(first.get("url") or "")[:160],
                "count": len(results),
                "durationMs": int((_t.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}


# ---------------------------------------------------------------------------
# 企微回调（接收消息服务器）：echo 校验 + 消息接收 + 被动回复。
#
# 这个端点**不设登录鉴权** —— 调用方是企微服务器，鉴权靠的是消息体签名
# （SHA1(token, timestamp, nonce, 密文)，密钥只有双方知道），比 cookie 更强。
# 路径刻意带随机段（/api/notify/wecom/callback/<path>），供企微后台配置；
# 经 FRP（VPS 10083 → 本机 18090）公网可达。
# ---------------------------------------------------------------------------
@app.get("/api/notify/wecom/callback/{path}")
async def wecom_callback_echo(path: str, request: Request):
    """GET：企微保存配置时的 URL 可达性验证（echostr 解密回显）。"""
    try:
        cfg = _notify._load()["channels"].get("wecom") or {}
        token = str(cfg.get("cb_token") or "").strip()
        aes_key = str(cfg.get("cb_aes_key") or "").strip()
        corpid = str(cfg.get("corpid") or "").strip()
        if not (token and aes_key):
            return Response(content="callback not configured", status_code=404)
        q = request.query_params
        echo = _notify.verify_echo(token, aes_key, corpid,
                                   q.get("echostr") or "",
                                   q.get("msg_signature") or "",
                                   q.get("timestamp") or "",
                                   q.get("nonce") or "")
        return Response(content=echo, media_type="text/plain")
    except Exception as e:
        logging.warning("[notify] wecom echo 校验失败: %s", str(e)[:120])
        return Response(content="verify fail", status_code=403)


@app.post("/api/notify/wecom/callback/{path}")
async def wecom_callback_msg(path: str, request: Request):
    """POST：企微推送消息 → 解密 → 自动回复（加密回包）。"""
    try:
        cfg = _notify._load()["channels"].get("wecom") or {}
        token = str(cfg.get("cb_token") or "").strip()
        aes_key = str(cfg.get("cb_aes_key") or "").strip()
        corpid = str(cfg.get("corpid") or "").strip()
        if not (token and aes_key):
            return Response(content="callback not configured", status_code=404)
        q = request.query_params
        body = (await request.body()).decode("utf-8", errors="replace")
        # 企微 POST 的 body 是 xml，密文在 <Encrypt> 里
        m = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]></Encrypt>", body, re.S)
        if not m:
            return Response(content="bad request", status_code=400)
        xml_text = _notify.decrypt_message(token, aes_key, corpid,
                                           m.group(1),
                                           q.get("msg_signature") or "",
                                           q.get("timestamp") or "",
                                           q.get("nonce") or "")
        msg = _notify.parse_message(xml_text)
        content = (msg.get("Content") or "").strip()
        from_user = msg.get("FromUserName") or ""
        to_user = msg.get("ToUserName") or ""
        # 交互命令：全部内置，无需辅助容器。所有指令都是被动回复（5s 内可完成，
        # 不会触发企微的重推）。需要长时间运行的（如每日任务）只回“已启动”，
        # 结果后续主动推送。
        #
        # 菜单点击走 event 消息：命令在 EventKey（如 AB_STATUS），没有 Content。
        # 必须在这里映射成文本命令，否则点按钮毫无反应 —— 只靠「用户手打命令」
        # 不算交互（MoviePilot 是自动注册菜单，点按钮即触发）。
        event_key = (msg.get("EventKey") or "").strip()
        if event_key:
            content = _notify.MENU_KEY_TO_CMD.get(event_key.upper(), "")
            if not content:
                content = "help"
        cmd = content.lower().strip()
        # 命令语义**唯一实现**在 notify.wecom_command_reply()（企微/Telegram 共用）。
        # 此前这里内联了一份 if/elif 命令链，与 notify._wecom_interpret 各改一半，
        # 导致「改了 notify.py 菜单却没变」—— 回调实际走的是本文件这份。
        # 2026-10-13 用户二次反馈后收拢：本文件只做加解密，不含命令语义。
        #
        # ---- MP 模式（2026-10-15 重写）----
        #
        # 关键参照：MoviePilot `app/application/messaging/ingress.py`
        #     def submit_message_to_host(payload, source, *, submit, timeout=15):
        #         submit(forward_message_to_host, dict(payload), source, ...)
        #         return True        # ← 立即返回 True，**不等结果**
        #
        # MP 日志实证（用户实测 6 分钟才回复）：
        #     18:04:11.619  POST /api/v1/message/... 200 OK   ← 回调立即返回
        #     18:04:11.626  收到微信消息：最近有哪些国内热门电影
        #     18:04:17.281  执行工具 search_web（8694ms）
        #     ...（5分41秒后台推理）...
        #     18:10:27.679  发送消息：根据搜索结果，以下是…
        #
        # 回调响应与实际答复**完全解耦**：回调只回“我收到了”，答复走
        # 另一条路（主动推送）。所以 MP 能跑 6 分钟不超时 ——
        # 而之前我们把答复塞进 5 秒内，复杂问题永远答不好。
        #
        # 与之配套（重要）：wecom_command_reply 不再传 allow_defer ——
        # 不再有「正在查询，请稍候」这类回执，用户发问后直接等答案。
        threading.Thread(
            target=_wecom_handle_async,
            args=(cmd, from_user or None),
            name="ab-wecom-handle", daemon=True).start()
        # 立即返回空串：企微只要求 5s 内响应，不要求响应体内容。
        return Response(content="", media_type="text/plain")
    except Exception as e:
        logging.warning("[notify] wecom 回调处理失败: %s", str(e)[:120])
        return Response(content="fail", status_code=403)


def _wecom_handle_async(cmd: str, session_id: Optional[str]) -> None:
    """后台处理企微消息，并把结果主动推送回去（MP 的 submit 模式）。

    跑多久都行 —— 回调早已返回，没有任何超时约束。
    只读命令会很快出结果；需要 LLM/联网的问题慢慢算，算完推送。
    """
    try:
        # allow_defer=False：这里已是后台线程，不需要再「先回执 + 后台推送」，
        # 直接同步把答案算出来，然后把答案本身推给用户。
        reply = _notify.wecom_command_reply(cmd, session_id, False)
        if not reply:
            return
        ok = _notify.push_text_to_wecom(reply)
        if not ok:
            logging.warning("[notify] 企微后台答复推送失败（session=%s）", session_id)
    except Exception as e:
        logging.warning("[notify] 企微后台处理异常：%s: %s",
                        type(e).__name__, str(e)[:150])


@app.put("/api/model-health")
async def model_health_put(request: Request, user: "str" = Depends(require_user)):
    """更新巡检配置（开关 / 间隔 / 账号范围 / 自动启用）。"""
    return await _forward_gateway("PUT", "/model-health/config", await request.body(), request=request)


@app.post("/api/model-health/run")
async def model_health_run(request: Request, user: "str" = Depends(require_user)):
    """立即执行一轮巡检。

    探测是逐个账号 × 模型发真实请求，可能耗时较久，因此这里放宽超时
    （默认 5s 的内部客户端不够用）。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=300.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/model-health/run",
                                  content=body or b"{}",
                                  headers=_gw_headers(request, body or b"{}"))
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/gateway-info")
async def gateway_info_api(request: Request, user: "str" = Depends(require_user)):
    """连接信息：对外地址、端点、模型/账号规模、密钥状态。

    2026-10-21 v1.0.18 起从公开白名单里移出并挂上 require_user：
    它转发到的网关 /gateway/info 已挂门（响应含账号规模、密钥统计等
    侦察信息），代理侧若继续公开，等于换个路径就能无凭据读到同一份数据。
    三个使用点（账号池统计 / KPI / 关于页）都在登录后的管理界面里，
    前端对失败都有 `r.ok ? r.json() : {}` 兜底，不会白屏。
    """
    return await _forward_gateway("GET", "/gateway/info", request=request)


@app.get("/api/api-keys")
async def api_keys_get(request: Request, user: "str" = Depends(require_user)):
    return await _forward_gateway("GET", "/api-keys/status", request=request)


@app.post("/api/api-keys")
async def api_keys_create(request: Request, user: "str" = Depends(require_user)):
    return await _forward_gateway("POST", "/api-keys", await request.body(), request=request)


@app.post("/api/api-keys/update")
async def api_keys_update(request: Request, user: "str" = Depends(require_user)):
    return await _forward_gateway("POST", "/api-keys/update", await request.body(), request=request)


@app.post("/api/api-keys/reset-stats")
async def api_keys_reset_stats(request: Request, user: "str" = Depends(require_user)):
    return await _forward_gateway("POST", "/api-keys/reset-stats", await request.body(), request=request)


@app.post("/api/api-keys/delete")
async def api_keys_delete(request: Request, user: "str" = Depends(require_user)):
    return await _forward_gateway("POST", "/api-keys/delete", await request.body(), request=request)


@app.post("/api/api-keys/delete-all")
async def api_keys_delete_all(request: Request, user: "str" = Depends(require_user)):
    return await _forward_gateway("POST", "/api-keys/delete-all", request=request)


@app.put("/api/api-keys/config")
async def api_keys_config(request: Request, user: "str" = Depends(require_user)):
    return await _forward_gateway("PUT", "/api-keys/config", await request.body(), request=request)




@app.post("/api/gateway-selftest")
async def gateway_selftest(request: Request, user: "str" = Depends(require_user)):
    """从容器内实测一次网关连通性。

    浏览器无法直接访问 18091（跨源且网关无 CORS），所以自检必须由代理侧发起：
    loopback 请求免密钥校验，正好验证「网关活着 + 模型清单可用 + 密钥配置自洽」。

    2026-10-23 修复：签名此前漏了 ``request`` 参数，却在第三项
    （密钥配置自洽性）里把它传给 ``_gw_headers(request)`` 去透传登录态
    Cookie —— 于是自检第三步必抛 ``NameError: name 'request' is not defined``，
    被下面的 except 捕获后渲染成一行红字。前两步不依赖 request，所以
    ​一直显示绿，故障长这样：
       ✓ 网关健康检查 /health — HTTP 200 · 39 个模型 · 密钥校验已开启
       ✓ 模型清单 /v1/models — HTTP 200 · 39 个模型
       ✗ 密钥配置自洽性 — name 'request' is not defined
    加上参数即修复（FastAPI 按类型注解注入 Request，与同文件其它代理端点一致）。
    """
    steps = []
    out = {"steps": steps}

    async with _internal_client(timeout=8.0) as client:
        try:
            r = await client.get(GATEWAY_BASE_URL + "/health")
            ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
            if ok:
                try:
                    h = r.json()
                    out["models"] = h.get("models_count")
                    out["requireKey"] = h.get("require_api_key")
                    detail = f"HTTP 200 · {h.get('models_count')} 个模型 · 密钥校验{'已开启' if h.get('require_api_key') else '未开启'}"
                except Exception:
                    pass
            steps.append({"name": "网关健康检查 /health", "ok": ok, "detail": detail})
        except Exception as e:
            steps.append({"name": "网关健康检查 /health", "ok": False, "detail": str(e)})

        try:
            r = await client.get(GATEWAY_BASE_URL + "/v1/models")
            count = 0
            if r.status_code == 200:
                try:
                    count = len((r.json() or {}).get("data") or [])
                except Exception:
                    count = 0
            steps.append({
                "name": "模型清单 /v1/models",
                "ok": r.status_code == 200,
                "detail": f"HTTP {r.status_code} · {count} 个模型",
            })
        except Exception as e:
            steps.append({"name": "模型清单 /v1/models", "ok": False, "detail": str(e)})

        try:
            r = await client.get(GATEWAY_BASE_URL + "/api-keys/status",
                                headers=_gw_headers(request))
            keys = {}
            if r.status_code == 200:
                try:
                    keys = (r.json() or {})
                except Exception:
                    keys = {}
            require = bool(keys.get("requireKey"))
            enabled = int((keys.get("stats") or {}).get("enabled") or 0)
            ok = not require or enabled > 0
            detail = ("已开启校验，可用密钥 %d 个" % enabled) if require else "未开启校验，接口开放访问"
            steps.append({"name": "密钥配置自洽性", "ok": ok, "detail": detail})
        except Exception as e:
            steps.append({"name": "密钥配置自洽性", "ok": False, "detail": str(e)})

    out["ok"] = all(s["ok"] for s in steps)
    return out


async def _fetch_gateway_catalog() -> list:
    """取网关（18091）可路由的完整模型清单。

    网关的清单 = 内置基础清单 + 从官方 usage 自动发现出来的模型，是「当前能调
    用哪些模型」的唯一权威来源。取不到时降级为空数组，由调用方回退到 usage。
    """
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_MODELS_URL)
            if r.status_code != 200:
                return []
            data = r.json() or {}
            return [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except Exception:
        return []


def _load_accounts_for_models() -> list:
    """读账号池，**合并两份账号文件**（按 ID 去重，字段互补）。

    两个文件各有各的盲区，只取其一必然漏账号：
      - ``/data/.wb-switch/accounts.json``：官方维护的那份，用户新加入的
        账号（如「一杯美式」）先出现在这里；
      - ``$AB_DATA_DIR/accounts.json``：容器自己的副本，字段更全。

    历史 bug：早先写的是「找到第一个存在的文件就 break」，于是只认自己那份，
    官方新加的账号根本进不了 /api/account-models 与 /api/account-pool，
    表现为「新账号的卡片上没有任何控件」（启用/设为首选/检测/调用次数全无）。

    合并原则：先官方后本地覆盖，同 ID 以本地为准；读不到就返回空列表，
    绝不因账号文件异常而让调用方失败。
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for acc_file in [get_switch_file("accounts.json"),
                     Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "accounts.json"]:
        if not acc_file.exists():
            continue
        try:
            with open(acc_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        if not isinstance(data, list):
            continue
        for acc in data:
            if not isinstance(acc, dict):
                continue
            key = str(acc.get("id") or acc.get("uid") or "")
            if not key:
                # 没 ID 的条目按昵称/邮箱兜底，总比整条丢掉强。
                key = str(acc.get("nickname") or acc.get("email") or "")
            if not key:
                continue
            # 后遍历的（本地那份）覆盖先遍历的（官方那份）—— 本地字段更全。
            merged[key] = {**(merged.get(key) or {}), **acc}
    return list(merged.values())


@app.get("/api/account-models")
async def account_models_api(request: Request, user: "str" = Depends(require_user)):
    """按账号返回「可用模型 + 已调用模型」，供账号卡片动态展示。

    两个数据源合并，缺一不可：

    1. **网关自身流水**（`token_stats_logs.json`，带 accountId/accountName）——
       实时、覆盖所有账号，是「这个账号到底调用过哪些模型」的权威来源。
    2. **官方 usage 缓存**（`official_usage_cache.json`）—— 上游只对「当前账号」
       返回模型明细，其余账号的 `models` 恒为空，因此只能作为补充。

    只靠官方缓存会出现「只有一个账号有调用记录」的假象，这正是必须叠加网关归因的原因。
    """
    import json as _json
    data_dir = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
    cache_file = data_dir / "official_usage_cache.json"
    tracker_file = data_dir / "token_stats_logs.json"

    catalog = await _fetch_gateway_catalog()
    result = {"accounts": {}, "catalog": catalog, "discovered": []}

    # 模型级禁用策略：由网关（18091）持有并落盘，这里读一份用于渲染禁用态。
    # 读不到就当空策略 —— UI 显示「全部可用」，绝不因为策略读取失败而让整卡消失。
    # policySources 让卡片能区分「手动禁用」与「巡检禁用」，两者恢复方式不同。
    disabled_by_account = {}
    disabled_sources_by_account = {}
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-models/config",
                                headers=_gw_headers(request))
            if r.status_code == 200:
                body = r.json() or {}
                disabled_by_account = body.get("policy") or {}
                disabled_sources_by_account = body.get("policySources") or {}
    except Exception:
        disabled_by_account = {}
        disabled_sources_by_account = {}

    agg = {}

    def _touch(account_id, account_name):
        entry = agg.get(account_id)
        if entry is None:
            entry = {"name": account_name, "used": set(), "usage": {}, "gatewayCalls": 0}
            agg[account_id] = entry
        elif not entry.get("name") and account_name:
            entry["name"] = account_name
        return entry

    # ---- 来源 1：网关自身归因（实时、全账号） ----
    #
    # 走 load_tracker_records()，**不要**直接读 token_stats_logs.json：
    # v0.9.29 起明细已迁到 SQLite，那个 JSON 自迁移后就不再写入，
    # 直接读它只会拿到迁移当天为止的旧数据 —— 新账号（如 17521565831）
    # 明明有调用却显示不出任何模型（v0.9.30 修复的回归）。
    try:
        tracker_logs = load_tracker_records() or []
    except Exception:
        # 兜底：新路径失败时退回旧文件，至少不整页空白
        try:
            with open(tracker_file, "r", encoding="utf-8") as f:
                tracker_logs = _json.load(f) or []
        except Exception:
            tracker_logs = []

    for rec in tracker_logs:
        if not isinstance(rec, dict):
            continue
        aid = rec.get("accountId")
        model = rec.get("model")
        if not aid or not model:
            continue
        entry = _touch(str(aid), rec.get("accountName"))
        entry["used"].add(str(model))
        entry["gatewayCalls"] += 1
        stat = entry["usage"].setdefault(str(model), {"requests": 0, "credit": None, "gatewayCalls": 0})
        stat["requests"] = (stat.get("requests") or 0) + 1
        stat["gatewayCalls"] = (stat.get("gatewayCalls") or 0) + 1
        stat["tokens"] = (stat.get("tokens") or 0) + int(rec.get("total") or 0)

    # ---- 来源 2：官方 usage 缓存（可能只覆盖当前账号） ----
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            payload = (_json.load(f) or {}).get("payload") or {}
    except Exception:
        payload = {}

    for acc in payload.get("accounts") or []:
        if not isinstance(acc, dict) or not acc.get("accountId"):
            continue
        entry = _touch(acc["accountId"], acc.get("accountName"))
        for day in acc.get("daily") or []:
            for m in (day or {}).get("models") or []:
                if isinstance(m, dict) and m.get("model"):
                    entry["used"].add(m["model"])

    # payload.models / requests 补充调用次数与积分，并兜底补齐 model
    for item in payload.get("models") or []:
        if isinstance(item, dict) and item.get("model"):
            for aid in list(agg.keys()):
                stat = agg[aid]["usage"].setdefault(item["model"], {"requests": None, "credit": None})
                if stat.get("credit") is None:
                    stat["credit"] = item.get("credit")

    for req in payload.get("requests") or []:
        if not isinstance(req, dict) or not req.get("accountId") or not req.get("model"):
            continue
        entry = _touch(req["accountId"], req.get("accountName"))
        entry["used"].add(req["model"])

    # 兜底：某账号可能「一条调用记录都没有，但已被用户禁用过模型」。
    # 不补进 agg 的话，它的禁用状态会静默丢失，用户会以为点了没生效。
    #
    # ‼️ 但必须先确认这个账号**现在还在账号池里**。
    # 这里曾经无约束地把策略文件里的每个账号键都补进来，结果历史残留的
    # 策略（账号已删、键还在）会被当成真账号渲染成一张卡片 ——
    # 卡片标题只能回退成 UUID，与控制条、模型区都对不上，
    # 看上去就像「多出来几个没有名字、没有任何功能的卡片」。
    # 策略文件不会自己清理旧键，所以这层过滤必须放在渲染侧。
    try:
        pool_ids = {
            str(acc.get("id") or acc.get("uid") or "")
            for acc in _load_accounts_for_models()
        }
    except Exception as e:
        print(f"[account-models] 读账号池做策略键过滤失败: {e}")
        pool_ids = set()

    for aid, models in disabled_by_account.items():
        if aid not in agg and models and str(aid) in pool_ids:
            _touch(str(aid), None)

    all_used = set()
    for aid, entry in agg.items():
        used = sorted(entry["used"])
        all_used.update(used)
        # 名称兜底：新账号往往没有昵称，官方流水里 accountName 也是空的。
        # 留空会让前端拿卡片标题去反查时对不上，进而丢掉 accountId ——
        # 而所有需要 accountId 的交互（点击禁用 / 全部恢复）都靠它，
        # 结果就是「新加的账号卡片上没有任何功能性按钮」。
        # 宁可回退成账号 ID（至少唯一、能对上接口），也不留空。
        label = entry.get("name") or str(aid)
        result["accounts"][aid] = {
            "id": aid,
            "name": label,
            # 供前端建立多重索引：昵称可能重名或为空，accountId 永远唯一。
            "aliases": [x for x in (entry.get("name"), str(aid)) if x],
            # 可用清单与网关保持一致，新账号也不会是空的
            "models": catalog or sorted(entry["used"]),
            "used": used,
            "usage": entry.get("usage") or {},
            "gatewayCalls": entry.get("gatewayCalls", 0),
            # 该账号被禁用的模型（模型级禁用），供卡片渲染灰化删除线状态
            "disabled": disabled_by_account.get(str(aid)) or [],
            # 每个禁用项的来源："manual"（人手禁的）/ "auto"（巡检禁的）
            "disabledSources": disabled_sources_by_account.get(str(aid)) or {},
        }
    # 账号池里存在、但两条流水源都没覆盖到的账号（例如刚添加、未曾调用），
    # 同样要出现在结果里 —— 否则它们的卡片拿不到 id，功能按钮全数消失。
    try:
        for acc in _load_accounts_for_models():
            aid = str(acc.get("id") or acc.get("uid") or "")
            if not aid or aid in result["accounts"]:
                continue
            label = acc.get("nickname") or acc.get("email") or aid
            result["accounts"][aid] = {
                "id": aid,
                "name": label,
                "aliases": [x for x in (acc.get("nickname"), acc.get("email"), aid) if x],
                "models": catalog,
                "used": [],
                "usage": {},
                "gatewayCalls": 0,
                "disabled": disabled_by_account.get(aid) or [],
                "disabledSources": disabled_sources_by_account.get(aid) or {},
            }
    except Exception as e:
        print(f"[account-models] 补全账号池条目失败: {e}")
    result["discovered"] = sorted(all_used)
    result["disabledTotal"] = sum(len(v) for v in disabled_by_account.values())
    return result

# ---------------------------------------------------------------------------
# WorkBuddy 每日成长任务服务转发（由容器内网 loopback 18093 提供，不对外暴露端口）。
# 由 wb_daily.py 托管 vendor 版签到脚本；账号池 cn 账号只读共用凭据，无烧号风险。
# ---------------------------------------------------------------------------
WB_DAILY_INTERNAL = os.getenv("WB_DAILY_INTERNAL", "http://127.0.0.1:18093")


def _wb_daily_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, trust_env=False)


@app.get("/api/wb-daily/config")
async def wb_daily_config_get(user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/config")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/wb-daily/config")
async def wb_daily_config_post(request: Request, user: "str" = Depends(require_user)):
    body = await request.body()
    async with _wb_daily_client() as client:
        r = await client.post(f"{WB_DAILY_INTERNAL}/api/wb-daily/config", content=body, headers={"Content-Type": "application/json"})
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/accounts")
async def wb_daily_accounts(user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/accounts")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/catalog")
async def wb_daily_catalog(user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/catalog")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/runs")
async def wb_daily_runs(user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/runs")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/run-accounts/{job_id}")
async def wb_daily_run_accounts(job_id: str, user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/run-accounts/{job_id}")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/tasks")
async def wb_daily_tasks(limit: int = 200, account: Optional[str] = None, user: "str" = Depends(require_user)):
    """任务台账：任务维度的记录（面板「任务记录」卡片）。"""
    async with _wb_daily_client() as client:
        params = {"limit": limit}
        if account:
            params["account"] = account
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/tasks", params=params)
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


def _merge_checkin_credits(payload: Any) -> Dict[str, Any]:
    """把「签到积分」并入 /api/wb-daily/credit-summary 的结果。

    背景（用户 2026-09-27 反馈「今日签到肯定有积分，但你都算成任务积分了」）：
    聚合原本只从 wb_daily_tasks 的 detail 文本解析「+N积分」，
    而 checkin 行的 detail 恒为「✅签到: 今天已签到，请明天再来」，
    **不含任何数字**（实测 18 条全如此），所以签到奖励一直没被统计，
    所有积分都被算进了「任务积分」。

    签到奖励的真实来源是每个账号签到状态里的
      raw.daily_credit   —— 每日签到奖励额
      raw.checkin_dates  —— 历史签到日期列表
    由此可同时补出「今日签到积分」与各历史日期的签到积分。
    """
    data = payload if isinstance(payload, dict) else {}
    cached = _CHECKIN_STATUS_CACHE.get("payload")
    accounts = (cached or {}).get("accounts") if isinstance(cached, dict) else None
    if not accounts:
        # 签到状态尚未缓存（首次访问），保持任务侧结果，等缓存预热后自动补全
        return data

    today = time.strftime("%Y-%m-%d", time.localtime())
    add_today = 0.0
    checkin_by_day: Dict[str, float] = {}

    for a in accounts or []:
        raw = (a or {}).get("raw") or {}
        try:
            credit = float(raw.get("daily_credit") or 0)
        except (TypeError, ValueError):
            continue
        if credit <= 0:
            continue
        for d in (raw.get("checkin_dates") or []):
            key = str(d)
            if key == today:
                add_today += credit
            checkin_by_day[key] = checkin_by_day.get(key, 0.0) + credit

    # 合并到按天明细（原先 days 只有 task 侧数据）
    days_map = {d.get("date"): dict(d) for d in (data.get("days") or []) if isinstance(d, dict)}
    for key, val in checkin_by_day.items():
        bucket = days_map.setdefault(key, {"date": key, "checkin": 0.0, "task": 0.0, "total": 0.0})
        bucket["checkin"] = round(float(bucket.get("checkin") or 0) + val, 2)
    days = []
    for key in sorted(days_map, reverse=True):
        b = days_map[key]
        ci = round(float(b.get("checkin") or 0), 2)
        tk = round(float(b.get("task") or 0), 2)
        days.append({"date": key, "checkin": ci, "task": tk, "total": round(ci + tk, 2)})

    data["todayCheckinCredits"] = round(float(data.get("todayCheckinCredits") or 0) + add_today, 2)
    data["todayCredits"] = round(float(data.get("todayCredits") or 0) + add_today, 2)
    data["totalCredits"] = round(
        float(data.get("totalCredits") or 0) + sum(checkin_by_day.values()), 2)
    data["days"] = days
    return data


@app.get("/api/wb-daily/credit-summary")
async def wb_daily_credit_summary(user: "str" = Depends(require_user)):
    """每日任务积分聚合：今日 / 签到 / 任务 / 累计（转发到 wb-daily 服务）。"""
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/credit-summary")
        try:
            payload = json.loads(r.content or "{}")
        except Exception:
            payload = {}
        try:
            payload = _merge_checkin_credits(payload)
        except Exception:
            pass
        return Response(content=json.dumps(payload, ensure_ascii=False),
                        status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/task-summary")
async def wb_daily_task_summary(user: "str" = Depends(require_user)):
    """任务台账聚合：每个任务项最近一次结果 + 成功次数。"""
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/task-summary")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/wb-daily/run-task/{task_key}")
async def wb_daily_run_task(task_key: str, user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.post(f"{WB_DAILY_INTERNAL}/api/wb-daily/run-task/{task_key}")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/wb-daily/run")
async def wb_daily_run(user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.post(f"{WB_DAILY_INTERNAL}/api/wb-daily/run")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/jobs")
async def wb_daily_jobs(user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/jobs")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/status/{job_id}")
async def wb_daily_status(job_id: str, user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/status/{job_id}")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.delete("/api/wb-daily/abort/{job_id}")
async def wb_daily_abort(job_id: str, user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.delete(f"{WB_DAILY_INTERNAL}/api/wb-daily/abort/{job_id}")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/health")
async def wb_daily_health(user: "str" = Depends(require_user)):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/health")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy_all(request: Request, path: str):
    # 签到状态：stale-while-revalidate。
    # 上游实测 4.9~8.9s（官方后端逐个账号实时查），前端 Promise.all 等它会把
    # 整页拖死 → fetch 中断 → 抛「无法连接 workbuddy-switch 服务」
    # （2026-10-15 用户反馈「统计加载失败/卡死/重启报错」）。
    # 因此用户请求**永远不等上游**：有数据（哪怕陈旧）立即返回并触发后台刷新；
    # 只有从未拿到过数据（首启）才同步等一次。
    if request.method == "GET" and path == "api/checkin/status":
        payload, age = _checkin_status_serve()
        if payload is not None:
            hdr = {"x-autobuddy-cache": "stale" if age > _CHECKIN_FRESH_SEC else "hit",
                   "x-autobuddy-cache-age": str(int(age))}
            return Response(content=json.dumps(payload, ensure_ascii=False),
                            media_type="application/json",
                            headers=hdr)
        # 无数据：同步取一次（首启 unavoidable），失败返回空结构而非 5xx
        try:
            async with _internal_client(timeout=90.0) as client:
                r = await client.get(f"{BACKEND_URL}/api/checkin/status")
            if r.status_code == 200:
                _checkin_status_store(r.json())
                return Response(content=r.content, status_code=200,
                                media_type="application/json",
                                headers={"x-autobuddy-cache": "miss"})
            return Response(content=json.dumps({"accounts": []}),
                            status_code=200, media_type="application/json",
                            headers={"x-autobuddy-cache": "error"})
        except Exception as e:
            logging.warning("[checkin] 同步获取失败: %s", str(e)[:120])
            return Response(content=json.dumps({"accounts": []}),
                            status_code=200, media_type="application/json",
                            headers={"x-autobuddy-cache": "error"})

    url = f"{BACKEND_URL}/{path}"
    query = str(request.query_params)
    if query:
        url = f"{url}?{query}"
    
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    
    async with _internal_client(timeout=60.0) as client:
        r = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body
        )
        content = r.content
        media_type = r.headers.get("content-type", "")
        
        is_html = "text/html" in media_type or content.lstrip().startswith(b"<!doctype html") or content.lstrip().startswith(b"<html")
        
        res_headers = {k: v for k, v in r.headers.items() if k.lower() not in ["content-encoding", "content-length", "transfer-encoding"]}
        
        if is_html:
            res_headers["content-type"] = "text/html; charset=utf-8"
            content = clean_mac_content(content)
            # 移动端全屏适配（2026-10-16，参考 www.baidu.com 移动端做法）：
            #   官方 viewport 只有 width=device-width, initial-scale=1.0 ——
            #   允许用户双指缩放，且没有 viewport-fit=cover，iPhone 刘海/圆角
            #   区域会被当成安全区之外，顶栏贴不到屏幕最上沿。
            # 百度移动端用的是：
            #   width=device-width,minimum-scale=1.0,maximum-scale=1.0,user-scalable=no
            # 这里再加 viewport-fit=cover（让页面延伸到刘海区），配合 CSS 里的
            # env(safe-area-inset-*) 把内容从刘海/Home 条下面让开。
            # 用响应改写而不是 JS 注入：viewport 必须**首屏渲染前**就位，
            # 事后用 JS 改会触发一次可见的重排/缩放跳动。
            content = _wb_fix_viewport(content)
            if b"</body>" in content:
                content = content.replace(b"</body>", f"{COLLAPSE_SCRIPT}</body>".encode("utf-8"))
            # 注入脚本与文案替换都发生在响应阶段，而 assets 文件名带 hash 不会变。
            # 不禁止缓存的话，浏览器会一直用旧副本，表现为「改了但没生效」。
            res_headers["cache-control"] = "no-store, must-revalidate"
        elif "javascript" in media_type:
            # is_js=True：TEXT_REPLACEMENTS 是前端 UI 文案，只存在于 JS 资源里。
            content = clean_mac_content(content, is_js=True)
            res_headers["cache-control"] = "no-store, must-revalidate"

        # 签到状态取到真值后写入缓存（只缓存成功响应，失败不缓存，
        # 否则一次上游抖动会被缓存放大成整分钟的「查不到签到状态」）。
        if request.method == "GET" and path == "api/checkin/status" and r.status_code == 200:
            try:
                _checkin_status_store(json.loads(content))
            except Exception as e:
                logging.warning("[checkin] 缓存写入失败: %s", e)

        return Response(
            content=content,
            status_code=r.status_code,
            headers=res_headers
        )

class _AccessNoiseFilter(logging.Filter):
    """静音高频轮询与探活的 access log（与 gateway/main.py 中同名实现保持一致）。

    见 main.py 里的详细说明：WebUI 定时轮询与健康探活会在 access log 里刷屏，
    真实请求反而被淹没。这里按「路径 + 2xx」过滤，4xx/5xx 一律保留。
    两个进程各自过滤自己监听端口上的请求：WebUI 面板（本文件，:18090）
    与 API 网关（main.py，:18091）。
    """

    NOISY_PATHS = (
        "/health",
        "/api/status",
        "/api/gateway-info",
        "/gateway/info",
        "/api/model-health",
        "/model-health/config",
        "/api-keys/status",
        "/api/api-keys",
        "/api/checkin/logs",
        "/api/rotate/logs",
        "/api/rotate/status",
        "/api/rate-limits",
        "/assets/",
        "/icon.png",
        "/favicon.ico",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if " 2" not in msg or " OK" not in msg:
            return True
        return not any(p in msg for p in self.NOISY_PATHS)


# ---------------------------------------------------------------------------
# access log 相邻去重（与 gateway/main.py 中同名实现保持一致）
#
# 背景（用户 2026-09-27）：相同内容重复刷屏没有意义，出现一条即可 ——
# 即便是 API 调用日志。实测 2322 行日志归一化后只有 106 种不同内容，
# /api/switch/progress 一条就占 1533 次，access log 占全部日志的 98%。
#
# NOISY_PATHS 是黑名单，只能盖住**已知**接口（新增轮询就得手工补）；
# 这里做**通用相邻去重**兜底：不管什么接口，与上一条完全相同就只累计。
# access log 多路交错（A、B 交替轮询），所以按规范化内容各自独立记录。
# 规范化 = 抹掉客户端端口号，否则同一接口两条日志永远判不相等。
# ---------------------------------------------------------------------------
_ACCESS_DEDUP_LOCK = threading.Lock()
_ACCESS_DEDUP_LAST: Dict[str, str] = {}
_ACCESS_DEDUP_COUNT: Dict[str, int] = {}

# 被吞掉的重复次数不能只在「内容变化时」才汇报 —— 见 _flush_access_dedup 的说明。
# 改成周期汇报：默认 60s 一次，可用环境变量调（测试时可设成几秒）。
_ACCESS_DEDUP_FLUSH_SEC = float(os.getenv("ACCESS_DEDUP_FLUSH_SEC", "60") or 60)
# 键的数量上限。access log 的内容种类理论上无界（URL 里带动态 id 时尤甚），
# 不封顶就是一条缓慢的内存泄漏。
_ACCESS_DEDUP_MAX_KEYS = 500
_ACCESS_DEDUP_NEXT_FLUSH = 0.0


def _flush_access_dedup(now: float = 0.0) -> None:
    """汇报并清空各条内容累积的重复次数（与 gateway/main.py 同名实现一致）。

    为什么必须周期汇报，而不是「内容变化时汇报」：

    为了抗交错轮询（A、B 交替），去重是**按内容各自独立**记录的 ——
    c3 累积的次数记在 c3 名下。等 d4 出现时去查的是 d4 的计数（= 0），
    于是「上一条重复 N 次」这行**永远不会触发**（实测已确认）。

    所以改为按时间周期把各条内容的待报次数统一打出来并清零。
    """
    global _ACCESS_DEDUP_NEXT_FLUSH
    if not now:
        now = time.time()
    with _ACCESS_DEDUP_LOCK:
        pending = [(k, c) for k, c in _ACCESS_DEDUP_COUNT.items() if c > 0]
        for k in list(_ACCESS_DEDUP_COUNT.keys()):
            _ACCESS_DEDUP_COUNT[k] = 0
        over = len(_ACCESS_DEDUP_LAST) - _ACCESS_DEDUP_MAX_KEYS
        if over > 0:
            for k in list(_ACCESS_DEDUP_LAST.keys())[:over]:
                _ACCESS_DEDUP_LAST.pop(k, None)
                _ACCESS_DEDUP_COUNT.pop(k, None)
        _ACCESS_DEDUP_NEXT_FLUSH = now + _ACCESS_DEDUP_FLUSH_SEC
    for k, c in pending:
        # 同一行末尾追加 ×N（与 gateway/main.py 的实现保持一致）。
        print(f"INFO:     {k} ×{c}", flush=True)


def _normalize_access_msg(msg: str) -> str:
    """规范化 access log：抹掉每次连接都不同的客户端端口号。"""
    return re.sub(r"(\d+\.\d+\.\d+\.\d+):\d+", r"\1", msg)


class _AccessDedupFilter(logging.Filter):
    """access log 通用相邻去重：完全相同的日志只出现一条。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        key = _normalize_access_msg(msg)
        now = time.time()
        if now >= _ACCESS_DEDUP_NEXT_FLUSH:
            _flush_access_dedup(now)
        with _ACCESS_DEDUP_LOCK:
            if _ACCESS_DEDUP_LAST.get(key) == key:
                _ACCESS_DEDUP_COUNT[key] = _ACCESS_DEDUP_COUNT.get(key, 0) + 1
                return False
            _ACCESS_DEDUP_LAST[key] = key
            _ACCESS_DEDUP_COUNT.setdefault(key, 0)
        return True


def _install_access_filter() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessNoiseFilter) for f in lg.filters):
        lg.addFilter(_AccessNoiseFilter())
    if not any(isinstance(f, _AccessDedupFilter) for f in lg.filters):
        lg.addFilter(_AccessDedupFilter())


if __name__ == "__main__":
    port = int(os.getenv("PORT", 18090))
    _install_access_filter()
    # Telegram 交互：容器启动即恢复长轮询（配置就绪才真跑）。
    # 走 getUpdates 拉取，不需要公网入口。
    try:
        _notify.start_telegram_polling()
    except Exception:
        pass
    # 企业微信智能机器人：WebSocket 长连接（出站，不需要可信 IP）。
    # 配置了 bot_id/bot_secret 才真跑；未配则静默跳过。
    try:
        _notify.start_wecom_bot()
    except Exception:
        pass
    uvicorn.run(app, host="0.0.0.0", port=port)
