"""Test output format layer for CLI vs bot mode."""

from omicsclaw.runtime.context_layers import ContextAssemblyRequest
from omicsclaw.runtime.context_layers.output_format import build_output_format_layer


def test_cli_format_instructions():
    """CLI mode should provide plain-text formatting instructions."""
    request = ContextAssemblyRequest(surface="interactive")
    result = build_output_format_layer(request)

    assert result is not None
    assert "CLI Mode" in result
    assert "plain text" in result.lower()
    assert "avoid emoji" in result.lower()
    assert "markdown bold" in result.lower()
    print("✓ CLI format instructions correct")


def test_bot_format_instructions():
    """Bot mode should provide rich markdown formatting instructions."""
    request = ContextAssemblyRequest(surface="bot")
    result = build_output_format_layer(request)

    assert result is not None
    assert "Bot Mode" in result
    assert "markdown" in result.lower()
    assert "emoji" in result.lower()
    assert "SOUL.md" in result
    print("✓ Bot format instructions correct")


def test_pipeline_mode_no_format():
    """Pipeline mode should not have specific format instructions."""
    request = ContextAssemblyRequest(surface="pipeline")
    result = build_output_format_layer(request)

    # Pipeline mode gets no specific format layer
    assert result is None
    print("✓ Pipeline mode has no format layer")


def test_format_examples():
    """Verify format examples are present in CLI mode."""
    request = ContextAssemblyRequest(surface="interactive")
    result = build_output_format_layer(request)

    assert "Example Good CLI Response" in result
    assert "Example Bad CLI Response" in result
    print("✓ CLI format examples present")


if __name__ == "__main__":
    test_cli_format_instructions()
    test_bot_format_instructions()
    test_pipeline_mode_no_format()
    test_format_examples()
    print("\nAll tests passed!")
