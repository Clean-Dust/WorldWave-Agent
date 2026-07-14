"""Wavegate Telegram Adapter.

Ports the existing gateway/telegram.py to the Wavegate architecture.
Normalizes Telegram messages into UnifiedMessage format and routes through
the Wavegate on_message callback.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Callable, Optional

from google.protobuf.timestamp_pb2 import Timestamp

from proto.wavegate.v1.unified_message_pb2 import (
    UnifiedMessage, Sender, Content, TextContent, RoutingHints,
)
from gateway.adapters import BaseAdapter, AdapterRegistry

log = logging.getLogger("gateway.telegram")

TELEGRAM_API = "https://api.telegram.org/bot"


class TelegramAdapter(BaseAdapter):
    """Telegram platform adapter for Wavegate.

    Features:
    - Long-polling via getUpdates
    - @mention detection
    - Normalization to UnifiedMessage
    - Streaming response via editMessageText
    """

    platform = "telegram"

    def __init__(
        self,
        token: str = "",
        workspace_id: Optional[int] = None,
        poll_interval: float = 2.0,
        on_message: Optional[Callable] = None,
        session_mgr=None,
        pairing_mgr=None,
    ):
        self._token = token or os.environ.get("TELEGRAM_WW_TOKEN", "")
        self._workspace_id = workspace_id
        if not self._workspace_id:
            raw = os.environ.get("TELEGRAM_WW_WORKSPACE", "")
            if raw:
                self._workspace_id = int(raw)
        self._poll_interval = poll_interval
        self._on_message = on_message
        self._session_mgr = session_mgr

        # DM pairing — if not provided, create a standalone instance
        if pairing_mgr:
            self._pairing = pairing_mgr
        else:
            from gateway.pairing import PairingManager
            self._pairing = PairingManager()

        self._bot_username: str = ""
        self._offset: int = 0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._pending_approvals: dict = {}  # HITL approval tracking
        self._stream_state: dict = {}       # Streaming debounce state
        self.STREAM_DEBOUNCE_SEC = 1.5      # blueprint: 1.5-2s debounce

        # WW local API URL for slash commands
        _port = os.environ.get("WW_PORT", "9300")
        _key = os.environ["WW_API_KEY"]  # required, no default
        self._ww_api = f"http://localhost:{_port}"
        self._ww_key = _key

    # ── Adapter interface ──────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        if not self._token or self._token == "your_bot_token_here":
            log.warning("Telegram adapter: no token, skipping")
            return

        self._resolve_username()
        self._register_commands()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-adapter")
        self._thread.start()
        self._running = True
        log.info("Telegram adapter started (@%s)", self._bot_username)

    def stop(self):
        if not self._running:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        return self._api_call("sendMessage", {
            "chat_id": str(chat_id),
            "text": text[:4000],
            "parse_mode": kwargs.get("parse_mode", "Markdown"),
        }).get("ok", False)

    def send_stream_chunk(self, chat_id: str, chunk) -> bool:
        """Send a streaming chunk with debounced editMessageText.
        
        Blueprint: "Debounce mechanism buffers high-frequency token
        streams, setting updates to every 1.5-2s to avoid 429 errors."
        
        Maintains per-chat state: current message ID, accumulator buffer,
        last edit timestamp.
        """
        if not chunk:
            return True

        now = time.time()
        key = str(chat_id)
        state = self._stream_state.get(key)

        if state is None or state.get("done"):
            # New stream: send initial message
            text = chunk if isinstance(chunk, str) else str(chunk)
            result = self._api_call("sendMessage", {
                "chat_id": str(chat_id),
                "text": text[:4000],
            })
            if result.get("ok"):
                state = {
                    "message_id": result["result"]["message_id"],
                    "buffer": text,
                    "last_edit": now,
                    "done": False,
                }
                self._stream_state[key] = state
                return True
            return False

        # Accumulate and debounce
        new_text = chunk if isinstance(chunk, str) else str(chunk)
        state["buffer"] = new_text
        elapsed = now - state["last_edit"]

        if elapsed >= self.STREAM_DEBOUNCE_SEC:
            # Send update
            self._api_call("editMessageText", {
                "chat_id": str(chat_id),
                "message_id": state["message_id"],
                "text": new_text[:4000],
            })
            state["last_edit"] = now

        return True

    def end_stream(self, chat_id: str, final_text: str = ""):
        """Finalize a stream by sending the last edit and marking done."""
        key = str(chat_id)
        state = self._stream_state.get(key)
        if state and not state.get("done"):
            text = final_text or state.get("buffer", "")
            self._api_call("editMessageText", {
                "chat_id": str(chat_id),
                "message_id": state["message_id"],
                "text": text[:4000],
            })
            state["done"] = True

    # ── HITL Interactive Cards ──────────────────────────────────

    def request_approval(
        self,
        chat_id: str,
        tool_name: str,
        description: str,
        risk_level: str = "medium",
        timeout: int = 300,
    ) -> str:
        """Send an inline keyboard with Approve/Reject buttons.

        Returns an approval_id for tracking the response.
        Blueprint: "Interactive UI cards with Approve and Reject buttons."
        """
        import hashlib

        approval_id = hashlib.md5(
            f"{chat_id}:{tool_name}:{time.time()}".encode()
        ).hexdigest()[:12]

        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "✅ Approve",
                    "callback_data": f"hitl:approve:{approval_id}",
                },
                {
                    "text": "❌ Reject",
                    "callback_data": f"hitl:reject:{approval_id}",
                },
            ]]
        }

        text = (
            "⚠️ *Tool requires approval*" + chr(10) + chr(10) +
            f"*Tool:* `{tool_name}`" + chr(10) +
            f"*Risk:* {risk_level}" + chr(10) + chr(10) +
            f"{description}" + chr(10) + chr(10) +
            f"_Expires in {timeout // 60} minutes_"
        )

        result = self._api_call("sendMessage", {
            "chat_id": str(chat_id),
            "text": text[:4000],
            "parse_mode": "Markdown",
            "reply_markup": json.dumps(keyboard),
        })

        if result.get("ok"):
            message_id = result["result"]["message_id"]
            self._pending_approvals[approval_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "tool_name": tool_name,
                "created_at": time.time(),
                "timeout": timeout,
                "result": None,  # None=pending, True=approved, False=rejected
            }
            log.info(
                "HITL approval requested: %s for tool=%s chat=%s",
                approval_id, tool_name, chat_id,
            )
            return approval_id

        log.error("Failed to send approval keyboard to %s", chat_id)
        return ""

    def get_approval_result(self, approval_id: str) -> Optional[bool]:
        """Check the result of a pending approval request.

        Returns True (approved), False (rejected), or None (still pending/expired).
        """
        pending = self._pending_approvals.get(approval_id)
        if not pending:
            return None

        # Check timeout
        if time.time() - pending["created_at"] > pending["timeout"]:
            pending["result"] = False
            self._cleanup_approval_message(pending)
            del self._pending_approvals[approval_id]
            return False

        if pending["result"] is not None:
            self._cleanup_approval_message(pending)
            return pending["result"]

        return None

    def wait_approval(self, approval_id: str, poll_interval: float = 1.0) -> bool:
        """Block until approval is resolved (approved or rejected).

        Returns True if approved, False if rejected/timed out.
        """
        while True:
            result = self.get_approval_result(approval_id)
            if result is not None:
                return result
            time.sleep(poll_interval)

    # ── Internal: callback handling ─────────────────────────────

    def _process_callback(self, callback_query: dict):
        """Handle inline keyboard button presses."""
        data = callback_query.get("data", "")
        if not data.startswith("hitl:"):
            return

        parts = data.split(":", 2)
        if len(parts) < 3:
            return

        action = parts[1]  # approve or reject
        approval_id = parts[2]

        pending = self._pending_approvals.get(approval_id)
        if not pending or pending["result"] is not None:
            self._api_call("answerCallbackQuery", {
                "callback_query_id": callback_query["id"],
                "text": "This request has expired.",
            })
            return

        if action == "approve":
            pending["result"] = True
            self._api_call("answerCallbackQuery", {
                "callback_query_id": callback_query["id"],
                "text": "Approved ✓",
            })
            log.info("HITL approved: %s", approval_id)
        else:
            pending["result"] = False
            self._api_call("answerCallbackQuery", {
                "callback_query_id": callback_query["id"],
                "text": "Rejected ✗",
            })
            log.info("HITL rejected: %s", approval_id)

    def _cleanup_approval_message(self, pending: dict):
        """Remove the inline keyboard after approval is resolved."""
        try:
            status = "✅ Approved" if pending["result"] else "❌ Rejected"
            self._api_call("editMessageReplyMarkup", {
                "chat_id": str(pending["chat_id"]),
                "message_id": pending["message_id"],
                "reply_markup": json.dumps({"inline_keyboard": []}),
            })
            self._api_call("editMessageText", {
                "chat_id": str(pending["chat_id"]),
                "message_id": pending["message_id"],
                "text": f"{status} — `{pending['tool_name']}`",
                "parse_mode": "Markdown",
            })
        except Exception:
            pass

    # ── Factory ────────────────────────────────────────────────

    @classmethod
    def try_register(cls, on_message=None, session_mgr=None):
        """Auto-register if token is configured."""
        token = os.environ.get("TELEGRAM_WW_TOKEN", "")
        if token and token != "your_bot_token_here":
            adapter = cls(token=token, on_message=on_message, session_mgr=session_mgr)
            AdapterRegistry.register(adapter)

    # ── Polling loop ───────────────────────────────────────────

    def _resolve_username(self):
        try:
            data = self._api_call("getMe")
            if data.get("ok"):
                self._bot_username = data["result"].get("username", "").lower()
                log.info("Bot username resolved: @%s", self._bot_username)
            else:
                log.error("getMe failed: %s", data)
        except Exception as e:
            log.error("Cannot resolve bot username (getMe failed): %s", e)
            log.error("Bot will NOT respond to @mentions until this is fixed.")
            log.error("Check TELEGRAM_WW_TOKEN — current value length: %d", len(self._token) if self._token else 0)

    def _register_commands(self):
        """Register bot commands with Telegram so they appear in the menu."""
        commands = [
            {"command": "help", "description": "Show available commands"},
            {"command": "status", "description": "Show current session status"},
            {"command": "tools", "description": "List available tools"},
            {"command": "model", "description": "Show current model info"},
            {"command": "memory", "description": "Show memory stats"},
            {"command": "new", "description": "Start a fresh session"},
            {"command": "stop", "description": "Stop current task"},
            {"command": "clear", "description": "Clear conversation history"},
        ]
        try:
            import json as _json
            import urllib.request as _ur
            payload = _json.dumps({"commands": commands}).encode()
            req = _ur.Request(
                f"{TELEGRAM_API}{self._token}/setMyCommands",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with _ur.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read())
                if result.get("ok"):
                    log.info("Telegram commands registered (%d commands)", len(commands))
                else:
                    log.warning("setMyCommands failed: %s", result)
        except Exception as e:
            log.warning("setMyCommands error: %s", e)

    def _handle_direct_command(self, chat_id: str, command: str, args: str, context: dict = None) -> bool:
        """Handle a command directly (fast path, no LLM). Returns True if handled."""
        c = command.lower()

        if c in ("help", "start"):
            text = (
                "**Worldwave Bot Commands**\n\n"
                "/help — Show this menu\n"
                "/status — Session status\n"
                "/tools — List available tools\n"
                "/model — Show or switch model (eg /model flash)\n"
                "/memory — Memory statistics\n"
                "/new — Start a fresh session\n"
                "/stop — Stop current task\n"
                "/clear — Clear history\n\n"
                "Just send a message to start a task!"
            )
            self.send_message(chat_id, text)
            return True

        if c == "status":
            try:
                import requests
                r = requests.get(f"{self._ww_api}/ww/status?api_key={self._ww_key}", timeout=5)
                s = r.json()
                text = (
                    f"**Status:** {s.get('version', 'N/A')}\n"
                    f"**Autonomous:** {s.get('autonomous', {}).get('running', 'N/A')}\n"
                    f"**Tools:** {s.get('tool_count', 'N/A')}\n"
                )
                if s.get("ww", {}).get("session"):
                    sess = s["ww"]["session"]
                    text += f"**Spirals:** {sess.get('current_spiral', 'N/A')}\n"
                    text += f"**Phase:** {sess.get('current_phase', 'N/A')}"
            except Exception:
                text = "**Status:** Server not reachable"
            self.send_message(chat_id, text)
            return True

        if c == "tools":
            try:
                import requests
                r = requests.get(f"{self._ww_api}/ww/status?api_key={self._ww_key}", timeout=5)
                data = r.json()
                cats = data.get("tool_categories", {})
                count = data.get("tool_count", 0)
                text = f"**Tools:** {count} total\n\n"
                for cat, n in sorted(cats.items(), key=lambda x: -x[1])[:12]:
                    text += f"• `{cat}`: {n}\n"
            except Exception:
                text = "**Tools:** Could not fetch"
            self.send_message(chat_id, text)
            return True

        if c == "model":
            if args.strip():
                # Switch model
                try:
                    import requests
                    r = requests.post(f"{self._ww_api}/ww/model?api_key={self._ww_key}",
                                      json={"model": args.strip()}, timeout=5)
                    s = r.json()
                    if s.get("switched"):
                        text = f"✓ Switched: `{s['from']}` → `{s['to']}`"
                    else:
                        text = f"✗ Failed: {s.get('error', 'unknown')}"
                except Exception as e:
                    text = f"✗ Error: {e}"
            else:
                # Show current
                try:
                    import requests
                    r = requests.get(f"{self._ww_api}/ww/model?api_key={self._ww_key}", timeout=5)
                    s = r.json()
                    text = (
                        f"**Model:** `{s.get('model', 'N/A')}`\n"
                        f"**Provider:** `{s.get('provider', 'N/A')}`"
                    )
                except Exception:
                    text = "**Model:** Could not detect"
            self.send_message(chat_id, text)
            return True

        if c == "memory":
            try:
                import requests
                r = requests.get(f"{self._ww_api}/ww/memory/stats?api_key={self._ww_key}", timeout=5)
                s = r.json()
                text = (
                    f"**Memory System**\n"
                    f"**Total atoms:** {s.get('total_atoms', 'N/A')}\n"
                    f"**Hippocampus:** {s.get('hippocampus_size', 'N/A')}\n"
                    f"**Cortex index:** {s.get('cortex_size', 'N/A')}\n"
                    f"**Sleep cycles:** {s.get('sleep_cycles', 'N/A')}"
                )
            except Exception:
                text = "**Memory:** Could not fetch"
            self.send_message(chat_id, text)
            return True

        if c == "new":
            self.send_message(chat_id, "🆕 Starting a new session. Send your task!")
            return True

        if c == "stop":
            self.send_message(chat_id, "⏹️ Stop signal sent.")
            return True

        if c == "clear":
            self.send_message(chat_id, "🧹 History cleared. Fresh start!")
            return True

        return False  # Not handled, route to LLM

    def _api_call(self, method: str, data: dict = None, raw: bool = False):
        import urllib.request
        import urllib.parse

        url = f"{TELEGRAM_API}{self._token}/{method}"
        try:
            if data:
                payload = urllib.parse.urlencode(data).encode()
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            else:
                req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30 if raw else 10) as resp:
                body = resp.read()
                if raw:
                    return body
                return json.loads(body)
        except Exception as e:
            log.debug("Telegram API call failed: %s", e)
            return {} if not raw else b""

    def _get_updates(self) -> list:
        result = self._api_call("getUpdates", {
            "offset": self._offset,
            "timeout": 10,
            "allowed_updates": json.dumps(["message", "callback_query", "voice", "audio"]),
        })
        if not result.get("ok"):
            return []
        updates = result.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def _is_mention(self, message: dict) -> bool:
        if not self._bot_username:
            return False
        text = (message.get("text") or "").strip()
        entities = message.get("entities", [])

        for entity in entities:
            if entity.get("type") == "mention":
                start, length = entity.get("offset", 0), entity.get("length", 0)
                mentioned = text[start:start+length].lstrip("@").lower()
                if mentioned == self._bot_username:
                    return True

        if text.startswith("@" + self._bot_username):
            return True

        reply_to = message.get("reply_to_message")
        if reply_to and reply_to.get("from", {}).get("is_bot", False):
            return True

        return False

    def _download_voice(self, file_id: str) -> str:
        """Download a voice/audio file from Telegram and return the local path."""
        import tempfile

        # Get file path from Telegram
        file_info = self._api_call("getFile", {"file_id": file_id})
        if not file_info.get("ok"):
            log.warning("getFile failed for file_id=%s", file_id)
            return ""

        file_path = file_info.get("result", {}).get("file_path", "")
        if not file_path:
            return ""

        # Download raw bytes
        url = f"{TELEGRAM_API}file/bot{self._token}/{file_path}"
        import urllib.request
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except Exception as e:
            log.warning("Voice download failed: %s", e)
            return ""

        if not data:
            return ""

        # Save to persistent voice cache dir
        voice_dir = "/tmp/ww-voice"
        os.makedirs(voice_dir, exist_ok=True)
        ext = os.path.splitext(file_path)[1] or ".ogg"
        fd, local_path = tempfile.mkstemp(suffix=ext, prefix="ww_voice_", dir=voice_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        log.info("Voice downloaded: %s → %s (%d bytes)", file_id, local_path, len(data))
        return local_path

    def _download_photo(self, file_id: str) -> str:
        """Download a photo from Telegram and return the local path."""
        import tempfile
        
        # Get file path from Telegram
        file_info = self._api_call("getFile", {"file_id": file_id})
        if not file_info.get("ok"):
            log.warning("getFile failed for photo file_id=%s", file_id)
            return ""
        
        file_path = file_info.get("result", {}).get("file_path", "")
        if not file_path:
            return ""
        
        # Download raw bytes
        url = f"{TELEGRAM_API}file/bot{self._token}/{file_path}"
        import urllib.request
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except Exception as e:
            log.warning("Photo download failed: %s", e)
            return ""
        
        if not data:
            return ""
        
        # Save to persistent dir so WW's vision_analyze can access it
        photo_dir = "/tmp/ww_photos"
        os.makedirs(photo_dir, exist_ok=True)
        ext = os.path.splitext(file_path)[1] or ".jpg"
        fd, local_path = tempfile.mkstemp(suffix=ext, prefix="ww_photo_", dir=photo_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        
        log.info("Photo downloaded: %s → %s (%d bytes)", file_id, local_path, len(data))
        return local_path

    def _handle_photo_message(self, message: dict) -> str:
        """Download photo and inject file path + caption into message.
        
        Returns the constructed text, or empty string on failure.
        Modifies the message dict in-place to set message['text'].
        """
        # Get highest-resolution photo
        photos = message.get("photo", [])
        if not photos:
            return ""
        
        # Last photo in array has highest resolution
        largest = photos[-1]
        file_id = largest.get("file_id", "")
        if not file_id:
            return ""
        
        # Download photo
        local_path = self._download_photo(file_id)
        if not local_path:
            return ""
        
        # Build text from caption + photo path
        caption = message.get("caption", "")
        text = f"[Photo received: {local_path}]"
        if caption:
            text = f"{caption}\n\n[Photo: {local_path}]"
        
        message["text"] = text
        message["photo_path"] = local_path
        log.info("Photo message processed: %s", local_path)
        return text

    def _handle_voice_message(self, message: dict) -> str:
        """Download voice message and transcribe via STT.

        Returns the transcription text, or empty string on failure.
        Modifies the message dict in-place to set message['text'].
        """
        voice = message.get("voice", {})
        file_id = voice.get("file_id", "")
        if not file_id:
            # Also check for audio (music files) and video_note (round videos)
            audio = message.get("audio", {})
            file_id = audio.get("file_id", "")
        if not file_id:
            return ""

        # Download voice file
        local_path = self._download_voice(file_id)
        if not local_path:
            return ""

        # Convert ogg→wav if ffmpeg is available (more reliable for Whisper)
        audio_path = self._convert_to_wav(local_path)

        try:
            # Run STT
            from core.stt import transcribe_sync, is_available

            if not is_available():
                log.warning("STT not available — openai-whisper not installed")
                return ""

            text = transcribe_sync(audio_path or local_path)
            if text:
                log.info("STT transcription: %d chars", len(text))
                # Inject transcription into the message
                message["text"] = text
                message["stt_transcription"] = True
                return text
            else:
                log.warning("STT returned empty transcription")
                return ""
        finally:
            # Clean up temp files
            for p in (local_path, audio_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    def _convert_to_wav(self, path: str) -> str:
        """Convert an audio file to WAV via ffmpeg subprocess.

        Returns path to WAV file, or empty string if conversion fails/not needed.
        """
        if path.endswith(".wav"):
            return ""
        try:
            import subprocess, tempfile
            fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="ww_voice_", dir="/tmp/ww-voice")
            os.close(fd)
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", wav_path],
                capture_output=True, timeout=30,
            )
            if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 0:
                log.info("Converted %s → %s", os.path.basename(path), os.path.basename(wav_path))
                return wav_path
        except Exception:
            log.warning("ffmpeg conversion failed for %s, will pass original to Whisper", path)
        return ""

    def _extract_command(self, message: dict) -> tuple[str, str]:
        """Extract command prefix and clean text from a message."""
        text = (message.get("text") or "").strip()
        cmd_prefix = ""
        clean = text

        # Strip @mention
        if self._bot_username and ("@" + self._bot_username) in text.lower():
            idx = text.lower().find("@" + self._bot_username)
            after = text[idx + len(self._bot_username) + 1:].strip()
            before = text[:idx].strip()
            clean = after or before

        # Detect command prefix
        clean = clean.strip()
        ALL_COMMANDS = ["goal", "status", "stop", "help", "tools", "model", "memory", "new", "clear", "start"]
        for cmd in ALL_COMMANDS:
            prefix = f"/{cmd}"
            if clean.startswith(prefix):
                cmd_prefix = cmd
                clean = clean[len(prefix):].strip()
                break

        return cmd_prefix, clean

    def _normalize_message(self, message: dict) -> UnifiedMessage:
        """Convert a Telegram message into a UnifiedMessage."""
        chat = message.get("chat", {})
        sender_raw = message.get("from", {})

        chat_id = str(chat.get("id", ""))
        user_id = str(sender_raw.get("id", ""))
        display_name = sender_raw.get("first_name", sender_raw.get("username", "unknown"))

        session_key = f"telegram:{user_id}:{chat_id}"

        cmd_prefix, clean_text = self._extract_command(message)

        now = Timestamp()
        now.GetCurrentTime()

        return UnifiedMessage(
            event_id=str(uuid.uuid4()),
            platform="telegram",
            session_key=session_key,
            received_at=now,
            sender=Sender(
                platform_id=user_id,
                display_name=display_name,
                role="operator",
            ),
            content=Content(
                text=TextContent(
                    body=message.get("text", ""),
                    command_prefix=cmd_prefix,
                    clean_text=clean_text,
                ),
            ),
            routing=RoutingHints(queue_mode="steer", priority=0),
        )

    def _send_typing(self, chat_id: int):
        self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def _poll_loop(self):
        """Main polling loop."""
        log.info("Telegram poll loop started (workspace=%s)", self._workspace_id)

        while not self._stop_event.is_set():
            try:
                updates = self._get_updates()
            except Exception as e:
                log.warning("Poll error: %s, retrying...", e)
                time.sleep(self._poll_interval * 3)
                continue

            for update in updates:
                # Handle callback queries (inline button presses)
                callback = update.get("callback_query")
                if callback:
                    self._process_callback(callback)
                    continue

                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                is_private = chat.get("type") == "private"

                # Only enforce workspace filter for group chats (not DMs)
                if not is_private and self._workspace_id and chat_id != self._workspace_id:
                    continue

                sender = message.get("from", {})
                user_id = str(sender.get("id", ""))
                display_name = sender.get("first_name", sender.get("username", "?"))

                # Skip messages from the bot itself to prevent echo loops
                if sender.get("is_bot"):
                    continue

                # ── Voice/audio: bypass @mention, but still check pairing ──
                has_voice = bool(message.get("voice")) or bool(message.get("audio"))

                # ── Photo: bypass @mention in groups, process like voice ──
                has_photo = bool(message.get("photo"))

                # DMs (private chats) don't need @mention — direct message is the mention
                if not has_voice and not has_photo and not is_private and not self._is_mention(message):
                    continue

                # ── DM Pairing: whitelist check ──────────────
                if not self._pairing.is_allowed("telegram", user_id):
                    # Only true/1/yes/on auto-whitelist. "false" must not approve.
                    _auto = str(os.environ.get("WW_PAIRING_AUTO_APPROVE", "")).strip().lower()
                    if _auto in ("1", "true", "yes", "on", "y"):
                        self._pairing.add_to_whitelist("telegram", user_id, display_name)
                        log.info("Auto-approved user %s (%s)", display_name, user_id)
                    else:
                        code = self._pairing.request_pairing(
                            "telegram", user_id, display_name, str(chat_id),
                        )
                        log.info(
                            "Unknown user %s (%s) — dropped, pairing code: %s",
                            display_name, user_id, code,
                        )
                        # Silently drop — do not process the message
                        # In future: send pairing code to admin channel
                        continue

                log.info("Message from %s", display_name)

                # ── Voice/audio message: STT transcription ───
                if has_voice:
                    self._handle_voice_message(message)
                    if not message.get("text"):
                        # Transcription failed — skip silently
                        log.info(
                            "Voice message from %s — transcription unavailable",
                            display_name,
                        )
                        continue

                # ── Photo message: download for vision analysis ───
                if has_photo:
                    self._handle_photo_message(message)
                    if not message.get("text"):
                        log.info(
                            "Photo message from %s — download failed",
                            display_name,
                        )
                        continue

                # ── Direct command interception (fast path, no LLM) ──
                text = (message.get("text") or "").strip()
                if text.startswith("/"):
                    parts = text.split(None, 1)
                    raw_cmd = parts[0].lower()
                    args = parts[1] if len(parts) > 1 else ""
                    # Strip @bot suffix from command (/help@Cleandust_MemberNo3_bot)
                    cmd_name = raw_cmd.split("@")[0].lstrip("/")
                    if self._handle_direct_command(chat_id, cmd_name, args, message):
                        continue  # Handled directly, skip LLM

                # Show typing indicator
                self._send_typing(chat_id)

                # Normalize and route
                unified = self._normalize_message(message)

                if self._on_message:
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                self._on_message(unified), loop,
                            )
                        else:
                            loop.run_until_complete(self._on_message(unified))
                    except RuntimeError:
                        # No event loop in this thread; run in a new one
                        asyncio.run(self._on_message(unified))
                else:
                    log.debug("No message handler registered")

            time.sleep(self._poll_interval)

        log.info("Telegram poll loop exited")
