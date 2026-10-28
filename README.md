# Omniarr

Sonarr + Radarr + Prowlarr + Bazarr in one app with a modern web UI.
Add a show or movie once and Omniarr finds it on your indexers, sends it to qBittorrent,
renames it into your library and fetches English subtitles.

## What it does

- **TV and movies in one library.** Show info comes from TVMaze (no key needed); movie info from TMDB (free key).
- **Built-in indexers (replaces Prowlarr).** It ships with Prowlarr's 579 site definitions and updates them weekly.
  About 430 of them work out of the box. The rest are music/book-only sites or need a login captcha.
  Any Torznab link also works, e.g. an existing Jackett.
- **Built-in subtitles (replaces Bazarr).** Subtitles come from OpenSubtitles.com and Podnapisi, matched to your exact release.
  Files that already have English subtitles inside them are skipped.
- **English only.** By default it picks English audio or English dubs. Foreign-only and subbed-only anime are rejected.
  You can change this per show or movie.
- **Staying unblocked:**
  - FlareSolverr handles Cloudflare checks, and turns itself on per site when one is needed.
  - Requests to the same site are spaced out, with optional daily limits per indexer.
  - Sites that keep failing are paused automatically (5 min, rising to 6 h).
  - Search results are cached for 30 minutes.
  - An optional proxy covers sites your internet provider blocks, and ISP block pages are detected.
- **Library layout:** `tv/Show (Year)/Season 01/Show - S01E01 - Title [Quality].mkv` and
  `movies/Movie (Year)/Movie (Year) [Quality].mkv`, plus `.en.srt` beside each video. Specials go in `Season 00`.
- **Downloads:**
  - Finished downloads are hardlinked into the library (no extra space, keeps seeding).
  - Season packs are split into episodes, and RAR'd releases are unpacked.
  - Stalled or failed downloads are blocked and another release is searched for.
  - Torrents are removed once the site's seeding goals are met.

## Install (Docker Compose)

1. Put all your media and downloads under **one folder** on the host (e.g. `/srv/data`):
   ```
   /srv/data/torrents        qBittorrent's download folder
   /srv/data/media/tv
   /srv/data/media/movies
   ```
   One folder is what makes hardlinks possible. If downloads and library are on different drives, Omniarr copies instead.
2. Edit `docker-compose.yml`. Change `/srv/data`, and set `PUID`/`PGID` to your user (run `id` on the host).
3. Start it:
   ```
   docker compose up -d --build
   ```
4. Open **http://your-server:8484** and follow the **Finish setup** card on the dashboard:
   - **Library folders:** already set to `/data/media/tv` and `/data/media/movies`.
   - **Indexers:** Connections → **+ Add indexer**. Pick your trackers and enter your login or API key.
   - **Link qBittorrent:** see below.
   - **Movie info key:** a free TMDB key from themoviedb.org → Settings → API.

### Linking qBittorrent

In qBittorrent go to **Tools → Options → Web UI**, tick **Web User Interface** and set a username and password.
Then in Omniarr → Connections → qBittorrent:

| qBittorrent runs… | Address to use |
|---|---|
| in the same compose file (uncomment the block) | `http://qbittorrent:8080` |
| as a normal app on the same computer | `http://host.docker.internal:8080` |
| on another computer | `http://<that-computer's-IP>:8080` |

Press **Test and save**. If qBittorrent saves downloads to a path Omniarr can't see, Omniarr tells you and opens
**Folder mapping**. For example, qBittorrent on Windows saving to `D:\Downloads` maps to `/data/torrents`
if that drive is what you mounted as `/srv/data/torrents`.

For hardlinks, run qBittorrent with the **same PUID/PGID** as Omniarr. Otherwise Linux blocks the hardlink and Omniarr copies the file.

### Subtitles

Podnapisi works without an account. OpenSubtitles.com has the best coverage. It needs a free account plus an API key
(opensubtitles.com → Profile → API consumers). Add both under Connections → Subtitles.

## Good to know

- Language is judged from tags in the release name (`FRENCH`, `MULTi`, `Dual Audio`, `English Dub`…), since that's
  all an indexer shows before downloading. Untagged releases of English shows count as English. For anime and foreign
  films, "English only" means an English dub is required. Switch that title to "Any language" if you'd rather have subtitles.
- **Pick a release myself** on any show, season or episode lists every result, with the reason any were rejected.
  You can still download a rejected one.
- Data lives in `./config` (SQLite database, logs, site definitions). Back that folder up.
- Optional login: Connections → Login.

## Running without Docker

Python 3.13:
```
pip install -r requirements.txt
OMNIARR_CONFIG=./config python -m omniarr        # http://localhost:8484
```
To also unpack RAR releases, install `unar` (Linux/macOS) or 7-Zip (`7z`).

## Tests

```
python -m tests.run          # parser + real Prowlarr definitions against fixture pages
python -m tests.e2e          # full end-to-end run against stand-in qBittorrent, indexers, FlareSolverr, subtitle sites
```

Site definitions come from [Prowlarr/Indexers](https://github.com/Prowlarr/Indexers). Metadata comes from TVMaze and TMDB.
