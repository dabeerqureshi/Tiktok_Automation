"""
24/7 Background Daemon — TikTok Riddle Video Automation.

Pipeline per vid_N in Google Drive 'AI Generated Videos':
  1. Download vid_N from Google Drive
  2. Look up riddle answer + explanation from Google Sheet (row N)
  3. Build 4-segment video:
       [AI Clip 0-10s] → [Countdown 40s + music] → [Solution 5s + TTS]
       → [Explanation 10s + Ollama + TTS]
  4. Upload org_N.mp4 to Google Drive 'Final Videos'
  5. Mark complete in SQLite — never re-process

Runs in Cloud Mode (Google Drive API) when credentials are present.
"""
import logging
import os
import re
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

import config
import db
import video_processor
from gdrive_service import GDriveService
from sheet_reader import SheetReader
import ollama_client

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "daemon.log",
            maxBytes=10 * 1024 * 1024,   # 10 MB per file
            backupCount=5,               # keeps daemon.log.1 .. .5
            encoding="utf-8",
        ),
    ]
)
logger = logging.getLogger("TikTokDaemon")

RUNNING = True


# ---------------------------------------------------------------------------
# SIGNAL HANDLING
# ---------------------------------------------------------------------------

def signal_handler(signum, frame):
    global RUNNING
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name} — shutting down gracefully...")
    RUNNING = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def extract_number(filename: str) -> int:
    """Extracts the trailing integer from a filename stem (e.g. 'vid_42.mp4' → 42)."""
    stem = Path(filename).stem
    match = re.search(r'\d+', stem)
    return int(match.group()) if match else -1


# ---------------------------------------------------------------------------
# ASSET BOOTSTRAP — download or synthesize royalty-free music if missing
# ---------------------------------------------------------------------------

# URLs may 403 (hotlink protection) or fail offline, so we fall back to
# synthesizing royalty-free audio locally with FFmpeg. Procedural synthesis
# is 100% original & copyright-free — no Pixabay account or legal risk.

_ASSET_SOURCES = [
    (
        config.COUNTDOWN_MUSIC_FILE,
        "https://cdn.pixabay.com/audio/2023/02/27/audio_d6adea2e9b.mp3",
        "countdown_music.mp3",
        "synthesize_countdown",
    ),
    (
        config.REVEAL_SOUND_FILE,
        "https://cdn.pixabay.com/audio/2022/03/15/audio_1c7b82ddee.mp3",
        "reveal_sound.mp3",
        "synthesize_reveal",
    ),
]


def _validate_audio(path: Path, min_bytes: int = 500) -> bool:
    """Returns True if `path` exists, has data, and ffprobe can read audio."""
    if not path or not path.exists() or path.stat().st_size < min_bytes:
        return False
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return res.returncode == 0 and res.stdout.strip() == "audio"
    except Exception:
        return False


def _atomic_write_bytes(path: Path, data: bytes) -> bool:
    """Writes `data` to a temp file, then renames atomically."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path.stat().st_size > 0
    except Exception as e:
        logger.warning(f"Could not write asset '{path.name}': {e}")
        return False


def _run_synth(cmd: List[str], label: str) -> bool:
    """Runs an FFmpeg synthesis command and validates the output."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.warning(
                f"Synthesis '{label}' failed (rc={result.returncode}): "
                f"{result.stderr[-200:]}"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"Synthesis '{label}' crashed: {e}")
        return False


def _synthesize_countdown(path: Path) -> bool:
    """
    Generates a catchy, tension-building rhythmic beat for the countdown.
    Layered kick drum pulse + bass pulse + hi-hat tick that speeds up toward
    the end to create anticipation. 100% original audio.
    """
    logger.info(f"Synthesizing '{path.name}' (catchy countdown beat)...")
    tmp = path.with_name(f".{path.name}.synth.tmp.mp3")
    # Build a rhythmic pattern: kick + bass on the beat, hi-hat ticks,
    # with a riser at the end for tension. Uses volume envelopes to shape
    # the beat so it pulses and accelerates.
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        "aevalsrc="
        # Kick drum: low sine with fast decay, pulsing at 1.5Hz (90 BPM)
        "0.40*sin(2*PI*60*t)*exp(-8*(t-floor(t*1.5)/1.5))"
        # Bass pulse: root note with tremolo
        "+0.25*sin(2*PI*110*t)*(0.5+0.5*sin(2*PI*3*t))"
        # Hi-hat: high-frequency noise bursts on off-beats
        "+0.12*sin(2*PI*8000*t)*pow(abs(sin(2*PI*1.5*t)),8)"
        # Snare/clap on beats 2 and 4
        "+0.18*sin(2*PI*200*t)*exp(-12*(t-floor(t*0.75)/0.75))"
        # Tension riser: rising sine that gets louder toward the end (last 3s)
        "+0.15*sin(2*PI*(220+1800*max(0,t-5)/3)*t)*max(0,(t-5)/3)"
        ":s=44100:d=8",
        "-af", (
            "highpass=f=40,"
            "lowpass=f=12000,"
            "acompressor=ratio=4:threshold=-12dB:attack=2:release=50,"
            "afade=t=in:st=0:d=0.5,"
            "afade=t=out:st=6.5:d=1.5,"
            "volume=0.9,"
            "aformat=sample_rates=44100:channel_layouts=stereo"
        ),
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(tmp),
    ]
    if _run_synth(cmd, "countdown_music"):
        if _validate_audio(tmp):
            tmp.replace(path)
            logger.info(f"  ✅ Synthesized countdown music: {path.name}")
            return True
        tmp.unlink(missing_ok=True)
    return False


def _synthesize_reveal(path: Path) -> bool:
    """
    Generates a warm bell 'ding' (880 Hz + 1760 Hz partials, exponential
    decay) — a clean answer-reveal chime. 100% original audio.
    """
    logger.info(f"Synthesizing '{path.name}' (reveal chime)...")
    tmp = path.with_name(f".{path.name}.synth.tmp.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        "aevalsrc="
        "0.30*sin(2*PI*880*t)*exp(-4*t)"
        "+0.15*sin(2*PI*1760*t)*exp(-5*t)"
        ":s=44100:d=1.6",
        "-af", (
            "afade=t=in:st=0:d=0.01,"
            "afade=t=out:st=1.0:d=0.6,"
            "aformat=sample_rates=44100:channel_layouts=stereo"
        ),
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(tmp),
    ]
    if _run_synth(cmd, "reveal_sound"):
        if _validate_audio(tmp):
            tmp.replace(path)
            logger.info(f"  ✅ Synthesized reveal sound: {path.name}")
            return True
        tmp.unlink(missing_ok=True)
    return False


_SYNTH_FUNCS = {
    "synthesize_countdown": _synthesize_countdown,
    "synthesize_reveal": _synthesize_reveal,
}


def ensure_assets():
    """
    Ensures both audio assets exist under assets/ using a 3-tier strategy:
      1. Pre-existing file (user-supplied override)       → keep
      2. Download from Pixabay (CC0)                       → use
      3. Local FFmpeg synthesis (offline, copyright-free)  → fallback
    Callers (countdown/solution segments) already degrade to silent audio if
    an asset is still missing, so a fresh setup is never blocked.
    """
    import urllib.request

    config.LOCAL_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    for dest_path, url, name, synth_key in _ASSET_SOURCES:
        # Tier 1 — already present & valid
        if _validate_audio(dest_path):
            logger.info(f"Asset already present & valid: {name}")
            continue

        # Tier 2 — attempt download
        downloaded = False
        try:
            logger.info(f"Downloading asset '{name}' from Pixabay (CC0)...")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                if len(data) > 1000 and _atomic_write_bytes(dest_path, data):
                    if _validate_audio(dest_path):
                        logger.info(f"  ✅ Downloaded asset '{name}' "
                                    f"({dest_path.stat().st_size} bytes)")
                        downloaded = True
        except Exception as e:
            logger.warning(f"Could not auto-download '{name}' ({e}).")

        if downloaded:
            continue

        # Tier 3 — synthesize offline (Pixabay hotlink-protection/offline fallback)
        synth_fn = _SYNTH_FUNCS.get(synth_key)
        if synth_fn is not None:
            if synth_fn(dest_path):
                continue
            logger.warning(
                f"Synthesis failed for '{name}'. "
                f"You can place your own audio file at: {dest_path}"
            )
        else:
            logger.warning(f"No fallback synthesizer for '{name}'.")
# ---------------------------------------------------------------------------
# MAIN PROCESSING LOOP
# ---------------------------------------------------------------------------

def process_drive_queue(
    gdrive: GDriveService,
    sheet: SheetReader,
    folder_ids: Dict[str, str],
) -> int:
    """
    Scans 'AI Generated Videos' folder, builds & uploads riddle videos.
    Returns count of newly completed renders.

    - Completed files are skipped forever (SQLite).
    - Failed files are retried with exponential backoff, then permanently
      paused after MAX_RENDER_ATTEMPTS (auto-re-enabled when the source file is
      replaced in Drive, i.e. its md5 changes).
    - Uploads are idempotent: if org_N.mp4 already lives in the final folder,
      it is reused instead of uploading a duplicate.
    """
    ai_drive_videos = gdrive.list_videos_in_folder(folder_ids["ai"])

    if not ai_drive_videos:
        logger.info(
            "Waiting for AI generated videos in Google Drive "
            "'AI Generated Videos' folder..."
        )
        return 0

    # Sort by numeric index so we process vid_1, vid_2, … in order
    ai_drive_videos = sorted(
        ai_drive_videos,
        key=lambda x: (extract_number(x["name"]), x["name"])
    )
    rendered_count = 0

    for ai_file in ai_drive_videos:
        if not RUNNING:
            break

        ai_name   = ai_file["name"]
        ai_id     = ai_file["id"]
        ai_md5    = ai_file.get("md5Checksum")

        # ---- Retry / state management ----
        record = db.get_record(ai_name)
        if record and record.get("status") == "completed":
            logger.info(f"  ⏭ '{ai_name}' already processed — skipping.")
            continue
        if record and record.get("status") == "failed":
            if not db.is_retryable(ai_name, current_md5=ai_md5):
                logger.info(
                    f"  ⏸  '{ai_name}' failed previously and is in backoff or "
                    f"exhausted (attempts {record.get('attempts')}) — waiting. "
                    f"Replace the file in Drive to force a re-render."
                )
                continue
            db.reset_failure(ai_name)   # retry window opened OR file replaced
            logger.info(
                f"  🔁 '{ai_name}' retry window opened (or file replaced) — reprocessing."
            )

        ai_num = extract_number(ai_name)
        if ai_num == -1:
            err = f"Cannot extract index number from '{ai_name}'. Expected vid_N.mp4"
            logger.warning(f"Skipping '{ai_name}': {err}")
            db.register_failure(ai_name, err, md5_checksum=ai_md5,
                                ai_drive_id=ai_id, permanent=True)
            continue

        # ---- File paths ----
        final_name      = f"org_{ai_num}.mp4"
        local_ai_path   = config.LOCAL_AI_DIR  / ai_name
        local_final_path = config.LOCAL_FINAL_DIR / final_name
        work_dir        = config.LOCAL_WORK_DIR / f"work_{ai_num}"

        logger.info("=" * 65)
        logger.info(f"Processing: {ai_name}  (index {ai_num})")

        answer = explanation = ""
        try:
            # ---- Download AI clip (reuse already-downloaded copy) ----
            if not local_ai_path.exists() or local_ai_path.stat().st_size == 0:
                logger.info(f"  Downloading '{ai_name}' from Google Drive...")
                gdrive.download_file(ai_id, local_ai_path)

                                                # ---- Validate the clip BEFORE rendering (no ffmpeg crash loops) ----
            duration, has_video = video_processor.probe_ai_clip(local_ai_path)
            if not has_video:
                err = "Invalid AI clip — ffprobe found no video stream."
                logger.error(f"  ❌ '{ai_name}': {err}")
                db.register_failure(ai_name, err, md5_checksum=ai_md5,
                                    ai_drive_id=ai_id, permanent=True)
                continue

            # Use the uploaded clip at its exact native duration — no trimming,
            # no rejection based on length. normalize_ai_clip will convert it
            # to 9:16 portrait regardless of duration, and the countdown segment
            # auto-extends if the total is below MIN_TOTAL_DURATION.
            if duration:
                logger.info(f"  AI clip duration: {duration:.1f}s (using as-is)")

            # ---- Read sheet metadata ----
            metadata = sheet.get_row(ai_num)
            if not metadata:
                err = f"No sheet row found for vid_{ai_num} (sheet row {ai_num + 1})."
                logger.error(f"  ❌ '{ai_name}': {err}")
                db.register_failure(ai_name, err, md5_checksum=ai_md5,
                                    ai_drive_id=ai_id, permanent=True)
                continue

            answer      = metadata.get("answer", "").strip()
            explanation = metadata.get("explanation", "").strip()

            if not answer:
                err = f"Sheet row {ai_num + 1} has an empty Answer (column B)."
                logger.error(f"  ❌ '{ai_name}': {err}")
                db.register_failure(ai_name, err, md5_checksum=ai_md5,
                                    ai_drive_id=ai_id, sheet_row=ai_num,
                                    permanent=True)
                continue

            logger.info(f"  Answer     : {answer}")
            logger.info(f"  Explanation: {explanation[:80]}{'...' if len(explanation) > 80 else ''}")

            # ---- Build 4-segment riddle video ----
            logger.info(f"  Building riddle video → {final_name}")
            success = video_processor.build_riddle_video(
                ai_video_path=local_ai_path,
                answer=answer,
                explanation=explanation,
                output_path=local_final_path,
                work_dir=work_dir,
            )

            if not success or not local_final_path.exists():
                logger.error(f"  Render FAILED for '{ai_name}'. Skipping upload.")
                db.register_failure(ai_name, "Render pipeline returned failure.",
                                    md5_checksum=ai_md5, ai_drive_id=ai_id,
                                    sheet_row=ai_num, answer=answer)
                continue

            file_size_mb = local_final_path.stat().st_size / (1024 * 1024)
            logger.info(
                f"  Render complete: {final_name} ({file_size_mb:.1f} MB)"
            )

            # ---- Upload to Drive (idempotent — reuse existing org_N.mp4) ----
            existing = gdrive.find_file_in_folder(folder_ids["final"], final_name)
            if existing:
                final_drive_id = existing["id"]
                logger.info(
                    f"  📦 '{final_name}' already present in Drive "
                    f"(ID {final_drive_id}) — reusing, skipped upload."
                )
            else:
                logger.info(f"  Uploading '{final_name}' to Google Drive 'Final Videos'...")
                upload_result = gdrive.upload_file(
                    local_path=local_final_path,
                    parent_folder_id=folder_ids["final"],
                    file_name=final_name,
                )
                final_drive_id = upload_result.get("id")

            # ---- Mark complete in SQLite (clears any prior failure state) ----
            db.mark_as_processed(
                ai_filename=ai_name,
                final_filename=final_name,
                sheet_row=ai_num,
                answer=answer,
                ai_drive_id=ai_id,
                final_drive_id=final_drive_id,
                md5_checksum=ai_md5,
            )

            logger.info(
                f"  ✅ SUCCESS: '{final_name}' uploaded "
                f"(Drive ID: {final_drive_id})"
            )
            rendered_count += 1

            # ---- Cleanup ----
            if config.CLEANUP_AFTER_UPLOAD:
                for f in (local_final_path, local_ai_path):
                    try:
                        if f.exists():
                            f.unlink()
                    except Exception:
                        pass
                logger.info(f"  Cleaned up local files for '{final_name}'.")

        except Exception as e:
            logger.error(
                f"  Error processing '{ai_name}': {e}", exc_info=True
            )
            db.register_failure(ai_name, f"Processing error: {e}",
                                md5_checksum=ai_md5, ai_drive_id=ai_id,
                                sheet_row=ai_num,
                                answer=answer or None)

    return rendered_count


# ---------------------------------------------------------------------------
# DAEMON ENTRY POINT
# ---------------------------------------------------------------------------

def run_daemon():
    """Main 24/7 daemon loop."""

    # --- Setup local directories ---
    for d in (
        config.LOCAL_AI_DIR,
        config.LOCAL_WORK_DIR,
        config.LOCAL_FINAL_DIR,
        config.LOCAL_ASSETS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)

    db.init_db()

    # --- Print startup banner ---
    logger.info("=" * 65)
    logger.info("TikTok Riddle Automation Daemon — Starting")
    logger.info(f"  Output resolution : {config.TARGET_WIDTH}x{config.TARGET_HEIGHT} @ {config.TARGET_FPS}fps")
    logger.info(f"  Countdown duration: {config.COUNTDOWN_DURATION}s")
    logger.info(f"  Solution duration : {config.SOLUTION_DURATION}s")
    logger.info(f"  Explanation dur.  : {config.EXPLANATION_DURATION}s")
    logger.info(f"  TTS engine        : {config.TTS_ENGINE} / voice: {config.TTS_VOICE}")
    logger.info(f"  Ollama model      : {config.OLLAMA_MODEL} @ {config.OLLAMA_HOST}")
    logger.info(f"  Poll interval     : {config.POLL_INTERVAL_SECONDS}s")
    if config.SHEET_ID:
        logger.info(f"  Google Sheet ID   : {config.SHEET_ID[:20]}...{config.SHEET_ID[-6:]}")
    else:
        logger.warning("  ⚠ SHEET_ID not set — add SHEET_ID=... to your .env file!")
    logger.info("=" * 65)

    # --- Check Ollama availability ---
    if ollama_client.is_ollama_available():
        logger.info(f"  ✅ Ollama is online ({config.OLLAMA_MODEL})")
    else:
        logger.warning(
            f"  ⚠ Ollama not reachable at {config.OLLAMA_HOST}. "
            f"Start with: ollama serve\n"
            f"  Raw sheet explanations will be used as fallback."
        )

    # --- Auto-download assets ---
    ensure_assets()

    # --- Check credentials (file-based OR .env OAuth Client ID/Secret) ---
    has_env_oauth = bool(
        config.GOOGLE_CLIENT_ID.strip()
        and config.GOOGLE_CLIENT_SECRET.strip()
    )
    has_creds = (
        config.CREDENTIALS_FILE.exists()
        or config.TOKEN_FILE.exists()
        or config.SERVICE_ACCOUNT_FILE.exists()
        or has_env_oauth
    )

    if not has_creds:
        logger.error(
            "\n" + "!" * 65 + "\n"
            "[GOOGLE CREDENTIALS NOT FOUND]\n"
            "  Either add GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET to your\n"
            "  .env file, or place 'credentials.json' (OAuth Desktop) or\n"
            "  'service_account.json' (Service Account) in the project\n"
            "  directory, then restart the daemon.\n"
            "  See README.md for step-by-step instructions.\n"
            + "!" * 65
        )
        return

    if has_env_oauth:
        logger.info(
            "Using GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from .env "
            "(browser authorization will run once, then token.json is reused)."
        )

    # --- Connect to Google Drive ---
    logger.info("Connecting to Google Drive...")
    try:
        gdrive = GDriveService()
        folder_ids = gdrive.setup_drive_structure()
        logger.info("Google Drive connection successful.")
        logger.info(f"  AI Videos folder : {folder_ids['ai']}")
        logger.info(f"  Final Videos folder: {folder_ids['final']}")

        # Create SheetReader sharing the same credentials
        sheet = SheetReader(gdrive.creds)

    except Exception as e:
        logger.error(f"Failed to connect to Google Drive: {e}", exc_info=True)
        return

    # --- Main polling loop ---
    logger.info("\nDaemon active. Polling Google Drive every "
                f"{config.POLL_INTERVAL_SECONDS}s...\n")

    while RUNNING:
        try:
            rendered = process_drive_queue(gdrive, sheet, folder_ids)
            if rendered > 0:
                total = db.get_processed_count()
                logger.info(
                    f"Batch complete: {rendered} riddle video(s) produced. "
                    f"Total all-time: {total}"
                )
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)

        # Interruptible sleep
        for _ in range(config.POLL_INTERVAL_SECONDS):
            if not RUNNING:
                break
            time.sleep(1)

    logger.info("Daemon stopped cleanly.")


if __name__ == "__main__":
    run_daemon()
