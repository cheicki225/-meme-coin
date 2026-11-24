"""Plan bounded development work and run only approved offline validators.

This module does not dispatch agents or run mission-defined shell commands. It
records task leases and evidence so a human or an explicitly delegated Codex
task can work within a declared file scope.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_PLAN = ROOT / "orchestration" / "missions.json"
DEFAULT_CHECKPOINT = ROOT / ".orchestrator" / "checkpoint.json"
GITHUB_REPOSITORY = "cheicki225/-meme-coin"
TRUSTED_CI_WORKFLOW_BLOB_SHA = "38b9b46414def65f8a9b6529a42446bdb7d43737"
MAX_AGENT_SLOTS = 3  # Current Codex limit: four active agents including the coordinator.
MAX_ATTEMPTS = 3
MAX_BUDGET_MINUTES = 24 * 60
MAX_VALIDATION_SECONDS = 600
CHECKPOINT_SCHEMA_VERSION = 1

TASK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SKILL_CATALOG_ROW_RE = re.compile(
    r"^\|\s*([a-z][a-z0-9-]{1,63})\s*\|\s*\.agents/skills/([a-z][a-z0-9-]{1,63})/SKILL\.md\s*\|",
    re.MULTILINE,
)
SHA_RE = re.compile(r"^[a-fA-F0-9]{40}$")
GATE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ALLOWED_TASK_STATUSES = {"pending", "in_progress", "external_in_progress", "blocked", "completed"}
ALLOWED_INITIAL_STATUSES = {"pending", "external_in_progress", "blocked"}
ALLOWED_EVIDENCE = {"offline_tests", "ci_success"}
ALLOWED_VALIDATION_SECONDS = range(1, MAX_VALIDATION_SECONDS + 1)
SENSITIVE_FILE_NAME_RE = re.compile(
    r"(?:^|[._-])(?:secrets?|credentials?|private_key|keypair|api_key|access_token|"
    r"refresh_token|mnemonic|seed_phrase|id_rsa|id_ed25519)(?:[._-]|$)",
    re.IGNORECASE,
)
REQUIRED_CI_STEPS = {
    "Compile Python sources",
    "Verify PAPER mode is the default",
    "Run unit tests",
    "Lint critical errors",
    "Audit dependencies",
    "Scan repository secrets",
}


class OrchestratorError(ValueError):
    """Raised when a plan, checkpoint, gate, or validation is unsafe or invalid."""


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    priority: str
    depends_on: tuple[str, ...]
    scope_files: tuple[str, ...]
    skills: tuple[str, ...]
    budget_minutes: int
    max_attempts: int
    required_evidence: tuple[str, ...]
    manual_gates: tuple[str, ...]
    initial_status: str


@dataclass(frozen=True)
class Plan:
    max_parallel_agents: int
    tasks: tuple[Task, ...]

    @property
    def by_id(self) -> dict[str, Task]:
        return {task.task_id: task for task in self.tasks}


@dataclass(frozen=True)
class Readiness:
    ready: tuple[Task, ...]
    blockers: dict[str, tuple[str, ...]]
    max_parallel_agents: int
    next_task_proposal: Task | None


@dataclass(frozen=True)
class ValidationResult:
    profile: str
    return_code: int
    elapsed_seconds: float
    passed: bool


def utc_now() -> str:
    """Return an ISO 8601 UTC timestamp suitable for an audit event."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise OrchestratorError(f"{where}: invalid fields (missing={missing}, extra={extra})")


def _safe_scope_file(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise OrchestratorError("scope_files must contain repository-relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise OrchestratorError("scope_files cannot escape the repository")
    if any(char in value for char in "*?[]") or value.startswith(".orchestrator/"):
        raise OrchestratorError("scope_files must be exact code paths, not globs or checkpoint paths")
    name = path.name.lower()
    if name in {
        ".env", ".envrc", ".netrc", ".pypirc", ".npmrc", "wallet.json", "wallets.json",
        "wallet.dat", "id.json", "key.json", "keys.json",
    }:
        raise OrchestratorError("secret-bearing environment files cannot be assigned as task scope")
    if (name.startswith(".env.") and name not in {".env.example"}) or SENSITIVE_FILE_NAME_RE.search(name):
        raise OrchestratorError("secret, credential, or private-key filenames cannot be assigned as task scope")
    if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore"}:
        raise OrchestratorError("key and keystore files cannot be assigned as task scope")
    if any(part.lower() in {"secrets", "credentials", "private_keys", "keystores"} for part in path.parts):
        raise OrchestratorError("secret and credential directories cannot be assigned as task scope")
    return path.as_posix()


def _available_skill_ids(repo_root: Path | None = None) -> set[str]:
    """Return catalogued project Skills with a regular, present SKILL.md entry point."""
    repository_root = (repo_root or ROOT).resolve(strict=True)
    root = _confined_path(repository_root / ".agents" / "skills", repository_root)
    catalog_path = _confined_path(repository_root / "SKILLS_CATALOG.md", repository_root)
    if not root.is_dir() or not catalog_path.is_file():
        raise OrchestratorError("project Skills directory is missing or unsafe")
    try:
        catalog = catalog_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OrchestratorError("project Skills catalog could not be read") from exc
    catalog_rows = SKILL_CATALOG_ROW_RE.findall(catalog)
    catalog_ids = {skill_id for skill_id, path_id in catalog_rows if skill_id == path_id}
    if not catalog_ids:
        raise OrchestratorError("project Skills catalog has no valid Skill rows")
    available: set[str] = set()
    try:
        for directory in root.iterdir():
            skill_file = directory / "SKILL.md"
            if (
                SKILL_ID_RE.fullmatch(directory.name)
                and directory.name in catalog_ids
                and directory.is_dir()
                and not directory.is_symlink()
                and skill_file.is_file()
                and not skill_file.is_symlink()
            ):
                available.add(directory.name)
    except OSError as exc:
        raise OrchestratorError("project Skills could not be inventoried") from exc
    return available


def _confined_path(path: Path, repo_root: Path | None = None) -> Path:
    """Resolve an existing or prospective path inside the repository, rejecting symlinks."""
    try:
        root = (repo_root or ROOT).resolve(strict=True)
        candidate = path if path.is_absolute() else root / path
        lexical = Path(os.path.abspath(candidate))
        if not lexical.is_relative_to(root):
            raise OrchestratorError("plan and checkpoint paths must stay inside the repository")
        current = root
        for part in lexical.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise OrchestratorError("plan and checkpoint paths cannot traverse symbolic links")
        resolved = lexical.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise OrchestratorError("plan and checkpoint paths must stay inside the repository")
        return resolved
    except (OSError, RuntimeError) as exc:
        raise OrchestratorError("plan or checkpoint path could not be safely resolved") from exc


def load_plan(path: Path = DEFAULT_PLAN) -> Plan:
    """Load and validate a bounded, acyclic mission plan from JSON."""
    path = _confined_path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestratorError("mission plan could not be read as valid JSON") from exc
    if not isinstance(data, dict):
        raise OrchestratorError("mission plan root must be an object")
    _require_exact_keys(data, {"schema_version", "max_parallel_agents", "safety_policy", "tasks"}, "plan")
    if isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise OrchestratorError("unsupported mission plan schema version")
    max_agents = data["max_parallel_agents"]
    if isinstance(max_agents, bool) or not isinstance(max_agents, int) or not 1 <= max_agents <= MAX_AGENT_SLOTS:
        raise OrchestratorError(f"max_parallel_agents must be between 1 and {MAX_AGENT_SLOTS}")

    expected_safety = {
        "mode": "PAPER",
        "allow_live": False,
        "allow_secrets": False,
        "allow_signing": False,
        "allow_transactions": False,
        "allow_rpc": False,
        "allow_arbitrary_shell": False,
    }
    if data["safety_policy"] != expected_safety:
        raise OrchestratorError("safety_policy must keep PAPER-only operation and all sensitive actions disabled")
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise OrchestratorError("tasks must be a nonempty list")

    expected_task_keys = {
        "id", "title", "priority", "depends_on", "scope_files", "skills", "budget_minutes",
        "max_attempts", "required_evidence", "manual_gates", "initial_status",
    }
    available_skills = _available_skill_ids()
    tasks: list[Task] = []
    seen: set[str] = set()
    for index, row in enumerate(data["tasks"]):
        where = f"tasks[{index}]"
        if not isinstance(row, dict):
            raise OrchestratorError(f"{where} must be an object")
        _require_exact_keys(row, expected_task_keys, where)
        task_id = row["id"]
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id) or task_id in seen:
            raise OrchestratorError(f"{where}.id must be a unique lowercase task identifier")
        seen.add(task_id)
        if not isinstance(row["title"], str) or not row["title"].strip() or len(row["title"]) > 160:
            raise OrchestratorError(f"{where}.title must be a short nonempty string")
        if not isinstance(row["priority"], str) or row["priority"] not in {"P0", "P1", "P2"}:
            raise OrchestratorError(f"{where}.priority must be P0, P1, or P2")
        if not isinstance(row["depends_on"], list) or not all(isinstance(item, str) for item in row["depends_on"]):
            raise OrchestratorError(f"{where}.depends_on must be a list of task identifiers")
        if len(set(row["depends_on"])) != len(row["depends_on"]):
            raise OrchestratorError(f"{where}.depends_on contains duplicates")
        if not isinstance(row["scope_files"], list) or not row["scope_files"]:
            raise OrchestratorError(f"{where}.scope_files must list at least one exact path")
        scope_files = tuple(_safe_scope_file(item) for item in row["scope_files"])
        if len(set(scope_files)) != len(scope_files):
            raise OrchestratorError(f"{where}.scope_files contains duplicates")
        if not isinstance(row["skills"], list) or not row["skills"] or not all(
            isinstance(item, str) and SKILL_ID_RE.fullmatch(item) for item in row["skills"]
        ):
            raise OrchestratorError(f"{where}.skills must list one or more project Skill identifiers")
        if len(set(row["skills"])) != len(row["skills"]):
            raise OrchestratorError(f"{where}.skills contains duplicates")
        unknown_skills = sorted(set(row["skills"]) - available_skills)
        if unknown_skills:
            raise OrchestratorError(f"{where}.skills are not present in .agents/skills: {unknown_skills}")
        budget = row["budget_minutes"]
        attempts = row["max_attempts"]
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= MAX_BUDGET_MINUTES:
            raise OrchestratorError(f"{where}.budget_minutes must be between 1 and {MAX_BUDGET_MINUTES}")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= MAX_ATTEMPTS:
            raise OrchestratorError(f"{where}.max_attempts must be between 1 and {MAX_ATTEMPTS}")
        if not isinstance(row["required_evidence"], list) or not row["required_evidence"]:
            raise OrchestratorError(f"{where}.required_evidence must be a nonempty list")
        if not all(isinstance(item, str) and item in ALLOWED_EVIDENCE for item in row["required_evidence"]):
            raise OrchestratorError(f"{where}.required_evidence contains an unsupported evidence type")
        if not isinstance(row["manual_gates"], list) or not all(
            isinstance(item, str) and GATE_ID_RE.fullmatch(item) for item in row["manual_gates"]
        ):
            raise OrchestratorError(f"{where}.manual_gates must contain lowercase gate identifiers")
        if len(set(row["manual_gates"])) != len(row["manual_gates"]):
            raise OrchestratorError(f"{where}.manual_gates contains duplicates")
        initial_status = row["initial_status"]
        if not isinstance(initial_status, str) or initial_status not in ALLOWED_INITIAL_STATUSES:
            raise OrchestratorError(f"{where}.initial_status is not an allowed initial status")
        tasks.append(Task(
            task_id=task_id,
            title=row["title"].strip(),
            priority=row["priority"],
            depends_on=tuple(row["depends_on"]),
            scope_files=scope_files,
            skills=tuple(row["skills"]),
            budget_minutes=budget,
            max_attempts=attempts,
            required_evidence=tuple(row["required_evidence"]),
            manual_gates=tuple(row["manual_gates"]),
            initial_status=initial_status,
        ))

    by_id = {task.task_id: task for task in tasks}
    for task in tasks:
        unknown = sorted(set(task.depends_on) - set(by_id))
        if unknown:
            raise OrchestratorError(f"{task.task_id}: unknown dependencies {unknown}")
        if task.task_id in task.depends_on:
            raise OrchestratorError(f"{task.task_id}: task cannot depend on itself")
    _reject_cycles(by_id)
    return Plan(max_parallel_agents=max_agents, tasks=tuple(tasks))


def _reject_cycles(by_id: dict[str, Task]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise OrchestratorError("mission dependencies contain a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)


def empty_checkpoint() -> dict[str, Any]:
    """Return a new empty checkpoint without creating files."""
    return {"schema_version": CHECKPOINT_SCHEMA_VERSION, "tasks": {}, "audit": []}


def load_checkpoint(path: Path = DEFAULT_CHECKPOINT) -> dict[str, Any]:
    """Read a checkpoint, failing closed rather than replacing corrupt state."""
    path = _confined_path(path)
    if not path.exists():
        return empty_checkpoint()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestratorError("checkpoint could not be read; inspect it before recovery") from exc
    if not isinstance(state, dict) or set(state) != {"schema_version", "tasks", "audit"}:
        raise OrchestratorError("checkpoint has an unsupported structure; inspect it before recovery")
    if isinstance(state["schema_version"], bool) or state["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        raise OrchestratorError("checkpoint schema version is unsupported")
    if not isinstance(state["tasks"], dict) or not isinstance(state["audit"], list):
        raise OrchestratorError("checkpoint tasks or audit data are malformed")
    for task_id, entry in state["tasks"].items():
        if not isinstance(task_id, str) or not isinstance(entry, dict):
            raise OrchestratorError("checkpoint task entry is malformed")
        status = entry.get("status")
        attempts = entry.get("attempts")
        if not isinstance(status, str) or status not in ALLOWED_TASK_STATUSES or isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise OrchestratorError("checkpoint task status or attempt count is malformed")
    return state


def _priority_rank(priority: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2}[priority]


def _entry(state: dict[str, Any], task: Task) -> dict[str, Any]:
    current = state["tasks"].get(task.task_id, {})
    return {"status": current.get("status", task.initial_status), **current}


def _scope_key(value: str) -> str:
    return value.casefold()


def readiness(plan: Plan, state: dict[str, Any], slots: int | None = None) -> Readiness:
    """Select ready tasks by dependency, status, budget, gate, and file locks."""
    requested_slots = plan.max_parallel_agents if slots is None else slots
    if isinstance(requested_slots, bool) or not isinstance(requested_slots, int) or requested_slots < 1:
        raise OrchestratorError("available agent slots must be a positive integer")
    max_agents = min(requested_slots, plan.max_parallel_agents, MAX_AGENT_SLOTS)
    by_id = plan.by_id
    statuses = {task_id: _entry(state, task)["status"] for task_id, task in by_id.items()}
    completed = {task_id for task_id, status in statuses.items() if status == "completed"}
    active = [task for task in plan.tasks if statuses[task.task_id] in {"in_progress", "external_in_progress"}]
    active_scopes: dict[str, str] = {}
    blockers: dict[str, tuple[str, ...]] = {}
    active_conflicts: dict[str, set[str]] = {task.task_id: set() for task in active}
    for index, task in enumerate(active):
        for other in active[index + 1 :]:
            overlap = set(map(_scope_key, task.scope_files)) & set(map(_scope_key, other.scope_files))
            if overlap:
                active_conflicts[task.task_id].add(other.task_id)
                active_conflicts[other.task_id].add(task.task_id)
        for file_path in task.scope_files:
            active_scopes[_scope_key(file_path)] = task.task_id
    for task_id, conflicts in active_conflicts.items():
        if conflicts:
            blockers[task_id] = ("active_scope_conflict:" + ",".join(sorted(conflicts)),)
    for task in active:
        if _expired_active_task(task, _entry(state, task)):
            previous = blockers.get(task.task_id, ())
            blockers[task.task_id] = (*previous, "budget_expired_manual_recovery_required")

    candidates: list[Task] = []
    selected_scopes: set[str] = set()
    for task in sorted(plan.tasks, key=lambda item: (_priority_rank(item.priority), item.task_id)):
        status = statuses[task.task_id]
        reasons: list[str] = []
        if status != "pending":
            if task.task_id not in blockers:
                blockers[task.task_id] = ("status:" + status,)
            continue
        missing_dependencies = sorted(set(task.depends_on) - completed)
        if missing_dependencies:
            reasons.append("dependencies:" + ",".join(missing_dependencies))
        existing = _entry(state, task)
        attempts = existing.get("attempts", 0)
        if attempts >= task.max_attempts:
            reasons.append("attempt_budget_exhausted")
        for gate in task.manual_gates:
            reasons.append("manual_gate_requires_reviewed_plan:" + gate)
        if len(active) + len(candidates) >= max_agents:
            reasons.append("agent_capacity")
        for scope in task.scope_files:
            key = _scope_key(scope)
            owner = active_scopes.get(key)
            if owner:
                reasons.append("file_locked:" + scope + ":" + owner)
            elif key in selected_scopes:
                reasons.append("file_conflict_in_batch:" + scope)
        if reasons:
            blockers[task.task_id] = tuple(reasons)
            continue
        candidates.append(task)
        selected_scopes.update(map(_scope_key, task.scope_files))
    pending = [
        task for task in plan.tasks
        if _entry(state, task).get("status") == "pending"
    ]
    next_task_proposal = min(pending, key=lambda task: _priority_rank(task.priority)) if pending else None
    return Readiness(tuple(candidates), blockers, max_agents, next_task_proposal)


def _expired_active_task(task: Task, entry: dict[str, Any]) -> bool:
    """Detect an expired lease without releasing its file locks automatically."""
    started_at = entry.get("started_at")
    if not isinstance(started_at, str):
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return True
    if started.tzinfo is None:
        return True
    elapsed = datetime.now(timezone.utc) - started.astimezone(timezone.utc)
    return elapsed.total_seconds() > task.budget_minutes * 60


@contextmanager
def _checkpoint_lock(path: Path, timeout_seconds: float = 1.5) -> Iterator[None]:
    """Acquire a bounded exclusive lock; a stale lock fails closed for inspection."""
    path = _confined_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            except OSError as exc:
                os.close(descriptor)
                descriptor = None
                lock_path.unlink(missing_ok=True)
                raise OrchestratorError("checkpoint lock could not be initialized") from exc
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise OrchestratorError("checkpoint lock is held; inspect the owner before recovery") from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _save_checkpoint(path: Path, state: dict[str, Any]) -> None:
    path = _confined_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_name = temporary.name
            json.dump(state, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise OrchestratorError("checkpoint could not be written atomically") from exc


def _add_audit(state: dict[str, Any], event: str, task_id: str, **fields: Any) -> None:
    item = {"at": utc_now(), "event": event, "task_id": task_id, **fields}
    state["audit"].append(item)
    state["audit"] = state["audit"][-1000:]


def start_task(plan: Plan, checkpoint_path: Path, task_id: str) -> dict[str, Any]:
    """Reserve a ready task's declared file scope without starting a process or agent."""
    if task_id not in plan.by_id:
        raise OrchestratorError("unknown task id")
    with _checkpoint_lock(checkpoint_path):
        state = load_checkpoint(checkpoint_path)
        result = readiness(plan, state)
        task = plan.by_id[task_id]
        if task not in result.ready:
            reasons = ",".join(result.blockers.get(task_id, ("not_ready",)))
            raise OrchestratorError("task is not ready: " + reasons)
        current = _entry(state, task)
        run_id = uuid.uuid4().hex
        attempts = current.get("attempts", 0) + 1
        state["tasks"][task_id] = {
            "status": "in_progress",
            "attempts": attempts,
            "run_id": run_id,
            "started_at": utc_now(),
        }
        _add_audit(state, "task_started", task_id, run_id=run_id, attempt=attempts)
        _save_checkpoint(checkpoint_path, state)
        return {"task_id": task_id, "run_id": run_id, "scope_files": list(task.scope_files), "budget_minutes": task.budget_minutes}


def _github_get_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "solana-bot-development-orchestrator",
    })
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrchestratorError("GitHub CI evidence could not be verified; task remains incomplete") from exc
    if not isinstance(payload, dict):
        raise OrchestratorError("GitHub CI response is malformed")
    return payload


def verify_ci_run(run_id: int, expected_sha: str) -> dict[str, Any]:
    """Verify trusted-workflow CI for a source SHA and record the SHA actually tested."""
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or not isinstance(expected_sha, str)
        or not SHA_RE.fullmatch(expected_sha)
    ):
        raise OrchestratorError("CI run id or commit SHA is malformed")
    base_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/runs/{run_id}"
    payload = _github_get_json(base_url)
    repository = payload.get("repository")
    repo_name = repository.get("full_name") if isinstance(repository, dict) else None
    if not isinstance(repo_name, str) or repo_name.casefold() != GITHUB_REPOSITORY.casefold():
        raise OrchestratorError("CI run belongs to a different repository")
    if payload.get("name") != "CI" or payload.get("path") != ".github/workflows/ci.yml":
        raise OrchestratorError("CI run is not from the repository validation workflow")
    event = payload.get("event")
    if not isinstance(event, str) or event not in {"pull_request", "push"}:
        raise OrchestratorError("CI run event is not an allowed branch validation event")
    tested_sha = payload.get("head_sha")
    if not isinstance(tested_sha, str) or not SHA_RE.fullmatch(tested_sha):
        raise OrchestratorError("CI run tested commit SHA is malformed")
    source_shas: set[str] = set()
    pull_requests = payload.get("pull_requests", [])
    if isinstance(pull_requests, list):
        for pull_request in pull_requests:
            if isinstance(pull_request, dict):
                head = pull_request.get("head")
                if isinstance(head, dict) and isinstance(head.get("sha"), str):
                    source_shas.add(head["sha"].lower())
    if event == "push":
        if tested_sha.lower() != expected_sha.lower():
            raise OrchestratorError("push CI run does not test the requested commit SHA")
        source_sha = tested_sha.lower()
    else:
        if expected_sha.lower() not in source_shas:
            raise OrchestratorError("pull request CI run is not associated with the requested source SHA")
        source_sha = expected_sha.lower()
    if payload.get("status") != "completed" or payload.get("conclusion") != "success":
        raise OrchestratorError("CI run did not complete successfully")
    workflow_payload = _github_get_json(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/.github/workflows/ci.yml?ref={tested_sha}"
    )
    if (
        workflow_payload.get("path") != ".github/workflows/ci.yml"
        or workflow_payload.get("sha") != TRUSTED_CI_WORKFLOW_BLOB_SHA
    ):
        raise OrchestratorError("CI workflow definition differs from the trusted reviewed revision")
    jobs_payload = _github_get_json(base_url + "/jobs?per_page=100")
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise OrchestratorError("GitHub CI job evidence is malformed")
    validate_jobs = [job for job in jobs if isinstance(job, dict) and job.get("name") == "validate"]
    successful_jobs = [job for job in validate_jobs if job.get("conclusion") == "success"]
    if not successful_jobs:
        raise OrchestratorError("required CI validation job did not succeed")
    observed_steps: dict[str, str] = {}
    for job in successful_jobs:
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("name"), str):
                observed_steps[step["name"]] = str(step.get("conclusion"))
    missing_steps = sorted(step for step in REQUIRED_CI_STEPS if observed_steps.get(step) != "success")
    if missing_steps:
        raise OrchestratorError("required CI steps are missing or not successful: " + ",".join(missing_steps))
    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not re.fullmatch(
        r"https://github\.com/cheicki225/-meme-coin/actions/runs/\d+", html_url
    ):
        raise OrchestratorError("CI run URL is malformed")
    return {
        "run_id": run_id,
        "commit_sha": source_sha,
        "tested_commit_sha": tested_sha.lower(),
        "conclusion": "success",
        "url": html_url,
        "workflow_blob_sha": workflow_payload["sha"],
        "verified_steps": sorted(REQUIRED_CI_STEPS),
    }


def complete_task(plan: Plan, checkpoint_path: Path, task_id: str, commit_sha: str, ci_run_id: int) -> None:
    """Complete a task only after exact-SHA CI succeeds and its plan has no manual gates."""
    task = plan.by_id.get(task_id)
    if task is None:
        raise OrchestratorError("unknown task id")
    if task.manual_gates:
        raise OrchestratorError("manual gates require a reviewed plan change; this CLI cannot approve them")
    ci = verify_ci_run(ci_run_id, commit_sha)
    with _checkpoint_lock(checkpoint_path):
        state = load_checkpoint(checkpoint_path)
        current = _entry(state, task)
        if current["status"] not in {"in_progress", "external_in_progress"}:
            raise OrchestratorError("task must be in progress before it can be completed")
        attempts = current.get("attempts", 0)
        state["tasks"][task_id] = {
            "status": "completed",
            "attempts": attempts,
            "commit_sha": ci["commit_sha"],
            "tested_commit_sha": ci["tested_commit_sha"],
            "ci_run_id": ci["run_id"],
            "ci_run_url": ci["url"],
            "ci_workflow_blob_sha": ci["workflow_blob_sha"],
            "ci_conclusion": ci["conclusion"],
            "completed_at": utc_now(),
        }
        _add_audit(
            state,
            "task_completed",
            task_id,
            source_commit_sha=ci["commit_sha"],
            tested_commit_sha=ci["tested_commit_sha"],
            ci_run_id=ci["run_id"],
        )
        _save_checkpoint(checkpoint_path, state)


def fail_task(plan: Plan, checkpoint_path: Path, task_id: str, reason_code: str) -> None:
    """Mark a run blocked using a fixed reason code; arbitrary text is not logged."""
    allowed_reasons = {"validation_failed", "budget_exhausted", "manual_review_required", "external_blocker"}
    if task_id not in plan.by_id or reason_code not in allowed_reasons:
        raise OrchestratorError("unknown task or failure reason code")
    with _checkpoint_lock(checkpoint_path):
        state = load_checkpoint(checkpoint_path)
        current = _entry(state, plan.by_id[task_id])
        if current["status"] not in {"in_progress", "external_in_progress"}:
            raise OrchestratorError("only an in-progress task can be marked blocked")
        state["tasks"][task_id] = {
            "status": "blocked",
            "attempts": current.get("attempts", 0),
            "reason_code": reason_code,
            "blocked_at": utc_now(),
        }
        _add_audit(state, "task_blocked", task_id, reason_code=reason_code)
        _save_checkpoint(checkpoint_path, state)


VALIDATION_PROFILES: dict[str, tuple[str, ...]] = {
    "compile": ("-m", "compileall", "-q", "."),
    "orchestrator_tests": ("-m", "pytest", "-q", "tests/test_development_orchestrator.py"),
    "unit": ("-m", "pytest", "-q"),
    "ruff": ("-m", "ruff", "check", "--select", "E9,F63,F7,F82", "."),
    "skills_structure": ("scripts/validate_codex_skills.py",),
}


def _safe_environment() -> dict[str, str]:
    """Pass only platform runtime paths and explicit safe trading/test defaults."""
    env_keys = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT")
    environment = {key: os.environ[key] for key in env_keys if key in os.environ}
    environment.update({
        "PYTHON_DOTENV_DISABLED": "true",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TRADING_MODE": "PAPER",
        "LIVE_EXECUTION_ENABLED": "false",
    })
    return environment


def run_validation(profile: str, repo_root: Path = ROOT, timeout_seconds: int = 300) -> ValidationResult:
    """Run a fixed offline validation profile without a shell or inherited secrets."""
    if profile not in VALIDATION_PROFILES:
        raise OrchestratorError("validation profile is not allowlisted")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds not in ALLOWED_VALIDATION_SECONDS:
        raise OrchestratorError(f"timeout_seconds must be between 1 and {MAX_VALIDATION_SECONDS}")
    repo_root = _confined_path(repo_root)
    if repo_root != ROOT.resolve(strict=True) or not repo_root.is_dir():
        raise OrchestratorError("validation can run only from this project repository root")
    command = [sys.executable, *VALIDATION_PROFILES[profile]]
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            env=_safe_environment(),
            shell=False,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
        return_code = result.returncode
    except subprocess.TimeoutExpired:
        return_code = 124
    except OSError:
        return_code = 127
    return ValidationResult(profile, return_code, round(time.monotonic() - started, 3), return_code == 0)


def format_readiness(result: Readiness) -> dict[str, Any]:
    """Create a serializable summary without commands, credentials, or agent claims."""
    proposal = result.next_task_proposal
    return {
        "max_parallel_agents": result.max_parallel_agents,
        "ready": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "priority": task.priority,
                "scope_files": list(task.scope_files),
                "skills": list(task.skills),
                "budget_minutes": task.budget_minutes,
            }
            for task in result.ready
        ],
        "blockers": {task_id: list(reasons) for task_id, reasons in sorted(result.blockers.items())},
        "next_task_proposal": None if proposal is None else {
            "task_id": proposal.task_id,
            "title": proposal.title,
            "priority": proposal.priority,
            "skills": list(proposal.skills),
            "runnable": proposal in result.ready,
            "blocked_by": list(result.blockers.get(proposal.task_id, ())),
        },
        "dispatch": "not_configured",
    }


def _checkpoint_for_cli(path: Path) -> dict[str, Any]:
    return load_checkpoint(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded, PAPER-only Solana development planner")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="show ready tasks and blockers")
    start_parser = commands.add_parser("start", help="reserve one ready task's file scope")
    start_parser.add_argument("task_id")
    complete_parser = commands.add_parser("complete", help="verify exact-commit public CI evidence")
    complete_parser.add_argument("task_id")
    complete_parser.add_argument("--commit-sha", required=True)
    complete_parser.add_argument("--ci-run-id", required=True, type=int)
    fail_parser = commands.add_parser("fail", help="mark an in-progress task blocked")
    fail_parser.add_argument("task_id")
    fail_parser.add_argument("--reason-code", required=True, choices=[
        "validation_failed", "budget_exhausted", "manual_review_required", "external_blocker",
    ])
    validation_parser = commands.add_parser("validate", help="run one fixed offline validator")
    validation_parser.add_argument("--profile", choices=sorted(VALIDATION_PROFILES), required=True)
    validation_parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        if args.command == "plan":
            print(json.dumps(format_readiness(readiness(plan, _checkpoint_for_cli(args.checkpoint))), ensure_ascii=False, indent=2))
            return 0
        if args.command == "start":
            print(json.dumps(start_task(plan, args.checkpoint, args.task_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "complete":
            complete_task(plan, args.checkpoint, args.task_id, args.commit_sha, args.ci_run_id)
            print(json.dumps({"task_id": args.task_id, "completed": True}))
            return 0
        if args.command == "fail":
            fail_task(plan, args.checkpoint, args.task_id, args.reason_code)
            print(json.dumps({"task_id": args.task_id, "status": "blocked", "reason_code": args.reason_code}))
            return 0
        if args.command == "validate":
            result = run_validation(args.profile, ROOT, args.timeout_seconds)
            print(json.dumps(asdict(result)))
            return 0 if result.passed else 1
    except OrchestratorError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
