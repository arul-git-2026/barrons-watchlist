// progress.js — runs in the detached extraction window
// Reads job params from chrome.storage, runs extraction, shows live log

var _stopped  = false;
var _jobData  = null;
var _controller = null;

// ── Logging ────────────────────────────────────────────────────────────────
function log(msg, cls) {
  cls = cls || 'inf';
  var el = document.getElementById('log');
  var line = document.createElement('div');
  var ts = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  var icons = {ok:'✓',inf:'·',dim:' ',err:'✗',srv:'▸',cost:'$',warn:'⚠'};
  line.innerHTML = '<span class="dim">'+ts+'</span>  <span class="'+cls+'">'+(icons[cls]||'·')+' '+msg+'</span>';
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

// ── Status dot ─────────────────────────────────────────────────────────────
function setDot(state) {
  var dot   = document.getElementById('status-dot');
  var title = document.getElementById('win-title');
  var states = {
    running: ['running','Extracting…'],
    done:    ['done',   'Done'],
    error:   ['error',  'Error'],
    stopped: ['stopped','Stopped'],
  };
  var s = states[state] || states.running;
  dot.className = 'status-dot '+s[0];
  title.textContent = s[1];
  document.title = 'STW — '+s[1];
}

// ── Ticker chips ───────────────────────────────────────────────────────────
function showTickers(tickers, added, updated) {
  var el = document.getElementById('ticker-chips');
  el.innerHTML = '';
  (tickers || []).forEach(function(t) {
    var chip = document.createElement('span');
    chip.className = 'chip new';
    chip.textContent = t;
    el.appendChild(chip);
  });
}

// ── Summary stats ──────────────────────────────────────────────────────────
function setStats(added, updated, total, cost) {
  document.getElementById('s-new').textContent   = added   != null ? added   : '—';
  document.getElementById('s-upd').textContent   = updated != null ? updated : '—';
  document.getElementById('s-total').textContent = total   != null ? total   : '—';
  document.getElementById('s-cost').textContent  = cost    != null ? '$'+parseFloat(cost).toFixed(4) : '—';
}

// ── Cost tracking ──────────────────────────────────────────────────────────
function addSessionCost(amount) {
  chrome.storage.local.get('sessionCost', function(r) {
    var cur = parseFloat(r.sessionCost) || 0;
    chrome.storage.local.set({sessionCost: cur + (parseFloat(amount)||0)});
  });
}

// ── Run extraction ─────────────────────────────────────────────────────────
async function runExtraction(job) {
  _stopped = false;
  document.getElementById('btn-stop').disabled  = false;
  document.getElementById('btn-retry').disabled = true;
  setDot('running');
  setStats(null, null, null, null);

  log('source='+job.source+'  prefix='+job.prefix+'  ep='+job.prefix+':'+job.year+'/'+job.date, 'dim');
  log('model='+job.model+'  chars='+parseInt(job.textLen||0).toLocaleString(), 'dim');

  try {
    _controller = new AbortController();
    var ingestHeaders = {'Content-Type':'application/json'};
    if (job.token) ingestHeaders['X-Streetwise-Token'] = job.token;
    var res = await fetch(job.serverUrl+'/api/ingest-page', {
      method:  'POST',
      headers: ingestHeaders,
      signal:  _controller.signal,
      body: JSON.stringify({
        text:   job.text,
        date:   job.date,
        year:   job.year,
        source: job.source,
        prefix: job.prefix,
        title:  job.title,
        model:  job.model,
        force:  job.force || false,
      })
    });

    var data = await res.json();

    // Show server log lines
    if (data.log && data.log.length) {
      data.log.forEach(function(line) { log(line, 'srv'); });
    }

    if (!res.ok || !data.ok) {
      log('Error: '+(data.error||'HTTP '+res.status), 'err');
      setDot('error');
      document.getElementById('btn-retry').disabled = false;
      document.getElementById('btn-stop').disabled  = true;
      return;
    }

    // Already-processed article
    if (data.skipped) {
      log('⚠ ' + (data.message || 'Article already in DB'), 'warn');
      log('Tickers: ' + (data.tickers||[]).join(', '), 'dim');
      showTickers(data.tickers, 0, 0);
      setStats(0, 0, (data.tickers||[]).length, 0);
      setDot('done');
      document.getElementById('btn-stop').disabled  = true;
      document.getElementById('btn-retry').disabled = false;
      document.getElementById('btn-dash').disabled  = false;
      return;
    }

    // Cost
    if (data.cost) {
      addSessionCost(data.cost);
      log('cost: $'+parseFloat(data.cost).toFixed(4)+'  ('+( data.model||job.model)+')', 'cost');
    }

    log(data.tickers.length+' tickers extracted', 'ok');
    log(data.added+' new · '+data.updated+' updated', 'ok');

    showTickers(data.tickers, data.added, data.updated);
    setStats(data.added, data.updated, data.tickers.length, data.cost);
    setDot('done');

    document.getElementById('btn-stop').disabled  = true;
    document.getElementById('btn-retry').disabled = false;
    document.getElementById('btn-dash').disabled  = false;

  } catch(e) {
    if (e.name === 'AbortError' || _stopped) {
      log('Stopped by user', 'warn');
      setDot('stopped');
    } else {
      log('Error: '+e.message, 'err');
      setDot('error');
    }
    document.getElementById('btn-retry').disabled = false;
    document.getElementById('btn-stop').disabled  = true;
  }
}

// ── Stop ───────────────────────────────────────────────────────────────────
function stop() {
  _stopped = true;
  if (_controller) _controller.abort();
  document.getElementById('btn-stop').disabled  = true;
  document.getElementById('btn-retry').disabled = false;
  log('Stopping…', 'warn');
}

// ── Retry ──────────────────────────────────────────────────────────────────
function retry() {
  if (!_jobData) return;
  document.getElementById('log').innerHTML = '';
  document.getElementById('ticker-chips').innerHTML = '';
  runExtraction(_jobData);
}

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async function() {
  // Load job from storage
  var stored = await chrome.storage.local.get('pendingJob');
  var job    = stored.pendingJob;

  if (!job) {
    log('No job found — open from the extension popup', 'err');
    setDot('error');
    return;
  }

  // Clear pending job so it doesn't re-run on accidental reload
  chrome.storage.local.remove('pendingJob');
  _jobData = job;

  // Fill meta row
  document.getElementById('meta-source').textContent = job.source  || '—';
  document.getElementById('meta-ep').textContent     = (job.prefix||'?')+':'+(job.year||'?')+'/'+(job.date||'?');
  document.getElementById('meta-model').textContent  = job.model   || '—';
  document.getElementById('meta-chars').textContent  = parseInt(job.textLen||0).toLocaleString();

  // Wire buttons
  document.getElementById('btn-stop').addEventListener('click', stop);
  document.getElementById('btn-retry').addEventListener('click', retry);
  document.getElementById('btn-close').addEventListener('click', function() { window.close(); });
  document.getElementById('btn-dash').addEventListener('click', function() {
    var dashUrl = job.serverUrl + '/v2';
    chrome.tabs.create({url: dashUrl});
  });

  // Start
  runExtraction(job);
});
