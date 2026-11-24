"""photo_web/app.py｜上傳照片 → 自動讀字草稿、一頁確認（可全選）→ 既有腳本產出「材料自主檢查照片.xlsx」。
執行：streamlit run photo_web/app.py
讀圖（reader.py）依序是固定答案（測試）、Claude API（有金鑰與名單才用）、本機 OCR（預設，不用金鑰）、人工抄寫；規則照 photo-inspection-log SKILL.md。"""
import html
import logging
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as pl  # noqa: E402
import reader as rd  # noqa: E402

st.set_page_config(page_title="材料自主檢查照片", page_icon="📷", layout="wide")
try:
    st.set_option("client.showErrorDetails", "none")   # 例外不顯示訊息與堆疊（False 仍會顯示堆疊；第二輪稽核 F5）
except Exception:
    pass
st.title("材料自主檢查照片產生器")
HOURS = pl.JOB_MAX_AGE_S // 3600
PRIVACY = (f"照片只存在本次工作的暫存資料夾：按左側「結束並刪除暫存」立即刪除；沒按的話，"
           f"閒置超過 {HOURS} 小時後，在下一次有人開啟這個網頁時清除。（手機請點左上角展開側欄。）"
           "有設每日上限時，登入者的 email 與當日張數會記在伺服器，隔天起有人開啟這個網頁時清除。")
LIMIT_NOTE = "處理中請不要重新整理或更換檔案；中斷時沒用到的額度會退回，已送出讀圖的張數仍計入。"
st.caption("上傳查驗照片，自動讀出照片上的字，確認後產出公司格式的「材料自主檢查照片.xlsx」。")


@st.cache_resource(show_spinner="下載 Skill 腳本…")
def scripts_cache() -> str:
    if os.environ.get("PHOTO_SKILL_DIR"):               # 本機腳本：直接用，不走固定 commit 的快取（稽核：設了卻仍用舊快取）
        return os.environ["PHOTO_SKILL_DIR"]
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".skill_cache", pl.SKILL_COMMIT)
    if not all(os.path.exists(os.path.join(d, n)) for n in pl.SKILL_FILES):
        pl.fetch_scripts(d)
    return d


S = st.session_state
S.setdefault("job", None)
S.setdefault("stage", "upload")
S.setdefault("notes", {})
@st.cache_resource(show_spinner="準備自動讀字（第一次要下載模型，約一分鐘）…")
def _reader():
    return rd.make_reader()


@st.cache_resource(show_spinner="準備自動讀字…")
def _ocr_reader():
    return rd.make_ocr_reader()


READER = _reader()
USAGE = pl.Usage()                                    # 側欄的 reset() 會用到，要先定義（第四輪稽核：NameError）
if S.job and not S.job.alive():                       # 工作資料夾已不在（閒置太久被清除）
    S.job, S.stage = None, "upload"
    st.warning("上一次的工作閒置太久，暫存已清除，請重新上傳。")
if S.job and S.stage == "upload":                     # 上一輪收件或讀圖被中斷（處理中改了檔案等）：清掉並退回沒用到的預扣
    S.job.abandon(USAGE); S.job = None
if S.job:
    S.job.touch()
USAGE.prune()                                         # 舊日期的用量（含 email）開頁就清
pl.recover(USAGE)                                     # 別的工作階段中斷留下的預扣（處理中重新整理、斷線）：退回並清掉
pl.sweep()


_SAY = {"error": "background:rgba(255,43,43,.14);border-left:4px solid #ff4b4b;padding:.75rem 1rem;border-radius:.5rem;margin:0 0 1rem",
        "caption": "font-size:.875rem;opacity:.6;margin:0 0 .5rem"}


def say(kind: str, text: str) -> None:
    """含外來文字（匯入的讀圖檔、自動讀到的字、檔名）的提示：跳脫成純文字後用 st.html 顯示，完全不經 Markdown，
    不會變成圖片（瀏覽器對外連線）、連結、粗體、表情符號，也不會被改寫（-> 變箭頭）。"""
    st.html(f'<div style="{_SAY[kind]};overflow-wrap:anywhere">{html.escape(str(text))}</div>')


def show_result(res):
    with st.expander(f"{'✅' if res.ok else '⛔'} {res.name}（結束碼 {res.returncode}）", expanded=not res.ok):
        st.text("$ " + " ".join(res.cmd))
        st.code(res.stdout + (("\n" + res.stderr) if res.stderr else ""), language=None)


def guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except pl.PipelineError as e:
        say("error", str(e))
        if e.result:
            show_result(e.result)
        st.stop()
    except Exception as e:                                # 其他例外：不顯示路徑與堆疊（細節寫進伺服器紀錄）
        logging.exception("photo_web 未預期的錯誤")
        st.error(f"發生未預期的錯誤（{e.__class__.__name__}），這一步沒有完成。請重試，或按左側「結束並刪除暫存」重新開始。")
        st.stop()


def guard_upload(fn, *a, **k):
    """上傳階段用：失敗時不留半成品、退回預扣，並讓「開始」可以再按。"""
    try:
        return fn(*a, **k)
    except pl.PipelineError as e:
        fail_upload(str(e))
    except Exception as e:
        logging.exception("photo_web 未預期的錯誤")
        fail_upload(f"發生未預期的錯誤（{e.__class__.__name__}），沒有收下照片，請重試。")


def reset():
    if S.job:
        S.job.abandon(USAGE)                              # 還沒結清的預扣退回（付費讀圖已送出的張數照算）；已結清的不退
    S.job, S.stage = None, "upload"


def fail_upload(msg: str):
    """上傳階段失敗：清掉半成品、退回預扣，重跑頁面顯示訊息（「開始」按鈕恢復可按）。"""
    S.upload_err = msg
    reset()
    st.rerun()


with st.sidebar:
    st.subheader("進度")
    for key, label in [("upload", "1 上傳照片"), ("review", "2 確認內容"), ("done", "3 下載")]:
        st.write(("▶ " if S.stage == key else "　") + label)
    st.caption(f"腳本版本：{pl.skill_source()}")
    if S.job:
        st.divider()
        if st.button("結束並刪除暫存", type="secondary"):
            reset(); st.rerun()
        with st.expander("執行紀錄"):
            for r in S.job.log:
                st.write(f"{'✅' if r.ok else '⛔'} {r.name}（{r.returncode}）")

# ---------- 使用權限與用量（給外人使用時）----------
RESTRICTED, ALLOWED = pl.parse_allowed(os.environ.get("PHOTO_ALLOWED_EMAILS"))
DAILY, DAILY_ERR = pl.parse_daily(os.environ.get("PHOTO_DAILY_LIMIT"))   # 每人每日張數上限；0＝不限
if DAILY_ERR:
    st.error(DAILY_ERR)
    st.stop()


def email() -> str:
    try:
        if not st.user.is_logged_in or str(st.user.get("email_verified", True)).lower() != "true":   # 沒有這個欄位視為已驗證；None、false 都不算
            return ""
        return (st.user.email or "").lower()
    except Exception:
        return ""


def user_key() -> str:
    """用量的鍵：登入者用 email；沒登入用工作階段代號（不讓所有匿名使用者共用同一個額度）。"""
    if email():
        return email()
    S.setdefault("anon", os.urandom(6).hex())
    return "anon:" + S.anon


if RESTRICTED and not ALLOWED:
    st.error("使用者名單是空的（PHOTO_ALLOWED_EMAILS 有設定但沒有任何 email），目前沒有人可以使用，請管理者修正。")
    st.stop()
if ALLOWED:
    if not email():
        st.info("這個網頁只開放給名單內的人，請先登入。")
        try:
            if st.button("登入", type="primary"):
                st.login()
        except Exception:
            st.error("登入功能尚未設定（管理者需在平台密鑰設定填入 [auth] 登入設定，見 README）。")
        st.stop()
    if email() not in ALLOWED:
        st.error("這個帳號沒有使用權限，請向管理者申請。")
        if st.button("登出"):
            st.logout()
        st.stop()

if READER and READER.name == "Claude API" and not pl.paid_reading_allowed(ALLOWED, DAILY):
    # 會產生費用的讀圖要同時有使用者名單與每日上限才啟用，避免任何人、無上限地用金鑰
    st.warning("已設定 Claude API 金鑰，但沒有同時設定使用者名單（PHOTO_ALLOWED_EMAILS）與每日上限（PHOTO_DAILY_LIMIT），"
               "為避免費用失控，這次改用不需金鑰的自動讀字。")
    READER = _ocr_reader()


def page():
    global READER
    # ---------- 1 上傳 ----------
    if S.stage == "upload":
        st.header("1 上傳照片")
        st.caption("隱私說明：" + PRIVACY
                   + ("照片會送到 Claude API 讀取文字。" if READER and READER.name == "Claude API" else "目前不會送到外部服務。"))
        if DAILY:
            st.caption(f"今日已用 {USAGE.used(user_key())}／{DAILY} 張。" + LIMIT_NOTE)
        photos = st.file_uploader(f"照片（可多張，或 zip；一次 {pl.MAX_PHOTOS} 張以內，zip 裡的也算）", type=["jpg", "jpeg", "png", "heic", "heif", "webp", "zip"],
                                  accept_multiple_files=True)
        prev = st.file_uploader("接續上次：上傳上次的材料自主檢查照片.xlsx（沒有就不用）", type=["xlsx"])
        with st.expander("想要更準：用 AI 對話看圖，把結果貼進來（選用，不用金鑰）"):
            st.markdown("手寫字用自動讀字只有五成左右。做法：把同一批照片丟給 Claude 等 AI 對話，貼上下面第 1 格的那段話；"
                        "再把 AI 回覆的 JSON 整段貼到第 2 格（或存成 .json 檔上傳）。網頁就用它當草稿，裡面沒有的照片仍用自動讀字。"
                        "草稿一樣要你在下一步對照照片看過才寫入。"
                        "注意：照片是你自己交給那個 AI 服務的，會離開這台電腦，請先確認這批照片可以外傳；這個網頁本身不會送出照片。")
            st.caption("1. 複製這段話給 AI（右上角有複製鈕）")
            st.code(rd.IMPORT_PROMPT, language=None)
            readtext = st.text_area("2. 貼上 AI 回覆的 JSON", height=140, max_chars=rd.IMPORT_MAX, key="readtext")
            readfile = st.file_uploader("或上傳 .json 檔", type=["json"])
        if S.get("upload_err"):
            say("error", S.pop("upload_err"))
        go = S.pop("go", False)
        # 按下後這一輪按鈕就變成不能按，處理中不能再按第二次（第四輪稽核：連按會重複預扣）
        st.button("開始", type="primary", disabled=(not photos) or go, on_click=lambda: S.update(go=True))
        if go and photos:
            files = [(p.name, p.getvalue()) for p in photos]
            use = READER
            raw = readtext.strip().encode("utf-8") if readtext.strip() else (readfile.getvalue() if readfile is not None else b"")
            if raw:                                           # 匯入的讀圖結果：先檢查格式，不合就不收件
                try:
                    use = rd.ImportedReader(rd.parse_import(raw), fallback=_ocr_reader())
                except ValueError as e:
                    fail_upload(str(e))
            n_photos = guard_upload(pl.count_photos, files)   # zip 裡的也算（稽核：上限可用 zip 繞過）
            if n_photos > pl.MAX_PHOTOS:
                fail_upload(f"共 {n_photos} 張，超過一次 {pl.MAX_PHOTOS} 張的上限，請分批上傳。")
            S.job = guard_upload(lambda: pl.Job(scripts_src=scripts_cache()))
            # 收件到讀圖完成握著處理鎖；預扣記在工作資料夾，這一段被中斷（重新整理、改檔案）也退得回來（第五輪稽核）
            with S.job.processing():
                if DAILY and not guard_upload(S.job.reserve, USAGE, user_key(), n_photos, DAILY):   # 檢查與累加在同一個鎖裡
                    wait = pl.pending_reserved(user_key())
                    fail_upload(f"超過每日上限 {DAILY} 張，請明天再傳或減少張數。"
                                + (f"（另有 {wait} 張屬於還在處理或剛中斷的上傳，那一批停下後會退回，請稍後再試。）" if wait else ""))
                if not DAILY:
                    S.job.begin()                             # 沒設上限也留「處理中」紀錄，中斷後才清得掉
                bar = st.progress(0.0, text="收件中…")
                guard_upload(S.job.preflight_start)
                if prev:
                    guard_upload(S.job.restore, prev.name, prev.getvalue())
                added = guard_upload(S.job.add_photos, files)
                S.add_note = next((ln for ln in added.stdout.splitlines() if ln.startswith("OK 新增")), "")
                if not S.job.stopped() and not prev:
                    fail_upload("這批照片都是重複的或沒有可處理的照片，沒有新增任何一張。")
                S.how = use.name if use else ""
                if use:
                    S.notes = S.job.auto_read(
                        use, on_photo=lambda i, n: bar.progress(i / max(n, 1), text=f"讀圖中（{use.name}）第 {i + 1}／{n} 張…"))
                    for name in getattr(use, "missed", []):   # 讀圖檔裡沒有的照片：說清楚這張是自動讀字或要人工
                        S.notes.setdefault(name, []).insert(0, "讀圖檔裡沒有這一張" + ("，改用自動讀字" if use.fallback else "，請人工抄寫"))
                bar.progress(1.0, text="完成")                 # 使用者已離開時從這裡停下，不會走到結清
                S.job.settle(USAGE, len(S.job.stopped()))     # 只算實際新增的張數（重複的不算）
            S.stage = "review"; st.rerun()

    job = S.job

    # ---------- 2 確認內容（一頁）----------
    if S.stage == "review":
        st.header("2 確認內容")
        todo = job.stopped()
        how = S.get("how") or "人工抄寫（沒有可用的自動讀字）"
        if how.startswith("匯入的讀圖檔"):
            st.info("草稿來自你匯入的讀圖檔（AI 看圖抄的），不是這個網頁讀的：AI 也會抄錯或漏字，請每張對照照片。"
                    "每張的前兩行「查驗時間：」「照片日期：」要留著。看過後逐張打勾，或用「全選」。")
        elif how.startswith("自動讀字"):
            st.info("照片上的字已自動讀成草稿。印刷字大多讀得到，手寫字常讀錯或讀不到：請對照照片改正，"
                    "看不清的留〔?〕（Excel 會標成待確認）。沒標〔?〕的字也可能讀錯，請每張都對照照片。"
                    "每張的前兩行「查驗時間：」「照片日期：」要留著。看過後逐張打勾，或用「全選」。")
        else:
            st.info("只寫照片上看得到的字；看不清寫〔?〕。前兩行必須是「查驗時間：」「照片日期：」。每張看過後打勾，或用「全選」。")
        say("caption", f"讀字方式：{how}" + (f"｜收件：{S.add_note.removeprefix('OK ')}" if S.get("add_note") else ""))
        if S.get("review_err"):
            say("error", S.pop("review_err"))
        if todo:
            # 固定欄位：整批共用，預設取各張讀到的多數寫法；使用者在這裡確認一次（各張寫法不同時不再逐張擋下）
            st.subheader("工程資料（整批共用，確認一次）")
            opts = job.fixed_options()

            def _fx_changed():                                # 工程資料一改：先前勾的「是同一個工程」不算數，要重新確認
                S.fxver = S.get("fxver", 0) + 1               # 勾選格換新的 key：舊的勾（含瀏覽器端留著的狀態）全部失效（稽核第 3 輪必修 3）
            if S.get("fixed_note"):
                st.warning(S.pop("fixed_note"))
            cols = st.columns(3)
            for c, f in zip(cols, pl.FIXED_FIELDS):
                with c:
                    if f"fx{f}" not in S:
                        S[f"fx{f}"] = opts[f][0][0] if opts[f] else ""
                    st.text_input(f, key=f"fx{f}", max_chars=200, on_change=_fx_changed)
                    if not opts[f]:
                        st.caption("照片上沒讀到這一欄；知道的話可以自己填（會記在這一批），不知道就留空。")
                    elif len(opts[f]) > 1:
                        say("caption", "各張讀到的寫法不同：" + "；".join(f"「{v}」{n} 張" for v, n in opts[f][:4])
                                   + ("…" if len(opts[f]) > 4 else "") + "。請對照照片確認上面這一個。")
                    else:
                        st.caption("自動讀到的字可能有錯，請對照照片。")
            fx = {f: S.get(f"fx{f}", "").strip() for f in pl.FIXED_FIELDS}
            odd = job.fixed_mismatch(fx)                      # 讀到的工程資料和上面差很多的照片：可能是別的工程
            if odd:
                st.error("第 " + "、".join(str(int(n)) for n in sorted(odd)) + " 張讀到的工程資料和上面差很多，可能是別的工程的照片。"
                         "請到該張照片下方選擇：不放這一張，或確認是同一個工程。沒選之前不能產出。")
            st.subheader(f"照片（{len(todo)} 張）")

            def _all():
                for r in job.stopped():
                    S[f"ok{r['nnn']}"] = S.all_ok

            def _one():                                       # 取消任何一張：「全選」跟著取消
                S.all_ok = all(S.get(f"ok{r['nnn']}") for r in job.stopped())
            def _drop(nnn):                                   # 從這一批拿掉一張；其餘已打的勾留著
                try:
                    job.drop(nnn)
                except pl.PipelineError as e:
                    S.review_err = str(e); return
                for k in (f"ok{nnn}", f"t{nnn}", f"form{nnn}", f"fsame{nnn}", f"sug{nnn}"):
                    S.pop(k, None)
                new = job.fixed_defaults()                    # 工程資料依剩下的照片重取：被拿掉那張的值不得留著（稽核第 2 輪必修 1）
                changed = [f for f in pl.FIXED_FIELDS if S.get(f"fx{f}", "") != new[f]]
                for f in pl.FIXED_FIELDS:
                    S[f"fx{f}"] = new[f]
                if changed:
                    _fx_changed()
                    S.fixed_note = (f"已拿掉第 {int(nnn)} 張。上面的工程資料（{'、'.join(changed)}）已依剩下的照片重新帶入，"
                                    "你先前在這幾欄改過的字也一併重設，請再確認一次。")
                _one()
            st.checkbox("全選：下面每一張我都看過了", key="all_ok", on_change=_all)
            sugs = job.suggestions()
            for r in todo:
                nnn = r["nnn"]
                st.markdown(f"**第 {int(nnn)} 張**")
                say("caption", r["原檔名"])
                c1, c2 = st.columns([3, 2])
                with c1:
                    st.image(job.p("photos", f"{nnn}_grid.jpg"), width="stretch")
                with c2:
                    for w in S.notes.get(r["原檔名"], []):
                        say("caption", "⚠ " + w)
                    if nnn in odd:
                        say("error", "這一張讀到的" + "、".join(f"{f}是「{v}」" for f, v in odd[nnn])
                                 + "，和上面確認的不一樣。產出時這一張會記成上面的工程資料。")
                        st.checkbox("是同一個工程（只是讀錯字），照上面的工程資料寫入", key=f"same{nnn}v{S.get('fxver', 0)}")
                    st.text_area("照片文字", value=job.draft(nnn), height=300, key=f"t{nnn}", label_visibility="collapsed",
                                 max_chars=max(pl.MAX_TEXT, len(job.draft(nnn))))
                    if nnn in sugs:                           # 讀圖建議的檢查項目：看得到、可改、可清空，產出時只用這一格的值
                        st.text_input("檢查項目（讀圖建議；照片文字沒有「查驗項目」時才會用，可改、可清空）", value=sugs[nnn],
                                      key=f"sug{nnn}", max_chars=200)
                    if job.forms():                           # 材料表品項：用選的，不靠讀字
                        fo = dict(job.form_options(nnn))
                        st.selectbox("材料表品項（選用）", [-1] + list(fo), key=f"form{nnn}",
                                     format_func=lambda i, fo=fo: "不指定（照上面的字）" if i < 0 else fo[i],
                                     help="選了以後，檢查項目與設計/標準值照公司材料表帶入，不用打字；比較像的排在前面。")
                        if S.get(f"form{nnn}", -1) >= 0:
                            st.checkbox("實測值與材料表相符（實測值也照材料表寫）", key=f"fsame{nnn}")
                    st.checkbox("我看過這一張了", key=f"ok{nnn}", on_change=_one)
                    if len(todo) > 1:
                        st.button("不放這一張（從這一批拿掉）", key=f"drop{nnn}", on_click=_drop, args=(nnn,))
        else:
            st.success("這次沒有新照片要確認，可以直接產出。")
        if st.button("產出 Excel", type="primary"):
            miss = [int(r["nnn"]) for r in todo if not S.get(f"ok{r['nnn']}")]
            if miss:
                st.warning(f"還有 {len(miss)} 張沒打勾（第 {'、'.join(map(str, miss))} 張）。看過後逐張打勾，或勾「全選」。"); st.stop()
            fx = {f: S.get(f"fx{f}", "").strip() for f in pl.FIXED_FIELDS}
            unsure = [int(n) for n in sorted(job.fixed_mismatch(fx)) if not S.get(f"same{n}v{S.get('fxver', 0)}")] if todo else []
            if unsure:
                st.warning(f"第 {'、'.join(map(str, unsure))} 張可能是別的工程的照片：請在該張下方按「不放這一張」，"
                           "或勾「是同一個工程」。（只有一張時，別的工程請按左側「結束並刪除暫存」重新上傳。）"); st.stop()
            for r in todo:
                nnn = r["nnn"]
                job.write_text(nnn, S[f"t{nnn}"], job.fixed_for(nnn, fx))
            if todo:
                res = guard(job.addmany)
                left = job.stopped()
                if left:                                      # 還有寫不進去的：說明原因，留在這一頁改
                    why = [ln for ln in res.stdout.splitlines() if ln.startswith("未寫入：")]
                    S.review_err = (f"有 {len(left)} 張沒有寫入，請照說明修改後再按一次「產出 Excel」：\n\n"
                                    + "\n".join(f"- {pl.sanitize(pl.nth(w), job.dir)}" for w in why))
                    st.rerun()
            if job.pending_base():
                guard(job.confirm_base, ["--all"])            # 使用者已在本頁確認工程資料
            chosen = {r["nnn"]: (S[f"form{r['nnn']}"], bool(S.get(f"fsame{r['nnn']}"))) for r in todo if S.get(f"form{r['nnn']}", -1) >= 0}
            S.entries = job.apply_forms(job.apply_suggestions(guard(job.make_entries), {
                r["nnn"]: S.get(f"sug{r['nnn']}", "") for r in todo}), chosen)
            job.save_entries(S.entries)
            guard(job.build)
            guard(job.preflight_deliver)
            job.pdf = None
            S.stage = "done"; st.rerun()

    # ---------- 3 下載 ----------
    if S.stage == "done":
        st.header("3 下載")
        st.success("Excel 已產出，交檔檢查全部 PASS。")
        ents = S.get("entries") or []
        todo_notes = []
        n_sec = sum(1 for e in ents if not e["sec"]); n_item = sum(1 for e in ents if not e["item"])
        n_q = sum(ln.count("〔?") for e in ents for ln in e["design"] + e["measured"] + [e["item"], e["sec"]])
        if n_sec: todo_notes.append(f"分項工程名稱未填：{n_sec} 筆（白板上沒有這一欄，要填請用下面的「修改欄位」）")
        if n_item: todo_notes.append(f"檢查項目未填：{n_item} 筆")
        if n_q: todo_notes.append(f"待確認的字〔?〕：{n_q} 處（Excel 內以〔?1〕〔?2〕這樣的編號標出）")
        if S.get("how", "").startswith(("自動讀字", "匯入的讀圖檔")):
            todo_notes.append("內容是讀字草稿（自動讀字或匯入的讀圖檔）加上你的確認：沒標〔?〕的字也可能讀錯，交出去前請再對一次照片")
        n_long = len({ln.split("筆", 1)[0] for r in job.log[-2:] for ln in r.stdout.splitlines() if ln.startswith("【超出格子】")})
        if n_long: todo_notes.append(f"內容較長：{n_long} 筆已加高列高完整顯示（沒有截行、沒有縮字），列印前請看一下版面")
        st.download_button("下載 材料自主檢查照片.xlsx", job.output_bytes(), file_name=pl.OUTPUT, type="primary",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if todo_notes:
            st.warning("交檔前請留意：\n\n" + "\n".join(f"- {t}" for t in todo_notes))
        with st.expander("修改欄位後重新產出（選用）"):
            for i, e in enumerate(ents):
                st.markdown(f"**第 {i + 1} 筆**")
                c1, c2 = st.columns([2, 3])
                with c1:
                    if e.get("photo"):
                        st.image(job.p(e["photo"]), width="stretch")
                with c2:
                    st.text_input("分項工程名稱", value=e["sec"], key=f"e{i}sec", max_chars=max(200, len(e["sec"])))
                    st.text_input("檢查項目", value=e["item"], key=f"e{i}item", max_chars=max(200, len(e["item"])))
                    st.text_area("設計/標準值（一行一項）", value="\n".join(e["design"]), key=f"e{i}design", height=150, max_chars=pl.MAX_TEXT)
                    st.text_area("實測值（一行一項）", value="\n".join(e["measured"]), key=f"e{i}measured", height=100, max_chars=pl.MAX_TEXT)
                    st.text_input("棟別/層別/空間編號/空間位置", value=e["bldg"], key=f"e{i}bldg", max_chars=max(200, len(e["bldg"])))
            if st.button("重新產出 Excel"):
                for i, e in enumerate(ents):
                    e["sec"], e["item"], e["bldg"] = S[f"e{i}sec"].strip(), S[f"e{i}item"].strip(), S[f"e{i}bldg"].strip()
                    e["design"] = [x for x in S[f"e{i}design"].splitlines() if x.strip()]
                    e["measured"] = [x for x in S[f"e{i}measured"].splitlines() if x.strip()]
                job.save_entries(ents)
                guard(job.build)
                guard(job.preflight_deliver)
                job.pdf = None
                st.rerun()
        with st.expander("其他下載與檢查紀錄"):
            st.download_button("下載 交檔檢查紀錄.txt", job.check_report().encode("utf-8"), file_name="交檔檢查紀錄.txt", mime="text/plain")
            if st.button("產生 PDF 預覽"):
                job.pdf = job.to_pdf() or b""             # 綁在這個工作上：換工作不會拿到上一份的 PDF
            if getattr(job, "pdf", None):
                st.download_button("下載 PDF 預覽", job.pdf, file_name="材料自主檢查照片.pdf", mime="application/pdf")
            elif getattr(job, "pdf", None) == b"":
                st.warning("這個環境沒有 LibreOffice，無法轉 PDF。")
            for r in job.log[-2:]:
                show_result(r)
        st.caption("下次接續：在第 1 步上傳這次的材料自主檢查照片.xlsx，照片與原文會帶回來；用「修改欄位」改過的內容與選的材料表品項不會帶回，舊照片會回到讀字的內容。")
        if st.button("結束並刪除暫存，處理下一批"):
            reset(); st.rerun()


try:
    page()
except pl.PipelineError as e:
    say("error", str(e))
except Exception as e:  # 未預期的錯誤：不顯示路徑與堆疊（st.stop／st.rerun 不是 Exception，不會被攔）
    logging.exception("photo_web 未預期的錯誤")
    if S.stage == "upload":                               # 清掉半成品、退回預扣，重跑頁面讓「開始」可以再按（第五輪稽核建議）
        fail_upload(f"發生未預期的錯誤（{e.__class__.__name__}），沒有收下照片，請重試。")
    st.error(f"發生未預期的錯誤（{e.__class__.__name__}），這一步沒有完成。請重試，或按左側「結束並刪除暫存」重新開始。")
