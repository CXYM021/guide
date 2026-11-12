
(function () {
  var mask = document.getElementById('auth-mask');
  var err = document.getElementById('auth-error');
  var mode = 'login';
  function show(msg) { mask.style.display = 'flex'; if (msg) err.textContent = msg; }
  function hide() { mask.style.display = 'none'; err.textContent = ''; }
  function setMode(m) {
    mode = m;
    document.getElementById('auth-title').textContent = m === 'login' ? '登录' : '注册';
    document.getElementById('auth-submit').textContent = m === 'login' ? '登 录' : '注 册';
    document.getElementById('auth-switch').textContent = m === 'login'
      ? '没有账号？注册一个' : '已有账号？去登录';
    document.getElementById('auth-invite-wrap').style.display = m === 'login' ? 'none' : 'block';
    document.getElementById('auth-reset').style.display = m === 'login' ? '' : 'none';
    err.textContent = '';
  }
  window.__showAuth = show;
  var origFetch = window.fetch;
  window.fetch = function () {
    var url = String(arguments[0]);
    return origFetch.apply(this, arguments).then(function (r) {
      if (r.status === 401 && url.indexOf('/api/auth/') < 0) show('会话已过期，请重新登录');
      return r;
    });
  };
  function apiPost(path, body) {
    return fetch(path, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
          return d;
        });
      });
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function copyText(text, btn) {
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      btn.textContent = ok ? '已复制' : '请长按地址手动复制';
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = '已复制';
      }).catch(fallback);
    } else { fallback(); }
  }
  function addrRow(url, label) {
    return '<div style="margin:8px 0;padding:9px 10px;background:#f5f6fb;' +
      'border:1px solid #e6e8f2;border-radius:10px">' +
      '<div style="font-size:10.5px;color:#7d8496">' + esc(label) + '</div>' +
      '<div style="display:flex;gap:8px;align-items:center;margin-top:3px">' +
      '<code style="flex:1;word-break:break-all;font-size:12px;color:#3f3a8f">' +
      esc(url) + '</code>' +
      '<button data-copy="' + esc(url) + '" style="border:1px solid #c9c6e8;' +
      'background:#fff;color:#3f3a8f;border-radius:8px;padding:4px 9px;' +
      'font-size:11px;cursor:pointer;white-space:nowrap">复制</button></div></div>';
  }
  function openAccess() {
    var box = document.getElementById('access-body');
    box.textContent = '读取中…';
    document.getElementById('access-mask').style.display = 'flex';
    fetch('/api/access', { credentials: 'same-origin' }).then(function (r) {
      return r.json();
    }).then(function (d) {
      var h = addrRow(window.location.origin + '/', '当前这台设备正在用的地址');
      if (d.lan_reachable_now && d.lan_urls && d.lan_urls.length) {
        d.lan_urls.forEach(function (u) { h += addrRow(u, '同一 WiFi 的手机 / 他人电脑'); });
      } else if (!d.listening_on_all) {
        h += '<div style="margin:10px 0;padding:10px;background:#fff6e8;' +
          'border:1px solid #f0d9a8;border-radius:10px;color:#8a5a00">' +
          '服务现在只监听 127.0.0.1，别人打不开。' +
          esc(d.enable_lan_hint || '请在启动配置里改为对外监听后重启。') +
          '</div>';
      } else if (d.public_mode) {
        h += '<div style="margin:10px 0;font-size:12px;color:#7d8496">' +
          '公网模式：把浏览器地址栏里的域名发给对方即可；内网地址不在此展示。</div>';
      }
      h += '<ul style="padding-left:18px;margin:12px 0;color:#555c6e;font-size:12px">';
      (d.notes || []).forEach(function (n) { h += '<li>' + esc(n) + '</li>'; });
      h += '</ul>';
      h += '<div style="font-size:11.5px;color:#7d8496;border-top:1px solid #eee;' +
        'padding-top:10px">' +
        (d.pwa_installable
          ? '此地址可安装到手机主屏（浏览器菜单里的「安装应用 / 添加到主屏」）。'
          : 'HTTP 局域网下浏览器不会启用 Service Worker：可「添加到主屏」存快捷方式，但不能离线安装为 PWA。') +
        '<br>没有二维码：本机没有可用的二维码解码器，无法验证生成结果，' +
        '所以只提供可复制的准确地址。</div>';
      box.innerHTML = h;
      Array.prototype.forEach.call(box.querySelectorAll('[data-copy]'), function (b) {
        b.onclick = function () { copyText(b.dataset.copy, b); };
      });
    }).catch(function (e) {
      box.textContent = '读取失败：' + e.message;
    });
  }
  document.getElementById('access-close').onclick = function () {
    document.getElementById('access-mask').style.display = 'none';
  };
  document.getElementById('access-mask').onclick = function (e) {
    if (e.target === this) this.style.display = 'none';
  };
  document.getElementById('auth-switch').onclick = function (e) {
    e.preventDefault(); setMode(mode === 'login' ? 'register' : 'login');
  };
  document.getElementById('auth-submit').onclick = function () {
    var u = document.getElementById('auth-user').value.trim();
    var p = document.getElementById('auth-pass').value;
    var path = mode === 'login' ? '/api/auth/login' : '/api/auth/register';
    var body = { username: u, password: p };
    if (mode === 'register') body.invite_code = document.getElementById('auth-invite').value.trim();
    err.textContent = '';
    apiPost(path, body).then(function () {
      hide();
      // 注册成功后回到登录模式；否则退出登录时遮罩还停在「注册」，
      // 用户用同一组凭据点按钮会走注册接口报「用户名已被注册」。
      setMode('login');
      document.getElementById('auth-pass').value = '';
      if (typeof refresh === 'function') refresh();
      updateUserBar();
    }).catch(function (e) { err.textContent = e.message; });
  };
  document.getElementById('auth-pass').onkeydown = function (e) {
    if (e.key === 'Enter') document.getElementById('auth-submit').click();
  };
  function updateUserBar() {
    return fetch('/api/auth/me', { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (d) {
      var bar = document.querySelector('.statusbar');
      var old = document.getElementById('logout-btn');
      var oldAccess = document.getElementById('access-btn');
      if (old) old.remove();
      if (oldAccess) oldAccess.remove();
      if (!d || !d.ok || !bar) return;
      var a = document.createElement('button');
      a.id = 'access-btn';
      a.textContent = '他人访问';
      a.style.cssText = 'border:0;background:none;color:#3f3a8f;font-size:10.5px;cursor:pointer;padding:0;margin-right:10px';
      a.onclick = openAccess;
      bar.appendChild(a);
      var b = document.createElement('button');
      b.id = 'logout-btn';
      b.textContent = d.user.username + ' · 退出';
      b.style.cssText = 'border:0;background:none;color:#7d8496;font-size:10.5px;cursor:pointer;padding:0';
      b.onclick = function () {
        apiPost('/api/auth/logout', {}).then(function () {
          setMode('login');
          document.getElementById('auth-pass').value = '';
          show('已退出登录');
          document.getElementById('auth-user').focus();
        });
      };
      bar.appendChild(b);
    });
  }
  fetch('/api/auth/me', { credentials: 'same-origin' }).then(function (r) {
    if (r.ok) { hide(); if (typeof refresh === 'function') refresh(); updateUserBar(); }
    else show('');
  }).catch(function () { show('无法连接服务器'); });

  // Service Worker 只在安全上下文生效；局域网 HTTP 下不注册，避免静默失败。
  if ('serviceWorker' in navigator &&
      (location.protocol === 'https:' || location.hostname === 'localhost' ||
       location.hostname === '127.0.0.1')) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }
})();
