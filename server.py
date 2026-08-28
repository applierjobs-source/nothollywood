"""
ShowForge Studio / Not Hollywood backend.
Submits video generation to MiniMax H3, polls, downloads, optionally splits long
prompts into multiple 10-second scenes and stitches them together with ffmpeg.
"""
import base64
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from threading import Thread

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = "https://api.minimax.io/v2"


def _load_env_file() -> None:
    env_path = Path("/home/user/workspace/showforge/.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()
# MINIMAX_API_KEY: direct env var (dev sandbox) or
# CUSTOM_CRED_API_MINIMAX_IO_TOKEN: injected by publish_website credential proxy (prod sandbox)
API_KEY = (
    os.environ.get("MINIMAX_API_KEY", "")
    or os.environ.get("CUSTOM_CRED_API_MINIMAX_IO_TOKEN", "")
)
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")
# ROOT is the directory this file lives in. Works in both dev and prod sandboxes.
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
VIDEOS = STATIC / "videos"
THUMBS = STATIC / "thumbs"
JOBS_FILE = ROOT / "jobs.json"

SCENES = ROOT / "scenes"  # per-scene mp4s before concat

STATIC.mkdir(exist_ok=True)
VIDEOS.mkdir(exist_ok=True)
THUMBS.mkdir(exist_ok=True)
SCENES.mkdir(exist_ok=True)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def load_jobs() -> dict:
    if JOBS_FILE.exists():
        return json.loads(JOBS_FILE.read_text())
    return {}


def save_jobs(jobs: dict) -> None:
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))


JOBS: dict = load_jobs()


def headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def encode_image_bytes(raw: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def submit_h3(prompt: str, duration: int, resolution: str, ref_data_url: str | None) -> tuple[str | None, str | None]:
    content: list[dict] = [{"type": "text", "text": prompt}]
    if ref_data_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": ref_data_url},
            "role": "reference_image",
        })
    body: dict = {
        "model": "MiniMax-H3",
        "content": content,
        "duration": duration,
        "resolution": resolution,
    }
    if not ref_data_url:
        body["ratio"] = "16:9"
    try:
        r = requests.post(f"{BASE}/video_generation", headers=headers(), json=body, timeout=60)
    except Exception as e:  # noqa: BLE001
        return None, f"network error: {e}"
    if r.status_code != 200:
        return None, f"h3 http {r.status_code}: {r.text[:400]}"
    data = r.json()
    tid = data.get("task_id")
    if not tid:
        return None, f"h3 no task_id in response: {data}"
    return tid, None


def fetch_task(task_id: str) -> dict:
    r = requests.get(f"{BASE}/query/video_generation/{task_id}", headers=headers(), timeout=30)
    if r.status_code != 200:
        return {}
    return r.json().get("task", {})


def plan_scenes(total_seconds: int) -> list[int]:
    """Split total seconds into H3-compatible chunks (10s at a time; last may be smaller).

    H3 native durations are 4, 6, 8, 10 seconds. We use 10s for all but the final
    scene, and pick the closest supported length for the tail.
    """
    if total_seconds <= 10:
        # snap up to closest supported length
        for d in (4, 6, 8, 10):
            if total_seconds <= d:
                return [d]
        return [10]
    full = total_seconds // 10
    tail = total_seconds - full * 10
    scenes = [10] * full
    if tail > 0:
        for d in (4, 6, 8, 10):
            if tail <= d:
                scenes.append(d)
                break
    return scenes


def extract_last_frame(video_path: Path, out_path: Path) -> bool:
    """Grab the final frame of a video as a PNG. Returns True on success."""
    try:
        # -sseof -0.1 seeks to 0.1s before the end, then -frames:v 1 grabs one frame
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-sseof", "-0.1", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2", str(out_path),
            ],
            capture_output=True, timeout=60,
        )
        return result.returncode == 0 and out_path.exists()
    except Exception:  # noqa: BLE001
        return False


def concat_scenes(scene_paths: list[Path], out_path: Path) -> tuple[bool, str]:
    """Concatenate an ordered list of mp4s into one mp4 using ffmpeg's concat demuxer."""
    listfile = out_path.with_suffix(".txt")
    listfile.write_text("\n".join(f"file '{p}'" for p in scene_paths))
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(listfile),
                "-c", "copy", str(out_path),
            ],
            capture_output=True, timeout=180,
        )
        if result.returncode != 0:
            # fall back to re-encoding if streams don't match for stream copy
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listfile),
                    "-c:v", "libx264", "-c:a", "aac", "-preset", "fast",
                    str(out_path),
                ],
                capture_output=True, timeout=300,
            )
        listfile.unlink(missing_ok=True)
        if result.returncode != 0:
            return False, result.stderr.decode(errors="ignore")[-400:]
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def render_one_scene(
    scene_prompt: str,
    duration: int,
    resolution: str,
    ref_data_url: str | None,
    scene_out_path: Path,
    on_status,
) -> tuple[bool, str]:
    """Submit one H3 job, poll it, download to scene_out_path.

    on_status(status: str) called periodically for UI updates.
    Returns (ok, error).
    """
    task_id, err = submit_h3(scene_prompt, duration, resolution, ref_data_url)
    if not task_id:
        return False, err or "submit failed"

    start = time.time()
    max_wait = 8 * 60
    while time.time() - start < max_wait:
        time.sleep(15)
        task = fetch_task(task_id)
        status = task.get("status", "running")
        on_status(status)
        if status == "succeeded":
            url = (task.get("content") or {}).get("url")
            if not url:
                return False, "no url returned"
            try:
                with requests.get(url, timeout=180, stream=True) as resp:
                    resp.raise_for_status()
                    with open(scene_out_path, "wb") as fp:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            fp.write(chunk)
                return True, ""
            except Exception as e:  # noqa: BLE001
                return False, f"download failed: {e}"
        if status == "failed":
            return False, (task.get("error") or {}).get("message", "generation failed")
    return False, "scene timed out after 8 minutes"


def multi_scene_worker(job_id: str, initial_ref_data_url: str | None) -> None:
    """Render each scene sequentially (chaining last-frame -> next-frame ref),
    then concatenate the results into one video."""
    job = JOBS.get(job_id)
    if not job:
        return
    scenes = job["scenes_plan"]  # list of ints
    prompt = job["prompt"]
    resolution = job["resolution"]

    scene_dir = SCENES / job_id
    scene_dir.mkdir(exist_ok=True)

    scene_paths: list[Path] = []
    ref_data_url = initial_ref_data_url

    def _update_scene_status(idx: int, status: str):
        j = JOBS.get(job_id)
        if not j:
            return
        j["scene_status"] = status
        j["scene_index"] = idx + 1
        j["scene_total"] = len(scenes)
        j["status"] = "rendering"
        save_jobs(JOBS)

    for i, dur in enumerate(scenes):
        scene_prompt = prompt
        if len(scenes) > 1:
            scene_prompt = (
                f"Scene {i+1} of {len(scenes)} in a continuous story. "
                f"Maintain the exact same characters, wardrobe, setting, and visual style throughout. "
                f"{prompt}"
            )
        scene_out = scene_dir / f"scene_{i:02d}.mp4"
        ok, err = render_one_scene(
            scene_prompt, dur, resolution, ref_data_url, scene_out,
            on_status=lambda s, i=i: _update_scene_status(i, s),
        )
        if not ok:
            job["status"] = "failed"
            job["error"] = f"scene {i+1}/{len(scenes)}: {err}"
            save_jobs(JOBS)
            return
        scene_paths.append(scene_out)

        # Extract last frame to chain into next scene for continuity
        if i + 1 < len(scenes):
            frame_path = scene_dir / f"frame_{i:02d}.png"
            if extract_last_frame(scene_out, frame_path):
                raw = frame_path.read_bytes()
                ref_data_url = encode_image_bytes(raw, "image/png")

    # All scenes rendered — stitch
    j = JOBS.get(job_id)
    if j:
        j["status"] = "stitching"
        save_jobs(JOBS)

    dest = VIDEOS / f"{job_id}.mp4"
    if len(scene_paths) == 1:
        # single scene — just move it
        try:
            scene_paths[0].rename(dest)
        except Exception:
            import shutil
            shutil.copy(scene_paths[0], dest)
        ok, err = True, ""
    else:
        ok, err = concat_scenes(scene_paths, dest)

    j = JOBS.get(job_id)
    if not j:
        return
    if ok:
        j["status"] = "done"
        j["video"] = f"/static/videos/{job_id}.mp4"
        j["finished_at"] = time.time()
    else:
        j["status"] = "failed"
        j["error"] = f"stitch failed: {err}"
    save_jobs(JOBS)


@app.post("/api/generate")
async def generate(
    request: Request,
    prompt: str = Form(...),
    duration: int = Form(6),
    resolution: str = Form("768P"),
    reference: UploadFile | None = File(None),
):
    # Password gate
    if SITE_PASSWORD:
        supplied = request.headers.get("X-Site-Password", "")
        if supplied != SITE_PASSWORD:
            raise HTTPException(401, "password required or incorrect")
    if not prompt.strip():
        raise HTTPException(400, "prompt is empty")
    if duration < 4 or duration > 600:
        raise HTTPException(400, "duration must be between 4 and 600 seconds")
    if resolution not in ("768P", "1080P"):
        raise HTTPException(400, "resolution must be 768P or 1080P")
    ref_data_url = None
    if reference is not None:
        raw = await reference.read()
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(400, "reference image too large (max 8MB)")
        mime = reference.content_type or "image/png"
        ref_data_url = encode_image_bytes(raw, mime)

    scenes_plan = plan_scenes(duration)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "prompt": prompt.strip(),
        "duration": duration,
        "resolution": resolution,
        "scenes_plan": scenes_plan,
        "scene_index": 0,
        "scene_total": len(scenes_plan),
        "scene_status": "queued",
        "status": "queued",
        "created_at": time.time(),
    }
    save_jobs(JOBS)
    Thread(target=multi_scene_worker, args=(job_id, ref_data_url), daemon=True).start()
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs")
def list_jobs():
    # Return most recent first, drop internal task_id from public listing
    items = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
    return [{k: v for k, v in j.items() if k != "task_id"} for j in items[:50]]


# Serve the frontend and generated media
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


# Also serve the frontend assets at the root so that when deploy_website
# rehosts the /static/ folder at site root the same relative paths work in
# local dev too. Only whitelisted top-level filenames pass through.
_ROOT_ASSETS = {"styles.css", "app.js"}


@app.get("/{fname}")
def root_asset(fname: str):
    if fname in _ROOT_ASSETS:
        return FileResponse(str(STATIC / fname))
    raise HTTPException(404, "not found")


if __name__ == "__main__":
    import os
    import uvicorn
    # Railway (and most PaaS hosts) inject $PORT. Fall back to 5001 for local dev.
    port = int(os.environ.get("PORT", "5001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
