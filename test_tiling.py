"""Tiling tests: synthetic GPS-tagged photos -> grid plan -> rcbox/tileset -> MCP dry run
-> real run against a stub RealityScan.exe that fakes the 3D Tiles export."""
import asyncio
import json
import math
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import tiling  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="rstile_"))
PHOTOS = TMP / "photos"
OUT = TMP / "out"
STUB = TMP / "RealityScan.cmd"
# Stub: echo args, then create whatever -export3dTiles / -exportReconstructionRegion asked for.
STUB.write_text(
    "@echo off\r\necho STUB args: %*\r\n"
    ":loop\r\nif \"%~1\"==\"\" goto end\r\n"
    "if /I \"%~1\"==\"-export3dTiles\" ( mkdir \"%~dp2\" 2>nul & echo {\"asset\":{\"version\":\"1.0\"}} > \"%~2\" )\r\n"
    "if /I \"%~1\"==\"-exportReconstructionRegion\" ( mkdir \"%~dp2\" 2>nul & echo auto > \"%~2\" )\r\n"
    "shift\r\ngoto loop\r\n:end\r\nexit /b 0\r\n")


def _rat(x: float):
    d = int(x)
    m = int((x - d) * 60)
    s = round(((x - d) * 60 - m) * 60, 4)
    return (float(d), float(m), float(s))


def make_photos(n_rows: int, n_cols: int, step_m: float, lat0=37.5665, lon0=126.9780) -> int:
    """Serpentine flight: n_rows strips x n_cols shots, step_m apart, as 8x8 JPEGs with EXIF GPS."""
    PHOTOS.mkdir(parents=True, exist_ok=True)
    zone, north = tiling.utm_zone(lat0, lon0)
    e0, n0 = tiling.latlon_to_utm(lat0, lon0, zone, north)
    k = 0
    for r in range(n_rows):
        cols = range(n_cols) if r % 2 == 0 else range(n_cols - 1, -1, -1)
        for c in cols:
            lat, lon = tiling.utm_to_latlon(e0 + c * step_m, n0 + r * step_m, zone, north)
            exif = Image.Exif()
            exif[0x8825] = {1: "N", 2: _rat(lat), 3: "E", 4: _rat(lon), 5: b"\x00", 6: 120.0 + (k % 7)}
            sub = PHOTOS / f"flight{r // 10:02d}"
            sub.mkdir(exist_ok=True)
            Image.new("RGB", (8, 8), (k % 255, 0, 0)).save(sub / f"DJI_{k:05d}.JPG", exif=exif)
            k += 1
    # two photos without GPS
    Image.new("RGB", (8, 8)).save(PHOTOS / "nogps_a.jpg")
    Image.new("RGB", (8, 8)).save(PHOTOS / "nogps_b.jpg")
    return k


def test_utm_roundtrip():
    for lat, lon in [(37.5665, 126.978), (-33.86, 151.21), (64.1, -21.9), (0.5, 0.5)]:
        z, nth = tiling.utm_zone(lat, lon)
        e, n = tiling.latlon_to_utm(lat, lon, z, nth)
        lat2, lon2 = tiling.utm_to_latlon(e, n, z, nth)
        assert abs(lat - lat2) < 1e-8 and abs(lon - lon2) < 1e-8, (lat, lon, lat2, lon2)
    # Seoul city hall is in zone 52N; easting/northing sanity (metres)
    e, n = tiling.latlon_to_utm(37.5665, 126.978, 52, True)
    assert 320_000 < e < 330_000 and 4_158_000 < n < 4_162_000, (e, n)
    print("utm roundtrip OK")


def test_plan():
    total = make_photos(n_rows=30, n_cols=40, step_m=15)   # 1200 photos over ~600 x 450 m
    photos, no_gps = tiling.build_gps_index(PHOTOS, OUT / "gps_index.csv")
    assert len(photos) == total and len(no_gps) == 2, (len(photos), no_gps)
    photos2, no_gps2 = tiling.build_gps_index(PHOTOS, OUT / "gps_index.csv")   # cached path
    assert len(photos2) == total and len(no_gps2) == 2
    # EXIF DMS rounding: positions must survive within a few cm
    zone, north = tiling.utm_zone(photos[0].lat, photos[0].lon)
    p0 = tiling.latlon_to_utm(photos[0].lat, photos[0].lon, zone, north)
    p1 = tiling.latlon_to_utm(photos[1].lat, photos[1].lon, zone, north)
    assert abs(math.dist(p0, p1) - 15) < 0.1, math.dist(p0, p1)

    plan = tiling.plan_grid(photos, max_photos=200, overlap=0.2, min_photos=20)
    tiles = plan["tiles"]
    active = [t for t in tiles if t["status"] != "skipped"]
    assert plan["utm"]["epsg"] == 32652
    assert max(t["buffered_count"] for t in tiles) <= 200
    assert sum(t["core_count"] for t in tiles) == total, "every photo belongs to exactly one core cell"
    assert len(active) >= 6, len(active)
    # a photo inside a core cell must be in that tile's buffered photo list
    for t in active[:3]:
        names = tiling.photos_for_tile(photos, t, zone, north)
        assert len(names) == t["buffered_count"], (len(names), t["buffered_count"])
        assert t["buffered_count"] >= t["core_count"]
    # core cells never overlap, buffered boxes do
    a, b = active[0], active[1]
    assert a["core"][1] <= b["core"][0] or b["core"][1] <= a["core"][0] or a["core"][3] <= b["core"][2] or b["core"][3] <= a["core"][2]
    assert plan["grid"]["buffer_m"] == round(0.2 * plan["grid"]["cell_m"], 1)

    # rcbox XML parses and has the right size
    box = tiling.write_rcbox(OUT / "regions" / f"{a['id']}.rcbox", a, plan["utm"])
    root = ET.parse(box).getroot()
    assert root.tag == "ReconstructionRegion" and root.get("isGeoreferenced") == "1"
    w, h, d = (float(x) for x in root.find("widthHeightDepth").text.split())
    assert abs(w - plan["grid"]["cell_m"]) < 1e-3 and abs(h - plan["grid"]["cell_m"]) < 1e-3 and d > 250
    cx, cy, cz = (float(x) for x in root.find("CentreEuclid/centre").text.split())
    assert a["core"][0] < cx < a["core"][1] and a["core"][2] < cy < a["core"][3]
    assert "+proj=utm +zone=52" in root.get("globalCoordinateSystem")

    # parent tileset: only tiles marked done with an existing child tileset are referenced
    for t in active[:2]:
        t["status"] = "done"
        child = OUT / "tiles" / t["id"] / "tileset.json"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("{}")
    res = tiling.write_parent_tileset(OUT / "tiles" / "tileset.json", tiles, OUT / "tiles")
    ts = json.loads((OUT / "tiles" / "tileset.json").read_text())
    assert res["children"] == 2 and len(ts["root"]["children"]) == 2
    reg = ts["root"]["children"][0]["boundingVolume"]["region"]
    assert -math.pi <= reg[0] < reg[2] <= math.pi and -math.pi / 2 <= reg[1] < reg[3] <= math.pi / 2
    assert ts["root"]["children"][0]["content"]["uri"].endswith("/tileset.json")

    # single-tile case: few photos -> one tile, no crash
    small = tiling.plan_grid(photos[:50], max_photos=500)
    assert len(small["tiles"]) == 1 and small["grid"]["nx"] == 1 and small["grid"]["ny"] == 1
    # impossible case: everything at one point
    try:
        tiling.plan_grid([tiling.Photo(p.path, 37.0, 127.0, 100.0) for p in photos[:300]], max_photos=100)
        raise AssertionError("expected RuntimeError for zero-extent set")
    except RuntimeError as e:
        assert "dense" in str(e)
    print(f"plan OK: {len(active)} tiles, cell {plan['grid']['cell_m']} m, "
          f"max {max(t['buffered_count'] for t in tiles)} photos/tile")
    return total


async def test_server(total: int):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ, REALITYSCAN_EXE=str(STUB), RS_JOBS_DIR=str(TMP / "jobs"))
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=env)
    run_dir = OUT / "run"
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            assert {"rs_tiled_pipeline", "rs_tiled_status", "rs_tiled_abort", "rs_tiled_merge"} <= names

            async def call(tool, **kw):
                res = await s.call_tool(tool, kw)
                if res.isError:
                    raise RuntimeError(res.content[0].text)
                return json.loads(res.content[0].text)

            base = dict(images_folder=str(PHOTOS), output_folder=str(OUT), name="run",
                        max_photos_per_tile=200, detail="preview")
            dry = await call("rs_tiled_pipeline", **base, dry_run=True)
            assert dry["dry_run"] and dry["photo_count"] == total and dry["photos_without_gps"] == 2
            assert "-setReconstructionRegion" in dry["example_tile_cmdline"]
            assert "-export3dTiles" in dry["example_tile_cmdline"]
            assert (run_dir / "plan.json").exists() and (run_dir / "gps_index.csv").exists()
            n_tiles = dry["tile_count"]
            print("DRY:", dry["grid"], "tiles", n_tiles, "est h", dry["remaining_est_hours"])
            print("CMD:", dry["example_tile_cmdline"][:260], "...")

            started = await call("rs_tiled_pipeline", **base, max_tiles=2)
            assert started["started"] and started["resumed_existing_plan"]
            for _ in range(60):
                await asyncio.sleep(1)
                st = await call("rs_tiled_status", run_dir=str(run_dir))
                if not st["runner_alive"]:
                    break
            assert st["runner_error"] is None, st["runner_error"]
            assert st["tiles_by_status"].get("done") == 2, st["tiles_by_status"]
            assert st["state"] == "paused", st["state"]
            done = [t for t in st["tiles"] if t["status"] == "done"]
            tid = done[0]["id"]
            assert (run_dir / "tiles" / tid / "tileset.json").exists()
            assert (run_dir / "regions" / f"{tid}.rcbox").exists()
            assert (run_dir / "regions" / f"{tid}_auto.rcbox").exists()
            linked = list((run_dir / "images" / tid).glob("*.JPG"))
            assert len(linked) == done[0]["buffered_count"], (len(linked), done[0]["buffered_count"])
            job = await call("rs_job_status", job_id=done[0]["job_id"])
            assert job["state"] == "done" and "-setReconstructionRegion" in job["log_tail"]
            merged = await call("rs_tiled_merge", run_dir=str(run_dir))
            assert merged["children"] == 2
            parent = json.loads((run_dir / "tiles" / "tileset.json").read_text())
            assert len(parent["root"]["children"]) == 2
            print("SERVER OK: 2 tiles done via stub, parent tileset written, state", st["state"])

            # abort path: start the remaining tiles and stop immediately
            again = await call("rs_tiled_pipeline", **base)
            assert again["started"]
            ab = await call("rs_tiled_abort", run_dir=str(run_dir))
            assert ab["stop_requested"]
            for _ in range(30):
                await asyncio.sleep(1)
                st = await call("rs_tiled_status", run_dir=str(run_dir))
                if not st["runner_alive"]:
                    break
            assert st["state"] in ("aborted", "paused", "finished"), st["state"]
            assert st["tiles_by_status"].get("done", 0) >= 2
            assert not st["tiles_by_status"].get("running")
            print("ABORT OK: state", st["state"], st["tiles_by_status"])


if __name__ == "__main__":
    test_utm_roundtrip()
    n = test_plan()
    asyncio.run(test_server(n))
    print("ALL OK", TMP)
