# daily.py
"""
每日主入口：读 SQLite → 算 MACD 信号 → 推送企业微信。

    python daily.py                # 正常跑（非交易日会自动跳过）
    python daily.py --dry          # 只算不推，先看控制台输出
    python daily.py --force        # 忽略交易日判断（周末测试用）
    python daily.py --no-update    # 不拉最新数据，直接用库里的算

依赖：pip install akshare pandas requests
"""
import sys
import json
import time
import argparse
import datetime
import functools

import baostock as bs
import pandas as pd
import requests

import store
import signals as sig

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass



# =============================================================================
# 配置
# =============================================================================
def load_hook() -> str:
    try:
        import config
        h = getattr(config, "WECOM_HOOK", "").strip()
        if h and "你的key" not in h:
            return h
    except ImportError:
        pass
    import os
    return os.getenv("WECOM_HOOK", "").strip()


# =============================================================================
# 交易日
# =============================================================================
def beijing_today() -> datetime.date:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).date()


@functools.lru_cache(maxsize=8)
def is_trade_day(d: datetime.date) -> bool:
    """用 baostock 的交易日历。接口挂了就退化成'非周末即交易日'，宁可多跑一次。"""
    ds = d.strftime("%Y-%m-%d")
    try:
        bs.login()
        rs = bs.query_trade_dates(start_date=ds, end_date=ds)
        if rs.error_code != "0":
            raise RuntimeError(rs.error_msg)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if not rows:
            return False
        return rows[0][1] == "1"          # is_trading_day
    except Exception as e:
        print(f"⚠️  交易日历查询失败({e})，退化为周一至周五判断")
        return d.weekday() < 5


def cn_date(d: datetime.date) -> str:
    # 注意：不能用 strftime("%m月%d日")，Windows 会 UnicodeEncodeError
    return f"{d.month}月{d.day}日"


# =============================================================================
# 扫描
# =============================================================================
def scan(conn) -> list[dict]:
    codes = store.all_codes(conn)
    if not codes:
        print("❌ 数据库是空的。先跑： python fetch_bs.py --full")
        sys.exit(1)

    hits = []
    for code, name, kind, industry in codes:
        df = store.load(conn, code, limit=400)
        r = sig.scan_latest(df)
        if r:
            r.update({"code": code, "name": name, "kind": kind, "industry": industry})
            hits.append(r)

    # 先按行业聚在一起（方便看扎堆），行业内多重信号排前面
    return sorted(hits, key=lambda x: (x["industry"], -x["score"], x["code"]))


def group_by_industry(hits: list[dict]) -> list[tuple[str, list]]:
    """按行业分组，组内数量多的排前面 —— 扎堆的行业最该被看见。"""
    g = {}
    for h in hits:
        g.setdefault(h["industry"], []).append(h)
    return sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0]))


# =============================================================================
# 企微 markdown
# =============================================================================
def build_summary(hits: list[dict], day: datetime.date) -> str:
    """
    第 1 条：摘要，给通知栏看的。名字一个不落。
    按行业分组呈现 —— 老板一眼就能看出"这 13 只其实只是 3 个板块"。
    """
    ds = cn_date(day)
    if not hits:
        return f"📉 {ds}｜今日无买入信号\n沪深300 中 MACD 金叉 / 底背离均无标的命中。"

    groups = group_by_industry(hits)
    strong = [h for h in hits if h["score"] >= 2]

    head = f"📈 {ds}｜买入信号 {len(hits)} 只 / {len(groups)} 个行业"
    lines = [head, ""]

    for ind, items in groups:
        names = "、".join(h["name"] for h in items)
        flag = " ⚠️同板块扎堆" if len(items) >= 3 else ""
        lines.append(f"【{ind}】{len(items)}只{flag}\n{names}")

    if strong:
        lines += ["", "🔥 金叉+底背离共振：" + "、".join(h["name"] for h in strong)]

    body = "\n".join(lines) + "\n\n完整代码/价格/指标见下条。"
    return body if len(body.encode()) < 2000 else body[:700] + "…\n详见下条明细。"


def build_detail(hits: list[dict], day: datetime.date) -> list[str]:
    """第 2 条起：明细，群里展开看。按行业分组，不截断，超 4000 字节自动分条。"""
    ds = cn_date(day)
    if not hits:
        return [f"### 📉 {ds} 无买入信号\n"
                f"> 沪深300 中，MACD 金叉 / 底背离均无标的命中。\n"
                f"> <font color=\"comment\">技术指标自动筛选结果，非投资建议</font>"]

    groups = group_by_industry(hits)
    crowded = [ind for ind, items in groups if len(items) >= 3]

    blocks = []
    for ind, items in groups:
        warn = "　<font color=\"warning\">⚠️扎堆</font>" if len(items) >= 3 else ""
        blocks.append(f"> **【{ind}】** {len(items)}只{warn}")
        for h in items:
            hot = h["score"] >= 2
            blocks.append(
                f"> {'🔥' if hot else '▪'} {h['name']}　`{h['code']}`　¥{h['close']}"
                f"　<font color=\"{'warning' if hot else 'info'}\">{'+'.join(h['signals'])}</font>"
            )
        blocks.append("> ")

    tail_parts = ["> "]
    if crowded:
        tail_parts.append(
            f"> <font color=\"warning\">⚠️ {('、'.join(crowded))} 存在同板块共振，"
            f"这些信号并非相互独立，同时买入不构成分散</font>")
    tail_parts += [
        "> <font color=\"comment\">🔥 = 金叉与底背离共振（两个独立信号）</font>",
        f"> <font color=\"comment\">baostock 前复权日线　数据截至 {hits[0]['date']}</font>",
        "> <font color=\"comment\">技术指标自动筛选结果，非投资建议</font>",
    ]
    tail = "\n".join(tail_parts)

    # 按 4000 字节切分，保证一只不漏
    parts, cur, n = [], [], 1
    for b in blocks:
        probe = "\n".join([f"### 📈 {ds} 买入信号明细（{n}）", *cur, b, tail])
        if len(probe.encode()) > 4000 and cur:
            parts.append("\n".join([f"### 📈 {ds} 买入信号明细（{n}）", *cur]))
            cur, n = [b], n + 1
        else:
            cur.append(b)

    head = f"### 📈 {ds} 买入信号明细 {len(hits)} 只" if n == 1 \
        else f"### 📈 {ds} 买入信号明细（{n}）"
    parts.append("\n".join([head, *cur, tail]))
    return parts


def push(content: str, kind: str = "text") -> bool:
    """kind: 'text' | 'markdown'"""
    hook = load_hook()
    if not hook:
        print("❌ 没拿到 webhook，检查 config.py")
        return False

    payload = {"msgtype": kind, kind: {"content": content}}
    try:
        res = requests.post(hook, json=payload, timeout=10).json()
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        return False

    if res.get("errcode") == 0:
        print(f"✅ 已推送（{kind}）")
        return True
    print(f"❌ 推送失败 errcode={res.get('errcode')} {res.get('errmsg')}")
    return False


# =============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只算不推，控制台预览")
    ap.add_argument("--force", action="store_true", help="忽略交易日判断")
    ap.add_argument("--no-update", action="store_true", help="不拉最新数据")
    args = ap.parse_args()

    today = beijing_today()
    if not args.force and not is_trade_day(today):
        print(f"{today} 非交易日，退出。")
        return

    # 1. 增量更新数据（走 baostock，不会被限速）
    if not args.no_update:
        print("更新最新行情…")
        import subprocess
        subprocess.run([sys.executable, "fetch_bs.py"], check=False)

    # 2. 算信号
    conn = store.connect()
    hits = scan(conn)

    newest = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    print(f"\n数据截至 {newest}　命中 {len(hits)} 只：")
    for h in hits[:25]:
        print(f"  {'🔥' if h['score']>=2 else ' ▪'} {h['name']}({h['code']}) "
              f"{h['close']}  {'/'.join(h['signals'])}")
    if len(hits) > 25:
        print(f"  …另有 {len(hits)-25} 只")

    with open("data/signals.json", "w", encoding="utf-8") as f:
        json.dump({"date": str(today), "data_date": newest, "hits": hits},
                  f, ensure_ascii=False, indent=2)

    summary = build_summary(hits, today)
    details = build_detail(hits, today)

    # 3. 推送：先摘要（通知栏看全名），再明细（群里看详情）
    if args.dry:
        print("\n===== 第1条 · 摘要（text，通知栏用）=====")
        print(summary)
        for i, d in enumerate(details, 1):
            print(f"\n===== 第{i+1}条 · 明细（markdown）=====")
            print(d)
        print("\n(--dry，未推送)")
        return

    push(summary, "text")
    for d in details:
        time.sleep(300)          # 别贴太紧，通知会被合并
        push(d, "markdown")


if __name__ == "__main__":
    main()