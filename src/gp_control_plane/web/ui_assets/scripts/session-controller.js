/* Session epochs abort prior bootstrap work and invalidate all late callbacks. */
class SessionController {
  constructor(options) {
    this._api = options.api;
    this._token = options.token;
    this._ui = options.ui;
    this._realtime = options.realtime;
    this._epoch = 0;
    this._bootstrapController = null;
    this._lifetimeController = new AbortController();
    this._state = 'idle';
  }
  epoch() { return this._epoch; }
  state() { return this._state; }
  isCurrent(epoch) { return epoch === this._epoch; }
  signal() { return this._lifetimeController.signal; }
  active(epoch) { return epoch === this._epoch && !this._bootstrapController?.signal.aborted; }
  _invalidate() {
    this._epoch += 1;
    this._bootstrapController?.abort();
    this._bootstrapController = null;
    this._lifetimeController.abort();
    this._lifetimeController = new AbortController();
    this._realtime?.dispose();
    return this._epoch;
  }
  async bootstrap(load, apply) {
    const epoch = this._invalidate();
    const controller = new AbortController();
    this._bootstrapController = controller;
    this._state = 'loading';
    this._ui.begin?.(epoch);
    this._ui.boot('loading');
    try {
      const payload = await load(controller.signal);
      if (!this.active(epoch)) return false;
      apply(payload, epoch);
      if (!this.active(epoch)) return false;
      this._state = 'ready';
      this._ui.ready();
      this._realtime?.start();
      return true;
    } catch (error) {
      if (!this.active(epoch)) return false;
      controller.abort();
      this._state = 'failed';
      this._ui.boot('failed');
      return false;
    } finally {
      if (epoch === this._epoch) this._bootstrapController = null;
    }
  }
  async login(credentials, onError) {
    try {
      const data = await this._api.postJson('/api/auth/login', credentials);
      this._token.store(data);
      return this.bootstrap(this._ui.load, this._ui.apply);
    } catch (error) {
      onError(error);
      return false;
    }
  }
  async changePassword(payload, onError) {
    const epoch = this._epoch;
    try {
      await this._api.postJson('/api/auth/change-password', payload);
      // A previous password request must not log out a session which replaced it
      // while its response body was still being consumed.
      if (!this.isCurrent(epoch)) return false;
      this.logout();
      return true;
    } catch (error) {
      if (this.isCurrent(epoch)) onError(error);
      return false;
    }
  }
  logout(message) {
    this._token.clear();
    this._invalidate();
    this._state = 'idle';
    this._ui.login(message);
  }
  unauthorized() {
    if (this._token.get()) this.logout('Your session has expired. Sign in again.');
  }
}
