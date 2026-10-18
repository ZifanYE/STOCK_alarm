# test_shrink.py
"""
「绿柱缩短」信号的测试/观察脚本。只读 market.db，不推送、不写 picks.db。

用法：
    python test_shrink.py                    # 今天：哪些票触发了；哪些正在长绿柱里、还没拐头
    python test_shrink.py --days 60          # 最近 60 个交易日的所有触发，以及之后到今天涨了多少
    python test_shrink.py --code 600519      # 单只票：最近 30 天的绿柱明细，看它为什么触发/没触发
    python test_shrink.py --ratio 1.5        # 换个「很长」的倍数试试（默认 2.0）
    python test_shrink.py --shrink 0.3       # 换个「明显缩短」的幅度试试（默认 0.2 = 20%）

先要有 market.db（data/market.db）。本地没有的话先跑： python fetch_bs.py --full
"""
import sys
import argparse

import numpy as np

import store
import signals as sig

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOAD = 600          # 比 daily.py 的 400 多拉一点，--days 回看时前面也有一年参照


def _pct(v):
    return f"{v*100:+.1f}%" if v is not None and v == v else "—"


def _codes(conn):
    codes = store.all_codes(conn)
    if not codes:
        print("❌ market.db 是空的。先跑： python fetch_bs.py --full")
        sys.exit(1)
    return codes


def today(conn, ratio, shrink):
    """今天触发的 + 观察名单（绿柱已够长、已开始缩短，但还没缩短到位）。"""
    hits, watch = [], []
    for code, name, kind, industry in _codes(conn):
        df = store.load(conn, code, limit=LOAD)
        if df is None or len(df) < 120:
            continue
        g = sig.green_shrink(df, ratio=ratio, min_shrink=shrink)
        if g["flag"].iloc[-1]:
            hits.append((g["strength"].iloc[-1], code, name, industry,
                         df["close"].iloc[-1], g["shrink"].iloc[-1]))
            continue
        w = _watch_now(df)
        if w is not None and w[0] >= ratio and w[1] > 0:     # 还在变长的不要
            watch.append((w[0], code, name, industry, df["close"].iloc[-1], w[1]))

    newest = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    print(f"数据截至 {newest}　「很长」= 常态的 {ratio} 倍　「明显缩短」= {shrink:.0%}\n")
    print(f"🔭 今天触发 {len(hits)} 只（长绿柱明显变短）：")
    for st, code, name, ind, close, sh in sorted(hits, reverse=True):
        print(f"  {name}({code})  ¥{close:.2f}  绿柱为常态 {st:.1f} 倍  已缩短 {sh:.0%}  {ind}")
    print(f"\n👀 观察名单 {len(watch)} 只（绿柱已够长、已开始缩短，但还不到 {shrink:.0%}）：")
    for st, code, name, ind, close, sh in sorted(watch, reverse=True)[:30]:
        print(f"  {name}({code})  ¥{close:.2f}  绿柱为常态 {st:.1f} 倍  已缩短 {sh:.0%}  {ind}")
    if len(watch) > 30:
        print(f"  …另有 {len(watch)-30} 只")


def _watch_now(df):
    """
    今天仍是零轴下的绿柱，且本波最长那根到今天一路在变短时，
    返回 (最长绿柱是常态的几倍, 已缩短多少)；否则 None。
    """
    dif, _, hist = sig.macd(df["close"])
    h = hist.values
    if not (h[-1] < 0 and dif.iloc[-1] < 0):
        return None
    wave_id = (hist >= 0).cumsum()
    wave_min = hist.where(hist < 0).groupby(wave_id).cummin().values
    t = len(h) - 1
    while t > 0 and h[t - 1] < 0 and h[t] > h[t - 1]:
        t -= 1
    if h[t] != wave_min[t]:
        return None
    troughs = [-wave_min[i - 1] for i in range(max(1, t - 250), t)
               if h[i] >= 0 > h[i - 1]]
    if len(troughs) < 3:
        return None
    return -h[t] / float(np.median(troughs)), (h[-1] - h[t]) / -h[t]


def history(conn, days, ratio, shrink):
    """最近 days 个交易日的所有触发，以及触发后 5 / 10 天和到今天的涨跌。"""
    rows = []
    for code, name, kind, industry in _codes(conn):
        df = store.load(conn, code, limit=LOAD)
        if df is None or len(df) < 120:
            continue
        g = sig.green_shrink(df, ratio=ratio, min_shrink=shrink)
        gc = sig.golden_cross(df).values
        c = df["close"].values
        n = len(df)
        for i in np.where(g["flag"].values)[0]:
            if i < n - days:
                continue
            nx = [j for j in range(i + 1, n) if gc[j]]
            rows.append({
                "date": df["date"].iloc[i], "code": code, "name": name,
                "strength": g["strength"].iloc[i],
                "shrink": g["shrink"].iloc[i],
                "r5": c[i + 5] / c[i] - 1 if i + 5 < n else None,
                "r10": c[i + 10] / c[i] - 1 if i + 10 < n else None,
                "now": c[-1] / c[i] - 1,
                "lead": (nx[0] - i) if nx else None,
            })

    rows.sort(key=lambda r: (r["date"], -r["strength"]))
    print(f"最近 {days} 个交易日「绿柱缩短」触发 {len(rows)} 次（倍数 ≥ {ratio}，缩短 ≥ {shrink:.0%}）\n")
    print(f"  {'日期':<10} {'名称':<8} {'代码':<7} {'倍数':>5} {'缩短':>5} {'5日':>7} {'10日':>7} {'至今':>7}  几天后金叉")
    for r in rows:
        lead = f"{r['lead']} 天" if r["lead"] is not None else "未金叉"
        print(f"  {r['date']:<10} {r['name']:<8} {r['code']:<7} {r['strength']:>5.1f} "
              f"{r['shrink']:>5.0%} "
              f"{_pct(r['r5']):>7} {_pct(r['r10']):>7} {_pct(r['now']):>7}  {lead}")

    for key, label in (("r5", "5 日"), ("r10", "10 日")):
        v = np.array([r[key] for r in rows if r[key] is not None])
        if len(v):
            print(f"\n  {label}：样本 {len(v)}　平均 {_pct(v.mean())}　胜率 {(v > 0).mean():.0%}", end="")
    leads = [r["lead"] for r in rows if r["lead"] is not None]
    if leads:
        print(f"\n  之后出现金叉 {len(leads)}/{len(rows)}　中位提前 {int(np.median(leads))} 天")
    print()


def one(conn, code, ratio, shrink):
    """单只票最近 30 天的明细。"""
    df = store.load(conn, code, limit=LOAD)
    if df is None:
        print(f"❌ market.db 里没有 {code}")
        return
    dif, dea, hist = sig.macd(df["close"])
    g = sig.green_shrink(df, ratio=ratio, min_shrink=shrink)
    wave_id = (hist >= 0).cumsum()
    wave_min = hist.where(hist < 0).groupby(wave_id).cummin()

    print(f"{code}　最近 30 个交易日（★ = 触发；倍数/缩短只在触发当天显示）\n")
    print(f"  {'日期':<10} {'收盘':>8} {'DIF':>8} {'DEA':>8} {'MACD柱':>8} {'本波最长':>8} {'倍数':>5}")
    for i in range(max(0, len(df) - 30), len(df)):
        st = g["strength"].iloc[i]
        wm = wave_min.iloc[i]
        print(f"  {df['date'].iloc[i]:<10} {df['close'].iloc[i]:>8.2f} {dif.iloc[i]:>8.3f} "
              f"{dea.iloc[i]:>8.3f} {hist.iloc[i]:>8.3f} "
              f"{(f'{wm:.3f}' if wm == wm else '—'):>8} "
              f"{(f'{st:.1f}' if st == st else ''):>5} {'★' if g['flag'].iloc[i] else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="回看最近 N 个交易日的触发")
    ap.add_argument("--code", type=str, default="", help="只看一只票的明细")
    ap.add_argument("--ratio", type=float, default=2.0, help="绿柱「很长」的倍数，默认 2.0")
    ap.add_argument("--shrink", type=float, default=0.2, help="「明显缩短」的幅度，默认 0.2")
    args = ap.parse_args()

    conn = store.connect()
    if args.code:
        one(conn, args.code, args.ratio, args.shrink)
    elif args.days:
        history(conn, args.days, args.ratio, args.shrink)
    else:
        today(conn, args.ratio, args.shrink)


if __name__ == "__main__":
    main()
