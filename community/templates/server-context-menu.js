/*!
 * AlexiHub — контекстне меню сервера по правому кліку на іконці в рейці (.server-rail)
 * Підключення: <script src="/static/js/server-context-menu.js" defer></script>
 * (або вставити цей файл прямо перед </body>)
 *
 * Нічого додатково робити не треба: скрипт сам вішає обробник contextmenu
 * на кожну .server-dot[data-mention-server-id] у .server-rail.
 */
(function () {
  'use strict';
  if (window.__ahServerCtxMenuInit) return;
  window.__ahServerCtxMenuInit = true;

  /* ---------- helpers ---------- */

  function t(key, fallback) {
    try {
      if (window.AlexiHubI18n && typeof window.AlexiHubI18n.t === 'function') {
        var v = window.AlexiHubI18n.t(key);
        if (v && v !== key) return v;
      }
    } catch (e) {}
    return fallback;
  }

  function toast(msg) {
    if (typeof window.showActionToast === 'function') { window.showActionToast(msg); return; }
    if (typeof window.settingsToast === 'function') { window.settingsToast(msg); return; }
    var n = document.createElement('div');
    n.textContent = msg;
    n.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483647;background:#232428;color:#fff;border:1px solid #3b3d45;border-radius:10px;padding:9px 12px;font:700 12px Inter,system-ui;box-shadow:0 12px 34px rgba(0,0,0,.4)';
    document.body.appendChild(n);
    setTimeout(function () { n.remove(); }, 2200);
  }

  async function api(url, options) {
    var res = await fetch(url, Object.assign({
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' }
    }, options || {}));
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok || data.ok === false) {
      var err = new Error(data.message || data.error || 'request_failed');
      throw err;
    }
    return data;
  }

  function activeServerId() {
    var activeDot = document.querySelector('.server-dot.active[data-mention-server-id]');
    if (activeDot) return activeDot.getAttribute('data-mention-server-id');
    var m = location.pathname.match(/\/community\/servers\/(\d+)/);
    return m ? m[1] : null;
  }

  function serverName(dot) {
    return dot.getAttribute('title') || dot.getAttribute('data-server-name') || t('common.text.the_server', 'сервер');
  }

  var PREF_KEY = 'ah_server_ctx_prefs';
  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch (e) { return {}; }
  }
  function savePrefs(p) {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch (e) {}
  }
  function serverPrefs(id) {
    var all = loadPrefs();
    if (!all[id]) all[id] = { notif: 'mentions', ignoreEveryone: false, ignoreRoles: false, muteEvents: false, muteImportant: false, pushMobile: true, muteUntil: 0, hideMuted: false, showAll: true };
    return all[id];
  }
  function setServerPrefs(id, patch) {
    var all = loadPrefs();
    all[id] = Object.assign(serverPrefs(id), patch);
    savePrefs(all);
    return all[id];
  }

  /* ---------- styles ---------- */

  var css = ''
    + '.ahctx-menu{position:fixed;z-index:99999;min-width:264px;max-width:300px;background:#111214;border:1px solid #2b2d31;border-radius:10px;padding:6px;box-shadow:0 18px 50px rgba(0,0,0,.5);font-family:Inter,system-ui,sans-serif;color:#dbdee1;font-size:13.5px;display:none;user-select:none;}'
    + '.ahctx-menu.open{display:block;animation:ahctxPop .12s ease-out;}'
    + '@keyframes ahctxPop{from{opacity:0;transform:scale(.97)}to{opacity:1;transform:scale(1)}}'
    + '.ahctx-item{position:relative;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 8px;border-radius:5px;cursor:pointer;font-weight:600;line-height:1.15;}'
    + '.ahctx-item:hover,.ahctx-item.open-sub{background:#4752c4;color:#fff;}'
    + '.ahctx-item.danger{color:#f23f42;}'
    + '.ahctx-item.danger:hover{background:#da373c;color:#fff;}'
    + '.ahctx-item.disabled{opacity:.42;pointer-events:none;}'
    + '.ahctx-item .ahctx-label{display:flex;flex-direction:column;min-width:0;}'
    + '.ahctx-item .ahctx-sub{font-size:11px;font-weight:600;color:#949ba4;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}'
    + '.ahctx-item:hover .ahctx-sub,.ahctx-item.open-sub .ahctx-sub{color:rgba(255,255,255,.75);}'
    + '.ahctx-item .ahctx-right{display:flex;align-items:center;gap:6px;flex:0 0 auto;color:#949ba4;}'
    + '.ahctx-item:hover .ahctx-right,.ahctx-item.open-sub .ahctx-right{color:#fff;}'
    + '.ahctx-chevron{width:14px;height:14px;}'
    + '.ahctx-check{width:16px;height:16px;border-radius:4px;border:2px solid #4e5058;display:flex;align-items:center;justify-content:center;flex:0 0 auto;}'
    + '.ahctx-check.checked{background:#5865f2;border-color:#5865f2;}'
    + '.ahctx-check svg{width:11px;height:11px;display:none;}'
    + '.ahctx-check.checked svg{display:block;}'
    + '.ahctx-radio{width:16px;height:16px;border-radius:50%;border:2px solid #4e5058;display:flex;align-items:center;justify-content:center;flex:0 0 auto;}'
    + '.ahctx-radio.checked{border-color:#5865f2;}'
    + '.ahctx-radio.checked::after{content:"";width:8px;height:8px;border-radius:50%;background:#5865f2;}'
    + '.ahctx-idbadge{min-width:20px;height:16px;padding:0 3px;border-radius:4px;background:#4e5058;color:#dbdee1;font-size:10px;font-weight:950;display:inline-flex;align-items:center;justify-content:center;}'
    + '.ahctx-item:hover .ahctx-idbadge,.ahctx-item.open-sub .ahctx-idbadge{background:rgba(255,255,255,.25);color:#fff;}'
    + '.ahctx-divider{height:1px;background:rgba(255,255,255,.08);margin:6px 4px;}'
    + '.ahctx-sep-label{padding:6px 8px 2px;font-size:11px;font-weight:800;letter-spacing:.02em;color:#949ba4;text-transform:uppercase;}'
    + '.ahctx-sub-panel{position:fixed;z-index:100000;min-width:230px;max-width:290px;background:#111214;border:1px solid #2b2d31;border-radius:10px;padding:6px;box-shadow:0 18px 50px rgba(0,0,0,.5);font-family:Inter,system-ui,sans-serif;color:#dbdee1;font-size:13.5px;display:none;}'
    + '.ahctx-sub-panel.open{display:block;animation:ahctxPop .12s ease-out;}'
    + '.ahctx-sub-panel .ahctx-item{cursor:pointer;}'
    + '.ahctx-sub-panel .ahctx-item:hover{background:#3b3d45;color:#fff;}'
    + '.ahctx-mute-badge{position:absolute;right:-1px;bottom:-1px;width:16px;height:16px;border-radius:50%;background:#2b2d31;border:2px solid #070707;display:flex;align-items:center;justify-content:center;color:#949ba4;pointer-events:none;}'
    + '.ahctx-mute-badge svg{width:9px;height:9px;}';

  var styleTag = document.createElement('style');
  styleTag.setAttribute('data-ahctx', '1');
  styleTag.textContent = css;
  document.head.appendChild(styleTag);

  /* ---------- icons ---------- */

  var ICON = {
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 12 9 17 20 6"/></svg>',
    chevron: '<svg class="ahctx-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></svg>',
    muteBell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/></svg>'
  };

  /* ---------- DOM: main menu + submenu ---------- */

  var menu = document.createElement('div');
  menu.className = 'ahctx-menu';
  menu.setAttribute('role', 'menu');
  document.body.appendChild(menu);

  var sub = document.createElement('div');
  sub.className = 'ahctx-sub-panel';
  document.body.appendChild(sub);

  var state = { serverId: null, serverName: '', prefs: null, subFor: null, subCloseTimer: null };

  function closeSub() {
    sub.classList.remove('open');
    sub.innerHTML = '';
    state.subFor = null;
    menu.querySelectorAll('.open-sub').forEach(function (n) { n.classList.remove('open-sub'); });
  }

  function closeMenu() {
    menu.classList.remove('open');
    menu.innerHTML = '';
    closeSub();
    state.serverId = null;
  }

  function placeFixed(el, x, y) {
    el.style.left = '0px'; el.style.top = '0px'; el.style.visibility = 'hidden'; el.classList.add('open');
    var r = el.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;
    if (x + r.width > vw - 8) x = vw - r.width - 8;
    if (x < 8) x = 8;
    if (y + r.height > vh - 8) y = vh - r.height - 8;
    if (y < 8) y = 8;
    el.style.left = x + 'px'; el.style.top = y + 'px'; el.style.visibility = '';
  }

  function openSubPanel(anchorEl, html, id) {
    if (state.subFor === id) return;
    sub.innerHTML = html;
    state.subFor = id;
    var r = anchorEl.getBoundingClientRect();
    sub.style.display = 'block';
    placeFixed(sub, r.right + 4, r.top - 6);
    menu.querySelectorAll('.open-sub').forEach(function (n) { n.classList.remove('open-sub'); });
    anchorEl.classList.add('open-sub');
  }

  /* ---------- item builder ---------- */

  function makeItem(opts) {
    // opts: {label, sub, icon, danger, disabled, id, onClick, hasSubmenu, right}
    var el = document.createElement('div');
    el.className = 'ahctx-item' + (opts.danger ? ' danger' : '') + (opts.disabled ? ' disabled' : '');
    el.setAttribute('role', 'menuitem');
    var labelWrap = document.createElement('div');
    labelWrap.className = 'ahctx-label';
    var l = document.createElement('span');
    l.textContent = opts.label;
    labelWrap.appendChild(l);
    if (opts.sub) {
      var s = document.createElement('span');
      s.className = 'ahctx-sub';
      s.textContent = opts.sub;
      labelWrap.appendChild(s);
    }
    el.appendChild(labelWrap);
    var right = document.createElement('div');
    right.className = 'ahctx-right';
    if (opts.right) right.innerHTML = opts.right;
    else if (opts.hasSubmenu) right.innerHTML = ICON.chevron;
    el.appendChild(right);
    if (opts.onClick) el.addEventListener('click', function (e) { e.stopPropagation(); opts.onClick(e); });
    return el;
  }

  function checkBox(checked) {
    return '<span class="ahctx-check' + (checked ? ' checked' : '') + '">' + ICON.check + '</span>';
  }
  function radioDot(checked) {
    return '<span class="ahctx-radio' + (checked ? ' checked' : '') + '"></span>';
  }

  /* ---------- actions ---------- */

  function markServerRead(id, dot) {
    api('/community/api/inbox/read', { method: 'POST', body: JSON.stringify({ server_id: Number(id) }) })
      .then(function (data) {
        if (data.counts && window.AlexiHubAccountRealtime && typeof window.AlexiHubAccountRealtime.applyMentionCounts === 'function') {
          window.AlexiHubAccountRealtime.applyMentionCounts(data.counts);
        } else if (dot) {
          var badge = dot.querySelector('.server-mention-badge');
          if (badge) badge.hidden = true;
        }
        toast(t('server.ctx.marked_read', 'Помічено як прочитане'));
      })
      .catch(function () { toast(t('server.ctx.mark_read_failed', 'Не вдалося позначити прочитаним')); });
    closeMenu();
  }

  function openInvite(id, isActive) {
    closeMenu();
    if (isActive && typeof window.openServerInviteModal === 'function') {
      window.openServerInviteModal({ stopPropagation: function () {} });
      return;
    }
    // Легка резервна форма запрошення для будь-якого сервера в рейці —
    // використовує той самий контракт API, що й вбудована форма запрошень.
    var backdrop = document.createElement('div');
    backdrop.style.cssText = 'position:fixed;inset:0;z-index:100050;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;';
    var card = document.createElement('div');
    card.style.cssText = 'width:340px;background:#1e1f24;border:1px solid #313338;border-radius:14px;padding:18px;box-shadow:0 30px 90px rgba(0,0,0,.6);';
    card.innerHTML =
      '<div style="font-weight:900;font-size:16px;color:#fff;margin-bottom:10px;">' + t('server.ctx.invite_title', 'Запросити на сервер') + '</div>' +
      '<input type="text" placeholder="' + t('common.placeholder.friend_s_username', 'Ім’я користувача') + '" style="width:100%;height:38px;border-radius:8px;border:1px solid #3b3d45;background:#111214;color:#fff;padding:0 12px;outline:none;font:inherit;">' +
      '<div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end;">' +
      '<button type="button" data-act="cancel" style="border:0;border-radius:8px;background:#4e5058;color:#fff;font-weight:800;padding:9px 14px;cursor:pointer;">' + t('common.actions.cancel', 'Скасувати') + '</button>' +
      '<button type="button" data-act="send" style="border:0;border-radius:8px;background:#5865f2;color:#fff;font-weight:800;padding:9px 14px;cursor:pointer;">' + t('common.aria.yuncture', 'Надіслати') + '</button>' +
      '</div>';
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    var input = card.querySelector('input');
    input.focus();
    function close() { backdrop.remove(); }
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) close(); });
    card.querySelector('[data-act="cancel"]').addEventListener('click', close);
    card.querySelector('[data-act="send"]').addEventListener('click', function () {
      var username = input.value.trim();
      if (!username) { input.focus(); return; }
      var body = new FormData();
      body.append('username', username);
      body.append('redirect_to', location.pathname);
      fetch('/community/servers/' + id + '/invite', { method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json' }, body: body })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (data) {
          close();
          toast(data && data.ok ? t('server.ctx.invite_sent', 'Запрошення надіслано') : (data && data.message) || t('server.ctx.invite_failed', 'Не вдалося надіслати запрошення'));
        })
        .catch(function () { close(); toast(t('server.ctx.invite_failed', 'Не вдалося надіслати запрошення')); });
    });
  }

  function leaveServer(id, isActive) {
    closeMenu();
    if (isActive && typeof window.openModal === 'function' && document.getElementById('leave-server-modal')) {
      window.openModal('leave-server-modal');
      return;
    }
    if (!confirm(t('server.ctx.leave_confirm', 'Покинути «' + state.serverName + '»?'))) return;
    var form = document.createElement('form');
    form.method = 'post';
    form.action = '/community/servers/' + id + '/leave';
    form.style.display = 'none';
    document.body.appendChild(form);
    form.submit();
  }

  function copyServerId(id, dot) {
    var val = String(id);
    var done = function () { toast(t('common.feedback.copied', 'Скопійовано')); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(val).then(done).catch(function () { fallbackCopy(val, done); });
    } else {
      fallbackCopy(val, done);
    }
    closeMenu();
  }
  function fallbackCopy(val, done) {
    var i = document.createElement('input');
    i.value = val; document.body.appendChild(i); i.select();
    try { document.execCommand('copy'); } catch (e) {}
    i.remove(); done();
  }

  function setMuteBadge(dot, muted) {
    var existing = dot.querySelector('.ahctx-mute-badge');
    if (muted && !existing) {
      var b = document.createElement('span');
      b.className = 'ahctx-mute-badge';
      b.innerHTML = ICON.muteBell;
      dot.appendChild(b);
    } else if (!muted && existing) {
      existing.remove();
    }
  }

  /* ---------- submenus ---------- */

  var MUTE_OPTIONS = [
    { key: '15m', label: t('server.ctx.mute_15m', 'На 15 хвилин'), ms: 15 * 60 * 1000 },
    { key: '1h', label: t('server.ctx.mute_1h', 'На 1 годину'), ms: 60 * 60 * 1000 },
    { key: '3h', label: t('server.ctx.mute_3h', 'На 3 години'), ms: 3 * 60 * 60 * 1000 },
    { key: '8h', label: t('server.ctx.mute_8h', 'На 8 годин'), ms: 8 * 60 * 60 * 1000 },
    { key: '24h', label: t('server.ctx.mute_24h', 'На 24 години'), ms: 24 * 60 * 60 * 1000 },
    { key: 'always', label: t('server.ctx.mute_always', 'Поки не увімкну знову'), ms: 0 }
  ];

  function buildMuteSubmenu(id, dot) {
    var panel = document.createElement('div');
    MUTE_OPTIONS.forEach(function (opt) {
      var it = makeItem({ label: opt.label });
      it.addEventListener('click', function () {
        var until = opt.ms ? Date.now() + opt.ms : -1;
        setServerPrefs(id, { muteUntil: until });
        setMuteBadge(dot, true);
        toast(t('server.ctx.muted', 'Сервер заглушено') + ' — ' + opt.label.toLowerCase());
        closeMenu();
      });
      panel.appendChild(it);
    });
    return panel.innerHTML;
  }

  function buildNotifSubmenu(id, dot) {
    var p = serverPrefs(id);
    var panel = document.createElement('div');

    [['all', t('server.ctx.notif_all', 'Всі повідомлення')],
     ['mentions', t('server.ctx.notif_mentions', 'Тільки @згадування')],
     ['none', t('server.ctx.notif_none', 'Нічого')]].forEach(function (pair) {
      var it = makeItem({ label: pair[1], right: radioDot(p.notif === pair[0]) });
      it.addEventListener('click', function () {
        p = setServerPrefs(id, { notif: pair[0] });
        renderMenuFor(state.serverId, dot); // refresh subtitle in main menu
        sub.innerHTML = buildNotifSubmenu(id, dot);
        toast(t('server.ctx.notif_saved', 'Налаштування сповіщень збережено'));
      });
      panel.appendChild(it);
    });

    var divider = document.createElement('div'); divider.className = 'ahctx-divider'; panel.appendChild(divider);

    [
      ['ignoreEveryone', t('server.ctx.ignore_everyone', 'Ігнорувати @everyone та @here')],
      ['ignoreRoles', t('server.ctx.ignore_roles', 'Вимкнути всі @згадування ролей')],
      ['muteImportant', t('server.ctx.mute_important', 'Вимкнути сповіщення про важливі події')],
      ['muteEvents', t('server.ctx.mute_events', 'Заглушити нові події')]
    ].forEach(function (pair) {
      var it = makeItem({ label: pair[1], right: checkBox(!!p[pair[0]]) });
      it.addEventListener('click', function () {
        var patch = {}; patch[pair[0]] = !p[pair[0]];
        p = setServerPrefs(id, patch);
        sub.innerHTML = buildNotifSubmenu(id, dot);
      });
      panel.appendChild(it);
    });

    var divider2 = document.createElement('div'); divider2.className = 'ahctx-divider'; panel.appendChild(divider2);

    var pushIt = makeItem({ label: t('server.ctx.push_mobile', 'Мобільні Push-сповіщення'), right: checkBox(!!p.pushMobile) });
    pushIt.addEventListener('click', function () {
      p = setServerPrefs(id, { pushMobile: !p.pushMobile });
      sub.innerHTML = buildNotifSubmenu(id, dot);
    });
    panel.appendChild(pushIt);

    return panel.innerHTML;
  }

  function notifSubtitle(p) {
    if (p.notif === 'all') return t('server.ctx.notif_all', 'Всі повідомлення');
    if (p.notif === 'none') return t('server.ctx.notif_none', 'Нічого');
    return t('server.ctx.notif_mentions', 'Тільки @згадування');
  }

  /* ---------- main render ---------- */

  function renderMenuFor(id, dot) {
    var p = serverPrefs(id);
    var isActive = String(id) === String(activeServerId());
    var unreadBadge = dot.querySelector('.server-mention-badge');
    var hasUnread = !!(unreadBadge && !unreadBadge.hidden);
    var isMuted = p.muteUntil === -1 || (p.muteUntil && p.muteUntil > Date.now());

    menu.innerHTML = '';

    menu.appendChild(makeItem({
      label: t('home.text.nume_the_links_of_the_channel', 'Позначити як прочитане'),
      disabled: !hasUnread,
      onClick: function () { markServerRead(id, dot); }
    }));

    var d0 = document.createElement('div'); d0.className = 'ahctx-divider'; menu.appendChild(d0);

    menu.appendChild(makeItem({
      label: t('common.text.ysusp_to_the_server', 'Запросити на сервер'),
      onClick: function () { openInvite(id, isActive); }
    }));

    var muteItem = makeItem({
      label: t('server_home.aria.mute_server', 'Заглушити сервер'),
      sub: isMuted ? t('server.ctx.muted_state', 'Заглушено') : null,
      hasSubmenu: true
    });
    muteItem.addEventListener('mouseenter', function () { openSubPanel(muteItem, buildMuteSubmenu(id, dot), 'mute'); });
    muteItem.addEventListener('click', function () { openSubPanel(muteItem, buildMuteSubmenu(id, dot), 'mute'); });
    menu.appendChild(muteItem);
    if (isMuted) {
      var unmute = makeItem({ label: t('server.ctx.unmute', 'Увімкнути звук сервера') });
      unmute.addEventListener('click', function () {
        setServerPrefs(id, { muteUntil: 0 });
        setMuteBadge(dot, false);
        toast(t('server.ctx.unmuted', 'Звук сервера увімкнено'));
        closeMenu();
      });
      menu.appendChild(unmute);
    }

    var notifItem = makeItem({
      label: t('common.text.parameters_of_notifications', 'Параметри сповіщень'),
      sub: notifSubtitle(p),
      hasSubmenu: true
    });
    notifItem.addEventListener('mouseenter', function () { openSubPanel(notifItem, buildNotifSubmenu(id, dot), 'notif'); });
    notifItem.addEventListener('click', function () { openSubPanel(notifItem, buildNotifSubmenu(id, dot), 'notif'); });
    menu.appendChild(notifItem);

    menu.appendChild(makeItem({
      label: t('server.ctx.hide_muted_channels', 'Приховати заглушені канали'),
      right: checkBox(!!p.hideMuted),
      onClick: function () {
        p = setServerPrefs(id, { hideMuted: !p.hideMuted });
        document.querySelectorAll('.chan-link.is-muted').forEach(function (el) {
          el.closest('.chan-row').style.display = p.hideMuted ? 'none' : '';
        });
        renderMenuFor(id, dot);
      }
    }));

    menu.appendChild(makeItem({
      label: t('server.ctx.show_all_channels', 'Показати всі канали'),
      right: checkBox(p.showAll !== false),
      onClick: function () {
        p = setServerPrefs(id, { showAll: p.showAll === false });
        document.body.classList.toggle('ahctx-hide-restricted', p.showAll === false);
        renderMenuFor(id, dot);
      }
    }));

    var d1 = document.createElement('div'); d1.className = 'ahctx-divider'; menu.appendChild(d1);

    menu.appendChild(makeItem({
      label: t('common.text.confidential_settings', 'Налаштування конфіденційності'),
      onClick: function () {
        closeMenu();
        if (typeof window.showActionToast === 'function') window.showActionToast(t('server.stub.privacy', 'Розділ у розробці'));
      }
    }));

    menu.appendChild(makeItem({
      label: t('common.text.editing_a_personal', 'Редагувати особистий профіль сервера'),
      onClick: function () {
        closeMenu();
        if (isActive && typeof window.openSelfProfileSettings === 'function') window.openSelfProfileSettings();
        else toast(t('server.ctx.open_server_first', 'Спершу відкрийте цей сервер'));
      }
    }));

    var d2 = document.createElement('div'); d2.className = 'ahctx-divider'; menu.appendChild(d2);

    menu.appendChild(makeItem({
      label: t('common.text.leave_the_server', 'Покинути сервер'),
      danger: true,
      onClick: function () { leaveServer(id, isActive); }
    }));

    var d3 = document.createElement('div'); d3.className = 'ahctx-divider'; menu.appendChild(d3);

    menu.appendChild(makeItem({
      label: t('common.text.copy_id_servers', 'Копіювати ID сервера'),
      right: '<span class="ahctx-idbadge">ID</span>',
      onClick: function () { copyServerId(id, dot); }
    }));

    setMuteBadge(dot, isMuted);
  }

  /* ---------- event wiring ---------- */

  document.addEventListener('contextmenu', function (e) {
    var dot = e.target.closest && e.target.closest('.server-rail .server-dot[data-mention-server-id]');
    if (!dot) return;
    e.preventDefault();
    var id = dot.getAttribute('data-mention-server-id');
    state.serverId = id;
    state.serverName = serverName(dot);
    renderMenuFor(id, dot);
    placeFixed(menu, e.clientX, e.clientY);
  });

  document.addEventListener('click', function (e) {
    if (e.target.closest && (e.target.closest('.ahctx-menu') || e.target.closest('.ahctx-sub-panel'))) return;
    closeMenu();
  });
  document.addEventListener('contextmenu', function (e) {
    if (e.target.closest && e.target.closest('.server-rail .server-dot[data-mention-server-id]')) return;
    closeMenu();
  });
  window.addEventListener('scroll', closeMenu, true);
  window.addEventListener('resize', closeMenu);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenu(); });

  // Відновлюємо іконку "заглушено" при завантаженні сторінки для вже заглушених серверів
  document.addEventListener('DOMContentLoaded', function () {
    var all = loadPrefs();
    document.querySelectorAll('.server-rail .server-dot[data-mention-server-id]').forEach(function (dot) {
      var id = dot.getAttribute('data-mention-server-id');
      var p = all[id];
      if (p && (p.muteUntil === -1 || (p.muteUntil && p.muteUntil > Date.now()))) setMuteBadge(dot, true);
    });
  });
})();
