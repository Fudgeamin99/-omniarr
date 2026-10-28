/* Omniarr web UI — no build step, no external libraries. */
'use strict';

// ------------------------------------------------------------------ core ---
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = (s) => new Raw(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function val(v) {
  if (v === null || v === undefined || v === false) return '';
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(val).join('');
  return esc(v);
}
function html(strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => { out += s; if (i < vals.length) out += val(vals[i]); });
  return new Raw(out);
}

const S = {
  route: { parts: [], query: new URLSearchParams() },
  status: null, v: {}, ui: {}, form: {}, busy: {}, toasts: [], modal: null, needLogin: false, authEnabled: false,
};
const A = {};          // action handlers
const VIEWS = {};      // view definitions

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (res.status === 401 && data.code === 'auth') {
    S.needLogin = true; render();
    throw new Error('Please log in');
  }
  if (!res.ok) { const e = new Error(data.error || `HTTP ${res.status}`); e.data = data; throw e; }
  return data;
}

function getPath(obj, path) { return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj); }
function setPath(obj, path, value) {
  const keys = path.split('.'); let o = obj;
  keys.slice(0, -1).forEach((k) => { if (o[k] == null || typeof o[k] !== 'object') o[k] = {}; o = o[k]; });
  o[keys[keys.length - 1]] = value;
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const clone = (x) => JSON.parse(JSON.stringify(x));

// ---------------------------------------------------------------- morphing --
function patch(root, markup) {
  const tpl = document.createElement('template');
  tpl.innerHTML = String(markup);
  morphChildren(root, tpl.content);
}
function keyOf(n) { return n && n.nodeType === 1 ? n.getAttribute('data-key') : null; }
function morphChildren(from, to) {
  const toKids = Array.from(to.childNodes);
  const keyed = new Map();
  Array.from(from.childNodes).forEach((n) => { const k = keyOf(n); if (k) keyed.set(k, n); });
  let i = 0;
  for (const t of toKids) {
    let f = from.childNodes[i];
    const k = keyOf(t);
    if (k) {
      const match = keyed.get(k);
      if (match) {
        keyed.delete(k);
        if (match !== f) { from.insertBefore(match, f || null); f = match; }
      } else { from.insertBefore(t, f || null); i++; continue; }
    }
    if (!f) { from.appendChild(t); i++; continue; }
    if (f.nodeType !== t.nodeType || f.nodeName !== t.nodeName || keyOf(f) !== k) {
      from.replaceChild(t, f); i++; continue;
    }
    morphNode(f, t); i++;
  }
  while (from.childNodes.length > i) from.removeChild(from.lastChild);
}
function morphNode(f, t) {
  if (f.nodeType === 3 || f.nodeType === 8) { if (f.nodeValue !== t.nodeValue) f.nodeValue = t.nodeValue; return; }
  for (const a of Array.from(f.attributes)) if (!t.hasAttribute(a.name)) f.removeAttribute(a.name);
  for (const a of Array.from(t.attributes)) if (f.getAttribute(a.name) !== a.value) f.setAttribute(a.name, a.value);
  const focused = document.activeElement === f;
  if (f.tagName === 'INPUT') {
    if (f.type === 'checkbox' || f.type === 'radio') f.checked = t.hasAttribute('checked');
    else if (!focused) { const v = t.getAttribute('value') ?? ''; if (f.value !== v) f.value = v; }
    return;
  }
  if (f.tagName === 'TEXTAREA') { if (!focused && f.value !== t.textContent) f.value = t.textContent; return; }
  if (f.tagName === 'SELECT') {
    morphChildren(f, t);
    if (!focused) {
      const o = t.querySelector('option[selected]') || t.querySelector('option');
      const v = o ? (o.getAttribute('value') ?? o.textContent) : '';
      if (f.value !== v) f.value = v;
    }
    return;
  }
  if (f.tagName === 'IMG' && f.getAttribute('src') === t.getAttribute('src')) return;
  morphChildren(f, t);
}

// ------------------------------------------------------------------ format --
function fmtBytes(n) {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; let x = n;
  while (x >= 1000 && i < u.length - 1) { x /= 1000; i++; }
  return `${x >= 100 || i === 0 ? Math.round(x) : x.toFixed(1)} ${u[i]}`;
}
function fmtAgo(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 172800) return 'Yesterday';
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} days ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}
function fmtAge(ts) {
  if (!ts) return '—';
  const d = (Date.now() / 1000 - ts) / 86400;
  if (d < 1) return `${Math.max(1, Math.round(d * 24))} h`;
  if (d < 60) return `${Math.round(d)} d`;
  if (d < 730) return `${Math.round(d / 30)} mo`;
  return `${Math.round(d / 365)} y`;
}
function fmtDate(iso, opts) {
  if (!iso) return 'TBA';
  const d = new Date(iso.length === 10 ? iso + 'T12:00:00' : iso);
  return d.toLocaleDateString(undefined, opts || { day: 'numeric', month: 'short', year: 'numeric' });
}
function dayParts(iso) {
  const d = new Date(iso + 'T12:00:00');
  return { day: d.toLocaleDateString(undefined, { weekday: 'short' }), date: d.getDate() };
}
function fmtEta(s) {
  if (s == null || s < 0 || s >= 8640000) return '—';
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  return `${Math.round(s / 86400)} d`;
}
const pct = (p) => `${Math.round((p || 0) * 100)}%`;
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const stripTags = (s) => String(s || '').replace(/<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, '').replace(/&amp;/g, '&');

// ------------------------------------------------------------------- icons --
const I = {
  dash: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  lib: '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="16" rx="1"/><path d="M17 5l4 1-3 14-4-1"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  act: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12l5 5L20 7"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  subs: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 12h4M13 12h4M7 15h7"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  chev: '<path d="M6 9l6 6 6-6"/>',
  chevr: '<path d="M9 6l6 6-6 6"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeoff: '<path d="M3 3l18 18M10.6 6.1A9.9 9.9 0 0 1 12 6c6 0 10 6 10 6a17 17 0 0 1-3.2 3.7M6.6 6.6C3.9 8.3 2 12 2 12s4 7 10 7c1.7 0 3.2-.5 4.5-1.2"/>',
  img: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 15l5-4 4 3 3-2 6 4"/>',
  dl: '<path d="M12 4v12M6 10l6 6 6-6M4 20h16"/>',
  warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9M19 14v6H4V5h6"/>',
  out: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11"/>',
};
const ic = (name, size = 18, sw = 2) => raw(`<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${I[name]}</svg>`);
const logo = raw('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" aria-hidden="true"><circle cx="12" cy="12" r="6"/></svg>');
const spinner = raw('<span class="spin" aria-hidden="true"></span>');

// ------------------------------------------------------------------ toasts --
function toast(msg, kind = '') {
  const id = Math.random().toString(36).slice(2);
  S.toasts.push({ id, msg, kind });
  renderToasts();
  setTimeout(() => { S.toasts = S.toasts.filter((t) => t.id !== id); renderToasts(); }, kind === 'bad' ? 7000 : 4000);
}
function renderToasts() {
  patch(document.getElementById('toasts'), S.toasts.map((t) => html`<div class="toast ${t.kind}" data-key="${t.id}" role="status">
    <span style="color:${t.kind === 'bad' ? 'var(--red-text)' : t.kind === 'good' ? 'var(--green-text)' : 'var(--primary-text)'}">${ic(t.kind === 'bad' ? 'warn' : 'check', 16)}</span><span>${t.msg}</span></div>`).join(''));
}

// ----------------------------------------------------------------- routing --
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, qs] = h.split('?');
  return { parts: path.split('/').filter(Boolean), query: new URLSearchParams(qs || '') };
}
function go(hash) { location.hash = hash; }
function viewFor(parts) {
  const p0 = parts[0] || '';
  if (p0 === 'tv' && parts[1]) return 'series';
  if (p0 === 'movie' && parts[1]) return 'movie';
  if (p0 === 'indexers' && parts[1] === 'add') return 'indexerAdd';
  return { '': 'dashboard', library: 'library', add: 'add', activity: 'activity', settings: 'settings' }[p0] || 'dashboard';
}
async function onRoute() {
  const prev = S.route.view;
  S.route = { ...parseRoute() };
  S.route.view = viewFor(S.route.parts);
  if (prev !== S.route.view || S.route.parts.join('/') !== (S.lastPath || '')) {
    S.v = { loading: true };
    window.scrollTo(0, 0);
  }
  S.lastPath = S.route.parts.join('/');
  render();
  await loadView();
}
async function loadView() {
  const v = VIEWS[S.route.view];
  if (!v || !v.load) return;
  try {
    await v.load();
    S.v.loading = false;
    S.v.error = null;
  } catch (e) {
    S.v.loading = false;
    S.v.error = e.message;
  }
  render();
}

// ------------------------------------------------------------------ render --
function render() {
  const app = document.getElementById('app');
  if (S.needLogin) { patch(app, loginView()); return; }
  const v = VIEWS[S.route.view] || VIEWS.dashboard;
  let body;
  if (S.v.loading && !S.v.loaded) body = html`<div class="muted" style="padding:20px 0">${spinner} Loading…</div>`;
  else if (S.v.error && !S.v.loaded) body = html`<div class="notice bad">${S.v.error}</div>`;
  else {
    try { body = v.render(); } catch (e) { console.error(e); body = html`<div class="notice bad">This page hit an error: ${e.message}</div>`; }
  }
  patch(app, html`<div class="shell">${navView()}<main class="main" id="main">${body}</main></div>`);
  patch(document.getElementById('modal'), S.modal ? modalView() : '');
}

function navView() {
  const cur = S.route.view;
  const st = S.status;
  const q = st && st.qbittorrent;
  const qDot = !q || q.ok === null ? 'warn' : q.ok ? 'ok' : 'bad';
  const qText = !q || q.ok === null ? 'qBittorrent not linked' : q.ok ? 'qBittorrent connected' : 'qBittorrent unreachable';
  const ix = st && st.indexers;
  const iDot = !ix || !ix.enabled ? 'warn' : ix.healthy === ix.enabled ? 'ok' : 'warn';
  const iText = !ix || !ix.enabled ? 'No indexers yet' : `${ix.healthy} of ${plural(ix.enabled, 'indexer')} healthy`;
  const qCount = S.queueCount || 0;
  const item = (view, href, icon, label, extra = '') => html`<a class="item ${cur === view || (view === 'settings' && cur === 'indexerAdd') || (view === 'library' && (cur === 'series' || cur === 'movie')) ? 'active' : ''}" href="${href}">${ic(icon)}${label}${extra}</a>`;
  return html`<nav class="nav" aria-label="Main">
    <a class="brand" href="#/"><div class="brand-mark">${logo}</div><span>Omniarr</span></a>
    ${item('dashboard', '#/', 'dash', 'Dashboard')}
    ${item('library', '#/library', 'lib', 'Library')}
    ${item('add', '#/add', 'plus', 'Add new')}
    ${item('activity', '#/activity', 'act', 'Activity', qCount ? html`<span class="badge-count">${qCount}</span>` : '')}
    ${item('settings', '#/settings', 'gear', 'Connections')}
    <div class="nav-status">
      <a href="#/settings" style="color:inherit;display:flex;align-items:center;gap:8px"><span class="dot ${qDot}"></span>${qText}</a>
      <a href="#/settings" style="color:inherit;display:flex;align-items:center;gap:8px"><span class="dot ${iDot}"></span>${iText}</a>
      ${S.authEnabled ? html`<button class="btn sm ghost" data-act="logout" style="justify-content:flex-start;padding:0;height:24px;border:0;color:var(--muted)">${ic('out', 14)} Log out</button>` : ''}
    </div>
  </nav>`;
}

// ----------------------------------------------------------------- actions --
async function run(name, el, ev) {
  const fn = A[name];
  if (!fn) { console.warn('no action', name); return; }
  try { await fn(el, ev); } catch (e) {
    if (e.message !== 'Please log in') toast(e.message || String(e), 'bad');
    console.error(e);
  }
}
async function busy(key, fn) {
  if (S.busy[key]) return;
  S.busy[key] = true; render();
  try { return await fn(); } finally { delete S.busy[key]; render(); }
}
const isBusy = (k) => !!S.busy[k];
function bindValue(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.type === 'number') return el.value === '' ? null : Number(el.value);
  return el.value;
}

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  if (el.dataset.self && e.target !== el) return;   // e.g. modal backdrop: only direct clicks
  e.preventDefault();
  run(el.dataset.act, el, e);
});
document.addEventListener('input', (e) => {
  const el = e.target;
  if (el.dataset && el.dataset.bind) {
    setPath(S, el.dataset.bind, bindValue(el));
    if (el.dataset.oninput) run(el.dataset.oninput, el, e);
  }
});
document.addEventListener('change', (e) => {
  const el = e.target;
  if (el.dataset && el.dataset.bind) {
    setPath(S, el.dataset.bind, bindValue(el));
    if (el.dataset.onchange) run(el.dataset.onchange, el, e); else render();
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.dataset && e.target.dataset.enter) { e.preventDefault(); run(e.target.dataset.enter, e.target, e); }
  if (e.key === 'Escape' && S.modal) { S.modal = null; render(); }
});
document.addEventListener('focusin', (e) => {
  if (e.target.dataset && e.target.dataset.naming) S.ui.lastNamingField = e.target.dataset.bind;
});

// form helpers ---------------------------------------------------------------
function input(path, { type = 'text', placeholder = '', cls = '', attrs = '' } = {}) {
  const v = getPath(S, path);
  return html`<input class="input ${cls}" type="${type}" data-bind="${path}" value="${v ?? ''}" placeholder="${placeholder}" ${raw(attrs)}>`;
}
function check(path, label, attrs = '') {
  return html`<label class="check"><input type="checkbox" data-bind="${path}" ${getPath(S, path) ? 'checked' : ''} ${raw(attrs)}>${label}</label>`;
}
function select(path, options, attrs = '') {
  const v = getPath(S, path);
  return html`<select class="input" data-bind="${path}" ${raw(attrs)}>${options.map(([k, label]) => html`<option value="${k}" ${String(k) === String(v ?? '') ? 'selected' : ''}>${label}</option>`)}</select>`;
}
function field(label, control, hint) {
  return html`<label class="field">${label}${control}${hint ? html`<span class="tiny muted">${hint}</span>` : ''}</label>`;
}
function btn(label, act, { cls = '', icon = null, busyKey = null, attrs = '', disabled = false } = {}) {
  const b = busyKey && isBusy(busyKey);
  return html`<button class="btn ${cls}" data-act="${act}" ${raw(attrs)} ${b || disabled ? 'disabled' : ''}>${b ? spinner : icon ? ic(icon, 16) : ''}${label}</button>`;
}

// ------------------------------------------------------------------- login --
function loginView() {
  S.login = S.login || { username: '', password: '' };
  return html`<div class="login-wrap"><div class="card" style="width:100%;max-width:380px;display:flex;flex-direction:column;gap:16px">
    <div class="row"><div class="brand-mark">${logo}</div><h1 style="font-size:22px">Omniarr</h1></div>
    ${field('Username', input('login.username', { attrs: 'autocomplete="username" data-enter="login"' }))}
    ${field('Password', input('login.password', { type: 'password', attrs: 'autocomplete="current-password" data-enter="login"' }))}
    ${btn('Log in', 'login', { cls: 'primary', busyKey: 'login' })}
  </div></div>`;
}
A.login = () => busy('login', async () => {
  await api('/api/auth/login', { method: 'POST', body: S.login });
  S.needLogin = false; S.login = null;
  await refreshStatus(); onRoute();
});
A.logout = async () => { await api('/api/auth/logout', { method: 'POST' }); S.needLogin = true; render(); };

// ------------------------------------------------------------- shared bits --
function posterImg(src, size = 34) {
  return src ? html`<img src="${src}" alt="" loading="lazy" referrerpolicy="no-referrer">` : html`<span class="ph">${ic('img', size, 1.6)}</span>`;
}
const STATE_COLORS = { complete: 'var(--green)', missing: 'var(--orange)', downloading: 'var(--blue)', unreleased: 'var(--faint)', ok: 'var(--faint)' };
const EVENT_STYLE = {
  grabbed: ['var(--primary-soft)', 'var(--primary-text)', 'Grabbed'], imported: ['var(--green-soft)', 'var(--green-text)', 'Imported'],
  failed: ['var(--red-soft)', 'var(--red-text)', 'Failed'], subtitle: ['var(--purple-soft)', 'var(--purple-text)', 'Subtitles'],
  added: ['var(--raised)', 'var(--text2)', 'Added'], deleted: ['var(--raised)', 'var(--text2)', 'Deleted'],
  removed: ['var(--orange-soft)', 'var(--orange-text)', 'Removed'],
};
function eventBadge(ev) {
  const [bg, fg, label] = EVENT_STYLE[ev] || ['var(--raised)', 'var(--text2)', ev];
  return html`<span class="event" style="background:${bg};color:${fg}">${label}</span>`;
}
function langChips(r) {
  const out = [];
  if (r.dubbed) out.push(html`<span class="chip blue">EN dub</span>`);
  if (r.dual_audio) out.push(html`<span class="chip blue">Dual audio</span>`);
  (r.languages || []).forEach((l) => out.push(html`<span class="chip orange">${l.toUpperCase()}</span>`));
  return out;
}
function jobsStrip() {
  const jobs = (S.status && S.status.jobs) || [];
  if (!jobs.length) return '';
  return html`<div class="notice info row" style="gap:10px">${spinner}<span>${jobs[0].label}…${jobs.length > 1 ? html` <span class="muted">(+${jobs.length - 1} more queued)</span>` : ''}</span></div>`;
}

async function refreshStatus() {
  try {
    const [st, auth] = await Promise.all([api('/api/status'), api('/api/auth/status')]);
    S.status = st; S.authEnabled = auth.enabled;
    const q = await api('/api/queue');
    S.queueCount = q.items.filter((x) => x.status !== 'import_blocked').length;
  } catch (_) { /* shown elsewhere */ }
}

// =============================================================== DASHBOARD ===
VIEWS.dashboard = {
  async load() { S.v.data = await api('/api/dashboard'); S.v.loaded = true; },
  poll: 4000,
  render() {
    const d = S.v.data; const st = S.status || {};
    const setup = st.setup || [];
    const showSetup = st.setup && !st.setup_complete && !st.setup_dismissed;
    const focusFor = { folders: 'folders', indexers: 'indexers', qbittorrent: 'qbittorrent', tmdb: 'quality' };
    return html`
      <header class="row between">
        <h1>Dashboard</h1>
        <div class="row">
          <label class="searchbox" style="width:320px;max-width:100%">${ic('search', 16)}<input aria-label="Search your library" placeholder="Search your library…" data-bind="ui.dashq" value="${S.ui.dashq || ''}" data-enter="dashSearch"></label>
          <a class="btn primary" href="#/add">${ic('plus', 16, 2.4)}Add show or movie</a>
        </div>
      </header>
      ${showSetup ? html`<section class="card hl row" aria-label="Setup" style="gap:20px">
        <div class="col grow" style="gap:4px;flex-basis:260px"><h2>Finish setup</h2><span class="muted">Shown until everything is connected, then it disappears.</span></div>
        <div class="row" style="gap:10px">${setup.map((s) => s.done
    ? html`<span class="pill done">${ic('check', 14, 3)}${s.label}</span>`
    : html`<a class="pill go" href="#/settings?focus=${focusFor[s.id]}">${s.label} →</a>`)}
          <button class="btn sm ghost" data-act="dismissSetup" style="border:0;color:var(--muted)">Hide</button></div>
      </section>` : ''}
      ${jobsStrip()}
      <div class="stats">
        <a class="stat" href="#/library?tab=tv" style="color:inherit"><span class="l">Shows</span><span class="v">${d.counts.shows}</span></a>
        <a class="stat" href="#/library?tab=movies" style="color:inherit"><span class="l">Movies</span><span class="v">${d.counts.movies}</span></a>
        <a class="stat" href="#/library?filter=missing" style="color:inherit"><span class="l">Missing</span><span class="v" style="color:${d.counts.missing ? 'var(--orange)' : 'inherit'}">${d.counts.missing}</span></a>
        <a class="stat" href="#/activity" style="color:inherit"><span class="l">Downloading</span><span class="v" style="color:${d.counts.downloading ? 'var(--blue)' : 'inherit'}">${d.counts.downloading}</span></a>
      </div>
      <div class="split">
        <section class="card" style="flex:3 1 420px" aria-label="Downloading now">
          <div class="card-head" style="margin-bottom:12px"><h2>Downloading now</h2><a href="#/activity" class="small">View queue</a></div>
          ${d.queue.length ? html`<div class="col" style="gap:16px">${d.queue.map((q) => html`<div class="col" style="gap:8px" data-key="q${q.id}">
            <div class="row between" style="gap:12px"><span style="font-weight:500">${q.label}</span><span class="muted small">${q.quality} · ${pct(q.progress)}${q.eta && q.progress < 1 ? ` · ${fmtEta(q.eta)} left` : ''}</span></div>
            ${q.status === 'import_blocked' ? html`<div class="notice warn">${q.message}</div>` : html`<div class="progress"><div style="width:${pct(q.progress)}"></div></div>`}
          </div>`)}</div>` : html`<div class="muted">Nothing downloading right now.</div>`}
        </section>
        <section class="card" style="flex:2 1 300px" aria-label="Coming up">
          <h2 style="margin-bottom:14px">Coming up</h2>
          ${d.upcoming.length ? html`<div class="col" style="gap:14px">${d.upcoming.map((u) => { const p = dayParts(u.date); return html`<a class="row" style="gap:14px;color:inherit;flex-wrap:nowrap" href="${u.kind === 'movie' ? `#/movie/${u.movie_id}` : `#/tv/${u.series_id}`}">
            <div style="width:44px;flex:none;text-align:center;display:flex;flex-direction:column;padding:6px 0;border-radius:8px;background:var(--raised)"><span class="tiny muted" style="text-transform:uppercase">${p.day}</span><span style="font-size:16px;font-weight:600">${p.date}</span></div>
            <div class="col" style="gap:2px;min-width:0"><span style="font-weight:500">${u.title}</span><span class="small muted ellipsis">${u.detail}</span></div></a>`; })}</div>` : html`<div class="muted">Nothing airing in the next week.</div>`}
        </section>
      </div>
      <section class="card" aria-label="Recent activity">
        <div class="card-head" style="margin-bottom:12px"><h2>Recent activity</h2><a href="#/activity?tab=history" class="small">All history</a></div>
        ${d.recent.length ? html`<div class="col" style="gap:12px">${d.recent.map((h) => html`<div class="row" style="gap:14px;flex-wrap:nowrap">${eventBadge(h.event)}<span class="grow ellipsis"><b style="font-weight:500">${h.title}</b> <span class="muted">${h.message}</span></span><span class="muted small" style="flex:none">${fmtAgo(h.at)}</span></div>`)}</div>`
    : html`<div class="muted">Nothing yet. Add a show or movie to get started.</div>`}
      </section>`;
  },
};
A.dashSearch = () => go(`#/library?q=${encodeURIComponent(S.ui.dashq || '')}`);
A.dismissSetup = async () => { await api('/api/setup/dismiss', { method: 'POST' }); await refreshStatus(); render(); };

// ================================================================= LIBRARY ===
VIEWS.library = {
  async load() {
    const q = S.route.query;
    if (q.get('q') !== null && S.ui.libqFrom !== q.get('q')) { S.ui.libq = q.get('q'); S.ui.libqFrom = q.get('q'); }
    S.v.data = await api('/api/library'); S.v.loaded = true;
  },
  poll: 10000,
  render() {
    const q = S.route.query;
    const tab = q.get('tab') || 'all';
    const filter = q.get('filter') || '';
    const sort = S.ui.libsort || 'added';
    const text = (S.ui.libq || '').toLowerCase().trim();
    const items = S.v.data.items;
    const tv = items.filter((i) => i.kind === 'tv'); const mv = items.filter((i) => i.kind === 'movie');
    let list = tab === 'tv' ? tv : tab === 'movies' ? mv : items;
    if (filter === 'missing') list = list.filter((i) => i.state === 'missing');
    if (filter === 'downloading') list = list.filter((i) => i.state === 'downloading');
    if (filter === 'complete') list = list.filter((i) => i.state === 'complete');
    if (filter === 'nosubs') list = list.filter((i) => i.missing_subs > 0);
    if (text) list = list.filter((i) => i.title.toLowerCase().includes(text));
    const sorters = {
      added: (a, b) => (b.added_at || 0) - (a.added_at || 0),
      title: (a, b) => a.sort_title.localeCompare(b.sort_title),
      year: (a, b) => (b.year || 0) - (a.year || 0),
      missing: (a, b) => (b.missing || (b.state === 'missing' ? 1 : 0)) - (a.missing || (a.state === 'missing' ? 1 : 0)),
    };
    list = [...list].sort(sorters[sort]);
    const qs = (o) => { const p = new URLSearchParams({ tab, filter, ...o }); [...p.keys()].forEach((k) => { if (!p.get(k) || p.get(k) === 'all') p.delete(k); }); return `#/library?${p}`; };
    const fpill = (id, label, warn) => html`<a class="pill ${filter === id ? 'on' : ''} ${warn ? 'warn' : ''}" href="${qs({ filter: filter === id ? '' : id })}">${label}</a>`;
    const meta = (i) => {
      if (i.kind === 'tv') {
        if (i.state === 'downloading') return `${i.year || ''} · Downloading`;
        return `${i.year || ''} · ${i.have}/${i.aired} episodes`;
      }
      return `${i.year || ''} · ${{ complete: i.quality || 'Downloaded', missing: 'Missing', downloading: 'Downloading', unreleased: 'Not released' }[i.state] || ''}`;
    };
    const barPct = (i) => i.kind === 'tv' ? (i.aired ? Math.min(100, Math.round((i.have / i.aired) * 100)) : 0) : i.state === 'complete' ? 100 : i.state === 'downloading' ? 50 : 0;
    return html`
      <header class="row between"><h1>Library</h1><a class="btn primary" href="#/add">${ic('plus', 16, 2.4)}Add show or movie</a></header>
      <div class="row between">
        <div class="tabs" role="tablist" aria-label="Media type">
          <button role="tab" class="${tab === 'all' ? 'on' : ''}" data-act="nav" data-href="${qs({ tab: 'all' })}">All ${items.length}</button>
          <button role="tab" class="${tab === 'tv' ? 'on' : ''}" data-act="nav" data-href="${qs({ tab: 'tv' })}">TV ${tv.length}</button>
          <button role="tab" class="${tab === 'movies' ? 'on' : ''}" data-act="nav" data-href="${qs({ tab: 'movies' })}">Movies ${mv.length}</button>
        </div>
        <div class="row" style="gap:8px">
          ${fpill('missing', 'Missing', true)}${fpill('downloading', 'Downloading')}${fpill('complete', 'Complete')}${fpill('nosubs', 'Missing subtitles')}
          <label class="searchbox" style="height:36px;width:200px">${ic('search', 14)}<input aria-label="Filter by title" placeholder="Filter…" data-bind="ui.libq" value="${S.ui.libq || ''}" data-oninput="rerender"></label>
          <select class="input" style="height:36px;width:auto" data-bind="ui.libsort" aria-label="Sort">${[['added', 'Recently added'], ['title', 'Title'], ['year', 'Year'], ['missing', 'Most missing']].map(([k, l]) => html`<option value="${k}" ${sort === k ? 'selected' : ''}>Sort: ${l}</option>`)}</select>
        </div>
      </div>
      ${!items.length ? html`<div class="card empty">${ic('lib', 34, 1.5)}<span>Your library is empty.</span><a class="btn primary" href="#/add">Add your first show or movie</a></div>`
    : !list.length ? html`<div class="empty">Nothing matches these filters.</div>`
      : html`<div class="poster-grid">${list.map((i) => html`<a class="poster-card" data-key="${i.kind}${i.id}" href="${i.kind === 'tv' ? `#/tv/${i.id}` : `#/movie/${i.id}`}">
        <div class="poster">${posterImg(i.poster)}<span class="kind">${i.kind === 'tv' ? 'TV' : 'MOVIE'}</span>
          <div class="bar"><div style="background:${STATE_COLORS[i.state] || 'var(--faint)'};width:${barPct(i)}%"></div></div></div>
        <div class="col" style="gap:2px"><span class="t">${i.title}</span><span class="m">${meta(i)}</span></div></a>`)}</div>`}`;
  },
};
A.nav = (el) => go(el.dataset.href);
A.rerender = () => render();

// ================================================================== SERIES ===
VIEWS.series = {
  async load() {
    const id = S.route.parts[1];
    const d = await api(`/api/series/${id}`);
    S.v.data = d; S.v.loaded = true;
    if (!S.ui.openSeasons || S.ui.openSeasonsFor !== id) {
      const first = d.seasons.find((s) => s.season > 0 && s.aired > 0) || d.seasons[0];
      S.ui.openSeasons = first ? { [first.season]: true } : {};
      S.ui.openSeasonsFor = id;
    }
  },
  poll: 5000,
  render() {
    const s = S.v.data; const st = s.stats || {};
    const missing = st.missing || 0;
    const lang = s.language_mode === 'default' ? defaultLangLabel() : s.language_mode === 'english' ? 'English only' : 'Any language';
    const statusChip = s.status === 'Running' ? html`<span class="chip green">Continuing</span>` : s.status === 'Ended' ? html`<span class="chip">Ended</span>` : s.status ? html`<span class="chip">${s.status}</span>` : '';
    return html`
      <a href="#/library" class="small" style="color:var(--muted)">← Library</a>
      <section class="hero" aria-label="Show summary">
        <div class="poster">${posterImg(s.poster, 40)}</div>
        <div class="col grow" style="gap:14px;flex-basis:380px">
          <div class="row" style="gap:8px"><span class="chip">TV</span>${statusChip}<span class="chip ${s.monitored ? '' : 'orange'}">${s.monitored ? 'Monitored' : 'Not monitored'}</span>${s.is_anime ? html`<span class="chip purple">Anime</span>` : ''}</div>
          <h1>${s.title}</h1>
          <p class="muted">${[s.year, (s.genres || []).slice(0, 2).join(', '), s.network, plural(s.seasons.filter((x) => x.season > 0).length, 'season'), st.next_air ? `Next: ${fmtDate(st.next_air, { weekday: 'short', day: 'numeric', month: 'short' })}` : ''].filter(Boolean).join(' · ')}</p>
          ${s.overview ? html`<p style="color:var(--text2);line-height:1.55;max-width:720px">${s.overview}</p>` : ''}
          <div class="facts">
            <div><span class="k">Quality</span><span class="v">${s.profile_name}</span></div>
            <div><span class="k">Audio</span><span class="v">${lang}</span></div>
            <div><span class="k">Folder</span><span class="v">${s.path}</span></div>
            <div><span class="k">On disk</span><span class="v">${st.have || 0} of ${st.aired_monitored || 0} · ${fmtBytes(st.size)}</span></div>
          </div>
          <div class="row" style="gap:10px;padding-top:4px">
            ${btn(`Search missing${missing ? ` (${missing})` : ''}`, 'seriesSearch', { cls: 'primary', icon: 'search', busyKey: 'seriesSearch', disabled: !missing })}
            ${btn(`Find missing subtitles${s.missing_subs ? ` (${s.missing_subs})` : ''}`, 'seriesSubs', { icon: 'subs', busyKey: 'seriesSubs', disabled: !s.missing_subs })}
            ${btn('Edit', 'editItem', { icon: 'edit', attrs: 'data-kind="tv"' })}
            ${btn('Refresh & scan', 'seriesRefresh', { icon: 'refresh', busyKey: 'seriesRefresh' })}
            ${btn('', 'deleteItem', { cls: 'icon danger', icon: 'trash', attrs: 'data-kind="tv" aria-label="Delete show"' })}
          </div>
        </div>
      </section>
      ${jobsStrip()}
      ${s.seasons.map((sn) => seasonCard(s, sn))}`;
  },
};
function defaultLangLabel() {
  const m = S.settingsCache && S.settingsCache.language && S.settingsCache.language.mode;
  return m === 'any' ? 'Any language (default)' : 'English only (default)';
}
function seasonCard(s, sn) {
  const open = !!S.ui.openSeasons[sn.season];
  const name = sn.season === 0 ? 'Specials' : `Season ${sn.season}`;
  const complete = sn.aired > 0 && sn.have >= sn.aired;
  const countColor = complete ? 'var(--green-text)' : sn.aired ? 'var(--orange-text)' : 'var(--muted)';
  return html`<section class="card flush season ${open ? 'open' : ''}" aria-label="${name}" data-key="season${sn.season}">
    <div class="season-head" data-act="toggleSeason" data-season="${sn.season}">
      <div class="row" style="gap:12px">${ic(open ? 'chev' : 'chevr', 16)}<h2>${name}</h2><span class="small" style="color:${countColor}">${sn.have} of ${sn.aired}${complete ? ' · complete' : ''}</span>${sn.monitored ? '' : html`<span class="chip">Not monitored</span>`}</div>
      <div class="row" style="gap:8px">
        <button class="btn sm" data-act="seasonMonitor" data-season="${sn.season}" data-on="${sn.monitored ? '0' : '1'}">${ic(sn.monitored ? 'eyeoff' : 'eye', 14)}${sn.monitored ? 'Unmonitor' : 'Monitor'}</button>
        <button class="btn sm" data-act="seasonPick" data-season="${sn.season}">${ic('list', 14)}Pick a release</button>
        <button class="btn sm" data-act="seasonSearch" data-season="${sn.season}">${ic('search', 14)}Search season</button>
      </div>
    </div>
    ${open ? sn.episodes.map((e) => episodeRow(e)) : ''}
  </section>`;
}
function episodeRow(e) {
  const states = {
    have: ['state-have', e.file ? e.file.quality : 'Downloaded'],
    missing: ['state-missing', 'Missing'],
    downloading: ['state-downloading', e.queue ? (e.queue.status === 'import_blocked' ? 'Needs attention' : `Downloading ${pct(e.queue.progress)}`) : 'Downloading'],
    upcoming: ['state-upcoming', 'Upcoming'],
    unmonitored: ['state-unmonitored', 'Not monitored'],
  };
  const [cls, text] = states[e.state] || ['', e.state];
  let sub = html`<span class="muted">—</span>`;
  if (e.file) {
    const ss = e.file.sub_status;
    if (ss === 'ok' || ss === 'embedded') sub = html`<span class="state-have" title="${ss === 'embedded' ? 'Built into the video' : 'Subtitle file saved'}">${ic('subs', 15)}EN</span>`;
    else if (ss === 'missing') sub = html`<span class="state-missing" title="Still looking">${ic('subs', 15)}Looking</span>`;
    else if (ss === 'pending') sub = html`<span class="muted">${ic('subs', 15)}…</span>`;
  }
  return html`<div class="ep" data-key="ep${e.id}">
    <span class="n">${String(e.number).padStart(2, '0')}</span>
    <span class="t ellipsis" title="${e.overview || ''}">${e.title || 'TBA'}</span>
    <span class="d">${e.airdate ? fmtDate(e.airdate) : 'Not aired'}</span>
    <span class="s ${cls}"><span class="dot" style="background:currentColor"></span>${text}</span>
    <span class="sub">${sub}</span>
    <div class="row" style="gap:4px">
      <button class="btn icon" data-act="epMonitor" data-id="${e.id}" data-on="${e.monitored ? '0' : '1'}" aria-label="${e.monitored ? 'Stop monitoring' : 'Monitor'} episode" title="${e.monitored ? 'Monitored — click to stop' : 'Not monitored — click to monitor'}" style="color:${e.monitored ? 'var(--text2)' : 'var(--faint)'}">${ic(e.monitored ? 'eye' : 'eyeoff', 15)}</button>
      <button class="btn icon" data-act="epPick" data-id="${e.id}" aria-label="Pick a release for this episode" title="Pick a release">${ic('list', 15)}</button>
      <button class="btn icon" data-act="epSearch" data-id="${e.id}" aria-label="Search this episode" title="Search automatically">${ic('search', 15)}</button>
    </div>
  </div>`;
}
A.toggleSeason = (el) => { const n = el.dataset.season; S.ui.openSeasons[n] = !S.ui.openSeasons[n]; render(); };
A.seriesSearch = () => busy('seriesSearch', async () => { await api(`/api/series/${S.route.parts[1]}/search`, { method: 'POST' }); toast('Searching for missing episodes…'); await refreshStatus(); });
A.seriesSubs = () => busy('seriesSubs', async () => { await api(`/api/series/${S.route.parts[1]}/subtitles`, { method: 'POST' }); toast('Looking for subtitles…'); await refreshStatus(); });
A.seriesRefresh = () => busy('seriesRefresh', async () => {
  const r = await api(`/api/series/${S.route.parts[1]}/refresh`, { method: 'POST' });
  toast(`Refreshed · ${plural(r.new_episodes, 'new episode')} · ${plural(r.files_found, 'file')} found on disk`, 'good');
  await loadView();
});
A.seasonSearch = async (el) => { await api(`/api/series/${S.route.parts[1]}/search?season=${el.dataset.season}`, { method: 'POST' }); toast(`Searching season ${el.dataset.season}…`); await refreshStatus(); render(); };
A.seasonMonitor = async (el) => { await api(`/api/series/${S.route.parts[1]}/season-monitor`, { method: 'POST', body: { season: Number(el.dataset.season), monitored: el.dataset.on === '1' } }); await loadView(); };
A.epMonitor = async (el) => { await api(`/api/episodes/${el.dataset.id}`, { method: 'PUT', body: { monitored: el.dataset.on === '1' } }); await loadView(); };
A.epSearch = async (el) => { await api(`/api/episodes/${el.dataset.id}/search`, { method: 'POST' }); toast('Searching…'); await refreshStatus(); render(); };
A.epPick = (el) => openReleases(`/api/episodes/${el.dataset.id}/releases`, 'Pick a release');
A.seasonPick = (el) => openReleases(`/api/series/${S.route.parts[1]}/releases?season=${el.dataset.season}`, `Pick a release · Season ${el.dataset.season}`);

// =================================================================== MOVIE ===
VIEWS.movie = {
  async load() { S.v.data = await api(`/api/movies/${S.route.parts[1]}`); S.v.loaded = true; },
  poll: 5000,
  render() {
    const m = S.v.data;
    const lang = m.language_mode === 'default' ? defaultLangLabel() : m.language_mode === 'english' ? 'English only' : 'Any language';
    let stateChip;
    if (m.queue) stateChip = html`<span class="chip blue">${m.queue.status === 'import_blocked' ? 'Needs attention' : `Downloading ${pct(m.queue.progress)}`}</span>`;
    else if (m.file) stateChip = html`<span class="chip green">Downloaded</span>`;
    else if (!m.available) stateChip = html`<span class="chip">Not released yet</span>`;
    else stateChip = html`<span class="chip orange">Missing</span>`;
    const dates = [['In cinemas', m.theatrical_date], ['Digital', m.digital_date], ['Disc', m.physical_date]].filter((x) => x[1]);
    const f = m.file;
    let subText = '—';
    if (f) subText = f.sub_status === 'ok' ? 'English (file)' : f.sub_status === 'embedded' ? 'English (built in)' : f.sub_status === 'missing' ? 'Still looking' : 'Checking…';
    return html`
      <a href="#/library?tab=movies" class="small" style="color:var(--muted)">← Library</a>
      <section class="hero" aria-label="Movie summary">
        <div class="poster">${posterImg(m.poster, 40)}</div>
        <div class="col grow" style="gap:14px;flex-basis:380px">
          <div class="row" style="gap:8px"><span class="chip">MOVIE</span>${stateChip}<span class="chip ${m.monitored ? '' : 'orange'}">${m.monitored ? 'Monitored' : 'Not monitored'}</span></div>
          <h1>${m.title}</h1>
          <p class="muted">${[m.year, (m.genres || []).slice(0, 3).join(', '), m.runtime ? `${m.runtime} min` : ''].filter(Boolean).join(' · ')}</p>
          ${m.overview ? html`<p style="color:var(--text2);line-height:1.55;max-width:720px">${m.overview}</p>` : ''}
          <div class="facts">
            <div><span class="k">Quality</span><span class="v">${m.profile_name}</span></div>
            <div><span class="k">Audio</span><span class="v">${lang}</span></div>
            <div><span class="k">Folder</span><span class="v">${m.path}</span></div>
            ${dates.map(([k, d]) => html`<div><span class="k">${k}</span><span class="v">${fmtDate(d)}</span></div>`)}
          </div>
          <div class="row" style="gap:10px;padding-top:4px">
            ${btn(f ? 'Search for upgrade' : 'Search now', 'movieSearch', { cls: 'primary', icon: 'search', busyKey: 'movieSearch' })}
            ${btn('Pick a release myself', 'moviePick', { icon: 'list' })}
            ${f ? btn('Find subtitles', 'movieSubs', { icon: 'subs', busyKey: 'movieSubs' }) : ''}
            ${btn('Edit', 'editItem', { icon: 'edit', attrs: 'data-kind="movie"' })}
            ${btn('Refresh & scan', 'movieRefresh', { icon: 'refresh', busyKey: 'movieRefresh' })}
            ${btn('', 'deleteItem', { cls: 'icon danger', icon: 'trash', attrs: 'data-kind="movie" aria-label="Delete movie"' })}
          </div>
        </div>
      </section>
      ${jobsStrip()}
      ${m.queue && m.queue.status === 'import_blocked' ? html`<div class="notice warn">${m.queue.message}</div>` : ''}
      <section class="card" aria-label="File">
        <h2 style="margin-bottom:14px">File</h2>
        ${f ? html`<div class="facts"><div><span class="k">Path</span><span class="v mono">${f.path}</span></div><div><span class="k">Quality</span><span class="v">${f.quality}</span></div><div><span class="k">Size</span><span class="v">${fmtBytes(f.size)}</span></div><div><span class="k">Subtitles</span><span class="v">${subText}</span></div></div>`
    : html`<p class="muted">${m.available ? 'No file yet — it will be grabbed automatically when a good release appears.' : 'Not out for home viewing yet. It will be grabbed automatically once it is.'}</p>`}
      </section>`;
  },
};
A.movieSearch = () => busy('movieSearch', async () => { await api(`/api/movies/${S.route.parts[1]}/search`, { method: 'POST' }); toast('Searching…'); await refreshStatus(); });
A.moviePick = () => openReleases(`/api/movies/${S.route.parts[1]}/releases`, 'Pick a release');
A.movieSubs = () => busy('movieSubs', async () => { const r = await api(`/api/movies/${S.route.parts[1]}/subtitles`, { method: 'POST' }); toast(`Subtitles: ${r.result}`); await loadView(); });
A.movieRefresh = () => busy('movieRefresh', async () => { await api(`/api/movies/${S.route.parts[1]}/refresh`, { method: 'POST' }); toast('Refreshed', 'good'); await loadView(); });

// edit / delete modals (shared) ---------------------------------------------
A.editItem = async (el) => {
  const d = S.v.data; const kind = el.dataset.kind;
  const prof = await profiles();
  S.modal = { type: 'edit', kind, id: d.id, title: d.title, profiles: prof, form: { quality_profile: d.quality_profile, language_mode: d.language_mode, monitored: !!d.monitored } };
  render();
};
A.deleteItem = (el) => { const d = S.v.data; S.modal = { type: 'delete', kind: el.dataset.kind, id: d.id, title: d.title, form: { delete_files: false } }; render(); };
A.saveEdit = () => busy('saveEdit', async () => {
  const m = S.modal;
  await api(`/api/${m.kind === 'tv' ? 'series' : 'movies'}/${m.id}`, { method: 'PUT', body: m.form });
  S.modal = null; toast('Saved', 'good'); await loadView();
});
A.confirmDelete = () => busy('confirmDelete', async () => {
  const m = S.modal;
  await api(`/api/${m.kind === 'tv' ? 'series' : 'movies'}/${m.id}?delete_files=${m.form.delete_files ? 'true' : 'false'}`, { method: 'DELETE' });
  S.modal = null; toast(`Removed ${m.title}`, 'good'); go('#/library');
});
A.closeModal = () => { S.modal = null; render(); };
async function profiles() {
  if (!S.profilesCache) S.profilesCache = (await api('/api/profiles')).items;
  return S.profilesCache;
}

// ================================================================ RELEASES ===
async function openReleases(url, title) {
  S.modal = { type: 'releases', title, loading: true, url, filter: 'all' };
  render();
  try {
    const d = await api(url);
    if (!S.modal || S.modal.url !== url) return;
    S.modal.loading = false; S.modal.data = d; render();
  } catch (e) { if (S.modal) { S.modal.loading = false; S.modal.error = e.message; render(); } }
}
A.grab = (el) => busy(`grab${el.dataset.token}`, async () => {
  await api('/api/grab', { method: 'POST', body: { token: el.dataset.token } });
  toast('Sent to qBittorrent', 'good');
  const r = S.modal && S.modal.data && S.modal.data.releases.find((x) => x.token === el.dataset.token);
  if (r) r.grabbed = true;
  await refreshStatus(); loadView();
});
A.relFilter = (el) => { S.modal.filter = el.dataset.f; render(); };
function releasesModal(m) {
  if (m.loading) return html`<div class="col" style="gap:12px;align-items:center;padding:40px">${spinner}<span class="muted">Searching your indexers… this can take a little while because requests are spaced out to stay polite.</span></div>`;
  if (m.error) return html`<div class="notice bad">${m.error}</div>`;
  const rels = m.data.releases;
  const ok = rels.filter((r) => r.accepted);
  const list = m.filter === 'accepted' ? ok : rels;
  const errs = Object.entries(m.data.errors || {});
  return html`
    <div class="row between">
      <div class="tabs"><button class="${m.filter === 'all' ? 'on' : ''}" data-act="relFilter" data-f="all">All ${rels.length}</button><button class="${m.filter === 'accepted' ? 'on' : ''}" data-act="relFilter" data-f="accepted">Suitable ${ok.length}</button></div>
      <span class="tiny muted">Suitable releases are listed first, best match at the top.</span>
    </div>
    ${errs.length ? html`<div class="notice warn">${errs.map(([k, v]) => html`<div><b>${k}:</b> ${v}</div>`)}</div>` : ''}
    ${!list.length ? html`<div class="empty">No releases found.</div>` : html`<div class="table-wrap"><table class="tbl"><thead><tr><th>Release</th><th>Indexer</th><th>Quality</th><th>Size</th><th>Peers</th><th>Age</th><th></th></tr></thead><tbody>
    ${list.map((r) => html`<tr data-key="${r.token}">
      <td style="max-width:420px"><div class="row" style="gap:6px;flex-wrap:nowrap"><span style="color:${r.accepted ? 'var(--green)' : 'var(--red)'};flex:none">${ic(r.accepted ? 'check' : 'x', 14, 2.6)}</span><span style="word-break:break-word">${r.title}</span></div>
        <div class="row" style="gap:6px;margin-top:6px">${r.season_pack ? html`<span class="chip">Season pack</span>` : ''}${r.upgrade ? html`<span class="chip green">Upgrade</span>` : ''}${r.freeleech ? html`<span class="chip purple">Freeleech</span>` : ''}${langChips(r)}</div>
        ${r.rejections.length ? html`<div class="rel-reasons">${r.rejections.join(' · ')}</div>` : ''}</td>
      <td class="small">${r.indexer}</td><td class="small">${r.quality}</td><td class="small">${fmtBytes(r.size)}</td>
      <td class="small"><span style="color:${(r.seeders || 0) > 0 ? 'var(--green-text)' : 'var(--red-text)'}">${r.seeders ?? '?'}</span> / ${r.leechers ?? '?'}</td>
      <td class="small">${fmtAge(r.published)}</td>
      <td>${r.grabbed ? html`<span class="chip green">Sent</span>` : html`<button class="btn sm ${r.accepted ? 'primary' : ''}" data-act="grab" data-token="${r.token}" ${isBusy(`grab${r.token}`) ? 'disabled' : ''}>${isBusy(`grab${r.token}`) ? spinner : ic('dl', 14)}${r.accepted ? 'Download' : 'Anyway'}</button>`}</td>
    </tr>`)}</tbody></table></div>`}`;
}

// ===================================================================== ADD ===
VIEWS.add = {
  async load() {
    S.add = S.add || { term: '', type: 'both', results: [], errors: {}, sel: null, opts: {} };
    const q = S.route.query.get('q');
    if (q && q !== S.add.term) { S.add.term = q; await doLookup(); }
    S.v.settings = (await api('/api/settings')); S.settingsCache = S.v.settings.settings;
    S.v.loaded = true;
  },
  render() {
    const a = S.add;
    const set = S.v.settings;
    const tabs = [['both', 'Both'], ['tv', 'TV'], ['movie', 'Movies']];
    return html`
      <h1>Add a show or movie</h1>
      <div class="row">
        <label class="searchbox big grow" style="flex-basis:360px">${ic('search', 18)}<input aria-label="Search for a title" placeholder="Type a show or movie name…" data-bind="add.term" value="${a.term}" data-oninput="lookupDebounced" data-enter="lookup" autofocus></label>
        <div class="tabs" role="tablist" aria-label="Search in">${tabs.map(([k, l]) => html`<button role="tab" class="${a.type === k ? 'on' : ''}" data-act="addType" data-t="${k}" style="height:40px">${l}</button>`)}</div>
      </div>
      ${Object.entries(a.errors || {}).map(([k, v]) => html`<div class="notice ${k === 'movie' && /TMDB key/.test(v) ? 'info' : 'warn'}">${v}${/TMDB key/.test(v) ? html` <a href="#/settings?focus=quality">Add it in Connections →</a>` : ''}</div>`)}
      <div class="split">
        <section aria-label="Results" class="col" style="flex:3 1 460px;gap:12px">
          ${a.loading ? html`<div class="muted">${spinner} Searching…</div>` : ''}
          ${!a.loading && a.term && !a.results.length && !Object.keys(a.errors).length ? html`<div class="empty">No matches. Try a different spelling, or add the year.</div>` : ''}
          ${!a.term ? html`<div class="card empty">${ic('search', 30, 1.5)}<span>Search by name. TV info comes from TVMaze, movie info from TMDB.</span></div>` : ''}
          ${a.results.map((r, i) => { const sel = a.sel && a.sel.kind === r.kind && (a.sel.tvmaze_id || a.sel.tmdb_id) === (r.tvmaze_id || r.tmdb_id); return html`<div class="card row" data-key="r${r.kind}${r.tvmaze_id || r.tmdb_id}" style="padding:14px;flex-wrap:nowrap;align-items:flex-start;${sel ? 'border-color:var(--primary);background:#1A2236' : ''}">
            <div class="poster" style="width:64px;flex:none">${posterImg(r.poster_small || r.poster, 20)}</div>
            <div class="col grow" style="gap:6px"><div class="row" style="gap:8px"><span style="font-size:16px;font-weight:600">${r.title}</span><span class="chip">${r.kind === 'tv' ? 'TV' : 'MOVIE'}</span>${r.original_language && r.original_language !== 'en' ? html`<span class="chip orange" title="Original language">${r.original_language.toUpperCase()}</span>` : ''}</div>
              <span class="small muted">${[r.year, r.kind === 'tv' ? (r.network || '') : '', r.kind === 'tv' ? r.status : ''].filter(Boolean).join(' · ')}</span>
              <span class="small" style="color:var(--text2);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${r.overview || ''}</span></div>
            <div style="align-self:center;flex:none">${r.library_id ? html`<a class="small" href="${r.kind === 'tv' ? `#/tv/${r.library_id}` : `#/movie/${r.library_id}`}" style="color:var(--green-text)">In library →</a>` : html`<button class="btn sm ${sel ? 'primary' : ''}" data-act="addSelect" data-i="${i}">${sel ? 'Selected' : 'Select'}</button>`}</div>
          </div>`; })}
        </section>
        ${a.sel ? addPanel(a, set) : ''}
      </div>`;
  },
};
function addPanel(a, set) {
  const s = a.sel; const o = a.opts;
  const profilesOpt = set.profiles.map((p) => [p.id, `${p.name} — ${p.description}`]);
  const langDefault = set.settings.language.mode === 'any' ? 'Any language' : 'English only (incl. English dub)';
  const foreign = s.original_language && s.original_language !== 'en';
  return html`<aside class="card focus col" aria-label="Add options" style="flex:2 1 300px;gap:18px;position:sticky;top:16px">
    <div class="col" style="gap:4px"><span class="small muted">Adding</span><span style="font-size:18px;font-weight:600">${s.title}${s.year ? ` (${s.year})` : ''} · ${s.kind === 'tv' ? 'TV' : 'Movie'}</span></div>
    ${field('Save to', input('add.opts.root', { cls: 'mono' }))}
    ${field('Quality', select('add.opts.quality_profile', profilesOpt))}
    ${field('Audio language', select('add.opts.language_mode', [['default', `${langDefault} — default`], ['english', 'English only (incl. English dub)'], ['any', 'Any language']]),
    foreign && (o.language_mode === 'english' || (o.language_mode === 'default' && set.settings.language.mode === 'english')) ? `Original language is ${s.original_language.toUpperCase()} — only English-dubbed releases will be picked.` : '')}
    ${s.kind === 'tv' ? field('Get', select('add.opts.monitor', [['all', 'All episodes'], ['future', 'Only new episodes'], ['none', 'Nothing yet (just track it)']]))
    : check('add.opts.monitored', 'Monitor (grab it when available)')}
    ${check('add.opts.search', 'Start searching right away')}
    ${btn('Add to library', 'addConfirm', { cls: 'primary', busyKey: 'addConfirm', attrs: 'style="height:48px"' })}
  </aside>`;
}
async function doLookup() {
  const a = S.add;
  if (!a.term.trim()) { a.results = []; a.errors = {}; render(); return; }
  a.loading = true; render();
  const term = a.term;
  try {
    const r = await api(`/api/lookup?term=${encodeURIComponent(term)}&type=${a.type}`);
    if (term !== a.term) return;
    a.results = r.results; a.errors = r.errors || {};
  } finally { a.loading = false; render(); }
}
A.lookup = () => doLookup();
A.lookupDebounced = debounce(() => doLookup(), 450);
A.addType = (el) => { S.add.type = el.dataset.t; doLookup(); };
A.addSelect = (el) => {
  const r = S.add.results[Number(el.dataset.i)];
  const set = S.v.settings.settings;
  S.add.sel = r;
  S.add.opts = { root: r.kind === 'tv' ? set.folders.tv : set.folders.movies, quality_profile: set.quality.default_profile, language_mode: 'default', monitor: 'all', monitored: true, search: true };
  render();
};
A.addConfirm = () => busy('addConfirm', async () => {
  const s = S.add.sel;
  const body = { ...S.add.opts };
  let res;
  if (s.kind === 'tv') res = await api('/api/series', { method: 'POST', body: { ...body, tvmaze_id: s.tvmaze_id } });
  else res = await api('/api/movies', { method: 'POST', body: { ...body, tmdb_id: s.tmdb_id } });
  toast(`Added ${s.title}${body.search ? ' — searching now' : ''}`, 'good');
  S.add.sel = null; S.add.results.forEach((r) => { if (r === s) r.library_id = res.id; });
  await refreshStatus();
  go(res.kind === 'tv' ? `#/tv/${res.id}` : `#/movie/${res.id}`);
});

// ================================================================ ACTIVITY ===
VIEWS.activity = {
  async load() {
    const tab = S.route.query.get('tab') || 'queue';
    if (tab === 'queue') S.v.queue = (await api('/api/queue')).items;
    if (tab === 'history') S.v.history = (await api(`/api/history?limit=200${S.ui.histEvent ? `&event=${S.ui.histEvent}` : ''}`)).items;
    if (tab === 'blocked') S.v.blocked = (await api('/api/blocklist')).items;
    if (tab === 'logs') S.v.logs = (await api(`/api/logs?limit=400${S.ui.logLevel ? `&level=${S.ui.logLevel}` : ''}`)).items;
    if (tab === 'tasks') S.v.jobs = await api('/api/jobs');
    S.v.loaded = true;
  },
  poll: 3000,
  render() {
    const tab = S.route.query.get('tab') || 'queue';
    const t = (id, label) => html`<button role="tab" class="${tab === id ? 'on' : ''}" data-act="nav" data-href="#/activity?tab=${id}">${label}</button>`;
    let body = '';
    if (tab === 'queue') body = queueTable(S.v.queue || []);
    if (tab === 'history') body = historyList(S.v.history || []);
    if (tab === 'blocked') body = blockedList(S.v.blocked || []);
    if (tab === 'logs') body = logsList(S.v.logs || []);
    if (tab === 'tasks') body = tasksList(S.v.jobs || { items: [], tasks: [] });
    return html`
      <header class="row between"><h1>Activity</h1>
        <div class="tabs" role="tablist" aria-label="Activity view">${t('queue', `Queue${S.queueCount ? ` ${S.queueCount}` : ''}`)}${t('history', 'History')}${t('blocked', 'Blocked releases')}${t('tasks', 'Tasks')}${t('logs', 'Logs')}</div></header>
      ${jobsStrip()}
      ${body}`;
  },
};
function queueTable(items) {
  if (!items.length) return html`<div class="card empty">${ic('dl', 30, 1.5)}<span>Nothing in the queue.</span></div>`;
  return html`<section class="card flush" aria-label="Queue"><div class="table-wrap"><table class="tbl"><thead><tr><th>Title</th><th>Quality</th><th style="width:220px">Progress</th><th>Speed</th><th>ETA</th><th></th></tr></thead><tbody>
    ${items.map((q) => html`<tr data-key="q${q.id}">
      <td><div class="col" style="gap:3px;max-width:460px"><a href="${q.kind === 'movie' ? `#/movie/${q.movie_id}` : `#/tv/${q.series_id}`}" style="color:var(--text);font-weight:500">${q.label}</a><span class="tiny muted ellipsis" title="${q.title}">${q.title}</span>
        ${q.status === 'import_blocked' ? html`<div class="notice warn" style="margin-top:6px">${q.message}</div>` : ''}</div></td>
      <td class="small" style="color:var(--text2)">${q.quality}</td>
      <td>${q.status === 'importing' ? html`<span class="small state-downloading">${spinner} Importing…</span>` : html`<div class="row" style="gap:10px;flex-wrap:nowrap"><div class="progress grow"><div style="width:${pct(q.progress)}"></div></div><span class="tiny muted" style="width:36px">${pct(q.progress)}</span></div><div class="tiny muted" style="margin-top:4px">${stateLabel(q.state)}</div>`}</td>
      <td class="small" style="color:var(--text2)">${q.dlspeed ? `${fmtBytes(q.dlspeed)}/s` : '—'}</td>
      <td class="small" style="color:var(--text2)">${q.progress >= 1 ? 'Done' : fmtEta(q.eta)}</td>
      <td><div class="row" style="gap:6px;justify-content:flex-end;flex-wrap:nowrap">
        ${q.status === 'import_blocked' ? html`<button class="btn sm" data-act="retryImport" data-id="${q.id}">${ic('refresh', 14)}Retry import</button>` : ''}
        <button class="btn icon" data-act="queueRemove" data-id="${q.id}" aria-label="Remove and find another release" title="Remove and find another release">${ic('x', 15)}</button></div></td>
    </tr>`)}</tbody></table></div></section>`;
}
function stateLabel(s) {
  return ({ downloading: 'Downloading', stalledDL: 'Waiting for peers', metaDL: 'Getting file list', forcedMetaDL: 'Getting file list', queuedDL: 'Queued in qBittorrent', pausedDL: 'Paused', stoppedDL: 'Paused', checkingDL: 'Checking', forcedDL: 'Downloading', uploading: 'Seeding', stalledUP: 'Seeding', pausedUP: 'Finished', stoppedUP: 'Finished', queuedUP: 'Finished' })[s] || s || '';
}
A.queueRemove = (el) => {
  const q = (S.v.queue || []).find((x) => String(x.id) === el.dataset.id);
  S.modal = { type: 'removeQueue', id: el.dataset.id, title: q ? q.label : '', form: { blocklist: true, search: true } };
  render();
};
A.confirmRemoveQueue = () => busy('removeQueue', async () => {
  const m = S.modal;
  await api(`/api/queue/${m.id}?blocklist=${m.form.blocklist}&search=${m.form.search}`, { method: 'DELETE' });
  S.modal = null; toast('Removed from the queue', 'good'); await refreshStatus(); await loadView();
});
A.retryImport = async (el) => { const r = await api(`/api/queue/${el.dataset.id}/import`, { method: 'POST' }); toast(r.result, r.result === 'Imported' ? 'good' : 'bad'); await loadView(); };
function historyList(items) {
  return html`<section class="card" aria-label="History">
    <div class="card-head"><h2>History</h2>${select('ui.histEvent', [['', 'All events'], ['grabbed', 'Grabbed'], ['imported', 'Imported'], ['failed', 'Failed'], ['subtitle', 'Subtitles'], ['added', 'Added'], ['deleted', 'Deleted'], ['removed', 'Removed']], 'data-onchange="reloadView" style="width:auto;height:36px" aria-label="Filter events"')}</div>
    ${!items.length ? html`<div class="muted">No history yet.</div>` : html`<div class="col" style="gap:12px">${items.map((h) => html`<div class="row" data-key="h${h.id}" style="gap:14px;flex-wrap:nowrap;align-items:flex-start">${eventBadge(h.event)}<span class="grow" style="min-width:0"><b style="font-weight:500">${h.title}</b> <span class="muted" style="word-break:break-word">${h.message}</span></span><span class="muted small" style="flex:none" title="${new Date(h.at * 1000).toLocaleString()}">${fmtAgo(h.at)}</span></div>`)}</div>`}
  </section>`;
}
function blockedList(items) {
  return html`<section class="card" aria-label="Blocked releases"><div class="card-head"><div><h2>Blocked releases</h2><div class="sub">Releases that failed. They're skipped in future searches. Remove one to allow it again.</div></div></div>
    ${!items.length ? html`<div class="muted">Nothing blocked.</div>` : html`<div class="col" style="gap:10px">${items.map((b) => html`<div class="row" data-key="b${b.id}" style="flex-wrap:nowrap"><div class="col grow" style="gap:2px;min-width:0"><span class="ellipsis" style="font-weight:500">${b.title}</span><span class="tiny muted">${b.reason} · ${b.indexer_name || ''} · ${fmtAgo(b.at)}</span></div><button class="btn sm" data-act="unblock" data-id="${b.id}">Allow again</button></div>`)}</div>`}</section>`;
}
A.unblock = async (el) => { await api(`/api/blocklist/${el.dataset.id}`, { method: 'DELETE' }); await loadView(); };
A.reloadView = () => loadView();
function logsList(items) {
  const color = { ERROR: 'var(--red-text)', WARNING: 'var(--orange-text)', INFO: 'var(--text2)' };
  return html`<section class="card" aria-label="Logs"><div class="card-head"><h2>Logs</h2>${select('ui.logLevel', [['', 'Info and above'], ['WARNING', 'Warnings and errors'], ['ERROR', 'Errors only']], 'data-onchange="reloadView" style="width:auto;height:36px" aria-label="Log level"')}</div>
    <div class="col mono" style="gap:6px;max-height:70vh;overflow:auto">${items.length ? items.map((l) => html`<div style="color:${color[l.level] || 'var(--muted)'};white-space:pre-wrap;word-break:break-word"><span class="muted">${l.time.slice(5).replace('T', ' ')}</span> ${l.level === 'INFO' ? '' : `[${l.level}] `}<span class="muted">${l.logger}:</span> ${l.message}</div>`) : html`<span class="muted">Nothing logged yet.</span>`}</div></section>`;
}
function tasksList(d) {
  return html`<section class="card" aria-label="Scheduled tasks"><div class="card-head"><div><h2>Scheduled tasks</h2><div class="sub">These run on their own. You can run one now if you don't want to wait.</div></div>${btn('Search all missing now', 'searchAllMissing', { busyKey: 'searchAll', icon: 'search' })}</div>
    <div class="col" style="gap:0">${d.tasks.map((t) => html`<div class="list-row" data-key="t${t.name}" style="padding:12px 0">
      <div class="col grow" style="gap:2px;flex-basis:240px"><span style="font-weight:500">${t.label}</span><span class="tiny muted">Every ${fmtInterval(t.interval)} · ${t.last_run ? `last run ${fmtAgo(t.last_run)}: ${t.last_result}` : 'not run yet'}</span></div>
      ${t.running ? html`<span class="small state-downloading">${spinner} Running</span>` : html`<button class="btn sm" data-act="runTask" data-name="${t.name}">Run now</button>`}</div>`)}</div></section>
    <section class="card" aria-label="Searches"><h2 style="margin-bottom:12px">Recent searches</h2>
    ${d.items.length ? html`<div class="col" style="gap:10px">${d.items.map((j) => html`<div class="row" data-key="j${j.id}" style="flex-wrap:nowrap;gap:10px"><span style="flex:none">${j.state === 'running' ? spinner : j.state === 'queued' ? html`<span class="dot"></span>` : j.state === 'failed' ? html`<span class="dot bad"></span>` : html`<span class="dot ok"></span>`}</span><span class="grow ellipsis"><b style="font-weight:500">${j.label}</b> <span class="muted">${j.message || (j.state === 'queued' ? 'Waiting…' : '')}</span></span></div>`)}</div>` : html`<div class="muted">No searches yet.</div>`}</section>`;
}
function fmtInterval(s) { return s < 120 ? `${s} s` : s < 7200 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} days`; }
A.runTask = async (el) => { toast(`Running: ${el.closest('.list-row').querySelector('span').textContent}`); const p = api(`/api/tasks/${el.dataset.name}`, { method: 'POST' }); setTimeout(loadView, 300); const r = await p; toast(r.result, 'good'); await loadView(); };
A.searchAllMissing = () => busy('searchAll', async () => { const r = await api('/api/tasks/search_missing', { method: 'POST' }); toast(r.result, 'good'); await refreshStatus(); });

// ============================================================ CONNECTIONS ===
VIEWS.settings = {
  async load() {
    const [s, ix] = await Promise.all([api('/api/settings'), api('/api/indexers')]);
    S.v.settings = s; S.settingsCache = s.settings; S.v.indexers = ix.items;
    if (!S.v.loaded) { S.form = clone(s.settings); S.form._mapOpen = (s.settings.qbittorrent.path_mappings || []).length > 0; schedulePreview(); }
    S.v.loaded = true;
    const focus = S.route.query.get('focus');
    if (focus && !S.v.focused) { S.v.focused = true; setTimeout(() => { const el = document.getElementById(`sec-${focus}`); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 50); }
  },
  poll: 15000,
  pollFn: async () => { S.v.indexers = (await api('/api/indexers')).items; },
  render() {
    return html`
      <div class="col" style="gap:6px"><h1>Connections</h1><p class="muted">Everything Sonarr, Radarr, Prowlarr and Bazarr kept in four places lives here.</p></div>
      ${qbitSection()}${indexersSection()}${unblockSection()}
      <div class="split">${foldersSection()}${qualitySection()}</div>
      ${namingSection()}${subtitlesSection()}${searchRulesSection()}${securitySection()}`;
  },
};
function saveBtn(section) { return btn('Save', 'saveSection', { cls: 'primary', busyKey: `save-${section}`, attrs: `data-section="${section}"` }); }
A.saveSection = (el) => busy(`save-${el.dataset.section}`, async () => {
  const sec = el.dataset.section;
  const body = clone(S.form[sec]);
  const r = await api(`/api/settings/${sec}`, { method: 'PUT', body });
  S.form[sec] = { ...S.form[sec], ...r.values }; S.settingsCache[sec] = r.values;
  toast('Saved', 'good'); await refreshStatus();
});

function qbitSection() {
  const f = S.form.qbittorrent; const st = S.status && S.status.qbittorrent;
  const linked = st && st.ok;
  const chip = linked ? html`<span class="chip green">${st.message}</span>` : st && st.ok === false ? html`<span class="chip red">Can't reach it</span>` : html`<span class="chip orange">Not linked yet</span>`;
  const res = S.ui.qbitResult;
  return html`<section class="card ${linked ? '' : 'focus'}" id="sec-qbittorrent" aria-label="qBittorrent">
    <div class="card-head"><div><h2>qBittorrent</h2><div class="sub">In qBittorrent turn on Web UI (Tools → Options → Web UI), then fill this in.</div></div>${chip}</div>
    <div class="form-grid">
      ${field('Address', input('form.qbittorrent.url', { placeholder: 'http://192.168.1.10:8080' }))}
      ${field('Username', input('form.qbittorrent.username', { attrs: 'autocomplete="off"' }))}
      ${field('Password', input('form.qbittorrent.password', { type: 'password', attrs: 'autocomplete="new-password"' }))}
      ${field('Category', input('form.qbittorrent.category'), 'Only torrents in this category are touched.')}
    </div>
    ${res ? html`<div class="notice ${res.ok ? (res.hint ? 'warn' : 'good') : 'bad'}" style="margin-top:14px">${res.ok ? html`Linked to qBittorrent ${res.version}. ${res.hint || (res.save_path ? `Downloads land in ${res.save_path} — this app can see them.` : '')}` : res.error}</div>` : ''}
    <div class="row" style="margin-top:16px">${btn('Test and save', 'testQbit', { cls: 'primary', busyKey: 'testQbit' })}
      <button class="btn" data-act="toggleMap">${ic(S.form._mapOpen ? 'chev' : 'chevr', 16)}Folder mapping (advanced)</button></div>
    ${S.form._mapOpen ? html`<div class="col" style="margin-top:16px;gap:10px">
      <p class="small muted">Only needed if qBittorrent sees the download folder under a different path than this app. Example: qBittorrent on Windows saves to <span class="mono">D:\\Downloads</span>, which is mounted here as <span class="mono">/data/torrents</span>.</p>
      ${(f.path_mappings || []).map((m, i) => html`<div class="row" style="flex-wrap:nowrap" data-key="map${i}">${input(`form.qbittorrent.path_mappings.${i}.remote`, { cls: 'mono', placeholder: 'Path in qBittorrent' })}<span class="muted">→</span>${input(`form.qbittorrent.path_mappings.${i}.local`, { cls: 'mono', placeholder: 'Path in Omniarr' })}<button class="btn icon" data-act="delMap" data-i="${i}" aria-label="Remove mapping">${ic('x', 15)}</button></div>`)}
      <div class="row"><button class="btn sm" data-act="addMap">+ Add mapping</button>${saveBtn('qbittorrent')}</div>
      ${check('form.qbittorrent.remove_completed', 'Remove torrents from qBittorrent once imported and their seeding goals are met')}
    </div>` : ''}
  </section>`;
}
A.testQbit = () => busy('testQbit', async () => {
  try {
    const r = await api('/api/test/qbittorrent', { method: 'POST', body: S.form.qbittorrent });
    S.ui.qbitResult = r; toast('qBittorrent linked', 'good');
    if (r.hint) S.form._mapOpen = true;
  } catch (e) { S.ui.qbitResult = { ok: false, error: e.message }; }
  await refreshStatus();
});
A.toggleMap = () => { S.form._mapOpen = !S.form._mapOpen; render(); };
A.addMap = () => { (S.form.qbittorrent.path_mappings = S.form.qbittorrent.path_mappings || []).push({ remote: '', local: '' }); render(); };
A.delMap = (el) => { S.form.qbittorrent.path_mappings.splice(Number(el.dataset.i), 1); render(); };

function healthDot(h) { return { ok: 'ok', new: '', paused: 'warn', error: 'bad', off: '' }[h.state] || ''; }
function indexersSection() {
  const items = S.v.indexers || [];
  return html`<section class="card" id="sec-indexers" aria-label="Indexers">
    <div class="card-head"><div><h2>Indexers</h2><div class="sub">Built in — replaces Prowlarr. Uses Prowlarr's site list, so most trackers are one click. Shared by TV and movies.</div></div><div class="row" style="gap:8px">${btn('Add recommended public sites', 'addRecommended', { cls: 'sm', busyKey: 'addRecommended', attrs: 'style="height:40px"' })}<a class="btn" href="#/indexers/add" style="height:40px">+ Add indexer</a></div></div>
    ${!items.length ? html`<div class="empty">${ic('search', 28, 1.5)}<span>No indexers yet — add popular English public sites in one click, or add the trackers you use.</span><div class="row">${btn('Add recommended public sites', 'addRecommended', { cls: 'primary', busyKey: 'addRecommended' })}<a class="btn" href="#/indexers/add">Add indexer</a></div></div>`
    : html`<div class="col" style="gap:8px">${items.map((ix) => html`<div class="list-row click" data-act="editIndexer" data-id="${ix.id}" data-key="ix${ix.id}" style="border-radius:10px;background:var(--card2);border:0">
      <span class="grow" style="font-weight:500;flex-basis:160px">${ix.name}</span>
      <span class="chip">${ix.kind === 'torznab' ? 'Torznab' : ix.type === 'private' ? 'Private' : ix.type === 'semi-private' ? 'Semi-private' : 'Public'}</span>
      ${ix.flaresolverr ? html`<span class="chip purple">Cloudflare bypass</span>` : ''}${ix.use_proxy ? html`<span class="chip blue">Via proxy</span>` : ''}${!ix.enabled ? html`<span class="chip">Off</span>` : ''}
      <span class="small muted">${ix.query_limit ? `${ix.queries_today} / ${ix.query_limit} queries today` : `${ix.queries_today} queries today`}</span>
      <span class="row small" style="gap:8px;flex-wrap:nowrap;color:${{ ok: 'var(--green-text)', paused: 'var(--orange-text)', error: 'var(--red-text)' }[ix.health.state] || 'var(--muted)'}"><span class="dot ${healthDot(ix.health)}" style="${ix.health.state === 'ok' ? '' : 'background:currentColor'}"></span><span class="ellipsis" style="max-width:340px" title="${ix.health.text}">${ix.health.text}</span></span>
    </div>`)}</div>`}
  </section>`;
}

A.addRecommended = () => busy('addRecommended', async () => {
  const r = await api('/api/indexers/recommended', { method: 'POST' });
  toast(r.added.length ? `Added ${r.added.join(', ')}` : 'All recommended sites are already added', 'good');
  S.v.indexers = (await api('/api/indexers')).items; await refreshStatus();
});

function unblockSection() {
  const u = S.form.unblock; const fs = S.ui.fsResult;
  return html`<section class="card" id="sec-unblock" aria-label="Staying unblocked">
    <div class="card-head"><div><h2>Staying unblocked</h2><div class="sub">Keeps indexers reachable and stops you hitting their limits. On by default — you shouldn't need to touch this.</div></div>${saveBtn('unblock')}</div>
    <div class="form-grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">
      <div class="col" style="background:var(--card2);border-radius:12px;padding:16px;gap:10px">
        <div class="row between"><b>Cloudflare bypass (FlareSolverr)</b>${fs ? html`<span class="small row" style="gap:6px;color:${fs.ok ? 'var(--green-text)' : 'var(--red-text)'}"><span class="dot ${fs.ok ? 'ok' : 'bad'}"></span>${fs.ok ? 'Running' : 'Not reachable'}</span>` : ''}</div>
        <span class="small muted">Included in the Docker setup. Used only by indexers that need it, and switched on automatically when a site starts showing a browser check.</span>
        ${field('Address', input('form.unblock.flaresolverr_url', { placeholder: 'http://flaresolverr:8191' }))}
        ${fs && !fs.ok ? html`<span class="tiny" style="color:var(--red-text)">${fs.message}</span>` : ''}
        <div>${btn('Test', 'testFs', { cls: 'sm', busyKey: 'testFs' })}</div>
      </div>
      <div class="col" style="background:var(--card2);border-radius:12px;padding:16px;gap:8px">
        <b>Polite searching</b><span class="small muted">Respects each site's limits so accounts and IPs don't get flagged.</span>
        ${field('Minimum seconds between requests to the same site', input('form.unblock.min_interval', { type: 'number', attrs: 'min="0" step="0.5"' }))}
        ${check('form.unblock.daily_limits', 'Respect daily query and download limits set on each indexer')}
        ${check('form.unblock.backoff', 'Back off automatically after errors (5 min → 6 h)')}
        ${field('Cache search results (minutes)', input('form.unblock.cache_minutes', { type: 'number', attrs: 'min="0"' }))}
      </div>
      <div class="col" style="background:var(--card2);border-radius:12px;padding:16px;gap:10px">
        <b>Proxy (if your internet provider blocks a site)</b>
        <span class="small muted">Blocked sites are sent through this automatically. The docker-compose file includes a free one (Cloudflare WARP): <span class="mono">http://warp:1080</span>. A VPN provider's SOCKS5 proxy works too.</span>
        ${field('Proxy address', input('form.unblock.proxy_url', { placeholder: 'http://warp:1080' }))}
        ${S.ui.proxyResult ? html`<span class="tiny" style="color:${S.ui.proxyResult.ok ? 'var(--green-text)' : 'var(--red-text)'}">${S.ui.proxyResult.message}</span>` : ''}
        <div>${btn('Test proxy', 'testProxy', { cls: 'sm', busyKey: 'testProxy' })}</div>
        ${check('form.unblock.detect_isp_block', 'Spot internet-provider block pages')}
        ${check('form.unblock.auto_proxy', 'Automatically route blocked sites through the proxy')}
      </div>
    </div>
  </section>`;
}
A.testProxy = () => busy('testProxy', async () => {
  try { const r = await api('/api/test/proxy', { method: 'POST', body: { url: S.form.unblock.proxy_url } }); S.ui.proxyResult = { ok: true, message: r.message }; } catch (e) { S.ui.proxyResult = { ok: false, message: e.message }; }
});
A.testFs = () => busy('testFs', async () => {
  try { const r = await api('/api/test/flaresolverr', { method: 'POST', body: { url: S.form.unblock.flaresolverr_url } }); S.ui.fsResult = { ok: true, message: r.message }; } catch (e) { S.ui.fsResult = { ok: false, message: e.message }; }
});

function foldersSection() {
  const st = S.status && S.status.folders;
  const fs = (k) => st && st[k] ? (st[k].writable ? html`<span class="tiny" style="color:var(--green-text)">✓ Found${st[k].free ? ` · ${fmtBytes(st[k].free)} free` : ''}</span>` : html`<span class="tiny" style="color:var(--orange-text)">${st[k].exists ? "Can't write here" : "Doesn't exist yet — Save creates it"}</span>`) : '';
  return html`<section class="card col" id="sec-folders" aria-label="Library folders" style="flex:1 1 320px;gap:14px">
    <h2>Library folders</h2>
    <label class="field">TV shows${input('form.folders.tv', { cls: 'mono' })}${fs('tv')}</label>
    <label class="field">Movies${input('form.folders.movies', { cls: 'mono' })}${fs('movies')}</label>
    ${check('form.folders.use_hardlinks', 'Hardlink instead of copy (keeps seeding, no extra space)')}
    <span class="tiny muted">Hardlinks need downloads and library on the same drive — the docker-compose file sets that up with one /data folder.</span>
    <div>${saveBtn('folders')}</div>
  </section>`;
}
function qualitySection() {
  const profs = (S.v.settings.profiles || []).map((p) => [p.id, p.name]);
  const t = S.ui.tmdbResult;
  return html`<section class="card col" id="sec-quality" aria-label="Quality and info" style="flex:1 1 320px;gap:14px">
    <h2>Quality and info</h2>
    ${field('Default quality', select('form.quality.default_profile', profs))}
    ${field('Audio language', select('form.language.mode', [['english', 'English only (original or English dub)'], ['any', 'Any language']]))}
    <div class="col" style="gap:8px;padding:12px 14px;border-radius:10px;background:var(--card2);font-size:13px;color:var(--text2)">
      <span><span style="color:var(--green-text)">Allowed</span> English, English dub (DUB, DUBBED), Dual Audio or MULTi that includes English</span>
      <span><span style="color:var(--red-text)">Rejected</span> Foreign-only audio (e.g. FRENCH, GERMAN, iTA), subbed-only anime</span>
      ${check('form.language.anime_prefer_dub', 'For anime, prefer English dub over original audio')}
    </div>
    <label class="field">Movie info key (free TMDB key)${input('form.metadata.tmdb_key', { type: 'password', attrs: 'autocomplete="off"' })}<span class="tiny muted">Get one free at themoviedb.org → Settings → API. TV info comes from TVMaze and needs no key.</span></label>
    ${t ? html`<div class="notice ${t.ok ? 'good' : 'bad'}">${t.ok ? 'TMDB key works' : t.message}</div>` : ''}
    <div class="row">${btn('Save', 'saveQuality', { cls: 'primary', busyKey: 'saveQuality' })}${btn('Test key', 'testTmdb', { busyKey: 'testTmdb' })}</div>
  </section>`;
}
A.saveQuality = () => busy('saveQuality', async () => {
  for (const sec of ['quality', 'language', 'metadata']) {
    const r = await api(`/api/settings/${sec}`, { method: 'PUT', body: S.form[sec] });
    S.form[sec] = { ...S.form[sec], ...r.values }; S.settingsCache[sec] = r.values;
  }
  toast('Saved', 'good'); await refreshStatus();
});
A.testTmdb = () => busy('testTmdb', async () => {
  try { await api('/api/test/tmdb', { method: 'POST', body: { key: S.form.metadata.tmdb_key } }); S.ui.tmdbResult = { ok: true }; } catch (e) { S.ui.tmdbResult = { ok: false, message: e.message }; }
});

const NAMING_PRESETS = {
  standard: { episode: '{Show Title} - S{season:00}E{episode:00} - {Episode Title} [{Quality}]', movie_file: '{Movie Title} ({Year}) [{Quality}]' },
  noquality: { episode: '{Show Title} - S{season:00}E{episode:00} - {Episode Title}', movie_file: '{Movie Title} ({Year})' },
  minimal: { episode: '{Show Title} - S{season:00}E{episode:00}', movie_file: '{Movie Title} ({Year})' },
};
function namingSection() {
  const p = S.ui.namingPreview;
  const presetOn = (k) => S.form.naming.episode === NAMING_PRESETS[k].episode && S.form.naming.movie_file === NAMING_PRESETS[k].movie_file;
  const nf = (path, label) => field(label, input(path, { cls: 'mono', attrs: `data-naming="1" data-oninput="namingChanged"` }));
  return html`<section class="card" id="sec-naming" aria-label="File naming">
    <div class="card-head"><div><h2>File naming</h2><div class="sub">How folders and files are named when something is imported. Works out of the box for Plex, Jellyfin and Emby.</div></div><div class="row">${check('form.naming.rename', 'Rename files')}${saveBtn('naming')}</div></div>
    <div class="split" style="gap:24px">
      <div class="col" style="flex:3 1 420px;gap:14px">
        ${nf('form.naming.series_folder', 'Show folder')}${nf('form.naming.season_folder', 'Season folder')}${nf('form.naming.episode', 'Episode file')}${nf('form.naming.movie_folder', 'Movie folder')}${nf('form.naming.movie_file', 'Movie file')}
        <div class="col" style="gap:8px"><span class="small muted">Click to insert into the last field you clicked</span><div class="row" style="gap:8px">${(S.v.settings.naming_tokens || []).map((t) => html`<button class="token" data-act="insertToken" data-t="${t}">${t}</button>`)}</div></div>
        <div class="row" style="gap:8px"><span class="small muted">Presets:</span>${[['standard', 'Standard'], ['noquality', 'No quality tag'], ['minimal', 'Minimal (S01E02 only)']].map(([k, l]) => html`<button class="pill ${presetOn(k) ? 'on' : ''}" data-act="namingPreset" data-k="${k}">${l}</button>`)}</div>
      </div>
      <div class="tree" style="flex:2 1 320px" aria-label="Live preview">
        <span class="tiny muted" style="font-family:var(--font);text-transform:uppercase;letter-spacing:.04em">Live preview</span>
        ${p ? html`<span>${S.form.folders.tv}/</span><span style="padding-left:16px">${p.series_folder}/</span><span style="padding-left:32px">${p.season_folder}/</span>
          <span class="f" style="padding-left:48px">${p.episode}.mkv</span><span class="s" style="padding-left:48px">${p.episode}.en.srt</span><span class="f" style="padding-left:48px">${p.episode2}.mkv</span><span class="f" style="padding-left:48px">${p.multi}.mkv</span>
          <span style="padding-left:32px">${p.specials_folder}/ <span class="muted">(specials)</span></span>
          <span style="padding-top:8px">${S.form.folders.movies}/</span><span style="padding-left:16px">${p.movie_folder}/</span><span class="f" style="padding-left:32px">${p.movie_file}.mkv</span><span class="s" style="padding-left:32px">${p.movie_file}.en.srt</span>` : spinner}
      </div>
    </div>
  </section>`;
}
const schedulePreview = debounce(async () => {
  try { S.ui.namingPreview = await api('/api/naming/preview', { method: 'POST', body: S.form.naming }); render(); } catch (_) { /* ignore */ }
}, 250);
A.namingChanged = () => schedulePreview();
A.insertToken = (el) => {
  const path = S.ui.lastNamingField || 'form.naming.episode';
  setPath(S, path, (getPath(S, path) || '') + el.dataset.t);
  render(); schedulePreview();
};
A.namingPreset = (el) => { Object.assign(S.form.naming, NAMING_PRESETS[el.dataset.k]); render(); schedulePreview(); };

function subtitlesSection() {
  const s = S.form.subtitles;
  const res = S.ui.subTest || {};
  const prov = (p, i) => {
    const r = res[p.id];
    const label = p.id === 'opensubtitles' ? 'OpenSubtitles.com' : 'Podnapisi';
    const note = p.id === 'opensubtitles' ? 'Best coverage. Free account + API key (opensubtitles.com → Profile → API consumers).' : 'No account needed.';
    return html`<div class="col" data-key="prov${p.id}" style="gap:10px;padding:14px;border-radius:10px;background:var(--card2)">
      <div class="row between"><div class="row" style="gap:10px"><span class="muted small">${i + 1}</span><b>${label}</b></div>${check(`form.subtitles.providers.${i}.enabled`, 'On')}</div>
      <span class="tiny muted">${note}</span>
      ${p.id === 'opensubtitles' ? html`<div class="form-grid">${field('API key', input(`form.subtitles.providers.${i}.api_key`, { type: 'password', attrs: 'autocomplete="off"' }))}${field('Username', input(`form.subtitles.providers.${i}.username`, { attrs: 'autocomplete="off"' }))}${field('Password', input(`form.subtitles.providers.${i}.password`, { type: 'password', attrs: 'autocomplete="new-password"' }))}</div>` : ''}
      <div class="row">${btn('Test', 'testSub', { cls: 'sm', busyKey: `sub-${p.id}`, attrs: `data-i="${i}"` })}${r ? html`<span class="small" style="color:${r.ok ? 'var(--green-text)' : 'var(--red-text)'}">${r.message}</span>` : ''}</div>
    </div>`;
  };
  return html`<section class="card" id="sec-subtitles" aria-label="Subtitles">
    <div class="card-head"><div><h2>Subtitles</h2><div class="sub">Built in — replaces Bazarr. Fetched right after each import and saved next to the video.</div></div><div class="row">${check('form.subtitles.enabled', 'Download subtitles')}${saveBtn('subtitles')}</div></div>
    <div class="split" style="gap:24px">
      <div class="col" style="flex:1 1 320px;gap:14px">
        ${field('Languages', select('form.subtitles.languages.0', [['en', 'English']]))}
        ${field('Type', select('form.subtitles.mode', [['normal_hi_fallback', 'Normal + hearing impaired (SDH) as fallback'], ['normal', 'Normal only'], ['hi', 'Hearing impaired (SDH) preferred'], ['forced', 'Forced only (foreign-language parts)']]))}
        ${check('form.subtitles.skip_embedded', 'Skip if the video already has English subtitles inside it')}
        ${check('form.subtitles.retry', 'Keep looking for missing subtitles every 6 hours')}
        ${check('form.subtitles.upgrade', 'Upgrade to a better-matching subtitle when one appears')}
      </div>
      <div class="col" style="flex:1 1 320px;gap:10px"><span class="small muted">Subtitle sources (tried in order)</span>${s.providers.map(prov)}
        <span class="tiny muted">Matches are scored on release group, source and resolution (and an exact file fingerprint on OpenSubtitles), so subtitles line up with your exact file.</span></div>
    </div>
  </section>`;
}
A.testSub = (el) => {
  const i = Number(el.dataset.i); const p = S.form.subtitles.providers[i];
  return busy(`sub-${p.id}`, async () => {
    S.ui.subTest = S.ui.subTest || {};
    try { const r = await api('/api/test/subtitles', { method: 'POST', body: p }); S.ui.subTest[p.id] = { ok: true, message: r.message }; } catch (e) { S.ui.subTest[p.id] = { ok: false, message: e.message }; }
  });
};

function searchRulesSection() {
  return html`<section class="card" id="sec-search" aria-label="Search rules">
    <div class="card-head"><div><h2>Search rules</h2><div class="sub">Fine-tuning for what gets grabbed.</div></div>${saveBtn('search')}</div>
    <div class="form-grid">
      ${field('Minimum seeders', input('form.search.min_seeders', { type: 'number', attrs: 'min="0"' }))}
      ${field('Give up on a stalled download after (hours)', input('form.search.stall_hours', { type: 'number', attrs: 'min="1"' }))}
      ${field('Never grab releases containing (comma separated)', html`<input class="input" data-bind="ui.ignoredWords" data-oninput="ignoredChanged" value="${S.ui.ignoredWords ?? (S.form.search.ignored_words || []).join(', ')}" placeholder="e.g. HDCAM, 3D">`)}
    </div>
    <div class="col" style="gap:4px;margin-top:12px">
      ${check('form.search.reject_hardcoded_subs', 'Skip releases with hard-coded (burned-in) subtitles')}
      ${check('form.search.auto_research_failed', 'When a download fails, block it and search for another')}
      ${check('form.search.rss_enabled', 'Watch indexers for new releases every 15 minutes')}
    </div>
  </section>`;
}
A.ignoredChanged = () => { S.form.search.ignored_words = (S.ui.ignoredWords || '').split(',').map((x) => x.trim()).filter(Boolean); };

function securitySection() {
  return html`<section class="card" id="sec-security" aria-label="Login">
    <div class="card-head"><div><h2>Login</h2><div class="sub">Optional. Ask for a password before anyone on your network can open Omniarr.</div></div>${saveBtn('security')}</div>
    <div class="form-grid">
      ${field('Username', input('form.security.username', { attrs: 'autocomplete="off"' }))}
      ${field('New password', input('form.security.password', { type: 'password', attrs: 'autocomplete="new-password"', placeholder: S.form.security.password_hash ? 'Leave blank to keep current' : '' }))}
    </div>
    <div style="margin-top:12px">${check('form.security.enabled', 'Require login')}</div>
  </section>`;
}

// indexer edit modal --------------------------------------------------------
A.editIndexer = async (el) => {
  const ix = (S.v.indexers || []).find((x) => String(x.id) === el.dataset.id);
  if (!ix) return;
  let schema = [];
  if (ix.kind === 'cardigann') schema = (await api(`/api/definitions/${ix.definition_id}`)).settings;
  S.modal = { type: 'indexer', ix, schema, form: indexerForm(ix, schema), result: null };
  render();
};
function indexerForm(ix, schema) {
  const settingsVals = {};
  schema.forEach((s) => { if (!s.type || !s.type.startsWith('info')) settingsVals[s.name] = ix.settings[s.name] ?? s.default ?? (s.type === 'checkbox' ? false : ''); });
  if (ix.kind === 'torznab') { settingsVals.url = ix.settings.url || ''; settingsVals.api_key = ix.settings.api_key || ''; }
  return { id: ix.id, name: ix.name, kind: ix.kind, definition_id: ix.definition_id, enabled: ix.enabled, priority: ix.priority, use_for: ix.use_for, flaresolverr: ix.flaresolverr, use_proxy: ix.use_proxy, seed_goals: ix.seed_goals, query_limit: ix.query_limit, grab_limit: ix.grab_limit, settings: settingsVals };
}
function indexerFields(base, schema, kind) {
  const out = [];
  if (kind === 'torznab') {
    out.push(field('Torznab address', input(`${base}.settings.url`, { cls: 'mono', placeholder: 'http://jackett:9117/api/v2.0/indexers/xyz/results/torznab/' })));
    out.push(field('API key', input(`${base}.settings.api_key`, { type: 'password', attrs: 'autocomplete="off"' })));
    return out;
  }
  schema.forEach((s) => {
    const path = `${base}.settings.${s.name}`;
    if (s.type && s.type.startsWith('info')) {
      const text = stripTags(s.text || s.default || '').trim();
      if (text && s.type !== 'info_flaresolverr' && s.type !== 'info_category_8000') out.push(html`<div class="notice" style="white-space:pre-line">${s.label && s.label !== s.name ? html`<b>${s.label}</b><br>` : ''}${text}</div>`);
      return;
    }
    const label = s.name === 'base_url' ? 'Site address' : (s.label || s.name);
    if (s.type === 'checkbox') out.push(check(path, label));
    else if (s.type === 'select' && s.options) out.push(field(label, select(path, Object.entries(s.options))));
    else if (s.type === 'multi-select' && s.options) out.push(field(label, select(path, Object.entries(s.options))));
    else out.push(field(label, input(path, { type: s.type === 'password' || /pass|cookie|apikey|api_key|key$/i.test(s.name) ? 'password' : 'text', attrs: 'autocomplete="off"' }), s.name === 'cookie' ? 'Log in to the site in your browser, then copy the Cookie header from its developer tools.' : ''));
  });
  return out;
}
function indexerCommonFields(base) {
  return html`<div class="form-grid">
    ${field('Use for', select(`${base}.use_for`, [['both', 'TV and movies'], ['tv', 'TV only'], ['movies', 'Movies only']]))}
    ${field('Priority (lower wins ties)', input(`${base}.priority`, { type: 'number', attrs: 'min="1" max="50"' }))}
    ${field('Daily search limit', input(`${base}.query_limit`, { type: 'number', placeholder: 'No limit', attrs: 'min="0"' }))}
    ${field('Daily download limit', input(`${base}.grab_limit`, { type: 'number', placeholder: 'No limit', attrs: 'min="0"' }))}
  </div>
  <div class="col" style="gap:2px">
    ${check(`${base}.flaresolverr`, 'Cloudflare bypass (turned on automatically if the site needs it)')}
    ${check(`${base}.use_proxy`, 'Route through proxy')}
    ${check(`${base}.seed_goals`, "Keep seeding to the site's ratio rules")}
  </div>`;
}
function indexerModal(m) {
  const r = m.result;
  return html`
    ${check('modal.form.enabled', 'Turned on')}
    ${field('Name', input('modal.form.name'))}
    ${indexerFields('modal.form', m.schema, m.ix.kind)}
    ${indexerCommonFields('modal.form')}
    ${m.ix.health && m.ix.health.state === 'error' || m.ix.health.state === 'paused' ? html`<div class="notice warn">${m.ix.health.error || m.ix.health.text}</div>` : ''}
    ${r ? testResultNotice(r) : ''}
    <div class="row between"><button class="btn danger" data-act="deleteIndexer">${ic('trash', 16)}Delete</button>
      <div class="row">${btn('Test', 'testIndexer', { busyKey: 'testIndexer' })}${btn('Save', 'saveIndexer', { cls: 'primary', busyKey: 'saveIndexer' })}</div></div>`;
}
function testResultNotice(r) {
  if (r.ok) return html`<div class="notice good">Working — ${plural(r.results, 'recent release')} found in ${(r.ms / 1000).toFixed(1)} s${r.flaresolverr ? ' (Cloudflare bypass was needed and is now on)' : ''}${r.use_proxy ? ' (blocked by your internet provider — going through the proxy)' : ''}.</div>`;
  return html`<div class="notice bad">${r.message}</div>`;
}
A.testIndexer = () => busy('testIndexer', async () => {
  const r = await api('/api/indexers/test', { method: 'POST', body: S.modal.form });
  S.modal.result = r; if (r.flaresolverr) S.modal.form.flaresolverr = true; if (r.use_proxy) S.modal.form.use_proxy = true;
});
A.saveIndexer = () => busy('saveIndexer', async () => {
  await api(`/api/indexers/${S.modal.form.id}`, { method: 'PUT', body: S.modal.form });
  S.modal = null; toast('Indexer saved', 'good'); await loadView(); await refreshStatus();
});
A.deleteIndexer = async () => {
  if (!confirm(`Delete ${S.modal.form.name}?`)) return;
  await api(`/api/indexers/${S.modal.form.id}`, { method: 'DELETE' });
  S.modal = null; toast('Indexer deleted', 'good'); await loadView(); await refreshStatus();
};

// ============================================================ ADD INDEXER ===
VIEWS.indexerAdd = {
  async load() {
    S.ix = S.ix || { q: '', english: true, type: '', custom: false, sel: null, form: null, result: null };
    await loadDefs();
    S.v.loaded = true;
  },
  render() {
    const x = S.ix;
    const items = S.v.defs || [];
    const pill = (on, act, label, attrs = '') => html`<button class="pill ${on ? 'on' : ''}" data-act="${act}" ${raw(attrs)}>${label}</button>`;
    return html`
      <a href="#/settings?focus=indexers" class="small" style="color:var(--muted)">← Connections</a>
      <div class="col" style="gap:6px"><h1>Add an indexer</h1><p class="muted">Pick a site from the list. Its search rules, limits and Cloudflare needs come with it.</p></div>
      <div class="row">
        <label class="searchbox grow" style="flex-basis:320px;height:48px;border-radius:12px">${ic('search', 18)}<input aria-label="Search sites" placeholder="Search ${S.v.defsTotal || 500}+ sites…" data-bind="ix.q" value="${x.q}" data-oninput="defsSearch"></label>
        <div class="row" style="gap:8px">${pill(x.english, 'defsEnglish', 'English')}${pill(x.type === 'public', 'defsType', 'Public', 'data-t="public"')}${pill(x.type === 'private', 'defsType', 'Private', 'data-t="private"')}${pill(x.custom, 'defsCustom', 'Custom Torznab link')}</div>
      </div>
      <div class="split">
        ${x.custom ? html`<section class="card col" style="flex:3 1 440px;gap:10px"><h2>Custom Torznab</h2><p class="small muted">For anything that speaks Torznab — e.g. an existing Jackett or Prowlarr, or a site that gives you a Torznab link.</p></section>`
    : html`<section class="card flush" aria-label="Sites" style="flex:3 1 440px">
          ${!items.length ? html`<div class="empty">No sites match.</div>` : items.map((d) => html`<div class="list-row click" data-key="d${d.id}" data-act="pickDef" data-id="${d.id}" style="${x.sel && x.sel.id === d.id ? 'background:#1A2236' : ''}">
            <div class="col grow" style="gap:3px;flex-basis:200px"><span style="font-size:15px;font-weight:600">${d.name}</span><span class="tiny muted ellipsis">${d.description}</span></div>
            <span class="chip">${d.type === 'private' ? 'Private' : d.type === 'semi-private' ? 'Semi-private' : 'Public'}</span>
            ${d.kinds.length === 1 ? html`<span class="chip">${d.kinds[0] === 'tv' ? 'TV only' : 'Movies only'}</span>` : ''}
            ${d.flaresolverr_hint ? html`<span class="chip purple">Needs Cloudflare bypass</span>` : ''}
            ${d.unsupported ? html`<span class="chip red" title="${d.unsupported}">Not supported</span>` : ''}
            ${d.added ? html`<span class="small" style="color:var(--green-text)">Added</span>` : ''}
          </div>`)}
          ${S.v.defsTotal > items.length ? html`<div class="list-row muted small">Showing ${items.length} of ${S.v.defsTotal} — search to narrow down.</div>` : ''}
        </section>`}
        ${(x.sel || x.custom) && x.form ? indexerSetupPanel(x) : ''}
      </div>`;
  },
};
async function loadDefs() {
  const x = S.ix;
  const p = new URLSearchParams({ q: x.q || '', type: x.type || '', lang: x.english ? 'en' : '' });
  const r = await api(`/api/definitions?${p}`);
  S.v.defs = r.items; S.v.defsTotal = r.total;
}
A.defsSearch = debounce(async () => { await loadDefs(); render(); }, 250);
A.defsEnglish = async () => { S.ix.english = !S.ix.english; await loadDefs(); render(); };
A.defsType = async (el) => { S.ix.type = S.ix.type === el.dataset.t ? '' : el.dataset.t; await loadDefs(); render(); };
A.defsCustom = () => {
  S.ix.custom = !S.ix.custom; S.ix.sel = null; S.ix.result = null;
  S.ix.form = S.ix.custom ? { kind: 'torznab', name: '', enabled: true, priority: 25, use_for: 'both', flaresolverr: false, use_proxy: false, seed_goals: true, settings: { url: '', api_key: '' } } : null;
  render();
};
A.pickDef = async (el) => {
  const d = await api(`/api/definitions/${el.dataset.id}`);
  const vals = {};
  d.settings.forEach((s) => { if (!s.type || !s.type.startsWith('info')) vals[s.name] = s.default ?? (s.type === 'checkbox' ? false : ''); });
  S.ix.sel = d; S.ix.result = null; S.ix.custom = false;
  S.ix.form = { kind: 'cardigann', definition_id: d.id, name: d.name, enabled: true, priority: 25, use_for: d.kinds.length === 1 ? (d.kinds[0] === 'tv' ? 'tv' : 'movies') : 'both', flaresolverr: !!d.flaresolverr_hint, use_proxy: false, seed_goals: true, query_limit: null, grab_limit: null, settings: vals };
  render();
};
function indexerSetupPanel(x) {
  const d = x.sel;
  const r = x.result;
  return html`<aside class="card focus col" aria-label="Set up site" style="flex:2 1 340px;gap:16px;position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto">
    <div class="col" style="gap:4px"><span class="small muted">Setting up</span><span style="font-size:18px;font-weight:600">${d ? d.name : 'Custom Torznab'}</span>
      ${d ? html`<span class="small muted">${d.type === 'private' ? 'Private' : d.type === 'semi-private' ? 'Semi-private' : 'Public'} · ${d.kinds.map((k) => (k === 'tv' ? 'TV' : 'Movies')).join(', ')}${d.request_delay ? ` · max 1 search per ${d.request_delay} s` : ''}</span>` : ''}</div>
    ${d && d.unsupported ? html`<div class="notice bad">${d.unsupported}</div>` : ''}
    ${!d ? field('Name', input('ix.form.name', { placeholder: 'My indexer' })) : ''}
    ${indexerFields('ix.form', d ? d.settings : [], d ? 'cardigann' : 'torznab')}
    ${indexerCommonFields('ix.form')}
    ${r ? testResultNotice(r) : ''}
    <div class="row">${btn('Test', 'ixTest', { busyKey: 'ixTest' })}${btn(r && !r.ok ? 'Add anyway' : 'Test and add', 'ixAdd', { cls: 'primary', busyKey: 'ixAdd', disabled: !!(d && d.unsupported) })}</div>
  </aside>`;
}
A.ixTest = () => busy('ixTest', async () => {
  const r = await api('/api/indexers/test', { method: 'POST', body: S.ix.form });
  S.ix.result = r; if (r.flaresolverr) S.ix.form.flaresolverr = true; if (r.use_proxy) S.ix.form.use_proxy = true;
});
A.ixAdd = () => busy('ixAdd', async () => {
  if (!S.ix.result || S.ix.result.ok) {
    const r = await api('/api/indexers/test', { method: 'POST', body: S.ix.form });
    S.ix.result = r; if (r.flaresolverr) S.ix.form.flaresolverr = true; if (r.use_proxy) S.ix.form.use_proxy = true;
    if (!r.ok) { render(); return; }
  }
  await api('/api/indexers', { method: 'POST', body: S.ix.form });
  toast(`Added ${S.ix.form.name}`, 'good');
  S.ix = null; await refreshStatus(); go('#/settings?focus=indexers');
});

// ================================================================== MODALS ===
function modalView() {
  const m = S.modal;
  let title = ''; let body = ''; let wide = false;
  if (m.type === 'releases') { title = m.title; body = releasesModal(m); wide = true; }
  if (m.type === 'edit') {
    title = `Edit ${m.title}`;
    body = html`${field('Quality', select('modal.form.quality_profile', m.profiles.map((p) => [p.id, `${p.name} — ${p.description}`])))}
      ${field('Audio language', select('modal.form.language_mode', [['default', `Use default (${(S.settingsCache && S.settingsCache.language.mode) === 'any' ? 'any language' : 'English only'})`], ['english', 'English only (incl. English dub)'], ['any', 'Any language']]))}
      ${check('modal.form.monitored', m.kind === 'tv' ? 'Monitored (grab new episodes)' : 'Monitored (grab when available)')}
      <div class="row" style="justify-content:flex-end"><button class="btn" data-act="closeModal">Cancel</button>${btn('Save', 'saveEdit', { cls: 'primary', busyKey: 'saveEdit' })}</div>`;
  }
  if (m.type === 'delete') {
    title = `Remove ${m.title}?`;
    body = html`<p class="muted">It will be taken out of your library and nothing more will be downloaded for it.</p>
      ${check('modal.form.delete_files', 'Also delete its folder and files from disk')}
      <div class="row" style="justify-content:flex-end"><button class="btn" data-act="closeModal">Cancel</button>${btn('Remove', 'confirmDelete', { cls: 'danger', busyKey: 'confirmDelete' })}</div>`;
  }
  if (m.type === 'removeQueue') {
    title = 'Remove download?';
    body = html`<p class="muted">${m.title}</p>
      ${check('modal.form.blocklist', "Block this release so it isn't picked again")}
      ${check('modal.form.search', 'Search for another release')}
      <div class="row" style="justify-content:flex-end"><button class="btn" data-act="closeModal">Cancel</button>${btn('Remove', 'confirmRemoveQueue', { cls: 'danger', busyKey: 'removeQueue' })}</div>`;
  }
  if (m.type === 'indexer') { title = m.ix.name; body = indexerModal(m); }
  return html`<div class="overlay" data-act="overlayClick" data-self="1"><div class="dialog ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-label="${title}" data-stop="1">
    <div class="dialog-head"><h2 style="font-size:18px">${title}</h2><button class="btn icon" data-act="closeModal" aria-label="Close">${ic('x', 16)}</button></div>
    ${body}</div></div>`;
}
A.overlayClick = () => { S.modal = null; render(); };

// ================================================================== BOOT ===
let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  const v = VIEWS[S.route.view];
  const ms = (v && v.poll) || 10000;
  pollTimer = setTimeout(async () => {
    if (!document.hidden && !S.needLogin && !(S.modal && S.modal.type !== 'releases')) {
      try {
        if (v && v.pollFn) await v.pollFn(); else if (v && v.load && S.v.loaded) await v.load();
        await refreshStatus();
        render();
      } catch (_) { /* transient */ }
    }
    schedulePoll();
  }, ms);
}
window.addEventListener('hashchange', () => { onRoute(); schedulePoll(); });
(async function boot() {
  try {
    const auth = await api('/api/auth/status');
    S.authEnabled = auth.enabled;
    if (auth.enabled && !auth.logged_in) { S.needLogin = true; render(); return; }
  } catch (_) { /* server down: views show errors */ }
  await refreshStatus();
  try { S.settingsCache = (await api('/api/settings')).settings; } catch (_) { /* ignore */ }
  await onRoute();
  schedulePoll();
}());
