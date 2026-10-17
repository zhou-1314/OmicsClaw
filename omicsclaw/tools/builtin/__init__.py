"""Expose built-in tools for files, shell commands and other runtime services.

File tools share workspace resolution and path locks. Web tools share URL
validation and HTML reduction. Destination checks do not decide whether a
request may send data; tool policy and the deployment's approval gate do.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from .ask_user import AskUserTool
from .bash import BashTool
from .edit import EditTool
from .read import read_tool
from .web_fetch import WebFetchTool
from .web_search import WebSearchTool
from .write import WriteTool

__all__ = [
    "AskUserTool",
    "BashTool",
    "EditTool",
    "WebFetchTool",
    "WebSearchTool",
    "WriteTool",
    "read_tool",
]
