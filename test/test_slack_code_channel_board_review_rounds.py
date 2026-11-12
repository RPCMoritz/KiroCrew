"""The work board's edge cases: lineage status, publish scope and the publisher.

Split from ``test_slack_code_channel_board_live.py`` (the file cap): replaced
conductors, refused bars, same-second answers and finished calls. Shares the
board suites' fixtures and helpers (imported, so they apply here too).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_slack_code_channel_board as suite
import test_slack_code_channel_board_live as live

from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store
from kiro_crew.slack.code_channels.board import model, publisher, render, source
from kiro_crew.slack.code_channels.workspace import service

# The board suites' fixtures (autouse ones included) and helpers, bound here so
# they apply to this module too.
_no_sandbox = suite._no_sandbox
_feature_on = suite._feature_on
_surface_store = suite._surface_store
publisher_clock = suite.publisher_clock
slack = suite.slack
repo = suite.repo
StepClock = suite.StepClock
_Harness = suite._Harness
_Item = suite._Item
_SlackError = suite._SlackError
_NOW = suite._NOW
_binding = suite._binding
_link = suite._link
_publish = suite._publish
_reader = suite._reader
_row = suite._row
_snapshot = suite._snapshot
lineage = live.lineage
_Slots = live._Slots
_live_slot = live._live_slot
_watched = live._watched
_waiting_board = live._waiting_board
_active_snapshot = live._active_snapshot

# ── GPT / Opus round on 64cc0b6a74 ─────────────────────────────────────────


def test_a_long_run_of_failures_keeps_the_capped_backoff_computable():
    """GPT: 3 ** 647 overflowed a float, so the retry was never armed again."""
    publisher._failures["C1"] = 5000
    assert publisher._failed("C1", "update", _SlackError("ratelimited")) == "failed"
    assert publisher.take_retry("C1") == publisher._RETRY_CAP_SECS


def test_a_long_running_worker_keeps_asking_the_driver_for_processing(
    tmp_path, monkeypatch, lineage
):
    """GPT: Slack ends ``processing`` after an hour; the watch re-asks every pass."""
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    for _ in range(3):
        asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"] * 3


@pytest.mark.parametrize("answer", ["out_of_scope", "archived", "stopped", "denied"])
def test_a_board_not_shown_at_send_time_clears_the_sidebar_summary(monkeypatch, answer):
    from kiro_crew.slack.code_channels.board import links

    conductor = SimpleNamespace(key="chat-cond", _work_aggregate={"label": "earlier"})
    state = SimpleNamespace(get_slot=lambda key: conductor if key == "chat-cond" else None)
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    source.set_scope_predicate(lambda st, b: True)

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        return answer

    monkeypatch.setattr(publisher, "publish", fake_publish)
    snap = model.BoardSnapshot(rows=(_row(),))
    assert asyncio.run(board_pkg.publish(_binding(), snap)) == answer
    assert conductor._work_aggregate is None


def test_a_refresh_that_fails_after_its_read_clears_the_sidebar_summary(tmp_path, monkeypatch):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = _binding()
    asyncio.run(h.svc.ensure(binding))
    cleared: list = []
    monkeypatch.setattr(service, "_clear_aggregate", lambda b: cleared.append(b))

    async def boom(b, snap):
        raise RuntimeError("Slack went away")

    h.svc._publish_board = boom  # the board step fails after the read
    asyncio.run(h.svc.refresh(binding))
    assert cleared == [binding] and h.published == []
    board_pkg._timers.pop("C1", None)


def test_a_failing_channel_step_never_keeps_the_board_from_going_out(tmp_path, monkeypatch):
    """Adversarial F1: a channel PR read that raised every pass meant no board, ever."""
    from kiro_crew.slack.code_channels.workspace import git_ops

    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = _binding()
    asyncio.run(h.svc.ensure(binding))

    async def boom(record):
        raise git_ops.SandboxUnavailable("no sandbox backend")

    monkeypatch.setattr(h.svc, "_refresh_pr", boom)
    asyncio.run(h.svc.refresh(binding))
    assert len(h.published) == 1 and "T1:C1" in h.svc._board_refreshed


def test_the_watch_loop_does_not_inherit_the_starting_turns_context(tmp_path, monkeypatch):
    """A loop started inside a turn would tie the status driver's writes to that turn."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")
    h = _Harness(tmp_path)
    seen: list[str] = []

    async def tick(state):
        seen.append(marker.get())
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_WATCH_SECS", 0.0)
    monkeypatch.setattr(h.svc, "watch_tick", tick)

    async def main():
        marker.set("the starting turn")
        h.svc.start_watch(lambda: object())
        try:
            await h.svc._watch
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert seen == [""]


# ── GPT on a061b43b5d: lineage-driven status obeys the board's publish scope ──


def _lineage_watch(tmp_path, monkeypatch, lineage):
    lineage.record_worker("chat-w1", "chat-cond")
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: False)  # confirmed
    w1 = _live_slot("chat-w1")
    cond = _live_slot("chat-cond")
    dash = _Slots(**{"chat-cond": cond, "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    return h, rec, statuses, w1, cond, dash


def test_a_refused_channel_gets_no_status_from_a_lineage_worker(tmp_path, monkeypatch, lineage):
    h, _rec, statuses, w1, _cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    source.set_scope_predicate(lambda state, b: False)  # Slack Connect-shared
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_a_channel_refused_mid_run_hands_the_workers_status_back(tmp_path, monkeypatch, lineage):
    h, rec, statuses, w1, _cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"]
    source.set_scope_predicate(lambda state, b: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing", "active"] and rec.key not in h.svc._working
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing", "active"]  # once


def test_a_refused_channel_never_overrides_a_running_conductor(tmp_path, monkeypatch, lineage):
    h, _rec, statuses, w1, cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"]
    cond.turn_running = True  # the conductor's own turn owns the status now
    source.set_scope_predicate(lambda state, b: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["processing"]


def test_a_confirmed_refusal_ends_a_status_hold(tmp_path, monkeypatch, lineage):
    h, rec, statuses, w1, _cond, dash = _lineage_watch(tmp_path, monkeypatch, lineage)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    h.svc._status_hold[rec.key] = h.svc._clock() + 600  # held while sharing was unknown
    source.set_scope_predicate(lambda state, b: False)
    asyncio.run(h.svc.watch_tick(dash))
    assert rec.key not in h.svc._status_hold and statuses[-1] == "active"


def test_a_worker_filed_into_the_channel_folder_still_obeys_the_scope(
    tmp_path, monkeypatch, lineage
):
    """The board files lineage workers into the channel's folder: lineage still decides."""
    from kiro_crew.slack.code_channels.workspace import folders

    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, statuses = _watched(tmp_path, monkeypatch, folder_id="F-chan")
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: False)
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    monkeypatch.setattr(folders, "subtree_ids", lambda state, fid: {fid})
    monkeypatch.setattr(
        folders,
        "sessions_in",
        lambda rows, ids, worktree="": {"chat-w1": (w1.turn_running, w1._turn_generation)},
    )
    asyncio.run(h.svc.watch_tick(dash))
    source.set_scope_predicate(lambda state, b: False)
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_a_cached_waiting_row_sets_no_suspended_on_a_refused_channel(
    tmp_path, monkeypatch, lineage
):
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_sharing_unknown", lambda channel: False)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    # Admitted by the pass's first check, refused by the status check.
    verdicts = iter([True] + [False] * 10)
    source.set_scope_predicate(lambda state, b: next(verdicts))
    asyncio.run(h.svc.watch_tick(dash))
    assert "suspended" not in statuses and rec.key not in h.svc._boards


def test_the_board_is_not_pinned_once_the_scope_refuses(slack):
    _publish(_snapshot())
    pins = len(slack.of("add_pin"))
    surface_store.store().update("C1", board_pinned=False)  # someone unpinned it
    asyncio.run(
        publisher.publish(
            _binding(), _snapshot(), link_for=_link, owner_id="U0", admits=lambda: False
        )
    )
    assert len(slack.of("add_pin")) == pins


# ── the watch's status, scope and refresh edges ────────────────────────────────────────────────


def test_a_second_approval_under_the_same_report_pages_the_owner_again(slack):
    """Two approvals with no report between share report_stamp: each still pages once."""

    def asking(approval_id):
        return model.BoardSnapshot(
            rows=(
                _row(
                    state=model.WAITING_ON_YOU,
                    waiting_on="approval",
                    report_stamp="r1",
                    approval_key=model.approval_key(approval_id),
                ),
            )
        )

    _publish(asking("req-1"))
    _publish(asking("req-1"))  # the same approval: once
    assert len(slack.of("post_message")) == 1
    _publish(asking("req-2"))  # answered, then the next one inside one debounce
    assert len(slack.of("post_message")) == 2


def test_the_approval_key_comes_from_the_pending_approvals_request_id(monkeypatch):
    from kiro_crew.dashboard.session_control_parts import session_status

    slot = SimpleNamespace(
        key="chat-w1",
        project="",
        to_dict=lambda: {"pending_approval_info": {"tool_kind": "execute", "request_id": "r-9"}},
    )
    monkeypatch.setattr(source, "find_slot", lambda state, key: slot)
    monkeypatch.setattr(
        session_status,
        "_pending_approval_fields",
        lambda state, s: {"pending_approval": True, "pending_approval_tool": "Running: ls"},
    )
    asked = source.approval_activity(object(), "chat-w1")
    assert asked.approval_id == "r-9"
    assert model.approval_key("r-9") and model.approval_key("r-9") != model.approval_key("r-8")
    assert model.approval_key("") == ""


def test_a_conductor_turn_between_passes_makes_the_watch_resend_the_waiting_status(
    tmp_path, monkeypatch, lineage
):
    """The conductor's own hooks overwrote the status: the watch must not trust its cache."""
    h, rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"]
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended"]  # unchanged: nothing re-sent
    dash.get_slot("chat-cond")._turn_generation += 1  # a short turn started and ended
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == ["suspended", "suspended"]


def test_closing_an_idle_worker_refreshes_the_board(tmp_path, monkeypatch, lineage):
    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, scheduled, _statuses = _watched(tmp_path, monkeypatch)
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": _live_slot("chat-w1")})
    asyncio.run(h.svc.watch_tick(dash))
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == []
    del dash._slots["chat-w1"]  # the owner closed the idle worker
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [0.0]
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled == [0.0]  # once


# ── security: publish only what the channel admits ──


@pytest.mark.parametrize("where", ["C1", "C-origin"])
@pytest.mark.parametrize("mode", ["off", "review"])
def test_a_channel_or_origin_set_off_or_review_admits_no_board(monkeypatch, where, mode):
    """The activation gate is part of THE scope check, so every board path obeys it."""
    from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW

    source.set_scope_predicate(lambda state, binding: True)  # the exemption admits it
    binding = replace(_binding(), origin_channel_id="C-origin")
    assert source.scope_admits(None, binding)
    activation = ACTIVATION_OFF if mode == "off" else ACTIVATION_REVIEW
    monkeypatch.setattr(
        cc_binding, "live_config", lambda orch=None: suite._LiveConfig({where: activation})
    )
    assert not source.scope_admits(None, binding)


def test_the_board_publish_honours_an_off_channel(monkeypatch):
    from kiro_crew.config.loader import ACTIVATION_OFF
    from kiro_crew.slack.code_channels.board import links

    sent: list = []

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        sent.append(snapshot)
        return "updated"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    source.set_scope_predicate(lambda state, binding: True)
    monkeypatch.setattr(
        cc_binding, "live_config", lambda orch=None: suite._LiveConfig({"C1": ACTIVATION_OFF})
    )
    assert asyncio.run(board_pkg.publish(_binding(), _snapshot())) == "out_of_scope"
    assert sent == []


def test_worker_activity_refreshes_nothing_in_an_off_channel(tmp_path, repo, monkeypatch):
    """Off or Review: no bar, no diff tab, no board from a worker-triggered refresh."""
    from kiro_crew.config.loader import ACTIVATION_REVIEW

    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(title="Fix login"),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("a worker edited this\n")  # a new diff
    before = len(h.client.calls)
    monkeypatch.setattr(
        cc_binding,
        "live_config",
        lambda orch=None: suite._LiveConfig({"C1": ACTIVATION_REVIEW}),
    )
    asyncio.run(h.svc.refresh(binding))
    assert h.client.calls[before:] == [] and h.published == []


def test_the_watch_writes_no_worker_status_in_an_off_channel(tmp_path, monkeypatch, lineage):
    from kiro_crew.config.loader import ACTIVATION_OFF

    lineage.record_worker("chat-w1", "chat-cond")
    h, _rec, _scheduled, statuses = _watched(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cc_binding, "live_config", lambda orch=None: suite._LiveConfig({"C1": ACTIVATION_OFF})
    )
    w1 = _live_slot("chat-w1")
    dash = _Slots(**{"chat-cond": _live_slot("chat-cond"), "chat-w1": w1})
    asyncio.run(h.svc.watch_tick(dash))
    w1.turn_running, w1._turn_generation = True, 1
    asyncio.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_a_board_whose_scope_flips_mid_publish_sends_no_more(slack):
    """Asked again before each write: a refusal during the edit's retry posts nothing."""
    calls = {"n": 0}

    def admits():
        calls["n"] += 1
        return calls["n"] <= 2  # the post's two asks (the loop's, the send gate's) only

    waiting = model.BoardSnapshot(rows=(_row(state=model.WAITING_ON_YOU, report_stamp="r1"),))
    asyncio.run(
        publisher.publish(_binding(), waiting, link_for=_link, owner_id="U0", admits=admits)
    )
    assert len(slack.of("post_blocks")) == 1
    assert slack.of("post_message") == []  # the needs-you mention was re-checked and refused
    slack.fail["update_message"] = "message_not_found"
    calls["n"] = 0
    out = asyncio.run(
        publisher.publish(_binding(), _snapshot(), link_for=_link, owner_id="U0", admits=admits)
    )
    assert out == "out_of_scope" and len(slack.of("post_blocks")) == 1  # no repost


def test_the_publish_wrapper_hands_the_publisher_the_live_scope_check(monkeypatch):
    from kiro_crew.slack.code_channels.board import links

    seen: dict = {}

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        seen["admits"] = admits
        return "updated"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    verdict = {"now": True}
    source.set_scope_predicate(lambda state, binding: verdict["now"])
    asyncio.run(board_pkg.publish(_binding(), _snapshot()))
    assert callable(seen["admits"]) and seen["admits"]()
    verdict["now"] = False  # the publisher's later re-checks see the change
    assert not seen["admits"]()


def test_the_conductors_turn_hooks_forget_the_watchs_status_record(tmp_path, monkeypatch):
    """The base's turn hooks hand the status to the conductor; the watch's record goes."""
    h = _Harness(tmp_path)
    monkeypatch.setattr(h.svc, "ensure", lambda binding: asyncio.sleep(0))
    monkeypatch.setattr(h.svc, "schedule_refresh", lambda binding, *, delay=0.0: None)
    key = "T1:C1"
    for hook in (h.svc.on_turn_start, h.svc.on_turn_end):
        h.svc._working[key] = "suspended"
        h.svc._status_failures[key] = 2
        h.svc._status_hold[key] = h.svc._clock() + 60
        asyncio.run(hook(_binding()))
        assert key not in h.svc._working
        assert key not in h.svc._status_failures and key not in h.svc._status_hold


# ── filing, foreign repositories and stored rows ────────────────────────────────────────────────


def test_a_worker_filed_at_creation_and_later_unfiled_stays_out(monkeypatch):
    """GPT: an already-foldered worker was not remembered, so unfiling it was overridden."""
    from kiro_crew.slack.code_channels.board import filing

    state = suite._State(
        {
            "chat-cond": suite._slot("chat-cond", folder="F"),
            "chat-w1": suite._slot("chat-w1", folder="F"),
        }
    )
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)

    async def fake_place(st, slot, folder):
        slot.folder_id = folder
        return True

    monkeypatch.setattr(filing, "_place", fake_place)
    snap = model.BoardSnapshot(rows=(_row("it_00000001", session_key="chat-w1"),))
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 0
    state.slots["chat-w1"].folder_id = ""  # the owner moves it out
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 0
    assert state.slots["chat-w1"].folder_id == ""


def _two_repos(tmp_path, repo, monkeypatch):
    """The channel on acme/widgets; a worker whose session moved to acme/other."""
    other = tmp_path / "other"
    other.mkdir()
    suite._git("init", "-q", str(other), cwd=tmp_path)
    suite._git("remote", "add", "origin", "git@github.com:acme/other.git", cwd=other)
    w1 = _live_slot("chat-w1", project=str(other))
    state = _Slots(**{"chat-w1": w1})
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(service.git_ops, "remote_url", _remote_for(repo, other))
    return other


def _remote_for(repo, other):
    def remote(cwd):
        return (
            "git@github.com:acme/other.git"
            if str(cwd).startswith(str(other))
            else "git@github.com:acme/widgets"
        )

    return remote


def test_a_stored_board_record_reads_back_from_disk(tmp_path):
    """The base's strict row check must not read the board's bool and lists as corrupt."""
    path = tmp_path / "surface.json"
    first = surface_store.SurfaceStore(path)
    first.update(
        "C9", board_ts="100.1", board_pinned=True, board_alerts=("it_1:r1",), board_filed=("w1",)
    )
    again = surface_store.SurfaceStore(path).get("C9")
    assert again is not None
    assert again.board_pinned is True and again.board_filed == ("w1",)
    assert again.board_alerts == ("it_1:r1",)


@pytest.mark.parametrize(
    "bad", [{"board_pinned": "yes"}, {"board_filed": "w1"}, {"board_alerts": [1]}]
)
def test_a_wrong_typed_board_field_is_an_unreadable_row(tmp_path, bad):
    path = tmp_path / "surface.json"
    surface_store.SurfaceStore(path).update("C9", board_ts="100.1")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("records", data)
    row = rows[0] if isinstance(rows, list) else rows["C9"]
    row.update(bad)
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    # The base's rule: a row this build can't read makes the file unreadable, and
    # it is never rewritten (so nothing in it is silently shortened or lost).
    with pytest.raises(surface_store.SurfaceStoreUnavailable):
        surface_store.SurfaceStore(path).get("C9")
    assert path.read_bytes() == before


# ── a cached board belongs to the conductor it was read for ──


def _cached_for(h, conductor, title="Old worker"):
    key = "T1:C1"
    snap = model.BoardSnapshot(rows=(_row(state=model.DONE, title=title),))
    h.svc._boards[key] = snap
    h.svc._board_owner[key] = conductor
    return key, snap


def test_a_board_cached_for_a_replaced_conductor_is_not_reused(tmp_path):
    h = _Harness(tmp_path)
    key, snap = _cached_for(h, "chat-cond")
    assert h.svc.board("T1", "C1", session_key="chat-cond") is snap
    # The binding now names a replacement conductor: its caller gets nothing.
    assert h.svc.board("T1", "C1", session_key="chat-cond-2") is None
    # Once the service learns the new conductor, every read site drops it.
    h.svc._conductor_of[key] = "chat-cond-2"
    assert h.svc._cached_board(key) is None
    assert key not in h.svc._boards and key not in h.svc._board_owner
    assert not h.svc._board_due(key, {})


# ── publisher: the shared retry rule and the board pin (rounds on 7669f7cde1 and 64403785b0) ──


def test_the_board_classifies_and_waits_out_slack_errors_by_the_shared_rule(slack):
    """``slack.retry`` is the one Retry-After parser and retry classifier."""
    for name in ("_retry_after", "_PERMANENT"):
        assert not hasattr(publisher, name)
    # A 429's Retry-After is honoured, whatever its header's case.
    assert (
        publisher._failed("C1", "update", _SlackError("ratelimited", headers={"retry-after": "42"}))
        == "failed"
    )
    assert publisher.take_retry("C1") == 42.0 and not publisher.refused_permanently("C1")
    # A Retry-After on anything but a 429 is not read, and a negative one is ignored.
    publisher.forget("C1")
    publisher._failed("C1", "update", _SlackError("internal_error", headers={"Retry-After": "42"}))
    assert publisher.take_retry("C1") == 5.0
    publisher.forget("C1")
    publisher._failed("C1", "update", _SlackError("ratelimited", headers={"Retry-After": "-9"}))
    assert publisher.take_retry("C1") == 5.0
    # Slack's own "try again" answers come as an ok:false body on a 200: retried.
    for code in ("internal_error", "fatal_error", "request_timeout"):
        publisher.forget("C1")
        publisher._failed("C1", "update", _SlackError(code))
        assert publisher.take_retry("C1") == 5.0 and not publisher.refused_permanently("C1")
    # Slack's own refusal (an ok:false body on a 200) is not retried on a timer.
    publisher.forget("C1")
    assert publisher._failed("C1", "update", _SlackError("msg_too_long")) == "failed"
    assert publisher.refused_permanently("C1") and publisher.take_retry("C1") == 0.0
    # A post Slack answered with no ts is retried.
    publisher.forget("C1")
    publisher._failed("C1", "post", None)
    assert publisher.take_retry("C1") == 5.0 and not publisher.refused_permanently("C1")


def test_an_install_that_grants_pins_write_pins_the_board_on_its_first_publish(slack):
    """The manifest asks for ``pins:write`` with code channels, and a fresh grant pins."""
    from kiro_crew import slack_manifest
    from kiro_crew.slack import features

    on = slack_manifest.requirements(frozenset({"code_channels"}))
    off = slack_manifest.requirements(frozenset())
    assert publisher.PIN_SCOPE == "pins:write"
    assert "pins:write" in on.bot_scopes and "pins:write" not in off.bot_scopes
    features.record_granted_scopes(["chat:write", "code_channels:manage", "pins:write"])
    assert _publish(_active_snapshot()) == "posted"
    assert slack.of("add_pin") == [("add_pin", "C1", "100.1")]
    assert surface_store.store().get("C1").board_pinned


def test_a_re_approved_pins_write_pins_at_once_not_after_the_backoff(slack, publisher_clock):
    """An install that hadn't re-approved: the 10-minute backoff, until the grant lands."""
    from kiro_crew.slack import features

    slack.fail["add_pin"] = "missing_scope"  # granted: chat:write, code_channels:manage
    assert _publish(_active_snapshot()) == "posted"
    publisher_clock.step = publisher.MIN_EDIT_GAP_SECS
    _publish(model.BoardSnapshot(rows=_active_snapshot().rows, round=2))
    assert len(slack.of("add_pin")) == 1  # backing off
    features.record_granted_scopes(["chat:write", "code_channels:manage", "pins:write"])
    _publish(model.BoardSnapshot(rows=_active_snapshot().rows, round=3))
    assert len(slack.of("add_pin")) == 2
    assert surface_store.store().get("C1").board_pinned


def test_a_pin_slack_refuses_is_retried_only_every_repin_interval(slack, publisher_clock):
    """An install without ``pins:write``: one failing ``pins.add`` per interval, not per publish."""
    slack.fail["add_pin"] = "missing_scope"
    assert _publish(_active_snapshot()) == "posted"
    assert len(slack.of("add_pin")) == 1
    publisher_clock.step = publisher.MIN_EDIT_GAP_SECS
    for n in range(3):
        _publish(model.BoardSnapshot(rows=_active_snapshot().rows, round=n + 2))
    assert len(slack.of("add_pin")) == 1
    publisher_clock.step = publisher.REPIN_SECS
    _publish(_active_snapshot())
    assert len(slack.of("add_pin")) == 2
    assert surface_store.store().get("C1").board_pinned


# ── a refused bar or Code tab doesn't stop the board's watch ──


def test_a_slack_off_channel_still_watches_its_workers(tmp_path, monkeypatch, lineage):
    """Slack refused its bar (``slack_off``), but its board keeps publishing.

    A worker that goes from idle to running, with no report or approval, still
    refreshes the board, as it does in any other bound channel.
    """
    h, rec, scheduled, _statuses = _watched(tmp_path, monkeypatch)
    dash = _waiting_board(h, rec, monkeypatch, lineage)
    stored = h.svc.store.get("T1", "C1")
    stored.slack_off = "not_agent_channel"
    h.svc.store.put(stored)
    asyncio.run(h.svc.watch_tick(dash))  # the first look at its workers
    scheduled.clear()
    dash._slots["chat-w1"].turn_running = True
    dash._slots["chat-w1"]._turn_generation = 1
    asyncio.run(h.svc.watch_tick(dash))
    assert scheduled, "the worker's turn start must refresh the board"


# ── a same-second decide never answers a newer question ──

_SAME_SECOND = "2026-11-05T10:00:00+00:00"


@pytest.mark.parametrize("decide_last", [False, True], ids=["question-after", "decide-after"])
def test_the_store_seam_reads_a_same_second_answer_from_event_order(monkeypatch, decide_last):
    """The decide and the question share one stamp: only the tail's order tells."""
    from kiro_crew import work_ledger
    from kiro_crew.dashboard.handlers import work_ledger_board

    asked = work_ledger.WorkItem(
        item_id="it_0000000a",
        worker_session_key="chat-w1",
        status="question",
        reason="needs_human",
        last_report_at=_SAME_SECOND,
    )
    asked.decided_at = _SAME_SECOND
    decide = work_ledger.WorkEvent(ts=_SAME_SECOND, item_id="it_0000000a", kind="decision")
    report = work_ledger.WorkEvent(
        ts=_SAME_SECOND, item_id="it_0000000a", kind="report", status="question"
    )
    events = [report, decide] if decide_last else [decide, report]
    monkeypatch.setattr(work_ledger, "read_conductor", lambda key: work_ledger.ConductorRecord())
    monkeypatch.setattr(work_ledger, "list_work_items", lambda key: [asked])
    monkeypatch.setattr(work_ledger, "read_binding", lambda key: ("cond", "it_0000000a"))
    monkeypatch.setattr(work_ledger, "cache_dirty", lambda key: None)
    monkeypatch.setattr(work_ledger, "read_events", lambda slot, item, limit=None: list(events))
    _record, items = asyncio.run(work_ledger_board.own_worker_records("cond"))
    assert items[0].answered_by_event is decide_last


def test_a_question_after_a_same_second_decide_waits_on_the_owner_and_pages_them(slack):
    """The board's own read: waiting on you, the owner paged, the waiting count kept."""
    asked = _Item(
        "it_0000000e",
        title="Pick a retry limit",
        status="question",
        reason="needs_human",
        worker_session_key="chat-w1",
        last_report_at=_SAME_SECOND,
    )
    asked.decided_at = _SAME_SECOND  # the decide that answered the EARLIER report
    asked.answered_by_event = False  # the tail says the question came after it
    snap = source.project(SimpleNamespace(goal="", round=1), [asked], lambda k: model.LIVE_IDLE)
    [row] = snap.rows
    assert row.state == model.WAITING_ON_YOU and row.needs_owner
    assert "1 waiting on you" in snap.aggregate
    agg = model.work_aggregate(snap)
    assert agg["state"] == "waiting" and agg["counts"]["waiting"] == 1
    assert _publish(snap) == "posted"
    [alert] = slack.of("post_message")
    assert "Pick a retry limit" in alert[2] and alert[3] == "100.1"


# ── a finished call is not "Running" ──


def test_a_finished_tool_call_is_not_shown_as_running():
    """The worker's command returned (``meta.done``) and it is reasoning, with no new row."""
    slot = _live_slot("chat-w1", running=True, project="/p")
    slot.messages = [
        {"role": "user", "content": "go", "ts": "2026-10-20T13:20:00+00:00"},
        {
            "role": "tool",
            "content": "🔧 Running",
            "meta": {"kind": "execute", "input": '{"command": "make test"}', "done": True},
        },
    ]
    activity = source.worker_activity(_Slots(**{"chat-w1": slot}), "chat-w1")
    assert activity.turn_started_at == "2026-10-20T13:20:00+00:00"  # the turn start stays
    assert model.tool_step(activity) == ""
    assert model.step_text(activity) == model.STEP_THINKING
    slot.messages[-1]["meta"]["done"] = False  # still in flight: shown as before
    assert model.step_text(source.worker_activity(_Slots(**{"chat-w1": slot}), "chat-w1")) == (
        "Running make test"
    )


# ── a Stop fences the work in flight at the press, not the board's later passes ──


def _stopped_channel(binding):
    """The conductor's turn starts (a real ``_turn_started_at`` entry), then Stop."""
    asyncio.run(surface.note_turn_start(binding, None))
    surface.mark_stopped(binding.channel_id)
    assert surface.channel_stopped(binding.channel_id)  # the stopped turn is fenced


def _unstop(channel):
    surface._stopped_at.pop(channel, None)
    surface._turn_started_at.pop(channel, None)
    board_pkg._timers.pop(channel, None)


def test_a_stop_with_the_conductor_idle_still_lets_helper_agent_events_publish(tmp_path, repo):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    _stopped_channel(binding)

    async def go():
        await h.svc.on_turn_end(binding)  # the stopped turn's own refresh: skipped
        await h.svc.drain()
        after_stopped_turn = len(h.published)
        await h.svc.refresh(binding, recheck_pr=False)  # a helper agent's turn ends
        return after_stopped_turn

    try:
        assert asyncio.run(go()) == 0
        assert len(h.published) == 1
        with surface.own_pass():
            token = surface.admit()
            surface.mark_stopped(binding.channel_id)  # a Stop of the pass in flight
            try:
                assert surface.channel_stopped(binding.channel_id)
            finally:
                surface.release(token)
    finally:
        _unstop(binding.channel_id)


def test_a_delayed_rearm_still_refreshes_after_a_stop_of_the_turn_that_armed_it(
    tmp_path, repo, monkeypatch
):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    real_schedule = board_pkg._schedule

    def fake_schedule(channel_id, delay=None):
        if delay is not None:
            return real_schedule(channel_id, delay)
        h.svc.schedule_refresh(binding, delay=0.0)
        return True

    monkeypatch.setattr(board_pkg, "_schedule", fake_schedule)

    async def go():
        await surface.note_turn_start(binding, None)
        token = surface.admit()  # the turn's work arms the re-arm
        try:
            assert board_pkg._arm_timer("C1", 3600.0)
        finally:
            surface.release(token)
        surface.mark_stopped("C1")  # then the person stops that turn
        _due, handle = board_pkg._timers["C1"]
        handle._run()  # the timer fires (run in the context it was armed with)
        handle.cancel()
        await h.svc.drain()

    try:
        asyncio.run(go())
    finally:
        _unstop("C1")
    assert len(h.published) == 1  # the board's own pass is not fenced by that Stop


# ── Slack's "try again" answers: the board retries them, the shared rule doesn't ──


def _ok_false(code):
    from unittest.mock import MagicMock

    from slack_sdk.errors import SlackApiError

    resp = MagicMock()
    resp.status_code = 200
    resp.get = lambda key, default=None: code if key == "error" else default
    return SlackApiError(message=code, response=resp)


@pytest.mark.parametrize("code", sorted(publisher._BOARD_TRANSIENT))
def test_the_board_retries_slacks_try_again_answers_the_shared_rule_keeps_its_list(code):
    from kiro_crew.slack.retry import is_retryable_slack_error

    # The DM open and the OPTIONS expiry read this rule: it is unchanged for them.
    assert not is_retryable_slack_error(_ok_false(code))
    publisher.forget("C1")
    publisher._failed("C1", "update", _SlackError(code))
    assert publisher.take_retry("C1") == 5.0 and not publisher.refused_permanently("C1")


def test_the_dm_open_and_options_expiry_still_treat_a_200_internal_error_as_final():
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack.outbound import PostedOptions, expire_options
    from kiro_crew.slack.retry import open_dm_with_retry

    slack = MagicMock()
    slack.open_dm = AsyncMock(side_effect=_ok_false("internal_error"))
    with pytest.raises(Exception):
        asyncio.run(open_dm_with_retry(slack, "U1"))
    assert slack.open_dm.await_count == 1  # not retried
    slack.update_message = AsyncMock(side_effect=_ok_false("internal_error"))
    posted = PostedOptions(channel="C1", ts="1.0", choices=("a",), blocks=(), text="t")
    assert asyncio.run(expire_options(slack, posted)) is True  # settled, not kept


# ── home-relative paths ──


@pytest.mark.parametrize(("name", "token"), [("posix", "~"), ("nt", "%USERPROFILE%")])
def test_a_home_path_is_shown_home_relative_on_each_platform(name, token):
    assert model._home_token(name) == token


# ── GPT on b954b1acf7: a Stop that lands during an admitted publish ──


def _publish_stopped_during(monkeypatch, owner, name):
    """Publish as admitted work while Slack's Stop lands inside ``owner.name``."""
    stopped = [False]
    monkeypatch.setattr(surface, "channel_stopped", lambda channel: stopped[0])
    real = getattr(owner, name)

    async def _stop_then(*a, **k):
        stopped[0] = True
        return await real(*a, **k)

    monkeypatch.setattr(owner, name, _stop_then)

    async def go():
        token = surface.admit()  # a refresh is admitted work
        try:
            return await publisher.publish(_binding(), _snapshot(), link_for=_link, owner_id="U0")
        finally:
            surface.release(token)

    return asyncio.run(go())


def test_a_stop_during_the_policy_check_sends_no_board(slack, monkeypatch):
    assert _publish_stopped_during(monkeypatch, surface, "outbound_permitted") == "stopped"
    assert slack.calls == []


def test_a_stop_during_the_post_pins_nothing_and_pages_nobody(slack, monkeypatch):
    # "stopped": the pin and the page are owed to the pass the refresh re-arms.
    assert _publish_stopped_during(monkeypatch, slack, "post_blocks") == "stopped"
    assert len(slack.of("post_blocks")) == 1  # Slack already holds this one
    assert slack.of("add_pin") == [] and slack.of("post_message") == []


def test_a_path_scrub_cuts_only_whole_path_segments(monkeypatch):
    """Adversarial: ``/home/u/repo2`` came out as ``.2``, ``/home/ux`` as ``~x``."""
    monkeypatch.setattr(model.os.path, "expanduser", lambda p: "/home/u" if p == "~" else p)
    home = model._home_token()
    out = model._host_paths_out("see /home/u/repo/a /home/u/repo2/x and /home/ux/y", "/home/u/repo")
    assert out == f"see a {home}/repo2/x and …/y"
    assert model._host_paths_out("built /home/u/repo.", "/home/u/repo") == "built .."
    # A project above home is not cut: the user name would be left behind.
    assert model._host_paths_out("cat /home/u/x", "/home") == f"cat {home}/x"


def test_running_helpers_counts_the_watched_sessions_running_but_the_channels_own(tmp_path):
    h = _Harness(tmp_path)
    h.svc._seen["T1:C1"] = {
        "dashboard_chat-cond": (True, 1),  # the channel's own session
        "chat-w1": (True, 1),
        "chat-w2": (False, 1),  # idle
        "dashboard_chat-w3": (True, 2),
    }
    assert h.svc.running_helpers(suite._FakeBinding()) == 2
    assert h.svc.running_helpers(suite._FakeBinding(channel_id="C2")) == 0
    source.set_scope_predicate(lambda state, b: False)  # shared: the board withholds them
    assert h.svc.running_helpers(suite._FakeBinding()) == 0


def test_a_stop_clears_the_suspended_the_last_turn_left_for_the_worker_status(monkeypatch):
    """Adversarial: an idle Stop set ``active``, then the watch set ``suspended`` again."""
    from kiro_crew.slack.code_channels import status

    monkeypatch.setattr(status, "_driver", lambda: None)
    monkeypatch.setitem(status._after_turn, "C-held", "suspended")
    asyncio.run(status.settle_after_stop("C-held"))
    assert status.after_turn("C-held") == ""


# ── GPT / adversarial on 0878ae2b6c: gate at the send, published means sent ──


def test_a_stop_mid_pass_on_the_last_helper_event_still_ends_with_a_published_board(
    tmp_path, repo, monkeypatch
):
    """Adversarial: the fenced pass carried the last event, so nothing re-armed it."""
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(state=model.DONE),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    real_find = h.svc._find_pr
    pressed: list[bool] = []

    async def find_then_stop(repo_ref, branch):
        if not pressed:
            pressed.append(True)
            surface.mark_stopped("C1")  # the person presses Stop during the gh work
        return await real_find(repo_ref, branch)

    h.svc._find_pr = find_then_stop
    shown: list = []

    async def publish_board(b, snap):  # answers as board.publish does under the fence
        if surface.channel_stopped(b.channel_id):
            return "stopped"
        shown.append(snap)
        return "posted"

    h.svc._publish_board = publish_board
    monkeypatch.setattr(
        board_pkg, "_schedule", lambda ch, delay=None: h.svc.schedule_refresh(binding) or True
    )

    async def go():
        await surface.note_turn_start(binding, None)  # the channel has a real turn entry
        await h.svc.refresh(binding, recheck_pr=False)  # the helper agent's last event
        assert pressed and shown == []  # that pass was fenced
        _due, handle = board_pkg._timers["C1"]
        handle._run()  # the one re-publish, in the clean context it was armed with
        handle.cancel()
        await h.svc.drain()

    try:
        asyncio.run(go())
    finally:
        _unstop("C1")
        board_pkg._unsent.pop("C1", None)
    assert len(shown) == 1 and shown[0].rows[0].state == model.DONE


def test_no_pass_edits_the_board_before_slacks_retry_after(slack, monkeypatch):
    """GPT on d939cc22bb: the 60 s stamp timer (or the watch) retried inside Retry-After."""
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    _publish(_snapshot())
    slack.fail["update_message"] = "ratelimited"
    real = slack._maybe_fail

    def with_retry_after(method):
        if slack.fail.get(method) == "ratelimited":
            slack.fail.pop(method)
            raise _SlackError("ratelimited", status=429, headers={"Retry-After": "120"})
        return real(method)

    monkeypatch.setattr(slack, "_maybe_fail", with_retry_after)
    moved = model.BoardSnapshot(rows=_snapshot().rows[:1])
    now[0] += 30.0  # past the edit gap
    assert _publish(moved) == "failed"
    edits = len(slack.of("update_message"))
    publisher.take_retry("C1")
    now[0] += 60.0  # the stamp's own re-arm, or a watch pass, fires here
    assert publisher.holding("C1")
    assert _publish(moved) == "deferred" and len(slack.of("update_message")) == edits
    assert publisher.take_retry("C1") == pytest.approx(60.0)  # re-armed for the rest
    now[0] += 61.0
    assert not publisher.holding("C1")
    assert _publish(moved) == "updated" and not publisher.holding("C1")


def test_a_re_armed_pass_that_cannot_read_its_record_stays_pending(tmp_path, repo, monkeypatch):
    """Adversarial N2: an exit before the read used up the one pending re-publish."""
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(),)))
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    board_pkg._unsent["C1"] = 1  # an earlier pass left the board unsent; this is its re-arm
    monkeypatch.setattr(h.svc._store, "get", lambda team, channel: None)  # unreadable store

    async def no_ensure(b, **kw):
        return None

    monkeypatch.setattr(h.svc, "ensure", no_ensure)

    async def go():
        await h.svc.refresh(binding)
        assert "C1" in board_pkg._timers  # still one re-publish pending
        board_pkg._timers["C1"][1].cancel()

    asyncio.run(go())
    assert board_pkg._unsent["C1"] == 2 and h.published == []


def test_an_edit_the_clients_stop_fence_would_drop_counts_as_unsent(slack):
    """Adversarial F2: a muted chat.update returned like a sent one, so the board read as sent."""
    from kiro_crew.slack.agent_sessions import fence

    assert _publish(_snapshot()) == "posted"
    edits = len(slack.of("update_message"))

    async def go():
        reset = fence.enter_turn("C1", "999.0")
        try:
            fence.note_message("C1", "100.1")  # the turn's scope holds the board message
            fence.current_scope().ended = True  # Slack's native Stop ended that turn
            moved = model.BoardSnapshot(rows=_snapshot().rows[:1])
            return await publisher.publish(_binding(), moved, link_for=_link, owner_id="U0")
        finally:
            fence.exit_turn(reset)

    assert asyncio.run(go()) == "stopped"  # unsent: the refresh re-arms it
    assert len(slack.of("update_message")) == edits


def test_retry_after_is_timed_from_slacks_answer_not_the_passs_start(slack, monkeypatch):
    """GPT on 6e414c23dc: a slow 429 answer shortened Slack's required wait."""
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    _publish(_snapshot())
    real = slack._maybe_fail

    def slow_429(method):
        if method == "update_message":
            now[0] += 30.0  # Slack answers 30 s into the request
            raise _SlackError("ratelimited", status=429, headers={"Retry-After": "120"})
        return real(method)

    monkeypatch.setattr(slack, "_maybe_fail", slow_429)
    now[0] += 30.0  # past the edit gap
    assert _publish(model.BoardSnapshot(rows=_snapshot().rows[:1])) == "failed"
    answered = now[0]
    now[0] = answered + 119.0
    assert publisher.holding("C1")  # the full 120 s from the answer
    now[0] = answered + 121.0
    assert not publisher.holding("C1")


# ── GPT on 6e414c23dc: a bound bounds every field it retains ──


def _long_history():
    """2,000 settled items (accepted, rejected, abandoned) and 5 open ones."""
    finals = ("accepted", "rejected", "abandoned")
    items = [
        suite._Item(
            f"it_{n:08x}",
            title=f"Old {n}",
            state=finals[n % 3],
            created_at=f"2026-10-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z",
        )
        for n in range(2000)
    ]
    items += [
        suite._Item(f"it_f{n:07x}", title=f"Live {n}", created_at="2026-10-02T00:00:00Z")
        for n in range(5)
    ]
    return items


def test_a_long_history_keeps_the_newest_settled_rows_and_every_total(monkeypatch):
    record = SimpleNamespace(goal="", round=1)
    live = lambda key: model.LIVE_IDLE  # noqa: E731
    bounded = source.project(record, _long_history(), live)
    monkeypatch.setattr(model, "MAX_SETTLED", 10**6)
    unbounded = source.project(record, _long_history(), live)
    settled = [r for r in bounded.rows if r.settled]
    assert len(settled) == 100 and len(bounded.rows) == 105
    assert settled[-1].title == "Old 1999" and settled[0].title == "Old 1900"  # the newest
    assert bounded.total == unbounded.total == 2005 and bounded.dropped == 1900
    assert bounded.counts == unbounded.counts and bounded.done == unbounded.done == 667
    assert bounded.aggregate == unbounded.aggregate == "5 idle · 1333 closed · 667 of 2005 done"
    assert bounded.overall == unbounded.overall
    assert bounded.omitted == unbounded.omitted
    assert model.work_aggregate(bounded) == model.work_aggregate(unbounded)
    for look in ("plan", "rows"):
        same = [
            render.board_blocks(s, _link, updated="10:00 UTC", look=look, now=suite._NOW)
            for s in (bounded, unbounded)
        ]
        assert same[0] == same[1], look
    assert render.fallback_text(bounded) == render.fallback_text(unbounded)


def test_the_page_clock_keeps_only_the_items_paged_inside_the_gap(slack, monkeypatch):
    """Class sweep: the per-item page times grew with every item ever paged."""
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    for n in range(5):
        row = _row(f"it_{n:08x}", model.WAITING_ON_YOU, report_stamp=f"r{n}")
        _publish(model.BoardSnapshot(rows=(row,)))
        now[0] += publisher.PAGE_GAP_SECS  # each one paged a minute apart
    assert len(slack.of("post_message")) == 5
    assert len(publisher._paged["C1"]) <= 1  # older page times are let go
