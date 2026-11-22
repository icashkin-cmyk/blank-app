"""photo_web/app.py｜上傳照片 → 讀圖草稿逐張核對 → 既有腳本產出「材料自主檢查照片.xlsx」。
執行：streamlit run photo_web/app.py
讀圖（reader.py）有固定答案、Claude API、人工抄寫三種；規則照 photo-inspection-log SKILL.md。"""
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
           "有設每日上限時，登入者的 email 與當日張數會記到隔天。")
LIMIT_NOTE = "處理中請不要重新整理或更換檔案；中斷時沒用到的額度會退回，已送出讀圖的張數仍計入。"
st.caption(f"依 photo-inspection-log 既有腳本產出公司格式 Excel。腳本版本：{pl.skill_source()}。" + PRIVACY)


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
READER = rd.make_reader()
USAGE = pl.Usage()                                    # 側欄的 reset() 會用到，要先定義（第四輪稽核：NameError）
if S.job and not S.job.alive():                       # 工作資料夾已不在（閒置太久被清除）
    S.job, S.stage = None, "upload"
    st.warning("上一次的工作閒置太久，暫存已清除，請重新上傳。")
if S.job and S.stage == "upload":                     # 上一輪收件或讀圖被中斷（處理中改了檔案等）：清掉並退回沒用到的預扣
    S.job.abandon(USAGE); S.job = None
if S.job:
    S.job.touch()
pl.recover(USAGE)                                     # 別的工作階段中斷留下的預扣（處理中重新整理、斷線）：退回並清掉
pl.sweep()


def show_result(res):
    with st.expander(f"{'✅' if res.ok else '⛔'} {res.name}：{' '.join(res.cmd)}（結束碼 {res.returncode}）", expanded=not res.ok):
        st.code(res.stdout + (("\n" + res.stderr) if res.stderr else ""), language=None)


def guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except pl.PipelineError as e:
        st.error(str(e))
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
    for key, label in [("upload", "1 上傳照片"), ("text", "2 抄寫白板"), ("confirm", "3 確認基準與分項"),
                       ("entries", "4 填欄位"), ("done", "5 產出與下載")]:
        st.write(("▶ " if S.stage == key else "　") + label)
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
               "為避免費用失控，這次改用人工抄寫。")
    READER = None

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
        if S.get("upload_err"):
            st.error(S.pop("upload_err"))
        go = S.pop("go", False)
        # 按下後這一輪按鈕就變成不能按，處理中不能再按第二次（第四輪稽核：連按會重複預扣）
        st.button("開始", type="primary", disabled=(not photos) or go, on_click=lambda: S.update(go=True))
        if go and photos:
            files = [(p.name, p.getvalue()) for p in photos]
            n_photos = guard_upload(pl.count_photos, files)   # zip 裡的也算（稽核：上限可用 zip 繞過）
            if n_photos > pl.MAX_PHOTOS:
                fail_upload(f"共 {n_photos} 張，超過一次 {pl.MAX_PHOTOS} 張的上限，請分批上傳。")
            S.job = guard_upload(lambda: pl.Job(scripts_src=scripts_cache()))
            # 收件到讀圖完成握著處理鎖；預扣記在工作資料夾，這一段被中斷（重新整理、改檔案）也退得回來（第五輪稽核）
            with S.job.processing():
                if DAILY and not guard_upload(S.job.reserve, USAGE, user_key(), n_photos, DAILY):   # 檢查與累加在同一個鎖裡
                    wait = pl.pending_reserved(user_key())
                    fail_upload(f"超過每日上限 {DAILY} 張，請明天再傳或減少張數。"
                                + (f"（另有 {wait} 張屬於還在處理或剛中斷的上傳，約一分鐘內會退回，可稍後再試。）" if wait else ""))
                bar = st.progress(0.0, text="收件中…")
                guard_upload(S.job.preflight_start)
                if prev:
                    guard_upload(S.job.restore, prev.name, prev.getvalue())
                guard_upload(S.job.add_photos, files)
                if not S.job.stopped() and not prev:
                    fail_upload("這批照片都是重複的或沒有可處理的照片，沒有新增任何一張。")
                if READER:
                    S.notes = S.job.auto_read(
                        READER, on_photo=lambda i, n: bar.progress(i / max(n, 1), text=f"讀圖中（{READER.name}）第 {i + 1}／{n} 張…"))
                bar.progress(1.0, text="完成")                 # 使用者已離開時從這裡停下，不會走到結清
                S.job.settle(USAGE, len(S.job.stopped()))     # 只算實際新增的張數（重複的不算）
            S.stage = "text"; st.rerun()

    job = S.job

    # ---------- 2 抄寫 ----------
    if S.stage == "text":
        st.header("2 抄寫白板")
        st.info("只寫這張照片上看得到的字；看不清寫〔?〕；不要從其他照片、記憶或材料表補字。"
                "量測照片先讀刻度或螢幕，再和白板比對。前兩行必須是「查驗時間：」「照片日期：」。"
                "「（意象：…）」是照片內容描述，不是照片上的字。")
        st.caption(f"讀圖方式：{READER.name if READER else '人工抄寫（未設定讀圖）'}")
        show_result(job.log[-1])
        todo = job.stopped()
        if not todo:
            S.stage = "confirm"; st.rerun()
        for r in todo:
            nnn = r["nnn"]
            st.subheader(f"第 {int(nnn)} 張｜{r['原檔名']}｜狀態 {r['狀態']}")
            c1, c2 = st.columns([3, 2])
            with c1:
                st.image(job.p("photos", f"{nnn}_grid.jpg"), use_container_width=True)
            with c2:
                val = job.draft(nnn)                          # 讀圖草稿只在記憶體，勾選後才寫檔
                for w in S.notes.get(r["原檔名"], []):
                    st.warning(w)
                st.text_area("照片文字（工作檔格式；讀圖草稿請逐字核對）", value=val, height=320, key=f"t{nnn}")
                have = job.fixed(nnn) or job.draft_fixed.get(nnn, {})
                with st.expander("固定欄位（照片上有才填）", expanded=bool(have)):
                    for f in pl.FIXED_FIELDS:
                        st.text_input(f, value=have.get(f, ""), key=f"f{nnn}{f}")
                st.checkbox("我已看過這張照片，內容只寫照片上看得到的字", key=f"ok{nnn}")
        if st.button("寫入勾選的照片", type="primary"):
            n = 0
            for r in todo:
                nnn = r["nnn"]
                if S.get(f"ok{nnn}"):
                    job.write_text(nnn, S[f"t{nnn}"], {f: S.get(f"f{nnn}{f}", "") for f in pl.FIXED_FIELDS}); n += 1
            if not n:
                st.warning("沒有勾選任何照片。"); st.stop()
            guard(job.addmany)
            st.rerun()
        st.caption("沒讀完的照片不要勾選；全部寫入後才會進到下一步（交檔檢查會擋下未寫入的照片）。")

    # ---------- 3 確認 ----------
    if S.stage == "confirm":
        st.header("3 確認基準與分項工程名稱")
        show_result(job.log[-1])
        pend = job.pending_base()
        if pend:
            st.subheader("新基準（第一次出現的固定欄位，確認後才寫入基準）")
            for k, v, src in [(r + (None, None))[:3] for r in pend]:
                st.checkbox(f"新基準：{k}＝{v}（來源：{src}）", key=f"b{k}")
        st.subheader("分項工程名稱（白板沒有，請自行填；不知道可留空）")
        items = guard(job.items)
        for it in items:
            st.text_input(f"查驗項目「{it}」的分項工程名稱", key=f"s{it}")
        if st.button("下一步", type="primary"):
            keys = [r[0] for r in pend if S.get(f"b{r[0]}")]
            if keys:
                guard(job.confirm_base, keys)
            for it in items:
                if S.get(f"s{it}", "").strip():
                    guard(job.set_sub, it, S[f"s{it}"].strip())
            S.entries = job.apply_suggestions(guard(job.make_entries))
            S.stage = "entries"; st.rerun()

    # ---------- 4 填欄位 ----------
    if S.stage == "entries":
        st.header("4 填欄位")
        st.info("設計/標準值：對得到材料表就用材料表的品質管理標準（第一行寫材料名稱）；對不到用白板規格。"
                "實測值：白板或量測照片有才寫，沒有就留空（Excel 只留標籤）。")
        for i, e in enumerate(S.entries):
            st.subheader(f"第 {i + 1} 筆｜{e['_sheet']} 第 {e['_row']} 列")
            c1, c2 = st.columns([2, 3])
            with c1:
                if e.get("photo"):
                    st.image(job.p(e["photo"]), use_container_width=True)
            with c2:
                st.text_input("分項工程名稱", value=e["sec"], key=f"e{i}sec")
                st.text_input("檢查項目", value=e["item"], key=f"e{i}item")
                st.text_area("設計/標準值（一行一項）", value="\n".join(e["design"]), key=f"e{i}design", height=150)
                st.text_area("實測值（一行一項）", value="\n".join(e["measured"]), key=f"e{i}measured", height=100)
                st.text_input("棟別/層別/空間編號/空間位置", value=e["bldg"], key=f"e{i}bldg")
        if st.button("產出 Excel", type="primary"):
            for i, e in enumerate(S.entries):
                e["sec"], e["item"], e["bldg"] = S[f"e{i}sec"].strip(), S[f"e{i}item"].strip(), S[f"e{i}bldg"].strip()
                e["design"] = [s for s in S[f"e{i}design"].splitlines() if s.strip()]
                e["measured"] = [s for s in S[f"e{i}measured"].splitlines() if s.strip()]
            job.save_entries(S.entries)
            guard(job.build)
            guard(job.preflight_deliver)
            S.stage = "done"; st.rerun()

    # ---------- 5 產出 ----------
    if S.stage == "done":
        st.header("5 產出與下載")
        for r in job.log[-2:]:
            show_result(r)
        st.success("交檔檢查全部 PASS。")
        ents = S.get("entries") or []
        todo_notes = []
        n_sec = sum(1 for e in ents if not e["sec"]); n_item = sum(1 for e in ents if not e["item"])
        n_q = sum(ln.count("〔?") for e in ents for ln in e["design"] + e["measured"])
        if n_sec: todo_notes.append(f"分項工程名稱未填：{n_sec} 筆")
        if n_item: todo_notes.append(f"檢查項目未填：{n_item} 筆")
        if n_q: todo_notes.append(f"看不清的字〔?〕：{n_q} 處（請對照原照片確認後，下次上傳這份 Excel 接續修改）")
        todo_notes += [ln for r in job.log[-2:] for ln in r.stdout.splitlines() if ln.startswith("【超出格子】")]
        if todo_notes:
            st.warning("交檔前請留意：\n\n" + "\n".join(f"- {t}" for t in todo_notes))
        st.download_button("下載 材料自主檢查照片.xlsx", job.output_bytes(), file_name=pl.OUTPUT, type="primary",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.download_button("下載 交檔檢查紀錄.txt", job.check_report().encode("utf-8"), file_name="交檔檢查紀錄.txt", mime="text/plain")
        if st.button("產生 PDF 預覽"):
            job.pdf = job.to_pdf() or b""             # 綁在這個工作上：換工作不會拿到上一份的 PDF
        if getattr(job, "pdf", None):                     # 按下載後頁面重跑，按鈕還在
            st.download_button("下載 PDF 預覽", job.pdf, file_name="材料自主檢查照片.pdf", mime="application/pdf")
        elif getattr(job, "pdf", None) == b"":
            st.warning("這個環境沒有 LibreOffice，無法轉 PDF。")
        st.caption("下次接續請上傳這次的材料自主檢查照片.xlsx。接續時照片與原文會帶回來；"
               "第 4 步手動改過的欄位不會帶回，需要再確認一次。")


try:
    page()
except pl.PipelineError as e:
    st.error(str(e))
except Exception as e:  # 未預期的錯誤：不顯示路徑與堆疊（st.stop／st.rerun 不是 Exception，不會被攔）
    logging.exception("photo_web 未預期的錯誤")
    if S.stage == "upload":                               # 清掉半成品、退回預扣，重跑頁面讓「開始」可以再按（第五輪稽核建議）
        fail_upload(f"發生未預期的錯誤（{e.__class__.__name__}），沒有收下照片，請重試。")
    st.error(f"發生未預期的錯誤（{e.__class__.__name__}），這一步沒有完成。請重試，或按左側「結束並刪除暫存」重新開始。")
