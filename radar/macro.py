"""The gist: a handful of dials that say where the US economy and market stand.

Pure functions over series ([(iso_date, value)] oldest first) so every number on the page is
reproducible and tested (tests/test_macro.py). Nothing here predicts; each dial reports a
well-known measure, where it sits in its own history, and how that measure behaved before
past recessions (computed from the data, not quoted).

Every rule that turns a number into green / amber / red lives in RULES below and is published
with the data, so the page can show exactly how each dial is judged.
"""
from __future__ import annotations

import datetime as dt

Series = list[tuple[str, float]]

GREEN, AMBER, RED = "green", "amber", "red"

# The judging rules, in plain words. Shown on the page next to each dial.
RULES = {
    "valuation": "Stock market value ÷ GDP (the “Buffett indicator”), compared with its own history since 1970. "
                 "Red: higher than 90% of history. Amber: higher than 70%. A high reading says stocks are expensive "
                 "relative to the economy; it has said so for years at a time and is not a timing signal.",
    "recession": "Red: the Sahm rule has triggered (0.50 or more) or the smoothed recession probability is 50% or more. "
                 "Amber: the 10-year minus 3-month Treasury yield curve is inverted, or was within the last 18 months, "
                 "or the Sahm rule reads 0.30 or more. Green otherwise.",
    "credit": "Chicago Fed National Financial Conditions Index (0 = average conditions; above 0 = tighter than average). "
              "Red: above +0.5 or the VIX above 35. Amber: above 0 or the VIX above 25. Green otherwise.",
    "inflation": "Core CPI, change from a year earlier, against the Fed’s 2% goal. Green: 2.5% or less. "
                 "Amber: up to 3.5%. Red: above 3.5%.",
    "jobs": "Sahm rule reading: the 3-month average unemployment rate minus its low of the previous 12 months. "
            "Green: under 0.30. Amber: 0.30 to 0.49. Red: 0.50 or more.",
    "market": "S&P 500 fund (SPY) against its 200-day average, and the share of all priced US stocks above their own "
              "200-day average. Green: SPY above and more than half of stocks above. Red: SPY below and under 40% of "
              "stocks above. Amber otherwise.",
}


# ---------------------------------------------------------------- helpers
def monthly_last(s: Series) -> Series:
    """Last observation of each calendar month."""
    out: dict[str, tuple[str, float]] = {}
    for d, v in s:
        out[d[:7]] = (d, v)
    return [out[k] for k in sorted(out)]


def monthly_mean(s: Series) -> Series:
    """Mean of each calendar month, dated the first of the month."""
    acc: dict[str, list[float]] = {}
    for d, v in s:
        acc.setdefault(d[:7], []).append(v)
    return [(k + "-01", sum(v) / len(v)) for k, v in sorted(acc.items())]


def percentile(history: list[float], x: float) -> float:
    """Share of history at or below x, 0-100."""
    if not history:
        raise ValueError("empty history")
    return round(100 * sum(1 for v in history if v <= x) / len(history), 1)


def months_between(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def downsample(s: Series, n: int = 240) -> list[list]:
    """At most ~n points, always keeping the last one (for small history charts)."""
    if len(s) <= n:
        return [[d, round(v, 3)] for d, v in s]
    step = len(s) / n
    idx = sorted({int(i * step) for i in range(n)} | {len(s) - 1})
    return [[s[i][0], round(s[i][1], 3)] for i in idx]


# ---------------------------------------------------------------- yield curve
def curve_state(spread_daily: Series) -> dict:
    """State of a yield-curve spread (e.g. 10-year minus 3-month), from daily data.

    inverted: latest value below zero. days_inverted: length of the current run of negative
    days. last_inverted: most recent negative day (None if never). months_since_inverted:
    whole months from that day to the latest observation.
    """
    last_d, last_v = spread_daily[-1]
    run = 0
    for _, v in reversed(spread_daily):
        if v < 0:
            run += 1
        else:
            break
    last_neg = next((d for d, v in reversed(spread_daily) if v < 0), None)
    return {
        "as_of": last_d, "spread": round(last_v, 2), "inverted": last_v < 0, "days_inverted": run,
        "last_inverted": last_neg,
        "months_since_inverted": months_between(last_neg, last_d) if last_neg else None,
    }


def recession_starts(usrec: Series) -> list[str]:
    """Months where the NBER recession flag turns from 0 to 1."""
    return [usrec[i][0] for i in range(1, len(usrec)) if usrec[i][1] >= 0.5 > usrec[i - 1][1]]


def inversion_record(spread_daily: Series, usrec: Series, *, lead_months: int = 24, gap_months: int = 12) -> dict:
    """How the curve has done as a recession warning, computed from the data.

    An episode is a stretch of months whose average spread is below zero; stretches less than
    `gap_months` apart count as one episode. An episode is a hit if a recession starts within
    `lead_months` after it begins (or during it), a false alarm if none does and enough time
    has passed, and pending if `lead_months` have not yet passed since it ended.
    """
    m = monthly_mean(spread_daily)
    neg = [d for d, v in m if v < 0]
    episodes: list[list[str]] = []
    for d in neg:
        if episodes and months_between(episodes[-1][1], d) <= gap_months:
            episodes[-1][1] = d
        else:
            episodes.append([d, d])
    starts = [d for d in recession_starts(usrec) if d >= m[0][0]]
    latest = m[-1][0]
    out, used = [], set()
    for a, b in episodes:
        hit = next((r for r in starts if a <= r and months_between(b, r) <= lead_months), None)
        if hit:
            used.add(hit)
            out.append({"start": a, "end": b, "outcome": "recession", "recession": hit, "lead_months": months_between(a, hit)})
        elif months_between(b, latest) < lead_months:
            out.append({"start": a, "end": b, "outcome": "pending"})
        else:
            out.append({"start": a, "end": b, "outcome": "false alarm"})
    missed = [r for r in starts if r not in used and months_between(m[0][0], r) >= lead_months]
    return {
        "since": m[0][0], "episodes": out,
        "hits": sum(1 for e in out if e["outcome"] == "recession"),
        "false_alarms": sum(1 for e in out if e["outcome"] == "false alarm"),
        "pending": sum(1 for e in out if e["outcome"] == "pending"),
        "recessions": len([r for r in starts if months_between(m[0][0], r) >= lead_months]),
        "missed": missed,
    }


# ---------------------------------------------------------------- Buffett indicator
def buffett_series(equities_millions: Series, gdp_billions: Series) -> Series:
    """Market value of corporate equities ÷ GDP, in percent, for quarters present in both."""
    gdp = {d: v for d, v in gdp_billions}
    return [(d, v / 1000 / gdp[d] * 100) for d, v in equities_millions if d in gdp and gdp[d] > 0]


# ---------------------------------------------------------------- dials
def _dial(key, name, status, value, headline, parts, as_of, history=None, extra=None):
    return {"key": key, "name": name, "status": status, "value": value, "headline": headline,
            "rule": RULES[key], "parts": parts, "as_of": as_of, "history": history or [], **(extra or {})}


def valuation_dial(equities_millions: Series, gdp_billions: Series) -> dict:
    s = [(d, v) for d, v in buffett_series(equities_millions, gdp_billions) if d >= "1970-01-01"]
    if len(s) < 40:
        raise ValueError("Buffett indicator: not enough history")
    d, v = s[-1]
    pct = percentile([x for _, x in s], v)
    status = RED if pct > 90 else AMBER if pct > 70 else GREEN
    word = {RED: "very high", AMBER: "above normal", GREEN: "in its normal range"}[status]
    q = f"Q{(int(d[5:7]) - 1) // 3 + 1} {d[:4]}"
    return _dial("valuation", "Valuation", status, f"{v:.0f}%",
                 f"Stocks are worth {v:.0f}% of GDP, {word}: higher than {pct:.0f}% of readings since 1970.",
                 [{"label": "Buffett indicator (equities ÷ GDP)", "value": f"{v:.0f}%", "note": f"{q}; updates quarterly"},
                  {"label": "Higher than this share of history", "value": f"{pct:.0f}%", "note": "since 1970"},
                  {"label": "Median since 1970", "value": f"{sorted(x for _, x in s)[len(s) // 2]:.0f}%", "note": ""}],
                 d, downsample(s))


def recession_dial(t10y3m: Series, usrec: Series, sahm: Series, recprob: Series | None, t10y2y: Series | None = None) -> dict:
    c = curve_state(t10y3m)
    rec = inversion_record(t10y3m, usrec)
    sahm_d, sahm_v = sahm[-1]
    prob = recprob[-1] if recprob else None
    recently = c["months_since_inverted"] is not None and c["months_since_inverted"] <= 18
    if sahm_v >= 0.5 or (prob and prob[1] >= 50):
        status = RED
    elif c["inverted"] or recently or sahm_v >= 0.3:
        status = AMBER
    else:
        status = GREEN
    if c["inverted"]:
        curve_txt = f"The yield curve is inverted ({c['spread']:+.2f} points, {c['days_inverted']} trading days in a row)."
    elif recently:
        curve_txt = (f"The yield curve is no longer inverted ({c['spread']:+.2f}) but was {c['months_since_inverted']} months ago; "
                     "recessions have often started after the curve turns positive again.")
    else:
        curve_txt = f"The yield curve is not inverted ({c['spread']:+.2f} points)."
    record_txt = (f"Since {rec['since'][:4]}, inversions came before {rec['hits']} of {rec['recessions']} recessions, "
                  f"with {rec['false_alarms']} false alarm{'s' if rec['false_alarms'] != 1 else ''}"
                  + (f" and {rec['pending']} still open" if rec["pending"] else "") + ".")
    parts = [
        {"label": "10-year minus 3-month yield", "value": f"{c['spread']:+.2f} pts", "note": "inverted" if c["inverted"] else "not inverted"},
        {"label": "Sahm rule", "value": f"{sahm_v:.2f}", "note": "triggers at 0.50"},
    ]
    if t10y2y:
        parts.insert(1, {"label": "10-year minus 2-year yield", "value": f"{t10y2y[-1][1]:+.2f} pts", "note": "inverted" if t10y2y[-1][1] < 0 else "not inverted"})
    if prob:
        parts.append({"label": "Smoothed recession probability", "value": f"{prob[1]:.1f}%", "note": f"{prob[0][:7]}; published with a lag"})
    parts.append({"label": "Track record of inversions", "value": f"{rec['hits']}/{rec['recessions']}", "note": record_txt})
    return _dial("recession", "Recession risk", status, {RED: "High", AMBER: "Raised", GREEN: "Low"}[status],
                 curve_txt, parts, c["as_of"], downsample(monthly_mean(t10y3m)), {"record": rec, "curve": c})


def credit_dial(nfci: Series, vix: Series, hy_spread: Series | None = None) -> dict:
    d, v = nfci[-1]
    vx = vix[-1][1]
    status = RED if v > 0.5 or vx > 35 else AMBER if v > 0 or vx > 25 else GREEN
    word = {RED: "stressed", AMBER: "tighter than average", GREEN: "calm"}[status]
    parts = [{"label": "Financial conditions index (NFCI)", "value": f"{v:+.2f}", "note": "0 = average; above 0 = tighter"},
             {"label": "Stock volatility (VIX)", "value": f"{vx:.1f}", "note": f"higher than {percentile([x for _, x in vix], vx):.0f}% of history"}]
    if hy_spread:
        parts.append({"label": "High-yield bond spread", "value": f"{hy_spread[-1][1]:.2f} pts", "note": "extra yield over Treasuries"})
    return _dial("credit", "Credit stress", status, word.capitalize() if status != AMBER else "Tight",
                 f"Financial conditions are {word} (index {v:+.2f}; VIX {vx:.0f}).", parts, d, downsample(nfci))


def inflation_dial(cpi: Series, core: Series, breakeven: Series | None = None) -> dict:
    def yoy(s):
        return [(s[i][0], (s[i][1] / s[i - 12][1] - 1) * 100) for i in range(12, len(s))]
    h, c = yoy(cpi), yoy(core)
    v = c[-1][1]
    status = GREEN if v <= 2.5 else AMBER if v <= 3.5 else RED
    word = {GREEN: "near the Fed’s 2% goal", AMBER: "above the Fed’s 2% goal", RED: "well above the Fed’s 2% goal"}[status]
    month = dt.date.fromisoformat(c[-1][0]).strftime("%B %Y")
    parts = [{"label": "Core CPI, year over year", "value": f"{v:.1f}%", "note": month},
             {"label": "CPI, year over year", "value": f"{h[-1][1]:.1f}%", "note": month},
             {"label": "Core CPI a year earlier", "value": f"{c[-13][1]:.1f}%" if len(c) > 13 else "n/a", "note": ""}]
    if breakeven:
        parts.append({"label": "Market-implied 5-year inflation", "value": f"{breakeven[-1][1]:.2f}%", "note": "from Treasury yields"})
    return _dial("inflation", "Inflation", status, f"{v:.1f}%", f"Core inflation is {v:.1f}%, {word}.", parts, c[-1][0], downsample(c))


def jobs_dial(unrate: Series, sahm: Series, payrolls: Series, claims: Series | None = None) -> dict:
    d, u = unrate[-1]
    s = sahm[-1][1]
    status = GREEN if s < 0.3 else AMBER if s < 0.5 else RED
    chg = [payrolls[i][1] - payrolls[i - 1][1] for i in range(len(payrolls) - 3, len(payrolls))]
    avg3 = sum(chg) / 3
    year_ago = unrate[-13][1] if len(unrate) > 13 else None
    word = {GREEN: "steady", AMBER: "softening", RED: "weakening sharply"}[status]
    parts = [{"label": "Unemployment rate", "value": f"{u:.1f}%", "note": f"{year_ago:.1f}% a year earlier" if year_ago is not None else ""},
             {"label": "Sahm rule", "value": f"{s:.2f}", "note": "0.50 has marked the start of past recessions"},
             {"label": "Payroll growth, 3-month average", "value": f"{avg3:+,.0f}k", "note": "jobs added per month"}]
    if claims:
        parts.append({"label": "Jobless claims, 4-week average", "value": f"{claims[-1][1] / 1000:,.0f}k", "note": claims[-1][0]})
    return _dial("jobs", "Jobs", status, f"{u:.1f}%", f"The job market is {word}: unemployment {u:.1f}%, Sahm rule {s:.2f}.",
                 parts, d, downsample(unrate[-360:]))


def market_dial(spy: dict, breadth_pct: float | None, counted: int) -> dict:
    above = spy.get("vs_ma200")
    if above is None:
        raise ValueError("market: SPY has no 200-day average yet")
    up = above >= 0
    if breadth_pct is None:
        status = GREEN if up else RED
    else:
        status = GREEN if up and breadth_pct > 50 else RED if (not up and breadth_pct < 40) else AMBER
    word = {GREEN: "in an uptrend", AMBER: "mixed", RED: "in a downtrend"}[status]
    parts = [{"label": "S&P 500 vs its 200-day average", "value": f"{above:+.1f}%", "note": "above" if up else "below"},
             {"label": "S&P 500 from its 52-week high", "value": f"{spy['from_high']:+.1f}%", "note": ""},
             {"label": "S&P 500 this year", "value": f"{spy['chg_ytd']:+.1f}%" if spy.get("chg_ytd") is not None else "n/a", "note": ""}]
    if breadth_pct is not None:
        parts.insert(1, {"label": "Stocks above their 200-day average", "value": f"{breadth_pct:.0f}%", "note": f"of {counted:,} US stocks"})
    breadth_txt = f"; {breadth_pct:.0f}% of stocks are in uptrends" if breadth_pct is not None else ""
    return _dial("market", "Market trend", status, {GREEN: "Up", AMBER: "Mixed", RED: "Down"}[status],
                 f"The stock market is {word}: the S&P 500 is {abs(above):.1f}% {'above' if up else 'below'} its 200-day average{breadth_txt}.",
                 parts, spy["as_of"])


def breadth(prices: dict[str, dict], is_stock) -> tuple[float | None, int]:
    """Share of stocks trading above their own 200-day average."""
    vals = [v["vs_ma200"] for t, v in prices.items() if is_stock(t) and v.get("vs_ma200") is not None]
    if len(vals) < 50:
        return None, len(vals)
    return round(100 * sum(1 for x in vals if x >= 0) / len(vals), 1), len(vals)


PHRASES = {
    "valuation": {GREEN: "stocks are fairly valued", AMBER: "stocks are expensive", RED: "stocks are very expensive"},
    "recession": {GREEN: "recession signals are quiet", AMBER: "recession signals are raised", RED: "recession signals are flashing"},
    "credit": {GREEN: "credit is calm", AMBER: "credit is tight", RED: "credit is stressed"},
    "inflation": {GREEN: "inflation is near target", AMBER: "inflation is above target", RED: "inflation is high"},
    "jobs": {GREEN: "jobs are steady", AMBER: "jobs are softening", RED: "jobs are weakening"},
    "market": {GREEN: "the market is in an uptrend", AMBER: "the market is mixed", RED: "the market is in a downtrend"},
}


def summary(dials: list[dict]) -> str:
    """One sentence built only from the dial states, most concerning first."""
    order = {RED: 0, AMBER: 1, GREEN: 2}
    parts = [PHRASES[d["key"]][d["status"]] for d in sorted(dials, key=lambda d: order[d["status"]])]
    if not parts:
        return ""
    s = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return "In the US, " + s + "."
