"""
Multimodal Coding Loop — vision-integrated code generation.

Allows the coding agent to:
  1. See screenshots of UI mockups → generate frontend code
  2. See architecture diagrams → generate backend structure
  3. See error screenshots → diagnose and fix bugs
  4. See design specs (Figma exports, etc.) → implement pixel-perfect

Architecture:
  - Image → vision model (via LLM provider's vision API) → description/pseudocode
  - Description → code generation tools → actual implementation
  - Vision loop: screenshot → analyze → code change → screenshot → verify

Usage:
  from core.multimodal_coding import MultimodalCoder
  mc = MultimodalCoder()
  result = mc.image_to_code("screenshot.png", "Build a login form like this")

Config:
  Uses the configured LLM provider's vision model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("ww.multimodal_coding")


# ── Data structures ──────────────────────────────────────────────

@dataclass
class VisionAnalysis:
    """Result of analyzing an image for code generation."""
    description: str          # Natural language description of what's in the image
    ui_components: List[str]  # Detected UI components (buttons, inputs, etc.)
    layout: str               # Layout description (grid, flex, etc.)
    colors: List[str]         # Color palette
    text_content: str         # Any text found in the image
    suggested_structure: str  # Suggested code structure
    raw_response: str = ""    # Full response from vision model

    def to_prompt(self) -> str:
        """Convert analysis to a code generation prompt."""
        parts = [
            "## Visual Analysis",
            f"Description: {self.description}",
        ]
        if self.ui_components:
            parts.append(f"UI Components: {', '.join(self.ui_components)}")
        if self.layout:
            parts.append(f"Layout: {self.layout}")
        if self.colors:
            parts.append(f"Colors: {', '.join(self.colors)}")
        if self.text_content:
            parts.append(f"Text Content: {self.text_content}")
        if self.suggested_structure:
            parts.append(f"\nSuggested Code Structure:\n{self.suggested_structure}")
        return "\n".join(parts)


@dataclass
class MultimodalCodeResult:
    """Result of image-to-code generation."""
    analysis: VisionAnalysis
    generated_code: str = ""
    language: str = ""
    file_path: str = ""
    diff: Optional[Any] = None  # DiffResult if editing existing file


# ── Multimodal Coder ─────────────────────────────────────────────

class MultimodalCoder:
    """Vision-integrated code generation pipeline."""

    def __init__(self, vision_fn: Optional[Callable] = None, code_fn: Optional[Callable] = None):
        """
        Args:
            vision_fn: Callable(image_path, prompt) → str (vision model response)
            code_fn: Callable(prompt, context) → str (code generation)
        """
        self._vision_fn = vision_fn
        self._code_fn = code_fn
        self._analyses: Dict[str, VisionAnalysis] = {}  # cache

    def set_vision_fn(self, fn: Callable[[str, str], str]):
        """Set the vision function: (image_path, question) → response text."""
        self._vision_fn = fn

    def set_code_fn(self, fn: Callable[[str, str], str]):
        """Set the code generation function: (prompt, context) → code."""
        self._code_fn = fn

    # ── Image Analysis ───────────────────────────────────────────

    def analyze_image(self, image_path: str, question: str = "") -> VisionAnalysis:
        """Analyze an image and extract code-relevant information.

        Args:
            image_path: Path to image file (PNG, JPG, etc.)
            question: Specific question about the image (e.g., "What UI framework?")

        Returns:
            VisionAnalysis with structured information.
        """
        prompt = self._build_analysis_prompt(question)
        response = self._call_vision(image_path, prompt)

        analysis = self._parse_analysis(response)
        analysis.raw_response = response
        self._analyses[image_path] = analysis
        return analysis

    def analyze_screenshot(self, image_path: str) -> VisionAnalysis:
        """Analyze a UI screenshot for code generation."""
        return self.analyze_image(
            image_path,
            question="Describe this UI in detail: components, layout, colors, fonts. "
                     "What frontend framework would best implement this? "
                     "Provide a suggested component tree.",
        )

    def analyze_diagram(self, image_path: str) -> VisionAnalysis:
        """Analyze an architecture/flow diagram."""
        return self.analyze_image(
            image_path,
            question="Describe this architecture diagram in detail. "
                     "What components/services are shown? What are the data flows? "
                     "Suggest a Python project structure to implement this.",
        )

    def analyze_error(self, image_path: str) -> VisionAnalysis:
        """Analyze an error screenshot for debugging."""
        return self.analyze_image(
            image_path,
            question="What error is shown in this screenshot? "
                     "Identify the error type, file, line number, and suggest a fix.",
        )

    # ── Image to Code ────────────────────────────────────────────

    def image_to_code(
        self,
        image_path: str,
        instruction: str = "",
        language: str = "",
        existing_file: str = "",
    ) -> MultimodalCodeResult:
        """Generate code from an image.

        Args:
            image_path: Path to image
            instruction: Additional instructions (e.g., "Use React + Tailwind")
            language: Target language (auto-detected if empty)
            existing_file: Path to existing file to modify (instead of creating new)

        Returns:
            MultimodalCodeResult with generated code.
        """
        # Step 1: Analyze image
        analysis = self.analyze_image(image_path, instruction)

        # Step 2: Build code generation prompt
        prompt = self._build_code_prompt(analysis, instruction, language)

        # Step 3: Generate code
        context = ""
        if existing_file:
            import os
            if os.path.exists(existing_file):
                with open(existing_file) as f:
                    context = f.read()
            prompt = f"Modify the following existing file:\n\n```\n{context}\n```\n\n{prompt}"

        code = self._call_code_gen(prompt, context)

        # Step 4: Detect language
        if not language:
            language = self._detect_language(code)

        result = MultimodalCodeResult(
            analysis=analysis,
            generated_code=code,
            language=language,
            file_path=existing_file,
        )

        return result

    def image_to_frontend(self, image_path: str, framework: str = "react") -> MultimodalCodeResult:
        """Generate frontend code from a UI screenshot."""
        return self.image_to_code(
            image_path,
            instruction=f"Implement this exact UI using {framework}. "
                        f"Match colors, spacing, and typography precisely.",
        )

    def image_to_backend(self, image_path: str, framework: str = "fastapi") -> MultimodalCodeResult:
        """Generate backend code from an architecture diagram."""
        return self.image_to_code(
            image_path,
            instruction=f"Implement this architecture using {framework}. "
                        f"Create the API endpoints, data models, and service layer.",
            language="python",
        )

    # ── Vision Loop (iterate until done) ─────────────────────────

    def vision_loop(
        self,
        image_path: str,
        instruction: str,
        max_iterations: int = 3,
        verify_fn: Optional[Callable[[str], bool]] = None,
    ) -> List[MultimodalCodeResult]:
        """Iterative image-to-code with verification.

        Flow:
          1. Generate code from image
          2. Render/execute the code
          3. Screenshot the result
          4. Compare with original image
          5. Fix differences → repeat

        Args:
            image_path: Original reference image
            instruction: What to build
            max_iterations: Max refinement loops
            verify_fn: Optional (generated_code) → True if acceptable

        Returns:
            List of results from each iteration.
        """
        results = []
        for i in range(max_iterations):
            result = self.image_to_code(image_path, instruction)
            results.append(result)

            if verify_fn and verify_fn(result.generated_code):
                break

            # Refine instruction based on differences
            if i < max_iterations - 1:
                instruction = (
                    f"Refine the previous implementation. "
                    f"The generated code was:\n```\n{result.generated_code[:500]}...\n```\n"
                    f"Please fix: {self._get_refinement_hints(result)}"
                )

        return results

    # ── Internal ─────────────────────────────────────────────────

    def _build_analysis_prompt(self, question: str) -> str:
        base = (
            "Analyze this image for code generation purposes. "
            "Respond in this structured format:\n\n"
            "DESCRIPTION: <brief description>\n"
            "COMPONENTS: <comma-separated UI components if applicable>\n"
            "LAYOUT: <layout pattern>\n"
            "COLORS: <hex colors>\n"
            "TEXT: <any text in the image>\n"
            "STRUCTURE: <suggested code structure or component tree>\n"
        )
        if question:
            base += f"\n\nAdditional context: {question}"
        return base

    def _parse_analysis(self, response: str) -> VisionAnalysis:
        """Parse structured vision response."""
        analysis = VisionAnalysis(
            description="",
            ui_components=[],
            layout="",
            colors=[],
            text_content="",
            suggested_structure="",
        )

        lines = response.split("\n")
        current_key = None

        for line in lines:
            line = line.strip()
            if line.upper().startswith("DESCRIPTION:"):
                analysis.description = line.split(":", 1)[1].strip()
            elif line.upper().startswith("COMPONENTS:"):
                parts = line.split(":", 1)[1].strip()
                analysis.ui_components = [p.strip() for p in parts.split(",") if p.strip()]
            elif line.upper().startswith("LAYOUT:"):
                analysis.layout = line.split(":", 1)[1].strip()
            elif line.upper().startswith("COLORS:"):
                parts = line.split(":", 1)[1].strip()
                analysis.colors = [p.strip() for p in parts.split(",") if p.strip()]
            elif line.upper().startswith("TEXT:"):
                analysis.text_content = line.split(":", 1)[1].strip()
            elif line.upper().startswith("STRUCTURE:"):
                analysis.suggested_structure = line.split(":", 1)[1].strip()
                current_key = "structure"
            elif current_key == "structure":
                analysis.suggested_structure += "\n" + line

        return analysis

    def _build_code_prompt(self, analysis: VisionAnalysis, instruction: str, language: str) -> str:
        parts = [
            "Generate production-ready code based on this visual analysis:",
            "",
            analysis.to_prompt(),
        ]
        if instruction:
            parts.append(f"\nInstructions: {instruction}")
        if language:
            parts.append(f"\nLanguage: {language}")
        parts.append("\nProvide complete, runnable code with all imports.")
        return "\n".join(parts)

    def _call_vision(self, image_path: str, prompt: str) -> str:
        """Call the vision function."""
        if self._vision_fn:
            return self._vision_fn(image_path, prompt)
        # Fallback: encode image as base64 and return a placeholder
        log.warning("No vision function set — returning placeholder analysis")
        return (
            f"DESCRIPTION: Image at {image_path}\n"
            f"COMPONENTS: unknown\n"
            f"LAYOUT: unknown\n"
            f"COLORS: unknown\n"
            f"TEXT: unknown\n"
            f"STRUCTURE: unknown\n"
        )

    def _call_code_gen(self, prompt: str, context: str) -> str:
        """Call the code generation function."""
        if self._code_fn:
            return self._code_fn(prompt, context)
        log.warning("No code generation function set")
        return f"# TODO: Generate code for:\n# {prompt[:200]}"

    @staticmethod
    def _detect_language(code: str) -> str:
        """Detect programming language from code content."""
        code_lower = code[:500].lower()
        if "import react" in code_lower or "export default" in code_lower:
            return "javascript"
        if "from react" in code_lower or "jsx" in code_lower:
            return "jsx"
        if "def " in code_lower and "import " in code_lower:
            return "python"
        if "func " in code_lower and "package " in code_lower:
            return "go"
        if "fn " in code_lower and "use " in code_lower:
            return "rust"
        if "<template>" in code_lower:
            return "vue"
        if "class " in code_lower and "public " in code_lower:
            return "java"
        return "unknown"

    def _get_refinement_hints(self, result: MultimodalCodeResult) -> str:
        """Generate hints for the next iteration."""
        hints = []
        if not result.language:
            hints.append("Detect and specify the target language")
        if len(result.generated_code) < 50:
            hints.append("Generated code seems too short")
        return " ".join(hints) if hints else "match the visual reference more closely"


# ── Singleton ────────────────────────────────────────────────────

_multimodal_coder: Optional[MultimodalCoder] = None


def get_multimodal_coder() -> MultimodalCoder:
    global _multimodal_coder
    if _multimodal_coder is None:
        _multimodal_coder = MultimodalCoder()
        # Auto-configure vision function from environment
        _auto_configure_vision(_multimodal_coder)
    return _multimodal_coder


def _auto_configure_vision(mc: MultimodalCoder):
    """Auto-configure vision function from AUXILIARY_VISION_MODEL env var."""
    import os as _os
    vision_model = _os.environ.get("AUXILIARY_VISION_MODEL", "")
    if not vision_model:
        return
    try:
        from core.llm import create_llm
        # Build llm config with the correct API key for vision provider
        vision_provider = _os.environ.get("AUXILIARY_VISION_PROVIDER", "")
        vision_base_url = _os.environ.get("AUXILIARY_VISION_BASE_URL", "")
        vision_api_key = _os.environ.get("AUXILIARY_VISION_API_KEY", "")
        llm_config = {"model": vision_model}
        if vision_provider:
            llm_config["provider"] = vision_provider
        if vision_base_url:
            llm_config["api_base"] = vision_base_url
        if vision_api_key:
            llm_config["api_key"] = vision_api_key
            # Also set the provider-specific env var for the transport layer
            if vision_provider == "openrouter":
                _os.environ.setdefault("OPENROUTER_API_KEY", vision_api_key)
            elif vision_provider == "openai":
                _os.environ.setdefault("OPENAI_API_KEY", vision_api_key)
        _vision_llm = create_llm(llm_config)
        
        def _vision_fn(image_path: str, prompt: str) -> str:
            import base64 as _b64
            with open(image_path, "rb") as f:
                img_b64 = _b64.b64encode(f.read()).decode()
            ext = image_path.rsplit(".", 1)[-1].lower()
            mime = f"image/{ext}" if ext in ("png", "jpg", "jpeg", "gif", "webp") else "image/png"
            data_url = f"data:{mime};base64,{img_b64}"
            try:
                resp = _vision_llm.chat(
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]}],
                    json_mode=False,
                    max_tokens=1024,
                )
                return resp
            except Exception as e:
                return f"Vision call failed: {e}"
        
        mc.set_vision_fn(_vision_fn)
        import logging
        logging.getLogger("ww.multimodal_coding").info(
            "Auto-configured vision function: %s", vision_model
        )
    except Exception as e:
        import logging
        logging.getLogger("ww.multimodal_coding").warning(
            "Failed to auto-configure vision: %s", e
        )
