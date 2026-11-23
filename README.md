# weedian-spotify

Mirrors [Weedian](https://weedian420.bandcamp.com/music)'s Bandcamp compilations
as Spotify playlists. By default it covers the monthly *The Best Releases of …*
and yearly *The 100 Best Releases of …* comps, with one public playlist per
compilation. Each playlist keeps the compilation's track order and cover art.
Songs that aren't on Spotify are skipped and searched for again after 45 days.

## Setup (once)

1. **Spotify app.** On <https://developer.spotify.com/dashboard>, create an app
   (Web API) with the redirect URI `http://127.0.0.1:8888/callback`. A separate
   app keeps this job from using up the Dark Suomi app's rate limit.
2. **Authorize** the Spotify account that should own the playlists:

   ```
   pip install -r requirements.txt
   python spotify_authorize.py
   ```

   This asks for the app's client ID and secret, opens Spotify's login page and
   saves `WEEDIAN_SPOTIFY_CLIENT_ID`, `WEEDIAN_SPOTIFY_CLIENT_SECRET` and
   `WEEDIAN_SPOTIFY_REFRESH_TOKEN` as Windows user environment variables.
   Open a new terminal afterwards so they're picked up.

## Run

```
python weedian_playlists.py --dry-run -v   # only search; shows missing songs
python weedian_playlists.py                # create / update the playlists
python weedian_playlists.py --all          # also "Trip to …", "420", "Halloween" …
python weedian_playlists.py --match "Trip to Finland"
```

Each run does at most 300 Spotify searches to stay under the rate limit for
development-mode apps. Lookups are cached in `data/tracks.json`, so a big
backlog gets finished over a few runs.

## Automation

`.github/workflows/weekly.yml` runs every Monday and commits `data/` back to
the repository. Add the three `WEEDIAN_SPOTIFY_*` values as repository secrets.
