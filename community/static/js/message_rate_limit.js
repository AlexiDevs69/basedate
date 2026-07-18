(function () {
  'use strict';

  var cooldownUntil = 0;
  var updateTimer = null;
  var lastFocusedElement = null;

  function remainingMs() {
    return Math.max(0, cooldownUntil - Date.now());
  }

  function copy() {
    var stored = '';
    try { stored = localStorage.getItem('alexihub_language') || ''; } catch (error) {}
    var lang = String(stored || document.documentElement.lang || 'uk').toLowerCase().split('-')[0];
    var messages = {
      uk: {
        title: 'Обережно, не поспішай!',
        waiting: function (seconds) { return 'Ви надсилаєте повідомлення надто швидко. Зробіть коротку перерву — ще ' + seconds + ' с.'; },
        waitButton: function (seconds) { return 'Зачекайте ' + seconds + ' с'; },
        ready: 'Перерва завершилась — повідомлення знову можна надсилати.',
        returnButton: 'Повернутися до чату'
      },
      ru: {
        title: 'Осторожно, не спеши!',
        waiting: function (seconds) { return 'Вы отправляете сообщения слишком быстро. Сделайте короткий перерыв — ещё ' + seconds + ' с.'; },
        waitButton: function (seconds) { return 'Подождите ' + seconds + ' с'; },
        ready: 'Перерыв завершён — сообщения снова можно отправлять.',
        returnButton: 'Вернуться в чат'
      },
      en: {
        title: 'Whoa, slow down!',
        waiting: function (seconds) { return 'You are sending messages too quickly. Take a short break — ' + seconds + 's remaining.'; },
        waitButton: function (seconds) { return 'Wait ' + seconds + 's'; },
        ready: 'The break is over — you can send messages again.',
        returnButton: 'Return to chat'
      }
    };
    return messages[lang] || messages.uk;
  }

  function ensureUi() {
    var existing = document.getElementById('ah-message-rate-limit');
    if (existing) return existing;

    var style = document.createElement('style');
    style.id = 'ah-message-rate-limit-style';
    style.textContent = [
      '#ah-message-rate-limit{position:fixed;inset:0;z-index:2147483000;display:none;align-items:center;justify-content:center;padding:20px;background:rgba(0,0,0,.76);backdrop-filter:blur(2px)}',
      '#ah-message-rate-limit.ah-open{display:flex}',
      '#ah-message-rate-limit .ah-rate-card{position:relative;width:min(400px,calc(100vw - 32px));padding:24px;border:1px solid rgba(255,255,255,.08);border-radius:14px;background:#242429;color:#f2f3f5;box-shadow:0 18px 55px rgba(0,0,0,.55);font-family:inherit}',
      '#ah-message-rate-limit .ah-rate-close{position:absolute;top:13px;right:13px;width:34px;height:34px;border:0;border-radius:8px;background:transparent;color:#b5bac1;font-size:28px;line-height:28px;cursor:pointer}',
      '#ah-message-rate-limit .ah-rate-close:hover{color:#fff;background:rgba(255,255,255,.07)}',
      '#ah-message-rate-limit .ah-rate-title{margin:0 38px 8px 0;font-size:20px;font-weight:800;letter-spacing:.01em}',
      '#ah-message-rate-limit .ah-rate-text{margin:0 0 20px;color:#b5bac1;font-size:15px;line-height:1.45}',
      '#ah-message-rate-limit .ah-rate-action{width:100%;min-height:42px;border:0;border-radius:8px;background:#5865f2;color:#fff;font-family:inherit;font-size:14px;font-weight:600;line-height:1;cursor:pointer;transition:background .15s ease,opacity .15s ease}',
      '#ah-message-rate-limit .ah-rate-action:hover:not(:disabled){background:#4752c4}',
      '#ah-message-rate-limit .ah-rate-action:disabled{cursor:not-allowed;opacity:.72}',
      '@media (max-width:520px){#ah-message-rate-limit .ah-rate-card{padding:22px 18px}}'
    ].join('');
    document.head.appendChild(style);

    var overlay = document.createElement('div');
    overlay.id = 'ah-message-rate-limit';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'ah-message-rate-limit-title');
    overlay.innerHTML = '<div class="ah-rate-card"><button type="button" class="ah-rate-close" aria-label="Закрити">&times;</button><h2 class="ah-rate-title" id="ah-message-rate-limit-title">Обережно, не поспішай!</h2><p class="ah-rate-text">Ви надсилаєте повідомлення надто швидко.</p><button type="button" class="ah-rate-action"></button></div>';
    document.body.appendChild(overlay);

    function close() {
      overlay.classList.remove('ah-open');
      if (lastFocusedElement && typeof lastFocusedElement.focus === 'function') {
        lastFocusedElement.focus();
      }
    }

    overlay.querySelector('.ah-rate-close').addEventListener('click', close);
    overlay.querySelector('.ah-rate-action').addEventListener('click', function () {
      if (remainingMs() <= 0) close();
    });
    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) close();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && overlay.classList.contains('ah-open')) close();
    });
    return overlay;
  }

  function updateUi() {
    var overlay = ensureUi();
    var seconds = Math.ceil(remainingMs() / 1000);
    var messages = copy();
    overlay.querySelector('.ah-rate-title').textContent = messages.title;
    var text = overlay.querySelector('.ah-rate-text');
    var action = overlay.querySelector('.ah-rate-action');
    if (seconds > 0) {
      text.textContent = messages.waiting(seconds);
      action.textContent = messages.waitButton(seconds);
      action.disabled = true;
      return;
    }
    text.textContent = messages.ready;
    action.textContent = messages.returnButton;
    action.disabled = false;
    if (updateTimer) {
      clearInterval(updateTimer);
      updateTimer = null;
    }
    window.dispatchEvent(new CustomEvent('ah:message-cooldown-ended'));
  }

  function show(retryAfterMs) {
    var duration = Math.max(1000, Number(retryAfterMs) || 0);
    cooldownUntil = Math.max(cooldownUntil, Date.now() + duration);
    var overlay = ensureUi();
    lastFocusedElement = document.activeElement;
    overlay.classList.add('ah-open');
    updateUi();
    if (!updateTimer) updateTimer = setInterval(updateUi, 250);
    window.dispatchEvent(new CustomEvent('ah:message-rate-limited', {
      detail: { retryAfterMs: remainingMs() }
    }));
  }

  function installFromLocation() {
    var url = new URL(window.location.href);
    var seconds = Number(url.searchParams.get('rate_limited') || 0);
    if (!(seconds > 0)) return;
    url.searchParams.delete('rate_limited');
    window.history.replaceState({}, '', url.pathname + url.search + url.hash);
    show(seconds * 1000);
  }

  window.AlexiHubMessageRateLimit = {
    show: show,
    remainingMs: remainingMs,
    isBlocked: function () { return remainingMs() > 0; },
    installFromLocation: installFromLocation
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installFromLocation, { once: true });
  } else {
    installFromLocation();
  }
})();
