"""Spatial tiling for very large drone photo sets (10k-100k images).

Pure logic, no RealityScan dependency, so it can be unit-tested anywhere:
  * read EXIF GPS (or a sidecar CSV) for every photo and cache it as gps_index.csv
  * project WGS84 lat/lon to UTM metres (Karney/Krueger series, mm accurate)
  * choose a square grid so that every cell - including its overlap buffer -
    holds at most `max_photos` images
  * write a RealityScan reconstruction-region file (.rcbox) per tile covering
    only the *core* cell, so neighbouring tiles butt together without overlap
  * write a parent Cesium 3D Tiles tileset.json that references every
    finished tile's own tileset.json (external tilesets, no mesh merging)
"""
from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng"}

# ------------------------------------------------------------------ WGS84 <-> UTM
_A = 6378137.0
_F = 1 / 298.257223563
_N = _F / (2 - _F)
_AA = _A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64)
_ALPHA = (_N / 2 - 2 * _N ** 2 / 3 + 5 * _N ** 3 / 16,
          13 * _N ** 2 / 48 - 3 * _N ** 3 / 5,
          61 * _N ** 3 / 240)
_BETA = (_N / 2 - 2 * _N ** 2 / 3 + 37 * _N ** 3 / 96,
         _N ** 2 / 48 + _N ** 3 / 15,
         17 * _N ** 3 / 480)
_DELTA = (2 * _N - 2 * _N ** 2 / 3 - 2 * _N ** 3,
          7 * _N ** 2 / 3 - 8 * _N ** 3 / 5,
          56 * _N ** 3 / 15)
_K0 = 0.9996
_E0 = 500000.0


def utm_zone(lat: float, lon: float) -> tuple[int, bool]:
    """UTM zone number and hemisphere flag (True = north) for a point."""
    zone = int((lon + 180) // 6) + 1
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    if 72 <= lat < 84:
        if 0 <= lon < 9:
            zone = 31
        elif 9 <= lon < 21:
            zone = 33
        elif 21 <= lon < 33:
            zone = 35
        elif 33 <= lon < 42:
            zone = 37
    return max(1, min(60, zone)), lat >= 0


def utm_epsg(zone: int, north: bool) -> int:
    return (32600 if north else 32700) + zone


def utm_proj(zone: int, north: bool) -> str:
    return f"+proj=utm +zone={zone}{'' if north else ' +south'} +datum=WGS84 +units=m +no_defs"


def latlon_to_utm(lat: float, lon: float, zone: int, north: bool) -> tuple[float, float]:
    phi, lam = math.radians(lat), math.radians(lon)
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)
    k = 2 * math.sqrt(_N) / (1 + _N)
    t = math.sinh(math.atanh(math.sin(phi)) - k * math.atanh(k * math.sin(phi)))
    xi = math.atan2(t, math.cos(lam - lam0))
    eta = math.atanh(math.sin(lam - lam0) / math.sqrt(1 + t * t))
    e = eta + sum(a * math.cos(2 * j * xi) * math.sinh(2 * j * eta) for j, a in enumerate(_ALPHA, 1))
    n = xi + sum(a * math.sin(2 * j * xi) * math.cosh(2 * j * eta) for j, a in enumerate(_ALPHA, 1))
    return _E0 + _K0 * _AA * e, (0.0 if north else 10_000_000.0) + _K0 * _AA * n


def utm_to_latlon(e: float, n: float, zone: int, north: bool) -> tuple[float, float]:
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)
    xi = (n - (0.0 if north else 10_000_000.0)) / (_K0 * _AA)
    eta = (e - _E0) / (_K0 * _AA)
    xi_p = xi - sum(b * math.sin(2 * j * xi) * math.cosh(2 * j * eta) for j, b in enumerate(_BETA, 1))
    eta_p = eta - sum(b * math.cos(2 * j * xi) * math.sinh(2 * j * eta) for j, b in enumerate(_BETA, 1))
    chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    phi = chi + sum(d * math.sin(2 * j * chi) for j, d in enumerate(_DELTA, 1))
    lam = lam0 + math.atan2(math.sinh(eta_p), math.cos(xi_p))
    return math.degrees(phi), math.degrees(lam)


# ------------------------------------------------------------------ GPS index
@dataclass
class Photo:
    path: str
    lat: float
    lon: float
    alt: float


def _dms(v) -> float:
    d, m, s = (float(x) for x in v)
    return d + m / 60 + s / 3600


def exif_gps(path: Path) -> Optional[tuple[float, float, float]]:
    """(lat, lon, alt) from EXIF, or None when the photo has no GPS block."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            g = im.getexif().get_ifd(0x8825)
    except Exception:
        return None
    if not g or 2 not in g or 4 not in g:
        return None
    try:
        lat = _dms(g[2]) * (-1 if str(g.get(1, "N")).upper().startswith("S") else 1)
        lon = _dms(g[4]) * (-1 if str(g.get(3, "E")).upper().startswith("W") else 1)
        alt = float(g.get(6, 0.0)) if g.get(6) is not None else 0.0
        ref = g.get(5, 0)
        if (isinstance(ref, (bytes, bytearray)) and ref[:1] == b"\x01") or ref == 1:
            alt = -alt
    except Exception:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon, alt


def list_images(folder: Path) -> list[Path]:
    return sorted(f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_EXT)


def read_sidecar(csv_path: Path, folder: Path) -> dict[str, tuple[float, float, float]]:
    """CSV with columns name/filename/path, lat, lon[, alt]. Keys are lower-case basenames."""
    out: dict[str, tuple[float, float, float]] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (rd.fieldnames or [])}
        name_c = next((cols[c] for c in ("name", "filename", "file", "path", "image") if c in cols), None)
        lat_c = next((cols[c] for c in ("lat", "latitude") if c in cols), None)
        lon_c = next((cols[c] for c in ("lon", "lng", "longitude") if c in cols), None)
        alt_c = next((cols[c] for c in ("alt", "altitude", "height", "z") if c in cols), None)
        if not (name_c and lat_c and lon_c):
            raise RuntimeError("sidecar CSV needs columns name|filename|path, lat|latitude, lon|longitude")
        for row in rd:
            try:
                out[Path(row[name_c]).name.lower()] = (
                    float(row[lat_c]), float(row[lon_c]), float(row[alt_c]) if alt_c and row.get(alt_c) else 0.0)
            except (TypeError, ValueError):
                continue
    return out


def build_gps_index(folder: Path, cache_csv: Optional[Path] = None,
                    sidecar_csv: Optional[Path] = None, force: bool = False,
                    progress=None) -> tuple[list[Photo], list[str]]:
    """Return (photos_with_gps, paths_without_gps). Results are cached in cache_csv."""
    files = list_images(folder)
    if cache_csv and cache_csv.exists() and not force:
        cached: dict[str, Photo] = {}
        with open(cache_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cached[row["path"]] = Photo(row["path"], float(row["lat"]), float(row["lon"]), float(row["alt"]))
        if set(cached) and set(cached).issubset({str(f) for f in files}) and len(cached) >= 0.5 * len(files):
            missing = [str(f) for f in files if str(f) not in cached]
            # photos missing from the cache are those that had no GPS when indexed
            return list(cached.values()), missing
    side = read_sidecar(sidecar_csv, folder) if sidecar_csv else {}
    photos: list[Photo] = []
    no_gps: list[str] = []
    t0 = time.time()
    for i, f in enumerate(files):
        g = side.get(f.name.lower()) or exif_gps(f)
        if g:
            photos.append(Photo(str(f), *g))
        else:
            no_gps.append(str(f))
        if progress and i % 500 == 0:
            progress(i, len(files), time.time() - t0)
    if cache_csv:
        cache_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["path", "lat", "lon", "alt"])
            for p in photos:
                w.writerow([p.path, f"{p.lat:.8f}", f"{p.lon:.8f}", f"{p.alt:.2f}"])
    return photos, no_gps


# ------------------------------------------------------------------ grid planning
@dataclass
class Tile:
    id: str
    row: int
    col: int
    core: list[float]          # [e_min, e_max, n_min, n_max] metres (UTM)
    buffered: list[float]      # same, expanded by the overlap buffer
    lonlat: list[float]        # [west, south, east, north] degrees of the buffered box
    z: list[float]             # [z_min, z_max] metres for the region box
    core_count: int
    buffered_count: int
    status: str = "pending"    # pending | running | done | failed | skipped
    job_id: Optional[str] = None
    attempts: int = 0
    note: str = ""
    started: Optional[float] = None
    ended: Optional[float] = None


def _cell_counts(es, ns, e0, n0, cell, nx, ny, buf):
    core = [0] * (nx * ny)
    buffered = [0] * (nx * ny)
    for e, n in zip(es, ns):
        ci = min(nx - 1, max(0, int((e - e0) // cell)))
        ri = min(ny - 1, max(0, int((n - n0) // cell)))
        core[ri * nx + ci] += 1
        c_lo = max(0, int((e - e0 - buf) // cell))
        c_hi = min(nx - 1, int((e - e0 + buf) // cell))
        r_lo = max(0, int((n - n0 - buf) // cell))
        r_hi = min(ny - 1, int((n - n0 + buf) // cell))
        for r in range(r_lo, r_hi + 1):
            for c in range(c_lo, c_hi + 1):
                buffered[r * nx + c] += 1
    return core, buffered


def plan_grid(photos: list[Photo], max_photos: int = 500, overlap: float = 0.2,
              min_photos: int = 20, region_depth_m: float = 250.0,
              min_cell_m: float = 20.0) -> dict:
    """Pick the largest square cell such that no cell's buffered photo count exceeds max_photos."""
    if not photos:
        raise RuntimeError("no georeferenced photos to plan with")
    if max_photos < 10:
        raise RuntimeError("max_photos must be >= 10")
    lat_c = sum(p.lat for p in photos) / len(photos)
    lon_c = sum(p.lon for p in photos) / len(photos)
    zone, north = utm_zone(lat_c, lon_c)
    en = [latlon_to_utm(p.lat, p.lon, zone, north) for p in photos]
    es = [x[0] for x in en]
    ns = [x[1] for x in en]
    pad = 1.0
    e0, e1 = min(es) - pad, max(es) + pad
    n0, n1 = min(ns) - pad, max(ns) + pad
    W, H = e1 - e0, n1 - n0
    alts = sorted(p.alt for p in photos)
    z_top = alts[int(0.98 * (len(alts) - 1))] + 30.0
    z_bottom = alts[int(0.02 * (len(alts) - 1))] - region_depth_m

    def grid_for(cell):
        nx = max(1, math.ceil(W / cell))
        ny = max(1, math.ceil(H / cell))
        buf = overlap * cell
        core, buffered = _cell_counts(es, ns, e0, n0, cell, nx, ny, buf)
        return nx, ny, buf, core, buffered

    hi = max(W, H, min_cell_m)
    nx, ny, buf, core, buffered = grid_for(hi)
    if max(buffered) > max_photos:
        lo = min_cell_m
        _, _, _, _, b_lo = grid_for(lo)
        if max(b_lo) > max_photos:
            raise RuntimeError(
                f"even {min_cell_m:.0f} m cells hold {max(b_lo)} photos > max_photos={max_photos}; "
                "photos are too dense or lack real GPS variation - raise max_photos or check GPS.")
        for _ in range(40):                      # bisect on cell size (metres)
            mid = (lo + hi) / 2
            if max(grid_for(mid)[4]) <= max_photos:
                lo = mid
            else:
                hi = mid
            if hi - lo < 0.5:
                break
        cell = math.floor(lo)
        nx, ny, buf, core, buffered = grid_for(cell)
    else:
        cell = math.ceil(hi)
        nx, ny, buf, core, buffered = grid_for(cell)

    tiles: list[Tile] = []
    for r in range(ny):
        for c in range(nx):
            k = r * nx + c
            if buffered[k] == 0:
                continue
            ce0, ce1 = e0 + c * cell, e0 + (c + 1) * cell
            cn0, cn1 = n0 + r * cell, n0 + (r + 1) * cell
            be0, be1, bn0, bn1 = ce0 - buf, ce1 + buf, cn0 - buf, cn1 + buf
            corners = [utm_to_latlon(x, y, zone, north) for x in (be0, be1) for y in (bn0, bn1)]
            lonlat = [min(p[1] for p in corners), min(p[0] for p in corners),
                      max(p[1] for p in corners), max(p[0] for p in corners)]
            t = Tile(id=f"r{r:02d}c{c:02d}", row=r, col=c,
                     core=[ce0, ce1, cn0, cn1], buffered=[be0, be1, bn0, bn1],
                     lonlat=lonlat, z=[z_bottom, z_top],
                     core_count=core[k], buffered_count=buffered[k])
            if buffered[k] < min_photos:
                t.status, t.note = "skipped", f"only {buffered[k]} photos (< min_photos={min_photos})"
            tiles.append(t)
    return {
        "utm": {"zone": zone, "north": north, "epsg": utm_epsg(zone, north), "proj": utm_proj(zone, north)},
        "bounds_utm": [e0, e1, n0, n1],
        "extent_m": [round(W), round(H)],
        "grid": {"nx": nx, "ny": ny, "cell_m": cell, "overlap": overlap, "buffer_m": round(buf, 1)},
        "photo_count": len(photos),
        "max_photos": max_photos,
        "tiles": [asdict(t) for t in tiles],
    }


def photos_for_tile(photos: Iterable[Photo], tile: dict, zone: int, north: bool) -> list[str]:
    e0, e1, n0, n1 = tile["buffered"]
    out = []
    for p in photos:
        e, n = latlon_to_utm(p.lat, p.lon, zone, north)
        if e0 <= e < e1 and n0 <= n < n1:
            out.append(p.path)
    return out


# ------------------------------------------------------------------ RealityScan region file
RCBOX_TEMPLATE = """<ReconstructionRegion globalCoordinateSystem="{proj}" globalCoordinateSystemWkt="" globalCoordinateSystemName="epsg:{epsg} - WGS 84 / UTM zone {zone}{hemi}" isGeoreferenced="1" isLatLon="0">
  <yawPitchRoll>0 0 0</yawPitchRoll>
  <widthHeightDepth>{w:.3f} {h:.3f} {d:.3f}</widthHeightDepth>
  <Header magic="5395016" version="2"/>
  <CentreEuclid>
    <centre>{cx:.3f} {cy:.3f} {cz:.3f}</centre>
  </CentreEuclid>
  <Residual>
    <R>1 0 0 0 1 0 0 0 1</R>
    <t>0 0 0</t>
    <s>1</s>
    <ownerId>{{00000000-0000-0000-0000-000000000000}}</ownerId>
  </Residual>
</ReconstructionRegion>
"""


def write_rcbox(path: Path, tile: dict, utm: dict) -> Path:
    """Axis-aligned box over the tile's *core* cell in the plan's UTM CRS.

    NOTE: validate once against a file produced by `-exportReconstructionRegion`
    on the installed RealityScan build; the runner exports such a reference file
    (<tile>_auto.rcbox) next to this one for comparison.
    """
    e0, e1, n0, n1 = tile["core"]
    z0, z1 = tile["z"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RCBOX_TEMPLATE.format(
        proj=utm["proj"], epsg=utm["epsg"], zone=utm["zone"], hemi="N" if utm["north"] else "S",
        w=e1 - e0, h=n1 - n0, d=z1 - z0,
        cx=(e0 + e1) / 2, cy=(n0 + n1) / 2, cz=(z0 + z1) / 2), encoding="utf-8")
    return path


# ------------------------------------------------------------------ parent tileset
def write_parent_tileset(path: Path, tiles: list[dict], tiles_dir: Path,
                         geometric_error: float = 400.0) -> dict:
    """tileset.json whose children are the finished tiles' own tileset.json files."""
    children = []
    for t in tiles:
        child = tiles_dir / t["id"] / "tileset.json"
        if t.get("status") != "done" or not child.exists():
            continue
        w, s, e, n = t["lonlat"]
        z0, z1 = t["z"]
        children.append({
            "boundingVolume": {"region": [math.radians(w), math.radians(s), math.radians(e), math.radians(n),
                                          z0 - 50.0, z1 + 50.0]},
            "geometricError": geometric_error / 4,
            "refine": "REPLACE",
            "content": {"uri": f"{t['id']}/tileset.json"},
        })
    if not children:
        raise RuntimeError("no finished tiles with a tileset.json yet")
    regs = [c["boundingVolume"]["region"] for c in children]
    root_region = [min(r[0] for r in regs), min(r[1] for r in regs), max(r[2] for r in regs),
                   max(r[3] for r in regs), min(r[4] for r in regs), max(r[5] for r in regs)]
    ts = {
        "asset": {"version": "1.0", "generator": "realityscan-mcp rs_tiled_pipeline"},
        "geometricError": geometric_error,
        "root": {"boundingVolume": {"region": root_region}, "geometricError": geometric_error,
                 "refine": "ADD", "children": children},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ts, indent=2), encoding="utf-8")
    return {"tileset": str(path), "children": len(children)}
