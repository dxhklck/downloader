// pywebview 的 js_api 在 window.pywebview.api，需等就绪
let api = null;
let lastLogCount = 0;
const pendingJobs = new Set();  // 已入队、尚未收到 done 的任务 id
const thinkingByJob = new Map(); // job id -> 该任务的“思考中”占位
let runningJobId = null;   // job_start 标记正在执行的任务
let lastToolCall = null;   // 最近一次工具调用，供结果卡重试
let lastToolEventId = null;
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

// ---- 对话记录：服务端会话流水为唯一来源 ----
// 每条 record 都是一次对话动作，切换/刷新会话时由服务端记录回放。
let currentConversationId = null;

function saveHistory() { /* 兼容旧调用：记录已由服务端维护 */ }

function applyRecords(records) {
  const log = $('chatLog');
  log.innerHTML = '';
  if (!Array.isArray(records) || !records.length) {
    const empty = document.createElement('div');
    empty.className = 'chat-empty';
    empty.textContent = '这个会话还没有消息。在下面输入一句话开始吧。';
    log.appendChild(empty);
    scrollChat();
    return false;
  }
  records.forEach((rec) => renderRecord(rec, log));
  scrollChat();
  return true;
}

function renderRecord(rec, log) {
  if (!rec || !log) return;
  const t = rec.type;
  if (t === 'html') {
    const text = String(rec.html || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    if (text) addMessage(text.slice(0, 1000), 'agent');
  } else if (t === 'user') addMessage(rec.text || '', 'user');
  else if (t === 'msg' || t === 'status') addMessage(rec.text || '', 'agent');
  else if (t === 'tool_start') addToolStatus(rec.name || '', rec.args || {}, rec.tool_call_id);
  else if (t === 'tool_done') {
    const d = rec.payload || {};
    const card = findToolCard(d.tool_call_id, true);
    if (card) {
      const done = d.status === 'ok' || d.status === 'error';
      if (done) card.dataset.toolDone = d.status;
      setToolCardState(card, d.status === 'ok' ? 'ok' : 'fail',
        (d.status === 'ok' ? '完成' : d.status === 'error' ? '失败' : d.status) +
        (d.elapsed_ms ? ' · ' + d.elapsed_ms + 'ms' : ''));
    }
  } else if (t === 'result') {
    markToolStatus(rec.payload || {});
    addResult(rec.payload || {});
  } else if (t === 'plan') planCard(rec.steps || []);
  else if (t === 'ask_choice') showChoice(rec.payload || {});
  else if (t === 'organize_preview') organizeCard(rec.payload || {});
  else if (t === 'search_results') searchResultsCard(rec.payload || {});
}

function clearHistory() {
  $('chatLog').innerHTML = '';
}

const selectedConversations = new Set();

function renderConversationList(items, activeId) {
  const box = $('conversationList');
  if (!box) return;
  const valid = new Set(items.map((c) => c.id));
  Array.from(selectedConversations).forEach((id) => {
    if (!valid.has(id)) selectedConversations.delete(id);
  });
  box.innerHTML = items.length
    ? items.map((c) => '<div class="conversation-item' + (c.id === activeId ? ' active' : '') + '">' +
        '<input type="checkbox" class="conversation-check" data-cid="' + escapeHtml(c.id) + '"' +
        (selectedConversations.has(c.id) ? ' checked' : '') + ' aria-label="选择会话">' +
        '<button class="conversation-open" data-cid="' + escapeHtml(c.id) + '">' +
        '<span class="conversation-title">' + escapeHtml(c.title || '新对话') + '</span>' +
        '<span class="conversation-time">' + (c.updated_at ? new Date(c.updated_at * 1000).toLocaleDateString('zh-CN') : '') + '</span>' +
        '</button></div>').join('')
    : '<div class="conversation-empty">还没有会话</div>';
  box.querySelectorAll('.conversation-check').forEach((el) => {
    el.addEventListener('change', () => {
      if (el.checked) selectedConversations.add(el.dataset.cid);
      else selectedConversations.delete(el.dataset.cid);
    });
  });
  box.querySelectorAll('.conversation-open').forEach((btn) => {
    btn.addEventListener('click', () => switchConversation(btn.dataset.cid));
  });
}

async function refreshConversations(preferredId) {
  const select = $('conversationSelect');
  if (!select) return;
  try {
    const r = await api.list_conversations();
    const items = (r && r.items) || [];
    currentConversationId = preferredId || (r && r.current) || (items[0] && items[0].id) || null;
    if (currentConversationId && !items.some((c) => c.id === currentConversationId)) {
      items.unshift({ id: currentConversationId, title: '新对话', updated_at: 0 });
    }
    select.innerHTML = items.length
      ? items.map((c) => '<option value="' + escapeHtml(c.id) + '">' +
          escapeHtml(c.title || '新对话') + '</option>').join('')
      : '<option value="">新对话</option>';
    if (currentConversationId) select.value = currentConversationId;
    renderConversationList(items, currentConversationId);
  } catch (e) { console.error('会话列表加载失败', e); }
}

async function newConversation() {
  try {
    const r = await api.new_conversation();
    currentConversationId = r.id;
    clearHistory();
    await refreshConversations(r.id);
  } catch (e) { addResult({ success: false, message: friendlyError(e), detail: String(e) }); }
}

async function switchConversation(cid) {
  if (!cid) return;
  try {
    const r = await api.switch_conversation(cid);
    currentConversationId = r.id;
    clearHistory();
    applyRecords(r && r.records);
    await refreshConversations(r.id);
  } catch (e) { addResult({ success: false, message: friendlyError(e), detail: String(e) }); }
}

async function deleteConversation() {
  const ids = Array.from(selectedConversations);
  if (!ids.length) {
    const current = $('conversationSelect').value;
    if (current) ids.push(current);
  }
  if (!ids.length) return;
  const ok = await askConfirm('删除会话',
    `删除 ${ids.length} 个会话？对话记录和模型上下文会一并清除，无法恢复。`,
    `删除 ${ids.length} 个会话`);
  if (!ok) return;
  try {
    for (const cid of ids) {
      const r = await api.delete_conversation(cid);
      if (!r || !r.ok) throw new Error((r && r.message) || '删除失败');
    }
    selectedConversations.clear();
    const keep = ids.includes(currentConversationId) ? null : currentConversationId;
    await refreshConversations(keep);
    if (keep) {
      const r2 = await api.switch_conversation(keep);
      clearHistory();
      applyRecords(r2 && r2.records);
      await refreshConversations(keep);
    } else {
      await newConversation();
    }
  } catch (e) { addResult({ success: false, message: friendlyError(e), detail: String(e) }); }
}

function addMessage(text, who) {
  const div = document.createElement('div');
  div.className = 'msg msg-' + who;
  const avatar = who === 'user' ? '我' : '助';
  div.innerHTML =
    '<div class="msg-avatar">' + avatar + '</div>' +
    '<div class="msg-body"><div class="msg-text">' + escapeHtml(text) + '</div></div>';
  const empty = $('chatLog').querySelector('.chat-empty');
  if (empty) empty.remove();
  $('chatLog').appendChild(div);
  scrollChat();
  saveHistory();
  return div;
}

function organizeCard(data) {
  const box = $('choiceCard');
  if (!box || !data || !data.preview_id || !Array.isArray(data.changes)) return;
  const rows = data.changes.map((c) =>
    '<div class="choice-row">' + escapeHtml(c.src) + '<br>→ ' + escapeHtml(c.dest) + '</div>').join('');
  box.innerHTML = '<div class="choice-question">批量整理预览（' + (data.total || data.changes.length) +
    ' 项，下面显示前 ' + data.changes.length + ' 项）</div><div class="choice-changes">' + rows +
    '</div><div class="choice-options">' +
    '<button class="choice-option" data-organize-confirm="1" data-preview-id="' + escapeHtml(data.preview_id) + '">确认执行</button>' +
    '<button class="choice-option" data-organize-confirm="0">取消</button></div>' +
    '<div class="choice-note">仅执行预览中的文件；同名目标会被跳过。</div>';
  box.classList.remove('hidden');
  box.querySelectorAll('[data-organize-confirm]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const confirmed = btn.dataset.organizeConfirm === '1';
      const previewId = btn.dataset.previewId;
      btn.disabled = true;
      try {
        const r = await api.confirm_organize(previewId, confirmed);
        if (r && r.ok) {
          box.classList.add('hidden');
          addMessage('已确认批量整理（任务 #' + r.id + '）', 'user');
        } else if (!confirmed) {
          box.classList.add('hidden');
          addMessage('已取消批量整理', 'user');
        } else {
          btn.disabled = false;
          addResult({ success: false, message: r && r.message || '确认失败' });
        }
      } catch (e) {
        btn.disabled = false;
        addResult({ success: false, message: friendlyError(e), detail: String(e) });
      }
    });
  });
  scrollChat();
}

function planCard(steps) {
  const div = document.createElement('div');
  div.className = 'msg msg-agent';
  const items = steps.map((s, i) =>
    '<li class="plan-step"><span class="plan-index">' + (i + 1) + '</span>' +
    escapeHtml(s) + '</li>').join('');
  div.innerHTML = '<div class="msg-avatar">助</div><div class="msg-body">' +
    '<div class="plan-card"><div class="plan-head">执行计划</div><ol class="plan-list">' +
    items + '</ol></div></div>';
  const empty = $('chatLog').querySelector('.chat-empty');
  if (empty) empty.remove();
  $('chatLog').appendChild(div);
  scrollChat();
  saveHistory();
}

function showChoice(data) {
  const box = $('choiceCard');
  if (!box || !data || !data.id || !Array.isArray(data.options)) return;
  box.innerHTML = '<div class="choice-question">' + escapeHtml(data.question || '请选择') + '</div>' +
    '<div class="choice-options">' + data.options.map((opt, i) =>
      '<button class="choice-option" data-choice-id="' + escapeHtml(data.id) +
      '" data-choice-index="' + i + '">' + escapeHtml(opt) + '</button>').join('') +
    '</div><div class="choice-note">请选择一个选项后继续。</div>';
  box.classList.remove('hidden');
  box.querySelectorAll('[data-choice-id]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const answerId = btn.dataset.choiceId;
      const value = data.options[Number(btn.dataset.choiceIndex)];
      btn.disabled = true;
      try {
        const r = await api.answer_choice(answerId, value);
        if (r && r.ok) {
          box.classList.add('hidden');
          setToolCardState(findToolCard(lastToolEventId), 'ok', '已选择');
          addMessage('已选择：' + value, 'user');
        } else {
          btn.disabled = false;
          addResult({ success: false, message: r && r.message || '选择提交失败' });
        }
      } catch (e) {
        btn.disabled = false;
        addResult({ success: false, message: friendlyError(e), detail: String(e) });
      }
    });
  });
  scrollChat();
}

function searchResultsCard(data) {
  const div = document.createElement('div');
  div.className = 'msg msg-agent';
  const items = Array.isArray(data.items) ? data.items : [];
  const meta = data.out_dir ? ' · 保存到 ' + data.out_dir : '';
  const rows = items.map((it, i) =>
    '<div class="search-result-row">' +
    '<input type="checkbox" class="search-result-check" data-search-index="' + i + '" aria-label="选择">' +
    '<div class="search-result-title">' + (i + 1) + '. ' + escapeHtml(it.title || '无标题') + '</div>' +
    '<div class="search-result-meta">' + escapeHtml(it.uploader || '未知上传者') +
    (it.duration ? ' · ' + fmtDur(it.duration) : '') + '</div>' +
    '<button class="btn btn-ghost search-result-btn" data-search-url="' + escapeHtml(it.url || '') +
    '" data-search-index="' + i + '">下载</button></div>').join('');
  div.innerHTML = '<div class="msg-avatar">助</div><div class="msg-body">' +
    '<div class="search-card"><div class="search-card-head">搜索结果：' +
    escapeHtml(data.keyword || '') + meta + '（勾选后可批量下载）</div>' +
    '<div class="search-results">' + rows + '</div>' +
    '<div class="search-actions"><button class="btn btn-primary" data-search-all>下载全部</button>' +
    '<button class="btn btn-ghost" data-search-selected>下载所选</button></div>' +
    '</div></div>';
  div.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-search-url]');
    if (btn && !btn.disabled) {
      const item = items[Number(btn.dataset.searchIndex)];
      if (!item || !item.url) return;
      btn.disabled = true;
      btn.textContent = '已加入队列';
      downloadChosen([item], data.out_dir, data.quality, btn);
      return;
    }
    const all = e.target.closest('[data-search-all]');
    const selected = e.target.closest('[data-search-selected]');
    if (!all && !selected) return;
    const picked = all ? items : Array.from(div.querySelectorAll('.search-result-check:checked'))
      .map((n) => items[Number(n.dataset.searchIndex)]).filter(Boolean);
    if (!picked.length) {
      addResult({ success: false, message: '请先勾选要下载的视频' });
      return;
    }
    (all || selected).disabled = true;
    downloadChosen(picked, data.out_dir, data.quality, all || selected);
  });
  const empty = $('chatLog').querySelector('.chat-empty');
  if (empty) empty.remove();
  $('chatLog').appendChild(div);
  scrollChat();
  saveHistory();
}

async function downloadChosen(picked, outDir, quality, btn) {
  const items = picked.filter((it) => it && it.url).map((it) => ({
    url: it.url, title: it.title || '未命名'
  }));
  if (!items.length) {
    addResult({ success: false, message: '所选视频没有有效链接' });
    if (btn) btn.disabled = false;
    return;
  }
  try {
    const r = await api.download_batch(items, outDir || '', quality || 'best');
    if (r && r.ok) {
      (r.ids || []).forEach((id) => {
        const thinking = showThinking();
        pendingJobs.add(id);
        thinkingByJob.set(id, thinking);
      });
      refreshBusy();
      addMessage('下载 ' + items.length + ' 个视频（任务 #' + (r.ids || []).join(', ') + '）', 'user');
    } else {
      addResult({ success: false, message: (r && r.message) || '下载任务创建失败' });
      if (btn) btn.disabled = false;
    }
  } catch (e) {
    addResult({ success: false, message: friendlyError(e), detail: String(e) });
    if (btn) btn.disabled = false;
  }
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

function addToolStatus(name, args, toolCallId) {
  if (toolCallId && findToolCard(toolCallId, true)) return;
  const div = document.createElement('div');
  div.innerHTML = toolStatusCard(name, args, { cls: 'pending', text: '调用中' });
  const card = div.firstChild;
  $('chatLog').appendChild(card);
  card.dataset.toolName = name;
  card.dataset.toolArgs = JSON.stringify(args || {});
  const callId = toolCallId || lastToolEventId || '';
  if (callId) card.dataset.toolCallId = callId;
  lastToolCall = { name, args: args || {} };
  scrollChat();
  saveHistory();
}

function findToolCard(id, includeDone = false) {
  if (!id) return null;
  const cards = Array.from($('chatLog').querySelectorAll('.tool-card[data-tool-name]'));
  return cards.find((n) => n.dataset.toolCallId === id && (includeDone || !n.dataset.toolDone)) || null;
}

function setToolCardState(card, cls, text) {
  if (!card) return;
  const head = card.querySelector('.tool-card-head');
  let state = head && head.querySelector('.tool-card-state');
  if (!state && head) {
    state = document.createElement('span');
    head.appendChild(state);
  }
  if (!state) return;
  state.className = 'tool-card-state ' + cls;
  state.textContent = text;
}

function markToolStatus(result) {
  const cards = $('chatLog').querySelectorAll('.tool-card[data-tool-name]');
  let card = null;
  if (result && result.tool_call_id) {
    card = Array.from(cards).find((n) => n.dataset.toolCallId === result.tool_call_id && !n.dataset.toolDone);
  }
  if (!card) card = Array.from(cards).reverse().find((n) => !n.dataset.toolDone);
  if (!card) return;
  let args = {};
  try { args = JSON.parse(card.dataset.toolArgs || '{}'); } catch (e) {}
  const ok = !!(result && result.success);
  card.dataset.toolDone = ok ? 'ok' : 'fail';
  setToolCardState(card, ok ? 'ok' : 'fail', ok ? '完成' : '失败');
  if (!ok) {
    const argsBox = card.querySelector('.tool-card-args');
    const detail = result && ((result.detail ? result.detail + ' ' : '') + (result.message || ''));
    if (detail.trim()) {
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
    : ('✗ ' + (result.message ? String(result.message) : friendlyError(result.detail)));
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
      const thinking = showThinking();
      pendingJobs.add(r.id);
      thinkingByJob.set(r.id, thinking);
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
  const empty = $('chatLog').querySelector('.chat-empty');
  if (empty) empty.remove();
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
    const activeConversation = currentConversationId;
    events.forEach((ev) => {
      if (ev.data && ev.data.conversation_id && ev.data.conversation_id !== activeConversation) return;
      switch (ev.event) {
        case 'record':
          // 记录用于会话回放；实时展示仍由 msg/tool/result 等事件负责。
          break;
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
          lastToolEventId = ev.data && ev.data.tool_call_id ? ev.data.tool_call_id : null;
          addToolStatus(ev.data.name, ev.data.args, lastToolEventId);
          break;
        case 'tool_done':
          if (ev.data && ev.data.tool_call_id) {
            const doneCard = findToolCard(ev.data.tool_call_id, true);
            if (doneCard) {
              const done = ev.data.status === 'ok' || ev.data.status === 'error';
              if (done) doneCard.dataset.toolDone = ev.data.status;
              setToolCardState(doneCard, ev.data.status === 'ok' ? 'ok' : 'fail',
                (ev.data.status === 'ok' ? '完成' : ev.data.status === 'error' ? '失败' : ev.data.status) +
                (ev.data.elapsed_ms ? ' · ' + ev.data.elapsed_ms + 'ms' : ''));
            }
          }
          break;
        case 'plan':
          if (ev.data && Array.isArray(ev.data.steps)) planCard(ev.data.steps);
          break;
        case 'organize_preview':
          organizeCard(ev.data);
          break;
        case 'search_results':
          searchResultsCard(ev.data);
          break;
        case 'ask_choice': {
          const card = findToolCard(lastToolEventId);
          if (card) setToolCardState(card, 'waiting', '等待选择');
          showChoice(ev.data);
          break;
        }
        case 'status':
          if (runningJobId) clearThinking(runningJobId);
          addMessage(ev.data, 'agent');
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
        case 'site_login_done': {
          const box = $('siteLoginResult');
          const text = ev.data.message || (ev.data.success ? '登录成功，已记住' : '登录失败');
          if (box) { box.textContent = text; box.className = 'test-result ' + (ev.data.success ? 'ok' : 'fail'); }
          $('siteLoginBtn').disabled = false;
          $('confirmLoginBtn').disabled = false;
          refreshLoginStatus();
          break;
        }
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
  setSettingsTab('model');
  $('settingsOverlay').classList.remove('hidden');
  refreshLoginStatus();
  loadVersionInfo();
}

function setSettingsTab(name) {
  document.querySelectorAll('[data-settings-tab]').forEach((btn) => {
    const active = btn.dataset.settingsTab === name;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('[data-settings-panel]').forEach((panel) => {
    panel.classList.toggle('hidden', panel.dataset.settingsPanel !== name);
  });
  $('testBtn').classList.toggle('hidden', name !== 'model');
  $('saveBtn').classList.toggle('hidden', name !== 'model');
}

// 底部按钮的反馈写入当前标签页：模型页写 testResult，备份页写 backupResult，
// 其它页写各自的结果区；避免点了按钮却看不到任何反馈。
function currentSettingsFeedback() {
  const active = document.querySelector('[data-settings-tab].active');
  const tab = active ? active.dataset.settingsTab : 'model';
  if (tab === 'backup') return $('backupResult');
  return $('testResult');
}

function showFeedback(r) {
  const box = currentSettingsFeedback();
  if (!box) return;
  const msg = r && r.ok ? r.message : friendlyError(r && (r.detail || r.message));
  box.textContent = msg;
  box.className = 'test-result ' + (r && r.ok ? 'ok' : 'fail');
  box.title = r && !r.ok && r.detail ? r.detail : '';
}

async function loadVersionInfo() {
  const badge = $('versionBadge');
  const list = $('versionInfo');
  const author = $('versionAuthor');
  if (!badge || !list) return;
  badge.textContent = '读取中...';
  list.innerHTML = '<div><dt>版本</dt><dd>读取中...</dd></div>' +
    '<div><dt>运行环境</dt><dd>读取中...</dd></div>' +
    '<div><dt>数据目录</dt><dd>读取中...</dd></div>';
  try {
    const r = await api.get_version_info();
    if (!r || !r.ok) throw new Error((r && r.message) || '读取失败');
    badge.textContent = 'v' + escapeHtml(r.version);
    list.innerHTML =
      '<div><dt>版本</dt><dd>' + escapeHtml(r.version) + '</dd></div>' +
      '<div><dt>运行环境</dt><dd>' + escapeHtml(r.platform) + ' · Python ' + escapeHtml(r.python) + '</dd></div>' +
      '<div><dt>数据目录</dt><dd class="version-path" title="' + escapeHtml(r.root) + '">' + escapeHtml(r.root) + '</dd></div>';
    if (author) {
      const name = r.author || author.textContent;
      author.textContent = name;
      if (r.author_url) {
        author.href = r.author_url;
        author.title = '打开作者主页：' + name;
        author.onclick = (ev) => { ev.preventDefault(); api.open_external(r.author_url); };
      } else {
        author.removeAttribute('href');
        author.removeAttribute('title');
        author.onclick = null;
      }
    }
  } catch (e) {
    badge.textContent = '未知';
    list.innerHTML = '<div><dt>版本</dt><dd>读取失败</dd></div>' +
      '<div><dt>运行环境</dt><dd>' + escapeHtml(friendlyError(e)) + '</dd></div>';
    if (author) {
      author.textContent = '信息科技练老师';
      author.href = 'https://space.bilibili.com/477558144';
      author.onclick = (ev) => { ev.preventDefault(); api.open_external('https://space.bilibili.com/477558144'); };
    }
  }
}

async function refreshLoginStatus() {
  const box = $('loginStatus');
  if (!box) return;
  box.textContent = '检测中...';
  try {
    const r = await api.get_login_status();
    const label = { ok: '已登录', none: '未登录', unknown: '未知' };
    box.innerHTML = Object.entries(r).map(([k, v]) =>
      '<div class="login-item"><b>' + escapeHtml(v.label || k) + '</b> ' +
      escapeHtml(label[v.status] || v.status) +
      (v.domains && v.domains.length ? ' (' + escapeHtml(v.domains.slice(0, 3).join(', ')) + ')' : '') +
      '</div>').join('');
  } catch (e) { box.textContent = '检测失败：' + friendlyError(e); }
}

function showBackupResult(r) {
  // 后端消息直接展示，避免 “配置格式不是有效 JSON” 被通用文案吞掉。
  const box = $('backupResult');
  if (!box) return;
  box.textContent = r.ok ? r.message : (r.detail || r.message || '导入失败');
  box.className = 'test-result ' + (r.ok ? 'ok' : 'fail');
  box.title = !r.ok && r.detail ? r.detail : '';
}

async function exportConfig() {
  try {
    const r = await api.export_config();
    const text = JSON.stringify(r.config, null, 2);
    const input = $('importConfigInput');
    if (input) input.value = text;  // 同时显示在页面上，避免找不到下载文件
    const blob = new Blob([text], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'downloader-config.json';
    a.click();
    URL.revokeObjectURL(a.href);
    showBackupResult({ ok: r.ok, message: '已导出配置（不含 API Key / Cookie）：已下载 downloader-config.json，并填入下方文本框，可直接复制或另存' });
  } catch (e) { showBackupResult({ ok: false, message: friendlyError(e), detail: String(e) }); }
}

async function importConfig() {
  const input = $('importConfigInput');
  const text = input ? input.value.trim() : '';
  if (!text) {
    showBackupResult({ ok: false, message: '请先粘贴要导入的配置 JSON' });
    return;
  }
  try {
    const r = await api.import_config(text);
    showBackupResult(r);
    if (r && r.ok) input.value = '';
  } catch (e) { showBackupResult({ ok: false, message: friendlyError(e), detail: String(e) }); }
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
    box.textContent = '已下载到：' + d.path + '。点击下面按钮安装。';
    const install = document.createElement('button');
    install.className = 'btn btn-ghost';
    install.textContent = '运行安装包';
    install.onclick = async () => {
      install.disabled = true;
      try {
        const ir = await api.install_update(d.path);
        if (!ir || !ir.ok) throw new Error((ir && ir.message) || '启动失败');
        box.textContent = ir.message;
      } catch (e) {
        install.disabled = false;
        box.className = 'update-result fail';
        box.textContent = '无法启动安装包：' + friendlyError(e);
      }
    };
    box.appendChild(install);
  } catch (e) {
    box.className = 'update-result fail';
    box.textContent = '下载失败：' + friendlyError(e);
  }
}

function showTest(r) {
  showFeedback(r);
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
let assetPage = 1;
const ASSET_PAGE_SIZE = 100;

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
  all.onclick = () => { currentCat='__all__'; assetPage=1; renderCats(stats); renderAssets(); };
  tree.appendChild(all);
  Object.entries(stats.cats || {}).forEach(([cat, v]) => {
    const d = document.createElement('div');
    d.className = 'cat-item' + (currentCat===cat?' active':'');
    d.innerHTML = '<span>'+escapeHtml(cat)+'</span><span class="cnt">'+v.count+'</span>';
    d.onclick = () => { currentCat=cat; assetPage=1; renderCats(stats); renderAssets(); };
    tree.appendChild(d);
  });
  $('assetStats').innerHTML = '共 <b>'+stats.total+'</b> 个视频，<b>'+stats.total_gb+' GB</b>' +
    (window.lastAssetScanMessage ? '<span class="asset-scan-note"> · ' + escapeHtml(window.lastAssetScanMessage) + '</span>' : '');
}

function renderAssets() {
  const q = $('assetSearch').value.trim().toLowerCase();
  const sort = $('assetSort').value;
  const size = $('assetSize').value;
  const dur = $('assetDuration').value;
  const days = Number($('assetDate').value || 0);
  const cutoff = days ? Date.now() / 1000 - days * 86400 : 0;
  let rows = assetsData.filter(x =>
    (currentCat==='__all__' || x.cat===currentCat) &&
    (!q || x.name.toLowerCase().includes(q) || x.rel.toLowerCase().includes(q)) &&
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
  const pages = Math.max(1, Math.ceil(rows.length / ASSET_PAGE_SIZE));
  if (assetPage > pages) assetPage = pages;
  const start = (assetPage - 1) * ASSET_PAGE_SIZE;
  const shown = rows.slice(start, start + ASSET_PAGE_SIZE);
  const tbody = $('assetRows');
  tbody.innerHTML = shown.map(x =>
    '<tr data-path="'+escapeHtml(x.path)+'">' +
    '<td><input type="checkbox" class="asset-check"></td>' +
    '<td class="fname" title="'+escapeHtml(x.rel)+'">'+escapeHtml(x.name)+'</td>' +
    '<td>'+escapeHtml(x.cat)+'</td>' +
    '<td class="num">'+fmtSize(x.size)+'</td>' +
    '<td class="num">'+fmtDur(x.dur)+'</td>' +
    '<td class="num">'+fmtDate(x.mtime)+'</td></tr>'
  ).join('');
  $('assetFooter').textContent = '筛选 '+rows.length+' / '+assetsData.length+' 个 · 本页 '+shown.length+
    ' 条 · 双击打开 · 右键定位文件 · 删除可恢复';
  const pager = $('assetPager');
  const nums = pageNumbers(assetPage, pages);
  pager.innerHTML = '<button class="pager-btn" data-page="prev" ' + (assetPage===1?'disabled':'') + '>上一页</button>' +
    nums.map((n) => n === '…'
      ? '<span class="pager-ellipsis">…</span>'
      : '<button class="pager-btn' + (n===assetPage?' active':'') + '" data-page="' + n + '">' + n + '</button>').join('') +
    '<button class="pager-btn" data-page="next" ' + (assetPage===pages?'disabled':'') + '>下一页</button>';
  pager.querySelectorAll('[data-page]').forEach((btn) => {
    btn.onclick = () => {
      const v = btn.dataset.page;
      assetPage = v === 'prev' ? Math.max(1, assetPage - 1)
        : v === 'next' ? Math.min(pages, assetPage + 1) : Number(v);
      renderAssets();
    };
  });
}

function pageNumbers(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  if (current <= 4) return [1, 2, 3, 4, 5, '…', total];
  if (current >= total - 3) return [1, '…', total - 4, total - 3, total - 2, total - 1, total];
  return [1, '…', current - 1, current, current + 1, '…', total];
}

function selectedAssets() {
  return Array.from($('assetRows').querySelectorAll('tr[data-path]'))
    .filter((tr) => tr.querySelector('.asset-check').checked)
    .map((tr) => tr.dataset.path);
}

function askConfirm(title, message, okText) {
  return new Promise((resolve) => {
    const overlay = $('confirmOverlay');
    $('confirmTitle').textContent = title;
    $('confirmMessage').textContent = message;
    $('confirmOk').textContent = okText || '确认';
    overlay.classList.remove('hidden');
    const finish = (value) => {
      overlay.classList.add('hidden');
      $('confirmOk').onclick = null;
      $('confirmCancel').onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') finish(false);
      else if (e.key === 'Enter') finish(true);
    };
    $('confirmOk').onclick = () => finish(true);
    $('confirmCancel').onclick = () => finish(false);
    document.addEventListener('keydown', onKey);
  });
}

async function deleteAssets(paths) {
  if (!paths.length) { $('assetFooter').textContent = '请先勾选视频'; return; }
  const label = paths.length === 1 ? '这 1 个视频' : '这 '+paths.length+' 个视频';
  const ok = await askConfirm('移入回收站',
    '确定把'+label+'移入回收站吗？之后可以恢复。', '确认删除');
  if (!ok) return;
  $('assetFooter').textContent = '正在移入回收站...';
  try {
    const r = paths.length === 1
      ? await api.asset_trash(paths[0])
      : await api.asset_trash_batch(paths);
    if (!r || !r.success) {
      $('assetFooter').textContent = '删除失败：' + ((r && r.message) || '未知错误');
      return;
    }
    const failed = new Set(((r.data && r.data.failed) || []).map((f) => f.path));
    const removed = new Set(paths.filter((p) => !failed.has(p)));
    assetsData = assetsData.filter((x) => !removed.has(x.path));
    loadAssets(false).then(() => {
      $('assetFooter').textContent = r.message + ' · 可在回收站恢复';
    });
  } catch (e) {
    $('assetFooter').textContent = '删除失败：' + friendlyError(e);
  }
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
  searchTimer = setTimeout(() => { assetPage = 1; renderAssets(); }, 200);
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
  ['assetSize','assetDuration','assetDate'].forEach((id) => $(id).addEventListener('change', () => { assetPage = 1; renderAssets(); }));
  $('assetSort').addEventListener('change', () => { assetPage = 1; renderAssets(); });
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
  $('assetDeleteSelected').addEventListener('click', () => deleteAssets(selectedAssets()));
  (async () => {
    await refreshConversations();
    if (currentConversationId) await switchConversation(currentConversationId);
  })();
  $('clearChatBtn').addEventListener('click', clearHistory);
  $('newConversationBtn').addEventListener('click', newConversation);
  $('deleteConversationBtn').addEventListener('click', deleteConversation);
  $('conversationSelect').addEventListener('change', (e) => switchConversation(e.target.value));
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
  document.querySelectorAll('[data-settings-tab]').forEach((btn) => {
    btn.addEventListener('click', () => setSettingsTab(btn.dataset.settingsTab));
  });
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
    const input = $('siteUrlInput');
    const box = $('siteLoginResult');
    let url = input.value.trim();
    if (!url) {
      box.textContent = '请先填写网站首页地址';
      box.className = 'test-result fail'; return;
    }
    if (!/^https?:\/\//i.test(url)) {
      url = 'https://' + url;
      input.value = url;
    }
    const isXhs = /xiaohongshu\.com|xhslink\.com/i.test(url);
    $('siteLoginBtn').disabled = true;
    $('confirmLoginBtn').disabled = false;
    box.textContent = isXhs
      ? '正在打开小红书窗口，扫码后自动记住（最长5分钟）...'
      : '正在打开登录窗口，登录完成后回来点「完成登录」...';
    box.className = 'test-result';
    try {
      const r = isXhs ? await api.xhs_login() : await api.site_login(url);
      if (r && r.ok === false) {
        box.textContent = r.message; box.className = 'test-result fail';
        $('siteLoginBtn').disabled = false;
      }
    } catch (e) {
      box.textContent = friendlyError(e); box.className = 'test-result fail';
      $('siteLoginBtn').disabled = false;
    }
  });
  $('confirmLoginBtn').addEventListener('click', async () => {
    const box = $('siteLoginResult');
    try {
      const r = await api.confirm_login();
      box.textContent = r.message || (r.success ? '正在保存...' : '没有进行中的登录');
      box.className = 'test-result ' + (r.success ? 'ok' : 'fail');
      $('confirmLoginBtn').disabled = !!r.success;
    } catch (e) { box.textContent = friendlyError(e); box.className = 'test-result fail'; }
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
