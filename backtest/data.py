"""讀取本機日線 CSV。回測只讀落地檔案，確保可重現。

必要欄位：date, open, high, low, close, volume（FinMind 的 max/min/Trading_Volume 會自動改名）。
"""
import pandas as pd

RENAME = {"max": "high", "min": "low", "Trading_Volume": "volume"}
REQUIRED = ["date", "open", "high", "low", "close", "volume"]


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path).rename(columns=RENAME)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"{path} 缺少欄位：{missing}")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    if df["date"].duplicated().any():
        raise ValueError(f"{path} 有重複日期")
    # 成交量為 0（停牌或無成交）的日子不能成交，先剔除
    df = df[df["volume"] > 0].reset_index(drop=True)
    return df[REQUIRED]


def load_with_adj(raw_path: str, adj_path: str) -> pd.DataFrame:
    """合併實際價與還原價 CSV，加上 factor = 還原收盤 / 實際收盤。

    兩檔日期必須完全一致，否則報錯，不自行補值。
    """
    raw = load_csv(raw_path)
    adj = load_csv(adj_path)[["date", "close"]].rename(columns={"close": "adj_close"})
    if len(raw) != len(adj) or not raw["date"].reset_index(drop=True).equals(adj["date"].reset_index(drop=True)):
        missing = set(raw["date"]).symmetric_difference(adj["date"])
        raise ValueError(f"實際價與還原價日期不一致，差異 {len(missing)} 天")
    df = raw.merge(adj, on="date", how="inner")
    df["factor"] = df["adj_close"] / df["close"]
    return df.drop(columns="adj_close")
