"""HTTP for the fetchers: one User-Agent, timeouts, small retries, and a per-host pause.

Stdlib only, so the workflow needs nothing installed.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

# SEC asks automated clients to identify themselves; the others are fine with it too.
USER_AGENT = "ticker-radar/1.0 (+https://github.com/ideafactory-labs/ticker-radar)"

# Minimum seconds between requests to the same host (fair use; SEC allows 10/s).
_MIN_GAP = {"www.sec.gov": 0.15, "stooq.com": 0.25, "query1.finance.yahoo.com": 0.25}
_last: dict[str, float] = {}
_lock = threading.Lock()


class FetchError(Exception):
    pass


def _pace(host: str) -> None:
    gap = _MIN_GAP.get(host, 0.1)
    with _lock:
        wait = _last.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()


def get(url: str, *, headers: dict | None = None, timeout: float = 20, tries: int = 3) -> bytes:
    host = urlparse(url).hostname or ""
    h = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    h.update(headers or {})
    last_err: Exception | None = None
    for attempt in range(tries):
        _pace(host)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = FetchError(f"HTTP {e.code} from {host}")
            if e.code in (400, 401, 403, 404):
                break  # retrying will not help
        except Exception as e:  # timeouts, resets, DNS
            last_err = FetchError(f"{type(e).__name__} from {host}: {e}")
        time.sleep(1.5 * (attempt + 1))
    raise last_err or FetchError(f"failed: {url}")


def text(url: str, **kw) -> str:
    return get(url, **kw).decode("utf-8", errors="replace")
