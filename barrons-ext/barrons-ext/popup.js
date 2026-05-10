// popup.js — Barrons → Streetwise
//
// ── Architecture overview ─────────────────────────────────────────────────
// State variables (module-level):
//   _selectedModel   — active AI model ID (saved to chrome.storage.local)
//   _sessionCost     — running API cost total (saved to chrome.storage.local)
//   _sourcesRegistry — {prefix → source object} fetched from /api/sources
//   _activePrefix    — prefix of currently selected source (e.g. 'stw')
//
// Init order (DOMContentLoaded):
//   1. Restore chrome.storage.local → restore model/cost
//   2. Auto-detect tab title + date + source from current page (prefillFromPage)
//   3. pingServer()   → update the status dot
//   4. loadSources()  → build source dropdown, restore previous selection
//
// Flow when user clicks "Send to Streetwise":
//   sendPage() → getPageText() → chrome.storage.local.set(pendingJob) → open progress.html
//   progress.html reads pendingJob and posts to /api/extract-tickers
//
// DEBUG: To inspect extension state, open DevTools on the popup:
//   Right-click popup → Inspect → Console, then: chrome.storage.local.get(null, console.log)

// ── Progress log ───────────────────────────────────────────────────────────
function logClear() {
  var el = document.getElementById('progress-log');
  el.innerHTML = ''; el.style.display = 'none';
}
function log(msg, cls) {
  cls = cls || 'inf';
  var el = document.getElementById('progress-log');
  el.style.display = 'block';
  var line = document.createElement('div');
  var ts   = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  var icons = {ok:'✓',inf:'·',dim:' ',err:'✗',warn:'⚠',srv:'▸',cost:'$'};
  line.innerHTML = '<span class="dim">'+ts+'</span>  <span class="'+cls+'">'+(icons[cls]||'·')+' '+msg+'</span>';
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

// ── Status box ─────────────────────────────────────────────────────────────
function setStatus(type, html) {
  var box = document.getElementById('status-box');
  box.className = 'status '+type; box.innerHTML = html; box.style.display = 'block';
}
function clearStatus() {
  document.getElementById('status-box').style.display = 'none';
  logClear();
}

// ── Server base URL ───────────────────────────────────────────────────────
// EDIT: Update BASE_URL if the production server address changes.
//   Token auth is handled server-side via /etc/streetwise.env — no token needed here.
// DEBUG: If dot shows red "offline", open DevTools → Network and look for CORS or TLS errors.
var BASE_URL = 'https://app.barrons-watchlist-research.com';

// POST bodies — include Content-Type (triggers CORS preflight, handled by server)
// DEBUG: If POST requests get 403, check _check_token in server.py allows OPTIONS through
function authHeaders(extra) {
  return Object.assign({'Content-Type': 'application/json'}, extra || {});
}
// GET requests — no Content-Type so requests stay "simple" (no CORS preflight)
// WHY: Adding Content-Type to a GET makes it a non-simple request → triggers OPTIONS preflight
// DEBUG: If ping/loadSources fails with CORS error, check no Content-Type header is being added
function authGetHeaders() {
  return {};
}

// ── Episode key preview ───────────────────────────────────────────────────
function updatePreview() {
  var date   = document.getElementById('inp-date').value.trim()   || 'M/D';
  var year   = document.getElementById('inp-year').value.trim()   || '????';
  var prefix = _activePrefix || '???';
  document.getElementById('ep-key-preview').textContent = prefix+':'+year+'/'+date;
}

// ── Model selector ────────────────────────────────────────────────────────
var _selectedModel = 'claude-haiku';

function initModelButtons() {
  document.querySelectorAll('.mb').forEach(function(btn) {
    btn.addEventListener('click', function() {
      _selectedModel = this.dataset.model;
      document.querySelectorAll('.mb').forEach(function(b) {
        b.classList.remove('active-claude','active-gemini');
      });
      var isGemini = _selectedModel.startsWith('gemini');
      this.classList.add(isGemini ? 'active-gemini' : 'active-claude');
      chrome.storage.local.set({selectedModel: _selectedModel});
      log('Model: '+_selectedModel, 'dim');
    });
  });
}
function setActiveModel(model) {
  _selectedModel = model;
  document.querySelectorAll('.mb').forEach(function(b) {
    b.classList.remove('active-claude','active-gemini');
    if (b.dataset.model === model) {
      b.classList.add(model.startsWith('gemini') ? 'active-gemini' : 'active-claude');
    }
  });
}

// ── Session cost ──────────────────────────────────────────────────────────
var _sessionCost = 0;
function updateCostDisplay() {
  document.getElementById('session-cost').textContent = '$'+_sessionCost.toFixed(4);
}
function addCost(amount) {
  _sessionCost += parseFloat(amount) || 0;
  chrome.storage.local.set({sessionCost: _sessionCost});
  updateCostDisplay();
}
function resetCost() {
  _sessionCost = 0;
  chrome.storage.local.set({sessionCost: 0});
  updateCostDisplay();
  log('Session cost reset', 'dim');
}

// ── Sources registry ──────────────────────────────────────────────────────
// _sourcesRegistry: { prefix → {prefix, label, color, episodes: {key → {date,title}}} }
//   Populated from /api/sources on load; falls back to _FALLBACK_SOURCES if server offline
// _activePrefix: the currently selected source prefix (e.g. 'stw', 'ian')
//   Used by sendPage(), updatePreview(), and populateEpisodeDropdown()
// DEBUG: If episodes dropdown is empty, check _sourcesRegistry[prefix].episodes in DevTools console
var _sourcesRegistry = {};
var _activePrefix    = '';

// ── Source dropdown ───────────────────────────────────────────────────────
// WHY: Replaced quick-button pills with <select> — cleaner when sources list grows
// EDIT: To add a default source, append to _FALLBACK_SOURCES (used when server is offline)
var _FALLBACK_SOURCES = [
  {prefix:'stw', label:"Barron's Streetwise",    color:'#3b82f6'},
  {prefix:'ian', label:"Barron's Ian Salisbury",  color:'#8b5cf6'},
  {prefix:'div', label:'Dividends',               color:'#10b981'},
  {prefix:'bl',  label:'Barrons Live',            color:'#f59e0b'},
];

function _buildSourceOptions(sources) {
  var sel = document.getElementById('inp-source-sel');
  sel.innerHTML = '';
  sources.forEach(function(s) {
    var opt = document.createElement('option');
    opt.value          = s.prefix;
    opt.dataset.prefix = s.prefix;
    opt.dataset.label  = s.label || s.prefix;
    opt.textContent    = (s.label || s.prefix) + (s.count ? '  (' + s.count + ')' : '');
    sel.appendChild(opt);
  });
  // "Custom…" option at the end
  var custom = document.createElement('option');
  custom.value = '__custom__';
  custom.textContent = '— Custom…';
  sel.appendChild(custom);
}

function onSourceSelect(sel) {
  var val    = sel.value;
  var custom = document.getElementById('custom-source-row');

  if (val === '__custom__') {
    custom.classList.remove('hidden');
    // Clear inputs so user types fresh (not pre-filled with previous source values)
    document.getElementById('inp-source').value = '';
    document.getElementById('inp-prefix').value = '';
    _activePrefix = '';
    updatePreview();
    document.getElementById('inp-source').focus();
    document.getElementById('episode-row').style.display = 'none';
    return;
  }

  custom.classList.add('hidden');
  var opt    = sel.options[sel.selectedIndex];
  var prefix = opt.dataset.prefix || val;
  var label  = opt.dataset.label  || val;
  _setSource(prefix, label);
}

function _setSource(prefix, label) {
  _activePrefix = prefix;
  // Keep the hidden inputs populated so sendPage() can read them
  var srcEl = document.getElementById('inp-source');
  var pfxEl = document.getElementById('inp-prefix');
  if (srcEl) srcEl.value = label;
  if (pfxEl) pfxEl.value = prefix;
  updatePreview();
  _syncDropdown(prefix);
  populateEpisodeDropdown(prefix);
}

function selectSource(prefix, label) { _setSource(prefix, label); }

function _syncDropdown(prefix) {
  var sel = document.getElementById('inp-source-sel');
  for (var i = 0; i < sel.options.length; i++) {
    if (sel.options[i].dataset.prefix === prefix) {
      sel.selectedIndex = i;
      document.getElementById('custom-source-row').classList.add('hidden');
      return;
    }
  }
  // Not in list — switch to Custom
  for (var j = 0; j < sel.options.length; j++) {
    if (sel.options[j].value === '__custom__') { sel.selectedIndex = j; break; }
  }
  document.getElementById('custom-source-row').classList.remove('hidden');
}

// ── Episode dropdown ──────────────────────────────────────────────────────
// Shows existing episodes for the selected source so user can append tickers to them.
// Key format expected: "prefix:YYYY/M/D" — e.g. "stw:2025/3/15"
// DEBUG: If episodes never appear, check that /api/sources returns episodes dict per source
// DEBUG: If selecting an episode doesn't fill date/year, check key format matches "prefix:YYYY/M/D"
function populateEpisodeDropdown(prefix) {
  var epRow = document.getElementById('episode-row');
  var epSel = document.getElementById('inp-episode');
  var src   = _sourcesRegistry[prefix];
  if (!src || !src.episodes || Object.keys(src.episodes).length === 0) {
    epRow.style.display = 'none'; return;
  }
  epRow.style.display = '';
  epSel.innerHTML = '<option value="">— New episode —</option>';
  var eps = Object.entries(src.episodes).sort(function(a,b){
    return (b[1].date||'').localeCompare(a[1].date||'');
  });
  eps.forEach(function(entry) {
    var key = entry[0], info = entry[1];
    var opt = document.createElement('option');
    opt.value = key;
    var dp = info.date ? info.date.split('-') : [];
    var dLabel = dp.length===3
      ? (parseInt(dp[1])+'/'+parseInt(dp[2])+'/'+dp[0])
      : (key.split(':')[1] || key);
    opt.textContent = dLabel + (info.title ? ' — '+info.title.slice(0,38) : '');
    epSel.appendChild(opt);
  });
  epSel.onchange = function() {
    var key  = this.value; if (!key) return;
    var info = src.episodes[key]; if (!info) return;
    // BUG-GUARD: key format is "prefix:YYYY/M/D" — split(':')[1] is undefined for legacy keys
    var seg   = key.split(':')[1] || '';
    var parts = seg.split('/');
    if (parts.length === 3) {
      document.getElementById('inp-date').value = parts[1]+'/'+parts[2];
      document.getElementById('inp-year').value = parts[0];
    }
    if (info.title) document.getElementById('inp-title').value = info.title;
    updatePreview();
  };
}

// ── Server ping ───────────────────────────────────────────────────────────
async function pingServer() {
  var dot = document.getElementById('server-dot');
  var txt = document.getElementById('server-status-txt');
  try {
    var res = await fetch(BASE_URL + '/api/status', {headers:authGetHeaders(), signal:AbortSignal.timeout(2500)});
    if (res.ok) {
      var d = await res.json();
      dot.className = 'dot ok';
      txt.textContent = d.tickers_in_json + ' tickers';
    } else throw new Error('HTTP '+res.status);
  } catch(e) {
    dot.className = 'dot err';
    txt.textContent = 'offline';
  }
}

// ── Load sources ──────────────────────────────────────────────────────────
// Called on init and after deleteEpisode(). Fetches /api/sources, rebuilds the dropdown,
// then re-syncs the currently selected prefix so the UI state is consistent.
// DEBUG: If dropdown shows wrong sources, inspect /api/sources in browser or curl it
// NOTE: /api/sources returns episodes as an array [{key,date,title}...].
//   We convert to a dict {ep_key → {key,date,title}} so populateEpisodeDropdown
//   can do fast key lookups and option.value = ep_key (not array index).
async function loadSources() {
  try {
    var res = await fetch(BASE_URL + '/api/sources', {headers:authGetHeaders(), signal:AbortSignal.timeout(3000)});
    if (!res.ok) throw new Error('HTTP '+res.status);
    var data    = await res.json();
    var sources = data.sources || [];
    _sourcesRegistry = {};
    sources.forEach(function(s) {
      // Convert episodes array → dict keyed by ep_key for fast lookup
      var epMap = {};
      (s.episodes || []).forEach(function(ep) { if (ep.key) epMap[ep.key] = ep; });
      s.episodes = epMap;
      _sourcesRegistry[s.prefix] = s;
    });
    _buildSourceOptions(sources);
  } catch(e) {
    // Server offline or error — fall back to built-in list
    _buildSourceOptions(_FALLBACK_SOURCES);
  }

  // Re-sync current selection (preserves choice across reloads)
  if (_activePrefix) {
    _syncDropdown(_activePrefix);
    populateEpisodeDropdown(_activePrefix);
  } else {
    // Auto-select first item
    var sel = document.getElementById('inp-source-sel');
    if (sel.options.length && sel.options[0].value !== '__custom__') {
      sel.selectedIndex = 0;
      onSourceSelect(sel);
    }
  }
}

// ── Get article text from current tab ─────────────────────────────────────
async function getPageText(tabId) {
  var results = await chrome.scripting.executeScript({
    target: {tabId: tabId},
    func: function() {
      var NOISE = 'script,style,nav,header,footer,button,aside,[class*="Ad"],[class*="newsletter"],[class*="Subscribe"],[class*="related"]';
      function extractFrom(el) {
        var c = el.cloneNode(true);
        c.querySelectorAll(NOISE).forEach(function(n){n.remove();});
        return c.innerText.replace(/\n{3,}/g,'\n\n').trim();
      }
      var sels = ['article','[data-type="article"]','.article__body',
                  '[class*="ArticleBody"]','[class*="article-body"]',
                  '[class*="paywall"]','main'];
      for (var i = 0; i < sels.length; i++) {
        var el = document.querySelector(sels[i]);
        if (el) { var text = extractFrom(el); if (text.length > 800) return text; }
      }
      return Array.from(document.querySelectorAll('p'))
        .map(function(p){ return p.innerText.trim(); })
        .filter(function(t){ return t.length > 40; })
        .join('\n\n').trim();
    }
  });
  return (results[0] && results[0].result) || '';
}

// ── Prefill from current tab ──────────────────────────────────────────────
async function prefillFromPage(tab) {
  var results = await chrome.scripting.executeScript({
    target: {tabId: tab.id},
    func: function() {
      var h1 = document.querySelector('h1');
      var title = (h1 ? h1.innerText : document.title).trim().substring(0, 120);
      var dateMeta = (document.querySelector('meta[property="article:published_time"]') || {}).content
        || (document.querySelector('meta[name="date"]') || {}).content || '';
      return {title: title, dateMeta: dateMeta};
    }
  });
  var info = (results[0] && results[0].result) || {};
  if (info.title) document.getElementById('inp-title').value = info.title;
  if (info.dateMeta) {
    try {
      var d = new Date(info.dateMeta);
      document.getElementById('inp-date').value = (d.getMonth()+1)+'/'+d.getDate();
      document.getElementById('inp-year').value  = d.getFullYear();
    } catch(e) {}
  }
  var url = (tab.url || '').toLowerCase();
  if (url.includes('barrons.com')) {
    var isStw = url.includes('streetwise') || (tab.title||'').toLowerCase().includes('streetwise');
    var isBl  = url.includes('livecoverage') || url.includes('/live');
    if      (isStw) selectSource('stw', "Barron's Streetwise");
    else if (isBl)  selectSource('bl',  'Barrons Live');
    else            selectSource('ian', "Barron's Ian Salisbury");
  }
  updatePreview();
}

// ── Send page ─────────────────────────────────────────────────────────────
async function sendPage() {
  clearStatus();
  var btn  = document.getElementById('send-btn');
  var date = document.getElementById('inp-date').value.trim();
  var year = document.getElementById('inp-year').value.trim();

  // Read source from active selection
  var source = '';
  var prefix = '';
  var sel = document.getElementById('inp-source-sel');
  if (sel.value === '__custom__') {
    source = (document.getElementById('inp-source').value || '').trim();
    prefix = (document.getElementById('inp-prefix').value || '').trim().toLowerCase();
  } else {
    var opt = sel.options[sel.selectedIndex];
    prefix  = (opt && opt.dataset.prefix) || sel.value;
    source  = (opt && opt.dataset.label)  || prefix;
  }
  source = source || 'Unknown';
  prefix = prefix || 'src';

  var title = document.getElementById('inp-title').value.trim();
  var model = _selectedModel;

  if (!date)              { setStatus('error','⚠ Enter the article date'); return; }
  if (!year||!/^\d{4}$/.test(year)) { setStatus('error','⚠ Enter a 4-digit year'); return; }

  // Guard — server must be reachable
  if (document.getElementById('server-dot').classList.contains('err')) {
    setStatus('error',
      '✗ Server is offline at <b>'+BASE_URL+'</b><br>'+
      '<small>Check your internet connection or server status.</small>');
    return;
  }

  btn.disabled = true; btn.textContent = '⏳ Reading page…';

  try {
    var tabs = await chrome.tabs.query({active:true, currentWindow:true});
    var text = await getPageText(tabs[0].id);

    if (!text || text.trim().length < 100) {
      setStatus('error','⚠ Could not read article text. Is it fully loaded?');
      btn.disabled = false; btn.textContent = '📨  Send to Streetwise';
      return;
    }

    var job = {
      text:      text,
      textLen:   text.length,
      date:      date,
      year:      year,
      source:    source,
      prefix:    prefix,
      title:     title,
      model:     model,
      serverUrl: BASE_URL,
      token:     '',
      force:     document.getElementById('force-reextract').checked,
    };

    await chrome.storage.local.set({pendingJob: job});

    chrome.windows.create({
      url:     chrome.runtime.getURL('progress.html'),
      type:    'popup',
      width:   520,
      height:  580,
      focused: true,
    });

    setStatus('info', '⏳ Extraction started in a new window.');
    setTimeout(function() { window.close(); }, 800);

  } catch(e) {
    setStatus('error','✗ '+e.message+'<br><small>Is server.py running?</small>');
    btn.disabled = false; btn.textContent = '📨  Send to Streetwise';
  }
}

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async function() {

  // Restore saved settings
  var stored = await chrome.storage.local.get(['selectedModel','sessionCost']);
  if (stored.selectedModel) setActiveModel(stored.selectedModel);
  if (stored.sessionCost)  { _sessionCost = parseFloat(stored.sessionCost)||0; updateCostDisplay(); }

  document.getElementById('inp-year').value = new Date().getFullYear();

  // Input listeners (date + year → preview; custom source/prefix → preview)
  ['inp-date','inp-year'].forEach(function(id) {
    document.getElementById(id).addEventListener('input', updatePreview);
  });
  var srcEl = document.getElementById('inp-source');
  var pfxEl = document.getElementById('inp-prefix');
  if (srcEl) srcEl.addEventListener('input', updatePreview);
  if (pfxEl) pfxEl.addEventListener('input', function() {
    _activePrefix = this.value.trim().toLowerCase();
    updatePreview();
  });

  // Buttons
  document.getElementById('send-btn').addEventListener('click', sendPage);
  document.getElementById('cost-reset-btn').addEventListener('click', resetCost);
  initModelButtons();

  // Auto-detect source from current tab
  var tabs = await chrome.tabs.query({active:true, currentWindow:true});
  var tab  = tabs[0];
  if (tab) {
    document.getElementById('page-title').textContent = tab.title || tab.url || '—';
    try { await prefillFromPage(tab); } catch(e) {}
  }

  updatePreview();
  await pingServer();
  await loadSources();
});
