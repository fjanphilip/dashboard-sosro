const $ = s => document.querySelector(s);
// Math.round(v*10^d)||0 membuang tanda minus pada nilai yang dibulatkan menjadi nol (mis. -0,4 W -> -0,00 kW)
const nf = (v, d = 0) => (Math.round(Number(v) * 10 ** d) / 10 ** d || 0)
  .toLocaleString('id-ID', { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const DASH = '—';
const MODEL_LABEL = { pm5330: 'PM5330', sdm630: 'SDM630' };
const PHASE_MIN_V = 10; // di bawah ini fasa dianggap tidak terpasang / tidak bertegangan

const st = {
  page: 'dashboard', devices: [], site: 'all', area: 'all', q: '', openId: null, editId: null,
  dbConfig: null, dbStatus: {}, role: 'user', user: '',
  users: [], editUserId: null, dashSite: 'all', dashArea: 'all', dashMetric: 'kw', trendPoints: [], trendMeta: null,
  trendPeriod: 'live', trendLabels: [], trendSummary: null, trendError: null, trendFetchedAt: 0
};

// ---------- API ----------
const AUTH_KEY = 'authToken';
const getToken = () => { try { return localStorage.getItem(AUTH_KEY) || ''; } catch { return ''; } };
const setToken = t => { try { t ? localStorage.setItem(AUTH_KEY, t) : localStorage.removeItem(AUTH_KEY); } catch { } };

async function api(path, opts = {}) {
  const token = getToken();
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(token && { Authorization: 'Bearer ' + token }) },
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  const json = await res.json().catch(() => ({}));
  if (res.status === 401) {
    setToken('');
    location.href = '/login.html';
    throw new Error('belum login');
  }
  if (!res.ok) throw new Error(json.error || `${path}: HTTP ${res.status}`);
  return json;
}
const load = async () => { st.devices = await api('/api/devices'); };

// ---------- notifikasi ----------
const toastEls = new Map();
function toast(key, msg, type = 'warn', sticky = false) {
  const box = $('#toasts');
  if (!box) return;
  let el = toastEls.get(key);
  if (!el) {
    el = document.createElement('div');
    el.innerHTML = '<div class="toast-msg"></div><button class="toast-x" aria-label="Tutup">×</button>';
    el.querySelector('.toast-x').onclick = () => { el.remove(); toastEls.delete(key); };
    box.appendChild(el);
    toastEls.set(key, el);
  }
  el.className = 'toast ' + type;
  el.querySelector('.toast-msg').textContent = msg;
  clearTimeout(el._timer);
  if (!sticky) el._timer = setTimeout(() => { el.remove(); toastEls.delete(key); }, 5000);
}
function clearToast(key) {
  const el = toastEls.get(key);
  if (el) { clearTimeout(el._timer); el.remove(); toastEls.delete(key); }
}

// ---------- turunan data ----------
const devId = d => 'PM-' + String(d.id).padStart(3, '0');
const sum = a => a.reduce((s, v) => s + (v ?? 0), 0);
function summary(d) {
  const l = d.live;
  if (!l) return null;
  const vs = l.v.filter(v => v != null && v > PHASE_MIN_V);
  return {
    v: vs.length ? sum(vs) / vs.length : null, a: sum(l.i), kw: (l.p_total ?? sum(l.p)) / 1000,
    kva: (l.s_total ?? sum(l.s)) / 1000, freq: l.freq
  };
}

function sinceText(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return ` ${s} dtk`;
  if (s < 3600) return ` ${Math.round(s / 60)} mnt`;
  if (s < 86400) return ` ${Math.round(s / 3600)} jam`;
  return ` ${Math.round(s / 86400)} hari`;
}

// Status yang ditampilkan hanya ONLINE atau OFFLINE. Penyebab teknis (gateway /
// meter / konfigurasi / nilai abnormal) tetap dikirim server dan muncul di tooltip
// serta di drawer perangkat, supaya operator tetap tahu langkah perbaikannya.
function status(d) {
  if (!d.enabled) return '<span class="pill off">Nonaktif</span>';
  const h = d.health || {};
  const state = d.state || h.state;
  const dur = h.since ? sinceText(h.since).trim() : '';
  const detail = [h.message,
  d.endpoint && d.endpoint.suspect
    ? 'semua slave di endpoint ini gagal - cek gateway / kabel RS485 / LAN' : '',
  dur ? `sudah ${dur}` : '',
  ].filter(Boolean).join(' · ');
  const title = detail ? ` title="${esc(detail)}"` : '';
  const online = state === 'ONLINE' && d.link !== 'offline';
  return online
    ? `<span class="pill ok"${title}>Online</span>`
    : `<span class="pill bad"${title}>Offline</span>`;
}

// ---------- halaman Listrik ----------
function buildPage() {
  $('#page').innerHTML = `
  <div class="grid g4" id="kpi"></div>
  <div class="card mt">
    <div class="toolbar">
      <label class="pick-label" for="site-select">Site</label>
      <select id="site-select" class="pick"></select>
      <label class="pick-label" for="area-select" style="margin-left:8px">Area</label>
      <select id="area-select" class="pick"></select>
      <span style="margin-left:auto"></span>
      <input id="q" placeholder="Cari titik…" style="width:200px">
      ${st.role === 'admin' ? '<button id="btn-add">+ Tambah perangkat</button>' : ''}
    </div>
    <div class="table-wrap"><table class="wide">
      <thead><tr><th>ID</th><th>Titik ukur</th><th>Site</th><th>Area</th><th>Model</th><th class="num">ID Slave</th><th>Modbus TCP</th><th class="num">Tegangan (V)</th><th class="num">Arus (A)</th>
        <th class="num">Daya (kW)</th><th class="num">Daya semu (kVA)</th><th class="num">Frekuensi (Hz)</th><th>Status</th><th></th></tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
    <div class="muted small mt">Klik baris untuk melihat parameter real-time 3 fasa. Tegangan = rata-rata fasa yang bertegangan; arus, daya, dan daya semu = total 3 fasa.</div>
  </div>`;
  $('#q').oninput = e => { st.q = e.target.value; renderRows(); };
  const addBtn = $('#btn-add');
  if (addBtn) addBtn.onclick = () => openForm(null);
}

function renderAll() {
  renderKpi(); renderTabs(); renderRows(); renderDrawer();
}

function renderKpi() {
  const ok = st.devices.filter(d => d.live);
  const kw = sum(ok.map(d => summary(d).kw)), kva = sum(ok.map(d => summary(d).kva));
  const enabled = st.devices.filter(d => d.enabled).length;
  $('#kpi').innerHTML = `
    <div class="card kpi elec"><div class="label">Titik ukur</div><div class="value">${st.devices.length}</div><div class="sub">${enabled} aktif</div></div>
    <div class="card kpi elec"><div class="label">Meter online</div><div class="value">${ok.length}<span class="unit">/ ${enabled}</span></div></div>
    <div class="card kpi elec"><div class="label">Total daya saat ini</div><div class="value">${nf(kw, 2)}<span class="unit">kW</span></div></div>
    <div class="card kpi elec"><div class="label">Total daya semu</div><div class="value">${nf(kva, 2)}<span class="unit">kVA</span></div></div>`;
}

const groupKey = value => String(value || '').trim().toLowerCase();
const areaKey = a => groupKey(a);
const siteKey = d => groupKey(d.site);

// Gabungkan nilai yang beda kapitalisasi (mis. "Kitchen" & "kitchen") jadi satu tab.
// Label yang dipakai = ejaan yang paling sering muncul (tie-break alfabetis).
// Dipakai untuk site (lokasi) dan area (bagian di dalam site).
function groupList(devices, field) {
  const groups = new Map();
  for (const d of devices) {
    const label = String(d[field] || '').trim();
    if (!label) continue;
    const key = label.toLowerCase();
    const g = groups.get(key) || { key, counts: new Map() };
    g.counts.set(label, (g.counts.get(label) || 0) + 1);
    groups.set(key, g);
  }
  return [...groups.values()].map(g => {
    const entries = [...g.counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    return { key: g.key, label: entries[0][0], count: entries.reduce((n, e) => n + e[1], 0) };
  }).sort((a, b) => a.label.localeCompare(b.label));
}

const siteGroups = () => groupList(st.devices, 'site');
const devicesOfSite = site => (site === 'all' ? st.devices : st.devices.filter(d => siteKey(d) === site));
const areaGroups = site => groupList(devicesOfSite(site), 'area');

function renderTabs() {
  const sites = siteGroups();
  if (st.site !== 'all' && !sites.some(g => g.key === st.site)) st.site = 'all';

  const siteSel = $('#site-select');
  if (siteSel) {
    siteSel.innerHTML = `<option value="all">Semua site (${st.devices.length})</option>` +
      sites.map(g => `<option value="${esc(g.key)}">${esc(g.label)} (${g.count})</option>`).join('');
    siteSel.value = st.site;
    siteSel.onchange = () => {
      st.site = siteSel.value;
      st.area = 'all';
      renderTabs(); renderRows();
    };
  }

  const groups = areaGroups(st.site);
  if (st.area !== 'all' && !groups.some(g => g.key === st.area)) st.area = 'all';
  const scope = devicesOfSite(st.site);
  const areaSel = $('#area-select');
  if (areaSel) {
    areaSel.innerHTML = `<option value="all">Semua area (${scope.length})</option>` +
      groups.map(g => `<option value="${esc(g.key)}">${esc(g.label)} (${g.count})</option>`).join('');
    areaSel.value = st.area;
    areaSel.onchange = () => { st.area = areaSel.value; renderRows(); };
  }
}

function renderRows() {
  const q = st.q.toLowerCase();
  const rows = st.devices.filter(d => (st.site === 'all' || siteKey(d) === st.site) &&
    (st.area === 'all' || areaKey(d.area) === st.area) &&
    (d.name + devId(d) + d.host + (d.site || '') + (d.area || '')).toLowerCase().includes(q));
  $('#rows').innerHTML = rows.length ? rows.map(d => {
    const s = summary(d), cell = (v, dec) => s && v != null ? nf(v, dec) : DASH;
    return `<tr class="click ${d.id === st.openId ? 'sel' : ''}" data-id="${d.id}"><td>${devId(d)}</td><td><strong>${esc(d.name)}</strong></td>
      <td>${esc(d.site) || DASH}</td><td>${esc(d.area) || DASH}</td><td>${MODEL_LABEL[d.model] || esc(d.model)}</td><td class="num slave">${d.unit_id}</td><td>${esc(d.host)}:${d.port}</td>
      <td class="num">${cell(s?.v, 1)}</td><td class="num">${cell(s?.a, 2)}</td>
      <td class="num"><strong>${cell(s?.kw, 2)}</strong></td><td class="num">${cell(s?.kva, 2)}</td>
      <td class="num">${cell(s?.freq, 2)}</td><td>${status(d)}</td>
      <td>${st.role === 'admin'
        ? '<button class="sm ghost" data-act="edit">Edit</button> <button class="sm danger" data-act="del">Hapus</button>'
        : ''}</td></tr>`;
  }).join('') : `<tr><td colspan="13" class="muted">${st.devices.length ? 'Tidak ada titik yang cocok.' : 'Belum ada perangkat. Klik “Tambah perangkat”.'}</td></tr>`;

  document.querySelectorAll('#rows tr.click').forEach(tr => {
    const d = st.devices.find(x => x.id === +tr.dataset.id);
    tr.onclick = () => { st.openId = d.id; $('#drawer').hidden = false; renderDrawer(); renderRows(); };
    const editBtn = tr.querySelector('[data-act=edit]');
    const delBtn = tr.querySelector('[data-act=del]');
    if (editBtn) editBtn.onclick = e => { e.stopPropagation(); openForm(d); };
    if (delBtn) delBtn.onclick = e => { e.stopPropagation(); removeDevice(d); };
  });
}

// ---------- right bar: parameter real-time ----------
function renderDrawer() {
  const d = st.devices.find(x => x.id === st.openId);
  if (!d) { $('#drawer').hidden = true; return; }
  const l = d.live, s = summary(d);
  $('#drawer-title').textContent = `${devId(d)} · ${d.name}`;

  const phaseRow = (label, unit, arr, total, dec) => `<tr><td>${label} <span class="muted small">${unit}</span></td>
    ${[0, 1, 2].map(k => `<td class="num">${l && arr[k] != null ? nf(arr[k], dec) : DASH}</td>`).join('')}
    <td class="num"><strong>${l && total != null ? nf(total, dec) : DASH}</strong></td></tr>`;
  const age = d.ts ? Math.max(0, Math.round(Date.now() / 1000 - d.ts)) : null;

  $('#drawer-body').innerHTML = `
    <div class="muted">${d.site ? esc(d.site) + ' · ' : ''}${esc(d.area) || 'Tanpa area'} · ${MODEL_LABEL[d.model] || esc(d.model)} · ${esc(d.host)}:${d.port} · Unit ${d.unit_id} · ${d.function === 'input' ? 'FC4' : 'FC3'}</div>
    <div class="mt">${status(d)}${!l && d.error && d.enabled ? `<div class="muted small mt">${esc(d.error)}</div>` : ''}</div>
    <div class="grid g3 mt">
      <div class="tile"><div class="muted small">Daya</div><div class="v">${s ? nf(s.kw, 2) : DASH} <span class="unit">kW</span></div></div>
      <div class="tile"><div class="muted small">Daya semu</div><div class="v">${s ? nf(s.kva, 2) : DASH} <span class="unit">kVA</span></div></div>
      <div class="tile"><div class="muted small">Frekuensi</div><div class="v">${s && s.freq != null ? nf(s.freq, 2) : DASH} <span class="unit">Hz</span></div></div>
    </div>
    <div class="card mt"><h3>Parameter real-time <span class="muted small">${age != null ? 'diperbarui ' + age + ' detik lalu' : ''}</span></h3>
      <div class="table-wrap"><table class="drawer-table">
        <thead><tr><th>Parameter</th><th class="num">L1</th><th class="num">L2</th><th class="num">L3</th><th class="num">Total</th></tr></thead>
        <tbody>
          ${phaseRow('Tegangan', 'V', l?.v ?? [], s?.v, 1)}
          ${phaseRow('Arus', 'A', l?.i ?? [], s && s.a, 3)}
          ${phaseRow('Daya aktif', 'W', l?.p ?? [], l?.p_total, 1)}
          ${phaseRow('Daya semu', 'VA', l?.s ?? [], l?.s_total, 1)}
        </tbody></table></div>
      <div class="row mt"><span class="muted">Frekuensi</span><strong>${l && l.freq != null ? nf(l.freq, 2) + ' Hz' : DASH}</strong></div>
      <div class="muted small mt">Total tegangan = rata-rata fasa yang bertegangan (&gt; ${PHASE_MIN_V} V).</div>
    </div>
    ${st.role === 'admin' ? '<div class="toolbar mt"><button class="ghost" id="dr-edit">Edit</button><button class="danger" id="dr-del">Hapus</button></div>' : ''}`;
  const drEdit = $('#dr-edit'), drDel = $('#dr-del');
  if (drEdit) drEdit.onclick = () => openForm(d);
  if (drDel) drDel.onclick = () => removeDevice(d);
}

$('#drawer-close').onclick = () => { st.openId = null; $('#drawer').hidden = true; renderRows(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#form-dlg').open) $('#drawer-close').click(); });

// ---------- CRUD ----------
const FORM_FIELDS = ['name', 'site', 'area', 'model', 'host', 'port', 'unit_id', 'function', 'byte_order', 'interval_s'];
const DEFAULTS = { name: '', site: '', area: '', model: 'pm5330', host: '', port: 502, unit_id: 1, function: 'holding', byte_order: 'big', interval_s: 2, enabled: true };
// Default pembacaan tiap model meter; dipakai saat model diganti di form.
const MODEL_DEFAULTS = { pm5330: { function: 'holding', byte_order: 'big' }, sdm630: { function: 'input', byte_order: 'big' } };
$('#f-model').onchange = e => Object.entries(MODEL_DEFAULTS[e.target.value]).forEach(([f, val]) => { $('#f-' + f).value = val; });

// Dropdown site di form perangkat: pilih site yang sudah ada, atau "＋ Site baru…".
function syncSiteNew() {
  const sel = $('#f-site'), box = $('#f-site-new');
  if (!sel || !box) return;
  const isNew = sel.value === '__new';
  box.hidden = !isNew;
  if (isNew) { box.value = ''; box.focus(); }
}

function renderSiteSelect(current) {
  const sel = $('#f-site');
  if (!sel) return;
  const value = String(current || '').trim();
  const known = siteGroups();
  const listed = known.some(g => g.key === value.toLowerCase());
  sel.innerHTML = '<option value="">(tanpa site)</option>' +
    known.map(g => `<option value="${esc(g.label)}">${esc(g.label)}</option>`).join('') +
    (value && !listed ? `<option value="${esc(value)}">${esc(value)}</option>` : '') +
    '<option value="__new">＋ Site baru…</option>';
  sel.value = value;
  syncSiteNew();
}

function siteFromForm() {
  const sel = $('#f-site');
  if (!sel) return '';
  return sel.value === '__new' ? $('#f-site-new').value.trim() : sel.value;
}

if ($('#f-site')) $('#f-site').onchange = syncSiteNew;

function openForm(d) {
  st.editId = d ? d.id : null;
  const v = d || DEFAULTS;
  $('#form-title').textContent = d ? `Edit ${devId(d)}` : 'Tambah perangkat';
  renderSiteSelect(v.site);
  FORM_FIELDS.forEach(f => { $('#f-' + f).value = v[f]; });
  $('#f-enabled').checked = !!v.enabled;
  $('#form-err').hidden = true;
  $('#form-dlg').showModal();
}

$('#form-cancel').onclick = () => $('#form-dlg').close();
$('#form').onsubmit = async e => {
  e.preventDefault();
  const body = Object.fromEntries(FORM_FIELDS.map(f => [f, $('#f-' + f).value]));
  body.site = siteFromForm();   // dropdown bisa bernilai "__new" -> ambil dari input
  body.enabled = $('#f-enabled').checked;
  try {
    if (st.editId) await api('/api/devices/' + st.editId, { method: 'PUT', body });
    else st.openId = (await api('/api/devices', { method: 'POST', body })).id;
    $('#form-dlg').close();
    await load();
    if (st.openId) $('#drawer').hidden = false;
    renderAll();
  } catch (err) {
    $('#form-err').textContent = err.message; $('#form-err').hidden = false;
  }
};

async function removeDevice(d) {
  if (!confirm(`Hapus ${devId(d)} · ${d.name}? Konfigurasi perangkat akan hilang.`)) return;
  try {
    await api('/api/devices/' + d.id, { method: 'DELETE' });
    if (st.openId === d.id) st.openId = null;
    await load(); renderAll();
  } catch (err) { alert('Gagal menghapus: ' + err.message); }
}

// ---------- halaman Status Database (read-only) ----------
// Koneksi MySQL diatur di backend (database.json via SETUP.bat); dashboard hanya menampilkan statusnya.
const loadDbStatus = async () => { const r = await api('/api/db-status'); st.dbConfig = r.config; st.dbStatus = r.status; };

function buildDatabase() {
  $('#page').innerHTML = `
  <div class="grid g4" id="db-kpi"></div>
  <div class="card mt">
    <div class="muted small">Koneksi MySQL diatur di sisi server (file <code>database.json</code>, dibuat oleh <code>SETUP.bat</code>)
      dan tidak bisa diubah dari dashboard. Untuk mengganti server/user/password, jalankan <code>SETUP.bat</code> lagi lalu restart server.
      Pembacaan semua meter disimpan ke tabel <code>power_readings</code> sesuai interval. Selama server database tidak terjangkau,
      data ditahan di buffer lokal dan dikirim otomatis saat pulih.</div>
  </div>`;
}

function dbStatusPill(c, s) {
  if (!c) return '<span class="pill off">Belum dikonfigurasi</span>';
  if (s.state === 'ok') return '<span class="pill ok">Terhubung · menyimpan</span>';
  if (s.state === 'error') return `<span class="pill bad" title="${esc(s.error || '')}">Gagal</span>`;
  return '<span class="pill warn">Menghubungkan…</span>';
}

function renderDb() {
  const c = st.dbConfig, s = st.dbStatus;
  const last = s.last_write ? new Date(s.last_write * 1000).toLocaleTimeString('id-ID') : DASH;
  $('#db-kpi').innerHTML = `
    <div class="card kpi elec"><div class="label">Server database</div><div class="value" style="font-size:20px">${c ? esc(c.host) + ':' + c.port : 'Belum diatur'}</div>
      <div class="sub">${c ? esc(c.db_name) + ' · user ' + esc(c.db_user) + ' · simpan tiap ' + nf(c.store_interval_s, c.store_interval_s % 1 ? 1 : 0) + ' dtk' : 'Jalankan SETUP.bat di server'}</div></div>
    <div class="card kpi elec"><div class="label">Status</div><div class="value" style="font-size:20px">${dbStatusPill(c, s)}</div>
      <div class="sub">${s.state === 'error' ? esc(s.error) : '&nbsp;'}</div></div>
    <div class="card kpi elec"><div class="label">Terakhir menyimpan</div><div class="value" style="font-size:22px">${last}</div></div>
    <div class="card kpi elec"><div class="label">Baris tersimpan</div><div class="value">${nf(s.rows_total || 0)}</div><div class="sub">${s.pending ? nf(s.pending) + ' menunggu terkirim' : 'sejak server dijalankan'}</div></div>`;
}

async function refreshDb() { await loadDbStatus(); renderDb(); }

// ---------- halaman Akun ----------
const loadUsers = async () => { st.users = await api('/api/users'); };

function buildAccounts() {
  $('#page').innerHTML = `
  <div class="card">
    <div class="toolbar"><h3 style="margin:0">Akun dashboard</h3></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Username</th><th>Role</th><th></th></tr></thead>
      <tbody id="user-rows"></tbody>
    </table></div>
    <div class="muted small mt">Password disimpan sebagai hash di <code>meters.db</code>. Setelah password diubah, sesi lama akun itu otomatis berakhir.</div>
  </div>`;
}

function renderAccounts() {
  $('#user-rows').innerHTML = st.users.length ? st.users.map(u => `<tr data-id="${u.id}">
    <td><strong>${esc(u.username)}</strong></td>
    <td>${u.role === 'admin' ? '<span class="pill ok">admin</span>' : '<span class="pill off">user</span>'}</td>
    <td><button class="sm ghost" data-act="pw">Ubah password</button></td></tr>`).join('')
    : '<tr><td colspan="3" class="muted">Belum ada akun.</td></tr>';

  document.querySelectorAll('#user-rows tr[data-id]').forEach(tr => {
    const u = st.users.find(x => x.id === +tr.dataset.id);
    tr.querySelector('[data-act=pw]').onclick = () => openAccForm(u);
  });
}

async function refreshAccounts() { await loadUsers(); renderAccounts(); }

function openAccForm(u) {
  st.editUserId = u.id;
  $('#acc-title').textContent = `Ubah password “${u.username}”`;
  $('#a-password').value = '';
  $('#acc-err').hidden = true;
  $('#acc-dlg').showModal();
  $('#a-password').focus();
}
$('#acc-cancel').onclick = () => $('#acc-dlg').close();
$('#acc-form').onsubmit = async e => {
  e.preventDefault();
  try {
    await api('/api/users/' + st.editUserId, { method: 'PUT', body: { password: $('#a-password').value } });
    $('#acc-dlg').close();
    await refreshAccounts();
  } catch (err) {
    const el = $('#acc-err'); el.textContent = err.message; el.hidden = false;
  }
};

// ---------- halaman Dashboard ----------
const DASH_METRICS = {
  kw: { label: 'Daya', unit: 'kW', dec: 2 },
  kva: { label: 'Daya semu', unit: 'kVA', dec: 2 },
  v: { label: 'Tegangan', unit: 'V', dec: 1 },
  a: { label: 'Arus', unit: 'A', dec: 2 },
};

// Periode grafik tren. 'live' = 90 sampel terakhir dari trend_samples;
// sisanya agregasi riwayat MySQL (power_readings) per jam/hari.
const DASH_PERIODS = {
  live: { label: 'Realtime' },
  daily: { label: 'Harian', note: 'per jam' },
  weekly: { label: 'Mingguan', note: 'per hari' },
  monthly: { label: 'Bulanan', note: 'per hari' },
};

// Ringkasan satu kelompok area. Tegangan & frekuensi = rata-rata antar meter; daya & arus = total.
function areaStats(devs) {
  const stats = devs.filter(d => d.live).map(summary).filter(Boolean);
  const vs = stats.map(s => s.v).filter(v => v != null);
  const fs = stats.map(s => s.freq).filter(f => f != null);
  return {
    kw: sum(stats.map(s => s.kw)),
    kva: sum(stats.map(s => s.kva)),
    a: sum(stats.map(s => s.a)),
    v: vs.length ? sum(vs) / vs.length : null,
    freq: fs.length ? sum(fs) / fs.length : null,
    online: stats.length,
    total: devs.length,
  };
}

const areaDevices = (key, site = st.dashSite) => {
  const base = devicesOfSite(site);
  return key === 'all' ? base : base.filter(d => areaKey(d.area) === key);
};

function buildDashboard() {
  $('#page').innerHTML = `
  <div class="toolbar" id="dash-site-bar">
    <label class="pick-label" for="dash-site">Site</label>
    <select id="dash-site" class="pick"></select>
  </div>
  <div class="grid g4" id="dash-kpi"></div>
  <div class="grid g2 mt">
    <div class="card">
      <h3><span id="dash-trend-title">Tren</span><span class="muted small" id="dash-trend-sub">90 sampel terakhir</span></h3>
      <div class="tabs" id="dash-period" style="margin-bottom:8px"></div>
      <div class="chart-wrap"><canvas id="dash-trend"></canvas></div>
      <div class="muted small mt" id="dash-trend-summary"></div>
    </div>
    <div class="card">
      <h3>Perbandingan area <span class="muted small" id="dash-bar-unit"></span></h3>
      <div id="dash-bar"></div>
    </div>
  </div>
  <div class="card mt">
    <div class="toolbar">
      <h3 style="margin:0">Ringkasan per area</h3>
      <span style="margin-left:auto"></span>
      <div class="tabs" id="dash-metric"></div>
    </div>
    <div class="dash-areas" id="dash-areas"></div>
    <div class="muted small mt">Klik kartu area untuk melihat rincian meter di bawah; klik lagi untuk kembali ke semua area.</div>
  </div>
  <div class="card mt">
    <h3 id="dash-detail-title">Rincian semua area</h3>
    <div class="table-wrap"><table class="wide">
      <thead><tr><th>ID</th><th>Titik ukur</th><th>Site</th><th>Area</th><th class="num">ID Slave</th><th class="num">Tegangan (V)</th><th class="num">Arus (A)</th>
        <th class="num">Daya (kW)</th><th class="num">Daya semu (kVA)</th><th class="num">Frekuensi (Hz)</th><th>Status</th></tr></thead>
      <tbody id="dash-rows"></tbody>
    </table></div>
  </div>`;
}

function renderDashKpi() {
  const s = areaStats(devicesOfSite(st.dashSite));
  $('#dash-kpi').innerHTML = `
    <div class="card kpi elec"><div class="label">Total daya</div><div class="value">${nf(s.kw, 2)}<span class="unit">kW</span></div>
      <div class="sub">${s.online} dari ${s.total} meter online</div></div>
    <div class="card kpi elec"><div class="label">Total daya semu</div><div class="value">${nf(s.kva, 2)}<span class="unit">kVA</span></div>
      <div class="sub">seluruh area</div></div>
    <div class="card kpi elec"><div class="label">Tegangan rata-rata</div><div class="value">${s.v == null ? DASH : nf(s.v, 1)}<span class="unit">V</span></div>
      <div class="sub">fasa bertegangan</div></div>
    <div class="card kpi elec"><div class="label">Total arus</div><div class="value">${nf(s.a, 2)}<span class="unit">A</span></div>
      <div class="sub">${s.freq == null ? 'frekuensi —' : 'frekuensi ' + nf(s.freq, 2) + ' Hz'}</div></div>`;
}

function renderDashAreas() {
  const sites = siteGroups();
  if (st.dashSite !== 'all' && !sites.some(g => g.key === st.dashSite)) st.dashSite = 'all';
  const siteSel = $('#dash-site');
  if (siteSel) {
    siteSel.innerHTML = `<option value="all">Semua site (${st.devices.length})</option>` +
      sites.map(g => `<option value="${esc(g.key)}">${esc(g.label)} (${g.count})</option>`).join('');
    siteSel.value = st.dashSite;
    siteSel.onchange = () => {
      st.dashSite = siteSel.value;
      st.dashArea = 'all';
      st.trendFetchedAt = 0;
      renderDashKpi(); renderDashAreas(); renderDashCharts(true); renderDashDetail();
    };
  }
  const groups = areaGroups(st.dashSite);
  if (st.dashArea !== 'all' && !groups.some(g => g.key === st.dashArea)) st.dashArea = 'all';
  $('#dash-metric').innerHTML = Object.entries(DASH_METRICS).map(([k, m]) =>
    `<button data-m="${k}" class="${st.dashMetric === k ? 'on' : ''}">${m.label}</button>`).join('');
  $('#dash-metric').querySelectorAll('button').forEach(b => b.onclick = () => {
    st.dashMetric = b.dataset.m;
    renderDashAreas(); renderDashCharts(true);
  });

  const periodBox = $('#dash-period');
  if (periodBox) {
    periodBox.innerHTML = Object.entries(DASH_PERIODS).map(([k, p]) =>
      `<button data-p="${k}" class="${st.trendPeriod === k ? 'on' : ''}">${p.label}</button>`).join('');
    periodBox.querySelectorAll('button').forEach(b => b.onclick = () => {
      if (st.trendPeriod === b.dataset.p) return;
      st.trendPeriod = b.dataset.p;
      st.trendFetchedAt = 0;
      renderDashAreas(); renderDashCharts(true);
    });
  }

  const m = DASH_METRICS[st.dashMetric];
  const stats = groups.map(g => ({ ...g, s: areaStats(areaDevices(g.key)) }));
  const max = Math.max(1e-9, ...stats.map(x => Math.abs(x.s[st.dashMetric] ?? 0)));
  $('#dash-areas').innerHTML = stats.length ? stats.map(x => {
    const val = x.s[st.dashMetric];
    const pct = Math.max(0, Math.min(100, Math.abs(val ?? 0) / max * 100));
    return `<button class="area-card ${st.dashArea === x.key ? 'on' : ''}" data-k="${esc(x.key)}">
      <div class="area-head"><strong>${esc(x.label)}</strong><span class="muted small">${x.s.online}/${x.s.total} online</span></div>
      <div class="area-main">${val == null ? DASH : nf(val, m.dec)} <span class="unit">${m.unit}</span></div>
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div class="area-sub muted small">kW ${nf(x.s.kw, 2)} · kVA ${nf(x.s.kva, 2)} · V ${x.s.v == null ? DASH : nf(x.s.v, 1)} · A ${nf(x.s.a, 2)}</div>
    </button>`;
  }).join('') : '<div class="muted">Belum ada area. Tambahkan perangkat dengan area terisi.</div>';

  $('#dash-areas').querySelectorAll('.area-card').forEach(el => el.onclick = () => {
    st.dashArea = st.dashArea === el.dataset.k ? 'all' : el.dataset.k;
    renderDashAreas(); renderDashCharts(true); renderDashDetail();
  });
}

// ---------- grafik (canvas/SVG murni, tanpa library) ----------
// Deret tren dibaca dari database lewat /api/trend: semua browser melihat data yang
// sama, dan grafik langsung terisi saat halaman dibuka (tidak menunggu akumulasi).
const TREND_POINTS = 90;

async function loadTrend(force = false) {
  if (st.trendPeriod === 'live') {
    const query = new URLSearchParams({
      area: st.dashArea, site: st.dashSite, metric: st.dashMetric, limit: String(TREND_POINTS),
    });
    const result = await api('/api/trend?' + query.toString());
    st.trendMeta = { interval: result.interval_s };
    st.trendPoints = result.points || [];
    st.trendLabels = [];
    st.trendSummary = null;
    st.trendError = null;
    return;
  }
  // Riwayat harian/bulanan tidak perlu di-query tiap 2 detik: cache 60 detik,
  // dan dipaksa ambil ulang saat periode/metrik/area diganti.
  if (!force && st.trendFetchedAt && Date.now() - st.trendFetchedAt < 60000) return;
  const query = new URLSearchParams({
    period: st.trendPeriod, metric: st.dashMetric, area: st.dashArea, site: st.dashSite,
  });
  const result = await api('/api/history?' + query.toString());
  st.trendPoints = result.points || [];
  st.trendLabels = result.labels || [];
  st.trendSummary = result.summary || null;
  st.trendMeta = { interval: null, unit: result.unit, bucket: result.bucket };
  st.trendError = null;
  st.trendFetchedAt = Date.now();
}

function drawTrendChart() {
  const canvas = $('#dash-trend');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(240, rect.width), h = Math.max(140, rect.height || 220);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const m = DASH_METRICS[st.dashMetric];
  const labels = st.trendLabels || [];
  const series = (st.trendPoints || []).map(p => ({
    t: p.t, v: (p.v != null && isFinite(p.v)) ? p.v : null,
  }));
  const values = series.filter(p => p.v != null).map(p => p.v);
  const pad = { l: 52, r: 12, t: 12, b: labels.length ? 28 : 24 };
  const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;

  if (!values.length) {
    g.fillStyle = '#5b6b7f'; g.font = '12px Segoe UI'; g.textAlign = 'center';
    g.fillText(st.trendError || 'Belum ada data untuk periode ini…', w / 2, h / 2);
    g.textAlign = 'start';
    return;
  }

  let min = Math.min(...values), max = Math.max(...values);
  if (max - min < 1e-9) { max += Math.abs(max) * 0.05 || 1; min -= Math.abs(min) * 0.05 || 1; }
  const span = max - min, lo = min - span * 0.08, hi = max + span * 0.08;
  const x = i => pad.l + (series.length === 1 ? cw / 2 : cw * i / (series.length - 1));
  const y = v => pad.t + ch * (1 - (v - lo) / (hi - lo));

  g.strokeStyle = '#e6ecf3'; g.fillStyle = '#5b6b7f'; g.font = '11px Segoe UI'; g.lineWidth = 1;
  for (let k = 0; k <= 4; k++) {
    const yy = pad.t + ch * k / 4;
    g.beginPath(); g.moveTo(pad.l, yy); g.lineTo(pad.l + cw, yy); g.stroke();
    g.fillText(nf(hi - (hi - lo) * k / 4, m.dec), 6, yy + 3);
  }

  // Pecah garis di titik kosong supaya bucket tanpa data tampil sebagai celah,
  // bukan disambung lurus (penting untuk harian/mingguan/bulanan yang bolong).
  const runs = [];
  let run = [];
  series.forEach((p, i) => {
    if (p.v == null) { if (run.length) runs.push(run); run = []; }
    else run.push({ i, v: p.v });
  });
  if (run.length) runs.push(run);

  const grad = g.createLinearGradient(0, pad.t, 0, pad.t + ch);
  grad.addColorStop(0, 'rgba(13,127,115,.22)');
  grad.addColorStop(1, 'rgba(13,127,115,0)');
  runs.forEach(seg => {
    if (seg.length < 2) return;
    g.beginPath();
    seg.forEach((p, k) => { const px = x(p.i), py = y(p.v); k ? g.lineTo(px, py) : g.moveTo(px, py); });
    g.lineTo(x(seg[seg.length - 1].i), pad.t + ch);
    g.lineTo(x(seg[0].i), pad.t + ch);
    g.closePath(); g.fillStyle = grad; g.fill();
  });

  g.strokeStyle = '#0d7f73'; g.lineWidth = 2;
  runs.forEach(seg => {
    if (seg.length === 1) {
      // Bucket tunggal (mis. hanya 1 hari berisi data dalam sebulan) tetap harus
      // terlihat - tanpa ini titiknya tidak tergambar sama sekali.
      const only = seg[0];
      g.beginPath(); g.arc(x(only.i), y(only.v), 3, 0, Math.PI * 2);
      g.fillStyle = '#0d7f73'; g.fill();
      return;
    }
    g.beginPath();
    seg.forEach((p, k) => { const px = x(p.i), py = y(p.v); k ? g.lineTo(px, py) : g.moveTo(px, py); });
    g.stroke();
  });

  const lastRun = runs[runs.length - 1];
  if (lastRun && lastRun.length) {
    const last = lastRun[lastRun.length - 1];
    g.beginPath(); g.arc(x(last.i), y(last.v), 3.5, 0, Math.PI * 2);
    g.fillStyle = '#0d7f73'; g.fill();
  }

  if (labels.length) {
    g.fillStyle = '#5b6b7f'; g.font = '10px Segoe UI'; g.textAlign = 'center';
    const step = Math.max(1, Math.ceil(labels.length / 7));
    labels.forEach((label, i) => {
      if (i % step === 0 || i === labels.length - 1) g.fillText(label, x(i), h - 7);
    });
    g.textAlign = 'start';
  } else {
    g.fillStyle = '#5b6b7f'; g.font = '11px Segoe UI'; g.textAlign = 'right';
    g.fillText(`${values.length} sampel · ${m.unit}`, w - pad.r, h - 6);
    g.textAlign = 'start';
  }
}

function renderAreaBars(stats) {
  const box = $('#dash-bar');
  if (!box) return;
  const m = DASH_METRICS[st.dashMetric];
  const unit = $('#dash-bar-unit');
  if (unit) unit.textContent = m.unit;
  if (!stats.length) { box.innerHTML = '<div class="muted small">Belum ada area.</div>'; return; }

  const max = Math.max(1e-9, ...stats.map(x => Math.abs(x.s[st.dashMetric] ?? 0)));
  const rowH = 28, labelW = 120, barMax = 380, valueW = 64;
  const width = labelW + barMax + valueW, height = stats.length * rowH + 6;
  box.innerHTML = `<svg viewBox="0 0 ${width} ${height}" class="bar-svg" role="img">
    ${stats.map((x, i) => {
    const val = x.s[st.dashMetric];
    const len = Math.max(0, Math.min(1, Math.abs(val ?? 0) / max)) * barMax;
    const yy = i * rowH + 4;
    return `<g data-k="${esc(x.key)}">
        <title>${esc(x.label)}: ${val == null ? DASH : nf(val, m.dec) + ' ' + m.unit}</title>
        <text x="0" y="${yy + 16}" class="bar-label">${esc(x.label)}</text>
        <rect x="${labelW}" y="${yy + 4}" width="${barMax}" height="18" rx="4" class="bar-track"/>
        <rect x="${labelW}" y="${yy + 4}" width="${len}" height="18" rx="4" class="bar-fill"/>
        <text x="${labelW + barMax + 8}" y="${yy + 17}" class="bar-value">${val == null ? DASH : nf(val, m.dec)}</text>
      </g>`;
  }).join('')}
  </svg>`;

  box.querySelectorAll('g[data-k]').forEach(el => el.onclick = () => {
    st.dashArea = st.dashArea === el.dataset.k ? 'all' : el.dataset.k;
    renderDashAreas(); renderDashCharts(true); renderDashDetail();
  });
}

function trendSubtitle(m) {
  if (st.trendPeriod === 'live') {
    return st.trendMeta && st.trendMeta.interval
      ? `${TREND_POINTS} sampel · tiap ${Number(st.trendMeta.interval)}s`
      : `${TREND_POINTS} sampel terakhir`;
  }
  const p = DASH_PERIODS[st.trendPeriod];
  return `${p.label} · ${p.note} (${m.unit})`;
}

function renderDashTrendSummary() {
  const box = $('#dash-trend-summary');
  if (!box) return;
  if (st.trendPeriod === 'live') { box.textContent = ''; return; }
  if (st.trendError) { box.textContent = st.trendError; return; }
  const s = st.trendSummary;
  if (!s) { box.textContent = ''; return; }
  const m = DASH_METRICS[st.dashMetric];
  box.textContent = [
    `Rata-rata ${s.avg == null ? DASH : nf(s.avg, m.dec)} ${m.unit}`,
    `Puncak ${s.peak == null ? DASH : nf(s.peak, m.dec)} ${m.unit}`,
    `${s.meters} meter`,
    `${s.with_data}/${s.buckets} bucket ada data`,
  ].join(' · ');
}

function drawDashCharts() {
  const m = DASH_METRICS[st.dashMetric];
  const site = siteGroups().find(x => x.key === st.dashSite);
  const g = areaGroups(st.dashSite).find(x => x.key === st.dashArea);
  const scope = [
    st.dashSite === 'all' ? '' : (site ? site.label : st.dashSite),
    st.dashArea === 'all' ? 'semua area' : (g ? g.label : st.dashArea),
  ].filter(Boolean).join(' · ');
  const title = $('#dash-trend-title');
  if (title) title.textContent = `Tren ${m.label.toLowerCase()} — ${scope}`;
  const sub = $('#dash-trend-sub');
  if (sub) sub.textContent = trendSubtitle(m);
  drawTrendChart();
  renderDashTrendSummary();
  renderAreaBars(areaGroups(st.dashSite).map(gr => ({ ...gr, s: areaStats(areaDevices(gr.key)) })));
}

// Ambil deret sesuai periode lalu gambar ulang. Periode 'live' memakai
// /api/trend (90 sampel terakhir); periode lain memakai /api/history dari MySQL.
async function renderDashCharts(force = false) {
  try {
    await loadTrend(force);
  } catch (err) {
    st.trendPoints = [];
    st.trendLabels = [];
    st.trendSummary = null;
    st.trendError = st.trendPeriod === 'live' ? null : err.message;
    // Tetap pakai cache 60 detik saat gagal, supaya MySQL yang mati tidak
    // dihujani query baru tiap 2 detik.
    st.trendFetchedAt = Date.now();
  }
  drawDashCharts();
}

function renderDashDetail() {
  const site = siteGroups().find(x => x.key === st.dashSite);
  const g = areaGroups(st.dashSite).find(x => x.key === st.dashArea);
  const devs = areaDevices(st.dashArea);
  const where = [
    st.dashSite === 'all' ? '' : (site ? site.label : st.dashSite),
    st.dashArea === 'all' ? '' : (g ? g.label : st.dashArea),
  ].filter(Boolean).join(' · ');
  $('#dash-detail-title').textContent = `Rincian ${where || 'semua site'}`;
  $('#dash-rows').innerHTML = devs.length ? devs.map(d => {
    const s = summary(d), cell = (v, dec) => s && v != null ? nf(v, dec) : DASH;
    return `<tr><td>${devId(d)}</td><td><strong>${esc(d.name)}</strong></td><td>${esc(d.site) || DASH}</td><td>${esc(d.area) || DASH}</td><td class="num slave">${d.unit_id}</td>
      <td class="num">${cell(s?.v, 1)}</td><td class="num">${cell(s?.a, 2)}</td>
      <td class="num"><strong>${cell(s?.kw, 2)}</strong></td><td class="num">${cell(s?.kva, 2)}</td>
      <td class="num">${cell(s?.freq, 2)}</td><td>${status(d)}</td></tr>`;
  }).join('') : '<tr><td colspan="11" class="muted">Tidak ada perangkat pada pilihan ini.</td></tr>';
}

async function refreshDashboard() {
  await load();
  renderDashKpi();
  renderDashAreas();
  await renderDashCharts();
  renderDashDetail();
}

// ---------- router ----------
const PAGES = {
  dashboard: { title: 'Dashboard', build: buildDashboard, refresh: refreshDashboard, first: refreshDashboard },
  listrik: {
    title: 'Listrik', build: buildPage, refresh: async () => { await load(); renderKpi(); renderRows(); renderDrawer(); },
    first: async () => { await load(); renderAll(); }
  },
  database: { title: 'Status Database', build: buildDatabase, refresh: refreshDb, first: refreshDb },
  akun: { title: 'Akun', build: buildAccounts, refresh: refreshAccounts, first: refreshAccounts },
};

async function go() {
  const page = (location.hash || '#dashboard').slice(1);
  st.page = PAGES[page] ? page : 'dashboard';
  if ((st.page === 'database' || st.page === 'akun') && st.role !== 'admin') { location.hash = '#dashboard'; return; }
  const p = PAGES[st.page];
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('active', a.dataset.page === st.page));
  $('#page-title').textContent = p.title;
  st.openId = null;
  $('#drawer').hidden = true;
  p.build();
  try { await p.first(); } catch (e) { showBackendError(e); }
}

function showBackendError(e) {
  $('#page').innerHTML = `<div class="card"><h3>Tidak dapat terhubung ke backend</h3><p class="muted">${esc(e.message)}</p>
    <p>Jalankan <code>python server.py</code> di folder <code>dashboard</code>.</p></div>`;
}

// ---------- start ----------
setInterval(() => { $('#clock').textContent = new Date().toLocaleString('id-ID', { dateStyle: 'medium', timeStyle: 'medium' }); }, 1000);
window.addEventListener('hashchange', go);
window.addEventListener('resize', () => { if (st.page === 'dashboard') drawDashCharts(); });
$('#data-source').textContent = 'Sumber: Modbus TCP langsung';

// ---------- menu samping (tablet/mobile) ----------
const menuBtn = $('#menu-toggle');
const navOverlay = $('#nav-overlay');
function setNav(open) {
  document.body.classList.toggle('nav-open', open);
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) sidebar.classList.toggle('open', open);
  if (menuBtn) menuBtn.setAttribute('aria-expanded', String(open));
}
if (menuBtn) menuBtn.onclick = () => setNav(!document.body.classList.contains('nav-open'));
if (navOverlay) navOverlay.onclick = () => setNav(false);
document.querySelectorAll('nav a').forEach(a => a.addEventListener('click', () => setNav(false)));
document.addEventListener('keydown', e => { if (e.key === 'Escape') setNav(false); });
window.addEventListener('resize', () => { if (window.innerWidth > 820) setNav(false); });

// ---------- mode layar penuh ----------
// Saat fullscreen, body diberi kelas fs-mode: sidebar jadi off-canvas (tersembunyi)
// dan tombol hamburger muncul untuk membukanya kembali.
const fsBtn = $('#btn-fullscreen');
const fsLabel = $('#fs-label');

function syncFullscreenUi() {
  const active = !!document.fullscreenElement;
  document.body.classList.toggle('fs-mode', active);
  if (fsBtn) {
    fsBtn.setAttribute('aria-pressed', String(active));
    fsBtn.title = active ? 'Keluar layar penuh'
      : 'Mode layar penuh: sidebar disembunyikan, tombol menu muncul';
  }
  if (fsLabel) fsLabel.textContent = active ? 'Keluar layar penuh' : 'Layar penuh';
  if (!active) setNav(false);
}

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch (err) {
    toast('fs', 'Mode layar penuh tidak tersedia: ' + (err.message || 'ditolak browser'), 'warn');
  }
}

if (fsBtn) fsBtn.onclick = toggleFullscreen;
document.addEventListener('fullscreenchange', syncFullscreenUi);
syncFullscreenUi();

const logoutBtn = $('#logout');
if (logoutBtn) logoutBtn.onclick = async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch { /* sesi mungkin sudah habis */ }
  setToken('');
  location.href = '/login.html';
};

async function init() {
  try {
    const me = await api('/api/me');
    st.role = me.role; st.user = me.username;
  } catch { return; } // 401 sudah diarahkan ke login oleh api()
  document.querySelectorAll('[data-admin-only]').forEach(el => { el.hidden = st.role !== 'admin'; });
  const who = $('#whoami');
  if (who) who.textContent = `${st.user} (${st.role})`;
  go();
}

// ---------- pemantauan koneksi ----------
let lastDbState = null;
function watchDb(db) {
  if (!db) return;
  if (db.state === 'error') {
    toast('db', 'Koneksi database terputus: ' + (db.error || 'tidak diketahui') + '. Data ditahan di buffer lokal.', 'bad', true);
  } else if (db.state === 'ok') {
    if (lastDbState === 'error') toast('db-ok', 'Koneksi database pulih. Data buffer dikirim ulang.', 'ok');
    clearToast('db');
  } else {
    clearToast('db');
  }
  lastDbState = db.state;
}

let statusFails = 0;
async function monitorStatus() {
  if (!st.user) return; // belum login / sesi masih dimuat
  try {
    const s = await api('/api/status');
    statusFails = 0;
    clearToast('net');
    watchDb(s.db);
  } catch (e) {
    if (e.message === 'belum login') return;
    statusFails += 1;
    if (statusFails >= 2) toast('net', 'Tidak dapat terhubung ke server dashboard. Mencoba lagi…', 'bad', true);
  }
}

init();
setInterval(monitorStatus, 3000);
setInterval(async () => {
  if (!st.user) return; // belum login / masih memuat sesi
  if (document.querySelector('dialog[open]')) return; // jangan ganggu form yang sedang diisi
  try { await PAGES[st.page].refresh(); } catch { /* backend sementara putus; coba lagi pada siklus berikutnya */ }
}, 2000);
