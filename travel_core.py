"""旅遊規劃需求單與行程骨架（純邏輯，不依賴 Streamlit，方便測試）。

依 DGHP_TRAVEL_RULES.md（0.1.0-CANDIDATE）：
- 交通區分微型電動二輪、電輔車、大眾運輸；不預設可駕駛汽車／機車。
- 行程以「出發 → 去程 → 景點 → 餐飲 → 回程 → 到家」的順向流程表呈現。
- 本模組只產生骨架與時間預算，所有班次、店家、營業時間一律標 NOT_VERIFIED。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

REGIONS = {
    "北部": ["臺北市", "新北市", "基隆市", "桃園市", "新竹市", "新竹縣", "宜蘭縣"],
    "中部": ["苗栗縣", "臺中市", "彰化縣", "南投縣", "雲林縣"],
    "南部": ["嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣"],
    "東部": ["花蓮縣", "臺東縣"],
    "離島": ["澎湖縣", "金門縣", "連江縣（馬祖）", "綠島", "蘭嶼", "小琉球"],
}

# 行程長度：(標籤, 天數)；夜數 = 天數 - 1
TRIP_LENGTHS = {
    "半日遊": 1,
    "一日遊": 1,
    "二天一夜": 2,
    "三天二夜": 3,
    "四天三夜": 4,
    "五天四夜": 5,
}

PUBLIC_TRANSIT = "大眾運輸"
MICRO_EV = "微型電動二輪"
E_BIKE = "電輔車"
WALK = "步行"
CAR = "汽車（需明確有合法駕駛）"
SCOOTER = "機車（需明確有合法駕駛）"

TRANSPORT_MODES = [PUBLIC_TRANSIT, MICRO_EV, E_BIKE, WALK]
DRIVER_ONLY_MODES = [CAR, SCOOTER]

PUBLIC_TRANSIT_TYPES = ["台鐵", "高鐵", "捷運／輕軌", "公車／客運", "觀光巴士（台灣好行等）", "渡輪", "計程車／叫車"]

TRANSPORT_NOTES = {
    PUBLIC_TRANSIT: "需查指定日期官方班次、末班、轉乘站與餘票；一般時刻表不代表當日確定開行或有座位。",
    MICRO_EV: "與電輔車分開計算；不得行駛高速／快速道路，跨縣市長距離不適用，需確認續航、充電點與停車。上路規定以交通部公告為準。",
    E_BIKE: "與微型電動二輪分開計算；須確認電池續航、坡度、可否隨車上火車／捷運（各業者規定不同）與停放位置。",
    WALK: "站內／站外步行時間需計入每段交通，不只看兩個發車時刻。",
    CAR: "僅在使用者明確具合法駕駛資格時納入；需另計停車與塞車緩衝。",
    SCOOTER: "僅在使用者明確具合法駕駛資格時納入；需另計停車與天候風險。",
}

INTERESTS = ["自然風景", "老街／歷史", "博物館／展覽", "親子", "溫泉", "海邊", "登山步道", "夜景", "購物", "文青／咖啡", "宗教／廟宇", "攝影"]
FOOD_SLOTS = ["正餐", "小吃", "甜點", "飲品", "伴手禮"]
PACES = {"悠閒": 120, "一般": 90, "緊湊": 60}  # 每個景點停留分鐘數
LODGING = ["未定", "飯店", "民宿", "青年旅館", "露營", "親友家"]

VERIFY = "NOT_VERIFIED"
LONG_LEG_MINUTES = 90  # 去回程暫估，未依實際距離或班次計算


@dataclass
class TripRequest:
    region_group: str
    destination: str
    origin: str
    start_date: date
    trip_length: str
    transport: list[str]
    transit_types: list[str] = field(default_factory=list)
    companions: str = "1 人"
    budget_per_person: int = 0
    depart_time: time = time(8, 0)
    return_deadline: time = time(21, 0)
    pace: str = "一般"
    interests: list[str] = field(default_factory=list)
    food_slots: list[str] = field(default_factory=lambda: ["正餐", "小吃", "甜點"])
    exclude_night_market: bool = False
    lodging: str = "未定"
    fixed_nodes: str = ""
    notes: str = ""

    @property
    def days(self) -> int:
        return TRIP_LENGTHS[self.trip_length]

    @property
    def nights(self) -> int:
        return self.days - 1

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.days - 1)


WEEKDAYS = "一二三四五六日"


def fmt_date(d: date) -> str:
    return f"{d.isoformat()}（週{WEEKDAYS[d.weekday()]}）"


def missing_fields(req: TripRequest) -> list[str]:
    """回傳仍未提供、但規劃必需的欄位。"""
    missing = []
    if not req.destination.strip():
        missing.append("目的地")
    if not req.origin.strip():
        missing.append("出發地")
    if not req.transport:
        missing.append("交通方式")
    if PUBLIC_TRANSIT in req.transport and not req.transit_types:
        missing.append("大眾運輸種類")
    if req.budget_per_person <= 0:
        missing.append("每人預算")
    if req.trip_length != "半日遊" and req.nights > 0 and req.lodging == "未定":
        missing.append("住宿類型")
    return missing


def warnings(req: TripRequest) -> list[str]:
    """交通與時間的風險提示（規則推論，非查證結果）。"""
    out = []
    if req.destination in REGIONS["離島"] and PUBLIC_TRANSIT in req.transport and "渡輪" not in req.transit_types:
        out.append("離島目的地通常需要渡輪或班機，大眾運輸種類未勾選渡輪。")
    if req.transport and set(req.transport) <= {MICRO_EV, E_BIKE, WALK}:
        out.append("只選了微型電動二輪／電輔車／步行：跨縣市長距離需確認續航與路線，或改搭大眾運輸再於當地使用。")
    if req.days == 1 and req.return_deadline <= req.depart_time:
        out.append("回家截止時間早於或等於出發時間，請確認是否跨日。")
    if req.return_deadline < time(6, 0):
        out.append("回家截止在凌晨：日曆日期已是隔天，需另查跨午夜末班車與營業歸屬。")
    if any(m in req.transport for m in DRIVER_ONLY_MODES):
        out.append("已勾選汽車／機車：僅在你本人或同行者明確具合法駕駛資格時採用。")
    return out


def _add(t: datetime, minutes: int) -> datetime:
    return t + timedelta(minutes=minutes)


def build_skeleton(req: TripRequest) -> list[dict]:
    """產生順向流程表骨架。

    時間僅為依步調推估的預算，不是班次或營業時間；所有列狀態為 NOT_VERIFIED。
    """
    stay = PACES[req.pace]
    local_mode = "、".join(m for m in req.transport if m != PUBLIC_TRANSIT) or PUBLIC_TRANSIT
    long_mode = "大眾運輸（" + "、".join(req.transit_types) + "）" if PUBLIC_TRANSIT in req.transport and req.transit_types else "、".join(req.transport) or "未指定"
    rows: list[dict] = []

    def row(d: date, t: datetime | None, item: str, mode: str = "", minutes: int | None = None, risk: str = ""):
        rows.append({
            "日期": fmt_date(d),
            "預估時間": t.strftime("%H:%M") if t else "",
            "節點": item,
            "交通方式": mode,
            "停留／移動（分）": str(minutes) if minutes is not None else "",
            "風險／待查": risk,
            "驗證": VERIFY,
        })

    for i in range(req.days):
        d = req.start_date + timedelta(days=i)
        first, last = i == 0, i == req.days - 1
        t = datetime.combine(d, req.depart_time if first else time(9, 0))
        start_label = f"從{req.origin}出發" if first else "住宿處出發"
        row(d, t, start_label, risk="確認起點位置（不沿用舊定位）")
        if first:
            row(d, t, f"去程：{req.origin} → {req.destination}", long_mode, LONG_LEG_MINUTES, "查指定日期班次、上下車站、轉乘緩衝、餘票；移動時間為暫估")
            t = _add(t, LONG_LEG_MINUTES)
        spots = 1 if req.trip_length == "半日遊" else 2
        interest = "、".join(req.interests) or "景點"
        for s in range(spots):
            if s == 1:
                t = max(t, datetime.combine(d, time(11, 30)))
                lunch = "午餐（正餐）" if "正餐" in req.food_slots else "午餐"
                row(d, t, lunch, local_mode, 60, "抵達時是否營業、L.O.、候位；抵達≠吃完")
                t = _add(t, 60 + 20)
            row(d, t, f"景點 {s + 1}（{interest}）", local_mode, stay, "開放時間、門票、天候")
            t = _add(t, stay + 20)
        if req.trip_length != "半日遊":
            for slot in ("小吃", "甜點", "飲品"):
                if slot in req.food_slots:
                    row(d, t, slot, local_mode, 30, "順路程度、排隊")
                    t = _add(t, 30 + 10)
            t = max(t, datetime.combine(d, time(17, 30)))
            dinner_risk = "L.O.、與下一個硬出發時間的距離"
            if req.exclude_night_market:
                dinner_risk += "；已排除夜市"
            row(d, t, "晚餐（正餐）" if "正餐" in req.food_slots else "晚餐", local_mode, 75, dinner_risk)
            t = _add(t, 75 + 15)
        if last:
            if "伴手禮" in req.food_slots:
                row(d, t, "伴手禮", local_mode, 30, "營業時間、可否攜帶上車")
                t = _add(t, 40)
            deadline = datetime.combine(d, req.return_deadline)
            latest = _add(deadline, -LONG_LEG_MINUTES)
            back_risk = "先確認回程班次與末班，再排去程"
            if t > latest:
                back_risk += f"；推估已晚於最晚回程 {latest.strftime('%H:%M')}，需刪節點"
            row(d, t, f"回程：{req.destination} → {req.origin}", long_mode, LONG_LEG_MINUTES, back_risk)
            row(d, deadline, f"到家（截止 {req.return_deadline.strftime('%H:%M')}）", risk="硬截止，不可默默移動")
        else:
            lodging = req.lodging if req.lodging != "未定" else "住宿（類型未定）"
            row(d, t, f"入住：{lodging}", local_mode, None, "入住時間、停車／充電、早餐")
    return rows


BACKUP_PLANS = [
    {"方案": "A｜去程無票或延誤", "觸發": "指定車次售完或延誤", "改搭／改走": "先找能配對回程的下一合法班次", "少玩": "刪去最後一個景點", "回家": "維持原回程班次"},
    {"方案": "B｜店休或排隊過長", "觸發": "抵達時未營業或候位超過預算", "改搭／改走": "只替換該餐飲節點，改同區備選店", "少玩": "不動其他節點", "回家": "不變"},
    {"方案": "C｜天候或回程受阻", "觸發": "下雨、颱風、末班車取消", "改搭／改走": "改室內景點；回程改前一班或替代路線", "少玩": "戶外景點", "回家": "以可確認的最早班次為準"},
]


def to_payload(req: TripRequest) -> dict:
    data = asdict(req)
    data["start_date"] = req.start_date.isoformat()
    data["end_date"] = req.end_date.isoformat()
    data["depart_time"] = req.depart_time.strftime("%H:%M")
    data["return_deadline"] = req.return_deadline.strftime("%H:%M")
    data["days"] = req.days
    data["nights"] = req.nights
    data["missing"] = missing_fields(req)
    data["status"] = VERIFY
    return data


def to_json(req: TripRequest) -> str:
    return json.dumps(to_payload(req), ensure_ascii=False, indent=2)


def to_markdown(req: TripRequest) -> str:
    rows = build_skeleton(req)
    lines = [
        f"# {req.destination} {req.trip_length} 需求單",
        "",
        f"- 日期：{fmt_date(req.start_date)} ～ {fmt_date(req.end_date)}（{req.days} 天 {req.nights} 夜）",
        f"- 出發地：{req.origin}；目的地：{req.region_group}／{req.destination}",
        f"- 交通：{'、'.join(req.transport) or '未指定'}" + (f"（{'、'.join(req.transit_types)}）" if req.transit_types else ""),
        f"- 同行：{req.companions}；每人預算：{req.budget_per_person or '未填'} 元",
        f"- 出發 {req.depart_time.strftime('%H:%M')}；回家截止 {req.return_deadline.strftime('%H:%M')}；步調：{req.pace}",
        f"- 興趣：{'、'.join(req.interests) or '未指定'}；餐飲：{'、'.join(req.food_slots) or '未指定'}"
        + ("；排除夜市" if req.exclude_night_market else ""),
        f"- 住宿：{req.lodging if req.nights else '不需'}",
    ]
    if req.fixed_nodes.strip():
        lines.append(f"- 固定節點：{req.fixed_nodes.strip()}")
    if req.notes.strip():
        lines.append(f"- 備註：{req.notes.strip()}")
    miss = missing_fields(req)
    if miss:
        lines.append(f"- 尚缺：{'、'.join(miss)}")
    lines += ["", "## 順向流程表（骨架，時間為推估，全部 NOT_VERIFIED）", ""]
    headers = list(rows[0].keys())
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "---|" * len(headers))
    for r in rows:
        lines.append("| " + " | ".join(str(r[h]) for h in headers) + " |")
    lines += ["", "## 備案", ""]
    bh = list(BACKUP_PLANS[0].keys())
    lines.append("| " + " | ".join(bh) + " |")
    lines.append("|" + "---|" * len(bh))
    for b in BACKUP_PLANS:
        lines.append("| " + " | ".join(b[h] for h in bh) + " |")
    return "\n".join(lines) + "\n"


def to_prompt(req: TripRequest) -> str:
    """給旅遊對話串使用的規劃指令。"""
    return (
        "請依 DGHP_TRAVEL_RULES.md 規劃以下行程，產出一張從出發到回家的順向流程表與 A/B/C 備案。"
        "餐飲依 GOOGLE_ENTITY_VERIFIED → FOOD_REVIEW → TRAVEL_ROUTE_FIT → INSERT_INTO_ITINERARY，"
        "優先引用美食專案查詢結果；未查證項目標 NOT_VERIFIED；先確認回程再排去程。\n\n"
        + to_markdown(req)
    )
