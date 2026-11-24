"""photo_web/reader.py｜讀圖：任何照片先抓照片上的字，沒有字或字不足時描述意象，再組成 photo-inspection-log 的工作檔格式。

分兩層，讓規則可測、讀圖可換：
  1. 讀圖後端（Reader）回傳固定結構 PhotoRead（JSON）：
     - FixtureReader：讀預先抄好的 JSON（驗收用的固定答案，依照片 SHA-256 對應）。
     - ClaudeReader：呼叫 Claude API 視覺模型（需要平台密鑰 ANTHROPIC_API_KEY；費用項目待最後處理，尚未實跑）。
  2. compose_text()：程式把 PhotoRead 組成工作檔文字（日期換算用程式算，不交給模型）。

鐵則（SKILL.md）：只寫本張照片看得到的字；看不清寫〔?〕；不從其他照片、記憶、材料表補字；
量測讀數先獨立讀刻度或螢幕，再與白板比對；不用範例數字充當讀數。
描述（意象）與照片原字分開：描述一律寫在「（意象：…）」行，推定的檢查項目放 suggest_item，不寫進原字。
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field

KINDS = ["白板", "量測", "標示牌", "銘牌", "標示卡", "其他"]
NONE_FIELD = "—（照片無此欄）"

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "fixed": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in ("工程名稱", "監造單位", "承造單位")},
            "required": ["工程名稱", "監造單位", "承造單位"],
            "additionalProperties": False,
        },
        "inspect_date": {"type": "string"},
        "photo_date": {"type": "string"},
        "item": {"type": "string"},
        "location": {"type": "string"},
        "inspector": {"type": "string"},
        "head": {"type": "string"},
        "lines": {"type": "array", "items": {"type": "string"}},
        "readings": {"type": "array", "items": {"type": "string"}},
        "scene": {"type": "string"},
        "suggest_item": {"type": "string"},
    },
    "required": ["kind", "fixed", "inspect_date", "photo_date", "item", "location", "inspector", "head",
                 "lines", "readings", "scene", "suggest_item"],
    "additionalProperties": False,
}

PROMPT = """你是施工查驗照片的抄寫員。只看這一張照片，輸出 JSON（欄位定義如下）。
鐵則：
1. 只寫這張照片上看得到的字，逐字照抄（繁體中文；簡體寫法照字形轉繁體）；看不清的字寫〔?〕，不猜字、不補型號。
2. 不得從其他照片、記憶、常識或材料表補內容；照片上沒有的欄位一律留空字串。
3. 量測照片（捲尺、游標卡尺、量尺貼著材料）：先只看刻度或螢幕獨立讀數，寫進 readings（例「捲尺讀數：〈讀數〉 cm」）；
   讀不出就寫「〔?〕」。不得拿白板上的實測值當讀數。白板上的字照常寫進 lines。
4. 照片沒有字或字不足以說明內容時，在 scene 用一句話描述照片拍到什麼（物件、狀態），不得推測品牌型號。
欄位：
- kind：白板（查驗白板或夾板表單）／量測（主體是量具讀數）／標示牌（價格牌、告示）／銘牌（設備標籤）／標示卡（製品或物料標示卡）／其他
- fixed：工程名稱、監造單位、承造單位（照片上有才填，照抄）
- inspect_date：照片上寫的查驗日期原樣（例「115.10.27」），沒有就空字串
- photo_date：照片上的日期浮水印（例「2026-10-27」），沒有就空字串
- item：照片上「查驗項目」或「檢查項目」欄的字，沒有就空字串
- location：照片上「位置」欄的字；inspector：「檢查人員」欄的字
- head：白板表格的欄頭（例「品名 規格｜實測｜數量」），沒有就空字串
- lines：其餘看得到的字，一行一筆，保留原本分行；表格欄用「｜」分隔；旁註寫成「（旁註：…）」
- readings：量測讀數（見鐵則 3）
- scene：意象描述（見鐵則 4），字已足夠說明時可空字串
- suggest_item：建議的檢查項目名稱，只能取自照片上看得到的品名字樣，沒有就空字串"""


@dataclass
class PhotoRead:
    kind: str
    fixed: dict
    inspect_date: str = ""
    photo_date: str = ""
    item: str = ""
    location: str = ""
    inspector: str = ""
    head: str = ""
    lines: list = field(default_factory=list)
    readings: list = field(default_factory=list)
    scene: str = ""
    suggest_item: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "PhotoRead":
        return cls(**{k: d.get(k, "" if k not in ("lines", "readings", "fixed") else ([] if k != "fixed" else {}))
                      for k in SCHEMA["properties"]})


ROC = re.compile(r"(?<!\d)(\d{2,3})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})(?!\d)")
ISO = re.compile(r"(?<!\d)(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?!\d)")


def roc_to_iso(raw: str) -> str | None:
    """民國日期換西元（加 1911，程式算）；「115.10.27」→「2026-10-27」。看不出就回 None。"""
    if "〔?" in (raw or ""):
        return None
    m = ROC.search(raw or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else None


def norm_iso(raw: str) -> str | None:
    m = ISO.search(raw or "")
    if not m or "〔?" in raw:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else None


def compose_text(r: PhotoRead, exif_date: str = "") -> str:
    """組成工作檔文字（不含來源行；來源行由 pipeline.write_text 加）。exif_date 來自 prep 的 manifest。"""
    if r.inspect_date.strip():
        iso = roc_to_iso(r.inspect_date) or norm_iso(r.inspect_date)
        t = f"{iso}（{r.inspect_date.strip()}）" if iso else f"UNKNOWN（照片寫「{r.inspect_date.strip()}」，無法換算）"
    else:
        t = NONE_FIELD
    if exif_date and exif_date != "UNKNOWN":
        p = exif_date
    elif r.photo_date.strip():
        p = norm_iso(r.photo_date) or f"UNKNOWN（浮水印「{r.photo_date.strip()}」無法判讀）"
    else:
        p = "UNKNOWN（無 EXIF、無浮水印）"
    out = [f"查驗時間：{t}", f"照片日期：{p}",
           f"查驗項目：{r.item.strip() or NONE_FIELD}",
           f"位置：{r.location.strip() or NONE_FIELD}",
           f"檢查人員：{r.inspector.strip() or NONE_FIELD}",
           f"查驗說明（{r.head.strip()}）：" if r.head.strip() else "查驗說明："]
    out += [ln.strip() for ln in r.lines if ln.strip()]
    out += [f"（量測讀數：{x.strip()}）" for x in r.readings if x.strip()]
    if r.scene.strip():
        out.append(f"（意象：{r.scene.strip()}）")
    return "\n".join(out) + "\n"


NUM = re.compile(r"\d+(?:\.\d+)?")


def warnings(r: PhotoRead) -> list[str]:
    """鐵則自查：量測讀數和白板數字完全相同時提醒（可能拿白板值充當讀數）；列出待確認字數。"""
    out = []
    board = set(NUM.findall(" ".join(r.lines)))
    for x in r.readings:
        same = [n for n in NUM.findall(x) if n in board]
        if same:
            out.append(f"讀數「{x}」的數字 {'、'.join(same)} 與白板相同，請確認是從刻度或螢幕獨立讀出")
    q = sum(s.count("〔?") for s in [r.item, r.location, r.inspector, r.inspect_date, *r.lines, *r.readings])
    if q:
        out.append(f"待確認的字 {q} 處（〔?〕）")
    if not any([r.item, r.lines, r.readings]) and r.scene:
        out.append("照片上沒有可抄的字，只有意象描述")
    return out


def fixed_of(r: PhotoRead) -> dict:
    return {k: v.strip() for k, v in (r.fixed or {}).items() if v and v.strip()}


# ---------- 讀圖後端 ----------
class FixtureReader:
    """驗收用：依照片 SHA-256 讀預先抄好的答案（JSON：{sha256: PhotoRead 欄位}）。找不到就回 None（該張不寫，留給人工）。"""

    name = "固定答案"
    paid = False

    def __init__(self, path: str):
        self.data = json.load(open(path, encoding="utf-8"))

    def read(self, image_path: str, sha256: str, original_name: str) -> PhotoRead | None:
        d = self.data.get(sha256)
        return PhotoRead.from_dict(d) if d else None


IMPORT_PHOTO_MAX = 4000                  # 每張照片匯入內容的字數上限（同 pipeline.MAX_TEXT；Excel 一格上限 32767 字）
IMPORT_MAX = 1_000_000                                   # 讀圖檔上限（位元組）
IMPORT_PROMPT = PROMPT.replace("只看這一張照片，輸出 JSON（欄位定義如下）。",
                               "我會給你幾張照片。每一張都單獨看、單獨抄，不得把別張的字帶進來。") + """
輸出：只輸出一個 JSON 物件，放在一個程式碼區塊裡，不要其他說明。鍵是照片的原始檔名（例 "IMG_0001.jpeg"），值是上面那些欄位；
kind 只能是 """ + "、".join(KINDS) + """ 其中一個；fixed 是 {"工程名稱": "", "監造單位": "", "承造單位": ""}；lines、readings 是字串陣列，其餘是字串。
我會把你輸出的 JSON 貼到網頁，和照片一起上傳。"""


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x85\u2028\u2029\ufffe\uffff]")   # 同 pipeline.CTRL：Excel 不收的控制字元
FIELD_MAX = 200                                          # 單行欄位（工程資料、項目、位置、人員、日期、建議項目…）每欄上限


_QBIG = re.compile(r"〔\?[0-9０-９]{4,}〕")                # 待確認編號太大：拒收並說明（不靜默改）
_NOTE = re.compile(r"（[^（）\n]*）")                      # 同 pipeline.NOTE
_XESC = re.compile(r"_x[0-9A-Fa-f]{4}_|x005F_")                 # 同 pipeline.XESC


def _one_line(s: str) -> str:
    return _CTRL.sub("", " ".join(s.split()))             # 先把各種換行、定位、連續空白變一個空白，再去掉控制字元                             # 換行、定位、U+2028 等空白一律變一個空白（單行欄位不得帶換行）


def _find_json(text: str):
    """從貼上的整段文字取出 JSON 物件：先整段讀；不行就看 ``` 程式碼區塊（最多試 20 個）；再不行取第一個 { 到最後一個 }。
    只用字串切割，不用會回溯的正規表示式（稽核 v3 第 2 輪：1 MB 的特製內容曾讓伺服器卡 66 秒）。"""
    cands = [text]
    parts = text.split("```", 41)
    for blk in parts[1:41:2]:
        blk = blk.strip()
        if blk[:4].lower() == "json":
            blk = blk[4:].strip()
        if blk.startswith("{"):
            cands.append(blk)
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        cands.append(text[a:b + 1])
    for c in cands:
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
        if c is text:
            return obj                                     # 整段是合法 JSON 但不是物件：交給下面說明
    raise ValueError


def parse_import(data: bytes) -> dict:
    """使用者上傳的讀圖檔（AI 在對話裡看圖抄好的 JSON：{原檔名或 SHA-256: PhotoRead 欄位}）→ {鍵（小寫）: PhotoRead}。
    只收認得的欄位、型別不對就拒收並說明第幾筆；不執行、不解讀檔內任何指示（內容只當成抄寫文字，仍要使用者在確認頁看過）。"""
    if len(data) > IMPORT_MAX:
        raise ValueError("讀圖檔太大（上限 1 MB）")
    try:
        obj = _find_json(data.decode("utf-8-sig").strip())
    except Exception:
        raise ValueError("讀圖檔不是可以讀的 JSON（請整段複製 AI 回覆的程式碼區塊內容）") from None
    if not isinstance(obj, dict) or not obj:
        raise ValueError("讀圖檔的最外層要是「檔名 → 內容」的物件，而且至少一筆")
    try:
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        raise ValueError("讀圖檔裡有無法顯示的字元（不成對的代理字元），請 AI 重新輸出一次") from None
    out = {}
    for key, d in obj.items():
        if not isinstance(d, dict):
            raise ValueError(f"讀圖檔「{str(key)[:40]}」這一筆不是物件")
        clean = {}
        for k in SCHEMA["properties"]:
            v = d.get(k, {} if k == "fixed" else [] if k in ("lines", "readings") else "")
            if k == "fixed":
                if not isinstance(v, dict) or not all(isinstance(x, str) for x in v.values()):
                    raise ValueError(f"讀圖檔「{str(key)[:40]}」的 fixed 要是三個文字欄位")
                v = {f: _one_line(v.get(f, "")) for f in ("工程名稱", "監造單位", "承造單位")}
            elif k in ("lines", "readings"):
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError(f"讀圖檔「{str(key)[:40]}」的 {k} 要是文字陣列")
                v = [_one_line(x) for x in v]
            elif not isinstance(v, str):
                raise ValueError(f"讀圖檔「{str(key)[:40]}」的 {k} 要是文字")
            else:
                v = _one_line(v)
            clean[k] = v
        if clean["kind"] not in KINDS:
            clean["kind"] = "其他"
        read = PhotoRead.from_dict(clean)
        # 上限算「組成草稿後」的長度（每筆讀數、每個欄位組成草稿時會多出標籤字），不是原始字數
        if _QBIG.search(json.dumps(clean, ensure_ascii=False)):
            raise ValueError(f"讀圖檔「{str(key)[:40]}」的待確認編號太大（〔?N〕的 N 最多 3 位數），請改成〔?〕後再貼")
        flatj = json.dumps(clean, ensure_ascii=False)
        m = _XESC.search(flatj) or _XESC.search(_NOTE.sub("", flatj))
        if m:
            raise ValueError(f"讀圖檔「{str(key)[:40]}」含有「{m.group(0)}」這種字樣（底線、x、四位十六進位、底線；或 x005F 加底線），"
                             "寫進 Excel 會變成別的字或消失，請改掉後再貼")
        single = list(clean["fixed"].values()) + [v for k, v in clean.items() if isinstance(v, str) and k != "scene"]
        if any(len(x) > FIELD_MAX for x in single) or len(compose_text(read)) > IMPORT_PHOTO_MAX:
            raise ValueError(f"讀圖檔「{str(key)[:40]}」這一筆太長（工程資料、項目、位置、人員、日期、建議項目每欄上限 {FIELD_MAX} 字，"
                             f"組成草稿後上限 {IMPORT_PHOTO_MAX} 字）")
        out[re.split(r"[\\/]", str(key))[-1].strip().lower()] = read
    return out


class ImportedReader:
    """使用者匯入的讀圖檔（AI 在對話裡看圖抄的）：依原檔名或 SHA-256 取用；檔裡沒有的照片交給 fallback（本機 OCR，可為 None）。不用金鑰、不送外部服務。"""

    name = "匯入的讀圖檔（AI 看圖）"
    paid = False

    def __init__(self, data: dict, fallback=None):
        self.data, self.fallback = data, fallback
        self.missed: list[str] = []                       # 讀圖檔裡沒有的照片（原檔名）

    def read(self, image_path: str, sha256: str, original_name: str) -> PhotoRead | None:
        got = self.data.get(sha256.lower()) or self.data.get(os.path.basename(original_name).strip().lower())
        if got is not None:
            return got
        if original_name not in self.missed:
            self.missed.append(original_name)
        return self.fallback.read(image_path, sha256, original_name) if self.fallback else None


class ClaudeReader:
    """Claude API 視覺讀圖。金鑰只從環境變數或平台密鑰讀（ANTHROPIC_API_KEY），不寫進任何檔案。【NOT_RUN：費用待 MIKO 最後決定】"""

    name = "Claude API"
    paid = True                                           # 每張都會產生費用：中斷時已送出的張數照算每日用量

    def __init__(self, model: str | None = None):
        import anthropic  # 延後載入：沒裝 SDK 也能跑其他部分
        self.client = anthropic.Anthropic(timeout=120.0, max_retries=1)   # 一張卡住最久約 4 分鐘就改人工，不長時間占住處理鎖
        self.model = model or os.environ.get("PHOTO_READER_MODEL", "claude-opus-5-5")

    def read(self, image_path: str, sha256: str, original_name: str) -> PhotoRead | None:
        data = base64.standard_b64encode(open(image_path, "rb").read()).decode("utf-8")
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                {"type": "text", "text": PROMPT},
            ]}],
        )
        if resp.stop_reason != "end_turn":
            raise RuntimeError(f"讀圖未完成（stop_reason={resp.stop_reason}）")
        text = next(b.text for b in resp.content if b.type == "text")
        return PhotoRead.from_dict(json.loads(text))


def make_reader():
    """有 PHOTO_FIXTURE 就用固定答案；有 ANTHROPIC_API_KEY 用 Claude API；否則用本機 OCR（不用金鑰）；
    OCR 也不能用（沒裝、模型下載失敗、PHOTO_OCR=0）回 None，網頁改人工抄寫。"""
    fx = os.environ.get("PHOTO_FIXTURE")
    if fx and os.path.exists(fx):
        return FixtureReader(fx)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeReader()
    return make_ocr_reader()


def make_ocr_reader():
    if os.environ.get("PHOTO_OCR", "1").strip() == "0":
        return None
    try:
        import ocr_reader
        return ocr_reader.OcrReader()
    except Exception:
        import logging
        logging.exception("自動讀字（OCR）無法啟用，改人工抄寫")
        return None
