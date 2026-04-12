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

    if (data.article_summary) {
      renderSummary(data.article_summary);
    }

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

// ── Render markdown summary ────────────────────────────────────────────────
function mdToHtml(md) {
  var lines = md.split('\n');
  var html  = '';
  var inUl  = false, inTbl = false;

  function closeUl()  { if (inUl)  { html += '</ul>';   inUl  = false; } }
  function closeTbl() { if (inTbl) { html += '</tbody></table>'; inTbl = false; } }

  lines.forEach(function(raw) {
    var line = raw;

    // Inline: bold
    line = line.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Inline: italic
    line = line.replace(/\*(.+?)\*/g, '<em>$1</em>');

    // HR
    if (/^---+$/.test(raw.trim())) {
      closeUl(); closeTbl();
      html += '<hr>'; return;
    }
    // H2
    if (/^## /.test(raw)) {
      closeUl(); closeTbl();
      html += '<h2>' + line.replace(/^## /, '') + '</h2>'; return;
    }
    // H3
    if (/^### /.test(raw)) {
      closeUl(); closeTbl();
      html += '<h3>' + line.replace(/^### /, '') + '</h3>'; return;
    }
    // Table row
    if (/^\|/.test(raw.trim())) {
      if (/^[\|\s\-:]+$/.test(raw.replace(/[|]/g,''))) return; // separator row
      var cells = raw.split('|').filter(function(c,i,a){ return i>0 && i<a.length-1; });
      var cellHtml = cells.map(function(c){ return '<td>'+c.trim().replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')+'</td>'; }).join('');
      if (!inTbl) {
        closeUl();
        // First row → header
        var thHtml = cells.map(function(c){ return '<th>'+c.trim()+'</th>'; }).join('');
        html += '<table><thead><tr>'+thHtml+'</tr></thead><tbody>';
        inTbl = true;
      } else {
        html += '<tr>'+cellHtml+'</tr>';
      }
      return;
    }
    closeTbl();
    // Bullet
    if (/^[\*\-] /.test(raw.trim())) {
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += '<li>' + line.replace(/^[\s\*\-]+ /, '') + '</li>'; return;
    }
    closeUl();
    // Blank line
    if (!raw.trim()) { html += ''; return; }
    // Paragraph
    html += '<p>' + line + '</p>';
  });
  closeUl(); closeTbl();
  return html;
}

function renderSummary(md) {
  var el = document.getElementById('article-summary');
  el.innerHTML = mdToHtml(md);
  el.style.display = 'block';
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
