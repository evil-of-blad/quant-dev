"""
指标计算与评分
- 每个指标独立返回 0~100 的"极端度分数"
- 0 = 完全正常
- 100 = 极度异常（强烈信号）
"""
from dataclasses import dataclass


@dataclass
class IndicatorScore:
    name: str
    value: float                    # 原始值
    score: float                    # 0~100 极端度
    direction: str                  # "bullish"/"bearish"/"neutral"
    description: str                # 人类可读描述


def _scale_extreme(value: float, normal_low: float, normal_high: float,
                   extreme_low: float, extreme_high: float) -> tuple[float, str]:
    """
    把一个值映射到 0~100 极端度分数
    - 在 [normal_low, normal_high] 之间 → 0
    - 在 extreme_low 之外 → bearish 100
    - 在 extreme_high 之外 → bullish/bearish 100（取决于方向）
    """
    if normal_low <= value <= normal_high:
        return 0.0, "neutral"

    if value < normal_low:
        # 偏低
        if value <= extreme_low:
            return 100.0, "bearish_extreme"
        ratio = (normal_low - value) / (normal_low - extreme_low)
        return ratio * 100, "bearish"

    # 偏高
    if value >= extreme_high:
        return 100.0, "bullish_extreme"
    ratio = (value - normal_high) / (extreme_high - normal_high)
    return ratio * 100, "bullish"


# ----------------------------------------------------------------------
# 各指标评分
# ----------------------------------------------------------------------

def score_fear_greed(value: float) -> IndicatorScore:
    """
    恐贪指数
    <20 极度恐惧 → 可能底部 (bullish 信号)
    >80 极度贪婪 → 可能顶部 (bearish 信号)
    """
    score, direction = _scale_extreme(value, 30, 70, 10, 90)
    # 翻转方向：低值=底部=bullish
    if direction == "bearish":
        direction = "bullish"
    elif direction == "bearish_extreme":
        direction = "bullish_extreme"
    elif direction == "bullish":
        direction = "bearish"
    elif direction == "bullish_extreme":
        direction = "bearish_extreme"

    desc_map = {
        "neutral": f"恐贪指数 {value:.0f}（正常）",
        "bullish": f"恐贪指数 {value:.0f}（恐惧，可能接近底部）",
        "bullish_extreme": f"⚡ 恐贪指数 {value:.0f}（极度恐惧！历史底部信号）",
        "bearish": f"恐贪指数 {value:.0f}（贪婪，警惕顶部）",
        "bearish_extreme": f"⚡ 恐贪指数 {value:.0f}（极度贪婪！历史顶部信号）",
    }
    return IndicatorScore("fear_greed", value, score, direction, desc_map[direction])


def score_ma200_deviation(deviation_pct: float) -> IndicatorScore:
    """
    BTC 距 200 周均线偏离度
    <-15% 严重低估 → bullish
    >+150% 严重高估 → bearish
    """
    if deviation_pct >= 0:
        # 偏离上方
        score, direction = _scale_extreme(deviation_pct, 0, 100, -200, 200)
    else:
        # 偏离下方
        score, direction = _scale_extreme(deviation_pct, -10, 0, -50, 100)
        # 翻转方向：偏离下方=bullish
        if direction == "bearish":
            direction = "bullish"
        elif direction == "bearish_extreme":
            direction = "bullish_extreme"

    desc_map = {
        "neutral": f"BTC 距 200 周均线 {deviation_pct:+.1f}%（正常）",
        "bullish": f"BTC 距 200 周均线 {deviation_pct:+.1f}%（低估区间）",
        "bullish_extreme": f"⚡ BTC 距 200 周均线 {deviation_pct:+.1f}%（极度低估！）",
        "bearish": f"BTC 距 200 周均线 {deviation_pct:+.1f}%（高估区间）",
        "bearish_extreme": f"⚡ BTC 距 200 周均线 {deviation_pct:+.1f}%（极度高估！）",
    }
    return IndicatorScore("ma200_deviation", deviation_pct, score, direction, desc_map.get(direction, ""))


def score_funding_rate(rate: float) -> IndicatorScore:
    """
    BTC 永续资金费率
    < -0.05% 空头主导 → bullish
    > +0.10% 多头主导 → bearish
    """
    rate_pct = rate * 100
    if rate_pct >= 0:
        score, direction = _scale_extreme(rate_pct, 0, 0.01, -0.1, 0.1)
    else:
        score, direction = _scale_extreme(rate_pct, -0.01, 0, -0.1, 0.1)
        if direction == "bearish":
            direction = "bullish"
        elif direction == "bearish_extreme":
            direction = "bullish_extreme"

    desc_map = {
        "neutral": f"BTC 资金费率 {rate_pct:+.4f}%（正常）",
        "bullish": f"BTC 资金费率 {rate_pct:+.4f}%（空头压力大）",
        "bullish_extreme": f"⚡ BTC 资金费率 {rate_pct:+.4f}%（极端空头！）",
        "bearish": f"BTC 资金费率 {rate_pct:+.4f}%（多头亢奋）",
        "bearish_extreme": f"⚡ BTC 资金费率 {rate_pct:+.4f}%（极端多头亢奋！）",
    }
    return IndicatorScore("funding_rate", rate, score, direction, desc_map.get(direction, ""))


def score_btc_dominance(dominance: float) -> IndicatorScore:
    """
    BTC 主导率
    >60% BTC 季 → 中性偏多
    <40% 山寨季 → 中性偏空
    """
    score, direction = _scale_extreme(dominance, 45, 55, 35, 65)

    desc_map = {
        "neutral": f"BTC 主导率 {dominance:.1f}%（均衡）",
        "bullish": f"BTC 主导率 {dominance:.1f}%（BTC 强势期）",
        "bullish_extreme": f"BTC 主导率 {dominance:.1f}%（BTC 极端强势）",
        "bearish": f"BTC 主导率 {dominance:.1f}%（山寨币强势期）",
        "bearish_extreme": f"BTC 主导率 {dominance:.1f}%（山寨季顶峰）",
    }
    return IndicatorScore("btc_dominance", dominance, score, direction, desc_map.get(direction, ""))


def score_stablecoin_change(week_change_pct: float) -> IndicatorScore:
    """
    稳定币市值周变化
    >+3% 资金大量流入 → bullish（要进场）
    <-3% 资金流出 → bearish（要逃跑）
    """
    score, direction = _scale_extreme(week_change_pct, -1, 1, -5, 5)

    desc_map = {
        "neutral": f"稳定币市值周变化 {week_change_pct:+.2f}%（正常）",
        "bullish": f"稳定币市值周变化 {week_change_pct:+.2f}%（资金流入）",
        "bullish_extreme": f"⚡ 稳定币市值周变化 {week_change_pct:+.2f}%（大量资金流入！）",
        "bearish": f"稳定币市值周变化 {week_change_pct:+.2f}%（资金流出）",
        "bearish_extreme": f"⚡ 稳定币市值周变化 {week_change_pct:+.2f}%（大量资金流出！）",
    }
    return IndicatorScore("stablecoin_change", week_change_pct, score, direction, desc_map.get(direction, ""))
