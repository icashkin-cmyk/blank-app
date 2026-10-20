"""以答案已知的合成價格驗證計算機制。這些數字只證明引擎算對，不代表任何策略績效。"""
import math
import unittest

import numpy as np
import pandas as pd

from backtest import engine, metrics, strategy
from backtest.costs import CostModel

CAP = 1_000_000


def bars(ohlc):
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"])
    df.insert(0, "date", pd.date_range("2024-01-01", periods=len(df), freq="B"))
    df["volume"] = 1000
    return df


def manual_signals(n, entry_idx, dist):
    return pd.DataFrame({"entry": [i == entry_idx for i in range(n)], "trail_dist": [dist] * n})


BASE = [
    (100, 100, 100, 100),
    (100, 101, 99, 100),   # 1：收盤後訊號，初始停損 100-5=95
    (102, 106, 101, 105),  # 2：開盤 102 進場；收盤後停損 max(95, 105-5)=100
    (106, 112, 105, 110),  # 3：最低 105 > 100 不出場；收盤後停損 105
    (109, 109, 104, 104),  # 4：最低 104 ≤ 105，以 105 出場
    (104, 104, 104, 104),
]


class TestCosts(unittest.TestCase):
    def test_fee_and_tax(self):
        c = CostModel()
        self.assertEqual(c.fee(100_000), 142)      # 142.5 捨去
        self.assertEqual(c.fee(110_000), 156)      # 156.75 捨去
        self.assertEqual(c.tax(110_000), 330)
        self.assertEqual(c.fee(100), 20)           # 最低 20 元
        self.assertEqual(CostModel(fee_discount=0.6).fee(100_000), 85)

    def test_max_shares_fits_cash(self):
        c = CostModel()
        n = c.max_shares(CAP, 102)
        self.assertLessEqual(n * 102 + c.fee(n * 102), CAP)
        self.assertGreater((n + 1) * 102 + c.fee((n + 1) * 102), CAP)


class TestEngine(unittest.TestCase):
    def run_base(self, rows=BASE, entry_idx=1, dist=5.0):
        df = bars(rows)
        return engine.run(df, manual_signals(len(df), entry_idx, dist), CAP)

    def test_single_trade_trailing_stop(self):
        res = self.run_base()
        self.assertEqual(len(res.trades), 1)
        t = res.trades.iloc[0]
        self.assertEqual(t.entry_date, pd.Timestamp("2024-01-03"))  # 訊號隔日
        self.assertEqual(t.entry_price, 102)
        self.assertEqual(t.exit_price, 105)
        self.assertEqual(t.exit_reason, "stop")
        n = 9789  # 1e6 / (102 × 1.001425) = 9789.97；9789 股成本 998478+1422=999900 ≤ 1e6，9790 股為 1000002 超出
        self.assertEqual(t.shares, n)
        buy_fee = math.floor(n * 102 * 0.001425)
        sell_fee = math.floor(n * 105 * 0.001425)
        tax = math.floor(n * 105 * 0.003)
        expected = n * 105 - sell_fee - tax - (n * 102 + buy_fee)
        self.assertAlmostEqual(t.pnl, expected)
        self.assertAlmostEqual(res.equity.equity.iloc[-1], CAP + expected)

    def test_gap_through_stop_fills_at_open(self):
        rows = list(BASE)
        rows[4] = (103, 103, 101, 102)  # 開盤 103 已低於停損 105
        t = self.run_base(rows).trades.iloc[0]
        self.assertEqual((t.exit_price, t.exit_reason), (103, "stop_gap"))

    def test_stop_never_decreases(self):
        rng = np.random.default_rng(0)
        close = 100 + np.cumsum(rng.normal(0, 1, 300))
        rows = [(c, c + 1, c - 1, c) for c in close]
        res = self.run_base(rows, entry_idx=5, dist=8.0)
        eq = res.equity
        block = (eq.shares == 0).cumsum()  # 每段連續持倉為一組
        held = eq[eq.shares > 0]
        self.assertGreater(len(held), 0)
        for _, g in held.groupby(block[eq.shares > 0]):
            self.assertTrue((g.stop.diff().dropna() >= -1e-9).all())

    def test_no_trade_when_limit_up_locked(self):
        rows = list(BASE)
        rows[2] = (110, 110, 110, 110)  # 一價漲停，買不到
        res = self.run_base(rows)
        self.assertEqual(len(res.trades), 0)
        self.assertEqual(res.equity.equity.iloc[-1], CAP)

    def test_limit_down_locked_defers_sell(self):
        rows = list(BASE)
        rows[4] = (99, 99, 99, 99)       # 110 → 99 跌停一價，賣不掉
        rows[5] = (97, 98, 96, 97)       # 下一日開盤 97 賣出
        t = self.run_base(rows).trades.iloc[0]
        self.assertEqual((t.exit_date, t.exit_price), (pd.Timestamp("2024-01-08"), 97))

    def test_forced_exit_at_end(self):
        rows = BASE[:4]
        t = self.run_base(rows).trades.iloc[0]
        self.assertEqual((t.exit_price, t.exit_reason), (110, "forced_exit"))


class TestNoLookAhead(unittest.TestCase):
    def test_future_change_does_not_alter_past(self):
        rng = np.random.default_rng(1)
        close = 100 + np.cumsum(rng.normal(0, 1, 200))
        rows = [(c, c + 1, c - 1, c) for c in close]
        a, b = bars(rows), bars(rows)
        k = 120
        b.loc[k + 1:, ["open", "high", "low", "close"]] *= 1.5
        sa, sb = strategy.ma_cross_atr_trail(a), strategy.ma_cross_atr_trail(b)
        pd.testing.assert_frame_equal(sa.iloc[: k + 1], sb.iloc[: k + 1])
        ea, eb = engine.run(a, sa, CAP).equity, engine.run(b, sb, CAP).equity
        pd.testing.assert_frame_equal(ea.iloc[: k + 1], eb.iloc[: k + 1])


class TestMetrics(unittest.TestCase):
    def test_summary_values(self):
        trades = pd.DataFrame({"pnl": [300.0, -100.0, 200.0, -100.0]})
        equity = pd.DataFrame({"equity": [1000.0, 1300.0, 1200.0, 1400.0, 1300.0]})
        s = metrics.summarize(trades, equity, 1000.0)
        self.assertAlmostEqual(s["net_profit"], 300)
        self.assertAlmostEqual(s["return_pct"], 30)
        self.assertAlmostEqual(s["mdd_pct"], (1200 - 1300) / 1300 * 100)
        self.assertAlmostEqual(s["win_rate_pct"], 50)
        self.assertAlmostEqual(s["profit_factor"], 2.5)
        self.assertEqual(s["trades"], 4)
        self.assertAlmostEqual(s["payoff_ratio"], 2.5)


if __name__ == "__main__":
    unittest.main()
