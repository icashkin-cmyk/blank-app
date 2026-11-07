"""日線回測引擎（只做多、單一部位、全額投入）。

成交規則：
- 第 t 日收盤後的訊號於第 t+1 日開盤成交，無前視。
- 移動停損價 = max(前一日停損價, 進場後最高收盤價 − trail_dist[t])，只升不降；
  第 t 日算出的停損價於第 t+1 日起生效。
- 停損檢查自進場隔日開始（避免當沖）。開盤 ≤ 停損價時以開盤價成交（跳空穿越），
  否則最低價 ≤ 停損價時以停損價成交。
- 一價到底（開=高=低=收）且漲幅 ≥ 9.5% 視為漲停鎖死，買單不成交並取消；
  跌幅 ≤ −9.5% 視為跌停鎖死，賣單延後到下一個可成交日開盤。
- 回測最後一日仍持有時，以最後收盤價結算並標記 forced_exit。

除權息（df 含 factor 欄 = 還原收盤 / 實際收盤時）：
- 訊號與移動停損以還原價計算（停損內部存還原單位），盤中比較時換算成實際價：
  實際停損價 = 還原停損價 / factor[當日]。除權息日的價格跳空因此不會誤觸停損。
- 除息日持有者入帳每股 前一日實際收盤 × (1 − factor[前一日] / factor[當日])。
  配股也以等值現金入帳（簡化）；股利所得稅與二代健保補充保費未計。
- 減資等使參考價高於前收的事件（factor 下降），持股按 factor[當日] / factor[前一日] 等值縮減
  （可為小數股；退還股款視同按參考價再投入，簡化）。
- 回溯還原會把較早資料整體乘上常數，訊號與停損對整體縮放不變，故不引入前視。
"""
from dataclasses import dataclass, field
import pandas as pd

from .costs import CostModel

LOCK_PCT = 0.095


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.DataFrame
    initial_capital: float
    params: dict = field(default_factory=dict)


def _locked(row, prev_close, direction: int) -> bool:
    one_price = row.open == row.high == row.low == row.close
    if not one_price or prev_close is None or prev_close <= 0:
        return False
    chg = row.close / prev_close - 1
    return chg >= LOCK_PCT if direction > 0 else chg <= -LOCK_PCT


def run(df: pd.DataFrame, signals: pd.DataFrame, capital: float = 1_000_000,
        costs: CostModel = CostModel()) -> Result:
    df = df.reset_index(drop=True)
    factor = df["factor"].to_numpy() if "factor" in df else [1.0] * len(df)
    signals = signals.reset_index(drop=True)
    cash = float(capital)
    shares = 0
    entry = None  # 持倉資訊
    stop = None
    pending_buy = False
    pending_init_stop = None
    pending_sell = None  # 跌停延後賣出的原因
    trades, equity = [], []

    for i, row in enumerate(df.itertuples(index=False)):
        prev_close = df.at[i - 1, "close"] if i > 0 else None
        f = factor[i]

        # 0. 除權息、減資日：持有者入帳股利或等值縮減股數
        if shares > 0 and i > 0 and f > factor[i - 1] * (1 + 1e-9):
            div = shares * prev_close * (1 - factor[i - 1] / f)
            cash += div
            entry["dividends"] += div
        elif shares > 0 and i > 0 and f < factor[i - 1] * (1 - 1e-9):
            shares = shares * f / factor[i - 1]
            entry["shares_after_events"] = shares

        # 1. 開盤：處理前一日留下的買單
        if pending_buy and shares == 0:
            pending_buy = False
            if not _locked(row, prev_close, +1):
                n = costs.max_shares(cash, row.open)
                if n > 0:
                    amt = n * row.open
                    fee = costs.fee(amt)
                    cash -= amt + fee
                    shares = n
                    entry = {"entry_date": row.date, "entry_price": row.open, "shares": n,
                             "buy_fee": fee, "entry_idx": i, "max_close": None, "dividends": 0.0}
                    stop = pending_init_stop

        # 2. 盤中：停損或延後的賣單
        exit_price = exit_reason = None
        stop_raw = stop / f if stop is not None else None
        if shares > 0 and i > entry["entry_idx"]:
            if pending_sell is not None:
                if not _locked(row, prev_close, -1):
                    exit_price, exit_reason = row.open, pending_sell
            elif stop_raw is not None and row.open <= stop_raw:
                if _locked(row, prev_close, -1):
                    pending_sell = "stop_gap_locked"
                else:
                    exit_price, exit_reason = row.open, "stop_gap"
            elif stop_raw is not None and row.low <= stop_raw:
                exit_price, exit_reason = stop_raw, "stop"

        if exit_price is not None:
            amt = shares * exit_price
            fee, tax = costs.fee(amt), costs.tax(amt)
            cash += amt - fee - tax
            trades.append(_trade(entry, row.date, exit_price, exit_reason, fee, tax, shares))
            shares, entry, stop, pending_sell = 0, None, None, None

        # 3. 收盤：更新停損價、產生明日訊號
        if shares > 0:
            adj_close = row.close * f
            entry["max_close"] = adj_close if entry["max_close"] is None else max(entry["max_close"], adj_close)
            dist = signals.at[i, "trail_dist"]
            if pd.notna(dist):
                cand = entry["max_close"] - dist
                stop = cand if stop is None else max(stop, cand)
        elif bool(signals.at[i, "entry"]) and i + 1 < len(df):
            pending_buy = True
            dist = signals.at[i, "trail_dist"]
            # 以訊號日收盤計算初始停損，進場隔日起生效
            pending_init_stop = row.close * f - dist if pd.notna(dist) else None

        equity.append({"date": row.date, "cash": cash, "shares": shares,
                       "close": row.close, "equity": cash + shares * row.close,
                       "stop": stop / f if stop is not None else None})

    if shares > 0:
        last = df.iloc[-1]
        amt = shares * last.close
        fee, tax = costs.fee(amt), costs.tax(amt)
        cash += amt - fee - tax
        trades.append(_trade(entry, last.date, last.close, "forced_exit", fee, tax, shares))
        equity[-1].update({"cash": cash, "shares": 0, "equity": cash})

    cols = ["entry_date", "entry_price", "exit_date", "exit_price", "shares",
            "buy_fee", "sell_fee", "sell_tax", "dividends", "pnl", "return_pct", "exit_reason"]
    return Result(pd.DataFrame(trades, columns=cols), pd.DataFrame(equity), capital)


def _trade(entry, exit_date, exit_price, reason, fee, tax, exit_shares=None):
    cost = entry["shares"] * entry["entry_price"] + entry["buy_fee"]
    dividends = entry.get("dividends", 0.0)
    exit_shares = entry["shares"] if exit_shares is None else exit_shares
    proceeds = exit_shares * exit_price - fee - tax + dividends
    pnl = proceeds - cost
    return {"entry_date": entry["entry_date"], "entry_price": entry["entry_price"],
            "exit_date": exit_date, "exit_price": exit_price, "shares": entry["shares"],
            "buy_fee": entry["buy_fee"], "sell_fee": fee, "sell_tax": tax, "dividends": dividends,
            "pnl": pnl, "return_pct": pnl / cost * 100, "exit_reason": reason}


def buy_and_hold(df: pd.DataFrame, capital: float = 1_000_000, costs: CostModel = CostModel()) -> Result:
    """基準：第一日開盤買進、最後一日收盤賣出；有 factor 欄時股利以現金入帳（不再投入）。"""
    df = df.reset_index(drop=True)
    first = df.iloc[0]
    n = costs.max_shares(capital, first.open)
    buy_fee = costs.fee(n * first.open)
    cash = capital - n * first.open - buy_fee
    if "factor" in df:
        f = df["factor"]
        down = f < f.shift(1) * (1 - 1e-9)
        held = n * (f / f.shift(1)).where(down, 1.0).fillna(1.0).cumprod()  # 減資等值縮股
        per_share = (df["close"].shift(1) * (1 - f.shift(1) / f)).where(f > f.shift(1) * (1 + 1e-9), 0.0).fillna(0.0)
        div_cum = (held.shift(1).fillna(n) * per_share).cumsum()
    else:
        held = pd.Series(float(n), index=df.index)
        div_cum = pd.Series(0.0, index=df.index)
    equity = pd.DataFrame({"date": df["date"], "equity": cash + div_cum + held * df["close"]})
    last = df.iloc[-1]
    amt = held.iloc[-1] * last.close
    fee, tax = costs.fee(amt), costs.tax(amt)
    equity.loc[equity.index[-1], "equity"] = cash + div_cum.iloc[-1] + amt - fee - tax
    entry = {"entry_date": first.date, "entry_price": first.open, "shares": n, "buy_fee": buy_fee,
             "dividends": float(div_cum.iloc[-1])}
    trades = pd.DataFrame([_trade(entry, last.date, last.close, "end", fee, tax, held.iloc[-1])])
    return Result(trades, equity, capital)
