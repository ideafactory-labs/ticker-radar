"""Pure parsers: source response text in, clean records out. No network here, so every
function is tested offline against fixtures (tests/test_parse.py)."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from xml.etree import ElementTree

Date = dt.date


class ParseError(Exception):
    pass


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in ("", ".", "N/A", "NA", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(s: str) -> Date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(s[:10])  # also covers "2026-11-30T00:00:00"
    except ValueError:
        raise ParseError(f"unrecognised date {s!r}") from None


# ---------------------------------------------------------------- Treasury
def treasury_bill_rates(csv_text: str) -> list[dict]:
    """Daily Treasury Bill Rates CSV -> [{date, rates: {'4': 4.31, '13': ...}}] newest first.

    Columns look like "4 WEEKS COUPON EQUIVALENT"; we use the coupon-equivalent yield
    (comparable to note yields), not the bank discount rate.
    """
    rows = list(csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))))
    if not rows:
        raise ParseError("bill rates: empty CSV")
    cols = {}
    for c in rows[0].keys():
        m = re.match(r"\s*(\d+)\s*WEEKS?\s+COUPON\s+EQUIVALENT", c or "", re.I)
        if m:
            cols[m.group(1)] = c
    if not cols:
        raise ParseError(f"bill rates: no coupon-equivalent columns in {list(rows[0].keys())[:6]}")
    out = []
    for r in rows:
        rates = {w: _num(r[c]) for w, c in cols.items() if _num(r[c]) is not None}
        if rates:
            out.append({"date": _date(r.get("Date") or r.get("date")).isoformat(), "rates": rates})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


CURVE_COLS = ["1 Mo", "2 Mo", "3 Mo", "4 Mo", "6 Mo", "1 Yr", "2 Yr", "5 Yr", "10 Yr", "30 Yr"]


def treasury_yield_curve(csv_text: str) -> list[dict]:
    """Daily Treasury Par Yield Curve CSV -> [{date, yields: {'2 Yr': 3.9, ...}}] newest first."""
    rows = list(csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))))
    if not rows:
        raise ParseError("yield curve: empty CSV")
    keys = {k.strip(): k for k in rows[0].keys() if k}
    have = [c for c in CURVE_COLS if c in keys]
    if "2 Yr" not in have or "10 Yr" not in have:
        raise ParseError(f"yield curve: missing 2 Yr/10 Yr in {list(keys)[:8]}")
    out = []
    for r in rows:
        ys = {c: _num(r[keys[c]]) for c in have if _num(r[keys[c]]) is not None}
        if ys:
            out.append({"date": _date(r[keys.get("Date", "Date")]).isoformat(), "yields": ys})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def ago(series: list[dict], days: int, key: str, sub: str) -> float | None:
    """Value of series[...][key][sub] on the last date at least `days` before the newest."""
    if not series:
        return None
    newest = Date.fromisoformat(series[0]["date"])
    for row in series:
        if (newest - Date.fromisoformat(row["date"])).days >= days and sub in row[key]:
            return row[key][sub]
    return None


def treasury_auctions(json_text: str, *, today: Date) -> list[dict]:
    """TreasuryDirect TA_WS securities JSON (announced or auctioned) -> auctions."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ParseError(f"auctions: not JSON ({e})")
    if isinstance(data, dict):
        data = data.get("securityList") or data.get("data") or []
    out = []
    for s in data:
        term = (s.get("securityTerm") or s.get("term") or "").strip()
        ad = s.get("auctionDate")
        if not term or not ad:
            continue
        rate = _num(s.get("highInvestmentRate")) or _num(s.get("highYield"))
        out.append({
            "security": f"{term} {s.get('securityType', 'Bill')}".strip(),
            "auction_date": _date(ad).isoformat(),
            "issue_date": _date(s["issueDate"]).isoformat() if s.get("issueDate") else None,
            "high_rate": rate,
            "status": "Result" if rate is not None else ("Upcoming" if _date(ad) >= today else "Pending"),
        })
    out.sort(key=lambda a: a["auction_date"])
    return out


# ---------------------------------------------------------------- FRED
def fred_csv(csv_text: str, series_id: str) -> list[tuple[str, float]]:
    """fredgraph.csv -> [(date, value)] oldest first, skipping missing ('.') values."""
    rows = list(csv.reader(io.StringIO(csv_text.lstrip("﻿"))))
    if len(rows) < 2:
        raise ParseError(f"FRED {series_id}: empty CSV")
    head = [h.strip() for h in rows[0]]
    if len(head) < 2 or head[0].upper() not in ("DATE", "OBSERVATION_DATE"):
        raise ParseError(f"FRED {series_id}: unexpected header {head}")
    out = []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        v = _num(r[1])
        if v is not None:
            out.append((_date(r[0]).isoformat(), v))
    if not out:
        raise ParseError(f"FRED {series_id}: no values")
    return out


def indicator(obs: list[tuple[str, float]], transform: str) -> dict:
    """Apply a catalog transform; returns latest, previous, period dates and a short history."""
    if transform == "yoy":  # monthly index -> % change vs 12 observations earlier
        pts = [(obs[i][0], (obs[i][1] / obs[i - 12][1] - 1) * 100) for i in range(12, len(obs))]
    elif transform == "mom":
        pts = [(obs[i][0], (obs[i][1] / obs[i - 1][1] - 1) * 100) for i in range(1, len(obs))]
    elif transform == "diff":  # e.g. payrolls: monthly change, thousands
        pts = [(obs[i][0], obs[i][1] - obs[i - 1][1]) for i in range(1, len(obs))]
    elif transform == "thousands":
        pts = [(d, v / 1000) for d, v in obs]
    else:
        pts = list(obs)
    if len(pts) < 2:
        raise ParseError("indicator: not enough observations")
    return {
        "latest": round(pts[-1][1], 3), "latest_date": pts[-1][0],
        "previous": round(pts[-2][1], 3), "previous_date": pts[-2][0],
        "history": [[d, round(v, 3)] for d, v in pts[-24:]],
    }


# ---------------------------------------------------------------- SEC
def sec_company_tickers(json_text: str) -> list[dict]:
    """company_tickers_exchange.json -> [{ticker, name, exchange, cik}] in SEC's order."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ParseError(f"SEC tickers: not JSON ({e})")
    fields = data.get("fields")
    rows = data.get("data")
    if not fields or rows is None:
        raise ParseError("SEC tickers: missing fields/data")
    idx = {f: i for i, f in enumerate(fields)}
    out = []
    for r in rows:
        t = r[idx["ticker"]]
        if not t:
            continue
        out.append({"ticker": str(t).upper(), "name": r[idx["name"]], "exchange": r[idx.get("exchange", -1)] if "exchange" in idx else None, "cik": r[idx["cik"]]})
    if not out:
        raise ParseError("SEC tickers: no rows")
    return out


ATOM = "{http://www.w3.org/2005/Atom}"


def sec_current_filings(atom_text: str) -> list[dict]:
    """EDGAR 'latest filings' Atom feed -> [{form, company, cik, filed, url}]."""
    try:
        root = ElementTree.fromstring(atom_text)
    except ElementTree.ParseError as e:
        raise ParseError(f"EDGAR feed: not XML ({e})")
    out = []
    for e in root.iter(ATOM + "entry"):
        title = (e.findtext(ATOM + "title") or "").strip()
        m = re.match(r"(?P<form>[\w/-]+)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{6,10})\)", title)
        if not m:
            continue
        link = e.find(ATOM + "link")
        updated = e.findtext(ATOM + "updated") or ""
        out.append({"form": m["form"], "company": m["name"].strip(), "cik": m["cik"].lstrip("0"),
                    "filed": updated[:10], "url": link.get("href") if link is not None else None})
    return out


# ---------------------------------------------------------------- prices
def stooq_daily(csv_text: str) -> list[tuple[str, float]]:
    """Stooq daily history CSV -> [(date, close)] oldest first."""
    if csv_text.strip().lower().startswith(("no data", "exceeded", "<")):
        raise ParseError(f"stooq: {csv_text.strip()[:60]!r}")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows or "Close" not in rows[0]:
        raise ParseError(f"stooq: unexpected CSV {csv_text[:80]!r}")
    out = [(_date(r["Date"]).isoformat(), _num(r["Close"])) for r in rows if _num(r.get("Close")) is not None]
    if len(out) < 2:
        raise ParseError("stooq: too few rows")
    return out


def yahoo_chart(json_text: str) -> list[tuple[str, float]]:
    """Yahoo v8 chart JSON -> [(date, close)] oldest first."""
    try:
        res = json.loads(json_text)["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise ParseError(f"yahoo: unexpected JSON ({type(e).__name__})")
    out = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), float(c)) for t, c in zip(ts, closes) if c is not None]
    if len(out) < 2:
        raise ParseError("yahoo: too few rows")
    return out


def price_summary(series: list[tuple[str, float]]) -> dict:
    """[(date, close)] -> what a watchlist card needs."""
    last_year = series[-252:]
    closes = [c for _, c in last_year]
    return {
        "as_of": series[-1][0],
        "close": round(series[-1][1], 4),
        "prev_close": round(series[-2][1], 4),
        "change_pct": round((series[-1][1] / series[-2][1] - 1) * 100, 3),
        "high_52w": round(max(closes), 4),
        "low_52w": round(min(closes), 4),
        "spark": [round(c, 4) for _, c in series[-30:]],
    }


# ---------------------------------------------------------------- IPOs (Nasdaq)
def nasdaq_ipo_calendar(json_text: str) -> list[dict]:
    """api.nasdaq.com IPO calendar JSON (one month) -> [{name, ticker, exchange, status, ...}]."""
    try:
        data = json.loads(json_text).get("data") or {}
    except (json.JSONDecodeError, AttributeError) as e:
        raise ParseError(f"nasdaq: not JSON ({e})")
    out = []

    def rows(section, *path):
        node = data.get(section) or {}
        for p in path:
            node = (node or {}).get(p) or {}
        return node if isinstance(node, list) else (node.get("rows") or [] if isinstance(node, dict) else [])

    for r in rows("priced", "rows") or rows("priced"):
        out.append(_ipo_row(r, "priced", r.get("pricedDate")))
    for r in rows("upcoming", "upcomingTable", "rows"):
        out.append(_ipo_row(r, "upcoming", r.get("expectedPriceDate")))
    for r in rows("filed", "rows") or rows("filed"):
        out.append(_ipo_row(r, "filed", None, filed=r.get("filedDate")))
    return [o for o in out if o["name"]]


def _ipo_row(r: dict, status: str, date: str | None, filed: str | None = None) -> dict:
    def d(s):
        try:
            return _date(s).isoformat() if s else None
        except ParseError:
            return None
    return {
        "name": (r.get("companyName") or "").strip(),
        "ticker": (r.get("proposedTickerSymbol") or "").strip() or None,
        "exchange": (r.get("proposedExchange") or "").strip() or None,
        "price": (r.get("proposedSharePrice") or "").strip() or None,
        "status": status,
        "trade_date": d(date),
        "filed_date": d(filed),
        "deal_size": (r.get("dollarValueOfSharesOffered") or "").strip() or None,
    }


def ipo_timeline(ipo: dict) -> list[dict]:
    """Key dates for one IPO. Quiet-period end and lock-up expiry are estimates."""
    steps = []
    if ipo.get("filed_date"):
        steps.append({"key": "filed", "label": "Filed", "date": ipo["filed_date"]})
    if ipo.get("trade_date"):
        t = Date.fromisoformat(ipo["trade_date"])
        est = ipo["status"] == "upcoming"
        steps.append({"key": "priced", "label": "Prices" if est else "Priced", "date": t.isoformat(), "est": est})
        steps.append({"key": "listed", "label": "First trade", "date": t.isoformat(), "est": est})
        steps.append({"key": "quiet", "label": "Quiet period ends", "date": (t + dt.timedelta(days=25)).isoformat(), "est": True})
        steps.append({"key": "lockup", "label": "Lock-up ends", "date": (t + dt.timedelta(days=180)).isoformat(), "est": True})
    return steps


# ---------------------------------------------------------------- calendars
def ics_events(ics_text: str) -> list[dict]:
    """Minimal iCalendar reader -> [{date, summary}] (handles folded lines, DATE and DATE-TIME)."""
    if "BEGIN:VCALENDAR" not in ics_text:
        raise ParseError("ics: not a calendar")
    lines: list[str] = []
    for raw in ics_text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    out, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT" and cur is not None:
            if cur.get("date") and cur.get("summary"):
                out.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            k, v = ln.split(":", 1)
            name = k.split(";")[0].upper()
            if name == "DTSTART":
                cur["date"] = _date(v[:8]).isoformat()
            elif name == "SUMMARY":
                cur["summary"] = v.replace("\\,", ",").replace("\\;", ";").strip()
    return out


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"], 1)}


def fomc_meetings(html: str) -> list[dict]:
    """federalreserve.gov FOMC calendars page -> [{start, end}] for every year panel found.

    The page lists, per year, month names (e.g. "Jan/Feb" or "January") followed by day
    ranges such as "27-28" or "31-1*". We read panels headed "<year> FOMC Meetings".
    """
    out = []
    panels = re.split(r"(\d{4})\s+FOMC\s+Meetings", html)
    for i in range(1, len(panels) - 1, 2):
        year = int(panels[i])
        body = panels[i + 1]
        for mm, dd in re.findall(r'fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)\s*(?:</strong>)?\s*</div>\s*'
                                 r'<div[^>]*fomc-meeting__date[^>]*>\s*([\d\-–]+)', body):
            months = [MONTHS.get(_full_month(m)) for m in mm.split("/")]
            days = re.split(r"[-–]", dd)
            if not months[0] or not days[0].isdigit():
                continue
            start = Date(year, months[0], int(days[0]))
            end_m = months[-1] or months[0]
            end_d = int(days[-1]) if days[-1].isdigit() else int(days[0])
            end_y = year + 1 if end_m < months[0] else year
            out.append({"start": start.isoformat(), "end": Date(end_y, end_m, end_d).isoformat()})
    if not out:
        raise ParseError("FOMC: no meetings found on page")
    return out


def _full_month(m: str) -> str:
    m = m.strip().lower()
    for full in MONTHS:
        if full.startswith(m[:3]):
            return full
    return m
