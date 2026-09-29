/* CapsWriter 管理端前端逻辑
 * - 单页、无构建依赖；全部用户数据经 textContent 写入（防 XSS）
 * - 认证模式由 /api/v1/bootstrap 决定：
 *     password 模式：显示密码登录表单；
 *     gateway 模式：信任部署面板登录网关，未过网关时只显示提示（本页没有也不需要密码框）。
 * - 状态低频轮询（5s），页面隐藏/网络错误时自动暂停
 * - 写操作带 CSRF 头，并显示影响范围与结果反馈
 * - 语音客户端接入的部署面板 API Key 与本页无关，页面不含任何密钥管理
 */
'use strict';

(function () {
  var $ = function (id) { return document.getElementById(id); };

  var csrf = '';
  var authMode = 'password';
  var logPaused = false;
  var logCursor = 0;
  var pollTimer = null;
  var hotwordsMtime = null;
  var theme = localStorage.getItem('cw-theme') || 'light';

  // ---------- 基础 ----------

  function applyTheme() {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    $('themeBtn').textContent = theme === 'dark' ? '☀ 浅色' : '☾ 深色';
    localStorage.setItem('cw-theme', theme);
  }
  $('themeBtn').addEventListener('click', function () {
    theme = theme === 'dark' ? 'light' : 'dark';
    applyTheme();
  });

  function api(method, url, body, opts) {
    opts = opts || {};
    var headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (csrf) headers['X-CSRF-Token'] = csrf;
    return fetch(url, {
      method: method,
      headers: headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (res) {
      if (res.status === 401 && !opts.noRedirect) {
        showUnauthenticated();
        throw new Error('未登录或会话已过期');
      }
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(data.error || ('HTTP ' + res.status));
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function fmtBytes(n) {
    if (n === null || n === undefined) return '—';
    if (n > 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(0) + ' MB';
    return Math.round(n / 1024) + ' KB';
  }
  function fmtTime(ts) {
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    return d.toLocaleString('zh-CN', { hour12: false });
  }
  function fmtUptime(sec) {
    if (sec === null || sec === undefined) return '—';
    var m = Math.floor(sec / 60), s = Math.floor(sec % 60), h = Math.floor(m / 60);
    if (h > 0) return h + ' 时 ' + (m % 60) + ' 分';
    if (m > 0) return m + ' 分 ' + s + ' 秒';
    return s + ' 秒';
  }

  // ---------- 视图切换 ----------

  function hideAllViews() {
    $('loginView').classList.add('hidden');
    $('gatewayView').classList.add('hidden');
    $('mainView').classList.add('hidden');
  }
  function showLogin() {
    hideAllViews();
    $('loginView').classList.remove('hidden');
    stopPolling();
  }
  function showGatewayNotice() {
    hideAllViews();
    $('gatewayView').classList.remove('hidden');
    stopPolling();
  }
  function showMain() {
    hideAllViews();
    $('mainView').classList.remove('hidden');
    refreshAll();
    startPolling();
  }
  function showUnauthenticated() {
    if (authMode === 'gateway') showGatewayNotice();
    else showLogin();
  }

  $('loginForm').addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = $('loginBtn');
    btn.disabled = true;
    api('POST', '/api/v1/login', { password: $('loginPassword').value }, { noRedirect: true })
      .then(function (data) {
        csrf = data.csrf || '';
        $('loginPassword').value = '';
        $('loginError').classList.add('hidden');
        showMain();
      })
      .catch(function (err) {
        var el = $('loginError');
        el.textContent = err.status === 429 ? '尝试过于频繁，请稍后再试' : (err.message || '登录失败');
        el.classList.remove('hidden');
      })
      .finally(function () { btn.disabled = false; });
  });

  $('logoutBtn').addEventListener('click', function () {
    if (authMode === 'password') api('POST', '/api/v1/logout', {}).catch(function () {});
    csrf = '';
    showUnauthenticated();
  });

  // ---------- 状态轮询 ----------

  var STATE_LABEL = {
    online: '在线', starting: '启动中', stopped: '已停止', stale: '异常/无响应', failed: '失败'
  };

  function refreshAll() {
    api('GET', '/api/v1/status')
      .then(function (data) { renderStatus(data.asr); hideGlobalError(); })
      .catch(function (err) {
        if (err.status !== 401) showGlobalError('状态获取失败：' + err.message);
        setBadge('离线', 'stale');
      });
    api('GET', '/api/v1/settings').then(renderSettings).catch(function () {});
    api('GET', '/api/v1/hotwords').then(renderHotwords).catch(function () {});
    api('GET', '/api/v1/audit').then(renderAudit).catch(function () {});
    pollLogs();
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(function () {
      if (document.hidden) return; // 页面隐藏时不发请求
      api('GET', '/api/v1/status')
        .then(function (data) { renderStatus(data.asr); hideGlobalError(); })
        .catch(function (err) {
          if (err.status === 401) return;
          showGlobalError('状态获取失败：' + err.message);
        });
      pollLogs();
      pollAction();
    }, 5000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function setBadge(text, cls) {
    var badge = $('connBadge');
    badge.textContent = text;
    badge.style.color = '';
    badge.className = 'badge';
  }
  function showGlobalError(text) {
    var el = $('globalError');
    el.textContent = text;
    el.classList.remove('hidden');
  }
  function hideGlobalError() { $('globalError').classList.add('hidden'); }

  function renderStatus(asr) {
    if (!asr) return;
    var state = asr.state;
    var busy = asr.busy;
    var label = STATE_LABEL[state] || state;
    if (state === 'online' && busy) label = '识别中';
    $('asrState').className = 'stateDot ' + state;
    $('asrStateText').textContent = label;

    var d = asr.detail || {};
    $('asrStateDetail').textContent = state === 'online'
      ? (d.worker_alive === false ? '识别子进程未运行' : '控制通道正常，端口 ' + d.port)
      : (state === 'stopped' ? '识别进程未运行' : '控制通道不可达，按 PID/端口监控');
    $('mModel').textContent = d.model_type || '—';
    $('mConns').textContent = state === 'online' ? String(d.connections) : '—';
    $('mUptime').textContent = state === 'online' ? fmtUptime(d.uptime_s) : '—';
    $('mCheck').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    $('mMemory').textContent = fmtBytes(asr.memory_bytes);
    var gpu = asr.gpu;
    $('mGpu').textContent = gpu ? (gpu.used_mb + ' / ' + gpu.total_mb + ' MB') : '不可用';
    $('mBusy').textContent = state === 'online' ? (busy ? '是' : '否') : '—';
    $('mVersion').textContent = d.version || '—';

    setBadge(state === 'online' ? (busy ? '识别中' : '在线') : label, state);

    $('startBtn').disabled = !(state === 'stopped' || state === 'stale' || state === 'failed');
    $('stopBtn').disabled = state === 'stopped';
    $('restartBtn').disabled = state === 'stopped';
  }

  // ---------- 动作 ----------

  function confirmRestart() {
    return api('GET', '/api/v1/status').then(function (data) {
      var asr = data.asr || {};
      var conns = (asr.detail && asr.detail.connections) || 0;
      var msg = conns > 0
        ? '当前有 ' + conns + ' 个客户端连接。重启会先等待会话空闲（最长 120 秒），期间无法识别。确定重启？'
        : '确定重启识别服务？';
      return window.confirm(msg);
    });
  }

  $('restartBtn').addEventListener('click', function () {
    confirmRestart().then(function (yes) {
      if (!yes) return;
      submitAction('restart');
    });
  });
  $('startBtn').addEventListener('click', function () { submitAction('start'); });
  $('stopBtn').addEventListener('click', function () {
    if (window.confirm('确定停止识别服务？所有客户端将断开连接。')) submitAction('stop');
  });

  function submitAction(kind) {
    api('POST', '/api/v1/actions/' + kind, {})
      .then(function () {
        $('actionProgress').textContent = '已提交，等待执行…';
        $('actionProgress').classList.remove('hidden');
        pollAction();
      })
      .catch(function (err) { showGlobalError('提交失败：' + err.message); });
  }

  function pollAction() {
    api('GET', '/api/v1/actions/current').then(function (data) {
      var el = $('actionProgress');
      var action = data.action;
      if (!action) { el.classList.add('hidden'); return; }
      var line = { pending: '排队中', running: action.progress || '执行中', done: '', failed: '' }[action.state] || '';
      el.textContent = action.state === 'failed'
        ? ('失败：' + (action.message || '未知原因'))
        : action.state === 'done'
          ? '操作完成'
          : (action.type + ' · ' + line);
      el.classList.remove('hidden');
    }).catch(function () {});
  }

  // ---------- 热词 ----------

  function renderHotwords(data) {
    hotwordsMtime = data.mtime;
    $('hotwordsText').value = data.text || '';
    $('hotwordsMsg').textContent = data.exists ? '' : '文件尚不存在，保存后创建。';
  }
  $('hotwordsReload').addEventListener('click', function () {
    api('GET', '/api/v1/hotwords').then(renderHotwords).catch(function (err) {
      $('hotwordsMsg').textContent = '读取失败：' + err.message;
    });
  });
  $('hotwordsSave').addEventListener('click', function () {
    var btn = $('hotwordsSave');
    btn.disabled = true;
    api('PUT', '/api/v1/hotwords', { text: $('hotwordsText').value, base_mtime: hotwordsMtime })
      .then(function (res) {
        $('hotwordsMsg').textContent = res.restart_required
          ? '已保存。热词在识别进程重启后生效。'
          : '已保存。';
      })
      .catch(function (err) {
        $('hotwordsMsg').textContent = err.status === 409
          ? '热词文件已被其他进程修改，请点击“重新读取”后再保存。'
          : ('保存失败：' + err.message);
      })
      .finally(function () { btn.disabled = false; });
  });

  // ---------- 设置 ----------

  var settingsSchema = null;
  function renderSettings(data) {
    settingsSchema = data.schema;
    var wrap = $('settingsForm');
    wrap.textContent = '';
    var grid = document.createElement('div');
    grid.className = 'fieldRow';
    Object.keys(data.schema).forEach(function (key) {
      var spec = data.schema[key];
      var value = data.values[key];
      var field = document.createElement('div');
      field.className = 'field';
      var label = document.createElement('label');
      label.className = 'small muted';
      label.textContent = spec.label;
      label.htmlFor = 'set_' + key;
      field.appendChild(label);

      var input;
      if (spec.type === 'bool') {
        input = document.createElement('select');
        [{ v: 'true', t: '开启' }, { v: 'false', t: '关闭' }].forEach(function (o) {
          var opt = document.createElement('option');
          opt.value = o.v; opt.textContent = o.t;
          if (value === (o.v === 'true')) opt.selected = true;
          input.appendChild(opt);
        });
      } else if (spec.choices) {
        input = document.createElement('select');
        spec.choices.forEach(function (c) {
          var opt = document.createElement('option');
          opt.value = c;
          opt.textContent = { qwen_asr: 'Qwen3-ASR', fun_asr_nano: 'Fun-ASR-Nano', sensevoice: 'SenseVoice', paraformer: 'Paraformer' }[c] || c;
          if (value === c) opt.selected = true;
          input.appendChild(opt);
        });
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = value === null || value === undefined ? '' : String(value);
      }
      input.id = 'set_' + key;
      field.appendChild(input);
      if (spec.help) {
        var help = document.createElement('div');
        help.className = 'fieldHelp';
        help.textContent = spec.help;
        field.appendChild(help);
      } else if (spec.restart) {
        var help = document.createElement('div');
        help.className = 'fieldHelp';
        help.textContent = '修改后需重启识别进程生效';
        field.appendChild(help);
      }
      grid.appendChild(field);
    });
    wrap.appendChild(grid);
  }

  $('settingsSave').addEventListener('click', function () {
    if (!settingsSchema) return;
    var btn = $('settingsSave');
    btn.disabled = true;
    var values = {};
    Object.keys(settingsSchema).forEach(function (key) {
      var spec = settingsSchema[key];
      var el = $('set_' + key);
      if (!el) return;
      if (spec.type === 'bool') values[key] = el.value === 'true';
      else if (spec.type === 'int') values[key] = parseInt(el.value, 10) || 0;
      else values[key] = el.value;
    });
    api('PUT', '/api/v1/settings', values)
      .then(function (res) {
        $('settingsMsg').textContent = res.restart_required && res.restart_required.length
          ? '已保存。以下修改需重启识别进程生效：' + res.restart_required.join(', ')
          : '已保存，立即生效。';
      })
      .catch(function (err) { $('settingsMsg').textContent = '保存失败：' + err.message; })
      .finally(function () { btn.disabled = false; });
  });

  // ---------- 日志 ----------

  function pollLogs() {
    api('GET', '/api/v1/logs?cursor=' + logCursor)
      .then(function (data) {
        var view = $('logView');
        if (logCursor === 0) view.textContent = '';
        if (data.lines && data.lines.length) {
          // textContent 追加，天然转义
          view.textContent += data.lines.join('\n') + '\n';
          if (view.textContent.length > 200000) {
            view.textContent = view.textContent.slice(-150000);
          }
        }
        logCursor = data.next_cursor;
        if (data.truncated) view.textContent += '…（部分行已省略）\n';
        view.scrollTop = view.scrollHeight;
      })
      .catch(function () {});
  }

  $('logPause').addEventListener('click', function () {
    logPaused = !logPaused;
    $('logPause').textContent = logPaused ? '继续' : '暂停';
    if (!logPaused) pollLogs();
  });

  // ---------- 审计 ----------

  function renderAudit(data) {
    var view = $('auditView');
    var entries = data.entries || [];
    if (!entries.length) { view.textContent = '（暂无记录）'; return; }
    view.textContent = entries.map(function (e) {
      return e.time + '  ' + (e.ok ? '✓' : '✗') + '  ' + e.action + '  ' + (e.ip || '') + (e.detail ? '  ' + e.detail : '');
    }).join('\n');
  }

  applyTheme();
  // 启动：向服务端询问认证模式
  api('GET', '/api/v1/bootstrap', undefined, { noRedirect: true })
    .then(function (data) {
      authMode = data.mode || 'password';
      if (authMode === 'gateway') {
        if (data.authenticated && data.csrf) {
          csrf = data.csrf;
          showMain();
        } else {
          showGatewayNotice();
        }
      } else if (data.authenticated) {
        showMain();
      } else {
        showLogin();
      }
    })
    .catch(function () { showLogin(); });
})();
