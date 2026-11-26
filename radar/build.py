"""Build site/data/*.json from the live sources (or from recorded fixtures in tests).

    python3 -m radar.build --out site/data                 # live (GitHub runner)
    python3 -m radar.build --out site/data --fixtures tests/fixtures   # offline

Every source is fetched independently: one failing source never stops the others. Each
output file carries {source, official, fetched_at, as_of, stale, data}. If a source fails
and PREVIOUS_BASE is set (the live Pages URL), the last published file is reused and
marked stale instead of blanking that section. health.json records what happened.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import traceback

from . import net, parse

HERE = pathlib.Path(__file__).parent

URLS = {
    "bill_rates": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_bill_rates&field_tdr_date_value={year}&page&_format=csv",
    "yield_curve": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv",
    "auctions_announced": "https://www.treasurydirect.gov/TA_WS/securities/announced?format=json&type=Bill",
    "auctions_results": "https://www.treasurydirect.gov/TA_WS/securities/auctioned?format=json&type=Bill&days=21",
    "fred": "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}&cosd={start}",
    "sec_tickers": "https://www.sec.gov/files/company_tickers_exchange.json",
    "sec_filings": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb=&owner=include&count=100&output=atom",
    "nasdaq_listed": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "other_listed": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    "yahoo": "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d",
    "yahoo_spark": "https://query1.finance.yahoo.com/v8/finance/spark?symbols={syms}&range=1y&interval=1d",
    "nasdaq_ipos": "https://api.nasdaq.com/api/ipo/calendar?date={month}",
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "fred_calendar": "https://fred.stlouisfed.org/releases/calendar?rid={rid}",
    "bls_ics": "https://www.bls.gov/schedule/news_release/bls.ics",
}

# Indicator catalog: FRED series id -> (label, unit, transform, on by default)
INDICATORS = {
    "CPIAUCSL": ("CPI inflation, y/y", "%", "yoy", True),
    "CPILFESL": ("Core CPI, y/y", "%", "yoy", True),
    "PCEPI": ("PCE inflation, y/y", "%", "yoy", False),
    "PCEPILFE": ("Core PCE, y/y", "%", "yoy", False),
    "UNRATE": ("Unemployment rate", "%", "level", True),
    "PAYEMS": ("Nonfarm payrolls, monthly change", "k", "diff", True),
    "ICSA": ("Initial jobless claims", "k", "thousands", False),
    "EFFR": ("Effective fed funds rate", "%", "level", True),
    "DGS10": ("10-year Treasury yield", "%", "level", False),
    "T10Y2Y": ("10y – 2y spread", "pp", "level", True),
    "A191RL1Q225SBEA": ("Real GDP growth, q/q annualized", "%", "level", True),
    "MORTGAGE30US": ("30-year mortgage rate", "%", "level", False),
    "RSAFS": ("Retail sales, m/m", "%", "mom", False),
    "INDPRO": ("Industrial production, y/y", "%", "yoy", False),
    "UMCSENT": ("Consumer sentiment (UMich)", "", "level", False),
    "HOUST": ("Housing starts, annualized", "k", "level", False),
}

# ETFs people commonly watch; SEC's company list does not cover most funds.
ETFS = {
    "SPY": "SPDR S&P 500 ETF Trust", "VOO": "Vanguard S&P 500 ETF", "IVV": "iShares Core S&P 500 ETF",
    "QQQ": "Invesco QQQ Trust", "VTI": "Vanguard Total Stock Market ETF", "IWM": "iShares Russell 2000 ETF",
    "DIA": "SPDR Dow Jones Industrial Average ETF", "VEA": "Vanguard FTSE Developed Markets ETF",
    "VWO": "Vanguard FTSE Emerging Markets ETF", "SGOV": "iShares 0-3 Month Treasury Bond ETF",
    "BIL": "SPDR Bloomberg 1-3 Month T-Bill ETF", "SHV": "iShares Short Treasury Bond ETF",
    "TLT": "iShares 20+ Year Treasury Bond ETF", "IEF": "iShares 7-10 Year Treasury Bond ETF",
    "AGG": "iShares Core US Aggregate Bond ETF", "BND": "Vanguard Total Bond Market ETF",
    "GLD": "SPDR Gold Shares", "SLV": "iShares Silver Trust", "XLF": "Financial Select Sector SPDR",
    "XLK": "Technology Select Sector SPDR", "XLE": "Energy Select Sector SPDR", "XLV": "Health Care Select Sector SPDR",
    "KRE": "SPDR S&P Regional Banking ETF", "SMH": "VanEck Semiconductor ETF", "ARKK": "ARK Innovation ETF",
    "SCHD": "Schwab US Dividend Equity ETF", "VNQ": "Vanguard Real Estate ETF", "IBIT": "iShares Bitcoin Trust ETF",
}

UNIVERSE_STOCKS = int(os.environ.get("UNIVERSE_STOCKS", "400"))
LISTED = {"nasdaq", "nyse", "nyse american", "nyse arca", "cboe", "bats"}


def safe(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "-", ticker.upper())


class Source:
    """Live HTTP, or fixtures: tests/fixtures/<key>[__<arg>].<ext> stands in for each URL."""

    def __init__(self, fixtures: pathlib.Path | None):
        self.fixtures = fixtures

    def text(self, key: str, arg: str = "", *, tries: int = 3, headers: dict | None = None,
             timeout: float = 20, **fmt) -> str:
        if self.fixtures:
            stem = key + (f"__{arg}" if arg else "")
            hits = sorted(self.fixtures.glob(stem + ".*"))
            if not hits:
                raise net.FetchError(f"no fixture {stem}")
            return hits[0].read_text()
        url = URLS[key].format(**fmt)
        if key.startswith("sec_"):
            # SEC requires a User-Agent naming a contact; never invent one. Set the repository
            # variable SEC_USER_AGENT (e.g. "ticker-radar you@example.com") to enable SEC data.
            ua = os.environ.get("SEC_USER_AGENT", "").strip()
            if not ua:
                raise net.FetchError("SEC needs a contact User-Agent: set the SEC_USER_AGENT repository variable")
            headers = {**(headers or {}), "User-Agent": ua}
        try:
            body = net.text(url, tries=tries, headers=headers, timeout=timeout)
        except Exception as e:
            self._sample(key, url, f"<{e}>")
            raise
        self._sample(key, url, body)
        return body

    _sampled: dict = {}

    def _sample(self, key: str, url: str, body: str) -> None:
        """RADAR_SAMPLE=1: log the start of the first two responses per source (debugging formats)."""
        if not os.environ.get("RADAR_SAMPLE") or self._sampled.get(key, 0) >= 2:
            return
        self._sampled[key] = self._sampled.get(key, 0) + 1
        snippet = " ".join(body[:240].split())
        print(f"[sample] {key} {url[:110]} -> {snippet!r}", flush=True)


class Build:
    def __init__(self, out: pathlib.Path, src: Source, today: dt.date, previous_base: str | None):
        self.out, self.src, self.today, self.previous_base = out, src, today, previous_base
        self.health: dict[str, dict] = {}
        out.mkdir(parents=True, exist_ok=True)

    # ---- bookkeeping
    def write(self, name: str, payload) -> None:
        (self.out / name).write_text(json.dumps(payload, separators=(",", ":")))

    def section(self, name: str, source: str, official: bool, fn):
        t0 = time.monotonic()
        try:
            res = fn()
            data, as_of, count = res[:3]
            extra = res[3] if len(res) > 3 else {}
            self.write(f"{name}.json", {"source": source, "official": official, "fetched_at": _now(),
                                        "as_of": as_of, "stale": False, "data": data})
            self.health[name] = {"ok": True, "items": count, "as_of": as_of, "ms": _ms(t0), **extra}
        except Exception as e:  # keep going; record and fall back
            err = f"{type(e).__name__}: {e}"
            self.health[name] = {"ok": False, "error": err[:300], "ms": _ms(t0)}
            if os.environ.get("RADAR_DEBUG"):
                traceback.print_exc()
            self.fallback(f"{name}.json")

    def fallback(self, fname: str) -> None:
        name = fname.rsplit(".", 1)[0]
        if not self.previous_base or self.src.fixtures:
            return
        try:
            prev = json.loads(net.text(f"{self.previous_base.rstrip('/')}/data/{fname}", tries=1))
            prev["stale"] = True
            self.write(fname, prev)
            self.health[name]["fallback"] = "previous published data (marked stale)"
        except Exception as e:
            self.health[name]["fallback"] = f"none ({type(e).__name__})"

    # ---- sections
    def rates(self):
        def bills():
            series = []
            for y in (self.today.year, self.today.year - 1):
                try:
                    series += parse.treasury_bill_rates(self.src.text("bill_rates", str(y), year=y))
                except Exception:
                    if y == self.today.year and not series and self.today.month > 1:
                        raise
            series.sort(key=lambda r: r["date"], reverse=True)
            if not series:
                raise parse.ParseError("no bill rates")
            rows = []
            for w in sorted(series[0]["rates"], key=int):
                rows.append({"label": f"{w}-week bill", "yield": series[0]["rates"][w],
                             "w1": parse.ago(series, 7, "rates", w), "m1": parse.ago(series, 28, "rates", w)})
            return rows, series[0]["date"], len(rows)

        def curve():
            series = []
            for y in (self.today.year, self.today.year - 1):
                try:
                    series += parse.treasury_yield_curve(self.src.text("yield_curve", str(y), year=y))
                except Exception:
                    if y == self.today.year and not series and self.today.month > 1:
                        raise
            series.sort(key=lambda r: r["date"], reverse=True)
            if not series:
                raise parse.ParseError("no yield curve")
            pts = [{"tenor": c, "now": series[0]["yields"].get(c), "m1": parse.ago(series, 28, "yields", c)}
                   for c in parse.CURVE_COLS if c in series[0]["yields"]]
            return pts, series[0]["date"], len(pts)

        def auctions():
            raw_up, raw_done = self.src.text("auctions_announced"), self.src.text("auctions_results")
            up = parse.treasury_auctions(raw_up, today=self.today)
            done_all = parse.treasury_auctions(raw_done, today=self.today)
            done = [a for a in done_all if a["high_rate"] is not None][-8:]
            rows = [a for a in up if a["auction_date"] >= self.today.isoformat()] + list(reversed(done))
            diag = {"parsed_announced": len(up), "parsed_results": len(done_all)}
            if os.environ.get("RADAR_SAMPLE"):
                try:
                    rec = (json.loads(raw_done) or [{}])[0]
                    print("[diag] auction keys:", ",".join(rec.keys()), flush=True)
                    print("[diag] auction non-empty:", json.dumps({k: v for k, v in rec.items() if v not in ("", None)})[:1500], flush=True)
                except Exception as e:
                    print("[diag] auctions unreadable:", e, flush=True)
            if not rows:
                try:
                    first = (json.loads(raw_done) or [{}])[0]
                    diag["result_fields"] = {k: first.get(k) for k in first
                                             if any(w in k.lower() for w in ("term", "date", "rate", "type"))}
                except Exception as e:
                    diag["result_fields"] = f"unreadable: {e}"
            return rows, self.today.isoformat(), len(rows), diag

        self.section("bills", "U.S. Treasury, Daily Treasury Bill Rates", True, bills)
        self.section("curve", "U.S. Treasury, Daily Par Yield Curve Rates", True, curve)
        self.section("auctions", "TreasuryDirect auction data", True, auctions)

    def indicators(self):
        start = (self.today - dt.timedelta(days=365 * 4)).isoformat()

        def one(sid):
            label, unit, transform, default = INDICATORS[sid]
            obs = parse.fred_csv(self.src.text("fred", sid, id=sid, start=start), sid)
            return sid, {"id": sid, "label": label, "unit": unit, "default": default, **parse.indicator(obs, transform)}

        def run():
            out, errors = {}, {}
            with cf.ThreadPoolExecutor(4) as ex:
                for fut, sid in [(ex.submit(one, s), s) for s in INDICATORS]:
                    try:
                        k, v = fut.result()
                        out[k] = v
                    except Exception as e:
                        errors[sid] = str(e)[:120]
            if not out:
                raise parse.ParseError(f"no indicators: {errors}")
            latest = max(v["latest_date"] for v in out.values())
            return out, latest, len(out), ({"missing": errors} if errors else {})

        self.section("indicators", "FRED, Federal Reserve Bank of St. Louis", True, run)

    def universe_and_prices(self):
        listed: list[dict] = []

        def search():
            nonlocal listed
            rows, errs = [], []
            for key, kind in (("nasdaq_listed", "nasdaq"), ("other_listed", "other")):
                try:
                    rows += parse.nasdaq_symbol_directory(self.src.text(key, tries=2), kind)
                except Exception as e:
                    errs.append(f"{key}: {e}")
            source = "Nasdaq Trader symbol directory"
            if not rows:  # fall back to SEC's list when a contact is configured
                rows = [{**r, "etf": False} for r in parse.sec_company_tickers(self.src.text("sec_tickers"))
                        if (r["exchange"] or "").lower() in LISTED]
                source = "SEC company tickers"
            listed = rows
            seen, idx = set(), []
            for r in rows:
                if r["ticker"] not in seen:
                    seen.add(r["ticker"]); idx.append([r["ticker"], r["name"], r["exchange"] or ""])
            idx += [[t, n, "ETF"] for t, n in ETFS.items() if t not in seen]
            return idx, self.today.isoformat(), len(idx), {"from": source, **({"missing": errs} if errs else {})}

        self.section("search", "Nasdaq Trader symbol directory + a fixed ETF list", True, search)

        def prices():
            core = [t for ln in (HERE / "core_tickers.txt").read_text().splitlines()
                    if not ln.startswith("#") for t in ln.split()]
            core += [t for t in ETFS if t not in core]
            wide = [r["ticker"] for r in listed if not r.get("etf")] + [r["ticker"] for r in listed if r.get("etf") and r["ticker"] in ETFS]
            bundle: dict[str, dict] = {}
            notes: dict = {}

            def yahoo_sym(t):
                return t.replace(".", "-")

            # Phase 1: batch pricing of the whole listed universe (fast when Yahoo allows it).
            t0, budget = time.monotonic(), float(os.environ.get("BATCH_BUDGET_S", "300"))
            if wide and not self.src.fixtures:
                ordered = core + [t for t in wide if t not in set(core)]
                batches = [ordered[i:i + 20] for i in range(0, len(ordered), 20)]
                failures = 0
                for i, batch in enumerate(batches):
                    if time.monotonic() - t0 > budget or (failures >= 3 and not bundle):
                        notes["batch_stopped"] = f"after {i} of {len(batches)} batches"
                        break
                    try:
                        txt = self.src.text("yahoo_spark", syms=",".join(yahoo_sym(t) for t in batch), tries=1, timeout=15)
                        got = parse.yahoo_spark(txt)
                        back = {yahoo_sym(t): t for t in batch}
                        for sym, series in got.items():
                            if sym in back and len(series) >= 2:
                                bundle[back[sym]] = {"source": "yahoo", **parse.price_summary(series)}
                    except Exception as e:
                        failures += 1
                        notes["batch_error"] = str(e)[:160]
                notes["batch_priced"] = len(bundle)

            # Phase 2: one request per core ticker still missing, within its own budget.
            t1, budget2 = time.monotonic(), float(os.environ.get("CHART_BUDGET_S", "240"))
            todo = [t for t in core if t not in bundle]
            fails = 0

            def one(t):
                nonlocal fails
                if time.monotonic() - t1 > budget2 or (fails >= 8 and not bundle):
                    return t, None
                try:
                    txt = self.src.text("yahoo", t if self.src.fixtures else yahoo_sym(t), sym=yahoo_sym(t), tries=1, timeout=12)
                    return t, parse.price_summary(parse.yahoo_chart(txt))
                except Exception:
                    fails += 1
                    return t, None

            with cf.ThreadPoolExecutor(4) as ex:
                for t, summ in ex.map(one, todo):
                    if summ:
                        bundle[t] = {"source": "yahoo", **summ}
            if not bundle:
                raise parse.ParseError(f"no prices from Yahoo ({notes.get('batch_error', 'chart requests failed')})")
            as_of = max(v["as_of"] for v in bundle.values())
            missing = sorted(set(core) - set(bundle))
            detail = {**notes, "core_missing_count": len(missing), "core_missing": missing[:40], "wide_universe": len(wide)}
            # One file for every priced ticker, so the page never asks the server for the
            # tickers you watch: what you watch stays on your device.
            return dict(sorted(bundle.items())), as_of, len(bundle), detail

        self.section("prices", "Yahoo Finance daily closes", False, prices)

    def ipos(self):
        def run():
            months = [(self.today.replace(day=1) - dt.timedelta(days=31 * k)).strftime("%Y-%m") for k in (2, 1, 0)]
            nxt = (self.today.replace(day=28) + dt.timedelta(days=5)).strftime("%Y-%m")
            rows, errs = {}, []
            for m in months + [nxt]:
                try:
                    if errs and all("timed out" in e or "Timeout" in e for e in errs):
                        errs.append(f"{m}: skipped after timeouts")
                        continue
                    txt = self.src.text("nasdaq_ipos", m, month=m, tries=1, timeout=10, headers={"Accept": "application/json, text/plain, */*",
                                                                          "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
                    for r in parse.nasdaq_ipo_calendar(txt):
                        key = (r["ticker"] or r["name"]).upper()
                        if key not in rows or _rank(r) > _rank(rows[key]):
                            rows[key] = {**rows.get(key, {}), **{k: v for k, v in r.items() if v}}
                except Exception as e:
                    errs.append(f"{m}: {e}")
            if not rows:
                raise parse.ParseError("; ".join(errs) or "no IPOs")
            out = []
            for r in rows.values():
                r["steps"] = parse.ipo_timeline(r)
                out.append(r)
            out.sort(key=lambda r: (r.get("trade_date") or r.get("filed_date") or ""), reverse=True)
            return out[:60], self.today.isoformat(), len(out)

        self.section("ipos", "Nasdaq IPO calendar", False, run)
        # SEC filings below need SEC_USER_AGENT; without it the section is skipped quickly.

        def filings():
            out, errs = [], []
            for form in ("S-1", "F-1"):
                try:
                    out += [f for f in parse.sec_current_filings(self.src.text("sec_filings", form, form=form))
                            if f["form"] in (form, form + "/A")]
                except Exception as e:
                    errs.append(f"{form}: {e}")
            if len(errs) == 2:
                raise parse.ParseError("; ".join(errs))
            out.sort(key=lambda f: f["filed"], reverse=True)
            return out[:40], (out[0]["filed"] if out else self.today.isoformat()), len(out), ({"missing": errs} if errs else {})

        self.section("filings", "SEC EDGAR latest S-1/F-1 filings", True, filings)

    def calendar(self):
        events: list[dict] = []
        notes = {}
        horizon = (self.today + dt.timedelta(days=45)).isoformat()
        today = self.today.isoformat()
        try:
            meetings = parse.fomc_meetings(self.src.text("fomc", tries=2))
            notes["fomc"] = "federalreserve.gov"
        except Exception as e:
            meetings = json.loads((HERE / "fomc_fallback.json").read_text())["meetings"]
            notes["fomc"] = f"checked-in list (live page failed: {str(e)[:80]})"
        for m in meetings:
            events.append({"date": m["start"], "title": "FOMC meeting begins", "kind": "fed"})
            events.append({"date": m["end"], "title": "FOMC decision and press conference", "kind": "fed"})
        if os.environ.get("RADAR_SAMPLE") and not self.src.fixtures:
            for rid, name in ((10, "CPI"), (50, "Employment Situation")):
                try:
                    html = self.src.text("fred_calendar", str(rid), rid=rid, tries=1, timeout=15)
                    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
                    main = body[body.find('id="content-container"'):]
                    text = " ".join(re.sub(r"<[^>]+>", " ", main).split())
                    print(f"[diag] fred_calendar {name} text:", text[:1500], flush=True)
                    j = main.find("Consumer Price Index" if rid == 10 else "Employment Situation")
                    print(f"[diag] fred_calendar {name} html near release:", " ".join(main[max(j - 900, 0):j + 500].split()), flush=True)
                    print(f"[diag] fred_calendar {name} links:", sorted(set(re.findall(r'href="(/releases/calendar[^"]*)"', html)))[:25], flush=True)
                except Exception as e:
                    print(f"[diag] fred_calendar {name} failed: {e}", flush=True)
        keep = re.compile(r"Consumer Price Index|Employment Situation|Producer Price Index|Job Openings|"
                          r"Gross Domestic Product|Personal Income|Retail", re.I)
        try:
            for ev in parse.ics_events(self.src.text("bls_ics", tries=1, timeout=10)):
                if keep.search(ev["summary"]):
                    events.append({"date": ev["date"], "title": ev["summary"], "kind": "data"})
            notes["bls"] = "bls.gov release schedule"
        except Exception as e:
            notes["bls"] = f"unavailable ({str(e)[:80]})"
        for fname, kind in (("auctions.json", "auction"), ("ipos.json", "ipo")):
            p = self.out / fname
            if not p.exists():
                continue
            for a in json.loads(p.read_text())["data"]:
                if kind == "auction" and a["status"] == "Upcoming":
                    events.append({"date": a["auction_date"], "title": f"{a['security']} auction", "kind": "auction"})
                if kind == "ipo" and a.get("trade_date") and a["status"] == "upcoming":
                    events.append({"date": a["trade_date"], "title": f"{a['name']} expected to price", "kind": "ipo"})
                if kind == "ipo":
                    for s in a.get("steps", []):
                        if s["key"] == "lockup":
                            events.append({"date": s["date"], "title": f"{a['name']} lock-up ends (est.)", "kind": "ipo"})
        events = sorted({(e["date"], e["title"]): e for e in events if today <= e["date"] <= horizon}.values(),
                        key=lambda e: (e["date"], e["title"]))
        self.write("calendar.json", {"source": "Fed, BLS, TreasuryDirect, Nasdaq", "official": True,
                                     "fetched_at": _now(), "as_of": today, "stale": False, "notes": notes, "data": events})
        (self.out / "calendar.ics").write_text(_ics(events))
        self.health["calendar"] = {"ok": True, "items": len(events), "notes": notes}

    def run(self) -> dict:
        self.rates()
        self.indicators()
        self.universe_and_prices()
        self.ipos()
        self.calendar()
        summary = {"built_at": _now(), "sources": self.health}
        self.write("health.json", summary)
        return summary


def _rank(r):
    return {"priced": 3, "upcoming": 2, "filed": 1}.get(r.get("status"), 0)


def _ics(events) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ticker-radar//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Ticker Radar — key dates"]
    for e in events:
        d = e["date"].replace("-", "")
        uid = re.sub(r"[^a-z0-9]", "", (e["date"] + e["title"]).lower())[:60]
        title = e["title"].replace(",", "\\,").replace(";", "\\;")
        lines += ["BEGIN:VEVENT", f"UID:{uid}@ticker-radar", f"DTSTAMP:{d}T000000Z", f"DTSTART;VALUE=DATE:{d}",
                  f"SUMMARY:{title}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _ms(t0) -> int:
    return int((time.monotonic() - t0) * 1000)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--fixtures")
    ap.add_argument("--today", help="YYYY-MM-DD (tests)")
    ap.add_argument("--require", default="", help="comma list of sections that must succeed")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    b = Build(pathlib.Path(a.out), Source(pathlib.Path(a.fixtures) if a.fixtures else None), today,
              os.environ.get("PREVIOUS_BASE"))
    summary = b.run()
    width = max(len(k) for k in summary["sources"])
    for k, v in summary["sources"].items():
        state = "ok  " if v.get("ok") else "FAIL"
        extra = v.get("error") or {kk: vv for kk, vv in v.items() if kk not in ("ok",)}
        print(f"{state} {k:<{width}}  {json.dumps(extra)[:400]}")
    missing = [s for s in a.require.split(",") if s and not summary["sources"].get(s, {}).get("ok")]
    if missing:
        print(f"required sections failed: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
