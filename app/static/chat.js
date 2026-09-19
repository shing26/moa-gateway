/* Agent Gateway · 自主任务 Agent Web 聊天界面 */
(function () {
  'use strict';

  var $ = function (sel, root) {
    return (root || document).querySelector(sel);
  };

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtTime(ts) {
    var t = new Date(ts);
    if (Number.isNaN(t.getTime())) return '';
    function p(n) { return String(n).padStart(2, '0'); }
    return p(t.getHours()) + ':' + p(t.getMinutes()) + ':' + p(t.getSeconds());
  }

  var SESSION_KEY = 'moa-web-chat-sid';
  var sessionId = localStorage.getItem(SESSION_KEY) ||
    'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  localStorage.setItem(SESSION_KEY, sessionId);

  // 用户 ID 跨会话保持：重置会话换的是 session，长期记忆仍按同一个 user 归属。
  var USER_KEY = 'moa-web-chat-uid';
  var userId = localStorage.getItem(USER_KEY) || 'web-user';

  var busy = false;

  function sessionBadge() {
    var el = document.getElementById('chat-sid');
    if (el) el.textContent = sessionId;
    var uidEl = document.getElementById('chat-uid');
    if (uidEl) {
      uidEl.value = userId;
      uidEl.addEventListener('change', function () {
        userId = uidEl.value.trim() || 'web-user';
        localStorage.setItem(USER_KEY, userId);
      });
    }
  }

  function addMessage(role, text, time) {
    var list = document.getElementById('chat-messages');
    if (!list) return;
    var bubble = document.createElement('div');
    bubble.className = 'chat-row chat-' + role;
    bubble.innerHTML =
      '<div class="chat-avatar">' + (role === 'user' ? '我' : 'A') + '</div>' +
      '<div class="chat-bubble"><div class="chat-meta">' +
      (role === 'user' ? '我' : 'Agent') + ' · ' + esc(time || fmtTime(Date.now())) +
      '</div><pre class="chat-text">' + esc(text) + '</pre></div>';
    list.appendChild(bubble);
    list.scrollTop = list.scrollHeight;
    return bubble;
  }

  function renderHistory(history) {
    var list = document.getElementById('chat-messages');
    if (!list) return;
    list.innerHTML = '';
    if (!history || !history.length) {
      list.innerHTML = '<div class="empty-state">还没有对话，输入任务开始吧（例如：帮我算 3*7 并记下来）</div>';
      return;
    }
    history.forEach(function (item) {
      addMessage(item.role === 'user' ? 'user' : 'assistant', item.content);
    });
  }

  function loadHistory() {
      fetch('/dashboard/api/chat/history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    }).then(function (res) { return res.json(); })
      .then(function (data) { renderHistory(data.history || []); })
      .catch(function () { /* 历史加载失败静默 */ });
  }

  function sendMessage() {
    var input = document.getElementById('chat-input');
    var text = (input ? input.value : '').trim();
    if (!text || busy) return;
    input.value = '';
    addMessage('user', text);
    busy = true;
    var sendBtn = document.getElementById('chat-send-btn');
    if (sendBtn) {
      sendBtn.disabled = true;
      sendBtn.textContent = '处理中…';
    }
      fetch('/dashboard/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, text: text, user_id: userId })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { res: res, data: data };
      });
    }).then(function (result) {
      if (!result.res.ok) {
        addMessage('assistant', result.data.message || result.data.error || ('请求失败: ' + result.res.status));
        return;
      }
      addMessage('assistant', result.data.text || '（空回复）');
    }).catch(function (err) {
      addMessage('assistant', '请求失败: ' + err.message);
    }).finally(function () {
      busy = false;
      if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.textContent = '发送';
      }
      var inputEl = document.getElementById('chat-input');
      if (inputEl) inputEl.focus();
    });
  }

  function initChat() {
    sessionBadge();
    loadHistory();
    var form = document.getElementById('chat-form');
    if (form) form.addEventListener('submit', function (e) {
      e.preventDefault();
      sendMessage();
    });
    var sendBtn = document.getElementById('chat-send-btn');
    if (sendBtn) sendBtn.addEventListener('click', sendMessage);
    var resetBtn = document.getElementById('chat-reset-btn');
    if (resetBtn) resetBtn.addEventListener('click', function () {
      sessionId = 'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
      localStorage.setItem(SESSION_KEY, sessionId);
      sessionBadge();
      loadHistory();
    });
    document.addEventListener('DOMContentLoaded', function () {
      var inputEl = document.getElementById('chat-input');
      if (inputEl) inputEl.focus();
    });
  }

  if (document.body && document.body.dataset.page === 'chat') {
    initChat();
  } else {
    document.addEventListener('DOMContentLoaded', function () {
      if (document.body.dataset.page === 'chat') initChat();
    });
  }
})();
