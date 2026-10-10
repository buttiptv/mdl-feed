#!/usr/bin/env python3
"""
Standalone MyDramaList fetcher, meant to run on GitHub Actions (or any machine
with a clean, non-seedbox IP) rather than on the Release Trackarr server
itself, which MyDramaList blocks at the IP level regardless of headers or
TLS fingerprint.

Fetches the Japan and South Korea episode calendars, then fetches each
referenced show's own page (for its native title, original network, and
type -- Release Trackarr needs this to tell dramas from variety/reality
shows). Writes one mdl_broadcast.json combining both. Release Trackarr reads
that file's raw.githubusercontent.com URL instead of contacting MyDramaList
directly; all filtering by network/drama-type stays in Release Trackarr
itself, so changing that config needs no redeploy here.

No third-party dependencies -- standard library only, so the GitHub Actions
step needs nothing but a bare Python install.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser

MDL_COUNTRY = {"JP": "Japan", "KR": "South Korea"}
MDL_TZ = "Australia/Sydney"  # only affects which page-day an episode is listed under
MAX_SHOWS = 500               # safety cap on per-show detail fetches
SHOW_FETCH_DELAY = 0.4         # seconds between per-show fetches, be polite
OUTPUT = "mdl_broadcast.json"

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

# The calendar page lays out one <div id="dNN"> block per date (each starting
# with a "<small>Month D, Year</small>" heading), containing one "el-card" per
# show released that day. Each card's own release-time text (e.g. "01:00 AM"
# or "ALL DAY") is the show's real origin-market time; the "calendar-popover-
# content" tooltip nested inside each card is a hover preview of that show's
# OTHER nearby episodes (sometimes its whole run) and must be skipped over,
# not read as more real entries -- every <a> inside it looks like a genuine
# episode link, which is what silently produced garbage data at first.
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
    """Returns [{id, slug, title, date, time, tz, label}].
    time is [hour, minute] (24-hour) in the show's own broadcast-market clock,
    or null for an all-day release (movies mostly). date comes from each day
    block's own heading. label is MyDramaList's own release text for that card
    verbatim (e.g. "Episode 10", "Episode 2 - 1", "Movie") -- its numbering
    scheme (e.g. the "N - M" form used for some multi-part releases) isn't
    second-guessed here, just passed through."""
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
                        "label": label.strip()})
    return out


def parse_mdl_title(html):
    """Reads Native Title, Original Network, and Type from a show's own page."""
    lines, cur = [], []
    for tok in flatten(html):
        if tok[0] == "nl":
            if cur:
                lines.append(re.sub(r"\s+", " ", " ".join(cur)).strip())
            cur = []
        elif tok[0] == "t":
            cur.append(tok[1])
        else:
            cur.append(tok[2])
    if cur:
        lines.append(re.sub(r"\s+", " ", " ".join(cur)).strip())

    def field(label):
        for ln in lines:
            if ln.lower().startswith(label.lower()):
                return ln[len(label):].strip(" :")
        return ""
    return {"native": field("Native Title:"), "network": field("Original Network:"),
            "type": field("Type:")}


def diagnose(html):
    """Cheap signals for 'this wasn't the real calendar page', printed to the
    log so a zero-episode run can be debugged from the Actions output alone."""
    low = html.lower()
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    print("  page length: %d bytes" % len(html))
    print("  <title>: %s" % (re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "(none found)"))
    for needle in ("just a moment", "checking your browser", "cf-browser-verification",
                   "captcha", "access denied", "please verify you are a human", "attention required"):
        if needle in low:
            print("  SUSPECT: page text contains %r -- looks like a bot challenge page, not the calendar" % needle)
    if "episode-calendar" not in low and "mydramalist.com" not in low:
        print("  SUSPECT: page doesn't even mention mydramalist.com/episode-calendar -- probably not the real page")


def main():
    all_eps = []
    for n, (origin, country_name) in enumerate(MDL_COUNTRY.items()):
        if n:
            time.sleep(2)
        url = mdl_calendar_url(country_name, MDL_TZ)
        print("fetching calendar:", country_name, "...")
        try:
            html = fetch_text(url)
        except Exception as e:
            print("  failed:", e, file=sys.stderr)
            continue
        found = parse_mdl_calendar(html)
        print("  %d episodes found" % len(found))
        if not found:
            diagnose(html)
            with open("debug-%s.html" % origin.lower(), "w", encoding="utf-8") as f:
                f.write(html)
            print("  saved raw HTML to debug-%s.html for inspection" % origin.lower())
        all_eps.extend(found)

    if not all_eps:
        sys.exit("No episodes found for any country -- not overwriting the existing feed file.")

    show_ids = sorted({e["id"] for e in all_eps})[:MAX_SHOWS]
    print("fetching %d show detail pages..." % len(show_ids))
    shows = {}
    fails = 0
    for i, (show_id, slug) in enumerate((sid, next(e["slug"] for e in all_eps if e["id"] == sid))
                                        for sid in show_ids):
        if i:
            time.sleep(SHOW_FETCH_DELAY)
        try:
            html = fetch_text("https://mydramalist.com/%d-%s" % (show_id, slug), retries=2)
            shows[str(show_id)] = parse_mdl_title(html)
            fails = 0
        except Exception as e:
            fails += 1
            print("  could not fetch show %d (%s)" % (show_id, e), file=sys.stderr)
            if fails >= 10:
                print("  10 consecutive failures, stopping show detail fetches early", file=sys.stderr)
                break
    print("  %d/%d show pages fetched" % (len(shows), len(show_ids)))

    payload = {
        "fetched_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": all_eps,
        "shows": shows,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("wrote %s (%d episodes, %d shows)" % (OUTPUT, len(all_eps), len(shows)))


if __name__ == "__main__":
    main()
