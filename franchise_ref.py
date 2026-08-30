"""Franchise reference-pack: on-demand cast reference frame lookup.

When a user prompts "generate a <show> episode" and does not upload a
reference image, this module figures out what show they mean and produces
a public HTTPS URL to a cast reference frame that MiniMax H3 can use as
first_frame_url. Frame-chaining then locks the faces in for every
subsequent scene.

Pipeline (each step is skippable):

    1. Extract show title from prompt (regex first, LLM fallback if available).
    2. Look up disk cache at FRANCHISE_REFS/<slug>.png. Hit -> done.
    3. On miss: try Option B (web image search for a real cast still).
    4. If Option B fails: try Option A (LLM-described AI group shot).
    5. If everything fails: return None. Caller falls back to no-reference.

Every producing step writes the result to disk under FRANCHISE_REFS/<slug>.png
so we only pay the discovery cost once per franchise. Prepacked stills
committed in the repo (seinfeld.png, the-office.png) still take priority
because they're on disk before this ever runs.

Design constraints:
  * Must run on Railway (plain HTTPS + requests).
  * Never crash the generate handler on any failure -- always return a URL
    or None, log via print() on Railway logs.
  * No new hard dependencies -- uses stdlib + requests only.
  * All external services are optional; missing keys just skip that step.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Env-driven service configuration. Every one is optional.
# ---------------------------------------------------------------------------

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
# OpenAI images for AI-generated fallback ref (gpt-image-1 recommended).
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()
# LLM for ambiguous title extraction. Grok already wired for prompt_expander.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "").strip()
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-fast-non-reasoning").strip()

REQUEST_TIMEOUT = 30  # seconds; keep single request cheap


# ---------------------------------------------------------------------------
# Step 1: Show-title extraction
# ---------------------------------------------------------------------------

# Common patterns like "generate a Seinfeld episode", "an episode of Friends",
# "make a Curb Your Enthusiasm scene". Captures the show title as group 1.
# We keep these lenient -- if regex misses we fall through to LLM.
# Title body: capital-first tokens ONLY (case-sensitive), with allowed short
# lowercase glue words ('of', 'the', 'and') between capitalized tokens so
# names like 'Rick and Morty' or 'Curb Your Enthusiasm' still match. We stop
# as soon as we hit a lowercase content word ("where Michael quits", "about
# the gang") because those clearly aren't part of the title.
_TITLE_CORE = (
    r"[A-Z][\w'&.\-]*"
    r"(?:\s+(?:[A-Z][\w'&.\-]*|and|of|the|in|on|for|to|a|&))*"
)
_TITLE_BODY = rf"({_TITLE_CORE})"

# Trigger words are still matched case-insensitively via inline (?i:...)
# groups so 'Generate' / 'GENERATE' / 'generate' all work, while the title
# body regex stays case-sensitive.
_TRIGGERS_VERB = r"(?i:generate|make|create|write|produce)"
_TRIGGERS_ARTICLE = r"(?i:an?\s+)?"
_TRIGGERS_EPISODE = r"(?i:episode|scene|clip|pilot)"

_TITLE_REGEXES: tuple[re.Pattern, ...] = (
    # "episode of Seinfeld", "scene from Friends"
    re.compile(rf"(?i:episode|scene|clip|pilot)\s+(?i:of|from)\s+{_TITLE_BODY}"),
    # "generate a Seinfeld episode", "make an Office scene"
    re.compile(rf"{_TRIGGERS_VERB}\s+{_TRIGGERS_ARTICLE}{_TITLE_BODY}\s+{_TRIGGERS_EPISODE}"),
    # Leading article: "A Seinfeld episode where..."
    re.compile(rf"^\s*(?i:an?\s+){_TITLE_BODY}\s+{_TRIGGERS_EPISODE}\b"),
    # "Seinfeld episode" at start
    re.compile(rf"^\s*{_TITLE_BODY}\s+{_TRIGGERS_EPISODE}\b"),
)


# Words that show up in false-positive captures and should be rejected as
# titles. This is intentionally short -- LLM step handles edge cases.
_BAD_TITLES = {
    "the", "a", "an", "new", "another", "fake", "short", "long", "quick",
    "funny", "cool", "great", "sample", "test", "demo", "brief",
}


_GLUE_WORDS = {"and", "of", "the", "in", "on", "for", "to", "a", "&"}


def _clean_title(raw: str) -> str:
    t = re.sub(r"\s+", " ", raw).strip(" ,.:;'-\"")
    # Strip trailing lowercase glue words captured by the regex's optional tail
    # ("Breaking Bad in the" -> "Breaking Bad").
    parts = t.split(" ")
    while parts and parts[-1].lower() in _GLUE_WORDS:
        parts.pop()
    return " ".join(parts)


def _looks_like_title(title: str) -> bool:
    if not title:
        return False
    lowered = title.lower()
    if lowered in _BAD_TITLES:
        return False
    # Reject captures that are pure lowercase common words.
    if lowered.split()[0] in _BAD_TITLES and len(title.split()) == 1:
        return False
    return True


def _regex_extract_title(prompt: str) -> Optional[str]:
    for pat in _TITLE_REGEXES:
        m = pat.search(prompt)
        if not m:
            continue
        title = _clean_title(m.group(1))
        if _looks_like_title(title):
            return title
    return None


def _llm_extract_title(prompt: str) -> Optional[str]:
    """Ask Grok to name the show if the prompt clearly references one. Returns
    None on no key, ambiguous prompt, or any error. Never raises."""
    if not XAI_API_KEY:
        return None
    system = (
        "You extract a SINGLE TV show or movie title from a user's video-"
        "generation prompt. If the prompt clearly references an existing "
        "well-known TV show or movie by name, output that title. Otherwise "
        "output the exact string NONE.\n\n"
        "Output rules:\n"
        "- Just the canonical title, nothing else. No punctuation, no quotes.\n"
        "- No franchise expansion (Star Wars -> Star Wars, not A New Hope).\n"
        "- If the prompt only describes an original scene, output NONE.\n"
        "- If the show is fictional or made up by the user, output NONE."
    )
    body = {
        "model": XAI_MODEL,
        "max_tokens": 32,
        "system": system,
        "messages": [{"role": "user", "content": prompt[:2000]}],
    }
    try:
        r = requests.post(
            "https://api.x.ai/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": XAI_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            data=json.dumps(body),
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[franchise_ref] xai title extract http {r.status_code}: "
                  f"{r.text[:200]}")
            return None
        data = r.json()
        # Skip thinking blocks, read the first text block.
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                break
        text = text.strip().strip('"\'').strip()
        if not text or text.upper() == "NONE":
            return None
        # Guard against long, chatty replies.
        if len(text) > 80 or "\n" in text:
            return None
        return text
    except Exception as e:  # pragma: no cover -- defensive
        print(f"[franchise_ref] xai title extract exception: {e}")
        return None


def extract_show_title(prompt: str) -> Optional[str]:
    """Return canonical show title or None. Regex first (free, deterministic),
    LLM second (needs XAI_API_KEY). Never raises."""
    if not prompt:
        return None
    t = _regex_extract_title(prompt)
    if t:
        return t
    return _llm_extract_title(prompt)


# ---------------------------------------------------------------------------
# Slug + cache helpers
# ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    """Turn 'Curb Your Enthusiasm' -> 'curb-your-enthusiasm'.

    Used both to look up the disk cache and to derive the public URL, so it
    MUST be stable and safe as a filesystem name. We only allow [a-z0-9-].
    """
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unknown"


# ---------------------------------------------------------------------------
# Step 3 (Option B): Web image search + download
# ---------------------------------------------------------------------------

# Image-file magic bytes: JPEG FFD8FF, PNG 89504E47, WEBP RIFF....WEBP.
def _sniff_image(data: bytes) -> Optional[str]:
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _download_and_validate(url: str, min_bytes: int = 20_000,
                            max_bytes: int = 12_000_000) -> Optional[tuple[bytes, str]]:
    """Fetch URL, verify it's an image with real content, return (bytes, ext).
    Returns None on any failure -- network, HTTP error, wrong content type,
    too small (likely icon/logo), too big (probably not what we want)."""
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            # Some CDNs 403 without a UA.
            "User-Agent": "Mozilla/5.0 (NotHollywood franchise-ref fetcher)",
        }, stream=True)
        if r.status_code != 200:
            return None
        # Read up to max_bytes to avoid runaway downloads.
        buf = io.BytesIO()
        total = 0
        for chunk in r.iter_content(chunk_size=32_768):
            if not chunk:
                continue
            buf.write(chunk)
            total += len(chunk)
            if total > max_bytes:
                return None
        if total < min_bytes:
            return None
        data = buf.getvalue()
        ext = _sniff_image(data)
        if not ext:
            return None
        return data, ext
    except Exception as e:  # pragma: no cover -- defensive
        print(f"[franchise_ref] download failed for {url[:80]}: {e}")
        return None


def _search_cast_still_serpapi(title: str) -> Optional[tuple[bytes, str]]:
    """Query SerpAPI Google Images for '<title> cast', return validated bytes
    of the first candidate that downloads cleanly. Returns None on any failure.
    """
    if not SERPAPI_KEY:
        return None
    try:
        r = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_images",
                "q": f"{title} cast photo",
                "api_key": SERPAPI_KEY,
                "num": 20,
                # Prefer landscape group shots.
                "tbs": "isz:l",
                "safe": "active",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[franchise_ref] serpapi http {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
    except Exception as e:
        print(f"[franchise_ref] serpapi exception: {e}")
        return None

    for hit in (data.get("images_results") or [])[:12]:
        url = hit.get("original") or hit.get("thumbnail")
        if not url or not url.startswith("http"):
            continue
        got = _download_and_validate(url)
        if got:
            print(f"[franchise_ref] serpapi selected: {url[:100]}")
            return got
    print(f"[franchise_ref] serpapi: no candidates validated for '{title}'")
    return None


# ---------------------------------------------------------------------------
# Step 4 (Option A): OpenAI image-generation fallback
# ---------------------------------------------------------------------------

_OPENAI_CAST_PROMPT_TEMPLATE = (
    "Photorealistic wide-angle group photo of the main cast of the well-known "
    "TV show '{title}' in a signature setting from the show. All main "
    "characters visible together, natural expressions, wardrobe and hair "
    "matching the show's iconic look, cinematic lighting matching the show's "
    "visual style. Do not include captions, text overlays, watermarks, or "
    "on-screen graphics."
)


def _generate_cast_still_openai(title: str) -> Optional[tuple[bytes, str]]:
    """Ask OpenAI image API to produce a photorealistic cast group shot. Uses
    detailed physical description language rather than actor names. Returns
    None on no key or any failure."""
    if not OPENAI_API_KEY:
        return None
    prompt = _OPENAI_CAST_PROMPT_TEMPLATE.format(title=title)
    body = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "size": "1536x1024",  # 3:2 landscape, close to 16:9
        "n": 1,
    }
    try:
        r = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body),
            timeout=90,  # image gen can be slow
        )
        if r.status_code != 200:
            print(f"[franchise_ref] openai http {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        item = (data.get("data") or [{}])[0]
        b64 = item.get("b64_json")
        if b64:
            raw = base64.b64decode(b64)
        else:
            url = item.get("url")
            if not url:
                return None
            got = _download_and_validate(url)
            if not got:
                return None
            raw = got[0]
        ext = _sniff_image(raw)
        if not ext:
            return None
        return raw, ext
    except Exception as e:
        print(f"[franchise_ref] openai exception: {e}")
        return None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def resolve_franchise_ref(
    prompt: str,
    *,
    franchise_refs_dir: Path,
    public_origin: str,
) -> Optional[dict]:
    """Given a user prompt and no user upload, return
        {"url": "<https url>", "slug": "<slug>", "title": "<title>",
         "source": "cache" | "search" | "generated"}
    or None if we couldn't figure out a show or produce a reference.

    Never raises. Every subsystem failure logs and returns None.
    """
    if not prompt or not public_origin:
        return None

    title = extract_show_title(prompt)
    if not title:
        return None

    slug = slugify(title)
    if not slug:
        return None

    franchise_refs_dir.mkdir(parents=True, exist_ok=True)

    # Check every extension we might have cached under.
    for ext in ("png", "jpg", "webp"):
        cached = franchise_refs_dir / f"{slug}.{ext}"
        if cached.exists() and cached.stat().st_size >= 5_000:
            return {
                "url": f"{public_origin}/static/franchise-refs/{cached.name}",
                "slug": slug,
                "title": title,
                "source": "cache",
            }

    # Cache miss. Try search first (real still), then AI generation.
    started = time.time()
    got = _search_cast_still_serpapi(title)
    source = "search"
    if not got:
        got = _generate_cast_still_openai(title)
        source = "generated"
    if not got:
        print(f"[franchise_ref] no reference produced for title='{title}' "
              f"slug='{slug}' after {time.time()-started:.1f}s")
        return None

    data, ext = got
    out = franchise_refs_dir / f"{slug}.{ext}"
    try:
        out.write_bytes(data)
    except Exception as e:
        print(f"[franchise_ref] cache write failed: {e}")
        return None

    print(f"[franchise_ref] cached title='{title}' slug='{slug}' "
          f"source={source} ext={ext} bytes={len(data)} "
          f"took={time.time()-started:.1f}s")
    return {
        "url": f"{public_origin}/static/franchise-refs/{out.name}",
        "slug": slug,
        "title": title,
        "source": source,
    }
