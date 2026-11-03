"""
Voice rooms (Discord-style voice channels).

Audio / video / screen share go peer-to-peer over WebRTC (a full mesh, so a
room is capped at VOICE_MAX_PARTICIPANTS). This module is only the *signalling
server*: it keeps who-is-in-which-room in RAM, relays SDP / ICE between peers
and broadcasts mute / deafen / speaking state.  Nothing here touches the DB.

Besides the call sockets there are *watch* sockets (``VoiceHub.watch``): every open server page
subscribes to the voice roster of its server and gets a ``voice_state`` push on each change
(join / leave / mute / deafen / video), so the sidebar updates instantly.

State lives in this process' memory (exactly like ``realtime_channels``), so run
a single worker, or put sticky sessions in front of several.

Environment (all optional):
  VOICE_MAX_PARTICIPANTS   default 12
  VOICE_STUN_URLS          comma separated, default Google STUN
  VOICE_TURN_URLS          comma separated turn:/turns: urls (needed for users behind
                           strict NAT / mobile carriers - plain STUN is not enough there)
  VOICE_TURN_SECRET        coturn "use-auth-secret" (short-lived credentials), or
  VOICE_TURN_USERNAME / VOICE_TURN_CREDENTIAL   for a static TURN account
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
from typing import Awaitable, Callable

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

MAX_PARTICIPANTS = max(2, min(int(os.getenv("VOICE_MAX_PARTICIPANTS", "12") or 12), 40))
IDLE_TIMEOUT = 50.0          # client pings every 15 s
RECHECK_EVERY = 60.0         # re-validate membership / session this often
MAX_MESSAGE_CHARS = 64 * 1024
RATE_MAX_MESSAGES = 400      # per RATE_WINDOW seconds (ICE bursts are chatty)
RATE_WINDOW = 5.0
SIGNAL_KINDS = {"offer", "answer", "ice", "renegotiate"}
WATCH_DEBOUNCE = 0.12        # coalesce bursts of changes into one push
WATCH_IDLE_TIMEOUT = 75.0    # watch clients ping every 25 s
CAN_SEE_TTL = 60.0           # how long a per-channel access check is cached per watcher


def ice_servers(account_id: int) -> list[dict]:
    stun = [u.strip() for u in os.getenv(
        "VOICE_STUN_URLS", "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    ).split(",") if u.strip()]
    servers: list[dict] = [{"urls": stun}] if stun else []
    turn = [u.strip() for u in os.getenv("VOICE_TURN_URLS", "").split(",") if u.strip()]
    if turn:
        secret = os.getenv("VOICE_TURN_SECRET", "")
        if secret:
            username = f"{int(time.time()) + 6 * 3600}:{int(account_id)}"
            digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
            servers.append({"urls": turn, "username": username, "credential": base64.b64encode(digest).decode()})
        elif os.getenv("VOICE_TURN_USERNAME") and os.getenv("VOICE_TURN_CREDENTIAL"):
            servers.append({
                "urls": turn,
                "username": os.getenv("VOICE_TURN_USERNAME"),
                "credential": os.getenv("VOICE_TURN_CREDENTIAL"),
            })
    return servers


class Peer:
    __slots__ = (
        "ws", "account_id", "session_id", "profile", "self_mute", "self_deaf", "server_mute",
        "server_deaf", "video", "screen", "closed", "lock", "stamps",
    )

    def __init__(self, ws: WebSocket, account_id: int, profile: dict):
        self.ws = ws
        self.account_id = int(account_id)
        self.session_id = uuid.uuid4().hex[:16]
        self.profile = {
            "username": str(profile.get("username") or ""),
            "display_name": str(profile.get("display_name") or profile.get("username") or ""),
            "avatar_url": profile.get("avatar_url") or "",
        }
        self.self_mute = False
        self.self_deaf = False
        self.server_mute = False
        self.server_deaf = False
        self.video = False
        self.screen = False
        self.closed = False
        self.lock = asyncio.Lock()
        self.stamps: list[float] = []

    def public(self) -> dict:
        return {
            "account_id": self.account_id,
            "session_id": self.session_id,
            "profile": self.profile,
            "self_mute": self.self_mute,
            "self_deaf": self.self_deaf,
            "server_mute": self.server_mute,
            "server_deaf": self.server_deaf,
            "video": self.video,
            "screen": self.screen,
            "hand_raised": False,
            "suppressed": False,
            "can_speak": not (self.server_mute or self.server_deaf),
        }

    def rate_limited(self) -> bool:
        now = time.monotonic()
        self.stamps = [t for t in self.stamps if now - t < RATE_WINDOW]
        if len(self.stamps) >= RATE_MAX_MESSAGES:
            return True
        self.stamps.append(now)
        return False


class Watcher:
    """A server page that follows the voice roster of one server."""
    __slots__ = ("ws", "server_id", "account_id", "can_see", "cache", "closed", "lock")

    def __init__(self, ws: WebSocket, server_id: int, account_id: int, can_see: Callable[[int], Awaitable[bool]]):
        self.ws = ws
        self.server_id = int(server_id)
        self.account_id = int(account_id)
        self.can_see = can_see
        self.cache: dict[str, tuple[bool, float]] = {}
        self.closed = False
        self.lock = asyncio.Lock()


Key = tuple[int, int]


class VoiceHub:
    def __init__(self) -> None:
        self.rooms: dict[Key, dict[int, Peer]] = {}
        self.by_account: dict[int, tuple[Key, Peer]] = {}
        self.started: dict[Key, float] = {}   # key -> monotonic time the call began (first person joined)
        self.watchers: dict[int, set[Watcher]] = {}
        self._pending: set[int] = set()       # servers with a push already scheduled
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ io
    async def _send(self, peer: Peer, message: dict) -> None:
        if peer.closed:
            return
        try:
            async with peer.lock:
                if peer.ws.client_state == WebSocketState.CONNECTED:
                    await peer.ws.send_text(json.dumps(message, separators=(",", ":")))
        except Exception:
            peer.closed = True

    async def _close(self, peer: Peer, code: int = 1000) -> None:
        peer.closed = True
        try:
            if peer.ws.client_state == WebSocketState.CONNECTED:
                await peer.ws.close(code=code)
        except Exception:
            pass

    async def _broadcast(self, key: Key, message: dict, exclude: int | None = None) -> None:
        peers = [p for p in list(self.rooms.get(key, {}).values()) if p.account_id != exclude]
        if peers:
            await asyncio.gather(*(self._send(p, message) for p in peers), return_exceptions=True)

    # ------------------------------------------------------------- bookkeeping
    async def _remove(self, key: Key, peer: Peer) -> None:
        peer.closed = True
        room = self.rooms.get(key)
        removed = False
        if room and room.get(peer.account_id) is peer:
            del room[peer.account_id]
            removed = True
            if not room:
                self.rooms.pop(key, None)
                self.started.pop(key, None)   # empty room -> the next call starts a fresh timer
        cur = self.by_account.get(peer.account_id)
        if cur and cur[1] is peer:
            del self.by_account[peer.account_id]
        if removed:
            await self._broadcast(key, {"type": "participant_left", "account_id": peer.account_id})
            self._changed(key[0])

    async def _evict(self, key: Key, peer: Peer, message: dict) -> None:
        await self._send(peer, message)
        await self._remove(key, peer)
        await self._close(peer)

    # ------------------------------------------------------------------ public
    def snapshot(self, server_id: int) -> dict[str, list[dict]]:
        """channel_id -> participants, for the server sidebar."""
        out: dict[str, list[dict]] = {}
        now = time.monotonic()
        for (sid, cid), room in self.rooms.items():
            if sid != int(server_id) or not room:
                continue
            # seconds since the call began; the sidebar turns it into a running timer
            age = int(max(0.0, now - self.started.get((sid, cid), now)))
            out[str(cid)] = [
                {
                    "account_id": p.account_id,
                    "username": p.profile["username"],
                    "display_name": p.profile["display_name"],
                    "avatar_url": p.profile["avatar_url"],
                    "self_mute": p.self_mute,
                    "self_deaf": p.self_deaf,
                    "server_mute": p.server_mute,
                    "server_deaf": p.server_deaf,
                    "video": p.video,
                    "screen": p.screen,
                    "channel_age": age,
                }
                for p in room.values()
            ]
        return out

    # ------------------------------------------------------------- live sidebar
    def _changed(self, server_id: int) -> None:
        """Something in a voice room of this server changed -> push the roster to its watchers (debounced)."""
        sid = int(server_id)
        if not self.watchers.get(sid) or sid in self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending.add(sid)

        def fire() -> None:
            task = asyncio.ensure_future(self._publish(sid))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        loop.call_later(WATCH_DEBOUNCE, fire)

    async def _publish(self, server_id: int) -> None:
        self._pending.discard(server_id)
        watchers = list(self.watchers.get(server_id, ()))
        if watchers:
            snap = self.snapshot(server_id)
            await asyncio.gather(*(self._push(w, snap) for w in watchers), return_exceptions=True)

    async def _push(self, w: Watcher, snap: dict[str, list[dict]]) -> None:
        """Send one watcher only the channels it is allowed to see (access check cached per channel)."""
        now = time.monotonic()
        visible: dict[str, list[dict]] = {}
        for cid, people in snap.items():
            hit = w.cache.get(cid)
            if hit is None or now - hit[1] > CAN_SEE_TTL:
                try:
                    ok = bool(await w.can_see(int(cid)))
                except Exception:
                    ok = False
                w.cache[cid] = (ok, now)
            else:
                ok = hit[0]
            if ok:
                visible[cid] = people
        if w.closed:
            return
        try:
            async with w.lock:
                if w.ws.client_state == WebSocketState.CONNECTED:
                    await w.ws.send_text(json.dumps({"type": "voice_state", "channels": visible}, separators=(",", ":")))
        except Exception:
            w.closed = True

    async def watch(
        self,
        websocket: WebSocket,
        *,
        server_id: int,
        account_id: int,
        can_see: Callable[[int], Awaitable[bool]],
    ) -> None:
        """Subscribe a server page to live voice-roster updates."""
        await websocket.accept()
        w = Watcher(websocket, server_id, account_id, can_see)
        self.watchers.setdefault(w.server_id, set()).add(w)
        try:
            await self._push(w, self.snapshot(w.server_id))      # current state right away
            while not w.closed:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), WATCH_IDLE_TIMEOUT)
                except (asyncio.TimeoutError, WebSocketDisconnect):
                    break
                if raw and len(raw) < 64 and "ping" in raw:
                    try:
                        async with w.lock:
                            await websocket.send_text('{"type":"pong"}')
                    except Exception:
                        break
        except Exception:
            pass
        finally:
            w.closed = True
            group = self.watchers.get(w.server_id)
            if group:
                group.discard(w)
                if not group:
                    self.watchers.pop(w.server_id, None)
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.close()
            except Exception:
                pass

    async def close_channel(self, server_id: int, channel_id: int, reason: str = "channel_deleted") -> None:
        key = (int(server_id), int(channel_id))
        for peer in list(self.rooms.get(key, {}).values()):
            await self._evict(key, peer, {"type": "voice_error", "error": reason})

    async def disconnect_account(self, account_id: int, reason: str, server_id: int | None = None) -> None:
        cur = self.by_account.get(int(account_id))
        if cur and (server_id is None or cur[0][0] == int(server_id)):
            await self._evict(cur[0], cur[1], {"type": "voice_error", "error": reason})

    async def serve(
        self,
        websocket: WebSocket,
        *,
        server_id: int,
        channel_id: int,
        account_id: int,
        profile: dict,
        channel: dict,
        can_moderate: bool,
        may_moderate_target: Callable[[int], Awaitable[bool]],
        still_allowed: Callable[[], Awaitable[bool]],
    ) -> None:
        await websocket.accept()
        key: Key = (int(server_id), int(channel_id))

        # one voice connection per account: a new join replaces the old one
        old = self.by_account.get(int(account_id))
        if old:
            await self._evict(old[0], old[1], {"type": "voice_replaced", "reason": "voice_replaced"})

        room = self.rooms.setdefault(key, {})
        if len(room) >= MAX_PARTICIPANTS:
            if not room:
                self.rooms.pop(key, None)
            tmp = Peer(websocket, account_id, profile)
            await self._send(tmp, {"type": "voice_error", "error": "room_full"})
            await self._close(tmp)
            return

        me = Peer(websocket, account_id, profile)
        room[me.account_id] = me
        self.started.setdefault(key, time.monotonic())
        self.by_account[me.account_id] = (key, me)
        await self._send(me, {
            "type": "voice_ready",
            "self": {"account_id": me.account_id, "session_id": me.session_id},
            "ice_servers": ice_servers(me.account_id),
            "channel": {"id": int(channel_id), "name": channel.get("name") or "", "type": channel.get("type") or "voice"},
            "can_moderate": bool(can_moderate),
            "participants": [p.public() for p in room.values()],
        })
        await self._broadcast(key, {"type": "participant_joined", "participant": me.public()}, exclude=me.account_id)
        self._changed(key[0])

        last_check = time.monotonic()
        try:
            while not me.closed:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._send(me, {"type": "voice_error", "error": "timeout"})
                    break
                except WebSocketDisconnect:
                    break
                if me.closed or self.rooms.get(key, {}).get(me.account_id) is not me:
                    break  # evicted meanwhile
                if len(raw) > MAX_MESSAGE_CHARS:
                    continue
                if me.rate_limited():
                    await self._send(me, {"type": "rate_limited"})
                    continue
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = str(data.get("type") or "")

                now = time.monotonic()
                if now - last_check > RECHECK_EVERY:
                    last_check = now
                    if not await still_allowed():
                        await self._send(me, {"type": "voice_error", "error": "access_lost"})
                        break

                if kind == "ping":
                    continue
                if kind == "leave":
                    break
                if kind == "signal":
                    await self._on_signal(key, me, data)
                elif kind == "state":
                    await self._on_state(key, me, data)
                elif kind == "speaking":
                    await self._broadcast(
                        key, {"type": "speaking", "account_id": me.account_id, "value": bool(data.get("value"))},
                        exclude=me.account_id,
                    )
                elif kind == "mod":
                    await self._on_mod(key, me, data, can_moderate, may_moderate_target)
        except Exception:
            pass
        finally:
            await self._remove(key, me)
            await self._close(me)

    # ---------------------------------------------------------------- handlers
    async def _on_signal(self, key: Key, me: Peer, data: dict) -> None:
        try:
            to = int(data.get("to"))
        except (TypeError, ValueError):
            return
        sig_kind = str(data.get("kind") or "")
        payload = data.get("data")
        if sig_kind not in SIGNAL_KINDS or not isinstance(payload, dict) or to == me.account_id:
            return
        target = self.rooms.get(key, {}).get(to)
        if not target or target.session_id != str(data.get("to_session") or ""):
            return
        await self._send(target, {
            "type": "signal", "from": me.account_id, "from_session": me.session_id,
            "kind": sig_kind, "data": payload,
        })

    async def _on_state(self, key: Key, me: Peer, data: dict) -> None:
        me.self_mute = bool(data.get("self_mute"))
        me.self_deaf = bool(data.get("self_deaf"))
        me.video = bool(data.get("video"))
        me.screen = bool(data.get("screen"))
        await self._broadcast(key, {"type": "participant_updated", "participant": me.public()}, exclude=me.account_id)
        self._changed(key[0])

    async def _on_mod(self, key: Key, me: Peer, data: dict, can_moderate: bool, may_target) -> None:
        async def reply(ok: bool, error: str = "") -> None:
            await self._send(me, {"type": "mod_result", "ok": ok, "error": error})

        if not can_moderate:
            return await reply(False, "forbidden")
        try:
            target_id = int(data.get("target"))
        except (TypeError, ValueError):
            return await reply(False, "bad_target")
        action = str(data.get("action") or "")
        target = self.rooms.get(key, {}).get(target_id)
        if not target or target_id == me.account_id:
            return await reply(False, "not_in_room")
        if not await may_target(target_id):
            return await reply(False, "forbidden_target")
        if action == "server_mute":
            target.server_mute = bool(data.get("value"))
        elif action == "server_deaf":
            target.server_deaf = bool(data.get("value"))
        elif action == "disconnect":
            await self._evict(key, target, {"type": "voice_kicked", "reason": "moderator"})
            return await reply(True)
        else:
            return await reply(False, "bad_action")
        await self._broadcast(key, {"type": "participant_updated", "participant": target.public()})
        self._changed(key[0])
        await reply(True)


hub = VoiceHub()
