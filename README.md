# Omniarr

Sonarr + Radarr + Prowlarr + Bazarr in one app with a modern web UI.
Add a show or movie once and Omniarr finds it on your indexers, sends it to qBittorrent,
renames it into your library and fetches English subtitles.

### Docker Compose file

Copy this whole block into UGREEN's **Docker → Project → Create**. It's the same as
[`docker-compose.ugreen.yml`](docker-compose.ugreen.yml).

```yaml
# Omniarr for UGREEN NAS (UGOS Pro → Docker → Project → Create → paste this)
#
# Before you start:
#   1. In the Files app create these folders (all on the SAME volume so hardlinks work):
#        /volume1/docker/omniarr          ← Omniarr's settings & database
#        /volume1/docker/qbittorrent      ← qBittorrent's settings
#        /volume1/docker/warp             ← WARP tunnel settings
#        /volume1/data/torrents           ← qBittorrent downloads here
#        /volume1/data/media/tv           ← TV library   (point Plex/Jellyfin here)
#        /volume1/data/media/movies       ← Movie library
#   2. Check PUID/PGID: SSH in and run `id`. On UGOS the first user is usually uid=1000, gid=10.
#   3. Deploy, then open http://<NAS-IP>:8484

services:
  omniarr:
    image: ghcr.io/fudgeamin99/omniarr:latest
    container_name: omniarr
    environment:
      - PUID=1000
      - PGID=10
      - UMASK=002
      - TZ=Europe/London
      - OMNIARR_DEFAULT_PROXY=http://warp:1080
    volumes:
      - /volume1/docker/omniarr:/config
      - /volume1/data:/data
    ports:
      - "8484:8484"
    dns:                     # public DNS — gets around most UK internet-provider site blocks
      - 1.1.1.1
      - 1.0.0.1
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on:
      - flaresolverr
      - warp
    restart: unless-stopped

  # Free Cloudflare WARP tunnel. Blocked sites are routed through it automatically,
  # so your internet provider can't block your searches. No account needed.
  # (Only Omniarr's searches go through it — qBittorrent's downloads don't.)
  warp:
    image: caomingjun/warp:latest
    container_name: warp
    device_cgroup_rules:
      - 'c 10:200 rwm'
    cap_add:
      - MKNOD
      - AUDIT_WRITE
      - NET_ADMIN
    sysctls:
      - net.ipv6.conf.all.disable_ipv6=0
      - net.ipv4.conf.all.src_valid_mark=1
    environment:
      - WARP_SLEEP=2
    volumes:
      - /volume1/docker/warp:/var/lib/cloudflare-warp
    restart: unless-stopped

  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    dns:
      - 1.1.1.1
      - 1.0.0.1
    environment:
      - LOG_LEVEL=info
      - TZ=Europe/London
    restart: unless-stopped

  # qBittorrent — remove this block if you already run qBittorrent on the NAS.
  # In Omniarr use address http://qbittorrent:8080
  # First login: username admin, temporary password from the container log (Docker → qbittorrent → Log).
  # Then in qBittorrent: Options → Downloads → Default Save Path = /data/torrents
  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    environment:
      - PUID=1000
      - PGID=10
      - UMASK=002
      - TZ=Europe/London
      - WEBUI_PORT=8080
    volumes:
      - /volume1/docker/qbittorrent:/config
      - /volume1/data:/data
    ports:
      - "8080:8080"
      - "6881:6881"
      - "6881:6881/udp"
    restart: unless-stopped
```

## What it does

- **TV and movies in one library.** Show info comes from TVMaze (no key needed); movie info from TMDB (free key).
- **Built-in indexers (replaces Prowlarr).** It ships with Prowlarr's 579 site definitions and updates them weekly.
  About 430 of them work out of the box. The rest are music/book-only sites or need a login captcha.
  Any Torznab link also works, e.g. an existing Jackett.
- **Ready to search on first start.** A set of public indexers is added automatically, so searching works without any
  setup. Delete any you don't want; **Add recommended public sites** in Connections → Indexers puts them back.
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

## Install on a UGREEN NAS

### 0. One-time: make the image downloadable

The image is built automatically by GitHub Actions on every update and published as
`ghcr.io/fudgeamin99/omniarr:latest` (Intel and ARM). GitHub publishes new images as private, so make it public once:
GitHub → your profile → **Packages** → **omniarr** → **Package settings** → **Change visibility** → **Public**.

### 1. Create the folders

In the **Files** app, create these. Keep them all on the same volume so finished downloads can be
hardlinked into your library (instant, no extra space, keeps seeding):

```
/volume1/docker/omniarr          Omniarr's settings and database
/volume1/docker/qbittorrent      qBittorrent's settings
/volume1/docker/warp             WARP tunnel's settings
/volume1/data/torrents           where qBittorrent downloads to
/volume1/data/media/tv           TV library      (point Plex / Jellyfin here)
/volume1/data/media/movies       Movie library   (point Plex / Jellyfin here)
```

### 2. Deploy

1. Open **Docker → Project → Create** and name it `omniarr`.
2. Paste in the [Docker Compose file](#docker-compose-file) from the top of this page.
   - It runs as **PUID 1000 / PGID 10**, the usual first user on UGOS. If you've added users, SSH in, run `id`,
     and change the numbers for both `omniarr` and `qbittorrent` to match.
   - Already running qBittorrent on the NAS? Delete the `qbittorrent:` block and see
     [Using your existing qBittorrent](#using-your-existing-qbittorrent).
3. Click **Deploy**. Four containers start: `omniarr`, `warp`, `flaresolverr` and `qbittorrent`.

### 3. Set up qBittorrent (the included one)

1. Open `http://<NAS-IP>:8080`. The username is `admin` and the temporary password is in
   **Docker → Container → qbittorrent → Log**.
2. **Tools → Options → Web UI**: set your own password.
3. **Tools → Options → Downloads**: set **Default Save Path** to `/data/torrents`.

### 4. Finish setup in Omniarr

Open `http://<NAS-IP>:8484`. The dashboard shows a **Finish setup** card until everything is connected:

- **Library folders:** already set to `/data/media/tv` and `/data/media/movies`. Nothing to do.
- **Link qBittorrent:** Connections → qBittorrent → address `http://qbittorrent:8080`, plus the username and password
  from step 3 → **Test and save**.
- **Indexers:** some public indexers are already added for you, so searching works straight away. To add private trackers: Connections → **+ Add indexer** → pick the site → enter your login, cookie or
  API key → **Test and add**.
- **Movie info key:** create a free TMDB account → Settings → API → copy the key → Connections → Quality and info.
- **Subtitles (optional):** Podnapisi works straight away. For OpenSubtitles.com (best coverage), add your free account
  plus an API key from opensubtitles.com → Profile → API consumers.

Then go to **Add new**, search for a show or movie, and press **Add to library**.

### Updating

Every change pushed to this repo builds a new image. On the NAS go to **Docker → Project → omniarr**, then
**Pull image** / **Rebuild** (wording varies by UGOS version). Your settings in `/volume1/docker/omniarr` are kept.

### Using your existing qBittorrent

- **qBittorrent in Docker on the NAS:** give it the same `/volume1/data:/data` mount and the same PUID/PGID,
  set its save path to `/data/torrents`, and use `http://<NAS-IP>:8080` as the address in Omniarr.
- **qBittorrent on your PC:** use `http://<PC-IP>:8080`. Omniarr can't see your PC's disk, so after **Test and save** it will
  ask for a **folder mapping**. The PC's download folder must be one Omniarr can reach (e.g. a NAS share mounted
  on the PC), mapped to `/data/torrents`.

### Troubleshooting

| Problem | Fix |
|---|---|
| Deploy fails with "denied" or "unauthorized" pulling the image | The package is still private — do step 0. |
| "Can't see the downloaded files" in Activity | qBittorrent's save path isn't `/data/torrents`, or it doesn't have the `/volume1/data:/data` mount. |
| History says "Copied" instead of "Hardlinked" | qBittorrent and Omniarr need the same PUID/PGID, and everything under one `/volume1/data` folder. |
| Permission errors / files owned by root | PUID/PGID don't match your user — check with `id` over SSH. |
| A site says "Needs Cloudflare bypass" | Make sure the `flaresolverr` container is running; Omniarr's default address `http://flaresolverr:8191` is correct. |
| "Security certificate wasn't valid" / `CERTIFICATE_VERIFY_FAILED`, or a site looks blocked by your internet provider | UK providers block most public torrent sites. The compose file includes a free Cloudflare WARP tunnel, and Omniarr sends blocked sites through it automatically. If you deployed before it was added: redeploy with the new compose file, then in Connections → Staying unblocked set **Proxy address** to `http://warp:1080`, Save, and press **Test** on the indexer. |
| Anything else | **Activity → Logs** shows what happened. |

## Install (Docker Compose, build from source)

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
- Data lives in `/config` inside the container (`/volume1/docker/omniarr` on a UGREEN). It holds the SQLite database, logs and site definitions. Back that folder up.
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
