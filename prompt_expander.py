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
         the character bible + style guide so every scene renders a
         consistent-looking character.

Providers (chosen at startup by which env vars are set, first hit wins):
  1. Grok (xAI) via Anthropic-compat endpoint  — XAI_API_KEY
  2. Anthropic direct                          — ANTHROPIC_API_KEY
Both speak the same Anthropic Messages wire format so we only maintain one
call path. If neither is set (or the call fails), we fall back to the
catalog-based expander so renders still work.

Cost:
  Grok grok-4-fast: ~$0.20 / 1M input, ~$0.50 / 1M output.
  Claude Haiku 4.5: comparable range.
  ~1-2K tokens per plan = ~$0.001-0.003 per render either way.
Latency: ~2-4s single call at plan time.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

# ------------------------------------------------------------------
# Provider selection
# ------------------------------------------------------------------
# Grok (xAI) exposes an Anthropic-compatible /v1/messages endpoint at
# api.x.ai, so the same request body works for either provider — we just
# pick the URL + auth header at call time.
#
# Order of preference: Grok, then Anthropic. The user's setup uses Grok.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "").strip()
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-fast-non-reasoning")
XAI_URL = "https://api.x.ai/v1/messages"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _select_provider() -> tuple[str, str, str, dict[str, str]] | None:
    """Return (provider_name, url, model, headers) for the first configured LLM,
    or None when no key is set. Evaluated on every call so restarts pick up
    env changes without a code deploy.
    """
    if XAI_API_KEY:
        return (
            "grok",
            XAI_URL,
            XAI_MODEL,
            {
                "x-api-key": XAI_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
    if ANTHROPIC_KEY:
        return (
            "anthropic",
            ANTHROPIC_URL,
            ANTHROPIC_MODEL,
            {
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
    return None

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

CRITICAL rules about audio and speech:
- If the user prompt says a character talks, speaks, tells a story, narrates, \
or otherwise uses spoken language, EVERY scene must maintain that they are \
speaking (with the specified language) with visible lip movement / mouth \
sync. Do NOT let later scenes drift into silence, background music only, \
non-verbal sounds, or generic reactions.
- If the user prompt specifies English (or any specific language), repeat \
"speaking in [language] with clear lip sync" in every scene prompt.
- For animals or non-human characters that talk in the user prompt (e.g. \
"my dog telling me about her day in English"), each scene must reinforce \
"[animal] speaks [language] with human-like lip sync, mouth clearly forming \
words" — never barks / meows / silent reactions unless the user asked for it.
- Continuous action: pick up each scene where the previous one left off. \
Mention what the character was doing at the end of the previous scene so \
the frame-chain handoff (last-frame→first-frame) reads as one continuous shot.

MUSIC AND LAUGH-TRACK RULES (very important — don't skip):
- If a franchise audio signature is provided in the user message, embed the \
signature verbatim in EVERY scene prompt. The model has no idea whether a \
show has a laugh track, a slap-bass sting, or an orchestral cutaway cue \
unless we say so per scene.
- Multi-camera sitcoms (Seinfeld, Friends, Big Bang Theory, Cheers) MUST \
have studio-audience laughter on punchlines with a clear pause beat before \
the next line. Never omit this.
- Single-camera mockumentaries (The Office, Parks and Rec, Modern Family, \
Arrested Development, Always Sunny, Curb) MUST NEVER have a laugh track \
and MUST NEVER have underscore music playing under dialogue.
- Animated prime-time (Family Guy, Simpsons, American Dad) uses orchestral \
cartoon score with musical stings on cutaways/transitions, no laugh track.
- Adult animation (Rick and Morty, South Park, BoJack) has NO laugh track, \
minimal or sparse music, dry comedic timing.
- If NO franchise signature is provided but the prompt describes a sitcom-\
style scene, default to single-camera style (no laugh track, diegetic sound \
only). Do not invent a laugh track.
- The first scene of an episode should mention the show's theme/opening \
music cue briefly. The last scene should end with the show's closing music \
sting swelling on the final beat.

TRANSITION AND EDITORIAL RULES:
- End each scene on a beat that primes the next cut (a punchline, a reaction \
hold, an entrance, a discovery), not mid-motion.
- If the scene brief marks the scene as COLD OPEN, end with a smash cut to \
the theme/music sting.
- If the scene brief marks it as TAG (last scene), end with the closing \
music cue swelling under a held reaction shot.
- When two consecutive scenes belong to DIFFERENT story threads (A-story \
cutting to B-story or vice versa), the scene ending the outgoing thread \
should end on a hard punchline/reaction and briefly mention 'hard cut to \
[new location]' in its final beat so the transition reads editorially.
- For act-break scenes (roughly 1/3 and 2/3 through in a long episode), \
end on a held reaction beat with the show's music cue swelling, then \
fade-out language.

General rules:
- Every scene prompt MUST embed the style sentence and character descriptions \
inline. The model sees each scene in isolation — it has no shared context.
- Keep each scene prompt under 500 characters.
- Write like a director giving a shot brief: subject, action, camera, mood, \
audio.
- If the user names a real public figure (a politician, celebrity), replace \
with a fictional analog and note this in the `notes` field. The video model \
will refuse real-public-figure prompts and the whole render will fail.
- If the prompt is already detailed and doesn't reference any known IP, still \
split it into N scenes but keep the user's descriptions verbatim.

Output ONLY the JSON object, no prose."""


# ---------------------------------------------------------------------------
# Franchise audio + editorial signatures
# ---------------------------------------------------------------------------
#
# H3 will happily render a Seinfeld scene without any slap-bass sting and
# a Family Guy scene without the piano cue — it just picks whatever generic
# music (or silence) it feels like. Same story for laugh tracks: multi-cam
# sitcoms MUST have one on punchlines, single-cam MUST NOT, and adult
# animation rides on musical stings and character reactions instead. The
# model has no idea which is which unless we tell it, per scene, every time.
#
# This table gets injected into the system prompt so the LLM knows how to
# write audio direction into every scene, and gets injected into fallback
# scenes so we still get audio guidance when the LLM path fails.
#
# Each signature is a paragraph the LLM/model can consume verbatim, not
# structured fields — keeps the prompt readable when it's echoed into the
# scene brief.

_FRANCHISE_AUDIO_SIGNATURES: dict[str, str] = {
    "seinfeld": (
        "AUDIO SIGNATURE (Seinfeld): the signature slap-bass sting between "
        "scenes and as a music cue on punchlines. Multi-camera studio audience "
        "— hearty studio laughter on every punchline and reaction beat, with a "
        "clear beat of laughter pause before the next line. Warm apartment/diner "
        "room tone, no underscore during dialogue."
    ),
    "family guy": (
        "AUDIO SIGNATURE (Family Guy): brassy orchestral cartoon score with a "
        "jaunty piano vamp on scene transitions and cutaway gags. NO laugh "
        "track — this is animated prime-time, comedy is carried by musical "
        "stings and character reactions. Cartoonish sound effects (boings, "
        "honks, slide-whistles) on physical comedy beats."
    ),
    "the simpsons": (
        "AUDIO SIGNATURE (The Simpsons): light Alf Clausen-style orchestral "
        "underscore with woodwinds and brass punctuating gags. NO laugh track. "
        "Warm suburban room tone. Character voices carry the comedy; music "
        "stays low under dialogue and pops up on transitions."
    ),
    "rick and morty": (
        "AUDIO SIGNATURE (Rick and Morty): sparse synth score with occasional "
        "orchestral swells for cosmic beats. NO laugh track — adult animation, "
        "comedy is dry, science-fiction ambient sound design (portal woosh, "
        "sci-fi UI bleeps, dimensional hum) carries scene atmosphere. Music "
        "stays out of the way of Rick's fast dialogue."
    ),
    "south park": (
        "AUDIO SIGNATURE (South Park): minimal music, mostly diegetic. NO laugh "
        "track. Kids' voices carry every beat. Cheap synthesized cues on scene "
        "transitions, occasional guitar riff. Boys' hallway/classroom room tone "
        "or Cartman's basement ambience underneath dialogue."
    ),
    "the office": (
        "AUDIO SIGNATURE (The Office, US): single-camera mockumentary — "
        "absolutely NO laugh track and NO underscore during dialogue. Only "
        "diegetic sound: fluorescent hum, phones, keyboards, copier, distant "
        "office chatter. The soft acoustic-guitar theme cue appears ONLY on "
        "talking-head cuts or act-break transitions, never over dialogue."
    ),
    "parks and recreation": (
        "AUDIO SIGNATURE (Parks and Recreation): single-camera mockumentary — "
        "NO laugh track, NO underscore during dialogue. Diegetic office/park "
        "ambience only. Bouncy acoustic-banjo theme cue on talking-head cuts "
        "and act-break transitions."
    ),
    "the big bang theory": (
        "AUDIO SIGNATURE (The Big Bang Theory): multi-camera studio audience — "
        "loud studio laughter on every punchline with a clear pause beat. No "
        "underscore during dialogue. Barenaked Ladies-style upbeat theme cue "
        "on scene transitions only."
    ),
    "friends": (
        "AUDIO SIGNATURE (Friends): multi-camera studio audience — studio "
        "laughter and occasional applause on punchlines and character entrances. "
        "No underscore during dialogue. Warm apartment/coffee-house room tone. "
        "Rembrandts-style theme cue only on transitions."
    ),
    "cheers": (
        "AUDIO SIGNATURE (Cheers): multi-camera studio audience — studio "
        "laughter on punchlines and character entrances (especially Norm). Warm "
        "bar-room ambience with clinking glasses and low chatter under dialogue. "
        "Piano theme cue on transitions only, no underscore during dialogue."
    ),
    "it's always sunny": (
        "AUDIO SIGNATURE (It's Always Sunny in Philadelphia): single-camera — "
        "NO laugh track. Classical music cues (Temptation Rag piano, Bolero, "
        "etc.) on act-break transitions and scene changes. Otherwise dry "
        "diegetic sound: Paddy's Pub ambience, Philadelphia street noise."
    ),
    "curb your enthusiasm": (
        "AUDIO SIGNATURE (Curb Your Enthusiasm): single-camera — NO laugh "
        "track. The tuba-and-mandolin 'Frolic' cue on transitions and awkward "
        "beats. Otherwise natural room tone. Dry, awkward silences are load-bearing "
        "— don't fill them with music."
    ),
    "arrested development": (
        "AUDIO SIGNATURE (Arrested Development): single-camera with narrator — "
        "NO laugh track. Ukulele-and-banjo theme cue on transitions and "
        "narrator asides. Ron Howard-style dry voiceover narration between "
        "scenes. Otherwise diegetic sound only."
    ),
}

# Extra aliases mapping character names to franchises so we catch prompts
# that mention characters without naming the show.
_FRANCHISE_ALIASES: dict[str, str] = {
    "cartman": "south park", "kyle broflovski": "south park", "kenny mccormick": "south park",
    "peter griffin": "family guy", "stewie": "family guy", "quagmire": "family guy",
    "homer simpson": "the simpsons", "bart simpson": "the simpsons", "marge simpson": "the simpsons",
    "rick sanchez": "rick and morty",
    "michael scott": "the office", "dwight schrute": "the office", "jim halpert": "the office",
    "jerry seinfeld": "seinfeld", "george costanza": "seinfeld", "kramer": "seinfeld", "elaine benes": "seinfeld",
    "leslie knope": "parks and recreation", "ron swanson": "parks and recreation",
    "sheldon cooper": "the big bang theory", "leonard hofstadter": "the big bang theory",
    "ross geller": "friends", "chandler bing": "friends", "joey tribbiani": "friends",
    "norm peterson": "cheers", "sam malone": "cheers",
    "dennis reynolds": "it's always sunny", "charlie kelly": "it's always sunny", "frank reynolds": "it's always sunny",
    "larry david": "curb your enthusiasm",
    "michael bluth": "arrested development", "gob bluth": "arrested development",
}


def _franchise_audio_signature(prompt: str) -> str | None:
    """Return the audio+editorial signature for a franchise, or None.

    Prefers direct show mentions, falls back to character-name aliases. This
    is the source of truth for 'does this show have a laugh track', 'what's
    the music cue', 'multi-cam or single-cam editorial rhythm'.
    """
    p = prompt.lower()
    for key in _FRANCHISE_AUDIO_SIGNATURES:
        if key in p:
            return _FRANCHISE_AUDIO_SIGNATURES[key]
    for alias, franchise in _FRANCHISE_ALIASES.items():
        if alias in p:
            return _FRANCHISE_AUDIO_SIGNATURES.get(franchise)
    return None


# Generic transition guidance appended to the system prompt so the LLM writes
# real editorial language into scene prompts — not just "cut to" but the
# right KIND of cut for the format. When we have an outline (long form),
# expand_prompt also injects thread-change guidance so A↔B story cuts get
# proper transitional beats instead of smash-cuts.
_TRANSITION_GUIDANCE = (
    "TRANSITIONS: End each scene on a beat that primes the next cut. "
    "For multi-camera sitcoms use hard cuts on punchlines with a scene-transition "
    "music sting between scenes. For single-camera mockumentary use natural "
    "handheld cuts, occasional talking-head interjections between scenes. "
    "For animated prime-time use hard cuts with a brief musical stab on scene "
    "changes. For act breaks (scenes marked TAG or the last scene of an A/B "
    "story arc), end on a held reaction beat with the music cue swelling."
)


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


def expand_prompt(
    prompt: str,
    scene_durations: list[int],
    outline: dict | None = None,
) -> dict[str, Any]:
    """Expand a user prompt into a per-scene prompt list using an LLM.

    Args:
        prompt: The user's raw episode idea.
        scene_durations: The pre-computed per-scene durations (seconds).
        outline: Optional pre-approved story outline from plan_outline().
            When present, we hand it to the LLM as the authoritative story
            structure and ask for scene prompts that follow it — A-story
            scenes cut between B-story scenes, cold-open first, tag last.
            Without an outline (short renders), we use the original
            one-shot behavior.

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

    selected = _select_provider()
    if selected is None:
        return _fallback(prompt, scene_durations, t0, "no LLM key set (XAI_API_KEY or ANTHROPIC_API_KEY)")
    provider_name, url, model, headers = selected

    hint = _known_character_hint(prompt)
    audio_sig = _franchise_audio_signature(prompt)
    user_msg = (
        f"User episode idea:\n{prompt.strip()}\n\n"
        f"Number of scenes to produce: {n}\n"
        f"Per-scene durations (seconds): {scene_durations}\n"
    )
    if hint:
        user_msg += f"\nCanonical reference for this franchise:\n{hint}\n"
    if audio_sig:
        user_msg += (
            f"\nFranchise audio signature (embed this verbatim, or an obvious "
            f"paraphrase, in EVERY scene prompt — the video model has no memory "
            f"of what this show sounds like):\n{audio_sig}\n"
        )

    # If we have a pre-approved outline, hand it to the LLM as the story
    # blueprint. We ask it to distribute the N scenes across the cold open,
    # A-story beats, B-story beats, and tag — cutting between A and B as a
    # real sitcom would.
    if outline:
        user_msg += (
            "\nPre-approved story outline (follow this exactly, cutting "
            "between A-story and B-story scenes as a sitcom would):\n"
            + json.dumps(outline, indent=2)
            + "\n\nDistribute the scenes roughly:\n"
            "- Scene 1: cold open\n"
            "- ~55% of remaining scenes to A-story beats (in order)\n"
            "- ~35% of remaining scenes to B-story beats (in order)\n"
            "- Interleave A and B scenes as cuts (A, A, B, A, B, B, A, ...) "
            "so both stories progress in parallel. Don't render all A then all B.\n"
            "- Last scene: tag\n"
            "- Each scene prompt should reference which story thread it "
            "belongs to at the start of the prompt (e.g. 'A-STORY BEAT 2:' "
            "or 'B-STORY BEAT 1:' or 'COLD OPEN:' or 'TAG:').\n"
            "\nTransition and pacing rules for the outline:\n"
            "- Cold open ends with the theme/music sting swelling and a \"hard cut to titles\" beat.\n"
            "- The first scene AFTER a thread change (A->B or B->A) must include "
            "'hard cut to [new location]' in its opening beat so the editorial "
            "transition is legible; the scene BEFORE the thread change must end "
            "on a punchline/reaction hold, not mid-motion.\n"
            "- Roughly 1/3 and 2/3 through the scene list, mark that scene as an "
            "act break: end on a held reaction beat with the music cue swelling, "
            "then a fade-out.\n"
            "- Tag ends with the closing theme sting under a held final reaction.\n"
        )

    user_msg += (
        "\nReturn a JSON object with keys: style, characters, scenes "
        f"(array of exactly {n} strings), notes."
    )

    try:
        r = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": 4096,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=EXPANSION_TIMEOUT,
        )
    except requests.RequestException as e:
        return _fallback(prompt, scene_durations, t0, f"http error ({provider_name}): {e}")

    if r.status_code != 200:
        return _fallback(
            prompt, scene_durations, t0,
            f"{provider_name} {r.status_code}: {r.text[:200]}"
        )

    try:
        data = r.json()
        # Both providers use Anthropic's content-block schema. We only want
        # the actual text output — Grok additionally emits "thinking" blocks
        # (its reasoning trace) that we must ignore. Any block whose type
        # isn't exactly "text" is dropped.
        text_blocks = [
            b.get("text", "")
            for b in data.get("content", [])
            if b.get("type") == "text"
        ]
        raw = "".join(text_blocks).strip()
    except Exception as e:
        return _fallback(prompt, scene_durations, t0, f"parse error ({provider_name}): {e}")

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
        "provider": provider_name,
        "latency_ms": int((time.time() - t0) * 1000),
        "error": None,
    }


# Keywords that indicate spoken dialogue in the user prompt. When any of these
# appears, we inject an audio-continuity clause into every scene prompt so
# later scenes don't drift into silence or non-verbal sounds (barks, growls,
# background music only).
_SPEECH_KEYWORDS = (
    "talk", "talking", "talks", "speak", "speaks", "speaking", "tell",
    "telling", "tells", "say", "saying", "says", "narrate", "narrating",
    "narrator", "story", "english", "spanish", "french", "conversation",
    "dialogue", "monologue", "speech", "in english", "about her day",
    "about his day", "about my day",
)


def _speech_hint(prompt: str) -> str:
    """Return an audio-continuity clause if the prompt implies spoken dialogue.

    Detects animal-talking prompts too, since MiniMax H3 defaults to bark/meow
    on non-first scenes even when scene 1 has clear speech.
    """
    p = prompt.lower()
    if not any(kw in p for kw in _SPEECH_KEYWORDS):
        return ""
    # Animal-speaking? Extra emphasis — the model needs pushing here.
    animal_words = ("dog", "cat", "bird", "puppy", "kitten", "parrot", "pet")
    is_animal = any(a in p for a in animal_words)
    lang = "English"
    for kw, name in (("spanish", "Spanish"), ("french", "French"),
                     ("japanese", "Japanese"), ("german", "German")):
        if kw in p:
            lang = name
            break
    if is_animal:
        return (
            f"AUDIO REQUIREMENT: the animal speaks conversational {lang} with "
            f"clear human-like lip sync throughout — mouth clearly forming words, "
            f"never barking, meowing, growling, or silent. Continuous dialogue "
            f"from the previous shot. "
        )
    return (
        f"AUDIO REQUIREMENT: the character speaks {lang} with clear lip sync "
        f"throughout — continuous dialogue from the previous shot, never silent "
        f"or background-music only. "
    )


def _fallback(prompt: str, scene_durations: list[int], t0: float, error: str) -> dict[str, Any]:
    """Fallback: build per-scene prompts the same way we did before expansion.

    We still return the same shape so multi_scene_worker doesn't need branches —
    it just consumes `scenes` regardless of provider.

    Even in the fallback path we inject the franchise audio signature and a
    lightweight editorial cue for the first and last scene, so a fallback
    render doesn't lose the laugh-track/music behavior when the LLM is down.
    """
    n = len(scene_durations)
    hint = _known_character_hint(prompt) or ""
    audio_hint = _speech_hint(prompt)
    audio_sig = _franchise_audio_signature(prompt) or ""
    scenes = []
    for i in range(n):
        s = (
            f"Scene {i+1} of {n} in a continuous story. "
            f"Maintain the exact same characters, wardrobe, setting, and visual style throughout. "
        )
        if audio_hint:
            s += audio_hint
        if audio_sig:
            s += audio_sig + " "
        # Cold-open/tag editorial cues so the first and last scenes have the
        # theme sting even without the LLM's help.
        if i == 0 and audio_sig:
            s += "Open with the show's theme/music sting briefly under the opening beat. "
        if i == n - 1 and audio_sig:
            s += "End on a held reaction beat with the show's closing music cue swelling. "
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


# ---------------------------------------------------------------------------
# Story outline planner (Pass 1 of two-pass long-form workflow)
# ---------------------------------------------------------------------------
#
# Why this exists:
#   For short renders (< 60s / < 10 scenes) our one-shot expander produces
#   fine results — the video is essentially one continuous beat and needs no
#   story architecture.
#
#   For long renders (>= 60s / >= 10 scenes) the one-shot expander produces
#   120 continuous-action prompts with no dramatic arc. A real sitcom episode
#   is cold-open + A-story + B-story + tag, with the stories cutting back
#   and forth. Without that structure a 20-min render is one long monologue
#   with no tension.
#
# Two-pass flow:
#   1. plan_outline(prompt) — Grok returns a structured outline (cheap, ~3s).
#   2. USER APPROVES/EDITS the outline in the UI.
#   3. expand_prompt(prompt, durations, outline=...) — one prompt per scene,
#      each scene tagged with which story thread it belongs to.
#
# The outline is intentionally short (~500 tokens) so it's cheap enough that
# users don't feel penalized for looking at it before committing to a render.

OUTLINE_TIMEOUT = 20.0

OUTLINE_SYSTEM_PROMPT = """You are a TV sitcom writer's-room outliner.

Given a user's episode idea, produce a structured story outline in the shape
of a real 22-minute sitcom episode. The outline will be shown to the user
for approval before we generate 100+ scenes of video, so it must be tight,
readable, and honest about what the episode is actually about.

Output shape (JSON, no prose):
{
  "logline": "one-sentence pitch of the whole episode",
  "cold_open": {
    "beat": "one-sentence description of the pre-title scene",
    "characters": ["Char1", "Char2"]
  },
  "a_story": {
    "title": "3-5 word A-story title",
    "premise": "one sentence, what this story is about",
    "beats": [
      "Beat 1: setup",
      "Beat 2: complication",
      "Beat 3: escalation",
      "Beat 4: turn / climax",
      "Beat 5: resolution"
    ],
    "characters": ["Char1", "Char2"]
  },
  "b_story": {
    "title": "3-5 word B-story title",
    "premise": "one sentence, what this story is about",
    "beats": [
      "Beat 1: setup",
      "Beat 2: complication",
      "Beat 3: turn",
      "Beat 4: resolution"
    ],
    "characters": ["Char3", "Char4"]
  },
  "tag": {
    "beat": "one-sentence description of the post-credits tag",
    "characters": ["Char1"]
  },
  "notes": "any writer-room notes: real-figure substitutions, subplots dropped for length, etc."
}

Rules:
- A-story is always the main story implied by the user's prompt.
- B-story must be genuinely PARALLEL — a different set of characters doing
  a different thing that pays off separately. Do NOT make the B-story a
  sub-scene of the A-story.
- The B-story can lightly cross over with the A-story in one beat if it
  serves the ending.
- If the user's prompt is a franchise show (Seinfeld, The Office, Family
  Guy, South Park, etc.), cast the outline from that show's canonical
  characters. Use the group whose absence would make the episode NOT feel
  like an episode of that show. For Seinfeld default to Jerry/George/
  Elaine/Kramer; for The Office default to Michael/Dwight/Jim/Pam; for
  Family Guy default to Peter/Lois/Stewie/Brian; etc. Use Newman, Toby,
  etc. only when the story specifically calls for them.
- Do not invent characters that don't exist in the source show. For
  original prompts, invent whatever characters serve the story and
  describe them in the character list.
- If the user names a real public figure, replace with a fictional analog
  in the outline and mention this in `notes` — the video model will refuse
  real-figure prompts and the whole render will fail.
- Cold open should be a small self-contained moment that hints at the
  episode's theme without giving away the A-story.
- Tag should be a short post-credits button — often a callback to the
  cold open or a B-story character reacting to what happened.

Output ONLY the JSON object, no prose."""


def plan_outline(prompt: str) -> dict[str, Any]:
    """Pass 1 of the two-pass long-form workflow: return a structured story
    outline for the user to approve/edit before we generate scene prompts.

    Args:
        prompt: The user's raw episode idea.

    Returns:
        {
            "ok": bool,
            "outline": dict | None,     # the parsed outline JSON
            "provider": str,            # "grok" | "anthropic" | "fallback"
            "latency_ms": int,
            "error": str | None,
        }

    Never raises. On any LLM failure returns ok=False with a fallback
    outline shape so the frontend still has something to render.
    """
    t0 = time.time()
    selected = _select_provider()
    if selected is None:
        return _outline_fallback(prompt, t0, "no LLM key set (XAI_API_KEY or ANTHROPIC_API_KEY)")
    provider_name, url, model, headers = selected

    hint = _known_character_hint(prompt)
    user_msg = f"User episode idea:\n{prompt.strip()}\n"
    if hint:
        user_msg += f"\nCanonical reference for this franchise:\n{hint}\n"
    user_msg += "\nReturn the JSON outline."

    try:
        r = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": 2000,
                "system": OUTLINE_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=OUTLINE_TIMEOUT,
        )
    except requests.RequestException as e:
        return _outline_fallback(prompt, t0, f"http error ({provider_name}): {e}")

    if r.status_code != 200:
        return _outline_fallback(prompt, t0, f"{provider_name} {r.status_code}: {r.text[:200]}")

    try:
        data = r.json()
        text_blocks = [
            b.get("text", "")
            for b in data.get("content", [])
            if b.get("type") == "text"
        ]
        raw = "".join(text_blocks).strip()
    except Exception as e:
        return _outline_fallback(prompt, t0, f"parse error ({provider_name}): {e}")

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return _outline_fallback(prompt, t0, f"no JSON in response: {raw[:200]}")
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return _outline_fallback(prompt, t0, f"invalid JSON: {e}")

    # Light schema normalization — never crash, just fill missing sections.
    outline = _normalize_outline(parsed)

    return {
        "ok": True,
        "outline": outline,
        "provider": provider_name,
        "latency_ms": int((time.time() - t0) * 1000),
        "error": None,
    }


def _normalize_outline(obj: dict) -> dict:
    """Ensure the outline has all required keys with reasonable defaults."""
    def _str(x, default=""):
        return str(x).strip() if x else default
    def _list(x):
        return [str(i).strip() for i in x if str(i).strip()] if isinstance(x, list) else []

    def _story(x, default_beats: int) -> dict:
        x = x if isinstance(x, dict) else {}
        return {
            "title": _str(x.get("title"), "Untitled"),
            "premise": _str(x.get("premise")),
            "beats": _list(x.get("beats")) or [f"Beat {i+1}" for i in range(default_beats)],
            "characters": _list(x.get("characters")),
        }

    def _short(x) -> dict:
        x = x if isinstance(x, dict) else {}
        return {
            "beat": _str(x.get("beat")),
            "characters": _list(x.get("characters")),
        }

    return {
        "logline": _str(obj.get("logline")),
        "cold_open": _short(obj.get("cold_open")),
        "a_story": _story(obj.get("a_story"), 5),
        "b_story": _story(obj.get("b_story"), 4),
        "tag": _short(obj.get("tag")),
        "notes": _str(obj.get("notes")),
    }


def _outline_fallback(prompt: str, t0: float, error: str) -> dict[str, Any]:
    """Minimal outline so the frontend still has something to display when
    the LLM call fails. User can still edit it before approving."""
    return {
        "ok": False,
        "outline": {
            "logline": prompt.strip()[:200],
            "cold_open": {"beat": "", "characters": []},
            "a_story": {
                "title": "Main Story",
                "premise": prompt.strip()[:200],
                "beats": ["Setup", "Complication", "Escalation", "Turn", "Resolution"],
                "characters": [],
            },
            "b_story": {
                "title": "Subplot",
                "premise": "",
                "beats": ["Setup", "Complication", "Turn", "Resolution"],
                "characters": [],
            },
            "tag": {"beat": "", "characters": []},
            "notes": "(outline generation failed — you can still edit this manually)",
        },
        "provider": "fallback",
        "latency_ms": int((time.time() - t0) * 1000),
        "error": error,
    }
