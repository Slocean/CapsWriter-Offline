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
  function fmtBytesDecimal(n) {
    // 模型管理区专用：十进制单位（1 GB = 1,000,000,000 字节），
    // 与整套 5GB = 5,000,000,000 字节的硬上限同一口径，不混用 2^30 的 GiB
    if (n === null || n === undefined) return '—';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + ' KB';
    return n + ' B';
  }
  function fmtInt(n) {
    // 千位分隔（十进制字节数原值展示）
    return Number(n).toLocaleString('en-US');
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
    api('GET', '/api/v1/models').then(renderModels).catch(function () {});
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
      if ($('modelsCard').open) pollModels();
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
    var detailLine;
    if (asr.hint) {
      detailLine = asr.hint;
    } else if (state === 'online') {
      detailLine = d.worker_alive === false ? '识别子进程未运行' : '控制通道正常，端口 ' + d.port;
    } else if (state === 'stopped') {
      detailLine = '识别进程未运行';
    } else if (state === 'stale') {
      detailLine = '控制通道不可达，仅按 PID/端口监控';
    } else {
      detailLine = '—';
    }
    $('asrStateDetail').textContent = detailLine;
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

    // R03：启动与停止是不同的能力，按后端 can_start / can_stop 分开判断——
    // 已安全停止的新版必须允许启动；旧版（不可核身）保持全部禁用，
    // 避免误报“已停止”或拉起第二个模型
    $('startBtn').disabled = !asr.can_start;
    $('stopBtn').disabled = !asr.can_stop || state === 'stopped';
    $('restartBtn').disabled = !asr.can_restart || state === 'stopped';
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
        var msg = res.restart_required && res.restart_required.length
          ? '已保存。以下修改需重启识别进程生效：' + res.restart_required.join(', ')
          : '已保存。';
        if (res.notice) msg = msg + ' ' + res.notice;   // A05：生效范围如实说明
        $('settingsMsg').textContent = msg;
      })
      .catch(function (err) { $('settingsMsg').textContent = '保存失败：' + err.message; })
      .finally(function () { btn.disabled = false; });
  });

  // ---------- 语音模型管理 ----------

  var modelsData = null;

  function pollModels() {
    api('GET', '/api/v1/models').then(function (data) {
      renderModels(data);
    }).catch(function () {});
  }

  function modelStatusChips(m) {
    var chips = [];
    if (m.running) chips.push(['err', '运行中']);
    if (m.selected) chips.push(['accent', '已选']);
    if (m.installed_variant) chips.push(['chip', m.installed_variant]);
    if (m.complete) chips.push(['ok', '整套完整']);
    else if (m.main_ready) {
      chips.push(['ok', '主模型就绪']);
      var missing = (m.missing_components || []).length;
      if (missing > 0) chips.push(['warn', '缺 ' + missing + ' 个组件，可补下载']);
    } else if (m.size_bytes > 0) chips.push(['warn', '不完整']);
    else chips.push(['chip', '未安装']);
    return chips;
  }

  function makeChip(kindText) {
    var span = document.createElement('span');
    span.className = 'chip ' + (kindText[0] === 'chip' ? '' : kindText[0]);
    span.textContent = kindText[1];
    return span;
  }

  function renderModels(data) {
    modelsData = data;
    renderModelsTask(data.task);
    var wrap = $('modelsTable');
    wrap.textContent = '';
    var table = document.createElement('table');
    table.className = 'modelsTable';
    var thead = document.createElement('thead');
    var hr = document.createElement('tr');
    ['模型', '状态', '整套大小', '操作'].forEach(function (t) {
      var th = document.createElement('th'); th.textContent = t; hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    var tbody = document.createElement('tbody');
    (data.models || []).forEach(function (m) {
      var tr = document.createElement('tr');

      var tdName = document.createElement('td');
      var nameLine = document.createElement('div');
      nameLine.className = 'modelName';
      nameLine.textContent = m.label;
      tdName.appendChild(nameLine);
      var desc = document.createElement('div');
      desc.className = 'modelDesc';
      desc.textContent = m.description;
      tdName.appendChild(desc);
      // 组件用途与官方资产文件名：折叠进 <details>（导语承诺"资产文件名
      // 展开查看"），模型能力描述与状态保持常显
      if (m.components.length > 0) {
        var assets = document.createElement('details');
        assets.className = 'modelAssets';
        var sum = document.createElement('summary');
        sum.textContent = '组件与资产文件（' + m.components.length + '）';
        assets.appendChild(sum);
        m.components.forEach(function (c) {
          var line = document.createElement('div');
          line.className = 'modelAssetLine';
          var prefix = c.kind === 'aux' ? '辅助 · ' : '主模型 · ';
          line.textContent = prefix + c.purpose
            + '（' + c.asset.name + '，' + fmtBytesDecimal(c.asset.size) + '）';
          assets.appendChild(line);
        });
        tdName.appendChild(assets);
      }
      tr.appendChild(tdName);

      var tdState = document.createElement('td');
      modelStatusChips(m).forEach(function (c) { tdState.appendChild(makeChip(c)); });
      m.components.forEach(function (c) {
        var line = document.createElement('div');
        line.className = 'modelDesc';
        line.textContent = (c.kind === 'aux' ? '辅助' : '主模型') + '：'
          + (c.installed ? '已安装' : (c.exists ? '不完整（缺 ' + c.required_missing.length + ' 个必需文件）' : '未安装'));
        tdState.appendChild(line);
      });
      tr.appendChild(tdState);

      var tdSize = document.createElement('td');
      tdSize.textContent = m.size_bytes > 0 ? fmtBytesDecimal(m.size_bytes) : '—';
      if (m.size_bytes > 0) {
        var cap = document.createElement('div');
        cap.className = 'modelDesc';
        // 上限与大小同一十进制口径，并标明字节数原值（5 GB = 5,000,000,000 字节）
        cap.textContent = '上限 ' + fmtBytesDecimal(m.cap_bytes)
          + '（' + fmtInt(m.cap_bytes) + ' 字节）';
        tdSize.appendChild(cap);
      }
      tr.appendChild(tdSize);

      var tdBtn = document.createElement('td');
      tdBtn.className = 'btnCol';
      var dlBtn = document.createElement('button');
      dlBtn.className = 'ghost';
      dlBtn.textContent = '下载';
      if (m.download_block) { dlBtn.disabled = true; dlBtn.title = m.download_block; }
      dlBtn.addEventListener('click', function () { startModelDownload(m); });
      tdBtn.appendChild(dlBtn);
      var delBtn = document.createElement('button');
      delBtn.className = 'dangerGhost';
      delBtn.textContent = '删除';
      if (m.delete_block) { delBtn.disabled = true; delBtn.title = m.delete_block; }
      delBtn.addEventListener('click', function () { confirmDeleteModel(m); });
      tdBtn.appendChild(delBtn);
      tr.appendChild(tdBtn);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  function renderModelsTask(task) {
    var box = $('modelsTask');
    if (!task || ['done', 'failed', 'cancelled'].indexOf(task.state) >= 0) {
      if (task && task.state !== 'done') {
        // 结束态短暂展示结果，之后随下次轮询消失
        box.classList.remove('hidden');
        $('modelsTaskText').textContent = task.state === 'failed'
          ? '下载失败：' + (task.error || '未知原因')
          : '下载已取消';
        $('modelsTaskBar').value = 0;
        $('modelsTaskDetail').textContent = '';
        $('modelsTaskCancel').classList.add('hidden');
        return;
      }
      box.classList.add('hidden');
      return;
    }
    box.classList.remove('hidden');
    $('modelsTaskCancel').classList.remove('hidden');
    var label = { pending: '排队中', running: task.stage || '执行中' }[task.state] || task.state;
    $('modelsTaskText').textContent = '下载 ' + (task.label || task.model) + '：' + label;
    var total = task.bytes_total || 0;
    var done = task.bytes_received || 0;
    var bar = $('modelsTaskBar');
    bar.max = total > 0 ? total : 1;
    bar.value = done;
    $('modelsTaskDetail').textContent = total > 0
      ? ('组件 ' + ((task.asset_index || 0) + 1) + '/' + task.asset_count + ' · '
         + fmtBytesDecimal(done) + ' / ' + fmtBytesDecimal(total)
         + '（' + fmtInt(done) + ' / ' + fmtInt(total) + ' 字节）')
      : '';
  }

  $('modelsTaskCancel').addEventListener('click', function () {
    api('POST', '/api/v1/models/task/cancel', {})
      .then(function () { $('modelsMsg').textContent = '已请求取消，等待任务退出…'; pollModels(); })
      .catch(function (err) { $('modelsMsg').textContent = '取消失败：' + err.message; });
  });

  function startModelDownload(m) {
    api('POST', '/api/v1/models/download', { model: m.key })
      .then(function () {
        $('modelsMsg').textContent = '已开始下载 ' + m.label + '。';
        pollModels();
      })
      .catch(function (err) {
        $('modelsMsg').textContent = '下载被拒绝：' + err.message;
        pollModels();
      });
  }

  function confirmDeleteModel(m) {
    var lines = ['确定删除 ' + m.label + '？'];
    if (m.running) lines.push('警告：它正在运行，服务端也会拒绝。');
    if (m.selected) lines.push('注意：它是当前设置选中的模型，删除后启动识别会失败；建议先切换到其他已安装模型。');
    if (m.variant) lines.push('q4_k 与 q5_k 共享运行目录，删除会同时移除已安装的量化变体与对齐辅助模型。');
    lines.push('仅删除该模型的固定目录，不影响其他模型。');
    if (!window.confirm(lines.join('\n'))) return;
    api('POST', '/api/v1/models/delete', { model: m.key, confirm: true })
      .then(function (res) {
        $('modelsMsg').textContent = '已删除 ' + m.label + (res.note || '');
        pollModels();
      })
      .catch(function (err) {
        $('modelsMsg').textContent = '删除被拒绝：' + err.message;
        pollModels();
      });
  }

  // ---------- 日志 ----------

  function pollLogs() {
    if (logPaused) return;   // R14：暂停时不发日志请求、游标保持不变
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
        // R13：password 模式恢复已有会话时一并取回 CSRF，刷新后写操作可用
        if (data.csrf) csrf = data.csrf;
        showMain();
      } else {
        showLogin();
      }
    })
    .catch(function () { showLogin(); });
})();
