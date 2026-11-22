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
- `ANTHROPIC_API_KEY`（平台密鑰設定，不寫進檔案）：Claude API 視覺讀圖。**尚未實跑（NOT_RUN），費用待決定**。
  只有同時設了 `PHOTO_ALLOWED_EMAILS` 與 `PHOTO_DAILY_LIMIT` 才會啟用，避免任何人、無上限地用到金鑰；一張讀圖失敗只影響那一張，改人工抄寫。
- 都沒有：人工抄寫

讀圖結果只是草稿，放在記憶體；使用者在第 2 步逐張勾選「我已看過」後才寫入。沒勾的照片不會寫入，也到不了下一步。

## 環境變數
| 名稱 | 用途 | 預設 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API 讀圖的金鑰，只填在平台密鑰設定 | 無（人工抄寫） |
| `PHOTO_READER_MODEL` | 讀圖模型 | `claude-opus-5-5` |
| `PHOTO_ALLOWED_EMAILS` | 可使用的 email，逗號分隔；設了就要先登入。有設但內容是空的＝沒有人可用 | 無（不限、不需登入） |
| `PHOTO_DAILY_LIMIT` | 每人每日張數上限（zip 裡的也算） | 0（不限） |
| `PHOTO_SKILL_DIR` | 改用本機的 Skill 腳本資料夾（開發用） | 無（從 GitHub 固定 commit 下載） |
| `PHOTO_FIXTURE` | 固定讀圖答案（驗收用） | 無 |

## 限制
- 一次 20 張以內（`pipeline.MAX_PHOTOS`，zip 裡的也算，篩選條件與 Skill 的 `prep` 相同）、解開後總大小 300MB 以內、單張 8000 萬像素以內；巢狀 zip 不處理；副檔名是圖片但內容打不開的檔會在收件時擋下。
- 同名檔案會互相覆蓋，上傳前請確認檔名不同。
- 接續上次的 xlsx 時，照片與原文會帶回來；第 4 步手動改過的欄位（含讀圖建議的檢查項目）不會帶回，要再確認一次。
- 每日上限：收件失敗或重複的照片不算；`PHOTO_DAILY_LIMIT` 不是數字或是負數時頁面會顯示設定錯誤。未預期的錯誤細節只寫在伺服器紀錄。
- PDF 預覽需要環境裝有 LibreOffice；雲端沒有新細明體、標楷體時，PDF 的欄寬與字型和電腦版 Excel 不同，只供抽看。
- 每日用量先預扣、收件與讀圖完成後結清：預扣記在工作資料夾（`reserve.json`），處理中被中斷（重新整理、斷線、讀圖中更換檔案）時，沒用到的張數在下一次開啟頁面時退回；已送去付費讀圖（Claude API）的張數照算、不退，避免反覆中斷不占額度地花讀圖費用。結清後按「結束並刪除暫存」不退。
- 每日用量記在暫存資料夾，容器重啟會歸零；沒登入的人以工作階段計，重新整理或換瀏覽器就重算，所以只設每日上限、不設使用者名單時，上限擋不住人；要控管費用請設使用者名單。舊日期的用量（含 email）在隔天起有人開啟網頁時清除；工作資料夾裡的預扣紀錄也含 email，隨資料夾清除。
- 沒設每日上限時也會留一筆不含使用者的「處理中」紀錄，處理中斷的工作資料夾（含照片）在下一次有人開啟網頁時清掉。
- Claude API 讀圖每張逾時 120 秒、重試 1 次，仍失敗就改人工抄寫。
- 已知限制：第 2 步照片還在載入時版面會位移，請等照片顯示完再勾選；沒有全站的工作數與磁碟總量上限（部署時請調低 `server.maxUploadSize`）。
- 工作資料夾在 `<暫存>/photo_web_jobs/`：按「結束並刪除暫存」立即刪除；沒按時，閒置超過 2 小時後，在下一次有人開啟頁面時清除（沒有人開頁面就不會清；頁面開著並持續操作的工作不會被清）。

## 部署（Streamlit Community Cloud，尚未實際部署）
1. 在 share.streamlit.io 新增 App：repo `icashkin-cmyk/blank-app`、主檔 `photo_web/app.py`、相依檔 `photo_web/requirements.txt`。
2. 需要 PDF 預覽時，在 repo 根目錄加 `packages.txt`（內容 `libreoffice`）。
3. 在 App 的 Secrets 填環境變數（上表）。要限制使用者時再加 Streamlit 的登入設定：
   ```
   [auth]
   redirect_uri = "https://<你的 App 網址>/oauth2callback"
   cookie_secret = "<自己產生的隨機字串>"
   client_id = "<OIDC client id>"
   client_secret = "<OIDC client secret>"
   server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
   ```
   金鑰與上列密鑰只填在平台的 Secrets，不寫進 repo。
4. 關掉工具列與錯誤細節：Streamlit Community Cloud 沒有設定畫面，要在 repo 根目錄加 `.streamlit/config.toml`，內容是 `[client]` 區段的 `toolbarMode = "viewer"` 與 `showErrorDetails = "none"`（`false` 仍會顯示堆疊）。這個檔會同時影響同一個 repo 的其他 Streamlit 頁面，所以本 repo 目前沒有加，由部署的人決定。程式啟動時也會把錯誤細節設成 `none`，頁面主體另外包了一層攔截，未預期的錯誤只顯示一般訊息。

## 執行與測試
```
pip install -r photo_web/requirements.txt
streamlit run photo_web/app.py
PHOTO_SKILL_DIR=<miko>/skills/photo-inspection-log/scripts python3 -m pytest -q photo_web/tests
```
- 設了 `PHOTO_SKILL_DIR` 時 `test_download_matches_pinned_commit` 會失敗（本機腳本比固定 commit 新），到 `SKILL_COMMIT` 更新為止。
- 目前 `pipeline.SKILL_COMMIT` 固定在舊版腳本；photo-inspection-log v0.23（make_entries v0.5、build_company v0.6、log_photo v0.17）合併進 miko main 後要改成新的 commit。
  在那之前，不設 `PHOTO_SKILL_DIR` 跑測試會有 2 項失敗（預期新版腳本的行為），正式部署也會用到舊版腳本。
- 13 張驗收照片含工地與個人資訊，只放私有專案檔（`PHOTO_FIXTURE_DIR`），不進本公開 repo。
