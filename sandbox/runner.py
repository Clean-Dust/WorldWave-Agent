"""
ww/sandbox/runner.py — Code execution sandbox

Implement smolagents-style "code-as-action" pattern:
- When complex logic is needed, LLM no longer uses function calls, but directly writes Python code
- WW executes the code in an isolated environment and returns the result
- More flexible than function calls (loops, variables, conditions all available)

Security consideration: code runs at the subprocess level,
Can be upgraded to Docker sandbox later.
"""

from __future__ import annotations
import ast
import json
import subprocess
import sys
import tempfile
from typing import Dict, Any, List


# Allowed built-in modules (blacklist vs whitelist — using whitelist here)
ALLOWED_MODULES = {
    # Standard library (safe)
    "json", "math", "random", "datetime", "time",
    "collections", "itertools", "functools", "statistics",
    "re", "string", "enum", "textwrap", "typing",
    "os.path", "pathlib", "sys", "os",
    "hashlib", "base64", "binascii",
    "csv", "io", "copy", "pprint",
    "decimal", "fractions", "uuid",
    # Data processing
    "dataclasses", "operator",
}


class SandboxResult:
    """Code execution result."""
    def __init__(self, success: bool, output: str = "",
                 error: str = "", duration: float = 0.0,
                 result_data: Any = None):
        self.success = success
        self.output = output
        self.error = error
        self.duration = duration
        self.result_data = result_data
    
    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "output": self.output[:2000],
            "error": self.error[:500] if self.error else "",
            "duration_seconds": round(self.duration, 2),
            "has_result": self.result_data is not None,
        }


class CodeSandbox:
    """
    Code sandbox.
    
    Two layers of security:
    1. Static analysis — scan code before execution, check for dangerous operations
    2. Execution isolation — subprocess + timeout
    
    Not aiming for perfect security (that requires Docker), but provides basic protection.
    Advanced: spawn a subprocess without network to run.
    """
    
    def __init__(self, timeout: int = 30, workdir: str = ""):
        self.timeout = timeout
        self.workdir = workdir or tempfile.mkdtemp(prefix="ww_sandbox_")
    
    def run_code(self, code: str, context: Dict = None) -> SandboxResult:
        """
        Execute code and return result.
        
        code: Python code to execute (plain text)
        context: provide variables to the code (e.g., {"data": [...]})
        """
        start = __import__("time").time()
        
        # Step 1: Static analysis
        violations = self._static_analysis(code)
        if violations:
            return SandboxResult(
                success=False,
                error=f"❌ Security violation: {', '.join(violations)}",
                duration=__import__("time").time() - start,
            )
        
        # Step 2: Wrap into executable script
        script = self._wrap_code(code, context or {})
        
        # Step 3: Execute
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.workdir,
                env={
                    "PYTHONIOENCODING": "utf-8",
                    **{k: str(v) for k, v in (context or {}).items()
                       if isinstance(v, (str, int, float))},
                },
            )
            
            duration = __import__("time").time() - start
            
            if result.returncode == 0:
                # Try to parse result from output
                result_data = self._parse_output(result.stdout)
                return SandboxResult(
                    success=True,
                    output=result.stdout.strip(),
                    duration=duration,
                    result_data=result_data,
                )
            else:
                return SandboxResult(
                    success=False,
                    output=result.stdout.strip(),
                    error=result.stderr.strip(),
                    duration=duration,
                )
                
        except subprocess.TimeoutExpired:
            return SandboxResult(
                success=False,
                error=f"⏱ Execution timeout ({self.timeout}s)",
                duration=self.timeout,
            )
        except Exception as e:
            return SandboxResult(
                success=False,
                error=str(e),
                duration=__import__("time").time() - start,
            )
    
    def _static_analysis(self, code: str) -> List[str]:
        """
        Statically analyze code, check for dangerous operations.

        Checks:
        - Blacklisted modules in import
        - exec/eval/compile/__import__
        - os.system/subprocess (if not in sandbox context)
        - Reflection bypass: getattr(obj, 'method'), obj.__dict__['method']
        - Builtins escape: __builtins__.__dict__['exec']
        """
        violations = []

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"Syntax error: {e}"]

        for node in ast.walk(tree):
            # ── Check imports ──────────────────────────────────
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name not in ALLOWED_MODULES and \
                       not alias.name.startswith("ww_"):
                        violations.append(f"Disallowed module: {alias.name}")

            if isinstance(node, ast.ImportFrom):
                if node.module and node.module not in ALLOWED_MODULES:
                    violations.append(f"Disallowed source: {node.module}")

            # ── Check dangerous function calls ─────────────────
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile", "__import__"):
                        violations.append(f"Dangerous function: {node.func.id}")
                    if node.func.id == "__builtins__":
                        violations.append("Dangerous: direct access to __builtins__")

                    # getattr(obj, 'system') — reflection to access dangerous methods
                    if node.func.id == "getattr" and len(node.args) >= 2:
                        if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                            target = node.args[1].value
                            if target in ("system", "popen", "call", "run", "exec", "eval",
                                          "compile", "__import__"):
                                violations.append(
                                    f"Reflection bypass: getattr(..., '{target}')"
                                )

                # obj.system(...) / obj.popen(...) / ...
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("system", "popen", "call", "run"):
                        violations.append(f"Dangerous method: {node.func.attr}")

            # ── Check __dict__ / __builtins__ access ───────────
            # obj.__dict__['system'] — dictionary bypass
            if isinstance(node, ast.Subscript):
                if isinstance(node.value, ast.Attribute) and node.value.attr == "__dict__":
                    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                        key = node.slice.value
                        if key in ("system", "popen", "exec", "eval", "compile",
                                   "__import__", "run", "call", "Popen"):
                            violations.append(
                                f"Dict bypass: .__dict__['{key}']"
                            )

            # Access to __builtins__ (potential escape)
            if isinstance(node, ast.Attribute) and node.attr == "__builtins__":
                violations.append("Dangerous: access to __builtins__")
            if isinstance(node, ast.Name) and node.id == "__builtins__":
                violations.append("Dangerous: access to __builtins__")

        return violations
    
    def _wrap_code(self, code: str, context: Dict) -> str:
        """Wrap user code into a script that captures output."""
        # Inject context variables
        context_lines = ""
        for key, value in context.items():
            context_lines += f"{key} = {json.dumps(value)}\n"
        
        return f"""
import json, sys

# Inject context
{context_lines}

# User code
{code}

# Auto output the last expression (if no print)
if '_result' in dir():
    print("__RESULT__:" + json.dumps(_result))
"""
    
    def _parse_output(self, output: str) -> Any:
        """Parse result from output."""
        if "__RESULT__:" in output:
            try:
                data = output.split("__RESULT__:")[-1].strip()
                return json.loads(data)
            except:
                return output[-500:]
        return None
    
    def run_file(self, path: str) -> SandboxResult:
        """Execute a file."""
        try:
            with open(path) as f:
                code = f.read()
            return self.run_code(code)
        except Exception as e:
            return SandboxResult(success=False, error=str(e))
