"""BEAM data loading from local cache (sparse JSON, no pickle).

Default cache: ``~/.ww/beam_cache`` or env ``WW_BEAM_DATA``.
Expected layout (official / HF clone)::

    chats/<scale>/<chat_id>/chat.json
    chats/<scale>/<chat_id>/probing_questions/probing_questions.json

Clone example (document only; runner does not auto-download secrets)::

    # HuggingFace / GitHub official BEAM release into cache:
    git clone <BEAM-data-repo> ~/.ww/beam_cache
    # or: export WW_BEAM_DATA=/path/to/BEAM-data
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCALES = ("100K", "500K", "1M")

# Official ability keys observed in probing_questions.json
ABILITY_KEYS = (
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
)


def default_data_root() -> Path:
    env = (os.environ.get("WW_BEAM_DATA") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".ww" / "beam_cache").resolve()


def resolve_data_root(root: Optional[str | Path] = None) -> Path:
    if root is not None and str(root).strip():
        return Path(root).expanduser().resolve()
    # Prefer WW_BEAM_DATA / cache; fall back to common local smoke path
    primary = default_data_root()
    if (primary / "chats").is_dir():
        return primary
    fallback = Path("/tmp/BEAM-data")
    if (fallback / "chats").is_dir():
        return fallback
    return primary


@dataclass
class ProbeItem:
    ability: str
    index: int
    question: str
    ideal: str = ""
    rubric: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BeamChat:
    scale: str
    chat_id: str
    path: Path
    turns: List[Dict[str, str]] = field(default_factory=list)
    probes: List[ProbeItem] = field(default_factory=list)
    topic: Any = None


def _flatten_turns(raw: Any) -> List[Dict[str, str]]:
    """Normalize chat.json into [{role, content}, ...].

    Official BEAM chat.json is typically a list of batches, each with
    ``turns`` = list of [user_msg, assistant_msg] pairs (dicts with role/content).
    """
    out: List[Dict[str, str]] = []

    def _add(role: str, content: str) -> None:
        role = (role or "").strip().lower() or "user"
        content = (content or "").strip()
        if not content:
            return
        if role not in ("user", "assistant", "system"):
            role = "user"
        out.append({"role": role, "content": content})

    if raw is None:
        return out
    if isinstance(raw, dict):
        if "messages" in raw:
            raw = raw["messages"]
        elif "turns" in raw:
            raw = raw["turns"]
        elif "batches" in raw:
            raw = raw["batches"]
        else:
            # single message?
            if "role" in raw and "content" in raw:
                _add(str(raw["role"]), str(raw["content"]))
                return out
            raw = [raw]

    if not isinstance(raw, list):
        return out

    for item in raw:
        if isinstance(item, dict) and "turns" in item:
            # batch wrapper
            for pair in item.get("turns") or []:
                if isinstance(pair, list):
                    for msg in pair:
                        if isinstance(msg, dict):
                            _add(
                                str(msg.get("role") or "user"),
                                str(msg.get("content") or msg.get("text") or ""),
                            )
                elif isinstance(pair, dict):
                    _add(
                        str(pair.get("role") or "user"),
                        str(pair.get("content") or pair.get("text") or ""),
                    )
            continue
        if isinstance(item, list):
            for msg in item:
                if isinstance(msg, dict):
                    _add(
                        str(msg.get("role") or "user"),
                        str(msg.get("content") or msg.get("text") or ""),
                    )
            continue
        if isinstance(item, dict):
            if "role" in item or "content" in item:
                _add(
                    str(item.get("role") or "user"),
                    str(item.get("content") or item.get("text") or ""),
                )
    return out


def _load_probes(path: Path) -> List[ProbeItem]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items: List[ProbeItem] = []
    if not isinstance(data, dict):
        return items
    for ability, entries in data.items():
        if not isinstance(entries, list):
            continue
        for i, ent in enumerate(entries):
            if not isinstance(ent, dict):
                continue
            q = str(ent.get("question") or ent.get("query") or "").strip()
            if not q:
                continue
            ideal = str(
                ent.get("ideal_response")
                or ent.get("ideal_answer")
                or ent.get("answer")
                or ""
            ).strip()
            rubric = ent.get("rubric") or []
            if not isinstance(rubric, list):
                rubric = [str(rubric)]
            items.append(
                ProbeItem(
                    ability=str(ability),
                    index=i,
                    question=q,
                    ideal=ideal,
                    rubric=[str(r) for r in rubric],
                    meta={
                        k: v
                        for k, v in ent.items()
                        if k
                        not in (
                            "question",
                            "query",
                            "ideal_response",
                            "ideal_answer",
                            "answer",
                            "rubric",
                        )
                    },
                )
            )
    return items


def list_chat_ids(scale: str, root: Optional[str | Path] = None) -> List[str]:
    scale = scale.strip()
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {SCALES}, got {scale!r}")
    base = resolve_data_root(root) / "chats" / scale
    if not base.is_dir():
        return []
    ids = []
    for p in sorted(base.iterdir(), key=lambda x: (not x.name.isdigit(), x.name)):
        if p.is_dir() and (p / "chat.json").is_file():
            ids.append(p.name)
    return ids


def load_chat(
    scale: str,
    chat_id: str | int,
    root: Optional[str | Path] = None,
) -> BeamChat:
    scale = scale.strip()
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {SCALES}, got {scale!r}")
    cid = str(chat_id).strip()
    chat_dir = resolve_data_root(root) / "chats" / scale / cid
    chat_path = chat_dir / "chat.json"
    if not chat_path.is_file():
        raise FileNotFoundError(f"missing chat.json: {chat_path}")
    raw = json.loads(chat_path.read_text(encoding="utf-8"))
    turns = _flatten_turns(raw)
    probes = _load_probes(chat_dir / "probing_questions" / "probing_questions.json")
    topic = None
    topic_path = chat_dir / "topic.json"
    if topic_path.is_file():
        try:
            topic = json.loads(topic_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            topic = None
    return BeamChat(
        scale=scale,
        chat_id=cid,
        path=chat_dir,
        turns=turns,
        probes=probes,
        topic=topic,
    )


def chat_text_blob(chat: BeamChat, max_chars: int = 0) -> str:
    """Concatenate turns for context-only / RAG baselines."""
    parts = []
    for t in chat.turns:
        parts.append(f"{t['role'].upper()}: {t['content']}")
    blob = "\n\n".join(parts)
    if max_chars and max_chars > 0 and len(blob) > max_chars:
        # Keep tail (recent context) for long chats
        return blob[-max_chars:]
    return blob
