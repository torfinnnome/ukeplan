# Family Week Planner

A clean weekly family planner with custom child names & school classes (e.g. Ann: 1A, Berit: 6B, Matthew: 10C) and AI-powered extraction of school week schedules (PDF/images) and activities.

![Family Week Planner dashboard](ukeplan-shot.png)

## Features

1. **Weekly Family Dashboard (Web)**:
   - 7-day responsive grid (Monday through Sunday).
   - Categorized by **🎒 Pack & Bring** (*gym kit, swimming gear, outdoor trip items*), **📅 Activities** (*trainings, matches, appointments*), **📝 Homework & Tasks**, and **📌 Notes**.
   - **"Pack for Tomorrow" header banner**: Displays what needs to be packed into backpacks for the next day.
   - Filter by family member or child (with school class indicators).
   - Manage kids and family members (add, rename, update school classes).
   - Add, edit, check off, and delete items directly.
   - **Document archive**: every uploaded plan is stored on disk, auto-tagged with the child's name and week number — browse, view, re-tag and delete at `/documents`.
   - **Standing school schedules**: one permanent timetable per child (e.g. the full school-day layout), kept separate from the weekly plan — upload, replace, view and delete at `/schedules`.

2. **Ingestion Channels**:
   - **Apple Share Sheet (Shortcut)**: Select a schedule PDF in Mail, Safari, or Files on iPhone/iPad/Mac and tap **"Share to Week Planner"**.
   - **Email Forwarding (IMAP Bot)**: Forward any school email with PDF attachments to a dedicated email inbox; background service processes it automatically.
   - **Direct Drag-and-Drop**: Upload PDF or image schedules directly via the Web UI.
   - **Natural-language command bar**: Type straight into the Web UI — e.g. `Sigrid har sjakktrening hver onsdag 18:00-19:50` — and the AI turns it into structured calendar items matched to your family members.

3. **Local AI on CPU (llama.cpp in Docker)**:
   - Runs `llama.cpp:server` inside Docker on CPU (no GPU/CUDA or Nvidia driver setup needed on the host).
   - Uses 4-bit quantized `Qwen 2.5 7B` (or `3B`), with high accuracy in parsing complex tabular schedules and dates into strict JSON.
   - **OCR stage**: set `OCR_ENABLED=true` in `.env` to transcribe PDF uploads and photo uploads through a dedicated vision model (any OpenAI-compatible endpoint, e.g. llama.cpp, vLLM/SGLang serving `zai-org/GLM-OCR`) pointed at by `OCR_BASE_URL` (default `http://localhost:8080/v1`) with `OCR_MODEL` (default `glm-ocr`). PDFs are sent whole in one call when the endpoint ingests PDFs natively (`OCR_PDF_NATIVE=true`, the z.ai API); for endpoints that only accept images (llama.cpp rejects `data:application/pdf`), set `OCR_PDF_NATIVE=false` and the pages are rendered to 150 DPI PNG (max 8) and transcribed one per page. Photos are sent as PNG. The OCR model is prompted with the canonical `OCR` trigger (GLM-OCR is prompt-limited) and produces markdown; the extraction LLM above then structures that text. When OCR is disabled or fails, PDFs fall back to their digital text layer (scanned pages then yield no text).

4. **Apple Calendar Subscription (.ics)**:
   - Provides a live subscription feed (`/api/calendar.ics`) for native iOS/macOS Calendar, syncing all activities and gear reminders onto lock screens, Apple Watch, and CarPlay.

---

## Quickstart with Docker or Podman (CPU-only)

All you need is standard Docker (or Podman) with its Compose provider installed:

```bash
# Start the full stack in the background:
docker compose up -d
```

1. **`ukeplan-llm`**: Runs `ghcr.io/ggml-org/llama.cpp:server` with `--hf-repo ${LLM_HF_REPO:-bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M}` on port **8089**.
2. **`ukeplan-app`**: Runs the web application and API on port 8000.

The application will be accessible at `http://localhost:8000` (or your host's local network IP).

### Using Podman instead of Docker

The same `docker-compose.yml` works with [Podman](https://podman.io/) unchanged — it uses only standard Compose features. Just swap the command:

```bash
podman compose up -d
```

(On older setups, the Python `podman-compose` package also works: `podman-compose up -d`.)

One note: the `host.docker.internal:host-gateway` entry in the compose file is a Docker idiom used only so the app can reach an OCR endpoint running on the host (disabled by default via `OCR_ENABLED=false`). Modern rootless Podman adds `host.docker.internal` to the container automatically, so nothing needs changing. If an older Python `podman-compose` errors on that line, delete the `extra_hosts` entry (the hostname still resolves) or point `OCR_BASE_URL` at `http://host.containers.internal:8080/v1` instead.

### Tuning the AI timeout

Every AI call (document parsing and natural-language commands) has a read timeout of **300 seconds**, configurable with `AI_TIMEOUT_SECONDS` (env or `.env`):

```ini
AI_TIMEOUT_SECONDS=600
```

This is the budget for waiting on the AI endpoint's response — it matters most when you point `AI_BASE_URL` at a remote or shared endpoint you do not control, where a long week-plan document can queue behind other traffic. Raise it if you see `504` responses or `ReadTimeout` in the app logs; the connect timeout stays at 10 seconds so an unreachable endpoint still fails fast.

A timeout is reported as `504 Gateway Timeout` in the web UI and in the ingest log, and the document is kept under **Documents** so you can retry it. Note the endpoint may still be working on the request after the client gives up — raising the timeout is usually better than retrying, which doubles the load.

### Pointing at a gateway (e.g. OmniRoute)

If `AI_BASE_URL` is a gateway such as [OmniRoute](https://github.com/animeetb/omniroute) that fronts a remote reasoning model (e.g. `gpt-6.1-terra`), set:

```ini
AI_REASONING_EFFORT=none
```

Some gateways inject a default `reasoning_effort` for known reasoning models, and upstream APIs can reject the resulting request with `Function tools with reasoning_effort are not supported ...` — setting `reasoning_effort` explicitly in the app's request overrides the gateway default and clears the error. `none` also makes reasoning models answer faster, which suits extraction well. Leave the variable unset for plain OpenAI-compatible servers (llama.cpp, vLLM, ...) that ignore or reject the field.

### Direct document mode (skip OCR / text extraction)

By default, uploads go through a text stage first — OCR (when enabled) or the PDF's digital text layer — and only the resulting text is sent to the extraction LLM. Some models read documents natively (e.g. `gpt-6.1-terra` via OmniRoute accepts PDF and photo uploads in one call). To send the uploaded PDF or photo straight to the LLM and skip the text stage entirely, set:

```ini
AI_DIRECT_DOCUMENT=true
```

- PDFs are sent whole as a base64 `file` part (modern OpenAI schema) when `AI_DIRECT_PDF_NATIVE=true` (the default). For vision endpoints that only accept images (e.g. llama.cpp, which rejects `application/pdf`), set `AI_DIRECT_PDF_NATIVE=false` and the PDF's pages are rendered to 150 DPI PNGs (max 8) and sent as image parts instead.
- Photos are re-encoded to PNG and sent as one image part.
- When direct mode is on, the OCR stage is not used at all for PDFs/photos, and text-file uploads still go through the normal text path.
- Ingestion still fails (with the file kept under **Documents** for retry) if the endpoint rejects the direct request — e.g. a non-vision model behind `AI_BASE_URL`.

### Running under a sub-path (reverse proxy)

To serve the app at `https://my-domain.com/ukeplan/` instead of the domain root, set the public prefix with `APP_ROOT_PATH` (env or `.env`):

```ini
APP_ROOT_PATH=/ukeplan
```

`APP_ROOT_PATH` is the **public** path the proxy exposes (leading slash, no trailing slash; `ukeplan`, `/ukeplan` and `/ukeplan/` are all accepted and normalized). Leave it unset/empty to serve at the domain root (the default).

With `APP_ROOT_PATH=/ukeplan` every page link, `fetch` API call, the `/api/calendar.ics` subscription URL, the shortcut-download URL and the Apple Shortcut ingest URL are all generated under `/ukeplan/...`, so nothing hits the domain root.

The proxy must forward requests for `/ukeplan/` to the app on port 8000. The app tolerates both common proxy styles — a **stripped** prefix (`/ukeplan/api/items` → app sees `/api/items`) and a **preserved** prefix (app sees `/ukeplan/api/items`) — so just make sure `APP_ROOT_PATH` matches the public prefix. Examples:

Caddy (strips the prefix with `handle_path`):

```caddy
my-domain.com {
    handle_path /ukeplan/* {
        reverse_proxy 127.0.0.1:8000
    }
}
```

nginx — preserve the prefix (no trailing slash on `proxy_pass`) and set the header so `X-Forwarded-*` build correct absolute URLs:

```nginx
location /ukeplan/ {
    proxy_pass http://127.0.0.1:8000;   # no trailing slash => prefix preserved
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
}
```

(For nginx the stripped form `proxy_pass http://127.0.0.1:8000/;` also works, since the app re-applies `APP_ROOT_PATH` when building URLs regardless of which style the proxy uses.)

With docker compose, pass it through: `APP_ROOT_PATH=/ukeplan` in `.env` (the compose file forwards `APP_ROOT_PATH`).

---

## Apple Devices Setup

Open `http://<your-host-ip>:8000/setup/apple-shortcut` in Safari on your iPhone or Mac:
- **Shortcut**: One-tap download of a ready-made, signed "Share to Week Planner" iOS Share Sheet shortcut (imported in Files, no Mac required). Manual step-by-step instructions are provided as a fallback if the download is unavailable.
- **Calendar**: One-click subscription URL for Apple Calendar.

---

## Optional: Email Forwarding Configuration

To enable the IMAP forwarding bot, set the following environment variables (or create a `.env` file):

```ini
IMAP_ENABLED=true
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=your-family-planner@gmail.com
IMAP_PASSWORD=your-app-password
IMAP_POLL_INTERVAL_SECONDS=120
```

---

## Data Retention

The app automatically removes old data so the database and upload folder do not grow forever. A background task (at startup and then on an interval) deletes:

- **Calendar items** whose date is older than the retention window. Recurring-weekly items are standing entries and are never auto-removed.
- **Uploaded documents** (PDFs/photos) created before the retention window, including their file on disk.

Configure via environment variables (or `.env`):

```ini
ITEM_RETENTION_WEEKS=12
ITEM_PURGE_INTERVAL_SECONDS=86400
ITEM_AUTOPURGE=true
```

Set `ITEM_AUTOPURGE=false` to keep everything.
