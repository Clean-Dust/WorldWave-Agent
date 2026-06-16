"""ww/core — Worldwave core module

- state.py: state management (checkpointing / HITL / recovery)
- loop.py: Spiral main loop engine
- subconscious/: subconscious meta learning engine
- p2p/: Decentralized P2P networking and blockchain (→ p2p/)
- integrations/: Platform integrations (→ integrations/)
"""

# Note: Subconscious is NOT eagerly imported here to avoid circular imports
# with p2p.federation → core.predictor → core.__init__ → core.subconscious.
# Import directly: from core.subconscious import Subconscious

__all__ = [
    "MemorySystem", "MemoryAtom",
    "ContextWindow", "ConversationManager", "ContextMessage",
    "SpiralContextSummarizer", "estimate_tokens",
    "default_context_manager",
    "DelegationManager", "ChildTask", "ParallelPlanner",
    "Guardrails", "GuardrailsResult",
    "MemoryIntegrator",
    "PromptAssembler", "build_system_prompt",
    "CredentialStore", "mask_secret", "sanitize_output", "get_credential_store",
    "KanbanBoard", "Task",
]

# Context v0.2
from core.context import (
    ContextWindow, ConversationManager, ContextMessage,
    SpiralContextSummarizer, estimate_tokens,
    default_context_manager,
)

# Delegation v0.1
from core.delegation import (
    DelegationManager, ChildTask, ParallelPlanner,
    MAX_CONCURRENT_CHILDREN,
)

# Guardrails v0.1
from core.guardrails import (
    Guardrails, GuardrailsResult,
)

# Memory system v0.1 (built-in)
from core.memory import MemorySystem, MemoryAtom

# Memory integration v0.1 (old bridge layer)
from core.memory_integration import MemoryIntegrator

# Prompts v0.1
from core.prompts import PromptAssembler, build_system_prompt

# Credentials v0.1
from core.credentials import CredentialStore, mask_secret, sanitize_output, get_credential_store

# Kanban v0.1
from core.kanban import KanbanBoard, Task
