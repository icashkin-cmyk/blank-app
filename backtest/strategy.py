"""策略：只用第 t 日（含）以前的資料，輸出第 t 日收盤後的訊號。

輸出欄位：
- entry：第 t 日收盤後是否發出進場訊號（第 t+1 日開盤成交）。
- trail_dist：第 t 日的移動停損距離（參考高點減去此值即停損價）。
"""
import pandas as pd


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def ma_cross_atr_trail(df: pd.DataFrame, ma_n: int = 20, atr_n: int = 14, atr_k: float = 3.0) -> pd.DataFrame:
    """基準策略 v0：收盤由下往上穿越 N 日均線進場；ATR×k 移動停損出場。"""
    ma = df["close"].rolling(ma_n, min_periods=ma_n).mean()
    above = df["close"] > ma
    entry = above & ~above.shift(1, fill_value=True) & ma.notna() & ma.shift(1).notna()
    return pd.DataFrame({"entry": entry.fillna(False), "trail_dist": atr(df, atr_n) * atr_k}, index=df.index)


def adjusted(df: pd.DataFrame) -> pd.DataFrame:
    """把開高低收換算成還原價（乘上 factor）；沒有 factor 欄時原樣回傳。策略應在此結果上計算。"""
    if "factor" not in df:
        return df
    out = df.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = df[c] * df["factor"]
    return out
