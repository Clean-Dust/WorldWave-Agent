"""
core/memory/ltm_vfs.py — Long-term memory virtual filesystem (v-next)

Dual layer:
  Content — truth on disk as Markdown/tree
  Index   — URI + vectors/meta only (no full body)

URI: primary ww:// ; optional read alias viking://

Tree:
  resources/
  user/memories/   (8 categories + policies)
  agent/{skills,memories}/
  agent/memories/dreaming/  (9th — merge-update dream outputs)

Content tiers (NOT bare "L0" storage names):
  Abstract  (~100 tok)  — .abstract.md
  Overview  (~2k tok)   — .overview.md
  Detail    (full)      — body markdown
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ww.memory.ltm_vfs")

URI_PRIMARY = "ww://"
URI_ALIAS = "viking://"

# Content tier names (avoid bare L0 collision with storage layers)
class ContentTier(str, Enum):
    ABSTRACT = "abstract"   # ~100 tokens
    OVERVIEW = "overview"   # ~2k tokens
    DETAIL = "detail"       # full


ABSTRACT_MAX_CHARS = 400   # ~100 tok
OVERVIEW_MAX_CHARS = 8000  # ~2k tok

# Update policies
POLICY_MERGE_SINGLE = "merge_single"      # profile.md
POLICY_APPEND = "append"                  # preferences/, entities/
POLICY_IMMUTABLE = "immutable"            # events/, trajectories/
POLICY_MERGE_UPDATE = "merge_update"    # experiences/, tools/, skills/, dreaming/

USER_MEMORY_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "profile": {
        "path": "user/memories/profile.md",
        "policy": POLICY_MERGE_SINGLE,
        "is_file": True,
    },
    "preferences": {
        "path": "user/memories/preferences",
        "policy": POLICY_APPEND,
        "is_file": False,
    },
    "entities": {
        "path": "user/memories/entities",
        "policy": POLICY_APPEND,
        "is_file": False,
    },
    "events": {
        "path": "user/memories/events",
        "policy": POLICY_IMMUTABLE,
        "is_file": False,
    },
    "trajectories": {
        "path": "user/memories/trajectories",
        "policy": POLICY_IMMUTABLE,
        "is_file": False,
    },
    "experiences": {
        "path": "user/memories/experiences",
        "policy": POLICY_MERGE_UPDATE,
        "is_file": False,
    },
    "tools": {
        "path": "user/memories/tools",
        "policy": POLICY_MERGE_UPDATE,
        "is_file": False,
    },
    "skills": {
        "path": "user/memories/skills",
        "policy": POLICY_MERGE_UPDATE,
        "is_file": False,
    },
}

# Ninth category for dreaming outputs
DREAMING_CATEGORY = {
    "path": "agent/memories/dreaming",
    "policy": POLICY_MERGE_UPDATE,
    "is_file": False,
}

ROOT_DIRS = (
    "resources",
    "user/memories",
    "user/memories/preferences",
    "user/memories/entities",
    "user/memories/events",
    "user/memories/trajectories",
    "user/memories/experiences",
    "user/memories/tools",
    "user/memories/skills",
    "agent/skills",
    "agent/memories",
    "agent/memories/dreaming",
)


def normalize_uri(uri: str) -> str:
    """Map viking:// → ww:// ; ensure ww:// prefix for relative paths."""
    u = (uri or "").strip()
    if u.startswith(URI_ALIAS):
        u = URI_PRIMARY + u[len(URI_ALIAS):]
    if not u.startswith(URI_PRIMARY):
        u = URI_PRIMARY + u.lstrip("/")
    return u


def uri_to_relpath(uri: str) -> str:
    u = normalize_uri(uri)
    return u[len(URI_PRIMARY):].lstrip("/")


def relpath_to_uri(rel: str) -> str:
    return URI_PRIMARY + rel.lstrip("/")


def _slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "-", (text or "").strip(), flags=re.U)
    s = s.strip("-_")[:max_len] or "item"
    return s.lower() if s.isascii() else s


def make_abstract(text: str) -> str:
    t = (text or "").strip().replace("\n", " ")
    if len(t) <= ABSTRACT_MAX_CHARS:
        return t
    return t[: ABSTRACT_MAX_CHARS - 3] + "..."


def make_overview(text: str) -> str:
    t = (text or "").strip()
    if len(t) <= OVERVIEW_MAX_CHARS:
        return t
    # Keep structure: first paragraphs + last short tail note
    head = t[: OVERVIEW_MAX_CHARS - 40]
    return head + "\n\n… [overview truncated; detail available]"


@dataclass
class IndexEntry:
    uri: str
    category: str
    abstract: str
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    content_hash: str = ""
    # No full body in index
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "category": self.category,
            "abstract": self.abstract,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
            "content_hash": self.content_hash,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IndexEntry":
        return cls(
            uri=str(d.get("uri") or ""),
            category=str(d.get("category") or ""),
            abstract=str(d.get("abstract") or ""),
            title=str(d.get("title") or ""),
            created_at=float(d.get("created_at") or time.time()),
            updated_at=float(d.get("updated_at") or time.time()),
            tags=list(d.get("tags") or []),
            content_hash=str(d.get("content_hash") or ""),
            meta=dict(d.get("meta") or {}),
        )


class ImmutableLTMError(PermissionError):
    """Raised when updating an immutable LTM category (events/trajectories)."""


class LTMVFS:
    """Content + index layers for long-term memory."""

    def __init__(self, data_dir: str = ""):
        base = data_dir or os.path.join(
            os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww")), "memory"
        )
        self.root = Path(base) / "ltm"
        self.content_root = self.root / "content"
        self.index_path = self.root / "index.json"
        self.content_root.mkdir(parents=True, exist_ok=True)
        for d in ROOT_DIRS:
            (self.content_root / d).mkdir(parents=True, exist_ok=True)
        # Ensure profile.md exists as empty merge target
        profile = self.content_root / "user" / "memories" / "profile.md"
        if not profile.exists():
            profile.write_text("# User Profile\n", encoding="utf-8")
        self._index: Dict[str, IndexEntry] = {}
        self._load_index()

    def _load_index(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            for item in data.get("entries") or []:
                e = IndexEntry.from_dict(item)
                if e.uri:
                    self._index[e.uri] = e
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("LTM index load failed: %s", e)

    def _save_index(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"entries": [e.to_dict() for e in self._index.values()]}
            self.index_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("LTM index save failed: %s", e)

    def category_policy(self, category: str) -> str:
        if category == "dreaming":
            return DREAMING_CATEGORY["policy"]
        info = USER_MEMORY_CATEGORIES.get(category)
        if not info:
            return POLICY_MERGE_UPDATE
        return str(info["policy"])

    def category_base_path(self, category: str) -> str:
        if category == "dreaming":
            return DREAMING_CATEGORY["path"]
        info = USER_MEMORY_CATEGORIES.get(category)
        if not info:
            raise ValueError(f"Unknown LTM category: {category}")
        return str(info["path"])

    def _write_tiers(self, item_dir: Path, title: str, body: str) -> Tuple[str, str, str]:
        """Write abstract/overview/detail files. Returns (abs, ov, detail paths rel)."""
        item_dir.mkdir(parents=True, exist_ok=True)
        abstract = make_abstract(body if not title else f"{title}. {body}")
        overview = make_overview(body)
        detail = body
        (item_dir / ".abstract.md").write_text(abstract, encoding="utf-8")
        (item_dir / ".overview.md").write_text(overview, encoding="utf-8")
        (item_dir / "detail.md").write_text(detail, encoding="utf-8")
        if title:
            (item_dir / "README.md").write_text(f"# {title}\n\n{abstract}\n", encoding="utf-8")
        return abstract, overview, detail

    def write(
        self,
        category: str,
        content: str,
        *,
        title: str = "",
        name: str = "",
        tags: Optional[List[str]] = None,
        force_new: bool = False,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Write content into category respecting update policy. Returns URI."""
        policy = self.category_policy(category)
        base = self.category_base_path(category)
        tags = tags or []
        meta = meta or {}
        now = time.time()

        if category == "profile" or (
            USER_MEMORY_CATEGORIES.get(category, {}).get("is_file")
        ):
            # merge single file
            path = self.content_root / base
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            if content.strip() and content.strip() not in existing:
                if existing.strip():
                    merged = existing.rstrip() + "\n\n" + content.strip() + "\n"
                else:
                    merged = f"# User Profile\n\n{content.strip()}\n"
                path.write_text(merged, encoding="utf-8")
            elif not path.exists():
                path.write_text(f"# User Profile\n\n{content.strip()}\n", encoding="utf-8")
            uri = relpath_to_uri(base)
            body = path.read_text(encoding="utf-8")
            abstract = make_abstract(body)
            # tiers as sibling files for profile
            tier_dir = path.parent / ".profile_tiers"
            self._write_tiers(tier_dir, "profile", body)
            ch = hashlib.sha256(body.encode()).hexdigest()[:16]
            self._index[uri] = IndexEntry(
                uri=uri,
                category=category,
                abstract=abstract,
                title=title or "profile",
                created_at=self._index.get(uri, IndexEntry(uri=uri, category=category, abstract="")).created_at or now,
                updated_at=now,
                tags=tags,
                content_hash=ch,
                meta=meta,
            )
            self._save_index()
            return uri

        # Directory categories
        slug = _slug(name or title or content[:40])
        item_rel = f"{base}/{slug}"
        item_dir = self.content_root / item_rel
        detail_file = item_dir / "detail.md"
        uri = relpath_to_uri(item_rel)

        if policy == POLICY_IMMUTABLE and detail_file.exists() and not force_new:
            raise ImmutableLTMError(
                f"Category '{category}' is immutable; cannot update existing URI {uri}. "
                "Write a new name or use supersede chain."
            )

        if policy == POLICY_APPEND and detail_file.exists() and not force_new:
            # Append creates a new sibling file with unique suffix
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"
            item_rel = f"{base}/{slug}"
            item_dir = self.content_root / item_rel
            uri = relpath_to_uri(item_rel)

        if policy == POLICY_MERGE_UPDATE and detail_file.exists():
            old = detail_file.read_text(encoding="utf-8")
            if content.strip() and content.strip() not in old:
                content = old.rstrip() + "\n\n---\n\n" + content.strip()

        abstract, _, body = self._write_tiers(item_dir, title or slug, content)
        ch = hashlib.sha256(body.encode()).hexdigest()[:16]
        prev = self._index.get(uri)
        self._index[uri] = IndexEntry(
            uri=uri,
            category=category,
            abstract=abstract,
            title=title or slug,
            created_at=prev.created_at if prev else now,
            updated_at=now,
            tags=tags,
            content_hash=ch,
            meta=meta,
        )
        self._save_index()
        return uri

    def update(
        self,
        uri: str,
        content: str,
        *,
        title: str = "",
        tags: Optional[List[str]] = None,
    ) -> str:
        """Update existing URI; rejects immutable categories."""
        uri = normalize_uri(uri)
        entry = self._index.get(uri)
        category = entry.category if entry else self._infer_category(uri)
        policy = self.category_policy(category)
        if policy == POLICY_IMMUTABLE:
            raise ImmutableLTMError(
                f"Cannot update immutable LTM path {uri} (category={category})"
            )
        rel = uri_to_relpath(uri)
        if category == "profile" or rel.endswith("profile.md"):
            return self.write("profile", content, title=title or "profile", tags=tags)

        item_dir = self.content_root / rel
        if not item_dir.exists() and (self.content_root / (rel + ".md")).exists():
            # single file case
            pass
        old = ""
        detail = item_dir / "detail.md"
        if detail.exists():
            old = detail.read_text(encoding="utf-8")
        if self.category_policy(category) == POLICY_MERGE_UPDATE and old:
            if content.strip() not in old:
                content = old.rstrip() + "\n\n---\n\n" + content.strip()
        abstract, _, body = self._write_tiers(
            item_dir, title or (entry.title if entry else ""), content
        )
        now = time.time()
        ch = hashlib.sha256(body.encode()).hexdigest()[:16]
        self._index[uri] = IndexEntry(
            uri=uri,
            category=category,
            abstract=abstract,
            title=title or (entry.title if entry else ""),
            created_at=entry.created_at if entry else now,
            updated_at=now,
            tags=tags if tags is not None else (entry.tags if entry else []),
            content_hash=ch,
            meta=entry.meta if entry else {},
        )
        self._save_index()
        return uri

    def _infer_category(self, uri: str) -> str:
        rel = uri_to_relpath(uri)
        if "dreaming" in rel:
            return "dreaming"
        if rel.endswith("profile.md") or rel == "user/memories/profile.md":
            return "profile"
        for cat, info in USER_MEMORY_CATEGORIES.items():
            p = str(info["path"])
            if rel == p or rel.startswith(p + "/"):
                return cat
        return "experiences"

    def read(
        self,
        uri: str,
        tier: ContentTier = ContentTier.DETAIL,
    ) -> str:
        """Read content at progressive tier."""
        uri = normalize_uri(uri)
        rel = uri_to_relpath(uri)
        path = self.content_root / rel

        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if tier == ContentTier.ABSTRACT:
                return make_abstract(text)
            if tier == ContentTier.OVERVIEW:
                return make_overview(text)
            return text

        if path.is_dir():
            if tier == ContentTier.ABSTRACT:
                p = path / ".abstract.md"
                if p.exists():
                    return p.read_text(encoding="utf-8")
            if tier == ContentTier.OVERVIEW:
                p = path / ".overview.md"
                if p.exists():
                    return p.read_text(encoding="utf-8")
            detail = path / "detail.md"
            if detail.exists():
                return detail.read_text(encoding="utf-8")
            readme = path / "README.md"
            if readme.exists():
                return readme.read_text(encoding="utf-8")

        # viking alias already normalized
        raise FileNotFoundError(f"LTM URI not found: {uri}")

    def ls(self, uri: str = "ww://") -> List[str]:
        uri = normalize_uri(uri)
        rel = uri_to_relpath(uri)
        path = self.content_root / rel if rel else self.content_root
        if not path.exists():
            return []
        if path.is_file():
            return [relpath_to_uri(rel)]
        out = []
        for child in sorted(path.iterdir()):
            if child.name.startswith(".") and child.name not in (".abstract.md", ".overview.md"):
                if child.name.startswith(".profile"):
                    continue
            child_rel = str(Path(rel) / child.name) if rel else child.name
            out.append(relpath_to_uri(child_rel.replace("\\", "/")))
        return out

    def tree(self, uri: str = "ww://", max_depth: int = 3) -> str:
        lines: List[str] = []

        def walk(rel: str, depth: int) -> None:
            if depth > max_depth:
                return
            path = self.content_root / rel if rel else self.content_root
            if not path.exists() or not path.is_dir():
                return
            for child in sorted(path.iterdir()):
                if child.name.startswith("."):
                    continue
                child_rel = f"{rel}/{child.name}" if rel else child.name
                pad = "  " * depth
                mark = "/" if child.is_dir() else ""
                lines.append(f"{pad}{child.name}{mark}")
                if child.is_dir():
                    walk(child_rel, depth + 1)

        root_rel = uri_to_relpath(normalize_uri(uri))
        lines.append(normalize_uri(uri))
        walk(root_rel, 1)
        return "\n".join(lines)

    @staticmethod
    def _entry_entity_id(entry: "IndexEntry") -> str:
        """Resolve cognitive entity for an LTM index entry (meta or entity: tag)."""
        meta = entry.meta if isinstance(entry.meta, dict) else {}
        me = str(meta.get("entity_id") or "").strip()
        if me:
            return me
        for t in entry.tags or []:
            ts = str(t)
            if ts.startswith("entity:"):
                return ts[len("entity:"):].strip()
        return ""

    def entry_belongs_to_entity(self, entry: "IndexEntry", entity_id: str) -> bool:
        """Hard partition: never surface another entity's LTM entries.

        Untagged (legacy) entries are visible only to entity ``default``.
        """
        entity = (entity_id or "").strip() or "default"
        owner = self._entry_entity_id(entry)
        if owner:
            return owner == entity
        return entity == "default"

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        tier: ContentTier = ContentTier.ABSTRACT,
        entity_id: str = "",
    ) -> List[dict]:
        """Semantic-lite search over index (keyword on abstract/title/tags).

        Progressive inject: returns Abstract by default; caller may expand.

        Always entity-scoped when ``entity_id`` is provided (Gate 0.4).
        Untagged legacy LTM is only visible to ``default``.
        """
        q = (query or "").lower().strip()
        eid = (entity_id or "").strip()
        scored: List[Tuple[float, IndexEntry]] = []
        for e in self._index.values():
            if eid and not self.entry_belongs_to_entity(e, eid):
                continue
            score = 0.0
            blob = f"{e.title} {e.abstract} {' '.join(e.tags)} {e.category}".lower()
            if not q:
                score = e.updated_at
            else:
                for term in re.findall(r"[a-z0-9_\u4e00-\u9fff]+", q):
                    if term in blob:
                        score += 1.0
                    if term in e.title.lower():
                        score += 1.5
            if score > 0 or not q:
                scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], -x[1].updated_at))
        out = []
        for score, e in scored[:top_k]:
            try:
                content = self.read(e.uri, tier=tier)
            except FileNotFoundError:
                content = e.abstract
            owner = self._entry_entity_id(e)
            out.append({
                "uri": e.uri,
                "category": e.category,
                "title": e.title,
                "score": score,
                "tier": tier.value,
                "content": content,
                "abstract": e.abstract,
                "entity_id": owner,
                "meta": dict(e.meta) if isinstance(e.meta, dict) else {},
            })
        return out

    def promote_topic(
        self,
        topic: Any,
        *,
        category: str = "experiences",
        tags: Optional[List[str]] = None,
        entity_id: str = "",
    ) -> str:
        """Write a promoted topic into LTM as experiences (default).

        Stamps ``meta.entity_id`` so abstract search cannot leak across
        cognitive entities (Gate 0.4 sequential isolation).
        """
        title = getattr(topic, "title", "") or getattr(topic, "topic_id", "topic")
        if hasattr(topic, "full_text"):
            body = topic.full_text()
        else:
            body = str(topic)
        tid = getattr(topic, "topic_id", "") or ""
        # Resolve entity: explicit > topic.meta > topic.entities membership
        eid = (entity_id or "").strip()
        if not eid:
            tmeta = getattr(topic, "meta", None) or {}
            if isinstance(tmeta, dict):
                eid = str(tmeta.get("entity_id") or "").strip()
        if not eid:
            ents = list(getattr(topic, "entities", None) or [])
            # Prefer non-key-like entity ids (beam_mini_*, ent_*, default)
            for e in ents:
                es = str(e).strip()
                if es and (es == "default" or "_" in es or es.startswith("ent")):
                    eid = es
                    break
        tag_list = list(tags or ["promoted_topic"])
        if eid and f"entity:{eid}" not in tag_list:
            tag_list.append(f"entity:{eid}")
        meta: Dict[str, Any] = {
            "topic_id": tid,
            "source": "hippo_promote",
        }
        if eid:
            meta["entity_id"] = eid
        return self.write(
            category,
            body,
            title=str(title)[:80],
            name=f"topic-{tid[:12]}" if tid else "",
            tags=tag_list,
            meta=meta,
        )

    def stats(self) -> dict:
        by_cat: Dict[str, int] = {}
        for e in self._index.values():
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
        return {
            "entries": len(self._index),
            "by_category": by_cat,
            "root": str(self.root),
        }
