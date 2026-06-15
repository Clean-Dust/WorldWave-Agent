"""
ww/core/voice.py — Voice / STT / TTS Integration v0.1

Multi-provider voice pipeline:
- STT (Speech-to-Text): faster-whisper (local), Groq, OpenAI Whisper
- TTS (Text-to-Speech): Edge TTS (free), ElevenLabs, OpenAI, MiniMax
- Voice mode: voice-in → text → LLM → voice-out

Zero required dependencies for default (Edge TTS built-in).
"""

from __future__ import annotations
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Union

logger = logging.getLogger("ww.voice")


class STTProvider(Enum):
    LOCAL_WHISPER = "local_whisper"  # faster-whisper (free, offline)
    GROQ = "groq"                    # Groq Whisper API (free tier)
    OPENAI = "openai"                # OpenAI Whisper (paid)


class TTSProvider(Enum):
    EDGE = "edge"                    # Edge TTS (free, built-in)
    ELEVENLABS = "elevenlabs"        # ElevenLabs (free tier)
    OPENAI = "openai"                # OpenAI TTS (paid)
    MINIMAX = "minimax"              # MiniMax (paid)


@dataclass
class VoiceConfig:
    """Voice pipeline configuration."""
    stt_provider: STTProvider = STTProvider.LOCAL_WHISPER
    tts_provider: TTSProvider = TTSProvider.EDGE
    stt_model: str = "base"  # tiny, base, small, medium, large-v3
    tts_voice: str = "en-US-AriaNeural"  # Edge TTS voice
    tts_speed: float = 1.0
    language: str = "auto"  # auto-detect or specific language code
    enabled: bool = True


class VoiceEngine:
    """Unified voice pipeline."""
    
    def __init__(self, config: VoiceConfig = None):
        self.config = config or VoiceConfig()
        self._stt_model = None  # Lazy-loaded whisper model
        
    # ── STT ─────────────────────────────────────────────────────
    
    async def transcribe(self, audio_data: bytes, format: str = "wav") -> str:
        """Transcribe audio to text."""
        if self.config.stt_provider == STTProvider.LOCAL_WHISPER:
            return await self._transcribe_whisper(audio_data, format)
        elif self.config.stt_provider == STTProvider.GROQ:
            return await self._transcribe_groq(audio_data, format)
        elif self.config.stt_provider == STTProvider.OPENAI:
            return await self._transcribe_openai(audio_data, format)
        raise ValueError(f"Unknown STT provider: {self.config.stt_provider}")
        
    async def _transcribe_whisper(self, audio_data: bytes, format: str) -> str:
        """Transcribe using local faster-whisper."""
        try:
            from faster_whisper import WhisperModel
            
            if self._stt_model is None:
                model_size = self.config.stt_model
                self._stt_model = WhisperModel(model_size, device="cpu", compute_type="int8")
                
            # Save audio to temp file
            with tempfile.NamedTemporaryFile(suffix=f".{format}", delete=False) as f:
                f.write(audio_data)
                temp_path = f.name
                
            try:
                segments, info = self._stt_model.transcribe(temp_path, language=self.config.language)
                text = " ".join(seg.text for seg in segments)
                return text.strip()
            finally:
                os.unlink(temp_path)
                
        except ImportError:
            logger.warning("faster-whisper not installed. Install with: pip install faster-whisper")
            return await self._transcribe_groq(audio_data, format)
            
    async def _transcribe_groq(self, audio_data: bytes, format: str) -> str:
        """Transcribe using Groq API."""
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            logger.error("GROQ_API_KEY not set")
            return ""
            
        import httpx
        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=f".{format}", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name
            
        try:
            async with httpx.AsyncClient() as client:
                with open(temp_path, 'rb') as af:
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        files={"file": af},
                        data={"model": "whisper-large-v3", "language": self.config.language},
                        timeout=60,
                    )
                    if resp.status_code == 200:
                        return resp.json().get("text", "")
                    else:
                        logger.error(f"Groq STT error: {resp.status_code} {resp.text}")
                        return ""
        finally:
            os.unlink(temp_path)
            
    async def _transcribe_openai(self, audio_data: bytes, format: str) -> str:
        """Transcribe using OpenAI API."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY not set")
            return ""
            
        import httpx
        with tempfile.NamedTemporaryFile(suffix=f".{format}", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name
            
        try:
            async with httpx.AsyncClient() as client:
                with open(temp_path, 'rb') as af:
                    resp = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        files={"file": af},
                        data={"model": "whisper-1", "language": self.config.language},
                        timeout=60,
                    )
                    if resp.status_code == 200:
                        return resp.json().get("text", "")
                    else:
                        logger.error(f"OpenAI STT error: {resp.status_code}")
                        return ""
        finally:
            os.unlink(temp_path)
            
    # ── TTS ─────────────────────────────────────────────────────
    
    async def synthesize(self, text: str) -> bytes:
        """Convert text to speech audio bytes."""
        if self.config.tts_provider == TTSProvider.EDGE:
            return await self._tts_edge(text)
        elif self.config.tts_provider == TTSProvider.ELEVENLABS:
            return await self._tts_elevenlabs(text)
        elif self.config.tts_provider == TTSProvider.OPENAI:
            return await self._tts_openai(text)
        elif self.config.tts_provider == TTSProvider.MINIMAX:
            return await self._tts_minimax(text)
        raise ValueError(f"Unknown TTS provider: {self.config.tts_provider}")
        
    async def _tts_edge(self, text: str) -> bytes:
        """Use Microsoft Edge TTS (free, no API key needed)."""
        try:
            import edge_tts
            
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                out_path = f.name
                
            communicate = edge_tts.Communicate(text, self.config.tts_voice)
            await communicate.save(out_path)
            
            with open(out_path, 'rb') as f:
                audio = f.read()
            os.unlink(out_path)
            return audio
            
        except ImportError:
            logger.warning("edge-tts not installed. Install with: pip install edge-tts")
            return b""
            
    async def _tts_elevenlabs(self, text: str) -> bytes:
        """Use ElevenLabs TTS."""
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            logger.error("ELEVENLABS_API_KEY not set")
            return b""
            
        import httpx
        voice_id = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": "eleven_monolingual_v1",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.content
            else:
                logger.error(f"ElevenLabs error: {resp.status_code}")
                return b""
                
    async def _tts_openai(self, text: str) -> bytes:
        """Use OpenAI TTS."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY not set")
            return b""
            
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "tts-1",
                    "input": text,
                    "voice": "alloy",
                    "speed": self.config.tts_speed,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.content
            else:
                logger.error(f"OpenAI TTS error: {resp.status_code}")
                return b""
                
    async def _tts_minimax(self, text: str) -> bytes:
        """Use MiniMax TTS."""
        api_key = os.getenv("MINIMAX_API_KEY")
        if not api_key:
            logger.error("MINIMAX_API_KEY not set")
            return b""
            
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.minimax.chat/v1/t2a_v2",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "speech-01",
                    "text": text,
                    "voice_setting": {"voice_id": "male-qn-qingse"},
                    "speed": self.config.tts_speed,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("base_resp", {}).get("status_code") == 0:
                    # MiniMax returns hex-encoded audio
                    import base64
                    return base64.b64decode(data.get("data", {}).get("audio", ""))
            logger.error(f"MiniMax TTS error: {resp.status_code}")
            return b""


# Singleton
_voice_engine: Optional[VoiceEngine] = None


def get_voice_engine() -> VoiceEngine:
    global _voice_engine
    if _voice_engine is None:
        _voice_engine = VoiceEngine()
    return _voice_engine
