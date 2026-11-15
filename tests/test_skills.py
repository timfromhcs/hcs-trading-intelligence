from pathlib import Path

import pytest

from hcs_trader.skills import (
    MANDATORY_SECTIONS,
    SkillMetadata,
    get_skill,
    list_skills,
    load_skill_file,
    parse_frontmatter,
    validate_skill_content,
)


def test_list_skills_discovers_all_required_skills() -> None:
    skills = list_skills()
    assert len(skills) == 5
    names = {s.name for s in skills}
    expected = {
        "market-research",
        "strategy-lab",
        "portfolio-operator",
        "risk-auditor",
        "release-engineer",
    }
    assert names == expected


def test_each_skill_has_valid_metadata_and_sections() -> None:
    skills = list_skills()
    for skill in skills:
        assert isinstance(skill, SkillMetadata)
        assert skill.name
        assert skill.version == "1.0.0"
        assert skill.purpose
        assert skill.capability_tier in {"READ", "RESEARCH", "ADMIN_LOCAL"}
        assert skill.path.is_file()
        assert len(skill.content) > 100

        # Verify all 10 mandatory sections are present
        missing = validate_skill_content(skill.content)
        assert not missing, f"Skill {skill.name} is missing sections: {missing}"


def test_get_skill_by_name() -> None:
    skill = get_skill("market-research")
    assert skill is not None
    assert skill.name == "market-research"
    assert "market.get_quotes" in skill.required_tools

    unknown = get_skill("non-existent-skill")
    assert unknown is None


def test_parse_frontmatter_without_header() -> None:
    text = "# Just markdown\nNo frontmatter here."
    fields, body = parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_validate_skill_content_detects_missing_sections() -> None:
    partial = "# Purpose & Triggers\nOnly one section."
    missing = validate_skill_content(partial)
    assert len(missing) == len(MANDATORY_SECTIONS) - 1
    assert "capability tier" in missing


def test_load_skill_file_fails_on_missing_section(tmp_path: Path) -> None:
    bad_skill = tmp_path / "SKILL.md"
    bad_skill.write_text(
        "---\nname: bad\n---\n# Purpose\nIncomplete skill", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="missing mandatory sections"):
        load_skill_file(bad_skill)
