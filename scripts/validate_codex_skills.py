"""Validate the repository-local Codex skill manifests and routing files.

This checks structure and local links only. It does not test Codex skill selection,
task quality, bot behavior, RPC behavior, or operational readiness.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / ".agents" / "skills"
CATALOG = ROOT / "SKILLS_CATALOG.md"
SCENARIOS = ROOT / "docs" / "SKILLS_VALIDATION_SCENARIOS.md"
RESULTS = ROOT / "docs" / "SKILLS_BEHAVIORAL_RESULTS.md"

EXPECTED_SKILLS = {
    "project-master",
    "solana-rpc-performance",
    "jupiter-transaction-security",
    "token-risk-antirug",
    "wallet-intelligence-collector",
    "copy-trading",
    "trading-risk-manager",
    "multi-wallet-execution",
    "pnl-accounting",
    "backtesting-validation",
    "telegram-monitoring",
    "python-testing-ci",
    "sqlite-recovery",
    "f-project-benchmark",
    "production-readiness-auditor",
}
REQUIRED_HEADINGS = (
    "Déclencheurs",
    "Audit avant toute modification",
    "Workflow",
    "Sécurité",
    "Tests",
    "Preuves attendues",
    "Limites",
    "Passages de relais",
)
EXPECTED_RESULT_IDS = {
    *(f"P{number:02}" for number in range(1, 16)),
    *(f"N{number:02}" for number in range(1, 7)),
    *(f"A{number:02}" for number in range(1, 7)),
}
RESULT_ROW = re.compile(r"^\| (P\d{2}|N\d{2}|A\d{2}) \| (OBSERVED|PARTIAL|NOT_RUN) \|", re.MULTILINE)


def parse_frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{path}: missing opening YAML frontmatter delimiter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: missing closing YAML frontmatter delimiter") from exc

    raw = "\n".join(lines[1:end])
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        yaml = None

    if yaml is not None:
        data = yaml.safe_load(raw)
    else:
        data: dict[str, str] = {}
        for line in lines[1:end]:
            if not line.strip():
                continue
            key, separator, value = line.partition(":")
            if not separator or key not in {"name", "description"} or key in data:
                raise ValueError(f"{path}: unsupported or duplicate frontmatter field")
            value = value.strip()
            if value.startswith('"'):
                try:
                    data[key] = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}: invalid quoted YAML scalar for {key}") from exc
            elif value and not re.search(r":\s|\s#", value):
                data[key] = value
            else:
                raise ValueError(f"{path}: use a simple scalar or JSON-style quoted value")
    if not isinstance(data, dict):
        raise ValueError(f"{path}: frontmatter must be a mapping")
    return data, "\n".join(lines[end + 1 :])


def check_skill(path: Path, errors: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8-sig")
        manifest, body = parse_frontmatter(text, path)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return

    folder_name = path.parent.name
    if set(manifest) != {"name", "description"}:
        errors.append(f"{path}: frontmatter must contain only name and description")
        return
    name = manifest.get("name")
    description = manifest.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        errors.append(f"{path}: name must be a lowercase hyphenated identifier")
    elif name != folder_name:
        errors.append(f"{path}: name does not match its skill folder")
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{path}: description is required")
    elif len(description) > 240:
        errors.append(f"{path}: description is too long for concise routing metadata")

    for heading in REQUIRED_HEADINGS:
        pattern = re.compile(
            rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(body)
        if not match or not match.group(1).strip():
            errors.append(f"{path}: missing or empty section '## {heading}'")

    if re.search(r"\b(?:TODO|FIXME|TBD)\b", body, re.IGNORECASE):
        errors.append(f"{path}: unfinished placeholder found")

    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", body):
        if target.startswith(("https://", "http://", "mailto:", "#")):
            continue
        local_target = target.split("#", 1)[0]
        if not local_target:
            continue
        resolved = (path.parent / local_target).resolve()
        if not resolved.exists():
            errors.append(f"{path}: broken local link {target}")


def check_behavioral_results(path: Path, errors: list[str]) -> None:
    """Check that every planned scenario has an explicit observed/unrun result."""
    if not path.is_file():
        errors.append(f"missing required file: {path.relative_to(ROOT)}")
        return

    content = path.read_text(encoding="utf-8-sig")
    matches = RESULT_ROW.findall(content)
    found = {scenario_id for scenario_id, _status in matches}
    duplicates = sorted(
        scenario_id for scenario_id in found
        if sum(1 for matched_id, _status in matches if matched_id == scenario_id) > 1
    )
    if duplicates:
        errors.append(f"behavioral result matrix has duplicate IDs: {duplicates}")
    if found != EXPECTED_RESULT_IDS:
        errors.append(
            "behavioral result IDs differ from planned scenarios; "
            f"missing={sorted(EXPECTED_RESULT_IDS - found)}, "
            f"unexpected={sorted(found - EXPECTED_RESULT_IDS)}"
        )
    if "does not test Codex skill selection" in content:
        errors.append("behavioral result report must distinguish observations from structure tests")


def main() -> int:
    errors: list[str] = []
    manifests = sorted(SKILLS_ROOT.glob("*/SKILL.md")) if SKILLS_ROOT.exists() else []
    found = {path.parent.name for path in manifests}

    if found != EXPECTED_SKILLS:
        errors.append(
            "skill folders differ from expected set; "
            f"missing={sorted(EXPECTED_SKILLS - found)}, "
            f"unexpected={sorted(found - EXPECTED_SKILLS)}"
        )
    for path in manifests:
        check_skill(path, errors)

    for required_file in (CATALOG, SCENARIOS, RESULTS):
        if not required_file.is_file():
            errors.append(f"missing required file: {required_file.relative_to(ROOT)}")
    if CATALOG.is_file():
        catalog = CATALOG.read_text(encoding="utf-8-sig")
        for name in EXPECTED_SKILLS:
            if f".agents/skills/{name}/SKILL.md" not in catalog:
                errors.append(f"catalog is missing the path for {name}")
        if "web-research-problem-solver" not in catalog:
            errors.append("catalog must record web-research-problem-solver status and scope")
    if SCENARIOS.is_file():
        scenarios = SCENARIOS.read_text(encoding="utf-8-sig")
        for name in EXPECTED_SKILLS:
            if name not in scenarios:
                errors.append(f"scenario matrix does not mention {name}")
        if "web-research-problem-solver" not in scenarios:
            errors.append("scenario matrix must cover conditional research routing")
        for scenario_id in ("A01", "A02", "A03", "A04", "A05", "A06"):
            if f"| {scenario_id} |" not in scenarios:
                errors.append(f"scenario matrix is missing ambiguous case {scenario_id}")
    if RESULTS.is_file():
        check_behavioral_results(RESULTS, errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print(
        f"Validated {len(manifests)} skill manifests, required sections, local links, "
        "catalog entries, routing scenarios, and behavioral result rows."
    )
    print(
        "Structural validation only: Codex invocation behavior, bot tests, and "
        "operational readiness were not tested."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
