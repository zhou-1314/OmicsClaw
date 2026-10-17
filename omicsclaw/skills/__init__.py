"""Load skill instructions and definitions.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from .discovery import (
    DEFAULT_SKILL_DISCOVERY_TOP_K,
    SKILL_DISCOVERY_MIN_SCORE,
    SPAN_SKILL_DISCOVERY,
    SkillDiscovery,
)
from .frontmatter import Frontmatter, parse_frontmatter
from .index import SkillIndex, SkillNotFound, SkipReason, SkippedSkill
from .loader import SKILL_FILENAME, SkillLoadError, load_skills
from .skill import Skill
from .search_skill import SEARCH_SKILL_SCHEMA, SEARCH_SKILL_TOOL_NAME, search_skill_tool
from .use_skill import USE_SKILL_SCHEMA, USE_SKILL_TOOL_NAME, use_skill_tool

__all__ = [
    "DEFAULT_SKILL_DISCOVERY_TOP_K",
    "Frontmatter",
    "SEARCH_SKILL_SCHEMA",
    "SEARCH_SKILL_TOOL_NAME",
    "SKILL_FILENAME",
    "Skill",
    "SkillDiscovery",
    "SkillIndex",
    "SkillLoadError",
    "SkillNotFound",
    "SkipReason",
    "SkippedSkill",
    "USE_SKILL_SCHEMA",
    "USE_SKILL_TOOL_NAME",
    "load_skills",
    "parse_frontmatter",
    "search_skill_tool",
    "use_skill_tool",
]
