#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""page.py — the default web page served by the web UI (one self-contained HTML file).

Inert resource, zero logic: inline CSS + inline JS, no external requests, no
telemetry (constitution principle 3). Kept in the library (not the web_ui facade)
so the facade stays about server behaviour and any future delivery layer can serve
the same page.

BYTE-LEVEL COMPATIBILITY: this string is the assertion object of
tests/t018_page_js_test.py (016's incident was two JS string literals whose
backslashes were not doubled for the Python literal). Do not reformat, do not
"fix" quoting, do not run an autoformatter over this file.
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MFT Search</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
         margin: 0; background: #f5f6f8; color: #1a1a1a; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  #status { font-size: 12px; color: #666; margin-bottom: 2px; }
  /* Live disk I/O of this process, small print under the status line (017) */
  #io { font-size: 11px; color: #8a8f99; margin: 0 0 16px; }
  #io[hidden] { display: none; }
  form { display: flex; gap: 8px; }
  #q { flex: 1; padding: 10px 12px; font-size: 15px;
       border: 1px solid #c8ccd4; border-radius: 8px; }
  #q:focus { outline: 2px solid #2563eb66; border-color: #2563eb; }
  /* Volume selector: shows which volume the index actually covers (013) */
  #vol { padding: 9px 8px; font-size: 14px; border: 1px solid #c8ccd4; border-radius: 8px;
         background: #fff; color: #1a1a1a; max-width: 180px; }
  #vol:disabled { color: #777; background: #eef1f5; cursor: not-allowed; }
  #vol[hidden] { display: none; }
  button { padding: 10px 20px; font-size: 15px; border: 0; border-radius: 8px;
           background: #2563eb; color: #fff; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  #meta { font-size: 13px; color: #444; margin: 14px 2px 8px; min-height: 18px; }
  #meta.hint { color: #b91c1c; }
  #results { list-style: none; margin: 0; padding: 0; }
  #results li { font-family: Consolas, "Cascadia Mono", monospace; font-size: 13px;
               padding: 6px 10px; background: #fff; border: 1px solid #e4e7ec;
               border-radius: 6px; margin-bottom: 6px; word-break: break-all; }
  /* Keyword highlight. <mark> is created through the DOM API; path data stays plain text. */
  #results li mark { background: #fde68a; color: #1a1a1a; border-radius: 2px;
                    padding: 0 1px; }
  /* Settings dropdown (search scope switch) */
  .settings { position: relative; }
  #settings-btn { background: #fff; color: #1a1a1a; border: 1px solid #c8ccd4;
                  white-space: nowrap; }
  #settings-btn:hover { background: #eef1f5; }
  #settings-menu { position: absolute; right: 0; top: calc(100% + 6px); z-index: 10;
                   background: #fff; border: 1px solid #e4e7ec; border-radius: 8px;
                   box-shadow: 0 4px 16px rgba(0,0,0,.12); padding: 10px 12px;
                   min-width: 150px; }
  #settings-menu[hidden] { display: none; }
  .settings-title { font-size: 12px; color: #666; margin: 0 0 6px; }
  #settings-menu label { display: flex; align-items: center; gap: 6px;
                         font-size: 13px; padding: 4px 0; cursor: pointer; }
  /* Index root block (whole drive / specific folder, 014) */
  #folder-row { margin: 4px 0 2px; }
  #folder-row[hidden] { display: none; }
  #folder-path { width: 100%; padding: 6px 8px; font-size: 12px;
                 border: 1px solid #c8ccd4; border-radius: 6px; }
  .settings-actions { display: flex; gap: 6px; margin-top: 6px; }
  #folder-apply { padding: 5px 10px; font-size: 12px; border-radius: 6px;
                  background: #2563eb; color: #fff; }
  #folder-apply:disabled { background: #9db2e0; cursor: progress; }
  .settings-hint { font-size: 11px; color: #777; margin-top: 6px; line-height: 1.4; }
</style>
</head>
<body>
<div class="wrap">
  <h1>MFT Search</h1>
  <div id="status">Loading index status...</div>
  <div id="io" hidden></div>
  <form id="form">
    <input id="q" type="text" placeholder="Search file names (space = AND, ! = exclude)"
           autocomplete="off" autofocus>
    <select id="vol" title="Indexed volume" hidden></select>
    <button type="submit">Search</button>
    <div class="settings">
      <button type="button" id="settings-btn">Settings</button>
      <div id="settings-menu" hidden>
        <div class="settings-title">Index root</div>
        <label><input type="radio" name="root" value="volume"> Whole drive</label>
        <label><input type="radio" name="root" value="folder"> Specific folder</label>
        <div id="folder-row" hidden>
          <input id="folder-path" type="text" placeholder="e.g. D:\\Projects"
                 spellcheck="false" autocomplete="off">
          <div class="settings-actions">
            <button type="button" id="folder-apply">Apply</button>
          </div>
        </div>
        <div class="settings-hint" id="root-hint"></div>
        <div class="settings-title" style="margin-top:10px">Search scope</div>
        <label><input type="radio" name="scope" value="filename"> File name only</label>
        <label><input type="radio" name="scope" value="path"> Full path</label>
      </div>
    </div>
  </form>
  <div id="meta"></div>
  <ul id="results"></ul>
</div>
<script>
var form = document.getElementById('form');
var q = document.getElementById('q');
var meta = document.getElementById('meta');
var results = document.getElementById('results');
var settingsBtn = document.getElementById('settings-btn');
var settingsMenu = document.getElementById('settings-menu');
var scopeRadios = settingsMenu.querySelectorAll('input[name=scope]');

/* ---------- Search scope (settings dropdown + localStorage persistence) ---------- */
var SCOPE_KEY = 'mft-search.scope';
var SCOPE_LABEL = { filename: 'file name', path: 'full path' };
var scope = 'filename';
try { scope = localStorage.getItem(SCOPE_KEY) || 'filename'; } catch (e) {}
if (scope !== 'path') { scope = 'filename'; }   // fallback: anything but 'path' is the default

function syncScopeUI() {
  for (var i = 0; i < scopeRadios.length; i++) {
    scopeRadios[i].checked = (scopeRadios[i].value === scope);
  }
  q.placeholder = (scope === 'path' ? 'Search full paths' : 'Search file names') +
                  ' (space = AND, ! = exclude)';
}

settingsBtn.addEventListener('click', function (e) {
  e.stopPropagation();
  settingsMenu.hidden = !settingsMenu.hidden;
});
settingsMenu.addEventListener('click', function (e) { e.stopPropagation(); });
document.addEventListener('click', function () { settingsMenu.hidden = true; });
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { settingsMenu.hidden = true; }
});
for (var i = 0; i < scopeRadios.length; i++) {
  scopeRadios[i].addEventListener('change', function () {
    scope = this.value;
    try { localStorage.setItem(SCOPE_KEY, scope); } catch (e) {}
    syncScopeUI();
    settingsMenu.hidden = true;
    if (q.value.trim()) { doSearch(); }   // switching scope re-runs the search immediately
  });
}

/* ---------- Index scope: whole drive / specific folder (014) ---------- */
var volSel = document.getElementById('vol');
var statusEl = document.getElementById('status');
var ioEl = document.getElementById('io');
var rootRadios = settingsMenu.querySelectorAll('input[name=root]');
var folderRow = document.getElementById('folder-row');
var folderPath = document.getElementById('folder-path');
var folderApply = document.getElementById('folder-apply');
var rootHint = document.getElementById('root-hint');
/* indexScope describes what the *server* is indexing; `scope` (above) is the search scope.
   Two different things, deliberately not merged into one variable. */
var indexScope = { kind: 'volume', target: null };
var canSwitch = true;
var switching = false;      /* a scope request is in flight */
var building = false;       /* the server is building an index */
var pollTimer = null;

function scopeLabel(s) {
  if (!s) { return 'unknown'; }
  return s.kind === 'volume' ? ('whole drive ' + s.target + ':') : ('folder ' + s.target);
}

function schedulePoll() {
  if (!pollTimer) { pollTimer = setInterval(loadStatus, 1000); }
}

function renderIo(io) {
  /* 017: this process's live disk read/write rate, small print under the status line.
     io is null when the counters are unavailable (non-Windows / API failure): hide the
     line instead of showing a stale or bogus number. */
  if (!io || io.available !== true) { ioEl.hidden = true; return; }
  ioEl.hidden = false;
  ioEl.textContent = 'Disk I/O: read ' + io.read_mb_s.toFixed(1) +
                     ' MB/s · write ' + io.write_mb_s.toFixed(1) + ' MB/s';
  ioEl.title = 'This process, cumulative: read ' + (io.read_mb / 1024).toFixed(2) +
               ' GB · write ' + (io.write_mb / 1024).toFixed(2) + ' GB';
}

function renderStatus(s) {
  indexScope = s.scope || indexScope;
  building = !!s.building;
  renderIo(s.io);
  if (building) {
    /* A three-minute walk must look alive: progress + elapsed, every second (FR-015). */
    statusEl.textContent =
      'Indexing ' + scopeLabel(s.scope) + ' ... ' +
      Number(s.progress || 0).toLocaleString() + ' entries (' +
      Math.round((s.building_ms || 0) / 1000) + ' s)';
    schedulePoll();
    updateVolDisabled();
    return;
  }
  var cacheTxt = '';
  if (s.cache && s.cache.mode === 'warm') {
    cacheTxt = ' · cache warm start (' + s.cache.rescanned + ' directories rescanned)';
  } else if (s.cache && s.cache.mode === 'cold') {
    cacheTxt = ' · cache cold start (writing in the background)';
  } else if (s.cache && s.cache.mode === 'nocache') {
    cacheTxt = ' · cache disabled';
  }
  statusEl.textContent =
    'Index: ' + s.entries.toLocaleString() + ' entries · ' + s.mode + ' mode · ' +
    scopeLabel(s.scope) + ' · built in ' + (s.build_ms / 1000).toFixed(2) + ' s' +
    cacheTxt + ' · scope: ' + SCOPE_LABEL[scope] +
    (s.note ? ' · ' + s.note : '') + (s.error ? ' · ' + s.error : '');
  updateVolDisabled();
  syncRootUI();
  schedulePoll();   /* 017: keep polling while idle, so the I/O line always says "now" */
}

function loadStatus() {
  return fetch('/api/status').then(function (r) { return r.json(); })
    .then(renderStatus)
    .catch(function () { statusEl.textContent = 'Could not load index status'; });
}

function buildVolumeOptions(v) {
  volSel.replaceChildren();
  indexScope = v.scope || indexScope;
  var vols = v.volumes || [];
  if (indexScope.kind === 'folder') {
    var cur = document.createElement('option');
    cur.value = '';
    cur.textContent = 'Folder: ' + indexScope.target;
    volSel.appendChild(cur);
  }
  for (var i = 0; i < vols.length; i++) {
    /* MFT needs NTFS; walk can traverse any fixed volume. Offer what this session can do. */
    if (v.mode === 'mft' ? !vols[i].selectable : !vols[i].walkable) { continue; }
    var o = document.createElement('option');
    o.value = vols[i].letter;
    o.textContent = vols[i].letter + ':' + (vols[i].label ? ' (' + vols[i].label + ')' : '');
    volSel.appendChild(o);
  }
  volSel.value = (indexScope.kind === 'volume') ? indexScope.target : '';
  if (!volSel.value && volSel.options.length) { volSel.selectedIndex = 0; }
}

function loadVolumes() {
  return fetch('/api/volumes').then(function (r) { return r.json(); }).then(function (v) {
    canSwitch = !!v.can_switch;
    buildVolumeOptions(v);
    volSel.hidden = false;
    updateVolDisabled();
    volSel.title = v.note || 'Search another drive (rebuilds the index)';
    rootHint.textContent = v.note || '';
    syncRootUI();
  }).catch(function () { volSel.hidden = true; });
}

function updateVolDisabled() {
  /* Deliberately not disabled while building: picking another drive is how you cancel a
     three-minute walk, so it must stay clickable. */
  volSel.disabled = switching || !canSwitch;
}

function syncRootUI() {
  for (var j = 0; j < rootRadios.length; j++) {
    rootRadios[j].checked = (rootRadios[j].value === indexScope.kind);
  }
  folderRow.hidden = indexScope.kind !== 'folder';
  if (indexScope.kind === 'folder') { folderPath.value = indexScope.target; }
}

function applyScope(rootValue) {
  switching = true;
  updateVolDisabled();
  folderApply.disabled = true;
  meta.className = '';
  meta.textContent = 'Indexing ' + rootValue + ' ...';
  return fetch('/api/scope?root=' + encodeURIComponent(rootValue), { method: 'POST' })
    .then(function (r) {
      return r.json().then(function (d) { return { code: r.status, d: d }; });
    })
    .then(function (res) {
      if (res.code !== 200 || res.d.error) {
        /* Every failure path leaves the old index in place, so keep showing it. */
        meta.textContent = 'Scope change failed: ' + (res.d.error || ('HTTP ' + res.code)) +
                           (res.d.hint ? ' — ' + res.d.hint : '');
        meta.className = 'hint';
        loadVolumes();
        return;
      }
      indexScope = res.d.scope;
      results.replaceChildren();
      meta.textContent = 'Now searching ' + scopeLabel(res.d.scope) +
        (res.d.mode === 'pending' ? ' (building in the background)' : '');
      meta.className = '';
      syncRootUI();
      loadStatus();
      loadVolumes();
    })
    .catch(function (err) {
      meta.textContent = 'Scope change failed: ' + err;
      meta.className = 'hint';
    })
    .finally(function () {
      switching = false;
      folderApply.disabled = false;
      updateVolDisabled();
    });
}

volSel.addEventListener('change', function () {
  var target = volSel.value;
  if (!target || (indexScope.kind === 'volume' && target === indexScope.target)) {
    syncRootUI();
    return;
  }
  applyScope(target + ':\\\\');
});

for (var r = 0; r < rootRadios.length; r++) {
  rootRadios[r].addEventListener('change', function () {
    if (this.value === 'folder') {
      folderRow.hidden = false;
      folderPath.focus();
      return;                       /* no folder typed yet: wait for Apply */
    }
    folderRow.hidden = true;
    if (indexScope.kind === 'volume') { return; }
    var letter = (indexScope.target || 'C').charAt(0);
    applyScope(letter + ':\\\\');     /* back to the whole drive of the current folder */
  });
}

folderApply.addEventListener('click', function () {
  var v = folderPath.value.trim();
  if (!v) {
    meta.textContent = 'Enter a folder path first';
    meta.className = 'hint';
    return;
  }
  applyScope(v);
});

folderPath.addEventListener('keydown', function (e) {
  if (e.key === 'Enter') { e.preventDefault(); folderApply.click(); }
});

/* ---------- Keyword highlight (DOM API assembly, never innerHTML) ---------- */
/* Note: PAGE is a plain triple-quoted string, so backslashes in the regex are escaped
   again for the Python literal. The same rule applies to every JS string literal below:
   a JS ":\" must be written as ':\\\\' here — one backslash would escape the closing
   quote and kill the whole inline script with a SyntaxError (016). */
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
}

function highlight(text, terms) {
  var frag = document.createDocumentFragment();
  if (!terms || terms.length === 0) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  var re = new RegExp('(' + terms.map(escapeRegExp).join('|') + ')', 'gi');
  var last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    }
    var mark = document.createElement('mark');
    mark.textContent = m[0];               // textContent: path data never reaches the HTML parser
    frag.appendChild(mark);
    last = m.index + m[0].length;
    if (m[0].length === 0) { re.lastIndex++; }   // guard against zero-length matches looping forever
  }
  if (last < text.length) {
    frag.appendChild(document.createTextNode(text.slice(last)));
  }
  return frag;
}

/* ---------- Search ---------- */
function doSearch() {
  var query = q.value.trim();
  if (!query) {
    meta.textContent = 'Please enter a keyword';
    meta.className = 'hint';
    results.replaceChildren();
    return;
  }
  meta.textContent = 'Searching...';
  meta.className = '';
  fetch('/api/search?q=' + encodeURIComponent(query) + '&scope=' + scope)
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.error) {
        meta.textContent = d.error;
        meta.className = 'hint';
        results.replaceChildren();
        return;
      }
      var extra = d.count >= d.limit ? ' (limit ' + d.limit + ' reached, showing the first '
                                        + d.limit + ')' : '';
      if (d.partial) { extra += ' · index still building, results may be incomplete'; }
      meta.textContent = d.count + ' hits · ' + d.ms.toFixed(1) + ' ms · scope: ' +
        (SCOPE_LABEL[d.scope] || d.scope) + extra;
      meta.className = '';
      var frag = document.createDocumentFragment();
      for (var i = 0; i < d.results.length; i++) {
        var li = document.createElement('li');
        li.appendChild(highlight(d.results[i], d.terms));   // highlight + textContent, belt and braces
        frag.appendChild(li);
      }
      results.replaceChildren(frag);
    })
    .catch(function (err) {
      meta.textContent = 'Request failed: ' + err;
      meta.className = 'hint';
    });
}

form.addEventListener('submit', function (e) {
  e.preventDefault();
  doSearch();
});

syncScopeUI();
loadStatus();
loadVolumes();
</script>
</body>
</html>
"""
