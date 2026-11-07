"""抓資料腳本的還原因子計算（不連網）。"""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fetch_finmind", Path(__file__).parent.parent / "tools" / "fetch_finmind.py")
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)


class TestBuildAdj(unittest.TestCase):
    def test_factor_from_dividend_result(self):
        rows = [{"date": d, "open": c, "max": c, "min": c, "close": c} for d, c in
                [("2024-06-11", 100.0), ("2024-06-12", 100.0), ("2024-06-13", 96.0), ("2024-06-14", 97.0)]]
        events = [{"date": "2024-06-13", "before_price": 100.0, "after_price": 95.0}]
        adj, checks = ff.build_adj(rows, events)
        self.assertEqual([r["close"] for r in adj], [95.0, 95.0, 96.0, 97.0])
        self.assertEqual(checks[0]["status"], "OK")

    def test_prev_close_mismatch_flagged(self):
        rows = [{"date": "a", "open": 1, "max": 1, "min": 1, "close": 10.0},
                {"date": "b", "open": 1, "max": 1, "min": 1, "close": 9.0}]
        _, checks = ff.build_adj(rows, [{"date": "b", "before_price": 11.0, "after_price": 10.0}])
        self.assertEqual(checks[0]["status"], "PREV_CLOSE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
