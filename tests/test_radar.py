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
        # Field names as TreasuryDirect serves them (seen in the live probe): the auction day is
        # competitiveAuctionCloseDate, and the result rate is highRate.
        up = parse.treasury_auctions(fx("auctions_announced.json"), today=TODAY)
        self.assertEqual([a["status"] for a in up], ["Upcoming"] * 3)
        self.assertEqual(up[0]["auction_date"], "2026-11-30")
        done = parse.treasury_auctions(fx("auctions_results.json"), today=TODAY)
        last = done[-1]
        self.assertEqual((last["security"], last["auction_date"], last["status"]), ("4-Week Bill", "2026-11-25", "Result"))
        self.assertEqual((last["high_rate"], last["investment_rate"], last["bid_to_cover"]), (4.115, 4.197, 2.64))

    def test_auction_without_a_date_is_skipped_not_guessed(self):
        self.assertEqual(parse.treasury_auctions('[{"securityTerm": "4-Week", "announcementDate": "2026-11-24T00:00:00"}]', today=TODAY), [])


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


class Directory(unittest.TestCase):
    def test_nasdaq_trader_files(self):
        rows = {r["ticker"]: r for r in parse.nasdaq_symbol_directory(fx("nasdaq_listed.txt"), "nasdaq")}
        self.assertEqual(rows["NVDA"]["name"], "NVIDIA Corporation")
        self.assertEqual(rows["QQQ"]["exchange"], "ETF")
        self.assertNotIn("ZTEST", rows)   # test issue
        self.assertNotIn("ABCDW", rows)   # warrant
        other = {r["ticker"]: r for r in parse.nasdaq_symbol_directory(fx("other_listed.txt"), "other")}
        self.assertEqual(other["BAC"]["exchange"], "NYSE")
        self.assertEqual(other["SPY"]["exchange"], "ETF")


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
    def test_yahoo_and_summary(self):
        s = parse.price_summary(parse.yahoo_chart(fx("yahoo__NVDA.json")))
        self.assertEqual(s["as_of"], "2026-11-24")
        self.assertAlmostEqual(s["change_pct"], 6.8, delta=0.05)
        self.assertEqual(len(s["spark"]), 30)
        self.assertLessEqual(s["low_52w"], s["close"])
        self.assertGreaterEqual(s["high_52w"], s["close"])

    def test_no_data_is_an_error_not_a_zero(self):
        with self.assertRaises(parse.ParseError):
            parse.yahoo_chart(fx("yahoo__MSFT.json"))

    def test_spark_batch(self):
        got = parse.yahoo_spark(fx("spark_sample.json"))
        self.assertEqual(got["SPY"][-1][0], "2026-11-24")
        flat = parse.yahoo_spark('{"AAPL": {"timestamp": [1795640400, 1795726800], "close": [1.0, 2.0]}}')
        self.assertEqual([c for _, c in flat["AAPL"]], [1.0, 2.0])

    def test_yahoo_chart(self):
        s = parse.yahoo_chart(fx("yahoo__SPY.json"))
        self.assertEqual(s[-1][0], "2026-11-24")


def _series(closes, end=dt.date(2026, 11, 24)):
    """Daily closes on consecutive calendar days ending at `end` (enough for signal maths)."""
    n = len(closes)
    return [((end - dt.timedelta(days=n - 1 - i)).isoformat(), float(c)) for i, c in enumerate(closes)]


class Signals(unittest.TestCase):
    def test_period_changes_and_range(self):
        closes = [100.0] * 239 + [100, 100, 100, 100, 100, 110, 110, 110, 110, 110, 121]  # 250 days
        s = parse.price_signals(_series(closes))
        self.assertEqual(s["chg_1w"], 10.0)            # 121 vs 110 five days earlier
        self.assertEqual(s["chg_1m"], 21.0)            # 121 vs 100
        self.assertEqual(s["chg_3m"], 21.0)
        self.assertEqual(s["from_high"], 0.0)
        self.assertEqual(s["from_low"], 21.0)
        self.assertEqual(s["vs_ma50"], 19.31)          # 121 vs the 50-day average 101.42
        self.assertGreater(s["week_z"], 3)             # a very unusual week for a flat stock

    def test_ytd_uses_last_close_of_previous_year(self):
        s = parse.price_signals([("2025-12-30", 90.0), ("2025-12-31", 100.0), ("2026-01-02", 104.0), ("2026-01-05", 110.0)])
        self.assertEqual(s["chg_ytd"], 10.0)
        only_this_year = parse.price_signals([("2026-01-02", 104.0), ("2026-01-05", 110.0)])
        self.assertIsNone(only_this_year["chg_ytd"])

    def test_short_history_gives_none_not_a_guess(self):
        s = parse.price_signals(_series([10, 11, 12]))
        for k in ("chg_1w", "chg_1m", "chg_3m", "vs_ma50", "vs_ma200", "cross", "week_z"):
            self.assertIsNone(s[k], k)
        self.assertEqual(s["from_high"], 0.0)

    def test_golden_and_death_cross(self):
        down_then_up = [200 - i * 0.5 for i in range(200)] + [100 + i * 4 for i in range(1, 41)]
        crosses = [parse.price_signals(_series(down_then_up[:n]))["cross"] for n in range(210, 241)]
        self.assertIn("golden", crosses)
        self.assertNotIn("death", crosses)
        up_then_down = [100 + i * 0.5 for i in range(200)] + [200 - i * 4 for i in range(1, 41)]
        self.assertIn("death", [parse.price_signals(_series(up_then_down[:n]))["cross"] for n in range(210, 241)])
        self.assertIsNone(parse.price_signals(_series([100.0 + i for i in range(250)]))["cross"])  # steady uptrend


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


class Stamp(unittest.TestCase):
    def test_versions_both_assets_and_records_the_build(self):
        from radar import stamp
        html = (pathlib.Path(__file__).parent.parent / "site" / "index.html").read_text()
        out = stamp.stamp(html, "abc123-9")
        self.assertIn('src="app.js?v=abc123-9"', out)
        self.assertIn('href="style.css?v=abc123-9"', out)
        self.assertIn('<meta name="build" content="abc123-9">', out)
        again = stamp.stamp(out, "def456-10")   # re-stamping replaces, never stacks
        self.assertIn('src="app.js?v=def456-10"', again)
        self.assertEqual(again.count('<meta name="build"'), 1)

    def test_rejects_unsafe_versions(self):
        from radar import stamp
        with self.assertRaises(ValueError):
            stamp.stamp('<script src="app.js"></script><link href="style.css">', 'x"><script>')


class FullBuild(unittest.TestCase):
    def test_build_from_fixtures(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d)
            b = build.Build(out, build.Source(FX), TODAY, None)
            health = b.run()["sources"]
            for name in ("bills", "curve", "auctions", "indicators", "search", "prices", "ipos", "calendar"):
                self.assertTrue(health[name]["ok"], (name, health[name]))
            # A ticker the price source has no data for is missing, never written as zero.
            self.assertGreater(health["prices"]["core_missing_count"], 0)
            prices = json.loads((out / "prices.json").read_text())["data"]
            self.assertNotIn("MSFT", prices)
            self.assertAlmostEqual(prices["NVDA"]["change_pct"], 6.8, delta=0.05)
            self.assertEqual(prices["SPY"]["source"], "yahoo")
            self.assertAlmostEqual(prices["NVDA"]["rel_3m"], prices["NVDA"]["chg_3m"] - prices["SPY"]["chg_3m"], places=2)
            gist = json.loads((out / "gist.json").read_text())["data"]
            self.assertEqual({d["key"]: d["status"] for d in gist["dials"]},
                             {"valuation": "red", "recession": "green", "credit": "green", "inflation": "amber", "jobs": "green", "market": "green"})
            self.assertEqual(gist["missing"], {})
            for d in gist["dials"]:   # every dial and every row on the page can be explained
                self.assertTrue(d["help"]["what"] and d["help"]["why"], d["key"])
                self.assertIsNotNone(d["trend"], d["key"])
                for part in d["parts"]:
                    self.assertTrue(part["help"], f"{d['key']}: no help for {part['label']!r}")
            infl = next(d for d in gist["dials"] if d["key"] == "inflation")
            self.assertEqual((infl["value"], infl["sub"]), ("2.9%", "core 3.1%"))
            self.assertTrue(gist["summary"].startswith("In the US, stocks are very expensive, inflation is above target"))
            rec = next(d for d in gist["dials"] if d["key"] == "recession")["record"]
            self.assertEqual((rec["hits"], rec["recessions"], rec["false_alarms"]), (3, 3, 1))
            self.assertEqual(gist["recessions"][0], ["2001-03-01", "2001-12-01"])
            glance = json.loads((out / "glance.json").read_text())["data"]
            self.assertEqual([g["ticker"] for g in glance], ["SPY", "VEA", "AGG", "GLD"])  # those with fixtures, in display order
            self.assertEqual(glance[0]["label"], "US stocks")
            search = json.loads((out / "search.json").read_text())["data"]
            self.assertIn(["BAC", "Bank of America Corporation", "NYSE"], search)
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
            # Only the symbol directory is available.
            (pathlib.Path(empty) / "nasdaq_listed.txt").write_text(fx("nasdaq_listed.txt"))
            b = build.Build(pathlib.Path(d), build.Source(pathlib.Path(empty)), TODAY, None)
            health = b.run()["sources"]
            self.assertTrue(health["search"]["ok"])
            self.assertFalse(health["bills"]["ok"])
            self.assertIn("error", health["bills"])
            self.assertTrue((pathlib.Path(d) / "health.json").exists())

    def test_one_missing_series_drops_only_its_dial(self):
        import shutil
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as partial:
            shutil.copytree(FX, partial, dirs_exist_ok=True)
            (pathlib.Path(partial) / "fred_full__NFCI.csv").unlink()
            build.Build(pathlib.Path(d), build.Source(pathlib.Path(partial)), TODAY, None).run()
            gist = json.loads((pathlib.Path(d) / "gist.json").read_text())["data"]
            self.assertEqual(list(gist["missing"]), ["credit"])
            self.assertEqual(len(gist["dials"]), 5)
            self.assertNotIn("credit", gist["summary"])


if __name__ == "__main__":
    unittest.main()
