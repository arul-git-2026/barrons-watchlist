// popup.js — Barrons → Streetwise  (sources-registry + model selector + cost tracker)

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
  var icon  = icons[cls] || '·';
  line.innerHTML = '<span class="dim">'+ts+'</span>  <span class="'+cls+'">'+icon+' '+msg+'</span>';
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}
function logTickers(added, updated, tickers) {
  var el = document.getElementById('progress-log');
  el.style.display = 'block';
  var line = document.createElement('div');
  line.style.marginTop = '4px';
  line.innerHTML = '<span class="ok">✓ '+added+' new · '+updated+' updated:  </span>'
    + tickers.map(function(t){ return '<span class="tck">'+t+'</span>'; }).join(' ');
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

// ── Helpers ────────────────────────────────────────────────────────────────
function getServerUrl() {
  return (document.getElementById('server-url').value || 'http://localhost:5000').replace(/\/$/,'');
}
function getToken() {
  return (document.getElementById('server-token').value || '').trim();
}
function authUrl(path) {
  return getServerUrl() + path;
}
function authHeaders(extra) {
  var h = Object.assign({'Content-Type': 'application/json'}, extra || {});
  var t = getToken();
  if (t) h['X-Streetwise-Token'] = t;
  return h;
}
function updatePreview() {
  var date   = document.getElementById('inp-date').value.trim()   || 'M/D';
  var year   = document.getElementById('inp-year').value.trim()   || '????';
  var prefix = document.getElementById('inp-prefix').value.trim().toLowerCase() || '???';
  document.getElementById('ep-key-preview').textContent = prefix+':'+year+'/'+date;
}
function setStatus(type, html) {
  var box = document.getElementById('status-box');
  box.className = 'status '+type; box.innerHTML = html; box.style.display = 'block';
}
function clearStatus() {
  document.getElementById('status-box').style.display = 'none';
  logClear();
}

// ── Model selector ─────────────────────────────────────────────────────────
var _selectedModel = 'claude-haiku';

function initModelButtons() {
  document.querySelectorAll('.mb').forEach(function(btn) {
    btn.addEventListener('click', function() {
      _selectedModel = this.dataset.model;
      // Clear all active classes
      document.querySelectorAll('.mb').forEach(function(b) {
        b.classList.remove('active-claude','active-gemini');
      });
      // Apply correct active class based on provider
      var isGemini = _selectedModel.startsWith('gemini');
      this.classList.add(isGemini ? 'active-gemini' : 'active-claude');
      // Persist
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

// ── Session cost tracker ───────────────────────────────────────────────────
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

// ── Sources registry cache ─────────────────────────────────────────────────
var _sourcesRegistry = {};

// ── Server ping ────────────────────────────────────────────────────────────
async function pingServer() {
  var dot = document.getElementById('server-dot');
  var txt = document.getElementById('server-status-txt');
  try {
    var res = await fetch(authUrl('/api/status'), {headers:authHeaders(),signal:AbortSignal.timeout(2500)});
    if (res.ok) {
      var d = await res.json();
      dot.className = 'dot ok';
      txt.textContent = d.tickers_in_json+' tickers';
    } else throw new Error('HTTP '+res.status);
  } catch(e) {
    dot.className = 'dot err';
    txt.textContent = 'offline';
  }
}

// ── Load sources ───────────────────────────────────────────────────────────
async function loadSources() {
  var container = document.getElementById('quick-btns');
  try {
    var res = await fetch(authUrl('/api/sources'), {headers:authHeaders(),signal:AbortSignal.timeout(3000)});
    if (!res.ok) throw new Error('HTTP '+res.status);
    var data = await res.json();
    var sources = data.sources || [];

    _sourcesRegistry = {};
    sources.forEach(function(s) { _sourcesRegistry[s.prefix] = s; });

    container.innerHTML = '';
    sources.forEach(function(s) {
      var btn = document.createElement('button');
      btn.className = 'qb';
      btn.dataset.prefix = s.prefix;
      btn.dataset.label  = s.label;
      btn.innerHTML =
        '<span class="qb-dot" style="background:'+(s.color||'#64748b')+'"></span>'
        + s.label
        + (s.count ? ' <span class="qb-count">('+s.count+')</span>' : '');
      btn.title = 'prefix: '+s.prefix;
      btn.addEventListener('click', function() {
        selectSource(this.dataset.prefix, this.dataset.label);
        document.querySelectorAll('.qb').forEach(function(b){ b.classList.remove('active'); });
        this.classList.add('active');
      });
      container.appendChild(btn);
    });
  } catch(e) {
    // Built-in fallbacks
    container.innerHTML = '';
    [{prefix:'stw',label:"Barron's Streetwise",color:'#3b82f6'},
     {prefix:'ian',label:"Barron's Ian Salisbury",color:'#8b5cf6'},
     {prefix:'div',label:'Dividends',color:'#10b981'},
     {prefix:'bl', label:'Barrons Live',color:'#f59e0b'}]
    .forEach(function(s) {
      var btn = document.createElement('button');
      btn.className = 'qb';
      btn.dataset.prefix = s.prefix; btn.dataset.label = s.label;
      btn.innerHTML = '<span class="qb-dot" style="background:'+s.color+'"></span>'+s.label;
      btn.addEventListener('click', function(){
        selectSource(this.dataset.prefix, this.dataset.label);
        document.querySelectorAll('.qb').forEach(function(b){ b.classList.remove('active'); });
        this.classList.add('active');
      });
      container.appendChild(btn);
    });
  }
}

function selectSource(prefix, label) {
  document.getElementById('inp-source').value = label;
  document.getElementById('inp-prefix').value = prefix;
  updatePreview();
  populateEpisodeDropdown(prefix);
}

function populateEpisodeDropdown(prefix) {
  var epRow = document.getElementById('episode-row');
  var epSel = document.getElementById('inp-episode');
  var src   = _sourcesRegistry[prefix];
  if (!src || !src.episodes || Object.keys(src.episodes).length === 0) {
    epRow.style.display = 'none'; return;
  }
  epRow.style.display = 'block';
  epSel.innerHTML = '<option value="">— New episode —</option>';
  var eps = Object.entries(src.episodes).sort(function(a,b){
    return (b[1].date||'').localeCompare(a[1].date||'');
  });
  eps.forEach(function(entry) {
    var key = entry[0], info = entry[1];
    var opt = document.createElement('option');
    opt.value = key;
    var dp = info.date ? info.date.split('-') : [];
    var dLabel = dp.length===3 ? (parseInt(dp[1])+'/'+parseInt(dp[2])+'/'+dp[0]) : (key.split(':')[1]||key);
    opt.textContent = dLabel + (info.title ? ' — '+info.title.slice(0,38) : '');
    epSel.appendChild(opt);
  });
  epSel.onchange = function() {
    var key = this.value; if (!key) return;
    var info = src.episodes[key]; if (!info) return;
    var parts = key.split(':')[1].split('/');
    if (parts.length===3) {
      document.getElementById('inp-date').value = parts[1]+'/'+parts[2];
      document.getElementById('inp-year').value = parts[0];
    }
    if (info.title) document.getElementById('inp-title').value = info.title;
    updatePreview();
  };
}

// ── Get article text ───────────────────────────────────────────────────────
async function getPageText(tabId) {
  var results = await chrome.scripting.executeScript({
    target: {tabId:tabId},
    func: function() {
      var NOISE = 'script,style,nav,header,footer,button,aside,[class*="Ad"],[class*="newsletter"],[class*="Subscribe"],[class*="related"],[class*="READ NEXT"]';

      function extractFrom(el) {
        var c = el.cloneNode(true);
        c.querySelectorAll(NOISE).forEach(function(n){n.remove();});
        return c.innerText.replace(/\n{3,}/g,'\n\n').trim();
      }

      // Try specific article selectors first
      var sels = [
        'article',
        '[data-type="article"]',
        '.article__body',
        '[class*="ArticleBody"]',
        '[class*="article-body"]',
        '[class*="paywall"]',   // Barrons wraps content in paywall div even when subscribed
        'main'
      ];
      for (var i = 0; i < sels.length; i++) {
        var el = document.querySelector(sels[i]);
        if (el) {
          var text = extractFrom(el);
          if (text.length > 800) return text;  // enough content — use it
        }
      }

      // Fallback: collect all paragraph text from the page
      var paras = Array.from(document.querySelectorAll('p'))
        .map(function(p){ return p.innerText.trim(); })
        .filter(function(t){ return t.length > 40; });
      return paras.join('\n\n').trim();
    }
  });
  return (results[0]&&results[0].result)||'';
}

// ── Prefill from page ──────────────────────────────────────────────────────
async function prefillFromPage(tab) {
  var results = await chrome.scripting.executeScript({
    target:{tabId:tab.id},
    func:function(){
      var h1=document.querySelector('h1');
      var title=(h1?h1.innerText:document.title).trim().substring(0,120);
      var dateMeta=(document.querySelector('meta[property="article:published_time"]')||{}).content
        ||(document.querySelector('meta[name="date"]')||{}).content||'';
      var bylineEl=document.querySelector('[class*="author"],[class*="byline"]');
      return{title:title,dateMeta:dateMeta,byline:bylineEl?bylineEl.innerText.trim():''};
    }
  });
  var info=(results[0]&&results[0].result)||{};
  if(info.title) document.getElementById('inp-title').value=info.title;
  if(info.dateMeta){
    try{
      var d=new Date(info.dateMeta);
      document.getElementById('inp-date').value=(d.getMonth()+1)+'/'+d.getDate();
      document.getElementById('inp-year').value=d.getFullYear();
    }catch(e){}
  }
  var url=(tab.url||'').toLowerCase();
  if(url.includes('barrons.com')){
    var isStw=url.includes('streetwise')||(tab.title||'').toLowerCase().includes('streetwise');
    var isBl=url.includes('livecoverage')||url.includes('/live');
    if(isStw)      selectSource('stw',"Barron's Streetwise");
    else if(isBl)  selectSource('bl','Barrons Live');
    else           selectSource('ian',"Barron's Ian Salisbury");
    var prefix=document.getElementById('inp-prefix').value;
    document.querySelectorAll('.qb').forEach(function(b){
      b.classList.toggle('active',b.dataset.prefix===prefix);
    });
  }
  updatePreview();
}

// ── Send page — opens detached progress window ────────────────────────────
async function sendPage() {
  clearStatus();
  var btn    = document.getElementById('send-btn');
  var date   = document.getElementById('inp-date').value.trim();
  var year   = document.getElementById('inp-year').value.trim();
  var source = document.getElementById('inp-source').value.trim() || 'Unknown';
  var prefix = document.getElementById('inp-prefix').value.trim().toLowerCase() || 'src';
  var title  = document.getElementById('inp-title').value.trim();
  var model  = _selectedModel;

  if(!date)  { setStatus('error','⚠ Enter the article date'); return; }
  if(!year||!/^\d{4}$/.test(year)) { setStatus('error','⚠ Enter a 4-digit year'); return; }
  if(!prefix){ setStatus('error','⚠ Enter a prefix'); return; }

  btn.disabled = true; btn.textContent = '⏳ Reading page…';

  try {
    var tabs = await chrome.tabs.query({active:true, currentWindow:true});
    var text = await getPageText(tabs[0].id);

    if (!text || text.trim().length < 100) {
      setStatus('error','⚠ Could not read article text. Is it fully loaded?');
      btn.disabled = false; btn.textContent = '📨  Send to Streetwise';
      return;
    }

    // Store job params — progress window picks this up on load
    var job = {
      text:      text,
      textLen:   text.length,
      date:      date,
      year:      year,
      source:    source,
      prefix:    prefix,
      title:     title,
      model:     model,
      serverUrl: getServerUrl(),
      token:     getToken(),
    };

    await chrome.storage.local.set({pendingJob: job});

    // Open detached progress window
    chrome.windows.create({
      url:    chrome.runtime.getURL('progress.html'),
      type:   'popup',
      width:  520,
      height: 580,
      focused: true,
    });

    // Update status in popup then close it
    setStatus('info', '⏳ Extraction started in a new window.');
    setTimeout(function() { window.close(); }, 800);

  } catch(e) {
    setStatus('error','✗ '+e.message+'<br><small>Is server.py running?</small>');
    btn.disabled = false; btn.textContent = '📨  Send to Streetwise';
  }
}

// ── Delete episode ─────────────────────────────────────────────────────────
async function deleteEpisode() {
  var epKey=document.getElementById('del-ep-key').value.trim();
  var rmEmpty=document.getElementById('del-rm-empty').checked;
  var statusEl=document.getElementById('del-status');
  var btn=document.getElementById('del-btn');
  statusEl.style.display='none';
  if(!epKey){statusEl.className='err';statusEl.textContent='⚠ Enter an episode key';statusEl.style.display='block';return;}
  if(!confirm('Delete episode "'+epKey+'"?\n\nThis cannot be undone.'))return;
  btn.disabled=true;btn.textContent='⏳…';
  try{
    var res=await fetch(authUrl('/api/delete-episode'),{
      method:'POST',headers:authHeaders(),
      body:JSON.stringify({ep_key:epKey,remove_empty:rmEmpty})
    });
    var data=await res.json();
    if(!res.ok||!data.ok)throw new Error(data.error||'server error');
    var msg='✓ Deleted "'+epKey+'"  ·  '+data.tickers_touched+' tickers updated';
    if(data.tickers_removed&&data.tickers_removed.length)
      msg+='  ·  '+data.tickers_removed.length+' removed';
    statusEl.className='ok';statusEl.textContent=msg;statusEl.style.display='block';
    document.getElementById('del-ep-key').value='';
    await loadSources();pingServer();
  }catch(e){
    statusEl.className='err';statusEl.textContent='✗ '+e.message;statusEl.style.display='block';
  }finally{btn.disabled=false;btn.textContent='Delete';}
}

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async function() {
  // Restore saved state
  var stored=await chrome.storage.local.get(['serverUrl','serverToken','selectedModel','sessionCost']);
  if(stored.serverUrl)   document.getElementById('server-url').value=stored.serverUrl;
  if(stored.serverToken) document.getElementById('server-token').value=stored.serverToken;
  if(stored.selectedModel) setActiveModel(stored.selectedModel);
  if(stored.sessionCost)  { _sessionCost=parseFloat(stored.sessionCost)||0; updateCostDisplay(); }

  document.getElementById('inp-year').value=new Date().getFullYear();

  // Input listeners
  ['inp-date','inp-year','inp-prefix'].forEach(function(id){
    document.getElementById(id).addEventListener('input',updatePreview);
  });
  document.getElementById('inp-source').addEventListener('input',function(){
    document.getElementById('episode-row').style.display='none';
    updatePreview();
  });
  document.getElementById('server-url').addEventListener('change',function(){
    chrome.storage.local.set({serverUrl:getServerUrl()});
    loadSources();pingServer();
  });
  document.getElementById('server-token').addEventListener('change',function(){
    chrome.storage.local.set({serverToken:getToken()});
    loadSources();pingServer();
  });

  // Buttons
  document.getElementById('send-btn').addEventListener('click',sendPage);
  document.getElementById('del-btn').addEventListener('click',deleteEpisode);
  document.getElementById('cost-reset-btn').addEventListener('click',resetCost);
  initModelButtons();

  // Auto-detect from current tab
  var tabs=await chrome.tabs.query({active:true,currentWindow:true});
  var tab=tabs[0];
  if(tab){
    document.getElementById('page-title').textContent=tab.title||tab.url||'—';
    try{await prefillFromPage(tab);}catch(e){}
  }

  updatePreview();
  await pingServer();
  await loadSources();

  // Re-mark active source button
  var curPrefix=document.getElementById('inp-prefix').value;
  if(curPrefix){
    document.querySelectorAll('.qb').forEach(function(b){
      b.classList.toggle('active',b.dataset.prefix===curPrefix);
    });
    populateEpisodeDropdown(curPrefix);
  }
});
