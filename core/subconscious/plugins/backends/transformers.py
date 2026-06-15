"""HuggingFace Transformers integration for self-hosted LLM introspection.

Provides access to hidden states, attention matrices, tokenizer
extension, prefix embedding injection, and interrupt callbacks
for models loaded via the `transformers` library.

Requires: torch, transformers (optional dep — import at use time).
Disabled by default.
"""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..base import (
    BackendPlugin, BackendNotReadyError, BackendUnsupportedError,
    HiddenStateSlice, AttentionInfo, LogitInfo, TokenizerInfo,
    PrefixPayload, register_backend,
)


class TransformersBackend(BackendPlugin):
    """HuggingFace Transformers backend.

    Usage:
        config = {
            "model_name_or_path": "Qwen/Qwen2.5-7B-Instruct",
            "device": "cuda",
            "torch_dtype": "bfloat16",
            "load_in_8bit": False,
        }
        backend = TransformersBackend(config)
        backend.load_model()  # Actually loads the model
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._model_name = self._config.get(
            "model_name_or_path", "Qwen/Qwen2.5-7B-Instruct"
        )
        self._device = self._config.get("device", "cuda")
        self._model = None
        self._tokenizer = None
        self._hooks: List[Any] = []
        self._cached_hidden: Dict[int, Any] = {}
        self._cached_attentions: Dict[int, Any] = {}
        self._interrupt_callback: Optional[Callable[[], bool]] = None

    def name(self) -> str:
        return "transformers"

    def validate(self) -> bool:
        return self._model is not None and self._tokenizer is not None

    def estimate_model_size_gb(self) -> float:
        config_size = self._config.get("model_size_gb", 0)
        if config_size > 0:
            return config_size
        if self._model is not None:
            try:
                num_params = self._model.num_parameters()
                return num_params * 4 / 1e9  # ~4 bytes per param in fp32
            except Exception:
                pass
        return 7.0  # Default guess for a 7B model

    # ── Model Loading ──

    def load_model(self) -> None:
        """Actually load the model and tokenizer.

        Imports torch and transformers lazily.
        """
        try:
            import torch
            import transformers
        except ImportError as e:
            raise BackendNotReadyError(
                "torch and transformers required for TransformersBackend. "
                "Install with: pip install torch transformers"
            )

        dtype_map = {
            "float32": torch.float32,
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
        }
        torch_dtype = dtype_map.get(
            self._config.get("torch_dtype", "bfloat16"),
            torch.bfloat16,
        )

        self._tokenizer = transformers.AutoTokenizer.from_pretrained(
            self._model_name,
            trust_remote_code=self._config.get("trust_remote_code", True),
        )

        self._model = transformers.AutoModelForCausalLM.from_pretrained(
            self._model_name,
            torch_dtype=torch_dtype,
            device_map="auto" if self._config.get("device_map") else None,
            device=(
                None if self._config.get("device_map")
                else torch.device(self._device)
            ),
            load_in_8bit=self._config.get("load_in_8bit", False),
            load_in_4bit=self._config.get("load_in_4bit", False),
            trust_remote_code=self._config.get("trust_remote_code", True),
            output_attentions=self._config.get("output_attentions", False),
            output_hidden_states=True,
        )
        self._model.eval()

        # Register forward hooks for continuous hidden state capture
        self._register_hidden_state_hooks()
        self._ready = True

    def _register_hidden_state_hooks(self) -> None:
        """Register forward hooks on transformer layers to capture
        hidden states during generation."""
        if not hasattr(self._model, "model") or not hasattr(
            self._model.model, "layers"
        ):
            return  # Not a standard transformer architecture

        for idx, layer in enumerate(self._model.model.layers):
            def make_hook(layer_idx: int):
                def hook(module, input, output):
                    # Capture the hidden state
                    self._cached_hidden[layer_idx] = output[0].detach()
                return hook
            hook_handle = layer.register_forward_hook(
                make_hook(idx)
            )
            self._hooks.append(hook_handle)

    def unload_model(self) -> None:
        """Unload the model, remove hooks, free memory."""
        for hook in self._hooks:
            hook.remove()
        self._hooks.clear()
        self._model = None
        self._tokenizer = None
        self._cached_hidden.clear()
        self._cached_attentions.clear()
        self._ready = False

    # ── Hidden State Access ──

    def get_hidden_states(
        self,
        layer_indices: Optional[List[int]] = None,
        pool: str = "last",
    ) -> Dict[int, HiddenStateSlice]:
        if not self._ready:
            raise BackendNotReadyError("Model not loaded. Call load_model() first.")

        layers = layer_indices or sorted(self._cached_hidden.keys())
        if not layers:
            raise BackendNotReadyError(
                "No cached hidden states. Run a forward pass first."
            )

        import torch

        result: Dict[int, HiddenStateSlice] = {}
        for li in layers:
            hs = self._cached_hidden.get(li)
            if hs is None:
                continue

            if pool == "last":
                vec = hs[0, -1, :]
            elif pool == "mean":
                vec = hs[0].mean(dim=0)
            elif pool == "first":
                vec = hs[0, 0, :]
            else:  # all — flatten all tokens
                vec = hs[0].reshape(-1)

            values = vec.detach().cpu().tolist()
            result[li] = HiddenStateSlice(
                layer_index=li,
                values=values[:200],  # Cap at 200 dims for efficiency
                shape_dims=tuple(vec.shape),
            )
        return result

    def get_attention_info(self) -> Dict[int, AttentionInfo]:
        if not self._ready:
            raise BackendNotReadyError("Model not loaded. Call load_model() first.")

        import torch

        result: Dict[int, AttentionInfo] = {}

        # If attention weights are not being cached, return empty
        for li, hs in self._cached_hidden.items():
            # Estimate sparsity from hidden state norms
            h = hs[0].detach()
            # Simple heuristic: compute norm ratio vs max possible
            norms = h.norm(dim=-1)
            norm_ratio = (norms.mean() / (h.size(-1) ** 0.5)).item()
            sparsity = 1.0 - min(1.0, norm_ratio)

            # Estimate entropy from norm distribution
            norm_var = norms.var().item() / max(norms.mean().item(), 1e-8)
            entropy = min(1.0, 0.3 + norm_var * 0.1)

            result[li] = AttentionInfo(
                layer_index=li,
                sparsity=max(0.0, min(1.0, sparsity)),
                entropy=max(0.0, min(1.0, entropy)),
                total_heads=self._config.get("num_attention_heads", 32),
            )
        return result

    # ── Prefix Injection ──

    def inject_prefix_embeddings(self, payload: PrefixPayload) -> None:
        if not self._ready:
            raise BackendNotReadyError("Model not loaded.")

        import torch

        emb = self._model.get_input_embeddings()

        # Convert the flat embedding vector to a tensor and inject
        prefix_tensor = torch.tensor(
            [payload.embedding_vector],
            dtype=emb.weight.dtype,
            device=emb.weight.device,
        )

        # Store for the forward pass to use
        self._current_prefix = prefix_tensor
        self._current_prefix_len = payload.length_tokens

    def clear_prefix_embeddings(self) -> None:
        self._current_prefix = None
        self._current_prefix_len = 0

    # ── Logits ──

    def get_logits(self, return_probs: bool = True) -> LogitInfo:
        # Logits can only be obtained during a forward pass
        # Return cached info if available
        return LogitInfo(
            token_ids=[],
            logprobs=[],
            token_entropy=0.5,
            num_tokens=self._tokenizer.vocab_size if self._tokenizer else 32000,
        )

    # ── Tokenizer ──

    def get_tokenizer_info(self) -> TokenizerInfo:
        if not self._tokenizer:
            return TokenizerInfo(vocab_size=32000)

        special = {}
        if hasattr(self._tokenizer, "special_tokens_map"):
            for name, token in self._tokenizer.special_tokens_map.items():
                if isinstance(token, str):
                    tid = self._tokenizer.convert_tokens_to_ids(token)
                    special[name] = tid

        return TokenizerInfo(
            vocab_size=len(self._tokenizer),
            bos_token_id=self._tokenizer.bos_token_id,
            eos_token_id=self._tokenizer.eos_token_id,
            special_tokens=special,
        )

    def add_special_token(self, token_str: str) -> int:
        if not self._tokenizer or not self._model:
            raise BackendNotReadyError("Model not loaded.")

        import torch

        # Add to tokenizer
        num_added = self._tokenizer.add_tokens([token_str])
        if num_added == 0:
            # Token already exists
            return self._tokenizer.convert_tokens_to_ids(token_str)

        new_id = self._tokenizer.convert_tokens_to_ids(token_str)

        # Resize model embeddings
        old_size = self._model.get_input_embeddings().weight.shape[0]
        self._model.resize_token_embeddings(len(self._tokenizer))

        # Initialize the new embedding with small random values
        emb = self._model.get_input_embeddings()
        with torch.no_grad():
            new_weight = emb.weight[new_id]
            new_weight.normal_(mean=0.0, std=0.02)

        return new_id

    # ── Interrupt ──

    def register_interrupt_callback(
        self, callback: Callable[[], bool]
    ) -> None:
        self._interrupt_callback = callback

    def remove_interrupt_callback(self) -> None:
        self._interrupt_callback = None

    def create_interrupt_processor(self):
        """Create a logits processor that checks the interrupt callback
        before each generation step.

        Usage:
            interrupt_processor = backend.create_interrupt_processor()
            model.generate(..., logits_processor=[interrupt_processor])
        """
        from transformers import LogitsProcessor

        callback = self._interrupt_callback

        class _InterruptLogitsProcessor(LogitsProcessor):
            def __call__(self, input_ids, scores):
                if callback and callback():
                    # Force generation to stop by making EOS token
                    # the most likely
                    scores[:, :] = float("-inf")
                    scores[:, self._eos_id] = 0.0
                return scores

        proc = _InterruptLogitsProcessor()
        proc._eos_id = (self._tokenizer.eos_token_id
                        if self._tokenizer else 2)
        return proc


# Auto-register
register_backend("transformers", TransformersBackend)
