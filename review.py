# review.py
"""
推荐复盘：把 N 天前推过的股票，按「推荐日收盘 → 最新收盘」算一下收益率。

存储分离：
    - market.db  ← 只放行情（daily / meta），保持纯净，本脚本只读它
    - picks.db   ← 本脚本自建，专存「哪天推了哪些票」，和行情库互不污染

配合 daily.py：
    - daily.py 每天推送后调 record_picks()，把当天命中的信号写进 picks.db。
    - 本脚本每晚单独跑一次，取「7 天前」「14 天前」这两天推的票，
      重新拉前复权价算区间涨跌，和沪深300比一比，再推一条 markdown。

用法：
    python review.py                          # 复盘「今天往前 7 天 / 14 天」两批
    python review.py --dry                    # 只算不推，先看控制台
    python review.py --date 2025-07-21        # 指定复盘基准日（回补/测试用）
    python review.py --windows 7,14,30        # 自定义回看窗口（天）

    # 补录历史推荐（库里没记录时，手工把当天推过的代码灌进来）：
    python review.py --add 2025-07-07 --codes 600519,000001,600036
    #   name/industry 自动从 market.db 的 meta 取；signals 尽量从历史数据重建，
    #   重建不出来就标「手工补录」。补完再正常 python review.py 即可。

    # 或者不给代码、直接用历史数据整批重建（按当前 signals.py 逻辑）：
    python review.py --backfill

为什么复盘要重新拉价，而不是直接用库里的？
    前复权价会随分红/除权「整体下移」（锚定最新价）。而 fetch_bs.py 增量更新
    只回补最近 5 天，更早的历史仍停在旧锚点。若推荐日到今天之间发生分红，
    用「旧锚点的推荐日价」减「新锚点的今日价」，收益率会算错。所以这里对每只票
    重拉 [推荐日, 今天] 的前复权序列，两端锚点一致，收益率才准。
    （拉不到时退回库存价近似，结果打 * 标记。）

依赖：pip install baostock pandas requests
"""
import os
import sys
import sqlite3
import argparse
import datetime

import baostock as bs
import pandas as pd
import requests

import store
import signals as sig

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PICKS_DB = os.path.join("data", "picks.db")   # 独立于 market.db
DEFAULT_WINDOWS = [7, 14]                       # 回看天数
BENCH_CODE = "sh.000300"                         # 沪深300 指数，作基准


# =============================================================================
# 配置 / 推送 —— 与 daily.py 同一套逻辑，这里自带一份，让本脚本能独立跑
# =============================================================================
def load_hook() -> str:
    try:
        import config
        h = getattr(config, "WECOM_HOOK", "").strip()
        if h and "你的key" not in h:
            return h
    except ImportError:
        pass
    return os.getenv("WECOM_HOOK", "").strip()


def push(content: str, kind: str = "markdown") -> bool:
    hook = load_hook()
    if not hook:
        print("❌ 没拿到 webhook，检查 config.py")
        return False
    payload = {"msgtype": kind, kind: {"content": content}}
    try:
        res = requests.post(hook, json=payload, timeout=15).json()
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        return False
    if res.get("errcode") == 0:
        print("✅ 已推送（复盘）")
        return True
    print(f"❌ 推送失败 errcode={res.get('errcode')} {res.get('errmsg')}")
    return False


def beijing_today() -> datetime.date:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).date()


def cn_date(d: datetime.date) -> str:
    return f"{d.month}月{d.day}日"


def to_bs_code(code: str) -> str:
    """600519 → sh.600519 ；000001 → sz.000001"""
    return ("sh." if code.startswith(("6", "5", "9")) else "sz.") + code


# =============================================================================
# picks.db —— 独立库，只装「哪天推了哪些票」
# =============================================================================
def _ensure_table(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS picks (
            date     TEXT NOT NULL,      -- 推荐日（北京时间）
            code     TEXT NOT NULL,
            name     TEXT,
            close    REAL,               -- 推荐日收盘（前复权，仅展示/兜底用）
            signals  TEXT,               -- '低位金叉+底背离' / '手工补录'
            score    INTEGER,
            industry TEXT,
            PRIMARY KEY (date, code)     -- 同一天同一只只留一行
        );
        CREATE INDEX IF NOT EXISTS idx_picks_date ON picks(date);
        """
    )
    conn.commit()


def picks_connect() -> sqlite3.Connection:
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(PICKS_DB)
    _ensure_table(conn)
    return conn


def record_picks(day, hits: list, conn: sqlite3.Connection = None) -> int:
    """
    daily.py 推送时调用（不传 conn，自己开 picks.db）：把当天命中的信号存档。
    空命中不写。传 conn 时复用调用方的连接、且不关闭（测试/批量用）。
    """
    if not hits:
        return 0
    own = conn is None
    conn = conn or picks_connect()
    ds = str(day)
    rows = [
        (ds, h["code"], h["name"], float(h["close"]),
         "+".join(h["signals"]), int(h["score"]), h.get("industry", "未分类"))
        for h in hits
    ]
    conn.executemany(
        "INSERT INTO picks (date,code,name,close,signals,score,industry) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(date,code) DO UPDATE SET "
        "name=excluded.name, close=excluded.close, signals=excluded.signals, "
        "score=excluded.score, industry=excluded.industry",
        rows,
    )
    conn.commit()
    if own:
        conn.close()
    return len(rows)


def picks_on(pconn, date_str: str) -> list:
    rows = pconn.execute(
        "SELECT code,name,close,signals,score,industry FROM picks "
        "WHERE date=? ORDER BY score DESC, code",
        (date_str,),
    ).fetchall()
    return [
        {"code": c, "name": n, "close": cl, "signals": s,
         "score": sc, "industry": ind}
        for c, n, cl, s, sc, ind in rows
    ]


# =============================================================================
# 手工补录 / 历史重建 —— 都写进 picks.db，只读 market.db
# =============================================================================
def _meta_of(mconn, code: str):
    """从 market.db 取 (name, industry)。取不到给缺省。"""
    row = mconn.execute(
        "SELECT name, COALESCE(industry,'未分类') FROM meta WHERE code=?",
        (code,),
    ).fetchone()
    return (row[0], row[1]) if row else (code, "未分类")


def _reconstruct_signal(mconn, code: str, date_str: str):
    """
    用 market.db 里 <= date_str 的历史，按当前 signals.py 重算 code 当天的信号。
    命中当天则返回 (signals_str, score, close)，否则 None。
    """
    df = store.load(mconn, code, limit=100000)
    if df is None:
        return None
    d = df[df["date"] <= date_str]
    if len(d) < 60:
        return None
    r = sig.scan_latest(d.reset_index(drop=True))
    if r and r.get("date") == date_str:
        return "+".join(r["signals"]), int(r["score"]), float(r["close"])
    return None


def add_picks(pconn, mconn, date_str: str, codes: list) -> int:
    """
    手工补录：把「date_str 当天推过的代码」灌进 picks.db。
    name/industry 从 market.db 取；signals 尽量重建，重建不出来标「手工补录」。
    收益率复盘时会重拉行情，所以 close 缺失也不影响主路径（只影响兜底近似）。
    """
    hits = []
    for code in codes:
        code = code.strip()
        if not code:
            continue
        name, industry = _meta_of(mconn, code)
        rec = _reconstruct_signal(mconn, code, date_str)
        if rec:
            signals_str, score, close = rec
        else:
            signals_str, score = "手工补录", 1
            row = mconn.execute(
                "SELECT close FROM daily WHERE code=? AND date=?",
                (code, date_str),
            ).fetchone()
            close = float(row[0]) if row and row[0] else 0.0
        hits.append({"code": code, "name": name, "industry": industry,
                     "close": close, "signals": signals_str.split("+"),
                     "score": score})
    if not hits:
        print(f"  ⚠️  {date_str}：没有有效代码，未补录")
        return 0
    n = record_picks(date_str, hits, conn=pconn)
    print(f"  ➕ 补录 {date_str}：{n} 只 → {'、'.join(h['name'] for h in hits)}")
    return n


def backfill(pconn, mconn, date_str: str) -> int:
    """
    不给代码、直接用历史数据整批重建 date_str 当天的推荐（引导用）。
    注意：这是事后按当前 signals.py 逻辑重建的「假如那天会推什么」，
    不等于当日真实推送；若 signals.py 之后改过口径，重建结果会随之变化。
    """
    codes = store.all_codes(mconn)
    hits = []
    for code, name, kind, industry in codes:
        rec = _reconstruct_signal(mconn, code, date_str)
        if rec:
            signals_str, score, close = rec
            hits.append({"code": code, "name": name, "industry": industry,
                         "close": close, "signals": signals_str.split("+"),
                         "score": score})
    n = record_picks(date_str, hits, conn=pconn)
    print(f"  🔧 回填 {date_str}：按历史数据重建 {n} 条推荐")
    return n


# =============================================================================
# 取价 / 算收益
# =============================================================================
def _close_series(bs_code: str, start: str, end: str, adjust: str = "2") -> pd.Series:
    """拉 [start,end] 收盘价，index=date(str)，默认前复权。取不到返回空 Series。"""
    rs = bs.query_history_k_data_plus(
        bs_code, "date,close",
        start_date=start, end_date=end,
        frequency="d", adjustflag=adjust,
    )
    if rs.error_code != "0":
        return pd.Series(dtype=float)
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=rs.fields)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    if df.empty:
        return pd.Series(dtype=float)
    return pd.Series(df["close"].values, index=df["date"].astype(str).values)


def _return_from(series: pd.Series, rec_date: str):
    """
    entry = 推荐日（含）之后第一个有价的收盘（停牌就顺延）；
    exit  = 序列最后一个收盘。
    返回 (entry, exit, exit_date, ret) 或 None。
    """
    if series.empty:
        return None
    on_or_after = series[series.index >= rec_date]
    if on_or_after.empty:
        return None
    entry = float(on_or_after.iloc[0])
    exit_p = float(series.iloc[-1])
    exit_d = str(series.index[-1])
    if entry <= 0:
        return None
    return entry, exit_p, exit_d, exit_p / entry - 1.0


def _fallback_return(mconn, code: str, entry_close):
    """重拉失败时的兜底：库存推荐价 → market.db 最新价。不含复权修正，仅近似。"""
    if not entry_close or float(entry_close) <= 0:
        return None
    row = mconn.execute(
        "SELECT close,date FROM daily WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not row or not row[0]:
        return None
    exit_p, exit_d = float(row[0]), row[1]
    entry = float(entry_close)
    return entry, exit_p, exit_d, exit_p / entry - 1.0


def bench_return(rec_date: str, today_str: str):
    """沪深300 指数区间涨跌，作基准。指数无需复权。"""
    s = _close_series(BENCH_CODE, rec_date, today_str, adjust="3")
    r = _return_from(s, rec_date)
    return r[3] if r else None


def review_cohort(pconn, mconn, rec_date: str, today_str: str):
    """复盘某一天推的全部票。无记录返回 None。"""
    picks = picks_on(pconn, rec_date)
    if not picks:
        return None

    results = []
    for p in picks:
        s = _close_series(to_bs_code(p["code"]), rec_date, today_str)
        r = _return_from(s, rec_date)
        approx = False
        if r is None:
            r = _fallback_return(mconn, p["code"], p["close"])
            approx = True
        if r is None:
            results.append({**p, "ret": None, "exit_date": None, "approx": True})
        else:
            entry, exit_p, exit_d, ret = r
            results.append({**p, "entry": entry, "exit": exit_p,
                            "exit_date": exit_d, "ret": ret, "approx": approx})

    valid = [x for x in results if x["ret"] is not None]
    avg = sum(x["ret"] for x in valid) / len(valid) if valid else None
    win = sum(1 for x in valid if x["ret"] > 0)
    results.sort(key=lambda x: (x["ret"] is None, -(x["ret"] or 0)))
    return {
        "rec_date": rec_date,
        "results": results,
        "avg": avg,
        "win": win,
        "n_valid": len(valid),
        "n": len(results),
    }


# =============================================================================
# 企微 markdown
# =============================================================================
def build_review_message(cohorts: list, as_of: datetime.date, verbose: bool = True) -> str:
    ds = cn_date(as_of)
    if not cohorts:
        return (f"### 📊 {ds}｜推荐复盘\n"
                f"> 7 天 / 14 天前均无推荐记录，暂无可复盘标的。\n"
                f"> <font color=\"comment\">收盘价对比，非投资建议</font>")

    L = [f"### 📊 {ds}｜历史推荐复盘",
         "> 推荐当日收盘 → 最新收盘（前复权），未计交易成本",
         "> "]

    for win_days, c in cohorts:
        head = f"> **【{win_days}天前 · {c['rec_label']}推 {c['n']}只】**"
        if c["avg"] is not None:
            arrow = "📈" if c["avg"] >= 0 else "📉"
            color = "warning" if c["avg"] >= 0 else "info"
            head += (f"　{arrow}均值 <font color=\"{color}\">{c['avg']*100:+.1f}%</font>"
                     f"　胜率 {c['win']}/{c['n_valid']}")
            bench = c.get("bench")
            if bench is not None:
                diff = c["avg"] - bench
                head += (f"　{'跑赢' if diff >= 0 else '跑输'}沪深300 "
                         f"{diff*100:+.1f}%")
        L.append(head)

        for x in c["results"]:
            if x["ret"] is None:
                if verbose:
                    L.append(f"> ▪ {x['name']}　`{x['code']}`　"
                             f"<font color=\"comment\">无数据</font>")
                continue
            dot = "🟢" if x["ret"] >= 0 else "🔴"
            star = "*" if x.get("approx") else ""
            color = "warning" if x["ret"] >= 0 else "info"
            tag = f"　<font color=\"comment\">{x['signals']}</font>" if verbose else ""
            L.append(f"> {dot} {x['name']}　`{x['code']}`　"
                     f"<font color=\"{color}\">{x['ret']*100:+.1f}%{star}</font>{tag}")
        L.append("> ")

    if any(c.get("bench") is not None for _, c in cohorts):
        bench_line = "　".join(
            f"{w}天 {c['bench']*100:+.1f}%"
            for w, c in cohorts if c.get("bench") is not None
        )
        L.append(f"> <font color=\"comment\">沪深300同期：{bench_line}</font>")
    L.append("> <font color=\"comment\">🟢涨 🔴跌｜* 为估算（重拉失败用库存价近似）｜"
             "按收盘价不含手续费滑点｜仅回顾信号有效性，非投资建议</font>")
    return "\n".join(L)


# =============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只算不推")
    ap.add_argument("--date", type=str, default="", help="复盘基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--windows", type=str, default="", help="回看天数，逗号分隔，默认 7,14")
    ap.add_argument("--backfill", action="store_true",
                    help="目标日无记录时，用历史数据整批重建当天推荐")
    ap.add_argument("--add", type=str, default="", metavar="YYYY-MM-DD",
                    help="手工补录某天的推荐（配合 --codes）")
    ap.add_argument("--codes", type=str, default="", help="逗号分隔的代码，配合 --add")
    args = ap.parse_args()

    mconn = store.connect()          # market.db，只读
    pconn = picks_connect()          # picks.db，独立

    # ---- 补录模式：加完即退，不做复盘 ----
    if args.add:
        if not args.codes:
            print("❌ --add 需要配合 --codes，例：--add 2025-07-07 --codes 600519,000001")
            return
        add_picks(pconn, mconn, args.add, args.codes.split(","))
        return

    as_of = datetime.date.fromisoformat(args.date) if args.date else beijing_today()
    windows = ([int(x) for x in args.windows.split(",") if x.strip()]
               if args.windows else DEFAULT_WINDOWS)
    today_str = as_of.strftime("%Y-%m-%d")

    lg = bs.login()
    if lg.error_code != "0":
        print(f"⚠️  baostock 登录失败（{lg.error_msg}），将退回库存价近似")
    try:
        cohorts = []
        for w in sorted(set(windows)):
            rec_date = (as_of - datetime.timedelta(days=w)).strftime("%Y-%m-%d")

            if args.backfill and not picks_on(pconn, rec_date):
                backfill(pconn, mconn, rec_date)

            c = review_cohort(pconn, mconn, rec_date, today_str)
            if not c:
                print(f"  {w}天前（{rec_date}）无推荐记录，跳过")
                continue
            c["rec_label"] = cn_date(datetime.date.fromisoformat(rec_date))
            c["bench"] = bench_return(rec_date, today_str)
            cohorts.append((w, c))

            avg = f"{c['avg']*100:+.1f}%" if c["avg"] is not None else "—"
            print(f"\n  {w}天前 {rec_date}：{c['n']}只　均值 {avg}　"
                  f"胜 {c['win']}/{c['n_valid']}")
            for x in c["results"]:
                r = f"{x['ret']*100:+.1f}%" if x["ret"] is not None else "无数据"
                print(f"     {x['name']}({x['code']}) {r}")
    finally:
        bs.logout()

    msg = build_review_message(cohorts, as_of)
    if len(msg.encode()) > 4000:
        msg = build_review_message(cohorts, as_of, verbose=False)

    if args.dry:
        print(f"\n===== 复盘消息（{len(msg.encode())} 字节 / 上限 4096）=====")
        print(msg)
        print("\n(--dry，未推送)")
        return

    push(msg, "markdown")


if __name__ == "__main__":
    main()