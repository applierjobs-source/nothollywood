"""Character-aware prompt expansion for the multi-scene renderer.

The problem this solves:
    User types "generate a South Park episode where cartman commits voter fraud".
    We auto-split into 7 scenes. Each scene prompt contains the name "cartman"
    but no visual description. MiniMax H3 recognizes "South Park" as an art
    style (cutout animation) but does NOT have a stored visual concept of the
    named character "Cartman" — so it invents a random South Park-styled adult.

The fix (this module):
    Before rendering, run the user prompt through a single LLM pass that:
      1. Identifies known characters/settings (Cartman, Peter Griffin, Homer,
         Michael Scott, Rick Sanchez, etc.)
      2. Writes a shared "character bible" describing each in canonical detail
      3. Produces N per-scene prompts (one per scene duration) that each embed
         the character bible + style guide so every parallel scene renders
         a consistent-looking character.

Fallback: if ANTHROPIC_API_KEY is missing or the call fails, we fall back to
the old behavior (raw prompt, no expansion) so renders still work.

Cost: Claude Haiku 4.5, ~1-2K tokens per plan = ~$0.001-0.003 per render.
Latency: ~2-4s single call at plan time, then all N scenes render in parallel.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Timeout for the whole expansion call. The video render takes minutes, so
# spending up to 15s here is fine, but we don't want to hang the worker if
# Anthropic is having a bad day.
EXPANSION_TIMEOUT = 15.0

SYSTEM_PROMPT = """You are a prompt engineer for a text-to-video model (MiniMax H3).

The model can render any visual style but has NO memory of named fictional \
characters. If a user says "Cartman" or "Peter Griffin" the model does not \
know what they look like — you must describe them.

Your job: given a user's short episode idea, produce a JSON object with:
  1. `style` — one sentence describing the visual/animation style (e.g. \
"Flat 2D construction-paper cutout animation in the style of South Park, \
with rough edges, minimal shading, and canonical bright saturated colors")
  2. `characters` — a paragraph describing each named character in canonical \
detail (age, body type, hair, wardrobe, distinguishing features). Use only \
character names that appear or are strongly implied in the user prompt.
  3. `scenes` — an array of N per-scene prompts. Each scene prompt must be \
self-contained: it must repeat the style + relevant character descriptions \
so the model renders each scene consistently. Each scene should describe a \
distinct beat of the story with concrete visual action, camera framing, \
and dialogue where appropriate.

Rules:
- Every scene prompt MUST embed the style sentence and character descriptions \
inline. The model sees each scene in isolation — it has no shared context.
- Keep each scene prompt under 500 characters.
- Write like a director giving a shot brief: subject, action, camera, mood.
- If the user names a real public figure (a politician, celebrity), replace \
with a fictional analog and note this in the `notes` field. The video model \
will refuse real-public-figure prompts and the whole render will fail.
- If the prompt is already detailed and doesn't reference any known IP, still \
split it into N scenes but keep the user's descriptions verbatim.

Output ONLY the JSON object, no prose."""


def _known_character_hint(prompt: str) -> str | None:
    """Fast keyword pre-check. If a prompt clearly references a well-known
    character catalog, we can pass a tiny extra hint to the LLM to lock in
    canonical designs. Returns None if nothing matches.

    This is a hint, not a gate — the LLM still writes the descriptions.
    """
    p = prompt.lower()
    catalogs = {
        "south park": "South Park characters (Cartman: chubby 8yo, red jacket, "
        "cyan/yellow bobble hat, brown pants, mittens; Stan: blue/red bobble hat, "
        "brown jacket; Kyle: green ushanka, orange jacket, red curly hair; "
        "Kenny: orange parka with hood covering face). Style: flat cutout, "
        "construction-paper animation.",
        "family guy": "Family Guy characters (Peter: fat middle-aged man in "
        "white shirt/green pants/glasses; Lois: red-haired thin woman in green "
        "shirt/tan pants; Stewie: football-shaped-head baby in yellow overalls; "
        "Brian: white anthropomorphic dog with red collar; Chris: chubby teen "
        "in white shirt/blue pants; Meg: teen girl in pink shirt/blue jeans/beanie). "
        "Style: flat 2D cartoon, thick black outlines, bright flat colors.",
        "the simpsons": "Simpsons characters (Homer: bald fat man in white shirt/"
        "blue pants; Marge: tall blue beehive hair, green dress, red necklace; "
        "Bart: spiky yellow hair, orange shirt, blue shorts; Lisa: yellow "
        "starfish hair, red dress; Maggie: baby with blue outfit/pacifier). "
        "All characters have bright yellow skin. Style: flat 2D cartoon, "
        "yellow palette, thick outlines.",
        "rick and morty": "Rick and Morty characters (Rick: elderly scientist, "
        "spiky light-blue hair, unibrow, white lab coat, blue pants, drooling; "
        "Morty: 14yo boy, brown hair, yellow shirt, blue pants, anxious). "
        "Style: 2D cartoon, adult-swim adult animation, thick outlines.",
        "the office": "The Office (US) characters — live-action mockumentary style, "
        "handheld camera, fluorescent office lighting, muted beige/blue palette. "
        "Michael Scott: middle-aged white man in cheap suit, dark hair. "
        "Dwight: bespectacled, mustard shirt, brown tie, olive pants, receding "
        "hairline. Jim: tall thin younger man, blue button-down. Pam: soft-spoken "
        "woman with light brown hair, cardigans.",
        "seinfeld": "Seinfeld characters — live-action 90s sitcom, multi-camera, "
        "warm apartment/diner lighting. Jerry: thin man in jeans and white sneakers. "
        "George: short bald stocky man in glasses. Elaine: brunette curly hair, "
        "blazers. Kramer: tall lanky man, wild hair, retro shirts.",
    }
    for kw, desc in catalogs.items():
        if kw in p:
            return desc
    if "cartman" in p or "kyle broflovski" in p or "kenny mccormick" in p:
        return catalogs["south park"]
    if "peter griffin" in p or "stewie" in p or "quagmire" in p:
        return catalogs["family guy"]
    if "homer simpson" in p or "bart simpson" in p or "marge simpson" in p:
        return catalogs["the simpsons"]
    if "rick sanchez" in p and ("morty" in p or "rick and" in p):
        return catalogs["rick and morty"]
    if "michael scott" in p or "dwight schrute" in p or "jim halpert" in p:
        return catalogs["the office"]
    if "jerry seinfeld" in p or "george costanza" in p or "kramer" in p:
        return catalogs["seinfeld"]
    return None


def expand_prompt(prompt: str, scene_durations: list[int]) -> dict[str, Any]:
    """Expand a user prompt into a per-scene prompt list using an LLM.

    Args:
        prompt: The user's raw episode idea.
        scene_durations: The pre-computed per-scene durations (seconds).

    Returns:
        {
            "ok": bool,
            "style": str,
            "characters": str,
            "scenes": [str, ...],   # length == len(scene_durations)
            "notes": str,           # e.g. "Replaced 'Obama' with fictional analog"
            "provider": "anthropic" | "fallback",
            "latency_ms": int,
            "error": str | None,
        }
    """
    n = len(scene_durations)
    t0 = time.time()

    if not ANTHROPIC_KEY:
        return _fallback(prompt, scene_durations, t0, "no ANTHROPIC_API_KEY set")

    hint = _known_character_hint(prompt)
    user_msg = (
        f"User episode idea:\n{prompt.strip()}\n\n"
        f"Number of scenes to produce: {n}\n"
        f"Per-scene durations (seconds): {scene_durations}\n"
    )
    if hint:
        user_msg += f"\nCanonical reference for this franchise:\n{hint}\n"
    user_msg += (
        "\nReturn a JSON object with keys: style, characters, scenes "
        f"(array of exactly {n} strings), notes."
    )

    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 4096,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=EXPANSION_TIMEOUT,
        )
    except requests.RequestException as e:
        return _fallback(prompt, scene_durations, t0, f"http error: {e}")

    if r.status_code != 200:
        return _fallback(
            prompt, scene_durations, t0,
            f"anthropic {r.status_code}: {r.text[:200]}"
        )

    try:
        data = r.json()
        text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        raw = "".join(text_blocks).strip()
    except Exception as e:
        return _fallback(prompt, scene_durations, t0, f"parse error: {e}")

    # Model sometimes wraps JSON in ```json fences even when told not to.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return _fallback(prompt, scene_durations, t0, f"no JSON in response: {raw[:200]}")
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return _fallback(prompt, scene_durations, t0, f"invalid JSON: {e}")

    scenes = parsed.get("scenes") or []
    if not isinstance(scenes, list) or len(scenes) != n:
        return _fallback(
            prompt, scene_durations, t0,
            f"expected {n} scenes, got {len(scenes) if isinstance(scenes, list) else type(scenes).__name__}"
        )

    # Normalize each scene to a str and cap length so we don't blow past
    # MiniMax's prompt limit.
    scenes_norm: list[str] = []
    for s in scenes:
        if isinstance(s, dict):
            s = s.get("prompt") or s.get("text") or json.dumps(s)
        s = str(s).strip()
        if len(s) > 1500:
            s = s[:1500]
        scenes_norm.append(s)

    return {
        "ok": True,
        "style": str(parsed.get("style", "")).strip(),
        "characters": str(parsed.get("characters", "")).strip(),
        "scenes": scenes_norm,
        "notes": str(parsed.get("notes", "")).strip(),
        "provider": "anthropic",
        "latency_ms": int((time.time() - t0) * 1000),
        "error": None,
    }


def _fallback(prompt: str, scene_durations: list[int], t0: float, error: str) -> dict[str, Any]:
    """Fallback: build per-scene prompts the same way we did before expansion.

    We still return the same shape so multi_scene_worker doesn't need branches —
    it just consumes `scenes` regardless of provider.
    """
    n = len(scene_durations)
    hint = _known_character_hint(prompt) or ""
    scenes = []
    for i in range(n):
        s = (
            f"Scene {i+1} of {n} in a continuous story. "
            f"Maintain the exact same characters, wardrobe, setting, and visual style throughout. "
        )
        if hint:
            s += f"Style/character reference: {hint} "
        s += prompt.strip()
        scenes.append(s)
    return {
        "ok": False,
        "style": "",
        "characters": hint,
        "scenes": scenes,
        "notes": "",
        "provider": "fallback",
        "latency_ms": int((time.time() - t0) * 1000),
        "error": error,
    }
