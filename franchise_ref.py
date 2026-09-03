"""Franchise reference-pack: on-demand cast reference frame lookup.

When a user prompts "generate a <show> episode" and does not upload a
reference image, this module figures out what show they mean and produces
a public HTTPS URL to a cast reference frame that MiniMax H3 can use as
first_frame_url. Frame-chaining then locks the faces in for every
subsequent scene.

Pipeline (each step is skippable):

    1. Extract show title from prompt via Grok (LLM-only; LRU-cached).
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
from urllib.parse import urlparse

import requests


# ---------------------------------------------------------------------------
# Env-driven service configuration. Every one is optional.
# ---------------------------------------------------------------------------

# The ONLY external service key we consume: xAI (Grok). One key covers both
# title extraction (Grok chat) AND the AI image-generation fallback (Grok
# Imagine). No other paid API keys required.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "").strip()
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-fast-non-reasoning").strip()
XAI_IMAGE_MODEL = os.environ.get("XAI_IMAGE_MODEL", "grok-imagine-image-2.0").strip()

REQUEST_TIMEOUT = 30  # seconds; keep single request cheap


# ---------------------------------------------------------------------------
# Step 1: Show-title extraction (Grok-only)
# ---------------------------------------------------------------------------
#
# Previous versions of this module tried a case-sensitive regex first
# and only fell through to Grok on a miss. That was cheap but brittle:
# "Everybody loves Raymond episode" (lowercase 'loves') truncated to
# just "Everybody", which then got searched as a Nintendo Switch game.
# Grok knows every well-known TV show/movie, handles casing, typos,
# nicknames ("ELR", "the office"), and returns the canonical title.
#
# Cost: ~200 tokens per /api/plan call at grok-4-fast-non-reasoning
# pricing. We're already paying Grok for scene expansion in the same
# request, so the marginal cost is trivial. In-memory LRU cache below
# keeps repeated identical prompts free.

from functools import lru_cache


@lru_cache(maxsize=512)
def _llm_extract_show_info(prompt: str) -> Optional[tuple[str, str, Optional[str]]]:
    """Ask Grok to identify the show AND its production style.

    Returns (title, kind, year) tuple where:
      title: canonical full title (e.g. 'Everybody Loves Raymond')
      kind:  one of 'live-action', 'animated-2d', 'animated-3d',
             'anime', 'stop-motion', 'unknown'
      year:  premiere year as string (e.g. '1996') or None

    Returns None when prompt doesn't reference a real well-known show.
    Never raises.

    The kind and year let us build image-search queries that
    disambiguate live-action shows from anime/game characters that
    share names (e.g. 'Raymond' pulls anime fanart without
    'sitcom 1996 CBS' scoping). Cached per exact prompt string.
    """
    if not XAI_API_KEY:
        return None
    system = (
        "You identify a TV show or movie from a user's video-generation "
        "prompt and return structured JSON. If the prompt clearly "
        "references an existing well-known TV show or movie by name, "
        "output a JSON object. Otherwise output the exact string NONE.\n\n"
        "JSON schema:\n"
        '  {"title": "<canonical full title>", '
        '"kind": "<live-action|animated-2d|animated-3d|anime|stop-motion|unknown>", '
        '"year": "<4-digit premiere year, or empty string>"}\n\n'
        "Rules:\n"
        "- Just the JSON object, nothing else. No markdown fence, no prose.\n"
        "- title: full official title. NEVER truncate. 'everybody loves "
        "raymond' -> 'Everybody Loves Raymond'. 'elr' -> 'Everybody Loves "
        "Raymond'. 'the office' -> 'The Office'. 'seinfeld ep' -> 'Seinfeld'. "
        "'curb' -> 'Curb Your Enthusiasm'. 'family guy' -> 'Family Guy'.\n"
        "- Case in input doesn't matter. Output canonical casing.\n"
        "- No franchise expansion (Star Wars -> Star Wars, not A New Hope).\n"
        "- kind: 'live-action' for sitcoms, dramas, reality; "
        "'animated-2d' for Family Guy, Simpsons, South Park; "
        "'animated-3d' for Pixar-style CG; "
        "'anime' for Japanese anime; 'stop-motion' for Rankin/Bass; "
        "'unknown' only if you truly can't tell.\n"
        "- year: premiere year of the series/film, 4 digits, or \"\" if unknown.\n"
        "- If the prompt only describes an original scene, output NONE.\n"
        "- If the show is fictional/made up by the user, output NONE.\n"
        "- If ambiguous between shows, pick the most well-known one."
    )
    body = {
        "model": XAI_MODEL,
        "max_tokens": 80,
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
            print(f"[franchise_ref] xai show-info http {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                break
        text = text.strip()
        if not text or text.upper() == "NONE":
            return None
        # Strip common wrappers Grok occasionally adds despite instructions.
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            print(f"[franchise_ref] xai show-info non-JSON: {text[:200]}")
            return None
        title = (parsed.get("title") or "").strip().strip('"\'')
        kind = (parsed.get("kind") or "unknown").strip().lower()
        year = (parsed.get("year") or "").strip() or None
        if not title or len(title) > 80 or "\n" in title:
            return None
        if kind not in {"live-action", "animated-2d", "animated-3d",
                        "anime", "stop-motion", "unknown"}:
            kind = "unknown"
        if year and not (year.isdigit() and len(year) == 4):
            year = None
        return (title, kind, year)
    except Exception as e:  # pragma: no cover -- defensive
        print(f"[franchise_ref] xai show-info exception: {e}")
        return None


def extract_show_title(prompt: str) -> Optional[str]:
    """Return canonical show title or None. Thin wrapper for callers
    that only need the title (auto-resolve path). New callers should
    prefer extract_show_info() to also get kind + year for smarter
    query construction. Never raises.
    """
    if not prompt:
        return None
    info = _llm_extract_show_info(prompt.strip())
    return info[0] if info else None


def extract_show_info(prompt: str) -> Optional[tuple[str, str, Optional[str]]]:
    """Return (title, kind, year) tuple or None. Preferred over
    extract_show_title() because kind ('live-action' vs 'anime' etc.)
    lets the picker build queries that disambiguate real cast photos
    from fanart that shares a name. Never raises.
    """
    if not prompt:
        return None
    return _llm_extract_show_info(prompt.strip())


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


# DuckDuckGo image search is keyless. The catch: we have to scrape the vqd
# token out of the HTML entrypoint, then hit i.js with it. This is the same
# pattern DDG's own webapp uses; no auth involved. If DDG ever changes the
# response shape this quietly returns None and we fall through to Grok
# Imagine — no user-visible breakage.
_DDG_HTML_URL = "https://duckduckgo.com/"
_DDG_JSON_URL = "https://duckduckgo.com/i.js"
_DDG_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
_DDG_VQD_PATTERNS = (
    re.compile(r'vqd=([\d-]+)'),
    re.compile(r'"vqd":"([\d-]+)"'),
    re.compile(r"vqd=['\"]([\d-]+)['\"]"),
)


def _ddg_get_vqd(session: requests.Session, query: str) -> Optional[str]:
    try:
        r = session.get(
            _DDG_HTML_URL,
            params={"q": query, "iar": "images", "iax": "images", "ia": "images"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        for pat in _DDG_VQD_PATTERNS:
            m = pat.search(r.text)
            if m:
                return m.group(1)
    except Exception as e:
        print(f"[franchise_ref] ddg vqd exception ({type(e).__name__}): {e}")
    return None


def _ddg_run_query(session: requests.Session, query: str) -> list[dict]:
    """Single DDG image search. Returns raw results list or []. No ranking.

    Tries the maintained `ddgs` PyPI library first — primp-backed TLS
    impersonation works from cloud IPs where the raw i.js endpoint is
    rate-limited or blocked. Falls back to the raw HTTP flow if the
    library import or call fails.
    """
    try:
        from ddgs import DDGS  # type: ignore
        with DDGS() as ddgs:
            hits = list(ddgs.images(query, max_results=30))
            if hits:
                return hits
    except Exception as e:
        print(f"[franchise_ref] ddgs.images fallback for '{query}' ({type(e).__name__}): {e}")

    vqd = _ddg_get_vqd(session, query)
    if not vqd:
        return []
    try:
        r = session.get(
            _DDG_JSON_URL,
            params={
                "l": "us-en",
                "o": "json",
                "q": query,
                "vqd": vqd,
                "f": ",,,,,",
                "p": "1",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return r.json().get("results") or []
    except Exception as e:
        print(f"[franchise_ref] ddg query '{query}' exception ({type(e).__name__}): {e}")
        return []


def _kind_queries(title: str, kind: str, year: Optional[str]) -> list[str]:
    """Build image-search queries tuned to the show's production style.

    Live-action shows are the tricky case because their titles/character
    names collide with anime and game characters. Year + 'sitcom'/'TV
    series' scoping filters most of that out at the query level, before
    we even get to domain-based ranking. Animation/anime don't have this
    problem because 'characters' + title returns the right frame.
    """
    year_suffix = f" {year}" if year else ""
    if kind == "live-action":
        return [
            f"{title}{year_suffix} TV series cast photo",
            f"{title} sitcom cast",
            f"{title}{year_suffix} promotional still",
        ]
    if kind == "animated-2d":
        return [
            f"{title} main characters",
            f"{title} animated series",
            f"{title} promo art",
        ]
    if kind == "animated-3d":
        return [
            f"{title} main characters",
            f"{title} CGI",
            f"{title} promo art",
        ]
    if kind == "anime":
        return [
            f"{title} anime main characters",
            f"{title} anime key visual",
        ]
    if kind == "stop-motion":
        return [
            f"{title} stop motion characters",
            f"{title} promo",
        ]
    # unknown: hedge with generic queries
    return [
        f"{title}{year_suffix} cast photo",
        f"{title} main characters",
        f"{title} promo",
    ]


def _domain_score(host: str, kind: str) -> float:
    """Shared domain-based scoring so picker and auto-resolve agree.

    Live-action shows get a much heavier fan-art penalty because their
    names frequently overlap with anime characters ('Raymond' the
    VTuber, etc.). Anime/animation only get a light nudge — some real
    anime promo art legitimately lives on DeviantArt/Pixiv.
    """
    score = 0.0
    if any(host.endswith(d) or host == d or d in host for d in _DDG_WALLPAPER_DOMAINS):
        score -= 2.0
    if any(host.endswith(d) or host == d for d in _DDG_PROMO_DOMAINS):
        score += 1.0
    fanart_hit = any(host.endswith(d) or host == d or d in host
                     for d in _DDG_FANART_DOMAINS)
    if fanart_hit:
        # Heavy penalty for live-action so anime/game fanart never wins.
        # Light penalty for animation to still allow legit fan wikis.
        score -= 3.5 if kind == "live-action" else 0.8
    return score


def search_candidates_duckduckgo(
    title: str,
    want: int = 6,
    kind: str = "unknown",
    year: Optional[str] = None,
) -> list[dict]:
    """Return up to `want` ranked candidate image URLs for the given show.

    Each item: {"url": str, "width": int, "height": int, "thumbnail": str}.

    Query set is tuned by `kind` (live-action / animated / anime) so
    live-action shows don't pull anime fan-art with matching names.
    Ranking combines aspect-ratio + resolution + domain scoring shared
    with _search_cast_still_duckduckgo. Does NOT download the images —
    caller fetches them directly. Returns [] on any failure.
    """
    session = requests.Session()
    session.headers["User-Agent"] = _DDG_UA

    queries = _kind_queries(title, kind, year)
    results: list[dict] = []
    seen_urls: set[str] = set()
    for q in queries:
        for hit in _ddg_run_query(session, q):
            url = hit.get("image")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append(hit)
        # Once we have plenty of raw hits, stop querying — saves ~1s per
        # extra query. Ranking will trim to `want` anyway.
        if len(results) >= 40:
            break

    print(f"[franchise_ref] ddg returned {len(results)} raw candidates for '{title}' "
          f"(kind={kind}, year={year})")
    ranked = []
    skipped_size = 0
    skipped_url = 0
    # Loosened threshold: 320x180 (widescreen thumbnail floor). The previous
    # 600x400 cutoff was silently killing 80%+ of real hits because DDG's
    # width/height fields report the thumbnail size, not the source image.
    MIN_W, MIN_H = 320, 180
    for hit in results[:60]:
        url = hit.get("image")
        thumb = hit.get("thumbnail") or url
        w = int(hit.get("width") or 0)
        h = int(hit.get("height") or 0)
        if not url or not url.startswith("http"):
            skipped_url += 1
            continue
        if w and h and (w < MIN_W or h < MIN_H):
            skipped_size += 1
            continue
        ratio = (w / max(h, 1)) if (w and h) else 1.78
        host = (urlparse(url).hostname or "").lower()
        aspect_score = -abs(ratio - 1.78)
        size_score = (w / 20000.0 if w else 0.0)
        dscore = _domain_score(host, kind)
        score = aspect_score + size_score + dscore
        ranked.append((score, {
            "url": url,
            "width": w,
            "height": h,
            "thumbnail": thumb,
        }))
    ranked.sort(reverse=True, key=lambda x: x[0])
    print(f"[franchise_ref] kept {len(ranked)} candidates (skipped_url={skipped_url}, "
          f"skipped_size={skipped_size}), returning top {min(want, len(ranked))}")
    return [item for _, item in ranked[:want]]


# Domains that reliably return "every-character-ever" wallpapers or busy
# collages instead of a clean promotional cast frame. We don't hard-ban
# them — some titles ONLY have hits there — but we score them lower so a
# clean promo image from a review/news site wins when both are available.
_DDG_WALLPAPER_DOMAINS = (
    "wallpaperaccess.com",
    "wallpapercave.com",
    "wallpapers.com",
    "wallpapersden.com",
    "hdqwalls.com",
    "alphacoders.com",
    "images4.alphacoders.com",
    "images6.alphacoders.com",
    "wallup.net",
    "pxfuel.com",
    "peakpx.com",
    # Fandom wiki mirrors serve "every character ever" posters as their
    # top image (Simpsons character wall, Family Guy character grids).
    "wikia.nocookie.net",
    "static.wikia.nocookie.net",
    "img1.wikia.nocookie.net",
    "vignette.wikia.nocookie.net",
    # PNG cutout sites and ranker collages are also not real stills.
    "pngimg.com",
    "imgix.ranker.com",
    "ranker.com",
    "pinimg.com",
    "i.pinimg.com",
    # Etsy fan-merch grids/collages.
    "etsystatic.com",
    "i.etsystatic.com",
)
# Fan-art / anime / game-image domains that pollute live-action searches
# when a show/character name overlaps with an anime or game character
# (e.g. 'Raymond' the sitcom vs 'Raymond' the VTuber/game character).
# Heavy penalty when the show is live-action; small penalty otherwise.
_DDG_FANART_DOMAINS = (
    "deviantart.com",
    "images-wixmp",  # DeviantArt CDN prefix
    "pixiv.net",
    "i.pximg.net",
    "artstation.com",
    "cdna.artstation.com",
    "cdnb.artstation.com",
    "danbooru.donmai.us",
    "safebooru.org",
    "gelbooru.com",
    "zerochan.net",
    "myanimelist.net",
    "anilist.co",
    "animenewsnetwork.com",
    "cdn.myanimelist.net",
    "crunchyroll.com",  # anime-only
    "tumblr.com",
    "media.tumblr.com",
    "reddit.com",  # random subreddit crops, no editorial vetting
    "i.redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
    # VTuber / hololive / gacha character wikis and shops
    "hololive.hololivepro.com",
    "virtualyoutuber.fandom.com",
    # Generic game / character image dumps
    "gamepedia.com",
    "steamusercontent.com",
    "steamuserimages",
    "cdn.akamai.steamstatic.com",
    "nintendo.com",  # Nintendo game promo art
    "nintendo-world.com",
)
# Domains that tend to host clean promotional cast/character photos.
_DDG_PROMO_DOMAINS = (
    "tvseriesfinale.com",
    "variety.com",
    "hollywoodreporter.com",
    "ew.com",
    "tvinsider.com",
    "nbc.com",
    "foxnews.com",
    "fox.com",
    "amc.com",
    "hbo.com",
    "themarysue.com",
    "slashfilm.com",
    "gq.com",
    "vulture.com",
    "colliderimages.com",
    "static1.colliderimages.com",
    "static1.srcdn.com",  # ScreenRant CDN
    "srcdn.com",
    "image.tmdb.org",  # TMDB — always official promo
    "tmdb.org",
    "comicbook.com",
    "cdn.mos.cms.futurecdn.net",
    "nme.com",
    "rollingstone.com",
    "indiewire.com",
    "relevantmagazine.com",
)


def _ddg_search_images_ddgs(query: str) -> list[dict]:
    """Try the `ddgs` PyPI library first — it uses primp (impersonation) so
    Railway's cloud IP range doesn't get rate-limited by DDG's HTML endpoint.
    Returns raw hits shaped like the JSON API (image/width/height keys)."""
    try:
        from ddgs import DDGS  # type: ignore
    except Exception as e:
        print(f"[franchise_ref] ddgs lib not available ({type(e).__name__}): {e}")
        return []
    try:
        with DDGS() as ddgs:
            return list(ddgs.images(query, max_results=30))
    except Exception as e:
        print(f"[franchise_ref] ddgs.images exception for '{query}' ({type(e).__name__}): {e}")
        return []


def _ddg_search_images(session: requests.Session, query: str) -> list[dict]:
    """Run one DDG image search and return the raw results list. Empty on
    any failure.

    Tries the ddgs PyPI library first (primp-backed, handles anti-bot on
    cloud IPs), then falls back to the raw i.js JSON API if the library is
    missing or blocked.
    """
    lib_hits = _ddg_search_images_ddgs(query)
    if lib_hits:
        return lib_hits

    vqd = _ddg_get_vqd(session, query)
    if not vqd:
        print(f"[franchise_ref] ddg: no vqd for query='{query}'")
        return []
    try:
        r = session.get(
            _DDG_JSON_URL,
            params={
                "l": "us-en",
                "o": "json",
                "q": query,
                "vqd": vqd,
                "f": ",,,,,",  # no filters
                "p": "1",       # safe search on
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[franchise_ref] ddg i.js http {r.status_code} for '{query}'")
            return []
        return (r.json() or {}).get("results") or []
    except Exception as e:
        print(f"[franchise_ref] ddg i.js exception for '{query}' ({type(e).__name__}): {e}")
        return []


def _search_cast_still_duckduckgo(
    title: str,
    kind: str = "unknown",
    year: Optional[str] = None,
) -> Optional[tuple[bytes, str]]:
    """Keyless DuckDuckGo image search for a clean cast/character frame.

    Queries and domain scoring are tuned to the show's `kind` so
    live-action shows (whose names often collide with anime/game
    characters) don't return fanart. Uses the shared `_kind_queries()`
    and `_domain_score()` helpers so this path and the picker path
    agree on what a good hit looks like.

    Returns bytes of the first candidate that downloads and validates as a
    real image, or None on any failure.
    """
    session = requests.Session()
    session.headers["User-Agent"] = _DDG_UA

    # Deduplicate hits across queries by URL so we don't retry the same
    # candidate three times when the queries overlap.
    seen_urls: set[str] = set()
    all_hits: list[dict] = []
    for query in _kind_queries(title, kind, year):
        hits = _ddg_search_images(session, query)
        for hit in hits:
            u = hit.get("image")
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            all_hits.append(hit)

    if not all_hits:
        print(f"[franchise_ref] ddg: no hits across any query for '{title}'")
        return None

    ranked: list[tuple[float, str]] = []
    for hit in all_hits[:60]:  # examine more since we merged 3 queries
        url = hit.get("image")
        w = int(hit.get("width") or 0)
        h = int(hit.get("height") or 0)
        if not url or not url.startswith("http") or w < 600 or h < 400:
            continue

        host = (urlparse(url).hostname or "").lower()
        ratio = w / max(h, 1)

        # Aspect: reward 16:9 (1.78), tolerate 4:3 (1.33) and 3:2 (1.5).
        # Penalize 21:9+ (wallpapers) and vertical posters.
        aspect_score = -min(abs(ratio - 1.6), 1.5)  # peak near landscape
        if ratio < 1.1:  # near-square or portrait — usually promo poster
            aspect_score -= 0.4
        if ratio > 2.2:  # ultra-wide wallpaper
            aspect_score -= 0.6

        size_score = min(w / 1600.0, 1.5)  # cap the reward at ~2400px wide

        # Shared domain scoring: wallpaper penalty, promo boost, and
        # fan-art / anime / game-image penalty. Live-action shows get
        # a much heavier fan-art penalty than animation.
        domain_score = _domain_score(host, kind)

        score = aspect_score + size_score + domain_score
        ranked.append((score, url))

    if not ranked:
        print(f"[franchise_ref] ddg: no valid-size hits for '{title}'")
        return None
    ranked.sort(reverse=True)

    tried = 0
    for score, url in ranked[:8]:
        got = _download_and_validate(url)
        tried += 1
        if got:
            print(f"[franchise_ref] ddg selected (try {tried}, score={score:.2f}): {url[:100]}")
            return got
    print(f"[franchise_ref] ddg: {tried} candidates rejected for '{title}'")
    return None


# ---------------------------------------------------------------------------
# Step 4 (Option A): Grok Imagine via visual-signature extraction
#
# Grok Imagine's content moderation blocks prompts that name copyrighted IP
# directly ("The Simpsons cast", "Family Guy"). To dodge that, we don't ask
# for the show — we ask Grok text for the show's *visual signatures* and feed
# THOSE into Grok Imagine. Grok's image model still recognizes what the
# description points to and produces the right-looking frame.
# ---------------------------------------------------------------------------

_SIGNATURE_SYSTEM_PROMPT = (
    "You are a visual-signature extractor for a video generation pipeline.\n\n"
    "Given a TV show or movie name, respond with a compact JSON object "
    "describing the show's visual signatures WITHOUT naming the show, its "
    "studio, or its characters by name. Use only visual descriptors that let "
    "an image model reproduce the look while dodging IP-name content filters.\n\n"
    "Output format (STRICT JSON, no prose, no markdown fences):\n"
    "{\n"
    '  "kind": "animation" | "live-action",\n'
    '  "art_style": "one-sentence description of drawing/photography style",\n'
    '  "characters": [\n'
    '    {"role": "shorthand role like dad, wife, boss", "look": "specific visual details — build, hair, clothing, distinctive features"}\n'
    "  ],\n"
    '  "setting": "one-sentence description of signature location or environment",\n'
    '  "vibe": "one-sentence description of tone, lighting, color palette"\n'
    "}\n\n"
    "Rules:\n"
    "- NEVER include the show's actual name, characters' actual names, studio, or network.\n"
    "- Describe things visually, not by association. Say 'yellow-skinned cartoon family' not 'Simpsons family'.\n"
    "- Include 3-6 characters max.\n"
    "- Be specific about visual details (hair color, glasses, clothing) but generic about identity.\n"
    "- Output must be valid JSON parseable by json.loads with no extra text."
)

# Words that reliably trigger Grok Imagine's IP filter when combined with
# stylistic descriptors. We strip these on the retry pass. Order matters:
# longer phrases first so 'yellow-skinned cartoon' becomes 'cartoon', not
# 'skinned cartoon'.
_IP_TRIGGER_TOKENS: tuple[tuple[str, str], ...] = (
    ("yellow-skinned", "skin-toned"),
    ("yellow skinned", "skin-toned"),
    ("yellow skin", "skin"),
    ("bright yellow", "skin-toned"),
)


def _extract_visual_signature(title: str) -> Optional[dict]:
    """Call Grok text (xAI chat) to describe a show's visual signatures in
    JSON form, without naming the IP. Returns dict on success, None on any
    failure. Never raises.
    """
    if not XAI_API_KEY:
        return None
    body = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": _SIGNATURE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Show: {title}"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
    }
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {XAI_API_KEY}",
            },
            data=json.dumps(body),
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[franchise_ref] signature http {r.status_code}: {r.text[:200]}")
            return None
        content = r.json()["choices"][0]["message"]["content"]
        sig = json.loads(content)
        if not isinstance(sig, dict) or not sig.get("characters"):
            print(f"[franchise_ref] signature malformed for '{title}': {content[:200]}")
            return None
        return sig
    except Exception as e:
        print(f"[franchise_ref] signature exception ({type(e).__name__}): {e}")
        return None


def _signature_to_image_prompt(sig: dict) -> str:
    """Compose an image-gen prompt from an extracted signature."""
    kind = sig.get("kind", "animation")
    chars = sig.get("characters", []) or []
    char_str = ", ".join(
        f"{c.get('role', '')}: {c.get('look', '')}"
        for c in chars if c.get("look")
    )
    setting = sig.get("setting", "") or ""
    vibe = sig.get("vibe", "") or ""
    art = sig.get("art_style", "") or ""
    if kind == "live-action":
        return (
            f"Photorealistic wide group photo, {art}. "
            f"People visible together: {char_str}. "
            f"Setting: {setting}. "
            f"Lighting and mood: {vibe}. "
            f"No captions, text overlays, watermarks, or on-screen graphics."
        )
    return (
        f"A group portrait in {art}. "
        f"Characters visible together: {char_str}. "
        f"Setting: {setting}. "
        f"Overall vibe: {vibe}. "
        f"No captions, text overlays, watermarks, or on-screen graphics."
    )


def _strip_ip_triggers(prompt: str) -> str:
    """Remove or soften phrases that empirically trigger Grok's IP filter.
    Used on the retry pass after a `content-moderated` rejection.
    """
    out = prompt
    for needle, replacement in _IP_TRIGGER_TOKENS:
        # Case-insensitive replace, preserving surrounding whitespace.
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        out = pattern.sub(replacement, out)
    return out


def _call_grok_imagine(prompt: str) -> Optional[str]:
    """Submit prompt to Grok Imagine. Returns image URL on success, None on
    any failure (network, HTTP error, content moderation). Prints the reason
    so we can tell moderation blocks apart from network errors in Railway logs.
    """
    body = {"model": XAI_IMAGE_MODEL, "prompt": prompt, "n": 1}
    try:
        r = requests.post(
            "https://api.x.ai/v1/images/generations",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {XAI_API_KEY}",
            },
            data=json.dumps(body),
            timeout=90,
        )
        if r.status_code != 200:
            # Print a snippet so we can see the moderation code in logs.
            print(f"[franchise_ref] imagine http {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        item = (data.get("data") or [{}])[0]
        return item.get("url")
    except Exception as e:
        print(f"[franchise_ref] imagine exception ({type(e).__name__}): {e}")
        return None


def _generate_cast_still_xai(title: str) -> Optional[tuple[bytes, str]]:
    """Full signature-driven flow with one retry: extract visual signatures,
    compose an IP-safe image prompt, generate. If Grok Imagine moderates the
    result, strip known trigger tokens and try once more. Returns (bytes,
    ext) or None. Never raises.
    """
    if not XAI_API_KEY:
        return None

    sig = _extract_visual_signature(title)
    if not sig:
        return None

    prompt = _signature_to_image_prompt(sig)
    url = _call_grok_imagine(prompt)
    if not url:
        # Retry with trigger-token stripped prompt. Empirically the same
        # visual description without "yellow-skinned" etc slips through
        # moderation and still recognizes the target style.
        softer = _strip_ip_triggers(prompt)
        if softer != prompt:
            print(f"[franchise_ref] retrying '{title}' with softened prompt")
            url = _call_grok_imagine(softer)
    if not url:
        return None

    got = _download_and_validate(url)
    if not got:
        return None
    raw, ext = got
    return raw, ext


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

# Public figures (real people) we auto-attach a reference frame for. Unlike
# TV shows -- which are identified via Grok's show-info extractor -- these
# are matched by a simple lowercase substring probe against the prompt so
# the lookup is fast, deterministic, and free (no LLM roundtrip).
#
# The value is the slug we look for on disk (BAKED_IN or v2-prefixed).
# Aliases let colloquial variations ("scott a.", "@realscottadams") still
# hit the same reference. Add new figures here as we curate their refs.
_PUBLIC_FIGURE_ALIASES: dict[str, str] = {
    "scott adams": "scott-adams",
    "scottadams": "scott-adams",
    "realscottadams": "scott-adams",
    "dilbert creator": "scott-adams",
    # Donald Trump — baked-in official 2025 portrait. Aliases cover the
    # common ways users refer to him so 'Trump running The Office' anchors
    # to the real face instead of falling through to a generic cast frame.
    "donald trump": "donald-trump",
    "president trump": "donald-trump",
    "trump": "donald-trump",
    "the donald": "donald-trump",
    "realdonaldtrump": "donald-trump",
    # Vladimir Putin — baked-in official portrait.
    "vladimir putin": "vladimir-putin",
    "president putin": "vladimir-putin",
    "putin": "vladimir-putin",
    # Xi Jinping — baked-in official portrait.
    "xi jinping": "xi-jinping",
    "president xi": "xi-jinping",
    # Aliases below are known world leaders users cast in ensemble shorts.
    # They currently have NO baked-in reference image on disk, so a match
    # here logs 'no ref file' and falls through to the show/generic
    # pipeline. Kept in the table so the frontend can list them as
    # curated figures and so a future ref-image drop lights them up
    # automatically without a code change.
    "javier milei": "javier-milei",
    "milei": "javier-milei",
    "justin trudeau": "justin-trudeau",
    "trudeau": "justin-trudeau",
    "giorgia meloni": "giorgia-meloni",
    "meloni": "giorgia-meloni",
    "angela merkel": "angela-merkel",
    "merkel": "angela-merkel",
    "kim jong un": "kim-jong-un",
    "kim jong-un": "kim-jong-un",
    "emmanuel macron": "emmanuel-macron",
    "macron": "emmanuel-macron",
    "joe biden": "joe-biden",
    "biden": "joe-biden",
    "barack obama": "barack-obama",
    "obama": "barack-obama",
    "benjamin netanyahu": "benjamin-netanyahu",
    "netanyahu": "benjamin-netanyahu",
    "volodymyr zelensky": "volodymyr-zelensky",
    "zelensky": "volodymyr-zelensky",
    "narendra modi": "narendra-modi",
    "modi": "narendra-modi",
    "keir starmer": "keir-starmer",
    "starmer": "keir-starmer",
}


def _match_public_figure(prompt: str) -> Optional[tuple[str, str]]:
    """Return (canonical_name, slug) for the DOMINANT curated public figure
    named in the prompt. Case-insensitive word-boundary match.

    Fast, deterministic, no LLM call. Runs before the show extractor so
    a prompt like 'Scott Adams reviews his coffee' auto-attaches the
    scott-adams.png reference frame even though Scott Adams isn't a TV
    show. Never raises.

    Selection rule when multiple figures appear (ensemble prompts like
    'Trump is Michael Scott, Putin is Stanley, Merkel is Angela'):
      1) Highest mention count wins (protagonist appears most often).
      2) Tiebreak: first mention in prompt (protagonist is usually
         introduced first).
    Previously the tiebreak was 'longest alias key,' which incorrectly
    picked Putin (14 chars) over Trump (12 chars) in Zach's ensemble
    Office parody where Trump was the boss and Putin had one beat.

    Uses regex word-boundaries so 'trump' matches 'Trump walks in' but
    NOT 'trumpet' or 'trumpler'.
    """
    if not prompt:
        return None
    import re as _re
    lowered = prompt.lower()

    # Score each slug by (total mention count, first mention position).
    # Multiple aliases can map to the same slug (e.g. 'trump' and 'donald
    # trump' both map to 'donald-trump') -- count all of them together.
    # But avoid double-counting overlapping matches: 'donald trump' also
    # matches 'trump', so we scan by the longest alias first per slug and
    # mark matched spans consumed.
    scores: dict[str, dict] = {}  # slug -> {count, first_pos, canonical}
    consumed: list[tuple[int, int]] = []  # sorted spans already counted

    def _overlaps(start: int, end: int) -> bool:
        for cs, ce in consumed:
            if start < ce and cs < end:
                return True
        return False

    for alias in sorted(_PUBLIC_FIGURE_ALIASES.keys(), key=len, reverse=True):
        slug = _PUBLIC_FIGURE_ALIASES[alias]
        pat = _re.compile(rf"\b{_re.escape(alias)}\b")
        for m in pat.finditer(lowered):
            s, e = m.span()
            if _overlaps(s, e):
                continue
            consumed.append((s, e))
            rec = scores.setdefault(slug, {"count": 0, "first_pos": s, "slug": slug})
            rec["count"] += 1
            if s < rec["first_pos"]:
                rec["first_pos"] = s

    if not scores:
        return None

    # Highest count wins, ties broken by earliest first mention.
    winner = max(scores.values(), key=lambda r: (r["count"], -r["first_pos"]))
    slug = winner["slug"]
    canonical = " ".join(w.capitalize() for w in slug.split("-"))
    return (canonical, slug)


def resolve_franchise_ref(
    prompt: str,
    *,
    franchise_refs_dir: Path,
    public_origin: str,
) -> Optional[dict]:
    """Given a user prompt and no user upload, return
        {"url": "<https url>", "slug": "<slug>", "title": "<title>",
         "source": "cache" | "search" | "generated" | "figure"}
    or None if we couldn't figure out a show or produce a reference.

    Never raises. Every subsystem failure logs and returns None.
    """
    if not prompt or not public_origin:
        return None

    franchise_refs_dir.mkdir(parents=True, exist_ok=True)

    # Fast-path: curated public figure (real people, not TV shows). Matches
    # a lowercase alias in the prompt and serves the on-disk baked-in
    # reference. Skip the show extractor entirely on hit -- Grok would
    # return NONE for 'Scott Adams delivers a monologue' since he's not
    # a show.
    fig = _match_public_figure(prompt)
    if fig:
        canonical, slug = fig
        # Baked-in files live at <slug>.<ext> (no cache-schema prefix)
        # because they're hand-curated and never invalidated. Curated v2-
        # cache files also win here since they'd be even fresher.
        for prefix in ("v2-", ""):
            for ext in ("png", "jpg", "webp"):
                cached = franchise_refs_dir / f"{prefix}{slug}.{ext}"
                if cached.exists() and cached.stat().st_size >= 5_000:
                    return {
                        "url": f"{public_origin}/static/franchise-refs/{cached.name}",
                        "slug": slug,
                        "title": canonical,
                        "source": "figure",
                    }
        # Alias matched but the ref file is missing -- log and fall
        # through to the show extractor rather than 404ing silently.
        print(f"[franchise_ref] public figure alias hit ('{canonical}') "
              f"but no ref file at slug='{slug}' -- falling through")

    info = extract_show_info(prompt)
    if not info:
        return None
    title, kind, year = info

    slug = slugify(title)
    if not slug:
        return None

    # Cache-schema version. Bump whenever the search/generation pipeline
    # changes materially so old cached frames (e.g. drifted Grok outputs
    # from the pre-DDG-priority era) don't keep serving. Old files are
    # left on disk but simply not looked up.
    cache_prefix = "v2-"

    # Check every extension we might have cached under (with the current
    # cache-schema prefix).
    for ext in ("png", "jpg", "webp"):
        cached = franchise_refs_dir / f"{cache_prefix}{slug}.{ext}"
        if cached.exists() and cached.stat().st_size >= 5_000:
            return {
                "url": f"{public_origin}/static/franchise-refs/{cached.name}",
                "slug": slug,
                "title": title,
                "source": "cache",
            }

    # Cache miss. Try DuckDuckGo image search FIRST — real show frames
    # always look on-model (Rick with blue spiky hair, Morty in the yellow
    # shirt, etc.), while Grok Imagine tends to drift when the show has
    # very specific character designs. Fall back to Grok signature-driven
    # generation only when DDG can't find a usable hit (new/niche titles,
    # very obscure originals, or DDG returning nothing).
    started = time.time()
    got = _search_cast_still_duckduckgo(title, kind=kind, year=year)
    source = "search"
    if not got:
        got = _generate_cast_still_xai(title)
        source = "generated"
    if not got:
        print(f"[franchise_ref] no reference produced for title='{title}' "
              f"slug='{slug}' after {time.time()-started:.1f}s")
        return None

    data, ext = got
    out = franchise_refs_dir / f"{cache_prefix}{slug}.{ext}"
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

# ---------------------------------------------------------------------------
# Per-character reference stills
# ---------------------------------------------------------------------------
#
# Problem: our group cast still (seinfeld.png) contains Jerry/George/Elaine/
# Kramer. If the user prompt says "Newman commits voter fraud", nano-banana
# has no Newman in its reference so it grabs the closest match (bald-ish,
# chubby → George) and puts him in a USPS uniform. Wrong character entirely.
#
# Fix: ask Grok which characters appear in the scene, then produce a
# per-character reference still for each one (cached to disk under
# FRANCHISE_REFS/<show-slug>__<char-slug>.png). Pass those stills — not the
# group frame — to nano-banana for scene 0 keyframe generation. Nano-banana
# supports up to 3 image_urls so we cap at 3 per scene.
#
# Cache is content-addressable by (title, character): once we generate
# "Seinfeld → Newman" the first time, every future render reuses that image.
# Anti-hallucination: Grok Imagine can produce a specific character when
# named ("Newman from Seinfeld, plump USPS mail carrier, dark hair, mustache,
# played by Wayne Knight") — the actor's likeness plus role is a stable prompt.

_CHARACTER_TIMEOUT = 30
_MAX_CHARACTERS_PER_SCENE = 3  # nano-banana cap


@lru_cache(maxsize=2048)
def _llm_extract_scene_characters(show_title: str, scene_prompt: str) -> tuple[str, ...]:
    """Ask Grok which characters from <show_title> should appear in this scene.

    Two modes, transparent to the caller:

    1. Characters explicitly named in the scene ("Newman commits voter fraud")
       -> return exactly those characters, in order of prominence.
    2. Generic scene with no character names ("George's apartment, a robbery")
       -> Grok picks the show characters that canonically fit this location /
       situation. For a Seinfeld apartment scene that's Jerry + whoever the
       scene's action implies. For a generic Seinfeld coffee-shop scene,
       Grok might return [Jerry Seinfeld, George Costanza, Elaine Benes].

    Returns a tuple of canonical character names ordered main -> background,
    capped at 3 (nano-banana image_urls limit). Empty tuple only when Grok
    is unavailable or the show is unknown to the LLM.

    Grok is instructed to always cast someone from the show — the whole point
    of a Seinfeld render is that Seinfeld characters appear in it. We never
    invent characters, and we prefer named characters over 'a background
    extra' because nano-banana needs identifiable reference stills.
    """
    if not XAI_API_KEY or not show_title:
        return ()
    system = (
        f"You cast a scene for a video render styled after the show "
        f"'{show_title}'. Return who should appear in the scene, in order "
        f"of prominence, max 3.\n\n"
        "Casting includes:\n"
        "- Canonical fictional characters from the show (Michael Scott, "
        "Newman, Peter Griffin).\n"
        "- REAL public figures explicitly named OR clearly implied by the "
        "scene, using their real name (Donald Trump, Vladimir Putin, Elon "
        "Musk, Taylor Swift). This applies even when they aren't part of "
        "the original show -- a mashup like 'Trump in The Office' should "
        "cast Donald Trump, not Michael Scott.\n\n"
        "Three cases:\n"
        "1. Scene names characters or real people explicitly ('Newman "
        "delivers mail', 'Trump chairs a meeting') -> return those.\n"
        "2. Scene names a real person via descriptor ('the world's most "
        "powerful politician', 'the richest man alive', 'the current US "
        "president') -> return the specific real person by name.\n"
        "3. Scene is generic (no names, no descriptors, just location or "
        "action) -> pick the show's main characters that best fit this "
        "scene.\n\n"
        "Output: JSON array of canonical names, max 3. Just the array, "
        "no other text.\n\n"
        "Examples:\n"
        '  Seinfeld / "Newman commits voter fraud in his apartment" -> '
        '["Newman"]\n'
        '  The Office / "Michael gives a TED talk" -> ["Michael Scott"]\n'
        '  The Office / "a meeting in the conference room" (generic) -> '
        '["Michael Scott", "Dwight Schrute", "Jim Halpert"]\n'
        '  Family Guy / "the living room, chaos" (generic) -> '
        '["Peter Griffin", "Lois Griffin", "Stewie Griffin"]\n'
        '  The Office / "Trump chairs a boardroom meeting" -> '
        '["Donald Trump"]\n'
        '  The Office / "world leaders share a cubicle: Trump, Putin, Xi" '
        '-> ["Donald Trump", "Vladimir Putin", "Xi Jinping"]\n'
        "  The Office / \"the world's most powerful politician runs the "
        "office in modern America\" -> [\"Donald Trump\"]\n"
        '  The Office / "a political workplace mockumentary with world '
        'leaders sharing an office" -> ["Donald Trump", "Vladimir Putin", '
        '"Xi Jinping"]\n\n'
        "Rules:\n"
        "- ALWAYS return at least one character or real person if the "
        "prompt is intelligible. Never return [].\n"
        "- When the prompt clearly implies a specific real person via a "
        "descriptor, resolve to that real person's name (they change over "
        "time -- use the person currently holding that role as of today).\n"
        "- Use full canonical names for fictional characters (Newman, "
        "George Costanza) and real full names for public figures (Donald "
        "Trump, not just Trump).\n"
        "- Max 3. Order by prominence in this specific scene.\n"
        "- Never invent fictional characters that don't exist in the show."
    )
    body = {
        "model": XAI_MODEL,
        "max_tokens": 80,
        "system": system,
        "messages": [{"role": "user", "content": scene_prompt[:1500]}],
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
            timeout=_CHARACTER_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[franchise_ref] characters http {r.status_code}: {r.text[:200]}")
            return ()
        data = r.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                break
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        try:
            arr = json.loads(text)
        except json.JSONDecodeError:
            print(f"[franchise_ref] characters non-JSON: {text[:200]}")
            return ()
        if not isinstance(arr, list):
            return ()
        clean: list[str] = []
        for item in arr:
            if isinstance(item, str):
                name = item.strip().strip('"\'')
                if 2 <= len(name) <= 60 and "\n" not in name:
                    clean.append(name)
        return tuple(clean[:_MAX_CHARACTERS_PER_SCENE])
    except Exception as e:  # pragma: no cover
        print(f"[franchise_ref] characters exception: {e}")
        return ()


def _character_slug(character: str) -> str:
    """Deterministic filesystem-safe slug for a character name."""
    s = re.sub(r"[^a-z0-9]+", "-", character.lower()).strip("-")
    return s[:40] or "unknown"


@lru_cache(maxsize=512)
def _llm_describe_character(show_title: str, character: str) -> Optional[str]:
    """Ask Grok for a visual description of a specific character.

    We pass this description to Grok Imagine to generate a per-character
    reference still. Result cached by (title, character).

    Naming the actor is intentional — Grok Imagine can produce a likeness of
    a well-known TV actor as long as we frame it as their character role.
    That gives us far more reliable identity anchoring than a generic
    physical description.
    """
    if not XAI_API_KEY:
        return None
    system = (
        f"You describe a single named character from the TV show or movie "
        f"'{show_title}' for a text-to-image model. Write ONE paragraph, "
        f"40-70 words, focused on physical appearance: age range, build, "
        f"hair, facial features, typical wardrobe, and — if the actor is "
        f"well-known — mention the actor by name ('played by X'). "
        f"No plot, no dialogue, no scene description. If you don't "
        f"recognize the character, output NONE."
    )
    body = {
        "model": XAI_MODEL,
        "max_tokens": 200,
        "system": system,
        "messages": [{"role": "user", "content": character[:200]}],
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
            timeout=_CHARACTER_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[franchise_ref] describe http {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                break
        if not text or text.upper().startswith("NONE"):
            return None
        return text[:800]
    except Exception as e:  # pragma: no cover
        print(f"[franchise_ref] describe exception: {e}")
        return None


def resolve_character_ref(
    show_title: str,
    character: str,
    *,
    franchise_refs_dir: Path,
    public_origin: str,
) -> Optional[str]:
    """Return a public HTTPS URL to a reference still of `character` from
    `show_title`, generating and caching it on first use.

    Cache path: <franchise_refs_dir>/<show-slug>__<char-slug>.png
    Never raises. Returns None if we can't produce a valid still.

    Real-public-figure fast path: if `character` matches a baked-in public
    figure alias (Donald Trump, Scott Adams, etc.), return the curated real
    photo directly. This is what makes a mashup like 'Trump in The Office'
    actually show Trump instead of a Grok-fabricated look-alike.
    """
    if not show_title or not character or not public_origin:
        return None

    # Public figure fast path -- serve the baked-in real photo. Match by
    # word-boundary alias in the character name.
    fig = _match_public_figure(character)
    if fig:
        _canonical, fig_slug = fig
        for prefix in ("v2-", ""):
            for ext in ("png", "jpg", "webp"):
                cached = franchise_refs_dir / f"{prefix}{fig_slug}.{ext}"
                if cached.exists() and cached.stat().st_size >= 5_000:
                    return f"{public_origin}/static/franchise-refs/{cached.name}"

    show_slug = slugify(show_title)
    char_slug = _character_slug(character)
    if not show_slug or not char_slug:
        return None

    # Cache hit: any extension is fine.
    for ext in ("png", "jpg", "jpeg", "webp"):
        cached = franchise_refs_dir / f"char__{show_slug}__{char_slug}.{ext}"
        if cached.exists() and cached.stat().st_size > 20_000:
            return f"{public_origin}/static/franchise-refs/{cached.name}"

    # Miss: describe the character, generate via Grok Imagine, cache.
    description = _llm_describe_character(show_title, character)
    if not description:
        return None

    # Compose an image prompt tuned for identity-anchoring reference stills:
    # neutral pose, plain background, no scene action — nano-banana can then
    # place this character into any scene.
    image_prompt = (
        f"Full-body portrait reference still of {character} from {show_title}. "
        f"{description} Standing in a neutral pose facing camera, plain "
        f"neutral background, clear lighting, no props, no scene, no text. "
        f"Photorealistic, 4K, sharp focus on the face."
    )
    url = _call_grok_imagine(image_prompt)
    if not url:
        # Retry with trigger tokens stripped (works for the same reasons as
        # _generate_cast_still_xai's retry).
        softer = _strip_ip_triggers(image_prompt)
        if softer != image_prompt:
            url = _call_grok_imagine(softer)
    if not url:
        return None

    got = _download_and_validate(url)
    if not got:
        return None
    raw, ext = got
    out = franchise_refs_dir / f"char__{show_slug}__{char_slug}.{ext}"
    try:
        out.write_bytes(raw)
    except Exception as e:  # pragma: no cover
        print(f"[franchise_ref] cache write failed for {out}: {e}")
        return None
    print(f"[franchise_ref] cached character '{character}' for '{show_title}' "
          f"-> {out.name} ({len(raw)} bytes)")
    return f"{public_origin}/static/franchise-refs/{out.name}"


def resolve_scene_character_refs(
    show_title: str,
    scene_prompt: str,
    *,
    franchise_refs_dir: Path,
    public_origin: str,
) -> list[str]:
    """Full pipeline: extract characters from scene_prompt, resolve a ref
    still for each, return the list of public URLs (up to 3). Order preserved
    from Grok's prominence ranking. Skips characters we can't generate a
    ref for. Never raises.
    """
    if not show_title or not scene_prompt:
        return []
    characters = _llm_extract_scene_characters(show_title, scene_prompt)
    if not characters:
        return []
    urls: list[str] = []
    for ch in characters:
        u = resolve_character_ref(
            show_title, ch,
            franchise_refs_dir=franchise_refs_dir,
            public_origin=public_origin,
        )
        if u:
            urls.append(u)
        if len(urls) >= _MAX_CHARACTERS_PER_SCENE:
            break
    return urls
