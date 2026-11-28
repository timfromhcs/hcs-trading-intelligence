"""Portable, versioned agent skills with strict capability boundaries."""

import re
from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent

MANDATORY_SECTIONS = (
    "purpose",
    "capability tier",
    "trusted and untrusted data",
    "input and output schema",
    "ordered workflow",
    "safe defaults and stop conditions",
    "failure and recovery",
    "evidence and citation",
    "explicit forbidden actions",
    "acceptance checks",
)

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    version: str
    purpose: str
    capability_tier: str
    required_tools: list[str]
    content: str
    path: Path
    description: str = ""


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_header = match.group(1)
    body = text[match.end() :]
    top: dict[str, str] = {}
    nested: dict[str, str] = {}
    for raw_line in raw_header.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        target = nested if raw_line[:1] in {" ", "	"} else top
        target[key.strip().lower()] = val.strip().strip("\"'")
    # One level of nesting (the Agent Skills `metadata:` map) is flattened, but
    # a nested key can never shadow a top-level key such as `name`.
    fields = {**nested, **top}
    fields.pop("metadata", None)
    return fields, body


def validate_skill_content(content: str) -> list[str]:
    """Verify that all mandatory specification sections are present."""
    lower_content = content.lower()
    missing: list[str] = []
    for section in MANDATORY_SECTIONS:
        if section not in lower_content:
            missing.append(section)
    return missing


def load_skill_file(skill_path: Path) -> SkillMetadata:
    content = skill_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)
    missing = validate_skill_content(content)
    if missing:
        missing_str = ", ".join(missing)
        raise ValueError(
            f"Skill at {skill_path.name} is missing mandatory sections: {missing_str}"
        )
    name = frontmatter.get("name", skill_path.parent.name)
    version = frontmatter.get("version", "1.0.0")
    description = frontmatter.get("description", "")
    purpose = frontmatter.get("purpose", description)
    tier = frontmatter.get("capability_tier", "READ")
    raw_tools = frontmatter.get("required_tools", "")
    tools = [t.strip() for t in raw_tools.split(",") if t.strip()] if raw_tools else []
    return SkillMetadata(
        name=name,
        version=version,
        purpose=purpose,
        capability_tier=tier,
        required_tools=tools,
        content=content,
        path=skill_path,
        description=description,
    )


def list_skills() -> list[SkillMetadata]:
    skills: list[SkillMetadata] = []
    if not SKILLS_DIR.is_dir():
        return skills
    for sub in sorted(SKILLS_DIR.iterdir()):
        skill_file = sub / "SKILL.md" if sub.is_dir() else None
        if skill_file and skill_file.is_file():
            skills.append(load_skill_file(skill_file))
    return skills


def get_skill(name: str) -> SkillMetadata | None:
    for skill in list_skills():
        if skill.name == name:
            return skill
    return None
