"""
Discord-style voice rooms ("voice" and "stage" server channels).

Media topology: WebRTC full mesh -- every participant streams directly to
every other participant. The backend never touches audio/video; it only:

  * tracks who is in which room and their voice state
    (self mute / deafen / camera / screen share, server mute / deafen, stage);
  * relays WebRTC signaling (SDP offer/answer + ICE candidates) strictly
    between two participants of the *same* room;
  * pushes "who is in voice" changes to every server member that can see the
    channel, so the sidebar can render the participant list like Discord;
  * applies moderation: server mute / deafen, disconnect, move, stage speakers.

State lives in process RAM, exactly like RealtimeChannelManager in router.py.
Run a single worker (or move this state to Redis pub/sub before scaling out).

A mesh is comfortable up to ~8-10 people (each client uploads N-1 streams).
For bigger rooms put an SFU (LiveKit / mediasoup) behind the same API: this
module then stays responsible for presence, permissions and moderation.

IMPORTANT about enforcement in a mesh: the server cannot physically stop a
modified client from sending audio. Server mute / stage "audience" are
therefore enforced on the RECEIVING side: every client must not play tracks
from a participant whose ``can_speak`` is false. Each listener protects
itself, so a single malicious sender still cannot be heard by honest clients.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

VOICE_CHANNEL_TYPES = {"voice", "stage"}

# Hard technical cap for a P2P mesh. Per-channel user_limit can only be lower.
HARD_CAP = max(2, min(int(os.getenv("VOICE_ROOM_HARD_CAP", "10") or 10), 25))

# A video+screen SDP with several codecs is ~10-25 KB. Anything far bigger is abuse.
MAX_SIGNAL_BYTES = 64_000
MAX_WS_TEXT_BYTES = MAX_SIGNAL_BYTES + 4_096

SIGNAL_KINDS = {"offer", "answer", "ice", "renegotiate", "restart_ice"}
SELF_STATE_FIELDS = ("self_mute", "self_deaf", "video", "screen")

# How long a socket may stay silent before we consider the peer dead.
# Clients should send {"type": "ping"} every ~15 seconds.
IDLE_TIMEOUT_SECONDS = 60.0
# How often permissions (ban / kick / revoked session / channel deleted /
# channel made private) are re-validated for a connected participant.
RECHECK_SECONDS = 30.0

# WebSocket close codes (4000-4999 are application defined).
CLOSE_REPLACED = 4001      # same account joined voice from another tab/device
CLOSE_KICKED = 4002        # moderator disconnected the user
CLOSE_MOVED = 4003         # moderator moved the user to another channel
CLOSE_FORBIDDEN = 4004     # lost access (kicked from server, channel deleted, ...)
CLOSE_FULL = 4005          # room is full
CLOSE_IDLE = 4006          # no heartbeat


def _now() -> float:
    return time.monotonic()


class TokenBucket:
    """Tiny per-connection rate limiter (no locking needed: one socket = one task)."""

    __slots__ = ("capacity", "refill_per_sec", "tokens", "updated")

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self.updated = _now()

    def allow(self, cost: float = 1.0) -> bool:
        now = _now()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


def connection_limiters() -> dict[str, TokenBucket]:
    return {
        # ICE bursts on join: ~10-20 candidates per peer * up to 9 peers.
        "signal": TokenBucket(400, 60),
        "state": TokenBucket(20, 2),
        "speaking": TokenBucket(30, 10),
        "mod": TokenBucket(10, 1),
        "other": TokenBucket(30, 5),
    }


def public_profile(account) -> dict:
    """Minimal public card for the voice UI. Deliberately does NOT include
    email, language, raw account_status ("invisible" must not leak), etc."""
    return {
        "id": int(account.id),
        "username": account.username,
        "display_name": getattr(account, "display_name", None) or account.username,
        "avatar_url": getattr(account, "avatar_url", None),
        "name_effect": getattr(account, "name_effect", None) or "none",
        "name_font": getattr(account, "name_font", None) or "default",
        "role_color_start": getattr(account, "role_color_start", None),
        "role_color_end": getattr(account, "role_color_end", None),
        "is_verified": bool(getattr(account, "is_verified", False)),
    }


@dataclass
class VoiceParticipant:
    account_id: int
    server_id: int
    channel_id: int
    session_id: str
    websocket: Any
    profile: dict
    is_stage: bool = False
    is_manager: bool = False
    joined_at: float = field(default_factory=time.time)
    self_mute: bool = False
    self_deaf: bool = False
    video: bool = False
    screen: bool = False
    server_mute: bool = False
    server_deaf: bool = False
    # Stage channels: audience members are "suppressed" until a moderator
    # invites them to speak.
    suppressed: bool = False
    hand_raised: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return (self.server_id, self.channel_id)

    @property
    def can_speak(self) -> bool:
        return not (self.server_mute or self.suppressed)

    def public(self) -> dict:
        return {
            "account_id": self.account_id,
            "session_id": self.session_id,
            "profile": self.profile,
            "joined_at": int(self.joined_at * 1000),
            "self_mute": self.self_mute,
            "self_deaf": self.self_deaf,
            "video": self.video,
            "screen": self.screen,
            "server_mute": self.server_mute,
            "server_deaf": self.server_deaf,
            "suppressed": self.suppressed,
            "hand_raised": self.hand_raised,
            # Receivers MUST NOT play this participant's audio when false.
            "can_speak": self.can_speak,
        }


Notifier = Callable[[int, int, dict], Awaitable[None]]


class VoiceRoomManager:
    def __init__(self) -> None:
        self.rooms: dict[tuple[int, int], dict[int, VoiceParticipant]] = {}
        # One active voice session per account across the whole app (Discord rule).
        self.by_account: dict[int, VoiceParticipant] = {}
        self.lock = asyncio.Lock()
        self._notifier: Notifier | None = None

    # ------------------------------------------------------------------ setup
    def set_notifier(self, notifier: Notifier) -> None:
        """notifier(server_id, channel_id, payload) fans a server-wide event out
        to every member who may see the channel (wired up in router.py)."""
        self._notifier = notifier

    async def _notify(self, server_id: int, channel_id: int, payload: dict) -> None:
        if not self._notifier:
            return
        try:
            await self._notifier(int(server_id), int(channel_id), payload)
        except Exception as exc:  # never let presence fan-out kill a voice socket
            print("Voice notify failed:", repr(exc))

    # ------------------------------------------------------------ send helpers
    @staticmethod
    async def _safe_send(websocket, payload: dict) -> bool:
        try:
            await websocket.send_json(payload)
            return True
        except Exception:
            return False

    @staticmethod
    async def _safe_close(websocket, code: int, payload: dict | None = None) -> None:
        if payload:
            try:
                await websocket.send_json(payload)
            except Exception:
                pass
        try:
            await websocket.close(code=code)
        except Exception:
            pass

    async def broadcast_room(self, key: tuple[int, int], payload: dict, exclude: int | None = None) -> None:
        async with self.lock:
            targets = [
                p.websocket for uid, p in self.rooms.get(key, {}).items()
                if exclude is None or uid != exclude
            ]
        await asyncio.gather(*(self._safe_send(ws, payload) for ws in targets))

    # ------------------------------------------------------------ membership
    async def join(
        self,
        server_id: int,
        channel_id: int,
        account_id: int,
        websocket,
        profile: dict,
        *,
        is_stage: bool,
        is_manager: bool,
        user_limit: int = 0,
    ) -> tuple[VoiceParticipant | None, str | None]:
        key = (int(server_id), int(channel_id))
        account_id = int(account_id)
        replaced: VoiceParticipant | None = None
        async with self.lock:
            room = self.rooms.get(key, {})
            previous = self.by_account.get(account_id)
            occupied = len(room) - (1 if previous is not None and previous.key == key else 0)
            limit = HARD_CAP
            if user_limit and user_limit > 0 and not is_manager:
                # Managers may enter a "full" room (Discord: Move Members), but
                # never beyond the technical mesh cap.
                limit = min(HARD_CAP, int(user_limit))
            if occupied >= limit:
                return None, "room_full"

            if previous is not None:
                replaced = previous
                old_room = self.rooms.get(previous.key, {})
                if old_room.get(account_id) is previous:
                    old_room.pop(account_id, None)
                if not old_room:
                    self.rooms.pop(previous.key, None)

            participant = VoiceParticipant(
                account_id=account_id,
                server_id=key[0],
                channel_id=key[1],
                session_id=uuid.uuid4().hex,
                websocket=websocket,
                profile=dict(profile),
                is_stage=bool(is_stage),
                is_manager=bool(is_manager),
                # Stage: everybody except moderators joins the audience.
                suppressed=bool(is_stage and not is_manager),
                # Carry mute/deafen over when hopping between rooms.
                self_mute=bool(replaced.self_mute) if replaced else False,
                self_deaf=bool(replaced.self_deaf) if replaced else False,
            )
            self.rooms.setdefault(key, {})[account_id] = participant
            self.by_account[account_id] = participant

        if replaced is not None:
            await self._safe_close(
                replaced.websocket,
                CLOSE_REPLACED,
                {"type": "voice_replaced", "server_id": key[0], "channel_id": key[1]},
            )
            await self._announce_leave(replaced, reason="replaced")

        await self.broadcast_room(
            key, {"type": "participant_joined", "participant": participant.public()}, exclude=account_id
        )
        await self._notify(key[0], key[1], {
            "type": "voice_state",
            "action": "join",
            "server_id": key[0],
            "channel_id": key[1],
            "participant": participant.public(),
        })
        return participant, None

    async def _announce_leave(self, participant: VoiceParticipant, reason: str) -> None:
        payload = {
            "type": "participant_left",
            "account_id": participant.account_id,
            "session_id": participant.session_id,
            "reason": reason,
        }
        await self.broadcast_room(participant.key, payload, exclude=participant.account_id)
        await self._notify(participant.server_id, participant.channel_id, {
            "type": "voice_state",
            "action": "leave",
            "server_id": participant.server_id,
            "channel_id": participant.channel_id,
            "account_id": participant.account_id,
            "session_id": participant.session_id,
            "reason": reason,
        })

    async def leave(self, participant: VoiceParticipant, reason: str = "left") -> bool:
        """Remove exactly this session. A stale socket's ``finally`` block must
        never remove the newer session that replaced it."""
        async with self.lock:
            room = self.rooms.get(participant.key, {})
            if room.get(participant.account_id) is not participant:
                return False
            room.pop(participant.account_id, None)
            if not room:
                self.rooms.pop(participant.key, None)
            if self.by_account.get(participant.account_id) is participant:
                self.by_account.pop(participant.account_id, None)
        await self._announce_leave(participant, reason)
        return True

    async def is_current(self, participant: VoiceParticipant) -> bool:
        async with self.lock:
            return self.by_account.get(participant.account_id) is participant

    async def _publish_update(self, participant: VoiceParticipant) -> None:
        public = participant.public()
        await self.broadcast_room(participant.key, {"type": "participant_updated", "participant": public})
        await self._notify(participant.server_id, participant.channel_id, {
            "type": "voice_state",
            "action": "update",
            "server_id": participant.server_id,
            "channel_id": participant.channel_id,
            "participant": public,
        })

    async def update_self_state(self, participant: VoiceParticipant, data: dict) -> bool:
        changed = False
        async with self.lock:
            if self.by_account.get(participant.account_id) is not participant:
                return False
            for name in SELF_STATE_FIELDS:
                if name in data and isinstance(data[name], bool) and getattr(participant, name) != data[name]:
                    setattr(participant, name, data[name])
                    changed = True
            # Discord semantics: deafen implies mute.
            if participant.self_deaf and not participant.self_mute:
                participant.self_mute = True
                changed = True
            # Stage audience cannot broadcast camera / screen.
            if participant.suppressed and (participant.video or participant.screen):
                participant.video = participant.screen = False
                changed = True
        if changed:
            await self._publish_update(participant)
        return changed

    async def set_hand(self, participant: VoiceParticipant, raised: bool) -> None:
        async with self.lock:
            if not participant.is_stage or self.by_account.get(participant.account_id) is not participant:
                return
            if not participant.suppressed:
                raised = False
            if participant.hand_raised == raised:
                return
            participant.hand_raised = raised
        await self._publish_update(participant)

    async def leave_stage_speakers(self, participant: VoiceParticipant) -> None:
        async with self.lock:
            if not participant.is_stage or participant.suppressed:
                return
            participant.suppressed = True
            participant.hand_raised = False
            participant.video = participant.screen = False
        await self._publish_update(participant)

    async def speaking(self, participant: VoiceParticipant, value: bool) -> None:
        # Room-only and RAM-only: speaking indicators never hit the DB or the
        # server-wide fan-out. Ignored for participants who may not speak.
        if value and not participant.can_speak:
            return
        await self.broadcast_room(
            participant.key,
            {"type": "speaking", "account_id": participant.account_id, "value": bool(value)},
            exclude=participant.account_id,
        )

    # ---------------------------------------------------------------- signaling
    async def relay_signal(self, sender: VoiceParticipant, data: dict) -> str | None:
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in SIGNAL_KINDS:
            return "bad_kind"
        try:
            target_id = int(data.get("to") or 0)
        except (TypeError, ValueError):
            return "bad_target"
        if not target_id or target_id == sender.account_id:
            return "bad_target"
        body = data.get("data")
        if body is not None and not isinstance(body, dict):
            return "bad_payload"
        try:
            encoded_size = len(json.dumps(body, separators=(",", ":")))
        except (TypeError, ValueError):
            return "bad_payload"
        if encoded_size > MAX_SIGNAL_BYTES:
            return "payload_too_large"
        to_session = str(data.get("to_session") or "")
        async with self.lock:
            if self.by_account.get(sender.account_id) is not sender:
                return "not_in_room"
            target = self.rooms.get(sender.key, {}).get(target_id)
            if target is None or (to_session and target.session_id != to_session):
                return "target_not_in_room"
            websocket = target.websocket
        delivered = await self._safe_send(websocket, {
            "type": "signal",
            "kind": kind,
            "from": sender.account_id,
            "from_session": sender.session_id,
            "data": body,
        })
        return None if delivered else "delivery_failed"

    # --------------------------------------------------------------- snapshots
    async def room_snapshot(self, server_id: int, channel_id: int) -> list[dict]:
        async with self.lock:
            room = self.rooms.get((int(server_id), int(channel_id)), {})
            return [p.public() for p in sorted(room.values(), key=lambda p: p.joined_at)]

    async def server_snapshot(self, server_id: int) -> dict[int, list[dict]]:
        async with self.lock:
            result: dict[int, list[dict]] = {}
            for (sid, cid), room in self.rooms.items():
                if sid == int(server_id) and room:
                    result[cid] = [p.public() for p in sorted(room.values(), key=lambda p: p.joined_at)]
            return result

    async def get_participant(self, account_id: int) -> VoiceParticipant | None:
        async with self.lock:
            return self.by_account.get(int(account_id))

    async def count(self, server_id: int, channel_id: int) -> int:
        async with self.lock:
            return len(self.rooms.get((int(server_id), int(channel_id)), {}))

    # --------------------------------------------------------------- moderation
    async def moderate_flag(self, target: VoiceParticipant, field_name: str, value: bool) -> bool:
        if field_name not in {"server_mute", "server_deaf"}:
            return False
        async with self.lock:
            if self.by_account.get(target.account_id) is not target:
                return False
            if getattr(target, field_name) == bool(value):
                return True
            setattr(target, field_name, bool(value))
        await self._publish_update(target)
        return True

    async def set_stage_speaker(self, target: VoiceParticipant, speaker: bool) -> bool:
        async with self.lock:
            if not target.is_stage or self.by_account.get(target.account_id) is not target:
                return False
            target.suppressed = not speaker
            target.hand_raised = False
            if not speaker:
                target.video = target.screen = False
        await self._publish_update(target)
        return True

    async def kick(self, target: VoiceParticipant, code: int = CLOSE_KICKED, payload: dict | None = None,
                   reason: str = "kicked") -> bool:
        removed = await self.leave(target, reason=reason)
        await self._safe_close(target.websocket, code, payload or {"type": "voice_kicked", "reason": reason})
        return removed

    async def disconnect_account(self, account_id: int, server_id: int | None = None,
                                 reason: str = "forbidden") -> None:
        participant = await self.get_participant(account_id)
        if participant is None:
            return
        if server_id is not None and participant.server_id != int(server_id):
            return
        await self.kick(participant, CLOSE_FORBIDDEN, {"type": "voice_kicked", "reason": reason}, reason)

    async def disconnect_channel(self, server_id: int, channel_id: int, reason: str = "channel_removed") -> None:
        async with self.lock:
            targets = list(self.rooms.get((int(server_id), int(channel_id)), {}).values())
        for participant in targets:
            await self.kick(participant, CLOSE_FORBIDDEN, {"type": "voice_kicked", "reason": reason}, reason)

    async def disconnect_server(self, server_id: int, reason: str = "server_removed") -> None:
        async with self.lock:
            targets = [p for (sid, _), room in self.rooms.items() if sid == int(server_id) for p in room.values()]
        for participant in targets:
            await self.kick(participant, CLOSE_FORBIDDEN, {"type": "voice_kicked", "reason": reason}, reason)


# ---------------------------------------------------------------------------
# ICE servers (STUN/TURN)
# ---------------------------------------------------------------------------
# Without TURN ~10-20% of users (symmetric NAT, mobile carriers, corporate
# networks) cannot connect at all. Configure one of:
#   VOICE_TURN_URLS="turn:turn.example.com:3478?transport=udp,turns:turn.example.com:5349"
#   + VOICE_TURN_SECRET=...             (coturn "use-auth-secret" / REST API, recommended:
#                                        short-lived per-user credentials)
#   or VOICE_TURN_USERNAME / VOICE_TURN_PASSWORD (static credentials, e.g. Metered/Twilio)
#   VOICE_STUN_URLS overrides the default public STUN server.
TURN_CREDENTIAL_TTL_SECONDS = 6 * 3600


def _split_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def ice_servers_for(account_id: int) -> list[dict]:
    servers: list[dict] = []
    stun_urls = _split_env("VOICE_STUN_URLS") or ["stun:stun.l.google.com:19302"]
    servers.append({"urls": stun_urls})

    turn_urls = _split_env("VOICE_TURN_URLS")
    if not turn_urls:
        return servers
    secret = os.getenv("VOICE_TURN_SECRET", "").strip()
    if secret:
        expires = int(time.time()) + TURN_CREDENTIAL_TTL_SECONDS
        username = f"{expires}:{int(account_id)}"
        digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
        servers.append({
            "urls": turn_urls,
            "username": username,
            "credential": base64.b64encode(digest).decode("ascii"),
            "ttl": TURN_CREDENTIAL_TTL_SECONDS,
        })
        return servers
    username = os.getenv("VOICE_TURN_USERNAME", "").strip()
    password = os.getenv("VOICE_TURN_PASSWORD", "").strip()
    if username and password:
        servers.append({"urls": turn_urls, "username": username, "credential": password})
    return servers


voice_rooms = VoiceRoomManager()
