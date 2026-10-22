"""端到端回測：讀本機 CSV → 訊號 → 成交 → 損益報告。

用法：
    python3 run_backtest.py data/raw/2330.csv data/raw/2454.csv --out reports/v0
若同目錄有 <名稱>_adj.csv（FinMind 還原股價），自動合併：訊號與停損用還原價、成交用實際價、股利入帳。
輸出：每檔 trades_<名稱>.csv、equity_<名稱>.csv，以及 summary.md（策略 vs 買進持有）。
"""
import argparse
from pathlib import Path

from backtest import engine, metrics, strategy
from backtest.costs import CostModel
from backtest.data import load_csv, load_with_adj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--out", default="reports/v0")
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--fee-discount", type=float, default=1.0)
    ap.add_argument("--ma", type=int, default=20)
    ap.add_argument("--atr", type=int, default=14)
    ap.add_argument("--k", type=float, default=3.0)
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    costs = CostModel(fee_discount=a.fee_discount)
    lines = [f"# 回測摘要 v0", "",
             f"規則：收盤上穿 {a.ma} 日均線進場（隔日開盤成交）；ATR({a.atr})×{a.k} 移動停損。",
             f"本金 {a.capital:,.0f} 元，手續費折扣 {a.fee_discount}，賣出證交稅 0.3%。", ""]
    for path in a.csv:
        name = Path(path).stem
        adj_path = Path(path).with_name(f"{name}_adj.csv")
        df = load_with_adj(path, str(adj_path)) if adj_path.exists() else load_csv(path)
        price_note = "含除權息還原" if "factor" in df else "未還原（無 _adj.csv），除權息跳空可能誤觸停損"
        sig = strategy.ma_cross_atr_trail(strategy.adjusted(df), a.ma, a.atr, a.k)
        res = engine.run(df, sig, a.capital, costs)
        bh = engine.buy_and_hold(df, a.capital, costs)
        res.trades.to_csv(out / f"trades_{name}.csv", index=False)
        res.equity.to_csv(out / f"equity_{name}.csv", index=False)
        rows = {"策略": metrics.summarize(res.trades, res.equity, a.capital),
                "買進持有": metrics.summarize(bh.trades, bh.equity, a.capital)}
        lines += [f"## {name}（{df.date.iloc[0]:%Y-%m-%d} ～ {df.date.iloc[-1]:%Y-%m-%d}，{len(df)} 個交易日，{price_note}）",
                  "", metrics.to_markdown(rows), ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
