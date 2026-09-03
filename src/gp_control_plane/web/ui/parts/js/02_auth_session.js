function authToken(){
  return localStorage.getItem(AUTH_TOKEN_KEY) || '';
}
function requestHeaders(headers){
  const token = authToken();
  return {
    ...(headers || {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {})
  };
}
function requestUrl(url){
  return url;
}
function storeAuthToken(payload){
  const token = String((payload || {}).access_token || (payload || {}).token || '').trim();
  if (!token) throw new Error('The server did not return an authorization token');
  localStorage.setItem(AUTH_TOKEN_KEY, token);
  return token;
}
function showLogin(message){
  el('app-shell').hidden = true;
  el('login-screen').hidden = false;
  el('login-error').textContent = message || '';
  requestAnimationFrame(() => el('login-username').focus());
}
function showApplication(){
  el('login-screen').hidden = true;
  el('app-shell').hidden = false;
}
function stopRealtimeEvents(){
  if (realtimeReconnectTimer) clearTimeout(realtimeReconnectTimer);
  realtimeReconnectTimer = null;
  if (realtimeSource) realtimeSource.abort();
  realtimeSource = null;
  realtimeConnected = false;
}
function renewRealtimeEvents(){
  stopRealtimeEvents();
  realtimeReconnectDelay = 1000;
  startRealtimeEvents({ alreadyStopped: true });
}function stopRealtimeFallback(){
  if (realtimeFallbackTimer) clearInterval(realtimeFallbackTimer);
  realtimeFallbackTimer = null;
}
function handleUnauthorized(){
  if (!authToken()) return;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  stopRealtimeEvents();
  stopRealtimeFallback();
  showLogin('Your session has expired. Sign in again.');
}
function logout(){
  localStorage.removeItem(AUTH_TOKEN_KEY);
  stopRealtimeEvents();
  stopRealtimeFallback();
  showLogin();
}
async function authFetch(url, options){
  const request = options || {};
  const response = await fetch(url, {
    ...request,
    headers: requestHeaders(request.headers),
    credentials: 'same-origin'
  });
  if (response.status === 401) handleUnauthorized();
  return response;
}
function startAuthenticatedUi(){
  showApplication();
  refresh();
  startRealtimeEvents();
  startRealtimeFallback();
}
async function submitLogin(event){
  event.preventDefault();
  const errorNode = el('login-error');
  const form = el('login-form');
  const button = form.querySelector('button[type="submit"]');
  errorNode.textContent = '';
  button.disabled = true;
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ username: el('login-username').value, password: el('login-password').value })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const apiError = data && typeof data.error === 'object' ? data.error : {};
      throw new Error(apiError.message || data.message || 'Unable to sign in');
    }
    storeAuthToken(data);
    startAuthenticatedUi();
  } catch (error) {
    errorNode.textContent = error.message || 'Unable to sign in';
  } finally {
    button.disabled = false;
  }
}
async function changePassword(){
  const form = el('change-password-form');
  const submitButton = form.querySelector('[type="submit"]');
  const status = el('change-password-status');
  const currentPassword = el('settings-current-password').value;
  const newPassword = el('settings-new-password').value;
  form.setAttribute('aria-busy', 'true');
  submitButton.disabled = true;
  status.textContent = 'Пароль изменяется…';
  try {
    await postJson('/api/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword
    });
    logout();
  } catch (error) {
    status.textContent = 'Не удалось изменить пароль. Проверьте текущий пароль и повторите попытку.';
  } finally {
    el('settings-current-password').value = '';
    el('settings-new-password').value = '';
    submitButton.disabled = false;
    form.removeAttribute('aria-busy');
  }
}function apiEndpoint(namespace, name){
  const group = API_ENDPOINTS[namespace] || {};
  const endpoint = group[name];
  if (!endpoint) throw new Error(`Unknown API endpoint: ${namespace}.${name}`);
  return endpoint;
}
