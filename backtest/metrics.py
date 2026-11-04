"""損益統計：淨利、報酬率、MDD、勝率、獲利因子、交易次數、盈虧比。"""
import math
import pandas as pd


def summarize(trades: pd.DataFrame, equity: pd.DataFrame, capital: float) -> dict:
    eq = equity["equity"]
    peak = eq.cummax()
    mdd = ((eq - peak) / peak).min() * 100 if len(eq) else 0.0
    pnl = trades["pnl"] if len(trades) else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    net = float(eq.iloc[-1] - capital) if len(eq) else 0.0
    return {
        "net_profit": net,
        "return_pct": net / capital * 100,
        "mdd_pct": float(mdd),
        "win_rate_pct": len(wins) / len(pnl) * 100 if len(pnl) else math.nan,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else math.nan),
        "trades": int(len(pnl)),
        "payoff_ratio": (wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else math.nan,
    }


LABELS = {
    "net_profit": "淨利（元）", "return_pct": "報酬率（%）", "mdd_pct": "最大回撤 MDD（%）",
    "win_rate_pct": "勝率（%）", "profit_factor": "獲利因子", "trades": "交易次數", "payoff_ratio": "盈虧比",
}


def to_markdown(rows: dict[str, dict]) -> str:
    """rows: {名稱: summarize 結果}，輸出比較表。"""
    names = list(rows)
    out = ["| 指標 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for k, label in LABELS.items():
        cells = []
        for n in names:
            v = rows[n][k]
            cells.append(str(v) if k == "trades" else f"{v:,.2f}")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(out)
