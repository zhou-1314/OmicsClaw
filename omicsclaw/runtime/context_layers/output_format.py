"""Output format layer for CLI vs bot mode formatting."""

from __future__ import annotations

from ..context_layers import ContextAssemblyRequest


def build_output_format_layer(request: ContextAssemblyRequest) -> str | None:
    """Build output format instructions based on surface type."""
    if request.surface in ("interactive", "cli"):
        return _get_cli_format_instructions()
    elif request.surface == "bot":
        return _get_bot_format_instructions()
    return None


def _get_cli_format_instructions() -> str:
    return """
## Output Format (CLI Mode)

You are in CLI/terminal mode. Format your responses for plain-text readability:

**Text Formatting:**
- Use plain text without markdown bold (**text**), italics (*text*), or headers (###)
- Avoid emoji entirely — they clutter terminal output
- Use simple ASCII formatting: UPPERCASE for emphasis, --- for separators
- Structure with indentation and blank lines, not markdown syntax

**Lists and Structure:**
- Use simple bullets (-, •) or numbers (1., 2.)
- Indent nested items with 2-4 spaces
- Separate sections with blank lines, not markdown headers

**Code and Paths:**
- File paths and commands can use backticks if needed for clarity
- Keep code blocks minimal — prefer showing just the command to run

**Tone:**
- Direct and concise (10-20 words per sentence)
- Lead with the answer, then explain if needed
- No preamble like "Let me help you with that"

**Example Good CLI Response:**
```
I'll run differential expression analysis using p-value thresholds as you requested.

IMPORTANT: Standard practice uses adjusted p-values (FDR) to control false positives
when testing thousands of genes. Using raw p-values will increase false discoveries.

To proceed, I need:
  1. Data type (Visium, scRNA-seq, bulk RNA-seq, etc.)
  2. File path to your data
  3. Comparison groups (e.g., control vs treatment)
  4. Thresholds: p-value cutoff and log2FC cutoff

Ready when you are.
```

**Example Bad CLI Response (avoid this):**
```
📋 **重要提醒**
根据 RNA-seq 差异表达分析的最佳实践，**通常推荐使用调整 p 值（padj/FDR）**...

---

🧬 **现在开始差异分析**

你需要进行哪种类型的差异分析？

1. **空间转录组学** - 比较不同区域...
```
""".strip()


def _get_bot_format_instructions() -> str:
    return """
## Output Format (Bot Mode)

You are in messaging bot mode (Telegram/Feishu). Format for rich messaging:

**Text Formatting:**
- Use markdown: **bold** for emphasis, *italic* for gene names
- Use emoji sparingly (max 1 per message): 🧬 📊 ✅ ⚠️ 🔬
- Structure with markdown headers (##, ###) and separators (---)

**Tone:**
- Warm and supportive while maintaining scientific rigor
- Use characteristic phrases: "Let's take a look", "Good question!", "Hope that helps!"
- Sign off appropriately based on context

**Lists:**
- Use numbered lists for steps
- Use bullet points for options
- Keep items concise

Follow SOUL.md persona guidelines for voice and expertise boundaries.
""".strip()
