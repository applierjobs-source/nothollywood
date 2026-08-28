# Not Hollywood

Netflix-style AI video studio. Users write a prompt, pick a duration, and the backend renders it via **MiniMax H3** (image-to-video). Multi-scene clips are stitched together from the last frame of the previous scene for continuity.

- Frontend: static `index.html` + `styles.css` + `app.js` (no build step). Cinematic hero, horizontal shelves, hover-scale tiles, modal composer.
- Backend: FastAPI + uvicorn, single file (`server.py`). Password-gated `/api/generate` endpoint, background worker for multi-scene renders.

## Local dev

```bash
pip install -r requirements.txt
SITE_PASSWORD="your-password" MINIMAX_API_KEY="sk-..." python3 server.py
# → http://localhost:5001
```

## Environment variables

| Name | Required | Notes |
| --- | --- | --- |
| `SITE_PASSWORD` | yes | Sent as `X-Site-Password` header from the frontend to gate `/api/generate`. Anyone with it can spend MiniMax credits. |
| `MINIMAX_API_KEY` | yes | Raw MiniMax bearer token. |
| `PORT` | no | Injected by Railway/Render/Fly. Falls back to 5001 for local dev. |

## Deploy to Railway

1. Connect this repo to a Railway project
2. Set the two env vars above
3. Railway auto-detects Python, installs `requirements.txt`, runs `python3 server.py` (see `railway.json`)
4. Point your custom domain at the Railway-provided target

## Persistence

Rendered videos currently live on the container filesystem under `static/videos/` and `scenes/`. These are **ephemeral** — they disappear on every redeploy. For durable storage, wire up Supabase (bucket for MP4s, table for job metadata) — see `TODO.md`.

## Directory layout

```
server.py          # FastAPI app + background worker
requirements.txt   # Python deps
railway.json       # Railway build/start config
static/
  index.html       # Netflix-style shell
  styles.css       # Dark theme, shelves, modals
  app.js           # Frontend logic (jobs, composer, detail modal)
```
