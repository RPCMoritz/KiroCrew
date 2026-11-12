"""A chat whose MCP servers wait for a sign-in says so once, without being asked.

On KAS a server that needs a sign-in exposes no tools, and its sign-in is offered
only when the user's message names it. A chat that never names it shows missing
tools and nothing that says why. ``chat_runner._notice_mcp_sign_in_needed`` adds
one row naming the servers the session reports as waiting, read from the
session's own MCP report, and starts no sign-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, JsonRpcMessage
from kiro_crew.dashboard import chat_runner

_REFUSED = "Sign-in for notes could not be refreshed (token endpoint answered 401)."


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every row appended through ``append_and_surface``, as its arguments."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls="", **kw: seen.append(
            {"role": role, "text": text, "cls": cls, "meta": kw.get("meta")}
        ),
    )
    return seen


def _slot(*messages: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(_mcp_sign_in_notice_shown=False, messages=list(messages))


def _notice(slot: SimpleNamespace, *waiting: str) -> None:
    chat_runner._notice_mcp_sign_in_needed(
        SimpleNamespace(), slot, {"awaiting_auth": list(waiting)}
    )


def test_a_waiting_server_is_named_once_with_how_to_sign_in(rows: list) -> None:
    slot = _slot()
    _notice(slot, "notes")
    [row] = rows
    assert row["role"] == "notice"
    assert row["cls"] == "msg msg-warn"
    assert row["meta"] == {"kind": "mcp_sign_in_needed", "servers": ["notes"]}
    # The leading glyph is what NoticeCard reads the warn tone from.
    assert row["text"].startswith("⚠ notes needs you to sign in")
    assert '"Sign in to notes"' in row["text"]
    assert "MCP Servers" in row["text"]
    # The same chat does not say it again, whatever waits later.
    _notice(slot, "notes", "tasks")
    assert len(rows) == 1


def test_several_servers_share_one_row_and_a_long_list_is_counted(rows: list) -> None:
    names = [f"server-{n}" for n in range(7)]
    _notice(_slot(), *names)
    [row] = rows
    assert row["meta"]["servers"] == names[:5]
    assert "and 2 more need you to sign in" in row["text"]
    assert '"Sign in to server-0"' in row["text"]


def test_a_server_whose_sign_in_card_is_open_in_the_chat_is_left_to_that_card(rows: list) -> None:
    card = {"role": "mcp_oauth", "meta": {"server_name": "notes", "oauth_url": "https://x/a"}}
    slot = _slot(card)
    _notice(slot, "notes")
    assert rows == []
    # Nothing was shown, so a server with no card is still named later.
    _notice(slot, "notes", "tasks")
    [row] = rows
    assert row["meta"]["servers"] == ["tasks"]


def test_a_finished_or_withdrawn_card_does_not_count(rows: list) -> None:
    done = {"role": "mcp_oauth", "meta": {"server_name": "notes", "completed": True}}
    _notice(_slot(done), "notes")
    assert [row["meta"]["servers"] for row in rows] == [["notes"]]


@pytest.mark.parametrize("payload", [None, {}, {"awaiting_auth": []}, {"awaiting_auth": [7, ""]}])
def test_nothing_waiting_shows_nothing(rows: list, payload: Any) -> None:
    slot = _slot()
    chat_runner._notice_mcp_sign_in_needed(SimpleNamespace(), slot, payload)
    assert rows == []
    assert slot._mcp_sign_in_notice_shown is False


def test_a_row_that_cannot_be_built_never_fails_the_turn(rows: list) -> None:
    slot = SimpleNamespace(_mcp_sign_in_notice_shown=False)  # no transcript to read
    _notice(slot, "notes")
    assert rows == []


def test_publishing_a_report_with_a_refused_sign_in_shows_the_row(
    rows: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = McpSessionReport()
    report.record_frame(
        JsonRpcMessage(
            method=METHOD_KAS_MCP_STATUS,
            params={
                "sessionId": "s1",
                "servers": [
                    {
                        "name": "notes",
                        "status": "failed",
                        "failedAuthorization": False,
                        "errorMessage": _REFUSED,
                    }
                ],
            },
        ),
        owned=True,
    )
    slot = _slot()
    slot.key = "chat-1"
    slot.set_mcp_report = lambda payload, session_id: False
    provider = SimpleNamespace(mcp_session_report=lambda: report, session_id="s1")
    chat_runner._publish_session_mcp_report(SimpleNamespace(), slot, provider)
    assert [row["meta"]["servers"] for row in rows] == [["notes"]]
