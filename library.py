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
import subprocess
from pathlib import Path
from typing import Optional

import requests


SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = "renders"
_REQ_TIMEOUT = 30


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


def _upload_to_storage(storage_path: str, local_path: Path, content_type: str) -> bool:
    """POST a file into Supabase Storage. Overwrite if the object already
    exists (x-upsert=true) — makes retries idempotent when a job resubmits.
    """
    if not library_enabled():
        return False
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}"
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
        if r.status_code >= 300:
            print(f"[library] upload {storage_path} failed ({r.status_code}): {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[library] upload {storage_path} exception: {e}")
        return False


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

    # 1) Upload the MP4
    storage_path = f"{user_id}/{job_id}.mp4"
    ok = _upload_to_storage(storage_path, video_path, "video/mp4")
    if not ok:
        return False

    # 2) Best-effort thumbnail. If it fails, we still record the render row
    # and the frontend will just show a video-icon placeholder.
    thumb_path_str: Optional[str] = None
    tmp_thumb = video_path.parent / f"{job_id}_thumb.jpg"
    if _extract_thumbnail(video_path, tmp_thumb):
        thumb_storage = f"{user_id}/{job_id}_thumb.jpg"
        if _upload_to_storage(thumb_storage, tmp_thumb, "image/jpeg"):
            thumb_path_str = thumb_storage
        try:
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
        if r.status_code >= 300:
            print(f"[library] insert row failed ({r.status_code}): {r.text[:300]}")
            return False
    except Exception as e:
        print(f"[library] insert row exception: {e}")
        return False

    print(f"[library] saved {job_id} for {user_id} ({bytes_/1024/1024:.1f}MB)")
    return True


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
