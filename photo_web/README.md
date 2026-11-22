# photo_web｜材料自主檢查照片產生器

上傳施工查驗照片（或任何照片），依 photo-inspection-log Skill 規則產出公司格式「材料自主檢查照片.xlsx」。
網頁只負責收件、呼叫、確認、交件；排版與名詞只由 Skill 的既有腳本與範本決定，腳本在執行時從
icashkin-cmyk/miko 固定 commit（`pipeline.SKILL_COMMIT`）下載，不在本 repo 複製一份。

## 流程（對應 SKILL.md「二、流程」）
1. 上傳照片（20 張以內或 zip；可附上次的 xlsx 接續）→ `preflight start`、`restore`、`prep`（SHA-256 去重）
2. 讀圖：任何照片先抓照片上的字；沒有字或字不足時寫「（意象：…）」描述。原字與描述分開標示。
   使用者逐張核對後勾選寫入（`addmany`，來源行由程式依 manifest 產生）
3. 確認新基準（工程名稱、監造、承造第一次出現時）與分項工程名稱（`base`、`sub`）
4. 填欄位：`make_entries` 後，照片沒有查驗項目欄時用照片上的品名當建議；量測讀數搬到實測值
5. `build_company` → `preflight deliver`（印 FAIL 也回 0 的既有缺陷由本模組攔下）→ 下載

## 讀圖方式（`reader.make_reader()`）
- `PHOTO_FIXTURE=<answers.json>`：固定答案（驗收用，依照片 SHA-256 對應）
- `ANTHROPIC_API_KEY`（平台密鑰設定，不寫進檔案）：Claude API 視覺讀圖。**尚未實跑，費用待決定**
- 都沒有：人工抄寫

## 執行與測試
```
pip install -r photo_web/requirements.txt
streamlit run photo_web/app.py
PHOTO_SKILL_DIR=<miko>/skills/photo-inspection-log/scripts python3 -m pytest -q photo_web/tests
```
13 張驗收照片含工地與個人資訊，只放私有專案檔（`PHOTO_FIXTURE_DIR`），不進本公開 repo。
