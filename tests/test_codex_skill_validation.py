"""Structural checks for the repository-local Codex Skills library."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_skill_and_behavioral_matrix_structure():
    result = subprocess.run(
        [sys.executable, "scripts/validate_codex_skills.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "behavioral result rows" in result.stdout
    assert "Structural validation only" in result.stdout
