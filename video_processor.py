"""
Video Processing Engine — 4-segment riddle video builder.

Pipeline for each vid_N:
  Segment 1: AI clip (vid_N)    — normalized to 9:16 (1080x1920, 30fps)
  Segment 2: Countdown timer    — 40s (Pillow visual sequence + background music)
  Segment 3: Solution screen    — Pillow card + TTS voice + reveal sound
  Segment 4: Explanation screen — Pillow card + Ollama enrichment + TTS voice

All segments are normalized to strictly identical stream parameters:
  - Video: 1080x1920, 30 fps, yuv420p, libx264
  - Audio: AAC, 44100 Hz, stereo, 192 kbps
Then concatenated losslessly using FFmpeg's concat demuxer (-c copy).

Uses Pillow for high quality typography and graphics, avoiding FFmpeg
drawtext / freetype compilation dependencies.
"""
import json
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

import config
import tts_engine
import ollama_client

logger = logging.getLogger("TikTokDaemon")

W = config.TARGET_WIDTH     # 1080
H = config.TARGET_HEIGHT    # 1920
FPS = config.TARGET_FPS     # 30


# ===========================================================================
# FONT HELPER
# ===========================================================================

def _get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Finds and loads a clean system TrueType font, or falls back to default."""
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Trebuchet MS.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ===========================================================================
# AI-GENERATED CONTENT LABEL
# ===========================================================================

def _make_ai_label_png(path: Path) -> Path:
    """
    Renders the permanent 'AI GENERATED' watermark as an RGBA PNG (transparent).
    Overlaid on the AI clip segment so the finished video is clearly labeled —
    required by TikTok monetization rules for AI-generated content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (300, 74), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _get_font(30)
    text = config.AI_LABEL_TEXT or "AI GENERATED"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    pad_x, pad_y = 16, 10
    box = [0, 0, text_w + pad_x * 2, text_h + pad_y * 2]
    draw.rounded_rectangle(box, radius=14, fill=(0, 0, 0, 150))
    draw.text((pad_x, pad_y), text, font=font, fill=(255, 255, 255, 230))
    img.save(str(path), "PNG")
    return path


def _draw_ai_label(draw: ImageDraw.ImageDraw):
    """Draws the small 'AI GENERATED' tag in the top-right corner of a card."""
    if not config.AI_LABEL_ENABLED:
        return
    font = _get_font(28)
    text = config.AI_LABEL_TEXT or "AI GENERATED"
    # Shadow for contrast on any background
    draw.text((W - 41, 41), text, font=font, fill=(0, 0, 0), anchor="ra")
    draw.text((W - 40, 40), text, font=font, fill=(235, 235, 235), anchor="ra")


def _draw_end_cta(draw: ImageDraw.ImageDraw):
    """Draws the 'Follow for more riddles!' call-to-action at the bottom."""
    if not config.ENABLE_END_CTA:
        return
    font = _get_font(48)
    text = config.END_CTA_TEXT or "Follow for more riddles!"
    draw.text((W // 2, H - 170), text, fill=(255, 215, 0), font=font, anchor="mm")


# ===========================================================================
# 1. AI CLIP NORMALIZER
# ===========================================================================

def normalize_ai_clip(
    input_path: Path,
    output_path: Path,
    label_path: Optional[Path] = None,
) -> bool:
    """
    Converts any input video to 1080x1920 (9:16) portrait.
    Background: scaled to fill + blurred (boxblur).
    Foreground: scaled to fit + centered sharp.
    Audio: guaranteed 44100Hz stereo AAC (adds silence if input has no audio).
    If `label_path` is provided, the RGBA watermark is overlaid top-right.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(output_path)

    has_audio = _has_audio_stream(input_path)
    clip_label = label_path if label_path and label_path.exists() else None

    if has_audio:
        if clip_label:
            filter_complex = (
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=20:5,setsar=1[bg];"
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
                f"[base][1:v]overlay=36:36[vout];"
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-i", str(clip_label),
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "[aout]",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                str(tmp),
            ]
        else:
            filter_complex = (
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=20:5,setsar=1[bg];"
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[vout];"
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "[aout]",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                str(tmp),
            ]
    else:
        # Generate silent audio track
        if clip_label:
            filter_complex = (
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=20:5,setsar=1[bg];"
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
                f"[base][1:v]overlay=36:36[vout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-i", str(clip_label),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "2:a",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                "-shortest",
                str(tmp),
            ]
        else:
            filter_complex = (
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=20:5,setsar=1[bg];"
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "1:a",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                "-shortest",
                str(tmp),
            ]

    if _run(cmd, "normalize_ai_clip"):
        tmp.replace(output_path)
        return True
    return False


# ===========================================================================
# 2. COUNTDOWN TIMER SEGMENT
# ===========================================================================

def build_countdown_segment(output_path: Path, duration: Optional[int] = None) -> bool:
    """
    Generates a countdown screen (e.g. 40 -> 0) with:
    - Sleek dark academia / TikTok card aesthetic
    - Large animated countdown number
    - "Think about it..." subtitle
    - Looping background music (or silence)
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(output_path)
    dur = duration or config.COUNTDOWN_DURATION

    frames_dir = output_path.parent / f".frames_{os.getpid()}"
    frames_dir.mkdir(parents=True, exist_ok=True)

    font_huge = _get_font(220)
    font_badge = _get_font(44)
    font_sub = _get_font(52)
    font_hint = _get_font(36)

    try:
        # Generate one PNG per second
        for sec in range(dur):
            val = dur - sec
            img = Image.new("RGB", (W, H), color=(12, 15, 32))
            draw = ImageDraw.Draw(img)

            # Outer subtle border card
            draw.rounded_rectangle([50, 160, W - 50, H - 160], radius=40, outline=(40, 50, 90), width=3, fill=(16, 20, 42))

            # Top Badge: TIME TO THINK
            draw.rounded_rectangle([320, 260, W - 320, 360], radius=25, fill=(255, 215, 0))
            draw.text((W // 2, 310), "TIME TO THINK", fill=(12, 15, 32), font=font_badge, anchor="mm")

            # AI-generated content watermark (top-right)
            _draw_ai_label(draw)

            # Center circle timer container
            cx, cy, radius = W // 2, H // 2 - 40, 260
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=(255, 215, 0), width=6, fill=(22, 28, 58))
            draw.ellipse([cx - radius + 15, cy - radius + 15, cx + radius - 15, cy + radius - 15], outline=(60, 75, 130), width=2)

            # Countdown number
            draw.text((cx, cy), str(val), fill=(255, 255, 255), font=font_huge, anchor="mm")

            # Subtitles
            draw.text((W // 2, cy + radius + 100), "Can you figure it out?", fill=(255, 255, 255), font=font_sub, anchor="mm")
            draw.text((W // 2, cy + radius + 180), "Solution coming up...", fill=(180, 190, 220), font=font_hint, anchor="mm")

            frame_path = frames_dir / f"frame_{sec:04d}.png"
            img.save(str(frame_path), "PNG")

        # Encode frames into video with audio
        music_path = config.COUNTDOWN_MUSIC_FILE
        has_music = music_path.exists() and music_path.stat().st_size > 0

        if has_music:
            filter_complex = (
                f"[1:a]aloop=loop=-1:size=2e+09,atrim=duration={dur},"
                f"volume={config.COUNTDOWN_MUSIC_VOLUME},"
                f"afade=t=out:st={max(0, dur - 2)}:d=2,"
                f"aformat=sample_rates=44100:channel_layouts=stereo[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-framerate", "1",
                "-i", str(frames_dir / "frame_%04d.png"),
                "-i", str(music_path),
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                "-t", str(dur),
                str(tmp),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-framerate", "1",
                "-i", str(frames_dir / "frame_%04d.png"),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                "-t", str(dur),
                str(tmp),
            ]

        if _run(cmd, "build_countdown_segment"):
            tmp.replace(output_path)
            return True
        return False

    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


# ===========================================================================
# 3. SOLUTION SCREEN
# ===========================================================================

def build_solution_segment(answer: str, tts_audio: Path, output_path: Path) -> bool:
    """
    Generates the solution reveal screen:
    - High-aesthetic dark navy card with gold accents
    - Header: THE ANSWER
    - Large answer text
    - TTS voice audio + optional reveal chime
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(output_path)
    card_img = output_path.parent / f".card_solution_{os.getpid()}.png"

    audio_dur = _get_audio_duration(tts_audio) or float(config.SOLUTION_DURATION)
    duration = max(audio_dur + 0.8, float(config.SOLUTION_DURATION))

    # Render Pillow Card
    img = Image.new("RGB", (W, H), color=(10, 13, 28))
    draw = ImageDraw.Draw(img)

    # Card border
    draw.rounded_rectangle([60, 220, W - 60, H - 220], radius=45, outline=(255, 215, 0), width=4, fill=(18, 22, 48))

    # Badge: THE ANSWER
    font_badge = _get_font(48)
    draw.rounded_rectangle([320, 340, W - 320, 450], radius=25, fill=(255, 215, 0))
    draw.text((W // 2, 395), "THE ANSWER", fill=(10, 13, 28), font=font_badge, anchor="mm")

    # AI-generated content watermark (top-right)
    _draw_ai_label(draw)

    # Wrapped answer text
    font_answer = _get_font(85)
    wrapped_lines = _wrap_text_lines(answer, max_chars=22)
    line_h = 110
    total_h = len(wrapped_lines) * line_h
    start_y = (H // 2) - (total_h // 2) + 20

    for idx, line in enumerate(wrapped_lines):
        y = start_y + idx * line_h
        draw.text((W // 2, y), line, fill=(255, 255, 255), font=font_answer, anchor="mm")

    # Bottom hint
    font_hint = _get_font(40)
    draw.text((W // 2, H - 340), "Here is why...", fill=(255, 215, 0), font=font_hint, anchor="mm")

    img.save(str(card_img), "PNG")

    try:
        reveal_sfx = config.REVEAL_SOUND_FILE
        has_reveal = reveal_sfx.exists() and reveal_sfx.stat().st_size > 0

        if has_reveal:
            filter_complex = (
                f"[1:a]apad=pad_dur={duration},aformat=sample_rates=44100:channel_layouts=stereo[tts];"
                f"[2:a]volume=0.8,aformat=sample_rates=44100:channel_layouts=stereo[sfx];"
                f"[tts][sfx]amix=inputs=2:duration=longest[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(card_img),
                "-i", str(tts_audio),
                "-i", str(reveal_sfx),
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                "-t", str(duration),
                str(tmp),
            ]
        else:
            filter_complex = (
                f"[1:a]apad=pad_dur={duration},"
                f"aformat=sample_rates=44100:channel_layouts=stereo[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(card_img),
                "-i", str(tts_audio),
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", config.VIDEO_CODEC,
                "-preset", config.VIDEO_PRESET,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-c:a", config.AUDIO_CODEC,
                "-b:a", config.AUDIO_BITRATE,
                "-t", str(duration),
                str(tmp),
            ]

        if _run(cmd, "build_solution_segment"):
            tmp.replace(output_path)
            return True
        return False

    finally:
        if card_img.exists():
            card_img.unlink()


# ===========================================================================
# 4. EXPLANATION SCREEN
# ===========================================================================

def build_explanation_segment(explanation: str, tts_audio: Path, output_path: Path) -> bool:
    """
    Generates the explanation screen:
    - Dark academia scholar card
    - Header: EXPLANATION
    - Formatted, wrapped explanation text
    - TTS voice audio narration
    - AI watermark + follow call-to-action (retention)
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(output_path)
    card_img = output_path.parent / f".card_explanation_{os.getpid()}.png"

    audio_dur = _get_audio_duration(tts_audio) or float(config.EXPLANATION_DURATION)
    duration = max(audio_dur + 1.0, float(config.EXPLANATION_DURATION))

    # Render Pillow Card
    img = Image.new("RGB", (W, H), color=(8, 10, 24))
    draw = ImageDraw.Draw(img)

    # Card border
    draw.rounded_rectangle([60, 200, W - 60, H - 200], radius=45, outline=(60, 80, 140), width=3, fill=(15, 19, 44))

    # Badge: EXPLANATION
    font_badge = _get_font(46)
    draw.rounded_rectangle([320, 290, W - 320, 395], radius=25, fill=(255, 215, 0))
    draw.text((W // 2, 342), "EXPLANATION", fill=(8, 10, 24), font=font_badge, anchor="mm")

    # AI-generated content watermark (top-right)
    _draw_ai_label(draw)

    # Multi-line explanation text
    font_body = _get_font(56)
    wrapped_lines = _wrap_text_lines(explanation, max_chars=28)
    line_h = 82
    total_h = len(wrapped_lines) * line_h
    start_y = (H // 2) - (total_h // 2) + 30

    for idx, line in enumerate(wrapped_lines):
        y = start_y + idx * line_h
        draw.text((W // 2, y), line, fill=(240, 243, 255), font=font_body, anchor="mm")

    # Follow call-to-action (retention hook)
    _draw_end_cta(draw)

    img.save(str(card_img), "PNG")

    try:
        filter_complex = (
            f"[1:a]apad=pad_dur={duration},"
            f"aformat=sample_rates=44100:channel_layouts=stereo[aout]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(card_img),
            "-i", str(tts_audio),
            "-filter_complex", filter_complex,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", config.VIDEO_CODEC,
            "-preset", config.VIDEO_PRESET,
            "-r", str(FPS),
            "-pix_fmt", "yuv420p",
            "-c:a", config.AUDIO_CODEC,
            "-b:a", config.AUDIO_BITRATE,
            "-t", str(duration),
            str(tmp),
        ]

        if _run(cmd, "build_explanation_segment"):
            tmp.replace(output_path)
            return True
        return False

    finally:
        if card_img.exists():
            card_img.unlink()


# ===========================================================================
# 5. FINAL CONCATENATION
# ===========================================================================

def _concat_copy(segment_paths: List[Path], output: Path, list_file: Path) -> bool:
    """Lossless concat via concat demuxer (-c copy). Fast, no quality loss."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(output),
    ]
    return _run(cmd, "concat_segments")


def _concat_reencode(segment_paths: List[Path], output: Path) -> bool:
    """
    Fallback concat via the concat filter (full re-encode).
    Slower but resilient to any subtle stream-parameter mismatch between segments.
    """
    n = len(segment_paths)
    vfilter = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]"
    afilter = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[aout]"
    cmd = [
        "ffmpeg", "-y",
        *sum((["-i", str(s)] for s in segment_paths), []),
        "-filter_complex", f"{vfilter};{afilter}",
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", config.VIDEO_CODEC,
        "-preset", config.VIDEO_PRESET,
        "-r", str(FPS),
        "-pix_fmt", "yuv420p",
        "-c:a", config.AUDIO_CODEC,
        "-b:a", config.AUDIO_BITRATE,
        str(output),
    ]
    return _run(cmd, "concat_segments_reencode")


def concat_segments(segment_paths: List[Path], output_path: Path) -> bool:
    """
    Concatenates multiple video segments into one final video.
    Tries lossless -c copy first; falls back to a full re-encode if stream
    params ever mismatch so a render never dies because of a concat quirk.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(output_path)
    list_file = output_path.parent / f".concat_{os.getpid()}_{output_path.stem}.txt"

    try:
        with open(list_file, "w") as f:
            for seg in segment_paths:
                f.write(f"file '{seg.resolve()}'\n")

        if _concat_copy(segment_paths, tmp, list_file):
            tmp.replace(output_path)
            return True

        logger.warning("Lossless concat failed — retrying with re-encode fallback...")
        if _concat_reencode(segment_paths, tmp):
            tmp.replace(output_path)
            return True
        return False
    finally:
        if list_file.exists():
            list_file.unlink()
        if tmp.exists():
            tmp.unlink()


# ===========================================================================
# FULL PIPELINE — build_riddle_video
# ===========================================================================

def build_riddle_video(
    ai_video_path: Path,
    answer: str,
    explanation: str,
    output_path: Path,
    work_dir: Optional[Path] = None,
) -> bool:
    """
    Full 4-segment pipeline for one riddle video:
      1. Normalize AI clip -> 9:16 portrait (1080x1920)
      2. Build 40s countdown timer screen with music
      3. Generate solution TTS -> build solution screen
      4. Enrich explanation via Ollama -> generate TTS -> build explanation screen
      5. Concat all 4 segments -> org_N.mp4

    Returns True on success.
    """
    work = work_dir or (config.LOCAL_WORK_DIR / output_path.stem)
    work.mkdir(parents=True, exist_ok=True)

    seg1 = work / "seg1_ai.mp4"
    seg2 = work / "seg2_countdown.mp4"
    seg3 = work / "seg3_solution.mp4"
    seg4 = work / "seg4_explanation.mp4"
    tts_solution = work / "tts_solution.aac"
    tts_explanation = work / "tts_explanation.aac"
    label_png = work / "ai_label.png"

    try:
        # Pre-render the "AI GENERATED" watermark (overlaid on the AI clip)
        if config.AI_LABEL_ENABLED:
            _make_ai_label_png(label_png)
        label_path = label_png if config.AI_LABEL_ENABLED else None

        # [Seg 1/4] Normalize AI clip
        logger.info("  [Seg 1/4] Normalizing AI clip to 9:16 (1080x1920)...")
        if not normalize_ai_clip(ai_video_path, seg1, label_path):
            logger.error("Segment 1 (AI clip normalization) FAILED.")
            return False

        # [Seg 2/4] Countdown timer
        logger.info(f"  [Seg 2/4] Building {config.COUNTDOWN_DURATION}s countdown timer...")
        if not build_countdown_segment(seg2, config.COUNTDOWN_DURATION):
            logger.error("Segment 2 (countdown) FAILED.")
            return False

        # [Seg 3/4] Solution screen + TTS
        logger.info(f"  [Seg 3/4] Generating solution audio: '{answer}'")
        solution_script = f"The answer is... {answer}"
        if not tts_engine.speak_to_file(solution_script, tts_solution):
            logger.error("TTS for solution FAILED.")
            return False

        if not build_solution_segment(answer, tts_solution, seg3):
            logger.error("Segment 3 (solution screen) FAILED.")
            return False

        # [Seg 4/4] Explanation screen + Ollama + TTS
        logger.info("  [Seg 4/4] Enriching explanation via Ollama...")
        enriched = ollama_client.enrich_explanation(answer, explanation)
        logger.info(f"  Narration: '{enriched[:80]}...'")

        if not tts_engine.speak_to_file(enriched, tts_explanation):
            logger.error("TTS for explanation FAILED.")
            return False

        if not build_explanation_segment(enriched, tts_explanation, seg4):
            logger.error("Segment 4 (explanation screen) FAILED.")
            return False

        # [Duration check] Enforce TikTok monetization minimum length by
        # extending the countdown if the assembled video would be too short.
        dur1 = _get_duration(seg1) or 0.0
        dur2 = _get_duration(seg2) or 0.0
        dur3 = _get_duration(seg3) or 0.0
        dur4 = _get_duration(seg4) or 0.0
        total = dur1 + dur2 + dur3 + dur4

        deficit = config.MIN_TOTAL_DURATION - total
        if deficit > 0:
            extra = int(math.ceil(deficit))
            logger.info(
                f"  [Duration] Assembled video {total:.1f}s < "
                f"MIN_TOTAL_DURATION={config.MIN_TOTAL_DURATION}s — "
                f"extending countdown by {extra}s."
            )
            if not build_countdown_segment(seg2, config.COUNTDOWN_DURATION + extra):
                logger.error("Countdown extension FAILED.")
                return False
            dur2 = _get_duration(seg2) or 0.0
            total = dur1 + dur2 + dur3 + dur4
        logger.info(f"  [Duration] Final video length: {total:.1f}s")

        # [Concat] Concatenate all 4 segments (lossless + re-encode fallback)
        logger.info("  [Concat] Joining all 4 segments into final video...")
        segments = [seg1, seg2, seg3, seg4]
        if not concat_segments(segments, output_path):
            logger.error("Final concatenation FAILED.")
            return False

        logger.info(f"  ✅ Final video created: {output_path.name}")
        return True

    finally:
        shutil.rmtree(work, ignore_errors=True)


# ===========================================================================
# UTILITIES
# ===========================================================================

def _run(cmd: List[str], stage: str) -> bool:
    """Runs an FFmpeg command, logs error on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            tail = result.stderr[-500:] if result.stderr else "(no output)"
            logger.error(f"FFmpeg [{stage}] failed (rc={result.returncode}):\n{tail}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg [{stage}] timed out after 600s.")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found! Install via: brew install ffmpeg")
        return False


def _tmp_path(output_path: Path) -> Path:
    """Returns an atomic write path."""
    return output_path.with_name(f".{output_path.stem}_{os.getpid()}.tmp.mp4")


def _has_audio_stream(video_path: Path) -> bool:
    """Checks if video file contains an audio stream."""
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=10
        )
        return bool(res.stdout.strip())
    except Exception:
        return False


def _get_audio_duration(audio_path: Path) -> Optional[float]:
    """Returns audio file duration in seconds using ffprobe."""
    return _get_duration(audio_path)


def _get_duration(path: Path) -> Optional[float]:
    """Returns media file duration in seconds using ffprobe (video or audio)."""
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass
    return None


def probe_ai_clip(video_path: Path) -> Tuple[Optional[float], bool]:
    """
    Validates an uploaded AI clip before rendering.
    Returns (duration_seconds, has_video_stream). If ffprobe cannot read the
    file at all, returns (None, False) so the caller can mark it invalid.
    """
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=15
        )
        if res.returncode != 0 or not res.stdout.strip():
            logger.warning(f"ffprobe could not read '{video_path.name}': "
                           f"{res.stderr.strip()[-200:]}")
            return None, False

        data = json.loads(res.stdout)
        has_video = any(
            s.get("codec_type") == "video" for s in data.get("streams", [])
        )
        raw_dur = data.get("format", {}).get("duration")
        duration = float(raw_dur) if raw_dur else None
        return duration, has_video
    except Exception as e:
        logger.warning(f"probe_ai_clip failed for '{video_path.name}': {e}")
        return None, False


def _wrap_text_lines(text: str, max_chars: int = 25) -> List[str]:
    """Word-wraps text into a list of strings with at most max_chars per line."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        if len(current) + len(word) + (1 if current else 0) <= max_chars:
            current = f"{current} {word}".strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]
