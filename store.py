# store.py
"""
统一存储层：一个 SQLite 文件装下所有标的的日线。

为什么不继续用 CSV：
  - 300 只股票 = 300 个文件，增量更新时每次都要整个读+整个写
  - SQLite 支持 UPSERT，重复跑不会产生脏数据，中断了也能接着跑
  - Python 自带 sqlite3，不用装任何东西
"""
import os
import sqlite3
import pandas as pd

DB_PATH = os.path.join("data", "market.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    code    TEXT NOT NULL,
    date    TEXT NOT NULL,          -- 'YYYY-MM-DD'
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  REAL,
    PRIMARY KEY (code, date)        -- 同一天同一只股票只会有一行
);
CREATE TABLE IF NOT EXISTS meta (
    code       TEXT PRIMARY KEY,
    name       TEXT,
    kind       TEXT,                -- 'stock' | 'etf'
    last_date  TEXT,                -- 已抓到哪一天，断点续传靠它
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_code ON daily(code);
"""


def connect() -> sqlite3.Connection:
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """给已经建好的库补字段。SQLite 没有 ADD COLUMN IF NOT EXISTS，只能试错。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(meta)")}
    if "industry" not in cols:
        conn.execute("ALTER TABLE meta ADD COLUMN industry TEXT")
        conn.commit()
        print("  [migrate] meta 表已添加 industry 字段")


# =============================================================================
# 行业
# =============================================================================
def set_industry(conn, mapping: dict):
    """mapping: {code: industry}。只更新已存在的标的。"""
    conn.executemany(
        "UPDATE meta SET industry=? WHERE code=?",
        [(ind, code) for code, ind in mapping.items()],
    )
    conn.commit()


def industry_of(conn, code: str) -> str:
    r = conn.execute("SELECT industry FROM meta WHERE code=?", (code,)).fetchone()
    return (r[0] if r and r[0] else "未分类")


# =============================================================================
# 写
# =============================================================================
def upsert_daily(conn, code: str, df: pd.DataFrame):
    """写入日线。已存在的行会被覆盖，不会重复。df 需含 date/open/high/low/close/volume。"""
    if df is None or df.empty:
        return 0

    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    for c in ["open", "high", "low", "close", "volume"]:
        if c not in d.columns:
            d[c] = None
    d["code"] = code

    rows = d[["code", "date", "open", "high", "low", "close", "volume"]].values.tolist()
    conn.executemany(
        "INSERT INTO daily (code,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(code,date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        rows,
    )
    conn.commit()
    return len(rows)


def set_meta(conn, code: str, name: str, kind: str, last_date: str):
    conn.execute(
        "INSERT INTO meta (code,name,kind,last_date,updated_at) "
        "VALUES (?,?,?,?,datetime('now')) "
        "ON CONFLICT(code) DO UPDATE SET "
        "name=excluded.name, kind=excluded.kind, "
        "last_date=excluded.last_date, updated_at=excluded.updated_at",
        (code, name, kind, last_date),
    )
    conn.commit()


# =============================================================================
# 读
# =============================================================================
def get_last_date(conn, code: str) -> str | None:
    r = conn.execute("SELECT last_date FROM meta WHERE code=?", (code,)).fetchone()
    return r[0] if r else None


def load(conn, code: str, limit: int = 400) -> pd.DataFrame | None:
    """取最近 limit 根 K 线，按日期升序返回。算 MACD 有 400 根绰绰有余。"""
    df = pd.read_sql_query(
        "SELECT date,open,high,low,close,volume FROM daily "
        "WHERE code=? ORDER BY date DESC LIMIT ?",
        conn, params=(code, limit),
    )
    if df.empty:
        return None
    return df.sort_values("date").reset_index(drop=True)


def all_codes(conn, kind: str | None = None) -> list[tuple]:
    """返回 [(code, name, kind, industry), ...]"""
    q = "SELECT code, name, kind, COALESCE(industry,'未分类') FROM meta"
    p = ()
    if kind:
        q += " WHERE kind=?"
        p = (kind,)
    return conn.execute(q, p).fetchall()


def stats(conn) -> dict:
    n_code = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    n_row = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    rng = conn.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
    return {"标的数": n_code, "总行数": n_row, "日期范围": f"{rng[0]} ~ {rng[1]}"}


# =============================================================================
# 把你已有的 CSV 导进来（一次性）
# =============================================================================
OLD_CSV = {   # 你 engine_cn.py 里的命名
    "hs300_etf":  ("510300", "沪深300 ETF"),
    "czcz_etf":   ("159967", "创成长 ETF"),
    "zz2000_etf": ("563000", "中证2000 ETF"),
    "gold_etf":   ("518880", "黄金 ETF"),
    "nasdaq_etf": ("513100", "纳指 ETF"),
}


def import_old_csv(conn, folder: str = "data"):
    """把 Streamlit 时代抓的 5 个 ETF CSV 导入 SQLite。跑一次就行。"""
    total = 0
    for fname, (code, name) in OLD_CSV.items():
        path = os.path.join(folder, f"{fname}.csv")
        if not os.path.exists(path):
            print(f"  跳过 {fname}.csv（不存在）")
            continue
        try:
            df = pd.read_csv(path)
        except Exception as e:
            print(f"  ❌ {fname}.csv 读取失败: {e}")
            continue

        missing = {"date", "close"} - set(df.columns)
        if missing:
            print(f"  ❌ {fname}.csv 缺列 {missing}，跳过")
            continue
        if "low" not in df.columns:
            print(f"  ⚠️  {fname}.csv 没有 low 列，底背离算不了（金叉可以）")

        n = upsert_daily(conn, code, df)
        last = pd.to_datetime(df["date"]).max().strftime("%Y-%m-%d")
        set_meta(conn, code, name, "etf", last)
        print(f"  ✅ {name} ({code})  {n} 行，截至 {last}")
        total += n
    return total


if __name__ == "__main__":
    conn = connect()
    print("导入旧 CSV…")
    import_old_csv(conn)
    print("\n当前库存：")
    for k, v in stats(conn).items():
        print(f"  {k}: {v}")