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

# The quarter calendar page (view=large&sort=start-date) is structurally
# different from the weekly page: it's a premiere/start-date listing grouped
# by content type ("Drama" / "Movie" / "TV Show" <h2> headings), not a
# day-by-day broadcast schedule, and its cards use "el-card episode-card"
# (two classes) + "episode-card-large", not the weekly page's "el-card"
# (one class) + "episode-card-sm". Confirmed against real captured HTML
# (debug-quarter-2026q4.html / debug-quarter-2027q1.html) -- the old
# MOVIE_TILE_RE guess ("poster-card" / "released") matched nothing real;
# there is no such class on this page at all.
QUARTER_CARD_RE = re.compile(
    r'<div class="el-card episode-card">.*?'
    r'<a href="/(\d+)-([^"/]+)">\s*<img[^>]*alt="([^"]*)".*?'
    r'<div class="content-type">([^<]*)</div>.*?'
    r'<div class="airing-at">([^<]*)</div>\s*'
    r'<div class="release-time">([^<]*)</div>',
    re.S
)
QUARTER_COUNTRY = {"Korean": "KR", "Japanese": "JP"}
QUARTER_TZ = {"KR": "Asia/Seoul", "JP": "Asia/Tokyo"}

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


def parse_quarter_cards(html):
    """Parses the quarter calendar page into one row per card. Each card
    gives either an exact start date ("Aired on" + a full date like
    "October 1, 2026") or, for shows/movies that haven't started yet, only a
    month ("Airing in" + "Jan 2027") -- MDL doesn't expose a day for those,
    so callers should not treat "airing in" entries as day-accurate.
    """
    out = []
    for mid, slug, alt, content_type, status, release_text in QUARTER_CARD_RE.findall(html):
        content_type = content_type.strip()
        parts = content_type.split()
        if not parts:
            continue
        country = QUARTER_COUNTRY.get(parts[0])
        if not country:
            continue
        if content_type.endswith("TV Show"):
            kind = "TV Show"
        elif content_type.endswith("Movie"):
            kind = "Movie"
        elif content_type.endswith("Drama"):
            kind = "Drama"
        else:
            continue

        status = status.strip()
        release_text = release_text.strip()
        exact_date = None
        month_label = None
        if status == "Aired on":
            try:
                exact_date = datetime.strptime(release_text, "%B %d, %Y").date()
            except ValueError:
                pass
        else:
            month_label = release_text  # e.g. "Jan 2027" -- month only, no day

        out.append({
            "id": int(mid),
            "slug": slug,
            "title": alt.strip(),
            "kind": kind,
            "country": country,
            "status": status,
            "date": exact_date.isoformat() if exact_date else None,
            "month_label": month_label,
        })
    return out


def quarter_movies(cards):
    movies = []
    for c in cards:
        if c["kind"] != "Movie":
            continue
        if c["date"]:
            d = c["date"]
        elif c["month_label"]:
            try:
                d = datetime.strptime(c["month_label"], "%b %Y").date().isoformat()
            except ValueError:
                continue
        else:
            continue
        movies.append({
            "id": c["id"],
            "slug": c["slug"],
            "date": d,
            "time": None,
            "tz": QUARTER_TZ[c["country"]],
            "title": c["title"],
            "label": "Movie Release" if c["status"] == "Aired on" else "Expected Movie Release",
            "movie": True,
        })
    return movies


def quarter_premieres(cards, all_eps):
    """Adds a premiere/start-date entry for dramas and TV shows whose exact
    start date MDL has already confirmed ("Aired on" + a real date), when the
    weekly calendar didn't already carry an entry for that show on that day.
    This replaces the old per-show-detail-page "predicted Episode 1" guess:
    the quarter page's own "Aired on" date is MDL's own confirmed start date,
    not a guess, and it covers Korean shows too (the old guess only covered
    a fixed list of Japanese networks). "Airing in" (month-only) entries are
    skipped here -- not precise enough for a day-level calendar; they're only
    good enough for quarter_movies' coarser monthly fallback.
    """
    have = {(e["id"], e["date"]) for e in all_eps}
    added = []
    for c in cards:
        if c["kind"] not in ("Drama", "TV Show"):
            continue
        if c["status"] != "Aired on" or not c["date"]:
            continue
        if (c["id"], c["date"]) in have:
            continue
        added.append({
            "id": c["id"],
            "slug": c["slug"],
            "date": c["date"],
            "time": None,
            "tz": QUARTER_TZ[c["country"]],
            "title": c["title"],
            "label": "Premiere",
            "predicted": True,
        })
        have.add((c["id"], c["date"]))
    return added


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


# NOTE: the old per-show-detail-page "predicted Episode 1" feature
# (maybe_add_predicted_episode, keyed off an unverified "Aired:" field and a
# fixed Japanese-network list) has been replaced by quarter_premieres(),
# which uses the quarter calendar's own confirmed "Aired on" start dates --
# see above. It covers Korean shows too, not just a fixed JP network list.


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
    # the request volume; no need to add parallel load here too). The quarter
    # page is a premiere/start-date listing, not a day-by-day broadcast
    # schedule, so it's parsed separately (parse_quarter_cards) and only
    # contributes movies + premiere entries, not regular broadcast rows.
    quarter_cards = []
    for q, url in quarter_urls:
        log(f"Fetching quarter calendar {q}", verbose)
        try:
            raw_html = cached_fetch(url, args.cache_dir, max_age_days=1, verbose=verbose)
        except Exception as e:
            log(f"Quarter fetch failed for {q}: {e}", verbose)
            continue
        cards = parse_quarter_cards(raw_html)
        log(f"{len(cards)} cards from quarter {q}", verbose)
        if not cards:
            diagnose(raw_html, "quarter-%s" % re.sub(r"[^a-zA-Z0-9]+", "-", q))
        quarter_cards.extend(cards)

    qmovies = quarter_movies(quarter_cards)
    movies.extend(qmovies)
    log(f"{len(qmovies)} movies from quarter calendars", verbose)

    if not all_eps and not movies:
        sys.exit("No episodes or movies found — not overwriting existing feed.")

    # Fetch show detail pages in parallel. Includes quarter-premiere shows
    # (Drama/TV Show cards from the quarter calendars) even when the weekly
    # calendar had no entry for them yet, so release_tracker.py's network/
    # type filtering has real data for those too, rather than silently
    # dropping every premiere-only show for lack of a cached detail page.
    slug_map = {e["id"]: e["slug"] for e in all_eps}
    for c in quarter_cards:
        if c["kind"] in ("Drama", "TV Show"):
            slug_map.setdefault(c["id"], c["slug"])
    show_ids = sorted(slug_map.keys())[:MAX_SHOWS]

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

    # Add premiere entries from the quarter calendars' own confirmed
    # "Aired on" dates, for shows the weekly calendar didn't already cover
    # on that day (see quarter_premieres()).
    premieres = quarter_premieres(quarter_cards, all_eps)
    all_eps.extend(premieres)
    log(f"{len(premieres)} premiere entries added from quarter calendars", verbose)

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
