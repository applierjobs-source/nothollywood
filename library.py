"""Not Hollywood — persistent library for user renders.

Uploads finished MP4s to Supabase Storage and records metadata in the
`renders` table so users can revisit their videos across deploys and
browser reloads. All operations are best-effort — a library-write failure
never fails the render itself.

Design constraints:
- Uses the service_role Supabase key (SUPABASE_SERVICE_ROLE_KEY) via REST.
- No dependency on the supabase-py SDK; keeps the container slim.
- RLS is on the renders table: users only see their own rows via anon key.
- File size limit is 50MB per file (bucket config) — our 20-min renders at
  720p typically land 30-45MB. A future 4K upgrade would need larger cap.

Public API:
    save_render_to_library(job_id, user_id, prompt, video_path, meta) -> bool
    list_renders(user_id) -> list[dict]        # server helper (service key)
    signed_url_for(storage_path, ttl=3600) -> str | None
    delete_render(job_id, user_id) -> bool
"""

from __future__ import annotations

import os
import json
import time
import threading
import subprocess
from pathlib import Path
from typing import Optional

import requests


SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = "renders"
_REQ_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Retry + persistent save queue
# ---------------------------------------------------------------------------
#
# Renders used to silently disappear from user libraries when a Supabase
# upload timed out or returned a transient 5xx during the one-shot save at
# render-completion time. The retroactive rescue in /api/library only worked
# while the local .mp4 still existed on Railway's disk, which is wiped on
# every deploy. Users noticed by pinging Zach; we noticed by reading logs.
#
# Fix: every save that fails writes a row to pending_saves.json (on the same
# persistent volume as jobs.json / static/videos). A background thread
# started by server.py pops entries off that queue and retries every couple
# of minutes until success. Success removes the entry.
#
# Data model (list of dicts, newest first):
#   {"job_id": str, "user_id": str, "prompt": str, "video_path": str,
#    "meta": {...}, "attempts": int, "last_error": str,
#    "last_attempt_ts": float, "enqueued_ts": float}

ROOT = Path(__file__).resolve().parent
PENDING_SAVES_FILE = ROOT / "pending_saves.json"
_PENDING_LOCK = threading.Lock()


def _load_pending() -> list[dict]:
    if not PENDING_SAVES_FILE.exists():
        return []
    try:
        with open(PENDING_SAVES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[library] pending_saves.json read failed: {e}")
        return []


def _save_pending(entries: list[dict]) -> None:
    try:
        tmp = PENDING_SAVES_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(entries, f, indent=2)
        tmp.replace(PENDING_SAVES_FILE)
    except Exception as e:
        print(f"[library] pending_saves.json write failed: {e}")


def _enqueue_pending(job_id: str, user_id: str, prompt: str,
                    video_path: Path, meta: dict, error: str) -> None:
    """Add a failed save to the retry queue. Deduplicates on job_id so
    repeated failures accumulate attempt count on a single row.
    """
    with _PENDING_LOCK:
        entries = _load_pending()
        now = time.time()
        found = False
        for e in entries:
            if e.get("job_id") == job_id:
                e["attempts"] = int(e.get("attempts", 0)) + 1
                e["last_error"] = error
                e["last_attempt_ts"] = now
                found = True
                break
        if not found:
            entries.insert(0, {
                "job_id": job_id,
                "user_id": user_id,
                "prompt": prompt,
                "video_path": str(video_path),
                "meta": meta or {},
                "attempts": 1,
                "last_error": error,
                "last_attempt_ts": now,
                "enqueued_ts": now,
            })
        _save_pending(entries)
        print(f"[library] pending save queued: {job_id} ({len(entries)} in queue)")


def _dequeue_pending(job_id: str) -> None:
    with _PENDING_LOCK:
        entries = _load_pending()
        new = [e for e in entries if e.get("job_id") != job_id]
        if len(new) != len(entries):
            _save_pending(new)


def pending_count() -> int:
    return len(_load_pending())


def _svc_headers(json_ct: bool = False) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    if json_ct:
        h["Content-Type"] = "application/json"
    return h


def library_enabled() -> bool:
    """Library requires both a URL and a service-role key to work.

    We check both here so callers can no-op cheaply when either is missing
    (e.g. local dev without Supabase configured).
    """
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _extract_thumbnail(video_path: Path, out_path: Path) -> bool:
    """Grab a JPEG thumbnail 1 second into the video. Best-effort — returns
    False on any ffmpeg error and callers should tolerate a missing thumb.
    """
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", "1", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "3",
                "-vf", "scale=640:-2",
                str(out_path),
            ],
            capture_output=True,
            timeout=30,
        )
        return r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:
        print(f"[library] thumbnail extraction failed: {e}")
        return False


def _upload_to_storage(storage_path: str, local_path: Path, content_type: str,
                       max_attempts: int = 4) -> tuple[bool, str]:
    """POST a file into Supabase Storage with exponential backoff. Overwrites
    if the object already exists (x-upsert=true) so retries are idempotent.

    Returns (ok, error_message). error_message is empty on success.

    Retries on network exceptions and 5xx responses; a 4xx is treated as a
    permanent failure and we bail immediately (invalid path, auth, bucket).
    """
    if not library_enabled():
        return False, "library not enabled"
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}"
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            with open(local_path, "rb") as f:
                r = requests.post(
                    url,
                    headers={
                        **_svc_headers(),
                        "Content-Type": content_type,
                        "x-upsert": "true",
                    },
                    data=f,
                    timeout=120,
                )
            if r.status_code < 300:
                return True, ""
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            # 4xx = permanent (bad path, auth, bucket missing). Don't retry.
            if 400 <= r.status_code < 500:
                print(f"[library] upload {storage_path} permanent failure: {last_err}")
                return False, last_err
            print(f"[library] upload {storage_path} attempt {attempt}/{max_attempts} "
                  f"failed: {last_err}")
        except Exception as e:
            last_err = f"exception: {e}"
            print(f"[library] upload {storage_path} attempt {attempt}/{max_attempts} "
                  f"exception: {e}")
        if attempt < max_attempts:
            # Backoff: 2s, 4s, 8s. Cap short so render-completion path stays
            # snappy; the persistent queue handles longer-term retries.
            time.sleep(2 ** attempt)
    return False, last_err


def save_render_to_library(
    job_id: str,
    user_id: str,
    prompt: str,
    video_path: Path,
    meta: Optional[dict] = None,
) -> bool:
    """Upload the finished MP4 + a thumbnail to Supabase Storage and insert
    a row into public.renders. Returns True on success, False on any failure.

    Non-fatal: every failure path prints a warning and returns False so the
    caller (multi_scene_worker) never crashes from a library issue.

    Args:
        job_id: canonical job id (used as primary key + storage filename)
        user_id: supabase auth uuid; falsy user_id short-circuits (anonymous
            renders are not saved to the library)
        prompt: raw prompt the user typed
        video_path: absolute path to the finished MP4 on the container disk
        meta: optional dict with any of:
            title, slug, duration, resolution, scene_count, scenes,
            franchise_ref_url
    """
    if not library_enabled():
        return False
    if not user_id:
        # Anonymous renders (legacy password-only mode) can't be saved because
        # we don't have a user to attribute them to. Skip silently.
        return False
    if not video_path.exists() or video_path.stat().st_size == 0:
        print(f"[library] refusing to save empty video for job {job_id}")
        return False

    meta = meta or {}
    bytes_ = video_path.stat().st_size

    # 1) Upload the MP4 (with retry). Two failure modes matter here:
    #   - Transient (5xx / network): retry a few times inline; if still failing,
    #     park in the persistent queue and let the reaper try later.
    #   - Permanent (413 EntityTooLarge from Supabase's per-file cap): the
    #     bucket physically can't hold this file. Instead of dropping the row,
    #     record the render with a LOCAL: sentinel so /api/library serves it
    #     from Railway's static-videos volume. The row survives redeploys of
    #     the app code, the mp4 survives on the mounted volume, and the user
    #     never loses the render again.
    #
    # NB: local-served rows have `storage_path='LOCAL:{job_id}.mp4'` and no
    # thumb_path. /api/library special-cases this prefix.
    storage_path = f"{user_id}/{job_id}.mp4"
    ok, err = _upload_to_storage(storage_path, video_path, "video/mp4")
    served_locally = False
    if not ok:
        # Distinguish the size cap from other 4xx / 5xx errors. A 413 is
        # permanent for this project's plan tier — no amount of retrying
        # will move a >50 MB file into the Free-tier bucket.
        is_size_cap = ("413" in err) or ("EntityTooLarge" in err) or ("Payload too large" in err.lower() if err else False)
        if is_size_cap:
            print(f"[library] {job_id} exceeds Supabase per-file cap; "
                  f"falling back to LOCAL-served row so it stays in the library")
            storage_path = f"LOCAL:{job_id}.mp4"
            served_locally = True
        else:
            _enqueue_pending(job_id, user_id, prompt, video_path, meta,
                             f"mp4 upload failed: {err}")
            return False

    # 2) Best-effort thumbnail. If it fails, we still record the render row
    # and the frontend will just show a video-icon placeholder. Skip the
    # thumbnail entirely for LOCAL-served rows — they'd hit the same 413 for
    # any thumb over the cap, and a JPEG thumb is small enough to serve
    # locally too if we ever need it.
    thumb_path_str: Optional[str] = None
    tmp_thumb = video_path.parent / f"{job_id}_thumb.jpg"
    if _extract_thumbnail(video_path, tmp_thumb):
        if served_locally:
            # Keep the JPEG on disk so /static/videos/<id>.jpg can serve it;
            # server.py's own worker already writes this path when the render
            # completes, so this is defensive only.
            try:
                local_thumb_dest = video_path.parent / f"{job_id}.jpg"
                if not local_thumb_dest.exists():
                    tmp_thumb.replace(local_thumb_dest)
                thumb_path_str = f"LOCAL:{job_id}.jpg"
            except Exception:
                pass
        else:
            thumb_storage = f"{user_id}/{job_id}_thumb.jpg"
            thumb_ok, _ = _upload_to_storage(thumb_storage, tmp_thumb, "image/jpeg")
            if thumb_ok:
                thumb_path_str = thumb_storage
        try:
            if tmp_thumb.exists():
                tmp_thumb.unlink()
        except Exception:
            pass

    # 3) Insert row into public.renders. We use POST with Prefer:
    # resolution=merge-duplicates so a repeated save is idempotent.
    row = {
        "id": job_id,
        "user_id": user_id,
        "prompt": prompt,
        "title": meta.get("title"),
        "slug": meta.get("slug"),
        "storage_path": storage_path,
        "thumb_path": thumb_path_str,
        "duration": int(meta.get("duration") or 0),
        "resolution": meta.get("resolution"),
        "scene_count": int(meta.get("scene_count") or 1),
        "scenes": meta.get("scenes"),
        "franchise_ref_url": meta.get("franchise_ref_url"),
        "bytes": bytes_,
    }
    url = f"{SUPABASE_URL}/rest/v1/renders"
    row_err = ""
    for attempt in range(1, 4):  # 3 tries: immediate, +2s, +4s
        try:
            r = requests.post(
                url,
                headers={
                    **_svc_headers(json_ct=True),
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                data=json.dumps(row),
                timeout=_REQ_TIMEOUT,
            )
            if r.status_code < 300:
                row_err = ""
                break
            row_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if 400 <= r.status_code < 500:
                print(f"[library] insert row permanent failure: {row_err}")
                break
            print(f"[library] insert row attempt {attempt}/3 failed: {row_err}")
        except Exception as e:
            row_err = f"exception: {e}"
            print(f"[library] insert row attempt {attempt}/3 exception: {e}")
        if attempt < 3:
            time.sleep(2 ** attempt)

    if row_err:
        # Mp4 (and maybe thumb) are already in Supabase Storage; only the DB
        # row is missing. Queue for retry — storage upsert is idempotent so
        # a re-run just overwrites bytes-identical objects and then inserts.
        _enqueue_pending(job_id, user_id, prompt, video_path, meta,
                         f"row insert failed: {row_err}")
        return False

    # Success — remove any previous queued entry for this job.
    _dequeue_pending(job_id)
    print(f"[library] saved {job_id} for {user_id} ({bytes_/1024/1024:.1f}MB)")
    return True


# ---------------------------------------------------------------------------
# Reaper: retry pending saves in the background
# ---------------------------------------------------------------------------

_REAPER_STARTED = False


def _reaper_loop(interval_s: float) -> None:
    """Wake up every `interval_s` seconds, pop each pending save, and try it
    again. Runs forever inside a daemon thread — dies with the process.
    """
    while True:
        try:
            time.sleep(interval_s)
            pending = _load_pending()
            if not pending:
                continue
            print(f"[library] reaper: {len(pending)} pending saves to retry")
            for entry in list(pending):
                job_id = entry.get("job_id") or ""
                user_id = entry.get("user_id") or ""
                video_path = Path(entry.get("video_path") or "")
                if not video_path.exists():
                    # The local mp4 got wiped (deploy, disk cleanup). Nothing
                    # to re-upload — drop the entry and log loudly so the
                    # operator can decide whether to recover from a backup.
                    print(f"[library] reaper: dropping {job_id}, source mp4 "
                          f"missing at {video_path}")
                    _dequeue_pending(job_id)
                    continue
                # Cap attempts so a permanently-broken job doesn't spin forever
                # (bad user_id column, bucket removed, etc.). 20 attempts at
                # 3-minute intervals ≈ 1 hour of retries.
                if int(entry.get("attempts", 0)) >= 20:
                    print(f"[library] reaper: giving up on {job_id} after "
                          f"{entry.get('attempts')} attempts; last error: "
                          f"{entry.get('last_error')}")
                    _dequeue_pending(job_id)
                    continue
                try:
                    save_render_to_library(
                        job_id=job_id,
                        user_id=user_id,
                        prompt=entry.get("prompt") or "",
                        video_path=video_path,
                        meta=entry.get("meta") or {},
                    )
                except Exception as e:
                    print(f"[library] reaper: {job_id} raised: {e}")
        except Exception as e:
            # Reaper must never die. Log and keep looping.
            print(f"[library] reaper loop exception (continuing): {e}")


def start_reaper(interval_s: float = 180.0) -> None:
    """Idempotently start the background reaper. Safe to call multiple times
    (subsequent calls no-op). Server.py calls this once on startup.
    """
    global _REAPER_STARTED
    if _REAPER_STARTED:
        return
    if not library_enabled():
        print("[library] reaper not started: library not enabled")
        return
    t = threading.Thread(target=_reaper_loop, args=(interval_s,),
                         name="library-reaper", daemon=True)
    t.start()
    _REAPER_STARTED = True
    print(f"[library] reaper started (interval={interval_s}s, "
          f"pending={pending_count()})")


def list_renders(user_id: str) -> list[dict]:
    """Fetch all renders for a user, newest first. Returns [] on any error.

    Uses the service-role key so RLS is bypassed — the endpoint filters by
    user_id manually. The endpoint that calls this is behind require_user()
    so we know user_id is authenticated.
    """
    if not library_enabled() or not user_id:
        return []
    url = f"{SUPABASE_URL}/rest/v1/renders"
    try:
        r = requests.get(
            url,
            headers=_svc_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "select": "*",
                "order": "created_at.desc",
                "limit": "200",
            },
            timeout=_REQ_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[library] list failed ({r.status_code}): {r.text[:200]}")
            return []
        return r.json() or []
    except Exception as e:
        print(f"[library] list exception: {e}")
        return []


def signed_url_for(storage_path: str, ttl_seconds: int = 3600) -> Optional[str]:
    """Ask Supabase for a signed URL to a private object. ttl_seconds bounds
    how long the URL is valid — 1 hour default keeps the trust window narrow.
    """
    if not library_enabled() or not storage_path:
        return None
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{storage_path}"
    try:
        r = requests.post(
            url,
            headers=_svc_headers(json_ct=True),
            data=json.dumps({"expiresIn": ttl_seconds}),
            timeout=_REQ_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[library] sign {storage_path} failed ({r.status_code}): {r.text[:200]}")
            return None
        signed = r.json().get("signedURL") or ""
        if not signed:
            return None
        # signedURL is a path — prefix with the storage base.
        if signed.startswith("/"):
            return f"{SUPABASE_URL}/storage/v1{signed}"
        return f"{SUPABASE_URL}/storage/v1/{signed}"
    except Exception as e:
        print(f"[library] sign exception: {e}")
        return None


def delete_render(job_id: str, user_id: str) -> bool:
    """Delete a render owned by user_id. Returns True on success.

    Removes both the DB row (row-level filter by user_id + id) and the two
    storage objects. Anything that fails is logged but does not abort the
    other deletions — best-effort cleanup.
    """
    if not library_enabled() or not user_id or not job_id:
        return False
    # 1) DB row — but only if user_id matches, so we don't wipe someone else's.
    try:
        r = requests.delete(
            f"{SUPABASE_URL}/rest/v1/renders",
            headers=_svc_headers(),
            params={"id": f"eq.{job_id}", "user_id": f"eq.{user_id}"},
            timeout=_REQ_TIMEOUT,
        )
        row_ok = r.status_code in (200, 204)
    except Exception as e:
        print(f"[library] delete row exception: {e}")
        row_ok = False

    # 2) Storage objects — path derives from user_id + job_id so this is
    # inherently owner-scoped.
    try:
        requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{user_id}/{job_id}.mp4",
            headers=_svc_headers(),
            timeout=_REQ_TIMEOUT,
        )
        requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{user_id}/{job_id}_thumb.jpg",
            headers=_svc_headers(),
            timeout=_REQ_TIMEOUT,
        )
    except Exception as e:
        print(f"[library] delete storage exception: {e}")

    return row_ok
