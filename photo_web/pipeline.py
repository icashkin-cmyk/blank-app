"""photo_web/pipeline.py｜流程控制：照順序呼叫 photo-inspection-log 的既有腳本，不改寫腳本邏輯。

每次工作（Job）一個暫存資料夾，結構與 SKILL.md「一、整包取得」相同：
    <工作資料夾>/scripts/   既有腳本與範本（取自 miko main 固定 commit）
    <工作資料夾>/           執行目錄（incoming/、photos/、text/、fixed/、manifest.csv、照片紀錄.xlsx …）
讀圖草稿（reader.py）只放在記憶體，使用者逐張核對勾選後才寫檔；本模組只負責檢查收件、寫檔與呼叫腳本。
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field

from openpyxl import load_workbook

import reader as rd

try:                                 # iPhone HEIC：收件檢查也要能開（log_photo.py 自己也註冊；沒註冊時裝了 pillow-heif 反而全部被拒）
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:                    # 沒裝，或底層函式庫壞掉：當成沒有解碼器，不讓整頁起不來
    pillow_heif = None

# 網頁鎖定的 Skill 版本（miko main）。這個 commit 是舊版腳本；photo-inspection-log v0.23 合併進 main 後要改成新的 commit（見 README）。
SKILL_COMMIT = "6b95d8b455a825e4ed8116e6d2f62020c225cae1"
SKILL_FILES = ["log_photo.py", "build_company.py", "make_entries.py", "preflight.py", "forms.csv", "材料自主檢查照片_範本.xlsx"]
RAW_URL = "https://raw.githubusercontent.com/icashkin-cmyk/miko/{commit}/skills/photo-inspection-log/scripts/{name}"
WORKBOOK = "照片紀錄.xlsx"
OUTPUT = "材料自主檢查照片.xlsx"
TEXT_FIELDS = ["查驗時間", "照片日期", "查驗項目", "位置", "檢查人員"]
FIXED_FIELDS = ["工程名稱", "監造單位", "承造單位"]


def skill_source() -> str:
    """實際使用的 Skill 腳本來源（頁面與檢查紀錄都顯示這個，不顯示伺服器路徑）。"""
    return "本機腳本（PHOTO_SKILL_DIR，未固定版本）" if os.environ.get("PHOTO_SKILL_DIR") else f"miko {SKILL_COMMIT[:7]}"


class PipelineError(Exception):
    """某一步失敗：訊息給使用者看，result 留存命令與輸出。"""

    def __init__(self, msg: str, result: "StepResult | None" = None):
        super().__init__(msg)
        self.result = result


@dataclass
class StepResult:
    name: str
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def fail_lines(self) -> list[str]:
        # preflight 印 FAIL 卻回傳 0（既有缺陷，safe-implementation ADR-065），所以不能只看結束碼
        return [ln for ln in self.stdout.splitlines() if ln.startswith(("FAIL ", "STOP "))]

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.fail_lines


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


IMG_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")
MAX_PHOTOS = 20                      # 一次工作的照片張數上限（含 zip 內）
MAX_BYTES = 300 * 1024 * 1024        # 一次工作解開後的總大小上限
MAX_PIXELS = 80_000_000              # 單張像素上限（手機照片約 1200～5000 萬；擋像素極大的壓縮炸彈）
JOB_ROOT = os.path.join(tempfile.gettempdir(), "photo_web_jobs")
JOB_MAX_AGE_S = 2 * 3600             # 沒有動作超過這個時間的工作資料夾自動清除


def _zip_images(data: bytes) -> list[zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            # 條件要和既有腳本 log_photo.prep 完全相同（只排除「._」開頭）；不同的話，上限會被繞過（第二輪稽核 F4）
            return [i for i in z.infolist() if os.path.basename(i.filename) and i.filename.lower().endswith(IMG_EXT)
                    and not os.path.basename(i.filename).startswith("._")]
    except zipfile.BadZipFile as e:
        raise PipelineError("zip 檔打不開，請重新壓縮後再上傳") from e


def count_photos(files: list[tuple[str, bytes]]) -> int:
    """上傳的照片張數，zip 裡的也算（2026-11-23 稽核：只算散檔時，上限可用 zip 繞過）。"""
    n = 0
    for name, data in files:
        n += len(_zip_images(data)) if name.lower().endswith(".zip") else 1
    return n


def _check_image(name: str, data: bytes) -> None:
    """內容必須是開得起來的圖片；HEIC 在沒有解碼器的環境不檢查內容（交給既有腳本處理）。"""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            raise PipelineError(f"「{os.path.basename(name)}」的像素太大（上限 {MAX_PIXELS // 1_000_000} 百萬像素），請縮小後重新上傳")
        im.verify()
    except PipelineError:
        raise
    except Exception as e:
        if name.lower().endswith((".heic", ".heif")) and pillow_heif is None:
            return                   # 沒有解碼器的環境：內容交給既有腳本處理
        raise PipelineError(f"「{os.path.basename(name)}」不是可以開啟的圖片，請移除後重新上傳") from e


def sanitize(text: str, job_dir: str = "") -> str:
    """給使用者看的輸出：拿掉伺服器路徑與 Traceback 細節（2026-11-23 稽核 W4）。"""
    if "Traceback (most recent call last)" in text:
        head, _, tail = text.partition("Traceback (most recent call last)")
        last = [ln for ln in tail.splitlines() if ln.strip() and not ln.startswith(" ")]
        text = head + "程式錯誤：" + (last[-1] if last else "（無訊息）") + "\n"
    for path, label in ((job_dir, "<工作資料夾>"), (os.environ.get("PHOTO_SKILL_DIR", ""), "<Skill 腳本>")):
        if path:
            text = text.replace(path, label)
    text = re.sub(r"/(?:[\w.+-]+/)+(?:site-packages|dist-packages|lib/python[\d.]+)(?:/[\w.+-]+)*", "<系統>", text)
    return re.sub(r"(?<![\w<>.])/(?:home|tmp|usr|opt|var|srv|app|mount|mnt|root|workspace|private|Users|etc|data|proc|nix)(?:/[\w.+@~-]+)+", "<路徑>", text)


def sweep(root: str = "", max_age_s: int = JOB_MAX_AGE_S) -> int:
    """清掉太久沒動的工作資料夾（使用者沒按「結束並刪除暫存」就關掉分頁時）。回傳清掉幾個。"""
    root = root or JOB_ROOT
    n, now = 0, time.time()
    if not os.path.isdir(root):
        return 0
    for d in os.listdir(root):
        full = os.path.join(root, d)
        if d.startswith("photojob_") and os.path.isdir(full) and now - os.path.getmtime(full) > max_age_s:
            shutil.rmtree(full, ignore_errors=True); n += 1
    return n


class Usage:
    """每人每日張數。鍵是「日期|使用者」；檔案放在工作根目錄（不進 repo），寫入時上鎖。容器重啟會歸零。"""

    def __init__(self, path: str = ""):
        self.path = path or os.path.join(JOB_ROOT, "usage.json")

    def _load(self) -> dict:
        try:
            return json.load(open(self.path, encoding="utf-8"))
        except Exception:
            return {}

    def used(self, user: str) -> int:
        return self._load().get(f"{datetime.date.today()}|{user}", 0)

    def try_add(self, user: str, n: int, limit: int = 0) -> bool:
        """在同一個鎖裡檢查上限並累加；超過上限回 False、不加。limit=0 不限。"""
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            d = {k: v for k, v in self._load().items() if k.startswith(str(datetime.date.today()))}   # 只留今天
            k = f"{datetime.date.today()}|{user}"
            if limit and d.get(k, 0) + n > limit:
                return False
            d[k] = d.get(k, 0) + n
            tmp = self.path + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8")); os.replace(tmp, self.path)
            return True

    def prune(self) -> None:
        """清掉不是今天的紀錄（含 email）。每次開頁呼叫，不必等到有人上傳。"""
        import fcntl
        today = str(datetime.date.today())
        if all(k.startswith(today) for k in self._load()):
            return
        with open(self.path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            d = {k: v for k, v in self._load().items() if k.startswith(today)}
            tmp = self.path + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8")); os.replace(tmp, self.path)

    def add(self, user: str, n: int) -> None:
        self.try_add(user, n, 0)

    def refund(self, user: str, n: int) -> None:
        """退回預扣的張數（收件失敗、重複照片）；不會退成負數。"""
        if n > 0:
            self.try_add(user, -min(n, self.used(user)), 0)


def parse_daily(raw: str | None) -> tuple[int, str]:
    """PHOTO_DAILY_LIMIT → (上限, 錯誤訊息)。空的＝0 不限；非數字或負數回報設定錯誤。"""
    if raw is None or not raw.strip():
        return 0, ""
    try:
        n = int(raw.strip())
    except ValueError:
        return 0, "每日上限（PHOTO_DAILY_LIMIT）不是數字，請管理者修正。"
    return (n, "") if n >= 0 else (0, "每日上限（PHOTO_DAILY_LIMIT）不能是負數，請管理者修正。")


def recover(usage: "Usage", root: str = "") -> int:
    """退回「預扣了卻沒結清、也沒有人在處理」的工作（處理中重新整理、斷線），並清掉資料夾。回傳處理了幾個。
    是否還在處理，用工作資料夾裡的鎖判斷：處理中的那一輪握著鎖，中斷或結束就放開。"""
    import fcntl
    root = root or JOB_ROOT
    n = 0
    if not os.path.isdir(root):
        return 0
    for d in os.listdir(root):
        full = os.path.join(root, d)
        rpath = os.path.join(full, "reserve.json")
        if not (d.startswith("photojob_") and os.path.exists(rpath)):
            continue
        try:
            with open(os.path.join(full, ".busy"), "w") as lk:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue                                  # 有人正在處理
                if json.load(open(rpath, encoding="utf-8")).get("settled"):
                    continue
                r = claim_reservation(full)
                if r is None:
                    continue                                  # 別人已經退過
                _refund_unspent(usage, r)
                shutil.rmtree(full, ignore_errors=True); n += 1
        except Exception:
            continue
    return n


def claim_reservation(folder: str) -> dict | None:
    """把預扣紀錄改名取走（改名是原子動作）：同一筆預扣只有一方拿得到，不會退兩次。拿不到回 None。"""
    src = os.path.join(folder, "reserve.json")
    dst = os.path.join(folder, f"reserve.claimed.{os.urandom(4).hex()}")
    try:
        os.rename(src, dst)
        return json.load(open(dst, encoding="utf-8"))
    except Exception:
        return None


def _refund_unspent(usage: "Usage", r: dict) -> None:
    """退回沒結清的預扣；已送去付費讀圖的張數（spent）照算，不退。隔天的紀錄不退（用量已歸零）。"""
    if r.get("user") and not r.get("settled") and r.get("date") == str(datetime.date.today()):
        usage.refund(r["user"], int(r["n"]) - int(r.get("spent", 0)))


def pending_reserved(user: str, root: str = "") -> int:
    """這個人還沒結清的預扣張數（處理中，或剛中斷、還沒退回）。"""
    root = root or JOB_ROOT
    n = 0
    if not os.path.isdir(root):
        return 0
    for d in os.listdir(root):
        try:
            r = json.load(open(os.path.join(root, d, "reserve.json"), encoding="utf-8"))
        except Exception:
            continue
        if r.get("user") == user and not r.get("settled") and r.get("date") == str(datetime.date.today()):
            n += int(r["n"]) - int(r.get("spent", 0))
    return n


def parse_allowed(raw: str | None) -> tuple[bool, set[str]]:
    """PHOTO_ALLOWED_EMAILS → (有沒有設, 名單)。有設但解析後是空的：視為「有設、沒有人可用」，不是不設限。"""
    if raw is None:
        return False, set()
    return True, {e.strip().lower() for e in raw.split(",") if e.strip()}


def paid_reading_allowed(allowed: set[str], daily: int) -> bool:
    """會產生費用的讀圖：要同時有使用者名單與每日上限才啟用。"""
    return bool(allowed) and daily > 0


def fetch_scripts(dest: str, commit: str = SKILL_COMMIT, local_dir: str | None = None) -> dict[str, str]:
    """把既有腳本放進 dest，回傳 {檔名: SHA-256}。local_dir（或環境變數 PHOTO_SKILL_DIR）有值時從本機複製，否則從 GitHub 固定 commit 下載。"""
    os.makedirs(dest, exist_ok=True)
    local_dir = local_dir or os.environ.get("PHOTO_SKILL_DIR")
    hashes = {}
    for name in SKILL_FILES:
        if local_dir:
            data = open(os.path.join(local_dir, name), "rb").read()
        else:
            url = RAW_URL.format(commit=commit, name=urllib.request.quote(name))
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = r.read()
            except Exception as e:  # 任一檔失敗就停（SKILL.md：缺一不可）
                raise PipelineError(f"下載 {name} 失敗（{e.__class__.__name__}），停止：Skill 必須整包使用") from e
        open(os.path.join(dest, name), "wb").write(data)
        hashes[name] = sha256_bytes(data)
    return hashes


class Job:
    """一次工作：上傳 → 抄寫 → 寫入 → entries → 產出 → 交檔檢查。"""

    def __init__(self, root: str | None = None, scripts_src: str | None = None):
        root = root or JOB_ROOT
        os.makedirs(root, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="photojob_", dir=root)
        self.drafts: dict[str, str] = {}             # nnn → 讀圖草稿（使用者勾選前不寫進 text/）
        self.draft_fixed: dict[str, dict] = {}       # nnn → 讀圖讀到的固定欄位
        self.scripts = os.path.join(self.dir, "scripts")
        self.log: list[StepResult] = []
        self.reads: dict[str, rd.PhotoRead] = {}   # sha256 → 讀圖結果（建議欄位用）
        try:
            if scripts_src:  # 已下載好的快取資料夾
                shutil.copytree(scripts_src, self.scripts)
                self.hashes = {n: sha256_bytes(open(os.path.join(self.scripts, n), "rb").read()) for n in SKILL_FILES}
            else:
                self.hashes = fetch_scripts(self.scripts)
        except Exception as e:           # 腳本來源缺檔：不留下空殼資料夾
            shutil.rmtree(self.dir, ignore_errors=True)
            if isinstance(e, PipelineError):
                raise
            raise PipelineError("Skill 腳本不完整，無法開始（Skill 必須整包使用），請通知管理者。") from e

    # ---------- 共用 ----------
    def p(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    def run(self, name: str, script: str, *args: str) -> StepResult:
        cmd = [sys.executable, "-I", "-B", f"scripts/{script}", *args]
        cp = subprocess.run(cmd, cwd=self.dir, capture_output=True, text=True, timeout=600)
        os.utime(self.dir)                              # 還在用：不被 sweep 清掉
        res = StepResult(name, cmd[2:], cp.returncode, sanitize(cp.stdout, self.dir), sanitize(cp.stderr, self.dir))
        self.log.append(res)
        return res

    def must(self, res: StepResult, msg: str) -> StepResult:
        if not res.ok:
            raise PipelineError(msg, res)
        return res

    # ---------- 每日用量：預扣記在工作資料夾，工作階段消失也退得回來 ----------
    def processing(self):
        """收件到讀圖完成這一段握著鎖；recover() 看到鎖就不動這個工作。"""
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def _cm():
            with open(self.p(".busy"), "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                yield
        return _cm()

    def reserve(self, usage: "Usage", user: str, n: int, limit: int = 0) -> bool:
        if not usage.try_add(user, n, limit):
            return False
        self._save_reservation({"user": user, "n": n, "date": str(datetime.date.today()), "settled": False, "spent": 0})
        return True

    def begin(self) -> None:
        """沒設每日上限時也留一筆「處理中」紀錄（不記使用者、不動用量）：中斷後 recover() 才認得、清得掉（第六輪稽核 M1）。"""
        self._save_reservation({"user": "", "n": 0, "date": str(datetime.date.today()), "settled": False, "spent": 0})

    def _save_reservation(self, r: dict) -> None:
        tmp = self.p("reserve.tmp")
        json.dump(r, open(tmp, "w", encoding="utf-8")); os.replace(tmp, self.p("reserve.json"))

    def _claim(self) -> dict | None:
        return claim_reservation(self.dir)

    def _spend(self) -> None:
        """付費讀圖每送出一張就記一筆：之後中斷，這幾張照算不退。"""
        r = self._reservation()
        if r and not r.get("settled") and r.get("user"):
            r["spent"] = int(r.get("spent", 0)) + 1
            self._save_reservation(r)

    def _reservation(self) -> dict | None:
        try:
            return json.load(open(self.p("reserve.json"), encoding="utf-8"))
        except Exception:
            return None

    def settle(self, usage: "Usage", actual: int) -> None:
        """收件與讀圖都完成：只算實際新增的張數，多扣的退回，標成已結清。"""
        r = self._reservation()
        if not r or r.get("settled"):
            return
        actual = max(actual, int(r.get("spent", 0)))           # 已付費讀過的張數不會少算
        if r.get("user"):
            usage.refund(r["user"], int(r["n"]) - actual)
        r.update(n=actual, settled=True)
        self._save_reservation(r)

    def abandon(self, usage: "Usage") -> None:
        """放棄這個工作並刪除資料夾；還沒結清的預扣退回，已結清的不退。"""
        r = self._reservation()
        if r and not r.get("settled"):
            r = self._claim()
            if r:
                _refund_unspent(usage, r)
        self.cleanup()

    def touch(self) -> None:
        """頁面每次重跑時呼叫：還有人在用，不被 sweep 清掉。"""
        if os.path.isdir(self.dir):
            os.utime(self.dir)

    def alive(self) -> bool:
        return os.path.isdir(self.dir)

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------- 第 0、1 步 ----------
    def preflight_start(self) -> StepResult:
        return self.must(self.run("開工檢查", "preflight.py", "start"), "開工檢查有 FAIL，停止")

    def restore(self, name: str, data: bytes) -> StepResult:
        src = self.p("上次_" + os.path.basename(name))
        open(src, "wb").write(data)
        return self.must(self.run("接續上次檔案", "log_photo.py", "restore", os.path.basename(src), WORKBOOK), "接續失敗")

    # ---------- 第 2 步：收照片 ----------
    def add_photos(self, files: list[tuple[str, bytes]], max_photos: int | None = None) -> StepResult:
        """收件前先檢查：張數（含 zip 內）、總大小、每個檔都是開得起來的圖片；不合格就不寫入任何檔。"""
        max_photos = MAX_PHOTOS if max_photos is None else max_photos
        n = count_photos(files)
        if n == 0:
            raise PipelineError("沒有收到照片")
        if max_photos and n > max_photos:
            raise PipelineError(f"共 {n} 張，超過一次 {max_photos} 張的上限，請分批上傳")
        total = 0
        for name, data in files:
            if name.lower().endswith(".zip"):
                infos = _zip_images(data)
                total += sum(i.file_size for i in infos)
                if total > MAX_BYTES:
                    raise PipelineError("照片總大小超過上限，請分批上傳")
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for i in infos:
                        _check_image(i.filename, z.read(i))
            else:
                total += len(data)
                _check_image(name, data)
        if total > MAX_BYTES:
            raise PipelineError("照片總大小超過上限，請分批上傳")
        inc = self.p("incoming")
        os.makedirs(inc, exist_ok=True)
        for name, data in files:
            base = os.path.basename(name)
            if not base:
                continue
            open(os.path.join(inc, base), "wb").write(data)
        zips = [os.path.basename(n) for n, _ in files if n.lower().endswith(".zip")]
        res = None
        if any(not n.lower().endswith(".zip") for n, _ in files):
            res = self.must(self.run("整理照片", "log_photo.py", "prep", "incoming", WORKBOOK), "整理照片失敗")
        for z in zips:
            res = self.must(self.run("整理照片（zip）", "log_photo.py", "prep", f"incoming/{z}", WORKBOOK), "整理 zip 失敗")
        if res is None:
            raise PipelineError("沒有收到照片")
        todo = len(self.stopped())
        if max_photos and todo > max_photos:                # 再用 manifest 複核一次：實際收到的不得多於上限
            raise PipelineError(f"實際收到 {todo} 張，超過一次 {max_photos} 張的上限，請分批上傳")
        return res

    def manifest(self) -> list[dict]:
        mf = self.p("manifest.csv")
        return list(csv.DictReader(open(mf, encoding="utf-8"))) if os.path.exists(mf) else []

    def template(self, nnn: str) -> str:
        return open(self.p("text", f"{nnn}.tpl"), encoding="utf-8").read()

    def write_text(self, nnn: str, body: str, fixed: dict[str, str] | None = None) -> str:
        """寫 text/NNN.txt：第一行來源行照 manifest（log_photo v0.16 要求），第二行起是使用者填的工作檔內容。"""
        row = next((r for r in self.manifest() if r["nnn"] == nnn), None)
        if row is None:
            raise PipelineError(f"manifest 沒有第 {nnn} 張")
        path = self.p("text", f"{nnn}.txt")
        open(path, "w", encoding="utf-8").write(f"來源：photos/{nnn}_grid.jpg（{row['原檔名']}）\n{body.strip()}\n")
        given = fixed is not None
        fixed = {k: v.strip() for k, v in (fixed or {}).items() if v and v.strip()}
        fpath = self.p("fixed", f"{nnn}.txt")
        if fixed:
            os.makedirs(self.p("fixed"), exist_ok=True)
            open(fpath, "w", encoding="utf-8").write("".join(f"{k}：{v}\n" for k, v in fixed.items()))
        elif given and os.path.exists(fpath):
            os.remove(fpath)   # 使用者把固定欄位全部清空：不再帶入
        return path

    def fixed(self, nnn: str) -> dict[str, str]:
        fpath = self.p("fixed", f"{nnn}.txt")
        if not os.path.exists(fpath):
            return {}
        return dict(ln.rstrip("\n").split("：", 1) for ln in open(fpath, encoding="utf-8") if "：" in ln)

    def fixed_options(self) -> dict[str, list[tuple[str, int]]]:
        """各固定欄位在待寫入照片的讀圖草稿裡出現過的寫法與張數，多的排前面；已有基準（接續上次）時基準排第一。
        含〔?〕的寫法（沒讀清楚）排最後。"""
        from collections import Counter
        todo = {r["nnn"] for r in self.stopped()}
        base = {r[0]: r[1] for r in self._sheet_rows("基準") if r and r[0] in FIXED_FIELDS and r[1]} if os.path.exists(self.p(WORKBOOK)) else {}
        out = {}
        for f in FIXED_FIELDS:
            c = Counter(v[f].strip() for n, v in self.draft_fixed.items() if n in todo and v.get(f, "").strip())
            opts = sorted(c.items(), key=lambda kv: ("〔?" in kv[0], -kv[1]))
            if base.get(f):
                opts = [(base[f], c.get(base[f], 0))] + [o for o in opts if o[0] != base[f]]
            out[f] = opts
        return out

    def fixed_for(self, nnn: str, confirmed: dict[str, str]) -> dict[str, str]:
        """這一張要寫入的固定欄位：使用者在確認頁確認過的整批值。
        有讀圖草稿時，只給「這張照片上讀得到該欄」的照片（照片上沒有的欄位不帶入）；沒有讀圖（人工抄寫）時每張都用確認值。"""
        if not self.draft_fixed:
            return dict(confirmed)
        mine = self.draft_fixed.get(nnn, {})
        return {f: v for f, v in confirmed.items() if mine.get(f, "").strip()}

    def draft(self, nnn: str) -> str:
        """第 2 步文字框的預設內容：讀圖草稿，沒有就用空白範本。"""
        return self.drafts.get(nnn) or self.template(nnn)

    def auto_read(self, reader, on_photo=None) -> dict[str, list[str]]:
        """讀圖後端逐張讀還沒寫入的照片，組成工作檔文字放在草稿（self.drafts）；回傳 {原檔名: 提醒}。
        草稿不寫進 text/NNN.txt：使用者在第 2 步勾選「我已看過」後才由 write_text 寫入
        （2026-11-23 稽核 W2：草稿先落檔時，沒勾選的照片也會被 addmany 寫入）。一張失敗不影響其他張。
        on_photo(i, n)：每張讀之前呼叫；reader.paid 為真時，每送出一張就記進預扣紀錄（中斷後照算）。"""
        notes = {}
        todo = [r for r in self.manifest()
                if r["狀態"] != "done" and not os.path.exists(self.p("text", f"{r['nnn']}.txt"))]
        for i, r in enumerate(todo):
            self.touch()
            if on_photo:                                      # 頁面在這裡更新進度；使用者中斷時也是從這裡停下（不攔）
                on_photo(i, len(todo))
            if getattr(reader, "paid", False):
                self._spend()
            try:
                got = reader.read(self.p("photos", f"{r['nnn']}.jpg"), r["sha256"], r["原檔名"])
            except Exception:
                notes[r["原檔名"]] = ["讀圖失敗，請人工抄寫"]
                continue
            if got is None:
                notes[r["原檔名"]] = ["讀圖沒有結果，請人工抄寫"]
                continue
            exif = r["照片日期"] if r.get("exif時間") else ""
            self.drafts[r["nnn"]] = rd.compose_text(got, exif)
            self.draft_fixed[r["nnn"]] = rd.fixed_of(got)
            self.reads[r["sha256"]] = got
            notes[r["原檔名"]] = rd.warnings(got)
        return notes

    def apply_suggestions(self, entries: list[dict]) -> list[dict]:
        """照片沒有「查驗項目」欄時，用讀圖的建議品名當檢查項目；只改 entries（給使用者在第 4 步確認），不改工作檔原文。
        意象描述保留在設計/標準值（2026-11-22 稽核：意象在 Excel 看不到）。量測讀數行由 make_entries v0.5 放進實測值；
        舊版 make_entries（v0.4 以前）留在設計/標準值的量測讀數行，這裡搬到實測值，白板實測一併保留。"""
        where = {r["列號"]: r["sha256"] for r in self.manifest() if r.get("列號")}
        for e in entries:
            got = self.reads.get(where.get(f"{e['_sheet']}:{e['_row']}", ""))
            if (not e["item"] or e["item"].startswith(("—", "UNKNOWN"))) and got and got.suggest_item:
                e["item"] = got.suggest_item
            elif e["item"].startswith(("—", "UNKNOWN")):
                e["item"] = ""
            meas = [ln[len("（量測讀數："):].removesuffix("）") for ln in e["design"] if ln.startswith("（量測讀數：")]
            if meas:
                e["design"] = [ln for ln in e["design"] if not ln.startswith("（量測讀數：")]
                e["measured"] = [m for m in e["measured"] if not m.startswith("（量測讀數：")] + meas
        return entries

    def check_report(self) -> str:
        """每一步的命令、結束碼與輸出，跟 Excel 一起交（2026-11-22 稽核：交付物缺交檔檢查紀錄）。"""
        src = skill_source()
        out = [f"材料自主檢查照片｜檢查紀錄｜Skill 來源：{src}", "腳本 SHA-256："] + [f"- {n}：{h}" for n, h in self.hashes.items()] + [""]
        for r in self.log:
            out += [f"## {r.name}（結束碼 {r.returncode}）", "$ " + " ".join(r.cmd), r.stdout.rstrip()]
            if r.stderr.strip():
                out.append("stderr: " + r.stderr.rstrip())
            out.append("")
        return sanitize("\n".join(out), self.dir)

    def addmany(self) -> StepResult:
        return self.must(self.run("寫入工作檔", "log_photo.py", "addmany", WORKBOOK), "寫入工作檔失敗")

    def stopped(self) -> list[dict]:
        return [r for r in self.manifest() if r["狀態"] != "done"]

    # ---------- 基準與分項工程 ----------
    def _sheet_rows(self, sheet: str) -> list[tuple]:
        if not os.path.exists(self.p(WORKBOOK)):
            return []
        wb = load_workbook(self.p(WORKBOOK), read_only=True)
        if sheet not in wb.sheetnames:
            return []
        return [r for r in wb[sheet].iter_rows(min_row=2, values_only=True) if r and r[0]]

    def pending_base(self) -> list[tuple]:
        return self._sheet_rows("基準待確認")

    def confirm_base(self, keys: list[str]) -> StepResult:
        return self.must(self.run("確認新基準", "log_photo.py", "base", WORKBOOK, *keys), "寫入基準失敗")

    def items(self) -> list[str]:
        out = []
        for e in self.make_entries(write=False):
            if e["item"] not in out:
                out.append(e["item"])
        return out

    def set_sub(self, item: str, name: str) -> StepResult:
        return self.must(self.run("分項工程名稱", "log_photo.py", "sub", WORKBOOK, item, name), "記錄分項工程名稱失敗")

    # ---------- 第 3～7 步 ----------
    def make_entries(self, write: bool = True) -> list[dict]:
        out = "entries.json" if write else "_entries_preview.json"
        self.must(self.run("產生 entries", "make_entries.py", WORKBOOK, out, "photos_out"), "產生 entries 失敗（可能有照片缺漏）")
        return json.load(open(self.p(out), encoding="utf-8"))

    def save_entries(self, entries: list[dict]) -> None:
        json.dump(entries, open(self.p("entries.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def build(self) -> StepResult:
        return self.must(self.run("產出公司格式", "build_company.py", "entries.json", OUTPUT, WORKBOOK), "產出失敗")

    def preflight_deliver(self) -> StepResult:
        return self.must(self.run("交檔檢查", "preflight.py", "deliver", OUTPUT, "entries.json"), "交檔檢查有 FAIL，不得交檔")

    def to_pdf(self) -> bytes | None:
        """LibreOffice 轉 PDF 給使用者預覽；環境沒有 soffice 時回傳 None（不擋交檔）。"""
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            return None
        cp = subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", self.dir, self.p(OUTPUT)],
                            capture_output=True, text=True, timeout=180)
        pdf = self.p(OUTPUT.replace(".xlsx", ".pdf"))
        return open(pdf, "rb").read() if cp.returncode == 0 and os.path.exists(pdf) else None

    def output_bytes(self) -> bytes:
        return open(self.p(OUTPUT), "rb").read()
