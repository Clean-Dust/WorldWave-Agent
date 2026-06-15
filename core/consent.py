"""
ww/core/consent.py — User consent management (Consent Manager)

Antivirus false positive core contradiction:
  Worldwave line is (backgroundservice, P2P network, dynamicloadexternalweight) and malware line is
  Exactly the same. The only difference is "whether the user knows and consents".

This module ensures Worldwave obtains explicit user consent before performing any "suspicious" operation.
Consent log written to ~/.worldwave/consent.json, irreversible line (once consented, cannot be revoked design
Detrimental to security, therefore we allow users to revoke consent at any time).

usage: 
  consent = ConsentManager()
  if not consent.check("p2p_network"):
      print("Please execute first: python -m ww setup")
      sys.exit(1)
"""

from __future__ import annotations
import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger("ww.consent")

CONSENT_DIR = os.path.expanduser("~/.worldwave")
CONSENT_FILE = os.path.join(CONSENT_DIR, "consent.json")

# Define all features requiring user consent
CONSENT_ITEMS = {
    "p2p_network": {
        "label": "P2P decentralized network",
        "description": "Allow Worldwave to connect to public Nostr relay stations, and other users globally "
                       "subconscious model line sync.\n"
                       "This involves:\n"
                       "  - background WebSocket connection to multiple unfamiliar servers\n"
                       "  - receive model updates from other users\n"
                       "  - broadcast local optimized weights (protected by differential privacy, without any code or conversation content)\n"
                       "If not agreed, subconscious only runs locally, does not participate in federated learning.",
        "risk": "Low — only transmits encrypted model weights (KB level), no personal data",
        "antivirus_note": "Antivirus software may misjudge P2P connection as Botnet/C&C communication."
                          "This is a known False Positive.",
    },
    "model_broadcast": {
        "label": "Model weight sharing",
        "description": "Allow local subconscious model to optimize weights (protected by differential privacy)"
                       "broadcastto  Nostr network. \n"
                       "  - Without any code, conversation records or personal data\n"
                       "  - Weights undergo Laplace noise injection (ε=3.0), reverse engineering not possible\n"
                       "  - Each broadcast requires PoW computation (5-10 seconds CPU)",
        "risk": "Low — DP protection ensures training data cannot be reverse-engineered",
    },
    "auto_update": {
        "label": "Auto-receive community optimization",
        "description": "Allow Worldwave to automatically receive model updates from other users on the P2P network.\n"
                       "All external updates will first be validated by local sandbox (Validation Set test),"
                       "Only updates that perform better than the local model will be adopted.",
        "risk": "  — but still protected by sandbox + Multi-Krum defense",
    },
}


class ConsentManager:
    """
    User consent management.

    Trace whether the user has explicitly consented to each feature.
    All consent decisions are persisted to ~/.worldwave/consent.json.
    """

    def __init__(self, consent_file: str = CONSENT_FILE):
        self.consent_file = consent_file
        self._consent: Dict[str, bool] = {}
        self._loaded = False

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.consent_file), exist_ok=True)

    def load(self):
        """Load and save consent."""
        if self._loaded:
            return
        self._ensure_dir()
        if os.path.exists(self.consent_file):
            try:
                with open(self.consent_file) as f:
                    data = json.load(f)
                    self._consent = data.get("consent", {})
                    logger.info(f" Load consent settings: {len(self._consent)} items")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Consent file read failed: {e}")
                self._consent = {}
        self._loaded = True

    def save(self):
        """Save consent to disk."""
        self._ensure_dir()
        data = {
            "version": 1,
            "consent": self._consent,
        }
        with open(self.consent_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Consent settings saved: {self.consent_file}")

    def check(self, feature: str) -> bool:
        """
        Check if the user has consented to a feature.

        Args:
            feature: featurename (p2p_network, model_broadcast, auto_update) 

        Returns:
            True if user consents
        """
        self.load()
        return self._consent.get(feature, False)

    def grant(self, feature: str):
        """User consents to a feature."""
        self.load()
        self._consent[feature] = True
        self.save()

    def revoke(self, feature: str):
        """User revokes consent."""
        self.load()
        self._consent[feature] = False
        self.save()

    def grant_all(self):
        """Consent to all features (for users who trust this software)."""
        for key in CONSENT_ITEMS:
            self.grant(key)

    def is_first_run(self) -> bool:
        """Whether it is the first execution (no consent log yet)."""
        self.load()
        return len(self._consent) == 0

    def pending_items(self) -> List[str]:
        """Return list of features not yet consented."""
        self.load()
        return [k for k in CONSENT_ITEMS if k not in self._consent or not self._consent[k]]

    def summary(self) -> Dict[str, bool]:
        self.load()
        return dict(self._consent)


def print_consent_prompt(feature: str) -> str:
    """Return interactive consent hint text for feature."""
    item = CONSENT_ITEMS.get(feature)
    if not item:
        return f"Unknown feature: {feature}"
    lines = [
        f"╔══ {item['label']} ═══",
        f"║",
        f"║ {item['description']}",
        f"║",
        f"║ Risk level: {item['risk']}",
    ]
    note = item.get("antivirus_note")
    if note:
        lines.append(f"║ Antivirus note: {note}")
    lines.append(f"║")
    lines.append(f"╚══  Allow? [y/N] ")
    return "\n".join(lines)
