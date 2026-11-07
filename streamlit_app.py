from datetime import date, time, timedelta

import pandas as pd
import streamlit as st

import travel_core as tc

st.set_page_config(page_title="旅遊規劃", page_icon="🧳", layout="wide")
st.title("🧳 旅遊規劃")
st.caption("填寫需求後產生行程骨架與需求單。時間僅為推估，班次、店家、營業時間一律標 NOT_VERIFIED，需另行查證。")

with st.sidebar:
    st.header("基本條件")
    region_group = st.selectbox("地區", list(tc.REGIONS))
    dest_choice = st.selectbox("目的地", tc.REGIONS[region_group] + ["其他（自填）"])
    destination = st.text_input("自填目的地") if dest_choice == "其他（自填）" else dest_choice
    origin = st.text_input("出發地", placeholder="例如：臺北車站", key="origin")
    trip_length = st.radio("天數", list(tc.TRIP_LENGTHS), index=1, horizontal=True)
    start_date = st.date_input("出發日期", value=date.today() + timedelta(days=7))
    c1, c2 = st.columns(2)
    depart_time = c1.time_input("出發時間", value=time(8, 0), step=900)
    return_deadline = c2.time_input("回家截止", value=time(21, 0), step=900)

st.subheader("交通方式")
transport = st.multiselect(
    "可用交通（可複選）",
    tc.TRANSPORT_MODES,
    default=[tc.PUBLIC_TRANSIT],
    help="微型電動二輪與電輔車分開計算。",
)
can_drive = st.checkbox("我或同行者有合法駕駛資格（才會開放汽車／機車）")
if can_drive:
    transport += st.multiselect("自駕選項", tc.DRIVER_ONLY_MODES)
transit_types = []
if tc.PUBLIC_TRANSIT in transport:
    transit_types = st.multiselect("大眾運輸種類", tc.PUBLIC_TRANSIT_TYPES, default=["台鐵", "公車／客運"])
for m in transport:
    st.caption(f"**{m}**：{tc.TRANSPORT_NOTES[m]}")

st.subheader("其他選項")
c1, c2, c3 = st.columns(3)
companions = c1.selectbox("同行者", ["1 人", "2 人", "3–4 人", "5 人以上", "含長輩", "含小孩", "含寵物"])
budget = c2.number_input("每人預算（元，0 表示未定）", min_value=0, step=500, value=0)
pace = c3.radio("步調", list(tc.PACES), index=1, horizontal=True)
interests = st.multiselect("興趣", tc.INTERESTS)
c1, c2 = st.columns([3, 1])
food_slots = c1.multiselect("餐飲安排", tc.FOOD_SLOTS, default=["正餐", "小吃", "甜點"])
exclude_nm = c2.checkbox("排除夜市")
lodging = st.selectbox("住宿", tc.LODGING, disabled=tc.TRIP_LENGTHS[trip_length] == 1)
fixed_nodes = st.text_input("固定節點（必去景點、已訂車次、夜景時間等）")
notes = st.text_area("備註", height=70)

req = tc.TripRequest(
    region_group=region_group,
    destination=destination or "",
    origin=origin,
    start_date=start_date,
    trip_length=trip_length,
    transport=transport,
    transit_types=transit_types,
    companions=companions,
    budget_per_person=int(budget),
    depart_time=depart_time,
    return_deadline=return_deadline,
    pace=pace,
    interests=interests,
    food_slots=food_slots,
    exclude_night_market=exclude_nm,
    lodging=lodging,
    fixed_nodes=fixed_nodes,
    notes=notes,
)

st.divider()
st.subheader(f"{req.destination or '（未選目的地）'}｜{req.trip_length}｜{tc.fmt_date(req.start_date)} ～ {tc.fmt_date(req.end_date)}")

missing = tc.missing_fields(req)
if missing:
    st.warning("尚缺：" + "、".join(missing))
for w in tc.warnings(req):
    st.info(w)

tab1, tab2, tab3 = st.tabs(["順向流程表", "備案", "需求單／匯出"])
with tab1:
    st.dataframe(pd.DataFrame(tc.build_skeleton(req)), hide_index=True, width="stretch")
with tab2:
    st.dataframe(pd.DataFrame(tc.BACKUP_PLANS), hide_index=True, width="stretch")
with tab3:
    st.markdown("複製下方指令貼到旅遊對話串，即可依規則展開完整行程。")
    st.code(tc.to_prompt(req), language="markdown")
    c1, c2 = st.columns(2)
    name = f"trip_{req.start_date.isoformat()}"
    c1.download_button("下載 Markdown", tc.to_markdown(req), file_name=f"{name}.md", mime="text/markdown")
    c2.download_button("下載 JSON", tc.to_json(req), file_name=f"{name}.json", mime="application/json")
