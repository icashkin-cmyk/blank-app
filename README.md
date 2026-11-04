# 🎈 Blank app template

A simple Streamlit app template for you to modify!

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://blank-app-template.streamlit.app/)

### How to run it on your own machine

1. Install the requirements

   ```
   $ pip install -r requirements.txt
   ```

2. Run the app

   ```
   $ streamlit run streamlit_app.py
   ```

## 臺股回測引擎 v0

流程：本機日線 CSV → 訊號（第 t 日收盤）→ 第 t+1 日開盤成交 → ATR 移動停損出場 → 損益報告（策略 vs 買進持有）。

```
python3 tools/fetch_finmind.py 2330 2454 2317 --start 2015-01-01
python3 run_backtest.py data/raw/2330.csv data/raw/2454.csv data/raw/2317.csv --out reports/v0
python3 -m unittest discover -s tests
```

- 成本：手續費 0.1425%（可用 `--fee-discount` 設折扣，最低 20 元）、賣出證交稅 0.3%。
- 跳空穿越停損以開盤價成交；一價漲停買不到、一價跌停賣單延後；停損自進場隔日起檢查。
- 除權息：同目錄放 `<代號>_adj.csv`（`fetch_finmind.py --adj`）即自動啟用，訊號與停損用還原價、成交用實際價、股利現金入帳；配股以等值現金簡化，股利稅與二代健保未計。
- 已知限制：日線無法得知盤中先觸停損或新高。
