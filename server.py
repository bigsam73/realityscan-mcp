"""RealityScan MCP server.

Controls Epic RealityScan (formerly RealityCapture) through its command-line interface.
Long operations (align / mesh / texture / export) run as background jobs so the MCP
client never blocks; poll them with rs_job_status.

Environment variables (all optional):
  REALITYSCAN_EXE  full path to RealityScan.exe (auto-detected otherwise)
  RS_JOBS_DIR      root folder for job logs and default outputs (default C:\\DroneJobs)
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("realityscan")

JOBS_DIR = Path(os.environ.get("RS_JOBS_DIR", r"C:\DroneJobs"))
JOBS_META = JOBS_DIR / "_mcp_jobs"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETAIL_CMD = {
    "preview": "-calculatePreviewModel",
    "normal": "-calculateNormalModel",
    "high": "-calculateHighModel",
}


# --------------------------------------------------------------------------- helpers
def find_exe() -> Optional[Path]:
    env = os.environ.get("REALITYSCAN_EXE")
    if env and Path(env).is_file():
        return Path(env)
    cands: list[str] = []
    for base in (r"C:\Program Files\Epic Games", r"C:\Program Files\Capturing Reality"):
        cands += glob.glob(base + r"\RealityScan*\RealityScan.exe")
        cands += glob.glob(base + r"\RealityCapture*\RealityCapture.exe")
    return Path(sorted(cands)[-1]) if cands else None


def need_exe() -> Path:
    exe = find_exe()
    if not exe:
        raise RuntimeError(
            "RealityScan.exe not found. Install it from the Epic Games Launcher "
            "(default C:\\Program Files\\Epic Games\\RealityScan\\) or set REALITYSCAN_EXE."
        )
    return exe


def norm_cmds(commands) -> list[str]:
    """Accept a list of tokens, a list of 'cmd arg' strings, or one string; return CLI tokens."""
    if isinstance(commands, str):
        commands = [commands]
    out: list[str] = []
    for c in commands:
        c = str(c).strip()
        if not c:
            continue
        if c.startswith("-") and " " in c:
            head, rest = c.split(" ", 1)
            out.append(head)
            out.append(rest.strip().strip('"'))
        else:
            out.append(c)
    # -quit is appended by the runner; never let it appear mid-sequence
    return [t for t in out if t.lower() != "-quit"]


def tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-n:]) if n > 0 else ""


def detail_command(detail: str) -> str:
    cmd = DETAIL_CMD.get(detail.lower())
    if not cmd:
        raise RuntimeError("detail must be preview, normal or high")
    return cmd


# --------------------------------------------------------------------------- jobs
class Job:
    def __init__(self, name: str, args: list[str]):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.name = name
        self.args = args
        self.dir = JOBS_META / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "realityscan.log"
        self.started = time.time()
        self.ended: Optional[float] = None
        self.exit: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self.cmdline = ""

    def start(self) -> None:
        exe = need_exe()
        full = [str(exe), "-headless", "-stdConsole", "-printProgress",
                "-silent", str(self.dir)] + self.args + ["-quit"]
        self.cmdline = subprocess.list2cmdline(full)
        (self.dir / "command.txt").write_text(self.cmdline, encoding="utf-8")
        self._logf = open(self.log, "ab")
        self.proc = subprocess.Popen(full, stdout=self._logf, stderr=subprocess.STDOUT,
                                     cwd=str(self.dir), creationflags=NO_WINDOW)
        self.save()
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self) -> None:
        assert self.proc
        self.exit = self.proc.wait()
        self.ended = time.time()
        self._logf.close()
        self.save()

    def state(self) -> str:
        if self.proc is not None and self.proc.poll() is None:
            return "running"
        if self.exit is None:
            return "unknown"
        return "done" if self.exit == 0 else "failed"

    def abort(self) -> bool:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            return True
        return False

    def info(self, tail_lines: int = 25) -> dict:
        end = self.ended or time.time()
        return {
            "job_id": self.id,
            "name": self.name,
            "state": self.state(),
            "exit_code": self.exit,
            "elapsed_min": round((end - self.started) / 60, 1),
            "job_dir": str(self.dir),
            "log": str(self.log),
            "log_tail": tail(self.log, tail_lines),
        }

    def save(self) -> None:
        meta = {k: v for k, v in self.info(0).items() if k != "log_tail"}
        meta["cmdline"] = self.cmdline
        meta["started"] = self.started
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


JOBS: dict[str, Job] = {}


def load_old_jobs() -> None:
    if not JOBS_META.exists():
        return
    for meta in JOBS_META.glob("*/meta.json"):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            j = Job.__new__(Job)
            j.id, j.name, j.args = m["job_id"], m["name"], []
            j.dir, j.log = Path(m["job_dir"]), Path(m["log"])
            j.started = m.get("started", 0)
            j.exit = m.get("exit_code")
            j.proc = None
            j.cmdline = m.get("cmdline", "")
            j.ended = j.started + m.get("elapsed_min", 0) * 60
            JOBS[j.id] = j
        except Exception:
            pass


load_old_jobs()


def launch(name: str, commands: list[str], dry_run: bool) -> dict:
    args = norm_cmds(commands)
    if dry_run:
        exe = find_exe() or Path(r"C:\Program Files\Epic Games\RealityScan\RealityScan.exe")
        full = [str(exe), "-headless", "-stdConsole", "-printProgress", "-silent", "<job_dir>"] + args + ["-quit"]
        return {"dry_run": True, "name": name, "cmdline": subprocess.list2cmdline(full)}
    job = Job(name, args)
    job.start()
    JOBS[job.id] = job
    return {"started": True, **job.info(0), "hint": "poll with rs_job_status(job_id)"}


# --------------------------------------------------------------------------- tools: status
@mcp.tool()
def rs_status() -> dict:
    """Report whether RealityScan is installed, the GPU, running GUI instances and known jobs."""
    exe = find_exe()
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                              "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=10, creationflags=NO_WINDOW).stdout.strip()
    except Exception:
        gpu = "nvidia-smi not available"
    running: list[str] = []
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RealityScan.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        running = [line.split('","')[1] for line in out.splitlines() if line.startswith('"RealityScan.exe"')]
    except Exception:
        pass
    return {
        "installed": exe is not None,
        "exe": str(exe) if exe else None,
        "install_hint": None if exe else "Install RealityScan via Epic Games Launcher (free under 1M USD revenue).",
        "gpu": gpu,
        "running_realityscan_pids": running,
        "jobs_dir": str(JOBS_DIR),
        "jobs": {j.id: j.state() for j in JOBS.values()},
    }


@mcp.tool()
def rs_cli_reference() -> str:
    """Short reference of the RealityScan CLI commands this server relies on, with doc links."""
    return """Project: -newScene | -load proj.rsproj | -save [proj.rsproj] | -addFolder DIR | -add IMG
Alignment: -align | -selectMaximalComponent | -mergeComponents | -importGroundControlPoints gcp.csv | -detectMarkers
Region: -setReconstructionRegionAuto | -setReconstructionRegion box.rcbox | -exportReconstructionRegion box.rcbox
Mesh: -calculatePreviewModel | -calculateNormalModel | -calculateHighModel | -simplify N | -smooth | -closeHoles N | -cleanModel
Texture: -unwrap [params.xml] | -calculateTexture
Export: -setOutputCoordinateSystem epsg:4326|epsg:5186|Local:1 | -export3dTiles DIR\\tileset.json | -exportSelectedModel file.glb|.obj|.fbx [params.xml] | -exportModel NAME file [params.xml] | -exportLod file
Settings: -set "key=value" | -setProjectCoordinateSystem authority:id
GUI instance: -setInstanceName RS1 | -delegateTo RS1|* <commands> | -getStatus RS1 | -waitCompleted RS1 | -abortInstance RS1
Docs: https://dev.epicgames.com/documentation/realityscan/command-line-operations
      https://rshelp.capturingreality.com/en-US/tutorials/commandline.htm"""


# --------------------------------------------------------------------------- tools: images
@mcp.tool()
def rs_inspect_images(folder: str, sample: int = 40) -> dict:
    """Inspect a photo folder before processing: count, resolution, EXIF GPS presence, size,
    and a recommendation (chunking, feature count) for a 16 GB RAM / 8 GB VRAM machine."""
    p = Path(folder)
    if not p.is_dir():
        raise RuntimeError(f"folder not found: {folder}")
    files = [f for f in p.rglob("*") if f.suffix.lower() in IMAGE_EXT and f.is_file()]
    total_mb = sum(f.stat().st_size for f in files) / 1e6
    res: dict[str, int] = {}
    cams: dict[str, int] = {}
    gps = 0
    sampled_n = 0
    try:
        from PIL import Image
        step = max(1, len(files) // sample) if files else 1
        sampled = files[::step][:sample]
        sampled_n = len(sampled)
        for f in sampled:
            try:
                with Image.open(f) as im:
                    key = f"{im.width}x{im.height}"
                    res[key] = res.get(key, 0) + 1
                    ex = im.getexif()
                    model = str(ex.get(0x0110, "")).strip()
                    cams[model] = cams.get(model, 0) + 1
                    if ex.get_ifd(0x8825):
                        gps += 1
            except Exception:
                pass
    except ImportError:
        pass
    n = len(files)
    if n == 0:
        advice = "No images found."
    elif n <= 500:
        advice = "Fits one project on 16 GB RAM. Use defaults (40k features, Normal detail)."
    elif n <= 1200:
        advice = ("Set max features per image to 20000 (max_features=20000) or split "
                  "into 2 reconstruction regions after one alignment.")
    else:
        advice = ("Too many for one project on this machine: use rs_tiled_pipeline "
                  "(GPS grid tiles of <=500 photos each, merged into one 3D Tiles tileset).")
    return {
        "folder": str(p), "image_count": n, "total_mb": round(total_mb),
        "sampled": sampled_n, "resolutions": res, "cameras": cams,
        "gps_in_sample": gps,
        "georeferenced": (gps > 0 and gps >= sampled_n * 0.8) if sampled_n else None,
        "advice": advice,
    }


# --------------------------------------------------------------------------- tools: jobs
@mcp.tool()
def rs_run(commands: list[str], name: str = "custom", dry_run: bool = False) -> dict:
    """Run an arbitrary RealityScan CLI command sequence headless as a background job.
    Each list item is one command, e.g. ["-load C:/p/a.rsproj", "-calculateNormalModel", "-save"].
    -headless/-stdConsole/-silent/-quit are added automatically. Returns a job_id."""
    return launch(name, commands, dry_run)


@mcp.tool()
def rs_job_status(job_id: str, tail_lines: int = 30) -> dict:
    """State (running/done/failed), exit code, elapsed minutes and the last log lines of a job."""
    j = JOBS.get(job_id)
    if not j:
        raise RuntimeError(f"unknown job {job_id}; known: {list(JOBS)}")
    return j.info(tail_lines)


@mcp.tool()
def rs_job_list() -> dict:
    """List all known jobs, newest first."""
    return {"jobs": [j.info(0) for j in sorted(JOBS.values(), key=lambda x: x.started, reverse=True)]}


@mcp.tool()
def rs_job_abort(job_id: str) -> dict:
    """Kill a running job."""
    j = JOBS.get(job_id)
    if not j:
        raise RuntimeError(f"unknown job {job_id}")
    return {"job_id": job_id, "killed": j.abort(), "state": j.state()}


# --------------------------------------------------------------------------- tools: pipeline
@mcp.tool()
def rs_drone_pipeline(
    images_folder: str,
    output_folder: str,
    name: str = "site",
    detail: str = "normal",
    simplify_triangles: int = 3_000_000,
    texture: bool = True,
    export_3d_tiles: bool = True,
    tiles_epsg: str = "epsg:4326",
    model_formats: Optional[list[str]] = None,
    gcp_csv: Optional[str] = None,
    max_features: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Full drone-mapping pipeline in one background job:
    add photos -> align -> largest component -> auto region -> mesh (preview|normal|high)
    -> save .rsproj -> simplify -> texture -> export Cesium 3D Tiles (+ glb/obj/fbx in Local:1).
    model_formats defaults to ["glb"]. Use dry_run=True to see the command line without running."""
    detail_cmd = detail_command(detail)
    formats = [f.lstrip(".").lower() for f in (model_formats if model_formats is not None else ["glb"])]
    out = Path(output_folder) / name
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)
    proj = out / f"{name}.rsproj"
    cmds: list[str] = []
    if max_features:
        cmds += ["-set", f"sfmMaxFeaturesPerImage={max_features}"]
    cmds += ["-newScene", "-addFolder", str(Path(images_folder))]
    if gcp_csv:
        cmds += ["-importGroundControlPoints", str(Path(gcp_csv))]
    cmds += ["-align", "-selectMaximalComponent", "-setReconstructionRegionAuto", detail_cmd,
             "-save", str(proj)]
    if simplify_triangles and simplify_triangles > 0:
        cmds += ["-simplify", str(simplify_triangles)]
    if texture:
        cmds += ["-calculateTexture"]
    if export_3d_tiles:
        cmds += ["-setOutputCoordinateSystem", tiles_epsg,
                 "-export3dTiles", str(out / "tiles" / "tileset.json")]
    for fmt in formats:
        cmds += ["-setOutputCoordinateSystem", "Local:1", "-exportSelectedModel", str(out / f"{name}.{fmt}")]
    cmds += ["-save"]
    res = launch(f"pipeline:{name}", cmds, dry_run)
    res["outputs"] = {
        "project": str(proj),
        "tiles": str(out / "tiles" / "tileset.json") if export_3d_tiles else None,
        "models": [str(out / f"{name}.{f}") for f in formats],
    }
    return res


@mcp.tool()
def rs_align(images_folder: str, project_path: str, gcp_csv: Optional[str] = None,
             max_features: Optional[int] = None, dry_run: bool = False) -> dict:
    """Step 1: add a photo folder, align, keep the largest component, save the .rsproj."""
    cmds: list[str] = []
    if max_features:
        cmds += ["-set", f"sfmMaxFeaturesPerImage={max_features}"]
    cmds += ["-newScene", "-addFolder", str(Path(images_folder))]
    if gcp_csv:
        cmds += ["-importGroundControlPoints", str(Path(gcp_csv))]
    cmds += ["-align", "-selectMaximalComponent", "-save", str(Path(project_path))]
    if not dry_run:
        Path(project_path).parent.mkdir(parents=True, exist_ok=True)
    return launch("align", cmds, dry_run)


@mcp.tool()
def rs_mesh(project_path: str, detail: str = "normal", region_rsbox: Optional[str] = None,
            simplify_triangles: Optional[int] = None, clean: bool = True, dry_run: bool = False) -> dict:
    """Step 2: load an aligned project, set the reconstruction region (auto or an .rsbox file),
    compute the mesh at preview|normal|high detail, optionally clean and simplify, save."""
    detail_cmd = detail_command(detail)
    cmds = ["-load", str(Path(project_path)), "-selectMaximalComponent"]
    cmds += ["-setReconstructionRegion", str(Path(region_rsbox))] if region_rsbox else ["-setReconstructionRegionAuto"]
    cmds += [detail_cmd]
    if clean:
        cmds += ["-cleanModel"]
    if simplify_triangles:
        cmds += ["-simplify", str(simplify_triangles)]
    cmds += ["-save"]
    return launch("mesh", cmds, dry_run)


@mcp.tool()
def rs_texture(project_path: str, unwrap_params_xml: Optional[str] = None, dry_run: bool = False) -> dict:
    """Step 3: load a project, unwrap (optional params.xml) and texture the selected model, save."""
    cmds = ["-load", str(Path(project_path))]
    cmds += ["-unwrap", str(Path(unwrap_params_xml))] if unwrap_params_xml else ["-unwrap"]
    cmds += ["-calculateTexture", "-save"]
    return launch("texture", cmds, dry_run)


@mcp.tool()
def rs_export(project_path: str, output_path: str, kind: str = "model",
              coordinate_system: str = "Local:1", params_xml: Optional[str] = None,
              dry_run: bool = False) -> dict:
    """Step 4: export the selected model. kind='model' writes output_path (.glb/.obj/.fbx/.ply...),
    kind='tiles' writes Cesium 3D Tiles to output_path (a tileset.json), kind='lod' linear LODs.
    coordinate_system: Local:1, epsg:4326, epsg:5186, epsg:5179 ..."""
    export_cmd = {"model": "-exportSelectedModel", "tiles": "-export3dTiles", "lod": "-exportLod"}.get(kind)
    if not export_cmd:
        raise RuntimeError("kind must be model, tiles or lod")
    outp = str(Path(output_path))
    cmds = ["-load", str(Path(project_path)), "-setOutputCoordinateSystem", coordinate_system, export_cmd, outp]
    if params_xml:
        cmds.append(str(Path(params_xml)))
    if not dry_run:
        Path(outp).parent.mkdir(parents=True, exist_ok=True)
    return launch(f"export:{kind}", cmds, dry_run)


# --------------------------------------------------------------------------- tools: GUI instance control
@mcp.tool()
def rs_gui_launch(instance_name: str = "RS1", project_path: Optional[str] = None) -> dict:
    """Open a visible RealityScan window named instance_name (optionally loading a project)
    so the user can watch while commands are delegated to it with rs_gui_send."""
    exe = need_exe()
    args = [str(exe), "-setInstanceName", instance_name]
    if project_path:
        args += ["-load", str(Path(project_path))]
    p = subprocess.Popen(args, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return {"pid": p.pid, "instance": instance_name, "cmdline": subprocess.list2cmdline(args)}


@mcp.tool()
def rs_gui_send(commands: list[str], instance_name: str = "*", wait_completed: bool = False,
                timeout_s: int = 120) -> dict:
    """Delegate commands to an already running RealityScan window ('*' = first instance found).
    Returns immediately unless wait_completed=True (blocks up to timeout_s)."""
    exe = need_exe()
    args = [str(exe), "-delegateTo", instance_name] + norm_cmds(commands)
    if wait_completed:
        args += ["-waitCompleted", instance_name]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, creationflags=NO_WINDOW)
    return {"cmdline": subprocess.list2cmdline(args), "exit_code": r.returncode,
            "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:]}


@mcp.tool()
def rs_gui_status(instance_name: str = "*") -> dict:
    """Ask a running RealityScan window for its progress status (-getStatus)."""
    exe = need_exe()
    args = [str(exe), "-stdConsole", "-getStatus", instance_name]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    return {"exit_code": r.returncode, "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:]}


@mcp.tool()
def rs_gui_abort(instance_name: str = "*") -> dict:
    """Abort the process running in a RealityScan window (-abortInstance)."""
    exe = need_exe()
    r = subprocess.run([str(exe), "-abortInstance", instance_name], capture_output=True, text=True,
                       timeout=30, creationflags=NO_WINDOW)
    return {"exit_code": r.returncode, "stdout": r.stdout[-500:]}


# --------------------------------------------------------------------------- tools: tiled pipeline (30k+ photos)
import tiling  # noqa: E402  (local module, keeps grid maths testable without the server)

EST_MIN_PER_PHOTO = {"preview": 0.08, "normal": 0.25, "high": 0.6}   # rough, RTX 4060 Laptop


def _plan_path(run_dir: Path) -> Path:
    return run_dir / "plan.json"


def _load_plan(run_dir: Path) -> dict:
    p = _plan_path(run_dir)
    if not p.exists():
        raise RuntimeError(f"no plan.json in {run_dir}; run rs_tiled_pipeline first")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_plan(run_dir: Path, plan: dict) -> None:
    tmp = _plan_path(run_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    tmp.replace(_plan_path(run_dir))


def _plan_summary(plan: dict, run_dir: Path) -> dict:
    tiles = plan["tiles"]
    counts: dict[str, int] = {}
    for t in tiles:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    active = [t for t in tiles if t["status"] != "skipped"]
    per = EST_MIN_PER_PHOTO.get(plan["params"]["detail"], 0.25)
    est_min = sum(t["buffered_count"] * per for t in active if t["status"] != "done")
    return {
        "run_dir": str(run_dir),
        "state": plan.get("state", "planned"),
        "utm_epsg": plan["utm"]["epsg"],
        "extent_m": plan["extent_m"],
        "grid": plan["grid"],
        "photo_count": plan["photo_count"],
        "photos_without_gps": plan.get("photos_without_gps", 0),
        "tile_count": len(active),
        "tiles_by_status": counts,
        "remaining_est_hours": round(est_min / 60, 1),
        "max_tile_photos": max((t["buffered_count"] for t in active), default=0),
        "tiles": [{k: t[k] for k in ("id", "status", "core_count", "buffered_count", "job_id", "note")}
                  for t in tiles],
        "parent_tileset": str(run_dir / "tiles" / "tileset.json"),
    }


def _link_tile_images(run_dir: Path, tile_id: str, photos: list[str], mode: str) -> Path:
    dst = run_dir / "images" / tile_id
    dst.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for src in photos:
        s = Path(src)
        name = s.name
        if name.lower() in seen:                       # same basename from two sub-folders
            name = f"{s.parent.name}_{s.name}"
        seen.add(name.lower())
        d = dst / name
        if d.exists():
            continue
        try:
            if mode == "symlink":
                os.symlink(s, d)
            elif mode == "copy":
                shutil.copy2(s, d)
            else:
                try:
                    os.link(s, d)
                except OSError:                        # other volume: fall back to copy
                    shutil.copy2(s, d)
        except OSError as e:
            raise RuntimeError(f"cannot place {s} into {dst}: {e}")
    return dst


class TiledRun:
    """Runs the tiles of one plan sequentially (one RealityScan process at a time)."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.stop = threading.Event()
        self.current: Optional[Job] = None
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, max_tiles: Optional[int]) -> None:
        self.thread = threading.Thread(target=self._loop, args=(max_tiles,), daemon=True)
        self.thread.start()

    def _tile_commands(self, plan: dict, tile: dict, images_dir: Path) -> list[str]:
        p = plan["params"]
        rd = self.run_dir
        tid = tile["id"]
        proj = rd / "projects" / tid / f"{tid}.rsproj"
        proj.parent.mkdir(parents=True, exist_ok=True)
        tiles_out = rd / "tiles" / tid / "tileset.json"
        cmds: list[str] = []
        if p.get("max_features"):
            cmds += ["-set", f"sfmMaxFeaturesPerImage={p['max_features']}"]
        cmds += ["-newScene", "-addFolder", str(images_dir)]
        if p.get("gcp_csv"):
            cmds += ["-importGroundControlPoints", str(Path(p["gcp_csv"]))]
        cmds += ["-align", "-selectMaximalComponent",
                 "-exportReconstructionRegion", str(rd / "regions" / f"{tid}_auto.rcbox")]
        if p["region_mode"] == "rcbox":
            box = tiling.write_rcbox(rd / "regions" / f"{tid}.rcbox", tile, plan["utm"])
            cmds += ["-setReconstructionRegion", str(box)]
        else:
            cmds += ["-setReconstructionRegionAuto"]
        cmds += [detail_command(p["detail"]), "-save", str(proj)]
        if p.get("simplify_triangles"):
            cmds += ["-simplify", str(p["simplify_triangles"])]
        if p.get("texture", True):
            cmds += ["-calculateTexture"]
        cmds += ["-setOutputCoordinateSystem", p["tiles_epsg"], "-export3dTiles", str(tiles_out), "-save"]
        return cmds

    def _loop(self, max_tiles: Optional[int]) -> None:
        rd = self.run_dir
        try:
            plan = _load_plan(rd)
            plan["state"] = "running"
            _save_plan(rd, plan)
            photos, _ = tiling.build_gps_index(Path(plan["images_folder"]), rd / "gps_index.csv",
                                               plan["params"].get("gps_csv") and Path(plan["params"]["gps_csv"]))
            zone, north = plan["utm"]["zone"], plan["utm"]["north"]
            done_now = 0
            for tile in plan["tiles"]:
                if self.stop.is_set() or (max_tiles and done_now >= max_tiles):
                    break
                if tile["status"] != "pending":
                    continue
                tid = tile["id"]
                tile.update(status="running", attempts=tile["attempts"] + 1, started=time.time(), note="")
                _save_plan(rd, plan)
                try:
                    names = tiling.photos_for_tile(photos, tile, zone, north)
                    (rd / "plan_photos").mkdir(exist_ok=True)
                    (rd / "plan_photos" / f"{tid}.txt").write_text("\n".join(names), encoding="utf-8")
                    images_dir = _link_tile_images(rd, tid, names, plan["params"]["link_mode"])
                    cmds = self._tile_commands(plan, tile, images_dir)
                    job = Job(f"tile:{plan['name']}:{tid}", norm_cmds(cmds))
                    job.start()
                    JOBS[job.id] = job
                    self.current = job
                    tile["job_id"] = job.id
                    _save_plan(rd, plan)
                    while job.state() == "running":
                        time.sleep(2)
                    self.current = None
                    out_ok = (rd / "tiles" / tid / "tileset.json").exists()
                    if self.stop.is_set() and job.exit not in (0,):
                        tile.update(status="pending", note="aborted by user", ended=time.time())
                    elif job.exit == 0 and out_ok:
                        tile.update(status="done", ended=time.time())
                        done_now += 1
                        if plan["params"].get("cleanup_images"):
                            shutil.rmtree(images_dir, ignore_errors=True)
                    else:
                        tile.update(status="failed", ended=time.time(),
                                    note=f"exit={job.exit}, tileset.json present={out_ok}; see {job.log}")
                except Exception as e:                   # noqa: BLE001
                    tile.update(status="failed", ended=time.time(), note=f"{type(e).__name__}: {e}")
                _save_plan(rd, plan)
            try:
                tiling.write_parent_tileset(rd / "tiles" / "tileset.json", plan["tiles"], rd / "tiles")
            except RuntimeError:
                pass
            pending = [t for t in plan["tiles"] if t["status"] in ("pending", "running")]
            failed = [t for t in plan["tiles"] if t["status"] == "failed"]
            plan["state"] = ("aborted" if self.stop.is_set() else
                             "paused" if pending else "finished_with_failures" if failed else "finished")
            _save_plan(rd, plan)
        except Exception as e:                           # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            try:
                plan = _load_plan(rd)
                plan["state"] = f"error: {self.error}"
                _save_plan(rd, plan)
            except Exception:
                pass

    def abort(self) -> dict:
        self.stop.set()
        killed = self.current.abort() if self.current else False
        return {"stop_requested": True, "killed_current_job": killed}


TILED: dict[str, TiledRun] = {}


@mcp.tool()
def rs_tiled_pipeline(
    images_folder: str,
    output_folder: str,
    name: str = "bigsite",
    max_photos_per_tile: int = 500,
    overlap: float = 0.2,
    detail: str = "normal",
    simplify_triangles: int = 3_000_000,
    texture: bool = True,
    tiles_epsg: str = "epsg:4326",
    gcp_csv: Optional[str] = None,
    gps_csv: Optional[str] = None,
    max_features: Optional[int] = None,
    region_mode: str = "rcbox",
    region_depth_m: float = 250.0,
    min_photos_per_tile: int = 20,
    link_mode: str = "hardlink",
    cleanup_images: Optional[bool] = None,
    resume: bool = True,
    retry_failed: bool = False,
    replan: bool = False,
    max_tiles: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Process a huge photo set (e.g. 30 000+ images) as a grid of GPS-based tiles.

    Reads every photo's EXIF GPS (or gps_csv: name,lat,lon[,alt]), projects to UTM, and picks the
    largest square cell such that each tile *including its overlap buffer* holds at most
    max_photos_per_tile images. Each tile is aligned from the buffered photo set but meshed only
    inside its core cell (region_mode="rcbox"; use "auto" for RealityScan's automatic region,
    which overlaps neighbours). Tiles run one after another as background jobs; a parent
    tiles/tileset.json referencing every finished tile is written for Cesium.

    Layout under <output_folder>/<name>/: plan.json, gps_index.csv, images/<tile> (hard links),
    regions/<tile>.rcbox, projects/<tile>/<tile>.rsproj, tiles/<tile>/tileset.json.
    dry_run=True only writes plan.json + gps_index.csv and returns the grid plan.
    Re-calling with the same name resumes (resume=True); retry_failed=True re-queues failed tiles;
    replan=True recomputes the grid from scratch. max_tiles limits how many tiles run this call.
    Call rs_tiled_status(run_dir) to follow progress, rs_tiled_abort(run_dir) to stop.
    """
    detail_command(detail)
    if region_mode not in ("rcbox", "auto"):
        raise RuntimeError("region_mode must be rcbox or auto")
    if link_mode not in ("hardlink", "copy", "symlink"):
        raise RuntimeError("link_mode must be hardlink, copy or symlink")
    if not 0 <= overlap <= 1:
        raise RuntimeError("overlap must be between 0 and 1 (fraction of the cell size)")
    src = Path(images_folder)
    if not src.is_dir():
        raise RuntimeError(f"folder not found: {images_folder}")
    run_dir = Path(output_folder) / name
    run_dir.mkdir(parents=True, exist_ok=True)
    run = TILED.get(str(run_dir))
    if run and run.alive():
        raise RuntimeError(f"{name} is already running; use rs_tiled_status / rs_tiled_abort")
    busy = [r for r in TILED.values() if r.alive()]
    if busy and not dry_run:
        raise RuntimeError(f"another tiled run is active ({busy[0].run_dir}); only one at a time fits the GPU")

    params = {
        "detail": detail, "simplify_triangles": simplify_triangles, "texture": texture,
        "tiles_epsg": tiles_epsg, "gcp_csv": gcp_csv, "gps_csv": gps_csv, "max_features": max_features,
        "region_mode": region_mode, "region_depth_m": region_depth_m, "link_mode": link_mode,
        "cleanup_images": (link_mode == "copy") if cleanup_images is None else cleanup_images,
    }
    plan: Optional[dict] = None
    if resume and not replan and _plan_path(run_dir).exists():
        plan = _load_plan(run_dir)
        if Path(plan["images_folder"]).resolve() != src.resolve():
            raise RuntimeError(f"plan.json belongs to {plan['images_folder']}; use replan=True or another name")
        plan["params"].update(params)
        for t in plan["tiles"]:
            if t["status"] == "running":
                t.update(status="pending", note="interrupted; re-queued")
            elif t["status"] == "failed" and retry_failed:
                t.update(status="pending", note="retry")
        reused = True
    else:
        photos, no_gps = tiling.build_gps_index(src, run_dir / "gps_index.csv",
                                                Path(gps_csv) if gps_csv else None, force=replan)
        if not photos:
            raise RuntimeError("no photos with GPS found; drone EXIF GPS or gps_csv is required for tiling")
        plan = tiling.plan_grid(photos, max_photos_per_tile, overlap, min_photos_per_tile, region_depth_m)
        plan.update(name=name, images_folder=str(src), output_folder=str(Path(output_folder)),
                    created=time.time(), params=params, photos_without_gps=len(no_gps),
                    no_gps_sample=no_gps[:5], state="planned")
        reused = False
    _save_plan(run_dir, plan)

    summary = _plan_summary(plan, run_dir)
    summary["resumed_existing_plan"] = reused
    if dry_run:
        summary["dry_run"] = True
        summary["example_tile_cmdline"] = None
        first = next((t for t in plan["tiles"] if t["status"] == "pending"), None)
        if first:
            tr = TiledRun(run_dir)
            cmds = tr._tile_commands(plan, first, run_dir / "images" / first["id"])
            summary["example_tile_cmdline"] = launch("tile-dry", cmds, True)["cmdline"]
        return summary
    need_exe()
    run = TiledRun(run_dir)
    TILED[str(run_dir)] = run
    run.start(max_tiles)
    summary.update(started=True, hint="poll rs_tiled_status(run_dir); each tile also appears in rs_job_list")
    return summary


@mcp.tool()
def rs_tiled_status(run_dir: str, tail_lines: int = 15) -> dict:
    """Progress of a tiled run (<output_folder>/<name>): tiles by status, remaining estimate,
    and the log tail of the tile currently running."""
    rd = Path(run_dir)
    plan = _load_plan(rd)
    s = _plan_summary(plan, rd)
    run = TILED.get(str(rd))
    s["runner_alive"] = bool(run and run.alive())
    s["runner_error"] = run.error if run else None
    cur = next((t for t in plan["tiles"] if t["status"] == "running"), None)
    if cur and cur.get("job_id") in JOBS:
        s["current_tile"] = cur["id"]
        s["current_log_tail"] = JOBS[cur["job_id"]].info(tail_lines)["log_tail"]
    return s


@mcp.tool()
def rs_tiled_abort(run_dir: str) -> dict:
    """Stop a tiled run after killing the tile currently processing; finished tiles are kept
    and the run can be resumed later with rs_tiled_pipeline(..., same name)."""
    run = TILED.get(str(Path(run_dir)))
    if not run or not run.alive():
        return {"stop_requested": False, "note": "no active runner for this run_dir"}
    return run.abort()


@mcp.tool()
def rs_tiled_merge(run_dir: str, geometric_error: float = 400.0) -> dict:
    """(Re)write the parent tiles/tileset.json that references every finished tile's tileset.json.
    Runs automatically at the end of a tiled run; call it manually after partial runs."""
    rd = Path(run_dir)
    plan = _load_plan(rd)
    return tiling.write_parent_tileset(rd / "tiles" / "tileset.json", plan["tiles"], rd / "tiles", geometric_error)


if __name__ == "__main__":
    mcp.run()
