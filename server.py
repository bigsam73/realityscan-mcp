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
Region: -setReconstructionRegionAuto | -setReconstructionRegion box.rsbox | -exportReconstructionRegion box.rsbox
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
        advice = "Split into chunks of <=600 images (separate flights/areas) and merge or tile the outputs."
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


if __name__ == "__main__":
    mcp.run()
