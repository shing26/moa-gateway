/* Hallmark · dashboard interactions · bright SaaS console */
(function () {
  'use strict';

  var $ = function (sel, root) {
    return (root || document).querySelector(sel);
  };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fetchJSON(url, options) {
    return fetch(url, options).then(function (res) {
      if (!res.ok) {
        throw new Error('请求失败: ' + res.status);
      }
      return res.json();
    });
  }

  function setStat(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = String(value);
  }

  var toastTimer = null;
  function toast(message, tone) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = message;
    el.dataset.tone = tone || 'success';
    el.classList.add('show');
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      el.classList.remove('show');
    }, 2400);
  }

  function setLoading(btn, loading) {
    if (!btn) return;
    if (loading) {
      btn.dataset.originalText = btn.textContent.trim();
      btn.textContent = btn.dataset.loadingText || '处理中…';
      btn.disabled = true;
      btn.classList.add('is-loading');
    } else {
      btn.textContent = btn.dataset.originalText || btn.textContent;
      btn.disabled = false;
      btn.classList.remove('is-loading');
    }
  }

  function emptyState(text) {
    return '<div class="empty-state">' + esc(text) + '</div>';
  }

  function badge(value, tone) {
    return '<span class="badge badge-' + (tone || 'neutral') + '">' + esc(value) + '</span>';
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    var t = new Date(ts);
    if (Number.isNaN(t.getTime())) return String(ts).slice(0, 19).replace('T', ' ');
    function p(n) {
      return String(n).padStart(2, '0');
    }
    return t.getFullYear() + '-' + p(t.getMonth() + 1) + '-' + p(t.getDate()) + ' ' +
      p(t.getHours()) + ':' + p(t.getMinutes()) + ':' + p(t.getSeconds());
  }

  function redisLabel(value) {
    if (!value) return '未知';
    var s = String(value);
    if (s.indexOf('connected') !== -1) return '已连接';
    if (s.indexOf('fallback') !== -1) return '内存回退';
    if (s.indexOf('error') !== -1) return '异常';
    return s;
  }

  function statusTone(value) {
    var s = String(value);
    if (s.indexOf('error') !== -1) return 'danger';
    if (s.indexOf('fallback') !== -1) return 'warn';
    return 'success';
  }

  function initHealthChip() {
    var chip = document.getElementById('health-chip');
    if (!chip) return;
    fetchJSON('/healthz').then(function (health) {
      var ok = health.status === 'healthy';
      chip.textContent = ok ? '系统健康' : '系统降级';
      chip.dataset.tone = ok ? 'success' : 'warn';
    }).catch(function () {
      chip.textContent = '服务离线';
      chip.dataset.tone = 'danger';
    });
  }

  var overviewBusy = false;

  function initOverview() {
    refreshOverview();
    window.setInterval(function () {
      if (!overviewBusy) refreshOverview();
    }, 5000);
  }

  function refreshOverview() {
    if (overviewBusy) return;
    overviewBusy = true;
    Promise.all([
      fetchJSON('/healthz'),
      fetchJSON('/dashboard/api/sessions'),
      fetchJSON('/knowledge/list'),
      fetchJSON('/dashboard/api/logs')
    ]).then(function (results) {
      var health = results[0];
      var sessions = results[1];
      var kb = results[2];
      var logs = results[3];
      var ok = health.status === 'healthy';
      setStat('stat-health', ok ? '健康' : '降级');
      setStat('stat-redis', redisLabel(health.checks && health.checks.redis));
      setStat('stat-sessions', (sessions.sessions || []).length);
      setStat('stat-docs', kb.total || 0);

      var checks = health.checks || {};
      var detail = document.getElementById('health-detail');
      var keys = Object.keys(checks);
      detail.innerHTML = keys.length
        ? keys.map(function (key) {
            return '<div class="check-item">' +
              '<span class="status-dot status-' + statusTone(checks[key]) + '"></span>' +
              '<span class="check-label">' + esc(key) + '</span>' +
              '<span class="check-value mono">' + esc(String(checks[key])) + '</span></div>';
          }).join('')
        : emptyState('暂无检查项');

      renderRecentLogs((logs.logs || []).slice(0, 5));
    }).catch(function (err) {
      var detail = document.getElementById('health-detail');
      if (detail) detail.innerHTML = emptyState(err.message);
    }).finally(function () {
      overviewBusy = false;
    });
  }

  function renderRecentLogs(logs) {
    var box = document.getElementById('recent-logs');
    if (!box) return;
    if (!logs.length) {
      box.innerHTML = emptyState('暂无请求日志');
      return;
    }
    box.innerHTML = '<div class="mini-log">' + logs.map(function (entry) {
      return '<div class="mini-log-row">' +
        '<span class="mono mini-time">' + esc(fmtTime(entry.timestamp)) + '</span>' +
        badge(entry.intent || 'unknown', 'accent') +
        '<span class="mini-agent">' + esc(entry.agent_name || '—') + '</span>' +
        '<span class="mini-input">' + esc((entry.input_preview || '').slice(0, 60)) + '</span>' +
        '</div>';
    }).join('') + '</div>';
  }

  function initKnowledge() {
    var upload = document.getElementById('kb-upload');
    if (upload) upload.addEventListener('submit', onUpload);
    var fileBtn = document.getElementById('kb-file-btn');
    if (fileBtn) fileBtn.addEventListener('click', onUploadFile);
    var searchForm = document.getElementById('kb-search-form');
    if (searchForm) searchForm.addEventListener('submit', onKbSearch);
    loadKb();
  }

  function onUpload(event) {
    event.preventDefault();
    var btn = document.getElementById('kb-upload-btn');
    var title = document.getElementById('kb-title').value.trim();
    var content = document.getElementById('kb-content').value.trim();
    if (!title || !content) return;
    setLoading(btn, true);
    fetchJSON('/dashboard/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title, content: content })
    }).then(function () {
      toast('已上传：' + title);
      document.getElementById('kb-title').value = '';
      document.getElementById('kb-content').value = '';
      loadKb();
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  function onUploadFile() {
    var input = document.getElementById('kb-file');
    var btn = document.getElementById('kb-file-btn');
    var file = input && input.files && input.files[0];
    if (!file) {
      toast('请先选择文件', 'warn');
      return;
    }
    setLoading(btn, true);
    var formData = new FormData();
    formData.append('file', file);
    fetchJSON('/dashboard/api/knowledge/upload_file', {
      method: 'POST',
      body: formData
    }).then(function (data) {
      toast('已上传：' + (data.title || file.name));
      input.value = '';
      loadKb();
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  function onKbSearch(event) {
    event.preventDefault();
    var btn = document.getElementById('kb-search-btn');
    var query = document.getElementById('kb-search-query').value.trim();
    if (!query) return;
    setLoading(btn, true);
    fetchJSON('/dashboard/api/knowledge/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query, top_k: 5 })
    }).then(function (data) {
      renderSearchResults(data);
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  function renderSearchResults(data) {
    var box = document.getElementById('kb-search-results');
    var count = document.getElementById('kb-search-count');
    if (!box) return;
    var hits = data.hits || [];
    if (count) count.textContent = hits.length ? '命中 ' + hits.length + ' 个分块' : '';
    if (!hits.length) {
      box.innerHTML = emptyState('未命中任何分块，换个关键词试试');
      return;
    }
    box.innerHTML = '<div class="hit-list">' + hits.map(function (hit) {
      return '<div class="hit-item">' +
        '<span class="badge badge-accent">#' + hit.index + '</span>' +
        '<span class="hit-text">' + esc(hit.chunk) + '</span></div>';
    }).join('') + '</div>';
  }

  function loadKb() {
    var body = document.getElementById('kb-body');
    var count = document.getElementById('kb-count');
    if (!body) return;
    body.innerHTML = '<tr><td colspan="4" class="row-empty">加载中…</td></tr>';
    fetchJSON('/knowledge/list').then(function (data) {
      var docs = data.documents || [];
      if (count) count.textContent = '共 ' + docs.length + ' 篇';
      if (!docs.length) {
        body.innerHTML = '<tr><td colspan="4" class="row-empty">暂无文档，先上传一篇</td></tr>';
        return;
      }
      body.innerHTML = docs.map(function (doc) {
        return '<tr>' +
          '<td><strong>' + esc(doc.title) + '</strong></td>' +
          '<td class="mono">' + esc(String(doc.id).slice(0, 12)) + '</td>' +
          '<td>' + esc(doc.chunks) + ' 块</td>' +
          '<td class="col-actions">' +
          '<a class="btn btn-ghost btn-xs" href="/dashboard/knowledge/' + encodeURIComponent(doc.id) + '">查看</a> ' +
          '<button class="btn btn-danger btn-xs" type="button" data-id="' + esc(doc.id) + '">删除</button></td>' +
          '</tr>';
      }).join('');
      $$('[data-id]', body).forEach(function (btn) {
        btn.addEventListener('click', function () {
          onDeleteDoc(btn);
        });
      });
    }).catch(function (err) {
      body.innerHTML = '<tr><td colspan="4" class="row-empty">' + esc(err.message) + '</td></tr>';
    });
  }

  function onDeleteDoc(btn) {
    btn.disabled = true;
    fetchJSON('/dashboard/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc_id: btn.dataset.id })
    }).then(function () {
      toast('已删除文档');
      loadKb();
    }).catch(function (err) {
      btn.disabled = false;
      toast(err.message, 'danger');
    });
  }

  function initKnowledgeDetail() {
    var root = document.getElementById('doc-detail-root');
    if (!root) return;
    loadDocDetail(root.dataset.docId);
  }

  function loadDocDetail(id) {
    var box = document.getElementById('doc-detail-root');
    if (!box) return;
    box.innerHTML = '<div class="empty-state">加载中…</div>';
    fetchJSON('/dashboard/api/knowledge/' + encodeURIComponent(id)).then(function (data) {
      var doc = data.doc || {};
      var chunks = doc.chunks || [];
      box.innerHTML =
        '<div class="doc-head"><h3>' + esc(doc.title) + '</h3>' +
        '<span class="muted">' + chunks.length + ' 个分块</span></div>' +
        '<div class="doc-meta mono">' + esc(doc.id) + '</div>' +
        '<div class="doc-content"><strong>全文</strong><p>' + esc(doc.content) + '</p></div>' +
        '<div class="doc-chunks"><strong>分块</strong>' +
        chunks.map(function (chunk, i) {
          return '<div class="chunk-item"><span class="badge badge-neutral">#' + (i + 1) + '</span>' +
            '<span class="chunk-text">' + esc(chunk) + '</span></div>';
        }).join('') + '</div>' +
        '<div class="form-row doc-actions">' +
        '<button id="doc-delete-btn" class="btn btn-danger" type="button">删除文档</button></div>';
      var del = document.getElementById('doc-delete-btn');
      if (del) {
        del.addEventListener('click', function () {
          del.disabled = true;
          fetchJSON('/dashboard/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ doc_id: id })
          }).then(function () {
            toast('已删除文档');
            window.location.href = '/dashboard/knowledge';
          }).catch(function (err) {
            del.disabled = false;
            toast(err.message, 'danger');
          });
        });
      }
    }).catch(function (err) {
      box.innerHTML = emptyState(err.message);
    });
  }

  function initSessions() {
    loadSessions();
  }

  function loadSessions() {
    var body = document.getElementById('session-body');
    var count = document.getElementById('session-count');
    if (!body) return;
    body.innerHTML = '<tr><td colspan="4" class="row-empty">加载中…</td></tr>';
    fetchJSON('/dashboard/api/sessions').then(function (data) {
      var sessions = data.sessions || [];
      if (count) count.textContent = '共 ' + sessions.length + ' 个';
      if (!sessions.length) {
        body.innerHTML = '<tr><td colspan="4" class="row-empty">暂无活跃会话</td></tr>';
        return;
      }
      body.innerHTML = sessions.map(function (s) {
        var preview = (s.history || []).slice(-2).map(function (h) {
          return '<div class="msg-row">' +
            '<span class="msg-role msg-role-' + esc(h.role) + '">' + esc(h.role) + '</span>' +
            '<span class="msg-text">' + esc(h.content) + '</span></div>';
        }).join('') || '<div class="msg-row muted">无对话记录</div>';
        return '<tr>' +
          '<td class="session-id"><span class="mono">' + esc(s.session_id) + '</span></td>' +
          '<td>' + badge(s.mode_label || s.mode || 'default', 'accent') + '</td>' +
          '<td class="preview-cell">' + preview + '</td>' +
          '<td class="col-actions">' +
          '<a class="btn btn-ghost btn-xs" href="/dashboard/sessions/' + encodeURIComponent(s.id) + '">详情</a> ' +
          '<button class="btn btn-ghost btn-xs" type="button" data-id="' + esc(s.id) + '">清空</button></td>' +
          '</tr>';
      }).join('');
      $$('[data-id]', body).forEach(function (btn) {
        btn.addEventListener('click', function () {
          onClearSession(btn);
        });
      });
    }).catch(function (err) {
      body.innerHTML = '<tr><td colspan="4" class="row-empty">' + esc(err.message) + '</td></tr>';
    });
  }

  function onClearSession(btn) {
    var id = btn.dataset.id;
    btn.disabled = true;
    fetchJSON('/dashboard/api/sessions/' + encodeURIComponent(id) + '/clear', {
      method: 'POST'
    }).then(function () {
      toast('会话已清空');
      loadSessions();
    }).catch(function (err) {
      btn.disabled = false;
      toast(err.message, 'danger');
    });
  }

  function initSessionDetail() {
    var root = document.getElementById('session-detail-root');
    if (!root) return;
    var sid = root.dataset.sid;
    loadSessionDetail(sid);
    var modeBtn = document.getElementById('session-mode-btn');
    if (modeBtn) {
      modeBtn.addEventListener('click', function () {
        onSessionMode(sid);
      });
    }
    var clearBtn = document.getElementById('session-clear-btn');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        onSessionClearDetail(sid);
      });
    }
  }

  function loadSessionDetail(sid) {
    var box = document.getElementById('session-history');
    var count = document.getElementById('session-history-count');
    if (!box) return;
    box.innerHTML = '<div class="empty-state">加载中…</div>';
    fetchJSON('/dashboard/api/sessions/' + encodeURIComponent(sid)).then(function (data) {
      var select = document.getElementById('session-mode-select');
      if (select) select.value = data.mode || 'default';
      if (count) count.textContent = data.history.length + ' 条消息';
      if (!data.history.length) {
        box.innerHTML = '<div class="empty-state">暂无对话记录</div>';
        return;
      }
      box.innerHTML = '<div class="history-list">' + data.history.map(function (h) {
        return '<div class="history-item history-' + esc(h.role) + '">' +
          '<span class="history-role">' + esc(h.role) + '</span>' +
          '<div class="history-bubble">' + esc(h.content) + '</div></div>';
      }).join('') + '</div>';
    }).catch(function (err) {
      box.innerHTML = '<div class="empty-state">' + esc(err.message) + '</div>';
    });
  }

  function onSessionMode(sid) {
    var select = document.getElementById('session-mode-select');
    var btn = document.getElementById('session-mode-btn');
    if (!select || !btn) return;
    setLoading(btn, true);
    fetchJSON('/dashboard/api/sessions/' + encodeURIComponent(sid) + '/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: select.value })
    }).then(function (data) {
      toast('模式已切换：' + data.mode_label);
      loadSessionDetail(sid);
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  function onSessionClearDetail(sid) {
    var btn = document.getElementById('session-clear-btn');
    if (!btn) return;
    setLoading(btn, true);
    fetchJSON('/dashboard/api/sessions/' + encodeURIComponent(sid) + '/clear', {
      method: 'POST'
    }).then(function () {
      toast('会话已清空');
      loadSessionDetail(sid);
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  function initTest() {
    var form = document.getElementById('webhook-form');
    if (form) form.addEventListener('submit', onTestSubmit);
  }

  function onTestSubmit(event) {
    event.preventDefault();
    var btn = document.getElementById('wh-send-btn');
    var text = document.getElementById('wh-text').value.trim();
    var body = JSON.stringify({
      session_id: document.getElementById('wh-session').value.trim() || 'dash-test',
      user_id: document.getElementById('wh-user').value.trim() || 'admin',
      text: text,
      message_id: 'dash-' + Date.now()
    });
    var box = document.getElementById('wh-result');
    box.hidden = false;
    document.getElementById('wh-status').textContent = '发送中…';
    document.getElementById('wh-body').textContent = '';
    setLoading(btn, true);
    fetch('/webhook/feishu', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body
    }).then(function (res) {
      return res.text().then(function (raw) {
        return { res: res, raw: raw };
      });
    }).then(function (result) {
      document.getElementById('wh-status').textContent =
        result.res.status + ' ' + result.res.statusText;
      var dot = box.querySelector('.status-dot');
      dot.className = 'status-dot status-' + (result.res.ok ? 'success' : 'danger');
      var pretty = result.raw;
      try {
        pretty = JSON.stringify(JSON.parse(result.raw), null, 2);
      } catch (e) {
        /* keep raw text */
      }
      document.getElementById('wh-body').textContent = pretty;
    }).catch(function (err) {
      document.getElementById('wh-status').textContent = '请求失败';
      document.getElementById('wh-body').textContent = err.message;
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  var allLogs = [];

  function initLogs() {
    var filter = document.getElementById('log-filter');
    if (filter) filter.addEventListener('input', renderLogs);
    loadLogs();
    window.setInterval(function () {
      if (!window.__moaLogsLoading) loadLogs();
    }, 3000);
  }

  function loadLogs() {
    var body = document.getElementById('log-body');
    if (!body) return;
    window.__moaLogsLoading = true;
    body.innerHTML = '<tr><td colspan="8" class="row-empty">加载中…</td></tr>';
    fetchJSON('/dashboard/api/logs').then(function (data) {
      allLogs = data.logs || [];
      renderLogs();
    }).catch(function (err) {
      body.innerHTML = '<tr><td colspan="8" class="row-empty">' + esc(err.message) + '</td></tr>';
    }).finally(function () {
      window.__moaLogsLoading = false;
    });
  }

  function renderLogs() {
    var body = document.getElementById('log-body');
    var count = document.getElementById('log-count');
    var empty = document.getElementById('log-empty');
    if (!body) return;
    var filterEl = document.getElementById('log-filter');
    var q = (filterEl ? filterEl.value : '').trim().toLowerCase();
    var logs = allLogs.filter(function (entry) {
      if (!q) return true;
      return JSON.stringify(entry).toLowerCase().indexOf(q) !== -1;
    });
    if (count) count.textContent = allLogs.length ? '显示 ' + logs.length + ' / ' + allLogs.length : '暂无记录';
    if (!logs.length) {
      body.innerHTML = '';
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    body.innerHTML = logs.map(function (entry) {
      return '<tr>' +
        '<td class="mono">' + esc(fmtTime(entry.timestamp)) + '</td>' +
        '<td>' + badge(entry.intent || 'unknown', 'accent') + '</td>' +
        '<td>' + esc(entry.agent_name || '—') + '</td>' +
        '<td class="mono">' + esc(String(entry.session_id || '—').slice(0, 16)) + '</td>' +
        '<td class="mono">' + esc(entry.eval_score) + '</td>' +
        '<td class="mono log-preview" title="' + esc((entry.input_preview || '').slice(0, 200)) + '">' +
          esc((entry.input_preview || '').slice(0, 40)) + '</td>' +
        '<td class="mono log-preview" title="' + esc((entry.output_preview || '').slice(0, 200)) + '">' +
          esc((entry.output_preview || '').slice(0, 40)) + '</td>' +
        '<td class="col-actions"><button class="btn btn-ghost btn-xs" type="button" data-log="' +
        esc(JSON.stringify(entry)) + '">详情</button></td>' +
        '</tr>';
    }).join('');
    $$('[data-log]', body).forEach(function (btn) {
      btn.addEventListener('click', function () {
        toggleLogDetail(btn);
      });
    });
  }

  function toggleLogDetail(btn) {
    var row = btn.closest('tr');
    var detail = row.nextElementSibling;
    if (detail && detail.classList.contains('log-detail')) {
      detail.remove();
      return;
    }
    detail = document.createElement('tr');
    detail.className = 'log-detail';
    detail.innerHTML = '<td colspan="8"><pre class="log-json">' +
      esc(JSON.stringify(JSON.parse(btn.dataset.log), null, 2)) + '</pre></td>';
    row.insertAdjacentElement('afterend', detail);
  }

  function initOps() {
    loadOpsConfig();
    var form = document.getElementById('ops-config-form');
    if (form) form.addEventListener('submit', onOpsSave);
    var testBtn = document.getElementById('ops-test-btn');
    if (testBtn) testBtn.addEventListener('click', onOpsTest);
    var syncBtn = document.getElementById('obsidian-sync-btn');
    if (syncBtn) syncBtn.addEventListener('click', onObsidianSync);
  }

  function loadOpsConfig() {
    fetchJSON('/dashboard/api/ops/config').then(function (data) {
      var model = document.getElementById('ops-model');
      var baseUrl = document.getElementById('ops-base-url');
      if (model) model.value = data.llm.model || '';
      if (baseUrl) baseUrl.value = data.llm.base_url || '';
      var keyStatus = document.getElementById('ops-key-status');
      if (keyStatus) {
        keyStatus.textContent = data.llm.api_key_set ? '当前已配置 API Key' : '当前未配置 API Key';
      }
      renderOpsStatus(data);
      renderFlags(data.flags || []);
    }).catch(function (err) {
      var status = document.getElementById('ops-status');
      if (status) status.innerHTML = emptyState(err.message);
    });
  }

  function renderOpsStatus(data) {
    var box = document.getElementById('ops-status');
    if (!box) return;
    var obs = data.obsidian || {};
    var obsStatusEl = document.getElementById('ops-obsidian-status');
    if (obsStatusEl) {
      obsStatusEl.textContent = obs.enabled
        ? 'Vault: ' + (obs.root || '') + ' · ' + (obs.docs || 0) + ' 篇'
        : 'Obsidian 未启用';
    }
    var items = [
      ['Redis', data.redis && data.redis.url ? data.redis.url : '未知'],
      ['飞书卡片', data.feishu && data.feishu.configured ? '已配置' : '未配置'],
      ['OTel 端点', (data.tracing && data.tracing.otlp_endpoint) || '未设置'],
      ['全局限流', data.limiter && data.limiter.wired ? '已接入' : (data.limiter && data.limiter.note) || '未接入'],
      ['Obsidian 同步', obs.enabled ? (obs.docs || 0) + ' 篇 · ' + ((obs.last_sync || '').slice(0, 19).replace('T', ' ') || '未同步') : '未启用']
    ];
    box.innerHTML = items.map(function (item) {
      return '<div class="check-item"><span class="status-dot status-neutral"></span>' +
        '<span class="check-label">' + esc(item[0]) + '</span>' +
        '<span class="check-value mono">' + esc(item[1]) + '</span></div>';
    }).join('');
  }

  function onObsidianSync() {
    var btn = document.getElementById('obsidian-sync-btn');
    if (!btn) return;
    setLoading(btn, true);
    fetchJSON('/dashboard/api/ops/obsidian/sync', { method: 'POST' }).then(function (data) {
      toast('Obsidian 同步完成：' + data.changed + ' 篇变更');
      loadOpsConfig();
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  function renderFlags(flags) {
    var body = document.getElementById('ops-flag-body');
    if (!body) return;
    if (!flags.length) {
      body.innerHTML = '<tr><td colspan="3" class="row-empty">暂无 Feature Flags</td></tr>';
      return;
    }
    body.innerHTML = flags.map(function (flag) {
      var tone = flag.value === true ? 'success' : flag.value === false ? 'neutral' : 'accent';
      var toggle = typeof flag.value === 'boolean'
        ? '<button class="btn btn-ghost btn-xs" type="button" data-name="' + esc(flag.name) +
          '" data-value="' + flag.value + '">切换</button> '
        : '';
      return '<tr>' +
        '<td class="mono">' + esc(flag.name) + '</td>' +
        '<td>' + badge(String(flag.value), tone) + '</td>' +
        '<td class="col-actions">' + toggle +
        '<button class="btn btn-danger btn-xs" type="button" data-del="' + esc(flag.name) + '">还原</button></td>' +
        '</tr>';
    }).join('');
    $$('[data-name]', body).forEach(function (btn) {
      btn.addEventListener('click', function () {
        onFlagToggle(btn);
      });
    });
    $$('[data-del]', body).forEach(function (btn) {
      btn.addEventListener('click', function () {
        onFlagDelete(btn);
      });
    });
  }

  function onFlagToggle(btn) {
    var name = btn.dataset.name;
    var next = btn.dataset.value === 'true' ? false : true;
    btn.disabled = true;
    fetchJSON('/dashboard/api/ops/flags/' + encodeURIComponent(name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: next })
    }).then(function () {
      toast('Flag 已更新：' + name);
      loadOpsConfig();
    }).catch(function (err) {
      btn.disabled = false;
      toast(err.message, 'danger');
    });
  }

  function onFlagDelete(btn) {
    var name = btn.dataset.del;
    btn.disabled = true;
    fetch('/dashboard/api/ops/flags/' + encodeURIComponent(name), { method: 'DELETE' })
      .then(function (res) {
        if (!res.ok) throw new Error('请求失败: ' + res.status);
        toast('Flag 已还原：' + name);
        loadOpsConfig();
      })
      .catch(function (err) {
        btn.disabled = false;
        toast(err.message, 'danger');
      });
  }

  function onOpsSave(event) {
    event.preventDefault();
    var btn = document.getElementById('ops-save-btn');
    var body = {
      model: document.getElementById('ops-model').value.trim(),
      base_url: document.getElementById('ops-base-url').value.trim(),
      api_key: document.getElementById('ops-api-key').value.trim()
    };
    setLoading(btn, true);
    fetchJSON('/dashboard/api/ops/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function () {
      toast('配置已保存（运行时生效）');
      document.getElementById('ops-api-key').value = '';
      loadOpsConfig();
    }).catch(function (err) {
      toast(err.message, 'danger');
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  function onOpsTest() {
    var btn = document.getElementById('ops-test-btn');
    var message = (document.getElementById('ops-test-message').value || '').trim() || 'ping';
    var body = {
      message: message,
      model: document.getElementById('ops-model').value.trim() || null,
      base_url: document.getElementById('ops-base-url').value.trim() || null,
      api_key: document.getElementById('ops-api-key').value.trim() || null
    };
    var box = document.getElementById('ops-test-result');
    box.hidden = false;
    document.getElementById('ops-test-status').textContent = '测试中…';
    document.getElementById('ops-test-body').textContent = '';
    var dot = box.querySelector('.status-dot');
    dot.className = 'status-dot status-neutral';
    setLoading(btn, true);
    fetchJSON('/dashboard/api/ops/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (data) {
      var ok = !!data.ok;
      document.getElementById('ops-test-status').textContent = ok ? '连接成功' : '连接失败';
      dot.className = 'status-dot status-' + (ok ? 'success' : 'danger');
      document.getElementById('ops-test-body').textContent = ok ? data.reply : data.error;
    }).catch(function (err) {
      document.getElementById('ops-test-status').textContent = '请求失败';
      dot.className = 'status-dot status-danger';
      document.getElementById('ops-test-body').textContent = err.message;
    }).finally(function () {
      setLoading(btn, false);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initHealthChip();
    var page = document.body.dataset.page;
    if (page === 'overview') initOverview();
    if (page === 'knowledge') initKnowledge();
    if (page === 'knowledge-detail') initKnowledgeDetail();
    if (page === 'sessions') initSessions();
    if (page === 'session-detail') initSessionDetail();
    if (page === 'test') initTest();
    if (page === 'logs') initLogs();
    if (page === 'ops') initOps();
    var refresh = document.getElementById('refresh-btn');
    if (refresh) {
      refresh.addEventListener('click', function () {
        window.location.reload();
      });
    }
  });
})();
