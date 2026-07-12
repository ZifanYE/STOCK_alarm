# fetch_bs.py
"""
baostock 版抓取 —— 替代 fetch_history.py

为什么换：
  - 登录制、官方为批量设计，不存在 IP 封禁/限速问题（这正是你担心的）
  - 300 只股票拉 4 年历史，约 3-6 分钟（akshare 逐只要 30-40 分钟）
  - 沪深300 成分股、交易日历都能从 baostock 直接拿，少一个依赖

用法：
    python fetch_bs.py --full           # 首次：拉完整历史
    python fetch_bs.py                  # 每天：增量补最新
    python fetch_bs.py --full --limit 5 # 试水

⚠️ 时效性：baostock 的当日数据通常在**北京时间 19:00 之后**才更新。
   如果你想 15:15 收盘就推送，见文件底部的说明。
"""
import sys
import time
import argparse
import datetime

import baostock as bs
import pandas as pd

import store

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

START_DATE = "2021-01-01"
FIELDS = "date,open,high,low,close,volume,tradestatus"


# =============================================================================
# 工具
# =============================================================================
def beijing_today() -> datetime.date:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).date()


def to_bs_code(code: str) -> str:
    """600519 → sh.600519 ；000001 → sz.000001"""
    return ("sh." if code.startswith(("6", "5", "9")) else "sz.") + code


def rs_to_df(rs) -> pd.DataFrame:
    """baostock 的结果集是个游标，字段全是字符串，得自己转。"""
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=rs.fields)


def check(rs, what: str):
    """baostock 不抛异常，错误码藏在 error_code 里，必须手动检查。"""
    if rs.error_code != "0":
        raise RuntimeError(f"{what} 失败: [{rs.error_code}] {rs.error_msg}")
    return rs


# =============================================================================
# 交易日历 / 成分股
# =============================================================================
def is_trade_day(d: datetime.date) -> bool:
    ds = d.strftime("%Y-%m-%d")
    df = rs_to_df(check(bs.query_trade_dates(start_date=ds, end_date=ds), "交易日历"))
    if df.empty:
        return False
    return df.iloc[0]["is_trading_day"] == "1"


def hs300(day: datetime.date) -> list[tuple[str, str]]:
    """
    沪深300 成分股。注意：baostock 的成分股按日期快照，
    传今天可能返回空（还没更新），所以往前退着找最近有数据的一天。
    """
    for back in range(0, 15):
        d = (day - datetime.timedelta(days=back)).strftime("%Y-%m-%d")
        df = rs_to_df(check(bs.query_hs300_stocks(date=d), "沪深300成分股"))
        if not df.empty:
            print(f"  成分股快照日期: {d}（{len(df)} 只）")
            # code 形如 sh.600519，去掉前缀存库
            return [(c.split(".")[1], n) for c, n in zip(df["code"], df["code_name"])]
    raise RuntimeError("15 天内都没拿到沪深300成分股，检查 baostock 状态")


def fetch_industries() -> dict:
    """
    一次请求拿全市场的申万一级行业分类。
    baostock 的 query_stock_industry 不传 code 就返回全部，300 只股票不用发 300 次。
    """
    rs = check(bs.query_stock_industry(), "行业分类")
    df = rs_to_df(rs)
    if df.empty:
        return {}
    # code 形如 sh.600519 → 600519
    return {
        c.split(".")[1]: (ind or "未分类")
        for c, ind in zip(df["code"], df["industry"])
    }


# =============================================================================
# 行情
# =============================================================================
def fetch(code: str, start: str, end: str) -> pd.DataFrame | None:
    """
    adjustflag: '1'=后复权  '2'=前复权  '3'=不复权(默认)
    必须用 '2'，不复权的话除权日会有假跳空，MACD 会造出假金叉/假背离。
    """
    rs = bs.query_history_k_data_plus(
        to_bs_code(code), FIELDS,
        start_date=start, end_date=end,
        frequency="d", adjustflag="2",
    )
    if rs.error_code != "0":
        raise RuntimeError(f"[{rs.error_code}] {rs.error_msg}")

    df = rs_to_df(rs)
    if df.empty:
        return None

    # 停牌日 tradestatus=0，价格是 0 或空，必须剔除，否则 K 线上出现一根价格为 0 的柱子
    df = df[df["tradestatus"] == "1"]
    if df.empty:
        return None

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]

    return df[["date", "open", "high", "low", "close", "volume"]] if not df.empty else None


# =============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="拉完整历史（首次用）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只，试水用")
    args = ap.parse_args()

    # baostock 必须先 login，匿名即可，不需要注册
    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败: {lg.error_msg}")
        sys.exit(1)
    print("✅ baostock 已登录")

    try:
        conn = store.connect()
        today = beijing_today()
        end = today.strftime("%Y-%m-%d")

        universe = hs300(today)
        if args.limit:
            universe = universe[: args.limit]

        print(f"\n开始抓取 {len(universe)} 只，模式={'全量' if args.full else '增量'}\n")

        ok = skip = empty = fail = 0
        t0 = time.time()

        for i, (code, name) in enumerate(universe, 1):
            last = store.get_last_date(conn, code)

            # 断点续传：已经是最新的直接跳过，中断后重跑不会白费功夫
            if last and pd.to_datetime(last).date() >= today:
                skip += 1
                continue

            if last and not args.full:
                # 增量：从上次日期往前退 5 天重拉，覆盖可能的数据修正 + 复权因子变动
                start = (pd.to_datetime(last) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
            else:
                start = START_DATE

            try:
                df = fetch(code, start, end)
                if df is None:
                    empty += 1
                    continue

                store.upsert_daily(conn, code, df)
                last_date = df["date"].max()
                store.set_meta(conn, code, name, "stock", last_date)
                ok += 1

                if i % 25 == 0 or args.limit:
                    el = time.time() - t0
                    eta = el / i * (len(universe) - i)
                    print(f"[{i:3}/{len(universe)}] {name}({code}) {len(df)}行 截至{last_date}"
                          f"　已用{el:.0f}s 预计还要{eta:.0f}s")

            except KeyboardInterrupt:
                print("\n⏸  已中断。进度已存库，重跑本命令会接着来。")
                break
            except Exception as e:
                fail += 1
                print(f"[{i:3}/{len(universe)}] ❌ {name}({code}) {e}")

        print(f"\n完成：成功 {ok}｜跳过(已最新) {skip}｜无数据 {empty}｜失败 {fail}"
              f"｜耗时 {(time.time()-t0)/60:.1f} 分钟")

        # 更新行业标签（一次请求，很快，每次都刷新）
        try:
            ind = fetch_industries()
            store.set_industry(conn, ind)
            n = conn.execute(
                "SELECT COUNT(*) FROM meta WHERE industry IS NOT NULL").fetchone()[0]
            print(f"✅ 行业标签已更新（{n}/{len(universe)} 只有分类）")
        except Exception as e:
            print(f"⚠️  行业标签更新失败: {e}（不影响信号计算）")

        print("\n当前库存：")
        for k, v in store.stats(conn).items():
            print(f"  {k}: {v}")

        # 数据新鲜度检查 —— 这个提醒很重要，见文件顶部说明
        newest = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        if newest and pd.to_datetime(newest).date() < today and is_trade_day(today):
            print(f"\n⚠️  今天({today})是交易日，但库里最新只到 {newest}。")
            print("   baostock 当日数据通常 19:00 后才更新，现在跑太早了。")

    finally:
        bs.logout()


if __name__ == "__main__":
    main()