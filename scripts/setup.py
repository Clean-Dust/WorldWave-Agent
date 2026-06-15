"""
ww/scripts/setup.py — Worldwave first-time setup wizard

usage: 
    python -m ww setup

feature: 
    1. Check if it is the first execution
    2. Ask for consent item by item for P2P, model broadcast, autoupdate, etc.
    3. write ~/.worldwave/consent.json
    4. Output firewall/antivirus software setup guide

This is the key to preventing false positives by antivirus software: user proactively consents → clear operation trail →
  Antivirus software "line rating" decreases (not a silent background process).
"""

from __future__ import annotations
import logging
import sys
import os

# Ensure can find to core suite
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), ".."))

from core.consent import ConsentManager, CONSENT_ITEMS, print_consent_prompt

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ww.setup")


def print_header():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║          Worldwave subconsciousnetwork — first-time setup             ║")
    print("╠══════════════════════════════════════════════════════╣")
    print("║  Worldwave uses decentralized P2P network (Nostr)             ║")
    print("║  For subconscious model federated learning.                         ║")
    print("║                                                      ║")
    print("║  Due to involvement of background service + network communication, antivirus software may misjudge.      ║")
    print("║  The following is a description of each feature — please confirm item by item.               ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()


def print_firewall_guide():
    print()
    print("╔══ firewall and antivirus software setup guide ═══════════════════════════════════╗")
    print("║                                                              ║")
    print("║  If antivirus software/firewall shows a warning, please add the following items to the whitelist:        ║")
    print("║                                                              ║")
    print("║  Windows Defender:                                           ║")
    print("║    Settings → Update and Security → Windows Security →                  ║")
    print("║    Firewall and network protection → Allow an app through firewall →            ║")
    print("║    Add Worldwave to the allowed list                                  ║")
    print("║                                                              ║")
    print("║  Nostr relay station domain (WebSocket connection):                       ║")
    print("║    wss://relay.damus.io                                     ║")
    print("║    wss://nos.lol                                             ║")
    print("║    wss://relay.nostr.band                                   ║")
    print("║    wss://relay.snort.social                                 ║")
    print("║                                                              ║")
    print("║  port: WebSocket 443                                       ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()


def print_completion(consent: ConsentManager):
    print()
    print("╔══ setup complete ═══════════════════════════════════════════════════╗")
    print("║                                                              ║")
    summary = consent.summary()
    for key, granted in summary.items():
        item = CONSENT_ITEMS.get(key, {})
        icon = "✓" if granted else "✗"
        status = " Allowed" if granted else " Denied"
        label = item.get("label", key)
        print(f"║  {icon} {label}: {status}                    ")
    print("║                                                              ║")
    print("║   Can be modified later: python -m ww setup --revoke               ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    # Display note items
    if not consent.check("p2p_network"):
        print("⚠  P2P network disabled: subconscious only operates locally.")
    if not consent.check("auto_update"):
        print("⚠  autoupdate disabled: will not receive community model optimizations.")
    if not consent.check("model_broadcast"):
        print("⚠  Model sharing disabled: local optimizations will not broadcast to network.")
    print()


def run_interactive(revoke_mode: bool = False):
    consent = ConsentManager()

    if revoke_mode:
        print("Revoke consent mode: please select the feature to disable.")
        print()
        current = consent.summary()
        for key in CONSENT_ITEMS:
            item = CONSENT_ITEMS[key]
            label = item["label"]
            if current.get(key, False):
                print(f"  [{key}] {label} — current: ✓  Allowed")
            else:
                print(f"  [{key}] {label} — current: ✗  Denied")

        print()
        to_revoke = input("input the feature name to disable (leave blank = cancel all): ").strip()
        if to_revoke:
            consent.revoke(to_revoke)
            print(f"✓  Revoked: {to_revoke}")
        else:
            for key in CONSENT_ITEMS:
                consent.revoke(key)
            print("✓  Revoked all features")
        print_completion(consent)
        return

    if not consent.is_first_run():
        print("Worldwave has already been set up. Current state:")
        print_completion(consent)
        print("Re-setup all features? [y/N] ", end="")
        resp = input().strip().lower()
        if resp != "y":
            print("No changes.")
            return

    print_header()

    results = {}
    for key, item in CONSENT_ITEMS.items():
        while True:
            print(print_consent_prompt(key))
            resp = input().strip().lower()
            if resp in ("y", "yes"):
                results[key] = True
                print("  →  Allowed")
                break
            elif resp in ("", "n", "no"):
                results[key] = False
                print("  →  Denied")
                break

    # write consent
    for key, granted in results.items():
        if granted:
            consent.grant(key)
        else:
            consent.revoke(key)

    print_firewall_guide()
    print_completion(consent)


def run_headless(allowed: bool = True):
    """No interaction mode: directly allow all / reject all."""
    consent = ConsentManager()
    if allowed:
        consent.grant_all()
        print("✓  Allowed all features")
    else:
        for key in CONSENT_ITEMS:
            consent.revoke(key)
        print("✓  Disabled all features")
    print_completion(consent)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Worldwave subconscious network setup wizard",
    )
    parser.add_argument("--allow-all", action="store_true",
                        help="No interaction: allow all features")
    parser.add_argument("--deny-all", action="store_true",
                        help="No interaction: deny all features")
    parser.add_argument("--revoke", action="store_true",
                        help="Revoke consent mode")
    parser.add_argument("--status", action="store_true",
                        help="View current setup state")
    args = parser.parse_args()

    consent = ConsentManager()

    if args.status:
        print_completion(consent)
        return

    if args.allow_all:
        run_headless(allowed=True)
        return

    if args.deny_all:
        run_headless(allowed=False)
        return

    run_interactive(revoke_mode=args.revoke)


if __name__ == "__main__":
    main()
