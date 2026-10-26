#!/usr/bin/env python3
"""
NZ Height Fix - convert drone photos or WebODM outputs to NZVD2016 heights
(height above mean sea level, New Zealand).

Two kinds of input:
  A) A folder of raw drone photos (JPEG). Corrected copies are written with
     the GPS altitude (EXIF GPSAltitude and DJI XMP AbsoluteAltitude) adjusted.
     Process those copies in WebODM and every output comes out in NZVD2016.
  B) WebODM outputs (the extracted "All assets" folder, or the all.zip itself).
     Elevation rasters (dsm.tif / dtm.tif), point clouds (.laz/.las) and the
     textured model (.obj) are rewritten with corrected heights.

Correction modes:
  auto    : (WebODM outputs only) compare the DTM (or DSM) with the LINZ national
            LiDAR elevation model, which is in NZVD2016, and shift every output
            by the median difference. Needs internet; no other input needed.
  geoid   : heights are GNSS ellipsoidal heights (RTK/PPK drones, or ODM output
            from them). H_NZVD2016 = h_ellipsoid - N, with N from NZGeoid2016.
  takeoff : (photos only, DJI) altitude = takeoff ground height + the drone's
            RelativeAltitude. Use this for non-RTK DJI drones, whose absolute
            altitude is often tens of metres out.
  offset  : add a fixed number of metres to every height.

Run with no arguments for the window, or see --help for command-line use.
"""

import argparse
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import urllib.request
import zipfile

import numpy as np

__version__ = "1.0.0"
APP_NAME = "NZ Height Fix"
HELP_URL = "https://github.com/nicholasjg503/nz-height-fix#readme"

GEOID_FILENAME = "nz_linz_nzgeoid2016.tif"
GEOID_URL = "https://cdn.proj.org/" + GEOID_FILENAME

LINZ_ELEVATION = "https://nz-elevation.s3.ap-southeast-2.amazonaws.com/new-zealand/"
# Reference collections, best first. Tiles are named by NZTopo50 sheet, e.g. BA35.tiff
LINZ_REFERENCE = {
    "dtm": ["new-zealand/dem_1m/2193", "new-zealand-contour/dem_8m/2193"],
    "dsm": ["new-zealand/dsm_1m/2193", "new-zealand/dem_1m/2193",
            "new-zealand-contour/dem_8m/2193"],
}

PHOTO_EXTS = (".jpg", ".jpeg")
SKIP_DIRS = {"entwine_pointcloud", "potree_pointcloud", "3d_tiles", "odm_report"}


class FixError(Exception):
    pass


# --------------------------------------------------------------------------
# NZGeoid2016
# --------------------------------------------------------------------------

def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource(*parts):
    """Path to a bundled file (PyInstaller puts them in sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.join(_app_dir(), "packaging")
    return os.path.join(base, *parts)


def _cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "NZHeightFix")


def find_geoid_grid(explicit=None, log=print):
    candidates = [explicit, os.environ.get("NZGEOID_GRID"),
                  os.path.join(_app_dir(), GEOID_FILENAME), _resource(GEOID_FILENAME),
                  os.path.join(_cache_dir(), GEOID_FILENAME)]
    for c in candidates:
        if c and os.path.isfile(c):
            return c

    target = os.path.join(_cache_dir(), GEOID_FILENAME)
    os.makedirs(_cache_dir(), exist_ok=True)
    log(f"Downloading NZGeoid2016 grid from {GEOID_URL} (one-off) ...")
    tmp = target + ".part"
    try:
        with urllib.request.urlopen(GEOID_URL, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, target)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise FixError(
            f"Could not download the NZGeoid2016 grid ({e}).\n"
            f"Download {GEOID_URL} manually and put it next to this program.")
    log(f"Saved geoid grid to {target}")
    return target


class GeoidGrid:
    """NZGeoid2016 undulation N (metres) by bilinear interpolation."""

    def __init__(self, path):
        import rasterio
        with rasterio.open(path) as ds:
            self.data = ds.read(1).astype(np.float64)
            if ds.nodata is not None:
                self.data[self.data == ds.nodata] = np.nan
            self.inv = ~ds.transform
            self.west = ds.bounds.left
            self.h, self.w = self.data.shape

    def undulation(self, lon, lat):
        lon = np.atleast_1d(np.asarray(lon, dtype=np.float64))
        lat = np.atleast_1d(np.asarray(lat, dtype=np.float64))
        lon = np.where(lon < self.west, lon + 360.0, lon)  # Chatham Is. etc.
        col, row = self.inv * (lon, lat)
        c = np.asarray(col) - 0.5  # GDAL transforms are pixel-corner based
        r = np.asarray(row) - 0.5
        if np.any((c < -0.5) | (r < -0.5) | (c > self.w - 0.5) | (r > self.h - 0.5)):
            raise FixError("Location is outside NZGeoid2016 coverage - is this site in New Zealand?")
        c0 = np.clip(np.floor(c).astype(int), 0, self.w - 2)
        r0 = np.clip(np.floor(r).astype(int), 0, self.h - 2)
        fc = np.clip(c - c0, 0, 1)
        fr = np.clip(r - r0, 0, 1)
        d = self.data
        n = (d[r0, c0] * (1 - fc) * (1 - fr) + d[r0, c0 + 1] * fc * (1 - fr)
             + d[r0 + 1, c0] * (1 - fc) * fr + d[r0 + 1, c0 + 1] * fc * fr)
        if np.any(np.isnan(n)):
            raise FixError("No NZGeoid2016 value at this location (offshore / outside grid).")
        return n


class DeltaField:
    """Height correction (metres to add) over a projected extent.

    The geoid is very smooth, so it is sampled on a ~25 m lattice and
    interpolated, instead of transforming every pixel/point to lat/lon.
    """

    def __init__(self, mode, offset, geoid, crs, xmin, ymin, xmax, ymax):
        self.const = None
        if mode == "offset":
            self.const = float(offset)
            return
        from pyproj import Transformer
        if crs is None:
            raise FixError("File has no coordinate system, so the geoid can't be looked up.")
        nx = int(np.clip(np.ceil((xmax - xmin) / 25.0) + 1, 2, 400))
        ny = int(np.clip(np.ceil((ymax - ymin) / 25.0) + 1, 2, 400))
        self.xs = np.linspace(xmin, xmax, nx)
        self.ys = np.linspace(ymin, ymax, ny)
        gx, gy = np.meshgrid(self.xs, self.ys)
        t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        lon, lat = t.transform(gx.ravel(), gy.ravel())
        self.grid = -geoid.undulation(lon, lat).reshape(ny, nx)

    def __call__(self, x, y):
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if self.const is not None:
            return np.full(np.broadcast(x, y).shape, self.const)
        fx = np.clip((x - self.xs[0]) / (self.xs[-1] - self.xs[0]) * (len(self.xs) - 1), 0, len(self.xs) - 1)
        fy = np.clip((y - self.ys[0]) / (self.ys[-1] - self.ys[0]) * (len(self.ys) - 1), 0, len(self.ys) - 1)
        x0 = np.clip(np.floor(fx).astype(int), 0, len(self.xs) - 2)
        y0 = np.clip(np.floor(fy).astype(int), 0, len(self.ys) - 2)
        tx, ty = fx - x0, fy - y0
        g = self.grid
        return (g[y0, x0] * (1 - tx) * (1 - ty) + g[y0, x0 + 1] * tx * (1 - ty)
                + g[y0 + 1, x0] * (1 - tx) * ty + g[y0 + 1, x0 + 1] * tx * ty)

    def summary(self):
        if self.const is not None:
            return f"{self.const:+.3f} m"
        return f"{self.grid.min():+.3f} to {self.grid.max():+.3f} m"


# --------------------------------------------------------------------------
# LINZ national LiDAR elevation (NZVD2016), read straight from LINZ's open
# data bucket as cloud-optimised GeoTIFFs - only the needed area is fetched.
# --------------------------------------------------------------------------

_SHEET_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # NZTopo50 rows skip I and O


def topo50_sheet(x, y):
    """NZTopo50 sheet code (e.g. 'BA35') for an NZTM2000 coordinate."""
    r = int((6234000 - y) // 36000) + 16  # row AS = index 16
    c = int((x - 1012000) // 24000) + 1
    if r < 0 or c < 1:
        return None
    return _SHEET_LETTERS[r // 24] + _SHEET_LETTERS[r % 24] + f"{c:02d}"


def _gdal_env():
    env = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MAX_RETRY": "3",
           "GDAL_HTTP_RETRY_DELAY": "1"}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        env["GDAL_HTTP_PROXY"] = proxy
    if not os.environ.get("CURL_CA_BUNDLE"):
        try:
            import certifi
            env["CURL_CA_BUNDLE"] = certifi.where()
        except ImportError:
            pass
    return env


def linz_heights(kind, x, y, log):
    """LINZ reference heights at NZTM2000 points (NaN where there's no data)."""
    import rasterio
    from affine import Affine
    from rasterio.errors import RasterioIOError
    from rasterio.windows import Window
    out = np.full(len(x), np.nan)
    sheets = np.array([topo50_sheet(a, b) or "" for a, b in zip(x, y)])
    with rasterio.Env(**_gdal_env()):
        for coll in LINZ_REFERENCE[kind]:
            for sheet in sorted(set(sheets[np.isnan(out)]) - {""}):
                sel = np.isnan(out) & (sheets == sheet)
                url = f"/vsicurl/{LINZ_ELEVATION}{coll}/{sheet}.tiff"
                try:
                    with rasterio.open(url) as ds:
                        xs, ys = x[sel], y[sel]
                        win = ds.window(xs.min() - 2, ys.min() - 2, xs.max() + 2, ys.max() + 2)
                        win = win.round_offsets().round_lengths()
                        win = win.intersection(Window(0, 0, ds.width, ds.height))
                        # keep downloads modest for big sites (geoid-style offsets are smooth)
                        scale = max(1.0, max(win.width, win.height) / 4000)
                        shape = (max(1, int(win.height / scale)), max(1, int(win.width / scale)))
                        a = ds.read(1, window=win, out_shape=shape).astype(np.float64)
                        if ds.nodata is not None:
                            a[a == ds.nodata] = np.nan
                        wt = ds.window_transform(win) * Affine.scale(
                            win.width / shape[1], win.height / shape[0])
                        cols, rows = ~wt * (xs, ys)
                        cols = np.clip(np.floor(cols).astype(int), 0, shape[1] - 1)
                        rows = np.clip(np.floor(rows).astype(int), 0, shape[0] - 1)
                        out[sel] = a[rows, cols]
                except RasterioIOError:
                    continue  # no tile here (sea / no coverage)
            got = np.isfinite(out).sum()
            log(f"    LINZ {coll.split('/')[1]}: {got} of {len(out)} sample points have data")
            if got >= 0.5 * len(out):
                break
    return out


def _raster_samples(path):
    """~200 x 200 evenly spread (x, y, z) samples of an elevation raster, plus its CRS."""
    import rasterio
    with rasterio.open(path) as src:
        if src.crs is None:
            raise FixError(f"{os.path.basename(path)} has no coordinate system.")
        step = max(1, int(max(src.width, src.height) / 200))
        shape = (max(1, src.height // step), max(1, src.width // step))
        a = src.read(1, out_shape=shape).astype(np.float64)
        if src.nodata is not None:
            a[a == src.nodata] = np.nan
        rows, cols = np.nonzero(np.isfinite(a))
        sx, sy = src.width / shape[1], src.height / shape[0]
        xs, ys = src.transform * ((cols + 0.5) * sx, (rows + 0.5) * sy)
        return np.asarray(xs), np.asarray(ys), a[rows, cols], src.crs


def _cloud_samples(path, fallback_crs, target=40000):
    """About `target` evenly thinned (x, y, z) points of a point cloud, plus its CRS."""
    import laspy
    with laspy.open(path) as reader:
        crs = None
        try:
            crs = reader.header.parse_crs()
        except Exception:
            pass
        crs = crs or fallback_crs
        if crs is None:
            raise FixError(f"{os.path.basename(path)} has no coordinate system.")
        stride = max(1, reader.header.point_count // target)
        xs, ys, zs = [], [], []
        for pts in reader.chunk_iterator(2_000_000):
            xs.append(np.asarray(pts.x)[::stride])
            ys.append(np.asarray(pts.y)[::stride])
            zs.append(np.asarray(pts.z)[::stride])
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs), crs


def linz_offset(path, kind, log, fallback_crs=None):
    """Median (LINZ - this raster or point cloud) height difference and its spread (MAD), metres."""
    from pyproj import Transformer
    if path.lower().endswith((".laz", ".las")):
        xs, ys, mine, crs = _cloud_samples(path, fallback_crs)
    else:
        xs, ys, mine, crs = _raster_samples(path)
    if len(mine) < 50:
        raise FixError(f"{os.path.basename(path)} has too few valid heights to compare.")
    nx, ny = Transformer.from_crs(crs, "EPSG:2193", always_xy=True).transform(xs, ys)
    log(f"    comparing {len(mine)} points of {os.path.basename(path)} with LINZ elevation ...")
    ref = linz_heights(kind, np.asarray(nx), np.asarray(ny), log)
    ok = np.isfinite(ref)
    if ok.sum() < 50:
        raise FixError("LINZ has no elevation data for this site (or it couldn't be downloaded).")
    diff = ref[ok] - mine[ok]
    med = float(np.median(diff))
    mad = float(np.median(np.abs(diff - med)))
    log(f"    LINZ minus WebODM: median {med:+.2f} m, typical spread +/-{mad:.2f} m "
        f"over {ok.sum()} points")
    if mad > 2.0:
        log("    WARNING: large spread - the ground may have changed since the LiDAR survey, "
            "or the model is tilted/distorted. Check the result.")
    return med, mad


# --------------------------------------------------------------------------
# A) Photos: EXIF GPSAltitude + DJI XMP
# --------------------------------------------------------------------------

XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_NUM = rb"([+-]?\d+(?:\.\d+)?)"


def _jpeg_segments(data):
    """Yield (marker, seg_start, payload_start, seg_end) up to start of scan."""
    if data[:2] != b"\xff\xd8":
        raise FixError("not a JPEG")
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise FixError("corrupt JPEG header")
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xDA or marker == 0xD9:
            return
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        yield marker, pos, pos + 4, pos + 2 + length
        pos += 2 + length


class ExifGps:
    """Reads GPS from an EXIF block and patches altitude in place."""

    TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

    def __init__(self, buf, tiff_start):
        self.buf = buf
        self.base = tiff_start
        bo = bytes(buf[tiff_start:tiff_start + 2])
        if bo == b"II":
            self.e = "<"
        elif bo == b"MM":
            self.e = ">"
        else:
            raise FixError("bad EXIF byte order")
        ifd0 = self._u32(tiff_start + 4)
        self.gps = {}
        gps_ptr = self._entries(ifd0).get(0x8825)
        if gps_ptr is None:
            return
        self.gps = self._entries(self._u32(gps_ptr[3]))

    def _u16(self, p):
        return struct.unpack(self.e + "H", self.buf[p:p + 2])[0]

    def _u32(self, p):
        return struct.unpack(self.e + "I", self.buf[p:p + 4])[0]

    def _entries(self, ifd_off):
        p = self.base + ifd_off
        out = {}
        for i in range(self._u16(p)):
            ep = p + 2 + 12 * i
            tag, typ, count = struct.unpack(self.e + "HHI", self.buf[ep:ep + 8])
            size = self.TYPE_SIZE.get(typ, 1) * count
            # (type, count, absolute data position, raw value field position)
            data_pos = ep + 8 if size <= 4 else self.base + self._u32(ep + 8)
            out[tag] = (typ, count, data_pos, ep + 8)
        return out

    def _rationals(self, tag):
        typ, count, pos, _ = self.gps[tag]
        fmt = self.e + ("i" if typ == 10 else "I") * 2
        vals = []
        for i in range(count):
            n, d = struct.unpack(fmt, self.buf[pos + 8 * i:pos + 8 * i + 8])
            vals.append(n / d if d else 0.0)
        return vals

    def _char(self, tag):
        return chr(self.buf[self.gps[tag][2]]) if tag in self.gps else ""

    def latlon(self):
        if not all(t in self.gps for t in (2, 4)):
            return None
        dms = self._rationals(2)
        lat = dms[0] + dms[1] / 60 + dms[2] / 3600
        dms = self._rationals(4)
        lon = dms[0] + dms[1] / 60 + dms[2] / 3600
        if self._char(1) == "S":
            lat = -lat
        if self._char(3) == "W":
            lon = -lon
        return lat, lon

    def altitude(self):
        if 6 not in self.gps:
            return None
        alt = self._rationals(6)[0]
        if 5 in self.gps and self.buf[self.gps[5][2]] == 1:
            alt = -alt
        return alt

    def set_altitude(self, alt):
        typ, _, pos, _ = self.gps[6]
        if alt < 0 and 5 not in self.gps and typ != 10:
            raise FixError("new altitude is below sea level but photo has no GPSAltitudeRef tag")
        if typ == 10:  # SRATIONAL: store the sign directly
            val, ref = alt, 0
        else:
            val, ref = abs(alt), (1 if alt < 0 else 0)
        den = 1000
        num = int(round(val * den))
        self.buf[pos:pos + 8] = struct.pack(self.e + ("i" if typ == 10 else "I") + "I", num, den)
        if 5 in self.gps:
            self.buf[self.gps[5][2]] = ref


def _xmp_get(xmp, name):
    m = re.search(rb"drone-dji:" + name + rb'="' + _NUM + rb'"', xmp) or \
        re.search(rb"<drone-dji:" + name + rb">" + _NUM + rb"<", xmp)
    return float(m.group(1)) if m else None


def _xmp_set(xmp, name, value):
    new = b"%+.3f" % value
    xmp, n = re.subn(rb"(drone-dji:" + name + rb'=")' + _NUM + rb'"',
                     lambda m: m.group(1) + new + b'"', xmp)
    if n == 0:
        xmp = re.sub(rb"(<drone-dji:" + name + rb">)" + _NUM + rb"<",
                     lambda m: m.group(1) + new + b"<", xmp)
    return xmp


def read_photo(path):
    with open(path, "rb") as f:
        buf = bytearray(f.read())
    info = {"buf": buf, "exif": None, "xmp": None}
    for marker, s, p, e in _jpeg_segments(buf):
        if marker != 0xE1:
            continue
        if buf[p:p + 6] == b"Exif\x00\x00" and info["exif"] is None:
            info["exif"] = ExifGps(buf, p + 6)
        elif buf[p:p + len(XMP_HEADER)] == XMP_HEADER and info["xmp"] is None:
            info["xmp"] = (s, e)
    ex = info["exif"]
    info["latlon"] = ex.latlon() if ex else None
    info["exif_alt"] = ex.altitude() if ex else None
    info["xmp_abs"] = info["xmp_rel"] = None
    if info["xmp"]:
        seg = bytes(buf[info["xmp"][0]:info["xmp"][1]])
        info["xmp_abs"] = _xmp_get(seg, b"AbsoluteAltitude")
        info["xmp_rel"] = _xmp_get(seg, b"RelativeAltitude")
    return info


def write_photo(info, out_path, new_exif_alt, new_xmp_abs):
    buf = info["buf"]
    if new_exif_alt is not None:
        info["exif"].set_altitude(new_exif_alt)
    if new_xmp_abs is not None and info["xmp"]:
        s, e = info["xmp"]
        payload = _xmp_set(bytes(buf[s + 4:e]), b"AbsoluteAltitude", new_xmp_abs)
        if len(payload) + 2 > 0xFFFF:
            raise FixError("XMP block too large")
        buf = buf[:s] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + buf[e:]
    with open(out_path, "wb") as f:
        f.write(buf)


def process_photos(photos, in_root, out_root, mode, offset, takeoff, geoid, check, log):
    rows = []
    for path in photos:
        try:
            info = read_photo(path)
        except Exception as e:
            log(f"  SKIP {os.path.relpath(path, in_root)}: {e}")
            continue
        if info["latlon"] is None or (info["exif_alt"] is None and info["xmp_abs"] is None):
            log(f"  SKIP {os.path.relpath(path, in_root)}: no GPS position/altitude")
            continue
        rows.append((path, info))
    if not rows:
        raise FixError("No photos with GPS altitude found.")

    def med(vals):
        vals = [v for v in vals if v is not None]
        return f"{np.median(vals):.2f} m" if vals else "n/a"

    lat = np.array([i["latlon"][0] for _, i in rows])
    lon = np.array([i["latlon"][1] for _, i in rows])
    log(f"{len(rows)} geotagged photos, centre {lat.mean():.5f}, {lon.mean():.5f}")
    log(f"  median EXIF GPSAltitude         : {med([i['exif_alt'] for _, i in rows])}")
    log(f"  median DJI AbsoluteAltitude     : {med([i['xmp_abs'] for _, i in rows])}")
    log(f"  median DJI RelativeAltitude     : {med([i['xmp_rel'] for _, i in rows])}")
    implied = [(i["xmp_abs"] if i["xmp_abs"] is not None else i["exif_alt"]) - i["xmp_rel"]
               for _, i in rows if i["xmp_rel"] is not None]
    if implied:
        log(f"  => drone thinks takeoff ground is at {np.median(implied):.2f} m "
            f"(compare with the real ground height there)")
    n = None
    if geoid is not None:
        n = geoid.undulation(lon, lat)
        log(f"  NZGeoid2016 N at site           : {n.mean():.3f} m "
            f"(ellipsoidal height - N = NZVD2016 height)")

    if check:
        return
    if mode == "takeoff" and any(i["xmp_rel"] is None for _, i in rows):
        raise FixError("takeoff mode needs DJI RelativeAltitude in every photo; some don't have it.")

    shifts = []
    for k, (path, info) in enumerate(rows):
        rel = os.path.relpath(path, in_root)
        if mode == "geoid":
            d = -n[k]
            new_e = info["exif_alt"] + d if info["exif_alt"] is not None else None
            new_x = info["xmp_abs"] + d if info["xmp_abs"] is not None else None
        elif mode == "offset":
            new_e = info["exif_alt"] + offset if info["exif_alt"] is not None else None
            new_x = info["xmp_abs"] + offset if info["xmp_abs"] is not None else None
        else:  # takeoff
            new_alt = takeoff + info["xmp_rel"]
            new_e = new_alt if info["exif_alt"] is not None else None
            new_x = new_alt if info["xmp_abs"] is not None else None
        old = info["exif_alt"] if info["exif_alt"] is not None else info["xmp_abs"]
        new = new_e if new_e is not None else new_x
        shifts.append(new - old)
        out = os.path.join(out_root, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(path, out)  # keep timestamps, then overwrite contents
        write_photo(info, out, new_e, new_x)
    log(f"Wrote {len(rows)} corrected photos. Altitude change: "
        f"{min(shifts):+.2f} to {max(shifts):+.2f} m")
    log("Process these photos in WebODM (no GCPs needed for the height fix).")


# --------------------------------------------------------------------------
# B) WebODM outputs
# --------------------------------------------------------------------------

def _crs_from_text(text):
    from pyproj import CRS
    text = text.strip()
    m = re.match(r"WGS84 UTM (\d+)([NS])", text, re.I)
    if m:
        zone, hemi = int(m.group(1)), m.group(2).upper()
        return CRS.from_epsg((32600 if hemi == "N" else 32700) + zone)
    return CRS.from_user_input(text)


def read_coords_txt(root):
    """ODM's odm_georeferencing/coords.txt: CRS line, then 'x_offset y_offset'."""
    for dirpath, dirs, files in os.walk(root):
        if "coords.txt" in files:
            with open(os.path.join(dirpath, "coords.txt")) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            try:
                crs = _crs_from_text(lines[0])
                ox, oy = (float(v) for v in lines[1].split()[:2])
                return crs, ox, oy
            except Exception:
                return None
    return None


def process_raster(path, out, mode, offset, geoid, check, log):
    import rasterio
    from rasterio.windows import Window
    with rasterio.open(path) as src:
        if src.count != 1 or not np.issubdtype(np.dtype(src.dtypes[0]), np.floating):
            log("    not an elevation raster (e.g. orthophoto) - skipped, it needs no change")
            return None
        b = src.bounds
        field = DeltaField(mode, offset, geoid, src.crs, b.left, b.bottom, b.right, b.top)
        log(f"    correction {field.summary()}")
        if check:
            return field
        profile = src.profile.copy()
        profile.update(driver="GTiff", BIGTIFF="IF_SAFER")
        nodata = src.nodata
        T = src.transform
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with rasterio.open(out, "w", **profile) as dst:
            step = 1024
            for r0 in range(0, src.height, step):
                for c0 in range(0, src.width, step):
                    win = Window(c0, r0, min(step, src.width - c0), min(step, src.height - r0))
                    a = src.read(1, window=win).astype(np.float64)
                    cols, rows = np.meshgrid(np.arange(win.width) + c0 + 0.5,
                                             np.arange(win.height) + r0 + 0.5)
                    xs, ys = T * (cols, rows)
                    valid = ~np.isnan(a)
                    if nodata is not None:
                        valid &= a != nodata
                    a[valid] += field(xs[valid], ys[valid])
                    dst.write(a.astype(profile["dtype"]), 1, window=win)
            dst.update_tags(VERTICAL_DATUM="NZVD2016" if mode == "geoid" else
                            f"heights shifted {offset:+.3f} m")
    return field


def process_pointcloud(path, out, mode, offset, geoid, fallback_crs, check, log):
    import laspy
    with laspy.open(path) as reader:
        h = reader.header
        crs = None
        try:
            crs = h.parse_crs()
        except Exception:
            pass
        crs = crs or fallback_crs
        field = DeltaField(mode, offset, geoid, crs, h.mins[0], h.mins[1], h.maxs[0], h.maxs[1])
        log(f"    {h.point_count:,} points, correction {field.summary()}")
        if check:
            return field
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with laspy.open(out, mode="w", header=h) as writer:
            for pts in reader.chunk_iterator(2_000_000):
                x, y = np.asarray(pts.x), np.asarray(pts.y)
                pts.z = np.asarray(pts.z) + field(x, y)
                writer.write_points(pts)
    return field


def process_obj(path, out, mode, offset, geoid, coords, check, log):
    if coords is None:
        log("    no odm_georeferencing/coords.txt found - can't locate the model, skipped")
        return
    crs, ox, oy = coords
    # One pass to find the extent (vertices are stored relative to coords.txt offset)
    xmin = ymin = np.inf
    xmax = ymax = -np.inf
    with open(path, "r", errors="replace") as f:
        for ln in f:
            if ln.startswith("v "):
                p = ln.split()
                x, y = float(p[1]), float(p[2])
                xmin, xmax = min(xmin, x), max(xmax, x)
                ymin, ymax = min(ymin, y), max(ymax, y)
    if xmin == np.inf:
        log("    no vertices - skipped")
        return
    if abs(xmin) > 100000:  # already absolute coordinates
        ox = oy = 0.0
    field = DeltaField(mode, offset, geoid, crs, xmin + ox, ymin + oy, xmax + ox, ymax + oy)
    log(f"    correction {field.summary()}")
    if check:
        return

    def flush(batch, fout):
        xyz = np.array([[float(v) for v in ln.split()[1:4]] for ln in batch])
        dz = field(xyz[:, 0] + ox, xyz[:, 1] + oy)
        for ln, z in zip(batch, xyz[:, 2] + dz):
            p = ln.split()
            p[3] = f"{z:.6f}"
            fout.write(" ".join(p) + "\n")
        batch.clear()

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(path, "r", errors="replace") as fin, open(out, "w") as fout:
        batch = []
        for ln in fin:
            if ln.startswith("v "):
                batch.append(ln)
                if len(batch) >= 200_000:
                    flush(batch, fout)
            else:
                if batch:
                    flush(batch, fout)
                fout.write(ln)
        if batch:
            flush(batch, fout)
    # textures/materials must sit next to the .obj to open it
    src_dir, dst_dir = os.path.dirname(path), os.path.dirname(out)
    for fn in os.listdir(src_dir):
        if fn.lower().endswith((".mtl", ".png", ".jpg", ".jpeg")):
            shutil.copy2(os.path.join(src_dir, fn), os.path.join(dst_dir, fn))


# --------------------------------------------------------------------------
# Simple export: one corrected elevation GeoTIFF + the orthophoto GeoTIFF,
# optionally cropped so there are no black (nodata/transparent) edges.
# --------------------------------------------------------------------------

def _largest_rectangle(mask):
    """Largest all-True axis-aligned rectangle in a 2-D bool array, as (r0, c0, r1, c1)."""
    h, w = mask.shape
    heights = np.zeros(w + 1, dtype=np.int64)  # trailing 0 flushes the stack each row
    best = (0, 0, 0, 0, 0)
    for r in range(h):
        heights[:w] = np.where(mask[r], heights[:w] + 1, 0)
        hl = heights.tolist()
        stack = []  # (start column, height), heights increasing
        for c, hc in enumerate(hl):
            start = c
            while stack and stack[-1][1] >= hc:
                s, sh = stack.pop()
                if sh * (c - s) > best[0]:
                    best = (sh * (c - s), r - sh + 1, s, r + 1, c)
                start = s
            stack.append((start, hc))
    return best[1:]


def _valid_rect(read_mask, height, width, max_cells=600):
    """Window of the largest fully-valid rectangle. read_mask(row0, nrows, ncols) -> bool array.

    Works on blocks of k x k pixels; a block only counts as valid if every pixel is,
    so the result never includes a nodata pixel.
    """
    from rasterio.windows import Window
    k = max(1, int(np.ceil(max(width, height) / max_cells)))
    nr, nc = height // k, width // k
    if nr == 0 or nc == 0:
        return None
    small = np.zeros((nr, nc), dtype=bool)
    for i in range(nr):
        m = np.asarray(read_mask(i * k, k, nc * k), dtype=bool)
        small[i] = m.reshape(k, nc, k).all(axis=(0, 2))
    r0, c0, r1, c1 = _largest_rectangle(small)
    if r1 <= r0 or c1 <= c0:
        return None
    return Window(c0 * k, r0 * k, (c1 - c0) * k, (r1 - r0) * k)


def _overview_factors(width, height):
    return [f for f in (2, 4, 8, 16, 32, 64) if min(width, height) // f >= 256]


def _read_dem(path, bounds):
    """Elevation raster (NaN for nodata), its transform and CRS, limited to bounds if given."""
    import rasterio
    from rasterio.windows import Window
    with rasterio.open(path) as src:
        full = Window(0, 0, src.width, src.height)
        win = full if bounds is None else \
            src.window(*bounds).round_offsets().round_lengths().intersection(full)
        a = src.read(1, window=win).astype(np.float64)
        if src.nodata is not None:
            a[a == src.nodata] = np.nan
        return a, src.window_transform(win), src.crs


def _cloud_to_dem(path, bounds, out_crs, fallback_crs, max_cells=16_000_000):
    """Grid a point cloud into a surface model (mean height per cell), filling small holes.

    Returns (heights with NaN for no data, transform, crs, cell size).
    """
    import laspy
    from pyproj import CRS, Transformer
    from rasterio.fill import fillnodata
    from rasterio.transform import from_origin
    with laspy.open(path) as reader:
        h = reader.header
        crs = None
        try:
            crs = h.parse_crs()
        except Exception:
            pass
        crs = crs or fallback_crs
        out_crs = out_crs or crs
        tf = None
        if crs is not None and out_crs is not None and CRS.from_user_input(crs) != CRS.from_user_input(out_crs):
            tf = Transformer.from_crs(crs, out_crs, always_xy=True)
        if bounds is None:
            bounds = (h.mins[0], h.mins[1], h.maxs[0], h.maxs[1])
        xmin, ymin, xmax, ymax = bounds
        density = h.point_count / max(1.0, (h.maxs[0] - h.mins[0]) * (h.maxs[1] - h.mins[1]))
        res = max(0.05, np.sqrt(4.0 / density),  # about 4 points per cell
                  np.sqrt((xmax - xmin) * (ymax - ymin) / max_cells))
        res = float(np.ceil(res * 100) / 100)
        nx = max(1, int(np.ceil((xmax - xmin) / res)))
        ny = max(1, int(np.ceil((ymax - ymin) / res)))
        sums = np.zeros(nx * ny)
        counts = np.zeros(nx * ny)
        for pts in reader.chunk_iterator(2_000_000):
            x, y, z = np.asarray(pts.x), np.asarray(pts.y), np.asarray(pts.z)
            if tf is not None:
                x, y = tf.transform(x, y)
            c = np.floor((x - xmin) / res).astype(np.int64)
            r = np.floor((ymax - y) / res).astype(np.int64)
            ok = (c >= 0) & (c < nx) & (r >= 0) & (r < ny)
            idx = r[ok] * nx + c[ok]
            sums += np.bincount(idx, weights=z[ok], minlength=nx * ny)
            counts += np.bincount(idx, minlength=nx * ny)
    dem = np.full(nx * ny, np.nan)
    has = counts > 0
    dem[has] = sums[has] / counts[has]
    dem = dem.reshape(ny, nx)
    valid = np.isfinite(dem)
    # fill gaps up to ~10 cells from real data; anything further stays nodata
    filled = fillnodata(np.where(valid, dem, 0).astype(np.float32), mask=valid.astype(np.uint8),
                        max_search_distance=10)
    reach = fillnodata(valid.astype(np.float32), mask=valid.astype(np.uint8),
                       max_search_distance=10)
    dem = np.where(reach > 0.5, filled, np.nan).astype(np.float64)
    return dem, from_origin(xmin, ymax, res, res), out_crs, res


def _write_ortho(src_path, out, win, drop_alpha):
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.windows import Window
    with rasterio.open(src_path) as src:
        bands = list(range(1, src.count + 1))
        if drop_alpha and src.count == 4 and src.colorinterp[3] == ColorInterp.alpha:
            bands = bands[:3]  # nothing is transparent once the edges are cropped off
        w, h = int(win.width), int(win.height)
        profile = src.profile.copy()
        profile.update(driver="GTiff", width=w, height=h, count=len(bands),
                       transform=src.window_transform(win), tiled=True, blockxsize=512,
                       blockysize=512, compress="deflate", predictor=2, BIGTIFF="IF_SAFER")
        if len(bands) >= 3 and profile["dtype"] == "uint8":
            profile["photometric"] = "RGB"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with rasterio.open(out, "w", **profile) as dst:
            for r in range(0, h, 1024):
                n = min(1024, h - r)
                sub = Window(win.col_off, win.row_off + r, w, n)
                dst.write(src.read(bands, window=sub), window=Window(0, r, w, n))
            dst.colorinterp = src.colorinterp[:len(bands)]
            factors = _overview_factors(w, h)
            if factors:
                dst.build_overviews(factors, Resampling.average)


def _write_dem(out, dem, transform, crs, note):
    import rasterio
    from rasterio.enums import Resampling
    h, w = dem.shape
    nodata = -9999.0
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with rasterio.open(out, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       crs=crs, transform=transform, nodata=nodata, tiled=True, blockxsize=512,
                       blockysize=512, compress="deflate", predictor=3, BIGTIFF="IF_SAFER") as dst:
        dst.write(np.where(np.isfinite(dem), dem, nodata).astype(np.float32), 1)
        dst.update_tags(VERTICAL_DATUM="NZVD2016", NOTE=note)
        factors = _overview_factors(w, h)
        if factors:
            dst.build_overviews(factors, Resampling.average)


def export_maps(files, out_root, name, mode, offset, geoid, task_crs, crop, check, log):
    """Write '<name> - orthophoto.tif' and '<name> - DSM/DTM NZVD2016.tif'."""
    import rasterio
    from rasterio.transform import array_bounds
    from rasterio.windows import Window
    from rasterio.windows import bounds as window_bounds
    from rasterio.windows import transform as window_transform
    by_name = {os.path.basename(f).lower(): f for f in files}
    ortho = by_name.get("odm_orthophoto.tif")
    dem_path = by_name.get("dtm.tif") or by_name.get("dsm.tif")
    clouds = [f for f in files if f.lower().endswith((".laz", ".las"))]
    log("Exporting GeoTIFFs:")

    bounds = ortho_crs = ortho_win = None
    if ortho:
        with rasterio.open(ortho) as src:
            ortho_crs = src.crs
            ortho_win = Window(0, 0, src.width, src.height)
            if crop:
                ortho_win = _valid_rect(
                    lambda r0, n, w: src.dataset_mask(window=Window(0, r0, w, n)) > 0,
                    src.height, src.width)
                if ortho_win is None:
                    raise FixError("The orthophoto has no area without gaps to crop to.")
            bounds = window_bounds(ortho_win, src.transform)
            rx, ry = src.res
            log(f"    orthophoto: {ortho_win.width * rx:.0f} x {ortho_win.height * ry:.0f} m"
                + (" after cropping off the black edges" if crop else "")
                + f" ({100 * ortho_win.width * ortho_win.height / (src.width * src.height):.0f}% "
                  f"of the original image)")
    else:
        log("    no orthophoto in the input - only the elevation model is exported")

    if dem_path:
        kind = "DTM" if os.path.basename(dem_path).lower() == "dtm.tif" else "DSM"
        dem, transform, dem_crs = _read_dem(dem_path, bounds)
        note = f"from WebODM {os.path.basename(dem_path)}"
    elif clouds:
        kind = "DSM"
        dem, transform, dem_crs, res = _cloud_to_dem(clouds[0], bounds, ortho_crs, task_crs)
        note = "gridded from the WebODM point cloud"
        log(f"    no DEM in the input - built a {res:.2f} m surface model (DSM) from the point cloud")
    else:
        dem = None
        log("    no elevation model or point cloud in the input - no DEM exported")

    if dem is not None:
        if crop and ortho is None:
            ok = np.isfinite(dem)
            win = _valid_rect(lambda r0, n, w: ok[r0:r0 + n, :w], *dem.shape)
            if win is not None:
                r0, c0 = int(win.row_off), int(win.col_off)
                dem = dem[r0:r0 + int(win.height), c0:c0 + int(win.width)]
                transform = window_transform(win, transform)
        valid = np.isfinite(dem)
        if valid.any():
            rows, cols = np.nonzero(valid)
            xs, ys = transform * (cols + 0.5, rows + 0.5)
            west, south, east, north = array_bounds(dem.shape[0], dem.shape[1], transform)
            field = DeltaField(mode, offset, geoid, dem_crs, west, south, east, north)
            dem[rows, cols] += field(np.asarray(xs), np.asarray(ys))
        log(f"    elevation model ({kind}): {dem.shape[1]} x {dem.shape[0]} cells, "
            f"{100 * valid.mean():.0f}% with data")

    if check:
        return
    if ortho:
        out = os.path.join(out_root, f"{name} - orthophoto.tif")
        _write_ortho(ortho, out, ortho_win, drop_alpha=crop)
        log(f"    wrote {os.path.basename(out)}")
    if dem is not None:
        out = os.path.join(out_root, f"{name} - {kind} NZVD2016.tif")
        _write_dem(out, dem, transform, dem_crs, note)
        log(f"    wrote {os.path.basename(out)}")


def process_outputs(files, in_root, out_root, mode, offset, geoid, check, log,
                    name="map", full=False, crop=True):
    """Returns {"offset": metres, "spread": metres} for automatic mode, else {}.

    Always exports the orthophoto and a corrected elevation model as GeoTIFFs; with
    full=True the point cloud, 3D model and WebODM's own DEMs are corrected too.
    """
    out_root = out_root or ""  # check runs may have no output folder; nothing is written
    result = {}
    coords = read_coords_txt(in_root)
    task_crs = coords[0] if coords else None
    rasters = [f for f in files if f.lower().endswith((".tif", ".tiff"))]
    clouds = [f for f in files if f.lower().endswith((".laz", ".las"))]
    objs = [f for f in files if f.lower().endswith("_geo.obj")]
    others = [f for f in files if f.lower().endswith((".glb", ".gltf", ".ply"))]

    if mode == "auto":
        # DTM vs LINZ bare-earth DEM is the cleanest comparison; DSM (or the point
        # cloud, which is a surface model too) vs LINZ DSM otherwise
        by_name = {os.path.basename(f).lower(): f for f in rasters}
        if "dtm.tif" in by_name:
            ref, kind = by_name["dtm.tif"], "dtm"
        elif "dsm.tif" in by_name:
            ref, kind = by_name["dsm.tif"], "dsm"
        elif clouds:
            ref, kind = clouds[0], "dsm"
        else:
            raise FixError("Automatic mode needs dtm.tif, dsm.tif or the point cloud (.laz) "
                           "in the WebODM outputs.")
        log(f"Finding offset from {os.path.basename(ref)}:")
        offset, spread = linz_offset(ref, kind, log, task_crs)
        result = {"offset": offset, "spread": spread}
        mode = "offset"
        log(f"Applying {offset:+.2f} m to all outputs.")

    if not (rasters or clouds or objs):
        raise FixError("No WebODM outputs (dsm.tif, dtm.tif, .laz, _geo.obj) found.")
    if task_crs is None:
        import rasterio
        for path in rasters:
            with rasterio.open(path) as ds:
                task_crs = task_crs or ds.crs
    export_maps(files, out_root, name, mode, offset, geoid, task_crs, crop, check, log)
    if not full:
        if not check:
            log("Done.")
        return result

    log("Correcting the other WebODM outputs:")
    for path in rasters:
        rel = os.path.relpath(path, in_root)
        log(f"  {rel}")
        f = process_raster(path, os.path.join(out_root, rel), mode, offset, geoid, check, log)
        if f is not None and task_crs is None:
            import rasterio
            with rasterio.open(path) as ds:
                task_crs = ds.crs
    for path in clouds:
        rel = os.path.relpath(path, in_root)
        log(f"  {rel}")
        process_pointcloud(path, os.path.join(out_root, rel), mode, offset, geoid,
                           task_crs, check, log)
    for path in objs:
        rel = os.path.relpath(path, in_root)
        log(f"  {rel}")
        process_obj(path, os.path.join(out_root, rel), mode, offset, geoid, coords, check, log)
    for path in others:
        log(f"  {os.path.relpath(path, in_root)}: format not supported - NOT corrected")
    if not check:
        log("Done.")
    return result


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _export_name(input_path):
    """'18458-all.zip' -> '18458': the prefix for exported file names."""
    base = os.path.basename(os.path.normpath(input_path))
    if base.lower().endswith(".zip"):
        base = base[:-4]
    return re.sub(r"[-_ ]all$", "", base, flags=re.I) or "map"


def run(input_path, output_dir, mode="geoid", offset=0.0, takeoff=None, check=False,
        grid=None, log=print, full=False, crop=True):
    if mode not in ("auto", "geoid", "offset", "takeoff"):
        raise FixError(f"unknown mode {mode}")
    if mode == "takeoff" and takeoff is None:
        raise FixError("takeoff mode needs the takeoff ground height (m above sea level)")

    tmp = None
    try:
        if os.path.isfile(input_path) and input_path.lower().endswith(".zip"):
            tmp = tempfile.mkdtemp(prefix="nzhf_")
            log(f"Extracting {os.path.basename(input_path)} ...")
            with zipfile.ZipFile(input_path) as z:
                members = [m for m in z.namelist()
                           if not any(p in SKIP_DIRS for p in m.split("/"))]
                z.extractall(tmp, members)
            in_root = tmp
        elif os.path.isdir(input_path):
            in_root = input_path
        else:
            raise FixError(f"Input not found: {input_path}")

        out_abs = os.path.abspath(output_dir) if output_dir else None
        files = []
        for dirpath, dirs, fns in os.walk(in_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                       and os.path.abspath(os.path.join(dirpath, d)) != out_abs]
            files += [os.path.join(dirpath, fn) for fn in fns]

        is_outputs = any(f.lower().endswith((".laz", ".las", "_geo.obj")) or
                         os.path.basename(f).lower() in ("dsm.tif", "dtm.tif") for f in files)
        if not check:
            if not output_dir:
                raise FixError("Choose an output folder.")
            if os.path.abspath(output_dir) == os.path.abspath(input_path):
                raise FixError("Output folder must be different from the input folder.")
            os.makedirs(output_dir, exist_ok=True)

        geoid = None
        if mode == "geoid" or check:
            try:
                geoid = GeoidGrid(find_geoid_grid(grid, log))
            except FixError:
                if mode == "geoid":
                    raise
                log("  (geoid grid unavailable - geoid info not shown)")

        if mode == "auto" and not is_outputs:
            raise FixError("Automatic mode works on WebODM outputs. Process the photos in "
                           "WebODM first, then run this on the results (all.zip).")
        if is_outputs:
            if mode == "takeoff":
                raise FixError("takeoff mode is for raw photos; use geoid or offset for WebODM outputs")
            log(f"Input looks like WebODM outputs. Mode: {mode}")
            return process_outputs(files, in_root, output_dir, mode, offset, geoid, check, log,
                                   name=_export_name(input_path), full=full, crop=crop)
        else:
            photos = sorted(f for f in files if f.lower().endswith(PHOTO_EXTS))
            if not photos:
                raise FixError("No JPEG photos or WebODM outputs found in the input.")
            log(f"Input looks like raw photos. Mode: {mode}")
            process_photos(photos, in_root, output_dir, mode, offset, takeoff, geoid, check, log)
            return {}
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _describe_result(result, check, out_dir):
    """Plain-English summary of a run, and whether it deserves a warning."""
    if "offset" not in result:
        return ("Check finished - see the details below." if check else
                f"Finished. Corrected copies are in:\n{out_dir}"), False
    off, spread = result["offset"], result["spread"]
    warn = spread > 2.0
    if abs(off) < 0.05:
        msg = "Your heights already match the LINZ elevation model - nothing to fix."
    elif check:
        msg = (f"Your heights are {abs(off):.2f} m too {'low' if off > 0 else 'high'}. "
               f"Click \"Fix heights\" to correct them.")
    else:
        msg = (f"Done! Heights were {'raised' if off > 0 else 'lowered'} by {abs(off):.2f} m.\n"
               f"Corrected files are in: {out_dir}")
    if warn:
        msg += (f"\nWarning: the match varies by about +/-{spread:.1f} m across the site. The ground "
                "may have changed since LINZ's survey, or the model may be tilted. Check the result.")
    return msg, warn


def gui():
    import queue
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            pass

    root = tk.Tk()
    root.title(f"{APP_NAME} {__version__}")
    root.geometry("820x760")
    root.minsize(680, 560)
    try:
        root.iconbitmap(_resource("icon.ico"))
    except Exception:
        pass
    q = queue.Queue()

    style = ttk.Style()
    style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
    style.configure("Hint.TLabel", foreground="#5a5a5a")
    style.configure("Step.TLabel", font=("Segoe UI", 11, "bold"))
    style.configure("Go.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8))
    style.configure("Check.TButton", padding=(12, 8))
    style.configure("Status.TLabel", font=("Segoe UI", 11))
    style.configure("Good.Status.TLabel", foreground="#1b6e2a")
    style.configure("Warn.Status.TLabel", foreground="#9a5b00")
    style.configure("Bad.Status.TLabel", foreground="#b3261e")
    style.configure("Link.TLabel", foreground="#1a5fb4")

    inp, outp = tk.StringVar(), tk.StringVar()
    mode = tk.StringVar(value="auto")
    offset, takeoff = tk.StringVar(value="0"), tk.StringVar()
    advanced = tk.BooleanVar(value=False)
    crop = tk.BooleanVar(value=True)
    full = tk.BooleanVar(value=False)
    status = tk.StringVar(value="Choose your WebODM all.zip to start.")
    suggested = {"out": ""}  # output folder we filled in, replaced when the input changes

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(0, weight=1)

    ttk.Label(frm, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(frm, text="Puts drone survey heights at true height above sea level (NZVD2016). "
                        "New Zealand sites only.", style="Hint.TLabel").grid(
        row=1, column=0, sticky="w", pady=(0, 14))

    # 1. input
    s1 = ttk.Frame(frm)
    s1.grid(row=2, column=0, sticky="ew")
    s1.columnconfigure(0, weight=1)
    ttk.Label(s1, text="1.  What do you want to fix?", style="Step.TLabel").grid(
        row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(s1, text="The all.zip you downloaded from WebODM (\"Download Assets > All Assets\"), "
                       "or the folder you unzipped it to.", style="Hint.TLabel").grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Entry(s1, textvariable=inp).grid(row=2, column=0, sticky="ew", padx=(0, 6))

    def pick_zip():
        p = filedialog.askopenfilename(title="Choose the WebODM all.zip",
                                       filetypes=[("WebODM download", "*.zip"), ("All files", "*.*")])
        if p:
            inp.set(os.path.normpath(p))

    def pick_in_dir():
        p = filedialog.askdirectory(title="Choose the WebODM results folder")
        if p:
            inp.set(os.path.normpath(p))

    ttk.Button(s1, text="Choose zip...", command=pick_zip).grid(row=2, column=1)
    ttk.Button(s1, text="Choose folder...", command=pick_in_dir).grid(row=2, column=2, padx=(6, 0))

    # 2. output
    s2 = ttk.Frame(frm)
    s2.grid(row=3, column=0, sticky="ew", pady=(14, 0))
    s2.columnconfigure(0, weight=1)
    ttk.Label(s2, text="2.  Where should the corrected files go?", style="Step.TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(s2, text="A new folder. Your original files are never changed.",
              style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
    ttk.Entry(s2, textvariable=outp).grid(row=2, column=0, sticky="ew", padx=(0, 6))

    def pick_out_dir():
        p = filedialog.askdirectory(title="Choose where to save the corrected files")
        if p:
            outp.set(os.path.normpath(p))

    ttk.Button(s2, text="Change...", command=pick_out_dir).grid(row=2, column=1)
    ttk.Label(s2, text="You'll get two GeoTIFFs: the orthophoto and an elevation model in NZVD2016.",
              style="Hint.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
    ttk.Checkbutton(s2, text="Crop off the black edges (keeps the largest rectangle with no gaps)",
                    variable=crop).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))
    ttk.Checkbutton(s2, text="Also export the point cloud and 3D model (.laz, .obj)",
                    variable=full).grid(row=5, column=0, columnspan=2, sticky="w")

    def suggest_output(*_):
        src = inp.get().strip().strip('"')
        if not src or outp.get() not in ("", suggested["out"]):
            return  # don't overwrite a folder the user chose
        base = os.path.splitext(os.path.basename(src.rstrip("\\/")))[0] or "output"
        suggested["out"] = os.path.join(os.path.dirname(src.rstrip("\\/")), f"{base} - NZVD2016")
        outp.set(suggested["out"])
        status.set("Ready. Click \"Check first\" to see how far out the heights are, "
                   "or \"Fix heights\" to go.")
        status_lbl.configure(style="Status.TLabel")

    inp.trace_add("write", suggest_output)

    # 3. go
    s3 = ttk.Frame(frm)
    s3.grid(row=4, column=0, sticky="ew", pady=(14, 0))
    s3.columnconfigure(1, weight=1)
    ttk.Label(s3, text="3.  Fix the heights", style="Step.TLabel").grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
    b_check = ttk.Button(s3, text="Check first (changes nothing)", style="Check.TButton",
                         command=lambda: start(True))
    b_go = ttk.Button(s3, text="Fix heights", style="Go.TButton", command=lambda: start(False))
    b_check.grid(row=1, column=0, sticky="w")
    b_go.grid(row=1, column=2, sticky="e")
    progress = ttk.Progressbar(s3, mode="indeterminate", length=180)
    progress.grid(row=1, column=1, padx=12)
    progress.grid_remove()

    res = ttk.Frame(frm)
    res.grid(row=5, column=0, sticky="ew", pady=(12, 0))
    res.columnconfigure(0, weight=1)
    status_lbl = ttk.Label(res, textvariable=status, style="Status.TLabel", wraplength=740,
                           justify="left")
    status_lbl.grid(row=0, column=0, sticky="w")
    b_open = ttk.Button(res, text="Open output folder", command=lambda: os.startfile(outp.get()))
    b_open.grid(row=0, column=1, sticky="ne", padx=(8, 0))
    b_open.grid_remove()

    # advanced options (hidden unless asked for)
    adv = ttk.Frame(frm)
    adv.grid(row=6, column=0, sticky="ew", pady=(14, 0))
    adv.columnconfigure(0, weight=1)
    mf = ttk.LabelFrame(adv, text="Correction method", padding=8)

    def toggle_advanced():
        if advanced.get():
            mf.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        else:
            mode.set("auto")
            mf.grid_remove()

    ttk.Checkbutton(adv, text="Show advanced options (most people don't need these)",
                    variable=advanced, command=toggle_advanced).grid(row=0, column=0, sticky="w")
    ttk.Radiobutton(mf, text="Automatic - match LINZ LiDAR elevation (recommended, needs internet)",
                    variable=mode, value="auto").grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Radiobutton(mf, text="GNSS ellipsoid heights -> NZVD2016 with NZGeoid2016 (RTK/PPK drones only)",
                    variable=mode, value="geoid").grid(row=1, column=0, columnspan=2, sticky="w")
    ttk.Radiobutton(mf, text="Raw DJI photos: takeoff ground height + relative altitude. Takeoff height (m):",
                    variable=mode, value="takeoff").grid(row=2, column=0, sticky="w")
    ttk.Entry(mf, textvariable=takeoff, width=10).grid(row=2, column=1, sticky="w", padx=(6, 0))
    ttk.Radiobutton(mf, text="Fixed offset - metres to add (negative to lower):",
                    variable=mode, value="offset").grid(row=3, column=0, sticky="w")
    ttk.Entry(mf, textvariable=offset, width=10).grid(row=3, column=1, sticky="w", padx=(6, 0))

    # details log
    ttk.Label(frm, text="Details", style="Hint.TLabel").grid(row=7, column=0, sticky="w", pady=(12, 2))
    logbox = scrolledtext.ScrolledText(frm, height=10, font=("Consolas", 9), relief="flat",
                                       borderwidth=1)
    logbox.grid(row=8, column=0, sticky="nsew")
    frm.rowconfigure(8, weight=1)

    foot = ttk.Frame(frm)
    foot.grid(row=9, column=0, sticky="ew", pady=(8, 0))
    help_lbl = ttk.Label(foot, text="Help & instructions", style="Link.TLabel", cursor="hand2")
    help_lbl.pack(side="left")
    help_lbl.bind("<Button-1>", lambda e: webbrowser.open(HELP_URL))
    ttk.Label(foot, text="Elevation data: LINZ, CC BY 4.0", style="Hint.TLabel").pack(side="right")

    buttons = [b_check, b_go]

    def start(check):
        src = inp.get().strip().strip('"')
        dst = outp.get().strip().strip('"')
        if not src:
            messagebox.showinfo(APP_NAME, "First choose the WebODM all.zip (step 1).")
            return
        if not os.path.exists(src):
            messagebox.showerror(APP_NAME, f"Can't find:\n{src}")
            return
        if not check:
            if not dst:
                messagebox.showinfo(APP_NAME, "Choose where to save the corrected files (step 2).")
                return
            if os.path.isdir(dst) and os.listdir(dst) and not messagebox.askyesno(
                    APP_NAME, f"This folder already has files in it:\n{dst}\n\n"
                              "Files with the same names will be replaced. Continue?"):
                return
        try:
            off = float(offset.get() or 0)
            tko = float(takeoff.get()) if takeoff.get().strip() else None
        except ValueError:
            messagebox.showerror(APP_NAME, "The offset and takeoff height must be numbers.")
            return
        for b in buttons:
            b.state(["disabled"])
        b_open.grid_remove()
        progress.grid()
        progress.start(12)
        status.set("Checking... (this usually takes under a minute)" if check else
                   "Fixing heights... (this usually takes a minute or two)")
        status_lbl.configure(style="Status.TLabel")
        logbox.delete("1.0", "end")

        def work():
            try:
                result = run(src, dst, mode.get(), off, tko, check,
                             log=lambda m: q.put(("log", m)), full=full.get(), crop=crop.get())
                q.put(("done", (check, result or {}, dst)))
            except Exception as e:
                q.put(("err", str(e) or e.__class__.__name__))
        threading.Thread(target=work, daemon=True).start()

    def finish():
        progress.stop()
        progress.grid_remove()
        for b in buttons:
            b.state(["!disabled"])

    def poll():
        try:
            while True:
                kind, msg = q.get_nowait()
                if kind == "log":
                    logbox.insert("end", msg + "\n")
                    logbox.see("end")
                elif kind == "done":
                    finish()
                    check, result, dst = msg
                    text, warn = _describe_result(result, check, dst)
                    status.set(text)
                    status_lbl.configure(style="Warn.Status.TLabel" if warn else "Good.Status.TLabel")
                    if not check and os.path.isdir(dst):
                        b_open.grid()
                else:
                    finish()
                    status.set(f"Something went wrong: {msg}")
                    status_lbl.configure(style="Bad.Status.TLabel")
                    logbox.insert("end", f"ERROR: {msg}\n")
                    logbox.see("end")
        except queue.Empty:
            pass
        root.after(100, poll)

    poll()
    root.mainloop()


def _selftest():
    """Used by the build: exit 0 if every library and the bundled geoid grid load."""
    import laspy
    import rasterio  # noqa: F401
    from pyproj import Transformer
    laspy.LazBackend.Lazrs.is_available() or sys.exit(2)
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True).transform(174.76, -36.85)
    if topo50_sheet(x, y) is None:
        sys.exit(3)
    n = GeoidGrid(find_geoid_grid(log=lambda m: None)).undulation(174.76, -36.85)[0]
    sys.exit(0 if 30 < n < 40 else 4)  # Auckland N is about 34-35 m


def main():
    if len(sys.argv) == 1:
        gui()
        return
    if sys.argv[1] == "--selftest":
        _selftest()
    ap = argparse.ArgumentParser(description="Convert drone photos or WebODM outputs to NZVD2016 heights.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("input", help="photo folder, WebODM output folder, or WebODM all.zip")
    ap.add_argument("output", nargs="?", help="output folder for corrected copies")
    ap.add_argument("--mode", choices=["auto", "geoid", "offset", "takeoff"], default="auto")
    ap.add_argument("--offset", type=float, default=0.0, help="metres to add (offset mode)")
    ap.add_argument("--takeoff-elev", type=float, help="takeoff ground height, m above sea level (takeoff mode)")
    ap.add_argument("--check", action="store_true", help="only report heights, write nothing")
    ap.add_argument("--keep-edges", action="store_true",
                    help="don't crop the exported GeoTIFFs to the gap-free area")
    ap.add_argument("--full", action="store_true",
                    help="also correct the point cloud, 3D model and WebODM's own DEMs")
    ap.add_argument("--grid", help=f"path to {GEOID_FILENAME} (downloaded automatically if omitted)")
    a = ap.parse_args()
    try:
        run(a.input, a.output, a.mode, a.offset, a.takeoff_elev, a.check, a.grid,
            full=a.full, crop=not a.keep_edges)
    except FixError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
