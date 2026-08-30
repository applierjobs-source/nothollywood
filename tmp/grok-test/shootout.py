"""
Multi-model image-gen shootout on the 7-show reference-image gauntlet.
Tests reAPI models: gemini-3-pro-image-preview, imagen-4-0, flux-2-pro, midjourney (v8), gpt-image-2
"""
import os
# Ensure requests trusts the sandbox proxy CA before requests is imported
CA = "/etc/ssl/certs/agent-proxy-ca-2.pem"
os.environ.setdefault("REQUESTS_CA_BUNDLE", CA)
os.environ.setdefault("CURL_CA_BUNDLE", CA)

import json, time, sys, pathlib, urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
BASE = "https://reapi.ai/api/v1"
OUT = pathlib.Path(__file__).parent / "shootout_out"
OUT.mkdir(exist_ok=True)

SHOWS = [
    ("Rick and Morty", "animation"),
    ("The Simpsons", "animation"),
    ("Family Guy", "animation"),
    ("SpongeBob SquarePants", "animation"),
    ("Seinfeld", "live-action"),
    ("The Office", "live-action"),
    ("Breaking Bad", "live-action"),
]

# Restrict via CLI arg or env; default = top 4 candidates
DEFAULT_MODELS = [
    ("gemini-3-pro", "gemini-3-pro-image-preview", {"size": "16:9", "resolution": "2K"}),
    ("midjourney-v8", "midjourney", {"version": "8.1", "size": "16:9"}),
    ("imagen-4", "imagen-4-0", {"size": "16:9"}),
    ("gpt-image-2", "gpt-image-2", {"size": "16:9"}),
]
MODELS = DEFAULT_MODELS

def build_prompt(title: str, kind: str) -> str:
    if kind == "animation":
        return (
            f"A high-quality group shot of the main cast of the well-known animated TV show '{title}', "
            f"faithfully rendered in the show's original animation style. All main characters clearly visible together, "
            f"in a signature setting from the show, matching the iconic look of the show. "
            f"No captions, text overlays, watermarks, or on-screen graphics."
        )
    else:
        return (
            f"Photorealistic wide-angle group photo of the main cast of the well-known TV show '{title}' "
            f"in a signature setting from the show. All main characters clearly visible together, "
            f"natural expressions, wardrobe and hair matching the show's iconic look, "
            f"cinematic lighting matching the show's visual style. "
            f"No captions, text overlays, watermarks, or on-screen graphics."
        )

def submit(model_str: str, prompt: str, extra: dict):
    body = {"model": model_str, "prompt": prompt, **extra}
    r = requests.post(
        f"{BASE}/images/generations",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=60,
        verify=False,
    )
    if r.status_code != 200:
        return None, f"http {r.status_code}: {r.text[:200]}"
    data = r.json()
    tid = data.get("id") or data.get("task_id") or (data.get("data") or {}).get("id")
    if not tid and isinstance(data.get("data"), list) and data["data"]:
        # Some models return image directly (no polling)
        url = data["data"][0].get("url")
        if url:
            return {"immediate_url": url, "raw": data}, None
    if not tid:
        return None, f"no task id in response: {json.dumps(data)[:200]}"
    return {"task_id": tid, "raw": data}, None

def poll(task_id: str, max_seconds: int = 90):
    t0 = time.time()
    while time.time() - t0 < max_seconds:
        r = requests.get(f"{BASE}/tasks/{task_id}", timeout=30, verify=False)
        if r.status_code != 200:
            return None, f"poll http {r.status_code}: {r.text[:200]}"
        d = r.json()
        status = d.get("status") or (d.get("data") or {}).get("status")
        if status == "completed" or status == "SUCCESS" or status == "success":
            # find image URL
            payload = d
            for path in [
                lambda x: (x.get("output") or {}).get("image_urls", [None])[0],
                lambda x: (x.get("output") or {}).get("image_url"),
                lambda x: (x.get("output") or {}).get("images", [{}])[0].get("url") if (x.get("output") or {}).get("images") else None,
                lambda x: (x.get("data") or {}).get("image_urls", [None])[0] if isinstance(x.get("data"), dict) else None,
                lambda x: (x.get("data") or {}).get("image_url"),
                lambda x: (x.get("data") or {}).get("url"),
                lambda x: x.get("image_url"),
                lambda x: x.get("url"),
            ]:
                try:
                    v = path(payload)
                    if v:
                        return v, None
                except Exception:
                    pass
            return None, f"completed but no url: {json.dumps(d)[:300]}"
        if status in ("failed", "FAILED", "error", "ERROR"):
            return None, f"failed: {json.dumps(d)[:300]}"
        time.sleep(3)
    return None, "timeout"

def download(url: str, dst: pathlib.Path):
    # Use pplx SDK content.fetch_image because CDN hosts aren't on the direct-proxy allowlist
    try:
        import pplx_sdk
        pplx_sdk.content.fetch_image(url, output=str(dst))
        return True, dst.stat().st_size
    except Exception as e:
        return False, f"dl error: {e}"

def main():
    for model_id, model_str, extra in MODELS:
        model_dir = OUT / model_id
        model_dir.mkdir(exist_ok=True)
        print(f"\n{'='*60}\n== MODEL: {model_id} ({model_str})\n{'='*60}")
        for title, kind in SHOWS:
            slug = title.lower().replace(" ", "_").replace("'", "")
            out_path = model_dir / f"{slug}.jpg"
            if out_path.exists() and out_path.stat().st_size > 5000:
                print(f"  [skip] {title} (cached)")
                continue
            prompt = build_prompt(title, kind)
            print(f"  [{kind}] {title} ... ", end="", flush=True)
            t0 = time.time()
            res, err = submit(model_str, prompt, extra)
            if err:
                print(f"SUBMIT FAIL in {time.time()-t0:.1f}s: {err}")
                (model_dir / f"{slug}.ERR.txt").write_text(err)
                continue
            if "immediate_url" in res:
                url = res["immediate_url"]
            else:
                url, err = poll(res["task_id"])
                if err:
                    print(f"POLL FAIL in {time.time()-t0:.1f}s: {err}")
                    (model_dir / f"{slug}.ERR.txt").write_text(err)
                    continue
            ok, size_or_err = download(url, out_path)
            dt = time.time() - t0
            if not ok:
                print(f"DL FAIL in {dt:.1f}s: {size_or_err}")
                (model_dir / f"{slug}.ERR.txt").write_text(str(size_or_err))
                continue
            print(f"ok in {dt:.1f}s ({size_or_err} bytes)")

if __name__ == "__main__":
    main()
