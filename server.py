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

from auth import require_user, get_user

# Provider: reAPI (unmoderated MiniMax H3 host).
# Migrated from MiniMax direct on 2026-08-28. reAPI proxies to the same underlying H3
# model with the option to disable Google-style content filtering via content_filter:false.
# See https://reapi.ai/models/minimax-h3 for the full schema.
BASE = "https://reapi.ai/api/v1"
GROK_BASE = "https://api.x.ai/v1"

# Public origin for scene-chaining reference-image URLs. reAPI's first_frame_url
# rejects data URLs and requires a public https URL, so we serve extracted frames
# and uploaded references from our own /static mount and pass those URLs to reAPI.
# Falls back to a relative path in dev; reAPI will reject those, which is fine
# because single-scene text-to-video does not use reference images.
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")


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
# reAPI key.
# REAPI_API_KEY: primary Railway/dev env var
# CUSTOM_CRED_REAPI_AI_TOKEN: publish_website credential proxy fallback (main-agent sandbox)
API_KEY = (
    os.environ.get("REAPI_API_KEY", "")
    or os.environ.get("CUSTOM_CRED_REAPI_AI_TOKEN", "")
)
# Grok Imagine fallback: kicks in when MiniMax flags a prompt as sensitive.
# XAI_API_KEY: direct env var or
# CUSTOM_CRED_API_X_AI_TOKEN: publish_website credential proxy
GROK_API_KEY = (
    os.environ.get("XAI_API_KEY", "")
    or os.environ.get("CUSTOM_CRED_API_X_AI_TOKEN", "")
)
# Legacy pre-auth gate. Kept as a defensive break-glass so if Supabase auth is
# somehow bypassed we can re-enable a shared password. In normal operation
# SITE_PASSWORD is unset and Supabase JWT auth is the sole gate.
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
# ROOT is the directory this file lives in. Works in both dev and prod sandboxes.
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
VIDEOS = STATIC / "videos"
THUMBS = STATIC / "thumbs"
JOBS_FILE = ROOT / "jobs.json"

SCENES = ROOT / "scenes"  # per-scene mp4s before concat
FRAMES = STATIC / "frames"  # reference frames (uploads + extracted last frames)
                            # served publicly at /static/frames/<name> so reAPI
                            # can fetch them for first_frame_url

STATIC.mkdir(exist_ok=True)
VIDEOS.mkdir(exist_ok=True)
THUMBS.mkdir(exist_ok=True)
SCENES.mkdir(exist_ok=True)
FRAMES.mkdir(exist_ok=True)

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


def grok_headers() -> dict:
    return {"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"}


# Substrings that indicated MiniMax direct rejected the prompt for policy reasons.
# Kept for the Grok fallback path but effectively dead now that reAPI runs H3
# with content_filter:false — reAPI won't return these strings for policy issues.
# The Grok fallback code below is left in place as a break-glass option, but the
# earlier research confirmed Grok's public API also doesn't support NSFW output,
# so this path will not help with real content-policy failures.
_SENSITIVE_MARKERS = (
    "sensitive",           # 'input new_sensitive, input text sensitive'
    "content policy",
    "policy violation",
    "prohibited",
    "not allowed",
)


def _is_sensitive_error(err: str) -> bool:
    if not err:
        return False
    low = err.lower()
    return any(m in low for m in _SENSITIVE_MARKERS)


# Map MiniMax durations (4/6/8/10) to Grok Imagine durations (5-15s).
# Grok's minimum is 5s, so anything <=5 becomes 6.
def _grok_duration(minimax_duration: int) -> int:
    if minimax_duration <= 5:
        return 6
    if minimax_duration > 15:
        return 15
    return minimax_duration


def submit_grok(prompt: str, duration: int, resolution: str, ref_data_url: str | None) -> tuple[str | None, str | None]:
    """Submit a video generation request to Grok Imagine Video 1.5.

    Returns (request_id, error).
    """
    if not GROK_API_KEY:
        return None, "grok api key not configured"
    body: dict = {
        "model": "grok-imagine-video-1.5",
        "prompt": prompt,
        "duration": _grok_duration(duration),
        "resolution": "720p",
        "aspect_ratio": "16:9",
    }
    if ref_data_url:
        body["image_url"] = ref_data_url
    try:
        r = requests.post(f"{GROK_BASE}/videos/generations", headers=grok_headers(), json=body, timeout=60)
    except Exception as e:  # noqa: BLE001
        return None, f"grok network error: {e}"
    if r.status_code != 200:
        return None, f"grok http {r.status_code}: {r.text[:400]}"
    data = r.json()
    rid = data.get("request_id") or data.get("id")
    if not rid:
        return None, f"grok no request_id in response: {data}"
    return rid, None


def fetch_grok(request_id: str) -> dict:
    """Poll a Grok Imagine job. Normalizes response to look like MiniMax's task shape:
    {status: 'succeeded'|'failed'|'running', content: {url: ...}, error: {message: ...}}
    """
    try:
        r = requests.get(f"{GROK_BASE}/videos/{request_id}", headers=grok_headers(), timeout=30)
    except Exception:
        return {}
    if r.status_code != 200:
        return {}
    d = r.json()
    status = (d.get("status") or "").lower()
    # Grok uses 'done'/'succeeded' terminology; normalize
    if status in ("done", "succeeded", "completed"):
        norm_status = "succeeded"
    elif status in ("failed", "error", "cancelled"):
        norm_status = "failed"
    else:
        norm_status = "running"
    url = d.get("video_url") or d.get("url") or (d.get("video") or {}).get("url")
    err_msg = d.get("error") or d.get("message") or ""
    if isinstance(err_msg, dict):
        err_msg = err_msg.get("message", "")
    return {
        "status": norm_status,
        "content": {"url": url} if url else {},
        "error": {"message": err_msg} if err_msg else {},
    }


def encode_image_bytes(raw: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ─── reAPI resolution mapping ─────────────────────────────────────────────
# Internal API surface uses "768P" / "1080P" (kept for backward compat with the
# frontend). reAPI supports "768P" or "2K" only. 1080P callers get bumped to 2K.
_REAPI_RESOLUTIONS = {"768P": "768P", "1080P": "2K", "2K": "2K"}


def _reapi_resolution(res: str) -> str:
    return _REAPI_RESOLUTIONS.get(res, "768P")


def submit_h3(prompt: str, duration: int, resolution: str, ref_url: str | None) -> tuple[str | None, str | None]:
    """Submit a scene to reAPI's minimax-h3 endpoint.

    ref_url must be a public https URL — reAPI rejects data URLs. When ref_url is
    provided, aspect_ratio is omitted (orientation derives from the source image).
    content_filter is disabled unconditionally; that is the entire reason we picked
    reAPI over MiniMax direct.
    """
    body: dict = {
        "model": "minimax-h3",
        "prompt": prompt,
        "duration": duration,
        "resolution": _reapi_resolution(resolution),
        "content_filter": False,
    }
    if ref_url:
        body["first_frame_url"] = ref_url
    else:
        body["aspect_ratio"] = "16:9"
    try:
        r = requests.post(f"{BASE}/videos/generations", headers=headers(), json=body, timeout=60)
    except Exception as e:  # noqa: BLE001
        return None, f"network error: {e}"
    if r.status_code >= 300:
        # reAPI error shape: {"error": {"code": int, "message": str, "request_id": str}}
        try:
            err_obj = r.json().get("error") or {}
            msg = err_obj.get("message") or r.text[:400]
        except Exception:  # noqa: BLE001
            msg = r.text[:400]
        return None, f"reapi http {r.status_code}: {msg}"
    data = r.json()
    tid = data.get("id")
    if not tid:
        return None, f"reapi no id in response: {data}"
    return tid, None


def fetch_task(task_id: str) -> dict:
    """Poll reAPI task status. Returns a dict shaped like the old MiniMax response so
    render_one_scene doesn't need to change: {status, content: {url}, error: {message}}.

    reAPI's raw shape is {id, status: processing|completed|failed, output: {video_urls},
    error: {code, message}, usage: {credits}}. Statuses are remapped to succeeded/failed/running
    to match the legacy contract downstream.
    """
    try:
        r = requests.get(f"{BASE}/tasks/{task_id}", headers=headers(), timeout=30)
    except Exception:  # noqa: BLE001
        return {}
    if r.status_code != 200:
        return {}
    raw = r.json()
    status = raw.get("status", "processing")
    mapped_status = {
        "completed": "succeeded",
        "processing": "running",
        "queued": "queued",
        "failed": "failed",
    }.get(status, status)
    out: dict = {"status": mapped_status}
    urls = (raw.get("output") or {}).get("video_urls") or []
    if urls:
        out["content"] = {"url": urls[0]}
    err = raw.get("error")
    if err:
        out["error"] = {"message": err.get("message", "generation failed")}
    return out


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


WATERMARK_PATH = ROOT / "watermark.png"


def apply_watermark(src: Path, dest: Path) -> tuple[bool, str]:
    """Overlay a semi-transparent Not Hollywood watermark on the bottom-right corner.

    ffmpeg overlay filter with a scaled logo pinned to (W-w-20, H-h-20) and reduced
    to 55% opacity via colorchannelmixer. Re-encodes video (libx264, CRF 20, veryfast)
    but stream-copies audio to keep the aac track intact.
    """
    if not WATERMARK_PATH.exists():
        # No watermark asset present — just copy through, don't fail the render.
        import shutil
        shutil.copy(src, dest)
        return True, ""
    try:
        # Scale the watermark to ~18% of the output width, then reduce alpha to 55%.
        # overlay is pinned 20px from the right/bottom edges.
        filter_complex = (
            "[1:v]scale=iw*0.65:-1,format=rgba,colorchannelmixer=aa=0.55[wm];"
            "[0:v][wm]overlay=W-w-20:H-h-20"
        )
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(src),
                "-i", str(WATERMARK_PATH),
                "-filter_complex", filter_complex,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(dest),
            ],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            return False, result.stderr.decode(errors="ignore")[-400:]
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _download_video(url: str, out_path: Path) -> tuple[bool, str]:
    try:
        with requests.get(url, timeout=180, stream=True) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as fp:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fp.write(chunk)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"download failed: {e}"


def _render_with_grok(
    scene_prompt: str,
    duration: int,
    resolution: str,
    ref_data_url: str | None,
    scene_out_path: Path,
    on_status,
) -> tuple[bool, str]:
    """Grok Imagine fallback path. Mirrors _render_with_minimax's control flow."""
    on_status("grok_submitting")
    request_id, err = submit_grok(scene_prompt, duration, resolution, ref_data_url)
    if not request_id:
        return False, err or "grok submit failed"
    start = time.time()
    max_wait = 8 * 60
    while time.time() - start < max_wait:
        time.sleep(10)
        task = fetch_grok(request_id)
        status = task.get("status", "running")
        on_status(f"grok_{status}")
        if status == "succeeded":
            url = (task.get("content") or {}).get("url")
            if not url:
                return False, "grok returned no url"
            return _download_video(url, scene_out_path)
        if status == "failed":
            return False, "grok: " + (task.get("error") or {}).get("message", "generation failed")
    return False, "grok scene timed out after 8 minutes"


def render_one_scene(
    scene_prompt: str,
    duration: int,
    resolution: str,
    ref_data_url: str | None,
    scene_out_path: Path,
    on_status,
) -> tuple[bool, str]:
    """Submit one scene to MiniMax H3; on sensitivity rejection, fall back to Grok Imagine.

    on_status(status: str) called periodically for UI updates.
    Returns (ok, error).
    """
    task_id, err = submit_h3(scene_prompt, duration, resolution, ref_data_url)
    if not task_id:
        # Submit itself failed. If it's a sensitivity rejection, try Grok directly.
        if _is_sensitive_error(err or "") and GROK_API_KEY:
            return _render_with_grok(
                scene_prompt, duration, resolution, ref_data_url, scene_out_path, on_status
            )
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
            return _download_video(url, scene_out_path)
        if status == "failed":
            mm_err = (task.get("error") or {}).get("message", "generation failed")
            # MiniMax refused for policy reasons - retry with Grok Imagine.
            if _is_sensitive_error(mm_err) and GROK_API_KEY:
                return _render_with_grok(
                    scene_prompt, duration, resolution, ref_data_url, scene_out_path, on_status
                )
            return False, mm_err
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
    # ref_url is always a public https URL (reAPI requires it). The initial ref
    # comes from the /api/generate handler which saved the upload to FRAMES/.
    ref_url = initial_ref_data_url  # arg is now a URL, not a data URL; name kept for callers

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
            scene_prompt, dur, resolution, ref_url, scene_out,
            on_status=lambda s, i=i: _update_scene_status(i, s),
        )
        if not ok:
            job["status"] = "failed"
            job["error"] = f"scene {i+1}/{len(scenes)}: {err}"
            save_jobs(JOBS)
            return
        scene_paths.append(scene_out)

        # Extract last frame to chain into next scene for continuity. reAPI needs
        # a public URL, so we save the frame under /static/frames/ where the
        # FastAPI StaticFiles mount serves it publicly.
        if i + 1 < len(scenes):
            frame_name = f"{job_id}_frame_{i:02d}.png"
            frame_path = FRAMES / frame_name
            if extract_last_frame(scene_out, frame_path) and PUBLIC_ORIGIN:
                ref_url = f"{PUBLIC_ORIGIN}/static/frames/{frame_name}"
            else:
                # No PUBLIC_ORIGIN configured (dev) or frame extraction failed:
                # drop chaining and let the next scene render text-only.
                ref_url = None

    # All scenes rendered — stitch
    j = JOBS.get(job_id)
    if j:
        j["status"] = "stitching"
        save_jobs(JOBS)

    dest = VIDEOS / f"{job_id}.mp4"
    # Combine scenes into one intermediate file (unwatermarked), then watermark
    # as a final pass so multi-scene concat isn't slowed by re-encoding twice.
    unwatermarked = scene_dir / "combined.mp4"
    if len(scene_paths) == 1:
        # single scene — use as-is
        try:
            scene_paths[0].rename(unwatermarked)
        except Exception:
            import shutil
            shutil.copy(scene_paths[0], unwatermarked)
        ok, err = True, ""
    else:
        ok, err = concat_scenes(scene_paths, unwatermarked)

    if ok:
        # Apply Not Hollywood watermark as final step.
        ok, err = apply_watermark(unwatermarked, dest)

    j = JOBS.get(job_id)
    if not j:
        return
    if ok:
        j["status"] = "done"
        j["video"] = f"/static/videos/{job_id}.mp4"
        j["finished_at"] = time.time()
    else:
        j["status"] = "failed"
        j["error"] = f"finalize failed: {err}"
    save_jobs(JOBS)


@app.get("/api/config")
def public_config():
    """Frontend calls this once on load to get Supabase URL + anon key.

    Both values are safe to expose (the anon key is designed to be shipped to
    browsers and Supabase Row-Level Security is the real gate). Returning them
    from the backend means we don't have to hard-code them in app.js and can
    swap Supabase projects via env var without rebuilding.
    """
    return {
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
        "auth_required": bool(SUPABASE_URL and SUPABASE_ANON_KEY),
    }


@app.post("/api/generate")
async def generate(
    request: Request,
    prompt: str = Form(...),
    duration: int = Form(6),
    resolution: str = Form("768P"),
    reference: UploadFile | None = File(None),
):
    # Auth gate: require a signed-in Supabase user. Falls back to the legacy
    # shared password if SITE_PASSWORD is set (break-glass only).
    user_id: str | None = None
    user_email: str | None = None
    if SUPABASE_URL:
        claims = require_user(request)
        user_id = claims.get("sub")
        user_email = claims.get("email")
    elif SITE_PASSWORD:
        supplied = request.headers.get("X-Site-Password", "")
        if supplied != SITE_PASSWORD:
            raise HTTPException(401, "password required or incorrect")
    if not prompt.strip():
        raise HTTPException(400, "prompt is empty")
    if duration < 4 or duration > 600:
        raise HTTPException(400, "duration must be between 4 and 600 seconds")
    if resolution not in ("768P", "1080P"):
        raise HTTPException(400, "resolution must be 768P or 1080P")
    scenes_plan = plan_scenes(duration)
    job_id = uuid.uuid4().hex[:12]

    # Save uploaded reference under /static/frames/ so reAPI can fetch it.
    ref_url: str | None = None
    if reference is not None:
        raw = await reference.read()
        if len(raw) > 8 * 1024 * 1024:
            raise HTTPException(400, "reference image too large (max 8MB)")
        # Normalize extension by content-type; default to .png
        mime = (reference.content_type or "image/png").lower()
        ext = "jpg" if "jpeg" in mime or "jpg" in mime else "png"
        ref_name = f"{job_id}_upload.{ext}"
        (FRAMES / ref_name).write_bytes(raw)
        if PUBLIC_ORIGIN:
            ref_url = f"{PUBLIC_ORIGIN}/static/frames/{ref_name}"
        # If PUBLIC_ORIGIN is unset (dev with no public URL), we silently drop
        # the reference. Better than sending a data URL that reAPI will reject.
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
        "user_id": user_id,
        "user_email": user_email,
    }
    save_jobs(JOBS)
    Thread(target=multi_scene_worker, args=(job_id, ref_url), daemon=True).start()
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    # If Supabase auth is configured, only allow the job owner to see it.
    # Anonymous jobs (created before auth was enabled) stay accessible for
    # backward compat with any user still polling them.
    if SUPABASE_URL and job.get("user_id"):
        claims = get_user(request)
        caller = claims.get("sub") if claims else None
        if caller != job["user_id"]:
            raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs")
def list_jobs(request: Request):
    """Return jobs owned by the caller, most recent first.

    - When Supabase auth is enabled and the caller is signed in, returns only
      that user's jobs.
    - When Supabase auth is enabled but the caller is anonymous, returns an
      empty list (no cross-user leakage on the anonymous homepage).
    - When Supabase auth is not configured, returns all jobs (legacy behavior).
    """
    items = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
    if SUPABASE_URL:
        claims = get_user(request)
        caller = claims.get("sub") if claims else None
        if caller is None:
            return []
        items = [j for j in items if j.get("user_id") == caller]
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
