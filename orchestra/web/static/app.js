/* Orchestra — interface logic.
 *
 * Talks to the same endpoints the CLI's pipeline exposes; no business
 * logic lives here. Everything below is presentation, keyboard handling,
 * and being honest about what the backend is doing.
 */
'use strict';

var $ = function (id) { return document.getElementById(id); };

var el = {
  thread: $('thread'), messages: $('messages'), nodes: $('nodes'),
  constellation: $('constellation'), sessionList: $('session-list'),
  sessionCount: $('session-count'), health: $('health'), subtitle: $('chat-subtitle'),
  composer: $('composer'), input: $('input'), send: $('send'), jump: $('jump'),
  toast: $('toast'), paletteOverlay: $('palette-overlay'), paletteInput: $('palette-input'),
  paletteList: $('palette-list'), helpOverlay: $('help-overlay')
};

var state = {
  roster: [],
  sessions: [],
  sessionId: null,
  streaming: false,
  controller: null,
  lastUserText: '',
  lastReply: '',
  pinned: true,
  paletteIndex: 0,
  paletteItems: []
};

var MAC = /Mac|iPhone|iPad/.test(navigator.platform || '');

/* ── Small utilities ─────────────────────────────────────────────── */
function toast(msg) {
  el.toast.textContent = msg;
  el.toast.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(function () { el.toast.classList.remove('show'); }, 1900);
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise(function (resolve, reject) {   // http:// on a LAN address
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy') ? resolve() : reject(); }
    catch (e) { reject(e); }
    finally { ta.remove(); }
  });
}

function icon(paths, fill) {
  return '<svg viewBox="0 0 24 24" fill="' + (fill || 'none') +
    '" stroke="currentColor" stroke-width="2" aria-hidden="true">' + paths + '</svg>';
}
var ICONS = {
  copy: icon('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>'),
  check: icon('<path d="m5 12.5 4.5 4.5L19 7"/>'),
  regen: icon('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>'),
  chevron: icon('<path d="m9 6 6 6-6 6"/>'),
  trash: icon('<path d="M4 7h16M9 7V5h6v2M6.5 7l1 13h9l1-13"/>'),
  chat: icon('<path d="M20 15a3 3 0 0 1-3 3H8l-4 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3Z"/>'),
  bolt: icon('<path d="M13 2 4.5 13.5H11l-1 8.5L19 10.5h-6.5Z"/>')
};

function relativeGroup(ts) {
  var d = new Date(ts * 1000), now = new Date();
  var startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  var t = d.getTime();
  if (t >= startOfToday) return 'Today';
  if (t >= startOfToday - 864e5) return 'Yesterday';
  if (t >= startOfToday - 6 * 864e5) return 'Previous 7 days';
  return 'Older';
}

/* ── Theme ───────────────────────────────────────────────────────── */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('orchestra:theme', theme); } catch (e) { /* ignore */ }
  var dark = theme === 'dark';
  document.querySelector('[data-icon="moon"]').hidden = !dark;
  document.querySelector('[data-icon="sun"]').hidden = dark;
  drawConstellation();
}
function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
}

/* ── Sidebar ─────────────────────────────────────────────────────── */
function toggleSidebar() {
  var narrow = window.matchMedia('(max-width: 780px)').matches;
  document.body.classList.toggle(narrow ? 'sidebars-open' : 'sidebars-hidden');
}

/* ── Health ──────────────────────────────────────────────────────── */
function loadHealth() {
  fetch('/api/health').then(function (r) { return r.json(); }).then(function (h) {
    el.health.classList.add('up');
    el.health.querySelector('.txt').textContent = h.backend + ' · ' + h.main_model;
    el.health.title = h.specialists + ' specialists · concurrency ' + h.concurrency +
      ' · fast tier ' + h.fast_model;
  }).catch(function () {
    el.health.classList.add('down');
    el.health.querySelector('.txt').textContent = 'backend unreachable';
  });
}

/* ── Roster & constellation ──────────────────────────────────────── */
function loadRoster() {
  return fetch('/api/roster').then(function (r) { return r.json(); }).then(function (list) {
    state.roster = list;
    el.nodes.innerHTML = list.map(function (s) {
      return '<div class="node" data-name="' + MD.escapeHtml(s.name) + '" title="' +
        MD.escapeHtml(s.categories.join(', ')) + '"><span class="dot"></span>' +
        '<span class="label">' + MD.escapeHtml(s.name) + '</span></div>';
    }).join('');
  }).catch(function () { /* roster is decoration; chat still works */ });
}

var litOrder = [];

function clearConstellation() {
  litOrder = [];
  el.constellation.innerHTML = '';
  el.nodes.querySelectorAll('.node.active').forEach(function (n) { n.classList.remove('active'); });
}

function litNode(name) {
  if (!name || litOrder.indexOf(name) !== -1) return;
  var node = el.nodes.querySelector('.node[data-name="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
  if (!node) return;
  node.classList.add('active');
  litOrder.push(name);
  drawConstellation();
}

/* Lines are redrawn from live geometry rather than stored, so a theme
   switch, a resize, or a collapsed sidebar can't leave them stranded. */
function drawConstellation() {
  if (!el.constellation) return;
  el.constellation.innerHTML = '';
  var root = el.constellation.getBoundingClientRect();
  var prev = null;
  litOrder.forEach(function (name) {
    var node = el.nodes.querySelector('.node[data-name="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
    if (!node) return;
    var b = node.getBoundingClientRect();
    var point = { x: b.left - root.left + b.width / 2, y: b.top - root.top + b.height / 2 };
    if (prev) {
      var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', prev.x); line.setAttribute('y1', prev.y);
      line.setAttribute('x2', point.x); line.setAttribute('y2', point.y);
      el.constellation.appendChild(line);
      requestAnimationFrame(function () { line.classList.add('lit'); });
    }
    prev = point;
  });
}
window.addEventListener('resize', drawConstellation);

/* ── Sessions ────────────────────────────────────────────────────── */
function loadSessions() {
  return fetch('/api/sessions').then(function (r) { return r.json(); }).then(function (list) {
    state.sessions = list;
    renderSessions();
  }).catch(function () {
    el.sessionList.innerHTML = '<div class="sessions-empty">Can\'t reach the server. ' +
      'Is <code>python -m orchestra serve</code> still running?</div>';
  });
}

function renderSessions() {
  if (!state.sessions.length) {
    el.sessionList.innerHTML = '<div class="sessions-empty">No chats yet. Ask something to start one.</div>';
    el.sessionCount.textContent = '';
    return;
  }
  var html = '', group = null;
  state.sessions.forEach(function (s) {
    var g = relativeGroup(s.updated_at);
    if (g !== group) { group = g; html += '<div class="session-group">' + g + '</div>'; }
    html +=
      '<div class="session-item' + (s.id === state.sessionId ? ' active' : '') + '" data-id="' + s.id + '">' +
        '<span class="session-title" title="Double-click to rename">' + MD.escapeHtml(s.title) + '</span>' +
        '<button class="session-act" data-del="' + s.id + '" title="Delete this chat" aria-label="Delete chat">' +
          ICONS.trash + '</button>' +
      '</div>';
  });
  el.sessionList.innerHTML = html;
  el.sessionCount.textContent = state.sessions.length + (state.sessions.length === 1 ? ' chat' : ' chats');
}

function openSession(id) {
  state.sessionId = id;
  clearConstellation();
  el.thread.innerHTML = '';
  renderSessions();
  var session = state.sessions.filter(function (s) { return s.id === id; })[0];
  setTitle(session ? session.title : null);

  return fetch('/api/sessions/' + id).then(function (r) { return r.json(); }).then(function (msgs) {
    msgs.forEach(function (m, i) {
      if (m.role === 'user') { state.lastUserText = m.text; addUser(m.text); }
      else { state.lastReply = m.text; addAssistant(m.text, null, null, i === msgs.length - 1); }
    });
    scrollToEnd(true);
    document.body.classList.remove('sidebars-open');
  });
}

function newChat() {
  if (state.streaming) stopStreaming();
  state.sessionId = null;
  state.lastUserText = '';
  state.lastReply = '';
  clearConstellation();
  renderEmptyState();
  renderSessions();
  setTitle(null);
  document.body.classList.remove('sidebars-open');
  el.input.focus();
}

function setTitle(title) {
  el.subtitle.textContent = title || 'local multi-agent orchestrator';
  document.title = title ? title + ' — Orchestra' : 'Orchestra';
}

/* Rename: double-click the title, Enter commits, Escape reverts. */
el.sessionList.addEventListener('dblclick', function (e) {
  var title = e.target.closest('.session-title');
  if (!title) return;
  var id = title.closest('.session-item').dataset.id;
  var original = title.textContent;
  title.contentEditable = 'true';
  title.focus();
  document.getSelection().selectAllChildren(title);

  function finish(commit) {
    title.contentEditable = 'false';
    var value = title.textContent.trim();
    title.removeEventListener('keydown', onKey);
    title.removeEventListener('blur', onBlur);
    if (!commit || !value || value === original) { title.textContent = original; return; }
    fetch('/api/sessions/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: value })
    }).then(function () {
      toast('Renamed');
      if (id === state.sessionId) setTitle(value);
      loadSessions();
    });
  }
  function onKey(ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
    if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
  }
  function onBlur() { finish(true); }
  title.addEventListener('keydown', onKey);
  title.addEventListener('blur', onBlur);
});

/* Delete asks once, in place — no modal, no silent data loss. */
el.sessionList.addEventListener('click', function (e) {
  var del = e.target.closest('[data-del]');
  if (del) {
    e.stopPropagation();
    if (!del.classList.contains('confirm')) {
      resetDeleteButtons();
      del.classList.add('confirm');
      del.innerHTML = 'Delete?';
      setTimeout(function () { if (del.isConnected) resetDeleteButtons(); }, 4000);
      return;
    }
    var id = del.dataset.del;
    fetch('/api/sessions/' + id, { method: 'DELETE' }).then(function () {
      toast('Chat deleted');
      if (id === state.sessionId) newChat();
      loadSessions();
    });
    return;
  }
  var item = e.target.closest('.session-item');
  if (item && item.dataset.id !== state.sessionId) openSession(item.dataset.id);
});

function resetDeleteButtons() {
  el.sessionList.querySelectorAll('.session-act.confirm').forEach(function (b) {
    b.classList.remove('confirm');
    b.innerHTML = ICONS.trash;
  });
}

/* ── Message rendering ───────────────────────────────────────────── */
function renderEmptyState() {
  var starters = [
    ['math + writing', 'Multiply 6 by 7, and write a one-line haiku about the moon.'],
    ['memory', 'Remember that I run Orchestra on Windows with Ollama.'],
    ['files', 'Write a two-line note about today to notes.txt, then read it back.']
  ];
  el.thread.innerHTML =
    '<div class="empty">' +
      '<h2>Nine specialists, one local model.</h2>' +
      '<p>A planner splits your request into typed tasks, the executor routes each one, ' +
      'and the roster on the left lights up in the order work actually happens.</p>' +
      '<div class="starters">' +
        starters.map(function (s) {
          return '<button class="starter" type="button" data-prompt="' + MD.escapeHtml(s[1]) + '">' +
            '<span class="cat">' + s[0] + '</span><span>' + MD.escapeHtml(s[1]) + '</span></button>';
        }).join('') +
      '</div>' +
    '</div>';
}

el.thread.addEventListener('click', function (e) {
  var starter = e.target.closest('.starter');
  if (starter) {
    el.input.value = starter.dataset.prompt;
    autosize();
    el.input.focus();
  }
});

function clearEmptyState() {
  var empty = el.thread.querySelector('.empty');
  if (empty) empty.remove();
}

function addUser(text) {
  clearEmptyState();
  var node = document.createElement('div');
  node.className = 'msg user';
  node.innerHTML = '<div class="bubble" dir="auto"></div>';
  node.querySelector('.bubble').textContent = text;
  el.thread.appendChild(node);
  scrollToEnd();
  return node;
}

function addAssistant(text, tasks, runId, isLast) {
  clearEmptyState();
  var node = document.createElement('div');
  node.className = 'msg assistant';
  node.dataset.reply = text;

  var bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.setAttribute('dir', 'auto');
  bubble.innerHTML = MD.render(text);
  node.appendChild(bubble);

  var actions = document.createElement('div');
  actions.className = 'msg-actions';
  actions.innerHTML =
    '<button class="act" data-copy-reply>' + ICONS.copy + 'Copy</button>' +
    '<button class="act" data-regen>' + ICONS.regen + 'Regenerate</button>';
  node.appendChild(actions);

  if (tasks && tasks.length) {
    var toggle = document.createElement('button');
    toggle.className = 'act';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.innerHTML = ICONS.chevron + 'Audit · ' + tasks.length +
      (tasks.length === 1 ? ' task' : ' tasks');
    actions.appendChild(toggle);

    var panel = document.createElement('div');
    panel.className = 'audit';
    panel.hidden = true;
    panel.innerHTML =
      '<span class="run-id">run ' + MD.escapeHtml(runId || '—') + '</span>' +
      tasks.map(function (t) {
        return '<div class="row"><span class="tag ' + MD.escapeHtml(t.status) + '">' +
          MD.escapeHtml(t.status) + '</span><span class="desc">' +
          MD.escapeHtml(t.category) + ' → ' + MD.escapeHtml(t.specialist || 'unassigned') +
          ' · ' + MD.escapeHtml(t.description) + '</span></div>';
      }).join('');
    node.appendChild(panel);

    toggle.addEventListener('click', function () {
      panel.hidden = !panel.hidden;
      toggle.setAttribute('aria-expanded', String(!panel.hidden));
    });
  }

  el.thread.appendChild(node);
  if (isLast !== false) scrollToEnd();
  return node;
}

function addSystem(text, opts) {
  clearEmptyState();
  var node = document.createElement('div');
  node.className = 'msg system' + (opts && opts.error ? ' error' : '');
  var bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  if (opts && opts.action) {
    var btn = document.createElement('button');
    btn.className = 'inline-link';
    btn.type = 'button';
    btn.textContent = opts.action.label;
    btn.addEventListener('click', opts.action.run);
    bubble.appendChild(btn);
  }
  node.appendChild(bubble);
  el.thread.appendChild(node);
  scrollToEnd();
  return node;
}

/* Copy buttons — one delegated listener for replies and code blocks alike. */
document.addEventListener('click', function (e) {
  var codeBtn = e.target.closest('[data-copy-code]');
  if (codeBtn) {
    var code = codeBtn.closest('.code-block').querySelector('code').textContent;
    copyText(code).then(function () {
      codeBtn.textContent = 'Copied';
      codeBtn.classList.add('done');
      setTimeout(function () { codeBtn.textContent = 'Copy'; codeBtn.classList.remove('done'); }, 1600);
    }).catch(function () { toast('Copy failed — select the text manually'); });
    return;
  }
  var replyBtn = e.target.closest('[data-copy-reply]');
  if (replyBtn) {
    copyText(replyBtn.closest('.msg').dataset.reply).then(function () {
      replyBtn.innerHTML = ICONS.check + 'Copied';
      replyBtn.classList.add('done');
      setTimeout(function () { replyBtn.innerHTML = ICONS.copy + 'Copy'; replyBtn.classList.remove('done'); }, 1600);
    }).catch(function () { toast('Copy failed — select the text manually'); });
    return;
  }
  if (e.target.closest('[data-regen]')) regenerate();
});

/* ── Scroll behaviour ────────────────────────────────────────────── */
function nearBottom() {
  return el.messages.scrollHeight - el.messages.scrollTop - el.messages.clientHeight < 80;
}
function scrollToEnd(force) {
  if (force || state.pinned) {
    el.messages.scrollTop = el.messages.scrollHeight;
    el.jump.classList.remove('show');
  } else {
    el.jump.classList.add('show');
  }
}
el.messages.addEventListener('scroll', function () {
  state.pinned = nearBottom();
  el.jump.classList.toggle('show', !state.pinned && el.thread.children.length > 0);
});
el.jump.addEventListener('click', function () {
  state.pinned = true;
  scrollToEnd(true);
});

/* ── Composer ────────────────────────────────────────────────────── */
function autosize() {
  el.input.style.height = 'auto';
  el.input.style.height = Math.min(el.input.scrollHeight, 200) + 'px';
}
el.input.addEventListener('input', autosize);

el.input.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    el.composer.requestSubmit();
    return;
  }
  // Empty composer + Up = pull your last message back for editing.
  if (e.key === 'ArrowUp' && !el.input.value && state.lastUserText) {
    e.preventDefault();
    el.input.value = state.lastUserText;
    autosize();
    el.input.setSelectionRange(el.input.value.length, el.input.value.length);
  }
});

el.composer.addEventListener('submit', function (e) {
  e.preventDefault();
  if (state.streaming) { stopStreaming(); return; }
  var text = el.input.value.trim();
  if (!text) return;
  el.input.value = '';
  autosize();
  send({ message: text });
});

function setStreaming(on) {
  state.streaming = on;
  el.send.classList.toggle('stop', on);
  el.send.title = on ? 'Stop watching this run (Esc)' : 'Send (Enter)';
  el.send.setAttribute('aria-label', on ? 'Stop watching this run' : 'Send message');
  el.send.querySelector('[data-icon="send"]').hidden = on;
  el.send.querySelector('[data-icon="stop"]').hidden = !on;
}

function stopStreaming() {
  if (state.controller) state.controller.abort();
}

/* ── The run ─────────────────────────────────────────────────────── */
function regenerate() {
  if (state.streaming) return toast('Already running');
  if (!state.sessionId) return toast('Nothing to regenerate yet');
  var replies = el.thread.querySelectorAll('.msg.assistant');
  if (replies.length) replies[replies.length - 1].remove();
  send({ regenerate: true });
}

function send(payload) {
  if (payload.message) {
    state.lastUserText = payload.message;
    addUser(payload.message);
  }
  clearConstellation();
  setStreaming(true);
  state.pinned = true;

  var progress = document.createElement('div');
  progress.className = 'msg assistant';
  progress.innerHTML = '<div class="progress"><span class="pulse"></span>' +
    '<span class="progress-text">Starting…</span><span class="elapsed"></span></div>';
  var label = progress.querySelector('.progress-text');
  var elapsed = progress.querySelector('.elapsed');
  el.thread.appendChild(progress);
  scrollToEnd();

  var started = Date.now();
  var ticker = setInterval(function () {
    elapsed.textContent = ((Date.now() - started) / 1000).toFixed(0) + 's';
  }, 1000);

  state.controller = new AbortController();
  var body = {
    message: payload.message || '',
    session_id: state.sessionId,
    regenerate: !!payload.regenerate
  };

  fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: state.controller.signal
  }).then(function (res) {
    if (!res.ok) {
      return res.json().catch(function () { return {}; }).then(function (err) {
        throw new Error(err.detail || ('server returned ' + res.status));
      });
    }
    var reader = res.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    var finalData = null;

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) return finalData;
        buffer += decoder.decode(chunk.value, { stream: true });
        var frames = buffer.split('\n\n');
        buffer = frames.pop();
        frames.forEach(function (frame) {
          if (frame.indexOf('data: ') !== 0) return;
          var event = JSON.parse(frame.slice(6));
          if (event.type === 'session') {
            if (!state.sessionId) { state.sessionId = event.id; loadSessions(); }
          } else if (event.type === 'progress') {
            label.textContent = event.text;
            if (event.specialist) litNode(event.specialist);
            scrollToEnd();
          } else if (event.type === 'done') {
            finalData = event.data;
          } else if (event.type === 'error') {
            throw new Error(event.text);
          }
        });
        return pump();
      });
    }
    return pump();
  }).then(function (data) {
    progress.remove();
    if (!data) return;
    state.lastReply = data.reply;
    (data.tasks || []).forEach(function (t) { litNode(t.specialist); });
    addAssistant(data.reply, data.tasks, data.run_id, true);
    loadSessions();
  }).catch(function (err) {
    progress.remove();
    if (err && err.name === 'AbortError') {
      addSystem('Stopped watching. The run is still finishing on the server and will be saved to this chat.', {
        action: { label: 'Reload chat', run: function () { if (state.sessionId) openSession(state.sessionId); } }
      });
      return;
    }
    addSystem('That run failed: ' + (err && err.message ? err.message : err) +
      '. Check the terminal running the server for the full trace.', { error: true });
  }).then(function () {
    clearInterval(ticker);
    setStreaming(false);
    state.controller = null;
    el.input.focus();
  });
}

/* ── Command palette ─────────────────────────────────────────────── */
var COMMANDS = [
  { label: 'New chat', hint: 'N', icon: ICONS.bolt, run: newChat },
  { label: 'Regenerate last reply', hint: 'R', icon: ICONS.regen, run: regenerate },
  { label: 'Copy last reply', hint: 'C', icon: ICONS.copy, run: copyLastReply },
  { label: 'Toggle light / dark theme', hint: 'T', icon: ICONS.bolt, run: toggleTheme },
  { label: 'Toggle sidebar', hint: 'B', icon: ICONS.bolt, run: toggleSidebar },
  { label: 'Keyboard shortcuts', hint: '?', icon: ICONS.bolt, run: openHelp }
];

/* Subsequence match, so "mth slv" finds "Math Solver" the way ⌘K should. */
function fuzzy(needle, haystack) {
  if (!needle) return true;
  var n = needle.toLowerCase(), h = haystack.toLowerCase(), i = 0;
  for (var j = 0; j < h.length && i < n.length; j++) {
    if (n[i] === ' ' ) { i++; continue; }
    if (h[j] === n[i]) i++;
  }
  return i >= n.replace(/\s+$/, '').length;
}

function openPalette() {
  el.paletteOverlay.hidden = false;
  el.paletteInput.value = '';
  renderPalette('');
  el.paletteInput.focus();
}
function closePalette() { el.paletteOverlay.hidden = true; }

function renderPalette(query) {
  var commands = COMMANDS.filter(function (c) { return fuzzy(query, c.label); });
  var sessions = state.sessions.filter(function (s) { return fuzzy(query, s.title); }).slice(0, 8);
  state.paletteItems = commands.map(function (c) { return { kind: 'command', data: c }; })
    .concat(sessions.map(function (s) { return { kind: 'session', data: s }; }));
  state.paletteIndex = 0;

  if (!state.paletteItems.length) {
    el.paletteList.innerHTML = '<div class="palette-empty">Nothing matches “' +
      MD.escapeHtml(query) + '”.</div>';
    return;
  }
  var html = '', lastKind = null;
  state.paletteItems.forEach(function (item, i) {
    if (item.kind !== lastKind) {
      lastKind = item.kind;
      html += '<div class="palette-section">' + (item.kind === 'command' ? 'Commands' : 'Chats') + '</div>';
    }
    var label = item.kind === 'command' ? item.data.label : item.data.title;
    var right = item.kind === 'command' ? '<span class="kbd-hint">' + item.data.hint + '</span>' : '';
    html += '<button class="palette-item" role="option" data-index="' + i + '"' +
      (i === 0 ? ' aria-selected="true"' : '') + '>' +
      (item.kind === 'command' ? item.data.icon : ICONS.chat) +
      '<span class="label">' + MD.escapeHtml(label) + '</span>' + right + '</button>';
  });
  el.paletteList.innerHTML = html;
}

function movePalette(delta) {
  if (!state.paletteItems.length) return;
  state.paletteIndex = (state.paletteIndex + delta + state.paletteItems.length) % state.paletteItems.length;
  el.paletteList.querySelectorAll('.palette-item').forEach(function (node, i) {
    var on = i === state.paletteIndex;
    node.setAttribute('aria-selected', String(on));
    if (on) node.scrollIntoView({ block: 'nearest' });
  });
}

function runPaletteItem(index) {
  var item = state.paletteItems[index];
  if (!item) return;
  closePalette();
  if (item.kind === 'command') item.data.run();
  else openSession(item.data.id);
}

el.paletteInput.addEventListener('input', function () { renderPalette(el.paletteInput.value.trim()); });
el.paletteList.addEventListener('click', function (e) {
  var item = e.target.closest('.palette-item');
  if (item) runPaletteItem(+item.dataset.index);
});
el.paletteInput.addEventListener('keydown', function (e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); movePalette(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); movePalette(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); runPaletteItem(state.paletteIndex); }
});

/* ── Help sheet ──────────────────────────────────────────────────── */
function openHelp() { el.helpOverlay.hidden = false; }
function closeHelp() { el.helpOverlay.hidden = true; }

[el.paletteOverlay, el.helpOverlay].forEach(function (overlay) {
  overlay.addEventListener('mousedown', function (e) {
    if (e.target === overlay) { closePalette(); closeHelp(); }
  });
});

function copyLastReply() {
  if (!state.lastReply) return toast('No reply to copy yet');
  copyText(state.lastReply).then(function () { toast('Reply copied'); })
    .catch(function () { toast('Copy failed'); });
}

function cycleSession(delta) {
  if (!state.sessions.length) return;
  var i = state.sessions.findIndex(function (s) { return s.id === state.sessionId; });
  var next = i === -1 ? 0 : (i + delta + state.sessions.length) % state.sessions.length;
  openSession(state.sessions[next].id);
}

/* ── Keyboard ────────────────────────────────────────────────────── */
function isTyping(target) {
  return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' ||
    target.isContentEditable);
}

document.addEventListener('keydown', function (e) {
  var mod = MAC ? e.metaKey : e.ctrlKey;

  if (mod && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    el.paletteOverlay.hidden ? openPalette() : closePalette();
    return;
  }

  if (e.key === 'Escape') {
    if (!el.paletteOverlay.hidden) return closePalette();
    if (!el.helpOverlay.hidden) return closeHelp();
    if (state.streaming) return stopStreaming();
    if (isTyping(e.target)) e.target.blur();
    return;
  }

  if (isTyping(e.target) || e.altKey || e.ctrlKey || e.metaKey) return;

  switch (e.key) {
    case '/': e.preventDefault(); el.input.focus(); break;
    case 'n': newChat(); break;
    case 'r': regenerate(); break;
    case 'c': copyLastReply(); break;
    case 't': toggleTheme(); break;
    case 'b': toggleSidebar(); break;
    case '[': cycleSession(-1); break;
    case ']': cycleSession(1); break;
    case '?': openHelp(); break;
  }
});

/* ── Wiring ──────────────────────────────────────────────────────── */
$('btn-new').addEventListener('click', newChat);
$('btn-search').addEventListener('click', openPalette);
$('btn-theme').addEventListener('click', toggleTheme);
$('btn-help').addEventListener('click', openHelp);
$('btn-sidebar').addEventListener('click', toggleSidebar);
$('btn-regen-head').addEventListener('click', regenerate);

/* ── Boot ────────────────────────────────────────────────────────── */
applyTheme(document.documentElement.dataset.theme);
loadHealth();
loadRoster();
loadSessions();
newChat();
