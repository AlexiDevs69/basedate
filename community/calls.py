"""
1:1 WebRTC call signaling for direct-message threads.

This module owns everything about calls: relaying offer/answer/ICE
candidates between the two participants of a DM thread, a tiny in-memory
state machine (`active_calls`) so stale/duplicate offers are rejected
instead of silently corrupting state, and ringing the callee's account-level
socket so an incoming call shows up even on pages where the DM thread isn't
open.

Deliberately NOT persisted to Postgres: a ringing/active call is not chat
history, it's transient signaling state. If you later want a call log
(missed calls, duration), that's a separate `crud_calls.py` + small table
that reads `CallState` when a call ends -- this module doesn't need to know
about that.

Design note: this module takes `realtime_channels` / `account_realtime` as
arguments rather than importing them from `router.py`. Those singletons are
currently defined inline in router.py, and importing them here at module
level would create a circular import (router.py -> calls.py -> router.py).
Passing them in keeps calls.py import-cycle-free and easy to test in
isolation. If you ever move RealtimeChannelManager/AccountRealtimeManager
into their own `realtime.py`, this module can import from there directly
and router.py's call site gets simpler -- not required for this to work today.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

CALL_RING_TIMEOUT_SECONDS = 45.0  # ringing longer than this without an answer is treated as stale


@dataclass
class CallState:
    thread_id: int
    caller_id: int
    callee_id: int
    status: str = "ringing"  # ringing -> active -> (removed on end/reject/timeout)
    started_at: float = field(default_factory=time.monotonic)
    answered_at: float | None = None


# One call per DM thread at a time. RAM-only, same reasoning as the existing
# typing-indicator state in RealtimeChannelManager: it's not chat content,
# it doesn't need to survive a restart, and persisting it to Postgres would
# just be a write for every offer/answer/hangup for no benefit.
active_calls: dict[int, CallState] = {}


def _other_participant(thread, account_id: int) -> int:
    """Same low/high id convention already used for DM threads elsewhere."""
    account_id = int(account_id)
    return int(thread.user_high_id) if int(thread.user_low_id) == account_id else int(thread.user_low_id)


def _drop_if_stale(thread_id: int) -> None:
    state = active_calls.get(thread_id)
    if (
        state
        and state.status == "ringing"
        and time.monotonic() - state.started_at > CALL_RING_TIMEOUT_SECONDS
    ):
        active_calls.pop(thread_id, None)


async def handle_message(
    data: dict,
    *,
    account_id: int,
    thread,
    key: tuple[int, int],
    realtime_channels,
    account_realtime,
    caller_profile: dict,
) -> None:
    """Dispatch one call_* event. Call this from the DM WS loop in router.py
    and `continue` afterwards -- it never needs to fall through to the rest
    of the loop.

    `key` is the same (0, thread_id) realtime_channels key router.py already
    uses for chat messages/typing, so signaling rides the exact same fan-out
    path with no new connection registry. Every relayed payload carries
    `from_account_id` so the client can ignore its own echo, the same way it
    already has to for other broadcast events on this channel.
    """
    event_type = str(data.get("type") or "")
    account_id = int(account_id)
    thread_id = int(thread.id)
    other_id = _other_participant(thread, account_id)
    _drop_if_stale(thread_id)

    if event_type == "call_offer":
        existing = active_calls.get(thread_id)
        if existing and existing.status in {"ringing", "active"}:
            # Don't clobber a call in progress -- tell the caller it's busy.
            await realtime_channels.broadcast(
                key,
                {"type": "call_busy", "thread_id": thread_id, "from_account_id": account_id},
            )
            return
        active_calls[thread_id] = CallState(thread_id=thread_id, caller_id=account_id, callee_id=other_id)
        await realtime_channels.broadcast(
            key,
            {
                "type": "call_offer",
                "thread_id": thread_id,
                "from_account_id": account_id,
                "sdp": data.get("sdp"),
            },
        )
        await notify_incoming_call(account_realtime, other_id, thread_id, caller_profile)
        return

    if event_type == "call_answer":
        state = active_calls.get(thread_id)
        if not state or state.caller_id != other_id or state.callee_id != account_id:
            return  # no matching ringing call, or answer from the wrong side
        state.status = "active"
        state.answered_at = time.monotonic()
        await realtime_channels.broadcast(
            key,
            {
                "type": "call_answer",
                "thread_id": thread_id,
                "from_account_id": account_id,
                "sdp": data.get("sdp"),
            },
        )
        return

    if event_type == "call_ice_candidate":
        state = active_calls.get(thread_id)
        if not state or account_id not in (state.caller_id, state.callee_id):
            return  # ignore candidates for a call that isn't live
        await realtime_channels.broadcast(
            key,
            {
                "type": "call_ice_candidate",
                "thread_id": thread_id,
                "from_account_id": account_id,
                "candidate": data.get("candidate"),
            },
        )
        return

    if event_type == "call_reject":
        active_calls.pop(thread_id, None)
        await realtime_channels.broadcast(
            key,
            {"type": "call_reject", "thread_id": thread_id, "from_account_id": account_id},
        )
        return

    if event_type == "call_end":
        active_calls.pop(thread_id, None)
        await realtime_channels.broadcast(
            key,
            {"type": "call_end", "thread_id": thread_id, "from_account_id": account_id},
        )
        return


async def notify_incoming_call(account_realtime, callee_id: int, thread_id: int, caller_profile: dict) -> None:
    """Rings the callee's account-level socket (account_realtime), so an
    incoming call surfaces even on pages where this DM thread isn't open --
    same idea as how presence/mention updates already reach every open page.
    """
    await account_realtime.send_to_account(
        int(callee_id),
        {"type": "incoming_call", "thread_id": int(thread_id), "caller": caller_profile},
    )


def handle_disconnect(account_id: int) -> None:
    """Call from the DM WS `finally` block. Without this, a dropped
    connection (tab closed mid-ring, network drop) leaves a phantom
    ringing/active CallState behind that the other participant can never
    clear, since call_end/call_reject would come from the socket that just
    died.
    """
    account_id = int(account_id)
    for thread_id, state in list(active_calls.items()):
        if account_id in (state.caller_id, state.callee_id):
            active_calls.pop(thread_id, None)
