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

#### Option A: OAuth 2.0 (Recommended for personal Gmail)

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → Create project
2. Enable APIs:
   - **Google Drive API**
   - **Google Sheets API**
3. Configure OAuth Consent Screen → External → add your Gmail as test user
4. Create **OAuth Client ID** (Desktop app) → Download as `credentials.json`
5. Place `credentials.json` in the project folder

#### Option B: Service Account

1. Create a Service Account → download key as `service_account.json`
2. Share your `AI Automation TikTok` Drive folder and your Google Sheet with the service account email

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
| **Atomic writes** | Videos written to `.tmp` then renamed — no corrupt files |
| **Work dir isolation** | Each riddle gets its own temp folder, auto-cleaned after render |
| **Ollama fallback** | If Ollama is down, raw sheet explanation is used for TTS |
| **edge-tts → pyttsx3 fallback** | If edge-tts fails, offline macOS voice kicks in |
| **Music auto-download** | Royalty-free CC0 music downloaded from Pixabay on first run |
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
