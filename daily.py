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
import base64
import hashlib
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
def build_message(hits: list[dict], day: datetime.date, data_date: str) -> str:
    """
    合并成**一条** markdown。企微一条消息只能一个 msgtype，图文没法共存，
    所以选 markdown —— 通知栏能读到开头，群里展开能看全。

    结构（顺序是刻意的）：
      1. 标题行 + 全部名字   ← 锁屏预览只显示开头，所以名字必须靠前
      2. 按行业分组的明细    ← 代码、价格、信号
      3. 扎堆警告 + 免责声明

    上限 4096 字节，超了自动降级（砍掉 DIF 等次要信息）。
    """
    ds = cn_date(day)

    if not hits:
        return (f"### 📉 {ds}｜今日无买入信号\n"
                f"> 沪深300 中，MACD 金叉 / 底背离均无标的命中。\n"
                f"> <font color=\"comment\">数据截至 {data_date}｜非投资建议</font>")

    groups = group_by_industry(hits)
    crowded = [g for g, items in groups if len(items) >= 3]
    strong = [h for h in hits if h["score"] >= 2]

    def compose(verbose: bool) -> str:
        ###具体的格式
        names = "、".join(h["name"] for h in hits)
        L = [f"### 📈 {ds}｜买入信号 {len(hits)} 只 / {len(groups)} 个行业：{names}"]
        if strong:
            L.append(f"> 🔥 <font color=\"warning\">金叉+底背离共振："
                     f"{'、'.join(h['name'] for h in strong)}</font>")
        L.append("> ")

        for ind, items in groups:
            warn = "　<font color=\"warning\">⚠️扎堆</font>" if len(items) >= 3 else ""
            L.append(f"> **【{ind}】**{len(items)}只{warn}")
            for h in items:
                hot = h["score"] >= 2
                dif = f"　<font color=\"comment\">DIF {h['dif']:+.3f}</font>" if verbose else ""
                L.append(
                    f"> {'🔥' if hot else '▪'} {h['name']}　`{h['code']}`　¥{h['close']}"
                    f"　<font color=\"{'warning' if hot else 'info'}\">"
                    f"{'+'.join(h['signals'])}</font>{dif}")
            L.append("> ")

        if crowded:
            L.append(f"> <font color=\"warning\">⚠️ {'、'.join(crowded)} 同板块共振，"
                     f"这些信号并非相互独立，同时买入不构成分散</font>")
        L.append(f"> <font color=\"comment\">🔥=金叉与底背离共振｜前复权日线｜"
                 f"数据截至 {data_date}</font>")
        L.append("> <font color=\"comment\">技术指标自动筛选结果，非投资建议</font>")
        return "\n".join(L)

    msg = compose(verbose=True)
    if len(msg.encode()) > 4000:
        msg = compose(verbose=False)      # 降级：砍掉 DIF
    return msg


def push(content: str, kind: str = "text") -> bool:
    """kind: 'text' | 'markdown'"""
    hook = load_hook()
    if not hook:
        print("❌ 没拿到 webhook，检查 config.py")
        return False

    payload = {"msgtype": kind, kind: {"content": content}}
    return _send(hook, payload, kind)


def push_image(path: str) -> bool:
    """企微图片消息：base64 + md5。注意 md5 是**原始二进制**的 md5，不是 base64 串的。
    搞错会返回 errcode 40058，很多人卡在这里。"""
    hook = load_hook()
    if not hook:
        return False

    raw = open(path, "rb").read()
    if len(raw) > 2 * 1024 * 1024:
        print("❌ 图片超过 2MB，企微不收")
        return False

    payload = {"msgtype": "image", "image": {
        "base64": base64.b64encode(raw).decode(),
        "md5": hashlib.md5(raw).hexdigest(),
    }}
    return _send(hook, payload, "image")


def _send(hook: str, payload: dict, label: str) -> bool:
    try:
        res = requests.post(hook, json=payload, timeout=15).json()
    except Exception as e:
        print(f"❌ 推送异常({label}): {e}")
        return False

    if res.get("errcode") == 0:
        print(f"✅ 已推送（{label}）")
        return True
    print(f"❌ 推送失败({label}) errcode={res.get('errcode')} {res.get('errmsg')}")
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

    msg = build_message(hits, today, newest or "-")

    # 3. 推送：只发一条 markdown。
    # 老板嫌两条烦，而企微一条消息只能一个 msgtype（图文不能共存），
    # 所以放弃图片、保留 markdown —— 通知栏能读到开头的名字，群里展开看全。
    if args.dry:
        print(f"\n===== 单条消息（{len(msg.encode())} 字节 / 上限 4096）=====")
        print(msg)
        print("\n(--dry，未推送)")
        return

    push(msg, "markdown")


if __name__ == "__main__":
    main()