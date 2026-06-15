"""ww/core/persistence.py — WW session persistence v0.1
Allow WW to auto-recover to pre-crash state after crash.
"""

import json
import os
import time
import shutil
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any

PERSIST_DIR = os.path.expanduser("~/.ww_data")
SESSION_FILE = os.path.join(PERSIST_DIR, "session.json")
CHECKPOINT_FILE = os.path.join(PERSIST_DIR, "checkpoint.json")
BACKUP_DIR = os.path.join(PERSIST_DIR, "backups")


class SessionPersistence:
    """WW session persistence.
    
    Auto-save:
    - When working phase (goal, spiral count, phase)
    - Metric history
    - configuration
    - scheduletask
    """
    
    def __init__(self, persist_dir: str = PERSIST_DIR):
        self.persist_dir = persist_dir
        self._lock = threading.Lock()
        self._save_interval = 30  # Auto-save interval (seconds)
        self._last_save = 0
        os.makedirs(persist_dir, exist_ok=True)
        os.makedirs(BACKUP_DIR, exist_ok=True)
    
    def save_session(self, state: Dict):
        """Save when working phase."""
        with self._lock:
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "version": "0.3.0",
                "state": state,
            }
            # Atomic write (write temp file first, then rename)
            tmp = SESSION_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, SESSION_FILE)
    
    def load_session(self) -> Optional[Dict]:
        """Load next working phase."""
        if not os.path.isfile(SESSION_FILE):
            return None
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            return data.get("state")
        except Exception:
            return None
    
    def save_checkpoint(self, checkpoint: Dict):
        """Save checkpoint (for task crash recovery)."""
        with self._lock:
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "checkpoint": checkpoint,
            }
            tmp = CHECKPOINT_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, CHECKPOINT_FILE)
    
    def load_checkpoint(self) -> Optional[Dict]:
        """Load checkpoint."""
        if not os.path.isfile(CHECKPOINT_FILE):
            return None
        try:
            with open(CHECKPOINT_FILE) as f:
                data = json.load(f)
            return data.get("checkpoint")
        except Exception:
            return None
    
    def clear_checkpoint(self):
        """Clear checkpoint upon task completion."""
        if os.path.isfile(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)
    
    def auto_save_loop(self, ww):
        """ Background thread: periodically save WW state."""
        
        def _loop():
            while True:
                time.sleep(self._save_interval)
                try:
                    if ww and ww.running:
                        state = {
                            "current_spiral": ww.state.current_spiral,
                            "current_phase": ww.state.current_phase,
                            "session_id": ww.state.session_id,
                        }
                        self.save_session(state)
                except Exception:
                    pass
        
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
    
    def backup_config(self, config: Dict):
        """Backup configuration."""
        backup_path = os.path.join(
            BACKUP_DIR,
            f"config.{int(time.time())}.json"
        )
        with open(backup_path, "w") as f:
            json.dump(config, f, indent=2)
        
        # Keep only the most recent 10 copies
        backups = sorted(
            [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
             if f.startswith("config.")],
            key=os.path.getmtime
        )
        while len(backups) > 10:
            os.remove(backups.pop(0))
    
    def recovery_check(self) -> Dict:
        """Check if recovery is needed. Return recovery suggestion."""
        result = {"needs_recovery": False, "type": None, "data": None}
        
        session = self.load_session()
        checkpoint = self.load_checkpoint()
        
        if checkpoint:
            result["needs_recovery"] = True
            result["type"] = "checkpoint"
            result["data"] = checkpoint
            result["message"] = "Detected crash with incomplete task"
        
        elif session:
            result["needs_recovery"] = True
            result["type"] = "session"
            result["data"] = session
            result["message"] = "Detected previous session"
        
        return result
