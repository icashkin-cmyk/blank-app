"""從 FinMind 抓臺股日線，落地成 CSV 並寫 meta.json（來源、抓取日期、區間、欄位）。

用法（可在 Mac 或已開放 api.finmindtrade.com 的環境執行）：
    export FINMIND_TOKEN=你的token   # 可省略，省略時額度較低
    python3 tools/fetch_finmind.py 2330 2454 2317 --start 2015-01-01 --end 2026-10-19

每檔抓 TaiwanStockPrice（實際價）、TaiwanStockDividendResult（除權息結果）與
TaiwanStockCapitalReductionReferencePrice（減資恢復買賣參考價），
以事件日 after_price / before_price 為調整比回溯連乘成 factor，寫出 <代號>_adj.csv。
（TaiwanStockPriceAdj 還原價資料集免費帳號不開放，2026-11-04 實測回 400。）

已存在的檔案不會重抓，避免浪費額度；要重抓請先手動移走舊檔。
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.finmindtrade.com/api/v4/data"


def fetch(dataset, stock_id, start, end, token):
    q = urllib.parse.urlencode({"dataset": dataset, "data_id": stock_id, "start_date": start, "end_date": end})
    req = urllib.request.Request(f"{API}?{q}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.load(r)
    if body.get("status") != 200:
        raise RuntimeError(f"{stock_id}: FinMind 回應 {body.get('status')} {body.get('msg')}")
    return body["data"]


def cached_fetch(cache: Path, dataset, stock_id, start, end, token):
    """回應原樣存在 data/cache/finmind/，被限流中斷後重跑只抓缺的部分。"""
    path = cache / f"{dataset}_{stock_id}_{start}_{end}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch(dataset, stock_id, start, end, token)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    time.sleep(1)
    return data


def build_adj(rows, events):
    """回傳還原價列與除權息事件檢核。factor 最後一日為 1，除權息日之前乘上 after/before。"""
    idx = {r["date"]: i for i, r in enumerate(rows)}
    ratio = [1.0] * len(rows)
    checks = []
    for e in events:
        i = idx.get(e["date"])
        if i is None or i == 0 or not e["before_price"]:
            checks.append({"date": e["date"], "status": "SKIPPED_NOT_IN_PRICE"})
            continue
        ratio[i] = e["after_price"] / e["before_price"]
        checks.append({"date": e["date"], "kind": e.get("kind", "dividend"), "before_price": e["before_price"], "after_price": e["after_price"],
                       "prev_close": rows[i - 1]["close"],
                       "status": "OK" if abs(rows[i - 1]["close"] - e["before_price"]) < 1e-6 else "PREV_CLOSE_MISMATCH"})
    f = [1.0] * len(rows)
    for i in range(len(rows) - 1, 0, -1):
        f[i - 1] = f[i] * ratio[i]
    adj = [{**r, **{k: round(r[k] * fi, 6) for k in ("open", "max", "min", "close")}} for r, fi in zip(rows, f)]
    return adj, checks


def write_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stocks", nargs="+")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--cache", default="data/cache/finmind")
    a = ap.parse_args()
    cache = Path(a.cache)
    cache.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("FINMIND_TOKEN", "")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    for sid in a.stocks:
        path = out / f"{sid}.csv"
        if path.exists():
            print(f"略過 {path}（已存在）")
            continue
        rows = cached_fetch(cache, "TaiwanStockPrice", sid, a.start, a.end, token)
        if not rows:
            print(f"{sid}: 無資料", file=sys.stderr)
            continue
        events = cached_fetch(cache, "TaiwanStockDividendResult", sid, a.start, a.end, token)
        reductions = cached_fetch(cache, "TaiwanStockCapitalReductionReferencePrice", sid, a.start, a.end, token)
        events = events + [{"date": r["date"], "before_price": r["ClosingPriceonTheLastTradingDay"],
                            "after_price": r["PostReductionReferencePrice"], "kind": "capital_reduction"}
                           for r in reductions]
        adj, checks = build_adj(rows, events)
        adj_path = out / f"{sid}_adj.csv"
        write_rows(path, rows)
        write_rows(adj_path, adj)
        meta = {"source": "FinMind TaiwanStockPrice + TaiwanStockDividendResult + TaiwanStockCapitalReductionReferencePrice", "api": API, "stock_id": sid,
                "fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "requested_range": [a.start, a.end],
                "actual_range": [rows[0]["date"], rows[-1]["date"]],
                "rows": len(rows), "columns": list(rows[0]),
                "adjustment": "factor 由除權息日 after_price / before_price 回溯連乘",
                "ex_rights_events": checks,
                "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, adj_path)}}
        path.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        bad = [c for c in checks if c["status"] != "OK"]
        print(f"{path}: {len(rows)} 筆 {rows[0]['date']} ~ {rows[-1]['date']}，除權息 {len(checks)} 次，檢核異常 {len(bad)}")


if __name__ == "__main__":
    main()
