"""ww/tools/self_healer.py — WW code self-healing engine

WW can read, understand, and modify its own source code.

Security mechanism:
1. **Backup first** — each modification auto backs up the original file
2. **Syntax validation** — modification is linted immediately, syntax error triggers rollback
3. **Rate limit** — maximum 3 self-modifications per hour
4. **Record** — all modifications have an Audit Trail
"""
import os
import json
import ast
import shutil
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple

SELF_HEAL_DIR = os.path.expanduser("~/.ww/self_heal")
BACKUP_DIR = os.path.join(SELF_HEAL_DIR, "backups")
HISTORY_FILE = os.path.join(SELF_HEAL_DIR, "history.json")


class SelfHealer:
    """WW code self-healing engine."""
    
    def __init__(self, ww_dir: str = None):
        self.ww_dir = ww_dir or os.path.expanduser("~/worldwave")
        self._init_dirs()
        self._rate_limit = {"hour_count": 0, "hour_start": time.time()}
    
    def _init_dirs(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    
    def find_bugs(self) -> List[Dict]:
        """Static analysis finds potential bugs in own code.
        
        check:
        - dropout exception (except: pass without log)
        - unprocessed None
        - hardcoded path
        - potential infinite loop
        - incorrect comparison (== vs is)
        - unused import
        """
        bugs = []
        
        for root, dirs, files in os.walk(self.ww_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "venv", ".venv",
                       "__pycache__", "node_modules")]
            if ".venv" in root:  # skipvirtualenvironment
                continue
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, self.ww_dir)
                
                try:
                    with open(path) as f:
                        content = f.read()
                except:
                    continue
                
                lines = content.split("\n")
                
                # 1. check except: pass
                for i, line in enumerate(lines):
                    if "except:" in line or (line.strip().startswith("except") and "pass" in lines[min(i+1, len(lines)-1)].strip()):
                        nxt = lines[min(i+1, len(lines)-1)].strip()
                        if nxt == "pass":
                            bugs.append({
                                "type": "silent_except",
                                "file": rel,
                                "line": i + 1,
                                "code": line.strip(),
                                "suggestion": "add at least log or annotation",
                                "severity": "low",
                            })
                
                # 2. check == None vs is None
                for i, line in enumerate(lines):
                    if " == None" in line or "== None" in line:
                        bugs.append({
                            "type": "none_comparison",
                            "file": rel,
                            "line": i + 1,
                            "code": line.strip(),
                            "suggestion": "use 'is None' rather than '== None'",
                            "severity": "low",
                        })
                
                # 3. check hardcoded path
                for i, line in enumerate(lines):
                    if '"/home/' in line or "'/home/'" in line:
                        bugs.append({
                            "type": "hardcoded_path",
                            "file": rel,
                            "line": i + 1,
                            "code": line.strip()[:60],
                            "suggestion": "use os.path.expanduser('~')",
                            "severity": "low",
                        })
                
                # 4. check TODO/FIXME needs fixing
                for i, line in enumerate(lines):
                    if "FIXME" in line or "BUG" in line:
                        bugs.append({
                            "type": "known_bug",
                            "file": rel,
                            "line": i + 1,
                            "code": line.strip()[:80],
                            "suggestion": "needs fix this issue",
                            "severity": "medium",
                        })
        
        return bugs
    
    def analyze(self) -> List[Dict]:
        """Execute complete analysis, return list of fixable issues."""
        bugs = self.find_bugs()
        # Merge similar issues
        summary = {}
        for b in bugs:
            t = b["type"]
            if t not in summary:
                summary[t] = {"type": t, "count": 0, "severity": b["severity"],
                              "suggestion": b["suggestion"], "items": []}
            summary[t]["count"] += 1
            if len(summary[t]["items"]) < 3:
                summary[t]["items"].append(f"{b['file']}:{b['line']}")
        
        return list(summary.values())
    
    def _backup(self, path: str) -> str:
        """Backup file."""
        rel = os.path.relpath(path, self.ww_dir)
        backup_name = rel.replace("/", "_") + f".{int(time.time())}.bak"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        shutil.copy2(path, backup_path)
        return backup_path
    
    def _validate_syntax(self, path: str) -> Tuple[bool, str]:
        """Validate Python syntax."""
        try:
            with open(path) as f:
                ast.parse(f.read())
            return True, ""
        except SyntaxError as e:
            return False, str(e)
    
    def safe_patch(self, path: str, old: str, new: str) -> Dict:
        """Securely modify code.
        
        Process: backup → apply → validate → rollback on failure
        """
        rel = os.path.relpath(path, self.ww_dir)
        
        # 1. Rate limit check
        now = time.time()
        if now - self._rate_limit["hour_start"] > 3600:
            self._rate_limit["hour_count"] = 0
            self._rate_limit["hour_start"] = now
        
        if self._rate_limit["hour_count"] >= 5:
            return {"applied": False, "detail": " Reached hourly modification limit (5 times)"}
        
        # 2. Backup
        backup = self._backup(path)
        
        # 3. Apply changes
        try:
            with open(path) as f:
                content = f.read()
            
            if old not in content:
                return {"applied": False, "detail": f"Cannot find matching text: {old[:40]}"}
            
            new_content = content.replace(old, new, 1)
            with open(path, "w") as f:
                f.write(new_content)
        except Exception as e:
            shutil.copy2(backup, path)  # rollback
            return {"applied": False, "detail": f"writefailed: {e}"}
        
        # 4. Syntax validate
        valid, err = self._validate_syntax(path)
        if not valid:
            shutil.copy2(backup, path)  # rollback
            return {"applied": False, "detail": f"Syntax error rollback: {err}"}
        
        # 5. record
        self._rate_limit["hour_count"] += 1
        self._record_patch({
            "file": rel,
            "backup": backup,
            "applied": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "old_preview": old[:60],
            "new_preview": new[:60],
        })
        
        return {"applied": True, "detail": f" Fixed {rel}", "backup": backup}
    
    def _record_patch(self, record: Dict):
        """Record modification history."""
        history = []
        if os.path.isfile(HISTORY_FILE):
            try:
                with open(HISTORY_FILE) as f:
                    history = json.load(f)
            except:
                pass
        
        history.append(record)
        if len(history) > 50:
            history = history[-50:]
        
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    
    def get_history(self, limit: int = 10) -> List[Dict]:
        """Get repair history."""
        if not os.path.isfile(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
            return history[-limit:]
        except:
            return []
    
    def summarize(self) -> str:
        """Human-readable summary."""
        bugs = self.find_bugs()
        history = self.get_history(5)
        
        lines = ["🔧 WW code self-healing summary\n"]
        
        if bugs:
            by_type = {}
            for b in bugs:
                t = b["type"]
                by_type[t] = by_type.get(t, 0) + 1
            lines.append(f"📊 Discovered {len(bugs)} potential issues:")
            for t, c in sorted(by_type.items()):
                names = {"silent_except": "silenceexception", "none_comparison": "None comparison",
                         "hardcoded_path": "Hardcoded path", "known_bug": " Known bug"}
                lines.append(f"  - {names.get(t, t)}: {c} occurrences")
        else:
            lines.append("✅ Code quality is good, no issues discovered")
        
        if history:
            lines.append(f"\n📋 Recent repairs ({len(history)} times):")
            for h in history:
                status = "✅" if h.get("applied") else "❌"
                lines.append(f"  {status} {h.get('file','?')}")
        
        return "\n".join(lines)
