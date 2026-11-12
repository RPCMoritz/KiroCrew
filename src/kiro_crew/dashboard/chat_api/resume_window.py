"""A live slot's in-memory window: caught up with its transcript on disk, served to a resume."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        _attach_variants,
        _ChatSlot,
        _collapse_wire_rows,
        _live_child_instance,
        _prepare_messages,
        _redact_meta_for_role,
        carry_provenance,
        load_window_off_loop,
        logger,
        queue_view,
        redact_credentials,
        redact_exfiltration_urls,
        remember_unknown_row_fields,
        slot_history_key,
    )


async def _reconcile_slot_window(state: DashboardState, slot: "_ChatSlot") -> None:
    """Detect and reconcile stale in-memory window from disk.

    A live slot's window can fall behind disk when messages are written to the
    session file by a path that does not (or cannot) also push into the
    in-memory window — e.g. a concurrent subagent flush, a channel-origin
    append, or a persistence race during heavy traffic.

    This function compares the slot's believed disk coverage
    (``_disk_older_count + len(messages)``) against the actual on-disk message
    count. If disk has grown beyond what the slot accounts for, the missing
    tail is read and appended to the in-memory window, making the next detail
    or resume response self-healing on refresh.

    Safety: skips reconciliation when the slot has unflushed in-memory rows
    beyond what the last save persisted (``len(messages) > _disk_window_len``),
    because a concurrent flush could persist those rows between the
    ``represented`` snapshot and the disk read, leading to a duplicate
    append. Re-validates after the await to guard against appends that landed
    during the disk read.
    """
    if not state.conversation_log:
        return
    # Safety gate: do not reconcile a slot that has in-memory rows the last
    # flush has not yet persisted, or that is mid-rewind, or that has unsaved
    # in-place edits (dirty) — clearing dirty at the end would erase the edit.
    if len(slot.messages) > getattr(slot, "_disk_window_len", 0):
        return
    if getattr(slot, "_pending_rewrite", False):
        return
    if getattr(slot, "_dirty_flag", False):
        return
    history_key = slot_history_key(slot)
    # Deliberately ``_disk_older_count``, NOT ``_disk_older_durable_count``:
    # this reconciliation reasons about the on-disk FILE LAYOUT (how many disk
    # lines the slot represents), so the all-rows counter is the one whose
    # units match. The durable counter exists for absolute message positions
    # (session_control.read_messages), a different measurement.
    represented = (getattr(slot, "_disk_older_count", 0) or 0) + len(slot.messages)
    try:
        disk_msgs = await asyncio.to_thread(
            state.conversation_log.read_messages_chained, history_key
        )
    except Exception:
        logger.warning("reconcile: read_messages_chained failed for %s", history_key, exc_info=True)
        return
    disk_total = len(disk_msgs)
    if disk_total <= represented:
        return
    # Post-await safety: the slot may have received appends (and a flush) while
    # we were reading disk. Re-check and recompute represented to avoid
    # duplicating rows that arrived during the await.
    if len(slot.messages) > getattr(slot, "_disk_window_len", 0):
        return
    if getattr(slot, "_pending_rewrite", False):
        return
    if getattr(slot, "_dirty_flag", False):
        return
    represented = (getattr(slot, "_disk_older_count", 0) or 0) + len(slot.messages)
    if disk_total <= represented:
        return
    # Validate alignment: if transcript rotation shifted offsets, the disk
    # prefix does not match memory — abort to avoid appending wrong rows.
    # The slot's window starts at disk offset _disk_older_count, so we compare
    # the last memory row against its expected position on disk.
    disk_older = getattr(slot, "_disk_older_count", 0) or 0
    if slot.messages and (disk_older + len(slot.messages)) <= len(disk_msgs):
        last_mem = slot.messages[-1]
        expected_pos = disk_older + len(slot.messages) - 1
        disk_at = disk_msgs[expected_pos]
        if last_mem.get("ts", "") != disk_at.get("ts", "") or last_mem.get("role") != disk_at.get(
            "role"
        ):
            logger.info(
                "reconcile: slot %s alignment mismatch at offset %d — skipping "
                "(possible transcript rotation)",
                slot.key,
                expected_pos,
            )
            return
    # Disk has rows the slot does not know about — append the tail.
    fresh = disk_msgs[represented:]
    logger.info(
        "reconcile: slot %s has %d messages in memory + %d older on disk = %d represented, "
        "but disk has %d; appending %d missing rows",
        slot.key,
        len(slot.messages),
        getattr(slot, "_disk_older_count", 0) or 0,
        represented,
        disk_total,
        len(fresh),
    )
    for msg in fresh:
        role = msg.get("role", "assistant")
        cls = msg.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = msg.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=msg.get("ts", ""),
            broadcast=False,
            meta=(
                _redact_meta_for_role(role, msg["meta"])
                if isinstance(msg.get("meta"), dict)
                else None
            ),
            mint_mid=False,
        )
        carry_provenance(slot.messages[-1], msg)
        remember_unknown_row_fields(slot, msg)
        _attach_variants(slot, msg)
    # The appended rows came from the file, so drain the replay frames and
    # mark the window as persisted (not dirty) — the next save must not
    # re-serialize them, and a fork/SSE drain must not treat them as new.
    slot.drain()
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False


async def _live_slot_resume_payload(state, existing) -> dict:
    """The resume endpoint's dedup body: the already-open slot's live window."""
    # The live slot a resume resolves to can be a restored sidebar row, under a
    # name the per-slot route's own lookup did not match (``dashboard:<key>``):
    # its window loads here, with the read off the loop, before anything reads it.
    await load_window_off_loop(existing)
    await _reconcile_slot_window(state, existing)
    window = _collapse_wire_rows(existing.messages)
    total = len(window)
    recent = window[-200:] if total > 200 else window
    next_before = (getattr(existing, "_disk_older_count", 0) or 0) + (total - len(recent))
    # The page slot detail serves: none of the rows the tab's display floor hides.
    from kiro_crew.dashboard.display_floor import clip_page_off_loop

    recent, next_before, floor_reached = await clip_page_off_loop(
        state.conversation_log, slot_history_key(existing), recent, next_before
    )
    prepared = _prepare_messages(
        recent,
        existing.running,
        live_child=_live_child_instance(state, existing),
        workspace=existing.workspace,
    )
    return {
        "ok": True,
        "key": existing.key,
        "history_key": slot_history_key(existing),
        "messages": prepared,
        "queue": queue_view(existing._queue),
        "total": total,
        "has_more": next_before > 0 and not floor_reached,
        "next_before": next_before,
        "memory_mode": existing.memory_mode,
        "mode": existing.mode,
        "surface": existing.mode,
        "folder_id": existing.folder_id,
    }
