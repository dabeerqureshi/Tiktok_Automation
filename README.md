# TikTok Riddle Automation — Local Production Pipeline

A 24/7 Python automation daemon that turns your AI-generated riddle clips into fully produced TikTok videos — **entirely locally** using FFmpeg, Ollama, and edge-tts.

---

## 🎬 How It Works

You upload an AI-generated riddle clip (`vid_1.mp4`, `vid_2.mp4`, …) to Google Drive.  
The daemon automatically:

1. **Downloads** the clip
2. **Reads** the answer + explanation from your Google Sheet (row N → `vid_N`)
3. **Builds** a 4-segment final video:

```
┌────────────────────────────────────────────────────┐
│  Segment 1 — AI Clip  (0-10s)                      │
│  Your AI-generated character asks the riddle        │
├────────────────────────────────────────────────────┤
│  Segment 2 — Countdown Timer  (40s)                │
│  40 → 0 countdown + light background music         │
│  "Think about it…"                                  │
├────────────────────────────────────────────────────┤
│  Segment 3 — Solution Screen  (~5s)                │
│  "💡 The Answer: An egg."  + TTS voice             │
├────────────────────────────────────────────────────┤
│  Segment 4 — Explanation Screen  (~10s)            │
│  Ollama enriches explanation → TTS narrates it      │
│  "🧠 Here's Why: You have to crack…"               │
└────────────────────────────────────────────────────┘
```

4. **Uploads** `org_N.mp4` to Google Drive → `Final Videos/`

---

## 📁 Google Drive Folder Structure

```
Google Drive/
└── AI Automation TikTok/
    ├── AI Generated Videos/    ← Upload your AI clips here (vid_1.mp4, vid_2.mp4, …)
    └── Final Videos/           ← Finished riddle videos appear here (org_1.mp4, org_2.mp4, …)
```

Your **Google Sheet** (anywhere in Drive, referenced by `SHEET_ID`) provides the metadata:

| Column A | Column B | Column C |
|----------|----------|----------|
| AI generation prompt (informational) | **Answer** | **Explanation** |

Row 1 = header.  Row N+1 = metadata for `vid_N`.

---

## ⚙️ Local Stack (100% Free, No Cloud AI Costs)

| Tool | Purpose |
|------|---------|
| **FFmpeg** | All video rendering — normalization, countdown, text overlays, concat |
| **Ollama** (`llama3.2:3b`) | Rewrites raw explanations into polished TTS narration |
| **edge-tts** | Microsoft neural TTS voice (free, no API key) |
| **Google Drive API** | Download AI clips, upload final videos |
| **Google Sheets API** | Read riddle metadata per row |

---

## 🔑 One-Time Setup

### Step 1 — Prerequisites

```bash
# Install FFmpeg
brew install ffmpeg

# Install Ollama (if not already installed)
brew install ollama
ollama pull llama3.2:3b
```

### Step 2 — Google API Credentials

You need one set of credentials for **Drive + Sheets** access.

#### Option A: OAuth 2.0 Desktop (Recommended — best for a personal Gmail on this Mac)

Two equivalent ways to provide the credentials — **pick one**:

**A1 — Client ID + Secret in `.env` (no file needed):** after creating the OAuth app (steps 1–4 below), just paste your Client ID and Client Secret into `.env`:

```
GOOGLE_CLIENT_ID="xxxx.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="GOCSPX-..."
```

**A2 — `credentials.json` in the project root:** if you already downloaded the JSON from Google Console, drop it here with that name.

Either way, the **first** `./run.sh` opens a browser tab once; after you click **Allow**, the token is saved to `token.json` and reused forever (no re-auth needed).

1. Go to Google Cloud Console → [console.cloud.google.com](https://console.cloud.google.com/) → **Create project** (or select an existing one)
2. **Enable APIs** — search & enable:
   - **Google Drive API**
   - **Google Sheets API**
3. Configure **OAuth consent screen** → *External* → fill app name + your email → **Add your Gmail as a test user**
4. Create credentials: **Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Click **Create**
5. **Download** the JSON → place it in the project root as **`credentials.json`**
6. Run `./run.sh` — a **browser opens** → sign in with your Gmail → click **Allow**
   - Done! `token.json` is created and all future runs skip the browser.

#### Option B: Service Account (best for fully headless 24/7)

This saves as `service_account.json` — no browser flow, runs unattended forever.

1. Google Cloud Console → IAM & Admin → **Service Accounts** → Create Service Account
2. Create a key → download as **`service_account.json`** → place in project root
3. **Share** your `AI Automation TikTok` Drive folder **and** your Google Sheet with the service-account email (viewer is enough for downloads/uploads)

### Step 3 — Configure `.env`

```bash
cp .env.example .env
# Edit .env and set SHEET_ID to your Google Sheet's ID
```

Get your Sheet ID from its URL:
```
https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID_HERE/edit
```

### Step 4 — Run

```bash
chmod +x run.sh
./run.sh
```

The first time you run it, a browser window will open for Google OAuth. Authorize once — the token is saved and reused forever.

---

## 🚀 Daemon Commands

```bash
# Foreground (interactive, Ctrl+C to stop):
./run.sh

# Background (24/7 service):
./run.sh background

# Check status:
./run.sh status

# Live logs:
./run.sh logs

# Stop background daemon:
./run.sh stop

# Restart:
./run.sh restart
```

---

## 🛡️ Reliability Features

| Feature | Detail |
|---------|--------|
| **SQLite tracking** | Prevents any `vid_N` from being processed twice |
| **Retry state machine** | Failed renders use exponential backoff (5 min → 30 min → 2 h) and auto-pause after 3 attempts — no infinite re-render loops |
| **Source-change recovery** | Replacing a `vid_N.mp4` in Drive changes its md5 and automatically resets its failure state |
| **Idempotent uploads** | `org_N.mp4` is only uploaded once — if it already exists in Drive, it's reused instead of duplicated |
| **Input validation** | Uploaded clips are ffprobe-checked (must have a video stream, ≤ 10s) before rendering |
| **Monetization guard** | Assembled videos are force-extended to `MIN_TOTAL_DURATION` (61s) via the countdown segment |
| **AI-content labeling** | Permanent "AI GENERATED" watermark on the AI clip + cards (TikTok requirement for AI monetization) |
| **Atomic writes** | Videos written to `.tmp` then renamed — no corrupt files |
| **Work dir isolation** | Each riddle gets its own temp folder, auto-cleaned after render |
| **Ollama fallback** | If Ollama is down, raw sheet explanation is used for TTS |
| **edge-tts → macOS say → pyttsx3** | Triple TTS fallback chain, never silently silent |
| **Music auto-provisioning** | Countdown music + reveal chime are auto-downloaded from Pixabay (CC0); on 403/offline, **FFmpeg synthesizes original royalty-free audio locally** — never blocks a fresh setup |
| **Log rotation** | `daemon.log` rotates at 10 MB × 5 backups — disk never fills |
| **Concat fallback** | Lossless `-c copy` concat retries with a full re-encode if stream params ever mismatch |
| **Graceful shutdown** | SIGINT/SIGTERM handled cleanly without leaving dangling processes |

---

## 🎛️ Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `SHEET_ID` | *(required)* | Google Sheet ID from the URL |
| `SHEET_NAME` | `Sheet1` | Tab name inside the spreadsheet |
| `COUNTDOWN_DURATION` | `40` | Seconds for the timer screen |
| `SOLUTION_DURATION` | `5` | Minimum seconds for answer reveal |
| `EXPLANATION_DURATION` | `10` | Minimum seconds for explanation |
| `MIN_TOTAL_DURATION` | `61` | Enforced minimum video length; countdown auto-extends if shorter (TikTok monetization eligibility) |
| `AI_CLIP_MAX_DURATION` | `10` | Rejects uploaded AI clips longer than this before rendering |
| `AI_LABEL_ENABLED` | `true` | Draws the permanent "AI GENERATED" watermark (required for AI monetization) |
| `AI_LABEL_TEXT` | `AI GENERATED` | Watermark text |
| `ENABLE_END_CTA` | `true` | Shows "Follow for more riddles!" on the last screen |
| `END_CTA_TEXT` | `Follow for more riddles!` | End-card call-to-action text |
| `MAX_RENDER_ATTEMPTS` | `3` | Retry attempts before a broken file is paused |
| `RETRY_BACKOFF_DELAYS` | `300,1800,7200` | Backoff seconds between retries (5 min → 30 min → 2 h) |
| `TTS_ENGINE` | `edge-tts` | `edge-tts` or `pyttsx3` |
| `TTS_VOICE` | `en-US-GuyNeural` | edge-tts voice name |
| `OLLAMA_MODEL` | `llama3.2:3b` | Ollama model to use |
| `COUNTDOWN_MUSIC_VOLUME` | `0.4` | 0.0–1.0 volume of background music |
| `POLL_INTERVAL_SECONDS` | `30` | How often to scan Google Drive |
| `CLEANUP_AFTER_UPLOAD` | `true` | Delete local files after upload |

---

## 📐 Video Specs

- **Resolution**: 1080×1920 (TikTok 9:16)
- **FPS**: 30
- **Codec**: H.264 (libx264) / AAC audio
- **Normalization**: Blurred background + crisp centered foreground (landscape → portrait)
