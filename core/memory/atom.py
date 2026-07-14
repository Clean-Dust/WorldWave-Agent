"""
ww/core/memory/atom.py — Memory atom + entity resolution + Fact Store

MemoryAtom: Smallest memory unit, containing timestamp, entity link, confidence.
            Now a Pydantic BaseModel for type safety and auto-serialization.
EntityResolver: Normalize multiple spellings, resolve pronouns, group synonyms.
FactStore: Fact-based query layer, supports entity detection, relational inference.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger("ww.memory.atom")

_WW_CFG = Path(os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww")))
MEMORY_DIR = _WW_CFG / "memory"


# ── Memory atom ──


class MemoryAtom(BaseModel):
    """
    Smallest memory unit — a Pydantic model for type safety and serialization.

    Each atom represents an indivisible memory fragment:
    - A conversation summary
    - A tool call result
    - A learned fact
    - An error/exception experience
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Core identity ──
    content: str
    atom_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    atom_type: str = Field(default="episodic", description="episodic | semantic | procedural")

    # ── Metadata ──
    entities: List[str] = Field(default_factory=list)
    emotion: float = Field(default=0.0, ge=-1.0, le=1.0, description="Negative→neutral→positive")
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    timestamp: float = Field(default_factory=time.time)
    source: str = Field(default="", description="user | system | tool | inference")
    tags: List[str] = Field(default_factory=list)
    context_id: str = Field(default="", description="Belonging spiral ID")

    # ── Temporal validity (entity continuity) ──
    valid_from: float = Field(default=0.0, description="When this fact became true (epoch)")
    valid_until: float = Field(default=0.0, description="When stopped being true (0 = still valid)")
    superseded_by: str = Field(default="", description="atom_id that supersedes this one")

    # ── Link trace (maintained by Amygdala/Sleep) ──
    links: Dict[str, float] = Field(default_factory=dict)
    recall_count: int = 0
    last_recalled: float = 0.0
    stability: float = Field(default=1.0, ge=0.0, description="Higher = harder to prune, more stable")
    context_trace: List[Dict] = Field(default_factory=list, description="Recall context trace (reconsolidation)")
    is_core: bool = Field(default=False, description="Cannot be forgotten/overwritten/pruned")
    is_archived: bool = Field(default=False, description="Semantically merged, recall skips by default")
    is_immutable: bool = Field(default=False, description="Code memory, NEVER pruned/abstracted/scored")

    # ── Multimodal data ──
    visual_data: Optional[Dict] = Field(default=None, description="Visual memory (screenshots, UI elements, regions)")
    spatial_markers: Optional[List[Dict]] = Field(default=None, description="Non-compressible coordinate reference points")

    @model_validator(mode="after")
    def _normalize_timestamps(self):
        """Use current time when timestamp/valid_from are unset (0)."""
        if self.timestamp == 0.0:
            self.timestamp = time.time()
        if self.valid_from == 0.0:
            self.valid_from = self.timestamp
        return self

    @property
    def is_currently_valid(self) -> bool:
        """True if this fact is still valid (not expired, not superseded)."""
        if self.superseded_by:
            return False
        if self.valid_until and time.time() > self.valid_until:
            return False
        return True

    # ── Serialization (backward-compatible) ──

    def to_dict(self) -> dict:
        """Serialize to dict (backward-compatible wrapper around model_dump).

        Preserves existing behavior: content truncated to 500 chars, floats rounded to 4dp.
        context_trace intentionally excluded from serialization.
        """
        return {
            "atom_id": self.atom_id,
            "content": self.content[:500],
            "atom_type": self.atom_type,
            "entities": self.entities,
            "emotion": round(self.emotion, 4),
            "importance": round(self.importance, 4),
            "timestamp": self.timestamp,
            "source": self.source,
            "tags": self.tags,
            "context_id": self.context_id,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "superseded_by": self.superseded_by,
            "links": {k: round(v, 4) for k, v in self.links.items()},
            "recall_count": self.recall_count,
            "last_recalled": self.last_recalled,
            "stability": round(self.stability, 4),
            "is_core": self.is_core,
            "is_archived": self.is_archived,
            "is_immutable": self.is_immutable,
            "visual_data": self.visual_data,
            "spatial_markers": self.spatial_markers,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryAtom":
        """Deserialize from dict (backward-compatible wrapper around model_validate).

        Pre-fills defaults so old serialized data with missing fields loads cleanly.
        """
        defaults: dict = {
            "content": "",
            "atom_type": "episodic",
            "entities": [],
            "emotion": 0.0,
            "importance": 0.5,
            "timestamp": time.time(),
            "source": "",
            "tags": [],
            "context_id": "",
            "valid_from": 0.0,
            "valid_until": 0.0,
            "superseded_by": "",
            "links": {},
            "recall_count": 0,
            "last_recalled": 0.0,
            "stability": 1.0,
            "context_trace": [],
            "is_core": False,
            "is_archived": False,
            "is_immutable": False,
        }
        merged = {**defaults, **d}
        return cls.model_validate(merged)

    # ── Visual / spatial helpers ──

    def set_visual_memory(
        self,
        screenshot_path: str = "",
        ui_elements: Optional[List[Dict]] = None,
        region: Optional[Dict] = None,
        screen_size: Optional[Dict] = None,
    ):
        """Store visual memory data (dedicated for Computer Use Vision Loop)."""
        data: dict = {}
        if screenshot_path:
            data["screenshot_path"] = screenshot_path
        if ui_elements:
            data["ui_elements"] = ui_elements
        if region:
            data["region_of_interest"] = region
        if screen_size:
            data["screen_size"] = screen_size
        self.visual_data = data if data else None

    def add_spatial_marker(self, name: str, x: int, y: int, context: str = ""):
        """Add spatial marker (mouse anchor point / button coordinates)."""
        if self.spatial_markers is None:
            self.spatial_markers = []
        self.spatial_markers.append({
            "name": name, "x": x, "y": y, "context": context,
        })
        # Limit marker count to prevent bloat
        if len(self.spatial_markers) > 20:
            self.spatial_markers = self.spatial_markers[-20:]

    # ── Debug ──

    def summary(self) -> str:
        """One-line summary for debugging and log output."""
        preview = self.content[:80].replace("\n", " ")
        status = (
            "archived" if self.is_archived
            else "superseded" if self.superseded_by
            else "valid" if self.is_currently_valid
            else "expired"
        )
        return (
            f"MemoryAtom(id={self.atom_id[:12]}, "
            f"type={self.atom_type}, "
            f"status={status}, "
            f"imp={self.importance:.2f}, "
            f"content='{preview}…')"
        )

    def __repr__(self) -> str:
        return self.summary()


# ── entity resolve ──


class EntityResolver:
    """
    Entity resolver for normalizing and classifying entities.

    Handles:
    - Normalize multiple spellings ("FastAPI" == "fastapi" == "Fast Api")
    - Pronoun resolution (simple rule)
    - Synonym grouping
    - Entity type classification
    """

    # Common technical term normalization mapping
    NORMALIZE_MAP = {
        "fast api": "fastapi",
        "fastapi": "fastapi",
        "sql lite": "sqlite",
        "sqlite3": "sqlite",
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "js": "javascript",
        "javascript": "javascript",
        "ts": "typescript",
        "py": "python",
        "python3": "python",
    }

    # Entity type keywords
    TYPE_KEYWORDS: Dict[str, Set[str]] = {
        "library": {"fastapi", "flask", "django", "sqlite", "postgresql",
                     "redis", "pandas", "numpy", "pytorch", "tensorflow"},
        "language": {"python", "javascript", "typescript", "rust", "go",
                      "java", "cpp", "c++"},
        "tool": {"git", "docker", "kubernetes", "vscode", "vim"},
        "concept": {"dependency injection", "rest api", "orm", "mvc"},
    }

    def __init__(self):
        self._known_entities: Dict[str, str] = {}  # spelling -> standard name
        self._entity_types: Dict[str, str] = {}    # standard name -> type

    def normalize(self, raw: str) -> str:
        """Normalize entity name to canonical form."""
        key = raw.strip().lower()
        return self.NORMALIZE_MAP.get(key, key)

    def classify(self, entity: str) -> str:
        """Classify entity type (library, language, tool, concept, unknown)."""
        norm = self.normalize(entity)
        if norm in self._entity_types:
            return self._entity_types[norm]
        for etype, keywords in self.TYPE_KEYWORDS.items():
            if norm in keywords:
                self._entity_types[norm] = etype
                return etype
        return "unknown"

    def extract(self, text: str) -> List[Dict[str, str]]:
        """Extract entities from text.

        Returns:
            [{"name": "FastAPI", "type": "library", "normalized": "fastapi"}, ...]
        """
        found = []
        words = text.split()
        for word in words:
            clean = word.strip(".,!?;:\"'()[]{}")
            if not clean:
                continue
            norm = self.normalize(clean)
            if norm in self.NORMALIZE_MAP or len(clean) > 3:
                if clean[0].isupper() or norm in self.NORMALIZE_MAP:
                    atype = self.classify(norm)
                    found.append({
                        "name": clean,
                        "type": atype,
                        "normalized": norm,
                    })
        return found


# ── Fact Store (holographic memory) ──


class FactStore:
    """
    Fact storage with entity indexing and trust scoring.

    Lightweight knowledge graph:
    - Each fact has content, entities, trust_score
    - Entity detection: find all facts about a certain entity
    - Relational inference: which facts share a certain entity
    - trust_score auto-adjusts with number of validations
    """

    def __init__(self, data_dir: str = ""):
        self.data_dir = Path(data_dir) if data_dir else MEMORY_DIR
        self._facts: Dict[int, dict] = {}
        self._entity_index: Dict[str, Set[int]] = defaultdict(set)
        self._next_id: int = 0
        self._loaded = False

    def _ensure_dir(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _path(self) -> Path:
        return self.data_dir / "facts.json"

    def load(self):
        if self._loaded:
            return
        self._ensure_dir()
        path = self._path()
        if path.is_file():
            try:
                data = json.loads(path.read_text())
                self._facts = {int(k): v for k, v in data.get("facts", {}).items()}
                self._next_id = data.get("next_id", len(self._facts))
                self._rebuild_index()
                logger.info("FactStore loaded: %d facts", len(self._facts))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("FactStore load failed: %s", e)
        self._loaded = True

    def save(self):
        self._ensure_dir()
        data = {
            "next_id": self._next_id,
            "facts": self._facts,
        }
        self._path().write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def _rebuild_index(self):
        self._entity_index.clear()
        for fid, fact in self._facts.items():
            for ent in fact.get("entities", []):
                self._entity_index[ent].add(fid)

    def add(
        self,
        content: str,
        entities: Optional[List[str]] = None,
        category: str = "general",
        tags: Optional[List[str]] = None,
    ) -> int:
        """Add a new fact."""
        self.load()
        fid = self._next_id
        self._next_id += 1
        fact = {
            "id": fid,
            "content": content,
            "entities": entities or [],
            "category": category,
            "tags": tags or [],
            "trust_score": 0.5,
            "created_at": time.time(),
            "last_accessed": time.time(),
            "access_count": 0,
        }
        self._facts[fid] = fact
        for ent in fact["entities"]:
            self._entity_index[ent].add(fid)
        self.save()
        return fid

    def probe(
        self,
        entity: str,
        min_trust: float = 0.3,
        include_archived: bool = False,
    ) -> List[dict]:
        """Find all facts about a given entity.

        Args:
            entity: Entity name.
            min_trust: Minimum trust filter.
            include_archived: Whether to include archived facts.

        Returns:
            Matching facts sorted by trust_score descending.
        """
        self.load()
        fids = self._entity_index.get(entity, set())
        results = []
        for fid in fids:
            fact = self._facts.get(fid)
            if not fact or fact["trust_score"] < min_trust:
                continue
            if not include_archived and fact.get("is_archived"):
                continue
            fact["last_accessed"] = time.time()
            fact["access_count"] += 1
            results.append(dict(fact))
        self.save()
        return sorted(results, key=lambda f: f["trust_score"], reverse=True)

    def reason(self, entities: List[str], min_trust: float = 0.3) -> List[dict]:
        """Find facts involving all of the given entities (intersection).

        Args:
            entities: Entity list.
            min_trust: Minimum trust filter.

        Returns:
            Intersection facts sorted by trust_score descending.
        """
        if not entities:
            return []
        self.load()
        sets = [self._entity_index.get(e, set()) for e in entities]
        common = set.intersection(*sets) if len(sets) > 1 else sets[0]
        results = []
        for fid in common:
            fact = self._facts.get(fid)
            if fact and fact["trust_score"] >= min_trust:
                results.append(dict(fact))
        return sorted(results, key=lambda f: f["trust_score"], reverse=True)

    def search(self, query: str, limit: int = 10) -> List[dict]:
        """Search facts by keyword (content or entity name)."""
        self.load()
        q = query.lower()
        results = []
        for fact in self._facts.values():
            if q in fact["content"].lower():
                results.append(dict(fact))
            elif any(q in e.lower() for e in fact["entities"]):
                results.append(dict(fact))
        results.sort(key=lambda f: f["trust_score"], reverse=True)
        return results[:limit]

    def update_trust(self, fact_id: int, delta: float):
        """Adjust fact trust score by delta (clamped to [0, 1])."""
        self.load()
        fact = self._facts.get(fact_id)
        if fact:
            fact["trust_score"] = max(0.0, min(1.0, fact["trust_score"] + delta))
            self.save()

    def set_archived(self, fact_id: int, archived: bool = True):
        """Mark fact as archived (semantic merging, prevents reprocessing)."""
        self.load()
        fact = self._facts.get(fact_id)
        if fact:
            fact["is_archived"] = archived
            self.save()

    def stats(self) -> dict:
        """Return summary statistics."""
        self.load()
        by_category = defaultdict(int)
        for fact in self._facts.values():
            by_category[fact["category"]] += 1
        return {
            "total": len(self._facts),
            "by_category": dict(by_category),
            "avg_trust": round(
                sum(f["trust_score"] for f in self._facts.values()) / max(1, len(self._facts)),
                3,
            ),
        }
