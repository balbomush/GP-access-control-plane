/* Transport only: this module deliberately has no DOM or product state. */
class ApiClient {
  constructor(options = {}) {
    this._getToken = options.getToken || (() => '');
    this._getEpoch = options.getEpoch || (() => 0);
    this._isEpochCurrent = options.isEpochCurrent || (() => true);
    this._getSignal = options.getSignal || (() => null);
    this._onUnauthorized = options.onUnauthorized || (() => {});
    // Resolve browser fetch at request time: embedders may replace it.
    this._fetch = options.fetch || ((...args) => fetch(...args));
  }

  headers(headers) {
    const token = this._getToken();
    return { ...(headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) };
  }

  async request(url, options = {}) {
    const token = this._getToken();
    const epoch = this._getEpoch();
    const signal = ApiClient.combineSignals(options.signal, this._getSignal());
    const { keepSessionSignal, ...requestOptions } = options;
    let releaseSignal = true;
    try {
      const response = await this._fetch(url, {
        ...requestOptions,
        signal: signal.value,
        headers: this.headers(options.headers),
        credentials: 'same-origin'
      });
      this.assertCurrent(token, epoch);
      const unauthorized = response.status === 401;
      if (keepSessionSignal) {
        // A 401 deliberately invalidates the session. Preserve its established
        // HTTP error contract instead of relabelling that intentional logout as
        // an unrelated stale completion while its error body is consumed.
        response.__gpRequestLifetime = { token, epoch, signal: signal.value, release: signal.release, unauthorized };
        releaseSignal = false;
      } else if (unauthorized) {
        this._onUnauthorized();
      }
      return response;
    } catch (error) {
      // A lifetime abort is more useful to consumers as a stale-session result than
      // as a generic successful-looking AbortError continuation.
      if (!this.isCurrent(token, epoch)) throw ApiClient.staleSessionError();
      throw error;
    } finally {
      if (releaseSignal) signal.release();
    }
  }

  async getJson(url, options) {
    const response = await this.request(url, { ...(options || {}), keepSessionSignal: true });
    try {
      if (!response.ok) {
        let message;
        try { message = await response.text(); }
        catch (error) { throw this.bodyError(response, error); }
        this.assertResponseCurrent(response);
        throw new Error(message);
      }
      let data;
      try { data = await response.json(); }
      catch (error) { throw this.bodyError(response, error); }
      this.assertResponseCurrent(response);
      return data;
    } finally {
      this.releaseResponse(response);
    }
  }

  async postJson(url, payload, options = {}) {
    const response = await this.request(url, {
      ...options,
      keepSessionSignal: true,
      method: 'POST',
      headers: { ...(options.headers || {}), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    try {
      let data;
      try {
        data = await response.json();
      } catch (error) {
        const cancellation = this.bodyCancellation(response, error);
        if (cancellation) throw cancellation;
        // The established JSON adapter contract treats malformed response JSON
        // as an empty payload. Cancellation and stale bodies are handled above,
        // so neither is ever promoted to a successful result or synthetic HTTP error.
        data = {};
      }
      this.assertResponseCurrent(response);
      if (!response.ok) throw this.jsonError(response, data);
      return data;
    } finally {
      this.releaseResponse(response);
    }
  }

  jsonError(response, data) {
    const apiError = data && typeof data.error === 'object' ? data.error : {};
    const error = new Error(apiError.message || data.message || response.statusText);
    error.status = response.status;
    error.code = apiError.code || '';
    error.details = apiError.details || {};
    error.data = data;
    return error;
  }

  async blob(url, options) {
    const response = await this.request(url, { ...(options || {}), keepSessionSignal: true });
    try {
      if (!response.ok) {
        let message;
        try { message = await response.text(); }
        catch (error) { throw this.bodyError(response, error); }
        this.assertResponseCurrent(response);
        throw new Error(message || response.statusText);
      }
      let value;
      try { value = await response.blob(); }
      catch (error) { throw this.bodyError(response, error); }
      this.assertResponseCurrent(response);
      return { blob: value, response };
    } finally {
      this.releaseResponse(response);
    }
  }

  isCurrent(token, epoch) {
    return token === this._getToken() && this._isEpochCurrent(epoch);
  }

  assertCurrent(token, epoch) {
    if (!this.isCurrent(token, epoch)) throw ApiClient.staleSessionError();
  }

  assertResponseCurrent(response) {
    const lifetime = response && response.__gpRequestLifetime;
    if (lifetime && !lifetime.unauthorized) this.assertCurrent(lifetime.token, lifetime.epoch);
  }

  bodyCancellation(response, error) {
    const lifetime = response && response.__gpRequestLifetime;
    if (!lifetime) return null;
    if (!this.isCurrent(lifetime.token, lifetime.epoch)) return ApiClient.staleSessionError();
    // Fetch implementations do not agree on the error reported when a body is
    // aborted after headers (Chromium reports AbortError while Node may retain a
    // TimeoutError).  The response read is the authoritative phase boundary, so
    // preserve that error instead of replacing it with the signal's reason.
    if (lifetime.signal && lifetime.signal.aborted) return error;
    return null;
  }

  bodyError(response, error) {
    return this.bodyCancellation(response, error) || error;
  }

  releaseResponse(response) {
    const lifetime = response && response.__gpRequestLifetime;
    if (!lifetime) return;
    delete response.__gpRequestLifetime;
    lifetime.release();
    // A native response body inherits the request signal. Invalidate the
    // session only after its 401 detail has been consumed and preserved.
    // A delayed 401 belongs to the token/epoch that opened it.  It may only
    // invalidate that still-current session, never a replacement session.
    if (lifetime.unauthorized && this.isCurrent(lifetime.token, lifetime.epoch)) this._onUnauthorized();
  }

  async streamSse(url, options = {}) {
    const response = await this.request(url, {
      ...options,
      keepSessionSignal: true,
      headers: { ...(options.headers || {}), Accept: 'text/event-stream' }
    });
    if (!response.ok) {
      this.releaseResponse(response);
      throw new Error(response.statusText || 'SSE connection failed');
    }
    if (!response.body) {
      this.releaseResponse(response);
      throw new Error('SSE stream is unavailable');
    }
    options.onOpen?.(response);
    const reader = response.body.getReader();
    const cancelReader = () => { reader.cancel().catch(() => {}); };
    options.signal?.addEventListener('abort', cancelReader, { once: true });
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (!options.signal || !options.signal.aborted) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() || '';
        for (const frame of frames) {
          const parsed = ApiClient.parseSseFrame(frame);
          if (parsed.data) options.onEvent?.(parsed.event, parsed.data);
        }
      }
      // EOF is not an SSE frame delimiter. Decode any final UTF-8 bytes so the
      // decoder can be collected, but deliberately discard an unterminated frame.
      decoder.decode();
    } finally {
      options.signal?.removeEventListener('abort', cancelReader);
      cancelReader();
      this.releaseResponse(response);
    }
  }

  static parseSseFrame(frame) {
    let event = 'message';
    const data = [];
    for (const line of String(frame || '').split(/\r?\n/)) {
      if (!line || line.startsWith(':')) continue;
      const separator = line.indexOf(':');
      const field = separator < 0 ? line : line.slice(0, separator);
      const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      if (field === 'data') data.push(value);
    }
    return { event, data: data.join('\n') };
  }

  static staleSessionError() {
    const error = new Error('Session changed while the request was in flight');
    error.name = 'AbortError';
    error.staleSession = true;
    return error;
  }

  static combineSignals(primary, session) {
    if (!primary && !session) return { value: undefined, release: () => {} };
    if (!primary || primary === session) return { value: session || primary, release: () => {} };
    if (!session) return { value: primary, release: () => {} };
    const controller = new AbortController();
    const abort = (event) => controller.abort(event && event.target ? event.target.reason : undefined);
    primary.addEventListener('abort', abort, { once: true });
    session.addEventListener('abort', abort, { once: true });
    if (primary.aborted) controller.abort(primary.reason);
    else if (session.aborted) controller.abort(session.reason);
    return {
      value: controller.signal,
      release: () => {
        primary.removeEventListener('abort', abort);
        session.removeEventListener('abort', abort);
      }
    };
  }
}
