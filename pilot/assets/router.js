/* ============================================================================
   AlexiHub App Shell — Крок 1 (Пілотний MVP)
   ----------------------------------------------------------------------------
   Дві відповідальності в цьому файлі:

   1) AhVoiceDock  — єдиний, постійний оверлей-плеєр ГС. Має РІВНО ОДИН
      <audio> елемент, який живе в index.html (у шеллі) і ніколи не
      видаляється з DOM. Коли користувач тисне play на будь-якому голосовому
      повідомленні (в dm_chat або в server_channel), ми НЕ програємо
      локальний <audio> з повідомлення — ми віддаємо src у цей єдиний
      елемент. Тому заміна контенту в #app-dynamic фізично не може
      перервати відтворення: audio-нода просто не бере участі в innerHTML-
      заміні.

   2) AhRouter — pushState-роутер. Перехоплює кліки на <a data-route>,
      підвантажує повну сторінку через fetch(), витягує з неї вміст
      #app-dynamic (DOMParser), підміняє його в шеллі та виконує inline
      <script>-теги фрагмента вручну (innerHTML їх сам не запускає).
   ========================================================================== */

(function () {
  'use strict';

  /* ---------------------------------------------------------------------- */
  /* AhVoiceDock                                                            */
  /* ---------------------------------------------------------------------- */
  var Dock = (function () {
    var PREF_KEY = 'ah_voice_prefs_v1';
    var audio = null;      // the one persistent <audio> element (in the shell)
    var ui = null;
    var meta = { author: '', when: '', src: '' };
    var prefs = { volume: 1, muted: false };

    try {
      var saved = JSON.parse(localStorage.getItem(PREF_KEY) || 'null');
      if (saved && typeof saved === 'object') {
        prefs.volume = isFinite(+saved.volume) ? Math.max(0, Math.min(1, +saved.volume)) : 1;
        prefs.muted = !!saved.muted;
      }
    } catch (e) {}

    function savePrefs() {
      try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) {}
    }

    function fmt(s) {
      s = Math.max(0, Math.floor(s || 0));
      var m = Math.floor(s / 60), r = s % 60;
      return (m < 10 ? '0' : '') + m + ':' + (r < 10 ? '0' : '') + r;
    }

    function init(audioEl, uiRoot) {
      audio = audioEl;
      ui = {
        el: uiRoot,
        play: uiRoot.querySelector('.ahvd-play'),
        author: uiRoot.querySelector('.ahvd-author'),
        when: uiRoot.querySelector('.ahvd-when'),
        time: uiRoot.querySelector('.ahvd-time'),
        close: uiRoot.querySelector('.ahvd-close'),
        bar: uiRoot.querySelector('.ahvd-bar'),
        fill: uiRoot.querySelector('.ahvd-fill')
      };
      audio.volume = prefs.volume;
      audio.muted = prefs.muted;

      ui.play.addEventListener('click', toggle);
      ui.close.addEventListener('click', close);

      var seeking = false;
      function seekTo(e) {
        var r = ui.bar.getBoundingClientRect(), d = audio.duration || 0;
        if (!r.width || !d) return;
        var pct = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
        audio.currentTime = pct * d;
        render();
      }
      ui.bar.addEventListener('pointerdown', function (e) { seeking = true; seekTo(e); });
      window.addEventListener('pointermove', function (e) { if (seeking) seekTo(e); });
      window.addEventListener('pointerup', function () { seeking = false; });

      audio.addEventListener('timeupdate', render);
      audio.addEventListener('play', render);
      audio.addEventListener('pause', render);
      audio.addEventListener('ended', render);
    }

    function render() {
      if (!ui || !audio) return;
      var playing = !audio.paused && !audio.ended;
      ui.el.classList.toggle('playing', playing);
      ui.author.textContent = meta.author || 'Голосове повідомлення';
      ui.when.textContent = meta.when || '';
      ui.time.textContent = fmt(audio.currentTime);
      ui.fill.style.width = (audio.duration ? Math.min(100, audio.currentTime / audio.duration * 100) : 0) + '%';
      syncInlineButtons();
    }

    // reflect play/pause state onto whichever inline message widget matches the current src
    function syncInlineButtons() {
      document.querySelectorAll('.ah-voice-message,.dm-voice-message').forEach(function (wrap) {
        var a = wrap.querySelector('audio');
        var isCurrent = a && meta.src && a.getAttribute('src') === meta.src;
        wrap.classList.toggle('playing', !!(isCurrent && audio && !audio.paused && !audio.ended));
        var progress = wrap.querySelector('.ah-voice-progress,.dm-voice-progress');
        if (progress) {
          progress.style.width = (isCurrent && audio && audio.duration)
            ? Math.min(100, audio.currentTime / audio.duration * 100) + '%'
            : '0%';
        }
      });
    }

    function open() {
      if (ui) ui.el.classList.add('open');
    }
    function close() {
      if (audio) { audio.pause(); }
      meta = { author: '', when: '', src: '' };
      if (ui) ui.el.classList.remove('open', 'playing');
      syncInlineButtons();
    }
    function toggle() {
      if (!audio) return;
      if (audio.paused || audio.ended) audio.play().catch(function () {});
      else audio.pause();
    }

    // Called from an inline voice-message play button.
    function playFromWidget(wrap) {
      var a = wrap.querySelector('audio');
      if (!a) return;
      var src = a.getAttribute('src');
      var row = wrap.closest('.message,.msg');
      var timeEl = row && row.querySelector('.message-time,.msg-time');

      if (meta.src === src && audio && !audio.paused) {
        audio.pause();
        return;
      }

      meta = {
        author: (row && row.dataset && row.dataset.messageAuthor) || '',
        when: timeEl ? timeEl.textContent.trim() : '',
        src: src
      };
      audio.src = src;
      audio.currentTime = 0;
      audio.play().catch(function () {});
      open();
      render();
    }

    return { init: init, playFromWidget: playFromWidget };
  })();

  // Global entry points used by onclick="" handlers inside message markup
  window.ahToggleVoicePlay = function (btn) { Dock.playFromWidget(btn.closest('.ah-voice-message')); };
  window.toggleDmVoicePlay = function (btn) { Dock.playFromWidget(btn.closest('.dm-voice-message')); };

  /* ---------------------------------------------------------------------- */
  /* AhRouter                                                                */
  /* ---------------------------------------------------------------------- */
  var Router = (function () {
    var mount = null; // #app-dynamic in the shell

    // route table for the Step-1 pilot: only these two page types exist
    var ROUTES = [
      { test: /^\/community\/dm\//, file: 'pages/dm_chat.html' },
      { test: /^\/community\/servers\/\d+\/channel\/\d+/, file: 'pages/server_channel.html' }
    ];

    function resolve(path) {
      for (var i = 0; i < ROUTES.length; i++) if (ROUTES[i].test.test(path)) return ROUTES[i].file;
      return null;
    }

    function setActiveRail(path) {
      var onServer = /^\/community\/servers\//.test(path);
      document.querySelectorAll('.server-rail .server-dot').forEach(function (dot) {
        dot.classList.remove('active');
      });
      var target = onServer
        ? document.querySelector('.server-rail .server-dot[data-mention-server-id]')
        : document.querySelector('.server-rail .server-dot.home-dot');
      if (target) target.classList.add('active');
    }

    // innerHTML does not execute <script> tags — re-create them so page-level
    // init code (e.g. binding the composer, highlighting the active channel) runs.
    function runScripts(container) {
      container.querySelectorAll('script').forEach(function (old) {
        var s = document.createElement('script');
        for (var i = 0; i < old.attributes.length; i++) {
          s.setAttribute(old.attributes[i].name, old.attributes[i].value);
        }
        s.textContent = old.textContent;
        old.replaceWith(s);
      });
    }

    function swap(html, path) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var fresh = doc.getElementById('app-dynamic');
      if (!fresh) {
        console.error('[AhRouter] fetched page has no #app-dynamic root:', path);
        return;
      }
      mount.replaceChildren.apply(mount, fresh.childNodes);
      runScripts(mount);
      setActiveRail(path);
      document.title = doc.title || document.title;
    }

    function navigate(path, push) {
      var file = resolve(path);
      if (!file) { window.location.href = path; return; } // unknown route: real navigation (out of pilot scope)

      mount.classList.add('route-loading');
      fetch(file, { credentials: 'same-origin' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.text();
        })
        .then(function (html) {
          swap(html, path);
          if (push) history.pushState({ path: path }, '', path);
        })
        .catch(function (err) {
          console.error('[AhRouter] navigation failed:', err);
        })
        .finally(function () {
          mount.classList.remove('route-loading');
        });
    }

    function onClick(e) {
      var link = e.target.closest('a[href]');
      if (!link) return;
      var href = link.getAttribute('href');
      if (!href || href.charAt(0) !== '/') return;      // ignore external / hash links
      if (link.target === '_blank' || e.metaKey || e.ctrlKey || e.shiftKey) return;
      if (!resolve(href)) return;                        // let non-pilot routes navigate normally

      e.preventDefault();
      navigate(href, true);
    }

    function init(mountEl) {
      mount = mountEl;
      document.addEventListener('click', onClick);
      window.addEventListener('popstate', function (e) {
        var path = (e.state && e.state.path) || location.pathname;
        navigate(path, false);
      });
    }

    return { init: init, navigate: navigate };
  })();

  /* ---------------------------------------------------------------------- */
  /* Boot                                                                    */
  /* ---------------------------------------------------------------------- */
  document.addEventListener('DOMContentLoaded', function () {
    Dock.init(document.getElementById('ah-shell-audio'), document.getElementById('ah-voice-dock'));
    Router.init(document.getElementById('app-dynamic'));
    Router.navigate(location.pathname === '/' ? '/community/dm/marichka' : location.pathname, false);
  });
})();
