"""photo_web/app.py｜S1 最小成品：上傳施工查驗照片 → 逐張填白板文字 → 既有腳本產出「材料自主檢查照片.xlsx」。
執行：streamlit run photo_web/app.py
S1 不接 AI，白板文字由使用者自己抄寫；規則照 photo-inspection-log SKILL.md。"""
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as pl  # noqa: E402
import reader as rd  # noqa: E402

st.set_page_config(page_title="材料自主檢查照片", page_icon="📷", layout="wide")
st.title("材料自主檢查照片產生器（S1 測試版）")
st.caption(f"依 photo-inspection-log 既有腳本產出公司格式 Excel。腳本版本：miko {pl.SKILL_COMMIT[:7]}。"
           "照片只存在本次工作的暫存資料夾，按「結束並刪除暫存」或關閉工作後刪除。")


@st.cache_resource(show_spinner="下載 Skill 腳本…")
def scripts_cache() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".skill_cache", pl.SKILL_COMMIT)
    if not all(os.path.exists(os.path.join(d, n)) for n in pl.SKILL_FILES):
        pl.fetch_scripts(d)
    return d


S = st.session_state
S.setdefault("job", None)
S.setdefault("stage", "upload")
S.setdefault("notes", {})
READER = rd.make_reader()


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


def reset():
    if S.job:
        S.job.cleanup()
    S.job, S.stage = None, "upload"


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
ALLOWED = {e.strip().lower() for e in os.environ.get("PHOTO_ALLOWED_EMAILS", "").split(",") if e.strip()}
DAILY = int(os.environ.get("PHOTO_DAILY_LIMIT", "0") or 0)   # 每人每日張數上限；0＝不限
USAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".usage.json")


def who() -> str:
    try:
        return (st.user.email or "").lower() if getattr(st.user, "is_logged_in", True) else ""
    except Exception:
        return ""


def used_today(email: str) -> int:
    import datetime, json
    try:
        return json.load(open(USAGE)).get(f"{datetime.date.today()}|{email}", 0)
    except Exception:
        return 0


def add_usage(email: str, n: int) -> None:
    import datetime, json
    try:
        d = json.load(open(USAGE))
    except Exception:
        d = {}
    k = f"{datetime.date.today()}|{email}"
    d[k] = d.get(k, 0) + n
    json.dump(d, open(USAGE, "w"))


if ALLOWED and who() not in ALLOWED:
    st.error("這個帳號沒有使用權限，請向管理者申請。")
    st.stop()

# ---------- 1 上傳 ----------
if S.stage == "upload":
    st.header("1 上傳照片")
    st.caption("隱私說明：照片只存在本次工作的暫存資料夾，按「結束並刪除暫存」或下載後刪除；"
               + ("照片會送到 Claude API 讀取文字。" if READER and READER.name == "Claude API" else "目前不會送到外部服務。"))
    if DAILY:
        st.caption(f"今日已用 {used_today(who())}／{DAILY} 張")
    photos = st.file_uploader("施工查驗照片（可多張，或一個 zip；20 張以內）", type=["jpg", "jpeg", "png", "heic", "heif", "webp", "zip"],
                              accept_multiple_files=True)
    prev = st.file_uploader("接續上次：上傳上次的材料自主檢查照片.xlsx（沒有就不用）", type=["xlsx"])
    if st.button("開始", type="primary", disabled=not photos):
        if len([p for p in photos if not p.name.lower().endswith(".zip")]) > 20:
            st.error("超過 20 張，請改傳 zip 或分批。"); st.stop()
        n_photos = len([p for p in photos if not p.name.lower().endswith(".zip")])
        if DAILY and used_today(who()) + n_photos > DAILY:
            st.error(f"超過每日上限 {DAILY} 張，請明天再傳或減少張數。"); st.stop()
        S.job = pl.Job(scripts_src=scripts_cache())
        guard(S.job.preflight_start)
        if prev:
            guard(S.job.restore, prev.name, prev.getvalue())
        guard(S.job.add_photos, [(p.name, p.getvalue()) for p in photos])
        if DAILY:
            add_usage(who(), len(S.job.manifest()))
        if READER:
            with st.spinner(f"讀圖中（{READER.name}）…"):
                S.notes = S.job.auto_read(READER)
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
            draft = job.p("text", f"{nnn}.txt")
            val = open(draft, encoding="utf-8").read().split("\n", 1)[1] if os.path.exists(draft) else job.template(nnn)
            for w in S.notes.get(r["原檔名"], []):
                st.warning(w)
            st.text_area("照片文字（工作檔格式；讀圖草稿請逐字核對）", value=val, height=320, key=f"t{nnn}")
            have = job.fixed(nnn)
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
    st.download_button("下載 材料自主檢查照片.xlsx", job.output_bytes(), file_name=pl.OUTPUT, type="primary",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if st.button("產生 PDF 預覽"):
        pdf = job.to_pdf()
        if pdf:
            st.download_button("下載 PDF 預覽", pdf, file_name="材料自主檢查照片.pdf", mime="application/pdf")
        else:
            st.warning("這個環境沒有 LibreOffice，無法轉 PDF。")
    st.caption("下次接續請上傳這次的材料自主檢查照片.xlsx。")
