# monthly.py
"""
月度战记：统计「上个月推过的所有票」的表现，产出一份 JSON，供 Astock 网站展示。

口径（与 Astock 量化页的 review_pick 一致）：
    entry = 推荐日（含）之后第一个有价的收盘（前复权，停牌顺延）
    T+7 / T+14 = entry 之后第 7 / 14 个交易日收盘
    月末       = 推荐月最后一个交易日收盘（看「这个月推的票，月底账面如何」）
    基准       = 沪深300 指数同一区间的涨跌；超额 = 个股收益 - 基准

月初跑的时候，月底那几天推的票 T+7 / T+14 还没到期，会记为 null（页面显示「未到期」）。
之后想补全，用 --month 重跑同一个月即可，会覆盖同名文件。

用法：
    python monthly.py                    # 统计上个月（按北京时间）
    python monthly.py --month 2026-09    # 指定月份（回补/重跑）
    python monthly.py --dry              # 只打印摘要，不写文件

输出：reports/monthly/YYYY-MM.json
    workflow 会把它复制到 Astock 仓库的 data/monthly/ 下，网站自动读取。
"""
import os
import sys
import json
import argparse
import datetime
from collections import defaultdict

import baostock as bs

import review

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT_DIR = os.path.join("reports", "monthly")
HORIZONS = [7, 14]
SCHEMA_VERSION = 1


# =============================================================================
# 日期
# =============================================================================
def prev_month(d: datetime.date) -> str:
    first = d.replace(day=1)
    return (first - datetime.timedelta(days=1)).strftime("%Y-%m")


def month_range(month: str):
    """'2026-09' → ('2026-09-01', '2026-09-30')"""
    y, m = map(int, month.split("-"))
    start = datetime.date(y, m, 1)
    nxt = datetime.date(y + (m == 12), m % 12 + 1, 1)
    end = nxt - datetime.timedelta(days=1)
    return start.isoformat(), end.isoformat()


# =============================================================================
# 取数
# =============================================================================
def picks_in_month(pconn, month: str) -> list:
    start, end = month_range(month)
    rows = pconn.execute(
        "SELECT date,code,name,close,signals,score,industry FROM picks "
        "WHERE date BETWEEN ? AND ? ORDER BY date, code",
        (start, end),
    ).fetchall()
    return [
        {"date": d, "code": c, "name": n, "close": cl, "signals": s,
         "score": sc, "industry": _clean_ind(ind)}
        for d, c, n, cl, s, sc, ind in rows
    ]


def _clean_ind(s):
    """证监会行业带 'C38' 这类前缀，展示时去掉。"""
    import re
    return re.sub(r"^[A-Z]\d+", "", s or "") or "未分类"


def fetch_series(codes: list, start: str, end: str, fetch=None) -> dict:
    """
    {code: pd.Series(close, index=date)}；另附 '__bench__' 为沪深300。
    fetch 可注入（测试用），默认走 review._close_series（baostock 前复权）。
    """
    fetch = fetch or review._close_series
    out = {"__bench__": fetch(review.BENCH_CODE, start, end, "3")}
    for i, code in enumerate(codes, 1):
        out[code] = fetch(review.to_bs_code(code), start, end, "2")
        if i % 50 == 0:
            print(f"  …已拉 {i}/{len(codes)}")
    return out


# =============================================================================
# 计算
# =============================================================================
def _bench_close(bench, date: str):
    """基准在 date 当天（没有就取之前最近一天）的收盘。"""
    if bench is None or bench.empty:
        return None
    s = bench[bench.index <= date]
    return float(s.iloc[-1]) if not s.empty else None


def _bench_ret(bench, d0: str, d1: str):
    b0, b1 = _bench_close(bench, d0), _bench_close(bench, d1)
    if not b0 or not b1:
        return None
    return b1 / b0 - 1.0


def evaluate_pick(p: dict, series, bench, month_end: str) -> dict:
    """单条推荐的各持有期收益。拉不到价的整条记为 null。"""
    r = {**p, "entry": None, "entry_date": None}
    for h in HORIZONS:
        r.update({f"ret{h}": None, f"bench{h}": None, f"exc{h}": None,
                  f"exit{h}_date": None})
    r.update({"ret_me": None, "bench_me": None, "exc_me": None})

    if series is None or series.empty:
        return r
    after = series[series.index >= p["date"]]
    if after.empty:
        return r

    entry_d, entry = str(after.index[0]), float(after.iloc[0])
    r["entry"], r["entry_date"] = round(entry, 4), entry_d

    def fill(key, exit_d, exit_p):
        ret = exit_p / entry - 1.0
        b = _bench_ret(bench, entry_d, exit_d)
        r[f"ret{key}"] = ret
        r[f"bench{key}"] = b
        r[f"exc{key}"] = (ret - b) if b is not None else None

    for h in HORIZONS:
        if len(after) > h:
            fill(h, str(after.index[h]), float(after.iloc[h]))
            r[f"exit{h}_date"] = str(after.index[h])

    in_month = after[after.index <= month_end]
    if not in_month.empty:
        fill("_me", str(in_month.index[-1]), float(in_month.iloc[-1]))
    return r


def summarize(items: list, key: str) -> dict:
    """key: '7' / '14' / '_me' → 该持有期的统计。"""
    rk, ek, bk = f"ret{key}", f"exc{key}", f"bench{key}"
    v = [x for x in items if x[rk] is not None]
    exc = [x[ek] for x in v if x[ek] is not None]
    bench = [x[bk] for x in v if x[bk] is not None]
    n = len(v)
    return {
        "n": len(items),
        "valid": n,
        "pending": len(items) - n,
        "avg": sum(x[rk] for x in v) / n if n else None,
        "median": _median([x[rk] for x in v]),
        "up": sum(1 for x in v if x[rk] > 0),
        "up_rate": sum(1 for x in v if x[rk] > 0) / n if n else None,
        "beat": sum(1 for e in exc if e > 0),
        "beat_rate": sum(1 for e in exc if e > 0) / len(exc) if exc else None,
        "avg_exc": sum(exc) / len(exc) if exc else None,
        "bench": sum(bench) / len(bench) if bench else None,
        "best": max((x[rk] for x in v), default=None),
        "worst": min((x[rk] for x in v), default=None),
    }


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def group_stats(items: list, field: str, key: str = "7") -> list:
    g = defaultdict(list)
    for x in items:
        g[x[field]].append(x)
    rows = []
    for name, xs in g.items():
        s = summarize(xs, key)
        rows.append({"name": name, "n": len(xs), "valid": s["valid"],
                     "avg": s["avg"], "up_rate": s["up_rate"],
                     "avg_exc": s["avg_exc"]})
    return sorted(rows, key=lambda r: (-r["n"], r["name"]))


def build_report(month: str, picks: list, series: dict, generated_at: str) -> dict:
    _, month_end = month_range(month)
    bench = series.get("__bench__")
    items = [evaluate_pick(p, series.get(p["code"]), bench, month_end) for p in picks]

    by_day = defaultdict(list)
    for x in items:
        by_day[x["date"]].append(x)
    daily = []
    for d in sorted(by_day):
        s7, sme = summarize(by_day[d], "7"), summarize(by_day[d], "_me")
        daily.append({"date": d, "n": len(by_day[d]),
                      "avg7": s7["avg"], "up_rate7": s7["up_rate"],
                      "avg_me": sme["avg"]})

    as_of = None
    if bench is not None and not bench.empty:
        as_of = str(bench.index[-1])

    def rnd(v):
        return round(v, 6) if isinstance(v, float) else v

    return {
        "schema_version": SCHEMA_VERSION,
        "month": month,
        "generated_at": generated_at,
        "as_of": as_of,                         # 行情截至
        "kpi": {
            "picks": len(items),
            "days": len(by_day),
            "codes": len({x["code"] for x in items}),
            "repeat_codes": _count_repeats(items),   # 月内被推过不止一次的票
        },
        "horizons": {
            "7": summarize(items, "7"),
            "14": summarize(items, "14"),
            "me": summarize(items, "_me"),
        },
        "by_signal": group_stats(items, "signals"),
        "by_industry": group_stats(items, "industry")[:15],
        "daily": daily,
        "items": [{k: rnd(v) for k, v in x.items() if k != "close"} for x in items],
    }


def _count_repeats(items):
    cnt = defaultdict(int)
    for x in items:
        cnt[x["code"]] += 1
    return sum(1 for v in cnt.values() if v > 1)


# =============================================================================
def _p(v):
    return f"{v*100:+.1f}%" if v is not None else "—"


def print_summary(rep: dict):
    k = rep["kpi"]
    print(f"\n===== {rep['month']} 月度战记（行情截至 {rep['as_of']}）=====")
    print(f"  推荐 {k['picks']} 次 / {k['days']} 个交易日 / {k['codes']} 只标的")
    for key, label in (("7", "T+7"), ("14", "T+14"), ("me", "月末")):
        s = rep["horizons"][key]
        print(f"  {label:5s} 样本 {s['valid']:>3}（未到期 {s['pending']}）"
              f"  均值 {_p(s['avg'])}  上涨率 {_p(s['up_rate'])[1:]}"
              f"  超额 {_p(s['avg_exc'])}  沪深300 {_p(s['bench'])}")
    print("  信号：" + "　".join(
        f"{g['name']} {g['n']}次 T+7 {_p(g['avg'])}" for g in rep["by_signal"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", type=str, default="", help="YYYY-MM，默认上个月（北京时间）")
    ap.add_argument("--out", type=str, default=OUT_DIR, help="输出目录")
    ap.add_argument("--dry", action="store_true", help="只打印摘要，不写文件")
    args = ap.parse_args()

    today = review.beijing_today()
    month = args.month or prev_month(today)
    start, _ = month_range(month)

    pconn = review.picks_connect()
    picks = picks_in_month(pconn, month)
    pconn.close()
    if not picks:
        print(f"{month} 没有推荐记录，不生成月报。")
        return
    print(f"{month}：{len(picks)} 条推荐，开始拉行情…")

    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败（{lg.error_msg}），月报需要重拉行情，退出。")
        sys.exit(1)
    try:
        codes = sorted({p["code"] for p in picks})
        series = fetch_series(codes, start, today.isoformat())
    finally:
        bs.logout()

    tz = datetime.timezone(datetime.timedelta(hours=8))
    gen = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    rep = build_report(month, picks, series, gen)
    print_summary(rep)

    if rep["as_of"] is None:
        print("❌ 沪深300 行情为空，疑似接口异常，不写文件。")
        sys.exit(1)

    if args.dry:
        print("\n(--dry，未写文件)")
        return
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{month}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"\n📝 已写 {path}")


if __name__ == "__main__":
    main()
