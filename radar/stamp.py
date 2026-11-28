"""Stamp the page's own script and stylesheet with a version, so a normal reload after a
deploy never pairs a new index.html with an old cached app.js (or the reverse).

    python3 -m radar.stamp site <version>

Rewrites src="app.js" / href="style.css" in site/index.html to app.js?v=<version>, and
records the version in a meta tag that app.js compares with its own copy.
"""
from __future__ import annotations

import pathlib
import re
import sys

ASSETS = ("app.js", "style.css")


def stamp(html: str, version: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", version):
        raise ValueError(f"unsafe version string: {version!r}")
    for name in ASSETS:
        html, n = re.subn(rf'(\b(?:src|href)=")({re.escape(name)})(?:\?v=[^"]*)?(")', rf"\g<1>\g<2>?v={version}\g<3>", html)
        if n != 1:
            raise ValueError(f"expected exactly one reference to {name}, found {n}")
    html = re.sub(r'<meta name="build" content="[^"]*">\n?', "", html)
    return html.replace("<head>\n", f'<head>\n<meta name="build" content="{version}">\n', 1)


def main(argv: list[str]) -> int:
    site, version = pathlib.Path(argv[0]), argv[1][:12]
    page = site / "index.html"
    page.write_text(stamp(page.read_text(), version))
    js = site / "app.js"
    js.write_text(js.read_text().replace("__BUILD__", version))
    print(f"stamped {page} and {js} with {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
