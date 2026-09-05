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
# ASSET BOOTSTRAP — download royalty-free music if missing
# ---------------------------------------------------------------------------

def ensure_assets():
    """
    Downloads royalty-free countdown music + reveal sound from Pixabay
    if not already present in the assets/ directory.
    """
    config.LOCAL_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    assets_to_fetch = [
        (
            config.COUNTDOWN_MUSIC_FILE,
            # Thinking music — soft ambient, CC0 from Pixabay
            "https://cdn.pixabay.com/audio/2023/02/27/audio_d6adea2e9b.mp3",
            "countdown_music.mp3",
        ),
        (
            config.REVEAL_SOUND_FILE,
            # Short reveal chime — CC0 from Pixabay
            "https://cdn.pixabay.com/audio/2022/03/15/audio_1c7b82ddee.mp3",
            "reveal_sound.mp3",
        ),
    ]

    import urllib.request

    for dest_path, url, name in assets_to_fetch:
        if dest_path.exists() and dest_path.stat().st_size > 0:
            logger.info(f"Asset already present: {name}")
            continue
        try:
            logger.info(f"Downloading asset '{name}' from Pixabay (CC0)...")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                if len(data) > 1000:
                    with open(dest_path, "wb") as f:
                        f.write(data)
                    logger.info(f"Asset saved: {dest_path} ({len(data)} bytes)")
        except Exception as e:
            logger.warning(
                f"Could not auto-download '{name}' ({e}). "
                f"You can place your own audio file at: {dest_path}"
            )


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
            if duration and duration > config.AI_CLIP_MAX_DURATION:
                err = (f"AI clip is {duration:.1f}s, exceeding AI_CLIP_MAX_DURATION="
                       f"{config.AI_CLIP_MAX_DURATION}s. Please upload a 0-10s clip.")
                logger.error(f"  ❌ '{ai_name}': {err}")
                db.register_failure(ai_name, err, md5_checksum=ai_md5,
                                    ai_drive_id=ai_id, permanent=True)
                continue

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

    # --- Check credentials ---
    has_creds = (
        config.CREDENTIALS_FILE.exists()
        or config.TOKEN_FILE.exists()
        or config.SERVICE_ACCOUNT_FILE.exists()
    )

    if not has_creds:
        logger.error(
            "\n" + "!" * 65 + "\n"
            "[GOOGLE CREDENTIALS NOT FOUND]\n"
            "  Place 'credentials.json' (OAuth) or 'service_account.json'\n"
            "  in the project directory and restart the daemon.\n"
            "  See README.md for setup instructions.\n"
            + "!" * 65
        )
        return

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
