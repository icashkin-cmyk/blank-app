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
