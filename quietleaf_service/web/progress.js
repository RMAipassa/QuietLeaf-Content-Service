/* Durable, revision-checked progress queue. Stores positions/IDs, never chapter text or credentials. */
((root) => {
  "use strict";
  const position = (value) => value ? { chapterId: value.chapterId, fraction: Number(value.fraction) } : null;
  const equal = (a, b) => a === b || Boolean(a && b && a.chapterId === b.chapterId && Math.abs(a.fraction - b.fraction) < 1e-6);

  class ProgressClient {
    constructor(storage, transport, clientId, eventId) {
      this.storage = storage;
      this.transport = transport;
      this.clientId = clientId;
      this.eventId = eventId;
      this.running = new Map();
      this.memory = new Map();
    }
    load(key) {
      try { return JSON.parse(this.storage.getItem(`quietleaf.progress.${key}`) || "null") || this.memory.get(key) || null; }
      catch (_) { return this.memory.get(key) || null; }
    }
    save(key, state) {
      this.memory.set(key, state);
      try { this.storage.setItem(`quietleaf.progress.${key}`, JSON.stringify(state)); } catch (_) { /* Private modes may deny storage. */ }
      return state;
    }
    open(key, remote) {
      const previous = this.load(key);
      const remotePosition = position(remote);
      if (!previous) return this.save(key, { revision: remote?.revision || 0, baseline: remotePosition, draft: remotePosition, pending: null, conflict: null });
      if ((remote?.revision || 0) < previous.revision) return previous;
      const dirty = previous.pending || !equal(previous.draft, previous.baseline);
      if (dirty) {
        if ((remote?.revision || 0) !== previous.revision && remote?.eventId !== previous.pending?.eventId) {
          previous.conflict = { progress: remote };
        }
        return this.save(key, previous);
      }
      return this.save(key, { revision: remote?.revision || 0, baseline: remotePosition, draft: remotePosition, pending: null, conflict: null });
    }
    stage(key, value) {
      const state = this.load(key);
      if (!state) return;
      state.draft = position(value);
      this.save(key, state);
    }
    async flush(key, keepalive = false) {
      if (this.running.has(key)) return this.running.get(key);
      const work = this.pump(key, keepalive).finally(() => this.running.delete(key));
      this.running.set(key, work);
      return work;
    }
    async pump(key, keepalive) {
      for (let attempt = 0; attempt < 4; attempt++) {
        let state = this.load(key);
        if (!state || state.conflict || !state.draft) return state;
        if (!state.pending) {
          if (equal(state.draft, state.baseline)) return state;
          state.pending = { ...state.draft, baseRevision: state.revision, clientId: this.clientId, eventId: this.eventId() };
          this.save(key, state);
        }
        const sent = state.pending;
        const reply = await this.transport(key, sent, keepalive);
        state = this.load(key);
        if (!state || state.pending?.eventId !== sent.eventId) return state;
        if (reply.conflict) {
          state.conflict = { progress: reply.progress };
          return this.save(key, state);
        }
        state.revision = reply.progress.revision;
        state.baseline = position(sent);
        state.pending = null;
        this.save(key, state);
      }
      return this.load(key);
    }
    resolve(key, remote, keepLocal) {
      const previous = this.load(key);
      const remotePosition = position(remote);
      return this.save(key, { revision: remote?.revision || 0, baseline: remotePosition,
        draft: keepLocal ? previous?.draft : remotePosition, pending: null, conflict: null });
    }
  }
  const exports = { ProgressClient, equalPosition: equal };
  if (typeof module !== "undefined" && module.exports) module.exports = exports;
  else root.QuietLeafProgress = exports;
})(typeof window === "undefined" ? globalThis : window);
