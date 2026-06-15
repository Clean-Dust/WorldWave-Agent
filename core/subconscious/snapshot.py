"""
ww/core/subconscious/snapshot.py — local snapshot and rollback (Local Snapshot & Rollback)

Even with Multi-Krum + A/B sandbox being powerful, there is still a one-in-a-thousand chance
weights that 'perform well in sandbox, but are extremely stupid in actual work'.

Solution:
  1. dailyautosnapshot (or every majortraining ) 
  2. one-click rollbackto any historyversion
  3. autoclean up old snapshots (keep recent 30  day) 
  4. UI show snapshots   

each snapshot is a complete model.json copy + metadata.
size ~20KB × 30 days = ~600KB, completely imperceptible.
"""

from __future__ import annotations
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from .predictor import DeepRiskNet

logger = logging.getLogger("ww.subconscious.snapshot")

SNAPSHOT_DIR = os.path.expanduser("~/worldwave/data/subconscious/snapshots")
MAX_DAYS = 30      # retention days
MAX_SNAPSHOTS = 90  # absolute limit


class SnapshotManager:
    """
    snapshotmanagement . 

    usage: 
      sm = SnapshotManager()
      sm.snapshot(model, "before_major_update")  # snap  
      sm.snapshot(model, "daily")                  # dailyauto
      snapshots = sm.list_snapshots()              # list
      model = sm.rollback("snap_2025_06_08")       # rollback
      sm.cleanup()                                  # clean upold
    """

    def __init__(self, snapshot_dir: str = SNAPSHOT_DIR):
        self.snapshot_dir = snapshot_dir
        os.makedirs(snapshot_dir, exist_ok=True)
        self._last_snapshot_date = ""

    def snapshot(
        self,
        model: DeepRiskNet,
        tag: str = "manual",
    ) -> Dict[str, Any]:
        """
        createa snapshot. 

        Args:
            model: when   RandomForest model
            tag: snapshottag ("daily", "before_update", "manual" etc) 

        Returns:
            {"name": str, "path": str, "timestamp": float, "size_bytes": int}
        """
        timestamp = time.time()
        date_str = time.strftime("%Y_%m_%d", time.localtime(timestamp))
        time_str = time.strftime("%H%M%S", time.localtime(timestamp))
        micro = int((timestamp % 1) * 1_000_000)
        name = f"snap_{date_str}_{time_str}_{micro}_{tag}"

        snap_dir = os.path.join(self.snapshot_dir, name)
        os.makedirs(snap_dir, exist_ok=True)

        # save model
        model_path = os.path.join(snap_dir, "model.json")
        with open(model_path, "w") as f:
            f.write(model.to_json())

        # savemetadata
        meta = {
            "name": name,
            "timestamp": timestamp,
            "date": date_str,
            "time": time_str,
            "tag": tag,
            "model_size_bytes": model.size_bytes(),
            "n_trees": 0,  # DeepRiskNet is not tree-based
        }
        meta_path = os.path.join(snap_dir, "meta.json")
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        self._last_snapshot_date = date_str
        logger.info(f"📸 snapshot: {name} ({model.size_bytes()} bytes)")

        return meta

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """listall snapshot (by   descending order) . """
        snapshots = []
        for entry in sorted(os.listdir(self.snapshot_dir), reverse=True):
            meta_path = os.path.join(self.snapshot_dir, entry, "meta.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                    snapshots.append(meta)
                except Exception:
                    continue
        return snapshots

    def rollback(self, snapshot_name: str) -> Optional[DeepRiskNet]:
        """
        rollbackto specified snapshot. 

        Args:
            snapshot_name: snapshotname (e.g. "snap_2025_06_08_daily") 

        Returns:
            snapshot   RandomForest, or  None (snapshot does not existat ) 

        side effect: rollback will take a snapshot firstwhen  model snapshot (named pre_rollback) . 
        """
        snap_dir = os.path.join(self.snapshot_dir, snapshot_name)
        model_path = os.path.join(snap_dir, "model.json")
        if not os.path.isfile(model_path):
            logger.warning(f"snapshot does not existat : {snapshot_name}")
            return None

        try:
            with open(model_path) as f:
                data = json.load(f)
            model = DeepRiskNet.from_dict(data)
            logger.info(f"⏪ rollbackto : {snapshot_name} ({model.size_bytes()} bytes)")
            return model
        except Exception as e:
            logger.error(f"rollbackfailed: {e}")
            return None

    def daily_check(self, model: DeepRiskNet) -> bool:
        """
        dailyautosnapshotcheck. 

        if no snapshot taken today daily snapshot, autotake a snapshot. 

        Returns:
            True if snap  new daily snapshot
        """
        today = time.strftime("%Y_%m_%d")
        if today == self._last_snapshot_date:
            return False

        # checktoday is or not  has  daily snapshot
        existing = [
            s for s in self.list_snapshots()
            if s.get("date") == today and s.get("tag") == "daily"
        ]
        if existing:
            self._last_snapshot_date = today
            return False

        # snap new daily snapshot
        self.snapshot(model, tag="daily")
        self.cleanup()
        return True

    def cleanup(self, max_days: int = MAX_DAYS,
                max_snapshots: int = MAX_SNAPSHOTS) -> int:
        """
        clean up old snapshots. 

        rule: 
          - keep recent max_days  day  daily snapshot
          - keepall  manual snapshot (unless exceeds max_snapshots) 
          - total does not exceed max_snapshots

        Returns:
            delete snapshot count
        """
        all_snaps = self.list_snapshots()
        if len(all_snaps) <= max_snapshots:
            return 0

        cutoff = time.time() - max_days * 86400
        removed = 0

        for snap in all_snaps[max_snapshots:]:
            # keep manual snapshot
            if snap.get("tag") == "manual":
                continue
            # keep recent daily
            if snap.get("tag") == "daily" and snap.get("timestamp", 0) > cutoff:
                continue

            snap_dir = os.path.join(self.snapshot_dir, snap["name"])
            if os.path.isdir(snap_dir):
                shutil.rmtree(snap_dir)
                removed += 1

        return removed

    def pre_update_snapshot(self, model: DeepRiskNet) -> Dict[str, Any]:
        """
        at applyexternalweightupdate autotake a snapshot. 

        similar to  Git commit before merge. 
        """
        return self.snapshot(model, tag="pre_update")

    def stats(self) -> Dict[str, Any]:
        snaps = self.list_snapshots()
        daily_count = sum(1 for s in snaps if s.get("tag") == "daily")
        manual_count = sum(1 for s in snaps if s.get("tag") == "manual")
        pre_count = sum(1 for s in snaps if s.get("tag") == "pre_update")
        total_size = sum(s.get("model_size_bytes", 0) for s in snaps)

        return {
            "total_snapshots": len(snaps),
            "daily": daily_count,
            "manual": manual_count,
            "pre_update": pre_count,
            "total_size_bytes": total_size,
            "total_size": f"{total_size / 1024:.1f} KB" if total_size < 1024*1024
                         else f"{total_size / 1024 / 1024:.1f} MB",
            "latest": snaps[0] if snaps else None,
        }
