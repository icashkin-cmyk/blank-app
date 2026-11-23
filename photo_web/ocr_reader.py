"""photo_web/ocr_reader.py｜不用金鑰的自動讀字：本機 OCR（RapidOCR，PP-OCRv6 small）讀照片上的字，組成 reader.PhotoRead。

限制（實測，2026-11-23 的 9 張手寫白板）：印刷字大多讀得到，手寫字約一半以下；所以這裡只做「草稿」：
  - 信心低的字後面加〔?〕，信心很低的整格只留〔?〕（不留亂猜的字）；
  - 草稿一律要使用者看過、勾選後才寫入（網頁的確認關卡），讀不到的由使用者補填；
  - OCR 沒辦法描述照片內容（意象），scene 一律留空。
分兩層：ocr() 呼叫引擎回傳 Box 清單；compose() 是純函式（可用假的 Box 測），負責找欄位、排列、加標記。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import reader as rd

LOW, DROP = 0.90, 0.50                 # 低於 LOW 加〔?〕；低於 DROP 整格只留〔?〕（手寫字信心 0.8 上下仍常讀錯，門檻取高）
Q = "〔?〕"
KEEP = "台干后里面表板系布余松斗谷制范云几卷只才"   # 繁體也在用的字：不轉換（「台南」不可變成「臺南」）

LABELS = [                              # (欄位鍵, 標籤寫法)；順序＝比對優先序
    ("工程名稱", r"工.名[稱称]"),           # 中間一字放寬：印刷標籤也會被讀錯一個字（例「工解名稱」）
    ("監造單位", r"[監监]造.位"),
    ("承造單位", r"承[攬揽].{1,2}商|承造.位|施工.商"),
    ("item", r"[查檢检].?[項项]目"),
    ("desc", r"[查檢检].[說说]明"),
    ("inspector", r"[檢检查][查驗验]人.?[員员]|人[員员]"),
    ("date", r".[驗验查]日期|日期"),
    ("location", r"位置"),
    ("brand", r"[廠厂麻][牌]"),
]
SHORT = re.compile(r"^(人[員员]|日期|位置|[廠厂麻]牌)$")   # 短標籤只在一格的開頭才算，避免把內文當標籤
LABEL_RE = re.compile("|".join(f"(?P<g{i}>{p})" for i, (_, p) in enumerate(LABELS)))
WATERMARK = re.compile(r"^(20\d\d)[-./](\d{1,2})[-./](\d{1,2})$")
QTY = re.compile(r"^\d{1,4}\s*[組组個个支只箱捲卷米台套片]?$")


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    score: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(1.0, self.y1 - self.y0)


_cc = None


def to_trad(s: str) -> str:
    """簡體字形轉繁體（OCR 模型常輸出簡體）；KEEP 內的字不動。沒裝 opencc 就原樣回傳。"""
    global _cc
    if _cc is None:
        try:
            import opencc
            _cc = opencc.OpenCC("s2t")
        except Exception:
            _cc = False
    if not _cc:
        return s
    out = []
    for ch in s:
        out.append(ch if ch in KEEP or ord(ch) < 0x2E80 else _cc.convert(ch))
    return "".join(out)


def mark(text: str, score: float) -> str:
    """依信心加待確認標記：很低只留〔?〕，偏低在後面加〔?〕。"""
    text = to_trad(text.strip())
    if not text or score < DROP:
        return Q
    return text + Q if score < LOW else text


def _rows(boxes: list[Box]) -> list[list[Box]]:
    """照 y 中心分列（容許半個字高的誤差），列內照 x 排。"""
    rows: list[list[Box]] = []
    for b in sorted(boxes, key=lambda b: b.cy):
        if rows and abs(rows[-1][-1].cy - b.cy) <= 0.6 * max(rows[-1][-1].h, b.h):
            rows[-1].append(b)
        else:
            rows.append([b])
    return [sorted(r, key=lambda b: b.x0) for r in rows]


def _below(b: Box, pool: list[Box]) -> Box | None:
    """標籤正下方最近的一格（「廠牌」是欄頭，廠牌名稱寫在它下面）。"""
    cand = [o for o in pool if o is not b and 0 < o.cy - b.cy <= 2.5 * b.h and o.x0 < b.x1 and o.x1 > b.x0]
    return min(cand, key=lambda o: o.cy - b.cy) if cand else None


def _right_of(b: Box, pool: list[Box]) -> Box | None:
    """標籤右邊、同一列附近最近的一格（手寫的值常比印刷標籤高或低一點）。"""
    cand = [o for o in pool if o is not b and (o.x0 + o.x1) / 2 > (b.x0 + b.x1) / 2 and abs(o.cy - b.cy) <= 1.2 * b.h]
    return min(cand, key=lambda o: (max(0.0, o.x0 - b.x1), abs(o.cy - b.cy))) if cand else None


def _labels(text: str) -> list[re.Match]:
    """這一格裡的標籤；「人員」「日期」這類短標籤只在開頭才算（避免把內文的字當標籤）。"""
    return [m for m in LABEL_RE.finditer(text) if not (SHORT.match(m.group(0)) and m.start() > 1)]


def _has_label(b: Box) -> bool:
    return bool(_labels(b.text))


def compose(boxes: list[Box], width: int, height: int) -> rd.PhotoRead:
    """Box 清單 → PhotoRead。只用這張照片的 Box；找不到的欄位留空，不補。"""
    photo_date, rest = "", []
    for b in boxes:
        m = WATERMARK.match(b.text.strip())
        if m and b.cy > 0.85 * height and b.score >= LOW:      # 右下角的日期浮水印
            photo_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        else:
            rest.append(b)

    found: dict[str, tuple[str, float]] = {}      # 欄位鍵 → (值, 信心)
    label_boxes: list[Box] = []
    used: set[int] = set()
    for b in rest:
        ms = _labels(b.text)
        if not ms:
            continue
        label_boxes.append(b); used.add(id(b))
        for i, m in enumerate(ms):
            key = LABELS[int(m.lastgroup[1:])][0]
            end = ms[i + 1].start() if i + 1 < len(ms) else len(b.text)
            val = b.text[m.end():end].strip(" :：·.．/／、")
            score = b.score
            if key == "brand":                     # 廠牌：只取正下方那一格，沒有就不填
                o = None if val else _below(b, [x for x in rest if id(x) not in used])
                if o is not None:
                    val, score = o.text, o.score
                    used.add(id(o))
            elif not val and i + 1 == len(ms):     # 值在右邊另一格（手寫）
                o = _right_of(b, [x for x in rest if id(x) not in used and not _has_label(x)])   # 右邊是另一個標籤：不是這一欄的值
                if o is not None:
                    val, score = o.text, o.score
                    used.add(id(o))
            if key not in found and val:
                found[key] = (val, score)

    is_board = len(label_boxes) >= 2
    others = [b for b in rest if id(b) not in used]
    inside, outside = others, []
    if is_board:                                   # 白板範圍：標籤所在的上下範圍、標籤與值的左右範圍再放寬一點
        top = min(b.y0 for b in label_boxes) - 0.5 * label_boxes[0].h
        bottom = max(b.y1 for b in label_boxes) + 0.5 * label_boxes[0].h
        left = min(b.x0 for b in label_boxes)
        anchors = label_boxes + [b for b in rest if id(b) in used]
        right = left + (max(b.x1 for b in anchors) - left) * 1.12
        left -= 0.05 * (right - left)
        inside = [b for b in others if top <= b.cy <= bottom and left <= (b.x0 + b.x1) / 2 <= right]
        outside = [b for b in others if b not in inside]

    lines, head = [], ""
    item_box = next((b for b in label_boxes if re.search(LABELS[3][1], b.text)), None)
    for row in _rows(inside):
        if item_box is not None and abs(row[0].cy - item_box.cy) <= 1.2 * item_box.h and len(row) == 1 and len(row[0].text) <= 4:
            head = "品名規格｜數量"                 # 查驗項目那一列右邊的短字是「數量」欄頭
            continue
        cells = [mark(b.text, b.score) for b in row]
        if len(row) >= 2 and QTY.match(row[-1].text.strip()):
            lines.append(" ".join(cells[:-1]) + "｜" + cells[-1])
        else:
            lines.append(" ".join(cells))
    extra = [mark(b.text, b.score) for row in _rows(outside) for b in row if b.score >= LOW]
    if extra:
        lines.append("（旁註：白板以外的字：" + "、".join(extra[:12]) + ("…" if len(extra) > 12 else "") + "）")

    def val(key: str) -> str:
        return mark(*found[key]) if key in found else ""

    if "brand" in found:
        lines.append("廠牌：" + val("brand"))
    if "desc" in found:                             # 「查驗說明：」後面同一格的字是第一行內容
        lines.insert(0, val("desc"))
    date = val("date")
    if date and Q not in date:
        iso = rd.roc_to_iso(date) if re.fullmatch(r"\d{2,3}[.\-/]\d{1,2}[.\-/]\d{1,2}", date) else None
        if iso is None or (photo_date and iso != photo_date):
            date += Q                               # 不像日期、或和照片日期不同：標待確認，不自行改寫
    return rd.PhotoRead(
        kind="白板" if is_board else "其他",
        fixed={k: val(k) for k in ("工程名稱", "監造單位", "承造單位")},
        inspect_date=date, photo_date=photo_date,
        item=val("item"), location=val("location"), inspector=val("inspector"),
        head=head, lines=lines, readings=[], scene="", suggest_item="")


def boxes_from_quads(quads: list[tuple[list[tuple[float, float]], str, float]]) -> list[Box]:
    """四邊形（左上、右上、右下、左下）→ Box。白板常是斜拿的：用長格子的上緣斜率取中位數，把 y 扶正後再分列。"""
    slopes = sorted((q[1][1] - q[0][1]) / (q[1][0] - q[0][0]) for q, _, _ in quads
                    if q[1][0] - q[0][0] > 3 * max(1.0, q[3][1] - q[0][1]))
    k = slopes[len(slopes) // 2] if slopes else 0.0
    out = []
    for q, text, score in quads:
        ys = [y - k * x for x, y in q]
        top, bot = (ys[0] + ys[1]) / 2, (ys[2] + ys[3]) / 2
        out.append(Box(min(x for x, _ in q), top, max(x for x, _ in q), bot, text, score))
    return out


class OcrReader:
    """本機 OCR 讀圖，不用金鑰、不送外部服務。模型第一次使用時下載（約 30MB），之後留在快取。"""

    name = "自動讀字（OCR）"
    paid = False

    def __init__(self):
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import ModelType, OCRVersion
        v, m = OCRVersion.PPOCRV6, ModelType.SMALL
        self.engine = RapidOCR(params={"Det.ocr_version": v, "Det.model_type": m, "Rec.ocr_version": v, "Rec.model_type": m,
                                       "Global.text_score": 0.0, "Global.log_level": "error"})

    def ocr(self, image_path: str) -> tuple[list[Box], int, int]:
        from PIL import Image
        with Image.open(image_path) as im:
            w, h = im.size
        r = self.engine(image_path)
        quads = []
        if r is not None and getattr(r, "txts", None):
            quads = [([(float(p[0]), float(p[1])) for p in pts], str(t), float(s)) for pts, t, s in zip(r.boxes, r.txts, r.scores)]
        return boxes_from_quads(quads), w, h

    def read(self, image_path: str, sha256: str, original_name: str) -> rd.PhotoRead | None:
        boxes, w, h = self.ocr(image_path)
        return compose(boxes, w, h)
