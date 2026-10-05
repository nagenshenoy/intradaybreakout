(() => {
  'use strict';

  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const TAB_DEFS = window.TAB_DEFS;
  const ACTIONS = window.ACTION_TYPES;

  const S = {
    tab: '5m',
    fetchedVersion: {},        // tab -> last version rendered
    filtersDirty: true,
    rows: [],
    seenIds: new Set(),
    sortKey: null,             // null = server order (newest first)
    sortDir: 'desc',
    limit: 1000,
    scanning: false,
    collapsed: { '5m': new Set(), '60m': new Set() },   // per-tab collapsed Scan# groups
    scanList: {},              // tab -> [{scan_no, time, count}], for the Export panel
    totalFiltered: 0,          // rows matching the current Filters panel query, this tab
  };

  // ------------------------------------------------------------ helpers
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const f2 = (v) => (v === null || v === undefined || Number.isNaN(v)) ? '' : Number(v).toFixed(2);
  const relVolClass = (v) => (!v || v <= 0) ? '' : (v >= 1 ? 'positive' : 'negative');

  let toastTimer;
  function toast(msg, kind = '') {
    const t = $('#toast');
    t.textContent = msg;
    t.className = `show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = ''; }, 4000);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (_) { /* csv or empty */ }
    if (!res.ok || data.ok === false) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }
  const postJSON = (url, body = {}) => api(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });

  // -------------------------------------------------------------- theme
  function applyTheme(light) {
    document.body.classList.toggle('light', light);
    $('#theme-icon').textContent = light ? '☀️' : '🌙';
    $('#theme-label').textContent = light ? 'Light' : 'Dark';
  }
  try { applyTheme(localStorage.getItem('theme') === 'light'); } catch (_) { applyTheme(false); }
  $('#theme-btn').addEventListener('click', () => {
    const light = !document.body.classList.contains('light');
    applyTheme(light);
    try { localStorage.setItem('theme', light ? 'light' : 'dark'); } catch (_) { /* ignore */ }
  });

  // ------------------------------------------------------------ filters
  function selectedActions() { return $$('.act-check').filter(c => c.checked).map(c => c.value); }

  function updateActionSummary() {
    const n = selectedActions().length;
    $('#action-summary').textContent = n === ACTIONS.length ? 'All selected' : `${n} of ${ACTIONS.length} selected`;
  }

  function filterQuery() {
    const p = new URLSearchParams();
    p.set('tab', S.tab);
    p.set('symbol', $('#f-symbol').value);
    p.set('band', $('#f-band').value);
    p.set('change_op', $('#f-change-op').value);
    p.set('change_val', $('#f-change-val').value);
    p.set('rsi_op', $('#f-rsi-op').value);
    p.set('rsi_val', $('#f-rsi-val').value);
    p.set('relvol_op', $('#f-relvol-op').value);
    p.set('relvol_val', $('#f-relvol-val').value);
    p.set('week1', $('#f-week1').value);
    p.set('week2', $('#f-week2').value);
    p.set('scan_from', $('#f-from').value);
    p.set('scan_to', $('#f-to').value);
    p.set('action_types', selectedActions().join(','));
    return p.toString();
  }

  let appliedQuery = null;   // snapshot of filters at last "Apply", used by polling + export

  function applyFilters() {
    appliedQuery = filterQuery();
    S.filtersDirty = true;
    return loadResults();
  }

  function resetFilters() {
    $('#f-symbol').value = '';
    $('#f-band').value = 'All';
    $('#f-change-op').value = 'none';
    $('#f-change-val').value = '0';
    $('#f-rsi-op').value = 'none';
    $('#f-rsi-val').value = '50';
    $('#f-relvol-op').value = 'none';
    $('#f-relvol-val').value = '1';
    $('#f-week1').value = 'All';
    $('#f-week2').value = 'All';
    $('#f-from').value = '';
    $('#f-to').value = '';
    $$('.act-check').forEach(c => { c.checked = true; });
    updateActionSummary();
    applyFilters();
  }

  // ------------------------------------------------------------- table
  const COLS = [
    { key: 'Scan#', label: 'Scan#', num: true },
    { key: 'Time', label: 'Time' },
    { key: 'Symbol', label: 'Symbol' },
    { key: 'LTP', label: 'LTP', num: true },
    { key: 'Change%', label: 'Change%', num: true },
    { key: 'RSI(14)', label: 'RSI(14)', num: true },
    { key: 'Band Status', label: 'Band' },
    { key: 'ActionType', label: 'Action' },
    { key: 'RelVol', label: 'Rel Vol', num: true, tip: "Today's average volume so far vs. the previous full trading day's average" },
    { key: 'Week1Pct', label: 'Wk H/L', num: true, tip: '% beyond previous week high (green) or low (red)' },
    { key: 'Week2Pct', label: '-2Wk H/L', num: true, tip: '% beyond the high/low from two weeks ago' },
  ];

  function sortValue(row, key) {
    if (key === 'ActionType') {
      const a = row.ActionType || '';
      return a === 'New Signal' ? 0 : parseInt(a.replace(/\D/g, ''), 10) || 0;
    }
    const v = row[key];
    return v === null || v === undefined ? -Infinity : v;
  }

  function renderHead() {
    $('#thead-row').innerHTML = COLS.map(c => {
      const sorted = S.sortKey === c.key;
      const arrow = sorted ? (S.sortDir === 'asc' ? '↑' : '↓') : '↕';
      return `<th data-key="${esc(c.key)}" class="${c.num ? 'num ' : ''}${sorted ? 'sorted' : ''}"${c.tip ? ` title="${esc(c.tip)}"` : ''}>${esc(c.label)}<span class="sort-arrow">${arrow}</span></th>`;
    }).join('');
  }

  function weekCell(pct) {
    if (pct === null || pct === undefined) return '<td class="num dim">–</td>';
    const cls = pct >= 0 ? 'positive' : 'negative';
    return `<td class="num ${cls}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</td>`;
  }

  function actionPill(a) {
    if (a === 'New Signal') return '<span class="tag-pill fresh">New</span>';
    return `<span class="tag-pill cont">${esc(a)}</span>`;
  }

  function rowHtml(r) {
    const above = r['Band Status'] === 'Above Band';
    const tv = TAB_DEFS[S.tab].tv_interval;
    const sym = String(r.Symbol);
    const url = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(sym.replace('.NS', ''))}&interval=${tv}`;
    const chg = r['Change%'];
    const isNew = !S.seenIds.has(r.id) && S.seenIds.size > 0;
    return `<tr data-id="${r.id}" class="${above ? 'row-positive' : 'row-negative'}${isNew ? ' row-new' : ''}">
      <td class="num muted">${r['Scan#']}</td>
      <td class="muted">${esc(r.Time)}</td>
      <td><a class="sym-link" href="${esc(url)}" target="_blank" rel="noopener">${esc(sym)}</a></td>
      <td class="num">${f2(r.LTP)}</td>
      <td class="num ${chg >= 0 ? 'positive' : 'negative'}">${chg >= 0 ? '+' : ''}${f2(chg)}%</td>
      <td class="num">${f2(r['RSI(14)'])}</td>
      <td><span class="badge ${above ? 'above' : 'below'}">${esc(r['Band Status'])}</span></td>
      <td>${actionPill(r.ActionType)}</td>
      <td class="num ${relVolClass(r.RelVol)}">${r.RelVol > 0 ? f2(r.RelVol) + '×' : '<span class="dim">–</span>'}</td>
      ${weekCell(r.Week1Pct)}
      ${weekCell(r.Week2Pct)}
    </tr>`;
  }

  // Group by Scan# (newest scan first, regardless of any column sort), collapsible per group.
  function groupByScan(rows) {
    const map = new Map();
    rows.forEach(r => {
      const k = r['Scan#'];
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(r);
    });
    return [...map.entries()].sort((a, b) => b[0] - a[0]);
  }

  function groupHeaderHtml(scanNo, groupRows) {
    const above = groupRows.filter(r => r['Band Status'] === 'Above Band').length;
    const below = groupRows.length - above;
    const collapsed = S.collapsed[S.tab].has(scanNo);
    const time = groupRows[0] ? esc(groupRows[0].Time) : '';
    return `<tr class="scan-group-row${collapsed ? ' collapsed' : ''}" data-scan="${scanNo}">
      <td colspan="${COLS.length}">
        <span class="chev">▾</span> Scan #${scanNo}<span class="grp-time">${time} · ${groupRows.length} signal${groupRows.length === 1 ? '' : 's'}</span>
        <span class="grp-counts"><span class="positive">${above} above</span> · <span class="negative">${below} below</span></span>
      </td>
    </tr>`;
  }

  function renderRows() {
    let rows = S.rows.slice();
    if (S.sortKey) {
      const dir = S.sortDir === 'asc' ? 1 : -1;
      rows.sort((a, b) => {
        const va = sortValue(a, S.sortKey), vb = sortValue(b, S.sortKey);
        if (va < vb) return -1 * dir;
        if (va > vb) return 1 * dir;
        return b.id - a.id;
      });
    }
    const groups = groupByScan(rows);
    const collapsedSet = S.collapsed[S.tab];
    $('#tbody').innerHTML = groups.map(([scanNo, groupRows]) => {
      const collapsed = collapsedSet.has(scanNo);
      return groupHeaderHtml(scanNo, groupRows) + (collapsed ? '' : groupRows.map(rowHtml).join(''));
    }).join('');
    rows.forEach(r => S.seenIds.add(r.id));
    $('#empty').style.display = rows.length ? 'none' : 'block';
    $('#results-table').style.display = rows.length ? '' : 'none';
    renderHead();
  }

  // ----------------------------------------------------------- results
  async function loadResults() {
    if (appliedQuery === null) appliedQuery = filterQuery();
    // the tab is part of the query; rebuild with the current tab but the applied filters
    const p = new URLSearchParams(appliedQuery);
    p.set('tab', S.tab);
    try {
      const d = await api(`/api/results?${p}`);
      S.rows = d.rows;
      S.limit = d.limit;
      $('#kpi-above').textContent = d.above;
      $('#kpi-below').textContent = d.below;
      $('#table-title').textContent = `${TAB_DEFS[S.tab].name} results`;
      const shown = d.rows.length;
      $('#table-count').textContent = `${d.total_filtered} of ${d.total_all} signals`;
      S.totalFiltered = d.total_filtered;
      renderRows();
      document.getElementById('trunc')?.remove();
      if (d.total_filtered > shown) {
        const n = document.createElement('div');
        n.id = 'trunc'; n.className = 'trunc-note';
        n.textContent = `Showing the newest ${shown} of ${d.total_filtered} matching rows. Narrow the filters or export the tab for the full set.`;
        $('.data-table-scroll').appendChild(n);
      }
      S.filtersDirty = false;
      S.fetchedVersion[S.tab] = S.lastVersions ? S.lastVersions[S.tab] : undefined;
      return true;
    } catch (e) {
      toast(e.message, 'error');
      S.filtersDirty = false;   // don't hammer the server with a bad filter every poll
      return false;
    }
  }

  // -------------------------------------------------------------- state
  function renderProgress(s) {
    const row = $('#progress-row'), p = s.progress || {};
    const track = $('#progress-track'), fill = $('#progress-fill');
    const show = s.scanning && (p.phase === 'scanning' || p.phase === 'waiting' || /^(Starting|Stopping)/.test(s.status));
    row.hidden = !show;
    if (!show) return;
    row.classList.toggle('waiting', p.phase === 'waiting');
    let pct = 0;
    if (p.phase === 'scanning') {
      pct = p.total ? (p.started / p.total) * 100 : 0;
      const sym = p.symbol ? esc(p.symbol) : '…';
      $('#progress-main').innerHTML = `Scanning <span class="tick">${sym}</span>… ${p.started}/${p.total}`;
      $('#progress-meta').textContent = `${p.tab_name} · scan #${p.scan_no} · ${p.found} signal${p.found === 1 ? '' : 's'} so far`;
    } else if (p.phase === 'waiting') {
      pct = p.wait_total ? ((p.wait_total - p.wait_left) / p.wait_total) * 100 : 0;
      $('#progress-main').textContent = `Next scan in ${p.wait_left}s`;
      $('#progress-meta').textContent = 'Waiting for the next cycle';
    } else {
      $('#progress-main').textContent = /^Stopping/.test(s.status) ? 'Stopping after the current symbol…' : 'Starting…';
      $('#progress-meta').textContent = '';
    }
    fill.style.width = `${Math.max(0, Math.min(100, pct)).toFixed(1)}%`;
    track.setAttribute('aria-valuenow', Math.round(pct));
    track.setAttribute('aria-label', $('#progress-main').textContent);
  }

  function syncConfigOnce(cfg) {
    if (S.cfgSynced) return;
    S.cfgSynced = true;               // reflect a scan that is already running (e.g. after a page reload)
    $('#cfg-5m').checked = cfg.scan_5m;
    $('#cfg-60m').checked = cfg.scan_60m;
    $('#cfg-lookback').value = cfg.lookback_days;
    $('#cfg-interval').value = cfg.scan_interval;
    $('#cfg-workers').value = cfg.workers;
  }

  function renderState(s) {
    S.lastState = s;
    syncConfigOnce(s.config);
    if (S.scanning && !s.scanning && s.auto_stopped) {
      toast('Scan auto-stopped: market close (3:25 PM IST) reached', 'ok');
    }
    S.scanning = s.scanning;
    S.lastVersions = Object.fromEntries(Object.entries(s.tabs).map(([k, t]) => [k, t.version]));

    const pill = $('#live-pill');
    pill.classList.toggle('running', s.scanning);
    pill.classList.remove('offline');
    const stopping = s.scanning && /^Stopping/.test(s.status);
    const stoppedLabel = s.auto_stopped ? 'Stopped (3:25 PM cutoff)' : 'Stopped';
    $('#status-text').textContent = stopping ? 'Stopping…' : s.scanning ? 'Running' : (s.status === 'Ready' ? 'Idle' : stoppedLabel);
    $('#start-btn').disabled = s.scanning;
    $('#stop-btn').disabled = !s.scanning || stopping;
    renderProgress(s);

    $('#kpi-symbols').textContent = s.symbols;
    $('#kpi-file').textContent = s.file_name || 'No list loaded';
    $('#kpi-file').title = s.file_name || '';
    const note = $('#import-label');
    note.textContent = s.symbols ? `Loaded ${s.symbols} symbols from ${s.file_name}` : 'No list loaded';
    note.title = s.file_name || '';
    note.classList.toggle('loaded', !!s.symbols);
    updateSummaries();

    const t = s.tabs[S.tab];
    $('#kpi-scans').textContent = t.scan_count;
    $('#kpi-tabname').textContent = `${t.name} tab`;
    $('#kpi-total').textContent = t.total_results;
    for (const [k, tb] of Object.entries(s.tabs)) $(`#cnt-${k}`).textContent = tb.total_results;

    $('#kpi-errors').textContent = s.error_count;
    const last = s.errors[s.errors.length - 1];
    $('#kpi-lasterr').textContent = last ? `${last.symbol}: ${last.error}`.slice(0, 40) : 'none';
    $('#kpi-lasterr').title = last ? `${last.time} ${last.symbol} (${last.tab}): ${last.error}` : '';
  }

  async function poll() {
    try {
      const s = await api('/api/state');
      renderState(s);
      const v = s.tabs[S.tab].version;
      if (S.filtersDirty || S.fetchedVersion[S.tab] !== v) { await loadResults(); loadScanList(); }
    } catch (_) {
      const pill = $('#live-pill');
      pill.classList.remove('running'); pill.classList.add('offline');
      $('#status-text').textContent = 'Server unreachable';
    } finally {
      setTimeout(poll, S.scanning ? 700 : 2000);   // poll faster while a scan is running
    }
  }

  // ----------------------------------------------------------- actions
  function readScanSetup() {
    return {
      scan_5m: $('#cfg-5m').checked,
      scan_60m: $('#cfg-60m').checked,
      lookback_days: $('#cfg-lookback').value,
      scan_interval: $('#cfg-interval').value,
      workers: $('#cfg-workers').value,
    };
  }

  async function startScan() {
    const cfg = readScanSetup();
    try {
      await postJSON('/api/scan/start', cfg);
      toast('Scan started', 'ok');
      // Show a tab that is actually being scanned.
      if (!cfg.scan_5m && cfg.scan_60m && S.tab !== '60m') switchTab('60m');
    } catch (e) { toast(e.message, 'error'); }
  }

  // Push a Scan Setup change to the server while a scan is running, so it takes
  // effect from the next scan pass without needing Stop then Start.
  async function pushScanConfigIfRunning() {
    if (!S.scanning) return;
    try {
      const d = await postJSON('/api/scan/config', readScanSetup());
      toast(d.message, 'ok');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function stopScan() {
    try { await postJSON('/api/scan/stop'); } catch (e) { toast(e.message, 'error'); }
  }

  async function clearTab() {
    try {
      await postJSON('/api/clear', { tab: S.tab });
      S.seenIds.clear();
      S.filtersDirty = true;
      toast(`${TAB_DEFS[S.tab].name} results cleared`, 'ok');
    } catch (e) { toast(e.message, 'error'); }
  }

  // ------------------------------------------------------------- export
  // Independent of the Filters panel: exports either the whole tab or one
  // Scan#, with the symbol written in the chosen format.
  function renderScanSelect(scans) {
    const sel = $('#exp-scanno');
    const keep = sel.value;
    if (!scans.length) {
      sel.innerHTML = '<option value="">No scans yet</option>';
      sel.disabled = true;
    } else {
      sel.disabled = false;
      sel.innerHTML = scans.map(s =>
        `<option value="${s.scan_no}">Scan #${s.scan_no} · ${esc(s.time)} · ${s.count} signal${s.count === 1 ? '' : 's'}</option>`
      ).join('');
      if (scans.some(s => String(s.scan_no) === keep)) sel.value = keep;
    }
    updateSummaries();
  }

  async function loadScanList() {
    try {
      const d = await api(`/api/scans?tab=${S.tab}`);
      S.scanList = S.scanList || {};
      S.scanList[S.tab] = d.scans;
      renderScanSelect(d.scans);
    } catch (_) { /* non-critical; leave the dropdown as-is */ }
  }

  function exportTab() {
    const scope = $('#exp-scope').value;
    const fmt = $('#exp-format').value;
    let p;
    if (scope === 'filtered') {
      if (!S.totalFiltered) { toast('No filtered results in the active tab to export.', 'error'); return; }
      p = new URLSearchParams(appliedQuery || filterQuery());   // the Filters panel's current query
      p.set('tab', S.tab);
      p.set('symbol_format', fmt);
    } else {
      p = new URLSearchParams({ tab: S.tab, symbol_format: fmt });
      if (scope === 'scan') {
        const scanNo = $('#exp-scanno').value;
        if (!scanNo) { toast('No scans available to export yet.', 'error'); return; }
        p.set('scan_from', scanNo);
        p.set('scan_to', scanNo);
      } else {
        const list = (S.scanList && S.scanList[S.tab]) || [];
        if (!list.reduce((a, e) => a + e.count, 0)) {
          toast('No signals in this tab to export yet.', 'error');
          return;
        }
      }
    }
    window.location.href = `/api/export?${p}`;
  }

  // ---- stock list sources -------------------------------------------
  const sheetLabel = (sh) => `${sh.name} (${sh.count} symbols)`;

  function fillSelect(sel, sheets, keep) {
    sel.innerHTML = sheets.map(sh => `<option value="${esc(sh.name)}">${esc(sheetLabel(sh))}</option>`).join('');
    if (keep && sheets.some(sh => sh.name === keep)) sel.value = keep;
  }

  function renderSources(d) {
    const b = d.bundled, qs = $('#qs-select');
    if (b.available) {
      fillSelect(qs, b.sheets, qs.value);
      qs.disabled = false; $('#qs-load').disabled = false;
    } else {
      qs.innerHTML = '<option>ScannerData.xlsx not found</option>';
      qs.disabled = true; $('#qs-load').disabled = true;
    }
    const u = d.upload, chooser = $('#sheet-chooser');
    if (u && u.sheets.length > 1) {
      S.uploadToken = u.token;
      $('#sheet-label').textContent = `Select sheet · ${u.file_name}`;
      fillSelect($('#sheet-select'), u.sheets, $('#sheet-select').value);
      chooser.hidden = false;
    } else {
      S.uploadToken = null; chooser.hidden = true;
    }
  }

  async function loadSources() {
    try { renderSources(await api('/api/sources')); } catch (e) { toast(e.message, 'error'); }
  }

  async function loadSheet(source, sheet) {
    try {
      const d = await postJSON('/api/load', { source, sheet });
      toast(`Loaded ${d.count} symbols from ${d.file_name}.${d.note || ''}`, 'ok');
      api('/api/state').then(renderState).catch(() => {});   // refresh counts right away
    } catch (e) { toast(e.message, 'error'); }
  }

  async function importFile(file) {
    const fd = new FormData();
    fd.append('file', file);
    try {
      const d = await api('/api/import', { method: 'POST', body: fd });
      if (d.needs_sheet) {
        await loadSources();                       // reveals the Select Sheet dropdown
        toast(`${d.file_name} has ${d.sheets.length} sheets. Pick one and click Load sheet.`, 'ok');
        $('#sheet-select').focus();
      } else {
        toast(`Loaded ${d.count} symbols from ${d.file_name}.${d.note || ''}`, 'ok');
        $('#sheet-chooser').hidden = true;
        api('/api/state').then(renderState).catch(() => {});
      }
    } catch (e) { toast(e.message, 'error'); }
    $('#file-input').value = '';
  }

  function switchTab(key) {
    S.tab = key;
    S.seenIds.clear();
    S.sortKey = null;
    $$('.pill-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === key));
    S.filtersDirty = true;
    if (S.lastState) renderState(S.lastState);   // refresh KPIs for the new tab right away
    loadResults();
    loadScanList();
  }

  // ------------------------------------------------------------- modal
  function openModal(row) {
    const above = row['Band Status'] === 'Above Band';
    const tv = TAB_DEFS[S.tab].tv_interval;
    $('#m-symbol').textContent = row.Symbol;
    $('#m-sub').textContent = `${TAB_DEFS[S.tab].name} · scan #${row['Scan#']} at ${row.Time} · ${row.ActionType}`;
    $('#m-price').textContent = f2(row.LTP);
    const chg = row['Change%'];
    $('#m-change').innerHTML = `<span class="badge ${above ? 'above' : 'below'}">${esc(row['Band Status'])}</span> <span class="${chg >= 0 ? 'positive' : 'negative'}">${chg >= 0 ? '+' : ''}${f2(chg)}%</span>`;
    const m = (k, v, cls = '') => `<div class="metric"><div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div></div>`;
    const lvl = (v) => (v > 0 ? f2(v) : '–');
    const pct = (p) => p === null || p === undefined ? '–' : `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`;
    const pcls = (p) => p === null || p === undefined ? '' : (p >= 0 ? 'positive' : 'negative');
    $('#m-grid').innerHTML = [
      m('RSI (14)', f2(row['RSI(14)'])),
      m('Rel Vol', row.RelVol > 0 ? `${f2(row.RelVol)}×` : '–', relVolClass(row.RelVol)),
      m('Scan #', row['Scan#']),
      m('Prev week high', lvl(row.PrevWeekHigh)),
      m('Prev week low', lvl(row.PrevWeekLow)),
      m('vs prev week', pct(row.Week1Pct), pcls(row.Week1Pct)),
      m('Week status', esc(row.WeekHL_Color)),
      m('2-wk ago high', lvl(row.Prev2WeekHigh)),
      m('2-wk ago low', lvl(row.Prev2WeekLow)),
      m('vs 2-wk level', pct(row.Week2Pct), pcls(row.Week2Pct)),
      m('2-wk status', esc(row.Week2HL_Color)),
    ].join('');
    $('#m-link').href = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(String(row.Symbol).replace('.NS', ''))}&interval=${tv}`;
    $('#modal-overlay').classList.add('open');
    $('#m-close').focus();
  }
  const closeModal = () => $('#modal-overlay').classList.remove('open');

  // ------------------------------------------------------------ wiring
  $('#qs-load').addEventListener('click', () => loadSheet('bundled', $('#qs-select').value));
  $('#sheet-load').addEventListener('click', () => S.uploadToken && loadSheet(S.uploadToken, $('#sheet-select').value));
  $('#import-btn').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', e => { if (e.target.files[0]) importFile(e.target.files[0]); });
  $('#start-btn').addEventListener('click', startScan);
  $('#stop-btn').addEventListener('click', stopScan);
  $('#clear-btn').addEventListener('click', clearTab);
  $('#export-btn').addEventListener('click', exportTab);
  $('#apply-btn').addEventListener('click', applyFilters);
  $('#reset-btn').addEventListener('click', resetFilters);
  $$('.filters input[type="text"], .filters input[type="number"]').forEach(el =>
    el.addEventListener('keydown', e => { if (e.key === 'Enter') applyFilters(); }));
  $$('.filters select').forEach(el => el.addEventListener('change', applyFilters));

  $$('.act-check').forEach(c => c.addEventListener('change', updateActionSummary));
  $('#act-all').addEventListener('click', () => { $$('.act-check').forEach(c => { c.checked = true; }); updateActionSummary(); });
  $('#act-none').addEventListener('click', () => { $$('.act-check').forEach(c => { c.checked = false; }); updateActionSummary(); });
  document.addEventListener('click', e => {           // close dropdown on outside click, then apply
    const dd = $('#action-dd');
    if (dd.open && !dd.contains(e.target)) { dd.open = false; applyFilters(); }
  });

  $$('.pill-tab').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));

  $('#exp-scope').addEventListener('change', () => {
    $('#exp-scanno-field').hidden = $('#exp-scope').value !== 'scan';
    updateSummaries();
  });
  $$('#export-body select').forEach(el => el.addEventListener('change', updateSummaries));

  $('#thead-row').addEventListener('click', e => {
    const th = e.target.closest('th');
    if (!th) return;
    const key = th.dataset.key;
    if (S.sortKey === key) S.sortDir = S.sortDir === 'asc' ? 'desc' : 'asc';
    else { S.sortKey = key; S.sortDir = 'desc'; }
    renderRows();
  });

  $('#tbody').addEventListener('click', e => {
    const groupHeader = e.target.closest('.scan-group-row');
    if (groupHeader) {
      const scanNo = Number(groupHeader.dataset.scan);
      const set = S.collapsed[S.tab];
      if (set.has(scanNo)) set.delete(scanNo); else set.add(scanNo);
      renderRows();
      return;
    }
    if (e.target.closest('a')) return;                 // symbol link opens TradingView
    const tr = e.target.closest('tr');
    if (!tr) return;
    const row = S.rows.find(r => String(r.id) === tr.dataset.id);
    if (row) openModal(row);
  });

  $('#expand-all-btn').addEventListener('click', () => { S.collapsed[S.tab].clear(); renderRows(); });
  $('#collapse-all-btn').addEventListener('click', () => {
    groupByScan(S.rows).forEach(([scanNo]) => S.collapsed[S.tab].add(scanNo));
    renderRows();
  });

  $('#m-close').addEventListener('click', closeModal);
  $('#modal-overlay').addEventListener('click', e => { if (e.target.id === 'modal-overlay') closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  // ------------------------------------------------ collapsible panels
  function setCollapsed(panel, collapsed, persist = true) {
    panel.classList.toggle('is-collapsed', collapsed);
    $('.panel-toggle', panel).setAttribute('aria-expanded', String(!collapsed));
    if (persist) { try { localStorage.setItem(`panel:${panel.dataset.key}`, collapsed ? '1' : '0'); } catch (_) { /* ignore */ } }
    updateSummaries();
  }
  $$('.panel.collapsible').forEach(panel => {
    let saved = null;
    try { saved = localStorage.getItem(`panel:${panel.dataset.key}`); } catch (_) { /* ignore */ }
    setCollapsed(panel, saved === '1', false);
    $('.panel-toggle', panel).addEventListener('click', () => setCollapsed(panel, !panel.classList.contains('is-collapsed')));
  });

  // What you see while a panel is collapsed, so hidden settings are never a mystery.
  function updateSummaries() {
    const types = [$('#cfg-5m').checked && '5m', $('#cfg-60m').checked && '60m'].filter(Boolean).join(' + ') || 'no timeframe';
    const n = S.lastState ? S.lastState.symbols : 0;
    $('#setup-summary').textContent =
      `${types} · lookback ${$('#cfg-lookback').value}d · every ${$('#cfg-interval').value}s · ${n} symbols`;

    const active = [
      $('#f-symbol').value.trim() !== '', $('#f-band').value !== 'All', $('#f-change-op').value !== 'none',
      $('#f-rsi-op').value !== 'none', $('#f-relvol-op').value !== 'none', $('#f-week1').value !== 'All',
      $('#f-week2').value !== 'All', $('#f-from').value !== '' || $('#f-to').value !== '',
      selectedActions().length !== ACTIONS.length && selectedActions().length !== 0,
    ].filter(Boolean).length;
    $('#filters-summary').textContent = active ? `${active} filter${active === 1 ? '' : 's'} active` : 'no filters active';

    const scope = $('#exp-scope').value;
    const scanNo = $('#exp-scanno').value;
    const scopeLabel = scope === 'filtered' ? 'filtered results'
      : scope === 'all' ? 'all data'
      : (scanNo ? `Scan #${scanNo}` : 'no scan selected');
    const fmtLabel = { ns: '.NS suffix', plain: 'plain symbol', nse: 'NSE: prefix' }[$('#exp-format').value];
    $('#export-summary').textContent = `${scopeLabel} · ${fmtLabel}`;
  }
  $$('#setup-body input, #setup-body select, .filters input, .filters select').forEach(el => {
    el.addEventListener('input', updateSummaries);
    el.addEventListener('change', updateSummaries);
  });
  $$('.act-check, #act-all, #act-none').forEach(el => el.addEventListener('click', updateSummaries));
  // Scan Setup fields (not the stock-list controls) push live to the server when
  // a scan is already running, so changes apply from the next scan pass.
  $$('#cfg-5m, #cfg-60m, #cfg-lookback, #cfg-interval, #cfg-workers').forEach(el =>
    el.addEventListener('change', pushScanConfigIfRunning));

  // ------------------------------------------------------------- boot
  renderHead();
  updateActionSummary();
  updateSummaries();
  loadSources();
  loadScanList();
  poll();
})();
