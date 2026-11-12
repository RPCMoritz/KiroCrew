"""The ``mcp.json`` under ``KIRO_HOME`` is named for exclusion on a KAS agent block.

KAS reads its global ``mcp.json`` under ``KIRO_HOME``. Kiro Crew's own global
path (``agent._KIRO_MCP_JSON``) does not follow that variable, so where the two
differ a not-opted-in agent must still be told to leave the engine's servers out.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.kas_settings_mcp import finish_settings_mcp, stamp_settings_mcp
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.config import paths as paths_mod
from kiro_crew.mcp_gateway import session_servers as session_servers_mod


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def crew_global(tmp_path, monkeypatch):
    """Kiro Crew's own global file, as a default install resolves it."""
    path = tmp_path / "home" / ".kiro" / "settings" / "mcp.json"
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", path)
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    return path


@pytest.fixture
def kiro_home(tmp_path, monkeypatch):
    """A ``KIRO_HOME`` that is another folder than Kiro Crew's global one."""
    home = tmp_path / "kiro-home"
    home.mkdir()
    monkeypatch.setenv("KIRO_HOME", str(home))
    return home


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def _finish(work, **stamp):
    agent = {"id": "a", "tools": ["*"], "mcpServers": {"own": {"command": "x"}}}
    (stamped,) = stamp_settings_mcp([agent], "a", work, **stamp)
    return finish_settings_mcp(stamped)


def test_a_server_only_in_the_kiro_home_file_is_excluded(crew_global, kiro_home, work):
    _write(crew_global, {"mcpServers": {"extra": {}}})
    _write(kiro_home / "settings" / "mcp.json", {"mcpServers": {"homeonly": {"command": "h"}}})
    assert _finish(work)["excludedTools"] == ["@extra", "@homeonly"]


def test_an_opted_in_agent_keeps_it_and_gets_its_glob_disabled_tools(crew_global, kiro_home, work):
    _write(
        kiro_home / "settings" / "mcp.json", {"mcpServers": {"homeonly": {"disabledTools": ["w*"]}}}
    )
    assert _finish(work, opted_in=True)["excludedTools"] == ["@homeonly/w*"]


def test_the_kiro_home_entry_wins_over_crews_global_and_loses_to_the_workspace(
    crew_global, kiro_home, work
):
    """The order KAS reads them in: its own global file, then the workspace's."""
    _write(crew_global, {"mcpServers": {"s": {"disabledTools": ["a*"]}, "t": {}}})
    _write(
        kiro_home / "settings" / "mcp.json",
        {"mcpServers": {"s": {"disabledTools": ["b*"]}, "t": {"disabledTools": ["c*"]}}},
    )
    _write(
        work / ".kiro" / "settings" / "mcp.json", {"mcpServers": {"t": {"disabledTools": ["d*"]}}}
    )
    assert _finish(work, opted_in=True)["excludedTools"] == ["@s/b*", "@t/d*"]


def test_without_kiro_home_only_crews_global_file_is_read(crew_global, tmp_path, work, monkeypatch):
    monkeypatch.delenv("KIRO_HOME", raising=False)
    _write(crew_global, {"mcpServers": {"extra": {}}})
    _write(tmp_path / "kiro-home" / "settings" / "mcp.json", {"mcpServers": {"homeonly": {}}})
    assert _finish(work)["excludedTools"] == ["@extra"]


def test_a_kiro_home_that_is_crews_own_folder_is_read_once(crew_global, work, monkeypatch):
    monkeypatch.setenv("KIRO_HOME", str(crew_global.parent.parent))
    _write(crew_global, {"mcpServers": {"extra": {}}})
    notices: list[str] = []
    assert _finish(work, notices=notices)["excludedTools"] == ["@extra"]
    assert notices == []


def test_an_unreadable_kiro_home_file_is_named_and_the_session_still_starts(
    crew_global, kiro_home, work
):
    _write(crew_global, {"mcpServers": {"extra": {}}})
    (kiro_home / "settings").mkdir()
    (kiro_home / "settings" / "mcp.json").write_bytes(
        b"\xff\xfe" + json.dumps({"mcpServers": {"homeonly": {}}}).encode("utf-16-le")
    )
    notices: list[str] = []
    assert _finish(work, notices=notices)["excludedTools"] == ["@extra"]
    (line,) = notices
    assert line.startswith("⚠ Kiro Crew could not read the Kiro home's MCP settings (")
    assert "saved as UTF-16; re-save it as UTF-8" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("with_kiro_home", [True, False])
async def test_a_folder_agents_kas_session_carries_the_exclusion(
    monkeypatch, tmp_path, with_kiro_home
):
    """What ``session/new`` sends for a checkout's own agent, which never opts in."""
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent: frozenset()
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-global.json")
    monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", lambda *_a, **_kw: [{"id": "b"}])
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    home = tmp_path / "kiro-home"
    _write(home / "settings" / "mcp.json", {"mcpServers": {"homeonly": {"command": "h"}}})
    if with_kiro_home:
        monkeypatch.setenv("KIRO_HOME", str(home))
    else:
        monkeypatch.delenv("KIRO_HOME", raising=False)
    checkout = tmp_path / "checkout"
    spec = {"name": "b", "prompt": "p", "tools": ["*"]}
    _write(checkout / ".kiro" / "agents" / "b.json", spec)

    extras = await harness_for(ACP_BACKEND_KAS).session_extras("b", work_dir=str(checkout))
    params: dict = {"cwd": str(checkout), "mcpServers": []}
    attach_kas_custom_agents(params, extras.custom_agents)

    (sent,) = params["_meta"]["kiro"]["customAgents"]
    assert sent.get("excludedTools", []) == (["@homeonly"] if with_kiro_home else [])
