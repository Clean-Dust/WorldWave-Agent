"""core/stt.py — Asynchronous Whisper speech-to-text pipeline.

Design:
  - Lazy-loaded Whisper model (loaded on first call to avoid blocking startup)
  - Supports tiny/base/small/medium/large model sizes, configurable via env var
  - Runs inference in a thread pool to avoid blocking the event loop
  - Auto-downloads model files on first use (~75MB for tiny, ~145MB for base)
  - Zero external deps beyond openai-whisper + ffmpeg (both optional)

Usage:
    from core.stt import transcribe_voice

    text = await transcribe_voice("voice.ogg")  # blocks in thread pool
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("ww.stt")

# ── Model cache ──────────────────────────────────────────────────────

_model_lock = threading.Lock()
_model = None
_model_size: str = ""


def _get_model_size() -> str:
    """Resolve model size from env var, default 'tiny' (~75MB).

    Checks WW_WHISPER_MODEL first, then WW_STT_MODEL as fallback.
    """
    return os.environ.get("WW_WHISPER_MODEL",
           os.environ.get("WW_STT_MODEL", "tiny")).strip()


def _load_model():
    """Lazy-load the Whisper model (thread-safe, single load)."""
    global _model, _model_size

    size = _get_model_size()
    with _model_lock:
        if _model is not None and _model_size == size:
            return  # Already loaded

        import whisper
        log.info("Loading Whisper model: %s (first load, downloads if needed)", size)
        _model = whisper.load_model(size)
        _model_size = size
        log.info("Whisper model '%s' loaded successfully", size)


def _is_whisper_available() -> bool:
    """Check if openai-whisper is installed."""
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


# ── Public API ───────────────────────────────────────────────────────

async def transcribe_voice(audio_path: str, language: str = "") -> str:
    """Transcribe a voice/audio file to text using Whisper.

    Args:
        audio_path: Path to the audio file (ogg, mp3, wav, etc. — ffmpeg handles decoding)
        language: Optional ISO language code (e.g. 'zh', 'en'). Auto-detect if empty.

    Returns:
        Transcription text, or empty string on failure.
    """
    import asyncio

    if not _is_whisper_available():
        log.warning(
            "openai-whisper not installed. "
            "Install with: pip install worldwave[stt]"
        )
        return ""

    if not os.path.isfile(audio_path):
        log.error("Audio file not found: %s", audio_path)
        return ""

    # Load model (thread-safe, lazy)
    _load_model()

    # Run inference in thread pool (Whisper is CPU-bound and blocks)
    loop = asyncio.get_running_loop()

    def _run():
        try:
            global _model
            opts = {}
            if language:
                opts["language"] = language
            result = _model.transcribe(audio_path, **opts)
            return result.get("text", "").strip()
        except Exception:
            log.exception("Whisper transcription failed for %s", audio_path)
            return ""

    return await loop.run_in_executor(None, _run)


def transcribe_sync(audio_path: str, language: str = "") -> str:
    """Synchronous version for use in non-async contexts."""
    import warnings

    if not _is_whisper_available():
        warnings.warn("openai-whisper not installed")
        return ""

    _load_model()
    try:
        global _model
        opts = {}
        if language:
            opts["language"] = language
        result = _model.transcribe(audio_path, **opts)
        return result.get("text", "").strip()
    except Exception:
        log.exception("Whisper sync transcription failed")
        return ""


def is_available() -> bool:
    """Check if STT pipeline is usable."""
    return _is_whisper_available()
