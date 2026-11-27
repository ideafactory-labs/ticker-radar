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
        term = (s.get("securityTerm") or s.get("originalSecurityTerm") or "").strip()
        # TreasuryDirect has no "auctionDate": the auction is the day competitive bidding closes.
        ad = s.get("competitiveAuctionCloseDate") or s.get("auctionDate")
        if not term or not ad:
            continue
        high = _num(s.get("highRate"))                      # bills: high discount rate
        invest = _num(s.get("investmentRate")) or _num(s.get("highInvestmentRate"))  # coupon-equivalent
        done = high is not None or invest is not None
        out.append({
            "security": f"{term} {s.get('securityType') or 'Bill'}".strip(),
            "cmb": (s.get("cashManagementBill") or "").strip().lower() == "yes",
            "auction_date": _date(ad).isoformat(),
            "issue_date": _date(s["issueDate"]).isoformat() if s.get("issueDate") else None,
            "high_rate": high,
            "investment_rate": invest,
            "bid_to_cover": _num(s.get("bidToCoverRatio")),
            "status": "Result" if done else ("Upcoming" if _date(ad) >= today else "Pending"),
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


SUFFIXES = re.compile(r"\s+(?:-\s+)?(?:Common Stock|Ordinary Shares|American Depositary Shares.*|Common Shares.*|Shares of Beneficial Interest)\s*$", re.I)


def nasdaq_symbol_directory(text: str, kind: str) -> list[dict]:
    """nasdaqtrader.com nasdaqlisted.txt / otherlisted.txt (pipe-delimited) -> listed securities.

    Skips test issues, and warrants/units/rights/preferreds whose names say so."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    if len(lines) < 2 or "|" not in lines[0]:
        raise ParseError(f"symbol directory: unexpected text {text[:60]!r}")
    head = lines[0].split("|")
    idx = {h.strip(): i for i, h in enumerate(head)}
    sym_col = "Symbol" if kind == "nasdaq" else "ACT Symbol"
    if sym_col not in idx or "Security Name" not in idx:
        raise ParseError(f"symbol directory: header {head}")
    exch_map = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe", "V": "IEX"}
    out = []
    for ln in lines[1:]:
        f = ln.split("|")
        if len(f) < len(head) or f[idx["Test Issue"]].strip() == "Y":
            continue
        name = f[idx["Security Name"]].strip()
        if re.search(r"\b(Warrant|Unit|Right|Preferred|Depositary Shares, each representing|Notes due)\b", name, re.I) and not re.search(r"\bETF\b|Trust", name):
            continue
        etf = f[idx["ETF"]].strip() == "Y" if "ETF" in idx else False
        sym = f[idx[sym_col]].strip().replace("$", "-")
        exch = "Nasdaq" if kind == "nasdaq" else exch_map.get(f[idx["Exchange"]].strip(), "Other")
        out.append({"ticker": sym.upper(), "name": SUFFIXES.sub("", name), "exchange": "ETF" if etf else exch, "etf": etf})
    if not out:
        raise ParseError("symbol directory: no rows")
    return out


# ---------------------------------------------------------------- prices
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


def yahoo_spark(json_text: str) -> dict[str, list[tuple[str, float]]]:
    """Yahoo spark JSON (several symbols per call) -> {symbol: [(date, close)]}.

    Accepts both shapes Yahoo has served: {"spark": {"result": [{"symbol", "response": [chart]}]}}
    and {"AAPL": {"timestamp": [...], "close": [...]}}."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ParseError(f"spark: not JSON ({e})")
    out: dict[str, list[tuple[str, float]]] = {}

    def series(ts, closes):
        return [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), float(c))
                for t, c in zip(ts or [], closes or []) if c is not None]

    if isinstance(data, dict) and isinstance(data.get("spark"), dict):
        for r in data["spark"].get("result") or []:
            try:
                ch = r["response"][0]
                out[r["symbol"]] = series(ch["timestamp"], ch["indicators"]["quote"][0]["close"])
            except (KeyError, IndexError, TypeError):
                continue
    elif isinstance(data, dict):
        for sym, v in data.items():
            if isinstance(v, dict) and "close" in v:
                out[sym] = series(v.get("timestamp"), v.get("close"))
    if not out:
        raise ParseError(f"spark: no series in {json_text[:80]!r}")
    return out


def _pct(a: float, b: float) -> float:
    return round((a / b - 1) * 100, 2)


def _sma(closes: list[float], n: int, end: int | None = None) -> float | None:
    """Simple moving average of the n closes ending at index `end` (exclusive); None if too short."""
    end = len(closes) if end is None else end
    if end < n or n <= 0:
        return None
    return sum(closes[end - n:end]) / n


def price_signals(series: list[tuple[str, float]]) -> dict:
    """Signals from up to a year of daily closes. Every field is None when history is too short,
    never a guess. Definitions (trading days): 1w = 5, 1m = 21, 3m = 63.

    - chg_1w / chg_1m / chg_3m / chg_ytd: % change over the period.
    - from_high / from_low: % distance of the last close from the 52-week high / low.
    - vs_ma50 / vs_ma200: % distance from the 50- / 200-day simple moving average.
    - cross: "golden" if the 50-day average moved above the 200-day within the last 10
      trading days, "death" if below; else None.
    - week_z: this week's 5-day return measured in standard deviations of this stock's own
      5-day returns over the year (how unusual the week is for this stock).
    """
    closes = [c for _, c in series]
    n = len(closes)
    last = closes[-1]
    out: dict = {k: None for k in ("chg_1w", "chg_1m", "chg_3m", "chg_ytd", "vs_ma50", "vs_ma200", "cross", "week_z")}
    for key, d in (("chg_1w", 5), ("chg_1m", 21), ("chg_3m", 63)):
        if n > d:
            out[key] = _pct(last, closes[-1 - d])
    year = series[-1][0][:4]
    prior = [c for d, c in series if d[:4] < year]
    if prior:  # last close of the previous year is the YTD base
        out["chg_ytd"] = _pct(last, prior[-1])
    yr = closes[-252:]
    out["from_high"] = _pct(last, max(yr))
    out["from_low"] = _pct(last, min(yr))
    ma50, ma200 = _sma(closes, 50), _sma(closes, 200)
    if ma50:
        out["vs_ma50"] = _pct(last, ma50)
    if ma200:
        out["vs_ma200"] = _pct(last, ma200)
    if n >= 210:
        now = _sma(closes, 50) - _sma(closes, 200)
        then = _sma(closes, 50, n - 10) - _sma(closes, 200, n - 10)
        if then <= 0 < now:
            out["cross"] = "golden"
        elif then >= 0 > now:
            out["cross"] = "death"
    if n >= 65:
        rets = [closes[i] / closes[i - 5] - 1 for i in range(5, n)]
        mean = sum(rets) / len(rets)
        sd = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        if sd > 0:
            out["week_z"] = round((rets[-1] - mean) / sd, 2)
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
        **price_signals(series),
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
