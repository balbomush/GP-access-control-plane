/* Stream, reconnect, fallback and their timers are owned here, never by UI rendering. */
class RealtimeController {
  constructor(options) {
    this._api = options.api;
    this._url = options.url;
    this._onEvent = options.onEvent;
    this._onConnection = options.onConnection || (() => {});
    this._fallback = options.fallback || (() => {});
    this._isActive = options.isActive || (() => true);
    this._setTimeout = options.setTimeout || ((callback, delay) => setTimeout(callback, delay));
    this._clearTimeout = options.clearTimeout || ((timer) => clearTimeout(timer));
    this._setInterval = options.setInterval || ((callback, delay) => setInterval(callback, delay));
    this._clearInterval = options.clearInterval || ((timer) => clearInterval(timer));
    this._fallbackMs = options.fallbackMs || 30000;
    this._controller = null;
    this._reconnectTimer = null;
    this._fallbackTimer = null;
    this._connected = false;
    this._disposed = false;
    this._delay = 1000;
    this._epoch = 0;
  }
  connected() { return this._connected; }
  start() {
    this.disposeStream();
    this._disposed = false;
    this._delay = 1000;
    this._startFallback();
    this._connect(++this._epoch);
  }
  renew() { this.start(); }
  disposeStream() {
    if (this._reconnectTimer) this._clearTimeout(this._reconnectTimer);
    this._reconnectTimer = null;
    if (this._controller) this._controller.abort();
    this._controller = null;
    this._connected = false;
    this._onConnection(false);
  }
  dispose() {
    this._disposed = true;
    this.disposeStream();
    if (this._fallbackTimer) this._clearInterval(this._fallbackTimer);
    this._fallbackTimer = null;
  }
  _startFallback() {
    if (this._fallbackTimer) this._clearInterval(this._fallbackTimer);
    this._fallbackTimer = this._setInterval(() => {
      if (!this._connected && !this._disposed && this._isActive()) this._fallback();
    }, this._fallbackMs);
  }
  async _connect(epoch) {
    if (this._disposed || !this._isActive() || epoch !== this._epoch) return;
    const controller = new AbortController();
    this._controller = controller;
    try {
      await this._api.streamSse(this._url(), { signal: controller.signal, onOpen: () => {
        if (this._controller === controller && !controller.signal.aborted && epoch === this._epoch) {
          this._connected = true;
          this._delay = 1000;
          this._onConnection(true);
        }
      }, onEvent: (event, data) => {
        if (this._controller === controller && !controller.signal.aborted && !this._disposed && epoch === this._epoch && this._isActive()) this._onEvent(event, data);
      }});
    } catch (error) {
      if (!controller.signal.aborted) console.warn('Realtime connection stopped', error);
    } finally {
      if (this._controller === controller) this._controller = null;
      if (epoch !== this._epoch || controller.signal.aborted || this._disposed) return;
      this._connected = false;
      this._onConnection(false);
      this._scheduleReconnect(epoch);
    }
  }
  _scheduleReconnect(epoch) {
    if (this._reconnectTimer || this._disposed || !this._isActive()) return;
    const delay = this._delay;
    this._delay = Math.min(this._delay * 2, 30000);
    this._reconnectTimer = this._setTimeout(() => {
      this._reconnectTimer = null;
      this._connect(epoch);
    }, delay);
  }
}
