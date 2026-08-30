import os, json, base64, requests, time
from pathlib import Path

TESTS = [
    ("Rick and Morty", "animation"),
    ("The Simpsons", "animation"),
    ("Family Guy", "animation"),
    ("Seinfeld", "live-action"),
    ("The Office", "live-action"),
]

TEMPLATE = (
    "Photorealistic wide-angle group photo of the main cast of the well-known "
    "TV show '{title}' in a signature setting from the show. All main "
    "characters visible together, natural expressions, wardrobe and hair "
    "matching the show's iconic look, cinematic lighting matching the show's "
    "visual style. Do not include captions, text overlays, watermarks, or "
    "on-screen graphics."
)

out = Path("out")
out.mkdir(exist_ok=True)

for title, kind in TESTS:
    print(f"\n[{kind}] {title}")
    t0 = time.time()
    body = {
        "model": "grok-imagine-image-2.0",
        "prompt": TEMPLATE.format(title=title),
        "n": 1,
    }
    try:
        # Auth injected by custom-cred proxy on api.x.ai
        r = requests.post(
            "https://api.x.ai/v1/images/generations",
            headers={"Content-Type": "application/json"},
            data=json.dumps(body),
            timeout=120,
            verify="/etc/ssl/certs/agent-proxy-ca-2.pem",
        )
        dt = time.time() - t0
        if r.status_code != 200:
            print(f"  http {r.status_code} in {dt:.1f}s: {r.text[:300]}")
            continue
        data = r.json()
        item = (data.get("data") or [{}])[0]
        b64 = item.get("b64_json")
        url = item.get("url")
        if b64:
            raw = base64.b64decode(b64)
        elif url:
            print(f"  url: {url[:120]}")
            raw = requests.get(url, timeout=60, verify="/etc/ssl/certs/agent-proxy-ca-2.pem").content
        else:
            print(f"  no image data: {json.dumps(data)[:200]}")
            continue
        slug = title.lower().replace(" ", "_").replace("'","")
        ext = "jpg"
        if raw[:8] == b"\x89PNG\r\n\x1a\n": ext = "png"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP": ext = "webp"
        p = out / f"{slug}.{ext}"
        p.write_bytes(raw)
        print(f"  ok in {dt:.1f}s -> {p} ({len(raw)//1024} KB)")
    except Exception as e:
        print(f"  exception: {e}")
