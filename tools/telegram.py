"""ww/tools/telegram.py — Telegram Gateway tool

WW directly via Telegram Bot API send message, image, video.
No need to go through MQTT or Hermes bridge.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

import requests


TELEGRAM_API = "https://api.telegram.org/bot"


class TelegramPublisher:
    """WW's Telegram publisher. Supports text/image/video/schedule publishing."""

    def __init__(self, token: Optional[str] = None):
        if token is None:
            token = os.environ.get("TELEGRAM_WW_TOKEN", "")
        self.token = token
        self.base_url = f"{TELEGRAM_API}{token}"
        self._me = None

    def is_configured(self) -> bool:
        return bool(self.token)

    def verify(self) -> Dict[str, Any]:
        """Validate whether bot token is valid."""
        if not self.token:
            return {"ok": False, "error": "No token configured"}
        try:
            resp = requests.get(f"{self.base_url}/getMe", timeout=10)
            data = resp.json()
            if data.get("ok"):
                self._me = data["result"]
            return data
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_updates(self, offset: int = 0, timeout: int = 30) -> Dict[str, Any]:
        """Get bot received updates (for discovering group chat_id)."""
        params = {"offset": offset, "timeout": timeout}
        try:
            resp = requests.get(f"{self.base_url}/getUpdates", params=params, timeout=timeout + 5)
            return resp.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send_message(self, chat_id: str, text: str,
                     parse_mode: str = "Markdown",
                     disable_preview: bool = True) -> Dict[str, Any]:
        """Send text message to specified group/channel.

        Args:
            chat_id: Telegram chat ID (or @username)
            text: message text (supports Markdown)
            parse_mode: Markdown or HTML
            disable_preview: whether to disable link preview
        """
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        try:
            resp = requests.post(f"{self.base_url}/sendMessage",
                                 json=params, timeout=15)
            return resp.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send_photo(self, chat_id: str, photo_url: str,
                   caption: str = "", parse_mode: str = "Markdown") -> Dict[str, Any]:
        """Send image to specified group/channel."""
        params = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        try:
            resp = requests.post(f"{self.base_url}/sendPhoto",
                                 json=params, timeout=30)
            return resp.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send_file(self, chat_id: str, file_path: str,
                  caption: str = "") -> Dict[str, Any]:
        """Send local file (document/video/audio) to Telegram.
        
        Note: file must be readable on the host where the bot is located.
        """
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}

        ext = os.path.splitext(file_path)[1].lower()
        media_types = {".mp4": "video", ".avi": "video", ".mov": "video",
                       ".mp3": "audio", ".ogg": "audio", ".wav": "audio",
                       ".png": "photo", ".jpg": "photo", ".jpeg": "photo",
                       ".gif": "document", ".pdf": "document", ".zip": "document"}

        mtype = media_types.get(ext, "document")

        try:
            with open(file_path, "rb") as f:
                files = {mtype: f}
                data = {"chat_id": chat_id, "caption": caption}
                endpoint = {
                    "photo": "sendPhoto",
                    "video": "sendVideo",
                    "audio": "sendAudio",
                    "document": "sendDocument",
                }.get(mtype, "sendDocument")
                resp = requests.post(f"{self.base_url}/{endpoint}",
                                     data=data, files=files, timeout=120)
            return resp.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send_to_workspace(self, text: str) -> Dict[str, Any]:
        """Shortcut method: send to Working Space group.
        
        Working Space group ID read from environment variable or default value.
        """
        chat_id = os.environ.get("TELEGRAM_WW_WORKSPACE", "")
        if not chat_id:
            return {"ok": False, "error": "TELEGRAM_WW_WORKSPACE not set (no working space chat_id)"}
        return self.send_message(chat_id, text)

    def broadcast_to_all(self, text: str) -> Dict[str, Any]:
        """Broadcast to all notification groups (read from WW config)."""
        groups = os.environ.get("TELEGRAM_WW_GROUPS", "")
        if not groups:
            return {"ok": False, "error": "No groups configured"}
        results = {}
        for gid in groups.split(","):
            gid = gid.strip()
            if gid:
                results[gid] = self.send_message(gid, text)
        return {"ok": True, "results": results}


# ── WW Tool Handler ────────────────────────────────────

def get_publisher() -> TelegramPublisher:
    """Get or create TelegramPublisher instance (cached at module level)."""
    token_env = os.environ.get("TELEGRAM_WW_TOKEN")
    if not hasattr(get_publisher, "_cached") or not get_publisher._cached:
        get_publisher._cached = TelegramPublisher(token=token_env)
    return get_publisher._cached


def _telegram_send_handler(params: Dict = None) -> Dict[str, Any]:
    """sendtextmessageto  Telegram. """
    pub = get_publisher()
    if not pub.is_configured():
        return {"success": False, "error": "TELEGRAM_WW_TOKEN not set"}

    params = params or {}
    chat_id = params.get("chat_id", "")
    text = params.get("message", params.get("text", ""))
    parse_mode = params.get("parse_mode", "Markdown")
    disable_preview = params.get("disable_preview", True)

    if not chat_id:
        return {"success": False, "error": "chat_id is required"}
    if not text:
        return {"success": False, "error": "message text is required"}

    result = pub.send_message(chat_id, text, parse_mode, disable_preview)
    return {"success": result.get("ok", False), "output": str(result), "data": result}


def _telegram_send_photo_handler(params: Dict = None) -> Dict[str, Any]:
    """sendimageto  Telegram. """
    pub = get_publisher()
    if not pub.is_configured():
        return {"success": False, "error": "TELEGRAM_WW_TOKEN not set"}

    params = params or {}
    chat_id = params.get("chat_id", "")
    photo = params.get("photo_url", params.get("photo", ""))
    caption = params.get("caption", "")

    if not chat_id or not photo:
        return {"success": False, "error": "chat_id and photo_url are required"}

    result = pub.send_photo(chat_id, photo, caption)
    return {"success": result.get("ok", False), "output": str(result), "data": result}


def _telegram_send_file_handler(params: Dict = None) -> Dict[str, Any]:
    """Send file to Telegram."""
    pub = get_publisher()
    if not pub.is_configured():
        return {"success": False, "error": "TELEGRAM_WW_TOKEN not set"}

    params = params or {}
    chat_id = params.get("chat_id", "")
    file_path = params.get("file_path", "")
    caption = params.get("caption", "")

    if not chat_id or not file_path:
        return {"success": False, "error": "chat_id and file_path are required"}

    result = pub.send_file(chat_id, file_path, caption)
    return {"success": result.get("ok", False), "output": str(result), "data": result}


def _telegram_verify_handler(params: Dict = None) -> Dict[str, Any]:
    """validate Telegram bot connection. """
    pub = get_publisher()
    if not pub.is_configured():
        return {"success": False, "error": "TELEGRAM_WW_TOKEN not set"}
    result = pub.verify()
    bot_info = result.get("result", {})
    out = (f"Bot: @{bot_info.get('username','?')} "
           f"({bot_info.get('first_name','?')})")
    return {"success": result.get("ok", False), "output": out, "data": result}


def register_telegram_tools(r):
    """registerall  Telegram toolto  registry. """

    r.register_from_def("telegram_verify", "validate Telegram Bot Token is valid, return bot info.",
        {},
        "platform")

    r.register_from_def("telegram_send", "sendtextmessageto  Telegram group/channel. ",
        _telegram_send_handler,
        {"chat_id": {"type": "string", "description": "Telegram chat ID (e.g., -1003841986648)"},
         "message": {"type": "string", "description": "messagecontent (supports Markdown) "},
         "parse_mode": {"type": "string", "description": "resolvemode Markdown/HTML", "default": "Markdown"},
         "disable_preview": {"type": "boolean", "description": "Disable link preview", "default": True}},
        "platform")

    r.register_from_def("telegram_send_photo", "sendimageto  Telegram. ",
        _telegram_send_photo_handler,
        {"chat_id": {"type": "string", "description": "Telegram chat ID"},
         "photo_url": {"type": "string", "description": "image URL or local path"},
         "caption": {"type": "string", "description": "image description (optional)", "default": ""}},
        "platform")

    r.register_from_def("telegram_send_file", " Send local file to Telegram.",
        _telegram_send_file_handler,
        {"chat_id": {"type": "string", "description": "Telegram chat ID"},
         "file_path": {"type": "string", "description": "Local file path"},
         "caption": {"type": "string", "description": "Description (optional)", "default": ""}},
        "platform")
