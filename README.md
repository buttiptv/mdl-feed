# mdl-feed

A tiny, dependency-free relay that fetches MyDramaList's Japan and South
Korea episode calendars (plus each referenced show's own page, for its
native title / network / type) and commits the result as `mdl_broadcast.json`.

This exists because MyDramaList blocks the IP range of the seedbox that
Release Trackarr runs on, regardless of request headers or TLS fingerprint —
but it does not block GitHub Actions' IPs. This repo runs the fetch there on
a schedule, and Release Trackarr reads the resulting JSON file over HTTPS
instead of contacting MyDramaList directly. All the filtering logic (which
networks count, what counts as a drama, the prime-time/late-night window)
stays in Release Trackarr itself and is not duplicated here, so changing
that config needs no changes in this repo.

## Setup

1. Create a **public** GitHub repository (public keeps the raw file URL
   simple and key-free; a private repo works too but Release Trackarr would
   then need a GitHub token to read it).
2. Push these files to it: `mdl_fetch.py`, `.github/workflows/fetch.yml`,
   this README.
3. On GitHub, go to the repo's **Actions** tab and enable workflows if asked.
4. Trigger the workflow once manually (Actions tab → "Fetch MyDramaList
   broadcast feed" → "Run workflow") to confirm it succeeds and that
   `mdl_broadcast.json` appears in the repo root.
5. The feed's raw URL will be:
   `https://raw.githubusercontent.com/<you>/<repo>/main/mdl_broadcast.json`
   Give that URL to Release Trackarr as `mdl_feed_url` in its config.

The workflow runs twice a day (02:17 and 14:17 UTC) and only commits when the
content actually changed, so idle days produce no commit noise. Trigger a
run manually any time from the Actions tab if you want fresher data sooner.

## Files

- `mdl_fetch.py` — the fetcher. Standard library only, no pip installs needed.
- `.github/workflows/fetch.yml` — the schedule that runs it and commits the result.
- `mdl_broadcast.json` — generated output (appears after the first successful run).
