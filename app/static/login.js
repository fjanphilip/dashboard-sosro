const $ = s => document.querySelector(s);
const AUTH_KEY = 'authToken';
const getToken = () => { try { return localStorage.getItem(AUTH_KEY) || ''; } catch { return ''; } };
const setToken = t => { try { t ? localStorage.setItem(AUTH_KEY, t) : localStorage.removeItem(AUTH_KEY); } catch { } };

// Sudah punya sesi valid? Langsung ke dashboard.
(async () => {
  const token = getToken();
  if (!token) return;
  try {
    const res = await fetch('/api/me', { headers: { Authorization: 'Bearer ' + token } });
    if (res.ok) location.href = '/';
    else setToken('');
  } catch { /* biarkan form tampil */ }
})();

$('#login-form').onsubmit = async e => {
  e.preventDefault();
  const err = $('#login-err');
  const btn = e.target.querySelector('button');
  err.hidden = true;
  btn.disabled = true;
  try {
    const res = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('#u').value.trim(), password: $('#p').value }) });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || 'Gagal masuk');
    setToken(json.token);
    location.href = '/';
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
    btn.disabled = false;
  }
};
