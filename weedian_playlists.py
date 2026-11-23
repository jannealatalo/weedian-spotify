#!/usr/bin/env python3
"""
Turn Weedian's Bandcamp compilations into Spotify playlists.

Reads https://weedian420.bandcamp.com/music, picks the compilations to mirror
(by default the monthly "The Best Releases of ..." ones and the yearly
"The 100 Best Releases of ..."), finds each track on Spotify and creates or
updates one public playlist per compilation, in the compilation's order, with
the Bandcamp cover art.

State lives in data/:
  compilations.json  Bandcamp page -> title, tracklist, playlist id/url
  tracks.json        "artist|||title" -> Spotify URI (or null), with the
                     date it was looked up; misses are retried after a while,
                     since small bands often reach Spotify late.

Spotify credentials come from WEEDIAN_SPOTIFY_CLIENT_ID,
WEEDIAN_SPOTIFY_CLIENT_SECRET and WEEDIAN_SPOTIFY_REFRESH_TOKEN (set up by
spotify_authorize.py; on Windows they're also read from the saved user
environment). A --dry-run works without the refresh token.

Usage:
  python weedian_playlists.py                  # create/update playlists
  python weedian_playlists.py --dry-run        # only look up songs, show matches
  python weedian_playlists.py --limit 1        # handle at most 1 compilation
  python weedian_playlists.py --all            # also the "Trip to ..." etc. comps
  python weedian_playlists.py --match "Trip to Finland"   # pick by title regex
"""

import argparse
import base64
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
COMPS_FILE = DATA_DIR / "compilations.json"
TRACKS_FILE = DATA_DIR / "tracks.json"

BANDCAMP = "https://weedian420.bandcamp.com"
DEFAULT_MATCH = r"Best Releases of"
USER_AGENT = "Mozilla/5.0 (compatible; weedian-spotify playlist mirror)"

API = "https://api.spotify.com/v1"
# Development-mode Spotify apps have a low rate limit, and going over it can
# block the app for a day. Lookups are cached, so a capped run just continues
# where it left off next time.
MAX_SEARCHES_PER_RUN = 150   # 266 in one go got a 4-hour block
REQUEST_DELAY = 0.4      # seconds between Spotify calls
MAX_RETRY_WAIT = 60      # give up (and save) instead of waiting longer
RETRY_MISSES_AFTER = timedelta(days=45)
MIN_FOUND_SHARE = 0.3    # don't make a playlist out of a handful of songs


class RateLimited(Exception):
    pass


# --------------------------------------------------------------------------
# Helpers

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def env(name: str) -> str | None:
    """Read a variable from the process, or (Windows) the saved user env."""
    value = os.environ.get(name)
    if value or sys.platform != "win32":
        return value
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def norm(s: str) -> str:
    """Loose form for comparing names: no accents, case, punctuation or 'the'."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and")
    s = re.sub(r"^the\s+", "", s.strip())
    return re.sub(r"[^a-z0-9]", "", s)


def base_title(name: str) -> str:
    """'Song - Remastered 2011' / 'Song (feat. X)' / 'Song [Live]' -> 'song'."""
    return norm(re.split(r"\s+-\s+|\s*[(\[]", name)[0]) or norm(name)


def clean_artist(name: str) -> str:
    # "Buzzard (Doom Folk)" -> "Buzzard"; "X feat. Y" -> "X"
    name = re.sub(r"\s*[(\[].*?[)\]]", "", name)
    name = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", name, flags=re.I)[0]
    return name.strip()


def clean_title(name: str) -> str:
    name = re.sub(r"\s*[(\[](?:feat|ft)\.?\s.*?[)\]]", "", name, flags=re.I)
    return name.strip()


# --------------------------------------------------------------------------
# Bandcamp

def bandcamp_get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    time.sleep(1)  # be polite
    return resp.text


def list_releases() -> list[dict]:
    """All releases on the music page, newest first: {url, title, art_id}."""
    page = bandcamp_get(f"{BANDCAMP}/music")
    releases, seen = [], set()

    # The first rows are plain HTML...
    for m in re.finditer(
            r'<li data-item-id="album-\d+".*?href="(/album/[^"]+)".*?'
            r'<p class="title">\s*(.*?)\s*(?:<br>|</p>)', page, re.S):
        url, title = m.group(1), html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        art = re.search(r"/img/a(\d+)_", m.group(0))
        if url not in seen:
            seen.add(url)
            releases.append({"url": BANDCAMP + url, "title": title,
                             "art_id": int(art.group(1)) if art else None})

    # ...and the rest are in a JSON blob the page renders with JavaScript.
    blob = re.search(r'data-client-items="([^"]+)"', page)
    for item in json.loads(html.unescape(blob.group(1))) if blob else []:
        url = item.get("page_url", "")
        if item.get("type") == "album" and url not in seen:
            seen.add(url)
            releases.append({"url": BANDCAMP + url, "title": item["title"],
                             "art_id": item.get("art_id")})
    return releases


def split_artist(raw: str) -> dict:
    """'Artist - Title' -> {artist, title}; also 'Artist- Title' / 'Artist -Title'."""
    for sep in (r"\s+-\s+", r"\s*-\s+|\s+-\s*", r"\s*[–—]\s*"):
        parts = re.split(sep, raw.strip(), maxsplit=1)
        if len(parts) == 2 and all(p.strip() for p in parts):
            return {"artist": parts[0].strip(), "title": parts[1].strip()}
    return {"artist": "", "title": raw.strip()}  # can't search without an artist


def fetch_tracklist(url: str) -> dict:
    """{title, art_id, released, tracks: [{artist, title}]} of one album page."""
    page = bandcamp_get(url)
    m = re.search(r'data-tralbum="([^"]+)"', page)
    if not m:
        raise RuntimeError(f"no tracklist found on {url}")
    data = json.loads(html.unescape(m.group(1)))
    tracks = []
    for t in data.get("trackinfo", []):
        raw = (t.get("title") or "").strip()
        if t.get("artist"):
            tracks.append({"artist": t["artist"].strip(), "title": raw})
        else:
            tracks.append(split_artist(raw))
    return {
        "title": data["current"]["title"],
        "art_id": data.get("art_id"),
        "released": data["current"].get("release_date") or data.get("album_release_date"),
        "tracks": tracks,
    }


# --------------------------------------------------------------------------
# Spotify

class Spotify:
    def __init__(self, need_user: bool):
        cid = env("WEEDIAN_SPOTIFY_CLIENT_ID")
        secret = env("WEEDIAN_SPOTIFY_CLIENT_SECRET")
        refresh = env("WEEDIAN_SPOTIFY_REFRESH_TOKEN")
        if not (cid and secret):
            raise SystemExit("WEEDIAN_SPOTIFY_CLIENT_ID and WEEDIAN_SPOTIFY_CLIENT_SECRET "
                             "are required (see README.md)")
        if need_user and not refresh:
            raise SystemExit("WEEDIAN_SPOTIFY_REFRESH_TOKEN is required to make playlists "
                             "(run spotify_authorize.py), or use --dry-run")
        data = ({"grant_type": "refresh_token", "refresh_token": refresh} if refresh
                else {"grant_type": "client_credentials"})
        resp = requests.post("https://accounts.spotify.com/api/token",
                             auth=(cid, secret), data=data, timeout=20)
        resp.raise_for_status()
        self.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        self.searches = 0

    def call(self, method: str, path: str, headers: dict | None = None, **kwargs):
        for attempt in range(5):
            resp = requests.request(method, API + path,
                                    headers={**self.headers, **(headers or {})},
                                    timeout=30, **kwargs)
            time.sleep(REQUEST_DELAY)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "5"))
                if wait > MAX_RETRY_WAIT:
                    raise RateLimited(f"Spotify asks to wait {wait} s ({wait / 3600:.1f} h)")
                time.sleep(wait + 1)
                continue
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise requests.HTTPError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:300]}",
                    response=resp)
            return resp.json() if resp.content else None
        raise RuntimeError(f"Spotify kept failing on {path}")

    def search(self, query: str) -> list[dict]:
        if self.searches >= MAX_SEARCHES_PER_RUN:
            raise RateLimited(f"search budget of {MAX_SEARCHES_PER_RUN} used")
        self.searches += 1
        return self.call("GET", "/search", params={"q": query, "type": "track",
                                                   "limit": 10})["tracks"]["items"]

    def find_track(self, artist: str, title: str) -> str | None:
        """Spotify URI of the artist's recording of a song, if any."""
        artist, title = clean_artist(artist), clean_title(title)
        want_artist, want_title = norm(artist), base_title(title)
        if not want_artist or not want_title:
            return None

        def pick(items: list[dict], strict: bool) -> str | None:
            by_artist = [t for t in items
                         if any(norm(a["name"]) == want_artist for a in t["artists"])]
            scored = sorted(
                ((difflib.SequenceMatcher(None, base_title(t["name"]), want_title).ratio(), t)
                 for t in by_artist), key=lambda x: -x[0])
            if not scored:
                return None
            score, best = scored[0]
            # The fielded search already filters by title, so be lenient there;
            # the free-text fallback needs a real title match.
            if score >= (0.85 if strict else 0.5):
                return best["uri"]
            return None

        # 1. Fielded search, the precise one.
        uri = pick(self.search(f'track:"{title}" artist:"{artist}"'), strict=False)
        if uri:
            return uri
        # 2. Free text catches small differences in spelling or punctuation.
        return pick(self.search(f"{artist} {title}"), strict=True)

    def upload_cover(self, playlist_id: str, art_id: int) -> None:
        # Spotify takes a base64 JPEG of at most 256 KB; Bandcamp's _16 is 700 px.
        for size in (16, 9):
            img = requests.get(f"https://f4.bcbits.com/img/a{art_id}_{size}.jpg", timeout=30)
            if img.ok and len(base64.b64encode(img.content)) <= 256_000:
                self.call("PUT", f"/playlists/{playlist_id}/images",
                          data=base64.b64encode(img.content),
                          headers={"Content-Type": "image/jpeg"})
                return


# --------------------------------------------------------------------------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="don't touch playlists")
    parser.add_argument("--limit", type=int, default=0, help="max compilations to handle")
    parser.add_argument("--all", action="store_true", help="mirror every compilation")
    parser.add_argument("--match", default=DEFAULT_MATCH,
                        help=f"title regex of compilations to mirror (default {DEFAULT_MATCH!r})")
    parser.add_argument("--verbose", "-v", action="store_true", help="list missing songs")
    parser.add_argument("--adopt", nargs=2, metavar=("TITLE_REGEX", "PLAYLIST_URL"),
                        help="use an existing playlist for a compilation that has none "
                             "(e.g. one left behind by a crashed run)")
    args = parser.parse_args()

    comps: dict = load_json(COMPS_FILE, {})
    tracks: dict = load_json(TRACKS_FILE, {})
    today = date.today()

    def save():
        save_json(COMPS_FILE, comps)
        save_json(TRACKS_FILE, dict(sorted(tracks.items())))

    releases = list_releases()
    wanted = [r for r in releases if args.all or re.search(args.match, r["title"], re.I)]
    print(f"{len(releases)} releases on Bandcamp, {len(wanted)} to mirror")

    try:
        sp = Spotify(need_user=not args.dry_run)
    except RateLimited as e:
        print(f"  ! Spotify is rate-limiting this app: {e}")
        return 0

    handled = 0
    try:
        for rel in wanted:
            if args.limit and handled >= args.limit:
                break
            handled += 1
            comp = comps.setdefault(rel["url"], {})
            if not comp.get("tracks"):  # tracklists don't change; fetch once
                comp.update(fetch_tracklist(rel["url"]))
            comp["tracks"] = [split_artist(t["title"]) if not t["artist"] else t
                              for t in comp["tracks"]]
            if args.adopt and re.search(args.adopt[0], comp["title"], re.I) \
                    and not comp.get("playlist"):
                pid = re.search(r"playlist[/:]([A-Za-z0-9]+)", args.adopt[1]).group(1)
                comp["playlist"] = {"id": pid, "url": f"https://open.spotify.com/playlist/{pid}",
                                    "hash": None}
                print(f"   (using existing playlist {pid} for {comp['title']})")

            uris, missing = [], []
            for t in comp["tracks"]:
                key = f"{t['artist']}|||{t['title']}"
                hit = tracks.get(key)
                stale = (hit and hit["uri"] is None and
                         date.fromisoformat(hit["checked"]) + RETRY_MISSES_AFTER <= today)
                if hit is None or stale:
                    uri = sp.find_track(t["artist"], t["title"]) if t["artist"] else None
                    hit = tracks[key] = {"uri": uri, "checked": today.isoformat()}
                if hit["uri"]:
                    if hit["uri"] not in uris:
                        uris.append(hit["uri"])
                else:
                    missing.append(f"{t['artist']} - {t['title']}")

            total = len(comp["tracks"])
            print(f"{comp['title']}: {len(uris)}/{total} songs found")
            if args.verbose:
                for m in missing:
                    print(f"     missing: {m}")
            comp["found"] = len(uris)
            if args.dry_run or not uris or len(uris) < total * MIN_FOUND_SHARE:
                continue

            name = f"Weedian – {comp['title']}"[:100]
            description = (
                f"Spotify version of Weedian's Bandcamp compilation ({len(uris)} of "
                f"{total} songs are on Spotify). Support the bands: {rel['url']}"
            )[:300]
            digest = hashlib.sha1(("\n".join(uris) + name + description).encode()).hexdigest()
            existing = comp.get("playlist")
            if existing and existing.get("hash") == digest and existing.get("cover"):
                continue
            if existing:
                if existing.get("hash") != digest:
                    sp.call("PUT", f"/playlists/{existing['id']}",
                            json={"name": name, "description": description})
                playlist_id, url = existing["id"], existing["url"]
            else:
                # Development-mode apps must use /me/playlists and .../items;
                # the older /users/{id}/playlists and .../tracks return 403.
                created = sp.call("POST", "/me/playlists",
                                  json={"name": name, "description": description,
                                        "public": True})
                playlist_id, url = created["id"], created["external_urls"]["spotify"]
                # Remember it right away, so a crash later can't lead to a duplicate.
                comp["playlist"] = {"id": playlist_id, "url": url, "hash": None}
                save()
            if not (existing or {}).get("cover"):
                art = comp.get("art_id") or rel.get("art_id")
                try:
                    if art:
                        sp.upload_cover(playlist_id, art)
                        comp["playlist"]["cover"] = True
                except (requests.RequestException, RateLimited) as e:
                    print(f"   (cover upload failed, will retry next run: {e})")
            if comp["playlist"].get("hash") != digest:
                # Replace the first 100, then append the rest 100 at a time.
                sp.call("PUT", f"/playlists/{playlist_id}/items", json={"uris": uris[:100]})
                for i in range(100, len(uris), 100):
                    sp.call("POST", f"/playlists/{playlist_id}/items",
                            json={"uris": uris[i:i + 100]})
                comp["playlist"]["hash"] = digest
            print(f"   -> {'updated' if existing else 'created'} {url}")
            save()

    except RateLimited as e:
        print(f"  ! stopping early: {e}. Progress is saved; run again later.")
    finally:
        save()

    print(f"✓ {sum(1 for c in comps.values() if c.get('playlist'))} playlists, "
          f"{len(tracks)} song lookups cached, {sp.searches} searches this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
