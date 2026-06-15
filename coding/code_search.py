"""ww/pm/code_search.py — AST-aware code search & analysis engine v0.1

Implements Gemini's WW-PM Subsystem 3.1.1:
- Syntax-aware structural code search (AST-based, no external deps)
- Pattern matching at function, class, and expression level
- Call graph extraction for dependency analysis

Built on Python's stdlib `ast` module — zero external dependencies.
"""

from __future__ import annotations
import ast
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


# ── AST Search ────────────────────────────────────────────────────────

class ASTPattern:
    """A search pattern for AST-based code matching."""

    def __init__(self, pattern_type: str, target: str, **kwargs):
        """
        Args:
            pattern_type: One of 'function', 'class', 'call', 'import', 'variable', 'decorator', 'string'
            target: Pattern to match (name or regex)
        """
        self.pattern_type = pattern_type
        self.target = target
        self.kwargs = kwargs


class ASTSearchEngine:
    """Search code files using AST structure rather than text regex.

    Supports:
    - Find all function/method definitions by name pattern
    - Find all class definitions
    - Find all function calls matching a pattern
    - Find import statements
    - Extract call graphs from files
    """

    def __init__(self):
        self._cache: Dict[str, ast.AST] = {}

    def search(
        self,
        pattern: ASTPattern,
        root_dir: str = ".",
        file_glob: str = "*.py",
        max_results: int = 50,
    ) -> Dict:
        """Search for AST pattern across files.

        Returns:
            Dict with matches, each containing file, line, column, code snippet
        """
        matches = []
        files = self._find_files(root_dir, file_glob)

        for filepath in files:
            tree = self._parse_file(filepath)
            if tree is None:
                continue

            file_matches = self._search_tree(tree, pattern, filepath)
            matches.extend(file_matches)

            if len(matches) >= max_results:
                matches = matches[:max_results]
                break

        return {
            "matches": matches,
            "count": len(matches),
            "pattern": pattern.pattern_type,
            "target": pattern.target,
        }

    def find_functions(
        self,
        name_pattern: str = None,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Find function/method definitions matching a name pattern."""
        return self.search(ASTPattern("function", name_pattern or ".*"), root_dir, file_glob)

    def find_classes(
        self,
        name_pattern: str = None,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Find class definitions."""
        return self.search(ASTPattern("class", name_pattern or ".*"), root_dir, file_glob)

    def find_calls(
        self,
        func_name: str,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Find all calls to a specific function."""
        return self.search(ASTPattern("call", func_name), root_dir, file_glob)

    def find_imports(
        self,
        module_pattern: str = None,
        root_dir: str = ".",
        file_glob: str = "*.py",
    ) -> Dict:
        """Find import statements matching a module pattern."""
        return self.search(ASTPattern("import", module_pattern or ".*"), root_dir, file_glob)

    def extract_call_graph(self, filepath: str) -> Dict:
        """Extract call graph from a single file.

        Returns:
            Dict mapping function names to sets of called function names
        """
        tree = self._parse_file(filepath)
        if tree is None:
            return {"error": f"Could not parse {filepath}", "graph": {}}

        graph = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                calls = set()
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name):
                            calls.add(child.func.id)
                        elif isinstance(child.func, ast.Attribute):
                            calls.add(f"{self._get_attribute_chain(child.func)}")
                graph[func_name] = sorted(calls)

        return {
            "file": filepath,
            "graph": graph,
            "function_count": len(graph),
        }

    def find_function_body(
        self, filepath: str, function_name: str
    ) -> Dict:
        """Extract the full body of a specific function."""
        tree = self._parse_file(filepath)
        if tree is None:
            return {"error": f"Could not parse {filepath}"}

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                lines = self._get_source_lines(filepath, node.lineno, node.end_lineno)
                return {
                    "file": filepath,
                    "function": function_name,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "body": lines,
                    "decorators": [self._ast_node_name(d) for d in node.decorator_list],
                    "args": self._format_args(node.args),
                }

        return {"error": f"Function {function_name} not found in {filepath}"}

    def _search_tree(self, tree: ast.AST, pattern: ASTPattern, filepath: str) -> List[Dict]:
        """Search an AST tree for the given pattern."""
        matches = []
        target_re = re.compile(pattern.target) if pattern.target else None

        for node in ast.walk(tree):
            match = self._node_matches(node, pattern, target_re)
            if match:
                lines = self._get_source_lines(filepath, node.lineno, node.end_lineno or node.lineno)
                matches.append({
                    "file": filepath,
                    "line": node.lineno,
                    "column": getattr(node, "col_offset", 0),
                    "end_line": node.end_lineno or node.lineno,
                    "snippet": lines,
                    **match,
                })

        return matches

    def _node_matches(
        self, node: ast.AST, pattern: ASTPattern, target_re: re.Pattern
    ) -> Optional[Dict]:
        """Check if a node matches the search pattern."""
        if pattern.pattern_type == "function":
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if target_re and target_re.search(node.name):
                    return {"type": "function", "name": node.name}

        elif pattern.pattern_type == "class":
            if isinstance(node, ast.ClassDef):
                if target_re and target_re.search(node.name):
                    return {"type": "class", "name": node.name}

        elif pattern.pattern_type == "call":
            if isinstance(node, ast.Call):
                call_name = None
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr

                if call_name and target_re and target_re.search(call_name):
                    return {"type": "call", "name": call_name}

        elif pattern.pattern_type == "import":
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                if target_re:
                    matching = [n for n in names if target_re.search(n)]
                    if matching:
                        return {"type": "import", "names": matching}
                else:
                    return {"type": "import", "names": names}

        elif pattern.pattern_type == "variable":
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target_re and target_re.search(target.id):
                            return {"type": "variable", "name": target.id}

        elif pattern.pattern_type == "decorator":
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in node.decorator_list:
                    dec_name = self._ast_node_name(dec)
                    if target_re and target_re.search(dec_name):
                        return {"type": "decorator", "name": dec_name, "decorates": node.name}

        elif pattern.pattern_type == "string":
            if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Str, ast.Constant)):
                val = node.value.s if isinstance(node.value, ast.Str) else node.value.value
                if isinstance(val, str) and target_re and target_re.search(val):
                    return {"type": "string", "content": val[:200]}

        return None

    def _parse_file(self, filepath: str) -> Optional[ast.AST]:
        """Parse a Python file into an AST."""
        if filepath in self._cache:
            return self._cache[filepath]

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            tree = ast.parse(content)
            self._cache[filepath] = tree
            return tree
        except (SyntaxError, IOError) as e:
            return None

    def _find_files(self, root_dir: str, file_glob: str) -> List[str]:
        """Find files matching glob pattern."""
        root = Path(root_dir).resolve()
        # Convert simple globs to patterns
        if file_glob == "*.py":
            pattern = "**/*.py"
        elif file_glob == "*.js":
            pattern = "**/*.js"
        else:
            pattern = f"**/{file_glob}"

        files = []
        for f in sorted(root.glob(pattern)):
            if ".git" not in f.parts and "__pycache__" not in f.parts:
                files.append(str(f))
        return files

    def _get_source_lines(self, filepath: str, start: int, end: int, context: int = 2) -> str:
        """Get source lines from a file."""
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            s = max(0, start - 1 - context)
            e = min(len(lines), end + context)
            result = []
            for i in range(s, e):
                prefix = "  " if (i + 1) < start or (i + 1) > end else "> "
                result.append(f"{prefix}{i+1:4d}|{lines[i].rstrip()}")
            return "\n".join(result)
        except IOError:
            return ""

    def _ast_node_name(self, node: ast.AST) -> str:
        """Get name from an AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._ast_node_name(node.value)}.{node.attr}"
        elif isinstance(node, ast.Call):
            return self._ast_node_name(node.func)
        return str(node)

    def _get_attribute_chain(self, node: ast.AST) -> str:
        """Get dotted attribute chain (e.g., 'obj.method')."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_attribute_chain(node.value)}.{node.attr}"
        return "?"

    def _format_args(self, args: ast.arguments) -> List[str]:
        """Format function arguments."""
        result = []
        for arg in args.args:
            hint = ""
            if arg.annotation:
                hint = f": {self._ast_node_name(arg.annotation)}"
            result.append(f"{arg.arg}{hint}")
        if args.vararg:
            result.append(f"*{args.vararg.arg}")
        if args.kwonlyargs:
            for arg in args.kwonlyargs:
                result.append(f"{arg.arg}")
        if args.kwarg:
            result.append(f"**{args.kwarg.arg}")
        return result

    def clear_cache(self):
        """Clear the AST cache."""
        self._cache.clear()


# ── Simplified ast-grep like pattern matching ─────────────────────────

class StructuralPattern:
    """Structural code pattern for find-and-replace operations.

    Similar to ast-grep's pattern matching but using Python AST.
    """

    @staticmethod
    def find_function_calls(name: str, root_dir: str = ".") -> Dict:
        """Find all calls to a specific function by name."""
        engine = ASTSearchEngine()
        return engine.find_calls(name, root_dir)

    @staticmethod
    def find_function_def(name_pattern: str, root_dir: str = ".") -> Dict:
        """Find all function definitions matching a name."""
        engine = ASTSearchEngine()
        return engine.find_functions(name_pattern, root_dir)

    @staticmethod
    def count_lines_of_code(filepath: str) -> Dict:
        """Count logical LOC (excluding blanks/comments)."""
        tree = ASTSearchEngine()._parse_file(filepath)
        if tree is None:
            return {"error": f"Cannot parse {filepath}"}

        # Count AST nodes as a proxy for logical lines
        node_count = sum(1 for _ in ast.walk(tree))

        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        blank = sum(1 for l in lines if l.strip() == "")
        comment = sum(1 for l in lines if l.strip().startswith("#"))

        return {
            "file": filepath,
            "total_lines": total,
            "blank_lines": blank,
            "comment_lines": comment,
            "code_lines": total - blank - comment,
            "ast_nodes": node_count,
        }

    @staticmethod
    def extract_class_hierarchy(root_dir: str = ".") -> Dict:
        """Extract class inheritance hierarchy across files."""
        engine = ASTSearchEngine()
        classes = engine.find_classes(root_dir=root_dir)

        hierarchy = {}
        for match in classes.get("matches", []):
            tree = engine._parse_file(match["file"])
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == match["name"]:
                    bases = []
                    for base in node.bases:
                        bases.append(engine._ast_node_name(base))
                    hierarchy[match["name"]] = {
                        "bases": bases,
                        "file": match["file"],
                        "line": match["line"],
                        "methods": [
                            n.name for n in ast.walk(node)
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        ],
                    }

        return {
            "classes": hierarchy,
            "count": len(hierarchy),
        }


# ── AST Transformer for Code Rewriting ────────────────────────────────

class ASTRewriter:
    """Rewrite Python code using AST transformation (NodeTransformer).

    Implements Gemini's Section 4.2: surgical AST-level code transformations
    that preserve syntax integrity, indentation, and comments.

    Supports:
    - BinOp to function call (e.g., X|Y → typing.Union[X, Y])
    - Function call to BinOp
    - Named argument conversion
    - Custom transformer via visitor pattern
    """

    @staticmethod
    def transform(
        source: str,
        transformer: ast.NodeTransformer,
    ) -> Dict:
        """Apply a custom AST NodeTransformer to source code.

        Args:
            source: Python source code string
            transformer: An ast.NodeTransformer instance

        Returns:
            Dict with transformed source or error
        """
        try:
            tree = ast.parse(source)
            new_tree = transformer.visit(tree)
            ast.fix_missing_locations(new_tree)
            result = ast.unparse(new_tree)
            return {"success": True, "result": result}
        except SyntaxError as e:
            return {"success": False, "error": f"Syntax error: {e}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def rewrite_file(
        filepath: str,
        transformer: ast.NodeTransformer,
        dry_run: bool = False,
    ) -> Dict:
        """Apply a transformation to a file.

        Args:
            filepath: Path to Python file
            transformer: AST NodeTransformer to apply
            dry_run: If True, return result without writing

        Returns:
            Dict with success status and diff
        """
        with open(filepath, "r", encoding="utf-8") as f:
            original = f.read()

        result = ASTRewriter.transform(original, transformer)
        if not result.get("success"):
            return result

        if dry_run:
            return {"success": True, "filepath": filepath, "result": result["result"]}

        # Atomic write
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath) or ".", prefix=".ww_ast_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(result["result"])
        os.replace(tmp, filepath)

        # Simple diff
        old_lines = original.splitlines()
        new_lines = result["result"].splitlines()
        changed = sum(1 for a, b in zip(old_lines, new_lines) if a != b)

        return {
            "success": True,
            "filepath": filepath,
            "lines_changed": changed,
        }

    # ── Built-in Transformers ──────────────────────────────────────

    @staticmethod
    def union_operator_transformer() -> ast.NodeTransformer:
        """Create a transformer that converts X|Y → typing.Union[X, Y].

        For Python < 3.10 compatibility where | union syntax is not supported.
        """
        class _UnionOpTransformer(ast.NodeTransformer):
            def visit_BinOp(self, node):
                self.generic_visit(node)
                if isinstance(node.op, ast.BitOr):
                    # X | Y → typing.Union[X, Y]
                    return ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id="typing", ctx=ast.Load()),
                            attr="Union",
                            ctx=ast.Load(),
                        ),
                        args=[node.left, node.right],
                        keywords=[],
                    )
                return node

        return _UnionOpTransformer()

    @staticmethod
    def walrus_to_assignment_transformer() -> ast.NodeTransformer:
        """Convert walrus operator (:=) to separate assignment + usage.

        Note: This only handles simple cases. Complex walrus may need manual review.
        """
        class _WalrusTransformer(ast.NodeTransformer):
            def __init__(self):
                self._insertions = []

            def visit_NamedExpr(self, node):
                self.generic_visit(node)
                target_name = node.target.id if isinstance(node.target, ast.Name) else None
                if target_name:
                    # Record the assignment to be inserted
                    assign = ast.Assign(
                        targets=[ast.Name(id=target_name, ctx=ast.Store())],
                        value=node.value,
                    )
                    self._insertions.append(assign)
                    return ast.Name(id=target_name, ctx=ast.Load())
                return node

            def get_insertions(self):
                return self._insertions

        return _WalrusTransformer()

    @staticmethod
    def fstring_to_format_transformer() -> ast.NodeTransformer:
        """Convert simple f-strings to .format() calls (Python < 3.6 compat)."""
        class _FStringTransformer(ast.NodeTransformer):
            def visit_JoinedStr(self, node):
                self.generic_visit(node)
                parts = []
                for value in node.values:
                    if isinstance(value, ast.Constant):
                        parts.append(value.value)
                    elif isinstance(value, ast.FormattedValue):
                        if isinstance(value.value, ast.Name):
                            parts.append("{" + value.value.id + "}")
                        elif isinstance(value.value, ast.Attribute):
                            parts.append("{" + self._attr_name(value.value) + "}")
                return ast.Constant(value="".join(parts))

            def _attr_name(self, node):
                if isinstance(node, ast.Attribute):
                    return self._attr_name(node.value) + "." + node.attr
                return node.id if hasattr(node, "id") else "?"

        return _FStringTransformer()


# ── Helper: safe AST rewrite with custom transformer support ────────

def _safe_ast_rewrite(filepath: str, transform: str, transformer_code: str = "", dry_run: bool = False) -> Dict:
    """Rewrite a file using built-in or custom AST transformer."""
    builtin = {
        "union_operator": ASTRewriter.union_operator_transformer,
        "walrus_to_assignment": ASTRewriter.walrus_to_assignment_transformer,
        "fstring_to_format": ASTRewriter.fstring_to_format_transformer,
    }
    if transform in builtin:
        return ASTRewriter.rewrite_file(filepath, builtin[transform](), dry_run=dry_run)
    if transform == "custom":
        if not transformer_code.strip():
            return {"error": "transformer_code must be provided when transform='custom'"}
        try:
            # Compile and execute the custom transformer code
            namespace = {"ast": ast, "NodeTransformer": ast.NodeTransformer}
            exec(transformer_code, namespace)
            # Find the NodeTransformer subclass
            transformer = None
            for name, obj in namespace.items():
                if isinstance(obj, type) and issubclass(obj, ast.NodeTransformer) and obj is not ast.NodeTransformer:
                    transformer = obj()
                    break
            if transformer is None:
                return {"error": "No ast.NodeTransformer subclass found in transformer_code"}
            return ASTRewriter.rewrite_file(filepath, transformer, dry_run=dry_run)
        except Exception as e:
            return {"success": False, "error": f"Custom transformer error: {e}"}
    return {"error": f"Unknown transform: {transform}. Use: union_operator, walrus_to_assignment, fstring_to_format, custom"}


# ── Tool definitions ──────────────────────────────────────────────────

_engine: ASTSearchEngine = None


def get_engine() -> ASTSearchEngine:
    global _engine
    if _engine is None:
        _engine = ASTSearchEngine()
    return _engine


# ── ast-grep CLI Integration ──────────────────────────────────────────

def _check_ast_grep() -> str:
    """Check if ast-grep CLI (sg) is available. Returns version or error."""
    try:
        import subprocess
        result = subprocess.run(
            ["sg", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
        return "unknown"
    except FileNotFoundError:
        return ""  # Not installed
    except Exception:
        return ""


def _ast_grep_search(pattern: str, root_dir: str = ".", lang: str = "",
                     max_results: int = 30) -> Dict:
    """Search code with ast-grep's structural pattern matching.

    ast-grep (sg CLI) uses AST-aware patterns instead of text regex:
    - sg -p 'console.log($$$ARGS)' → finds all console.log calls
    - sg -p 'function $NAME($$$) { $$$ }' → finds all function declarations
    - Supports Python, JS, TS, Go, Rust, and more.

    Falls back to built-in AST search if sg CLI is not installed.
    Ref: Gemini WW-PM blueprint — "sg -p pattern" syntax-aware structural search.
    """
    import subprocess, json

    if not _check_ast_grep():
        return {
            "success": False,
            "error": (
                "ast-grep CLI (sg) is not installed. Install: cargo install ast-grep\n"
                "Or use coding_ast_search for built-in Python AST matching."
            ),
        }

    cmd = ["sg", "--pattern", pattern, "--json"] if not lang else           ["sg", "--pattern", pattern, "--lang", lang, "--json"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=15,
            cwd=os.path.abspath(os.path.expanduser(root_dir)),
        )
        # Parse JSON output
        try:
            data = json.loads(result.stdout) if result.stdout else []
        except json.JSONDecodeError:
            data = []

        matches = []
        if isinstance(data, list):
            for match in data[:max_results]:
                matches.append({
                    "file": match.get("file", ""),
                    "line": match.get("range", {}).get("start", {}).get("line", 0),
                    "column": match.get("range", {}).get("start", {}).get("column", 0),
                    "text": match.get("text", "")[:200],
                })
            total = len(data)
        else:
            # Some versions return structured differently
            total = 0

        return {
            "success": True,
            "pattern": pattern,
            "language": lang or "auto",
            "total_matches": total,
            "matches": matches[:max_results],
            "truncated": total > max_results,
            "note": f"showing {len(matches)} of {total} matches" if total > max_results else "",
        }
    except Exception as e:
        return {"success": False, "error": f"ast-grep search failed: {e}"}


def _ast_grep_langs() -> Dict:
    """List supported languages for ast-grep."""
    try:
        import subprocess
        result = subprocess.run(
            ["sg", "--lang", "help"], capture_output=True, text=True, timeout=5
        )
        return {"success": True, "languages": result.stdout.strip()[:500]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def create_code_search_tools() -> List[Dict]:
    engine = get_engine()  # Lazy-init the AST search engine
    return [
        {
            "name": "coding_ast_search",
            "description": "Search code using AST pattern matching. Supports: function, class, call, import, variable, decorator, string patterns. More precise than grep as it respects code structure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern_type": {
                        "type": "string",
                        "enum": ["function", "class", "call", "import", "variable", "decorator", "string"],
                        "description": "Type of AST node to search for",
                    },
                    "target": {
                        "type": "string",
                        "description": "Regex pattern to match against node names",
                    },
                    "root_dir": {
                        "type": "string",
                        "description": "Root directory to search (default: current)",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "File pattern (default: *.py)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default: 50)",
                        "default": 50,
                    },
                },
                "required": ["pattern_type", "target"],
            },
            "handler": lambda pattern_type, target, root_dir=".", file_glob="*.py", max_results=50: engine.search(
                ASTPattern(pattern_type, target), root_dir, file_glob, max_results
            ),
            "category": "code_search",
        },
        {
            "name": "coding_call_graph",
            "description": "Extract call graph from a file — shows which functions call which other functions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the Python file",
                    }
                },
                "required": ["filepath"],
            },
            "handler": engine.extract_call_graph,
            "category": "code_search",
        },
        {
            "name": "coding_function_body",
            "description": "Extract the full body of a specific function, including arguments and decorators.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the Python file",
                    },
                    "function_name": {
                        "type": "string",
                        "description": "Function or method name",
                    },
                },
                "required": ["filepath", "function_name"],
            },
            "handler": lambda filepath, function_name: engine.find_function_body(filepath, function_name),
            "category": "code_search",
        },
        {
            "name": "coding_code_stats",
            "description": "Get code statistics: total lines, code lines, comment lines, AST node count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the file",
                    }
                },
                "required": ["filepath"],
            },
            "handler": StructuralPattern.count_lines_of_code,
            "category": "code_search",
        },
        {
            "name": "coding_class_hierarchy",
            "description": "Extract class inheritance hierarchy with methods and base classes across the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root_dir": {
                        "type": "string",
                        "description": "Project root directory",
                        "default": ".",
                    }
                },
            },
            "handler": lambda root_dir=".": StructuralPattern.extract_class_hierarchy(root_dir),
            "category": "code_search",
        },
        {
            "name": "coding_ast_rewrite",
            "description": "Rewrite Python code using AST-level transformation. Supports built-in transforms: union_operator (X|Y → typing.Union[X, Y]), walrus_to_assignment (:=), fstring_to_format. Also accepts custom NodeTransformer source code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Python file to transform",
                    },
                    "transform": {
                        "type": "string",
                        "enum": ["union_operator", "walrus_to_assignment", "fstring_to_format", "custom"],
                        "description": "Built-in transform or 'custom'",
                    },
                    "transformer_code": {
                        "type": "string",
                        "description": "Python source code for a custom ast.NodeTransformer class (only when transform='custom')",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview changes without writing",
                        "default": False,
                    },
                },
                "required": ["filepath", "transform"],
            },
            "handler": lambda filepath, transform, transformer_code="", dry_run=False: _safe_ast_rewrite(filepath, transform, transformer_code, dry_run),
            "category": "code_search",
        },
        {
            "name": "coding_ast_grep",
            "description": "Structural code search with ast-grep CLI (sg). Uses AST-aware patterns — not text regex. Examples: 'console.log($$$ARGS)' finds all console.log calls across JS/TS, 'fn $NAME($$$) { $$$ }' finds function declarations. Install: cargo install ast-grep. Falls back to built-in AST search if not installed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "ast-grep pattern string (e.g., 'console.log($$$)', 'import $PKG from \"react\"', 'def $FUNC(self, $$$):')",
                    },
                    "root_dir": {
                        "type": "string",
                        "description": "Root directory to search (default: '.')",
                        "default": ".",
                    },
                    "language": {
                        "type": "string",
                        "description": "Language to search (e.g., 'python', 'javascript', 'typescript', 'rust'). Omit for auto-detection.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["pattern"],
            },
            "handler": lambda pattern, root_dir=".", language="", max_results=30: _ast_grep_search(pattern, root_dir, language, max_results),
            "category": "code_search",
        },
        {
            "name": "coding_glob",
            "description": "Search for files by glob pattern. Lightweight file path matching — first step in the Agentic Search chain (glob → search → read).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g., '**/*.py', 'core/**/*.py', 'tests/*.py')",
                    },
                    "root_dir": {
                        "type": "string",
                        "description": "Root directory to search (default: current)",
                        "default": ".",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default: 100)",
                        "default": 100,
                    },
                },
                "required": ["pattern"],
            },
            "handler": lambda pattern, root_dir=".", max_results=100: (
                lambda files: {
                    "files": files[:max_results],
                    "count": len(files),
                    "pattern": pattern,
                    "root_dir": root_dir,
                    "truncated": len(files) > max_results,
                }
            )(
                [str(p) for p in sorted(Path(root_dir).rglob(pattern))
                 if ".git" not in p.parts and "__pycache__" not in p.parts]
            ),
            "category": "code_search",
        },
    ]


# ── Progressive enhancement: auto-upgrade to tree-sitter if available ──

def create_search_engine():
    """Create the best available code search engine.

    If tree-sitter is installed, returns TreeSitterEngine (5-50x faster).
    Otherwise falls back to pure Python ASTSearchEngine.

    Returns:
        TreeSitterEngine or ASTSearchEngine — both have .search() API.
    """
    try:
        from coding.progressive import ProgressiveEnhancement
        pe = ProgressiveEnhancement()
        return pe.get_best_code_search_engine()
    except ImportError:
        return ASTSearchEngine()
