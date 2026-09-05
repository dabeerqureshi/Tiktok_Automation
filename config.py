"""
Configuration settings for the TikTok Riddle Automation Daemon.
4-segment pipeline: AI Clip → Countdown → Solution → Explanation → Final Video
All rendering done locally via FFmpeg + Ollama + TTS (no cloud rendering).
"""
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# GOOGLE DRIVE FOLDER STRUCTURE
# ---------------------------------------------------------------------------
GDRIVE_ROOT_FOLDER_NAME = os.getenv("GDRIVE_ROOT_FOLDER_NAME", "AI Automation TikTok")

# Optional: Pin the root folder ID directly to skip folder search on startup
GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "")

AI_GEN_DIR_NAME  = "AI Generated Videos"
FINAL_DIR_NAME   = "Final Videos"

# ---------------------------------------------------------------------------
# GOOGLE SHEETS — RIDDLE METADATA
# ---------------------------------------------------------------------------
# The Google Sheet ID or full URL:
#   https://docs.google.com/spreadsheets/d/16qhYVieRF96VIyumNTovMkDs9j6ZFfVt-Udr_-MoKSY/edit?gid=1298536790#gid=1298536790
_raw_sheet = os.getenv("SHEET_ID", "16qhYVieRF96VIyumNTovMkDs9j6ZFfVt-Udr_-MoKSY")

if "docs.google.com/spreadsheets/d/" in _raw_sheet:
    m_id = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', _raw_sheet)
    SHEET_ID = m_id.group(1) if m_id else _raw_sheet
    m_gid = re.search(r'[#&?]gid=([0-9]+)', _raw_sheet)
    SHEET_GID = m_gid.group(1) if m_gid else os.getenv("SHEET_GID", "")
else:
    SHEET_ID = _raw_sheet
    SHEET_GID = os.getenv("SHEET_GID", "1298536790")

# Optional tab name inside spreadsheet. If empty, auto-resolved via SHEET_GID or first tab
SHEET_NAME = os.getenv("SHEET_NAME", "")

# Column indices (0-based) for metadata columns:
# Col A (0) = full prompt/character description (informational)
# Col B (1) = Riddle Answer
# Col C (2) = Riddle Explanation
SHEET_COL_ANSWER      = int(os.getenv("SHEET_COL_ANSWER",      "1"))
SHEET_COL_EXPLANATION = int(os.getenv("SHEET_COL_EXPLANATION", "2"))

# Sheet cache TTL in seconds (avoids hammering Sheets API during high-frequency polls)
SHEET_CACHE_TTL = int(os.getenv("SHEET_CACHE_TTL", "60"))

# ---------------------------------------------------------------------------
# AUTHENTICATION FILES
# ---------------------------------------------------------------------------
CREDENTIALS_FILE    = Path(os.getenv("CREDENTIALS_FILE",    "./credentials.json"))
TOKEN_FILE          = Path(os.getenv("TOKEN_FILE",          "./token.json"))
SERVICE_ACCOUNT_FILE = Path(os.getenv("SERVICE_ACCOUNT_FILE", "./service_account.json"))

# OAuth scopes — includes Drive (upload/download) AND Sheets (read metadata)
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# ---------------------------------------------------------------------------
# LOCAL CACHE & STORAGE
# ---------------------------------------------------------------------------
LOCAL_CACHE_DIR  = Path("./.cache")
LOCAL_AI_DIR     = LOCAL_CACHE_DIR / "ai_videos"
LOCAL_WORK_DIR   = LOCAL_CACHE_DIR / "work"        # intermediate segments
LOCAL_FINAL_DIR  = LOCAL_CACHE_DIR / "final_videos"
LOCAL_ASSETS_DIR = Path("./assets")

# Delete local files after upload to keep Mac disk free
CLEANUP_AFTER_UPLOAD = os.getenv("CLEANUP_AFTER_UPLOAD", "true").lower() in ("true", "1", "yes")

# SQLite state database
DB_FILE = Path("./processed_tracker.db")

# ---------------------------------------------------------------------------
# VIDEO — RESOLUTION & ENCODING (TIKTOK 9:16)
# ---------------------------------------------------------------------------
TARGET_WIDTH  = 1080
TARGET_HEIGHT = 1920
TARGET_FPS    = 30

VIDEO_CODEC    = "libx264"
AUDIO_CODEC    = "aac"
AUDIO_BITRATE  = "192k"
VIDEO_PRESET   = "veryfast"   # fast encode; use 'medium' for smaller file size

# Supported input video extensions
VIDEO_EXTENSIONS = ('.mp4', '.mov', '.mkv', '.avi', '.webm')

# ---------------------------------------------------------------------------
# VIDEO SEGMENT DURATIONS (seconds)
# ---------------------------------------------------------------------------
COUNTDOWN_DURATION   = int(os.getenv("COUNTDOWN_DURATION",   "40"))   # timer screen
SOLUTION_DURATION    = int(os.getenv("SOLUTION_DURATION",    "5"))    # answer reveal
EXPLANATION_DURATION = int(os.getenv("EXPLANATION_DURATION", "10"))   # narration screen

# ---------------------------------------------------------------------------
# CONTENT & MONETIZATION SAFETY
# ---------------------------------------------------------------------------
# Minimum total video length (seconds). TikTok's longer-video rewards program
# requires longer-form content; enforcing >= 61s keeps every upload eligible.
# If the assembled video is shorter, the countdown segment is auto-extended.
MIN_TOTAL_DURATION = int(os.getenv("MIN_TOTAL_DURATION", "61"))

# Maximum allowed length (seconds) for the user-uploaded AI clip.
# Videos longer than this are rejected with a clear log before rendering.
AI_CLIP_MAX_DURATION = int(os.getenv("AI_CLIP_MAX_DURATION", "10"))

# Permanent "AI GENERATED" watermark label — required by TikTok for
# AI-generated content so the video stays eligible for monetization.
AI_LABEL_ENABLED = os.getenv("AI_LABEL_ENABLED", "true").lower() in ("true", "1", "yes")
AI_LABEL_TEXT = os.getenv("AI_LABEL_TEXT", "AI GENERATED")

# End-of-video call-to-action for retention signals.
ENABLE_END_CTA = os.getenv("ENABLE_END_CTA", "true").lower() in ("true", "1", "yes")
END_CTA_TEXT = os.getenv("END_CTA_TEXT", "Follow for more riddles!")

# ---------------------------------------------------------------------------
# TTS ENGINE
# ---------------------------------------------------------------------------
# Options: "edge-tts" (best — free Microsoft neural), "macos_say" (native offline), "pyttsx3"
TTS_ENGINE = os.getenv("TTS_ENGINE", "edge-tts")
TTS_VOICE  = os.getenv("TTS_VOICE",  "en-US-GuyNeural")   # edge-tts voice name

# ---------------------------------------------------------------------------
# OLLAMA — LOCAL LLM FOR EXPLANATION ENRICHMENT
# ---------------------------------------------------------------------------
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

# Max tokens for Ollama enriched explanation (keep it short for TTS)
OLLAMA_MAX_TOKENS = int(os.getenv("OLLAMA_MAX_TOKENS", "120"))

# If Ollama is unreachable, fall back to raw sheet explanation
OLLAMA_FALLBACK = os.getenv("OLLAMA_FALLBACK", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# ASSETS — MUSIC & SOUND EFFECTS
# ---------------------------------------------------------------------------
COUNTDOWN_MUSIC_FILE = LOCAL_ASSETS_DIR / "countdown_music.mp3"
REVEAL_SOUND_FILE    = LOCAL_ASSETS_DIR / "reveal_sound.mp3"

# Music volume during countdown (0.0 – 1.0)
COUNTDOWN_MUSIC_VOLUME = float(os.getenv("COUNTDOWN_MUSIC_VOLUME", "0.4"))

# ---------------------------------------------------------------------------
# FAILURE HANDLING & RETRIES
# ---------------------------------------------------------------------------
# Maximum render attempts per file before the daemon gives up and stops
# hammering the same broken file. Deleting the file's DB record (or replacing
# the source file in Drive, which changes its md5 and auto-resets) re-enables it.
MAX_RENDER_ATTEMPTS = int(os.getenv("MAX_RENDER_ATTEMPTS", "3"))

# Exponential backoff delays (seconds) between retries: ~5 min -> 30 min -> 2 h
_env_delays = os.getenv("RETRY_BACKOFF_DELAYS", "300,1800,7200")
RETRY_BACKOFF_DELAYS = [
    int(x.strip()) for x in _env_delays.split(",") if x.strip()
] or [300]

# ---------------------------------------------------------------------------
# POLLING & DAEMON BEHAVIOR
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
