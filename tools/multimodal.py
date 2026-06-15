"""Multimodal Coding Tools — vision-to-code pipeline."""

from __future__ import annotations
import os

from tools.registry import ToolRegistry, ToolDef
from core.multimodal_coding import get_multimodal_coder, VisionAnalysis, MultimodalCodeResult


def register_tools(registry: ToolRegistry):

    _mc = get_multimodal_coder()

    def handle_vision_analyze(image_path: str, question: str = "", **kwargs) -> dict:
        """Analyze an image for code generation."""
        try:
            image_path = os.path.expanduser(image_path)
            if not os.path.exists(image_path):
                return {"error": f"Image not found: {image_path}"}
            analysis = _mc.analyze_image(image_path, question)
            return {
                "description": analysis.description,
                "ui_components": analysis.ui_components,
                "layout": analysis.layout,
                "colors": analysis.colors,
                "text_content": analysis.text_content,
                "suggested_structure": analysis.suggested_structure,
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_image_to_code(
        image_path: str,
        instruction: str = "",
        language: str = "",
        **kwargs,
    ) -> dict:
        """Generate code from an image (UI mockup, diagram, etc.)."""
        try:
            image_path = os.path.expanduser(image_path)
            if not os.path.exists(image_path):
                return {"error": f"Image not found: {image_path}"}
            result = _mc.image_to_code(image_path, instruction, language)
            return {
                "language": result.language,
                "code": result.generated_code[:3000],
                "analysis": result.analysis.description[:300],
                "ui_components": result.analysis.ui_components,
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_analyze_screenshot(image_path: str, **kwargs) -> dict:
        """Analyze a UI screenshot for frontend code generation."""
        try:
            image_path = os.path.expanduser(image_path)
            if not os.path.exists(image_path):
                return {"error": f"Image not found: {image_path}"}
            analysis = _mc.analyze_screenshot(image_path)
            return {
                "description": analysis.description,
                "ui_components": analysis.ui_components,
                "layout": analysis.layout,
                "colors": analysis.colors,
                "suggested_structure": analysis.suggested_structure,
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_analyze_diagram(image_path: str, **kwargs) -> dict:
        """Analyze an architecture diagram for backend structure."""
        try:
            image_path = os.path.expanduser(image_path)
            if not os.path.exists(image_path):
                return {"error": f"Image not found: {image_path}"}
            analysis = _mc.analyze_diagram(image_path)
            return {
                "description": analysis.description,
                "components": analysis.ui_components,
                "layout": analysis.layout,
                "suggested_structure": analysis.suggested_structure,
            }
        except Exception as e:
            return {"error": str(e)}

    def handle_analyze_error_screenshot(image_path: str, **kwargs) -> dict:
        """Analyze an error screenshot and suggest fixes."""
        try:
            image_path = os.path.expanduser(image_path)
            if not os.path.exists(image_path):
                return {"error": f"Image not found: {image_path}"}
            analysis = _mc.analyze_error(image_path)
            return {
                "description": analysis.description,
                "suggested_fix": analysis.suggested_structure,
            }
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="vision_analyze",
        description="Analyze an image for code generation. Extracts UI components, layout, colors, and text.",
        handler=handle_vision_analyze,
        parameters={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to image file."},
                "question": {"type": "string", "description": "Optional question about the image.", "default": ""},
            },
            "required": ["image_path"],
        },
        category="cognitive",
    ))

    registry.register(ToolDef(
        name="image_to_code",
        description="Generate code from an image — UI mockup → frontend, diagram → backend structure.",
        handler=handle_image_to_code,
        parameters={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to image."},
                "instruction": {"type": "string", "description": "Additional instructions (e.g., 'Use React+Tailwind').", "default": ""},
                "language": {"type": "string", "description": "Target language (auto-detected if empty).", "default": ""},
            },
            "required": ["image_path"],
        },
        category="cognitive",
    ))

    registry.register(ToolDef(
        name="analyze_screenshot",
        description="Analyze a UI screenshot: detect components, layout, colors. Ready for frontend generation.",
        handler=handle_analyze_screenshot,
        parameters={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to screenshot."},
            },
            "required": ["image_path"],
        },
        category="cognitive",
    ))

    registry.register(ToolDef(
        name="analyze_diagram",
        description="Analyze an architecture diagram: detect services, data flows, suggest project structure.",
        handler=handle_analyze_diagram,
        parameters={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to diagram image."},
            },
            "required": ["image_path"],
        },
        category="cognitive",
    ))

    registry.register(ToolDef(
        name="analyze_error_screenshot",
        description="Analyze an error screenshot: identify error type, file, line, and suggest fixes.",
        handler=handle_analyze_error_screenshot,
        parameters={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to error screenshot."},
            },
            "required": ["image_path"],
        },
        category="cognitive",
    ))
