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
    <25 极度恐惧 → bullish（可能底部）
    >75 极度贪婪 → bearish（可能顶部）
    25~75 之间不报警
    """
    if value <= 10:
        return IndicatorScore("fear_greed", value, 100, "bullish_extreme",
                              f"⚡ 恐贪指数 {value:.0f}（极度恐惧！历史底部信号）")
    if value <= 25:
        score = (25 - value) / 15 * 100
        return IndicatorScore("fear_greed", value, score, "bullish",
                              f"恐贪指数 {value:.0f}（恐惧，可能接近底部）")
    if value >= 90:
        return IndicatorScore("fear_greed", value, 100, "bearish_extreme",
                              f"⚡ 恐贪指数 {value:.0f}（极度贪婪！历史顶部信号）")
    if value >= 75:
        score = (value - 75) / 15 * 100
        return IndicatorScore("fear_greed", value, score, "bearish",
                              f"恐贪指数 {value:.0f}（贪婪，警惕顶部）")
    return IndicatorScore("fear_greed", value, 0, "neutral",
                          f"恐贪指数 {value:.0f}（正常区间）")


def score_ma200_deviation(deviation_pct: float) -> IndicatorScore:
    """
    BTC 距 200 日均线偏离度
    历史观察:
      - <-25% 严重超卖（2018底/2020/2022底） → bullish
      - >+50% 严重超买（2017顶/2021顶/2024顶） → bearish
    """
    if deviation_pct <= -40:
        return IndicatorScore("ma200_deviation", deviation_pct, 100, "bullish_extreme",
                              f"⚡ BTC 距 200 日均线 {deviation_pct:+.1f}%（极度低估！）")
    if deviation_pct <= -20:
        score = (-20 - deviation_pct) / 20 * 100
        return IndicatorScore("ma200_deviation", deviation_pct, score, "bullish",
                              f"BTC 距 200 日均线 {deviation_pct:+.1f}%（低估区间）")
    if deviation_pct >= 80:
        return IndicatorScore("ma200_deviation", deviation_pct, 100, "bearish_extreme",
                              f"⚡ BTC 距 200 日均线 {deviation_pct:+.1f}%（极度高估！）")
    if deviation_pct >= 40:
        score = (deviation_pct - 40) / 40 * 100
        return IndicatorScore("ma200_deviation", deviation_pct, score, "bearish",
                              f"BTC 距 200 日均线 {deviation_pct:+.1f}%（高估区间）")
    return IndicatorScore("ma200_deviation", deviation_pct, 0, "neutral",
                          f"BTC 距 200 日均线 {deviation_pct:+.1f}%（正常）")


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


def score_oi_change(week_change_pct: float) -> IndicatorScore:
    """
    BTC 永续合约持仓量周变化
    >+15% OI 暴增 → 杠杆过热（bearish 警示）
    <-15% OI 暴跌 → 大量平仓（可能是底部洗盘 bullish）
    """
    if week_change_pct >= 0:
        score, direction = _scale_extreme(week_change_pct, -5, 5, -25, 25)
    else:
        score, direction = _scale_extreme(week_change_pct, -5, 5, -25, 25)

    desc_map = {
        "neutral": f"BTC 永续 OI 周变化 {week_change_pct:+.2f}%（正常）",
        "bullish": f"BTC 永续 OI 周变化 {week_change_pct:+.2f}%（持仓萎缩）",
        "bullish_extreme": f"⚡ BTC 永续 OI 周变化 {week_change_pct:+.2f}%（大规模平仓！可能洗盘见底）",
        "bearish": f"BTC 永续 OI 周变化 {week_change_pct:+.2f}%（杠杆增加）",
        "bearish_extreme": f"⚡ BTC 永续 OI 周变化 {week_change_pct:+.2f}%（杠杆过热！警惕回调）",
    }
    return IndicatorScore("oi_change", week_change_pct, score, direction, desc_map.get(direction, ""))


def score_long_short_ratio(ratio: float) -> IndicatorScore:
    """
    OKX 大户多空账户比
    >2.0 多头极度乐观（bearish 反向信号，可能见顶）
    <0.7 多头极度悲观（bullish 反向信号，可能见底）
    """
    score, direction = _scale_extreme(ratio, 0.9, 1.5, 0.5, 2.5)
    # 翻转：高比例=过度乐观=bearish；低比例=过度悲观=bullish
    if direction == "bullish":
        direction = "bearish"
    elif direction == "bullish_extreme":
        direction = "bearish_extreme"
    elif direction == "bearish":
        direction = "bullish"
    elif direction == "bearish_extreme":
        direction = "bullish_extreme"

    desc_map = {
        "neutral": f"大户多空比 {ratio:.2f}（均衡）",
        "bullish": f"大户多空比 {ratio:.2f}（散户偏空，反向看多）",
        "bullish_extreme": f"⚡ 大户多空比 {ratio:.2f}（极度看空，反向底部信号）",
        "bearish": f"大户多空比 {ratio:.2f}（散户偏多，警惕反转）",
        "bearish_extreme": f"⚡ 大户多空比 {ratio:.2f}（极度乐观，警惕顶部）",
    }
    return IndicatorScore("long_short_ratio", ratio, score, direction, desc_map.get(direction, ""))


def score_hl_premium(premium: float) -> IndicatorScore:
    """
    Hyperliquid 标记价 vs 预言机价 的溢价
    > +0.001 (0.1%) 期货大幅溢价 → 多头追涨过热（bearish）
    < -0.001 期货深度贴水 → 空头恐慌（bullish）
    """
    premium_pct = premium * 100
    score, direction = _scale_extreme(premium_pct, -0.05, 0.05, -0.2, 0.2)
    if direction == "bullish":
        direction = "bearish"
    elif direction == "bullish_extreme":
        direction = "bearish_extreme"
    elif direction == "bearish":
        direction = "bullish"
    elif direction == "bearish_extreme":
        direction = "bullish_extreme"

    desc_map = {
        "neutral": f"HL 期货溢价 {premium_pct:+.4f}%（正常）",
        "bullish": f"HL 期货溢价 {premium_pct:+.4f}%（贴水，反向看多）",
        "bullish_extreme": f"⚡ HL 期货溢价 {premium_pct:+.4f}%（深度贴水！可能见底）",
        "bearish": f"HL 期货溢价 {premium_pct:+.4f}%（溢价，警惕回调）",
        "bearish_extreme": f"⚡ HL 期货溢价 {premium_pct:+.4f}%（极端溢价！警惕暴跌）",
    }
    return IndicatorScore("hl_premium", premium, score, direction, desc_map.get(direction, ""))


def score_hl_funding(funding_rate: float) -> IndicatorScore:
    """
    Hyperliquid 1h 资金费率 (注意：HL 是1小时频率，OKX 是8小时)
    年化 = funding × 24 × 365
    > 0.0001 (0.01%/h = 87.6%/年) 多头亢奋
    < -0.0001 空头恐慌
    """
    annualized_pct = funding_rate * 24 * 365 * 100
    if funding_rate >= 0:
        score, direction = _scale_extreme(annualized_pct, -10, 10, -100, 100)
    else:
        score, direction = _scale_extreme(annualized_pct, -10, 10, -100, 100)
        if direction == "bearish":
            direction = "bullish"
        elif direction == "bearish_extreme":
            direction = "bullish_extreme"

    desc_map = {
        "neutral": f"HL 资金费率年化 {annualized_pct:+.1f}%（正常）",
        "bullish": f"HL 资金费率年化 {annualized_pct:+.1f}%（空头主导）",
        "bullish_extreme": f"⚡ HL 资金费率年化 {annualized_pct:+.1f}%（极端空头！）",
        "bearish": f"HL 资金费率年化 {annualized_pct:+.1f}%（多头亢奋）",
        "bearish_extreme": f"⚡ HL 资金费率年化 {annualized_pct:+.1f}%（极端多头！）",
    }
    return IndicatorScore("hl_funding", funding_rate, score, direction, desc_map.get(direction, ""))


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
