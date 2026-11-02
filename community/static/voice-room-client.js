/*
 * AlexiHub voice room client (reference implementation, no dependencies).
 * WebRTC full mesh + "perfect negotiation" over /community/ws/servers/{s}/voice/{c}.
 *
 *   const room = new VoiceRoom(serverId, channelId, {
 *     onParticipants: (list) => render(list),          // Map accountId -> participant
 *     onSpeaking: (accountId, value) => ...,
 *     onRemoteStream: (accountId, stream) => ...,      // attach to <audio>/<video>
 *     onClosed: (reason) => ...,
 *     onMove: (serverId, channelId) => ...,            // moderator moved us
 *   });
 *   await room.join();  room.setMute(true);  room.setDeaf(true);
 *   await room.toggleCamera();  await room.toggleScreen();  room.leave();
 *
 * Security note: server mute / stage audience are enforced on the RECEIVER:
 * audio of participants with can_speak === false is never played.
 */
class VoiceRoom {
  constructor(serverId, channelId, handlers = {}) {
    this.serverId = serverId;
    this.channelId = channelId;
    this.h = handlers;
    this.peers = new Map();          // accountId -> {pc, makingOffer, ignoreOffer, session, stream, audio}
    this.participants = new Map();   // accountId -> public participant
    this.selfId = null;
    this.iceServers = [];
    this.local = null;               // microphone stream
    this.videoTrack = null;
    this.screenTrack = null;
    this.deaf = false;
  }

  async join() {
    this.local = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/community/ws/servers/${this.serverId}/voice/${this.channelId}`);
    this.ws.onmessage = (e) => this._onMessage(JSON.parse(e.data));
    this.ws.onclose = (e) => this._teardown(e.code);
    this.heartbeat = setInterval(() => this._send({ type: "ping" }), 15000);
    this._startSpeakingDetector();
  }

  leave() { this._send({ type: "leave" }); this.ws && this.ws.close(); }

  setMute(value) {
    this.local.getAudioTracks().forEach((t) => (t.enabled = !value));
    this._send({ type: "state", self_mute: value });
  }

  setDeaf(value) {
    this.deaf = value;
    for (const peer of this.peers.values()) if (peer.audio) peer.audio.muted = value || !this._canHear(peer);
    if (value) this.setMute(true);
    this._send({ type: "state", self_deaf: value });
  }

  async toggleCamera() {
    if (this.videoTrack) { this._removeTrack(this.videoTrack); this.videoTrack = null; }
    else {
      const s = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 360, frameRate: 24 } });
      this.videoTrack = s.getVideoTracks()[0];
      this._addTrack(this.videoTrack, s);
    }
    this._send({ type: "state", video: !!this.videoTrack });
  }

  async toggleScreen() {
    if (this.screenTrack) { this._removeTrack(this.screenTrack); this.screenTrack = null; }
    else {
      const s = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 15 } });
      this.screenTrack = s.getVideoTracks()[0];
      this.screenTrack.onended = () => this.toggleScreen();
      this._addTrack(this.screenTrack, s);
    }
    this._send({ type: "state", screen: !!this.screenTrack });
  }

  moderate(action, target, extra = {}) { this._send({ type: "mod", action, target, ...extra }); }
  raiseHand(value) { this._send({ type: "raise_hand", value }); }

  // ------------------------------------------------------------------ internals
  _send(msg) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(msg)); }

  _canHear(peer) {
    const p = this.participants.get(peer.id);
    return !!p && p.can_speak !== false;
  }

  _emitParticipants() { this.h.onParticipants && this.h.onParticipants(new Map(this.participants)); }

  async _onMessage(msg) {
    switch (msg.type) {
      case "voice_ready":
        this.selfId = msg.self.account_id;
        this.iceServers = msg.ice_servers || [];
        for (const p of msg.participants) this.participants.set(p.account_id, p);
        this._emitParticipants();
        // The newcomer opens a connection to everybody already in the room.
        for (const p of msg.participants) if (p.account_id !== this.selfId) this._peer(p.account_id, p.session_id);
        break;
      case "participant_joined":
        this.participants.set(msg.participant.account_id, msg.participant);
        this._emitParticipants();
        break;
      case "participant_updated": {
        const p = msg.participant;
        this.participants.set(p.account_id, p);
        const peer = this.peers.get(p.account_id);
        if (peer && peer.audio) peer.audio.muted = this.deaf || !this._canHear(peer);
        if (p.account_id === this.selfId && !p.can_speak) this.local.getAudioTracks().forEach((t) => (t.enabled = false));
        this._emitParticipants();
        break;
      }
      case "participant_left":
        this.participants.delete(msg.account_id);
        this._closePeer(msg.account_id);
        this._emitParticipants();
        break;
      case "speaking":
        this.h.onSpeaking && this.h.onSpeaking(msg.account_id, msg.value);
        break;
      case "signal":
        await this._onSignal(msg);
        break;
      case "voice_move":
        this.h.onMove && this.h.onMove(msg.server_id, msg.channel_id);
        break;
      case "voice_error": case "voice_kicked": case "voice_replaced":
        this.closeReason = msg.error || msg.reason || msg.type;
        break;
    }
  }

  _peer(id, session) {
    let peer = this.peers.get(id);
    if (peer) return peer;
    const pc = new RTCPeerConnection({ iceServers: this.iceServers });
    peer = { id, pc, session, makingOffer: false, ignoreOffer: false, polite: this.selfId > id };
    this.peers.set(id, peer);
    for (const t of this.local.getTracks()) pc.addTrack(t, this.local);
    if (this.videoTrack) pc.addTrack(this.videoTrack);
    if (this.screenTrack) pc.addTrack(this.screenTrack);

    pc.onnegotiationneeded = async () => {
      try {
        peer.makingOffer = true;
        await pc.setLocalDescription();
        this._send({ type: "signal", to: id, to_session: peer.session, kind: "offer", data: { description: pc.localDescription } });
      } finally { peer.makingOffer = false; }
    };
    pc.onicecandidate = ({ candidate }) => {
      if (candidate) this._send({ type: "signal", to: id, to_session: peer.session, kind: "ice", data: { candidate } });
    };
    pc.oniceconnectionstatechange = () => { if (pc.iceConnectionState === "failed") pc.restartIce(); };
    pc.ontrack = ({ track, streams }) => {
      const stream = streams[0] || new MediaStream([track]);
      if (track.kind === "audio") {
        peer.audio = peer.audio || new Audio();
        peer.audio.srcObject = stream;
        peer.audio.muted = this.deaf || !this._canHear(peer);
        peer.audio.play().catch(() => {});
      }
      this.h.onRemoteStream && this.h.onRemoteStream(id, stream, track);
    };
    return peer;
  }

  async _onSignal({ from, from_session, kind, data }) {
    const known = this.participants.get(from);
    if (!known || known.session_id !== from_session) return;   // stale / foreign session
    const peer = this._peer(from, from_session);
    const pc = peer.pc;
    try {
      if (kind === "offer" || kind === "answer") {
        const description = data.description;
        const collision = description.type === "offer" && (peer.makingOffer || pc.signalingState !== "stable");
        peer.ignoreOffer = !peer.polite && collision;
        if (peer.ignoreOffer) return;
        await pc.setRemoteDescription(description);
        if (description.type === "offer") {
          await pc.setLocalDescription();
          this._send({ type: "signal", to: from, to_session: from_session, kind: "answer", data: { description: pc.localDescription } });
        }
      } else if (kind === "ice" && data.candidate) {
        try { await pc.addIceCandidate(data.candidate); } catch (e) { if (!peer.ignoreOffer) throw e; }
      }
    } catch (e) { console.warn("voice signal error", e); }
  }

  _addTrack(track, stream) { for (const p of this.peers.values()) p.pc.addTrack(track, stream); }

  _removeTrack(track) {
    track.stop();
    for (const p of this.peers.values()) {
      const sender = p.pc.getSenders().find((s) => s.track === track);
      if (sender) p.pc.removeTrack(sender);
    }
  }

  _closePeer(id) {
    const peer = this.peers.get(id);
    if (!peer) return;
    peer.pc.close();
    if (peer.audio) peer.audio.srcObject = null;
    this.peers.delete(id);
  }

  _startSpeakingDetector() {
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    ctx.createMediaStreamSource(this.local).connect(analyser);
    const buf = new Uint8Array(analyser.fftSize);
    let speaking = false, quietSince = 0;
    this.speakTimer = setInterval(() => {
      analyser.getByteTimeDomainData(buf);
      let peak = 0;
      for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
      const enabled = this.local.getAudioTracks().some((t) => t.enabled);
      const loud = enabled && peak > 12;
      const now = performance.now();
      if (loud) quietSince = now;
      const next = loud || now - quietSince < 300;
      if (next !== speaking) { speaking = next; this._send({ type: "speaking", value: speaking }); }
    }, 100);
    this.audioCtx = ctx;
  }

  _teardown(code) {
    clearInterval(this.heartbeat); clearInterval(this.speakTimer);
    for (const id of [...this.peers.keys()]) this._closePeer(id);
    this.local && this.local.getTracks().forEach((t) => t.stop());
    this.videoTrack && this.videoTrack.stop();
    this.screenTrack && this.screenTrack.stop();
    this.audioCtx && this.audioCtx.close();
    this.h.onClosed && this.h.onClosed(this.closeReason || code);
  }
}
window.VoiceRoom = VoiceRoom;
