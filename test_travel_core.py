import json
from datetime import date, time

import travel_core as tc


def make(**kw):
    base = dict(
        region_group="南部",
        destination="高雄市",
        origin="臺北車站",
        start_date=date(2026, 10, 24),
        trip_length="二天一夜",
        transport=[tc.PUBLIC_TRANSIT, tc.E_BIKE],
        transit_types=["高鐵"],
        budget_per_person=5000,
        lodging="飯店",
    )
    base.update(kw)
    return tc.TripRequest(**base)


def test_days_nights():
    r = make()
    assert (r.days, r.nights) == (2, 1)
    assert r.end_date == date(2026, 10, 25)
    assert make(trip_length="一日遊").nights == 0


def test_skeleton_starts_and_ends_at_home():
    rows = tc.build_skeleton(make())
    assert rows[0]["節點"].startswith("從臺北車站出發")
    assert rows[-1]["節點"].startswith("到家")
    assert any("入住" in r["節點"] for r in rows)
    assert all(r["驗證"] == "NOT_VERIFIED" for r in rows)


def test_day_trip_has_no_lodging():
    rows = tc.build_skeleton(make(trip_length="一日遊", lodging="未定"))
    assert not any("入住" in r["節點"] for r in rows)
    assert "住宿類型" not in tc.missing_fields(make(trip_length="一日遊", lodging="未定"))


def test_driving_not_default():
    assert not set(tc.DRIVER_ONLY_MODES) & set(tc.TRANSPORT_MODES)
    assert tc.MICRO_EV != tc.E_BIKE


def test_missing_and_warnings():
    r = make(origin="", transport=[tc.MICRO_EV], transit_types=[], budget_per_person=0)
    miss = tc.missing_fields(r)
    assert "出發地" in miss and "每人預算" in miss
    assert any("跨縣市" in w for w in tc.warnings(r))
    assert any("凌晨" in w for w in tc.warnings(make(return_deadline=time(0, 30))))


def test_exports():
    r = make(exclude_night_market=True)
    md = tc.to_markdown(r)
    assert "排除夜市" in md and "| 方案 |" in md
    data = json.loads(tc.to_json(r))
    assert data["days"] == 2 and data["status"] == "NOT_VERIFIED"


def test_return_overrun_flagged():
    rows = tc.build_skeleton(make(trip_length="一日遊", return_deadline=time(18, 0)))
    back = next(r for r in rows if r["節點"].startswith("回程"))
    assert "需刪節點" in back["風險／待查"]
