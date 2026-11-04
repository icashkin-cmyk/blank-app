"""從證交所 STOCK_DAY（個股日成交資訊）逐月抓日線，落地成 CSV、還原價 CSV 與 meta.json。

用法：
    python3 tools/fetch_twse.py 2330 2454 2317 --start 2015-01 --end 2026-11

- 每月回應原樣快取在 data/cache/twse/<代號>_<年月>.json，中斷後重跑只抓缺的月份；
  當月（未結束）的快取每次重抓。
- 除權息不另抓事件表：證交所的「漲跌價差」在除權息日是相對「除權息參考價」計算，
  所以 參考價 = 收盤 − 漲跌價差。參考價 ≠ 前一日收盤的日子就是除權息日，
  每日調整比 = 參考價 / 前一日收盤，回溯連乘得到 factor，寫成 <代號>_adj.csv（還原開高低收）。
- 只適用上市股票；上櫃股票不在 STOCK_DAY。
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import time
import urllib.request
from pathlib import Path

API = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={ym}01&stockNo={sid}"


def months(start: str, end: str):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y}{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def get_month(sid, ym, cache: Path, sleep: float):
    path = cache / f"{sid}_{ym}.json"
    this_month = dt.date.today().strftime("%Y%m")
    if path.exists() and ym < this_month:
        return json.loads(path.read_text(encoding="utf-8"))
    for attempt in range(4):
        try:
            with urllib.request.urlopen(API.format(ym=ym, sid=sid), timeout=30) as r:
                body = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:  # 被限流或回傳非 JSON 時退避重試
            wait = sleep * (2 ** (attempt + 1))
            print(f"{sid} {ym} 失敗（{e}），{wait:.0f} 秒後重試")
            time.sleep(wait)
    else:
        raise RuntimeError(f"{sid} {ym} 連續失敗，停止；已抓月份保留在快取")
    time.sleep(sleep)
    if body.get("stat") != "OK":
        body = {"stat": body.get("stat"), "data": []}  # 例如尚未上市或無交易的月份
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return body


def num(s: str):
    s = s.replace(",", "").replace("X", "").strip()
    return None if s in ("", "--", "---") else float(s)


def parse(rows):
    out = []
    for r in rows:
        y, m, d = r[0].split("/")
        o, h, l, c = (num(x) for x in r[3:7])
        if c is None:  # 當日無成交
            continue
        out.append({"date": f"{int(y) + 1911}-{m}-{d}", "open": o, "high": h, "low": l, "close": c,
                    "volume": int(num(r[1])), "change": num(r[7])})
    return out


def factors(rows):
    """回溯還原因子：最後一日為 1；除權息日 i 的 factor[i-1] = factor[i] × 參考價 / 前收。"""
    f = [1.0] * len(rows)
    events = []
    for i in range(len(rows) - 1, 0, -1):
        prev, cur = rows[i - 1]["close"], rows[i]
        ratio = 1.0
        if cur["change"] is not None:
            ref = round(cur["close"] - cur["change"], 4)
            if abs(ref - prev) > 1e-6 and 0 < ref < prev:
                ratio = ref / prev
                events.append({"date": cur["date"], "prev_close": prev, "ref_price": ref,
                               "drop": round(prev - ref, 4)})
        f[i - 1] = f[i] * ratio
    return f, sorted(events, key=lambda e: e["date"])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path, rows, cols):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stocks", nargs="+")
    ap.add_argument("--start", default="2015-01")
    ap.add_argument("--end", default=dt.date.today().strftime("%Y-%m"))
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--cache", default="data/cache/twse")
    ap.add_argument("--sleep", type=float, default=3.5)
    a = ap.parse_args()
    out, cache = Path(a.out), Path(a.cache)
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    for sid in a.stocks:
        rows = []
        for ym in months(a.start, a.end):
            rows += parse(get_month(sid, ym, cache, a.sleep)["data"])
        rows.sort(key=lambda r: r["date"])
        f, events = factors(rows)
        adj = [{**r, **{k: round(r[k] * fi, 6) for k in ("open", "high", "low", "close")}}
               for r, fi in zip(rows, f)]
        cols = ["date", "open", "high", "low", "close", "volume"]
        raw_path, adj_path = out / f"{sid}.csv", out / f"{sid}_adj.csv"
        write_csv(raw_path, rows, cols + ["change"])
        write_csv(adj_path, adj, cols)
        meta = {"source": "TWSE STOCK_DAY", "api": API.split("?")[0], "stock_id": sid,
                "fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "requested_range": [a.start, a.end], "actual_range": [rows[0]["date"], rows[-1]["date"]],
                "rows": len(rows), "columns": cols + ["change"],
                "adjustment": "factor 由漲跌價差反推除權息參考價後回溯連乘",
                "ex_rights_events": events,
                "sha256": {raw_path.name: sha256(raw_path), adj_path.name: sha256(adj_path)}}
        (out / f"{sid}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{sid}: {len(rows)} 筆 {rows[0]['date']} ~ {rows[-1]['date']}，除權息 {len(events)} 次")


if __name__ == "__main__":
    main()
