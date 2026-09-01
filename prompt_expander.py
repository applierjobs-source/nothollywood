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

SHOT BIBLE — CONSISTENCY LOCKS (very important — read this first):
Every episode has recurring elements the video model reinvents every scene \
unless you lock them down: a hero prop the plot revolves around, each \
character's wardrobe for the entire episode, and the visual spec of each \
recurring location. You MUST produce a `shot_bible` field in the JSON output \
with the following structure so we can re-inject these locks into every \
scene prompt downstream:
  {
    "hero_prop": "one sentence exact visual description of the plot object, \
repeated verbatim in every scene where it appears (color, shape, silhouette, \
material, brand markings). Null if the story has no single hero object.",
    "wardrobe": {
      "<character name>": "one sentence outfit lock for the whole episode \
(garments, colors, footwear, glasses, watches, facial hair). Sitcom \
characters wear the same outfit through an episode; only change it if the \
story explicitly requires a costume change and note that in the beat."
    },
    "locations": {
      "<location name>": "two-sentence canonical spec (interior/exterior, \
lighting, key set dressing, architectural features, wall/floor colors, \
signage). Use the franchise location cards below verbatim when they apply."
    }
  }

Every scene prompt you write MUST already reference the hero prop's exact \
description (not just "the shoes" — the FULL description) whenever the prop \
is on screen, the character's wardrobe lock whenever the character is on \
screen, and the location spec whenever the scene sets there. Don't rely on \
the model to remember from scene 1 — it can't.

ON-SCREEN TEXT RULE (very important):
- Text-in-image models frequently render garbled gibberish signage \
("GINNZS" instead of "MONK'S", "BATMR SHP" instead of "BARBER SHOP"). \
When the scene features a canonical location (Monk's Café, Jerry's \
apartment door, Central Perk, Paddy's Pub, etc.), you MUST specify the \
EXACT text that should appear on the signage in the scene prompt and \
add "clean legible signage typography, no garbled text" so the model \
prioritizes readable characters.
- For non-canonical background signage (menus, storefronts, posters), \
prefer 'blurred out-of-focus background signage' or 'nondescript signage \
out of focus' over an invented brand — that way we avoid gibberish text \
completely.

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


# ---------------------------------------------------------------------------
# Franchise location cards — canonical set specs
# ---------------------------------------------------------------------------
#
# H3 does not know what Monk's Café or Jerry's apartment look like. Left to
# itself it renders a generic diner (often with GIBBERISH signage like
# "GINNZS") and a generic loft (often with a Friends-style staircase). We
# feed it a per-location card so every scene that sets there gets the SAME
# interior every time.
#
# Each entry is `{location_slug: canonical_spec}` — the spec is dropped
# verbatim into the shot bible's `locations` field for that render.

_FRANCHISE_LOCATIONS: dict[str, dict[str, str]] = {
    "seinfeld": {
        "Monk's Café": (
            "Interior of a NYC-style diner: green vinyl booths along the "
            "windows, chrome counter with round red-topped stools, black-and-"
            "white tile floor, warm tungsten pendant lighting. Signage on the "
            "exterior window reads exactly 'MONK'S' in white block letters on "
            "a red awning; a smaller 'RESTAURANT' sign below. Menu boards "
            "behind the counter are blurred/out of focus. Multi-cam sitcom "
            "staging with the front booth as the hero table."
        ),
        "Jerry's Apartment": (
            "Interior of a SINGLE-FLOOR NYC walkup apartment — NO staircase, "
            "NO loft, one open living room with a small galley kitchen behind. "
            "Blue couch facing camera, dining table with wire-back chairs, "
            "bike hanging on the back wall, cereal boxes lined up on top of "
            "the fridge, off-white walls, chrome-and-glass coffee table, front "
            "door on stage-right. Warm tungsten lighting. Multi-cam staging."
        ),
        "Jerry's Apartment Hallway": (
            "Narrow NYC walkup hallway outside Jerry's apartment door — the "
            "door is off-white with a brass '5A' plate. Fluorescent hallway "
            "lighting, patterned carpet runner. No staircase visible in shot."
        ),
        "Comedy Club Stage": (
            "Small stand-up comedy stage in front of a red-brick wall, single "
            "microphone on a stand, warm spotlight, second empty mic stand "
            "visible in the background."
        ),
    },
    "friends": {
        "Central Perk": (
            "Interior of a Greenwich Village coffee house: exposed brick "
            "walls, worn orange upholstered couch as the hero seat, mismatched "
            "armchairs, low coffee table, chalk menu board, warm string "
            "lights, coffee bar in the back. Front window signage reads "
            "exactly 'CENTRAL PERK' in tan serif type. Multi-cam staging."
        ),
        "Monica's Apartment": (
            "Bright purple-walled NYC apartment: kitchen at stage-right with "
            "tile counter, dining table center, big picture window at the "
            "back showing a NYC brick wall, wooden floor, entertainment unit "
            "stage-left, big purple door at stage-right entrance. Multi-cam "
            "staging."
        ),
    },
    "the office": {
        "Dunder Mifflin Bullpen": (
            "Single-camera mockumentary interior of a mid-2000s regional paper "
            "company: rows of gray cubicles, drop-ceiling with fluorescent "
            "panels, beige carpet, corkboards, printers, water cooler in the "
            "back. Muted beige/blue palette. Handheld doc-camera framing."
        ),
        "Michael's Office": (
            "Glass-walled corner office off the bullpen: 'World's Best Boss' "
            "mug on the desk, framed cheap art on the walls, mini-fridge in "
            "the corner, cheap veneer desk, two guest chairs facing the desk."
        ),
        "Conference Room": (
            "Long oval table with black office chairs, whiteboard on one wall, "
            "projection screen, small window into the bullpen."
        ),
    },
    "cheers": {
        "Cheers Bar": (
            "Boston basement tavern interior: dark wood bar with brass rail, "
            "stained-glass window behind, mounted brass and neon beer signs, "
            "round tables, warm amber lighting. Signage above the bar reads "
            "exactly 'CHEERS' in gold serif type. Multi-cam staging."
        ),
    },
    "it's always sunny": {
        "Paddy's Pub": (
            "Divey Philadelphia Irish bar interior: scuffed hardwood floor, "
            "neon beer signs on brick walls, jukebox in the corner, worn "
            "pool table, back office door. Front window signage reads "
            "exactly 'PADDY'S PUB' in green with a shamrock. Single-camera "
            "handheld staging."
        ),
    },
    "parks and recreation": {
        "Pawnee City Hall Office": (
            "Single-camera mockumentary interior of a small-town municipal "
            "office: mint green walls, wood-panel accents, folk-art posters, "
            "beige cubicles, drop ceiling, framed portraits of local "
            "celebrities. Mockumentary handheld framing."
        ),
    },
    "the big bang theory": {
        "Leonard and Sheldon's Apartment": (
            "Pasadena apartment interior with a distinctive brown corduroy "
            "couch as Sheldon's hero seat, whiteboard covered in equations, "
            "periodic-table shower curtain visible through the hallway, IKEA "
            "kitchen behind, comic-book art on the walls. Multi-cam staging."
        ),
    },
}


def _franchise_locations(prompt: str) -> dict[str, str] | None:
    """Return the canonical location cards for a franchise, or None.

    Same alias-first resolution as the audio signature lookup. Returns a
    dict of {location_name: canonical_spec}. The shot bible embeds these
    verbatim so H3 renders the SAME interior every scene.
    """
    p = prompt.lower()
    for key in _FRANCHISE_LOCATIONS:
        if key in p:
            return _FRANCHISE_LOCATIONS[key]
    for alias, franchise in _FRANCHISE_ALIASES.items():
        if alias in p:
            return _FRANCHISE_LOCATIONS.get(franchise)
    return None


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


# ---------------------------------------------------------------------------
# Franchise theme-opener sequences (scene 1 for long-form renders)
# ---------------------------------------------------------------------------
#
# Real sitcoms always open with the title sequence + theme song. Skipping
# straight into the cold open makes an AI-generated episode feel like a
# rough cut, not a produced show. For long-form renders (60s+ where we
# have the outline flow) we now reserve scene 1 as the show's opener.
#
# Each entry is a compact scene brief describing the canonical title
# sequence for that franchise — signature imagery, title-card, and theme
# music. The video model reads this verbatim as scene 1's prompt.
#
# For unknown shows we fall back to a generic title-card opener.

_FRANCHISE_OPENERS: dict[str, str] = {
    "seinfeld": (
        "OPENING (Seinfeld cold open): Jerry Seinfeld on a small brick-wall "
        "stand-up stage in a warmly-lit 90s comedy club, gesturing with a "
        "microphone as he delivers a one-line observational joke. The audience "
        "laughs in the foreground. The signature Seinfeld slap-bass theme "
        "plucks underneath. He steps forward, gestures with his free hand, and "
        "finishes the joke. Continuous motion the whole time \u2014 he never stops "
        "moving. NO text on screen, NO title cards, NO logo cards, NO written "
        "words. Pure live-action performance footage."
    ),
    "family guy": (
        "OPENING TITLE SEQUENCE (Family Guy): the Griffin family (Peter in white "
        "shirt/green pants, Lois in green dress, Chris, Meg, Stewie in yellow "
        "overalls, Brian the white dog) sitting on a piano bench in front of "
        "their Quahog living room, singing the theme song vaudeville-style with "
        "Stewie playing piano. Bright cartoon palette, thick outlines. End on the "
        "'Family Guy' logo card as the theme resolves."
    ),
    "the simpsons": (
        "OPENING TITLE SEQUENCE (The Simpsons): the iconic pan through Springfield "
        "clouds to reveal the town, then quick cuts of Homer at the nuclear plant, "
        "Bart writing on the chalkboard, Marge and Maggie at the checkout counter, "
        "Lisa playing saxophone, and the family racing home to converge on the "
        "couch. Danny Elfman-style orchestral Simpsons theme. Yellow-skinned "
        "Simpsons style, thick outlines. End on the 'The Simpsons' logo card."
    ),
    "rick and morty": (
        "OPENING TITLE SEQUENCE (Rick and Morty): quick chaotic cuts of Rick and "
        "Morty running from monsters through a portal, a spaceship exploding, "
        "Rick drooling and holding a portal gun, Morty screaming, cosmic landscapes "
        "with tentacle creatures. Sparse synth Rick and Morty theme with the "
        "signature belch-and-riff. Adult-swim animation style, thick outlines. "
        "End on the 'Rick and Morty' logo card."
    ),
    "south park": (
        "OPENING TITLE SEQUENCE (South Park): quick construction-paper cutout "
        "cuts of the boys (Stan, Kyle, Cartman, Kenny) walking to the school "
        "bus stop in a snowy Colorado mountain town, with Primus-style guitar "
        "theme song. Flat cutout animation, minimal shading. End on the 'South "
        "Park' logo card."
    ),
    "the office": (
        "OPENING TITLE SEQUENCE (The Office, US): documentary-style handheld "
        "shots of Scranton, PA — pan across the office park sign, snow-covered "
        "streets, Dunder Mifflin building exterior. Then quick cuts of Michael "
        "laughing, Dwight scowling, Jim smirking at camera, Pam smiling at her "
        "reception desk. The iconic Office theme piano/acoustic melody plays. "
        "Fluorescent office lighting, muted beige/blue palette. End on the "
        "'The Office' logo card in the show's slab typography."
    ),
    "parks and recreation": (
        "OPENING TITLE SEQUENCE (Parks and Recreation): documentary-style shots "
        "of Pawnee, Indiana — the town's murals, the parks department building, "
        "and quick cuts of Leslie Knope smiling, Ron Swanson looking stoic with "
        "his moustache, April rolling her eyes. Bouncy banjo Parks and Rec theme "
        "plays. Handheld mockumentary look. End on the 'Parks and Recreation' "
        "logo card."
    ),
    "the big bang theory": (
        "OPENING TITLE SEQUENCE (The Big Bang Theory): rapid-fire montage of "
        "scientific and cultural history — planets, dinosaurs, cavemen, "
        "pyramids, DNA helices, Einstein, moon landing, city skylines — cut "
        "to the Barenaked Ladies' 'History of Everything' theme song. Ends on "
        "the 'The Big Bang Theory' logo card. Multi-camera sitcom color palette."
    ),
    "friends": (
        "OPENING TITLE SEQUENCE (Friends): the six main friends (Ross, Rachel, "
        "Monica, Chandler, Joey, Phoebe) splashing around in a fountain at night "
        "in matching casual outfits, laughing and dancing with a couch and "
        "umbrella props. The Rembrandts 'I'll Be There For You' theme plays. "
        "Warm 90s NYC lighting. End on the 'Friends' logo card in the show's "
        "colorful dotted typography."
    ),
    "cheers": (
        "OPENING TITLE SEQUENCE (Cheers): vintage sepia-toned illustrations "
        "and photos of old Boston bars, patrons drinking together across "
        "decades, warm sepia palette. The 'Where Everybody Knows Your Name' "
        "piano theme plays. End on the 'Cheers' logo card in the show's "
        "classic serif typography."
    ),
    "it's always sunny": (
        "OPENING TITLE SEQUENCE (It's Always Sunny in Philadelphia): grainy "
        "handheld shots of Philadelphia — South Street, the Liberty Bell, "
        "gritty urban streets, Paddy's Pub exterior — with the 'Temptation Rag' "
        "jaunty piano theme playing. Casual naturalistic look. End on the 'It's "
        "Always Sunny in Philadelphia' logo card in typewriter typography."
    ),
    "curb your enthusiasm": (
        "OPENING (Curb Your Enthusiasm cold open): Larry David walking down a "
        "sunny Los Angeles street in his signature khakis and casual polo, "
        "muttering to himself and gesturing in mild irritation, glancing at "
        "passing pedestrians. Natural handheld camera, warm LA daylight. The "
        "'Frolic' tuba-and-mandolin theme by Luciano Michelini plays under the "
        "scene. Continuous walking motion throughout."
    ),
    "arrested development": (
        "OPENING TITLE SEQUENCE (Arrested Development): quick cuts of the "
        "Bluth family posing awkwardly against a white background, with Ron "
        "Howard's narrator voice introducing 'the story of a wealthy family who "
        "lost everything.' The ukulele-and-banjo Arrested Development theme "
        "plays. Bright, single-camera look. End on the 'Arrested Development' "
        "logo card."
    ),
}

# Note: We deliberately AVOID asking H3 Max to render title-card text.
# fal H3 Max cannot reliably render specific words on screen — asking for
# "a Seinfeld logo card" produces frozen frames with misspelled text like
# 'Seinleld' or 'Simsonps'. Instead we describe a lively, motion-filled
# opening establishing shot with the show's theme playing under it. If
# franchise-specific openers below still say "logo card" or "title card",
# rewrite them to describe motion without text.
_GENERIC_OPENER = (
    "OPENING establishing shot: a signature location or character moment "
    "that immediately signals the show's tone \u2014 lively, in motion, with "
    "clear on-screen action from beginning to end. The show's theme music "
    "plays under the scene. NO text on screen, NO title cards, NO logo "
    "cards, NO written words. Continuous camera motion or subject motion "
    "the whole time; nothing static."
)


# fal H3 Max cannot render specific words on screen — it produces frozen
# frames with garbled misspellings ("Seinleld", "Simsonps"). Every legacy
# opener above ends with "End on the 'X' logo card" which is exactly the
# thing that breaks. Strip those sentences at emission time and append a
# hard "no text on screen" instruction so H3 doesn't fall back to a
# hallucinated title card. One place to change instead of touching every
# entry.
_LOGO_CARD_SUFFIX_RE = re.compile(
    r"\s*(?:End on |Ends on |Cut to |End with )"
    r"[^.]*?(?:logo card|title card|title-card)[^.]*\.\s*",
    re.IGNORECASE,
)

_NO_TEXT_APPENDIX = (
    " NO on-screen text, NO title cards, NO logo cards, NO written words "
    "anywhere in the frame \u2014 every second must be lively performance "
    "footage in continuous motion. The show's theme music plays throughout."
)


def _sanitize_opener(opener: str) -> str:
    """Strip 'End on the X logo card' sentences and append a hard no-text
    directive so H3 Max renders live motion instead of a frozen misspelled
    title card."""
    cleaned = _LOGO_CARD_SUFFIX_RE.sub(" ", opener).strip()
    # Collapse any double spaces the substitution left behind
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned + _NO_TEXT_APPENDIX


def _franchise_opener(prompt: str) -> str:
    """Return the opening title sequence brief for a franchise.

    Falls back to a generic title-card opener when no franchise matches, so
    every long-form render still gets a proper theme-song intro. All openers
    are sanitized to remove text-on-screen requests that H3 Max would
    render as frozen misspelled logo cards.
    """
    p = prompt.lower()
    for key in _FRANCHISE_OPENERS:
        if key in p:
            return _sanitize_opener(_FRANCHISE_OPENERS[key])
    for alias, franchise in _FRANCHISE_ALIASES.items():
        if alias in p:
            opener = _FRANCHISE_OPENERS.get(franchise)
            if opener:
                return _sanitize_opener(opener)
    return _sanitize_opener(_GENERIC_OPENER)


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

    # Helper: apply the long-form opener guarantee to a scenes list. Called on
    # every early-return path so a fallback caused by an LLM outage still ships
    # with a proper theme sequence as scene 1.
    def _ensure_opener(result: dict[str, Any]) -> dict[str, Any]:
        if outline and result.get("scenes"):
            first = str(result["scenes"][0]).upper()
            if not any(marker in first for marker in
                       ("OPENING TITLE SEQUENCE", "TITLE SEQUENCE",
                        "LOGO CARD", "THEME SONG")):
                result["scenes"][0] = _franchise_opener(prompt)
        return result

    selected = _select_provider()
    if selected is None:
        return _ensure_opener(_fallback(prompt, scene_durations, t0, "no LLM key set (XAI_API_KEY or ANTHROPIC_API_KEY)"))
    provider_name, url, model, headers = selected

    hint = _known_character_hint(prompt)
    audio_sig = _franchise_audio_signature(prompt)
    loc_cards = _franchise_locations(prompt)
    user_msg = (
        f"User episode idea:\n{prompt.strip()}\n\n"
        f"Number of scenes to produce: {n}\n"
        f"Per-scene durations (seconds): {scene_durations}\n"
    )
    if hint:
        user_msg += f"\nCanonical reference for this franchise:\n{hint}\n"
    if loc_cards:
        cards_text = "\n".join(f"- {name}: {spec}" for name, spec in loc_cards.items())
        user_msg += (
            f"\nCanonical franchise LOCATION CARDS (use verbatim in the "
            f"shot_bible.locations field for any of these locations that "
            f"appear in the story, and reference them in the scene prompts "
            f"whenever the scene sets there):\n{cards_text}\n"
        )
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
        # Reserve scene 1 as the opening title sequence for long-form renders.
        # Real sitcoms always open with the theme song; skipping straight to
        # the cold open makes the episode feel like a rough cut. We pass the
        # opener brief to the LLM and require it to use it verbatim for scene 1,
        # then distribute the outline beats across scenes 2..N.
        opener_brief = _franchise_opener(prompt)
        # Detect thread mode: dual = outline has b_story, single = it doesn't.
        # Single-thread outlines produce a cleaner story on 1-5 min renders
        # because we have ~10-50 scenes total, not enough to develop two threads
        # without both feeling half-baked. Supporting characters become A-story
        # texture rather than their own parallel plot.
        has_b_story = isinstance(outline.get("b_story"), dict) and outline["b_story"].get("beats")

        if has_b_story:
            # Dual-thread (>=5min): interleave A/B as a real 22-min sitcom.
            user_msg += (
                "\nPre-approved story outline (follow this exactly, cutting "
                "between A-story and B-story scenes as a sitcom would):\n"
                + json.dumps(outline, indent=2)
                + "\n\nRESERVED SCENE 1 — OPENING TITLE SEQUENCE (use verbatim as "
                "scene 1's prompt, this is non-negotiable):\n"
                + opener_brief
                + "\n\nDistribute the remaining scenes (2 through N) as follows:\n"
                "- Scene 2: cold open\n"
                "- ~55% of remaining scenes to A-story beats (in order)\n"
                "- ~35% of remaining scenes to B-story beats (in order)\n"
                "- Interleave A and B scenes as cuts (A, A, B, A, B, B, A, ...) "
                "so both stories progress in parallel. Don't render all A then all B.\n"
                "- Last scene: tag\n"
                "- Each scene prompt (except scene 1) should reference which story "
                "thread it belongs to at the start of the prompt (e.g. 'A-STORY BEAT 2:' "
                "or 'B-STORY BEAT 1:' or 'COLD OPEN:' or 'TAG:').\n"
                "\nTransition and pacing rules for the outline:\n"
                "- Scene 1 (title sequence) ends on the logo card hold with the theme resolving.\n"
                "- Scene 2 (cold open) opens by cutting from the logo card into the scene, "
                "and ends with the theme/music sting swelling before act one begins.\n"
                "- The first scene AFTER a thread change (A->B or B->A) must include "
                "'hard cut to [new location]' in its opening beat so the editorial "
                "transition is legible; the scene BEFORE the thread change must end "
                "on a punchline/reaction hold, not mid-motion.\n"
                "- Roughly 1/3 and 2/3 through the scene list, mark that scene as an "
                "act break: end on a held reaction beat with the music cue swelling, "
                "then a fade-out.\n"
                "- Tag ends with the closing theme sting under a held final reaction.\n"
            )
        else:
            # Single-thread (<5min): one story, escalating beats. No B-story.
            # This produces a tighter episode for short-form because every scene
            # develops the one plot instead of jump-cutting between two.
            user_msg += (
                "\nPre-approved SINGLE-THREAD story outline (follow this exactly "
                "as one continuous story — no B-story, no parallel plot, all "
                "scenes serve the A-story escalation):\n"
                + json.dumps(outline, indent=2)
                + "\n\nRESERVED SCENE 1 — OPENING TITLE SEQUENCE (use verbatim as "
                "scene 1's prompt, this is non-negotiable):\n"
                + opener_brief
                + "\n\nDistribute the remaining scenes (2 through N) as follows:\n"
                "- Scene 2: cold open\n"
                "- Scenes 3 through N-1: A-story beats IN ORDER, dividing scenes "
                "proportionally so each beat gets its due weight. Later beats "
                "(escalation, turn, climax) can get more scenes than the setup.\n"
                "- Last scene: tag\n"
                "- Each scene prompt (except scene 1) should label its role at "
                "the start (e.g. 'COLD OPEN:', 'BEAT 2 (rising action):', "
                "'BEAT 4 (climax):', 'TAG:').\n"
                "- Supporting characters (Kramer / Dwight / Stewie / etc.) can "
                "appear in individual beats as texture — reactions, sabotage, "
                "commentary — but do NOT peel them off into their own plot.\n"
                "\nTransition and pacing rules for the outline:\n"
                "- Scene 1 (title sequence) ends on the logo card hold with the theme resolving.\n"
                "- Scene 2 (cold open) opens by cutting from the logo card into the scene, "
                "and ends with the theme/music sting swelling before act one begins.\n"
                "- Every scene ends on a beat that motivates the next: a reaction, "
                "a discovery, a decision, a stakes bump. No mid-motion cuts.\n"
                "- The scene marking the story's climax should end on a held "
                "reaction beat with the music cue swelling.\n"
                "- Tag ends with the closing theme sting under a held final reaction.\n"
            )

    user_msg += (
        "\nReturn a JSON object with keys: style, characters, shot_bible "
        "(object with hero_prop, wardrobe, locations — see SHOT BIBLE rules "
        f"above), scenes (array of exactly {n} strings), notes."
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
        return _ensure_opener(_fallback(prompt, scene_durations, t0, f"http error ({provider_name}): {e}"))

    if r.status_code != 200:
        return _ensure_opener(_fallback(
            prompt, scene_durations, t0,
            f"{provider_name} {r.status_code}: {r.text[:200]}"
        ))

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
        return _ensure_opener(_fallback(prompt, scene_durations, t0, f"parse error ({provider_name}): {e}"))

    # Model sometimes wraps JSON in ```json fences even when told not to.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return _ensure_opener(_fallback(prompt, scene_durations, t0, f"no JSON in response: {raw[:200]}"))
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return _ensure_opener(_fallback(prompt, scene_durations, t0, f"invalid JSON: {e}"))

    scenes = parsed.get("scenes") or []
    if not isinstance(scenes, list) or len(scenes) != n:
        return _ensure_opener(_fallback(
            prompt, scene_durations, t0,
            f"expected {n} scenes, got {len(scenes) if isinstance(scenes, list) else type(scenes).__name__}"
        ))

    # Normalize each scene to a str and cap length so we don't blow past
    # MiniMax's prompt limit.
    scenes_norm: list[str] = []
    for s in scenes:
        if isinstance(s, dict):
            s = s.get("prompt") or s.get("text") or json.dumps(s)
        s = str(s).strip()
        # Cap per-scene at 2500 chars — leaves room for the SHOT BIBLE header
        # we prepend below without truncating the scene body. MiniMax H3
        # comfortably accepts prompts in this range.
        if len(s) > 2500:
            s = s[:2500]
        scenes_norm.append(s)

    # Safety net: for long-form (outline) renders, scene 1 MUST be the opening
    # title sequence. We asked the LLM to use the opener verbatim but LLMs
    # sometimes paraphrase or blend it into the cold open. If scene 1 doesn't
    # look like a title sequence, replace it. Only applies when outline is set,
    # so short renders (< 60s) still use their first scene as intended.
    if outline and scenes_norm:
        first = scenes_norm[0].upper()
        looks_like_opener = (
            "OPENING TITLE SEQUENCE" in first
            or "TITLE SEQUENCE" in first
            or "LOGO CARD" in first
            or "THEME SONG" in first
        )
        if not looks_like_opener:
            scenes_norm[0] = _franchise_opener(prompt)

    # Shot bible extraction + injection.
    #
    # The LLM produced a shot_bible object with hero_prop / wardrobe /
    # locations. It's supposed to reference these inline in every scene, but
    # even a good LLM drifts ("the shoes" instead of the full description).
    # We enforce the locks by prepending a compact SHOT BIBLE header to every
    # scene prompt — the video model sees the exact same lock text every
    # single render call, so props/wardrobe/locations stop shape-shifting
    # between scenes.
    shot_bible = parsed.get("shot_bible") or {}
    if not isinstance(shot_bible, dict):
        shot_bible = {}
    # If the LLM omitted the field entirely but we have franchise location
    # cards, seed the bible with just the location cards actually referenced
    # in the scene prompts. Including all four Seinfeld locations when only
    # Monk's + Jerry's Apartment appear in the story would blow the 1500-char
    # per-scene cap once the bible header is prepended.
    if not shot_bible.get("locations") and loc_cards:
        joined_scenes = " ".join(scenes_norm).lower()
        used = {
            name: spec
            for name, spec in loc_cards.items()
            if name.lower().split("'")[0].strip() in joined_scenes
            or any(tok in joined_scenes for tok in name.lower().split())
        }
        if used:
            shot_bible["locations"] = used

    bible_block = _format_shot_bible(shot_bible)
    if bible_block:
        # Scene 1 is the theme opener — pure title sequence, no locks needed.
        # Every other scene gets the bible header prepended.
        start_idx = 1 if (outline and scenes_norm) else 0
        for i in range(start_idx, len(scenes_norm)):
            scenes_norm[i] = bible_block + scenes_norm[i]
            # Hard cap at 3500 chars post-injection so a runaway bible +
            # scene body doesn't push us into rejected-prompt territory.
            if len(scenes_norm[i]) > 3500:
                scenes_norm[i] = scenes_norm[i][:3500]

    return {
        "ok": True,
        "style": str(parsed.get("style", "")).strip(),
        "characters": str(parsed.get("characters", "")).strip(),
        "shot_bible": shot_bible,
        "scenes": scenes_norm,
        "notes": str(parsed.get("notes", "")).strip(),
        "provider": provider_name,
        "latency_ms": int((time.time() - t0) * 1000),
        "error": None,
    }


def _format_shot_bible(bible: dict) -> str:
    """Render the shot bible dict into a compact header injected into every
    scene prompt.

    The header is kept terse so it doesn't blow past MiniMax's prompt limit
    when concatenated with the scene body. Empty sections are omitted.
    Returns "" if the bible has no usable content.
    """
    parts: list[str] = []

    hero = bible.get("hero_prop")
    if isinstance(hero, str) and hero.strip() and hero.strip().lower() not in ("null", "none"):
        parts.append(f"HERO PROP LOCK: {hero.strip()}")

    wardrobe = bible.get("wardrobe")
    if isinstance(wardrobe, dict) and wardrobe:
        wardrobe_bits = [
            f"{name} wears {desc.strip()}"
            for name, desc in wardrobe.items()
            if isinstance(desc, str) and desc.strip()
        ]
        if wardrobe_bits:
            parts.append("WARDROBE LOCK: " + "; ".join(wardrobe_bits) + ".")

    locations = bible.get("locations")
    if isinstance(locations, dict) and locations:
        loc_bits = [
            f"{name} — {desc.strip()}"
            for name, desc in locations.items()
            if isinstance(desc, str) and desc.strip()
        ]
        if loc_bits:
            parts.append("LOCATION LOCKS: " + " | ".join(loc_bits))

    if not parts:
        return ""
    return "[SHOT BIBLE — keep these EXACT across every scene] " + " ".join(parts) + " [END SHOT BIBLE]\n\n"


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

# The B-story dividing line. Under this many seconds we produce a
# single-thread outline (setup / rising action / turn / climax / resolution);
# at or above this we produce a full A-story + B-story sitcom outline.
#
# Why 300s: real sitcom B-stories only start earning their runtime around the
# 22-minute mark. Below ~5 minutes we don't have enough scenes to develop two
# threads without both feeling half-baked, and the extra location cuts hurt
# character consistency in H3. Subplots emerge naturally as A-story TEXTURE
# (other characters reacting, sabotaging, commenting) without needing their
# own thread.
DUAL_THREAD_THRESHOLD_S = 300


OUTLINE_SYSTEM_PROMPT_SINGLE = """You are a TV writer's-room outliner for SHORT-FORM video.

Given a user's episode idea, produce a tight single-thread story outline
suitable for a 1-5 minute video. Real short-form (Robot Chicken, SNL sketches,
Rick and Morty cold opens, animated shorts) is single-thread — one story with
clear beats, no B-story. Supporting characters add texture within the A-story,
not a parallel plot.

Return a JSON object with this exact shape:
{
  "logline": "one-sentence pitch of the episode",
  "cold_open": {
    "beat": "one-sentence description of the cold open (before titles)",
    "characters": ["Char1", "Char2"]
  },
  "a_story": {
    "title": "3-5 word episode title",
    "premise": "one-sentence premise",
    "beats": [
      "Beat 1: setup / inciting incident",
      "Beat 2: rising action / first complication",
      "Beat 3: escalation / higher stakes",
      "Beat 4: turn / reversal",
      "Beat 5: climax / resolution"
    ],
    "characters": ["Char1", "Char2", "Char3"]
  },
  "tag": {
    "beat": "one-sentence post-credits button (callback to cold open or a supporting character reacting)",
    "characters": ["Char1"]
  },
  "notes": "any writer-room notes: real-figure substitutions, scope trimmed for length, etc."
}

Rules:
- SINGLE THREAD ONLY. No B-story. Do not invent a parallel plot with different
  characters. Supporting characters (Kramer, Dwight, Stewie, etc.) can play a
  significant role in one or two beats of the A-story, but they should be
  weaving into the A-story, not running their own separate plot.
- Escalation is the load-bearing structure. Each beat should raise the stakes,
  reverse expectations, or deepen the character's problem.
- If the user's prompt is a franchise show (Seinfeld, The Office, Family
  Guy, South Park, etc.), cast the outline from that show's canonical
  characters. Use the group whose absence would make the episode NOT feel
  like an episode of that show.
- Do not invent characters that don't exist in the source show. For
  original prompts, invent whatever characters serve the story and
  describe them in the character list.
- If the user names a real public figure, replace with a fictional analog
  in the outline and mention this in `notes`.
- Cold open should be a small self-contained moment that hints at the
  episode's theme without giving away the A-story.
- Tag should be a short post-credits button.

Output ONLY the JSON object, no prose."""


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


def plan_outline(prompt: str, duration_s: int = 0) -> dict[str, Any]:
    """Pass 1 of the two-pass long-form workflow: return a structured story
    outline for the user to approve/edit before we generate scene prompts.

    Args:
        prompt: The user's raw episode idea.
        duration_s: Total render duration in seconds. Under DUAL_THREAD_THRESHOLD_S
            (5 min) we ask for a single-thread outline (no B-story); at or above
            it we ask for the full A-story + B-story sitcom outline. Defaults to 0
            which triggers single-thread (safest for unknown durations).

    Returns:
        {
            "ok": bool,
            "outline": dict | None,     # the parsed outline JSON
            "mode": "single" | "dual",  # which shape the outline is in
            "provider": str,            # "grok" | "anthropic" | "fallback"
            "latency_ms": int,
            "error": str | None,
        }

    Never raises. On any LLM failure returns ok=False with a fallback
    outline shape so the frontend still has something to render.
    """
    t0 = time.time()
    dual_thread = duration_s >= DUAL_THREAD_THRESHOLD_S
    outline_mode = "dual" if dual_thread else "single"
    system_prompt = OUTLINE_SYSTEM_PROMPT if dual_thread else OUTLINE_SYSTEM_PROMPT_SINGLE

    def _tag_mode(fb: dict) -> dict:
        fb["mode"] = outline_mode
        if outline_mode == "single" and isinstance(fb.get("outline"), dict):
            fb["outline"].pop("b_story", None)
        return fb

    selected = _select_provider()
    if selected is None:
        return _tag_mode(_outline_fallback(prompt, t0, "no LLM key set (XAI_API_KEY or ANTHROPIC_API_KEY)"))
    provider_name, url, model, headers = selected

    hint = _known_character_hint(prompt)
    user_msg = f"User episode idea:\n{prompt.strip()}\n"
    if hint:
        user_msg += f"\nCanonical reference for this franchise:\n{hint}\n"
    user_msg += f"\nTarget total duration: {duration_s}s ({outline_mode}-thread outline)."
    user_msg += "\nReturn the JSON outline."

    try:
        r = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "max_tokens": 2000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=OUTLINE_TIMEOUT,
        )
    except requests.RequestException as e:
        return _tag_mode(_outline_fallback(prompt, t0, f"http error ({provider_name}): {e}"))

    if r.status_code != 200:
        return _tag_mode(_outline_fallback(prompt, t0, f"{provider_name} {r.status_code}: {r.text[:200]}"))

    try:
        data = r.json()
        text_blocks = [
            b.get("text", "")
            for b in data.get("content", [])
            if b.get("type") == "text"
        ]
        raw = "".join(text_blocks).strip()
    except Exception as e:
        return _tag_mode(_outline_fallback(prompt, t0, f"parse error ({provider_name}): {e}"))

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return _tag_mode(_outline_fallback(prompt, t0, f"no JSON in response: {raw[:200]}"))
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return _tag_mode(_outline_fallback(prompt, t0, f"invalid JSON: {e}"))

    # Light schema normalization — never crash, just fill missing sections.
    # For single-thread we DROP the b_story entirely so the outline card and
    # downstream expander don't try to render it.
    outline = _normalize_outline(parsed, mode=outline_mode)

    return {
        "ok": True,
        "outline": outline,
        "mode": outline_mode,
        "provider": provider_name,
        "latency_ms": int((time.time() - t0) * 1000),
        "error": None,
    }


def _normalize_outline(obj: dict, mode: str = "dual") -> dict:
    """Ensure the outline has all required keys with reasonable defaults.

    For mode='single' we OMIT the b_story field entirely so the frontend
    knows to render a single-thread card and the expander won't try to
    interleave a nonexistent B-thread.
    """
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

    result = {
        "logline": _str(obj.get("logline")),
        "cold_open": _short(obj.get("cold_open")),
        "a_story": _story(obj.get("a_story"), 5),
        "tag": _short(obj.get("tag")),
        "notes": _str(obj.get("notes")),
    }
    if mode == "dual":
        result["b_story"] = _story(obj.get("b_story"), 4)
    return result


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
