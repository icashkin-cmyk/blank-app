"""photo_web/tests/test_ocr_reader.py｜不用金鑰的自動讀字：規則與反向測試（用假的 OCR 格子，不需要模型）。
執行：python3 -m pytest -q photo_web/tests/test_ocr_reader.py
實際跑模型的測試要設 PHOTO_OCR_DIR（一個放照片的資料夾，照片不進 repo），沒設就略過。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ocr_reader as oc  # noqa: E402
import pipeline as pl  # noqa: E402
import reader as rd  # noqa: E402

W, H = 1477, 1108


def B(x0, y0, x1, y1, text, score=0.99):
    return oc.Box(x0, y0, x1, y1, text, score)


def board(extra=(), date="115.11.23", date_score=0.95):
    """一塊標準白板：三個印刷標籤、手寫內容、右下角浮水印。"""
    return [
        B(200, 60, 880, 95, "工程名稱：〈工程甲〉"),
        B(200, 110, 1080, 145, "監造單位：〈監造乙〉/承攬廠商：〈承造丙〉"),
        B(200, 160, 390, 195, "查驗項目："), B(400, 158, 720, 198, "〈項目丁〉材料進場", 0.95), B(900, 160, 1000, 195, "數量", 0.95),
        B(210, 230, 300, 262, "廠牌", 0.95), B(205, 290, 300, 325, "〈牌〉", 0.97),
        B(360, 230, 800, 265, "AB-123 〈品名一〉", 0.96), B(930, 232, 990, 264, "22", 0.99),
        B(360, 290, 800, 325, "CD-456 〈品名二〉", 0.93), B(930, 292, 990, 324, "5", 0.98),
        B(200, 560, 380, 595, "檢查人員："), B(400, 550, 640, 590, "〈人名〉", 0.94),
        B(700, 560, 850, 595, "查驗日期："), B(860, 556, 1040, 592, date, date_score),
        B(1234, 1056, 1474, 1090, "2026-11-23", 1.0),
        *extra,
    ]


def test_board_fields_and_rows():
    r = oc.compose(board(), W, H)
    assert r.kind == "白板"
    assert r.fixed == {"工程名稱": "〈工程甲〉", "監造單位": "〈監造乙〉", "承造單位": "〈承造丙〉"}
    assert r.item == "〈項目丁〉材料進場" and r.inspector == "〈人名〉"
    assert r.inspect_date == "115.11.23" and r.photo_date == "2026-11-23"
    assert r.head == "品名規格｜數量"
    assert r.lines[:2] == ["AB-123 〈品名一〉｜22", "CD-456 〈品名二〉｜5"]
    assert "廠牌：〈牌〉" in r.lines                              # 廠牌取標籤正下方那一格，不取右邊的品名
    assert r.scene == "" and r.suggest_item == "" and r.readings == []   # OCR 不描述、不推定、不產生讀數
    text = rd.compose_text(r)
    assert text.splitlines()[0] == "查驗時間：2026-11-23（115.11.23）" and text.splitlines()[1] == "照片日期：2026-11-23"


def test_low_confidence_is_marked_and_very_low_is_not_guessed():
    """反向：看不清的字不得被當成事實寫入。信心偏低加〔?〕；很低只留〔?〕，連猜的字都不留。"""
    bx = board()
    bx[7] = B(360, 230, 800, 265, "AB-123 亂猜的字", 0.70)
    bx[9] = B(360, 290, 800, 325, "完全亂碼", 0.30)
    r = oc.compose(bx, W, H)
    assert r.lines[0] == "AB-123 亂猜的字〔?〕｜22"
    assert r.lines[1] == "〔?〕｜5" and "完全亂碼" not in rd.compose_text(r)
    assert any("待確認" in w for w in rd.warnings(r))


def test_blurred_fixed_field_is_marked():
    bx = board()
    bx[0] = B(200, 60, 880, 95, "工程名稱：〈工程甲〉糊掉", 0.72)
    r = oc.compose(bx, W, H)
    assert r.fixed["工程名稱"].endswith("〔?〕")


def test_missing_fields_stay_empty_not_filled():
    """反向：照片上沒有的欄位留空，不從別處補（同一個 reader 連讀兩張，第二張不得帶到第一張的值）。"""
    full = oc.compose(board(), W, H)
    assert full.fixed["承造單位"]
    part = oc.compose([b for b in board() if "監造" not in b.text and "檢查人員" not in b.text and "〈人名〉" not in b.text], W, H)
    assert part.fixed["監造單位"] == "" and part.fixed["承造單位"] == "" and part.inspector == ""
    assert "檢查人員：" + rd.NONE_FIELD in rd.compose_text(part)


def test_non_board_photo_keeps_text_only():
    """反向：不是白板的照片（價格牌、銘牌）不硬套欄位，只照抄看得到的字。"""
    r = oc.compose([B(100, 100, 600, 150, "〈商品名〉"), B(100, 200, 300, 250, "$ 799"), B(100, 300, 500, 340, "看不清的小字", 0.6)], W, H)
    assert r.kind == "其他" and r.item == "" and r.fixed == {"工程名稱": "", "監造單位": "", "承造單位": ""}
    assert r.lines == ["〈商品名〉", "$ 799", "看不清的小字〔?〕"]


def test_photo_without_any_text():
    r = oc.compose([], W, H)
    assert r.kind == "其他" and r.lines == [] and r.item == ""
    assert rd.compose_text(r).startswith("查驗時間：" + rd.NONE_FIELD + "\n照片日期：UNKNOWN")
    assert rd.warnings(r) == []                                  # 沒有字也沒有描述：草稿是空白範本，由使用者填


def test_text_outside_board_goes_to_side_note():
    extra = [B(1150, 300, 1400, 340, "紙箱型號 XY-1", 0.97), B(300, 900, 700, 950, "DANCELIGHT", 0.98), B(1150, 400, 1300, 430, "糊字", 0.4)]
    r = oc.compose(board(extra), W, H)
    note = [ln for ln in r.lines if ln.startswith("（旁註：白板以外的字：")]
    assert len(note) == 1 and "紙箱型號 XY-1" in note[0] and "DANCELIGHT" in note[0] and "糊字" not in note[0]
    assert not any("紙箱型號" in ln for ln in r.lines if not ln.startswith("（旁註"))


def test_date_that_disagrees_with_watermark_is_flagged_not_rewritten():
    """反向：日期讀成別的年份時不自行改成浮水印的日期，只標待確認。"""
    r = oc.compose(board(date="215.11.23"), W, H)
    assert r.inspect_date == "215.11.23〔?〕"
    assert rd.compose_text(r).startswith("查驗時間：UNKNOWN（照片寫「215.11.23〔?〕」")
    r = oc.compose(board(date="115.11 2-3"), W, H)
    assert r.inspect_date == "115.11 2-3〔?〕"


def test_simplified_converted_but_tai_kept():
    assert oc.mark("台南市 检查项目 厂牌 灯", 0.99) in ("台南市 檢查項目 廠牌 燈", "台南市 检查项目 厂牌 灯")   # 沒裝 opencc 時原樣
    assert oc.mark("台南", 0.99) == "台南" and oc.mark("臺南", 0.99) == "臺南"            # 照片印「台」就寫「台」


def test_tilted_board_rows_do_not_merge():
    """斜拿的白板：右邊的數量比左邊的品名低，扶正後仍在同一列，上下兩列不黏在一起。"""
    k = 0.06
    def q(x0, y0, x1, y1):
        return [(x0, y0 + k * x0), (x1, y0 + k * x1), (x1, y1 + k * x1), (x0, y1 + k * x0)]
    quads = [(q(200, 60, 880, 95), "工程名稱：〈工程甲〉", 0.99), (q(200, 110, 880, 145), "監造單位：〈監造乙〉", 0.99),
             (q(200, 160, 390, 195), "查驗項目：", 0.99), (q(400, 160, 720, 195), "〈項目丁〉", 0.95),
             (q(360, 230, 800, 265), "〈品名一〉", 0.96), (q(930, 230, 990, 265), "7", 0.99),
             (q(360, 295, 800, 330), "〈品名二〉", 0.96), (q(930, 295, 990, 330), "8", 0.99),
             (q(200, 560, 380, 595), "檢查人員：", 0.99), (q(700, 560, 850, 595), "查驗日期：", 0.99), (q(860, 560, 1040, 595), "115.11.23", 0.95)]
    r = oc.compose(oc.boxes_from_quads(quads), W, H)
    assert r.lines == ["〈品名一〉｜7", "〈品名二〉｜8"]


def test_label_word_inside_content_is_not_a_label():
    bx = board([B(360, 350, 800, 385, "出廠日期 2026/10", 0.96)])
    r = oc.compose(bx, W, H)
    assert r.inspect_date == "115.11.23" and "出廠日期 2026/10" in r.lines


# ---------- 網頁流程：整批共用的工程資料 ----------
def _job_with(tmp_path, fixed_by_nnn):
    j = pl.Job(root=str(tmp_path))
    j.draft_fixed = fixed_by_nnn
    j.stopped = lambda: [{"nnn": n} for n in fixed_by_nnn]
    return j


def test_fixed_options_majority_and_unclear_last(tmp_path):
    j = _job_with(tmp_path, {"001": {"工程名稱": "〈甲〉"}, "002": {"工程名稱": "〈甲〉"}, "003": {"工程名稱": "〈甲錯字〉"},
                             "004": {"工程名稱": "〈糊〉〔?〕"}, "005": {"工程名稱": "〈糊〉〔?〕"}, "006": {"工程名稱": "〈糊〉〔?〕"}})
    o = j.fixed_options()
    assert o["工程名稱"][0] == ("〈甲〉", 2) and o["工程名稱"][-1][0].endswith("〔?〕")   # 沒讀清楚的寫法張數再多也不當預設
    assert o["監造單位"] == []
    j.cleanup()


def test_fixed_for_only_photos_that_show_the_field(tmp_path):
    """反向：照片上讀不到工程名稱的那一張（例如價格牌）不帶入工程名稱。"""
    j = _job_with(tmp_path, {"001": {"工程名稱": "〈甲錯字〉", "監造單位": ""}, "002": {}})
    ok = {"工程名稱": "〈甲〉", "監造單位": "〈乙〉", "承造單位": ""}
    assert j.fixed_for("001", ok) == {"工程名稱": "〈甲〉", "監造單位": "〈乙〉"}
    assert j.fixed_for("002", ok) == {"監造單位": "〈乙〉"}          # 整批都沒讀到、使用者補填的欄位每張都寫（稽核第 1 輪必修 3）；工程名稱仍不帶入
    j.draft_fixed = {}                                           # 人工抄寫：沒有讀圖草稿，使用者填的值每張都用
    assert j.fixed_for("002", ok) == ok
    j.cleanup()


def test_make_reader_falls_back(monkeypatch):
    monkeypatch.delenv("PHOTO_FIXTURE", raising=False); monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PHOTO_OCR", "0")
    assert rd.make_reader() is None                              # 關掉 OCR：人工抄寫
    monkeypatch.setenv("PHOTO_OCR", "1")
    monkeypatch.setattr(oc, "OcrReader", lambda: (_ for _ in ()).throw(RuntimeError("模型下載失敗 /secret")))
    assert rd.make_reader() is None                              # OCR 起不來：不讓整頁壞掉，改人工抄寫


# ---------- 實際跑模型（照片不進 repo）----------
OCR_DIR = os.environ.get("PHOTO_OCR_DIR", "")


@pytest.mark.skipif(not os.path.isdir(OCR_DIR), reason="PHOTO_OCR_DIR 沒設")
def test_real_photos_end_to_end(tmp_path):
    """真實照片：上傳 → OCR 草稿 → 全部確認 → 產出 → 交檔檢查全 PASS；每張草稿前兩行格式正確。"""
    from openpyxl import load_workbook
    j = pl.Job(root=str(tmp_path))
    j.preflight_start()
    files = [(n, open(os.path.join(OCR_DIR, n), "rb").read()) for n in sorted(os.listdir(OCR_DIR))]
    j.add_photos(files)
    n = len(j.stopped())
    notes = j.auto_read(oc.OcrReader())
    assert len(notes) == n and not any("讀圖失敗" in " ".join(v) for v in notes.values())
    fx = {f: (o[0][0] if o else "") for f, o in j.fixed_options().items()}
    assert j.fixed_mismatch(fx) == {}                            # 稽核第 3 輪必修 2：同一工程的照片不得被指成別的工程
    for r in j.stopped():
        t = j.draft(r["nnn"]).splitlines()
        assert t[0].startswith("查驗時間：") and t[1].startswith("照片日期：")
        j.write_text(r["nnn"], j.draft(r["nnn"]), j.fixed_for(r["nnn"], fx))
    res = j.addmany()
    assert j.stopped() == [], res.stdout                         # 各張工程名稱讀法不同也不會卡住（「沒有輸出」的原因）
    if j.pending_base():
        j.confirm_base(["--all"])
    ents = j.apply_suggestions(j.make_entries())
    assert len(ents) == n
    j.save_entries(ents); j.build()
    assert "交檔結果：全部 PASS" in j.preflight_deliver().stdout
    assert len(load_workbook(j.p(pl.OUTPUT)).active._images) == n
    j.cleanup()


def test_empty_label_does_not_take_next_label_as_value():
    """稽核第 1 輪建議 1：檢查人員空白時，不得把右邊的「查驗日期：115.11.23」當成檢查人員。"""
    bx = [b for b in board() if b.text not in ("〈人名〉", "查驗日期：", "115.11.23")]
    bx.append(B(700, 560, 1040, 595, "查驗日期：115.11.23", 0.97))
    r = oc.compose(bx, W, H)
    assert r.inspector == "" and r.inspect_date == "115.11.23"


def test_simplified_words_become_traditional():
    """MIKO 2026-11-23 回報「還有簡體字」：只會是簡體的字都要轉；型號、數字、〔?〕不動。"""
    pytest.importorskip("opencc")
    assert oc.to_trad("检查项目 材料进场 厂牌 灯具 发电机 配电柜 开关 电线 数量 规格 型号 长度 镀锌钢管 灭火器 紧急广播 3个 气体 网路") == \
        "檢查項目 材料進場 廠牌 燈具 發電機 配電柜 開關 電線 數量 規格 型號 長度 鍍鋅鋼管 滅火器 緊急廣播 3個 氣體 網路"
    assert oc.to_trad("台南 台电 3台 面板 系统 松木 表面") == "台南 台電 3台 面板 系統 松木 表面"   # 「台」不改成「臺」；本來就對的字不動
    assert oc.to_trad("AB-123 25W 5.5mm²〔?〕") == "AB-123 25W 5.5mm²〔?〕"
    assert oc.to_trad("") == ""


def test_traditional_words_are_never_rewritten():
    """稽核第 3 輪必修 1：本來就正確的繁體字（含材料表用字）不得被改寫；有簡體字的詞才轉。"""
    pytest.importorskip("opencc")
    keep = ["掛式便斗U4360F", "游標卡尺", "液面控制器", "干擾", "里長", "范先生", "余先生", "于先生", "污排水管材料進場", "沉水式污水泵",
            "水表", "公厘", "面板", "系統", "松木", "表面", "乾燥", "公里", "制水閥", "範圍", "斗", "后", "里", "干", "余", "范", "云", "几",
            "茶几", "控制箱", "台電", "PVC管 4\"(100mm)", "E19 符合CNS2606"]
    assert [w for w in keep if oc.to_trad(w) != w] == []
    # 稽核第 4 輪：跨詞、人名、姓氏後面接簡體字，都不得改到繁體也在用的字；只轉只會是簡體的字
    names = ["管制作業", "交通管制作業", "控制作動", "管制品", "公里面積", "皇后面前", "王志杰", "林志杰", "胡适", "柜台", "李宁", "种師道", "杰出"]
    assert [w for w in names if oc.to_trad(w) != w] == []
    assert oc.to_trad("范国昌建築師事務所") == "范國昌建築師事務所" and oc.to_trad("余国华") == "余國華"
    assert oc.to_trad("于国栋") == "于國棟" and oc.to_trad("涂庆文") == "涂慶文"
    assert oc.to_trad("规范 这里 后面 制造") == "規范 這里 后面 制造"          # 已揭露的限制：這類字留著，由使用者在確認頁改
    assert oc.to_trad("掛式便斗 规格") == "掛式便斗 規格" and oc.to_trad("污排水管材料进场") == "污排水管材料進場"   # 同一格裡混著簡體字也不改壞
