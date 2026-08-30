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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import require_user, get_user
from prompt_expander import expand_prompt
from franchise_ref import (
    resolve_franchise_ref,
    extract_show_title,
    slugify as _slugify_title,
    search_candidates_duckduckgo,
    _download_and_validate,
)

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

# Testing kill-switch: when AUTH_DISABLED=1 the frontend hides sign-in/signup
# and /api/generate accepts anonymous requests. Set to "" (unset) to re-enable
# auth without touching any other env var. Supabase env vars can remain set;
# they're simply ignored on the auth path while this is on.
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "").strip() in {"1", "true", "yes", "on"}

# ────────────────────────────────────────────────────────────────────
# Stripe checkout config. STRIPE_SECRET_KEY is the only required var;
# the four STRIPE_PRICE_* env vars point at the recurring Stripe Price
# IDs Zach creates in the Stripe dashboard for each credit pack.
# ────────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_STARTER = os.environ.get("STRIPE_PRICE_STARTER", "")
STRIPE_PRICE_STUDIO = os.environ.get("STRIPE_PRICE_STUDIO", "")
STRIPE_PRICE_FEATURE = os.environ.get("STRIPE_PRICE_FEATURE", "")
STRIPE_PRICE_BLOCKBUSTER = os.environ.get("STRIPE_PRICE_BLOCKBUSTER", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# ────────────────────────────────────────────────────────────────────
# Transactional email via Resend. Optional — if RESEND_API_KEY is unset
# we simply skip sending the completion email (job still finishes). Set
# EMAIL_FROM to a verified Resend sender, e.g. "Not Hollywood <hello@
# nothollywood.ai>"; falls back to onboarding@resend.dev for local dev.
# ────────────────────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Not Hollywood <onboarding@resend.dev>")
SITE_URL = os.environ.get("SITE_URL", "https://www.nothollywood.ai").rstrip("/")

PACKS = {
    "starter":     {"price_id": STRIPE_PRICE_STARTER,     "credits": 75,   "dollars": 15},
    "studio":      {"price_id": STRIPE_PRICE_STUDIO,      "credits": 500,  "dollars": 75},
    "feature":     {"price_id": STRIPE_PRICE_FEATURE,     "credits": 1800, "dollars": 250},
    "blockbuster": {"price_id": STRIPE_PRICE_BLOCKBUSTER, "credits": 3600, "dollars": 500},
}
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
FRANCHISE_REFS = STATIC / "franchise-refs"  # curated cast reference frames
                                            # one per franchise slug
                                            # served publicly at
                                            # /static/franchise-refs/<slug>.png

STATIC.mkdir(exist_ok=True)
VIDEOS.mkdir(exist_ok=True)
THUMBS.mkdir(exist_ok=True)
SCENES.mkdir(exist_ok=True)
FRAMES.mkdir(exist_ok=True)
FRANCHISE_REFS.mkdir(exist_ok=True)


# Franchise reference-pack lookup lives in franchise_ref.py. Given a prompt
# and PUBLIC_ORIGIN, it returns a dict {url, slug, title, source} pointing to
# a cast reference frame. It reads the on-disk cache first (pre-committed
# stills like seinfeld.png / the-office.png), then falls through to web
# image search (SerpAPI), then AI image generation (OpenAI), caching each
# result on disk under FRANCHISE_REFS/<slug>.<ext> for future runs. Every
# subsystem is optional — missing keys just skip that step and fall through.

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


# Poll intervals. Tighter = faster wall-clock because we detect completion
# sooner. reAPI and Grok Imagine both tolerate rapid polling comfortably at
# 3s. We also apply a mild backoff after the first minute since long-running
# jobs are unlikely to complete on the very next poll and rapid polling only
# helps near the end.
POLL_FAST_S = 3      # first 60s of a scene
POLL_SLOW_S = 5      # after 60s
POLL_SWITCH_S = 60   # when to switch from fast to slow


def _poll_interval(elapsed: float) -> float:
    return POLL_FAST_S if elapsed < POLL_SWITCH_S else POLL_SLOW_S


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
    submit_start = time.time()
    request_id, err = submit_grok(scene_prompt, duration, resolution, ref_data_url)
    submit_took = time.time() - submit_start
    if not request_id:
        return False, err or "grok submit failed"
    print(f"[render] grok submit ok request_id={request_id} in {submit_took:.1f}s")
    start = time.time()
    max_wait = 8 * 60
    while time.time() - start < max_wait:
        elapsed = time.time() - start
        time.sleep(_poll_interval(elapsed))
        task = fetch_grok(request_id)
        status = task.get("status", "running")
        on_status(f"grok_{status}")
        if status == "succeeded":
            url = (task.get("content") or {}).get("url")
            if not url:
                return False, "grok returned no url"
            print(f"[render] grok succeeded in {time.time()-start:.1f}s poll-time")
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
    submit_start = time.time()
    task_id, err = submit_h3(scene_prompt, duration, resolution, ref_data_url)
    submit_took = time.time() - submit_start
    if not task_id:
        # Submit itself failed. If it's a sensitivity rejection, try Grok directly.
        if _is_sensitive_error(err or "") and GROK_API_KEY:
            return _render_with_grok(
                scene_prompt, duration, resolution, ref_data_url, scene_out_path, on_status
            )
        return False, err or "submit failed"
    print(f"[render] h3 submit ok task_id={task_id} in {submit_took:.1f}s")

    start = time.time()
    max_wait = 8 * 60
    polls = 0
    while time.time() - start < max_wait:
        elapsed = time.time() - start
        time.sleep(_poll_interval(elapsed))
        polls += 1
        task = fetch_task(task_id)
        status = task.get("status", "running")
        on_status(status)
        if status == "succeeded":
            url = (task.get("content") or {}).get("url")
            if not url:
                return False, "no url returned"
            dl_start = time.time()
            ok, msg = _download_video(url, scene_out_path)
            print(f"[render] h3 succeeded poll_time={time.time()-start:.1f}s "
                  f"polls={polls} download={time.time()-dl_start:.1f}s")
            return ok, msg
        if status == "failed":
            mm_err = (task.get("error") or {}).get("message", "generation failed")
            # MiniMax refused for policy reasons - retry with Grok Imagine.
            if _is_sensitive_error(mm_err) and GROK_API_KEY:
                return _render_with_grok(
                    scene_prompt, duration, resolution, ref_data_url, scene_out_path, on_status
                )
            return False, mm_err
    return False, "scene timed out after 8 minutes"


def send_completion_email(user_email: str, job: dict) -> None:
    """Best-effort email notification when a render finishes.

    No-op if RESEND_API_KEY is unset or user_email is missing. Never raises —
    a bad email must not fail a completed render. Logs to stdout so Railway
    captures the outcome.
    """
    if not RESEND_API_KEY or not user_email:
        return
    try:
        job_id = job.get("id", "")
        status = job.get("status", "")
        duration = job.get("duration", 0)
        prompt = (job.get("prompt") or "").strip()
        preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
        video_path = job.get("video") or ""
        # Both buttons deep-link to the app so the browser reuses an already-open
        # tab. Previously "Watch it now" pointed at the raw .mp4, which browsers
        # always open in a new tab because it's a file, not a page. The app
        # handles the #job-<id> fragment on load and scrolls the tile into view.
        library_url = f"{SITE_URL}/#job-{job_id}"
        video_url = library_url
        # We still expose the raw .mp4 as a plain-text fallback for cases where
        # the app URL is unreachable (e.g. app offline). Only used in the text/plain
        # body, not in any button.
        raw_video_url = f"{SITE_URL}{video_path}" if video_path else ""

        if status == "done":
            subject = f"Your Not Hollywood render is ready — {duration}s"
            heading = "Your show is ready."
            body_html = (
                f'<p style="margin:0 0 12px;font-size:15px;color:#e8e8e8;">'
                f'“{preview}”</p>'
                f'<p style="margin:0 0 20px;font-size:14px;color:#a8a8a8;">'
                f'{duration} seconds · {job.get("resolution", "768P")}</p>'
                f'<p style="margin:0 0 24px;">'
                f'<a href="{library_url}" '
                f'style="display:inline-block;background:#e5322f;color:#fff;'
                f'text-decoration:none;padding:12px 22px;border-radius:6px;'
                f'font-weight:600;">Watch in Not Hollywood</a></p>'
                + (
                    f'<p style="margin:0;font-size:13px;color:#7a7a7a;">'
                    f'Direct video file: '
                    f'<a href="{raw_video_url}" style="color:#7a7a7a;">download .mp4</a></p>'
                    if raw_video_url else ""
                )
            )
            plain = (
                f"Your Not Hollywood render is ready.\n\n"
                f'"{preview}"\n'
                f"{duration} seconds · {job.get('resolution', '768P')}\n\n"
                f"Open in Not Hollywood: {library_url}\n"
                + (f"Direct video file: {raw_video_url}\n" if raw_video_url else "")
            )
        elif status == "failed":
            err = job.get("error", "unknown error")
            subject = "Your Not Hollywood render didn\u2019t finish"
            heading = "Your render didn\u2019t finish."
            body_html = (
                f'<p style="margin:0 0 12px;font-size:15px;color:#e8e8e8;">'
                f'“{preview}”</p>'
                f'<p style="margin:0 0 20px;font-size:14px;color:#ff8a8a;">'
                f'Error: {err[:200]}</p>'
                f'<p style="margin:0 0 24px;font-size:14px;color:#a8a8a8;">'
                f'Your credits were refunded. Try again from your library.</p>'
                f'<p style="margin:0;">'
                f'<a href="{library_url}" '
                f'style="display:inline-block;background:#333;color:#fff;'
                f'text-decoration:none;padding:12px 22px;border-radius:6px;'
                f'font-weight:600;">Open library</a></p>'
            )
            plain = (
                f"Your Not Hollywood render didn't finish.\n\n"
                f'"{preview}"\n'
                f"Error: {err[:200]}\n\n"
                f"Your credits were refunded.\n"
                f"Library: {library_url}\n"
            )
        else:
            return  # only email on terminal states

        html = (
            f'<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;'
            f'background:#0b0b0b;padding:40px 20px;">'
            f'<div style="max-width:520px;margin:0 auto;background:#141414;'
            f'border-radius:12px;padding:36px 32px;">'
            f'<div style="font-weight:800;font-size:20px;letter-spacing:.02em;'
            f'color:#e5322f;margin-bottom:24px;">NOT HOLLYWOOD</div>'
            f'<h1 style="margin:0 0 20px;font-size:22px;color:#fff;">{heading}</h1>'
            f'{body_html}'
            f'</div>'
            f'<p style="max-width:520px;margin:16px auto 0;text-align:center;'
            f'font-size:12px;color:#5a5a5a;">'
            f'You received this because you rendered a video at nothollywood.ai.</p>'
            f'</div>'
        )

        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM,
                "to": [user_email],
                "subject": subject,
                "html": html,
                "text": plain,
            },
            timeout=15,
        )
        if r.status_code >= 300:
            print(f"[email {job_id}] resend {r.status_code}: {r.text[:200]}")
        else:
            print(f"[email {job_id}] sent to {user_email}")
    except Exception as e:
        print(f"[email {job.get('id','?')}] send failed: {e}")


# Max scenes rendering concurrently. reAPI can handle several jobs in parallel;
# this bounds our exposure to their rate limits and to token bucket bursts.
# Env-tunable so we can back off if reAPI starts 429ing under load.
SCENE_CONCURRENCY = int(os.environ.get("SCENE_CONCURRENCY", "4"))
# Threshold at/below which we prefer sequential frame-chaining over parallel
# rendering. Chaining preserves character continuity but N× wall-clock.
# For short renders (≤60s) continuity dominates; for longer renders parallel
# wins because the wall-clock penalty is too painful.
SEQUENTIAL_MAX_SECONDS = int(os.environ.get("SEQUENTIAL_MAX_SECONDS", "60"))


def multi_scene_worker(job_id: str, initial_ref_data_url: str | None) -> None:
    """Render scenes sequentially with frame-chaining, then concat.

    Every multi-scene render uses last-frame→first-frame handoff so scene[i+1]
    starts from scene[i]'s final frame. This preserves character continuity
    (same dog stays the same dog, same actor stays the same actor).

    Wall clock: N × slowest scene. Users have said this is acceptable — the
    previous parallel path wrecked continuity by making every scene render
    fresh from the initial reference (or nothing).
    """

    job = JOBS.get(job_id)
    if not job:
        return
    scenes = job["scenes_plan"]  # list of ints (durations)
    prompt = job["prompt"]
    resolution = job["resolution"]
    total_duration = sum(scenes)
    # User feedback: parallel rendering wrecks character continuity ("keeps
    # starting each scene over with the reference frame"). Sequential frame-chain
    # is now the ONLY multi-scene path. Single-scene renders skip the loop entirely.
    use_sequential = len(scenes) > 1

    scene_dir = SCENES / job_id
    scene_dir.mkdir(exist_ok=True)

    # Every scene starts from the same initial ref (the upload, if any).
    ref_url = initial_ref_data_url  # arg name is legacy; already a public https URL

    # If the user did NOT upload a reference AND did NOT pick one from the
    # /api/plan approval flow AND the prompt names a TV show or movie, resolve
    # a cast reference frame now (cache hit is instant; DDG search or Grok
    # Imagine may take a few seconds). This is intentionally done inside the
    # worker thread so /api/generate stays fast. Failure just falls through to
    # no-reference generation — same as before this feature.
    # Skip when ref_source == 'chosen' (user already approved a pick).
    if not ref_url and job.get("ref_source") != "chosen":
        try:
            info = resolve_franchise_ref(
                prompt,
                franchise_refs_dir=FRANCHISE_REFS,
                public_origin=PUBLIC_ORIGIN,
            )
        except Exception as e:
            print(f"[worker] franchise ref resolve crashed: {type(e).__name__}: {e}")
            info = None
        if info:
            ref_url = info["url"]
            job["ref_url"] = ref_url
            job["ref_source"] = f"franchise:{info['source']}"
            job["ref_title"] = info.get("title")
            job["ref_slug"] = info.get("slug")
            save_jobs(JOBS)

    # scene_states tracks the status string reported by reAPI for each scene
    # so we can compute an aggregate progress signal for the UI.
    scene_states: dict[int, str] = {i: "queued" for i in range(len(scenes))}
    scene_started_at: dict[int, float] = {}
    scene_finished_at: dict[int, float] = {}

    # Seed the scenes_state array once so the UI can render the scene list
    # from the moment the job is created, not after the first status callback.
    _now = time.time()
    _scenes_state_seed = [
        {
            "index": i,
            "duration": dur,
            "status": "queued",
            "started_at": 0.0,
            "finished_at": 0.0,
            "eta_seconds": max(20, min(180, int(dur * 5 + 10))),
        }
        for i, dur in enumerate(scenes)
    ]
    _job0 = JOBS.get(job_id)
    if _job0 is not None:
        _job0["scenes_state"] = _scenes_state_seed
        _job0["expansion_status"] = "expanding"
        save_jobs(JOBS)

    # ==== Character-aware prompt expansion ====
    # Before rendering, run the raw user prompt through an LLM pass that:
    #   - Identifies named characters (Cartman, Peter Griffin, Homer, etc.)
    #   - Writes a shared character bible + style guide
    #   - Produces one expanded prompt per scene, each self-contained with
    #     the style + character descriptions embedded inline
    # Falls back to the raw-prompt path if ANTHROPIC_API_KEY is unset or the
    # call fails; the render still works, just with lower character fidelity.
    # If the user pre-approved scene prompts via /api/plan, skip re-expanding.
    preapproved = job.get("preapproved_scene_prompts")
    if preapproved and len(preapproved) == len(scenes):
        scene_prompts_expanded: list[str] = list(preapproved)
        expansion = {
            "ok": True,
            "scenes": scene_prompts_expanded,
            "provider": "user-approved",
            "latency_ms": 0,
            "error": None,
            "style": None,
            "characters": None,
            "notes": "scene prompts approved by user in preview step",
        }
    else:
        expansion = expand_prompt(prompt, scenes)
        scene_prompts_expanded = expansion["scenes"]

    _jobx = JOBS.get(job_id)
    if _jobx is not None:
        _jobx["expansion_status"] = "expanded" if expansion["ok"] else "fallback"
        _jobx["expansion_provider"] = expansion["provider"]
        _jobx["expansion_latency_ms"] = expansion["latency_ms"]
        if expansion.get("error"):
            _jobx["expansion_error"] = expansion["error"]
        # Store the character bible so the UI (later) can surface it and so
        # debug endpoints show what the model actually saw.
        if expansion.get("style"):
            _jobx["expansion_style"] = expansion["style"]
        if expansion.get("characters"):
            _jobx["expansion_characters"] = expansion["characters"]
        if expansion.get("notes"):
            _jobx["expansion_notes"] = expansion["notes"]
        _jobx["expansion_scene_prompts"] = scene_prompts_expanded
        save_jobs(JOBS)

    def _publish_agg_status():
        """Write an aggregate progress snapshot back to JOBS[job_id].

        Publishes two shapes so the frontend has both a summary and detail:
          - scene_index / scene_total / scene_status / scene_eta_seconds
            (legacy single-bar contract, kept for backward compat with the
            existing progress-tick code path)
          - scenes_state: [{index, status, started_at, finished_at, eta_seconds}]
            (per-scene detail so the UI can render a real scene list)
        """
        j = JOBS.get(job_id)
        if not j:
            return
        done_count = sum(1 for s in scene_states.values() if s == "succeeded")
        failed_count = sum(1 for s in scene_states.values() if s == "failed")
        any_running = any(s == "running" for s in scene_states.values())
        j["scene_total"] = len(scenes)
        j["scene_done"] = done_count
        j["scene_failed"] = failed_count
        j["scene_running"] = sum(1 for s in scene_states.values() if s == "running")
        # Cap at N so the UI shows 'Scene N/N' during the final concat step
        # rather than snapping past the total.
        j["scene_index"] = min(done_count + (1 if any_running else 0), len(scenes))
        j["scene_status"] = "running" if any_running else (
            "succeeded" if done_count == len(scenes) else "queued"
        )
        # First running scene stamps the aggregate start time.
        if any_running and not j.get("scene_started_at"):
            j["scene_started_at"] = time.time()
            slowest = max(scenes) if scenes else 6
            j["scene_eta_seconds"] = max(30, min(180, int(slowest * 5 + 10)))
        j["status"] = "rendering"

        # Rebuild scenes_state from scratch so the UI always sees a consistent
        # snapshot. Cheap — N is at most ~15 for the longest renders.
        j["scenes_state"] = [
            {
                "index": i,
                "duration": scenes[i],
                "status": scene_states.get(i, "queued"),
                "started_at": scene_started_at.get(i, 0.0),
                "finished_at": scene_finished_at.get(i, 0.0),
                "eta_seconds": max(20, min(180, int(scenes[i] * 5 + 10))),
            }
            for i in range(len(scenes))
        ]
        save_jobs(JOBS)

    def _render_scene(idx: int, dur: int, scene_ref_url: str | None) -> tuple[int, bool, str, Path]:
        """Render one scene. Returns (idx, ok, err, out_path).

        scene_ref_url:
          - In parallel mode: same shared initial ref for every scene.
          - In sequential mode: previous scene's extracted last frame (or the
            initial ref for idx==0).
        """
        scene_out = scene_dir / f"scene_{idx:02d}.mp4"
        # Prefer the LLM-expanded per-scene prompt when available (style +
        # character bible embedded inline). Fall back to the raw prompt if
        # expansion produced fewer entries than expected — defensive.
        if idx < len(scene_prompts_expanded) and scene_prompts_expanded[idx]:
            scene_prompt = scene_prompts_expanded[idx]
        else:
            scene_prompt = prompt
            if len(scenes) > 1:
                scene_prompt = (
                    f"Scene {idx+1} of {len(scenes)} in a continuous story. "
                    f"Maintain the exact same characters, wardrobe, setting, and visual style throughout. "
                    f"{prompt}"
                )

        def _on_status(status: str):
            prev = scene_states.get(idx)
            scene_states[idx] = status
            if status == "running" and idx not in scene_started_at:
                scene_started_at[idx] = time.time()
            _publish_agg_status()

        ok, err = render_one_scene(
            scene_prompt, dur, resolution, scene_ref_url, scene_out,
            on_status=_on_status,
        )
        # Terminal state update (render_one_scene doesn't call on_status on exit)
        scene_states[idx] = "succeeded" if ok else "failed"
        scene_finished_at[idx] = time.time()
        _publish_agg_status()
        return idx, ok, err, scene_out

    scene_paths_by_idx: dict[int, Path] = {}
    first_failure: tuple[int, str] | None = None

    # ==== SEQUENTIAL FRAME-CHAIN ====
    # For multi-scene renders: each scene's opening frame is the previous
    # scene's last frame. For single-scene renders: just render once with the
    # initial ref (if any).
    current_ref = ref_url  # initial upload if any, else None
    j = JOBS.get(job_id)
    if j is not None:
        j["render_mode"] = "sequential" if use_sequential else "single"
        save_jobs(JOBS)
    for i, dur in enumerate(scenes):
        try:
            idx, ok, err, out_path = _render_scene(i, dur, current_ref)
        except Exception as e:  # noqa: BLE001
            ok, err, out_path = False, f"scene thread crashed: {e}", scene_dir / f"scene_{i:02d}.mp4"
            idx = i
        if not ok:
            if first_failure is None:
                first_failure = (idx, err)
            break  # stop the chain — no last frame to chain from
        scene_paths_by_idx[idx] = out_path
        # Extract last frame for next scene's ref. If extraction or hosting
        # fails, we degrade to "no ref" for the next scene rather than aborting.
        if i + 1 < len(scenes):
            frame_name = f"chain_{job_id}_{i:02d}.jpg"
            frame_path = FRAMES / frame_name
            extracted = extract_last_frame(out_path, frame_path)
            if extracted and PUBLIC_ORIGIN:
                current_ref = f"{PUBLIC_ORIGIN}/static/frames/{frame_name}"
            else:
                # No public origin means reAPI can't fetch our frame. Keep going
                # with prompt-only continuity — still better than random.
                current_ref = None
                if not PUBLIC_ORIGIN:
                    print(f"[render {job_id}] no PUBLIC_ORIGIN set \u2014 frame-chain degraded to prompt-only")

    if first_failure is not None:
        idx, err = first_failure
        job = JOBS.get(job_id)
        if job:
            job["status"] = "failed"
            job["error"] = f"scene {idx+1}/{len(scenes)}: {err}"
            job["finished_at"] = time.time()
            save_jobs(JOBS)
            send_completion_email(job.get("user_email") or "", job)
        return

    # Preserve original scene order for concat regardless of completion order.
    scene_paths: list[Path] = [scene_paths_by_idx[i] for i in range(len(scenes))]

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

    # Watermark is best-effort — if ffmpeg is missing or the overlay filter
    # fails, we ship the unwatermarked scene rather than losing the whole render.
    # This protects against Railway rebuilds where the apt package didn't
    # install cleanly and against edge-case ffmpeg filter errors.
    if ok:
        wm_ok, wm_err = apply_watermark(unwatermarked, dest)
        if not wm_ok:
            print(f"[render {job_id}] watermark failed ({wm_err[:200]}) \u2014 shipping unwatermarked")
            try:
                import shutil
                shutil.copy(unwatermarked, dest)
            except Exception as e:
                ok, err = False, f"copy-unwatermarked failed: {e}"

    j = JOBS.get(job_id)
    if not j:
        return
    if ok:
        j["status"] = "done"
        j["video"] = f"/static/videos/{job_id}.mp4"
        j["finished_at"] = time.time()
        # Persist to user library (best-effort; never fails the render).
        # Anonymous jobs (no user_id) are skipped inside save_render_to_library.
        try:
            import library as _lib
            library_meta = {
                "title": j.get("franchise_title"),
                "slug": j.get("franchise_slug"),
                "duration": j.get("duration") or sum(j.get("scenes") or []),
                "resolution": j.get("resolution"),
                "scene_count": len(j.get("scenes") or [1]),
                "scenes": j.get("scenes") or [],
                "franchise_ref_url": j.get("ref_url_used") or j.get("franchise_ref_url"),
            }
            saved = _lib.save_render_to_library(
                job_id=job_id,
                user_id=j.get("user_id") or "",
                prompt=j.get("prompt") or "",
                video_path=dest,
                meta=library_meta,
            )
            if saved:
                j["saved_to_library"] = True
                save_jobs(JOBS)
        except Exception as e:
            print(f"[render {job_id}] library save exception (non-fatal): {e}")
    else:
        j["status"] = "failed"
        j["error"] = f"finalize failed: {err}"
        j["finished_at"] = time.time()
    save_jobs(JOBS)
    send_completion_email(j.get("user_email") or "", j)


@app.post("/api/create-checkout-session")
async def create_checkout_session(request: Request):
    """Create a Stripe Checkout session for a credit pack.

    Request body: {"pack": "starter"|"studio"|"feature"|"blockbuster",
                   "success_url": str, "cancel_url": str}

    Response: {"url": "https://checkout.stripe.com/..."} — the frontend
    redirects the browser to this URL.

    Requires STRIPE_SECRET_KEY plus a STRIPE_PRICE_<PACK> env var per pack.
    If Supabase auth is enabled and the caller supplied a JWT, the user's
    id/email are attached to the Stripe session's client_reference_id and
    customer_email so we can credit them via webhook.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe not configured on this deployment")

    body = await request.json()
    pack_id = (body.get("pack") or "").lower()
    pack = PACKS.get(pack_id)
    if not pack:
        raise HTTPException(status_code=400, detail=f"Unknown pack: {pack_id}")
    if not pack["price_id"]:
        raise HTTPException(
            status_code=503,
            detail=f"Stripe price ID for '{pack_id}' pack is not configured",
        )

    success_url = body.get("success_url") or f"{request.base_url}?checkout=success&pack={pack_id}"
    cancel_url = body.get("cancel_url") or f"{request.base_url}pricing.html?checkout=cancelled"

    # Optional Supabase JWT → user id/email for webhook attribution
    user_id = ""
    user_email = ""
    try:
        import auth as _auth  # local module
        claims = _auth.get_user(request)
        if claims:
            user_id = claims.get("sub", "") or ""
            user_email = claims.get("email", "") or ""
    except Exception as e:
        # Non-fatal — checkout can still proceed anonymously; webhook will just
        # need to match by email or session_id later.
        print(f"[stripe] JWT verify failed (proceeding anon): {e}")

    # Build the Stripe API form payload (application/x-www-form-urlencoded)
    form = {
        "mode": "payment",
        "line_items[0][price]": pack["price_id"],
        "line_items[0][quantity]": "1",
        "success_url": success_url + ("&" if "?" in success_url else "?") + "session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": cancel_url,
        "metadata[pack]": pack_id,
        "metadata[credits]": str(pack["credits"]),
        "payment_intent_data[metadata][pack]": pack_id,
        "payment_intent_data[metadata][credits]": str(pack["credits"]),
    }
    if user_id:
        form["client_reference_id"] = user_id
        form["metadata[user_id]"] = user_id
        form["payment_intent_data[metadata][user_id]"] = user_id
    if user_email:
        form["customer_email"] = user_email

    r = requests.post(
        "https://api.stripe.com/v1/checkout/sessions",
        auth=(STRIPE_SECRET_KEY, ""),
        data=form,
        timeout=15,
    )
    if not r.ok:
        # Bubble Stripe's error message up so it's visible in the browser alert
        err = r.json().get("error", {}).get("message", r.text) if r.text else f"HTTP {r.status_code}"
        print(f"[stripe] checkout create failed: {err}")
        raise HTTPException(status_code=502, detail=f"Stripe: {err}")

    session = r.json()
    return {"url": session["url"], "session_id": session["id"]}


# ────────────────────────────────────────────────────────────────────
# Stripe webhook → grants credits on successful payment.
#
# Ship-first storage strategy (matches the rest of this app):
#   1. Preferred: upsert into Supabase `user_credits` table via service-role
#      key. Zach must run the SQL migration below in Supabase once, and set
#      SUPABASE_SERVICE_ROLE_KEY on Railway. Idempotent via stripe_events log.
#   2. Fallback (no service role yet): append to /tmp/credit_ledger.jsonl so
#      the payment is never lost, plus /tmp/stripe_events.json for dedupe.
#      Zach can replay these into Supabase later.
#
# SQL to run in Supabase SQL editor once:
#   create table if not exists user_credits (
#     user_id uuid primary key references auth.users(id) on delete cascade,
#     balance int not null default 0,
#     updated_at timestamptz not null default now()
#   );
#   create table if not exists stripe_events (
#     event_id text primary key,
#     received_at timestamptz not null default now()
#   );
#   alter table user_credits enable row level security;
#   create policy "users read own credits" on user_credits
#     for select using (auth.uid() = user_id);
# ────────────────────────────────────────────────────────────────────
LEDGER_FILE = Path("/tmp/credit_ledger.jsonl")
EVENTS_FILE = Path("/tmp/stripe_events.json")


def _load_seen_events() -> set:
    if EVENTS_FILE.exists():
        try:
            return set(json.loads(EVENTS_FILE.read_text()))
        except Exception:
            return set()
    return set()


def _remember_event(event_id: str) -> None:
    seen = _load_seen_events()
    seen.add(event_id)
    # cap at last 10k events to keep file size bounded
    if len(seen) > 10000:
        seen = set(list(seen)[-10000:])
    EVENTS_FILE.write_text(json.dumps(list(seen)))


def _verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Verify Stripe's `Stripe-Signature` header per
    https://stripe.com/docs/webhooks#verify-official-libraries.

    Header shape: `t=<timestamp>,v1=<sig>,v1=<sig>,...`. Signed payload is
    `{timestamp}.{body}`, HMAC-SHA256 with the endpoint secret, hex-encoded.
    """
    import hmac
    import hashlib

    if not sig_header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    timestamp = parts.get("t")
    signatures = [v for k, v in
                  (p.split("=", 1) for p in sig_header.split(",") if "=" in p)
                  if k == "v1"]
    if not timestamp or not signatures:
        return False

    # Reject events older than 5 minutes to defend against replay
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
    except ValueError:
        return False

    signed = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def _grant_credits_via_supabase(user_id: str, credits: int) -> tuple[bool, str]:
    """Atomically add `credits` to user_credits.balance via PostgREST.

    Uses an RPC-style upsert with `Prefer: resolution=merge-duplicates` so a
    first-time buyer gets their row created, and a returning buyer gets their
    balance incremented. Requires SUPABASE_SERVICE_ROLE_KEY.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return False, "supabase service role not configured"

    # Read current balance (service role bypasses RLS)
    read = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_credits",
        params={"user_id": f"eq.{user_id}", "select": "balance"},
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=10,
    )
    if not read.ok:
        return False, f"read failed: {read.status_code} {read.text[:200]}"
    rows = read.json()
    current = rows[0]["balance"] if rows else 0
    new_balance = current + credits

    up = requests.post(
        f"{SUPABASE_URL}/rest/v1/user_credits",
        params={"on_conflict": "user_id"},
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        json={"user_id": user_id, "balance": new_balance},
        timeout=10,
    )
    if not up.ok:
        return False, f"upsert failed: {up.status_code} {up.text[:200]}"
    return True, f"balance {current} → {new_balance}"


def _append_ledger(entry: dict) -> None:
    """Always-on audit log. Every successful payment gets a row here even
    when Supabase upsert succeeds, so we have a paper trail if the DB is
    ever wrong."""
    entry["logged_at"] = int(time.time())
    with LEDGER_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    """Stripe → us. Called on `checkout.session.completed` (and others).

    Configure in Stripe dashboard → Developers → Webhooks:
      Endpoint URL: https://www.nothollywood.ai/api/stripe-webhook
      Events to send: checkout.session.completed
      Then copy the signing secret (whsec_...) into STRIPE_WEBHOOK_SECRET on Railway.
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        # Refuse to process anything until the secret is set — otherwise
        # anyone could POST here and mint credits.
        raise HTTPException(status_code=503, detail="STRIPE_WEBHOOK_SECRET not configured")

    if not _verify_stripe_signature(payload, sig, STRIPE_WEBHOOK_SECRET):
        print(f"[stripe-webhook] signature verification failed (sig header: {sig[:40]}...)")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_id = event.get("id", "")
    event_type = event.get("type", "")

    # Idempotency: Stripe retries webhooks, so dedupe by event id.
    seen = _load_seen_events()
    if event_id and event_id in seen:
        print(f"[stripe-webhook] duplicate event {event_id} ({event_type}) — ignoring")
        return {"received": True, "duplicate": True}

    if event_type != "checkout.session.completed":
        # Ack any other event so Stripe stops retrying.
        print(f"[stripe-webhook] event {event_id} type={event_type} — ignoring")
        if event_id:
            _remember_event(event_id)
        return {"received": True, "handled": False}

    session = event["data"]["object"]
    metadata = session.get("metadata") or {}
    pack_id = metadata.get("pack", "")
    credits = int(metadata.get("credits", 0) or 0)
    user_id = metadata.get("user_id") or session.get("client_reference_id") or ""
    user_email = session.get("customer_email") or (session.get("customer_details") or {}).get("email") or ""
    amount_total = session.get("amount_total", 0)

    # Belt-and-suspenders: if metadata is missing (e.g. checkout created
    # outside our endpoint), map the price back to a pack.
    if not credits and session.get("line_items"):
        # amount_total is in cents, packs table has dollar prices
        for pid, cfg in PACKS.items():
            if amount_total == cfg["dollars"] * 100:
                pack_id = pid
                credits = cfg["credits"]
                break

    ledger_entry = {
        "event_id": event_id,
        "session_id": session.get("id"),
        "user_id": user_id,
        "user_email": user_email,
        "pack": pack_id,
        "credits": credits,
        "amount_total_cents": amount_total,
        "status": "pending",
    }

    if not credits:
        ledger_entry["status"] = "failed"
        ledger_entry["error"] = "could not resolve credit amount"
        _append_ledger(ledger_entry)
        if event_id:
            _remember_event(event_id)
        print(f"[stripe-webhook] {event_id}: cannot resolve credits (pack={pack_id})")
        return {"received": True, "handled": False, "error": "unresolvable credits"}

    if not user_id:
        ledger_entry["status"] = "orphaned"
        ledger_entry["error"] = "no user_id — anonymous checkout (email in ledger)"
        _append_ledger(ledger_entry)
        if event_id:
            _remember_event(event_id)
        print(f"[stripe-webhook] {event_id}: anonymous purchase, {credits} credits owed to {user_email}")
        return {"received": True, "handled": True, "warning": "orphaned, see ledger"}

    ok, detail = _grant_credits_via_supabase(user_id, credits)
    ledger_entry["status"] = "granted" if ok else "queued"
    ledger_entry["grant_detail"] = detail
    _append_ledger(ledger_entry)
    if event_id:
        _remember_event(event_id)

    if ok:
        print(f"[stripe-webhook] granted {credits} credits to user {user_id}: {detail}")
    else:
        # Return 200 anyway — the ledger has the record and we don't want
        # Stripe to retry forever if e.g. Supabase is temporarily down.
        # Zach can replay from ledger.
        print(f"[stripe-webhook] queued {credits} credits for {user_id} (supabase failed: {detail})")

    return {"received": True, "handled": True, "credits": credits}


@app.get("/api/credits")
def get_credits(request: Request):
    """Return the signed-in user's credit balance.

    Reads from user_credits (RLS enforced via anon-key auth); when service role
    isn't configured yet, returns 0 with a stub flag so the UI can show a
    friendly "credits sync pending" message.
    """
    claims = get_user(request)
    if not claims:
        raise HTTPException(status_code=401, detail="sign in required")
    user_id = claims.get("sub", "")
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {"balance": 0, "stub": True}
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_credits",
        params={"user_id": f"eq.{user_id}", "select": "balance"},
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=10,
    )
    if not r.ok:
        return {"balance": 0, "error": r.text[:200]}
    rows = r.json()
    return {"balance": rows[0]["balance"] if rows else 0}


@app.get("/api/config")
def public_config():
    """Frontend calls this once on load to get Supabase URL + anon key.

    Both values are safe to expose (the anon key is designed to be shipped to
    browsers and Supabase Row-Level Security is the real gate). Returning them
    from the backend means we don't have to hard-code them in app.js and can
    swap Supabase projects via env var without rebuilding.
    """
    return {
        "supabase_url": "" if AUTH_DISABLED else SUPABASE_URL,
        "supabase_anon_key": "" if AUTH_DISABLED else SUPABASE_ANON_KEY,
        "auth_required": (not AUTH_DISABLED) and bool(SUPABASE_URL and SUPABASE_ANON_KEY),
        "auth_disabled": AUTH_DISABLED,
    }


@app.post("/api/plan")
async def plan(
    request: Request,
    prompt: str = Form(...),
    duration: int = Form(...),
):
    """Preview step BEFORE generation: return the show we detected, a set of
    candidate reference frames the user can pick from, and the storyboard
    (per-scene prompts) the LLM would send to the video model. The user
    approves or edits and then calls /api/generate with the chosen ref_url
    and scenes.

    Auth: uses require_user like /api/generate.
    """
    # Auth gate identical to /api/generate. Skip in disabled-auth testing mode.
    if not AUTH_DISABLED and SUPABASE_URL:
        require_user(request)
    elif SITE_PASSWORD:
        supplied = request.headers.get("X-Site-Password", "")
        if supplied != SITE_PASSWORD:
            raise HTTPException(401, "password required or incorrect")
    if not prompt.strip():
        raise HTTPException(400, "prompt is empty")
    if duration < 4 or duration > 600:
        raise HTTPException(400, "duration must be between 4 and 600 seconds")

    scenes_plan = plan_scenes(duration)

    # Show detection. Skip candidate search on a miss and return an empty
    # candidates list; the frontend will render the storyboard with a
    # 'no reference required' banner and let the user hit Generate anyway.
    title = extract_show_title(prompt)
    candidates: list[dict] = []
    slug: str | None = None
    if title:
        slug = _slugify_title(title)
        # Serve any pre-baked cache hit as the first option so users see
        # 'this is what we already have for this show' at the top.
        if PUBLIC_ORIGIN:
            for ext in ("png", "jpg", "webp"):
                cached = FRANCHISE_REFS / f"{slug}.{ext}"
                if cached.exists() and cached.stat().st_size >= 5_000:
                    candidates.append({
                        "url": f"{PUBLIC_ORIGIN}/static/franchise-refs/{cached.name}",
                        "thumbnail": f"{PUBLIC_ORIGIN}/static/franchise-refs/{cached.name}",
                        "width": 0,
                        "height": 0,
                        "source": "cache",
                    })
                    break
        # Then fetch 6 fresh DDG candidates (may add cached one for total of 7
        # -- frontend can dedupe if it wants).
        for c in search_candidates_duckduckgo(title, want=6):
            c["source"] = "search"
            candidates.append(c)

        # Fallback: if DDG returned nothing (common for animated/cartoon
        # shows like Rick and Morty, Family Guy, The Simpsons where DDG's
        # image results are anemic), generate a cast still with Grok
        # Imagine and add it as the sole candidate. Slow (~10s), so only
        # runs when we have zero real search hits.
        if not candidates:
            try:
                from franchise_ref import _generate_cast_still_xai
                got = _generate_cast_still_xai(title)
                if got:
                    raw, ext = got
                    fname = f"{slug}.{ext}"
                    dest = FRANCHISE_REFS / fname
                    dest.write_bytes(raw)
                    if PUBLIC_ORIGIN:
                        candidates.append({
                            "url": f"{PUBLIC_ORIGIN}/static/franchise-refs/{fname}",
                            "thumbnail": f"{PUBLIC_ORIGIN}/static/franchise-refs/{fname}",
                            "width": 0,
                            "height": 0,
                            "source": "generated",
                        })
            except Exception as e:
                print(f"[/api/plan] xai fallback failed for '{title}': {e}")

    # Storyboard: run the prompt expander now so the user sees the actual
    # per-scene prompts and can edit them before render.
    expansion = expand_prompt(prompt, scenes_plan)
    return {
        "title": title,
        "slug": slug,
        "scenes_plan": scenes_plan,
        "scene_prompts": expansion.get("scenes") or [prompt] * len(scenes_plan),
        "expansion_ok": expansion.get("ok", False),
        "expansion_provider": expansion.get("provider"),
        "style": expansion.get("style"),
        "characters": expansion.get("characters"),
        "notes": expansion.get("notes"),
        "candidates": candidates,
    }


@app.post("/api/plan/regenerate_refs")
async def regenerate_refs(
    request: Request,
    title: str = Form(...),
    variant: str = Form(""),
):
    """Re-run the DDG image search with a variant query so users can request
    fresh candidates when the initial set didn't have anything good.

    Variant appended to the query: e.g. 'group photo', 'promo shot', 'still',
    'poster'. Frontend rotates through these on repeated clicks.
    """
    if not AUTH_DISABLED and SUPABASE_URL:
        require_user(request)
    q = title.strip()
    if not q:
        raise HTTPException(400, "title is empty")
    # Compose an alternate query so DDG returns a different set of hits.
    alt_terms = {
        "group": "group photo",
        "promo": "promo photo",
        "still": "promotional still",
        "poster": "poster",
        "scene": "scene",
    }
    suffix = alt_terms.get(variant, "cast")
    # Bit of a hack: reach into franchise_ref by patching the query it builds.
    # Simplest way — do a direct duplicate of the search logic here so the
    # variant term flows through.
    from franchise_ref import (
        _DDG_UA,
        _DDG_JSON_URL,
        _ddg_get_vqd,
        REQUEST_TIMEOUT,
    )
    import requests as _rq
    session = _rq.Session()
    session.headers["User-Agent"] = _DDG_UA
    search_q = f"{q} {suffix}"
    vqd = _ddg_get_vqd(session, search_q)
    if not vqd:
        return {"candidates": []}
    try:
        r = session.get(
            _DDG_JSON_URL,
            params={"l": "us-en", "o": "json", "q": search_q, "vqd": vqd,
                    "f": ",,,,,", "p": "1"},
            timeout=REQUEST_TIMEOUT,
        )
        data = r.json() if r.status_code == 200 else {}
    except Exception:
        data = {}
    hits = data.get("results") or []
    out = []
    for hit in hits[:60]:
        url = hit.get("image")
        thumb = hit.get("thumbnail") or url
        w = int(hit.get("width") or 0)
        h = int(hit.get("height") or 0)
        if not url or not url.startswith("http"):
            continue
        if w and h and (w < 320 or h < 180):
            continue
        out.append({"url": url, "width": w, "height": h, "thumbnail": thumb, "source": "search"})
        if len(out) >= 6:
            break
    return {"candidates": out, "query_used": search_q}


@app.get("/api/library")
async def get_library(request: Request):
    """Return the current user's saved renders, newest first.

    Each item includes a short-lived signed video URL and (if we have one) a
    signed thumbnail URL. URLs expire in 1 hour, matching Supabase's default
    signed-URL policy. Anonymous callers get a 401.
    """
    claims = require_user(request)
    user_id = claims.get("sub") or ""
    if not user_id:
        raise HTTPException(401, "authentication required")
    import library as _lib
    rows = _lib.list_renders(user_id)
    out = []
    for row in rows:
        video_url = _lib.signed_url_for(row.get("storage_path") or "", ttl_seconds=3600)
        thumb_url = _lib.signed_url_for(row.get("thumb_path") or "", ttl_seconds=3600) if row.get("thumb_path") else None
        if not video_url:
            # Row exists but the storage object was purged or signing failed —
            # skip so the frontend doesn't render a broken tile.
            continue
        out.append({
            "id": row.get("id"),
            "prompt": row.get("prompt"),
            "title": row.get("title"),
            "duration": row.get("duration"),
            "resolution": row.get("resolution"),
            "scene_count": row.get("scene_count"),
            "created_at": row.get("created_at"),
            "video_url": video_url,
            "thumb_url": thumb_url,
            "bytes": row.get("bytes"),
        })
    return {"renders": out}


@app.delete("/api/library/{job_id}")
async def delete_from_library(job_id: str, request: Request):
    """Remove one of the caller's saved renders.

    Deletes both the DB row and storage objects. Ownership is enforced by
    matching user_id from the JWT against the row's user_id inside the
    library helper.
    """
    claims = require_user(request)
    user_id = claims.get("sub") or ""
    if not user_id:
        raise HTTPException(401, "authentication required")
    import library as _lib
    ok = _lib.delete_render(job_id, user_id)
    if not ok:
        raise HTTPException(500, "delete failed")
    return {"deleted": job_id}


@app.get("/api/_status/providers")
def provider_status():
    """Report which optional service providers are configured. Booleans only —
    NEVER returns secret values. Safe to expose publicly so we and the user
    can verify env-var wiring without shelling into Railway.
    """
    import franchise_ref as _fr
    return {
        "xai_api_key": bool(_fr.XAI_API_KEY),
        "xai_model": _fr.XAI_MODEL if _fr.XAI_API_KEY else None,
        "xai_image_model": _fr.XAI_IMAGE_MODEL if _fr.XAI_API_KEY else None,
        "reapi_key": bool(API_KEY),
        "public_origin": bool(PUBLIC_ORIGIN),
        "resend_email": bool(os.environ.get("RESEND_API_KEY")),
        "stripe": bool(os.environ.get("STRIPE_SECRET_KEY")),
        "franchise_refs_cached": sorted([
            p.stem for p in FRANCHISE_REFS.glob("*") if p.is_file()
        ]),
    }


@app.post("/api/generate")
async def generate(
    request: Request,
    prompt: str = Form(...),
    duration: int = Form(6),
    resolution: str = Form("768P"),
    reference: UploadFile | None = File(None),
    # Two-stage approval flow: /api/plan returned candidates+storyboard, user
    # picked one, and now hands us the pre-approved data. Both optional so the
    # legacy single-step path (no preview) still works.
    chosen_ref_url: str = Form(""),
    chosen_scenes: str = Form(""),   # JSON array of scene prompt strings
):
    # Auth gate: require a signed-in Supabase user. Falls back to the legacy
    # shared password if SITE_PASSWORD is set (break-glass only).
    user_id: str | None = None
    user_email: str | None = None
    if AUTH_DISABLED:
        # Testing mode: anonymous is allowed. No user_id is recorded, so these
        # renders won't appear in any user's library.
        pass
    elif SUPABASE_URL:
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

    # Pre-picked scenes from /api/plan approval step. Must be a JSON list of
    # strings whose length matches scenes_plan or we reject and force the
    # worker to re-expand. Better to fail loudly than silently mis-align.
    preapproved_scene_prompts: list[str] | None = None
    if chosen_scenes:
        try:
            parsed = json.loads(chosen_scenes)
            if isinstance(parsed, list) and len(parsed) == len(scenes_plan) and all(
                isinstance(s, str) and s.strip() for s in parsed
            ):
                preapproved_scene_prompts = [s.strip() for s in parsed]
            else:
                raise HTTPException(
                    400,
                    f"chosen_scenes must be a JSON array of {len(scenes_plan)} non-empty strings",
                )
        except json.JSONDecodeError:
            raise HTTPException(400, "chosen_scenes is not valid JSON")

    # Save uploaded reference under /static/frames/ so reAPI can fetch it.
    ref_url: str | None = None
    ref_source: str = "none"  # "upload" | "franchise" | "chosen" | "none"
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
            ref_source = "upload"
        # If PUBLIC_ORIGIN is unset (dev with no public URL), we silently drop
        # the reference. Better than sending a data URL that reAPI will reject.
    elif chosen_ref_url and chosen_ref_url.startswith("http"):
        # User picked a candidate in /api/plan. Try to download it to our own
        # /static/franchise-refs/ so reAPI gets a URL we control (avoids CDN
        # hotlink protection and 403s on third-party image URLs).
        # If download fails we still pass the raw URL and hope reAPI can fetch
        # it — worst case the render falls back to no-reference.
        try:
            result = _download_and_validate(chosen_ref_url)
            if result is not None and PUBLIC_ORIGIN:
                blob, ext = result
                out = FRANCHISE_REFS / f"chosen_{job_id}.{ext}"
                out.write_bytes(blob)
                ref_url = f"{PUBLIC_ORIGIN}/static/franchise-refs/{out.name}"
            else:
                ref_url = chosen_ref_url  # last-resort passthrough
        except Exception as e:  # noqa: BLE001
            print(f"[generate] chosen_ref_url download failed: {e}")
            ref_url = chosen_ref_url
        ref_source = "chosen"
    # NB: If no upload and no chosen_ref_url, the legacy path still applies:
    # multi_scene_worker will call resolve_franchise_ref itself before scene 1
    # so /api/generate stays fast.
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
        "ref_source": ref_source,
        "ref_url": ref_url,
        "preapproved_scene_prompts": preapproved_scene_prompts,
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


@app.get("/api/_debug/jobs")
def debug_all_jobs(request: Request):
    """Admin-only view of ALL active jobs (bypasses user scoping).

    Gated by ADMIN_TOKEN env var. Used by the operator (Zach) to check whether
    a stuck render is actually stuck or just slow, without needing to be signed
    in as the user who submitted it.

    Header: X-Admin-Token: <ADMIN_TOKEN>
    """
    import os as _os, time as _time
    admin_token = _os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        return JSONResponse({"error": "admin token not configured"}, status_code=503)
    supplied = request.headers.get("X-Admin-Token", "")
    if supplied != admin_token:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    now = _time.time()
    items = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
    out = []
    for j in items[:30]:
        ca = j.get("created_at", 0)
        ss = j.get("scene_started_at", 0)
        out.append({
            "id": j.get("id"),
            "status": j.get("status"),
            "scene_status": j.get("scene_status"),
            "scene_index": j.get("scene_index"),
            "scene_total": j.get("scene_total"),
            "scene_eta_seconds": j.get("scene_eta_seconds"),
            "task_id": j.get("task_id"),
            "user_id": j.get("user_id"),
            "user_email": j.get("user_email"),
            "age_seconds": int(now - ca) if ca else None,
            "scene_age_seconds": int(now - ss) if ss else None,
            "prompt": (j.get("prompt") or "")[:120],
            "error": j.get("error"),
        })
    return {"total": len(items), "jobs": out}


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
    if AUTH_DISABLED:
        # Testing mode: everyone sees every render (no user_id scoping).
        return [{k: v for k, v in j.items() if k != "task_id"} for j in items[:50]]
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
_ROOT_ASSETS = {
    "styles.css",
    "app.js",
    "pricing.html",
    "pricing.css",
    "pricing.js",
    "terms.html",
    "privacy.html",
    "refunds.html",
    "legal.css",
}


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
