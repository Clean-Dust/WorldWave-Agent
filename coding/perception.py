"""coding/perception.py — Repo map + grep for coding agents.

coding_repo_map: signature-level map ranked by graph degree/importance
coding_grep: ripgrep if available else grep -R
coding_outline: file symbol outline with line numbers
coding_explain_failure: traceback → short bullets
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


SKIP_DIRS = {
    ".git", "__pycache__", ".ww", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
}


def _estimate_tokens(text: str) -> int:
    # Rough: ~4 chars per token
    return max(1, len(text) // 4)


# ── Repo map ──────────────────────────────────────────────────────────

def _signature_of(node: ast.AST, source_lines: List[str]) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = []
        for a in node.args.args:
            args.append(a.arg)
        sig = f"def {node.name}({', '.join(args)})"
        if node.returns:
            try:
                sig += f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass
        return sig
    if isinstance(node, ast.ClassDef):
        bases = []
        for b in node.bases:
            try:
                bases.append(ast.unparse(b))
            except Exception:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
        base_s = f"({', '.join(bases)})" if bases else ""
        return f"class {node.name}{base_s}"
    return getattr(node, "name", "?")


def collect_signatures(root_dir: str = ".", max_files: int = 500) -> List[Dict]:
    """Collect function/class signatures across the tree."""
    root = Path(root_dir).resolve()
    items: List[Dict] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            fpath = Path(dirpath) / fn
            count += 1
            if count > max_files:
                return items
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
                lines = text.splitlines()
            except (SyntaxError, OSError):
                continue
            rel = str(fpath.relative_to(root)) if fpath.is_relative_to(root) else str(fpath)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    items.append({
                        "file": rel,
                        "name": node.name,
                        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                        "lineno": node.lineno,
                        "signature": _signature_of(node, lines),
                        "degree": 0,
                    })
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            items.append({
                                "file": rel,
                                "name": f"{node.name}.{child.name}",
                                "kind": "method",
                                "lineno": child.lineno,
                                "signature": f"  {_signature_of(child, lines)}",
                                "degree": 0,
                            })
    return items


def repo_map(root_dir: str = ".", token_budget: int = 4000, force_graph: bool = True) -> Dict:
    """Signature-level map ranked by degree/importance; truncated to token budget."""
    root = os.path.abspath(root_dir)
    items = collect_signatures(root)

    # Rank by code-graph degree when available
    degree: Dict[str, int] = {}
    try:
        from coding.code_graph import get_store
        store = get_store(root)
        if store.stats()["nodes"] == 0 and force_graph:
            store.build(root)
        degree = store.degree_map()
        # Map bare names
        name_degree: Dict[str, int] = {}
        for nid, deg in degree.items():
            # function:foo@path or class:Bar@path
            if ":" in nid:
                rest = nid.split(":", 1)[1]
                name = rest.split("@")[0].split(".")[-1]
                name_degree[name] = max(name_degree.get(name, 0), deg)
                name_degree[rest.split("@")[0]] = max(name_degree.get(rest.split("@")[0], 0), deg)
        for it in items:
            bare = it["name"].split(".")[-1]
            it["degree"] = max(
                name_degree.get(it["name"], 0),
                name_degree.get(bare, 0),
            )
    except Exception:
        pass

    items.sort(key=lambda x: (-x.get("degree", 0), x["file"], x["lineno"]))

    lines = [f"# Repo map: {root}", ""]
    used = _estimate_tokens("\n".join(lines))
    included = 0
    truncated = False
    current_file = None
    for it in items:
        if it["file"] != current_file:
            header = f"\n## {it['file']}\n"
            if used + _estimate_tokens(header) > token_budget:
                truncated = True
                break
            lines.append(header.rstrip())
            used += _estimate_tokens(header)
            current_file = it["file"]
        entry = f"  L{it['lineno']}: {it['signature']}"
        if it.get("degree"):
            entry += f"  [deg={it['degree']}]"
        entry += "\n"
        if used + _estimate_tokens(entry) > token_budget:
            truncated = True
            break
        lines.append(entry.rstrip())
        used += _estimate_tokens(entry)
        included += 1

    text = "\n".join(lines)
    return {
        "map": text,
        "symbols_included": included,
        "symbols_total": len(items),
        "token_estimate": used,
        "token_budget": token_budget,
        "truncated": truncated,
        "root_dir": root,
    }


# ── Grep ──────────────────────────────────────────────────────────────

def grep(
    pattern: str,
    path: str = ".",
    glob: str = None,
    context: int = 0,
    max_matches: int = 50,
    case_insensitive: bool = False,
) -> Dict:
    """Search with ripgrep if available, else grep -R."""
    path = os.path.abspath(path)
    rg = shutil.which("rg")
    matches: List[Dict] = []
    engine = "rg" if rg else "grep"

    try:
        if rg:
            cmd = ["rg", "--json", "-n", "--no-heading", f"--max-count={max_matches}"]
            if context:
                cmd.extend(["-C", str(context)])
            if case_insensitive:
                cmd.append("-i")
            if glob:
                cmd.extend(["-g", glob])
            cmd.extend(["--", pattern, path])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    import json
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "match":
                    continue
                data = obj.get("data", {})
                path_text = data.get("path", {}).get("text", "")
                line_num = data.get("line_number")
                text = data.get("lines", {}).get("text", "").rstrip("\n")
                matches.append({
                    "file": path_text,
                    "line": line_num,
                    "text": text,
                })
                if len(matches) >= max_matches:
                    break
        else:
            cmd = ["grep", "-R", "-n", "-I"]
            if case_insensitive:
                cmd.append("-i")
            if context:
                cmd.extend(["-C", str(context)])
            if glob:
                # grep --include
                cmd.append(f"--include={glob}")
            cmd.extend(["--", pattern, path])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            for line in (proc.stdout or "").splitlines():
                # file:line:text
                m = re.match(r"^([^:]+):(\d+):(.*)$", line)
                if not m:
                    # context lines file-line-text
                    m2 = re.match(r"^([^:]+)-(\d+)-(.*)$", line)
                    if m2:
                        matches.append({
                            "file": m2.group(1),
                            "line": int(m2.group(2)),
                            "text": m2.group(3),
                            "context": True,
                        })
                    continue
                matches.append({
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "text": m.group(3),
                })
                if len(matches) >= max_matches:
                    break
    except subprocess.TimeoutExpired:
        return {"error": "grep timed out", "matches": [], "engine": engine}
    except FileNotFoundError:
        return {"error": "Neither rg nor grep found", "matches": [], "engine": "none"}

    return {
        "pattern": pattern,
        "path": path,
        "glob": glob,
        "matches": matches,
        "count": len(matches),
        "engine": engine,
    }


# ── Outline ───────────────────────────────────────────────────────────

def outline(path: str) -> Dict:
    """File symbol outline with line numbers (functions/classes/methods)."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        return {"error": f"File not found: {path}"}
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
        tree = ast.parse(text)
    except SyntaxError as e:
        return {"error": f"SyntaxError: {e}", "path": path}
    except OSError as e:
        return {"error": str(e), "path": path}

    symbols: List[Dict] = []

    def walk_body(body, indent=0, prefix=""):
        for node in body:
            if isinstance(node, ast.ClassDef):
                symbols.append({
                    "kind": "class",
                    "name": node.name,
                    "qualname": f"{prefix}{node.name}" if prefix else node.name,
                    "lineno": node.lineno,
                    "end_lineno": getattr(node, "end_lineno", node.lineno),
                    "indent": indent,
                })
                walk_body(node.body, indent + 1, f"{prefix}{node.name}.")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append({
                    "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                    "name": node.name,
                    "qualname": f"{prefix}{node.name}" if prefix else node.name,
                    "lineno": node.lineno,
                    "end_lineno": getattr(node, "end_lineno", node.lineno),
                    "indent": indent,
                })
                # Nested functions
                walk_body(node.body, indent + 1, f"{prefix}{node.name}.")

    walk_body(tree.body)
    # Format pretty text
    lines = []
    for s in symbols:
        pad = "  " * s["indent"]
        lines.append(f"{pad}{s['kind']} {s['name']}  L{s['lineno']}-L{s['end_lineno']}")
    return {
        "path": path,
        "symbols": symbols,
        "outline": "\n".join(lines),
        "count": len(symbols),
    }


# ── Explain failure ───────────────────────────────────────────────────

def explain_failure(traceback_text: str) -> Dict:
    """Turn a traceback into short actionable bullets."""
    text = traceback_text or ""
    bullets: List[str] = []
    # Exception type + message
    exc_match = re.findall(
        r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Fail|Failed))\s*:\s*(.*)$",
        text,
        re.M,
    )
    for typ, msg in exc_match[-3:]:
        bullets.append(f"{typ}: {msg.strip()[:200]}")

    # File/line frames
    frames = re.findall(r'File "([^"]+)", line (\d+), in (\S+)', text)
    if frames:
        f, ln, fn = frames[-1]
        bullets.append(f"Last frame: {fn} at {f}:{ln}")
        if len(frames) > 1:
            f0, ln0, fn0 = frames[0]
            bullets.append(f"Origin frame: {fn0} at {f0}:{ln0}")

    # Assertion
    for m in re.finditer(r"assert\s+.+", text):
        bullets.append(f"Assertion: {m.group(0)[:160]}")
        break

    # FAILED lines from pytest
    for m in re.finditer(r"FAILED\s+(\S+)", text):
        bullets.append(f"Failed test: {m.group(1)}")

    if not bullets:
        # Fallback: first non-empty lines
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("File "):
                bullets.append(s[:200])
            if len(bullets) >= 5:
                break

    # Dedup preserve order
    seen = set()
    uniq = []
    for b in bullets:
        if b not in seen:
            seen.add(b)
            uniq.append(b)

    return {
        "bullets": uniq[:12],
        "summary": " | ".join(uniq[:3]) if uniq else "No structured failure found",
        "frame_count": len(frames) if frames else 0,
    }


def get_perception_tools() -> List[Dict]:
    return [
        {
            "name": "coding_repo_map",
            "description": "Signature-level repository map ranked by graph degree/importance. Truncated to a token budget.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_dir": {"type": "string", "default": "."},
                    "token_budget": {"type": "integer", "default": 4000},
                },
            },
            "handler": lambda root_dir=".", token_budget=4000: repo_map(root_dir, token_budget),
            "category": "code_search",
            "permission": "safe",
        },
        {
            "name": "coding_grep",
            "description": "Search code with ripgrep (if available) or grep -R. Supports pattern, path, glob, context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "description": "e.g. *.py"},
                    "context": {"type": "integer", "default": 0},
                    "max_matches": {"type": "integer", "default": 50},
                    "case_insensitive": {"type": "boolean", "default": False},
                },
                "required": ["pattern"],
            },
            "handler": lambda pattern, path=".", glob=None, context=0, max_matches=50, case_insensitive=False: grep(
                pattern, path, glob, context, max_matches, case_insensitive
            ),
            "category": "code_search",
            "permission": "safe",
        },
        {
            "name": "coding_outline",
            "description": "Symbol outline of a file with line numbers (classes, functions, methods).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                },
                "required": ["path"],
            },
            "handler": outline,
            "category": "code_search",
            "permission": "safe",
        },
        {
            "name": "coding_explain_failure",
            "description": "Turn a traceback or test failure dump into short actionable bullets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "traceback_text": {"type": "string", "description": "Raw traceback or pytest output"},
                },
                "required": ["traceback_text"],
            },
            "handler": lambda traceback_text: explain_failure(traceback_text),
            "category": "code_repair",
            "permission": "safe",
        },
    ]
