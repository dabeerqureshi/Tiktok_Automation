"""
TTS Engine — Local Text-to-Speech abstraction layer.

Priority:
  1. edge-tts   — Microsoft neural voices (free, high quality).
  2. macOS say  — Native built-in macOS speech synthesizer (/usr/bin/say).
                  Zero external dependencies, instant, 100% reliable offline.
                  Default voice: 'Daniel' (British scholar) or 'Samantha' (US).
  3. pyttsx3    — Offline cross-platform fallback.

Usage:
    from tts_engine import speak_to_file
    speak_to_file("You have to crack an eggshell!", Path("out.aac"))
"""
import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger("TikTokDaemon")


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------

def speak_to_file(text: str, output_path: Path, voice: Optional[str] = None) -> bool:
    """
    Converts `text` to speech and writes the audio to `output_path`.
    Returns True on success, False on failure.
    Falls back gracefully: edge-tts -> macOS say -> pyttsx3.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not text or not text.strip():
        logger.warning("Empty text passed to TTS engine.")
        return False

    clean_text = text.strip()

    # 1. Try edge-tts if configured or available
    if config.TTS_ENGINE == "edge-tts":
        success = _edge_tts(clean_text, output_path, voice or config.TTS_VOICE)
        if success:
            return True
        logger.warning("edge-tts unavailable or failed. Trying native macOS speech synthesizer...")

    # 2. Try native macOS say
    if shutil.which("say"):
        macos_voice = "Daniel"  # Fits the charismatic 35yo scholar persona
        success = _macos_say_tts(clean_text, output_path, voice=macos_voice)
        if success:
            return True
        logger.warning("macOS say failed. Trying pyttsx3 fallback...")

    # 3. Fallback: pyttsx3
    return _pyttsx3_tts(clean_text, output_path)


# ---------------------------------------------------------------------------
# 1. edge-tts BACKEND
# ---------------------------------------------------------------------------

def _edge_tts(text: str, output_path: Path, voice: str) -> bool:
    """Synthesizes speech using edge-tts (Microsoft neural voice)."""
    try:
        import edge_tts
    except ImportError:
        return False

    try:
        asyncio.run(_edge_tts_async(text, output_path, voice))
        if output_path.exists() and output_path.stat().st_size > 0:
            # Standardize audio to 44100Hz stereo
            _standardize_audio(output_path)
            logger.info(f"TTS (edge-tts): Generated '{output_path.name}'")
            return True
        return False
    except Exception as e:
        logger.warning(f"edge-tts error: {e}")
        return False


async def _edge_tts_async(text: str, output_path: Path, voice: str):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


# ---------------------------------------------------------------------------
# 2. native macOS say BACKEND
# ---------------------------------------------------------------------------

def _macos_say_tts(text: str, output_path: Path, voice: str = "Daniel") -> bool:
    """
    Synthesizes speech using macOS native /usr/bin/say.
    Produces high quality local audio in milliseconds without internet or pip packages.
    """
    aiff_path = output_path.with_name(f".{output_path.stem}_{os.getpid()}.aiff")
    try:
        # Check if specified voice exists, otherwise use default
        cmd_voice = ["-v", voice] if voice else []
        cmd = ["say", *cmd_voice, "-o", str(aiff_path), text]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # Retry without explicit voice
            cmd = ["say", "-o", str(aiff_path), text]
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)

        if not aiff_path.exists() or aiff_path.stat().st_size == 0:
            return False

        # Convert AIFF -> 44100Hz stereo AAC / MP3
        cmd_ff = [
            "ffmpeg", "-y",
            "-i", str(aiff_path),
            "-ar", "44100",
            "-ac", "2",
            "-c:a", "aac",
            "-b:a", "192k",
            str(output_path),
        ]
        res = subprocess.run(cmd_ff, capture_output=True, text=True, timeout=30)
        return res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0

    except Exception as e:
        logger.warning(f"macOS say error: {e}")
        return False
    finally:
        if aiff_path.exists():
            try:
                aiff_path.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 3. pyttsx3 FALLBACK BACKEND
# ---------------------------------------------------------------------------

def _pyttsx3_tts(text: str, output_path: Path) -> bool:
    """Synthesizes speech using pyttsx3."""
    try:
        import pyttsx3
    except ImportError:
        logger.error("No TTS backend available (edge-tts, macOS say, pyttsx3 all unavailable).")
        return False

    aiff_path = output_path.with_name(f".{output_path.stem}_{os.getpid()}_pytt.aiff")
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 160)
        engine.setProperty("volume", 1.0)
        engine.save_to_file(text, str(aiff_path))
        engine.runAndWait()

        if not aiff_path.exists() or aiff_path.stat().st_size == 0:
            return False

        cmd_ff = [
            "ffmpeg", "-y",
            "-i", str(aiff_path),
            "-ar", "44100",
            "-ac", "2",
            "-c:a", "aac",
            "-b:a", "192k",
            str(output_path),
        ]
        res = subprocess.run(cmd_ff, capture_output=True, text=True, timeout=30)
        return res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0

    except Exception as e:
        logger.error(f"pyttsx3 error: {e}")
        return False
    finally:
        if aiff_path.exists():
            try:
                aiff_path.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _standardize_audio(audio_path: Path):
    """Ensures audio file is 44100Hz stereo AAC."""
    tmp = audio_path.with_name(f".{audio_path.stem}_std.aac")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-ar", "44100",
        "-ac", "2",
        "-c:a", "aac",
        "-b:a", "192k",
        str(tmp),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(audio_path)
    else:
        if tmp.exists():
            tmp.unlink()
