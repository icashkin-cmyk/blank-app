"""S1 端到端測試：合成照片（不含任何真實工地資料）→ 既有腳本 → 材料自主檢查照片.xlsx。
執行：PHOTO_SKILL_DIR=<miko>/skills/photo-inspection-log/scripts python3 -m pytest -q photo_web/tests
沒設 PHOTO_SKILL_DIR 時從 GitHub 固定 commit 下載腳本（需要網路）。"""
import io
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
    assert job.items() == ["EMT管"]
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
    res = job.addmany()
    assert "寫入 11 張" in res.stdout, res.stdout
    assert job.stopped() == []
    pend = {r[0]: r[1] for r in job.pending_base()}
    assert set(pend) == {"工程名稱", "監造單位", "承造單位"}
    job.confirm_base(["--all"])
    entries = job.apply_suggestions(job.make_entries())
    assert len(entries) == 11
    assert not any(e["item"].startswith("—") for e in entries)
    assert not any(ln.startswith("（意象：") for e in entries for ln in e["design"])
    job.save_entries(entries)
    job.build()
    res = job.preflight_deliver()
    assert "交檔結果：全部 PASS" in res.stdout
    wb = load_workbook(job.p(pl.OUTPUT))
    assert len(wb.active._images) == 11
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
