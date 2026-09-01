"""
ShowForge Studio / Not Hollywood backend.
Submits video generation to MiniMax H3, polls, downloads, optionally splits long
prompts into multiple 10-second scenes and stitches them together with ffmpeg.
"""
import base64
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from threading import Thread

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import require_user, get_user
from prompt_expander import expand_prompt, plan_outline
from franchise_ref import (
    resolve_franchise_ref,
    resolve_scene_character_refs,
    extract_show_title,
    extract_show_info,
    slugify as _slugify_title,
    search_candidates_duckduckgo,
    _download_and_validate,
)

# Video provider: fal.ai H3 Max (fast). Renders a 5s 768p clip with synced audio
# in ~6-10s wall-clock. Full migration from fal (~2min) on 2026-08-31.
#
# Endpoint routing:
#   - Text-only:            minimax/h3-max/text-to-video      (~6s)
#   - Image-to-video:       minimax/h3-max/image-to-video     (~7s)
#   - Scene 0 with a user reference OR franchise still: we do NOT use H3's
#     reference-to-video (that endpoint is on standard H3 and takes ~2-3 min).
#     Instead we blend the reference into a natural opening keyframe via
#     fal-ai/nano-banana/edit (~15-20s), then feed the keyframe to H3 Max I2V
#     as its literal first frame (~7s). Total scene 0 = ~25s vs 2-3 min.
FAL_BASE = "https://queue.fal.run"
GROK_BASE = "https://api.x.ai/v1"

# Public origin for scene-chaining reference-image URLs. fal's image_url
# requires a public https URL (rejects data URLs), so we serve extracted frames
# and uploaded references from our own /static mount and pass those URLs to fal.
# Falls back to a relative path in dev; fal will reject those, which is fine
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
# fal.ai key (video + image edit).
# FAL_KEY: primary Railway/dev env var (matches fal's own convention)
# CUSTOM_CRED_QUEUE_FAL_RUN_TOKEN: publish_website credential proxy fallback
FAL_KEY = (
    os.environ.get("FAL_KEY", "")
    or os.environ.get("CUSTOM_CRED_QUEUE_FAL_RUN_TOKEN", "")
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

# Unlimited-credit user ids. These accounts skip the credit debit at
# /api/generate time and are reported to the frontend with balance =
# UNLIMITED_BALANCE so the credit widget renders "Unlimited" instead of
# a number. Refunds are no-ops for these users.
UNLIMITED_BALANCE = 10_000_000  # sentinel; anything >= this means unlimited
UNLIMITED_CREDIT_USER_IDS = {
    # zacharrow3@gmail.com (owner)
    "c129c72f-8636-4ca4-a8cf-218112824261",
}
# Emails on this list get unlimited credits the moment they sign up — no
# manual whitelisting after the fact needed. Lowercased for case-insensitive
# comparison. Both /api/credits and the /api/generate debit gate consult
# this set alongside UNLIMITED_CREDIT_USER_IDS.
UNLIMITED_CREDIT_EMAILS = {
    "zacharrow3@gmail.com",
    "lloydjarrow@gmail.com",
    "johndavidarrow@gmail.com",
}


def _is_unlimited(user_id: str | None, user_email: str | None) -> bool:
    """Return True if this user should skip credit debits.

    Whitelist matches on user_id OR lowercased email so we can pre-whitelist
    people (family, staff) before they've signed up and been issued a
    Supabase UUID. As soon as their first login pushes an email claim into
    the JWT, they're recognized as unlimited on the very first /api/credits
    call.
    """
    if user_id and user_id in UNLIMITED_CREDIT_USER_IDS:
        return True
    if user_email and user_email.lower().strip() in UNLIMITED_CREDIT_EMAILS:
        return True
    return False
# ROOT is the directory this file lives in. Works in both dev and prod sandboxes.
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
VIDEOS = STATIC / "videos"
THUMBS = STATIC / "thumbs"
JOBS_FILE = ROOT / "jobs.json"

SCENES = ROOT / "scenes"  # per-scene mp4s before concat
FRAMES = STATIC / "frames"  # reference frames (uploads + extracted last frames)
                            # served publicly at /static/frames/<name> so fal
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

# One-shot cache sweep: delete pre-v2 franchise ref files sitting in
# Railway's persistent volume that were saved during the pre-DDG-priority
# era when Grok Imagine's drifted outputs were the only fallback (Rick
# with a beard, wrong Morty, etc).
#
# Preserved:
#   - files with v2- prefix (current cache scheme)
#   - files with chosen_ prefix (per-job user picks)
#   - hand-picked baseline images committed in the repo (seinfeld.png,
#     the-office.png). We identify these by name, since git-tracking info
#     isn't reliable at runtime on Railway.
_BAKED_IN_REFS = {"seinfeld.png", "the-office.png"}
try:
    _pruned = 0
    for _p in FRANCHISE_REFS.glob("*"):
        if not _p.is_file():
            continue
        _name = _p.name
        if (
            _name.startswith("v2-")
            or _name.startswith("chosen_")
            or _name in _BAKED_IN_REFS
        ):
            continue
        try:
            _p.unlink()
            _pruned += 1
        except Exception as _e:
            print(f"[franchise_refs] prune failed for {_name}: {_e}")
    if _pruned:
        print(f"[franchise_refs] pruned {_pruned} pre-v2 cached refs on startup")
except Exception as _e:
    print(f"[franchise_refs] startup prune crashed: {_e}")


# Franchise reference-pack lookup lives in franchise_ref.py. Given a prompt
# and PUBLIC_ORIGIN, it returns a dict {url, slug, title, source} pointing to
# a cast reference frame. It reads the on-disk cache first (pre-committed
# stills like seinfeld.png / the-office.png), then falls through to web
# image search (SerpAPI), then AI image generation (OpenAI), caching each
# result on disk under FRANCHISE_REFS/<slug>.<ext> for future runs. Every
# subsystem is optional — missing keys just skip that step and fall through.

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _bootstrap_library_reaper():
    """Kick off the library save-queue reaper. Any render whose Supabase
    persistence failed on completion (network blip, 5xx, etc.) is parked in
    pending_saves.json on the same persistent volume as jobs.json; the
    reaper retries it every few minutes and drops it once the source mp4 is
    gone from the local disk. Without this thread, users notice missing
    library rows before we do.
    """
    try:
        import library as _lib
        _lib.start_reaper(interval_s=180.0)
    except Exception as _e:
        print(f"[startup] library reaper failed to start (non-fatal): {_e}")


def load_jobs() -> dict:
    if JOBS_FILE.exists():
        return json.loads(JOBS_FILE.read_text())
    return {}


def save_jobs(jobs: dict) -> None:
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))


JOBS: dict = load_jobs()


def fal_headers() -> dict:
    # fal.ai accepts both `Key <token>` and `Bearer <token>`. Bearer is friendlier
    # for our env-var mixing since some proxies rewrite Authorization headers.
    return {"Authorization": f"Bearer {FAL_KEY}", "Content-Type": "application/json"}


def grok_headers() -> dict:
    return {"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"}


# Substrings that indicated MiniMax direct rejected the prompt for policy reasons.
# Kept for the Grok fallback path but effectively dead now that fal runs H3
# with content_filter:false — fal won't return these strings for policy issues.
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


# ─── fal.ai video provider ─────────────────────────────────────────────────
# H3 Max resolutions: 480P and 768P only. Standard H3 (only used by ref-to-video,
# which we no longer call) supports up to 4K. We keep the frontend's legacy
# strings (768P, 1080P, 2K) and map anything above 768P down to 768P since H3
# Max is our only endpoint.
_FAL_MAX_RESOLUTIONS = {"480P": "480P", "768P": "768P", "1080P": "768P", "2K": "768P", "4K": "768P"}


def _submit_fal(
    prompt: str,
    duration: int,
    resolution: str,
    ref_url: str | None,
    ref_mode: str,
) -> tuple[str | None, str | None]:
    """Submit a scene to fal.ai H3 Max. Returns (task_id, error).

    Endpoint selection:
      - No ref_url:                       minimax/h3-max/text-to-video   (~6s)
      - ref_url (any ref_mode):           minimax/h3-max/image-to-video  (~7s)

    ref_mode is now advisory only: with fal we ALWAYS use I2V because H3 Max
    doesn't have R2V and standard H3's R2V is too slow. Callers that want
    identity anchoring (scene 0 with a user reference) should pre-blend the
    reference into a natural opening keyframe via generate_keyframe() and
    pass THAT to submit_h3 as first_frame.

    Task IDs are namespaced 'fal:{endpoint}:{request_id}' so fetch_task can
    rebuild the correct status/response URLs.
    """
    if ref_url:
        endpoint = "minimax/h3-max/image-to-video"
        body: dict = {
            "prompt": prompt,
            "image_url": ref_url,
            "resolution": _FAL_MAX_RESOLUTIONS.get(resolution, "768P"),
            "duration": duration,
            "prompt_expansion_mode": "balanced",
            "enable_safety_checker": False,
        }
    else:
        endpoint = "minimax/h3-max/text-to-video"
        body = {
            "prompt": prompt,
            "resolution": _FAL_MAX_RESOLUTIONS.get(resolution, "768P"),
            "duration": duration,
            "aspect_ratio": "16:9",
            "prompt_expansion_mode": "balanced",
            "enable_safety_checker": False,
        }

    try:
        r = requests.post(f"{FAL_BASE}/{endpoint}", headers=fal_headers(), json=body, timeout=60)
    except Exception as e:  # noqa: BLE001
        return None, f"network error: {e}"
    if r.status_code >= 300:
        try:
            err_obj = r.json()
            msg = err_obj.get("detail") or err_obj.get("message") or r.text[:400]
            if isinstance(msg, list):
                msg = "; ".join(str(m) for m in msg)
        except Exception:  # noqa: BLE001
            msg = r.text[:400]
        return None, f"fal http {r.status_code}: {msg}"
    data = r.json()
    req_id = data.get("request_id")
    if not req_id:
        return None, f"fal no request_id in response: {str(data)[:400]}"
    return f"fal:{endpoint}:{req_id}", None


def submit_h3(
    prompt: str,
    duration: int,
    resolution: str,
    ref_url: str | None,
    ref_mode: str = "first_frame",
) -> tuple[str | None, str | None]:
    """Submit a scene to fal.ai H3 Max. See _submit_fal for endpoint routing."""
    if not FAL_KEY:
        return None, "no fal.ai key configured: set FAL_KEY"
    return _submit_fal(prompt, duration, resolution, ref_url, ref_mode)


# Cheap parser for progress signals that Fal-hosted models emit into logs.
# MiniMax H3 (and most video models on Fal) print lines like:
#   "progress: 0.42"
#   "Generating frames [██████░░░░] 60%"
#   "step 12/50"
# We scan the latest logs and return a 0..1 fraction when we can find one,
# else None. Never throws — progress is best-effort and callers must tolerate
# a missing value (they fall back to wall-clock ETA).
_PROGRESS_RE = re.compile(r"progress[:=\s]+([01](?:\.\d+)?|\d{1,3}(?:\.\d+)?%?)", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_STEP_RE = re.compile(r"step\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def _parse_fal_progress(logs: list) -> float | None:
    """Walk logs newest-first, return the freshest 0..1 progress signal.

    Recognizes three shapes commonly emitted by video models on Fal:
      progress: 0.42        → 0.42
      ███░░ 42%              → 0.42
      step 12/50            → 0.24
    Returns None when no shape matches (so callers fall back to time-based).
    """
    if not logs or not isinstance(logs, list):
        return None
    for entry in reversed(logs):
        msg = entry.get("message") if isinstance(entry, dict) else (entry if isinstance(entry, str) else "")
        if not msg:
            continue
        m = _PROGRESS_RE.search(msg)
        if m:
            raw = m.group(1).rstrip("%")
            try:
                v = float(raw)
            except ValueError:
                continue
            if v > 1.0:  # "progress: 42" or "progress: 42%"
                v = v / 100.0
            return max(0.0, min(1.0, v))
        m = _STEP_RE.search(msg)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            if tot > 0:
                return max(0.0, min(1.0, cur / tot))
        m = _PERCENT_RE.search(msg)
        if m:
            try:
                v = float(m.group(1)) / 100.0
            except ValueError:
                continue
            return max(0.0, min(1.0, v))
    return None


def _fetch_fal(endpoint: str, request_id: str) -> dict:
    """Poll fal.ai queue for a request. Returns the legacy shape
    {status, content: {url}, error: {message}} plus optional progress signals:
      queue_position: int | None   — when IN_QUEUE
      progress_fraction: float | None  — when IN_PROGRESS and logs contain
                                        a parseable progress hint

    fal exposes status_url and response_url paths derived from the endpoint
    prefix. For 'minimax/h3-max/text-to-video' the request path becomes
    'minimax/h3-max/requests/{request_id}/status'. We request logs=1 so the
    status payload includes model stdout/stderr and we can parse
    per-scene percentages instead of guessing from wall-clock time alone.
    """
    prefix = endpoint.rsplit("/", 1)[0]
    status_url = f"{FAL_BASE}/{prefix}/requests/{request_id}/status?logs=1"
    response_url = f"{FAL_BASE}/{prefix}/requests/{request_id}"
    try:
        sr = requests.get(status_url, headers=fal_headers(), timeout=30)
    except Exception:  # noqa: BLE001
        return {}
    if sr.status_code != 200:
        return {}
    sj = sr.json()
    st = sj.get("status", "IN_QUEUE")
    mapped = {
        "IN_QUEUE": "queued",
        "IN_PROGRESS": "running",
        "COMPLETED": "succeeded",
        "FAILED": "failed",
    }.get(st, "running")
    out: dict = {"status": mapped}
    # Queue position surfaces "you're 3rd in line at Fal" to the UI so users
    # know a stuck scene is queued, not actually stuck.
    if mapped == "queued":
        qp = sj.get("queue_position")
        if isinstance(qp, int) and qp >= 0:
            out["queue_position"] = qp
    # Real per-scene progress from model logs — the whole point of this change.
    if mapped in ("queued", "running"):
        prog = _parse_fal_progress(sj.get("logs") or [])
        if prog is not None:
            out["progress_fraction"] = prog
    if mapped == "succeeded":
        try:
            rr = requests.get(response_url, headers=fal_headers(), timeout=30)
            res = rr.json()
            vid = (res.get("video") or {}).get("url")
            if vid:
                out["content"] = {"url": vid}
            else:
                out = {"status": "failed", "error": {"message": f"fal returned no video url: {str(res)[:300]}"}}
        except Exception as e:  # noqa: BLE001
            out = {"status": "failed", "error": {"message": f"fal result fetch failed: {e}"}}
    elif mapped == "failed":
        logs = sj.get("logs") or []
        msg = "fal generation failed"
        if logs and isinstance(logs, list):
            last = logs[-1]
            if isinstance(last, dict):
                msg = last.get("message") or msg
            elif isinstance(last, str):
                msg = last
        out["error"] = {"message": msg}
    return out


def fetch_task(task_id: str) -> dict:
    """Poll a submitted job. Returns {status, content: {url}, error: {message}}.

    task_id is namespaced by submit_h3 as 'fal:<endpoint>:<request_id>'.
    """
    if task_id.startswith("fal:"):
        _, rest = task_id.split(":", 1)
        endpoint, request_id = rest.rsplit(":", 1)
        return _fetch_fal(endpoint, request_id)
    # Legacy fal task ids from before the migration are silently failed;
    # they'll appear as failed renders and users can regenerate. This branch
    # is defensive; JOBS should not contain any in-flight fal ids.
    return {"status": "failed", "error": {"message": "provider retired"}}


# ─── fal.ai keyframe generation (nano-banana edit) ────────────────────────
# Used for scene 0 when a franchise cast still and/or user upload should
# anchor identity WITHOUT showing up as the literal opening frame. We call
# nano-banana/edit to blend the reference(s) with the scene prompt into a
# natural opening still (~15-20s), save it under /static/frames/, and hand
# the public URL back so submit_h3 can feed it to H3 Max I2V.
_NANOBANANA_ENDPOINT = "fal-ai/nano-banana/edit"


def _fal_edit_image(
    prompt: str,
    image_urls: list[str],
    aspect_ratio: str = "16:9",
) -> tuple[str | None, str | None]:
    """Submit a nano-banana edit and block until the result URL is available.

    Returns (image_url, error). ~15-20s wall clock. Nano-banana accepts up to
    3 image URLs; we pass at most the first 3.
    """
    if not FAL_KEY:
        return None, "no fal.ai key configured"
    body = {
        "prompt": prompt,
        "image_urls": image_urls[:3],
        "num_images": 1,
        "aspect_ratio": aspect_ratio,
        "output_format": "jpeg",
    }
    try:
        r = requests.post(f"{FAL_BASE}/{_NANOBANANA_ENDPOINT}", headers=fal_headers(), json=body, timeout=60)
    except Exception as e:  # noqa: BLE001
        return None, f"nano-banana network error: {e}"
    if r.status_code >= 300:
        try:
            err = r.json()
            msg = err.get("detail") or err.get("message") or r.text[:400]
            if isinstance(msg, list):
                msg = "; ".join(str(m) for m in msg)
        except Exception:  # noqa: BLE001
            msg = r.text[:400]
        return None, f"nano-banana http {r.status_code}: {msg}"
    data = r.json()
    req_id = data.get("request_id")
    if not req_id:
        return None, f"nano-banana no request_id: {str(data)[:200]}"
    status_url = f"{FAL_BASE}/fal-ai/nano-banana/requests/{req_id}/status"
    response_url = f"{FAL_BASE}/fal-ai/nano-banana/requests/{req_id}"
    # Poll for up to 60s at 1.5s intervals — normal completion is 15-20s.
    start = time.time()
    while time.time() - start < 60:
        time.sleep(1.5)
        try:
            sr = requests.get(status_url, headers=fal_headers(), timeout=15)
            if sr.status_code != 200:
                continue
            st = sr.json().get("status")
            if st == "COMPLETED":
                rr = requests.get(response_url, headers=fal_headers(), timeout=30)
                res = rr.json()
                imgs = res.get("images") or []
                if imgs and imgs[0].get("url"):
                    return imgs[0]["url"], None
                return None, f"nano-banana no image in result: {str(res)[:200]}"
            if st == "FAILED":
                return None, "nano-banana generation failed"
        except Exception:  # noqa: BLE001
            continue
    return None, "nano-banana timed out after 60s"


def generate_scene0_keyframe(
    scene_prompt: str,
    reference_urls: list[str],
    job_id: str,
) -> tuple[str | None, str | None]:
    """Blend one or more reference images (user upload, franchise cast still)
    with the scene 0 prompt into a natural opening keyframe. Downloads the
    result to /static/frames/ and returns its public URL.

    Returns (public_url, error). Public URL requires PUBLIC_ORIGIN; if unset,
    we return an error rather than a data URL — H3 Max I2V needs a public URL.
    """
    if not reference_urls:
        return None, "no references to blend"
    if not PUBLIC_ORIGIN:
        return None, "PUBLIC_ORIGIN unset — cannot host keyframe"

    # Compose an edit prompt that reframes the reference(s) as the opening
    # shot of the scene. The trick: describe the scene as an image, not a
    # video. Nano-banana treats the references as source subjects and the
    # prompt as the target composition.
    edit_prompt = (
        "A single cinematic film still that serves as the opening frame of "
        "the following scene. Blend the reference image(s) as the identity "
        "of the characters and setting into a natural establishing shot. "
        "16:9 widescreen, natural lighting, no captions, no text overlays.\n\n"
        f"Scene: {scene_prompt}"
    )

    img_url, err = _fal_edit_image(edit_prompt, reference_urls, aspect_ratio="16:9")
    if not img_url:
        return None, err or "keyframe generation failed"

    # Download to our own /static/frames/ so H3 Max I2V gets a URL we control
    # (avoids v3b.fal.media expiring or getting rate-limited).
    try:
        got = _download_and_validate(img_url)
        if not got:
            return None, "keyframe download failed"
        raw, ext = got
        frame_name = f"seed_{job_id}.{ext}"
        (FRAMES / frame_name).write_bytes(raw)
        return f"{PUBLIC_ORIGIN}/static/frames/{frame_name}", None
    except Exception as e:  # noqa: BLE001
        return None, f"keyframe save failed: {e}"




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
# sooner. fal and Grok Imagine both tolerate rapid polling comfortably at
# 3s. We also apply a mild backoff after the first minute since long-running
# jobs are unlikely to complete on the very next poll and rapid polling only
# helps near the end.
# fal H3 Max finishes in ~6–10s so we poll tight early.
POLL_TIGHT_S = 1.5
POLL_MEDIUM_S = 3
POLL_SLOW_S = 5
POLL_TIGHT_UNTIL_S = 30
POLL_MEDIUM_UNTIL_S = 60


def _poll_interval(elapsed: float) -> float:
    if elapsed < POLL_TIGHT_UNTIL_S:
        return POLL_TIGHT_S
    if elapsed < POLL_MEDIUM_UNTIL_S:
        return POLL_MEDIUM_S
    return POLL_SLOW_S


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
    ref_mode: str = "first_frame",
) -> tuple[bool, str]:
    """Submit one scene to MiniMax H3; on sensitivity rejection, fall back to Grok Imagine.

    on_status(status: str) called periodically for UI updates.
    ref_mode selects I2V ("first_frame") vs R2V ("subject") — see submit_h3.
    Returns (ok, error).
    """
    submit_start = time.time()
    task_id, err = submit_h3(scene_prompt, duration, resolution, ref_data_url, ref_mode=ref_mode)
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
        # Pass through the extra signals from Fal so the worker can attach
        # them to scenes_state and the UI can render a real progress bar
        # (percent + queue position) instead of a wall-clock estimate.
        # Older on_status callbacks only accept one positional arg; guard
        # with a TypeError fallback so we don't regress single-arg callers.
        try:
            on_status(
                status,
                queue_position=task.get("queue_position"),
                progress_fraction=task.get("progress_fraction"),
            )
        except TypeError:
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
            print(f"[email {job_id}] resend {r.status_code} from={EMAIL_FROM} to={user_email}: {r.text[:300]}")
        else:
            try:
                msg_id = (r.json() or {}).get("id", "?")
            except Exception:
                msg_id = "?"
            print(f"[email {job_id}] sent from={EMAIL_FROM} to={user_email} resend_id={msg_id}")
    except Exception as e:
        print(f"[email {job.get('id','?')}] send failed: {e}")


# Max scenes rendering concurrently. fal can handle several jobs in parallel;
# this bounds our exposure to their rate limits and to token bucket bursts.
# Env-tunable so we can back off if fal starts 429ing under load.
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

    # scene_states tracks the status string reported by fal for each scene
    # so we can compute an aggregate progress signal for the UI.
    scene_states: dict[int, str] = {i: "queued" for i in range(len(scenes))}
    scene_started_at: dict[int, float] = {}
    scene_finished_at: dict[int, float] = {}
    # Populated from Fal status polling in render_one_scene's on_status
    # callback. queue_position is set only while a scene is IN_QUEUE at Fal;
    # scene_progress is a 0..1 fraction parsed from model logs while running.
    scene_queue_position: dict[int, int] = {}
    scene_progress: dict[int, float] = {}

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
        # Long-form renders (>=60s) will have a pre-approved story outline
        # sitting on the JOBS record from the /api/plan approval gate. When
        # present, hand it to expand_prompt so scenes follow the A/B story
        # structure the user approved. Short renders pass None and get the
        # legacy one-shot expansion.
        approved_outline = (JOBS.get(job_id) or {}).get("preapproved_outline")
        expansion = expand_prompt(prompt, scenes, outline=approved_outline)
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
                # Real Fal signals when available. Frontend uses these when
                # present and falls back to wall-clock ETA otherwise.
                "queue_position": scene_queue_position.get(i),
                "progress": scene_progress.get(i),
            }
            for i in range(len(scenes))
        ]
        save_jobs(JOBS)

    def _render_scene(
        idx: int, dur: int, scene_ref_url: str | None, ref_mode: str = "first_frame"
    ) -> tuple[int, bool, str, Path]:
        """Render one scene. Returns (idx, ok, err, out_path).

        scene_ref_url:
          - In parallel mode: same shared initial ref for every scene.
          - In sequential mode: previous scene's extracted last frame (or the
            initial ref for idx==0).

        ref_mode:
          - "subject" for scene 0 when the ref is the user's picked promo/cast
            photo — keeps that photo out of the opening frame.
          - "first_frame" for chained scenes (idx > 0) so the last frame of
            scene N — an actual generated in-story frame — becomes scene N+1's
            opening frame for seamless continuity.
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

        def _on_status(status: str, queue_position: int | None = None, progress_fraction: float | None = None):
            prev = scene_states.get(idx)
            scene_states[idx] = status
            if status == "running" and idx not in scene_started_at:
                scene_started_at[idx] = time.time()
            # Stash Fal's per-scene signals so _publish_agg_status can copy
            # them onto scenes_state[idx]. Missing values are fine — UI
            # falls back to wall-clock ETA when neither is present.
            if queue_position is not None:
                scene_queue_position[idx] = queue_position
            elif status == "running":
                # Once the scene is running it's no longer queued; drop stale value.
                scene_queue_position.pop(idx, None)
            if progress_fraction is not None:
                scene_progress[idx] = float(progress_fraction)
            _publish_agg_status()

        ok, err = render_one_scene(
            scene_prompt, dur, resolution, scene_ref_url, scene_out,
            on_status=_on_status, ref_mode=ref_mode,
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
    # Scene 0 identity anchor: if the user picked a reference (upload or
    # franchise cast still), we pre-blend it with the scene prompt into a
    # natural opening keyframe via nano-banana/edit (~15-20s), then feed the
    # keyframe to H3 Max I2V as its literal first frame. This avoids the
    # ~2-3min standard-H3 R2V endpoint while still anchoring identity, and
    # avoids showing the raw reference photo as the first ~0.5s of video.
    #
    # Character-aware reference selection: when the render is franchise-based
    # (e.g. Seinfeld), the group cast frame doesn't contain every character.
    # If the scene mentions Newman, our seinfeld.png has Jerry/George/Elaine/
    # Kramer only — no Newman — so nano-banana grabs the closest lookalike
    # (George) and dresses him as a mail carrier. Fix: extract characters
    # from the scene prompt via Grok, resolve a per-character reference still
    # for each (Grok Imagine, cached to disk), and hand THOSE stills to
    # nano-banana instead of the group frame. Falls back to group frame if
    # no characters detected or generation fails.
    #
    # Scenes 1+ chain from the previous scene's extracted last frame (I2V).
    current_ref_mode = "first_frame"  # always I2V now
    if current_ref:
        j = JOBS.get(job_id)
        if j is not None:
            j["status"] = "keyframe"
            save_jobs(JOBS)
        # Use the scene 0 prompt as the blend target. scenes[0]_prompt is
        # written into the JOBS record by /api/generate before the worker
        # starts; fall back to the top-level prompt if missing.
        job_rec = JOBS.get(job_id) or {}
        scenes_meta = job_rec.get("scenes") or []
        s0_prompt = (
            (scenes_meta[0].get("prompt") if scenes_meta else None)
            or job_rec.get("prompt")
            or "cinematic film still"
        )
        top_prompt = (job_rec.get("prompt") or "").strip()

        # Resolve per-character references when this looks franchise-based.
        # extract_show_title returns None for original prompts, in which case
        # we skip the character path and just use the user's chosen ref.
        keyframe_refs: list[str] = []
        try:
            show_title = extract_show_title(top_prompt) if top_prompt else None
        except Exception as e:  # noqa: BLE001
            print(f"[render {job_id}] show-title extraction crashed: {e}")
            show_title = None
        cast_names: list[str] = []
        if show_title and PUBLIC_ORIGIN:
            try:
                from franchise_ref import _llm_extract_scene_characters
                cast_names = list(_llm_extract_scene_characters(show_title, s0_prompt))
                char_refs = resolve_scene_character_refs(
                    show_title, s0_prompt,
                    franchise_refs_dir=FRANCHISE_REFS,
                    public_origin=PUBLIC_ORIGIN,
                )
                if char_refs:
                    keyframe_refs = char_refs
                    print(f"[render {job_id}] scene 0 using {len(char_refs)} character ref(s) for '{show_title}': {cast_names}")
            except Exception as e:  # noqa: BLE001
                print(f"[render {job_id}] character-ref resolution crashed: {e}")

        # Persist cast + show on the job record so the frontend can render
        # the cast chip strip. Do this even when char-ref generation failed
        # (partial or total) so the user still sees who Grok cast, and can
        # tell us if Grok picked wrong.
        j2 = JOBS.get(job_id)
        if j2 is not None:
            if show_title:
                j2["show_title"] = show_title
            if cast_names:
                j2["cast"] = cast_names
            save_jobs(JOBS)

        # Fall back to the user-picked reference (group cast frame or user
        # upload) when we don't have character-specific refs.
        if not keyframe_refs:
            keyframe_refs = [current_ref]

        keyframe_url, kf_err = generate_scene0_keyframe(
            scene_prompt=s0_prompt,
            reference_urls=keyframe_refs,
            job_id=job_id,
        )
        if keyframe_url:
            print(f"[render {job_id}] scene 0 keyframe generated: {keyframe_url}")
            current_ref = keyframe_url
        else:
            # Keyframe generation failed — fall back to using the raw reference
            # as the first frame. It'll show as the opening ~0.5s but the render
            # completes fast rather than 2-3 min on R2V.
            print(f"[render {job_id}] keyframe generation failed ({kf_err}); using raw reference as first frame")
    j = JOBS.get(job_id)
    if j is not None:
        j["render_mode"] = "sequential" if use_sequential else "single"
        j["status"] = "rendering"
        save_jobs(JOBS)
    for i, dur in enumerate(scenes):
        try:
            idx, ok, err, out_path = _render_scene(i, dur, current_ref, ref_mode=current_ref_mode)
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
                # From scene 1 onward the ref IS the intended opening frame.
                current_ref_mode = "first_frame"
            else:
                # No public origin means fal can't fetch our frame. Keep going
                # with prompt-only continuity — still better than random.
                current_ref = None
                current_ref_mode = "first_frame"
                if not PUBLIC_ORIGIN:
                    print(f"[render {job_id}] no PUBLIC_ORIGIN set \u2014 frame-chain degraded to prompt-only")

    if first_failure is not None:
        idx, err = first_failure
        job = JOBS.get(job_id)
        if job:
            job["status"] = "failed"
            job["error"] = f"scene {idx+1}/{len(scenes)}: {err}"
            job["finished_at"] = time.time()
            refund_credits_for_failed_job(job)
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
    #
    # Either way we finish with a faststart remux so the moov atom is at the
    # front of the file. Without this browsers can't start playback until the
    # whole file has downloaded — that's the "ready but won't play, then plays
    # after a moment" bug users were reporting.
    if ok:
        wm_ok, wm_err = apply_watermark(unwatermarked, dest)
        if not wm_ok:
            print(f"[render {job_id}] watermark failed ({wm_err[:200]}) \u2014 shipping unwatermarked with faststart remux")
            try:
                remux = subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", str(unwatermarked),
                        "-c", "copy", "-movflags", "+faststart",
                        str(dest),
                    ],
                    capture_output=True, timeout=120,
                )
                if remux.returncode != 0:
                    # Remux failed too — fall back to raw copy so we still
                    # ship SOMETHING. Playback will be slow-start but working.
                    import shutil
                    shutil.copy(unwatermarked, dest)
                    print(f"[render {job_id}] faststart remux failed ({remux.stderr.decode(errors='ignore')[-200:]}) \u2014 shipped without faststart")
            except Exception as e:
                try:
                    import shutil
                    shutil.copy(unwatermarked, dest)
                except Exception as e2:
                    ok, err = False, f"copy-unwatermarked failed: {e2} (after remux exception: {e})"

    j = JOBS.get(job_id)
    if not j:
        return
    if ok:
        j["status"] = "done"
        j["video"] = f"/static/videos/{job_id}.mp4"
        j["finished_at"] = time.time()
        # Extract a JPEG poster next to the mp4 so the frontend tile can paint
        # instantly instead of waiting for the video to decode frame 0.8. Same
        # ffmpeg helper the library upload uses — keeps the two paths in sync.
        # Best-effort; a missing thumb just falls back to the old preload path.
        try:
            import library as _lib
            thumb_dest = dest.with_suffix(".jpg")
            if _lib._extract_thumbnail(dest, thumb_dest):
                j["thumb"] = f"/static/videos/{job_id}.jpg"
        except Exception as _e:
            print(f"[render {job_id}] local thumbnail extraction failed (non-fatal): {_e}")
        # Persist to user library (best-effort; never fails the render).
        # Anonymous jobs (no user_id) are skipped inside save_render_to_library.
        try:
            import library as _lib
            print(
                f"[render {job_id}] library save starting: enabled={_lib.library_enabled()} "
                f"user_id={(j.get('user_id') or '')[:12]!r} dest={dest} "
                f"exists={dest.exists()} bytes={dest.stat().st_size if dest.exists() else 0}"
            )
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
            print(f"[render {job_id}] library save result: saved={saved}")
            if saved:
                j["saved_to_library"] = True
                save_jobs(JOBS)
        except Exception as e:
            import traceback
            print(f"[render {job_id}] library save exception (non-fatal): {e}")
            traceback.print_exc()
    else:
        j["status"] = "failed"
        j["error"] = f"finalize failed: {err}"
        j["finished_at"] = time.time()
        refund_credits_for_failed_job(j)
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


def credit_cost_for(duration: int, resolution: str) -> int:
    """Match the pricing page contract: 1 credit = 1 second at 768P;
    1080P/2K costs 2 credits per second. Minimum 1 credit per render.
    """
    per_sec = 2 if resolution == "1080P" else 1
    return max(1, int(duration) * per_sec)


def _adjust_credits_via_supabase(user_id: str, delta: int) -> tuple[bool, str, int | None]:
    """Atomically adjust `user_credits.balance` by `delta` (positive = grant,
    negative = debit). Fails without changing balance when `delta < 0` and the
    user doesn't have enough credits. Returns (ok, message, new_balance).

    Note: PostgREST doesn't support conditional writes, so we do a
    read-check-write. Small race window is acceptable for now (single-user
    interaction; concurrent renders from the same account are rare).
    Requires SUPABASE_SERVICE_ROLE_KEY.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return False, "supabase service role not configured", None

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
        return False, f"read failed: {read.status_code} {read.text[:200]}", None
    rows = read.json()
    current = rows[0]["balance"] if rows else 0
    new_balance = current + delta
    if new_balance < 0:
        return False, f"insufficient credits: have {current}, need {-delta}", current

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
        return False, f"upsert failed: {up.status_code} {up.text[:200]}", None
    return True, f"balance {current} → {new_balance}", new_balance


def refund_credits_for_failed_job(job: dict) -> None:
    """Best-effort refund of a failed render. Idempotent: sets
    `credits_refunded=True` on the job so a repeated failure code path
    (e.g. worker error → finalize error) can't double-refund.
    """
    if not job:
        return
    if job.get("credits_refunded"):
        return
    debit = int(job.get("credits_debited") or 0)
    if debit <= 0:
        return
    user_id = job.get("user_id")
    if not user_id:
        return
    # Unlimited-credit accounts never debit in the first place, so there's
    # nothing to refund. Guard here in case a legacy job predates the
    # whitelist.
    # Refund guard: unlimited-by-user_id accounts couldn't have debited, but
    # keep the note for legacy jobs. Email-whitelisted users also never debit,
    # but their user_id is a normal UUID so we can't distinguish here; the
    # debit==0 early-return above already handles them correctly.
    if user_id in UNLIMITED_CREDIT_USER_IDS:
        job["credits_refunded"] = True
        job["credits_refund_note"] = "unlimited account—no refund needed"
        return
    try:
        ok, msg, _ = _adjust_credits_via_supabase(user_id, debit)
        job["credits_refunded"] = True
        job["credits_refund_note"] = msg if ok else f"refund failed: {msg}"
        print(f"[credits] {'refunded' if ok else 'REFUND FAILED'} {debit} to {user_id}: {msg}")
    except Exception as e:
        job["credits_refund_note"] = f"refund exception: {e}"
        print(f"[credits] refund exception for {user_id}: {e}")


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
    user_email = claims.get("email", "") or ""
    # Unlimited-credit accounts always report the sentinel balance so the
    # UI displays "Unlimited" regardless of what the DB row (if any) says.
    # Email matches too, so pre-whitelisted family/staff show "Unlimited"
    # from their first login without needing a manual user_id add.
    if _is_unlimited(user_id, user_email):
        return {"balance": UNLIMITED_BALANCE, "unlimited": True}
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


# Cache of proxied image bytes so we don't re-fetch every DDG hotlink hit
_PROXY_CACHE: dict[str, tuple[bytes, str, float]] = {}
_PROXY_CACHE_TTL = 3600.0   # 1 hour
_PROXY_CACHE_MAX = 200      # bounded LRU-ish

@app.get("/api/proxy_ref")
def proxy_ref(url: str):
    """Server-side image proxy for reference-picker candidates.

    Many DDG-returned image hosts (ew.com, colliderimages.com, static0.srcdn.com,
    futurecdn.net, etc.) refuse hotlinks from third-party origins even with
    referrerpolicy=no-referrer. The candidate grid in the plan modal then
    silently hides those tiles (onerror handler in renderRefTiles), and
    users see 'no reference images'.

    Fix: fetch the image from OUR origin with a browser-like UA, cache the
    bytes for an hour, and re-serve. The browser sees a same-origin request
    that always succeeds.

    Only http(s) URLs are allowed; the response is cached in memory (bounded
    to 200 entries) so a busy picker session doesn't hammer the third-party
    hosts. If the fetch fails we return 502 with a tiny transparent PNG so
    the tile still renders (user can pick a different one or upload).
    """
    import time
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    # Only proxy image-ish extensions or content types we control after fetch
    now = time.time()
    hit = _PROXY_CACHE.get(url)
    if hit and (now - hit[2]) < _PROXY_CACHE_TTL:
        body, ct, _ = hit
        return Response(content=body, media_type=ct, headers={"Cache-Control": "public, max-age=3600"})
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                # Send a plausible Referer matching the target host so hotlink
                # protections that check Referer accept it.
                "Referer": "/".join(url.split("/")[:3]) + "/",
            },
            timeout=10,
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.content:
            raise HTTPException(502, f"upstream {r.status_code}")
        ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
        if not ct.startswith("image/"):
            raise HTTPException(502, "not an image")
        body = r.content[:12_000_000]  # 12 MB cap per image
        # Simple bounded cache eviction
        if len(_PROXY_CACHE) >= _PROXY_CACHE_MAX:
            oldest = min(_PROXY_CACHE.items(), key=lambda kv: kv[1][2])[0]
            _PROXY_CACHE.pop(oldest, None)
        _PROXY_CACHE[url] = (body, ct, now)
        return Response(content=body, media_type=ct, headers={"Cache-Control": "public, max-age=3600"})
    except HTTPException:
        raise
    except Exception as e:
        print(f"[proxy_ref] {url[:80]} failed: {e}")
        # 1x1 transparent PNG so the tile still shows (user can skip / upload)
        px = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                           "0000000d49444154789c626001000000050001"
                           "0d0a2db40000000049454e44ae426082")
        return Response(content=px, media_type="image/png", status_code=502)


def _proxy_wrap(url: str) -> str:
    """Wrap a third-party image URL to go through /api/proxy_ref.

    Skip our own origins (they don't need proxying and shouldn't add a hop).
    """
    if not url or not url.startswith(("http://", "https://")):
        return url
    # Don't proxy our own domain(s) — that would infinite-loop
    if PUBLIC_ORIGIN and url.startswith(PUBLIC_ORIGIN):
        return url
    if "nothollywood.ai" in url or "nothollywood-production.up.railway.app" in url:
        return url
    from urllib.parse import quote
    return f"/api/proxy_ref?url={quote(url, safe='')}"


@app.get("/api/_debug/library_probe")
def library_probe(user_id: str):
    """Unauthed diagnostic that dumps what /api/library would return for a
    given user_id, including per-row signing status. Public but harmless:
    the signed URLs are the same 1-hour URLs the user would get in-app.

    Only used to diagnose 'videos not showing in library' bugs remotely,
    since we can't sign in as another user from this sandbox.
    """
    import traceback
    import library as _lib
    out = {"user_id": user_id, "rows": []}
    try:
        # Retroactive rescue for this user's jobs (same logic as /api/library)
        rescued = 0
        for jid, job in list(JOBS.items()):
            if job.get("user_id") != user_id:
                continue
            if job.get("status") != "done":
                continue
            if job.get("saved_to_library"):
                continue
            video_rel = job.get("video") or ""
            if not video_rel.startswith("/static/videos/"):
                continue
            local_path = VIDEOS / f"{jid}.mp4"
            if not local_path.exists() or local_path.stat().st_size == 0:
                continue
            meta = {
                "title": job.get("franchise_title"),
                "slug": job.get("franchise_slug"),
                "duration": job.get("duration") or sum(job.get("scenes") or []),
                "resolution": job.get("resolution"),
                "scene_count": len(job.get("scenes") or [1]),
                "scenes": job.get("scenes") or [],
                "franchise_ref_url": job.get("ref_url_used") or job.get("franchise_ref_url"),
            }
            ok_save = _lib.save_render_to_library(
                job_id=jid, user_id=user_id, prompt=job.get("prompt") or "",
                video_path=local_path, meta=meta,
            )
            if ok_save:
                job["saved_to_library"] = True
                save_jobs(JOBS)
                rescued += 1
        out["retroactive_rescued"] = rescued

        rows = _lib.list_renders(user_id)
        out["row_count"] = len(rows)
        for row in rows:
            sp = row.get("storage_path") or ""
            entry = {"id": row.get("id"), "created": row.get("created_at"),
                     "storage_path": sp, "bytes": row.get("bytes"),
                     "duration": row.get("duration")}
            if sp.startswith("LOCAL:"):
                local_file = VIDEOS / sp.split(":", 1)[1]
                entry["backend"] = "local"
                entry["exists_on_disk"] = local_file.exists()
                entry["disk_size"] = local_file.stat().st_size if local_file.exists() else 0
            else:
                entry["backend"] = "supabase"
                signed = _lib.signed_url_for(sp, ttl_seconds=600)
                entry["signable"] = bool(signed)
            out["rows"].append(entry)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["tb"] = traceback.format_exc()[-1500:]
    return out


@app.get("/api/_debug/plan_probe")
def plan_probe(prompt: str, duration: int = 6):
    """Unauthed probe that runs the /api/plan pipeline and reports exactly
    where it fails or what it returns. Zach uses this from the agent sandbox
    to diagnose 'preview modal never shows candidates' bugs on Railway.

    Safe to expose publicly: it returns the same public search results a
    signed-in user would see, and does not touch any user's library.
    """
    import traceback, time
    out = {"prompt": prompt, "duration": duration, "steps": []}
    try:
        t0 = time.time()
        info = extract_show_info(prompt)
        title = info[0] if info else None
        kind = info[1] if info else "unknown"
        year = info[2] if info else None
        out["steps"].append({"step": "extract_show_info", "ms": int((time.time()-t0)*1000),
                             "title": title, "kind": kind, "year": year})
        if not title:
            out["result"] = "no_title_detected"
            return out
        slug = _slugify_title(title)
        out["slug"] = slug

        # Cache check
        cached_hit = None
        for ext in ("png", "jpg", "webp"):
            p = FRANCHISE_REFS / f"v2-{slug}.{ext}"
            if p.exists() and p.stat().st_size >= 5_000:
                cached_hit = p.name
                break
        out["cached"] = cached_hit
        out["public_origin"] = PUBLIC_ORIGIN

        # DDG
        t1 = time.time()
        try:
            cands = search_candidates_duckduckgo(title, want=6, kind=kind, year=year)
            out["steps"].append({"step": "ddg", "ms": int((time.time()-t1)*1000),
                                 "returned": len(cands),
                                 "first_url": cands[0]["url"] if cands else None})
        except Exception as e:
            out["steps"].append({"step": "ddg", "ms": int((time.time()-t1)*1000),
                                 "error": f"{type(e).__name__}: {e}",
                                 "tb": traceback.format_exc()[-500:]})
            cands = []

        # xAI fallback probe (do NOT actually generate — too slow / expensive)
        if not cands and not cached_hit:
            try:
                from franchise_ref import _generate_cast_still_xai, XAI_API_KEY
                out["xai_available"] = bool(XAI_API_KEY)
            except Exception as e:
                out["xai_available"] = f"import_error: {e}"

        out["result"] = "ok"
        out["total_candidates"] = (1 if cached_hit else 0) + len(cands)
    except Exception as e:
        out["result"] = "error"
        out["error"] = f"{type(e).__name__}: {e}"
        out["tb"] = traceback.format_exc()[-1000:]
    return out


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
    # extract_show_info gives us kind + year so the picker doesn't pull
    # anime/game fanart when the show name (e.g. 'Raymond') collides.
    info = extract_show_info(prompt)
    title = info[0] if info else None
    kind = info[1] if info else "unknown"
    year = info[2] if info else None
    candidates: list[dict] = []
    slug: str | None = None
    if title:
        slug = _slugify_title(title)
        # Serve any pre-baked cache hit as the first option so users see
        # 'this is what we already have for this show' at the top.
        # Only trust the v2-prefixed cache — pre-v2 unprefixed files are
        # drifted Grok outputs from the pre-DDG-priority era.
        if PUBLIC_ORIGIN:
            for ext in ("png", "jpg", "webp"):
                cached = FRANCHISE_REFS / f"v2-{slug}.{ext}"
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
        # -- frontend can dedupe if it wants). Passing kind/year makes the
        # picker use scoped queries + heavier fanart penalty for live-action.
        for c in search_candidates_duckduckgo(title, want=6, kind=kind, year=year):
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
                    fname = f"v2-{slug}.{ext}"
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

    # For long-form renders (>= 60s), do a two-pass workflow: return only
    # the story outline for the user to approve/edit, and defer scene
    # expansion until /api/generate is called with the approved outline.
    # This gives users real subplot structure (A-story + B-story + tag)
    # instead of 100+ continuous-action prompts, and lets them catch bad
    # casting for ~1¢ instead of after a $6 render.
    #
    # For short renders (< 60s / < 10 scenes) the outline gate would just
    # slow down the fun — skip straight to scene expansion as before.
    use_outline = duration >= 60
    if use_outline:
        outline_result = plan_outline(prompt, duration_s=duration)
        return {
            "title": title,
            "slug": slug,
            "scenes_plan": scenes_plan,
            "outline": outline_result.get("outline"),
            "outline_ok": outline_result.get("ok", False),
            "outline_provider": outline_result.get("provider"),
            "outline_error": outline_result.get("error"),
            "outline_thread_mode": outline_result.get("mode", "single"),
            "candidates": candidates,
            "mode": "outline",  # frontend renders the approval card
        }

    # Short renders: one-shot expansion, same as before.
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
        "mode": "scenes",  # frontend renders the storyboard as before
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
        # Proxy third-party URLs through our origin so hotlink-protected
        # hosts (ew.com, colliderimages, futurecdn, srcdn, etc.) always load
        # in the browser. Keeps original URL in _raw for debugging.
        out.append({
            "url": _proxy_wrap(url),
            "_raw": url,
            "width": w, "height": h,
            "thumbnail": _proxy_wrap(thumb),
            "source": "search",
        })
        if len(out) >= 6:
            break
    return {"candidates": out, "query_used": search_q}


@app.get("/api/library")
async def get_library(request: Request):
    """Return the current user's saved renders, newest first.

    Each item includes a short-lived signed video URL and (if we have one) a
    signed thumbnail URL. URLs expire in 1 hour, matching Supabase's default
    signed-URL policy. Anonymous callers get a 401.

    Retroactive rescue: any completed job in JOBS that belongs to the caller
    and hasn't been saved yet gets uploaded on this read. This heals renders
    that finished before the library-save code shipped, or that missed the
    upload for any transient reason.
    """
    claims = require_user(request)
    user_id = claims.get("sub") or ""
    if not user_id:
        raise HTTPException(401, "authentication required")
    import library as _lib

    # Retroactive save: scan this user's completed jobs, save any that
    # aren't marked saved_to_library. Best-effort; failures are logged but
    # never block the listing.
    for jid, job in list(JOBS.items()):
        if job.get("user_id") != user_id:
            continue
        if job.get("status") != "done":
            continue
        if job.get("saved_to_library"):
            continue
        video_rel = job.get("video") or ""
        if not video_rel.startswith("/static/videos/"):
            continue
        local_path = VIDEOS / f"{jid}.mp4"
        if not local_path.exists() or local_path.stat().st_size == 0:
            continue
        try:
            meta = {
                "title": job.get("franchise_title"),
                "slug": job.get("franchise_slug"),
                "duration": job.get("duration") or sum(job.get("scenes") or []),
                "resolution": job.get("resolution"),
                "scene_count": len(job.get("scenes") or [1]),
                "scenes": job.get("scenes") or [],
                "franchise_ref_url": job.get("ref_url_used") or job.get("franchise_ref_url"),
            }
            ok_save = _lib.save_render_to_library(
                job_id=jid,
                user_id=user_id,
                prompt=job.get("prompt") or "",
                video_path=local_path,
                meta=meta,
            )
            if ok_save:
                job["saved_to_library"] = True
                save_jobs(JOBS)
                print(f"[library retro] rescued {jid} for {user_id[:8]}")
        except Exception as e:
            print(f"[library retro] {jid} failed: {e}")

    rows = _lib.list_renders(user_id)
    out = []
    for row in rows:
        storage_path = row.get("storage_path") or ""
        thumb_path = row.get("thumb_path") or ""

        # LOCAL-served rows: the mp4 was too big for Supabase's per-file cap,
        # so we recorded the row with a LOCAL: sentinel and kept the file on
        # the Railway persistent volume. Serve it straight from the static
        # route — same URL the just-finished-render UI uses.
        if storage_path.startswith("LOCAL:"):
            local_name = storage_path.split(":", 1)[1]
            local_file = VIDEOS / local_name
            if not local_file.exists() or local_file.stat().st_size == 0:
                # File got wiped from disk (rare — usually a deploy on a
                # non-persistent volume). Skip the tile so the frontend
                # doesn't render a broken player.
                print(f"[library] LOCAL row {row.get('id')} points at missing file {local_file}")
                continue
            video_url = f"/static/videos/{local_name}"
            if thumb_path.startswith("LOCAL:"):
                thumb_name = thumb_path.split(":", 1)[1]
                thumb_file = VIDEOS / thumb_name
                thumb_url = f"/static/videos/{thumb_name}" if thumb_file.exists() else None
            else:
                thumb_url = None
        else:
            video_url = _lib.signed_url_for(storage_path, ttl_seconds=3600)
            thumb_url = _lib.signed_url_for(thumb_path, ttl_seconds=3600) if thumb_path else None
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
        "fal_key": bool(FAL_KEY),
        "active_provider": ("fal" if FAL_KEY else "none"),
        "public_origin": bool(PUBLIC_ORIGIN),
        "resend_email": bool(os.environ.get("RESEND_API_KEY")),
        "email_from": EMAIL_FROM if os.environ.get("RESEND_API_KEY") else None,
        "supabase_service_key": bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY")),
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
    chosen_outline: str = Form(""),  # JSON outline dict from /api/plan (long form)
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

    # ── Reference-image gate ────────────────────────────────────────
    # Every render must have a reference image. Users either upload one or
    # pick one from the /api/plan candidate grid. Auto-resolve via DDG /
    # Grok is now only used as a picker candidate, not as a silent
    # fallback for a bare /api/generate call. Skip enforcement in
    # AUTH_DISABLED mode so smoke tests still work.
    if (
        not AUTH_DISABLED
        and reference is None
        and not (chosen_ref_url and chosen_ref_url.startswith("http"))
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "reference_required",
                "message": (
                    "A reference image is required. Upload one, or pick a "
                    "candidate from the show-reference step first."
                ),
            },
        )

    # ── Credit gate ─────────────────────────────────────────────────
    # Debit BEFORE we do any expensive work. If the render fails we refund
    # in the worker's failure paths. Skipped in AUTH_DISABLED mode (no user).
    # Whitelisted user_ids (staff / unlimited accounts) skip debit entirely
    # so their balance can never deplete.
    render_cost = credit_cost_for(duration, resolution)
    credits_debited = 0
    if _is_unlimited(user_id, user_email):
        print(f"[credits] unlimited-account render for {user_id or user_email} (skip debit)")
    elif user_id and SUPABASE_SERVICE_ROLE_KEY:
        ok, msg, new_balance = _adjust_credits_via_supabase(user_id, -render_cost)
        if not ok:
            if "insufficient credits" in msg:
                # 402 Payment Required — the frontend can catch this and open
                # the pricing page instead of showing a generic error.
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "insufficient_credits",
                        "required": render_cost,
                        "resolution": resolution,
                        "duration": duration,
                        "message": (
                            f"You need {render_cost} credits to render "
                            f"{duration}s at {resolution}. Add more credits to continue."
                        ),
                    },
                )
            # Supabase glitch — refuse the render rather than let it through
            # for free, since we can't guarantee we'll be able to debit later.
            print(f"[credits] debit failed for {user_id}: {msg}")
            raise HTTPException(status_code=503, detail="credits system unavailable, try again in a moment")
        credits_debited = render_cost
        print(f"[credits] debited {render_cost} from {user_id}: {msg}")

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

    # Pre-approved story outline from /api/plan (long-form two-pass flow).
    # Only used when the worker runs its own expansion — if the user already
    # sent chosen_scenes we honor those verbatim and ignore the outline.
    preapproved_outline: dict | None = None
    if chosen_outline and not preapproved_scene_prompts:
        try:
            parsed_outline = json.loads(chosen_outline)
            if isinstance(parsed_outline, dict):
                preapproved_outline = parsed_outline
            else:
                raise HTTPException(400, "chosen_outline must be a JSON object")
        except json.JSONDecodeError:
            raise HTTPException(400, "chosen_outline is not valid JSON")

    # Save uploaded reference under /static/frames/ so fal can fetch it.
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
        # the reference. Better than sending a data URL that fal will reject.
    elif chosen_ref_url and (chosen_ref_url.startswith("http") or chosen_ref_url.startswith("/api/proxy_ref")):
        # User picked a candidate in /api/plan. The candidate URL may be:
        #  - A raw third-party URL (older client, or our own cached URL)
        #  - Our /api/proxy_ref?url=<encoded> wrapper (new client) — unwrap first
        # Then download to /static/franchise-refs/ so fal gets a URL we
        # control (avoids CDN hotlink protection / 403s from image models).
        raw_url = chosen_ref_url
        if chosen_ref_url.startswith("/api/proxy_ref"):
            from urllib.parse import urlparse, parse_qs, unquote
            qs = parse_qs(urlparse(chosen_ref_url).query)
            raw_url = unquote((qs.get("url") or [""])[0]) or chosen_ref_url
        try:
            result = _download_and_validate(raw_url)
            if result is not None and PUBLIC_ORIGIN:
                blob, ext = result
                out = FRANCHISE_REFS / f"chosen_{job_id}.{ext}"
                out.write_bytes(blob)
                ref_url = f"{PUBLIC_ORIGIN}/static/franchise-refs/{out.name}"
            else:
                # Only pass raw http(s) URLs to the model; our proxy path is
                # not reachable from fal.
                ref_url = raw_url if raw_url.startswith("http") else None
        except Exception as e:  # noqa: BLE001
            print(f"[generate] chosen_ref_url download failed: {e}")
            ref_url = raw_url if raw_url.startswith("http") else None
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
        "preapproved_outline": preapproved_outline,
        "credits_debited": credits_debited,  # for refund on failure
        "credits_refunded": False,
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


@app.get("/api/_debug/pending_saves")
def debug_pending_saves(request: Request):
    """Admin-only view of the persistent library save-retry queue. Any render
    that failed to persist to Supabase on completion lives here until the
    reaper drains it. Zach uses this to diagnose the "my library is missing
    a render" class of bugs.

    Header: X-Admin-Token: <ADMIN_TOKEN>
    """
    import os as _os
    admin_token = _os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        return JSONResponse({"error": "admin token not configured"}, status_code=503)
    supplied = request.headers.get("X-Admin-Token", "")
    if supplied != admin_token:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        import library as _lib
        entries = _lib._load_pending()
        return {"total": len(entries), "entries": entries}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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


# Hash the frontend bundle on startup so /?v=<hash> forces a cold cache load
# whenever we ship new JS/CSS. Without this, browsers pin app.js forever and
# users see stale UX (e.g. John still hits the 'reference required' block
# because his cached app.js predates the empty-state upload button).
def _asset_version() -> str:
    import hashlib
    h = hashlib.sha1()
    for name in ("app.js", "styles.css", "index.html"):
        p = STATIC / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]

_ASSET_VERSION = _asset_version()
print(f"[assets] version={_ASSET_VERSION}")


@app.get("/")
def index():
    # Rewrite <script src="app.js"> and <link href="styles.css"> to include
    # the version tag so a fresh deploy busts every user's browser cache.
    html = (STATIC / "index.html").read_text()
    html = html.replace('href="styles.css"', f'href="styles.css?v={_ASSET_VERSION}"')
    html = html.replace('src="app.js"', f'src="app.js?v={_ASSET_VERSION}"')
    return HTMLResponse(html)


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
