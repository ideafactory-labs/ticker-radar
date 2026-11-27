"""Offline tests for the gist dials: hand-built series with known answers."""
import datetime as dt
import unittest

from radar import macro


def daily(start, values):
    d0 = dt.date.fromisoformat(start)
    return [((d0 + dt.timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def months(start_year, n, fn):
    out = []
    for i in range(n):
        y, m = start_year + i // 12, i % 12 + 1
        out.append((f"{y}-{m:02d}-01", float(fn(i))))
    return out


class Helpers(unittest.TestCase):
    def test_percentile_and_months(self):
        self.assertEqual(macro.percentile([1, 2, 3, 4], 3), 75.0)
        self.assertEqual(macro.percentile([1, 2, 3, 4], 0), 0.0)
        self.assertEqual(macro.months_between("2024-11-15", "2026-02-01"), 15)

    def test_monthly_mean_and_last(self):
        s = [("2026-01-05", 1.0), ("2026-01-20", 3.0), ("2026-02-02", 5.0)]
        self.assertEqual(macro.monthly_mean(s), [("2026-01-01", 2.0), ("2026-02-01", 5.0)])
        self.assertEqual(macro.monthly_last(s), [("2026-01-20", 3.0), ("2026-02-02", 5.0)])

    def test_downsample_keeps_last_point(self):
        s = [(f"2000-01-{i % 28 + 1:02d}", float(i)) for i in range(1000)]
        d = macro.downsample(s, 100)
        self.assertLessEqual(len(d), 101)
        self.assertEqual(d[-1][1], 999.0)


class Curve(unittest.TestCase):
    def test_inverted_run(self):
        c = macro.curve_state(daily("2026-01-01", [0.5, 0.2, -0.1, -0.2, -0.3]))
        self.assertTrue(c["inverted"])
        self.assertEqual((c["days_inverted"], c["spread"], c["months_since_inverted"]), (3, -0.3, 0))

    def test_uninverted_remembers_when(self):
        c = macro.curve_state(daily("2025-01-01", [-0.2] * 10 + [0.4] * 400))
        self.assertFalse(c["inverted"])
        self.assertEqual(c["days_inverted"], 0)
        self.assertEqual(c["last_inverted"], "2025-01-10")
        self.assertEqual(c["months_since_inverted"], 13)

    def test_never_inverted(self):
        c = macro.curve_state(daily("2026-01-01", [1.0, 1.1]))
        self.assertIsNone(c["last_inverted"])
        self.assertIsNone(c["months_since_inverted"])

    def test_record_hits_false_alarms_pending_and_missed(self):
        # 30 years of monthly data, 1990-2019. Inversions: 1992 (recession 1993-06: hit),
        # 2000 (no recession: false alarm), 2019-06.. (too recent to judge: pending).
        # A recession in 2010-03 with no inversion before it: missed.
        def spread(i):
            y = 1990 + i // 12
            return -0.5 if y == 1992 or y == 2000 or (y == 2019 and i % 12 >= 5) else 1.0
        sp = months(1990, 360, spread)
        rec = months(1990, 360, lambda i: 1 if (1990 + i // 12, i % 12 + 1) in {(1993, 6), (1993, 7), (2010, 3), (2010, 4)} else 0)
        r = macro.inversion_record(sp, rec)
        self.assertEqual([e["outcome"] for e in r["episodes"]], ["recession", "false alarm", "pending"])
        self.assertEqual(r["episodes"][0]["lead_months"], 17)   # 1992-01 -> 1993-06
        self.assertEqual((r["hits"], r["false_alarms"], r["pending"], r["recessions"]), (1, 1, 1, 2))
        self.assertEqual(r["missed"], ["2010-03-01"])

    def test_nearby_inversions_are_one_episode(self):
        sp = months(2000, 60, lambda i: -0.2 if i in (10, 11, 15, 16) else 0.8)
        r = macro.inversion_record(sp, months(2000, 60, lambda i: 0))
        self.assertEqual(len(r["episodes"]), 1)
        self.assertEqual((r["episodes"][0]["start"], r["episodes"][0]["end"]), ("2000-11-01", "2001-05-01"))


class Dials(unittest.TestCase):
    def quarters(self, n, fn):
        out = []
        for i in range(n):
            out.append((f"{1970 + i // 4}-{(i % 4) * 3 + 1:02d}-01", float(fn(i))))
        return out

    def test_buffett_units_and_percentile(self):
        # equities in millions, GDP in billions: 30,000,000 / 1000 / 20,000 = 150%
        eq = self.quarters(100, lambda i: 10_000_000 + i * 200_000)
        gdp = self.quarters(100, lambda i: 20_000)
        self.assertAlmostEqual(macro.buffett_series(eq, gdp)[-1][1], (10_000_000 + 99 * 200_000) / 1000 / 20_000 * 100)
        d = macro.valuation_dial(eq, gdp)
        self.assertEqual(d["status"], "red")          # the latest reading is the highest ever
        self.assertEqual(d["value"], "149%")
        self.assertIn("higher than 100% of readings since 1970", d["headline"])
        flat = macro.valuation_dial(self.quarters(100, lambda i: 20_000_000 if i < 99 else 10_000_000), gdp)
        self.assertEqual(flat["status"], "green")     # the latest reading is the lowest

    def test_buffett_needs_history(self):
        with self.assertRaises(ValueError):
            macro.valuation_dial(self.quarters(8, lambda i: 1e7), self.quarters(8, lambda i: 2e4))

    def recession_inputs(self, tail):
        sp = daily("2015-01-01", [1.5] * 3000 + tail)
        rec = months(2015, 150, lambda i: 0)
        return sp, rec

    def test_recession_dial_states(self):
        sp, rec = self.recession_inputs([1.2] * 30)
        calm = macro.recession_dial(sp, rec, [("2026-10-01", 0.10)], [("2026-09-01", 1.2)])
        self.assertEqual((calm["status"], calm["value"]), ("green", "Low"))
        sp, rec = self.recession_inputs([-0.3] * 30)
        inv = macro.recession_dial(sp, rec, [("2026-10-01", 0.10)], None)
        self.assertEqual(inv["status"], "amber")
        self.assertIn("inverted (-0.30 points, 30 trading days in a row)", inv["headline"])
        sp, rec = self.recession_inputs([-0.3] * 30 + [0.4] * 200)
        after = macro.recession_dial(sp, rec, [("2026-10-01", 0.10)], None)
        self.assertEqual(after["status"], "amber")    # un-inverted 6 months ago still counts
        self.assertIn("no longer inverted", after["headline"])
        sahm = macro.recession_dial(*self.recession_inputs([1.2] * 30), [("2026-10-01", 0.53)], None)
        self.assertEqual(sahm["status"], "red")
        prob = macro.recession_dial(*self.recession_inputs([1.2] * 30), [("2026-10-01", 0.0)], [("2026-09-01", 62.0)])
        self.assertEqual(prob["status"], "red")

    def test_credit_inflation_jobs_thresholds(self):
        vix = daily("2020-01-01", [15] * 100)
        self.assertEqual(macro.credit_dial([("2026-11-20", -0.45)], vix)["status"], "green")
        self.assertEqual(macro.credit_dial([("2026-11-20", 0.1)], vix)["status"], "amber")
        self.assertEqual(macro.credit_dial([("2026-11-20", -0.2)], daily("2020-01-01", [15] * 99 + [40]))["status"], "red")
        cpi = months(2020, 60, lambda i: 100 * 1.03 ** (i / 12))
        d = macro.inflation_dial(cpi, cpi)
        self.assertEqual((d["status"], d["value"]), ("amber", "3.0%"))
        self.assertEqual(macro.inflation_dial(cpi, months(2020, 60, lambda i: 100 * 1.02 ** (i / 12)))["status"], "green")
        self.assertEqual(macro.inflation_dial(cpi, months(2020, 60, lambda i: 100 * 1.05 ** (i / 12)))["status"], "red")
        un = months(2020, 60, lambda i: 4.0)
        pay = months(2020, 60, lambda i: 150_000 + 100 * i)
        j = macro.jobs_dial(un, [("2026-10-01", 0.35)], pay)
        self.assertEqual(j["status"], "amber")
        self.assertEqual(j["parts"][2]["value"], "+100k")

    def test_market_dial_and_breadth(self):
        spy = {"vs_ma200": 5.0, "from_high": -1.0, "chg_ytd": 12.0, "as_of": "2026-11-25"}
        self.assertEqual(macro.market_dial(spy, 62.0, 4000)["status"], "green")
        self.assertEqual(macro.market_dial(spy, 41.0, 4000)["status"], "amber")   # index up, most stocks not
        self.assertEqual(macro.market_dial({**spy, "vs_ma200": -4.0}, 30.0, 4000)["status"], "red")
        prices = {f"S{i}": {"vs_ma200": (1.0 if i < 60 else -1.0)} for i in range(100)}
        prices["ETF1"] = {"vs_ma200": -9.0}
        self.assertEqual(macro.breadth(prices, lambda t: t.startswith("S")), (60.0, 100))
        self.assertEqual(macro.breadth({"A": {"vs_ma200": 1.0}}, lambda t: True), (None, 1))  # too few to mean anything

    def test_summary_orders_by_concern(self):
        dials = [{"key": "market", "status": "green"}, {"key": "valuation", "status": "red"}, {"key": "inflation", "status": "amber"}]
        self.assertEqual(macro.summary(dials), "In the US, stocks are very expensive, inflation is above target and the market is in an uptrend.")


if __name__ == "__main__":
    unittest.main()
