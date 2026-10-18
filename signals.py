# signals.py
"""
信号层：纯计算，输入 DataFrame(date/open/high/low/close/volume)，输出信号。

重要：所有函数返回的信号都对齐到"当天收盘后可知"的口径，
      不使用任何未来数据。底背离尤其容易写出未来函数，见 bullish_divergence 注释。
"""
import numpy as np
import pandas as pd


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    """返回 (DIF, DEA, MACD柱)。MACD柱 = (DIF-DEA)*2，和通达信/同花顺口径一致。"""
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, (dif - dea) * 2.0


# =============================================================================
# 信号 1：MACD 金叉
# =============================================================================
# ⚠️ 重要：hist = (DIF-DEA)*2，所以 hist>0 ⟺ DIF>DEA。
#    "柱由绿转红"和"DIF上穿DEA"是**同一件事**，不是两个可以互相印证的信号。
#    把它们当两个信号来加分，会导致每一只命中都是"双重信号"，火焰标记完全失效。
#    所以这里只保留金叉，按 DIF 的位置分成两类。
# =============================================================================
def golden_cross(df: pd.DataFrame) -> pd.Series:
    """DIF 上穿 DEA。"""
    dif, dea, _ = macd(df["close"])
    return ((dif > dea) & (dif.shift(1) <= dea.shift(1))).fillna(False)


def is_low_cross(df: pd.DataFrame) -> pd.Series:
    """金叉发生在零轴下方 —— 通常被视为超跌反弹/抄底信号。
    零轴上方的金叉多为趋势中继，含义完全不同，不该混在一起看。"""
    dif, _, _ = macd(df["close"])
    return golden_cross(df) & (dif < 0)


# =============================================================================
# 信号 2：MACD 底背离
# =============================================================================
def _swing_lows(low: np.ndarray, order: int) -> list[int]:
    """局部低点：左右各 order 根 K 线内的最低点。"""
    idx = []
    for i in range(order, len(low) - order):
        window = low[i - order: i + order + 1]
        if low[i] == window.min():
            idx.append(i)
    return idx


def bullish_divergence(
    df: pd.DataFrame,
    order: int = 5,
    max_gap: int = 60,
    min_gap: int = 8,
) -> pd.Series:
    """
    底背离：价格创新低，但 DIF 没有创新低。

    ⚠️ 未来函数的坑：要确认第 i 根是局部低点，必须等到第 i+order 根收盘。
       所以信号的可用日期是 i2 + order，不是 i2。下面严格按这个口径打标记。
       如果你把信号标在 i2 上，回测收益会好看得离谱，但实盘一根都抓不到。

    order   : 摆动低点的确认窗口（越大越少但越可靠）
    min_gap : 两个低点至少隔多少根，太近的属于同一个坑
    max_gap : 两个低点最多隔多少根，太远就没有可比性
    """
    n = len(df)
    out = pd.Series(False, index=df.index)
    if n < 40:
        return out

    low = df["low"].values if "low" in df else df["close"].values
    dif, _, _ = macd(df["close"])
    dif_v = dif.values

    lows = _swing_lows(low, order)
    for a, b in zip(lows, lows[1:]):
        gap = b - a
        if not (min_gap <= gap <= max_gap):
            continue
        # 价格更低 & DIF 更高 → 背离
        if low[b] < low[a] and dif_v[b] > dif_v[a] and dif_v[b] < 0:
            confirm = b + order          # 信号在这天才"可知"
            if confirm < n:
                out.iloc[confirm] = True
    return out


# =============================================================================
# 信号 3：绿柱缩短 —— 一大段绿柱之后，第一天开始变短
# =============================================================================
# 金叉是滞后的：等 DIF 上穿 DEA，价格往往已经离最低点反弹了一截。
# 绿柱最长的那天，往往就是这一波下跌动能最强的时候；它明显变短，
# 是 MACD 开始拐头的信号，通常比金叉早好几天。
# 只缩短一点点（比如 1%）不算，那多半是噪音，第二天又接着变长。
#
# 「很长」不能用绝对值：黄金 ETF 的正常绿柱只有创业板的一半。
# 所以和这只票**自己过去一年**的绿柱比：本波最长绿柱 ≥ 过去一年各波
# 最长绿柱中位数的 ratio 倍，才算「很长」。只用当天收盘为止的数据。
# =============================================================================
def green_shrink(
    df: pd.DataFrame,
    ratio: float = 2.0,
    min_shrink: float = 0.20,
    lookback: int = 250,
    min_waves: int = 3,
) -> pd.DataFrame:
    """
    逐日标记，返回 DataFrame：flag 是否触发，strength 本波最长绿柱是常态的几倍，
    shrink 今天的绿柱比最长那根短了多少。

    触发条件：
      1. 零轴下方：DIF < 0，且今天仍是绿柱（hist < 0，尚未金叉）
      2. 明显缩短：从本波最长那根绿柱起，之后每天都在变短（中间没有再变长），
         且累计缩短 ≥ min_shrink（默认 20%）。只在**第一次**达到这个幅度的那天触发
      3. 很长：最长那根绿柱 ≥ 过去 lookback 天里各波最长绿柱中位数 × ratio
         （过去不足 min_waves 波绿柱时没有参照，不触发）
    """
    close = df["close"]
    dif, _, hist = macd(close)
    h = hist.values
    d = dif.values
    n = len(df)

    # 每一波绿柱的编号；cummin 得到「截至当天本波最长的绿柱」
    wave_id = (hist >= 0).cumsum()
    wave_min = hist.where(hist < 0).groupby(wave_id).cummin().values

    # 每波绿柱结束的位置和它的最长绿柱，用来当「常态」参照
    ends, troughs = [], []
    for i in range(1, n):
        if h[i] >= 0 > h[i - 1]:
            ends.append(i)
            troughs.append(-wave_min[i - 1])
    ends, troughs = np.array(ends), np.array(troughs)

    flag = np.zeros(n, dtype=bool)
    strength = np.full(n, np.nan)
    shrink = np.full(n, np.nan)
    for i in range(2, n):
        if not (h[i] < 0 and d[i] < 0 and h[i] > h[i - 1]):
            continue
        # 往回找本波最长那根：要求从它到今天一路都在变短
        t = i - 1
        while t > 0 and h[t - 1] < 0 and h[t] > h[t - 1]:
            t -= 1
        if h[t] != wave_min[t]:
            continue                    # 中途又变长过，或最长的不在这段里
        trough = -h[t]
        sh = (h[i] - h[t]) / trough      # 今天累计缩短
        sh_prev = (h[i - 1] - h[t]) / trough
        if not (sh >= min_shrink > sh_prev):
            continue                    # 只在第一次达到幅度那天触发
        past = troughs[(ends < t) & (ends >= t - lookback)] if len(ends) else []
        if len(past) < min_waves:
            continue
        st = trough / np.median(past)
        strength[i], shrink[i] = st, sh
        flag[i] = st >= ratio

    return pd.DataFrame({"flag": flag, "strength": strength, "shrink": shrink},
                        index=df.index)


def scan_green_shrink(df: pd.DataFrame, **kw) -> dict:
    """最新一根 K 线是否是「长绿柱第一天缩短」。没触发返回 {}。"""
    if df is None or len(df) < 120:
        return {}
    r = green_shrink(df, **kw).iloc[-1]
    if not bool(r["flag"]):
        return {}
    return {
        "date": str(df["date"].iloc[-1]),
        "close": round(float(df["close"].iloc[-1]), 2),
        "strength": round(float(r["strength"]), 2),   # 最长绿柱是常态的几倍
        "shrink": round(float(r["shrink"]), 4),       # 已比最长那根短了多少
    }


# =============================================================================
# 汇总：给某个标的算出"今天有没有信号"
# =============================================================================
def scan_latest(df: pd.DataFrame) -> dict:
    """
    返回最新一根 K 线上的信号状态。

    评分（score）：只有**互相独立**的信号才叠加。
      金叉  → 1 分
      底背离 → 1 分
      两者同时出现 → 2 分，这才是真正值得标 🔥 的情况
    """
    if df is None or len(df) < 60:
        return {}

    dif, dea, hist = macd(df["close"])
    i = len(df) - 1

    cross = bool(golden_cross(df).iloc[i])
    low_cross = bool(is_low_cross(df).iloc[i])
    div = bool(bullish_divergence(df).iloc[i])

    hits, score = [], 0
    if low_cross:
        hits.append("低位金叉")
        score += 1
    elif cross:
        hits.append("零轴上金叉")     # 趋势中继，含义不同，单独标
        score += 1
    if div:
        hits.append("底背离")
        score += 1

    if not hits:
        return {}

    return {
        "date": str(df["date"].iloc[i]),
        "close": round(float(df["close"].iloc[i]), 2),
        "dif": round(float(dif.iloc[i]), 4),
        "dea": round(float(dea.iloc[i]), 4),
        "hist": round(float(hist.iloc[i]), 4),
        "signals": hits,
        "score": score,
    }