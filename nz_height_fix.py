#!/usr/bin/env python3
"""
WebODM Height Fix NZ - convert drone photos or WebODM outputs to NZVD2016 heights
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
APP_NAME = "WebODM Height Fix NZ"
HELP_URL = "https://github.com/nicholasjg503/WebODM-Height-Fix-NZ#readme"

DEFAULT_CRS = "EPSG:2105"  # NZGD2000 / Mount Eden 2000
# Choices for the exports: NZTM, the 28 NZGD2000 meridional circuits, or WebODM's own UTM (None)
EXPORT_CRS = [("EPSG:2193", "NZGD2000 / New Zealand Transverse Mercator 2000")] + [
    (f"EPSG:{c}", None) for c in range(2105, 2133)]

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
        self.align = None
        if mode == "align":  # offset is an Alignment fitted against LINZ
            self.align = offset
            return
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
        if self.align is not None:
            return self.align.dz(x, y)
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
        if self.align is not None:
            return self.align.summary()
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
# Height alignment to LINZ 1 m LiDAR: a height plane fitted on stable open ground,
# so real change (e.g. slips since the LiDAR) stands out.
# --------------------------------------------------------------------------

def _linz_mosaic(coll, bbox, res=1.0):
    """A LINZ layer over an NZTM2000 bbox as (heights with NaN, transform), or None."""
    import rasterio
    from rasterio.errors import RasterioIOError
    from rasterio.transform import from_origin
    xmin, ymin = np.floor(bbox[0] / res) * res, np.floor(bbox[1] / res) * res
    xmax, ymax = np.ceil(bbox[2] / res) * res, np.ceil(bbox[3] / res) * res
    w, h = int(round((xmax - xmin) / res)), int(round((ymax - ymin) / res))
    out = np.full((h, w), np.nan)
    sheets = {topo50_sheet(x, y) for x in np.linspace(xmin, xmax, 6)
              for y in np.linspace(ymin, ymax, 6)} - {None}
    with rasterio.Env(**_gdal_env()):
        for sheet in sorted(sheets):
            try:
                with rasterio.open(f"/vsicurl/{LINZ_ELEVATION}{coll}/{sheet}.tiff") as ds:
                    nd = ds.nodata if ds.nodata is not None else -9999.0
                    a = ds.read(1, window=ds.window(xmin, ymin, xmax, ymax), out_shape=(h, w),
                                boundless=True, fill_value=nd).astype(np.float64)
            except RasterioIOError:
                continue  # no tile here (sea / no coverage)
            a[a == nd] = np.nan
            take = np.isnan(out) & np.isfinite(a)
            out[take] = a[take]
    if not np.isfinite(out).any():
        return None
    return out, from_origin(xmin, ymax, res, res)


def _bilinear(grid, x, y):
    """Sample a (array, transform) grid at map coordinates; NaN outside or next to no-data."""
    a, T = grid
    c, r = ~T * (np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    c, r = np.asarray(c) - 0.5, np.asarray(r) - 0.5  # pixel centres
    h, w = a.shape
    c0, r0 = np.floor(c).astype(np.int64), np.floor(r).astype(np.int64)
    ok = (c0 >= 0) & (r0 >= 0) & (c0 < w - 1) & (r0 < h - 1)
    out = np.full(c.shape, np.nan)
    c0, r0 = c0[ok], r0[ok]
    fc, fr = c[ok] - c0, r[ok] - r0
    out[ok] = (a[r0, c0] * (1 - fc) * (1 - fr) + a[r0, c0 + 1] * fc * (1 - fr)
               + a[r0 + 1, c0] * (1 - fc) * fr + a[r0 + 1, c0 + 1] * fc * fr)
    return out


def _flat_mask(a):
    """Cells whose 3x3 neighbourhood is dead flat: LINZ hydro-flattens water surfaces."""
    p = np.pad(a, 1, constant_values=np.nan)
    h, w = a.shape
    stack = np.stack([p[i:i + h, j:j + w] for i in range(3) for j in range(3)])
    with np.errstate(invalid="ignore"):
        return (np.nanmax(stack, axis=0) - np.nanmin(stack, axis=0)) < 0.01


def _surface_samples(path, fallback_crs, cell=1.0, target=60000):
    """(x, y, z) samples of a WebODM surface on a ~1 m grid (to match LINZ), plus CRS."""
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    if path.lower().endswith((".laz", ".las")):
        a, T, crs, _ = _cloud_to_dem(path, None, None, fallback_crs, res=cell)
    else:
        with rasterio.open(path) as src:
            k = max(1.0, cell / abs(src.res[0]))
            shape = (max(1, int(src.height / k)), max(1, int(src.width / k)))
            a = src.read(1, out_shape=shape, resampling=Resampling.average,
                         masked=True).astype(np.float64).filled(np.nan)
            T = src.transform * Affine.scale(src.width / shape[1], src.height / shape[0])
            crs = src.crs
    if crs is None:
        raise FixError(f"{os.path.basename(path)} has no coordinate system.")
    rows, cols = np.nonzero(np.isfinite(a))
    if len(rows) > target:
        pick = np.random.default_rng(0).choice(len(rows), target, replace=False)
        rows, cols = rows[pick], cols[pick]
    x, y = T * (cols + 0.5, rows + 0.5)
    return np.asarray(x), np.asarray(y), a[rows, cols], crs


class Alignment:
    """Height correction plane that puts a WebODM model onto LINZ.

    Horizontal positions are left as WebODM made them. (dx, dy) and the twist `kappa` (radians,
    about (x0, y0)) say where each spot of the model is in LINZ: they're only used to compare
    like with like - LINZ is sampled at the matching spot - because on a slope a 2 m sideways
    offset alone looks like ~1 m of height change.
    """

    def __init__(self, x0, y0, coef, spread, size, linz, dx=0.0, dy=0.0, kappa=0.0):
        self.x0, self.y0, self.dx, self.dy, self.kappa = x0, y0, dx, dy, kappa
        self.coef, self.spread, self.size, self.linz = coef, spread, size, linz

    def matching(self):
        """Affine taking a model position to the matching LINZ position (model coordinates)."""
        from affine import Affine
        k = self.kappa
        return Affine(1, -k, self.dx + k * self.y0, k, 1, self.dy - k * self.x0)

    def dz(self, x, y):
        a, b, c = self.coef
        return -(a * (np.asarray(x) - self.x0) + b * (np.asarray(y) - self.y0) + c)

    @property
    def offset(self):
        return -float(self.coef[2])

    @property
    def tilt(self):
        return float(np.hypot(self.coef[0], self.coef[1]) * self.size)

    def summary(self):
        return f"{self.offset:+.3f} m (tilt {self.tilt:.2f} m across the site)"


def linz_align(path, kind, log, fallback_crs=None):
    """Fit an Alignment of this DSM/DTM/point cloud to LINZ 1 m LiDAR, or None without LiDAR."""
    from pyproj import Transformer
    x, y, z, crs = _surface_samples(path, fallback_crs)
    if len(z) < 200:
        raise FixError(f"{os.path.basename(path)} has too few valid heights to compare.")
    to_nztm = Transformer.from_crs(crs, "EPSG:2193", always_xy=True)
    X, Y = (np.asarray(v) for v in to_nztm.transform(x, y))
    cx, cy = float(x.mean()), float(y.mean())
    jx, jy = to_nztm.transform([cx, cx + 1, cx], [cy, cy, cy + 1])
    J = np.array([[jx[1] - jx[0], jx[2] - jx[0]], [jy[1] - jy[0], jy[2] - jy[0]]])

    def at(grid, dx=0.0, dy=0.0):
        """A LINZ grid at the sample points, offset (dx, dy) m (scalars or per-point arrays)
        in the model's coordinates."""
        dx, dy = np.broadcast_arrays(np.asarray(dx, float), np.asarray(dy, float))
        sx = J[0, 0] * dx + J[0, 1] * dy
        sy = J[1, 0] * dx + J[1, 1] * dy
        return _bilinear(grid, X + sx, Y + sy)

    log(f"    comparing {os.path.basename(path)} with LINZ 1 m LiDAR ...")
    bbox = (X.min() - 15, Y.min() - 15, X.max() + 15, Y.max() + 15)
    dsm = _linz_mosaic("new-zealand/dsm_1m/2193", bbox)
    dem = _linz_mosaic("new-zealand/dem_1m/2193", bbox)
    if dsm is None or dem is None:
        log("    no LINZ 1 m LiDAR here")
        return None
    flat = (_flat_mask(dem[0]).astype(np.float64), dem[1])
    ref = dem if kind == "dtm" else dsm

    land = at(flat) < 0.5  # not water (NaN compares False, so no-data drops out)
    open_ = land & np.isfinite(z - at(ref)) & (np.abs(at(dsm) - at(dem)) < 0.3)
    if open_.sum() < 200:
        log("    not much open ground - fitting heights to all land instead")
        open_ = land & np.isfinite(z - at(ref))
    if open_.sum() < 50:
        return None

    # Starting point: coarse search for the sideways offset on open ground (trees and buildings
    # change shape between surveys).
    def score(dx, dy):
        r_ = (z - at(ref, dx, dy))[open_]
        r_ = r_[np.isfinite(r_)]
        return np.median(np.abs(r_ - np.median(r_))) if len(r_) >= 200 else np.inf
    best = min((score(dx, dy), dx, dy) for dx in np.arange(-8, 8.01, 1.0)
               for dy in np.arange(-8, 8.01, 1.0))

    # LZD ("least Z-difference", Rosenholm & Torlegard 1988) with robust reweighting: solve
    # together for the height shift, tilt (omega, phi), twist about the vertical (kappa) and
    # sideways shift that best fit the model onto LINZ, using the terrain's own slopes. Ground
    # that has changed since the LiDAR (slips, earthworks) gets down-weighted away, so it can't
    # drag the fit. Nothing is moved sideways in the outputs - the horizontal part is only used
    # to compare each spot with the matching spot in LINZ.
    xs, ys = x - cx, y - cy
    zs = z - np.median(z[open_])
    tz, tx, ty, om, ph, ka = 0.0, float(best[1]), float(best[2]), 0.0, 0.0, 0.0
    tz = float(np.nanmedian((at(ref, tx, ty) - z)[open_]))
    w = open_.astype(np.float64)
    for _ in range(25):
        hx = tx - ka * ys + ph * zs  # where each sample lands in LINZ (model coordinates)
        hy = ty + ka * xs - om * zs
        zr = at(ref, hx, hy)
        gx = (at(ref, hx + 1, hy) - at(ref, hx - 1, hy)) / 2  # LINZ slopes there
        gy = (at(ref, hx, hy + 1) - at(ref, hx, hy - 1)) / 2
        v = z + tz - ph * xs + om * ys - zr  # model (moved) minus LINZ
        ok = open_ & np.isfinite(v) & np.isfinite(gx) & np.isfinite(gy)
        sig = max(0.05, 1.4826 * float(np.median(np.abs(v[ok] - np.median(v[ok])))))
        u = (v - np.median(v[ok])) / (4.685 * sig)
        w = np.where(ok & (np.abs(u) < 1), (1 - u ** 2) ** 2, 0.0)  # Tukey biweight
        A = np.c_[np.ones_like(v), -gx, -gy, ys + gy * zs, -xs - gx * zs, gx * ys - gy * xs]
        sw = np.sqrt(w)[ok]
        step = np.linalg.lstsq(A[ok] * sw[:, None], -v[ok] * sw, rcond=None)[0]
        tz, tx, ty, om, ph, ka = (p + s for p, s in zip((tz, tx, ty, om, ph, ka), step))
        if abs(step[0]) < 0.002 and np.hypot(step[1], step[2]) < 0.01 and abs(step[5]) < 1e-6:
            break
    if not all(np.isfinite((tz, tx, ty, om, ph, ka))) or np.hypot(tx, ty) > 25:
        raise FixError("Couldn't line the model up with LINZ (the fit didn't settle).")
    inl = w > 0
    res = v - np.median(v[inl])
    spread = float(np.median(np.abs(res[inl])))
    size = float(max(np.ptp(x), np.ptp(y)))
    # height correction = what the fit adds to z: tz - phi*x + omega*y  (Alignment.dz form)
    coef = np.array([ph, -om, -tz])
    dx, dy = round(float(tx), 2), round(float(ty), 2)
    al = Alignment(cx, cy, coef, spread, size, {"dsm": dsm, "dem": dem, "flat": flat}, dx, dy,
                   float(ka))
    shift = np.hypot(dx, dy)
    if shift >= 0.3 or abs(ka) * size / 2 >= 0.3:
        log(f"    sideways: the model sits {shift:.1f} m from LINZ ({-dx:+.1f} m east, "
            f"{-dy:+.1f} m north of it)"
            + (f", twisted {np.degrees(ka):+.2f} deg ({abs(ka) * size / 2:.1f} m at the edges)"
               if abs(ka) * size / 2 >= 0.3 else "")
            + ". Your files aren't moved - LINZ is just compared at the matching spot, so slopes "
              "don't show false change.")
    stable = inl.sum() / open_.sum()
    log(f"    vertical: {al.offset:+.2f} m, tilt {al.tilt:.2f} m across {size:.0f} m, "
        f"fitted on {inl.sum():,} open-ground cells ({100 * stable:.0f}% matched LINZ, "
        f"typical misfit +/-{spread:.2f} m)")
    if stable < 0.6:
        log("    WARNING: a lot of the open ground differs from LINZ (big slip or earthworks?). "
            "Check the change map.")
    if al.tilt > 1.0:
        log("    WARNING: the model is noticeably tilted - ground control points would help.")
    return al


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
            dst.update_tags(VERTICAL_DATUM="NZVD2016", NOTE=f"corrected {field.summary()}")
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
# Simple export: one corrected elevation GeoTIFF + the orthophoto GeoTIFF, trimmed
# to the mapped area, with everything outside it marked as no-data (see-through).
# --------------------------------------------------------------------------

def _data_window(src):
    """Smallest window holding every valid (non-transparent) pixel of a raster."""
    from rasterio.windows import Window
    rows = np.zeros(src.height, dtype=bool)
    cols = np.zeros(src.width, dtype=bool)
    for r in range(0, src.height, 1024):
        n = min(1024, src.height - r)
        m = src.dataset_mask(window=Window(0, r, src.width, n)) > 0
        rows[r:r + n] = m.any(axis=1)
        cols |= m.any(axis=0)
    if not rows.any():
        return None
    r0, r1 = np.nonzero(rows)[0][[0, -1]]
    c0, c1 = np.nonzero(cols)[0][[0, -1]]
    return Window(int(c0), int(r0), int(c1 - c0 + 1), int(r1 - r0 + 1))


def _overview_factors(width, height):
    return [f for f in (2, 4, 8, 16, 32, 64) if min(width, height) // f >= 256]


def _read_dem(path, bounds, cell=None):
    """Elevation raster (NaN for nodata), its transform and CRS, limited to bounds if given.

    With `cell` (metres) coarser than the file, cells are averaged down to that size as they
    are read - a 5 cm WebODM DTM is otherwise tens of millions of cells.
    """
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.windows import Window
    with rasterio.open(path) as src:
        full = Window(0, 0, src.width, src.height)
        win = full if bounds is None else \
            src.window(*bounds).round_offsets().round_lengths().intersection(full)
        res = abs(src.res[0])
        if cell and cell > res * 1.01:
            shape = (max(1, int(round(win.height * res / cell))),
                     max(1, int(round(win.width * res / cell))))
            a = src.read(1, window=win, out_shape=shape, resampling=Resampling.average,
                         masked=True).astype(np.float64).filled(np.nan)
            T = src.window_transform(win) * Affine.scale(win.width / shape[1],
                                                         win.height / shape[0])
            return a, T, src.crs
        a = src.read(1, window=win).astype(np.float64)
        if src.nodata is not None:
            a[a == src.nodata] = np.nan
        return a, src.window_transform(win), src.crs


def _to_grid(a, src_transform, src_crs, dst_transform, dst_shape, dst_crs, method="bilinear"):
    """Resample a float grid (NaN = no data) onto another grid, with GDAL doing the work."""
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
    out = np.full(dst_shape, np.nan)
    reproject(np.asarray(a, dtype=np.float64), out, src_transform=src_transform, src_crs=src_crs,
              src_nodata=np.nan, dst_transform=dst_transform, dst_crs=dst_crs,
              dst_nodata=np.nan, resampling=getattr(Resampling, method))
    return out


def _box_mean(a, k):
    """Mean of a 2-D array over a k x k window (k odd), edges padded with zeros."""
    p = k // 2
    c = np.pad(a, p).cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    h, w = a.shape
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)


def _cloud_to_dem(path, bounds, out_crs, fallback_crs, res=None, max_cells=16_000_000):
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
        res = max(res or 0.05, np.sqrt(4.0 / density) if res is None else 0,  # ~4 points a cell
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
    # drop stray cells at the ragged edge of the model: keep only cells where most of the
    # surrounding ~7x7 cells have points too
    valid = np.isfinite(dem) & (_box_mean(np.isfinite(dem).astype(np.float64), 7) >= 0.5)
    dem[~valid] = np.nan
    # fill gaps up to ~10 cells from real data; anything further stays nodata
    filled = fillnodata(np.where(valid, dem, 0).astype(np.float32), mask=valid.astype(np.uint8),
                        max_search_distance=10)
    reach = fillnodata(valid.astype(np.float32), mask=valid.astype(np.uint8),
                       max_search_distance=10)
    dem = np.where(reach > 0.5, filled, np.nan).astype(np.float64)
    return dem, from_origin(xmin, ymax, res, res), out_crs, res


def _write_ortho(src_path, out, out_crs):
    """Write the orthophoto, reprojected to out_crs if given, trimmed to the mapped area.
    Outside the map is marked as no-data twice over: alpha = 0
    (for programs that use transparency) and every band = 0 with nodata=0 (for programs that
    only read the no-data value, which otherwise show the outside as black)."""
    import rasterio
    from affine import Affine
    from pyproj import CRS
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import Window
    with rasterio.open(src_path) as src:
        has_alpha = src.count == 4 and src.colorinterp[3].name == "alpha"
        if out_crs is None or CRS.from_user_input(out_crs) == CRS.from_user_input(src.crs):
            ds, transform0 = src, src.transform
        else:
            alpha = {"src_alpha": 4, "dst_alpha": 4} if has_alpha else {"add_alpha": True}
            ds = WarpedVRT(src, crs=out_crs, resampling=Resampling.bilinear, **alpha)
            transform0 = ds.transform
            has_alpha = True
        try:
            win = _data_window(ds)
            if win is None:
                raise FixError("The orthophoto is empty (every pixel is no-data).")
            w, h = int(win.width), int(win.height)
            col0, row0 = int(win.col_off), int(win.row_off)
            byte_rgb = ds.count >= 3 and ds.dtypes[0] == "uint8"
            profile = dict(driver="GTiff", width=w, height=h, count=ds.count, dtype=ds.dtypes[0],
                           crs=ds.crs, transform=transform0 * Affine.translation(col0, row0),
                           tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                           predictor=2, BIGTIFF="IF_SAFER")
            if byte_rgb:
                profile.update(photometric="RGB", nodata=0)
                if has_alpha:
                    # Tag band 4 as alpha at creation. "Associated" alpha is the form most
                    # non-GIS TIFF readers understand; with alpha only ever 0 or 255 (and
                    # colour 0 where alpha is 0) the pixels are identical either way.
                    profile["alpha"] = "PREMULTIPLIED"
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with rasterio.open(out, "w", **profile) as dst:
                for r in range(0, h, 1024):
                    n = min(1024, h - r)
                    sub = Window(col0, row0 + r, w, n)
                    a = ds.read(window=sub)
                    if byte_rgb:
                        inside = ds.dataset_mask(window=sub) > 127
                        # real black pixels inside the map become 1 so they aren't no-data
                        a[:, inside] = np.maximum(a[:, inside], 1)
                        a[:, ~inside] = 0
                        if has_alpha:
                            a[3] = np.where(inside, 255, 0)  # crisp edge after resampling
                    dst.write(a, window=Window(0, r, w, n))
                if has_alpha:
                    dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue,
                                       ColorInterp.alpha)
                factors = _overview_factors(w, h)
                if factors:
                    dst.build_overviews(factors, Resampling.average)
        finally:
            if ds is not src:
                ds.close()


def _write_png_copy(tif, png):
    """Transparent PNG of an RGBA GeoTIFF, placed by a .pgw world file and .prj, for CAD
    programs (BricsCAD, AutoCAD) that ignore GeoTIFF no-data and transparency."""
    import rasterio
    from pyproj import CRS
    from pyproj.enums import WktVersion
    with rasterio.open(tif) as src:
        a = src.read()
        if src.count == 3:
            a = np.concatenate([a, (src.dataset_mask() > 0)[None].astype(np.uint8) * 255])
        T, crs = src.transform, src.crs
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):  # no .aux.xml clutter
        with rasterio.open(png, "w", driver="PNG", width=a.shape[2], height=a.shape[1],
                           count=4, dtype="uint8") as dst:
            dst.write(a[:4])
    base = os.path.splitext(png)[0]
    with open(base + ".pgw", "w") as f:  # world file: centre of the top-left pixel
        f.write("\n".join(f"{v:.10f}" for v in (T.a, T.d, T.b, T.e, T.c + T.a / 2 + T.b / 2,
                                                T.f + T.d / 2 + T.e / 2)) + "\n")
    with open(base + ".prj", "w") as f:
        f.write(CRS.from_user_input(crs).to_wkt(WktVersion.WKT1_ESRI))


def _warp_array(a, transform, crs, dst_crs):
    """Reproject a float grid (NaN = no data) to dst_crs at about the same cell size."""
    from rasterio.enums import Resampling
    from rasterio.transform import array_bounds
    from rasterio.warp import calculate_default_transform, reproject
    h, w = a.shape
    west, south, east, north = array_bounds(h, w, transform)
    T2, w2, h2 = calculate_default_transform(crs, dst_crs, w, h, west, south, east, north,
                                             resolution=abs(transform.a))
    out = np.full((h2, w2), np.nan)
    reproject(a, out, src_transform=transform, src_crs=crs, src_nodata=np.nan,
              dst_transform=T2, dst_crs=dst_crs, dst_nodata=np.nan,
              resampling=Resampling.bilinear)
    return out, T2


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


def export_maps(files, out_root, name, mode, offset, geoid, task_crs, check, log, out_crs=None,
                cell=0.25, detect=True):
    """Write '<name> - orthophoto.tif', '<name> - DSM/DTM NZVD2016.tif' and, when aligned to
    LINZ, '<name> - change since LINZ (m).tif' plus the detected change areas.

    `cell` is the elevation model's cell size in metres (None keeps WebODM's). Returns a dict
    of what was found (for the window's summary)."""
    import rasterio
    from rasterio.transform import array_bounds
    from rasterio.windows import bounds as window_bounds
    by_name = {os.path.basename(f).lower(): f for f in files}
    ortho = by_name.get("odm_orthophoto.tif")
    dem_path = by_name.get("dtm.tif") or by_name.get("dsm.tif")
    clouds = [f for f in files if f.lower().endswith((".laz", ".las"))]
    log("Exporting GeoTIFFs:")

    bounds = ortho_crs = ortho_win = None
    if ortho:
        with rasterio.open(ortho) as src:
            ortho_crs = src.crs
            ortho_win = _data_window(src)
            if ortho_win is None:
                raise FixError("The orthophoto is empty (every pixel is no-data).")
            bounds = window_bounds(ortho_win, src.transform)
            rx, ry = src.res
            log(f"    orthophoto: {ortho_win.width * rx:.0f} x {ortho_win.height * ry:.0f} m at "
                f"{rx * 100:.0f} cm, area outside the map marked as no-data")
    else:
        log("    no orthophoto in the input - only the elevation model is exported")

    dsm = None
    if dem_path:
        kind = "DTM" if os.path.basename(dem_path).lower() == "dtm.tif" else "DSM"
        dem, transform, dem_crs = _read_dem(dem_path, bounds, cell)
        note = f"from WebODM {os.path.basename(dem_path)}"
        if kind == "DTM" and "dsm.tif" in by_name and mode == "align":
            # WebODM's surface model on the same grid: shows trees/buildings now, and is the true
            # ground wherever the ground is bare (see below)
            d, dT, dcrs = _read_dem(by_name["dsm.tif"], bounds, cell)
            dsm = _to_grid(d, dT, dcrs, transform, dem.shape, dem_crs, "average")
            del d
            # cars, tanks, utes and sheds aren't ground even when they aren't green: anything
            # standing up from its surroundings like a bump narrower than ~6 m is replaced by
            # the ground model. (A grey-scale opening keeps slopes, steps and scarps intact.)
            import scipy.ndimage as ndi
            k = max(3, int(round(6.0 / abs(transform.a))) | 1)
            filled = np.where(np.isfinite(dsm), dsm, np.nanmin(dsm) if np.isfinite(dsm).any() else 0)
            bump = (filled - ndi.grey_opening(filled, size=(k, k))) > 0.5
            dsm = np.where(bump, dem, dsm)
    elif clouds:
        kind = "DSM"
        dem, transform, dem_crs, res = _cloud_to_dem(clouds[0], bounds, ortho_crs, task_crs, res=cell)
        note = "gridded from the WebODM point cloud"
        log(f"    no DEM in the input - built a {res:.2f} m surface model (DSM) from the point cloud")
    else:
        dem = None
        log("    no elevation model or point cloud in the input - no DEM exported")

    if dem is not None:
        valid = np.isfinite(dem)
        if valid.any():
            rows, cols = np.nonzero(valid)
            xs, ys = transform * (cols + 0.5, rows + 0.5)
            west, south, east, north = array_bounds(dem.shape[0], dem.shape[1], transform)
            field = DeltaField(mode, offset, geoid, dem_crs, west, south, east, north)
            dem[rows, cols] += field(np.asarray(xs), np.asarray(ys))
            if dsm is not None:
                ok = np.isfinite(dsm)
                r2, c2 = np.nonzero(ok)
                x2, y2 = transform * (c2 + 0.5, r2 + 0.5)
                dsm[r2, c2] += field(np.asarray(x2), np.asarray(y2))
        log(f"    elevation model ({kind}): {dem.shape[1]} x {dem.shape[0]} cells at "
            f"{abs(transform.a):.2f} m, {100 * valid.mean():.0f}% with data")

    al = offset if mode == "align" else None
    change = det = None
    summary = {}
    if al is not None and dem is not None and dem_crs is not None:
        if dsm is not None and ortho:
            # WebODM's ground filter tends to cut steep bare faces (slip scarps, batters) out of
            # the DTM. Where the orthophoto shows bare ground, the surface model is the ground,
            # so use it - except on buildings that were already there when LINZ flew.
            bare = np.nan_to_num(_bare_ground(ortho, transform, dem.shape, dem_crs)) > 0.5
            then = (_linz_grid(al, "dsm", transform, dem.shape, dem_crs)
                    - _linz_grid(al, "dem", transform, dem.shape, dem_crs))
            with np.errstate(invalid="ignore"):
                use = bare & np.isfinite(dsm) & ~(((dsm - dem) > 2.5) & (then > 2.5))
            dem = np.where(use, dsm, dem)
            note += "; bare ground (from the orthophoto) taken from the surface model"
            log(f"    bare ground taken from WebODM's surface model ({100 * use.mean():.0f}% of cells) "
                "so steep bare faces aren't smoothed away")
        change = _change_since_linz(dem, transform, dem_crs, al, kind)
        if change is not None and kind == "DSM" and ortho:
            # surface model only: where the ground is bare now, the surface IS the ground, so
            # compare it with LINZ's ground (not its tree tops) to get the real ground change
            bare = np.nan_to_num(_bare_ground(ortho, transform, dem.shape, dem_crs)) > 0.5
            ground = _change_since_linz(dem, transform, dem_crs, al, "DTM")
            if ground is not None:
                then = (_linz_grid(al, "dsm", transform, dem.shape, dem_crs)
                        - _linz_grid(al, "dem", transform, dem.shape, dem_crs))
                with np.errstate(invalid="ignore"):
                    # grey roofs aren't green either: something tall then that still stands
                    # well above the ground now is a building, not bare ground
                    standing = (then > 2.5) & (ground > 2.5)
                change = np.where(bare & ~standing, ground, change)
        if change is not None and detect:
            if kind == "DSM":
                log("    note: this is a surface model, so trees and buildings can look like change. "
                    "For slips and earthworks, turn on DTM in WebODM.")
            surface_change = (_change_since_linz(dsm, transform, dem_crs, al, "DTM")
                              if dsm is not None else None)
            det = detect_change_areas(change, transform, dem_crs, al, log, surface_change, ortho)
            if det is not None:
                summary = {"slips": det["slips"], **det["counts"]}
    dsm = None

    if check:
        return summary
    from pyproj import CRS
    crs_name = CRS.from_user_input(out_crs or dem_crs or ortho_crs).name
    log(f"    coordinate system: {crs_name}")
    if ortho:
        out = os.path.join(out_root, f"{name} - orthophoto.tif")
        _write_ortho(ortho, out, out_crs)
        _write_png_copy(out, out[:-4] + ".png")
        log(f"    wrote {os.path.basename(out)} and a transparent .png (+ .pgw/.prj) for CAD")
    grids = []
    if dem is not None:
        grids.append((f"{name} - {kind} NZVD2016.tif", dem, transform, note))
    ref_name = "DEM" if kind == "DTM" else "DSM"
    if det is not None:
        grids.append((f"{name} - change since LINZ (m).tif", det["resid"], det["transform"],
                      f"this survey's {kind} minus LINZ 1 m LiDAR {ref_name}, broad warping removed, "
                      "trees/buildings/water blank; negative = ground lower now"))
    elif change is not None:
        grids.append((f"{name} - change since LINZ (m).tif", change, transform,
                      f"this survey's {kind} minus LINZ 1 m LiDAR {ref_name}; "
                      "negative = ground lower now"))
    for fn, grid, T, gnote in grids:
        crs = dem_crs
        if out_crs is not None and CRS.from_user_input(out_crs) != CRS.from_user_input(crs):
            grid, T = _warp_array(grid, T, crs, out_crs)
            crs = out_crs
        _write_dem(os.path.join(out_root, fn), grid, T, crs, gnote)
        log(f"    wrote {fn}")
    if det is not None and det["areas"]:
        ortho_out = os.path.join(out_root, f"{name} - orthophoto.tif") if ortho else None
        summary["preview"] = write_change_outputs(det, out_root, name, out_crs, ortho_out, log)
    return summary


def _linz_grid(al, key, transform, shape, crs, method="bilinear"):
    """One of the LINZ layers held by an Alignment ('dsm', 'dem', 'flat'), on a survey grid."""
    a, T = al.linz[key]
    # sample LINZ at the spot matching each survey cell (see Alignment)
    return _to_grid(a, T, "EPSG:2193", al.matching() * transform, shape, crs, method)


def _change_since_linz(dem, transform, crs, al, kind):
    """This survey minus the LINZ LiDAR it was aligned to, on the survey's grid (NaN on water)."""
    ref = _linz_grid(al, "dem" if kind == "DTM" else "dsm", transform, dem.shape, crs)
    water = _linz_grid(al, "flat", transform, dem.shape, crs, "nearest") >= 0.5
    change = np.where(water, np.nan, dem - ref)
    return change if np.isfinite(change).any() else None


# --------------------------------------------------------------------------
# Change detection: cut, fill and slips since the LINZ LiDAR.
#
# The raw change (survey minus LiDAR) is cleaned up first: trees and buildings
# (from LINZ's DSM - DEM, and the survey's own DSM - DTM), water and the ragged
# map edge are masked, and broad warping of the drone model is removed by
# subtracting a smooth background measured on the remaining stable ground.
# Whatever still differs by more than ~3x the noise, over a big enough area,
# is a change area: lower = cut, higher = fill, and a cut with fill directly
# downhill of it on a slope = slip (scarp + debris).
# --------------------------------------------------------------------------

DETECT_CELL = 0.5          # m - grid the detection runs on
BACKGROUND_WINDOW = 60.0   # m - broader warping of the drone model than this is removed
MIN_AREA = 20.0            # m2 - smaller patches are ignored
MIN_VOLUME = 5.0           # m3
# class codes in the change-areas raster, and how they're drawn
CLASSES = {1: ("CUT", (245, 160, 30)), 2: ("FILL", (40, 110, 225)),
           3: ("SLIP scarp", (225, 30, 30)), 4: ("SLIP debris", (150, 60, 210)),
           5: ("SLIP?", (245, 90, 140)), 6: ("CHECK", (235, 225, 60)),
           9: ("not measurable", (128, 128, 128))}
DXF_LAYERS = {"CUT": 30, "FILL": 5, "SLIP": 1, "SLIP?": 6, "CHECK": 2}  # layer: AutoCAD colour index


def _block_reduce(a, k, func):
    """Reduce a 2-D array by k x k blocks (a partial last block is padded with NaN)."""
    import warnings
    h, w = -(-a.shape[0] // k), -(-a.shape[1] // k)
    p = np.full((h * k, w * k), np.nan)
    p[:a.shape[0], :a.shape[1]] = a
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks
        return func(p.reshape(h, k, w, k), axis=(1, 3))


def _simplify(ring, tol):
    """Douglas-Peucker simplification of a closed ring of (x, y) points."""
    pts = np.asarray(ring, dtype=np.float64)
    if len(pts) < 5:
        return pts
    keep = np.zeros(len(pts), dtype=bool)
    keep[[0, -1]] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = pts[j] - pts[i]
        n = np.hypot(*seg)
        rel = pts[i + 1:j] - pts[i]
        d = (np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / n if n > 0
             else np.hypot(rel[:, 0], rel[:, 1]))
        m = int(np.argmax(d))
        if d[m] > tol:
            keep[i + 1 + m] = True
            stack += [(i, i + 1 + m), (i + 1 + m, j)]
    return pts[keep]


def _bare_ground(ortho, T, shape, crs):
    """Share of each grid cell that's bare ground (soil, rock, gravel, seal) in the orthophoto:
    not green, and not dark shadow."""
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    with rasterio.open(ortho) as o:
        s = max(1, int(abs(T.a) / abs(o.res[0]) / 2))  # ~2 orthophoto samples per cell
        shp = (max(1, o.height // s), max(1, o.width // s))
        rgb = o.read((1, 2, 3), out_shape=(3,) + shp, resampling=Resampling.average).astype(np.float64)
        inside = (o.read(4, out_shape=shp) > 0) if o.count >= 4 else np.ones(shp, dtype=bool)
        To = o.transform * Affine.scale(o.width / shp[1], o.height / shp[0])
        ocrs = o.crs
    r, g, b = rgb
    total = np.maximum(r + g + b, 1)
    exg = (2 * g - r - b) / total  # "excess green": grass/trees ~0.1-0.3, soil and rock ~0
    lit = inside & (total / 3 > 60)  # ignore dark shadow, where colour means little
    # split green from not-green with Otsu's method on this map's own colours (allows for
    # lighting and soil colour). On mostly-green sites Otsu splits grass from trees instead,
    # so it's only allowed to nudge the cut-off within a narrow band.
    cut = float(np.clip(_otsu(exg[lit]), 0.02, 0.05)) if lit.sum() > 1000 else 0.04
    bare = ((exg < cut) & lit).astype(np.float64)
    bare[~inside] = np.nan
    return _to_grid(bare, To, ocrs, T, shape, crs, "average")


def _otsu(values, bins=256):
    """Otsu's threshold: the split that best separates the values into two groups."""
    hist, edges = np.histogram(values, bins=bins)
    mids = (edges[:-1] + edges[1:]) / 2
    w1 = np.cumsum(hist)
    w2 = w1[-1] - w1
    m1 = np.cumsum(hist * mids) / np.maximum(w1, 1)
    m2 = (np.sum(hist * mids) - np.cumsum(hist * mids)) / np.maximum(w2, 1)
    return mids[int(np.argmax(w1 * w2 * (m1 - m2) ** 2))]


def detect_change_areas(change, transform, crs, al, log, surface_change=None, ortho=None):
    """Find cut, fill and slips in `change` (survey ground minus LINZ ground; NaN = water/no data).

    `surface_change` is the survey's DSM minus LINZ ground on the same grid (or None): where the
    orthophoto shows bare ground, the DSM *is* the ground - including steep slip scarps that
    WebODM's ground filter tends to cut out of the DTM - so it's used there instead.
    Returns a dict with the cleaned change, class raster, polygons and per-area numbers, all on
    a DETECT_CELL grid in `crs`.
    """
    import scipy.ndimage as ndi
    from affine import Affine
    from rasterio.features import shapes
    from rasterio.fill import fillnodata

    k = max(1, int(round(DETECT_CELL / abs(transform.a))))
    c = _block_reduce(change, k, np.nanmean)
    h, w = c.shape
    T = transform * Affine.scale(k)
    cell = abs(T.a)
    px = cell * abs(T.e)  # m2 per cell

    # what can't be measured: no data, the ragged map edge, water, trees and buildings
    valid = np.isfinite(c)
    edge = valid & ~ndi.binary_erosion(valid, iterations=max(1, int(round(3 / cell))))
    # the outer 10 m of the map: drone models bend most at their edges, so change found
    # mostly there is flagged for checking rather than trusted
    map_edge = valid & ~ndi.binary_erosion(valid, iterations=max(1, int(round(10 / cell))))
    ldsm = _linz_grid(al, "dsm", T, (h, w), crs)
    ldem = _linz_grid(al, "dem", T, (h, w), crs)
    bare = np.nan_to_num(_bare_ground(ortho, T, (h, w), crs)) > 0.5 if ortho else np.zeros((h, w), bool)
    with np.errstate(invalid="ignore"):
        then_high = (ldsm - ldem) > 2.5  # trees or buildings when LINZ flew
        if surface_change is not None:
            cs = _block_reduce(surface_change, k, np.nanmean)
            above = cs - c  # survey DSM - DTM: trees and buildings now
            # bare ground: use the surface model, unless it's an old building still standing
            use_dsm = bare & np.isfinite(cs) & ~((above > 2.5) & then_high)
            c = np.where(use_dsm, cs, c)
            # trees: standing above the DTM now, or there when LINZ flew (WebODM's DTM often
            # keeps the canopy of dense bush, so DSM - DTM alone misses it); bare ground is
            # never vegetation - that's where slips and earthworks show up
            vegetation = ((above > 1.0) | ((ldsm - ldem) > 1.0)) & ~use_dsm
        else:
            vegetation = ((ldsm - ldem) > 1.0) & ~bare
    vegetation = ndi.binary_dilation(vegetation, iterations=max(1, int(round(1 / cell))))
    valid = np.isfinite(c)
    unreliable = ~valid | edge | vegetation

    # remove broad warping: smooth background of the stable ground, on a ~5 m block grid
    B = max(1, int(round(5.0 / cell)))
    coarse = _block_reduce(np.where(unreliable, np.nan, c), B, np.nanmedian)
    win = max(3, int(BACKGROUND_WINDOW / (B * cell)) | 1)
    import warnings
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN neighbourhoods
        bg = ndi.generic_filter(coarse, np.nanmedian, size=win, mode="nearest", cval=np.nan)
    if np.isfinite(bg).any():
        fill_ok = np.isfinite(bg)
        bg = fillnodata(np.where(fill_ok, bg, 0).astype(np.float32), mask=fill_ok.astype(np.uint8),
                        max_search_distance=max(bg.shape)).astype(np.float64)
    else:
        bg = np.zeros_like(coarse)
    bg = _to_grid(bg, T * Affine.scale(B), crs, T, (h, w), crs)
    resid = c - np.nan_to_num(bg)

    stable = ~unreliable
    if stable.sum() < 100:
        log("    change areas: not enough open ground to measure change - skipped")
        return None
    sigma = 1.4826 * float(np.median(np.abs(resid[stable])))
    thr = max(0.5, 3 * sigma)       # a change area must reach this somewhere ...
    low = max(0.25, 1.5 * sigma)    # ... and extends over connected ground that moved this much

    # slope and downhill direction from the LINZ ground model
    g_row, g_col = np.gradient(ldem, cell)
    dzde, dzdn = np.nan_to_num(g_col), np.nan_to_num(-g_row)
    slope = np.degrees(np.arctan(np.hypot(dzde, dzdn)))
    # slope of the ground as it is NOW (survey): earthworks leave flat platforms, pads and
    # driveways; slip scars and debris are steep and lumpy. Smoothed over ~2 m.
    now = ndi.uniform_filter(np.nan_to_num(ldem + c, nan=0.0), size=max(3, int(round(2 / cell))))
    gr2, gc2 = np.gradient(now, cell)
    slope_now = np.degrees(np.arctan(np.hypot(gr2, gc2)))
    FLAT = 10.0  # degrees

    eight = np.ones((3, 3), dtype=bool)
    found = []
    for sign, kind_ in ((-1, "cut"), (1, "fill")):
        # hysteresis: grow each clearly-changed core out over connected ground that moved
        # at least `low`, so the outline follows the whole area, not just its deepest part
        strong = stable & (sign * resid > thr)
        weak = stable & (sign * resid > low)
        weak = ndi.binary_closing(ndi.binary_opening(weak, eight), eight) & stable
        lab, n = ndi.label(weak, structure=eight)
        cores = np.unique(lab[strong & (lab > 0)])
        if len(cores) == 0:
            continue
        mask = np.isin(lab, cores)
        mask = ndi.binary_fill_holes(mask) & stable
        lab, n = ndi.label(mask, structure=eight)
        idx = np.arange(1, n + 1)
        count = ndi.sum(np.ones_like(resid), lab, idx)
        vol = ndi.sum(np.nan_to_num(resid), lab, idx) * px
        ext = (ndi.minimum if sign < 0 else ndi.maximum)(np.nan_to_num(resid), lab, idx)
        com = ndi.center_of_mass(mask, lab, idx)
        sl = ndi.mean(slope, lab, idx)
        sn = ndi.median(np.where(np.isfinite(c), slope_now, np.nan), lab, idx)
        de, dn = ndi.mean(dzde, lab, idx), ndi.mean(dzdn, lab, idx)
        bf = ndi.mean(bare.astype(np.float64), lab, idx)
        ef = ndi.mean(map_edge.astype(np.float64), lab, idx)
        for i in range(n):
            area = count[i] * px
            if area < MIN_AREA or abs(vol[i]) < MIN_VOLUME:
                continue
            r, cc = com[i]
            x, y = T * (cc + 0.5, r + 0.5)
            found.append({"label": (kind_, i + 1), "lab": lab, "kind": kind_, "area": area,
                          "volume": float(vol[i]), "extreme": float(ext[i]), "slope": float(sl[i]),
                          "bare": float(bf[i]), "edge": float(ef[i]), "flat": float(sn[i]) < FLAT,
                          "down": -np.array([de[i], dn[i]]), "xy": np.array([x, y])})

    for f in found:
        f["type"], f["part"], f["group"] = ("CUT" if f["kind"] == "cut" else "FILL"), "", None
        if f["edge"] > 0.3:
            f["type"] = "CHECK"  # mostly in the outer 10 m of the map, where models bend most

    # slips: a cut on a slope with raised ground (debris) starting within DEBRIS_GAP metres
    # downhill of its edge. Several scarp lobes can feed one debris area.
    cuts = [f for f in found if f["kind"] == "cut" and f["type"] != "CHECK"]
    fills = [f for f in found if f["kind"] == "fill" and f["type"] != "CHECK"]
    fill_lab = fills[0]["lab"] if fills else None
    gap = max(1, int(round(15.0 / cell)))
    group = 0

    def _within(area, px_):
        """Cells within px_ cells (straight-line) of an area, in a window around it."""
        lab_, num_ = area["lab"], area["label"][1]
        sl_ = ndi.find_objects((lab_ == num_).astype(np.int8))[0]
        win_ = tuple(slice(max(s.start - px_, 0), s.stop + px_) for s in sl_)
        return ndi.distance_transform_edt(lab_[win_] != num_) <= px_, win_
    for cut in sorted(cuts, key=lambda f: f["volume"]):
        d = cut["down"]
        if cut["slope"] < 10 or cut["flat"] or not np.hypot(*d) or fill_lab is None:
            continue
        d = d / np.hypot(*d)
        near, win = _within(cut, gap)
        touched = set(np.unique(fill_lab[win][near]).tolist()) - {0}
        debris = []
        for f in fills:
            v = f["xy"] - cut["xy"]
            dist = np.hypot(*v)
            if (f["label"][1] in touched and not f["flat"] and dist > 0
                    and np.dot(v, d) / dist >= 0.3):
                debris.append(f)
        if debris:
            joined = next((f["group"] for f in debris if f["group"]), None)
            if joined is None:
                group += 1
                joined = group
            cut["type"], cut["group"], cut["part"] = "SLIP", joined, "scarp"
            for f in debris:
                f["type"], f["group"], f["part"] = "SLIP", joined, "debris"
    # further scarp lobes: lowered ground on a slope within the gap of a slip's scarp joins it
    cut_lab = cuts[0]["lab"] if cuts else None
    changed = True
    while changed:
        changed = False
        for scarp in [f for f in cuts if f["type"] == "SLIP"]:
            near, win = _within(scarp, gap)
            touched = set(np.unique(cut_lab[win][near]).tolist()) - {0, scarp["label"][1]}
            for f in cuts:
                if (f["type"] == "CUT" and f["slope"] >= 10 and not f["flat"]
                        and f["label"][1] in touched):
                    f["type"], f["group"], f["part"] = "SLIP", scarp["group"], "scarp"
                    changed = True
    for cut in sorted(cuts, key=lambda f: f["volume"]):
        if cut["type"] != "CUT":
            continue
        if cut["flat"]:
            continue  # cut down to a flat surface: a platform or track - earthworks
        if cut["slope"] >= 15 and cut["bare"] >= 0.3:
            # fresh bare ground lowered on a slope: a slip scar (any debris has gone or is
            # under vegetation)
            group += 1
            cut["type"], cut["part"], cut["group"] = "SLIP", "scarp", group
        elif cut["slope"] >= 25:
            cut["type"], cut["part"] = "SLIP?", "scarp only"

    # final numbering (biggest first), class raster, polygons, notes
    found.sort(key=lambda f: -abs(f["volume"]))
    classes = np.where(unreliable, 9, 0).astype(np.uint8)
    ids = np.zeros((h, w), dtype=np.int32)
    ring_px = max(1, int(round(2 / cell)))
    for n_, f in enumerate(found, 1):
        f["id"] = n_
        lab, num = f.pop("lab"), f["label"][1]
        sl_ = ndi.find_objects((lab == num).astype(np.int8))[0]
        pad = tuple(slice(max(s.start - ring_px - 1, 0), s.stop + ring_px + 1) for s in sl_)
        region = lab[pad] == num
        ids[pad][region] = n_
        ring = ndi.binary_dilation(region, iterations=ring_px) & ~region
        near = (unreliable[pad] & ring).sum() / max(1, ring.sum())
        f["note"] = "next to trees, water or the map edge - check" if near > 0.4 else ""
        if f["edge"] > 0.3:
            f["note"] = "at the edge of the map, where drone models bend most - check"
        if f["type"] == "FILL" and near > 0.4 and not f["flat"]:
            # lumpy raised ground hemmed in by bush is usually canopy WebODM's DTM didn't
            # remove (a flat raised surface - pad, driveway, platform - is real fill)
            f["type"] = "CHECK"
        code = ({"CUT": 1, "FILL": 2, "SLIP?": 5, "CHECK": 6}.get(f["type"])
                or (3 if f["part"] == "scarp" else 4))
        classes[pad][region] = code
    polys = {}
    for geom, v in shapes(ids, mask=ids > 0, transform=T, connectivity=8):
        polys.setdefault(int(v), []).extend(
            _simplify(ring, cell * 0.75) for ring in geom["coordinates"])
    for f in found:
        f["polygons"] = polys.get(f["id"], [])

    n_slip = len({f["group"] for f in found if f["type"] == "SLIP"})
    counts = {t: sum(f["type"] == t for f in found) for t in ("CUT", "FILL", "SLIP?", "CHECK")}
    measured = stable.sum() * px
    log(f"    change areas: noise +/-{sigma:.2f} m, so change over {thr:.2f} m counts; "
        f"{measured:,.0f} m2 of open ground measured ({100 * stable.mean():.0f}% of the map)")
    log(f"    found {n_slip} slip(s), {counts['SLIP?']} possible slip scarp(s), "
        f"{counts['CUT']} cut and {counts['FILL']} fill area(s), plus {counts['CHECK']} raised "
        "area(s) next to bush to check (usually leftover canopy)")
    for f in found[:15]:
        name = f["type"] + (f" {f['group']} {f['part']}" if f["group"] else "")
        log(f"      #{f['id']} {name}: {f['area']:,.0f} m2, {abs(f['volume']):,.0f} m3 "
            f"{'removed' if f['volume'] < 0 else 'added'}, up to {abs(f['extreme']):.1f} m "
            f"{'deep' if f['volume'] < 0 else 'high'}, slope {f['slope']:.0f} deg"
            + (f" ({f['note']})" if f["note"] else ""))
    return {"resid": np.where(unreliable, np.nan, resid), "classes": classes, "transform": T,
            "crs": crs, "areas": found, "threshold": thr, "sigma": sigma, "slips": n_slip,
            "counts": counts}


def write_change_outputs(det, out_root, name, out_crs, ortho_tif, log):
    """DXF outlines + labels, CSV table, GeoJSON, a transparent overlay (GeoTIFF + PNG for CAD)
    and a quick-look preview over the orthophoto."""
    import csv
    import json
    import rasterio
    from pyproj import CRS, Transformer
    T, crs, classes = det["transform"], det["crs"], det["classes"]
    to_out = None
    if out_crs is not None and CRS.from_user_input(out_crs) != CRS.from_user_input(crs):
        to_out = Transformer.from_crs(crs, out_crs, always_xy=True)
    dst_crs = out_crs or crs

    def xy(pts):
        pts = np.asarray(pts, dtype=np.float64)
        if to_out is None:
            return pts
        x, y = to_out.transform(pts[:, 0], pts[:, 1])
        return np.c_[x, y]

    areas = det["areas"]
    for f in areas:
        f["label_name"] = f["type"] + (f" {f['group']} {f['part']}" if f["group"] else "")
        f["xy_out"] = xy(f["xy"][None])[0]

    # DXF for BricsCAD / AutoCAD
    import ezdxf
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 6  # metres
    msp = doc.modelspace()
    for layer, colour in DXF_LAYERS.items():
        doc.layers.add(f"CHANGE_{layer.replace('?', '_POSSIBLE')}", color=colour)
    doc.layers.add("CHANGE_LABELS", color=7)
    for f in areas:
        layer = f"CHANGE_{f['type'].replace('?', '_POSSIBLE')}"
        for ring in f["polygons"]:
            msp.add_lwpolyline([tuple(p) for p in xy(ring)], close=True, dxfattribs={"layer": layer})
        sign = "-" if f["volume"] < 0 else "+"
        text = (f"#{f['id']} {f['label_name']}\\P{f['area']:,.0f} m²  {sign}{abs(f['volume']):,.0f} m³"
                f"\\P{'max depth' if f['volume'] < 0 else 'max height'} {abs(f['extreme']):.1f} m")
        msp.add_mtext(text, dxfattribs={"layer": "CHANGE_LABELS", "char_height": 1.0,
                                        "insert": tuple(f["xy_out"]),
                                        "attachment_point": 5})
    dxf_path = os.path.join(out_root, f"{name} - change areas.dxf")
    doc.saveas(dxf_path)

    # table
    with open(os.path.join(out_root, f"{name} - change areas.csv"), "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["id", "type", "slip", "part", "area_m2", "cut_m3", "fill_m3", "net_m3",
                     "max_depth_or_height_m", "slope_deg", "easting", "northing", "note"])
        for f in areas:
            v = f["volume"]
            wr.writerow([f["id"], f["type"], f["group"] or "", f["part"], round(f["area"], 1),
                         round(max(-v, 0), 1), round(max(v, 0), 1), round(v, 1),
                         round(abs(f["extreme"]), 2), round(f["slope"], 1),
                         round(float(f["xy_out"][0]), 2), round(float(f["xy_out"][1]), 2), f["note"]])

    # GeoJSON for GIS (in the export coordinate system)
    feats = [{"type": "Feature",
              "properties": {"id": f["id"], "type": f["type"], "slip": f["group"], "part": f["part"],
                             "area_m2": round(f["area"], 1), "net_m3": round(f["volume"], 1),
                             "note": f["note"]},
              "geometry": {"type": "Polygon", "coordinates": [xy(r).tolist() for r in f["polygons"]]}}
             for f in areas if f["polygons"]]
    epsg = CRS.from_user_input(dst_crs).to_epsg()
    with open(os.path.join(out_root, f"{name} - change areas.geojson"), "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats,
                   **({"crs": {"type": "name", "properties": {"name": f"EPSG:{epsg}"}}} if epsg else {})},
                  fh)

    # transparent coloured overlay, in the export coordinate system
    rgba_cls = np.zeros((4,) + classes.shape, dtype=np.uint8)
    for code, (_, rgb) in CLASSES.items():
        m = classes == code
        for b in range(3):
            rgba_cls[b][m] = rgb[b]
        rgba_cls[3][m] = 70 if code == 9 else 200
    T2, cls2 = T, classes
    if to_out is not None:
        from rasterio.enums import Resampling
        from rasterio.warp import calculate_default_transform, reproject
        from rasterio.transform import array_bounds
        hh, ww = classes.shape
        T2, w2, h2 = calculate_default_transform(crs, dst_crs, ww, hh,
                                                 *array_bounds(hh, ww, T), resolution=abs(T.a))
        cls2 = np.zeros((h2, w2), dtype=np.uint8)
        reproject(classes, cls2, src_transform=T, src_crs=crs, dst_transform=T2, dst_crs=dst_crs,
                  src_nodata=0, dst_nodata=0, resampling=Resampling.nearest)
        rgba_cls = np.zeros((4, h2, w2), dtype=np.uint8)
        for code, (_, rgb) in CLASSES.items():
            m = cls2 == code
            for b in range(3):
                rgba_cls[b][m] = rgb[b]
            rgba_cls[3][m] = 70 if code == 9 else 200
    overlay = os.path.join(out_root, f"{name} - change areas.tif")
    with rasterio.open(overlay, "w", driver="GTiff", width=rgba_cls.shape[2],
                       height=rgba_cls.shape[1], count=4, dtype="uint8", crs=dst_crs, transform=T2,
                       photometric="RGB", alpha="PREMULTIPLIED", compress="deflate") as dst:
        premult = rgba_cls.copy()
        for b in range(3):  # associated alpha: colour scaled by opacity
            premult[b] = (rgba_cls[b].astype(np.uint16) * rgba_cls[3] // 255).astype(np.uint8)
        dst.write(premult)
    _write_png_copy(overlay, overlay[:-4] + ".png")

    # quick-look preview: change areas drawn over the orthophoto
    preview = None
    if ortho_tif and os.path.exists(ortho_tif):
        from rasterio.enums import Resampling
        from rasterio.warp import reproject
        from affine import Affine
        with rasterio.open(ortho_tif) as o:
            s = max(1, int(np.ceil(max(o.width, o.height) / 1800)))
            shp = (max(1, o.height // s), max(1, o.width // s))
            img = o.read((1, 2, 3), out_shape=(3,) + shp, resampling=Resampling.average).astype(np.float64)
            alpha = o.read(4, out_shape=shp) if o.count >= 4 else np.full(shp, 255)
            Tp = o.transform * Affine.scale(o.width / shp[1], o.height / shp[0])
            pcrs = o.crs
        cls_p = np.zeros(shp, dtype=np.uint8)
        reproject(classes, cls_p, src_transform=T, src_crs=crs, dst_transform=Tp, dst_crs=pcrs,
                  src_nodata=0, dst_nodata=0, resampling=Resampling.nearest)
        import scipy.ndimage as ndi
        img[:, alpha == 0] = 255  # white outside the map
        for code, (_, rgb) in CLASSES.items():
            m = cls_p == code
            a = 0.25 if code == 9 else 0.45
            for b in range(3):
                img[b][m] = img[b][m] * (1 - a) + rgb[b] * a
            if code != 9:  # solid outline so small areas stand out
                edge = ndi.binary_dilation(m, iterations=2) & ~ndi.binary_erosion(m, iterations=1)
                for b in range(3):
                    img[b][edge] = rgb[b]
        preview = os.path.join(out_root, f"{name} - change areas preview.png")
        with rasterio.Env(GDAL_PAM_ENABLED="NO"):
            with rasterio.open(preview, "w", driver="PNG", width=shp[1], height=shp[0], count=3,
                               dtype="uint8") as dst:
                dst.write(np.clip(img, 0, 255).astype(np.uint8))
    log(f"    wrote change areas: .dxf (outlines + labels for CAD), .csv (volumes), .geojson, "
        f"coloured overlay (.tif + transparent .png)" + (" and a preview" if preview else ""))
    return preview


def process_outputs(files, in_root, out_root, mode, offset, geoid, check, log,
                    name="map", full=False, out_crs=None, cell=0.25, detect=True):
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
        log(f"Lining up {os.path.basename(ref)} with LINZ:")
        al = linz_align(ref, kind, log, task_crs)
        if al is not None:
            result = {"offset": al.offset, "spread": al.spread, "tilt": al.tilt}
            mode, offset = "align", al
            log(f"Applying to all outputs: heights {al.summary()}.")
        else:  # no 1 m LiDAR: fall back to a single height offset from the 8 m model
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
    result.update(export_maps(files, out_root, name, mode, offset, geoid, task_crs, check, log,
                              out_crs, cell, detect) or {})
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
        grid=None, log=print, full=False, out_crs=None, cell=0.25, detect=True):
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
                                   name=_export_name(input_path), full=full, out_crs=out_crs,
                                   cell=cell, detect=detect)
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


# --------------------------------------------------------------------------
# Auto-update from GitHub Releases
# --------------------------------------------------------------------------

UPDATE_API = "https://api.github.com/repos/nicholasjg503/WebODM-Height-Fix-NZ/releases/latest"
INSTALLER_PREFIX = "WebODM-Height-Fix-NZ-Setup-"


def _version_tuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3])


def _http(url, timeout):
    """urlopen with a User-Agent (GitHub requires one) and certifi's CA bundle when available."""
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": f"WebODMHeightFixNZ/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def check_for_update(timeout=10):
    """The latest GitHub release if it's newer than this copy, else None.

    Returns {"version", "notes_url", "installer": (file name, url), "sums_url"}. Releases without
    an installer and a SHA256SUMS.txt are ignored. WEBODM_HEIGHT_FIX_UPDATE_URL overrides the
    release API URL (for testing).
    """
    import json
    with _http(os.environ.get("WEBODM_HEIGHT_FIX_UPDATE_URL", UPDATE_API), timeout) as r:
        rel = json.load(r)
    if rel.get("draft") or rel.get("prerelease"):
        return None
    version = str(rel.get("tag_name", "")).lstrip("vV")
    if not version or _version_tuple(version) <= _version_tuple(__version__):
        return None
    assets = {a.get("name", ""): a.get("browser_download_url") for a in rel.get("assets", [])}
    installer = next((n for n in assets if n.startswith(INSTALLER_PREFIX) and n.endswith(".exe")), None)
    if installer is None or "SHA256SUMS.txt" not in assets:
        return None
    return {"version": version, "notes_url": rel.get("html_url"),
            "installer": (installer, assets[installer]), "sums_url": assets["SHA256SUMS.txt"]}


def download_update(info, progress=lambda done, total: None):
    """Download the release installer and check it against the release's SHA256SUMS.txt.

    Returns the installer's path. Raises FixError if the checksum is missing or wrong.
    """
    import hashlib
    name, url = info["installer"]
    with _http(info["sums_url"], 30) as r:
        sums = r.read().decode("utf-8", "replace")
    expected = None
    for ln in sums.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            expected = parts[0].lower()
    if not expected:
        raise FixError("The update has no checksum, so it wasn't installed.")
    folder = os.path.join(tempfile.gettempdir(), "WebODMHeightFixNZ-update")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    h = hashlib.sha256()
    with _http(url, 60) as r, open(path + ".part", "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            progress(done, total)
    if h.hexdigest().lower() != expected:
        os.remove(path + ".part")
        raise FixError("The downloaded update didn't match its checksum, so it wasn't installed.")
    os.replace(path + ".part", path)
    return path


def launch_installer(path):
    """Start the installer with a progress window only; it reopens the app when finished."""
    import subprocess
    local = os.path.normcase(os.environ.get("LOCALAPPDATA", ""))
    per_user = bool(local) and os.path.normcase(_app_dir()).startswith(local)
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                      "/RELAUNCH=1", "/CURRENTUSER" if per_user else "/ALLUSERS"], close_fds=True)


def _update_from_command_line():
    """--update: check, download, verify and start the installer without the window."""
    info = check_for_update()
    if info is None:
        print(f"{APP_NAME} {__version__} is up to date.")
        return 0
    print(f"Downloading {APP_NAME} {info['version']} ...")
    launch_installer(download_update(info))
    print("Installing - the app will reopen when it's done.")
    return 0


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
    if "slips" in result:
        found = [f"{result['slips']} slip(s)" if result["slips"] else "",
                 f"{result['CUT']} cut" if result["CUT"] else "",
                 f"{result['FILL']} fill" if result["FILL"] else "",
                 f"{result['SLIP?']} possible slip scarp(s)" if result["SLIP?"] else ""]
        found = ", ".join(x for x in found if x)
        msg += (f"\nChange since the LINZ LiDAR: {found or 'nothing significant found'}"
                + (f" (+{result['CHECK']} to check next to bush)" if result.get("CHECK") else "")
                + (" - see the change areas preview." if result.get("preview") else "."))
    if result.get("tilt", 0) > 1.0:
        msg += (f"\nNote: the model was tilted by about {result['tilt']:.1f} m across the site "
                "(corrected). Ground control points would make it more reliable.")
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
    root.geometry("900x860")
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
    full = tk.BooleanVar(value=False)
    detect = tk.BooleanVar(value=True)
    cell_choices = [(0.1, "0.1 m"), (0.25, "0.25 m (recommended)"), (0.5, "0.5 m"), (1.0, "1 m"),
                    (None, "Same as WebODM")]
    preview = {"path": None}
    from pyproj import CRS
    crs_choices = [(code, f"{name or CRS.from_user_input(code).name}  ({code})")
                   for code, name in EXPORT_CRS] + [(None, "Same as WebODM (UTM)")]
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
    ttk.Label(s2, text="You'll get the orthophoto (GeoTIFF + transparent PNG for CAD), an elevation "
                       "model in NZVD2016, and a map of change since the LINZ LiDAR.",
              style="Hint.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
    cf = ttk.Frame(s2)
    cf.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
    ttk.Label(cf, text="Coordinate system:").pack(side="left")
    crs_combo = ttk.Combobox(cf, values=[label for _, label in crs_choices], state="readonly",
                             width=58)
    crs_combo.current(next(i for i, (code, _) in enumerate(crs_choices) if code == DEFAULT_CRS))
    crs_combo.pack(side="left", padx=(6, 0))
    ttk.Label(cf, text="Elevation cell size:").pack(side="left", padx=(16, 0))
    cell_combo = ttk.Combobox(cf, values=[label for _, label in cell_choices], state="readonly",
                              width=16)
    cell_combo.current(1)
    cell_combo.pack(side="left", padx=(6, 0))
    ttk.Checkbutton(s2, text="Find cut, fill and slips since the LINZ LiDAR (outlines + volumes "
                             "as DXF/CSV)", variable=detect).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))
    ttk.Checkbutton(s2, text="Also export the point cloud and 3D model (.laz, .obj)",
                    variable=full).grid(row=6, column=0, columnspan=2, sticky="w")

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
    b_prev = ttk.Button(res, text="Open change preview",
                        command=lambda: preview["path"] and os.startfile(preview["path"]))
    b_prev.grid(row=1, column=1, sticky="ne", padx=(8, 0), pady=(4, 0))
    b_prev.grid_remove()

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
        b_prev.grid_remove()
        progress.grid()
        progress.start(12)
        status.set("Checking... (this usually takes under a minute)" if check else
                   "Fixing heights... (this usually takes a minute or two)")
        status_lbl.configure(style="Status.TLabel")
        logbox.delete("1.0", "end")
        out_crs = crs_choices[crs_combo.current()][0]
        cell = cell_choices[cell_combo.current()][0]
        find = detect.get()

        def work():
            try:
                result = run(src, dst, mode.get(), off, tko, check,
                             log=lambda m: q.put(("log", m)), full=full.get(),
                             out_crs=out_crs, cell=cell, detect=find)
                q.put(("done", (check, result or {}, dst)))
            except Exception as e:
                q.put(("err", str(e) or e.__class__.__name__))
        threading.Thread(target=work, daemon=True).start()

    def finish():
        progress.stop()
        progress.grid_remove()
        for b in buttons:
            b.state(["!disabled"])

    # update bar: shown above everything when GitHub has a newer release
    style.configure("Update.TFrame", background="#e3effb")
    style.configure("Update.TLabel", background="#e3effb", font=("Segoe UI", 10))
    bar = ttk.Frame(root, style="Update.TFrame", padding=(18, 8))
    bar_text = tk.StringVar()
    ttk.Label(bar, textvariable=bar_text, style="Update.TLabel").pack(side="left")
    b_later = ttk.Button(bar, text="Not now", command=bar.pack_forget)
    b_notes = ttk.Button(bar, text="What's new")
    b_update = ttk.Button(bar, text="Update now", command=lambda: start_update())
    for b in (b_later, b_notes, b_update):
        b.pack(side="right", padx=(6, 0))
    update = {}

    def check_updates():
        try:
            info = check_for_update()
        except Exception:
            return  # offline or GitHub unreachable - it'll check again next time
        if info:
            q.put(("upd-found", info))

    def start_update():
        if any(b.instate(["disabled"]) for b in buttons):
            messagebox.showinfo(APP_NAME, "Wait for the current job to finish, then click "
                                          "\"Update now\".")
            return
        for b in [b_update, b_later, b_notes] + buttons:
            b.state(["disabled"])
        bar_text.set("Downloading the update...")

        def work():
            try:
                path = download_update(update["info"],
                                       lambda d, t: q.put(("upd-progress", (d, t))))
                q.put(("upd-ready", path))
            except Exception as e:
                q.put(("upd-err", str(e) or e.__class__.__name__))
        threading.Thread(target=work, daemon=True).start()

    def on_update(kind, msg):
        if kind == "upd-found":
            update["info"] = msg
            bar_text.set(f"A new version ({msg['version']}) is available - you have {__version__}.")
            b_notes.configure(command=lambda: webbrowser.open(msg["notes_url"] or HELP_URL))
            bar.pack(before=frm, fill="x")
        elif kind == "upd-progress":
            done, total = msg
            if total:
                bar_text.set(f"Downloading the update... {100 * done // total}%")
        elif kind == "upd-ready":
            bar_text.set("Installing the update - the app will close and reopen in a moment.")
            try:
                launch_installer(msg)
            except Exception as e:
                on_update("upd-err", str(e))
                return
            root.after(1000, root.destroy)
        elif kind == "upd-err":
            bar_text.set(f"The update didn't work: {msg}")
            for b in [b_update, b_later, b_notes] + buttons:
                b.state(["!disabled"])

    # only the installed app updates itself (or a test run pointed at a test server)
    if getattr(sys, "frozen", False) or os.environ.get("WEBODM_HEIGHT_FIX_UPDATE_URL"):
        root.after(1500, lambda: threading.Thread(target=check_updates, daemon=True).start())

    def poll():
        try:
            while True:
                kind, msg = q.get_nowait()
                if kind.startswith("upd-"):
                    on_update(kind, msg)
                elif kind == "log":
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
                    preview["path"] = result.get("preview")
                    if preview["path"] and os.path.exists(preview["path"]):
                        b_prev.grid()
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
    if sys.argv[1] == "--update":
        try:
            sys.exit(_update_from_command_line())
        except Exception as e:
            sys.exit(f"Update failed: {e}")
    ap = argparse.ArgumentParser(description="Convert drone photos or WebODM outputs to NZVD2016 heights.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("input", help="photo folder, WebODM output folder, or WebODM all.zip")
    ap.add_argument("output", nargs="?", help="output folder for corrected copies")
    ap.add_argument("--mode", choices=["auto", "geoid", "offset", "takeoff"], default="auto")
    ap.add_argument("--offset", type=float, default=0.0, help="metres to add (offset mode)")
    ap.add_argument("--takeoff-elev", type=float, help="takeoff ground height, m above sea level (takeoff mode)")
    ap.add_argument("--check", action="store_true", help="only report heights, write nothing")
    ap.add_argument("--crs", default=DEFAULT_CRS,
                    help="coordinate system for the exported GeoTIFFs/PNG, e.g. EPSG:2193 for "
                         "NZTM 2000, or 'webodm' to keep WebODM's UTM (default EPSG:2105, "
                         "Mount Eden 2000)")
    ap.add_argument("--cell", default="0.25",
                    help="elevation model cell size in metres, or 'webodm' to keep WebODM's "
                         "(default 0.25)")
    ap.add_argument("--no-detect", action="store_true",
                    help="don't look for cut, fill and slips since the LINZ LiDAR")
    ap.add_argument("--full", action="store_true",
                    help="also correct the point cloud, 3D model and WebODM's own DEMs")
    ap.add_argument("--grid", help=f"path to {GEOID_FILENAME} (downloaded automatically if omitted)")
    a = ap.parse_args()
    try:
        run(a.input, a.output, a.mode, a.offset, a.takeoff_elev, a.check, a.grid,
            full=a.full, out_crs=None if a.crs.lower() == "webodm" else a.crs,
            cell=None if a.cell.lower() == "webodm" else float(a.cell), detect=not a.no_detect)
    except FixError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
