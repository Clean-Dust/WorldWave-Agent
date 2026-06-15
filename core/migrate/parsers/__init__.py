"""Source-system configuration parsers.

Each parser handles one source system's native configuration format:
- openclaw: JSON5 (openclaw.json, with $include support)
- hermes:   YAML (config.yaml) + Markdown persona files + SQLite memory
- claude:   Markdown (CLAUDE.md) + JSON (settings)
- codex:    Directory-based (.agents/skills/) with JSON metadata
"""
