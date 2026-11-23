// pywebview 的 js_api 在 window.pywebview.api，需等就绪
let api = null;
let lastLogCount = 0;
const pendingJobs = new Set();  // 已入队、尚未收到 done 的任务 id
const thinkingByJob = new Map(); // job id -> 该任务的“思考中”占位
let runningJobId = null;   // job_start 标记正在执行的任务
let lastToolCall = null;   // 最近一次工具调用，供结果卡重试
let logGen = -1;           // 日志代际号，配合后端 clear_logs 丢弃在途旧日志
const ERR_HINT = {
  1455: '电脑内存临时不够了，请关掉其它程序再试',
  'target page': '页面意外关闭，正在自动恢复',
  unicodedecode: '有个文件读不出来，可能是编码不对',
  timeout: '连接超时，请检查网络后重试',
  429: '请求太频繁被限流，稍等一会再试',
  401: 'API Key 无效，请在设置里重新填写'
};

function friendlyError(detail) {
  const s = String(detail || '');
  for (const k of Object.keys(ERR_HINT)) {
    if (s.toLowerCase().includes(k.toLowerCase())) return ERR_HINT[k];
  }
  return '操作失败，可展开详情查看原因';
}

function onReady(fn) {
  if (window.pywebview && window.pywebview.api) fn();
  else window.addEventListener('pywebviewready', fn);
}

const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function scrollChat() {
  const log = $('chatLog');
  log.scrollTop = log.scrollHeight;
}

// ---- 对话历史持久化（localStorage）----
const HISTORY_KEY = 'downloader_chat_history';
const HISTORY_MAX = 200;

function saveHistory() {
  try {
    const msgs = Array.from($('chatLog').querySelectorAll('.msg'))
      .filter((n) => !n.classList.contains('thinking'))
      .slice(-HISTORY_MAX)
      .map((n) => ({ html: n.outerHTML }));
    localStorage.setItem(HISTORY_KEY, JSON.stringify(msgs));
  } catch (e) { /* 隐私模式或空间不足时静默跳过 */ }
}

function restoreHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (!raw) return false;
    const msgs = JSON.parse(raw);
    if (!Array.isArray(msgs) || !msgs.length) return false;
    const log = $('chatLog');
    log.innerHTML = '';
    msgs.forEach((m) => {
      const div = document.createElement('div');
      div.innerHTML = m.html;
      if (div.firstChild) log.appendChild(div.firstChild);
    });
    scrollChat();
    return true;
  } catch (e) { return false; }
}

function clearHistory() {
  try { localStorage.removeItem(HISTORY_KEY); } catch (e) {}
  $('chatLog').innerHTML = '';
  saveHistory();
}

function addMessage(text, who) {
  const div = document.createElement('div');
  div.className = 'msg msg-' + who;
  const avatar = who === 'user' ? '我' : '助';
  div.innerHTML =
    '<div class="msg-avatar">' + avatar + '</div>' +
    '<div class="msg-body"><div class="msg-text">' + escapeHtml(text) + '</div></div>';
  $('chatLog').appendChild(div);
  scrollChat();
  saveHistory();
  return div;
}

function toolStatusCard(name, args, status) {
  const rows = Object.entries(args || {}).map(([k, v]) =>
    '<div class="tool-card-row"><span class="tool-card-key">' + escapeHtml(k) +
    '</span><span class="tool-card-val">' + escapeHtml(typeof v === 'object' ? JSON.stringify(v) : v) +
    '</span></div>').join('');
  return '<div class="msg msg-agent">' +
    '<div class="msg-avatar">助</div>' +
    '<div class="msg-body"><div class="tool-card" data-tool-card>' +
    '<div class="tool-card-head"><span class="tool-card-icon">&gt;</span>' +
    '<span class="tool-card-name">' + escapeHtml(name) + '</span>' +
    '<span class="tool-card-state ' + status.cls + '">' + escapeHtml(status.text) + '</span></div>' +
    (rows ? '<div class="tool-card-args">' + rows + '</div>' : '') +
    '</div></div></div>';
}

function addToolStatus(name, args) {
  const div = document.createElement('div');
  div.innerHTML = toolStatusCard(name, args, { cls: 'pending', text: '调用中' });
  const card = div.firstChild;
  $('chatLog').appendChild(card);
  card.dataset.toolName = name;
  card.dataset.toolArgs = JSON.stringify(args || {});
  lastToolCall = { name, args: args || {} };
  scrollChat();
  saveHistory();
}

function markToolStatus(result) {
  const cards = $('chatLog').querySelectorAll('.tool-card[data-tool-name]');
  const card = Array.from(cards).reverse().find((n) => !n.dataset.toolDone);
  if (!card) return;
  let args = {};
  try { args = JSON.parse(card.dataset.toolArgs || '{}'); } catch (e) {}
  const ok = !!(result && result.success);
  card.dataset.toolDone = ok ? 'ok' : 'fail';
  const head = card.querySelector('.tool-card-head');
  if (head) {
    const old = head.querySelector('.tool-card-state');
    if (old) old.remove();
    const state = document.createElement('span');
    state.className = 'tool-card-state ' + (ok ? 'ok' : 'fail');
    state.textContent = ok ? '完成' : '失败';
    head.appendChild(state);
  }
  if (!ok) {
    const argsBox = card.querySelector('.tool-card-args');
    const detail = result && (result.detail || result.message);
    if (detail) {
      const err = document.createElement('div');
      err.className = 'tool-card-error';
      err.textContent = String(detail);
      (argsBox || card.querySelector('.tool-card')).appendChild(err);
    }
  }
  saveHistory();
}

function detailBlock(detail) {
  if (!detail) return '';
  return '<details class="msg-tool-log"><summary>查看详情</summary>' +
    '<pre>' + escapeHtml(detail) + '</pre></details>';
}

function addResult(result) {
  const div = document.createElement('div');
  div.className = 'msg msg-agent';
  const cls = result.success ? 'ok' : 'fail';
  const text = result.success
    ? ('✓ ' + (result.message || ''))
    : ('✗ ' + friendlyError(result.detail || result.message));
  const retry = (!result.success && lastToolCall)
    ? '<button class="text-btn retry-btn" data-retry>重试</button>' : '';
  div.innerHTML =
    '<div class="msg-avatar">助</div>' +
    '<div class="msg-body"><div class="msg-result ' + cls + '">' +
    escapeHtml(text) + '</div>' +
    (result.success ? '' : detailBlock(result.detail || result.message) + retry) + '</div>';
  $('chatLog').appendChild(div);
  div.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-retry]');
    if (btn) retryResult(btn);
  });
  scrollChat();
  saveHistory();
}

async function retryResult(btn) {
  if (!lastToolCall) return;
  btn.disabled = true;
  try {
    const r = await api.retry_tool(lastToolCall.name, lastToolCall.args);
    if (r && r.ok) {
      pendingJobs.add(r.id);
      refreshBusy();
      addMessage('重试：' + lastToolCall.name + '（任务 #' + r.id + '）', 'user');
    } else {
      addResult(r);
    }
  } catch (e) { addResult({ success: false, message: friendlyError(e), detail: String(e) }); }
  btn.disabled = false;
}

// 任务占位：发送后立刻显示排队中；收到首个真实回复后移除
function showThinking() {
  const div = document.createElement('div');
  div.className = 'msg msg-agent thinking';
  div.innerHTML = '<div class="msg-avatar">助</div>' +
    '<div class="msg-body"><div class="msg-text">排队中...</div></div>';
  $('chatLog').appendChild(div);
  scrollChat();
  return div;
}
function removeThinking(node) {
  if (node && node.parentNode) node.remove();
  else $('chatLog').querySelectorAll('.msg.thinking').forEach((n) => n.remove());
}
function clearThinking(jobId) {
  const node = thinkingByJob.get(jobId);
  if (node) { removeThinking(node); thinkingByJob.delete(jobId); }
}

function refreshBusy() {
  const pending = Array.from(pendingJobs);
  const first = pending[0];
  $('toolStatus').textContent = pending.length
    ? '任务 #' + first + ' 处理中' + (pending.length > 1 ? '，另有 ' + (pending.length - 1) + ' 个排队' : '')
    : '';
}

async function send() {
  const input = $('chatInput');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addMessage(text, 'user');
  const thinking = showThinking();
  try {
    const r = await api.chat(text);
    if (r && r.id) {
      pendingJobs.add(r.id);
      thinkingByJob.set(r.id, thinking);
      refreshBusy();
    } else if (r && r.ok === false) {
      removeThinking(thinking);
      addResult(r);
    }
  } catch (e) {
    removeThinking(thinking);
    addResult({ success: false, message: friendlyError(e), detail: String(e) });
  }
}

// 单个任务终结：只移除对应 thinking，其他排队任务的占位保留
function endTurn(jobId) {
  pendingJobs.delete(jobId);
  clearThinking(jobId);
  refreshBusy();
}

// 轮询后端事件
async function poll() {
  try {
    const events = await api.poll();
    events.forEach((ev) => {
      switch (ev.event) {
        case 'job_start':
          runningJobId = ev.data && ev.data.id;
          break;
        case 'msg':
          if (runningJobId) clearThinking(runningJobId);
          else removeThinking();
          if (ev.data !== '思考中...' && ev.data) addMessage(ev.data, 'agent');
          break;
        case 'tool':
          if (ev.data && ev.data.job_id) clearThinking(ev.data.job_id);
          else if (runningJobId) clearThinking(runningJobId);
          else removeThinking();
          addToolStatus(ev.data.name, ev.data.args);
          break;
        case 'result':
          markToolStatus(ev.data);
          addResult(ev.data);
          break;
        case 'done':
          if (ev.data && ev.data.id) endTurn(ev.data.id);
          else if (ev.data && ev.data.kind === 'chat' && runningJobId) endTurn(runningJobId);
          break;
        case 'xhs_login_done':
        case 'site_login_done':
          $('testResult').textContent = ev.data.message || (ev.data.success ? '登录成功，已记住' : '登录失败');
          $('testResult').className = 'test-result ' + (ev.data.success ? 'ok' : 'fail');
          break;
        case 'state':
          updateState(ev.data);
          break;
      }
    });
  } catch (e) {
    console.error('poll error', e);
  }
  setTimeout(poll, 600);
}

function updateState(s) {
  $('taskName').textContent = s.task || '无';
  $('progressText').textContent = s.done + ' / ' + s.total;
  const pct = s.total ? Math.round(s.done / s.total * 100) : 0;
  $('progressPct').textContent = pct + '%';
  $('progressBar').style.width = pct + '%';

  const pill = $('statusPill');
  if (s.running) {
    pill.textContent = '运行中'; pill.className = 'pill pill-run';
  } else if (s.done > 0) {
    pill.textContent = '就绪'; pill.className = 'pill pill-done';
  } else {
    pill.textContent = '就绪'; pill.className = 'pill pill-muted';
  }

  const queueBox = $('queueList');
  const queue = s.queue || [];
  if (queueBox) {
    queueBox.classList.toggle('hidden', queue.length === 0);
    queueBox.innerHTML = queue.map((q) => '<div class="queue-item">#' + escapeHtml(q.id) +
      ' ' + escapeHtml(typeof q.name === 'string' ? q.name.slice(0, 40) : q.name) + '</div>').join('');
  }

  // 每集进度网格 + 取消按钮
  const eps = s.episodes || {};
  const keys = Object.keys(eps);
  const grid = $('episodeGrid');
  const cancelBtn = $('cancelTaskBtn');
  if (keys.length) {
    grid.classList.remove('hidden');
    const html = keys.map((k) => '<span class="ep ' + escapeHtml(eps[k]) + '" title="第' + escapeHtml(k) + '集：' + escapeHtml(eps[k]) + '">' + escapeHtml(k) + '</span>').join('');
    if (grid.innerHTML !== html) grid.innerHTML = html;
  } else {
    grid.classList.add('hidden');
    grid.innerHTML = '';
  }
  cancelBtn.classList.toggle('hidden', !s.running);

  // 日志增量渲染：代际号变化时重置（清空日志后丢弃在途 poll 的旧响应）
  const logs = s.logs || [];
  const gen = s.log_gen || 0;
  if (gen !== logGen) {
    $('logList').innerHTML = '<div class="log-empty">暂无日志</div>';
    lastLogCount = 0;
    logGen = gen;
  }
  if (logs.length < lastLogCount) { $('logList').innerHTML = ''; lastLogCount = 0; }
  if (logs.length > lastLogCount) {
    const empty = $('logList').querySelector('.log-empty');
    if (empty) empty.remove();
    for (let i = lastLogCount; i < logs.length; i++) {
      const d = document.createElement('div');
      d.className = 'log-item';
      d.textContent = logs[i];
      $('logList').appendChild(d);
    }
    lastLogCount = logs.length;
    const box = $('logList');
    box.scrollTop = box.scrollHeight;
  }
}

// 设置
async function openSettings() {
  let cfg;
  try { cfg = await api.get_config(); }
  catch (e) { alert('读取配置失败：' + friendlyError(e)); return; }
  const sel = $('providerSelect');
  sel.innerHTML = '';
  Object.entries(cfg.providers || {}).forEach(([k, v]) => {
    const opt = document.createElement('option');
    opt.value = k; opt.textContent = v.label;
    if (k === cfg.provider) opt.selected = true;
    sel.appendChild(opt);
  });
  $('apiKeyInput').value = '';  // 后端不回传真实 Key；留空表示不修改
  $('apiKeyInput').placeholder = cfg.configured ? '已配置，留空表示不修改' : '粘贴 API Key';
  $('modelInput').value = cfg.model || '';
  $('notifyOnComplete').checked = !!cfg.notify_on_complete;
  $('proxyInput').value = cfg.proxy || '';
  $('xhsCookieInput').value = cfg.xhs_cookie || '';
  $('siteUrlInput').value = cfg.last_site || '';
  $('testResult').textContent = '';
  $('settingsOverlay').classList.remove('hidden');
  refreshLoginStatus();
}

async function refreshLoginStatus() {
  const box = $('loginStatus');
  if (!box) return;
  box.textContent = '检测中...';
  try {
    const r = await api.get_login_status();
    const label = { ok: '已登录', none: '未登录', unknown: '未知' };
    box.innerHTML = Object.entries(r).map(([k, v]) =>
      '<div class="login-item"><b>' + escapeHtml(k) + '</b> ' + escapeHtml(label[v.status] || v.status) +
      (v.domains && v.domains.length ? ' (' + escapeHtml(v.domains.slice(0, 3).join(', ')) + ')' : '') +
      '</div>').join('');
  } catch (e) { box.textContent = '检测失败：' + friendlyError(e); }
}

async function exportConfig() {
  try {
    const r = await api.export_config();
    const text = JSON.stringify(r.config, null, 2);
    const blob = new Blob([text], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'downloader-config.json';
    a.click();
    URL.revokeObjectURL(a.href);
    showTest(r);
  } catch (e) { showTest({ ok: false, message: friendlyError(e), detail: String(e) }); }
}

async function importConfig() {
  const text = prompt('粘贴之前导出的配置 JSON；API Key/Cookie 不会导入');
  if (!text) return;
  try { showTest(await api.import_config(text)); }
  catch (e) { showTest({ ok: false, message: friendlyError(e), detail: String(e) }); }
}

async function checkUpdate() {
  const box = $('updateResult');
  box.classList.remove('hidden');
  box.className = 'update-result';
  box.textContent = '正在检查更新...';
  try {
    const r = await api.check_update();
    if (!r.ok) { box.className = 'update-result fail'; box.textContent = friendlyError(r.detail || r.message); return; }
    window.updateInfo = r;
    if (!r.has_update) {
      box.className = 'update-result ok';
      box.textContent = '当前已是最新版本（' + r.current + '）';
      return;
    }
    box.textContent = '发现新版本 ' + r.latest + '：' + (r.note || (r.asset ? '可下载安装包：' + r.asset : '请到发布页下载'));
    const download = document.createElement('button');
    download.className = 'btn btn-ghost';
    download.textContent = r.asset ? '下载安装包' : '打开发布页';
    download.onclick = () => downloadUpdate();
    box.appendChild(download);
  } catch (e) {
    box.className = 'update-result fail';
    box.textContent = '检查更新失败：' + friendlyError(e);
  }
}

async function downloadUpdate() {
  const r = window.updateInfo;
  const box = $('updateResult');
  if (!r || !r.asset) {
    if (r && r.page) api.open_external(r.page);
    return;
  }
  box.textContent = '正在下载 ' + r.asset + ' ...';
  try {
    const d = await api.download_update(r.asset, r.page);
    if (!d.ok) { box.className = 'update-result fail'; box.textContent = friendlyError(d.detail || d.message); return; }
    box.className = 'update-result ok';
    box.textContent = '已下载到：' + d.path + '。请手动运行安装包完成更新。';
  } catch (e) {
    box.className = 'update-result fail';
    box.textContent = '下载失败：' + friendlyError(e);
  }
}

function showTest(r) {
  const msg = r.ok ? r.message : friendlyError(r.detail || r.message);
  $('testResult').textContent = msg;
  $('testResult').className = 'test-result ' + (r.ok ? 'ok' : 'fail');
  if (!r.ok && r.detail) {
    $('testResult').title = r.detail;
  }
}

async function saveSettings() {
  const cfg = {
    provider: $('providerSelect').value,
    api_key: $('apiKeyInput').value.trim(),
    model: $('modelInput').value.trim(),
    proxy: $('proxyInput').value.trim(),
    xhs_cookie: $('xhsCookieInput').value.trim(),
    last_site: $('siteUrlInput').value.trim(),
    notify_on_complete: $('notifyOnComplete').checked
  };
  try { showTest(await api.save_config(JSON.stringify(cfg))); }
  catch (e) { showTest({ ok: false, message: friendlyError(e), detail: String(e) }); }
}

async function testLLM() {
  $('testResult').textContent = '测试中...';
  $('testResult').className = 'test-result';
  try { showTest(await api.check_llm()); }
  catch (e) { showTest({ ok: false, message: friendlyError(e), detail: String(e) }); }
}

// ============ 播放器（统一用系统内置播放器）============
function openPlayer(path, title) {
  api.asset_open(path).then((r) => {
    $('assetFooter').textContent = (r && r.success)
      ? '已用系统播放器打开'
      : '打开失败：' + ((r && r.message) || '未知错误');
  }).catch(() => {
    $('assetFooter').textContent = '打开失败，请稍后重试';
  });
}

// ============ 资产库 ============
let assetsData = [];
let currentCat = "__all__";
let assetSortDir = -1;

function fmtSize(n) {
  if (n >= 2**30) return (n/2**30).toFixed(2) + ' GB';
  if (n >= 2**20) return (n/2**20).toFixed(0) + ' MB';
  return (n/1024).toFixed(0) + ' KB';
}
function fmtDur(s) {
  if (!s) return '-';
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), sec = s%60;
  return h ? h+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0')
           : m+':'+String(sec).padStart(2,'0');
}
function fmtDate(ts) {
  const d = new Date(ts*1000);
  return (d.getMonth()+1)+'-'+String(d.getDate()).padStart(2,'0');
}

async function loadAssets(force) {
  $('assetStats').innerHTML = '扫描中...';
  try {
    const r = await api.asset_scan(!!force);
    if (!r.success) { $('assetStats').textContent = '扫描失败：' + friendlyError(r.message); return; }
    assetsData = r.data.items;
    if (r.message) window.lastAssetScanMessage = r.message;
    renderCats(r.data.stats);
    renderAssets();
  } catch (e) {
    $('assetStats').textContent = '扫描失败：' + friendlyError(e);
  }
}

function renderCats(stats) {
  const tree = $('catTree');
  tree.innerHTML = '';
  const all = document.createElement('div');
  all.className = 'cat-item' + (currentCat==='__all__'?' active':'');
  all.innerHTML = '<span>全部</span><span class="cnt">'+stats.total+'</span>';
  all.onclick = () => { currentCat='__all__'; renderCats(stats); renderAssets(); };
  tree.appendChild(all);
  Object.entries(stats.cats || {}).forEach(([cat, v]) => {
    const d = document.createElement('div');
    d.className = 'cat-item' + (currentCat===cat?' active':'');
    d.innerHTML = '<span>'+escapeHtml(cat)+'</span><span class="cnt">'+v.count+'</span>';
    d.onclick = () => { currentCat=cat; renderCats(stats); renderAssets(); };
    tree.appendChild(d);
  });
  $('assetStats').innerHTML = '共 <b>'+stats.total+'</b> 个视频，<b>'+stats.total_gb+' GB</b>' +
    (window.lastAssetScanMessage ? '<span class="asset-scan-note"> · ' + escapeHtml(window.lastAssetScanMessage) + '</span>' : '');
}

function renderAssets() {
  const q = $('assetSearch').value.trim().toLowerCase();
  const sort = $('assetSort').value;
  const ext = $('assetExt').value;
  const size = $('assetSize').value;
  const dur = $('assetDuration').value;
  const days = Number($('assetDate').value || 0);
  const cutoff = days ? Date.now() / 1000 - days * 86400 : 0;
  let rows = assetsData.filter(x =>
    (currentCat==='__all__' || x.cat===currentCat) &&
    (!q || x.name.toLowerCase().includes(q) || x.rel.toLowerCase().includes(q)) &&
    (!ext || x.name.toLowerCase().endsWith(ext)) &&
    (!size || (size==='small' && x.size < 100*2**20) ||
      (size==='medium' && x.size >= 100*2**20 && x.size <= 2**30) ||
      (size==='large' && x.size > 2**30)) &&
    (!dur || (dur==='short' && x.dur < 300) ||
      (dur==='medium' && x.dur >= 300 && x.dur <= 1800) ||
      (dur==='long' && x.dur > 1800)) &&
    (!cutoff || x.mtime >= cutoff)
  );
  rows.sort((a,b) => {
    let v = 0;
    if (sort==='name') v = a.name.localeCompare(b.name);
    else if (sort==='size') v = a.size - b.size;
    else if (sort==='dur') v = a.dur - b.dur;
    else v = a.mtime - b.mtime;
    return v * assetSortDir;
  });
  const tbody = $('assetRows');
  const CAP = 500;  // 渲染上限，防 2600 行全量建 DOM 卡顿
  const shown = rows.slice(0, CAP);
  tbody.innerHTML = shown.map(x =>
    '<tr data-path="'+escapeHtml(x.path)+'">' +
    '<td><input type="checkbox" class="asset-check"></td>' +
    '<td class="fname" title="'+escapeHtml(x.rel)+'">'+escapeHtml(x.name)+'</td>' +
    '<td>'+escapeHtml(x.cat)+'</td>' +
    '<td class="num">'+fmtSize(x.size)+'</td>' +
    '<td class="num">'+fmtDur(x.dur)+'</td>' +
    '<td class="num">'+fmtDate(x.mtime)+'</td></tr>'
  ).join('');
  const more = rows.length > CAP ? ' · 仅显示前 '+CAP+' 行，请细化搜索' : '';
  $('assetFooter').textContent = '显示 '+shown.length+' / '+assetsData.length+' 个 · 双击打开 · 右键定位文件'+more;
}

function selectedAssets() {
  return Array.from($('assetRows').querySelectorAll('tr[data-path]'))
    .filter((tr) => tr.querySelector('.asset-check').checked)
    .map((tr) => tr.dataset.path);
}

function exportAssetCsv() {
  const paths = selectedAssets();
  if (!paths.length) { $('assetFooter').textContent = '请先勾选视频'; return; }
  const csv = 'path\n' + paths.map((p) => '"' + p.replace(/"/g, '""') + '"').join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'video_assets.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// 事件委托：只在 tbody 上绑一次，避免每次渲染绑定 2N 个监听器
function initAssetEvents() {
  const tbody = $('assetRows');
  tbody.addEventListener('dblclick', (e) => {
    const tr = e.target.closest('tr[data-path]');
    if (tr) openPlayer(tr.dataset.path, tr.querySelector('.fname').textContent);
  });
  tbody.addEventListener('contextmenu', (e) => {
    const tr = e.target.closest('tr[data-path]');
    if (!tr) return;
    e.preventDefault();
    api.asset_locate(tr.dataset.path).catch(() => {});
  });
}

let searchTimer = null;
function debouncedSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(renderAssets, 200);
}

// ============ 主题切换 ============
function applyTheme(t) {
  if (t === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else document.documentElement.removeAttribute('data-theme');
  const btn = $('themeBtn');
  if (btn) btn.textContent = t === 'light' ? '☾' : '◐';
}

async function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  const next = cur === 'light' ? 'dark' : 'light';
  applyTheme(next);
  try { await api.set_theme(next); } catch (e) { /* 偏好存不上也不影响本次 */ }
}

onReady(async () => {
  api = window.pywebview.api;
  try {
    const r = await api.get_theme();
    applyTheme(r.theme || 'dark');
  } catch (e) { applyTheme('dark'); }
  $('themeBtn').addEventListener('click', toggleTheme);
  $('tabDownload').onclick = () => { $('viewDownload').classList.remove('hidden'); $('viewAssets').classList.add('hidden'); $('tabDownload').classList.add('active'); $('tabAssets').classList.remove('active'); };
  $('tabAssets').onclick = () => { $('viewDownload').classList.add('hidden'); $('viewAssets').classList.remove('hidden'); $('tabAssets').classList.add('active'); $('tabDownload').classList.remove('active'); if (!assetsData.length) loadAssets(false); };
  $('rescanBtn').onclick = () => loadAssets(true);
  initAssetEvents();
  $('assetSearch').addEventListener('input', debouncedSearch);
  ['assetExt','assetSize','assetDuration','assetDate'].forEach((id) => $(id).addEventListener('change', renderAssets));
  $('assetSelectAll').addEventListener('click', () => {
    const checks = $('assetRows').querySelectorAll('.asset-check');
    const shouldCheck = Array.from(checks).some((c) => !c.checked);
    checks.forEach((c) => { c.checked = shouldCheck; });
  });
  $('assetCopyPaths').addEventListener('click', async () => {
    const paths = selectedAssets();
    if (!paths.length) { $('assetFooter').textContent = '请先勾选视频'; return; }
    try { await navigator.clipboard.writeText(paths.join('\n')); $('assetFooter').textContent = '已复制 '+paths.length+' 条路径'; }
    catch (e) { $('assetFooter').textContent = '复制失败：' + friendlyError(e); }
  });
  $('assetExportCsv').addEventListener('click', exportAssetCsv);
  restoreHistory();
  $('clearChatBtn').addEventListener('click', clearHistory);
  $('cancelTaskBtn').addEventListener('click', async () => {
    try {
      const r = await api.cancel_task();
      $('testResult').textContent = r.message || '';
    } catch (e) { console.error('cancel failed', e); }
  });
  $('sendBtn').addEventListener('click', send);
  $('chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey || !e.shiftKey)) send();
  });
  $('settingsBtn').addEventListener('click', openSettings);
  $('closeSettings').addEventListener('click', () => $('settingsOverlay').classList.add('hidden'));
  $('settingsOverlay').addEventListener('click', (e) => {
    if (e.target === $('settingsOverlay')) $('settingsOverlay').classList.add('hidden');
  });
  $('saveBtn').addEventListener('click', saveSettings);
  $('testBtn').addEventListener('click', testLLM);
  $('checkUpdateBtn').addEventListener('click', checkUpdate);
  $('refreshLoginBtn').addEventListener('click', refreshLoginStatus);
  $('exportConfigBtn').addEventListener('click', exportConfig);
  $('importConfigBtn').addEventListener('click', importConfig);
  // 统一登录入口：小红书网址自动走扫码识别，其他网站走通用登录
  $('siteLoginBtn').addEventListener('click', async () => {
    const url = $('siteUrlInput').value.trim();
    if (!/^https?:\/\//i.test(url)) {
      $('testResult').textContent = '请先填写网站首页地址（以 http:// 或 https:// 开头）';
      $('testResult').className = 'test-result fail'; return;
    }
    const isXhs = /xiaohongshu\.com|xhslink\.com/i.test(url);
    $('testResult').textContent = isXhs
      ? '正在打开小红书窗口，扫码后自动记住（最长5分钟）...'
      : '正在打开登录窗口，登录完成后回来点「完成登录」...';
    $('testResult').className = 'test-result';
    try {
      const r = isXhs ? await api.xhs_login() : await api.site_login(url);
      if (r && r.ok === false) { $('testResult').textContent = r.message; $('testResult').className = 'test-result fail'; }
    } catch (e) { $('testResult').textContent = friendlyError(e); $('testResult').className = 'test-result fail'; }
  });
  $('confirmLoginBtn').addEventListener('click', async () => {
    try {
      const r = await api.confirm_login();
      $('testResult').textContent = r.message || (r.success ? '正在保存...' : '没有进行中的登录');
      $('testResult').className = 'test-result ' + (r.success ? 'ok' : 'fail');
    } catch (e) { $('testResult').textContent = friendlyError(e); $('testResult').className = 'test-result fail'; }
  });
  $('clearLogs').addEventListener('click', async () => {
    try {
      const r = await api.clear_logs();   // 先清后端，再清 DOM；gen 变化会丢弃在途旧日志
      if (r && r.ok) { logGen = r.log_gen; lastLogCount = 0; $('logList').innerHTML = '<div class="log-empty">暂无日志</div>'; }
    } catch (e) { /* 清日志失败不影响主流程 */ }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('settingsOverlay').classList.contains('hidden')) {
      $('settingsOverlay').classList.add('hidden');
    }
  });
  poll();
});
