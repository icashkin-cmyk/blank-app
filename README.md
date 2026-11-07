# 🧳 旅遊規劃介面

Streamlit 介面：選地區／目的地、交通方式、天數（半日遊、一日遊、二天一夜…五天四夜）、同行者、預算、步調、興趣、餐飲安排、住宿等，產生：

- **順向流程表骨架**：出發 → 去程 → 景點 → 餐飲 → 回程 → 到家，時間為依步調推估，所有列標 `NOT_VERIFIED`。
- **A/B/C 備案**：無票／延誤、店休／排隊、天候／回程受阻。
- **需求單匯出**：可複製的規劃指令、Markdown、JSON 下載。

交通選項依 `DGHP_TRAVEL_RULES.md`（0.1.0-CANDIDATE）：微型電動二輪、電輔車、大眾運輸分開；汽車／機車預設不開放，需勾選「有合法駕駛資格」才出現。

本介面不查班次、不查店家，不代表任何行程已驗證可執行。

## 執行

```
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 測試

```
pip install pytest
python -m pytest -q
```

## 臺股回測引擎 v0

流程：本機日線 CSV → 訊號（第 t 日收盤）→ 第 t+1 日開盤成交 → ATR 移動停損出場 → 損益報告（策略 vs 買進持有）。

```
python3 tools/fetch_twse.py 2330 2454 2317 --start 2015-01   # 或 tools/fetch_finmind.py（需 FINMIND_TOKEN）
python3 run_backtest.py data/raw/2330.csv data/raw/2454.csv data/raw/2317.csv --out reports/v0
python3 -m unittest discover -s tests
```

- 成本：手續費 0.1425%（可用 `--fee-discount` 設折扣，最低 20 元）、賣出證交稅 0.3%。
- 跳空穿越停損以開盤價成交；一價漲停買不到、一價跌停賣單延後；停損自進場隔日起檢查。
- 除權息：同目錄放 `<代號>_adj.csv`（`fetch_twse.py` 會一併產生；FinMind 還原價資料集需付費帳號）即自動啟用，訊號與停損用還原價、成交用實際價、股利現金入帳；配股以等值現金簡化，股利稅與二代健保未計。
- 已知限制：日線無法得知盤中先觸停損或新高。
