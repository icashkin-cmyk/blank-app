"""S1 端到端測試：合成照片（不含任何真實工地資料）→ 既有腳本 → 材料自主檢查照片.xlsx。
執行：PHOTO_SKILL_DIR=<miko>/skills/photo-inspection-log/scripts python3 -m pytest -q photo_web/tests
沒設 PHOTO_SKILL_DIR 時從 GitHub 固定 commit 下載腳本（需要網路）。"""
import datetime
import io
import json
import os
import sys

import pytest
from openpyxl import load_workbook
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pipeline as pl  # noqa: E402


def photo(seed: int, when: str) -> bytes:
    """合成一張「白板照」：只有色塊與編號，EXIF 拍攝時間 when（YYYY:MM:DD HH:MM:SS）。"""
    im = Image.new("RGB", (1600, 1200), (240, 240, 235))
    d = ImageDraw.Draw(im)
    d.rectangle([200, 150, 1400, 1050], outline=(20, 20, 20), width=8)
    d.text((260, 200 + seed * 40), f"TEST {seed}", fill=(0, 0, 160))
    ex = Image.Exif()
    ex[0x8769] = {0x9003: when}
    b = io.BytesIO()
    im.save(b, "JPEG", exif=ex.tobytes(), quality=85)
    return b.getvalue()


BODY = "查驗時間：2026-10-27（115.10.27）\n照片日期：2026-10-27\n查驗項目：EMT管材料進場\n位置：〈位置〉\n檢查人員：〈人員〉\n查驗說明（品名 規格｜數量）：\nEMT管 〈規格〉｜〈數量〉\n"


@pytest.fixture()
def job(tmp_path):
    j = pl.Job(root=str(tmp_path))
    yield j
    j.cleanup()


def test_preflight_start_pass(job):
    res = job.preflight_start()
    assert res.ok and "全部 PASS" in res.stdout


def test_end_to_end_four_photos(job):
    job.preflight_start()
    files = [(f"IMG_{i:04d}.JPG", photo(i, f"2026:10:27 09:{i:02d}:00")) for i in range(1, 5)]
    job.add_photos(files)
    rows = job.manifest()
    assert [r["原檔名"] for r in rows] == [n for n, _ in files]
    for i, r in enumerate(rows):
        body = job.template(r["nnn"])
        assert body.startswith("查驗時間：\n照片日期：2026-10-27")
        job.write_text(r["nnn"], BODY, {"工程名稱": "〈工程名稱〉"} if i == 0 else None)
    out = job.addmany()
    assert "寫入 4 張" in out.stdout and "新基準：工程名稱＝〈工程名稱〉（來源：IMG_0001.JPG）" in out.stdout
    assert job.stopped() == []
    assert [r[0] for r in job.pending_base()] == ["工程名稱"]
    job.confirm_base(["--all"])
    assert job.pending_base() == []
    assert job.items() == ["EMT導線管"]  # make_entries v0.5：檢查項目用材料表名稱
    job.set_sub("EMT管材料進場", "〈分項工程〉")
    entries = job.make_entries()
    assert len(entries) == 4 and all(e["sec"] == "〈分項工程〉" for e in entries)
    entries[0]["measured"] = ["〈實測〉"]
    job.save_entries(entries)
    job.build()
    res = job.preflight_deliver()
    assert "交檔結果：全部 PASS" in res.stdout and not res.fail_lines
    wb = load_workbook(job.p(pl.OUTPUT))
    assert len([w for w in wb.worksheets if w.sheet_state == "visible"]) == 1
    assert "原文" in wb.sheetnames and wb["原文"].sheet_state == "hidden"
    assert len(wb.active._images) == 4
    pdf = job.to_pdf()  # 沒有 LibreOffice 的環境回傳 None
    if pdf is not None:
        assert pdf[:4] == b"%PDF"


def test_missing_text_blocks_delivery(job):
    """有一張沒抄寫：manifest 留 todo，交檔檢查必須 FAIL（SKILL.md「沒讀完不寫」）。"""
    job.preflight_start()
    job.add_photos([("A.JPG", photo(1, "2026:10:27 09:01:00")), ("B.JPG", photo(2, "2026:10:27 09:02:00"))])
    first = job.manifest()[0]
    job.write_text(first["nnn"], BODY)
    job.addmany()
    assert [r["原檔名"] for r in job.stopped()] == ["B.JPG"]
    job.make_entries()
    job.build()
    with pytest.raises(pl.PipelineError) as e:
        job.preflight_deliver()
    assert e.value.result.returncode == 0  # preflight 印 FAIL 仍回 0（既有缺陷），本模組靠 FAIL 行擋下
    assert any("manifest 每張照片都已讀並寫入" in ln for ln in e.value.result.fail_lines)


def test_fixed_field_conflict_stops_photo(job):
    """固定欄位與待確認基準不同：該張不寫入（結束碼 2 → 狀態 stop2）。"""
    job.preflight_start()
    job.add_photos([("A.JPG", photo(1, "2026:10:27 09:01:00")), ("B.JPG", photo(2, "2026:10:27 09:02:00"))])
    a, b = job.manifest()
    job.write_text(a["nnn"], BODY, {"承造單位": "〈甲〉"})
    job.write_text(b["nnn"], BODY, {"承造單位": "〈乙〉"})
    out = job.addmany()
    assert "未寫入 1" in out.stdout
    assert [(r["原檔名"], r["狀態"]) for r in job.stopped()] == [("B.JPG", "stop2")]


def test_duplicate_photo_skipped(job):
    job.preflight_start()
    data = photo(1, "2026:10:27 09:01:00")
    job.add_photos([("A.JPG", data), ("A_copy.JPG", data)])
    assert len(job.manifest()) == 1


def test_restore_continues_previous_file(job, tmp_path):
    job.preflight_start()
    job.add_photos([("A.JPG", photo(1, "2026:10:27 09:01:00"))])
    job.write_text(job.manifest()[0]["nnn"], BODY)
    job.addmany(); job.make_entries(); job.build(); job.preflight_deliver()
    prev = job.output_bytes()
    j2 = pl.Job(root=str(tmp_path))
    try:
        j2.preflight_start()
        j2.restore(pl.OUTPUT, prev)
        j2.add_photos([("A.JPG", photo(1, "2026:10:27 09:01:00")), ("C.JPG", photo(3, "2026:10:28 10:00:00"))])
        assert [r["原檔名"] for r in j2.manifest()] == ["C.JPG"]  # A 已在上次檔案，被去重
        j2.write_text(j2.manifest()[0]["nnn"], BODY.replace("2026-10-27（115.10.27）", "2026-10-28（115.10.28）").replace("照片日期：2026-10-27", "照片日期：2026-10-28"))
        j2.addmany()
        assert len(j2.make_entries()) == 2
    finally:
        j2.cleanup()


def test_download_matches_pinned_commit(tmp_path):
    """從 GitHub 固定 commit 下載的腳本與本機 miko 副本 SHA-256 相同（需要網路與 PHOTO_SKILL_DIR）。"""
    local = os.environ.get("PHOTO_SKILL_DIR")
    if not local:
        pytest.skip("沒設 PHOTO_SKILL_DIR")
    os.environ.pop("PHOTO_SKILL_DIR")
    try:
        remote = pl.fetch_scripts(str(tmp_path / "dl"))
    finally:
        os.environ["PHOTO_SKILL_DIR"] = local
    assert remote == {n: pl.sha256_bytes(open(os.path.join(local, n), "rb").read()) for n in pl.SKILL_FILES}


# ---------- 固定驗收輸入：MIKO 提供的 13 張照片（私有專案檔，不進 repo） ----------
FIX = os.environ.get("PHOTO_FIXTURE_DIR")
need_fix = pytest.mark.skipif(not FIX, reason="沒設 PHOTO_FIXTURE_DIR（13 張驗收照片只放私有專案檔）")


@need_fix
def test_fixture13_end_to_end(job):
    import reader as rd
    rdr = rd.FixtureReader(os.path.join(FIX, "answers_13.json"))
    import zipfile
    with zipfile.ZipFile(os.path.join(FIX, "photos13.zip")) as z:  # 用 zip 保存：專案檔資料夾會改動散檔圖片的位元組
        files = [(n, z.read(n)) for n in sorted(z.namelist())]
    assert len(files) == 13
    job.preflight_start()
    out = job.add_photos(files)
    assert "新增 11 張" in out.stdout and "重複略過 2" in out.stdout  # IMG_2124=IMG_2022、IMG_2125=IMG_2023（SHA-256 相同）
    notes = job.auto_read(rdr)
    assert len(notes) == 11
    assert all(not any("與白板相同" in w for w in ws) for ws in notes.values())  # 沒有拿白板值充當讀數
    for r in job.manifest():                                           # 模擬使用者逐張勾選「我已看過」
        job.write_text(r["nnn"], job.draft(r["nnn"]), job.draft_fixed.get(r["nnn"]))
    res = job.addmany()
    assert "寫入 11 張" in res.stdout, res.stdout
    assert job.stopped() == []
    pend = {r[0]: r[1] for r in job.pending_base()}
    assert set(pend) == {"工程名稱", "監造單位", "承造單位"}
    job.confirm_base(["--all"])
    entries = job.apply_suggestions(job.make_entries())
    assert len(entries) == 11
    assert not any(e["item"].startswith("—") for e in entries)
    # 2026-11-22 獨立稽核後的驗收（AC3／AC6／AC7 與稽核問題 1～6）
    alld = [ln for e in entries for ln in e["design"]]
    assert sum(ln.startswith("（意象：") for ln in alld) == 9          # 意象保留在設計/標準值（11 張有 9 張有描述）
    assert not any(ln.startswith("（意象：") for e in entries for ln in e["measured"])
    emt = [e for e in entries if "EMT" in " ".join(e["design"] + [e["item"]])]
    assert len(emt) == 3 and all(e["item"] == "EMT導線管" for e in emt)   # 檢查項目用材料表名稱
    assert all(not any("｜" in ln for ln in e["design"]) and e["measured"] for e in emt)   # 實測欄不併進設計
    assert sum(any("讀數：" in m for m in e["measured"]) for e in emt) == 2   # 兩張量測照讀數進實測值
    assert not any(ln.startswith("（量測讀數：") for ln in alld)
    import re as _re
    for must in ("軟水管龍頭 LF6450N6", "$ 799", r"面盆 L33〔\?\d+〕", r"明鏡 BA18〔\?\d+〕0", r"LF11〔\?\d+〕4P"):  # 不截行、看不清寫〔?N〕
        assert any(_re.search(must.replace("$", r"\$"), ln) for ln in alld), must
    assert any(m.endswith("（管端刻度看不清）") for e in emt for m in e["measured"])
    job.save_entries(entries)
    job.build()
    res = job.preflight_deliver()
    assert "交檔結果：全部 PASS" in res.stdout
    wb = load_workbook(job.p(pl.OUTPUT))
    assert len(wb.active._images) == 11
    from PIL import Image as PI
    import io as _io
    for im in wb.active._images:                                        # 照片不變形：等比補白成照片格比例
        w, h = PI.open(_io.BytesIO(im._data())).size
        assert abs(w / h - 2875930 / 2156190) < 0.02, (w, h)
    open(os.path.join(os.environ.get("PHOTO_OUT_DIR", job.dir), pl.OUTPUT), "wb").write(job.output_bytes())


def test_compose_text_rules():
    """程式組字：民國換西元用程式算；照片沒有的欄位寫「—（照片無此欄）」；意象與讀數分行標示。"""
    import reader as rd
    r = rd.PhotoRead.from_dict({"kind": "量測", "fixed": {}, "inspect_date": "115.11.6", "photo_date": "2026-11-06",
                                "item": "〈項目〉", "lines": ["〈規格〉｜〈實測〉"], "readings": ["捲尺讀數：〔?〕"], "scene": "〈描述〉"})
    t = rd.compose_text(r)
    assert t.splitlines()[:3] == ["查驗時間：2026-11-06（115.11.6）", "照片日期：2026-11-06", "查驗項目：〈項目〉"]
    assert "位置：—（照片無此欄）" in t and "（量測讀數：捲尺讀數：〔?〕）" in t and t.rstrip().endswith("（意象：〈描述〉）")
    r2 = rd.PhotoRead.from_dict({"kind": "標示牌", "fixed": {}, "lines": ["〈品名〉"]})
    assert rd.compose_text(r2).splitlines()[:2] == ["查驗時間：—（照片無此欄）", "照片日期：UNKNOWN（無 EXIF、無浮水印）"]


def test_warning_when_reading_copies_whiteboard():
    import reader as rd
    r = rd.PhotoRead.from_dict({"kind": "量測", "fixed": {}, "lines": ["〈品名〉｜3.67m"], "readings": ["捲尺讀數：3.67m"]})
    assert any("與白板相同" in w for w in rd.warnings(r))


# ---------- 2026-11-23 網頁獨立稽核（W2、W4）必修項目的重現測試 ----------
class _FakeReader:
    name = "假讀圖"

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)

    def read(self, image_path, sha256, original_name):
        import reader as rd
        if original_name in self.fail_on:
            raise RuntimeError("API 壞了 /secret/path")
        return rd.PhotoRead.from_dict({"kind": "白板", "fixed": {}, "inspect_date": "115.10.27", "photo_date": "2026-10-27",
                                       "item": "〈項目〉", "lines": ["〈字〉"]})


def _three(job):
    job.preflight_start()
    job.add_photos([(f"IMG_{i:04d}.JPG", photo(i, f"2026:10:27 09:{i:02d}:00")) for i in range(1, 4)])
    return job.manifest()


def test_unchecked_photos_are_not_written(job):
    """稽核 W2：讀圖草稿在使用者勾選前不得落在 text/NNN.txt；只勾 1 張就只寫入 1 張。"""
    rows = _three(job)
    job.auto_read(_FakeReader())
    assert not any(os.path.exists(job.p("text", f"{r['nnn']}.txt")) for r in rows)
    assert all("〈字〉" in job.draft(r["nnn"]) for r in rows)
    job.write_text(rows[0]["nnn"], job.draft(rows[0]["nnn"]), None)
    job.addmany()
    assert len(job.stopped()) == 2


def test_auto_read_survives_one_failure(job):
    """稽核 W3：一張讀圖失敗不得整批失敗；該張留給人工，訊息不帶內部細節。"""
    rows = _three(job)
    notes = job.auto_read(_FakeReader(fail_on={"IMG_0002.JPG"}))
    assert notes["IMG_0002.JPG"] == ["讀圖失敗，請人工抄寫"]
    assert "〈字〉" in job.draft(rows[0]["nnn"]) and "〈字〉" in job.draft(rows[2]["nnn"])
    assert "〈字〉" not in job.draft(rows[1]["nnn"])


def test_photo_count_includes_zip_and_limit(job):
    """稽核 W4：張數上限要算 zip 裡的照片；超過上限在解壓前就擋下。"""
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for i in range(1, 6):
            z.writestr(f"p{i}.jpg", photo(i, f"2026:10:27 09:{i:02d}:00"))
    files = [("a.zip", b.getvalue()), ("x.jpg", photo(9, "2026:10:27 10:00:00"))]
    assert pl.count_photos(files) == 6
    job.preflight_start()
    with pytest.raises(pl.PipelineError, match="超過"):
        job.add_photos(files, max_photos=5)
    assert job.manifest() == []


def test_non_image_rejected_with_clear_message(job):
    """稽核 W5(b)：副檔名是 jpg 但內容不是圖片，要在收件時擋下並說是哪個檔，不得進到後面才丟 Traceback。"""
    job.preflight_start()
    with pytest.raises(pl.PipelineError, match="fake.jpg"):
        job.add_photos([("fake.jpg", b"this is not an image")])
    assert job.manifest() == []


def test_report_and_messages_hide_server_paths(job, monkeypatch):
    """稽核 W4：交檔檢查紀錄與顯示給使用者的輸出不得出現伺服器路徑。"""
    monkeypatch.setenv("PHOTO_SKILL_DIR", os.environ.get("PHOTO_SKILL_DIR", "/srv/secret/scripts"))
    job.preflight_start()
    rep = job.check_report()
    assert job.dir not in rep and os.environ["PHOTO_SKILL_DIR"] not in rep
    msg = pl.sanitize(f'Traceback\n  File "{job.dir}/scripts/x.py", line 3\n  File "/usr/lib/python3/site-packages/PIL/Image.py"\nValueError: 壞了', job.dir)
    assert job.dir not in msg and "site-packages" not in msg and "ValueError: 壞了" in msg


def test_sweep_removes_old_jobs(tmp_path):
    """稽核 W4：沒按「結束並刪除暫存」的工作資料夾，逾時要自動清掉；還在用的不動。"""
    old, new = pl.Job(root=str(tmp_path)), pl.Job(root=str(tmp_path))
    t = __import__("time").time() - 7200
    os.utime(old.dir, (t, t))
    assert pl.sweep(str(tmp_path), max_age_s=3600) == 1
    assert not os.path.exists(old.dir) and os.path.exists(new.dir)
    new.cleanup()


def test_usage_counts_per_user_and_blocks_anonymous_sharing(tmp_path):
    """稽核 W4：每日用量按使用者分開；沒登入的人不共用同一個額度鍵（改用工作階段代號）。"""
    u = pl.Usage(str(tmp_path / "usage.json"))
    u.add("a@example.com", 3); u.add("b@example.com", 1); u.add("a@example.com", 2)
    assert (u.used("a@example.com"), u.used("b@example.com"), u.used("anon:s1")) == (5, 1, 0)


# ---------- 2026-11-23 網頁第二輪獨立稽核的重現測試 ----------
def test_zip_dot_prefixed_images_are_counted(job):
    """稽核 F4：zip 裡檔名以「.」開頭的圖片，既有腳本 prep 會收（只排除「._」），計數與檢查必須用同一個條件。"""
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("n.jpg", photo(1, "2026:10:27 09:01:00"))
        for i in range(2, 8):
            z.writestr(f"sub/.h{i}.jpg", photo(i, f"2026:10:27 09:{i:02d}:00"))
        z.writestr("__MACOSX/._n.jpg", b"resource fork")          # 這種 prep 不收，也不算
    files = [("dot.zip", b.getvalue())]
    assert pl.count_photos(files) == 7
    job.preflight_start()
    with pytest.raises(pl.PipelineError, match="超過"):
        job.add_photos(files, max_photos=5)
    job.add_photos(files, max_photos=7)
    assert len(job.manifest()) == 7                                # 計數與實際收到的張數一致


def test_huge_pixel_image_rejected(job):
    """稽核建議：像素極大但檔案很小的圖（解壓炸彈）在收件時擋下。"""
    b = io.BytesIO()
    Image.new("L", (12000, 12000), 255).save(b, "PNG")
    job.preflight_start()
    with pytest.raises(pl.PipelineError, match="big.png"):
        job.add_photos([("big.png", b.getvalue())])


def test_sanitize_hides_other_absolute_paths():
    """稽核建議：錯誤最後一行帶其他絕對路徑時也要遮掉。"""
    msg = pl.sanitize("Traceback (most recent call last):\n  File \"/x/y.py\", line 1\nValueError: bad /home/appuser/app/photo_web/pipeline.py")
    assert "/home/appuser" not in msg and "ValueError: bad" in msg


def test_usage_try_add_is_atomic_check(tmp_path):
    """稽核建議：每日上限的檢查與累加要在同一個鎖裡；超過就不加。"""
    u = pl.Usage(str(tmp_path / "usage.json"))
    assert u.try_add("a@example.com", 4, limit=5) is True
    assert u.try_add("a@example.com", 2, limit=5) is False
    assert u.used("a@example.com") == 4
    assert u.try_add("a@example.com", 99, limit=0) is True         # 0＝不限


def test_job_touch_keeps_active_job_from_sweep(tmp_path):
    """稽核建議：使用者停在填欄位很久，只要頁面還在重跑（touch）就不被 sweep 清掉。"""
    j = pl.Job(root=str(tmp_path))
    t = __import__("time").time() - 7200
    os.utime(j.dir, (t, t))
    j.touch()
    assert pl.sweep(str(tmp_path), max_age_s=3600) == 0 and os.path.exists(j.dir)
    j.cleanup()


def test_access_policy():
    """稽核 F3／建議：名單變數有設但解析後是空的要擋下；未驗證的 email 不算；有金鑰時要同時有名單與每日上限才啟用付費讀圖。"""
    assert pl.parse_allowed(None) == (False, set())
    assert pl.parse_allowed(" , ") == (True, set())
    assert pl.parse_allowed("A@Example.com, b@example.com") == (True, {"a@example.com", "b@example.com"})
    assert pl.paid_reading_allowed(allowed={"a@example.com"}, daily=20) is True
    assert pl.paid_reading_allowed(allowed={"a@example.com"}, daily=0) is False
    assert pl.paid_reading_allowed(allowed=set(), daily=20) is False


# ---------- 2026-11-23 網頁第三輪獨立稽核的重現測試 ----------
def test_usage_refund(tmp_path):
    """稽核：收件失敗或照片重複時要把預扣的用量退回，不得白扣。"""
    u = pl.Usage(str(tmp_path / "usage.json"))
    assert u.try_add("a@example.com", 4, limit=5)
    u.refund("a@example.com", 3)
    assert u.used("a@example.com") == 1
    u.refund("a@example.com", 99)
    assert u.used("a@example.com") == 0                            # 不會退成負數


def test_parse_daily_limit():
    """稽核建議：每日上限設成非數字或負數時，回報設定錯誤而不是整頁壞掉或擋住所有人。"""
    assert pl.parse_daily(None) == (0, "") and pl.parse_daily("20") == (20, "")
    assert pl.parse_daily("abc")[1] and pl.parse_daily("-1")[1]


def test_heic_accepted(job):
    """稽核：裝了 pillow-heif 的正式環境，HEIC 不得在收件時被當成「不是圖片」拒收。"""
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    b = io.BytesIO()
    Image.new("RGB", (640, 480), (200, 200, 190)).save(b, "HEIF")
    import subprocess
    code = ("import sys, io; sys.path.insert(0, %r); import pipeline as pl; "
            "pl._check_image('IMG_0001.HEIC', sys.stdin.buffer.read()); print('ok')" % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cp = subprocess.run([sys.executable, "-c", code], input=b.getvalue(), capture_output=True)   # 新行程：不受本測試註冊的影響
    assert cp.stdout.strip() == b"ok", cp.stderr.decode()[-300:]


# ---------- 2026-11-23 網頁第四輪獨立稽核的重現測試 ----------
def test_failed_job_creation_leaves_no_folder(tmp_path):
    """稽核建議：腳本來源缺檔時，建立工作失敗不得留下空殼資料夾。"""
    root, empty = tmp_path / "jobs", tmp_path / "noscripts"
    empty.mkdir()
    with pytest.raises(Exception):
        pl.Job(root=str(root), scripts_src=str(empty))
    assert not root.exists() or os.listdir(root) == []


# ---------- 2026-11-23 網頁第五輪獨立稽核的重現測試：預扣記在工作資料夾，中斷後能退 ----------
def test_reservation_survives_lost_session_and_is_recovered(tmp_path):
    """稽核：處理中重新整理（工作階段消失）時，預扣沒人退，同一人再按就被每日上限擋下。
    預扣要記在工作資料夾；沒有結清、也沒有人在處理的工作，下一次開頁時退回並清掉。"""
    u = pl.Usage(str(tmp_path / "usage.json"))
    j = pl.Job(root=str(tmp_path))
    with j.processing():
        assert j.reserve(u, "a@example.com", 13, limit=20) is True
        assert pl.recover(u, str(tmp_path)) == 0               # 還在處理中：不能動
        assert u.used("a@example.com") == 13
    # 工作階段消失：沒有結清，也沒有人持有處理鎖
    assert pl.recover(u, str(tmp_path)) == 1
    assert u.used("a@example.com") == 0 and not os.path.exists(j.dir)


def test_settle_charges_actual_and_is_not_recovered(tmp_path):
    u = pl.Usage(str(tmp_path / "usage.json"))
    j = pl.Job(root=str(tmp_path))
    with j.processing():
        j.reserve(u, "a@example.com", 13, limit=20)
        j.settle(u, 11)                                        # 13 張含 2 張重複 → 只算 11
    assert u.used("a@example.com") == 11
    assert pl.recover(u, str(tmp_path)) == 0 and os.path.exists(j.dir)   # 已結清的工作不退、不刪
    j.abandon(u)                                               # 結清後按「結束並刪除暫存」：不退用量
    assert u.used("a@example.com") == 11 and not os.path.exists(j.dir)


def test_abandon_before_settle_refunds(tmp_path):
    u = pl.Usage(str(tmp_path / "usage.json"))
    j = pl.Job(root=str(tmp_path))
    j.reserve(u, "a@example.com", 5, limit=20)
    j.abandon(u)
    assert u.used("a@example.com") == 0 and not os.path.exists(j.dir)


def test_reserve_respects_limit(tmp_path):
    u = pl.Usage(str(tmp_path / "usage.json"))
    j = pl.Job(root=str(tmp_path))
    assert j.reserve(u, "a@example.com", 21, limit=20) is False
    assert u.used("a@example.com") == 0
    j.abandon(u)


# ---------- 2026-11-23 第五輪後補：中斷時付費讀圖已花的張數不退；讀圖可被中斷 ----------
class _PaidReader(_FakeReader):
    paid = True


def test_interrupted_paid_reading_keeps_spent_photos_charged(job, tmp_path):
    """處理中被中斷（重新整理、讀圖中改檔案）：已送去付費讀圖的張數照算，其餘退回；
    否則反覆中斷就能不占額度地一直花讀圖費用。"""
    u = pl.Usage(str(tmp_path / "usage.json"))

    class Stop(BaseException):
        pass

    def on_photo(i, n):
        if i == 2:                                             # 讀完 2 張後被中斷
            raise Stop()

    with pytest.raises(Stop):
        with job.processing():
            job.reserve(u, "a@example.com", 3, limit=20)
            _three(job)
            job.auto_read(_PaidReader(), on_photo=on_photo)
    assert pl.recover(u, os.path.dirname(job.dir)) == 1
    assert u.used("a@example.com") == 2


def test_interrupted_free_reading_refunds_everything(job, tmp_path):
    u = pl.Usage(str(tmp_path / "usage.json"))
    job.reserve(u, "a@example.com", 3, limit=20)
    _three(job)
    seen = []
    job.auto_read(_FakeReader(), on_photo=lambda i, n: seen.append((i, n)))
    assert seen == [(0, 3), (1, 3), (2, 3)]                    # 每張讀之前呼叫一次（頁面用來更新進度、讓中斷生效）
    job.abandon(u)
    assert u.used("a@example.com") == 0


def test_refund_happens_only_once(tmp_path):
    """同一筆預扣只能退一次：abandon 之後 recover 不再退。"""
    u = pl.Usage(str(tmp_path / "usage.json"))
    j1, j2 = pl.Job(root=str(tmp_path)), pl.Job(root=str(tmp_path))
    j1.reserve(u, "a@example.com", 5, limit=20)
    j2.reserve(u, "a@example.com", 4, limit=20)
    r = j1._claim()
    assert r and r["n"] == 5 and j1._claim() is None           # 第二次拿不到
    assert pl.pending_reserved("a@example.com", str(tmp_path)) == 4
    j1.cleanup(); j2.abandon(u)


# ---------- 2026-11-23 網頁第六輪獨立稽核 ----------
def test_interrupted_job_without_daily_limit_is_cleaned(tmp_path):
    """稽核 M1：沒設每日上限時，處理中斷的工作資料夾（含照片）也要在下一次開頁時清掉。"""
    u = pl.Usage(str(tmp_path / "usage.json"))
    j = pl.Job(root=str(tmp_path))
    with j.processing():
        j.begin()
        assert pl.recover(u, str(tmp_path)) == 0               # 處理中不動
    assert pl.recover(u, str(tmp_path)) == 1 and not os.path.exists(j.dir)
    assert u._load() == {}                                     # 沒設上限：不記任何人的用量


def test_finished_job_without_daily_limit_is_kept(tmp_path):
    u = pl.Usage(str(tmp_path / "usage.json"))
    j = pl.Job(root=str(tmp_path))
    with j.processing():
        j.begin(); j.settle(u, 3)
    assert pl.recover(u, str(tmp_path)) == 0 and os.path.exists(j.dir) and u._load() == {}
    j.cleanup()


def test_settle_never_charges_less_than_paid_reads(tmp_path):
    """稽核 S1：結清時已付費讀過的張數不能少算。"""
    u = pl.Usage(str(tmp_path / "usage.json"))
    j = pl.Job(root=str(tmp_path))
    j.reserve(u, "a@example.com", 5, limit=20)
    j._spend(); j._spend(); j._spend()
    j.settle(u, 1)
    assert u.used("a@example.com") == 3
    j.cleanup()


def test_prune_drops_old_days(tmp_path):
    """稽核 S3：舊日期的用量（含 email）在開頁時就清掉，不等到有人上傳。"""
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"2000-01-01|old@example.com": 5, f"{datetime.date.today()}|a@example.com": 2}), encoding="utf-8")
    u = pl.Usage(str(path))
    u.prune()
    assert u._load() == {f"{datetime.date.today()}|a@example.com": 2}
