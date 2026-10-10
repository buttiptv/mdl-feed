#!/usr/bin/env python3
"""
MyDramaList broadcast + movie fetcher with:
- Auto-quarter detection
- Manual quarter flags
- Caching
- Parallel fetching (ThreadPoolExecutor)
- Predicted Episode 1 for Japanese dramas
- Movie scraping from quarter pages
- Separate movies array
- TMDB merge for movies
- Verbose logging
- Dry-run mode
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

MDL_COUNTRY = {"JP": "Japan", "KR": "South Korea"}
MDL_TZ = "Australia/Sydney"
MAX_SHOWS = 500
OUTPUT = "mdl_broadcast.json"

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
    re.S
)
ICON_RE = re.compile(r"<i[^>]*></i>")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", re.I)

MOVIE_TILE_RE = re.compile(
    r'<div class="poster-card">.*?'
    r'<a href="/(\d+)-([^"/]+)".*?>.*?</a>.*?'
    r'<div class="title">([^<]+)</div>.*?'
    r'<div class="released">([^<]+)</div>',
    re.S
)

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
}


def log(msg, verbose=False):
    if verbose:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}")


class Flat(HTMLParser):
    BLOCK = {
        "div", "p", "li", "br", "tr", "td", "th", "section", "article",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "dd", "dt"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self._skip = 0
        self._a = None

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
            href, parts = self._a
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            self.tokens.append(("a", href, text))
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
    return (
        "https://mydramalist.com/episode-calendar?"
        + urllib.parse.urlencode(
            [
                ("view", "small"),
                ("scope", "all"),
                ("tz", tz),
                ("country[]", country_name),
            ]
        )
    )


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


def cached_fetch(url, cache_dir, max_age_days=1, verbose=False):
    os.makedirs(cache_dir, exist_ok=True)

    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    html_path = os.path.join(cache_dir, key + ".html")
    meta_path = os.path.join(cache_dir, key + ".meta")

    now = datetime.utcnow()

    if os.path.exists(html_path) and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as mf:
                meta = json.load(mf)
            ts = datetime.fromisoformat(meta["fetched_utc"])
            if now - ts < timedelta(days=max_age_days):
                log(f"[cache] hit for {url}", verbose)
                with open(html_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass

    log(f"[cache] miss for {url}", verbose)
    html = fetch_text(url)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump({"fetched_utc": now.isoformat()}, mf)

    return html


def parse_mdl_calendar(html):
    out = []
    day_matches = list(DAY_RE.finditer(html))

    for i, dm in enumerate(day_matches):
        try:
            current_date = datetime.strptime(
                dm.group(1).strip(), "%B %d, %Y"
            ).date()
        except ValueError:
            continue

        block_start = dm.end()
        block_end = (
            day_matches[i + 1].start()
            if i + 1 < len(day_matches)
            else len(html)
        )
        block = html[block_start:block_end]

        for (
            show_id,
            slug,
            alt,
            time_raw,
            tz,
            title_text,
            label,
        ) in CARD_RE.findall(block):

            time_clean = ICON_RE.sub("", time_raw).strip()
            tm = TIME_RE.search(time_clean)
            origin_time = None

            if tm:
                try:
                    t = datetime.strptime(
                        tm.group(1).upper().replace(" ", ""),
                        "%I:%M%p",
                    )
                    origin_time = [t.hour, t.minute]
                except ValueError:
                    pass

            out.append({
                "id": int(show_id),
                "slug": slug,
                "date": current_date.isoformat(),
                "time": origin_time,
                "tz": tz.strip(),
                "title": (
                    title_text
                    or alt
                    or slug.replace("-", " ").title()
                ).strip(),
                "label": label.strip(),
            })

    return out


def parse_mdl_title(html):
    lines = []
    cur = []

    for tok in flatten(html):
        if tok[0] == "nl":
            if cur:
                lines.append(
                    re.sub(r"\s+", " ", " ".join(cur)).strip()
                )
            cur = []
        elif tok[0] == "t":
            cur.append(tok[1])
        else:
            cur.append(tok[2])

    if cur:
        lines.append(
            re.sub(r"\s+", " ", " ".join(cur)).strip()
        )

    def field(label):
        for ln in lines:
            if ln.lower().startswith(label.lower()):
                return ln[len(label):].strip(" :")
        return ""

    return {
        "native": field("Native Title:"),
        "network": field("Original Network:"),
        "type": field("Type:"),
        "premiere": field("Aired:"),
    }


def detect_quarters():
    today = date.today()
    year = today.year
    month = today.month

    if month <= 3:
        return [f"{year}Q1", f"{year}Q2"]
    elif month <= 6:
        return [f"{year}Q2", f"{year}Q3"]
    elif month <= 9:
        return [f"{year}Q3", f"{year}Q4"]
    else:
        return [f"{year}Q4", f"{year+1}Q1"]


def normalize_quarter(q):
    """Accepts "2026Q4", "2026-Q4", "Q4-2026", "q4 2026" etc. and returns
    MyDramaList's own path form, "q4-2026" (quarter first, hyphen, year) --
    confirmed from a real MDL URL, not guessed. Returns None if unparseable."""
    q = q.strip()
    m = re.match(r"(?i)^(\d{4})[-_ ]?q([1-4])$", q)
    if m:
        year, num = m.group(1), m.group(2)
        return "q%s-%s" % (num, year)
    m = re.match(r"(?i)^q([1-4])[-_ ]?(\d{4})$", q)
    if m:
        num, year = m.group(1), m.group(2)
        return "q%s-%s" % (num, year)
    return None


def build_quarter_urls(quarters):
    """Returns [(original_label, url), ...] -- paired, not two parallel lists,
    so an unparseable quarter can be skipped without desyncing anything."""
    pairs = []
    for q in quarters:
        norm = normalize_quarter(q)
        if not norm:
            print("  could not parse quarter %r, skipping" % q, file=sys.stderr)
            continue
        url = (
            f"https://mydramalist.com/episode-calendar/{norm}?"
            "view=large&scope=all&tz=Australia%2FSydney&sort=start-date"
            "&country%5B%5D=Japan&country%5B%5D=South+Korea"
        )
        pairs.append((q, url))
    return pairs


def diagnose(html, label):
    """Cheap signals for 'this wasn't the page we expected', printed to the
    log so a zero-match run can be debugged from the Actions output alone --
    the quarter/movie scraping below is unverified against real MDL markup,
    so this matters more here than it did for the (already-checked) weekly
    calendar page."""
    low = html.lower()
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    print("  [%s] page length: %d bytes" % (label, len(html)))
    print("  [%s] <title>: %s" % (label, re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "(none found)"))
    for needle in ("just a moment", "checking your browser", "cf-browser-verification",
                   "captcha", "access denied", "please verify you are a human", "attention required"):
        if needle in low:
            print("  [%s] SUSPECT: page text contains %r -- looks like a bot challenge page" % (label, needle))
    safe_label = re.sub(r"[^a-z0-9_-]+", "-", label.lower())
    with open("debug-%s.html" % safe_label, "w", encoding="utf-8") as f:
        f.write(html)
    print("  [%s] saved raw HTML to debug-%s.html for inspection" % (label, safe_label))


def fetch_calendar(url, cache_dir, verbose=False):
    html = cached_fetch(url, cache_dir, max_age_days=1, verbose=verbose)
    eps = parse_mdl_calendar(html)
    log(f"{len(eps)} episodes found from {url}", verbose)
    return eps, html


def parse_quarter_movies(html):
    # NOTE: MOVIE_TILE_RE's markup assumptions (poster-card/title/released)
    # are unverified against a real quarter page -- see diagnose() above,
    # which dumps the raw HTML here if this (and parse_mdl_calendar) both
    # come up empty on a real run. Also: the quarter URL requests both Japan
    # and South Korea together, but this regex doesn't capture which country
    # a given tile belongs to, so there's no safe per-movie tz/country to set
    # yet -- left as None rather than guessing "Japan Standard Time" for
    # everything (which would also use a display name, not the "Asia/Tokyo"-
    # style IANA zone the rest of this feed and release_tracker.py rely on).
    # Fix this once the real markup confirms where the country is shown.
    movies = []
    for mid, slug, title, released in MOVIE_TILE_RE.findall(html):
        released = released.strip()
        try:
            dt = datetime.strptime(released, "%b %Y")
        except Exception:
            continue

        movies.append({
            "id": int(mid),
            "slug": slug,
            "date": dt.date().isoformat(),   # month-only → first day
            "time": None,
            "tz": None,
            "title": title.strip(),
            "label": "Movie Release",
            "movie": True,
        })
    return movies


SHOW_FETCH_WORKERS = 3       # kept low on purpose -- see note below
SHOW_FETCH_DELAY = 0.4       # seconds, applied per worker before each fetch
_show_fetch_lock = threading.Lock()


def fetch_show_page(show_id, slug, cache_dir, verbose=False):
    # Up to MAX_SHOWS (500) of these can run per invocation. The whole reason
    # this fetcher runs on GitHub Actions instead of the seedbox is to get a
    # clean, non-blocked IP -- hammering MyDramaList with many concurrent,
    # unthrottled requests from that one IP is exactly the kind of traffic
    # pattern that gets an IP rate-limited or blocked, which would just
    # reproduce the original problem on the new infrastructure. A cache hit
    # (cached_fetch) skips this delay entirely since no request is made.
    url = f"https://mydramalist.com/{show_id}-{slug}"
    cache_path_exists = os.path.exists(
        os.path.join(cache_dir, hashlib.sha256(url.encode()).hexdigest()[:20] + ".html"))
    if not cache_path_exists:
        with _show_fetch_lock:
            time.sleep(SHOW_FETCH_DELAY)
    html = cached_fetch(url, cache_dir, max_age_days=7, verbose=verbose)
    return parse_mdl_title(html)


# Real network values we've actually seen (via the committed feed) include
# things like "NHK G", "Fuji TV Tokai TV", "BS-TBS" -- an exact-equality
# check against a short lowercase list (the original version of this
# function) would almost never match any of those. This mirrors the
# word-boundary substring match release_tracker.py's own network_match()
# uses for the same reason.
JP_NETWORKS = ["NHK", "NTV", "TBS", "TV Asahi", "Fuji TV", "Fuji Television", "TV Tokyo", "WOWOW"]


def network_match(network, allowed):
    for part in re.split(r"[,/]", network or ""):
        part = part.strip()
        for a in allowed:
            if re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(a), part, re.I):
                return part
    return None


def maybe_add_predicted_episode(show_id, slug, show_info, all_eps, verbose=False):
    """
    Adds a predicted Episode 1 for Japanese dramas that have a known premiere date
    but no episode entries in the calendar.

    NOTE: this reads an "Aired:" field from the show's own detail page that
    hasn't been confirmed against real MDL markup (parse_mdl_title's other
    three fields were verified this session; this one wasn't). If it never
    fires, check a saved show-detail page to see what the field is actually
    called and how it's formatted, rather than assuming this guess is right.
    """
    network = network_match(show_info.get("network"), JP_NETWORKS)
    if not network:
        return

    premiere = show_info.get("premiere")
    if not premiere:
        return

    try:
        dt = datetime.strptime(premiere, "%b %d, %Y %I:%M %p")
    except Exception:
        return

    for ep in all_eps:
        if ep["id"] == show_id and ep["date"] == dt.date().isoformat():
            return

    predicted = {
        "id": show_id,
        "slug": slug,
        "date": dt.date().isoformat(),
        "time": [dt.hour, dt.minute],
        # "Asia/Tokyo", not a display name like "Japan Standard Time" --
        # release_tracker.py's TZ_COUNTRY lookup keys on the IANA zone name
        # used everywhere else in this feed, so anything else would silently
        # fail to match and get dropped.
        "tz": "Asia/Tokyo",
        "title": "Episode 1",
        "label": "Predicted Episode 1",
        "predicted": True,
    }

    all_eps.append(predicted)
    if verbose:
        log(f"Added predicted Episode 1 for {slug} on {predicted['date']}", verbose)


def tmdb_fetch_movie(slug, cache_dir, verbose=False):
    """
    Fetch TMDB metadata for a movie using its slug.
    We search TMDB by title because MDL IDs do not map to TMDB IDs.
    """
    if not TMDB_API_KEY:
        return None

    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha256(("tmdb:" + slug).encode()).hexdigest()[:20]
    path = os.path.join(cache_dir, key + ".tmdb.json")

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    url = (
        "https://api.themoviedb.org/3/search/movie?"
        + urllib.parse.urlencode({"api_key": TMDB_API_KEY, "query": slug.replace("-", " ")})
    )

    try:
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"TMDB fetch failed for {slug}: {e}", verbose)
        return None

    if not data.get("results"):
        return None

    movie = data["results"][0]

    detail_url = f"https://api.themoviedb.org/3/movie/{movie['id']}?api_key={TMDB_API_KEY}"
    try:
        req = urllib.request.Request(detail_url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            details = json.loads(resp.read().decode("utf-8"))
    except Exception:
        details = movie

    with open(path, "w", encoding="utf-8") as f:
        json.dump(details, f)

    return details


def parse_args():
    ap = argparse.ArgumentParser(description="MDL broadcast + movie fetcher")
    ap.add_argument("--quarters", default="", help="Comma-separated quarters")
    ap.add_argument("--auto-quarters", action="store_true", help="Detect current + next quarter")
    ap.add_argument("--cache-dir", default=".mdl_cache", help="Cache directory")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without fetching")
    return ap.parse_args()


def main():
    args = parse_args()
    verbose = args.verbose

    log("Starting MDL fetcher", verbose)

    quarters = []
    if args.quarters:
        quarters.extend(q.strip() for q in args.quarters.split(","))
    if args.auto_quarters:
        quarters.extend(detect_quarters())

    quarter_urls = build_quarter_urls(quarters)

    all_eps = []
    movies = []

    if args.dry_run:
        print("Dry-run mode:")
        print("Would fetch:")
        for origin, country_name in MDL_COUNTRY.items():
            print(f"  - {country_name} calendar")
        for q, u in quarter_urls:
            print(f"  - Quarter calendar {q}: {u}")
        print("Would fetch show detail pages and TMDB metadata after parsing.")
        return

    # Fetch JP + KR calendars
    for origin, country_name in MDL_COUNTRY.items():
        url = mdl_calendar_url(country_name, MDL_TZ)
        log(f"Fetching {country_name} calendar", verbose)
        html = cached_fetch(url, args.cache_dir, max_age_days=1, verbose=verbose)
        eps = parse_mdl_calendar(html)
        log(f"{len(eps)} episodes", verbose)
        if not eps:
            diagnose(html, origin)
        all_eps.extend(eps)

    # Fetch quarter calendars one at a time (not in parallel -- these are
    # extra, speculative fetches on top of the two calendars above, and the
    # per-show detail fetches further down already account for the bulk of
    # the request volume; no need to add parallel load here too).
    for q, url in quarter_urls:
        log(f"Fetching quarter calendar {q}", verbose)
        try:
            eps, raw_html = fetch_calendar(url, args.cache_dir, verbose)
        except Exception as e:
            log(f"Quarter fetch failed for {q}: {e}", verbose)
            continue
        qmovies = parse_quarter_movies(raw_html)
        log(f"{len(eps)} episodes, {len(qmovies)} movies from quarter {q}", verbose)
        if not eps and not qmovies:
            # Both parsers came up empty -- this markup is unverified against
            # a real quarter page, so don't trust a silent zero; dump it.
            diagnose(raw_html, "quarter-%s" % re.sub(r"[^a-zA-Z0-9]+", "-", q))
        all_eps.extend(eps)
        movies.extend(qmovies)

    if not all_eps and not movies:
        sys.exit("No episodes or movies found — not overwriting existing feed.")

    # Fetch show detail pages in parallel
    show_ids = sorted({e["id"] for e in all_eps})[:MAX_SHOWS]
    slug_map = {e["id"]: e["slug"] for e in all_eps}

    log(f"Fetching {len(show_ids)} show pages", verbose)

    shows = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=SHOW_FETCH_WORKERS) as ex:
        futures = {
            ex.submit(fetch_show_page, sid, slug_map[sid], args.cache_dir, verbose): sid
            for sid in show_ids
        }
        for fut in concurrent.futures.as_completed(futures):
            sid = futures[fut]
            try:
                shows[str(sid)] = fut.result()
            except Exception as e:
                log(f"Show {sid} failed: {e}", verbose)

    # Add predicted Episode 1 for Japanese dramas
    for sid, info in shows.items():
        slug = slug_map[int(sid)]
        maybe_add_predicted_episode(int(sid), slug, info, all_eps, verbose)

    # Merge TMDB metadata into movies
    for m in movies:
        tm = tmdb_fetch_movie(m["slug"], args.cache_dir, verbose)
        if tm:
            m["tmdb"] = {
                "id": tm.get("id"),
                "title": tm.get("title"),
                "overview": tm.get("overview"),
                "poster": tm.get("poster_path"),
                "backdrop": tm.get("backdrop_path"),
                "runtime": tm.get("runtime"),
                "genres": [g["name"] for g in tm.get("genres", [])],
                "release_date": tm.get("release_date"),
            }

    payload = {
        "fetched_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": all_eps,
        "movies": movies,
        "shows": shows,
    }

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    log(
        f"Wrote {OUTPUT} ({len(all_eps)} episodes, {len(movies)} movies, {len(shows)} shows)",
        verbose,
    )


if __name__ == "__main__":
    main()
