"""core/computer_use/config.py — Tier configuration for WW Computer Use

Defines the 7-tier progressive architecture. Users set WW_COMPUTER_USE_TIER
env var (0-6) to choose their capability level. Higher tiers enable more
features but require more hardware.

Tier 0 — Basic: GDI screenshot, manual coordinates, no automation
Tier 1 — Fast Capture: MSS/DXGI screenshot (~30ms vs 200ms GDI)
Tier 2 — UI Aware: + UIAutomation element tree extraction
Tier 3 — Set-of-Mark: + Numbered labels on screenshot for vision model
Tier 4 — Visual Feedback: + Post-action pixel diff verification
Tier 5 — Spatio-Temporal Memory: + Virtual canvas, scroll memory
Tier 6 — Edge Cerebellum: + Local 2B-8B VLM (requires GPU)
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Optional


# ── Tier definitions ────────────────────────────────────────────

@dataclass
class ComputerUseConfig:
    """Complete Computer Use configuration derived from tier selection.

    Each feature maps to one or more implementation choices. Higher tiers
    override lower-tier defaults automatically.
    """
    # ── Capture engine ──
    capture_method: str = "gdi"        # "gdi" | "dxgi" | "video"
    capture_fps: int = 10              # Frames per second (video mode)

    # ── Semantic extraction ──
    use_uia: bool = False              # Windows UIAutomation
    use_dom: bool = False              # Browser DOM extraction

    # ── Grounding / SoM ──
    use_som: bool = False              # Set-of-Mark numbering
    use_omniparser: bool = False       # OmniParser (needs GPU)

    # ── Reasoning ──
    vision_model: str = "qwen/qwen2.5-vl-72b-instruct"
    vision_provider: str = "openrouter"
    vision_max_steps: int = 20

    # ── Verification ──
    verify_action: bool = False        # Post-action pixel diff check
    verify_timeout: float = 1.0        # Seconds to wait after action
    verify_change_threshold: float = 0.01  # Min pixel change fraction

    # ── Memory ──
    use_action_history: bool = True    # Always on at any tier
    use_spacetime_memory: bool = False # Virtual canvas for scroll
    spacetime_canvas_width: int = 4096
    spacetime_canvas_height: int = 16384

    # ── Edge VLM ──
    use_cerebellum: bool = False       # Local 2B-8B VLM (GPU required)
    cerebellum_model: str = ""         # e.g. "microsoft/Florence-2-base"
    cerebellum_device: str = "cuda"    # "cuda" | "cpu"

    # ── Vision engine ──
    use_main_llm_vision: Optional[bool] = None  # None=auto, True=main LLM, False=external API
    main_llm_model: str = ""                     # Current model (auto-detected)

    # ── Browser / CDP ──
    use_cdp: bool = True
    use_stealth: bool = True

    @classmethod
    def from_tier(cls, tier: int) -> ComputerUseConfig:
        """Build config from a single tier number (0-6)."""
        cfg = cls()

        if tier >= 1:
            cfg.capture_method = "dxgi"

        if tier >= 2:
            cfg.use_uia = True

        if tier >= 3:
            cfg.use_som = True

        if tier >= 4:
            cfg.verify_action = True

        if tier >= 5:
            cfg.use_spacetime_memory = True

        if tier >= 6:
            cfg.use_cerebellum = True
            cfg.capture_method = "video"

        return cfg

    @classmethod
    def from_env(cls) -> ComputerUseConfig:
        """Build config from environment variables.

        Priority:
          1. WW_COMPUTER_USE_TIER (0-6) — quick preset
          2. Individual WW_CU_* vars — fine-grained override
        """
        tier_str = os.environ.get("WW_COMPUTER_USE_TIER", "1")
        try:
            tier = int(tier_str)
            tier = max(0, min(6, tier))
            cfg = cls.from_tier(tier)
        except (ValueError, TypeError):
            cfg = cls()

        # Individual overrides (fully capitalised env names)
        overrides = {
            "capture_method": "WW_CU_CAPTURE",
            "use_uia": "WW_CU_UIA",
            "use_som": "WW_CU_SOM",
            "use_omniparser": "WW_CU_OMNIPARSER",
            "verify_action": "WW_CU_VERIFY",
            "use_spacetime_memory": "WW_CU_SPACETIME",
            "use_cerebellum": "WW_CU_CEREBELLUM",
            "vision_model": "WW_CU_VISION_MODEL",
            "vision_max_steps": "WW_CU_MAX_STEPS",
            "use_cdp": "WW_CU_CDP",
            "use_main_llm_vision": "WW_CU_MAIN_LLM_VISION",
            "main_llm_model": "WW_CU_MAIN_MODEL",
        }
        for attr, env in overrides.items():
            val = os.environ.get(env)
            if val is not None:
                if attr == "use_main_llm_vision":
                    # Optional[bool]: explicit yes/no, or "auto" for None
                    if val.lower() in ("auto", "detect"):
                        setattr(cfg, attr, None)
                    else:
                        setattr(cfg, attr, val.lower() in ("1", "true", "yes", "on"))
                elif isinstance(getattr(cfg, attr), bool):
                    setattr(cfg, attr, val.lower() in ("1", "true", "yes", "on"))
                elif isinstance(getattr(cfg, attr), int):
                    try:
                        setattr(cfg, attr, int(val))
                    except ValueError:
                        pass
                else:
                    setattr(cfg, attr, val)

        return cfg

    def describe(self) -> list[str]:
        """Human-readable feature summary."""
        lines = []
        lines.append(f"Capture: {self.capture_method}")
        lines.append(f"UIA: {'on' if self.use_uia else 'off'}")
        lines.append(f"SoM: {'on' if self.use_som else 'off'}")
        lines.append(f"Verify: {'on' if self.verify_action else 'off'}")
        lines.append(f"Spacetime Memory: {'on' if self.use_spacetime_memory else 'off'}")
        lines.append(f"Cerebellum: {'on' if self.use_cerebellum else 'off'}")
        lines.append(f"Vision: {self.vision_model}")
        engine = "main LLM" if self.use_main_llm_vision else "external API"
        if self.use_main_llm_vision is None:
            engine = f"auto (model={self.main_llm_model or '?'})"
        lines.append(f"Vision engine: {engine}")
        return lines


# ── Helper: resolve the active config once ───────────────────────

_ACTIVE_CONFIG: Optional[ComputerUseConfig] = None


def get_config() -> ComputerUseConfig:
    global _ACTIVE_CONFIG
    if _ACTIVE_CONFIG is None:
        _ACTIVE_CONFIG = ComputerUseConfig.from_env()
    return _ACTIVE_CONFIG


def reload_config():
    """Force re-read from env (useful after config change)."""
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = ComputerUseConfig.from_env()
