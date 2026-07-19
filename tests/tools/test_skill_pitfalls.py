"""Tests for skill_pitfalls — the cheap pitfalls/gotchas extractor.

skill_pitfalls(name) returns just the pitfall-flavoured sections of a
skill so the agent can re-check known gotchas before destructive
operations, without paying the full skill_view cost (~30k chars for
the largest skills).

These tests cover:
  * Section heading detection (case-insensitive, multiple aliases)
  * Boundary handling (stop at same/higher heading level, NOT at ###)
  * Multiple matching sections — all returned in document order
  * No-pitfalls fallback — returns intro preview + hint
  * Truncation cap on pathological inputs
  * Pass-through of skill_view error shape for unknown skill
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skills_tool import (
    _extract_pitfall_sections,
    skill_pitfalls,
)


# ---------------------------------------------------------------------------
# Pure extractor (no I/O)
# ---------------------------------------------------------------------------


class TestExtractPitfallSections:
    """Test the pure heading-extraction logic in isolation."""

    def test_common_pitfalls_heading(self):
        content = """# Header

Intro.

## Common Pitfalls

- pitfall one
- pitfall two
"""
        sections = _extract_pitfall_sections(content)
        assert len(sections) == 1
        heading, body = sections[0]
        assert heading == "## Common Pitfalls"
        assert "pitfall one" in body
        assert "pitfall two" in body

    def test_case_insensitive(self):
        content = "## PITFALLS\n\nuppercase heading.\n"
        sections = _extract_pitfall_sections(content)
        assert len(sections) == 1
        assert "uppercase" in sections[0][1]

    def test_multiple_aliases(self):
        """Pitfalls, Gotchas, Known Issues, Warnings, Caveats, Footguns, Troubleshooting."""
        aliases = [
            "Pitfalls",
            "Gotchas",
            "Footguns",
            "Known Issues",
            "Warnings",
            "Caveats",
            "Troubleshooting",
        ]
        for alias in aliases:
            content = f"## {alias}\n\nBody for {alias}.\n"
            sections = _extract_pitfall_sections(content)
            assert len(sections) == 1, f"{alias} should match"
            assert f"Body for {alias}" in sections[0][1]

    def test_subsections_included_in_body(self):
        """### sub-headings should be INSIDE the parent ## section, not separate."""
        content = """## Pitfalls

Top pitfall.

### Sub-pitfall A

A's body.

### Sub-pitfall B

B's body.

## Other Section

Different content.
"""
        sections = _extract_pitfall_sections(content)
        assert len(sections) == 1
        body = sections[0][1]
        assert "Top pitfall." in body
        assert "### Sub-pitfall A" in body
        assert "A's body." in body
        assert "### Sub-pitfall B" in body
        assert "B's body." in body
        assert "Different content." not in body  # boundary held at next ##

    def test_section_ends_at_same_level_heading(self):
        content = """## Pitfalls

Pit body.

## Not Pitfalls

Other body.
"""
        sections = _extract_pitfall_sections(content)
        assert len(sections) == 1
        body = sections[0][1]
        assert "Pit body." in body
        assert "Other body." not in body

    def test_section_ends_at_higher_level_heading(self):
        content = """## Pitfalls

Pit body.

# Top-level Other

Higher heading body.
"""
        sections = _extract_pitfall_sections(content)
        body = sections[0][1]
        assert "Pit body." in body
        assert "Higher heading body." not in body

    def test_multiple_distinct_sections(self):
        content = """## Pitfalls

First pit.

## Other

skip me.

## Known Issues

Second pit.
"""
        sections = _extract_pitfall_sections(content)
        assert len(sections) == 2
        assert sections[0][0] == "## Pitfalls"
        assert "First pit." in sections[0][1]
        assert sections[1][0] == "## Known Issues"
        assert "Second pit." in sections[1][1]

    def test_dedup_when_heading_matches_multiple_patterns(self):
        """`Common Pitfalls` matches both 'pitfalls' and 'common pitfalls' regexes —
        result should NOT be duplicated."""
        content = "## Common Pitfalls\n\nBody.\n"
        sections = _extract_pitfall_sections(content)
        assert len(sections) == 1

    def test_no_match_returns_empty(self):
        content = "## Architecture\n\nNothing pitfall-flavoured here.\n"
        sections = _extract_pitfall_sections(content)
        assert sections == []

    def test_partial_word_does_not_match(self):
        """`## Pitfallout` should NOT match `pitfall` (word boundary required)."""
        content = "## Pitfallout\n\nFake.\n"
        sections = _extract_pitfall_sections(content)
        assert sections == []


# ---------------------------------------------------------------------------
# skill_pitfalls() integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_skills(tmp_path):
    """Build a fake skills tree with three skills:
       - 'has-pitfalls'   : full pitfalls section + body
       - 'no-pitfalls'    : skill with no pitfalls heading
       - 'huge-pitfalls'  : section content > 12000 char cap
    """
    skills_dir = tmp_path / "skills"

    # 1) Skill with pitfalls
    sk1 = skills_dir / "has-pitfalls"
    sk1.mkdir(parents=True)
    (sk1 / "SKILL.md").write_text(
        """---
name: has-pitfalls
description: A skill that has pitfalls
---

# Has Pitfalls Skill

Intro paragraph.

## Common Pitfalls

1. Never bypass the launcher.
2. Re-check timeouts before long benches.

## Quick Reference

unrelated.
"""
    )

    # 2) Skill without pitfalls
    sk2 = skills_dir / "no-pitfalls"
    sk2.mkdir(parents=True)
    (sk2 / "SKILL.md").write_text(
        """---
name: no-pitfalls
description: A skill with no pitfalls heading
---

# Plain Skill

Just an intro, no warnings to give. This skill description
should be returned as a fallback preview when skill_pitfalls
is called on it.
"""
    )

    # 3) Skill with pathologically large pitfalls section
    sk3 = skills_dir / "huge-pitfalls"
    sk3.mkdir(parents=True)
    huge_body = "\n".join(f"{i}. pit number {i}" for i in range(2000))
    (sk3 / "SKILL.md").write_text(
        f"""---
name: huge-pitfalls
description: A skill with a giant pitfalls section
---

## Pitfalls

{huge_body}
"""
    )

    with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
        yield skills_dir


class TestSkillPitfallsIntegration:
    def test_returns_pitfalls_section(self, fake_skills):
        result = json.loads(skill_pitfalls("has-pitfalls"))
        assert result["success"] is True
        assert result["section_count"] == 1
        section = result["sections"][0]
        assert "Common Pitfalls" in section["heading"]
        assert "Never bypass the launcher" in section["body"]
        assert "Quick Reference" not in section["body"]  # boundary held

    def test_no_pitfalls_returns_fallback(self, fake_skills):
        result = json.loads(skill_pitfalls("no-pitfalls"))
        assert result["success"] is True
        assert result["sections"] == []
        assert result["no_pitfalls_section"] is True
        assert "Plain Skill" in result["fallback_preview"]
        assert "skill_view" in result["hint"]

    def test_truncation_caps_size(self, fake_skills):
        result = json.loads(skill_pitfalls("huge-pitfalls"))
        assert result["success"] is True
        assert result["truncated"] is True
        total = sum(
            len(s["heading"]) + len(s["body"]) for s in result["sections"]
        )
        # Cap is _PITFALLS_MAX_CHARS = 12000 — allow a small overhang for the
        # truncation marker and heading.
        assert total < 13000

    def test_unknown_skill_returns_error(self, fake_skills):
        result = json.loads(skill_pitfalls("nonexistent-skill"))
        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestSchemaAndRegistration:
    def test_schema_shape(self):
        from tools.skills_tool import SKILL_PITFALLS_SCHEMA

        assert SKILL_PITFALLS_SCHEMA["name"] == "skill_pitfalls"
        assert "description" in SKILL_PITFALLS_SCHEMA
        params = SKILL_PITFALLS_SCHEMA["parameters"]
        assert "name" in params["properties"]
        assert params["required"] == ["name"]

    def test_tool_registered(self):
        from tools import registry as r
        import tools.skills_tool  # noqa: F401 -- trigger registration

        entry = r.registry.get_entry("skill_pitfalls")
        assert entry is not None
        assert entry.toolset == "skills"

    def test_dispatch_via_registry(self, fake_skills):
        from tools import registry as r
        import tools.skills_tool  # noqa: F401

        result = r.registry.dispatch("skill_pitfalls", {"name": "has-pitfalls"})
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["section_count"] == 1
