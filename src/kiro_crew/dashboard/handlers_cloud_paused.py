"""Record a cloud crew's Stop / Start on its Instances row.

``POST /api/cloud/{tag}/stop`` parks the EC2 instance, but the registry row that
reaches it knew nothing of that: auto-connect and the startup revive kept dialling
a machine that was off, and the panel showed the SSM ``TargetNotConnected`` failure
as a red Error. This module sets the row's ``cloud_stopped`` flag after a Stop and
clears it after a Start, so the connect path refuses a stopped crew and the panel
can show it as Stopped with Start as the way back.

Kept out of ``handlers_cloud`` because that module is past the size cap.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Iterable

from kiro_crew.dashboard.handlers_instances_gate import _apply_update, _registry

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.handlers_cloud_paused")


def _row_for_target(reg, instance_id: str):
    """The registry row reaching EC2 instance *instance_id*, or ``None``. Blocking."""
    for inst in reg.list():
        if inst.ssm_target == instance_id:
            return inst
    return None


async def mark_cloud_stopped(
    state: "DashboardState", jobs: Iterable, tag: str, stopped: bool
) -> None:
    """Set (or clear) ``cloud_stopped`` on the row of the crew launched as *tag*.

    The instance id comes from the launch job, the same server-owned correlation the
    panel uses to call a row a cloud crew. A stop also tears the tunnel down under
    the manager lock, so a connect racing it is either torn down or refused.

    Never raises: the AWS call already succeeded, so a row that cannot be marked is
    logged and the request still reports the stop or start it made.
    """
    iid = next((j.instance_id for j in jobs if j.tag == tag and j.instance_id), "")
    if not iid:
        return
    reg = _registry(state)
    try:
        row = await asyncio.to_thread(_row_for_target, reg, iid)
        if row is None or row.cloud_stopped == stopped:
            return
        apply = functools.partial(_apply_update, reg, row.id, {"cloud_stopped": stopped})
        mgr = getattr(state, "instances_manager", None)
        if stopped and mgr is not None:
            await mgr.reconfigure(row.id, apply)
        else:
            await asyncio.to_thread(apply)
    except Exception as exc:  # noqa: BLE001 - reported, never fails the AWS result
        logger.warning(
            "could not record cloud %s on crew %s: %s", "stop" if stopped else "start", tag, exc
        )
