#!/usr/bin/env python3
"""
Standalone MyDramaList fetcher, upgraded with:

- CLI flags (quarters, auto-quarters, cache-dir, verbose)
- Automatic quarter detection (current + next quarter)
- Caching of fetched HTML (with max-age)
- Parallel fetching of calendars and show detail pages
- Improved logging

Still uses only the Python standard library.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

MDL_COUNTRY = {"JP": "Japan", "KR": "South Korea"}
MDL_TZ = "Australia/Sydney"  # only affects which page-day an episode is listed under
MAX_SHOWS = 500              # safety cap on per-show detail fetches
SHOW_FETCH_DELAY = 0.4       # kept for compatibility; not used in parallel mode
OUTPUT = "mdl_broadcast.json"

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

DAY_RE = re.compile(r'<div id="d\d\d">\s*<small>([^<]+)</small>')
CARD_RE = re.compile(
    r'<div class="el-card">.*?'
    r'<a href="/(\d+)-([^"/]+)" target="_blank">\s*<img[^>]*alt="([^"]*)".*?'
    r'<div class="release-time calendar-popover-source"[^>]*>(.*?)</div>\s*'
    r'<div class="calendar-popover-content">\s*<div class="calendar-popover-title">([^<]*)</div>.*?'
    r'<button class="btn simple btn-manage-list".*?</button>\s*</div>\s*'
    r'<div class="top-append">\s*</div>\s*'
    r'<a href="[^"]*" target="_blank" class="text-primary _600">([^<]*)</a>\s*'
    r'<div(?: class="text-sm")?>\s*([^<]*?)\s*</div>',
    re.S)
ICON_RE = re.compile(r"<i[^>]*></i>")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", re.I)

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
}


class Flat(HTMLParser):
    """Turns a page into tokens: ('t', text), ('a', href, text), ('nl',)."""
    BLOCK = {"div", "p", "li", "br", "tr", "td", "th", "section", "article", "h1", "h2", "h3", "h4",
             "h5", "h6", "ul", "ol", "table", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens, self._skip, self._a = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._a = [dict(attrs).get("href", ""), []]
        elif tag in self.BLOCK:
            self.tokens.append(("nl",))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._a is not None:
            self.tokens.append(("a", self._a[0], re.sub(r"\s+", " ", "".join(self._a[1])).strip()))
            self._a = None
        elif tag in self.BLOCK:
            self.tokens.append(("nl",))

    def handle_data(self, data):
        if self._skip:
            return
        if self._a is not None:
            self._a[1].append(data)
        else:
            self.tokens.append(("t", data))


def flatten(html):
    p = Flat()
    p.feed(html)
    p.close()
    return p.tokens


def mdl_calendar_url(country_name, tz):
    return "https://mydramalist.com/episode-calendar?" + urllib.parse.urlencode(
        [("view", "small"), ("scope", "all"), ("tz", tz), ("country[]", country_name)])


def fetch_text(url, retries=3, timeout=45):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp1252", errors="replace")
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 2)
    raise last


def parse_mdl_calendar(html):
    """Returns [{id, slug, title, date, time, tz, label}]."""
    out = []
    day_matches = list(DAY_RE.finditer(html))
    for i, dm in enumerate(day_matches):
        try:
            current_date = datetime.strptime(dm.group(1).strip(), "%B %d, %Y").date()
        except ValueError:
            continue
        block_start = dm.end()
        block_end = day_matches[i + 1].start() if i + 1 < len(day_matches) else len(html)
        block = html[block_start:block_end]
        for show_id, slug, alt, time_raw, tz, title_text, label in CARD_RE.findall(block):
            time_clean = ICON_RE.sub("", time_raw).strip()
            tm = TIME_RE.search(time_clean)
            origin_time = None
            if tm:
                try:
                    t = datetime.strptime(tm.group(1).upper().replace(" ", ""), "%I:%M%p")
                    origin_time = [t.hour, t.minute]
                except ValueError:
                    pass
            out.append({"id": int(show_id), "slug": slug, "date": current_date.isoformat(),
                        "time": origin_time, "tz": tz.strip(),
                        "title": (title_text or alt or slug.replace("-", " ").title()).strip(),
                        "label": label.strip