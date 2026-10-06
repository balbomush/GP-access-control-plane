/* The accepted run and every run-derived UI field have one reconciliation owner. */
class RunState {
  constructor(view, options = {}) {
    this.view = view;
    this._runId = options.runId || ((row) => String((row || {}).run_id || (row || {}).id || ''));
    this._terminal = options.terminal || ((status) => ['success', 'failed', 'error', 'stopped', 'timeout'].includes(String(status || '').toLowerCase()));
    this._generation = 0;
    this._accepted = null;
    this._startInFlight = false;
    this._logRevision = 0;
    this._retiredRunIds = new Set();
    this._terminalEvidence = null;
    this._historyEvidence = new Map();
    this._pendingStatus = null;
    this._historyStatusPromoted = false;
  }

  accepted() { return this._accepted; }
  generation() { return this._generation; }
  isGenerationCurrent(generation) { return Boolean(this._accepted && this._accepted.generation === generation); }
  current() { return this._accepted || ((this.view.status || {}).current_run || null); }
  busy() { return Boolean(this.current()); }
  startInFlight() { return this._startInFlight; }
  setStartInFlight(value) { this._startInFlight = Boolean(value); this.view.startRequestInFlight = this._startInFlight; }
  resetForSession() {
    this._generation += 1;
    this._accepted = null;
    this._startInFlight = false;
    this._logRevision += 1;
    this._retiredRunIds.clear();
    this._terminalEvidence = null;
    this._historyEvidence.clear();
    this._pendingStatus = null;
    this._historyStatusPromoted = false;
    this.view.acknowledgedRun = null;
    this.view.runGeneration = this._generation;
    this.view.startRequestInFlight = false;
    this.view.finderLog = null;
  }

  acknowledge(runId) {
    this._generation += 1;
    this._accepted = { run_id: runId, status: 'queued', generation: this._generation };
    this._logRevision += 1;
    this._historyEvidence.set(runId, this._accepted);
    this._pendingStatus = null;
    this.view.acknowledgedRun = this._accepted;
    this.view.runGeneration = this._generation;
    this.view.finderLog = null;
    return this._generation;
  }

  rejectAccepted() {
    this._accepted = null;
    this.view.acknowledgedRun = null;
  }

  mergeHistory(payload, reset, latestById) {
    this._historyStatusPromoted = false;
    const rows = latestById((payload || {}).runs || []);
    rows.forEach((row) => this._rememberHistory(row));
    this.view.finderRuns = reset ? rows : latestById([...rows, ...this.view.finderRuns]);
    this.view.finderRunTotal = Number((payload || {}).total || this.view.finderRuns.length);
    this.view.finderRunOffset = Number((payload || {}).offset || 0) + ((payload || {}).runs || []).length;
    this.view.finderRunHasMore = Boolean((payload || {}).has_more);
    this.view.finderRunsLoaded = true;
    this.view.finderRunsLoading = false;
    this.convergeHistory(rows);
    // SSE may deliver status before runs.  Preserve an otherwise-untrusted
    // status just long enough for the corresponding nonterminal history row to
    // establish its identity; a second status event is not guaranteed.
    if (this._pendingStatus) {
      const pending = this._pendingStatus;
      const pendingRun = pending.current_run && typeof pending.current_run === 'object' ? pending.current_run : null;
      if (pendingRun && this._isCurrentHistoryRun(pendingRun)) {
        this._pendingStatus = null;
        this._historyStatusPromoted = this.mergeStatus(pending);
      }
    }
    return rows;
  }

  consumeHistoryStatusPromotion() {
    const promoted = this._historyStatusPromoted;
    this._historyStatusPromoted = false;
    return promoted;
  }

  convergeHistory(rows) {
    const current = this.current();
    if (!current) return false;
    const runId = this._runId(current);
    const terminal = (rows || []).find((row) => this._runId(row) === runId && this._terminal(row.status));
    if (!terminal) return false;
    this._retire(runId);
    this._terminalEvidence = { runId, generation: this._generation };
    this.rejectAccepted();
    if ((this.view.status || {}).current_run && this._runId(this.view.status.current_run) === runId) {
      this.view.status = { ...this.view.status, current_run: null };
    }
    return true;
  }

  mergeStatus(status) {
    if (!status) return false;
    const accepted = this._accepted;
    const reported = status.current_run && typeof status.current_run === 'object' ? status.current_run : null;
    if (reported && this._retiredRunIds.has(this._runId(reported))) {
      // History already proved this run terminal. Retain that barrier after the
      // accepted identity is cleared so a late status cannot revive it.
      return false;
    }
    if (!accepted && this._terminalEvidence && reported && !this._isCurrentHistoryRun(reported)) {
      // After terminal convergence, a distinct live run needs fresh history
      // evidence. This rejects delayed unknown old status without permanently
      // suppressing a server/other-client run once its history row arrives.
      this._pendingStatus = status;
      return false;
    }
    if (accepted) {
      if (reported && this._runId(reported) === accepted.run_id) {
        this._accepted = { ...accepted, ...reported, run_id: accepted.run_id, generation: accepted.generation };
        this.view.acknowledgedRun = this._accepted;
      }
      this.view.status = { ...status, current_run: this._accepted };
    } else {
      if (reported && this._runId(reported) !== this._runId(this.current())) {
        // An externally started run owns the same request lifetime as a local
        // acknowledgement. Late responses from the displayed terminal run
        // must not survive this transition, even after current becomes idle.
        this._generation += 1;
        this._logRevision += 1;
        this.view.runGeneration = this._generation;
        this.view.finderLog = null;
        this._terminalEvidence = null;
      }
      this.view.status = status;
    }
    this._pendingStatus = null;
    return true;
  }

  captureLogRequest() {
    const log = this.view.finderLog || {};
    return {
      generation: this._generation,
      runId: this._runId(this.current()) || (this._terminalEvidence || {}).runId || '',
      revision: this._logRevision,
      stdoutLog: log.stdout_log || '',
      stdoutSize: Number(log.stdout_size || 0),
      stderrLog: log.stderr_log || '',
      stderrSize: Number(log.stderr_size || 0),
      // Keep the text snapshot as received.  It is not safe to reconstruct a
      // raw-byte overlap by encoding decoded text: a concurrent response can
      // complete a character whose earlier response ended mid-UTF-8 sequence.
      stdoutTail: log.stdout_tail || '',
      stderrTail: log.stderr_tail || ''
    };
  }

  acceptLog(payload, incremental, mergeLog, request) {
    if (!payload) return false;
    const runId = this._runId(payload);
    const retired = this._retiredRunIds.has(runId);
    if (retired && !this._isTerminalLog(payload)) return false;
    if (!this._accepted && this._terminalEvidence && !retired && !this._isCurrentHistoryRun(payload)) return false;
    const currentRunId = this._runId(this.current());
    if (currentRunId && runId !== currentRunId) return false;
    if (retired && (!this._terminalEvidence || this._terminalEvidence.runId !== runId || this._terminalEvidence.generation !== this._generation)) return false;
    let next = payload;
    if (request) {
      const terminalCompletion = !currentRunId && retired && request.runId === runId && this._isTerminalLog(next);
      if (request.generation !== this._generation || (request.runId !== currentRunId && !terminalCompletion)) return false;
      if (request.revision !== this._logRevision) {
        next = incremental ? this._advanceIncrementalPayload(payload, request) : this._advanceFullPayload(payload);
        if (!next) return false;
      }
    }
    if (next.progress) next.progress.received_at_ms = Date.now();
    this.view.finderLog = incremental ? mergeLog(this.view.finderLog, next) : next;
    this._logRevision += 1;
    return true;
  }

  _retire(runId) {
    if (!runId) return;
    this._retiredRunIds.add(runId);
    // Keep enough completed identities to reject delayed status/log responses,
    // without letting an unusually long page session grow this barrier forever.
    while (this._retiredRunIds.size > 32) this._retiredRunIds.delete(this._retiredRunIds.values().next().value);
  }

  _rememberHistory(row) {
    const runId = this._runId(row);
    if (runId) this._historyEvidence.set(runId, row);
  }

  _isCurrentHistoryRun(row) {
    const history = this._historyEvidence.get(this._runId(row));
    return Boolean(history && !this._terminal(history.status));
  }

  _isTerminalLog(payload) {
    return this._terminal(payload.status);
  }

  _advanceIncrementalPayload(payload, request) {
    const current = this.view.finderLog || {};
    const stdoutAhead = Number(payload.stdout_size || 0) > Number(current.stdout_size || 0);
    const stderrAhead = Number(payload.stderr_size || 0) > Number(current.stderr_size || 0);
    if (!stdoutAhead && !stderrAhead) return null;
    const next = { ...payload };
    this._reconcileIncrementalStream(next, payload, request, current, 'stdout', stdoutAhead);
    this._reconcileIncrementalStream(next, payload, request, current, 'stderr', stderrAhead);
    return next;
  }

  _advanceFullPayload(payload) {
    const current = this.view.finderLog || {};
    if (this._runId(current) !== this._runId(payload)) return payload;
    const ahead = name => Number(payload[`${name}_size`] || 0) > Number(current[`${name}_size`] || 0);
    const terminalUpgrade = this._isTerminalLog(payload) && !this._isTerminalLog(current);
    if (!ahead('stdout') && !ahead('stderr') && !terminalUpgrade) return null;
    const next = { ...payload };
    for (const name of ['stdout', 'stderr']) {
      if (Number(payload[`${name}_size`] || 0) < Number(current[`${name}_size`] || 0)) {
        for (const field of ['size', 'log', 'tail']) next[`${name}_${field}`] = current[`${name}_${field}`];
        next[`${name}_append`] = '';
      }
    }
    return next;
  }

  _reconcileIncrementalStream(next, payload, request, current, name, ahead) {
    const size = `${name}_size`;
    const log = `${name}_log`;
    const tail = `${name}_tail`;
    const append = `${name}_append`;
    const requestName = name === 'stdout' ? 'stdout' : 'stderr';
    const requestLog = `${requestName}Log`;
    const requestTail = `${requestName}Tail`;
    if (!ahead) {
      // The other stream may have grown.  Retain this stream exactly as it was
      // instead of merging a stale duplicate append or moving its byte offset
      // backwards.
      next[size] = current[size] || 0;
      next[log] = current[log] || '';
      next[tail] = current[tail] || '';
      next[append] = '';
      return;
    }
    if (request[requestLog] && request[requestLog] === current[log] && payload[append]) {
      // This response is authoritative from the original request offset.  Build
      // from the captured decoded prefix and the complete newer append, rather
      // than byte-slicing decoded text.  This replaces a previous U+FFFD prefix
      // with the later complete UTF-8 character and avoids duplicate overlap.
      next[tail] = RunState.appendText(request[requestTail], payload[append]);
      next[append] = '';
    }
  }

  static appendText(base, addition) {
    const left = String(base || '');
    const right = String(addition || '');
    if (!left || !right || left.endsWith('\n') || right.startsWith('\n')) return left + right;
    return `${left}\n${right}`;
  }

  // Kept for diagnostics that compare the old raw-byte trimming behavior.  Log
  // reconciliation deliberately does not call it: decoded replacement text is
  // not a safe source for raw-byte slicing across a UTF-8 boundary.
  static dropPrefixBytes(value, byteCount) {
    if (!byteCount) return value;
    const bytes = new TextEncoder().encode(String(value || ''));
    return new TextDecoder().decode(bytes.slice(byteCount));
  }
}
