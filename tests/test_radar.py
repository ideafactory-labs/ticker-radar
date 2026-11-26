"""Offline tests for the data pipeline: parsers against recorded-format fixtures, and a full
build from fixtures. No network is touched."""
import datetime as dt
import json
import pathlib
import tempfile
import unittest

from radar import build, parse

FX = pathlib.Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 11, 25)


def fx(name: str) -> str:
    return (FX / name).read_text()


class Treasury(unittest.TestCase):
    def test_bill_rates_use_coupon_equivalent_newest_first(self):
        rows = parse.treasury_bill_rates(fx("bill_rates__2026.csv"))
        self.assertEqual(rows[0]["date"], "2026-11-24")
        self.assertEqual(rows[0]["rates"]["13"], 4.15)  # coupon equivalent, not 4.08 bank discount
        self.assertEqual(sorted(rows[0]["rates"], key=int), ["4", "6", "8", "13", "17", "26", "52"])

    def test_changes_look_back_by_calendar_days(self):
        rows = parse.treasury_bill_rates(fx("bill_rates__2026.csv"))
        self.assertAlmostEqual(parse.ago(rows, 7, "rates", "4"), 4.31)
        self.assertAlmostEqual(parse.ago(rows, 28, "rates", "4"), 4.34)

    def test_yield_curve_requires_2y_and_10y(self):
        rows = parse.treasury_yield_curve(fx("yield_curve__2026.csv"))
        self.assertEqual(rows[0]["yields"]["10 Yr"], 4.21)
        with self.assertRaises(parse.ParseError):
            parse.treasury_yield_curve('Date,"1 Mo"\n11/24/2026,4.3\n')

    def test_auctions_upcoming_and_results(self):
        up = parse.treasury_auctions(fx("auctions_announced.json"), today=TODAY)
        self.assertEqual([a["status"] for a in up], ["Upcoming"] * 3)
        done = parse.treasury_auctions(fx("auctions_results.json"), today=TODAY)
        self.assertEqual(done[-1]["high_rate"], 4.31)
        self.assertEqual(done[-1]["security"], "4-Week Bill")


class Fred(unittest.TestCase):
    def test_csv_skips_missing_values(self):
        obs = parse.fred_csv(fx("fred__EFFR.csv"), "EFFR")
        self.assertEqual(obs[-1], ("2026-11-23", 4.33))  # trailing "." dropped

    def test_old_and_new_header_names(self):
        self.assertEqual(parse.fred_csv("DATE,X\n2026-01-01,1\n", "X"), [("2026-01-01", 1.0)])
        with self.assertRaises(parse.ParseError):
            parse.fred_csv("<html>blocked</html>\n", "X")

    def test_transforms(self):
        cpi = parse.indicator(parse.fred_csv(fx("fred__CPIAUCSL.csv"), "CPIAUCSL"), "yoy")
        self.assertAlmostEqual(cpi["latest"], (1.0025 ** 12 - 1) * 100, places=1)
        pay = parse.indicator(parse.fred_csv(fx("fred__PAYEMS.csv"), "PAYEMS"), "diff")
        self.assertEqual(pay["latest"], 150)
        self.assertEqual(pay["latest_date"], "2026-10-01")


class Sec(unittest.TestCase):
    def test_company_tickers(self):
        rows = parse.sec_company_tickers(fx("sec_tickers.json"))
        self.assertEqual(rows[0], {"ticker": "NVDA", "name": "NVIDIA CORP", "exchange": "Nasdaq", "cik": 1045810})

    def test_latest_filings_feed(self):
        f = parse.sec_current_filings(fx("sec_filings__S-1.xml"))
        self.assertEqual([x["form"] for x in f], ["S-1", "S-1/A"])
        self.assertEqual(f[0]["company"], "Brookway Payments Inc.")
        self.assertEqual(f[0]["cik"], "1999001")
        self.assertEqual(f[0]["filed"], "2026-11-14")


class Prices(unittest.TestCase):
    def test_stooq_and_summary(self):
        s = parse.price_summary(parse.stooq_daily(fx("stooq__NVDA.csv")))
        self.assertEqual(s["as_of"], "2026-11-24")
        self.assertAlmostEqual(s["change_pct"], 6.8, delta=0.05)
        self.assertEqual(len(s["spark"]), 30)
        self.assertLessEqual(s["low_52w"], s["close"])
        self.assertGreaterEqual(s["high_52w"], s["close"])

    def test_stooq_no_data_is_an_error_not_a_zero(self):
        with self.assertRaises(parse.ParseError):
            parse.stooq_daily(fx("stooq__MSFT.csv"))

    def test_yahoo_chart(self):
        s = parse.yahoo_chart(fx("yahoo__SPY.json"))
        self.assertEqual(s[-1][0], "2026-11-24")


class Ipos(unittest.TestCase):
    def test_calendar_sections(self):
        rows = {r["ticker"]: r for r in parse.nasdaq_ipo_calendar(fx("nasdaq_ipos__2026-11.json"))}
        self.assertEqual(rows["CRLN"]["status"], "priced")
        self.assertEqual(rows["CRLN"]["trade_date"], "2026-06-03")
        self.assertEqual(rows["NWBT"]["status"], "upcoming")
        self.assertEqual(rows["NWBT"]["price"], "18.00-21.00")
        self.assertEqual(rows["BRKW"]["filed_date"], "2026-11-14")

    def test_timeline_estimates(self):
        steps = {s["key"]: s for s in parse.ipo_timeline({"status": "priced", "trade_date": "2026-06-03"})}
        self.assertEqual(steps["quiet"]["date"], "2026-06-28")
        self.assertEqual(steps["lockup"]["date"], "2026-11-30")
        self.assertTrue(steps["lockup"]["est"])
        self.assertFalse(steps["listed"]["est"])


class Calendars(unittest.TestCase):
    def test_fomc_page_including_meeting_across_months(self):
        m = parse.fomc_meetings(fx("fomc.html"))
        self.assertIn({"start": "2026-12-08", "end": "2026-12-09"}, m)
        self.assertIn({"start": "2027-01-31", "end": "2027-02-01"}, m)

    def test_ics_folded_lines_and_datetimes(self):
        ev = parse.ics_events(fx("bls_ics.ics"))
        self.assertEqual(ev[0], {"date": "2026-12-10", "summary": "Consumer Price Index for November 2026"})
        self.assertEqual(ev[1]["date"], "2026-12-04")


class FullBuild(unittest.TestCase):
    def test_build_from_fixtures(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d)
            b = build.Build(out, build.Source(FX), TODAY, None)
            health = b.run()["sources"]
            for name in ("bills", "curve", "auctions", "indicators", "search", "prices", "ipos", "filings", "calendar"):
                self.assertTrue(health[name]["ok"], (name, health[name]))
            # A ticker the price source has no data for is missing, never written as zero.
            self.assertIn("MSFT", health["prices"]["missing"])
            prices = json.loads((out / "prices.json").read_text())["data"]
            self.assertNotIn("MSFT", prices)
            self.assertAlmostEqual(prices["NVDA"]["change_pct"], 6.8, delta=0.05)
            self.assertEqual(prices["SPY"]["source"], "yahoo")
            # Every section file says where it came from and when.
            for f in ("bills", "indicators", "ipos", "prices"):
                doc = json.loads((out / f"{f}.json").read_text())
                self.assertTrue(doc["source"] and doc["as_of"] and doc["fetched_at"])
                self.assertFalse(doc["stale"])
            self.assertFalse(json.loads((out / "prices.json").read_text())["official"])
            cal = json.loads((out / "calendar.json").read_text())["data"]
            kinds = {e["kind"] for e in cal}
            self.assertEqual(kinds, {"fed", "data", "auction", "ipo"})
            self.assertTrue((out / "calendar.ics").read_text().startswith("BEGIN:VCALENDAR"))

    def test_one_failing_source_does_not_stop_the_rest(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as empty:
            # Only the SEC ticker list is available.
            (pathlib.Path(empty) / "sec_tickers.json").write_text(fx("sec_tickers.json"))
            b = build.Build(pathlib.Path(d), build.Source(pathlib.Path(empty)), TODAY, None)
            health = b.run()["sources"]
            self.assertTrue(health["search"]["ok"])
            self.assertFalse(health["bills"]["ok"])
            self.assertIn("error", health["bills"])
            self.assertTrue((pathlib.Path(d) / "health.json").exists())


if __name__ == "__main__":
    unittest.main()
