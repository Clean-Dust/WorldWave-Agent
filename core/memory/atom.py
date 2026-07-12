"""
ww/core/memory/atom.py — Memory atom + entity resolution + Fact Store

MemoryAtom: Smallest memory unit, containing timestamp, entity link, confidence
EntityResolver: Normalize multiple spellings, resolve pronouns, group synonyms
FactStore: Fact-based query layer, supports entity detection, relational inference
"""

from __future__ import annotations
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from typing import Dict, List, Optional, Set

logger = logging.getLogger("ww.memory.atom")

_WW_CFG = os.environ.get("WW_CONFIG", os.path.expanduser("~/.ww"))
MEMORY_DIR = os.path.join(_WW_CFG, "memory")


# ── Memory atom ──


class MemoryAtom:
    """
    Smallest memory unit.

    Each atom represents an indivisible memory fragment:
    - A conversation summary
    - A tool call result
    - A learned fact
    - An error/exception experience
    """

    def __init__(
        self,
        content: str,
        atom_id: str = "",
        atom_type: str = "episodic",  # episodic | semantic | procedural
        entities: Optional[List[str]] = None,
        emotion: float = 0.0,  # 0.0=neutral, negative=negative, positive=positive
        importance: float = 0.5,  # 0.0-1.0
        timestamp: float = 0,
        source: str = "",  # "user", "system", "tool", "inference"
        tags: Optional[List[str]] = None,
        context_id: str = "",  # Belonging spiral ID
        # ── Temporal validity (P1: Entity continuity) ──
        valid_from: float = 0,  # When this fact became true (epoch)
        valid_until: float = 0,  # When this fact stopped being true (0 = still valid)
        superseded_by: str = "",  # atom_id that supersedes this one
    ):
        self.atom_id = atom_id or uuid.uuid4().hex[:16]
        self.content = content
        self.atom_type = atom_type
        self.entities = entities or []
        self.emotion = emotion
        self.importance = importance
        self.timestamp = timestamp or time.time()
        self.source = source
        self.tags = tags or []
        self.context_id = context_id

        # ── Temporal validity ──
        self.valid_from = valid_from or self.timestamp
        self.valid_until = valid_until  # 0 = still valid
        self.superseded_by = superseded_by  # atom_id that replaces this

        # Link trace (maintained by Amygdala/Sleep)
        self.links: Dict[str, float] = {}  # target_atom_id -> link_strength
        self.recall_count: int = 0
        self.last_recalled: float = 0
        self.stability: float = 1.0  # 1.0=just formed, higher=more stable (not easily pruned)
        self.context_trace: List[Dict] = []  # Recall context trace (Reconsolidation write)
        self.is_core: bool = False  # True=cannot be forgotten/overwritten/pruned
        self.is_archived: bool = False  # True=semantically merged and archived, recall skips by default
        self.is_immutable: bool = False  # True=code memory, NEVER pruned/abstracted/scored

        # ── Multimodal data ──
        self.visual_data: Optional[Dict] = None
        """Visual memory data. Structure:
        {
            "screenshot_path": str,      # screenshot path (if has)
            "ui_elements": [              # Key UI elements
                {"label": str, "bbox": [x1,y1,x2,y2], "tag": str},
            ],
            "region_of_interest": {       # Area of interest
                "x": int, "y": int, "w": int, "h": int
            },
            "screen_size": {"w": int, "h": int},  # screen resolution
        }
        """
        self.spatial_markers: Optional[List[Dict]] = None
        """Spatial marker — non-compressible coordinate reference point.
        [{"name": str, "x": int, "y": int, "context": str}, ...]
        Example: [{"name": "Chrome address bar", "x": 300, "y": 50, "context": "browser_top"}]
        """

    @property
    def is_currently_valid(self) -> bool:
        """True if this fact is still valid (not expired, not superseded)."""
        if self.superseded_by:
            return False
        if self.valid_until and time.time() > self.valid_until:
            return False
        return True

    def to_dict(self) -> dict:
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
            # Temporal validity
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

    def set_visual_memory(self, screenshot_path: str = "",
                          ui_elements: Optional[List[Dict]] = None,
                          region: Optional[Dict] = None,
                          screen_size: Optional[Dict] = None):
        """Store visual memory data (dedicated for Computer Use Vision Loop)."""
        data = {}
        if screenshot_path:
            data["screenshot_path"] = screenshot_path
        if ui_elements:
            data["ui_elements"] = ui_elements
        if region:
            data["region_of_interest"] = region
        if screen_size:
            data["screen_size"] = screen_size
        self.visual_data = data if data else None

    def add_spatial_marker(self, name: str, x: int, y: int,
                           context: str = ""):
        """Add spatial marker (mouse anchor point / button coordinates)."""
        if self.spatial_markers is None:
            self.spatial_markers = []
        self.spatial_markers.append({
            "name": name, "x": x, "y": y, "context": context,
        })
        # Limit marker count to prevent bloat
        if len(self.spatial_markers) > 20:
            self.spatial_markers = self.spatial_markers[-20:]

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryAtom":
        atom = cls(
            content=d.get("content", ""),
            atom_id=d.get("atom_id", ""),
            atom_type=d.get("atom_type", "episodic"),
            entities=d.get("entities", []),
            emotion=d.get("emotion", 0.0),
            importance=d.get("importance", 0.5),
            timestamp=d.get("timestamp", 0),
            source=d.get("source", ""),
            tags=d.get("tags", []),
            context_id=d.get("context_id", ""),
            valid_from=d.get("valid_from", 0),
            valid_until=d.get("valid_until", 0),
            superseded_by=d.get("superseded_by", ""),
        )
        atom.links = d.get("links", {})
        atom.recall_count = d.get("recall_count", 0)
        atom.last_recalled = d.get("last_recalled", 0)
        atom.stability = d.get("stability", 1.0)
        atom.context_trace = d.get("context_trace", [])
        atom.is_core = d.get("is_core", False)
        atom.is_archived = d.get("is_archived", False)
        atom.is_immutable = d.get("is_immutable", False)
        atom.visual_data = d.get("visual_data")
        atom.spatial_markers = d.get("spatial_markers")
        return atom


# ── entityresolve  ──


class EntityResolver:
    """
    entityresolve . 

    process: 
    - Normalize multiple spellings ("FastAPI" == "fastapi" == "Fast Api")
    - pronoun resolve (simple rule)
    - synonym grouping
    - entitytypeclassification
    """

    # common technical term normalization mapping
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

    # entity type keywords
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
        """normalizeentityname. """
        key = raw.strip().lower()
        return self.NORMALIZE_MAP.get(key, key)

    def classify(self, entity: str) -> str:
        """classificationentitytype. """
        norm = self.normalize(entity)
        if norm in self._entity_types:
            return self._entity_types[norm]
        for etype, keywords in self.TYPE_KEYWORDS.items():
            if norm in keywords:
                self._entity_types[norm] = etype
                return etype
        return "unknown"

    def extract(self, text: str) -> List[Dict[str, str]]:
        """
        extract entities from text.

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
    fact save + entity trust score.

    similar to a lightweight knowledge graph:
    - each fact  has  content, entities, trust_score
    - supports entity detection (find all facts about a certain entity)
    - supports relational inference (which facts share a certain entity)
    - trust_score auto-adjusts with number of validations
    """

    def __init__(self, data_dir: str = ""):
        self.data_dir = data_dir or MEMORY_DIR
        self._facts: Dict[int, dict] = {}  # fact_id -> fact
        self._entity_index: Dict[str, Set[int]] = defaultdict(set)  # entity -> fact_ids
        self._next_id: int = 0
        self._loaded = False

    def _ensure_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)

    def _path(self) -> str:
        return os.path.join(self.data_dir, "facts.json")

    def load(self):
        if self._loaded:
            return
        self._ensure_dir()
        if os.path.exists(self._path()):
            try:
                with open(self._path()) as f:
                    data = json.load(f)
                    self._facts = {int(k): v for k, v in data.get("facts", {}).items()}
                    self._next_id = data.get("next_id", len(self._facts))
                    self._rebuild_index()
                    logger.info(f"FactStore loaded: {len(self._facts)} facts")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"FactStore load failed: {e}")
        self._loaded = True

    def save(self):
        self._ensure_dir()
        data = {
            "next_id": self._next_id,
            "facts": self._facts,
        }
        with open(self._path(), "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _rebuild_index(self):
        self._entity_index.clear()
        for fid, fact in self._facts.items():
            for ent in fact.get("entities", []):
                self._entity_index[ent].add(fid)

    def add(self, content: str, entities: Optional[List[str]] = None,
            category: str = "general", tags: Optional[List[str]] = None) -> int:
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
            "trust_score": 0.5,  # default value
            "created_at": time.time(),
            "last_accessed": time.time(),
            "access_count": 0,
        }
        self._facts[fid] = fact
        for ent in fact["entities"]:
            self._entity_index[ent].add(fid)
        self.save()
        return fid

    def probe(self, entity: str, min_trust: float = 0.3,
              include_archived: bool = False) -> List[dict]:
        """
        Detect entity: find all facts about a certain entity.

        Args:
            entity: entityname
            min_trust: minimum trust filter
            include_archived: whether to include archived facts

        Returns:
            fact list matching conditions
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
        """
        Inference: find facts involving the same and multiple entities.

        Args:
            entities: entitylist
            min_trust: minimum trust filter

        Returns:
            intersection facts
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
        """Search facts by keywords."""
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
        """Adjust fact trust score."""
        self.load()
        fact = self._facts.get(fact_id)
        if fact:
            fact["trust_score"] = max(0.0, min(1.0, fact["trust_score"] + delta))
            self.save()

    def set_archived(self, fact_id: int, archived: bool = True):
        """Mark fact as archived (semantic merging, prevent reprocessing or retrieval)."""
        self.load()
        fact = self._facts.get(fact_id)
        if fact:
            fact["is_archived"] = archived
            self.save()

    def stats(self) -> dict:
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
