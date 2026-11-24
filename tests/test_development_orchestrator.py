from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import development_orchestrator as orchestrator


@pytest.fixture(autouse=True)
def _sandbox_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    skill = tmp_path / ".agents" / "skills" / "python-testing-ci"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Test Skill\n", encoding="utf-8")
    (tmp_path / "SKILLS_CATALOG.md").write_text(
        "| python-testing-ci | .agents/skills/python-testing-ci/SKILL.md |\n",
        encoding="utf-8",
    )


def _task(
    task_id: str,
    *,
    depends_on: list[str] | None = None,
    scope: list[str] | None = None,
    status: str = "pending",
    gates: list[str] | None = None,
    skills: list[str] | None = None,
    priority: str = "P1",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "priority": priority,
        "depends_on": depends_on or [],
        "scope_files": scope or [f"src/{task_id}.py"],
        "skills": skills or ["python-testing-ci"],
        "budget_minutes": 30,
        "max_attempts": 3,
        "required_evidence": ["offline_tests", "ci_success"],
        "manual_gates": gates or [],
        "initial_status": status,
    }


def _write_plan(path: Path, tasks: list[dict[str, Any]], max_agents: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "max_parallel_agents": max_agents,
        "safety_policy": {
            "mode": "PAPER",
            "allow_live": False,
            "allow_secrets": False,
            "allow_signing": False,
            "allow_transactions": False,
            "allow_rpc": False,
            "allow_arbitrary_shell": False,
        },
        "tasks": tasks,
    }), encoding="utf-8")


def test_load_plan_rejects_dependency_cycles(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [
        _task("task_alpha", depends_on=["task_bravo"]),
        _task("task_bravo", depends_on=["task_alpha"]),
    ])

    with pytest.raises(orchestrator.OrchestratorError, match="cycle"):
        orchestrator.load_plan(plan_path)


@pytest.mark.parametrize("scope", [
    "../outside.py", "C:/outside.py", ".env", ".envrc", "private/key.pem", "secrets/token.txt",
    "secrets.yaml", "credentials.yml", "wallet.json", "private_key.txt", "api_key.txt",
    "seed_phrase.txt",
])
def test_load_plan_rejects_unsafe_scope_paths(tmp_path: Path, scope: str) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [_task("task_alpha", scope=[scope])])

    with pytest.raises(orchestrator.OrchestratorError):
        orchestrator.load_plan(plan_path)


def test_load_plan_enforces_three_agent_slot_cap(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [_task("task_alpha")], max_agents=4)

    with pytest.raises(orchestrator.OrchestratorError, match="max_parallel_agents"):
        orchestrator.load_plan(plan_path)


def test_load_plan_allows_wallet_manager_source_file(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [_task("task_alpha", scope=["wallet_manager.py"])])

    plan = orchestrator.load_plan(plan_path)

    assert plan.tasks[0].scope_files == ("wallet_manager.py",)


def test_load_plan_rejects_skill_missing_from_project_catalog(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [_task("task_alpha", skills=["not-a-project-skill"])])

    with pytest.raises(orchestrator.OrchestratorError, match="not present in .agents/skills"):
        orchestrator.load_plan(plan_path)


def test_skill_inventory_rejects_symlinked_agents_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    original_is_symlink = Path.is_symlink
    agents_path = tmp_path / ".agents"

    def fake_is_symlink(path: Path) -> bool:
        return path == agents_path or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(orchestrator.OrchestratorError, match="symbolic links"):
        orchestrator._available_skill_ids()


def test_plan_and_checkpoint_paths_cannot_escape_repository(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-plan.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(orchestrator.OrchestratorError, match="inside the repository"):
        orchestrator.load_plan(outside)
    with pytest.raises(orchestrator.OrchestratorError, match="inside the repository"):
        orchestrator.load_checkpoint(tmp_path.parent / "outside-checkpoint.json")


def test_checkpoint_rejects_completed_task_without_ci_evidence(tmp_path: Path) -> None:
    checkpoint = tmp_path / "state" / "checkpoint.json"
    state = orchestrator.empty_checkpoint()
    state["tasks"]["task_alpha"] = {"status": "completed", "attempts": 1}
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(orchestrator.OrchestratorError, match="missing required CI evidence"):
        orchestrator.load_checkpoint(checkpoint)


def test_mutating_cli_rejects_an_alternate_plan_within_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    canonical = root / "orchestration" / "missions.json"
    alternate = root / "tests" / "alternate_plan.json"
    canonical.parent.mkdir(parents=True)
    alternate.parent.mkdir(parents=True)
    canonical.write_text("{}", encoding="utf-8")
    alternate.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "ROOT", root)
    monkeypatch.setattr(orchestrator, "DEFAULT_PLAN", canonical)
    monkeypatch.setattr(
        orchestrator, "load_plan",
        lambda _path: pytest.fail("alternate plan must be rejected before it is loaded"),
    )

    result = orchestrator.main([
        "--plan", str(alternate), "--checkpoint", str(root / ".orchestrator" / "checkpoint.json"),
        "start", "task_alpha",
    ])

    assert result == 2
    assert "canonical orchestration/missions.json" in capsys.readouterr().err


def test_complete_task_refuses_manual_gate_without_calling_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [_task("task_alpha", gates=["independent_review"])])
    plan = orchestrator.load_plan(plan_path)
    monkeypatch.setattr(
        orchestrator, "urlopen",
        lambda *_args, **_kwargs: pytest.fail("manual gate must block before GitHub access"),
    )

    with pytest.raises(orchestrator.OrchestratorError, match="reviewed plan change"):
        orchestrator.complete_task(plan, checkpoint, "task_alpha", "a" * 40, 12345)

    assert not checkpoint.exists()


def test_readiness_honors_dependencies_capacity_and_file_locks(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [
        _task("task_alpha", scope=["src/shared.py"]),
        _task("task_bravo", scope=["src/shared.py"]),
        _task("task_charlie", depends_on=["task_alpha"], scope=["src/other.py"]),
        _task("task_delta", scope=["src/fourth.py"]),
    ], max_agents=3)
    plan = orchestrator.load_plan(plan_path)

    result = orchestrator.readiness(plan, orchestrator.empty_checkpoint(), slots=15)

    assert [task.task_id for task in result.ready] == ["task_alpha", "task_delta"]
    assert result.ready[0].skills == ("python-testing-ci",)
    proposal = orchestrator.format_readiness(result)
    assert proposal["ready"][0]["skills"] == ["python-testing-ci"]
    assert result.max_parallel_agents == 3
    assert "file_conflict_in_batch:src/shared.py" in result.blockers["task_bravo"]
    assert "dependencies:task_alpha" in result.blockers["task_charlie"]


def test_readiness_reports_overlap_between_active_external_tasks(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [
        _task("task_alpha", scope=["rpc_client.py"], status="external_in_progress"),
        _task("task_bravo", scope=["rpc_client.py"], status="external_in_progress"),
        _task("task_charlie", scope=["src/other.py"]),
    ])
    plan = orchestrator.load_plan(plan_path)

    result = orchestrator.readiness(plan, orchestrator.empty_checkpoint())

    assert "active_scope_conflict:task_bravo" in result.blockers["task_alpha"]
    assert "active_scope_conflict:task_alpha" in result.blockers["task_bravo"]
    assert [task.task_id for task in result.ready] == ["task_charlie"]


def test_expired_budget_keeps_file_lock_until_manual_recovery(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [
        _task("task_alpha", scope=["src/shared.py"]),
        _task("task_bravo", scope=["src/shared.py"]),
    ])
    plan = orchestrator.load_plan(plan_path)
    state = orchestrator.empty_checkpoint()
    state["tasks"]["task_alpha"] = {
        "status": "in_progress",
        "attempts": 1,
        "run_id": "run-1",
        "started_at": (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(),
    }

    result = orchestrator.readiness(plan, state)

    assert "budget_expired_manual_recovery_required" in result.blockers["task_alpha"]
    assert "file_locked:src/shared.py:task_alpha" in result.blockers["task_bravo"]
    assert result.ready == ()


def test_fail_task_cannot_retry_an_expired_lease_or_release_its_file_lock(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [
        _task("task_alpha", scope=["src/shared.py"]),
        _task("task_bravo", scope=["src/shared.py"]),
    ])
    plan = orchestrator.load_plan(plan_path)
    orchestrator.start_task(plan, checkpoint, "task_alpha")
    state = orchestrator.load_checkpoint(checkpoint)
    state["tasks"]["task_alpha"]["started_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=31)
    ).isoformat()
    orchestrator._save_checkpoint(checkpoint, state)

    with pytest.raises(orchestrator.OrchestratorError, match="budget expired"):
        orchestrator.fail_task(plan, checkpoint, "task_alpha", "validation_failed")

    saved = orchestrator.load_checkpoint(checkpoint)
    result = orchestrator.readiness(plan, saved)
    assert saved["tasks"]["task_alpha"]["status"] == "in_progress"
    assert "file_locked:src/shared.py:task_alpha" in result.blockers["task_bravo"]


def test_complete_task_refuses_expired_lease_before_ci_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    orchestrator.start_task(plan, checkpoint, "task_alpha")
    state = orchestrator.load_checkpoint(checkpoint)
    state["tasks"]["task_alpha"]["started_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=31)
    ).isoformat()
    orchestrator._save_checkpoint(checkpoint, state)
    monkeypatch.setattr(
        orchestrator, "verify_ci_run",
        lambda *_args, **_kwargs: pytest.fail("expired task must not query CI"),
    )

    with pytest.raises(orchestrator.OrchestratorError, match="budget expired"):
        orchestrator.complete_task(plan, checkpoint, "task_alpha", "a" * 40, 12345)

    assert orchestrator.load_checkpoint(checkpoint)["tasks"]["task_alpha"]["status"] == "in_progress"


def test_start_task_records_a_lease_and_excludes_overlapping_tasks(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [
        _task("task_alpha", scope=["src/shared.py"]),
        _task("task_bravo", scope=["src/shared.py"]),
    ])
    plan = orchestrator.load_plan(plan_path)

    lease = orchestrator.start_task(plan, checkpoint, "task_alpha")
    result = orchestrator.readiness(plan, orchestrator.load_checkpoint(checkpoint))

    assert lease["task_id"] == "task_alpha"
    assert len(lease["run_id"]) == 32
    assert [task.task_id for task in result.ready] == []
    state = orchestrator.load_checkpoint(checkpoint)
    assert state["tasks"]["task_alpha"]["status"] == "in_progress"
    assert state["audit"][-1]["event"] == "task_started"


def test_validation_failure_retries_then_escalates_at_attempt_limit(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)

    for attempt in (1, 2):
        orchestrator.start_task(plan, checkpoint, "task_alpha")
        orchestrator.fail_task(plan, checkpoint, "task_alpha", "validation_failed")
        state = orchestrator.load_checkpoint(checkpoint)
        assert state["tasks"]["task_alpha"]["status"] == "pending"
        assert state["tasks"]["task_alpha"]["attempts"] == attempt
        assert state["tasks"]["task_alpha"]["escalation_required"] is False

    orchestrator.start_task(plan, checkpoint, "task_alpha")
    orchestrator.fail_task(plan, checkpoint, "task_alpha", "validation_failed")
    state = orchestrator.load_checkpoint(checkpoint)
    assert state["tasks"]["task_alpha"]["status"] == "blocked"
    assert state["tasks"]["task_alpha"]["attempts"] == 3
    assert state["tasks"]["task_alpha"]["escalation_required"] is True
    assert state["audit"][-1]["event"] == "task_blocked_escalation_required"


def test_manual_review_and_external_failures_are_not_retried(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    orchestrator.start_task(plan, checkpoint, "task_alpha")

    orchestrator.fail_task(plan, checkpoint, "task_alpha", "external_blocker")

    entry = orchestrator.load_checkpoint(checkpoint)["tasks"]["task_alpha"]
    assert entry["status"] == "blocked"
    assert entry["attempts"] == 1
    assert entry["escalation_required"] is True


def test_budget_exhaustion_blocks_without_automatic_retry(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    orchestrator.start_task(plan, checkpoint, "task_alpha")

    orchestrator.fail_task(plan, checkpoint, "task_alpha", "budget_exhausted")

    entry = orchestrator.load_checkpoint(checkpoint)["tasks"]["task_alpha"]
    assert entry["status"] == "blocked"
    assert entry["attempts"] == 1
    assert entry["escalation_required"] is True


def test_checkpoint_replace_failure_preserves_previous_checkpoint(monkeypatch, tmp_path: Path) -> None:
    checkpoint = tmp_path / ".orchestrator" / "checkpoint.json"
    original = orchestrator.empty_checkpoint()
    original["audit"].append({"event": "original"})
    orchestrator._save_checkpoint(checkpoint, original)
    original_bytes = checkpoint.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    changed = orchestrator.empty_checkpoint()
    changed["audit"].append({"event": "changed"})

    with pytest.raises(orchestrator.OrchestratorError, match="atomically"):
        orchestrator._save_checkpoint(checkpoint, changed)

    assert checkpoint.read_bytes() == original_bytes
    assert list(checkpoint.parent.glob("tmp*")) == []


def test_stale_lock_fails_closed_without_overwriting_checkpoint(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    checkpoint.parent.mkdir(parents=True)
    lock = checkpoint.with_name(checkpoint.name + ".lock")
    lock.write_text("pid=unknown\n", encoding="ascii")

    with pytest.raises(orchestrator.OrchestratorError, match="lock is held"):
        orchestrator.start_task(plan, checkpoint, "task_alpha")
    assert not checkpoint.exists()


def test_manual_gate_allows_work_but_remains_pending_for_completion(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [_task("task_alpha", gates=["independent_review"])])
    plan = orchestrator.load_plan(plan_path)

    result = orchestrator.readiness(plan, orchestrator.empty_checkpoint())

    assert [task.task_id for task in result.ready] == ["task_alpha"]
    assert "task_alpha" not in result.blockers
    proposal = orchestrator.format_readiness(result)["next_task_proposal"]
    assert proposal["task_id"] == "task_alpha"
    assert proposal["runnable"] is True
    assert proposal["blocked_by"] == []
    assert proposal["pending_manual_gates"] == ["independent_review"]
    assert orchestrator.format_readiness(result)["ready"][0]["pending_manual_gates"] == ["independent_review"]


def test_next_task_proposal_prefers_blocked_p0_over_runnable_p1(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path, [
        _task("task_dependency", status="external_in_progress"),
        _task("task_p0", priority="P0", depends_on=["task_dependency"], gates=["independent_review"]),
        _task("task_p1", priority="P1"),
    ])
    plan = orchestrator.load_plan(plan_path)

    result = orchestrator.readiness(plan, orchestrator.empty_checkpoint())
    proposal = orchestrator.format_readiness(result)["next_task_proposal"]

    assert [task.task_id for task in result.ready] == ["task_p1"]
    assert proposal["task_id"] == "task_p0"
    assert proposal["runnable"] is False
    assert "dependencies:task_dependency" in proposal["blocked_by"]
    assert proposal["pending_manual_gates"] == ["independent_review"]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _ci_payload(
    sha: str,
    *,
    tested_sha: str | None = None,
    conclusion: str = "success",
    repository: str = "cheicki225/-meme-coin",
    workflow_path: str = ".github/workflows/ci.yml",
) -> dict[str, Any]:
    return {
        "name": "CI",
        "path": workflow_path,
        "head_sha": tested_sha or sha,
        "status": "completed",
        "conclusion": conclusion,
        "html_url": "https://github.com/cheicki225/-meme-coin/actions/runs/12345",
        "event": "pull_request",
        "repository": {"full_name": repository},
        "pull_requests": [{"head": {"sha": sha}}],
    }


def _ci_jobs_payload(
    *, omit_step: str | None = None, conclusion: str = "success", job_name: str = "validate",
) -> dict[str, Any]:
    steps = [
        {"name": name, "conclusion": "success"}
        for name in sorted(orchestrator.REQUIRED_CI_STEPS)
        if name != omit_step
    ]
    return {"jobs": [{"name": job_name, "conclusion": conclusion, "steps": steps}]}


def _ci_workflow_payload(*, blob_sha: str | None = None) -> dict[str, Any]:
    return {
        "path": ".github/workflows/ci.yml",
        "sha": blob_sha or orchestrator.TRUSTED_CI_WORKFLOW_BLOB_SHA,
    }


def test_verify_ci_requires_exact_repository_workflow_sha_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    tested_sha = "b" * 40
    responses = iter([
        _ci_payload(sha, tested_sha=tested_sha),
        _ci_workflow_payload(),
        _ci_jobs_payload(),
    ])
    monkeypatch.setattr(orchestrator, "urlopen", lambda *_args, **_kwargs: _FakeResponse(next(responses)))

    proof = orchestrator.verify_ci_run(12345, sha)

    assert proof["commit_sha"] == sha
    assert proof["tested_commit_sha"] == tested_sha
    assert proof["conclusion"] == "success"
    assert len(proof["verified_steps"]) == len(orchestrator.REQUIRED_CI_STEPS)

    monkeypatch.setattr(
        orchestrator, "urlopen",
        lambda *_args, **_kwargs: _FakeResponse(_ci_payload("c" * 40, tested_sha=tested_sha)),
    )
    with pytest.raises(orchestrator.OrchestratorError, match="source SHA"):
        orchestrator.verify_ci_run(12345, sha)

    monkeypatch.setattr(
        orchestrator, "urlopen",
        lambda *_args, **_kwargs: _FakeResponse(_ci_payload(sha, workflow_path=".github/workflows/other.yml")),
    )
    with pytest.raises(orchestrator.OrchestratorError, match="validation workflow"):
        orchestrator.verify_ci_run(12345, sha)


def test_verify_ci_rejects_wrong_repository_and_missing_required_step(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "e" * 40
    monkeypatch.setattr(
        orchestrator, "urlopen",
        lambda *_args, **_kwargs: _FakeResponse(_ci_payload(sha, repository="other/repo")),
    )
    with pytest.raises(orchestrator.OrchestratorError, match="different repository"):
        orchestrator.verify_ci_run(12345, sha)

    responses = iter([
        _ci_payload(sha), _ci_workflow_payload(),
        _ci_jobs_payload(omit_step="Scan repository secrets"),
    ])
    monkeypatch.setattr(orchestrator, "urlopen", lambda *_args, **_kwargs: _FakeResponse(next(responses)))
    with pytest.raises(orchestrator.OrchestratorError, match="required CI steps"):
        orchestrator.verify_ci_run(12345, sha)

    responses = iter([_ci_payload(sha), _ci_workflow_payload(), _ci_jobs_payload(job_name="unexpected")])
    monkeypatch.setattr(orchestrator, "urlopen", lambda *_args, **_kwargs: _FakeResponse(next(responses)))
    with pytest.raises(orchestrator.OrchestratorError, match="validation job did not succeed"):
        orchestrator.verify_ci_run(12345, sha)


def test_verify_ci_rejects_changed_workflow_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "f" * 40
    responses = iter([
        _ci_payload(sha),
        _ci_workflow_payload(blob_sha="0" * 40),
    ])
    monkeypatch.setattr(orchestrator, "urlopen", lambda *_args, **_kwargs: _FakeResponse(next(responses)))

    with pytest.raises(orchestrator.OrchestratorError, match="trusted reviewed revision"):
        orchestrator.verify_ci_run(12345, sha)


def test_complete_task_verifies_ci_before_checkpointing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    sha = "c" * 40
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    orchestrator.start_task(plan, checkpoint, "task_alpha")
    tested_sha = "e" * 40
    responses = iter([_ci_payload(sha, tested_sha=tested_sha), _ci_workflow_payload(), _ci_jobs_payload()])
    monkeypatch.setattr(orchestrator, "urlopen", lambda *_args, **_kwargs: _FakeResponse(next(responses)))

    orchestrator.complete_task(plan, checkpoint, "task_alpha", sha, 12345)

    state = orchestrator.load_checkpoint(checkpoint)
    assert state["tasks"]["task_alpha"]["status"] == "completed"
    assert state["tasks"]["task_alpha"]["ci_run_id"] == 12345
    assert state["tasks"]["task_alpha"]["commit_sha"] == sha
    assert state["tasks"]["task_alpha"]["tested_commit_sha"] == tested_sha


def test_complete_task_rejects_ci_proof_after_a_new_attempt_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    sha = "b" * 40
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    original = orchestrator.start_task(plan, checkpoint, "task_alpha")

    def verify_after_restarting(_run_id: int, commit_sha: str) -> dict[str, Any]:
        orchestrator.fail_task(plan, checkpoint, "task_alpha", "validation_failed")
        replacement = orchestrator.start_task(plan, checkpoint, "task_alpha")
        assert replacement["run_id"] != original["run_id"]
        return {
            "run_id": 12345,
            "commit_sha": commit_sha,
            "tested_commit_sha": commit_sha,
            "conclusion": "success",
            "url": "https://github.com/cheicki225/-meme-coin/actions/runs/12345",
            "workflow_blob_sha": orchestrator.TRUSTED_CI_WORKFLOW_BLOB_SHA,
            "verified_steps": sorted(orchestrator.REQUIRED_CI_STEPS),
        }

    monkeypatch.setattr(orchestrator, "verify_ci_run", verify_after_restarting)

    with pytest.raises(orchestrator.OrchestratorError, match="task run changed"):
        orchestrator.complete_task(plan, checkpoint, "task_alpha", sha, 12345)

    current = orchestrator.load_checkpoint(checkpoint)["tasks"]["task_alpha"]
    assert current["status"] == "in_progress"
    assert current["run_id"] != original["run_id"]
    assert current["attempts"] == 2


def test_complete_task_requires_its_declared_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    sha = "9" * 40
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    orchestrator.start_task(plan, checkpoint, "task_alpha")
    monkeypatch.setattr(orchestrator, "verify_ci_run", lambda *_args, **_kwargs: {
        "run_id": 12345,
        "commit_sha": sha,
        "tested_commit_sha": sha,
        "conclusion": "success",
        "url": "https://github.com/cheicki225/-meme-coin/actions/runs/12345",
        "workflow_blob_sha": orchestrator.TRUSTED_CI_WORKFLOW_BLOB_SHA,
        "verified_steps": sorted(orchestrator.REQUIRED_CI_STEPS - {"Run unit tests"}),
    })

    with pytest.raises(orchestrator.OrchestratorError, match="offline_tests"):
        orchestrator.complete_task(plan, checkpoint, "task_alpha", sha, 12345)

    assert orchestrator.load_checkpoint(checkpoint)["tasks"]["task_alpha"]["status"] == "in_progress"


def test_complete_task_rejects_failed_ci_without_changing_running_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    checkpoint = tmp_path / "state" / "checkpoint.json"
    sha = "d" * 40
    _write_plan(plan_path, [_task("task_alpha")])
    plan = orchestrator.load_plan(plan_path)
    orchestrator.start_task(plan, checkpoint, "task_alpha")
    monkeypatch.setattr(orchestrator, "urlopen", lambda *_args, **_kwargs: _FakeResponse(_ci_payload(sha, conclusion="failure")))

    with pytest.raises(orchestrator.OrchestratorError, match="successfully"):
        orchestrator.complete_task(plan, checkpoint, "task_alpha", sha, 12345)
    assert orchestrator.load_checkpoint(checkpoint)["tasks"]["task_alpha"]["status"] == "in_progress"


def test_validation_profile_uses_allowlisted_no_shell_command_and_minimal_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("HELIUS_RPC_URL", "https://invalid.example/secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-real-token")
    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    result = orchestrator.run_validation("unit", tmp_path, timeout_seconds=42)

    assert result.passed
    assert captured["shell"] is False
    assert captured["command"][:3] == [orchestrator.sys.executable, "-m", "pytest"]
    assert captured["timeout"] == 42
    assert "HELIUS_RPC_URL" not in captured["env"]
    assert "TELEGRAM_BOT_TOKEN" not in captured["env"]
    assert captured["env"]["TRADING_MODE"] == "PAPER"
    assert captured["env"]["LIVE_EXECUTION_ENABLED"] == "false"


def test_validation_rejects_arbitrary_commands_and_unbounded_timeouts(tmp_path: Path) -> None:
    with pytest.raises(orchestrator.OrchestratorError, match="not allowlisted"):
        orchestrator.run_validation("powershell -Command anything", tmp_path)
    with pytest.raises(orchestrator.OrchestratorError, match="timeout_seconds"):
        orchestrator.run_validation("unit", tmp_path, timeout_seconds=3600)
    with pytest.raises(orchestrator.OrchestratorError, match="inside the repository"):
        orchestrator.run_validation("compile", tmp_path.parent)


def test_mission_manifest_tracks_review_pending_and_active_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "ROOT", orchestrator.DEFAULT_PLAN.parent.parent)
    plan = orchestrator.load_plan()
    review_pending_ids = {
        "wallet_intelligence_provenance",
        "anti_rug_source_provenance",
        "backtest_valuation_metadata",
    }
    blocked_ids = {
        "mission_8_5_helius_reliability",
        "mission_8_6_pr10_pr11_integration",
        "mission_7_operational_gates",
        *review_pending_ids,
    }
    active_ids = {"pnl_sol_usd_fee_ledger", "risk_exit_controls_review"}

    assert len(plan.tasks) == 18
    reconciliation_scopes = set(
        plan.by_id["mission_8_5_8_6_reconciliation"].scope_files
    )
    assert {
        "telegram_bot.py",
        "tests/test_rpc_redirect_security.py",
        "tests/test_telegram_wallet_message_deletion.py",
    }.issubset(reconciliation_scopes)
    assert {
        task.task_id for task in plan.tasks
        if task.initial_status == "blocked"
    } == blocked_ids
    assert {
        task.task_id for task in plan.tasks
        if task.initial_status == "external_in_progress"
    } == active_ids
    assert plan.max_parallel_agents == 3

    result = orchestrator.readiness(plan, orchestrator.empty_checkpoint())
    assert result.ready == ()
    for task_id in review_pending_ids:
        assert result.blockers[task_id] == ("status:blocked",)
    for task_id in active_ids:
        assert result.blockers[task_id] == ("status:external_in_progress",)
    assert result.next_task_proposal is not None
    assert result.next_task_proposal.task_id == "mission_8_5_8_6_reconciliation"
    assert "dependencies:mission_8_5_helius_reliability,mission_8_6_pr10_pr11_integration" in result.blockers[
        "mission_8_5_8_6_reconciliation"
    ]
