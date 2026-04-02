from __future__ import annotations

from pathlib import Path


def test_env_example_covers_core_bot_runtime_keys():
    content = Path(".env.example").read_text(encoding="utf-8")

    expected_keys = [
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "OMICSCLAW_MODEL",
        "OMICSCLAW_LLM_TIMEOUT_SECONDS",
        "TELEGRAM_BOT_TOKEN",
        "FEISHU_APP_ID",
        "DINGTALK_CLIENT_ID",
        "DISCORD_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "WECOM_CORP_ID",
        "WECHAT_APP_ID",
        "QQ_APP_ID",
        "EMAIL_IMAP_HOST",
        "IMESSAGE_CLI_PATH",
        "ALLOWED_SENDERS",
        "GLOBAL_RATE_LIMIT",
        "OMICSCLAW_DATA_DIRS",
        "OMICSCLAW_MEMORY_DB_URL",
        "OMICSCLAW_MAX_HISTORY",
        "OMICSCLAW_MAX_HISTORY_CHARS",
        "OMICSCLAW_MAX_TOOL_ITERATIONS",
    ]

    for key in expected_keys:
        assert key in content, f"{key} missing from .env.example"
