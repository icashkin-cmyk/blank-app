"""從 FinMind 抓臺股日線，落地成 CSV 並寫 meta.json（來源、抓取日期、區間、欄位）。

用法（可在 Mac 或已開放 api.finmindtrade.com 的環境執行）：
    export FINMIND_TOKEN=你的token   # 可省略，省略時額度較低
    python3 tools/fetch_finmind.py 2330 2454 2317 --start 2015-01-01 --end 2026-10-19
    python3 tools/fetch_finmind.py 2330 --adj          # 改抓還原股價

已存在的檔案不會重抓，避免浪費額度；要重抓請先手動移走舊檔。
資料集名稱 TaiwanStockPrice / TaiwanStockPriceAdj 依 FinMind v4 文件，本環境尚未實際連線驗證。
"""
import argparse
import csv
import datetime as dt
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stocks", nargs="+")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--adj", action="store_true", help="抓還原股價 TaiwanStockPriceAdj")
    ap.add_argument("--out", default="data/raw")
    a = ap.parse_args()
    dataset = "TaiwanStockPriceAdj" if a.adj else "TaiwanStockPrice"
    token = os.environ.get("FINMIND_TOKEN", "")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    suffix = "_adj" if a.adj else ""

    for sid in a.stocks:
        path = out / f"{sid}{suffix}.csv"
        if path.exists():
            print(f"略過 {path}（已存在）")
            continue
        rows = fetch(dataset, sid, a.start, a.end, token)
        if not rows:
            print(f"{sid}: 無資料", file=sys.stderr)
            continue
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        meta = {"source": f"FinMind {dataset}", "api": API, "stock_id": sid,
                "fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "requested_range": [a.start, a.end],
                "actual_range": [rows[0]["date"], rows[-1]["date"]],
                "rows": len(rows), "columns": list(rows[0])}
        path.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{path}: {len(rows)} 筆 {rows[0]['date']} ~ {rows[-1]['date']}")
        time.sleep(1)


if __name__ == "__main__":
    main()
