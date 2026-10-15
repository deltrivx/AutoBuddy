"""任务积分规则表（2026-10-15 建立）。

## 为什么需要它

用户问「当前任务执行进度到哪了，**执行完后预计多少积分收益**？」时，
AutoBuddy 原先答不出来 —— 数据源里 `total_credits` 字段恒为 null，
没有任何「每个任务值多少积分」的规则可依。

本模块把**实测得出**的积分规则固化下来，供助手估算收益。

## 数据来源（全部实测，绝不编造）

### 1. 签到积分 —— 22 账号 × 5 个样本点，规律完全吻合

```
连签 4 天  → 累计  400
连签 8 天  → 累计  800
连签10 天  → 累计 1000
连签11 天  → 累计 1100
连签16 天  → 累计 1600
```

规律：**签到积分 = 100 × 连签天数**（纯累加，非递增）。
来源：`workbuddy_daily.py --no-desktop` 全流程实测日志
（2026-10-15 19:57~20:35，22 个账号）。

### 2. 领奖积分 —— 43 次实测

```
Expert_lighthouse  →  +100 积分 +5 能量
RichMeow_Chat      →  +100 积分 +5 能量
```

### 3. 服务端下发（不固定，只能读实际返回值）

上游脚本里这三处的积分值由服务端决定，无法预先算死：

| 来源 | 字段 | 说明 |
| :--- | :--- | :--- |
| 旅行礼物 | `reward_credit` | `/v2/activity/growth/buddy/travel/claim` |
| 连签兑换 | `credit_granted` | 分档（入门/进阶），服务端返回 |
| 新手礼包/补偿 | `credit` | 活动性，不定期 |

## 估算口径

对「预计收益」类问题，本模块提供：

- **确定部分**：签到（100×天数）、已知领奖项（如上表）—— 可精确算
- **浮动部分**：服务端下发项 —— 用**历史实测均值**给量级，并明确标注是估算

⚠️ 回答时必须区分「精确」与「估算」，不得把估算值说成确定值。
"""

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 规则常量（数值全部来自实测，见模块 docstring）
# ---------------------------------------------------------------------------

#: 签到：每天固定积分。实测 22 账号 × 5 样本点，全部符合「100 × 连签天数」。
CHECKIN_CREDITS_PER_DAY = 100

#: 签到同时获得的能量（实测：签到时能量与积分同源，通常 0~8 波动，
#: 非固定值，故不参与积分估算）。
CHECKIN_ENERGY_TYPICAL = 0

#: 已确认的领奖项（任务 code → 积分/能量）。
#: 依据：2026-10-15 全流程实测，43 次领奖记录。
KNOWN_CLAIM_REWARDS: Dict[str, Dict[str, int]] = {
    "Expert_lighthouse": {"credit": 100, "energy": 5},
    "RichMeow_Chat": {"credit": 100, "energy": 5},
}

#: 每日成长任务的典型积分区间（来自 credit-summary 的历史「任务」列）。
#: 这些是**统计口径**，不是单任务规则 —— 用于给「今天大概能赚多少」的量级感。
#: 依据：近 21 天台账（2026-10-15 读取）。
TASK_DAILY_TYPICAL_MIN = 600
TASK_DAILY_TYPICAL_MAX = 7100


def checkin_credits(streak_days: int) -> int:
    """按连签天数算签到积分（精确值）。

    >>> checkin_credits(16)
    1600
    >>> checkin_credits(4)
    400
    """
    try:
        n = int(streak_days)
    except (TypeError, ValueError):
        return 0
    return max(0, n) * CHECKIN_CREDITS_PER_DAY


def known_claim_credits(codes: Optional[List[str]] = None) -> int:
    """已知领奖项的积分合计（精确值）。

    ``codes`` 为空时返回全部已知项之和（"如果这些都跑，能拿多少"）。
    """
    items = KNOWN_CLAIM_REWARDS
    if codes:
        items = {c: v for c, v in KNOWN_CLAIM_REWARDS.items() if c in codes}
    return sum(int(v.get("credit") or 0) for v in items.values())


def describe_rules() -> str:
    """把规则表渲染成可读文本（供助手直接引用）。"""
    lines = [
        "任务积分规则（实测）：",
        "",
        "【签到】精确可算",
        "  · %d 积分/天 × 连签天数" % CHECKIN_CREDITS_PER_DAY,
        "  · 例：连签 16 天 = %d 积分" % checkin_credits(16),
        "",
        "【领奖】已知项",
    ]
    for code, v in KNOWN_CLAIM_REWARDS.items():
        lines.append("  · %s：+%d 积分 +%d 能量" % (code, v["credit"], v["energy"]))
    lines += [
        "",
        "【服务端下发】不可预算，只能读实际返回值",
        "  · 旅行礼物 / 连签兑换 / 新手礼包补偿",
        "",
        "【历史量级】近 21 天「任务」列实际区间",
        "  · %s ~ %s 积分/天（取决于当天跑了多少任务）"
        % (f"{TASK_DAILY_TYPICAL_MIN:,}", f"{TASK_DAILY_TYPICAL_MAX:,}"),
    ]
    return "\n".join(lines)


def estimate_pending(
    streak_days: Optional[int] = None,
    remaining_tasks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """估算「未完成部分能拿多少积分」。

    返回结构明确区分**精确**与**估算**，避免助手把估算说成确定值。

    :param streak_days: 当前连签天数（用于签到部分；已签到则不必传）
    :param remaining_tasks: 未完成任务名列表（有已知规则的可精确算）
    """
    exact = 0
    exact_items: List[str] = []

    if streak_days:
        c = checkin_credits(streak_days)
        exact += c
        exact_items.append("签到（连签 %d 天）：+%d" % (streak_days, c))

    if remaining_tasks:
        for t in remaining_tasks:
            # 任务名可能是中文名，也可能是 code；两种都试
            for code, v in KNOWN_CLAIM_REWARDS.items():
                if code == t or code.lower() in str(t).lower():
                    exact += v["credit"]
                    exact_items.append("%s：+%d" % (code, v["credit"]))
                    break

    return {
        "exact": exact,
        "exactItems": exact_items,
        "estimateRange": [TASK_DAILY_TYPICAL_MIN, TASK_DAILY_TYPICAL_MAX],
        # 明确标注：range 是历史统计，不是承诺
        "estimateNote": "浮动部分取决于服务端下发的奖励，按近 21 天实测区间估算",
    }
