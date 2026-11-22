"""photo_web/pipeline.py｜S1 流程控制：照順序呼叫 photo-inspection-log 的既有腳本，不改寫腳本邏輯。

每次工作（Job）一個暫存資料夾，結構與 SKILL.md「一、整包取得」相同：
    <工作資料夾>/scripts/   既有腳本與範本（取自 miko main 固定 commit）
    <工作資料夾>/           執行目錄（incoming/、photos/、text/、fixed/、manifest.csv、照片紀錄.xlsx …）
S1 不接 AI：白板文字由使用者在網頁上逐張填寫，本模組只負責寫檔與呼叫腳本。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field

from openpyxl import load_workbook

import reader as rd

# 網頁鎖定的 Skill 版本（miko main；待 MIKO／總召確認）。腳本最後一次修改在 c573e81，本 commit 的腳本與其相同。
SKILL_COMMIT = "a79ddfde2c7eaaaad8da9ada9e0ef15ccfb70a6e"
SKILL_FILES = ["log_photo.py", "build_company.py", "make_entries.py", "preflight.py", "forms.csv", "材料自主檢查照片_範本.xlsx"]
RAW_URL = "https://raw.githubusercontent.com/icashkin-cmyk/miko/{commit}/skills/photo-inspection-log/scripts/{name}"
WORKBOOK = "照片紀錄.xlsx"
OUTPUT = "材料自主檢查照片.xlsx"
TEXT_FIELDS = ["查驗時間", "照片日期", "查驗項目", "位置", "檢查人員"]
FIXED_FIELDS = ["工程名稱", "監造單位", "承造單位"]


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
        self.dir = tempfile.mkdtemp(prefix="photojob_", dir=root)
        self.scripts = os.path.join(self.dir, "scripts")
        self.log: list[StepResult] = []
        self.reads: dict[str, rd.PhotoRead] = {}   # sha256 → 讀圖結果（建議欄位用）
        if scripts_src:  # 已下載好的快取資料夾
            shutil.copytree(scripts_src, self.scripts)
            self.hashes = {n: sha256_bytes(open(os.path.join(self.scripts, n), "rb").read()) for n in SKILL_FILES}
        else:
            self.hashes = fetch_scripts(self.scripts)

    # ---------- 共用 ----------
    def p(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    def run(self, name: str, script: str, *args: str) -> StepResult:
        cmd = [sys.executable, "-I", "-B", f"scripts/{script}", *args]
        cp = subprocess.run(cmd, cwd=self.dir, capture_output=True, text=True, timeout=600)
        res = StepResult(name, cmd[2:], cp.returncode, cp.stdout, cp.stderr)
        self.log.append(res)
        return res

    def must(self, res: StepResult, msg: str) -> StepResult:
        if not res.ok:
            raise PipelineError(msg, res)
        return res

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
    def add_photos(self, files: list[tuple[str, bytes]]) -> StepResult:
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

    def auto_read(self, reader) -> dict[str, list[str]]:
        """讀圖後端逐張讀還沒寫入的照片，組成工作檔文字寫進 text/NNN.txt；回傳 {原檔名: 提醒}。讀不到的照片不寫（留給人工）。"""
        notes = {}
        for r in self.manifest():
            if r["狀態"] == "done" or os.path.exists(self.p("text", f"{r['nnn']}.txt")):
                continue
            got = reader.read(self.p("photos", f"{r['nnn']}.jpg"), r["sha256"], r["原檔名"])
            if got is None:
                notes[r["原檔名"]] = ["讀圖沒有結果，請人工抄寫"]
                continue
            exif = r["照片日期"] if r.get("exif時間") else ""
            self.write_text(r["nnn"], rd.compose_text(got, exif), rd.fixed_of(got))
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
        src = f"本機 {os.environ['PHOTO_SKILL_DIR']}" if os.environ.get("PHOTO_SKILL_DIR") else f"miko {SKILL_COMMIT}"
        out = [f"材料自主檢查照片｜檢查紀錄｜Skill 來源：{src}", "腳本 SHA-256："] + [f"- {n}：{h}" for n, h in self.hashes.items()] + [""]
        for r in self.log:
            out += [f"## {r.name}（結束碼 {r.returncode}）", "$ " + " ".join(r.cmd), r.stdout.rstrip()]
            if r.stderr.strip():
                out.append("stderr: " + r.stderr.rstrip())
            out.append("")
        return "\n".join(out)

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
