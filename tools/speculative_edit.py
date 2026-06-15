"""Speculative Edit tools — AI-powered next-edit prediction (Tab completion)."""

from tools.registry import ToolRegistry, ToolDef
from core.speculative_edit import get_speculative_engine


def register_tools(registry: ToolRegistry):

    _engine = get_speculative_engine()

    def handle_predict_edit(
        prefix: str,
        suffix: str = "",
        file_path: str = "",
        language: str = "",
        **kwargs,
    ) -> dict:
        """Predict what code the user will type next."""
        prediction = _engine.predict(
            prefix=prefix,
            suffix=suffix,
            file_path=file_path,
            language=language,
        )
        if prediction:
            return {
                "text": prediction.text,
                "confidence": prediction.confidence,
                "source": prediction.source,
                "display": prediction.display_text,
            }
        return {"text": "", "confidence": 0, "source": "none"}

    def handle_predict_multi(
        prefix: str,
        suffix: str = "",
        file_path: str = "",
        top_k: int = 3,
        **kwargs,
    ) -> dict:
        """Return multiple completion predictions."""
        predictions = _engine.predict_multiple(
            prefix=prefix,
            suffix=suffix,
            file_path=file_path,
            top_k=top_k,
        )
        return {
            "completions": [
                {"text": p.text, "confidence": p.confidence, "source": p.source}
                for p in predictions
            ],
            "total": len(predictions),
        }

    def handle_accept_completion(prediction_text: str, **kwargs) -> dict:
        """Record that user accepted a completion."""
        from core.speculative_edit import EditPrediction
        pred = EditPrediction(text=prediction_text, confidence=1.0, source="user")
        _engine.record_accept(pred)
        return {"accepted": True}

    def handle_reject_completion(prediction_text: str, **kwargs) -> dict:
        """Record that user rejected a completion."""
        from core.speculative_edit import EditPrediction
        pred = EditPrediction(text=prediction_text, confidence=0.0, source="user")
        _engine.record_reject(pred)
        return {"rejected": True}

    registry.register(ToolDef(
        name="predict_edit",
        description="AI-powered next-edit prediction. Predicts what code the user will type next based on context.",
        handler=handle_predict_edit,
        parameters={
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "Code before cursor (last ~1000 chars)."},
                "suffix": {"type": "string", "description": "Code after cursor (next ~500 chars).", "default": ""},
                "file_path": {"type": "string", "description": "Current file path for context.", "default": ""},
                "language": {"type": "string", "description": "Detected language.", "default": ""},
            },
            "required": ["prefix"],
        },
        category="code_search",
    ))

    registry.register(ToolDef(
        name="predict_multi",
        description="Return top-k completion predictions for a completion popup.",
        handler=handle_predict_multi,
        parameters={
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "Code before cursor."},
                "suffix": {"type": "string", "description": "Code after cursor.", "default": ""},
                "file_path": {"type": "string", "description": "Current file path.", "default": ""},
                "top_k": {"type": "integer", "description": "Number of predictions.", "default": 3},
            },
            "required": ["prefix"],
        },
        category="code_search",
    ))

    registry.register(ToolDef(
        name="accept_completion",
        description="Record that user accepted a completion (reinforcement signal).",
        handler=handle_accept_completion,
        parameters={
            "type": "object",
            "properties": {
                "prediction_text": {"type": "string", "description": "The accepted completion text."},
            },
            "required": ["prediction_text"],
        },
        category="code_search",
    ))

    registry.register(ToolDef(
        name="reject_completion",
        description="Record that user rejected a completion.",
        handler=handle_reject_completion,
        parameters={
            "type": "object",
            "properties": {
                "prediction_text": {"type": "string", "description": "The rejected completion text."},
            },
            "required": ["prediction_text"],
        },
        category="code_search",
    ))
