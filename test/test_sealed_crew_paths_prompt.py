# SPDX-License-Identifier: Apache-2.0
"""The agent is told what a sandbox refusal on ``config.json`` or ``work-ledger`` means.

The OS sandbox seals ``config.json`` read-only and hides ``work-ledger``, so a
script that touches either gets a bare errno that no Kiro Crew code sees. The
prompts of the agents that run such scripts, and of the root conductor that reads
their reports, are the only place that explains the refusal and names the route
that works.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import sandbox_leaves
from kiro_crew.agent_parts import spec_prompts

#: What each prompt must name: the two paths, the three errors a sealed path
#: raises, and the supported routes.
_REQUIRED = (
    "config.json",
    "work-ledger",
    "Operation not permitted",
    "Read-only file system",
    "Device or resource busy",
    "setting.change",
    "propose_change",
    "work_ledger_read",
)


def _default_contract() -> str:
    shipped = Path(kiro_crew.__file__).parent / "config" / "prompt.md"
    return shipped.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param(_default_contract, id="default-contract"),
        pytest.param(lambda: spec_prompts._CONDUCTOR_SYSTEM_PROMPT, id="root-conductor"),
        pytest.param(lambda: spec_prompts._WORKER_SYSTEM_PROMPT, id="worker"),
    ],
)
def test_prompt_explains_a_sealed_crew_path_refusal(prompt) -> None:
    text = " ".join(prompt().split())
    missing = [token for token in _REQUIRED if token not in text]
    assert missing == []


def test_the_named_paths_are_the_ones_the_sandbox_seals() -> None:
    """The prompt names real fences: if either leaf moves, the sentence is wrong."""
    assert "config.json" in sandbox_leaves._CREW_READONLY_LEAVES
    assert "work-ledger" in sandbox_leaves._CREW_HIDDEN_LEAVES
