# nextcloud-digital-frame

Turn an old tablet into a digital photo frame backed by your own Nextcloud.

A small service runs on a Raspberry Pi (or any always-on Linux box on the same
network). It asks Nextcloud which files you have marked as **favourites**,
downloads them, shrinks them to something an ageing tablet can actually render,
and serves a slideshow page. The tablet just opens a URL.

Star a photo in Nextcloud and it appears on the frame. Unstar it and it
disappears. That is the whole interface.

## Why not just open Nextcloud on the tablet?

Because on a tablet old enough to be spare, you usually can't. Nextcloud's web
frontend is a modern JavaScript bundle, and a browser engine from 2016 cannot
parse it — the page loads and then renders nothing at all. Rather than fight
that, this project moves all the work to the Pi and sends the tablet a
deliberately old-fashioned page.

The slideshow is written in strict ES5: no arrow functions, no `let`/`const`,
no template literals, no `fetch`, no `Promise`. It uses `XMLHttpRequest` and
`background-size` rather than `object-fit`, because that is what a 2016 engine
understands. **Please don't "modernise" `www/index.html`** — one arrow function
is enough to make the whole file fail to parse, and the symptom is a silent
black screen.

## What it does

- Pulls favourites from one or more Nextcloud accounts on the same server
- Downscales and re-encodes them (a 12 MP JPEG will make an old tablet's
  browser discard the tab; a 1400 px one will not)
- Shows a shuffled cycle — every photo appears once before any repeats
- Cross-fades between photos
- Captions each photo with its location and month/year, drawn in the bottom
  left corner **of the picture**, not of the screen, so it doesn't land on the
  letterbox bars beside a portrait shot
- Goes completely black during configurable quiet hours
- Keeps working when Nextcloud is unreachable, and keeps showing an account's
  photos if that one account fails to sync

## Tested on

This has only been tested with a **Raspberry Pi** serving an **iPad mini (1st
generation, iOS 9.3.5)**. Nothing here is specific to that hardware in
principle — it is a static page served over HTTP — but no other combination has
been tried, and the ES5 constraints are tuned to that browser's limits.

## Requirements

**On the Pi:**

- Python 3.9 or newer
- `python3-requests` and `python3-pil`
- Optional: `pillow-heif`, if your library contains `.heic` files from iPhones.
  Without it those files are skipped with a warning rather than breaking the
  sync.

**On Nextcloud:** an app password per account (Settings → Personal → Security →
"Create new app password"). Never the login password. Each account must
generate its own.

## Layout

```
backend/photoframe.py       the service: syncs from Nextcloud, serves the page
backend/photo-frame.env     configuration template
service/photo-frame.service systemd unit
www/index.html              the slideshow page
```

At runtime the Pi keeps its working files under `BASE_DIR`
(default `/opt/photo-frame`):

```
state.json      what has been downloaded, and its ETag and caption
geocache.json   coordinate -> place name, so each location is looked up once
www/photos/     the downscaled JPEGs
www/photos.json the manifest the page reads
```

## Installation

```bash
sudo apt install -y python3-requests python3-pil

sudo mkdir -p /opt/photo-frame/www
sudo chown -R $USER:$USER /opt/photo-frame

cp backend/photoframe.py /opt/photo-frame/
cp www/index.html /opt/photo-frame/www/

sudo cp backend/photo-frame.env /etc/photo-frame.env
sudo chown root:root /etc/photo-frame.env
sudo chmod 600 /etc/photo-frame.env
sudo nano /etc/photo-frame.env        # fill in NC_URL, NC_USER_1, NC_PASS_1
```

Test it in the foreground before making it a service. The env file is
root-only, so read it through `sudo` while keeping the shell as your own user —
running the script itself under `sudo` would leave root-owned files in
`www/photos/` that the service later cannot delete:

```bash
set -a && . <(sudo cat /etc/photo-frame.env) && set +a
python3 /opt/photo-frame/photoframe.py
```

You want a line reporting how many favourites were found, then a sync summary.
An HTTP 401 means the app password is wrong. The first sync is slow — it
downloads and re-encodes everything, and reverse geocoding is rate-limited to
one lookup per second. Later syncs match on ETag and skip unchanged files.

Then install the service:

```bash
sudo cp service/photo-frame.service /etc/systemd/system/
sudo nano /etc/systemd/system/photo-frame.service   # set User= and Group=
sudo systemctl daemon-reload
sudo systemctl enable --now photo-frame
journalctl -u photo-frame -f
```

Give the Pi a fixed address while you're here — a DHCP reservation in your
router, keyed to its MAC. The tablet points at a hardcoded IP.

## Tablet setup

Open `http://<pi-ip>:8080` (or whatever `PORT` you set), then:

- **Share → Add to Home Screen.** Launching from that icon runs the page
  without browser chrome, which on a small screen is a real fraction of the
  picture. The page carries the meta tags that enable this.
- **Auto-Lock → Never**, or the screen sleeps within minutes.
- **Auto-Brightness off**, then set the level by hand, or it will dim in the
  evening and look broken.
- **Do Not Disturb on**, so notification banners don't slide over the photos.
- Leave it on the charger.

To lock the tablet to the frame, use its kiosk or guided-access mode. On iOS
that's Settings → General → Accessibility → Guided Access; triple-click the
home button after launching. Note that such a session typically does not
survive a reboot.

Tap anywhere to skip to the next photo.

## Configuration

All settings live in `/etc/photo-frame.env`. systemd reads it as literal
`KEY=VALUE` pairs — no quotes, no spaces around the `=`.

### Nextcloud

| Variable | Default | Meaning |
| --- | --- | --- |
| `NC_URL` | — | Base URL of the server, no trailing slash |
| `NC_USER_1`, `NC_PASS_1` | — | First account and its app password |
| `NC_USER_2`, `NC_PASS_2` | — | Second account, and so on |

Favourites from every configured account are pooled into one shuffled stream.
Accounts are isolated from each other's failures: if one is unreachable, its
photos stay on the frame instead of being pruned as "no longer favourited",
and the log names the account that failed.

A photo favourited by two accounts appears twice, since favourites are
per-user and the two are different paths.

### Frame behaviour

| Variable | Default | Meaning |
| --- | --- | --- |
| `PHOTO_INTERVAL` | `30` | Seconds each photo stays on screen |
| `SYNC_INTERVAL` | `3600` | Seconds between checks for new favourites |
| `MAX_DIM` | `1400` | Longest edge in pixels after downscaling |
| `JPEG_QUALITY` | `82` | Re-encode quality |

`MAX_DIM` is the setting that matters most for an old tablet. A 1024×768
non-retina screen cannot show more than 1400 px of detail, so raising it costs
memory on the tablet rather than visible quality. If the frame shows a black
screen where a computer shows the photos fine, try lowering it to `1024`.

### Quiet hours

| Variable | Default | Meaning |
| --- | --- | --- |
| `QUIET_START` | unset | e.g. `23:00` |
| `QUIET_END` | unset | e.g. `07:00` |

The screen goes black between these times. The window may cross midnight.
Leave both blank to disable; a malformed value, or a start equal to the end, is
rejected with a logged warning rather than guessed at.

The comparison happens on **the tablet's** clock, not the Pi's. The Pi
publishes the window and the page checks it against local time every 30
seconds, so the transition lands on the minute rather than whenever the
manifest is next fetched, quiet hours keep working if the Pi goes offline
overnight, and only the tablet's timezone and DST matter.

This blanks the picture, not the backlight — no web page can switch a tablet's
screen off, so expect dark grey rather than true black. Taps are deliberately
inert during quiet hours so a stray touch at 3am doesn't light the room; there
is a commented line in the click handler to change that.

### Captions

| Variable | Default | Meaning |
| --- | --- | --- |
| `GEOCODE` | `1` | Set to `0` to disable location lookups entirely |
| `GEOCODE_URL` | Nominatim | Point at your own instance if you prefer |
| `GEOCODE_LANG` | `en` | Language for place names |
| `GEOCODE_AGENT` | — | Nominatim asks for a contact address here |

**Date** comes from the path first — any `<year>/<month>` pair of segments, so
`/photos/2024/03/`, `/Photos/2024/March/` and the German `/Fotos/2024/März/`
all work. Failing that it falls back to EXIF `DateTimeOriginal`, then to the
file's modified time. The EXIF step matters because Nextcloud's modified time
is often the upload date, which would caption a 2008 photo with last year.

**Location** comes from the photo's EXIF GPS tags, reverse geocoded to city and
country. Photos without GPS — most scans, and anything from a camera without a
GPS module — simply show the date alone.

If you enable geocoding, coordinates are sent to a third-party service.
They're rounded to roughly a kilometre first, both for privacy and because it
means a whole holiday in one town costs a single lookup. Results are cached in
`geocache.json` and negative results are cached too.

### Server

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8000` | Port to serve on |
| `BIND` | `0.0.0.0` | Interface to bind |
| `BASE_DIR` | `/opt/photo-frame` | Where the code and `www/` live |

There is no authentication. Keep this on your LAN and make sure no router port
forward points at it.

## Operating it

```bash
journalctl -u photo-frame -f        # watch it work
sudo systemctl restart photo-frame  # after editing the env file
```

Editing `/etc/photo-frame.env` only needs a restart — `daemon-reload` is for
changes to the `.service` file itself.

**Changing `MAX_DIM` or `JPEG_QUALITY` has no visible effect on its own**,
because existing photos match on ETag and are skipped. Force a full re-encode
by deleting the state file:

```bash
sudo systemctl stop photo-frame
rm /opt/photo-frame/state.json
sudo systemctl start photo-frame
```

New favourites reach the frame within `SYNC_INTERVAL`, and the page re-reads
the manifest every 15 minutes, so allow up to that before concluding something
is wrong.

**Unfavouriting deletes the local copy** at the next sync. Nothing is ever
deleted on Nextcloud — the service only issues `REPORT` and `GET`. If you would
rather the Pi accumulate an archive, the `os.remove` call in the prune loop in
`sync_once` is the only place removal happens.

## Troubleshooting

**Locations are missing on the Pi but work on your laptop.** Debian Bullseye
ships Pillow 8.1.2, the last release with a bug that makes
`Exif.get_ifd(0x8825)` return `None` for every photo: it looks the GPS offset
up through `__getitem__`, which has a special case that calls `get_ifd` back
recursively, so `seek()` receives a dict instead of an integer and the
resulting `TypeError` is swallowed. Fixed in 8.2.0. `gps_from_exif` works
around it by trying both access paths, but upgrading is cleaner:

```bash
python3 -c "import PIL; print(PIL.__version__)"
pip3 install --user --upgrade Pillow
```

Install as the same user as `User=` in the service file, or the service won't
see it. The same applies to `pillow-heif`.

**Nothing on screen, black page.** Check `photos.json` is being served and is
not empty, then check the browser console if you can attach one. If the photos
load on a computer but not the tablet, suspect `MAX_DIM` — the tablet is
running out of memory decoding them.

**Some photos are skipped.** `journalctl -u photo-frame | grep skipping` names
them. Usually HEIC without `pillow-heif`, or a RAW file Pillow can't read.

**An account's photos all vanished.** They shouldn't — a failed sync carries
the previous entries forward. If they did, that account's favourites really are
empty; check you starred files rather than folders, since favourited folders
are skipped.

**`Permission denied` sourcing the env file.** It's `chmod 600` and root-owned
by design. Use `. <(sudo cat /etc/photo-frame.env)`, or just let systemd start
it, which reads the file as root anyway.

## Contributing

Yes please, I'm open for ideas and PRs.

Most of the code (and README) was generated by an LLM and reviewed by me.

## Licence

MIT.
