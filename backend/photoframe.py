#!/usr/bin/env python3
"""
Nextcloud favourites photo frame.

Runs one process that does two things:

  1. Every SYNC_INTERVAL seconds, asks Nextcloud for every file each configured
     user has marked as a favourite (WebDAV REPORT with an oc:favorite filter
     rule), downloads the images, re-encodes them small enough for an old iPad,
     and works out a caption (place + month/year) for each.
  2. Serves a static directory containing those images plus a plain ES5
     slideshow page.

Multiple accounts on the same server are supported; their favourites are
pooled into one shuffled stream. Accounts are isolated from each other's
failures: if one cannot be reached, its photos stay in the frame rather than
being pruned as "no longer favourited".

The downscaling is the point of the exercise: an iPad Mini 1 has a 1024x768
non-retina screen and 512 MB of RAM shared with the GPU. Handing it a 12
megapixel JPEG makes Safari discard the tab. Handing it a 1400px JPEG does not.

Configuration comes from the environment -- see photo-frame.env.
"""

import email.utils
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import requests
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False


def env(name, default=None, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return cast(raw)


NC_URL = env("NC_URL", "").rstrip("/")

BASE_DIR = env("BASE_DIR", "/opt/photo-frame")
WWW_DIR = os.path.join(BASE_DIR, "www")
PHOTO_DIR = os.path.join(WWW_DIR, "photos")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
GEOCACHE_PATH = os.path.join(BASE_DIR, "geocache.json")
MANIFEST_PATH = os.path.join(WWW_DIR, "photos.json")

SYNC_INTERVAL = env("SYNC_INTERVAL", 3600, int)
PHOTO_INTERVAL = env("PHOTO_INTERVAL", 30, int)
MAX_DIM = env("MAX_DIM", 1400, int)
JPEG_QUALITY = env("JPEG_QUALITY", 82, int)

GEOCODE = env("GEOCODE", "1") == "1"
GEOCODE_URL = env("GEOCODE_URL", "https://nominatim.openstreetmap.org/reverse")
GEOCODE_LANG = env("GEOCODE_LANG", "en")
# Nominatim asks for a contact address in the User-Agent so they can get in
# touch before blocking you. Put a real one here if you geocode a lot.
GEOCODE_AGENT = env("GEOCODE_AGENT", "nextcloud-photo-frame/1.0")

PORT = env("PORT", 8000, int)
BIND = env("BIND", "0.0.0.0")

DAV = "{DAV:}"

REPORT_BODY = """<?xml version="1.0"?>
<oc:filter-files xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"
                 xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <d:getcontenttype/>
    <d:getetag/>
    <d:getlastmodified/>
    <oc:fileid/>
  </d:prop>
  <oc:filter-rules>
    <oc:favorite>1</oc:favorite>
  </oc:filter-rules>
</oc:filter-files>
"""

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Folder names are matched in English and German, with and without accents,
# plus common three-letter abbreviations. Extend if your library uses
# something else.
MONTH_LOOKUP = {}
for _i, _name in enumerate(MONTH_NAMES, start=1):
    MONTH_LOOKUP[_name.lower()] = _i
    MONTH_LOOKUP[_name.lower()[:3]] = _i
for _i, _name in enumerate(
    ["januar", "februar", "marz", "april", "mai", "juni",
     "juli", "august", "september", "oktober", "november", "dezember"],
    start=1,
):
    MONTH_LOOKUP.setdefault(_name, _i)

log = logging.getLogger("photoframe")


# ------------------------------------------------------------------ config

def discover_accounts():
    """
    Collect accounts from the environment.

    Numbered pairs, in numeric order:
        NC_USER_1 / NC_PASS_1
        NC_USER_2 / NC_PASS_2
    Plus the unnumbered NC_USER / NC_PASS from the original single-account
    setup, which still works and is treated as the first account.

    Numbered variables rather than one delimited string, because app passwords
    contain characters that would need escaping in any delimiter scheme.
    """
    accounts = []

    user = env("NC_USER")
    password = env("NC_PASS")
    if user and password:
        accounts.append((user, password))

    numbered = []
    for key in os.environ:
        match = re.fullmatch(r"NC_USER_(\d+)", key)
        if match:
            numbered.append(int(match.group(1)))

    for n in sorted(numbered):
        user = env("NC_USER_%d" % n)
        password = env("NC_PASS_%d" % n)
        if not user:
            continue
        if not password:
            log.warning("NC_USER_%d set but NC_PASS_%d missing -- skipping", n, n)
            continue
        if any(user == existing for existing, _ in accounts):
            log.warning("account %s configured more than once -- ignoring duplicate", user)
            continue
        accounts.append((user, password))

    return accounts


ACCOUNTS = discover_accounts()


# ---------------------------------------------------------------- Nextcloud

def list_favourites(session, user):
    """Return [(href, etag, lastmodified)] for every favourited image."""
    url = "%s/remote.php/dav/files/%s/" % (NC_URL, user)
    resp = session.request(
        "REPORT",
        url,
        data=REPORT_BODY.encode("utf-8"),
        headers={"Content-Type": "application/xml", "Depth": "infinity"},
        timeout=60,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    found = []
    for node in root.findall(DAV + "response"):
        href_el = node.find(DAV + "href")
        if href_el is None or not href_el.text:
            continue

        # A response can carry several propstat blocks (one per HTTP status),
        # so search across all of them rather than assuming the first.
        ctype = etag = lastmod = None
        for prop in node.iter(DAV + "prop"):
            for tag, setter in (
                ("getcontenttype", "ctype"),
                ("getetag", "etag"),
                ("getlastmodified", "lastmod"),
            ):
                el = prop.find(DAV + tag)
                if el is not None and el.text:
                    if setter == "ctype":
                        ctype = el.text
                    elif setter == "etag":
                        etag = el.text
                    else:
                        lastmod = el.text

        # Folders can be favourited too and have no content type. Skip
        # non-images so a favourited folder of documents stays out of the frame.
        if not ctype or not ctype.lower().startswith("image/"):
            continue

        found.append((href_el.text, etag or "", lastmod or ""))

    return found


def download(session, href):
    # href arrives percent-encoded and absolute (/remote.php/dav/...). Pass it
    # through as-is rather than decode-then-re-encode, which mangles anything
    # with a '+' or '#' in the filename.
    log.info("downloading file %s", href)
    resp = session.get(NC_URL + href, timeout=120)
    resp.raise_for_status()
    return resp.content


# -------------------------------------------------------------------- date

def date_from_path(href):
    """
    Find a year/month pair in the remote path, e.g. /photos/2024/03/ or
    /Photos/2024/March/.

    Rather than requiring the literal /photos/ prefix, this looks for any
    segment that is a plausible year immediately followed by a month -- which
    tolerates libraries nested a level deeper than expected.
    """
    segments = [s for s in unquote(href).split("/") if s]

    for i in range(len(segments) - 1):
        year_seg = segments[i]
        if not re.fullmatch(r"(19|20)\d{2}", year_seg):
            continue

        month = month_from_segment(segments[i + 1])
        if month:
            return int(year_seg), month

    return None


def month_from_segment(segment):
    segment = segment.strip().lower()

    if segment.isdigit():
        n = int(segment)
        return n if 1 <= n <= 12 else None

    # Handles "03 - March", "03_Maerz" and similar.
    leading = re.match(r"(\d{1,2})\b", segment)
    if leading:
        n = int(leading.group(1))
        if 1 <= n <= 12:
            return n

    cleaned = segment.replace("ä", "a").replace("ae", "a")
    for name, number in MONTH_LOOKUP.items():
        if cleaned == name or cleaned.startswith(name):
            return number

    return None


def date_from_exif(exif):
    """DateTimeOriginal, format 'YYYY:MM:DD HH:MM:SS'."""
    if not exif:
        return None
    raw = exif.get(36867) or exif.get(306)  # DateTimeOriginal, then DateTime
    if not raw or not isinstance(raw, str):
        return None
    match = re.match(r"(\d{4}):(\d{2})", raw)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) if 1 <= month <= 12 else None


def date_from_lastmod(lastmod):
    if not lastmod:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(lastmod)
        return dt.year, dt.month
    except (TypeError, ValueError):
        return None


def format_date(pair):
    if not pair:
        return ""
    year, month = pair
    return "%s %d" % (MONTH_NAMES[month - 1], year)


# ---------------------------------------------------------------- location

_geo_lock = threading.Lock()
_geo_last_call = [0.0]


def gps_from_exif(exif):
    """Return (lat, lon) in signed decimal degrees, or None."""
    if not exif:
        return None

    # Pillow >= 8.2 returns the GPS IFD from get_ifd() and a bare offset from
    # exif[0x8825]. Pillow 8.1.2 -- what Debian Bullseye ships -- is exactly
    # the other way round, because of a recursion bug in its __getitem__ that
    # makes get_ifd() swallow a TypeError and return None. Try both and take
    # whichever actually looks like a GPS IFD.
    gps = None
    for getter in (lambda: exif.get_ifd(0x8825), lambda: exif.get(0x8825)):
        try:
            candidate = getter()
        except Exception:
            continue
        if isinstance(candidate, dict) and 2 in candidate and 4 in candidate:
            gps = candidate
            break

    if not gps:
        return None

    try:
        lat = dms_to_degrees(gps[2], gps[1])
        lon = dms_to_degrees(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None

    if lat is None or lon is None:
        return None
    # Exactly (0, 0) is Null Island -- effectively always a broken tag rather
    # than a real photo taken in the Gulf of Guinea.
    if abs(lat) < 0.0001 and abs(lon) < 0.0001:
        return None
    return lat, lon

def _gps_from_exif(exif):
    """Return (lat, lon) in signed decimal degrees, or None."""
    if not exif:
        log.info("no exif")
        return None
    try:
        gps = exif.get_ifd(0x8825)  # GPSInfo IFD
    except AttributeError:
        log.exception("gps_from_exif %s", str(exif))
        return None
    if not gps:
        log.info("no gps")
        return None

    try:
        lat = dms_to_degrees(gps[2], gps[1])
        lon = dms_to_degrees(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None

    if lat is None or lon is None:
        return None
    # Exactly (0, 0) is Null Island -- effectively always a broken tag rather
    # than a real photo taken in the Gulf of Guinea.
    if abs(lat) < 0.0001 and abs(lon) < 0.0001:
        return None
    return lat, lon


def dms_to_degrees(dms, ref):
    degrees, minutes, seconds = (float(v) for v in dms)
    value = degrees + minutes / 60.0 + seconds / 3600.0
    if str(ref).upper().strip() in ("S", "W"):
        value = -value
    return value


def load_geocache():
    try:
        with open(GEOCACHE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def reverse_geocode(lat, lon, cache):
    """
    City + country for a coordinate, cached on disk.

    Coordinates are rounded to ~1km before being used as the cache key. City
    names do not change over 1km, and the rounding means a whole holiday's
    worth of photos from one town costs a single request.
    """
    if not GEOCODE:
        return ""

    key = "%.2f,%.2f" % (lat, lon)
    if key in cache:
        return cache[key]

    with _geo_lock:
        # Nominatim's usage policy allows at most one request per second.
        elapsed = time.time() - _geo_last_call[0]
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        _geo_last_call[0] = time.time()

        try:
            resp = requests.get(
                GEOCODE_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": 10,  # city / town level
                    "accept-language": GEOCODE_LANG,
                },
                headers={"User-Agent": GEOCODE_AGENT},
                timeout=30,
            )
            resp.raise_for_status()
            address = resp.json().get("address", {})
        except Exception as exc:
            log.warning("reverse geocode failed for %s: %s", key, exc)
            return ""

    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or ""
    )
    country = address.get("country", "")

    place = ", ".join(part for part in (city, country) if part)
    # Cache negative results too, so a coordinate in the middle of the ocean
    # is not re-queried on every sync.
    cache[key] = place
    return place


# ------------------------------------------------------------------ images

def process(raw, dest):
    """
    Downscale, fix orientation, save as baseline JPEG.

    Returns the source EXIF, read before the resize -- Pillow does not carry
    it into the saved file, and we want the metadata rather than the bytes.
    """
    log.info("saving photo locally to %s", dest)
    img = Image.open(io.BytesIO(raw))

    try:
        exif = img.getexif()
    except Exception:
        log.info("failed loading exif")
        exif = None

    # Phone photos carry rotation in EXIF rather than in the pixels. Safari 9
    # will not honour it, so bake it in here.
    img = ImageOps.exif_transpose(img)

    if img.mode != "RGB":
        img = img.convert("RGB")

    img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)

    tmp = dest + ".tmp"
    # Baseline rather than progressive: progressive JPEGs decode in several
    # passes, which is slower on an A5 for no benefit over a LAN.
    img.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True)
    os.replace(tmp, dest)

    return exif


# ------------------------------------------------------------------- state

def load_state():
    """
    Return {key: {"etag", "account", "place", "when"}}.

    Older formats are tolerated. Entries without caption fields are re-fetched
    once so their metadata can be read -- EXIF is not preserved in the
    downscaled copies, so there is nothing local to read it from.
    """
    try:
        with open(STATE_PATH) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}

    legacy_owner = ACCOUNTS[0][0] if ACCOUNTS else None
    state = {}
    for key, value in raw.items():
        if isinstance(value, str):
            state[key] = {"etag": value, "account": legacy_owner}
        elif isinstance(value, dict):
            state[key] = value
    return state


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # atomic, so the iPad never reads a half-written file


# -------------------------------------------------------------------- sync

def sync_account(user, password, old_state, new_state, geocache):
    """Sync one account into new_state. Raises if the account is unreachable."""
    session = requests.Session()
    session.auth = (user, password)

    favourites = list_favourites(session, user)
    added = skipped = failed = 0

    for href, etag, lastmod in favourites:
        # Stable filename derived from the remote path, which already contains
        # the username -- so two accounts with identically named files cannot
        # collide.
        key = hashlib.sha1(href.encode("utf-8")).hexdigest()[:16]
        dest = os.path.join(PHOTO_DIR, key + ".jpg")

        previous = old_state.get(key)
        if (
            previous
            and previous.get("etag") == etag
            and "when" in previous
            and os.path.exists(dest)
        ):
            new_state[key] = previous
            skipped += 1
            continue

        try:
            exif = process(download(session, href), dest)
        except Exception as exc:
            # One unreadable file (an odd RAW, a HEIC without pillow-heif)
            # should not abort the account.
            failed += 1
            log.warning("skipping %s: %s", unquote(href), exc)
            continue

        # Path first, as requested. EXIF capture date is a better fallback
        # than the server's mtime, which on Nextcloud is often upload time --
        # drop the middle line if you want strictly path-then-mtime.
        when = (
            date_from_path(href)
            or date_from_exif(exif)
            or date_from_lastmod(lastmod)
        )

        coords = gps_from_exif(exif)
        if not coords:
            log.info("failed loading coordinates from exit")
        place = reverse_geocode(coords[0], coords[1], geocache) if coords else ""
        if not place:
            log.info("failed finding photo's place")

        new_state[key] = {
            "etag": etag,
            "account": user,
            "place": place,
            "when": format_date(when),
        }
        added += 1

    log.info(
        "%s: %d favourite(s) -- %d new, %d unchanged, %d unreadable",
        user, len(favourites), added, skipped, failed,
    )


def sync_once():
    os.makedirs(PHOTO_DIR, exist_ok=True)

    old_state = load_state()
    new_state = {}
    geocache = load_geocache()

    for user, password in ACCOUNTS:
        try:
            sync_account(user, password, old_state, new_state, geocache)
        except Exception as exc:
            log.error("%s: sync failed (%s)", user, exc)
            # Carry this account's previous entries forward untouched. Without
            # this, an expired app password or a brief outage would look like
            # "nothing is favourited any more" and prune every one of that
            # user's photos off the frame.
            for key, value in old_state.items():
                if value.get("account") == user:
                    new_state[key] = value

    # Prune files on disk that no account vouches for any more.
    keep = {key + ".jpg" for key in new_state}
    removed = 0
    for name in os.listdir(PHOTO_DIR):
        if name not in keep:
            try:
                os.remove(os.path.join(PHOTO_DIR, name))
                removed += 1
            except OSError:
                pass

    # Only advertise files that actually exist -- a carried-forward entry
    # whose file went missing would otherwise 404 on the iPad.
    photos = []
    for key in sorted(new_state):
        if not os.path.exists(os.path.join(PHOTO_DIR, key + ".jpg")):
            continue
        entry = new_state[key]
        photos.append({
            "src": "photos/" + key + ".jpg",
            "place": entry.get("place", ""),
            "when": entry.get("when", ""),
        })

    save_json(STATE_PATH, new_state)
    save_json(GEOCACHE_PATH, geocache)
    save_json(
        MANIFEST_PATH,
        {
            "interval": PHOTO_INTERVAL,
            "updated": int(time.time()),
            "photos": photos,
        },
    )

    located = sum(1 for p in photos if p["place"])
    log.info(
        "sync complete: %d photo(s) in frame (%d with a location), %d removed",
        len(photos), located, removed,
    )


def sync_loop():
    while True:
        try:
            sync_once()
        except Exception as exc:
            log.error("sync failed: %s", exc)
        time.sleep(SYNC_INTERVAL)


# ------------------------------------------------------------------ server

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_DIR, **kwargs)

    def end_headers(self):
        path = self.path.split("?")[0]
        if path.startswith("/photos/"):
            # Filenames change when content changes, so these are immutable.
            self.send_header("Cache-Control", "public, max-age=31536000")
        else:
            # The manifest and the page itself must never be stale.
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Default behaviour logs every request to stderr, which on a Pi means
        # writing to the SD card via journald several times a minute forever.
        pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not NC_URL:
        raise SystemExit("Missing required config: NC_URL")
    if not ACCOUNTS:
        raise SystemExit(
            "No accounts configured. Set NC_USER_1/NC_PASS_1 (and optionally "
            "NC_USER_2/NC_PASS_2, ...) in /etc/photo-frame.env"
        )

    log.info("configured accounts: %s", ", ".join(u for u, _ in ACCOUNTS))
    if not HEIF:
        log.warning("pillow-heif not installed -- .heic favourites will be skipped")
    if not GEOCODE:
        log.info("reverse geocoding disabled -- captions will show dates only")

    os.makedirs(PHOTO_DIR, exist_ok=True)
    if not os.path.exists(MANIFEST_PATH):
        save_json(MANIFEST_PATH, {"interval": PHOTO_INTERVAL, "photos": []})

    threading.Thread(target=sync_loop, daemon=True).start()

    log.info("serving %s on %s:%d", WWW_DIR, BIND, PORT)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
