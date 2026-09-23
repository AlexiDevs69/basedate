
/* ===== script block #1 (id: ah-voice-dock-js) ===== */
/* Voice "dock": a small player bar at the top while a voice message plays (prev / play / next,
   speed, volume, seek). Survives page navigation: the state is kept in sessionStorage and the
   bar re-appears instantly on the next page, playback continues from the compensated position. */
(function(){
  if(window.AhVoiceDock)return;
  var KEY='ah_voice_dock_v1',PREF='ah_voice_prefs_v1';
  var AUDIO_SEL='audio.ah-voice-audio,audio.dm-voice-audio';
  var RATES=[1,1.5,2];
  var cur=null,orphan=null,ghost=null,warm=null,ui=null,unloading=false,lastSave=0,sessionReady=false;
  var meta={author:'',when:'',src:'',dur:0};
  var prefs={rate:1,volume:1,muted:false};
  try{var p0=JSON.parse(localStorage.getItem(PREF)||'null');if(p0&&typeof p0==='object'){prefs.rate=RATES.indexOf(+p0.rate)>=0?+p0.rate:1;prefs.volume=isFinite(+p0.volume)?Math.max(0,Math.min(1,+p0.volume)):1;prefs.muted=!!p0.muted;}}catch(e){}
  function savePrefs(){try{localStorage.setItem(PREF,JSON.stringify(prefs));}catch(e){}}
  function fmt(s){s=Math.max(0,Math.floor(s||0));var m=Math.floor(s/60),r=s%60;return (m<10?'0':'')+m+':'+(r<10?'0':'')+r;}
  function srcOf(a){return a?(a.getAttribute('src')||''):'';}
  function audios(){return [].slice.call(document.querySelectorAll(AUDIO_SEL));}
  function metaFrom(a){
    var row=a.closest&&a.closest('.message,.msg');
    var wrap=a.closest&&a.closest('.ah-voice-message,.dm-voice-message');
    var tEl=row&&row.querySelector('.message-time,.msg-time');
    var dur=0;
    if(wrap){
      dur=parseFloat(wrap.dataset.total||'');
      if(!isFinite(dur)){
        var lbl=wrap.querySelector('.ah-voice-duration,.dm-voice-duration');
        var m=lbl&&/^(\d+):(\d{2})$/.exec((lbl.dataset.label||lbl.textContent).trim());
        dur=m?(+m[1])*60+(+m[2]):0;
      }
    }
    return {author:row&&row.dataset?(row.dataset.messageAuthor||''):'',when:tEl?tEl.textContent.replace(/\s+/g,' ').trim():'',src:srcOf(a),dur:dur||0};
  }
  function mergeMeta(m){
    // keep what we restored (labels may not be hydrated yet right after a navigation)
    if(meta.src&&meta.src===m.src){
      if(meta.author)m.author=meta.author;
      if(meta.when)m.when=meta.when;
      if(!m.dur)m.dur=meta.dur;
    }
    return m;
  }
  function totalDur(){return cur&&isFinite(cur.duration)&&cur.duration>0?cur.duration:(meta.dur||0);}
  function view(){
    if(cur)return {t:cur.currentTime||0,dur:totalDur(),playing:!cur.paused&&!cur.ended};
    if(ghost)return ghost;
    return {t:0,dur:0,playing:false};
  }
  function applyPrefs(a){try{a.playbackRate=prefs.rate;a.volume=prefs.volume;a.muted=!!prefs.muted;}catch(e){}}
  function siblingAudio(dir){
    if(!cur||cur===orphan)return null;
    var list=audios(),i=list.indexOf(cur);
    return i<0?null:(list[i+dir]||null);
  }

  /* ---------- persistence ---------- */
  function save(force){
    if(unloading||!cur)return;
    var n=Date.now();
    if(!force&&n-lastSave<500)return;
    lastSave=n;
    try{sessionStorage.setItem(KEY,JSON.stringify({src:meta.src||srcOf(cur),t:cur.currentTime||0,playing:!cur.paused&&!cur.ended,author:meta.author,when:meta.when,dur:totalDur(),savedAt:n}));}catch(e){}
  }
  function clearState(){try{sessionStorage.removeItem(KEY);}catch(e){}}
  window.addEventListener('pagehide',function(){save(true);unloading=true;});
  window.addEventListener('pageshow',function(e){if(e.persisted)unloading=false;});
  document.addEventListener('visibilitychange',function(){if(document.hidden)save(true);});

  /* ---------- UI ---------- */
  var I={
    prev:'<svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zM20 6v12L9.5 12z"/></svg>',
    next:'<svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM4 6v12l10.5-6z"/></svg>',
    play:'<svg class="ahvd-i-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg><svg class="ahvd-i-pause" viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
    vol:'<svg class="ahvd-i-vol" viewBox="0 0 24 24"><path d="M3 10v4h4l5 4V6L7 10z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18 6a8.5 8.5 0 0 1 0 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><svg class="ahvd-i-mute" viewBox="0 0 24 24"><path d="M3 10v4h4l5 4V6L7 10z"/><path d="M16 9.5l5 5M21 9.5l-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    close:'<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>'
  };
  function ensureUI(){
    if(ui||!document.body)return;
    var el=document.createElement('div');
    el.id='ah-voice-dock';
    el.setAttribute('role','region');
    el.setAttribute('aria-label','Голосове повідомлення');
    el.innerHTML='<div class="ahvd-card"><div class="ahvd-main">'
      +'<button type="button" class="ahvd-btn ahvd-prev" title="Попереднє" aria-label="Попереднє">'+I.prev+'</button>'
      +'<button type="button" class="ahvd-btn ahvd-play" title="Відтворити / пауза" aria-label="Відтворити / пауза">'+I.play+'</button>'
      +'<button type="button" class="ahvd-btn ahvd-next" title="Наступне" aria-label="Наступне">'+I.next+'</button>'
      +'<div class="ahvd-info"><b class="ahvd-author"></b><span class="ahvd-when"></span></div>'
      +'<span class="ahvd-time">00:00</span>'
      +'<div class="ahvd-vol"><button type="button" class="ahvd-btn ahvd-mute" title="Звук" aria-label="Звук">'+I.vol+'</button><input class="ahvd-range" type="range" min="0" max="1" step="0.05" aria-label="Гучність"></div>'
      +'<button type="button" class="ahvd-rate" title="Швидкість" aria-label="Швидкість">1x</button>'
      +'<button type="button" class="ahvd-btn ahvd-close" title="Закрити" aria-label="Закрити">'+I.close+'</button>'
      +'</div><div class="ahvd-bar"><div class="ahvd-fill"></div></div></div>';
    document.body.appendChild(el);
    var q=function(s){return el.querySelector(s);};
    ui={el:el,prev:q('.ahvd-prev'),play:q('.ahvd-play'),next:q('.ahvd-next'),author:q('.ahvd-author'),when:q('.ahvd-when'),time:q('.ahvd-time'),mute:q('.ahvd-mute'),range:q('.ahvd-range'),rate:q('.ahvd-rate'),close:q('.ahvd-close'),bar:q('.ahvd-bar'),fill:q('.ahvd-fill')};
    ui.play.addEventListener('click',toggle);
    ui.prev.addEventListener('click',prev);
    ui.next.addEventListener('click',next);
    ui.close.addEventListener('click',close);
    ui.rate.addEventListener('click',cycleRate);
    ui.mute.addEventListener('click',function(){prefs.muted=!prefs.muted;if(!prefs.muted&&prefs.volume===0)prefs.volume=1;savePrefs();if(cur)applyPrefs(cur);render();});
    ui.range.addEventListener('input',function(){prefs.volume=+ui.range.value;prefs.muted=prefs.volume===0;savePrefs();if(cur)applyPrefs(cur);renderVolume();});
    var seeking=false;
    function seekTo(e){
      var r=ui.bar.getBoundingClientRect(),d=totalDur()||(ghost&&ghost.dur)||0;
      if(!r.width||!d)return;
      var pct=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
      ui.fill.style.width=(pct*100)+'%';
      ui.time.textContent=fmt(pct*d);
      if(cur){try{cur.currentTime=pct*d;}catch(_){}}
    }
    ui.bar.addEventListener('pointerdown',function(e){seeking=true;el.classList.add('seeking');try{ui.bar.setPointerCapture(e.pointerId);}catch(_){}seekTo(e);});
    ui.bar.addEventListener('pointermove',function(e){if(seeking)seekTo(e);});
    var endSeek=function(){seeking=false;el.classList.remove('seeking');};
    ui.bar.addEventListener('pointerup',endSeek);
    ui.bar.addEventListener('pointercancel',endSeek);
    ui.seekingRef=function(){return seeking;};
    render();
  }
  function renderVolume(){
    if(!ui)return;
    ui.range.value=prefs.muted?0:prefs.volume;
    ui.el.classList.toggle('muted',!!prefs.muted||prefs.volume===0);
  }
  function renderTime(){
    if(!ui||(ui.seekingRef&&ui.seekingRef()))return;
    var v=view();
    ui.time.textContent=fmt(v.t);
    ui.fill.style.width=(v.dur?Math.min(100,v.t/v.dur*100):0)+'%';
  }
  function render(){
    if(!ui)return;
    var v=view();
    ui.el.classList.toggle('playing',v.playing);
    ui.author.textContent=meta.author||'Голосове повідомлення';
    ui.when.textContent=meta.when||'';
    ui.rate.textContent=(prefs.rate===1?'1':String(prefs.rate))+'x';
    ui.rate.classList.toggle('fast',prefs.rate!==1);
    ui.next.disabled=!siblingAudio(1);
    renderVolume();
    renderTime();
  }
  function open(instant){
    ensureUI();
    if(!ui)return;
    if(instant){
      ui.el.classList.add('instant','open');
      requestAnimationFrame(function(){requestAnimationFrame(function(){if(ui)ui.el.classList.remove('instant');});});
    }else{
      ui.el.classList.remove('instant');
      ui.el.classList.add('open');
    }
  }

  /* ---------- controls ---------- */
  function playElement(a){
    // go through the message's own play button so its UI and the "pause others" logic run
    var wrap=a.closest&&a.closest('.ah-voice-message,.dm-voice-message');
    var btn=wrap&&wrap.querySelector('button');
    if(btn)btn.click();else{var pr=a.play();if(pr&&pr.catch)pr.catch(function(){});}
  }
  function toggle(){
    if(!cur)return;
    if(cur.paused||cur.ended){
      if(cur.ended)cur.currentTime=0;
      var pr=cur.play();if(pr&&pr.catch)pr.catch(function(){});
    }else cur.pause();
  }
  function prev(){
    if(!cur)return;
    var p=siblingAudio(-1);
    if(cur.currentTime>3||!p){cur.currentTime=0;return;}
    cur.pause();p.currentTime=0;playElement(p);
  }
  function next(){
    var n=siblingAudio(1);
    if(!n)return;
    if(cur)cur.pause();
    n.currentTime=0;playElement(n);
  }
  function cycleRate(){
    prefs.rate=RATES[(RATES.indexOf(prefs.rate)+1)%RATES.length];
    savePrefs();
    if(cur)try{cur.playbackRate=prefs.rate;}catch(e){}
    render();
  }
  function close(){
    var a=cur;
    cur=null;ghost=null;
    clearState();
    if(orphan){try{orphan.pause();orphan.removeAttribute('src');}catch(e){}orphan=null;}
    if(a){try{a.pause();}catch(e){}}
    if(ui)ui.el.classList.remove('open','playing');
    try{if('mediaSession' in navigator)navigator.mediaSession.playbackState='none';}catch(e){}
  }

  /* ---------- media events ---------- */
  function mediaSession(){
    if(!('mediaSession' in navigator))return;
    try{
      navigator.mediaSession.metadata=new MediaMetadata({title:'Голосове повідомлення',artist:meta.author||'',album:meta.when||''});
      navigator.mediaSession.setActionHandler('play',toggle);
      navigator.mediaSession.setActionHandler('pause',toggle);
      navigator.mediaSession.setActionHandler('previoustrack',prev);
      navigator.mediaSession.setActionHandler('nexttrack',next);
    }catch(e){}
  }
  function onMedia(evt,a){
    if(evt==='play'){
      if(orphan&&a!==orphan){try{orphan.pause();orphan.removeAttribute('src');}catch(e){}orphan=null;}
      cur=a;ghost=null;
      meta=mergeMeta(metaFrom(a));
      applyPrefs(a);
      open(false);
      render();save(true);mediaSession();
      try{if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing';}catch(e){}
      return;
    }
    if(a!==cur)return;
    if(evt==='pause'){render();save(true);try{if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused';}catch(e){}}
    else if(evt==='timeupdate'){renderTime();save(false);}
    else if(evt==='loadedmetadata'){renderTime();}
    else if(evt==='ended'){
      var n=siblingAudio(1);
      if(n){n.currentTime=0;playElement(n);}else close();
    }
  }
  ['play','pause','ended','timeupdate','loadedmetadata'].forEach(function(evt){
    document.addEventListener(evt,function(e){var a=e.target;if(a&&a.matches&&a.matches(AUDIO_SEL))onMedia(evt,a);},true);
  });
  function wireOrphan(a){
    ['play','pause','ended','timeupdate','loadedmetadata'].forEach(function(evt){a.addEventListener(evt,function(){onMedia(evt,a);});});
  }

  /* ---------- resume after a page change ---------- */
  function whenBody(fn){
    if(document.body)return fn();
    var mo=new (window.__NativeMO||MutationObserver)(function(){if(document.body){mo.disconnect();fn();}});
    mo.observe(document.documentElement,{childList:true});
  }
  function restore(){
    var st=null;
    try{st=JSON.parse(sessionStorage.getItem(KEY)||'null');}catch(e){}
    if(!st||!st.src)return;
    var gap=(Date.now()-(+st.savedAt||0))/1000;
    if(!(gap>=0)||gap>1800){clearState();return;}
    // only a short gap means "we were still playing while the page changed"
    var resume=!!st.playing&&gap<=8;
    meta={author:st.author||'',when:st.when||'',src:st.src,dur:+st.dur||0};
    ghost={t:(+st.t||0)+(resume?gap*prefs.rate:0),dur:meta.dur,playing:resume};
    open(true);render();
    // start fetching now: the real <audio> exists only after the page's scripts have run
    try{warm=new Audio();warm.preload='auto';warm.src=st.src;}catch(e){warm=null;}
    var attach=function(){
      var el=audios().filter(function(a){return srcOf(a)===st.src;})[0];
      var t=(+st.t||0)+(resume?((Date.now()-(+st.savedAt||0))/1000)*prefs.rate:0);
      if(meta.dur&&t>=meta.dur-0.15){close();return;}   // it finished while we were navigating
      if(el){
        if(warm){try{warm.removeAttribute('src');}catch(e){}warm=null;}
        cur=el;
      }else if(warm){
        orphan=warm;warm=null;wireOrphan(orphan);cur=orphan;
      }else return;
      ghost=null;
      applyPrefs(cur);
      try{cur.currentTime=t;}catch(e){}
      if(resume){var pr=cur.play();if(pr&&pr.catch)pr.catch(function(){render();});}
      render();
      if(!resume)save(true);
    };
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',attach);else attach();
  }
  whenBody(function(){ensureUI();restore();});

  window.AhVoiceDock={close:close,toggle:toggle,next:next,prev:prev};
})();

/* ===== script block #4 (id: anon-4) ===== */
(function(){
  window.openProfileCustomizer = function(){
    var settings=document.getElementById('ah-user-settings');
    if(settings && !settings.classList.contains('open')){
      settings.classList.add('open');
      settings.setAttribute('aria-hidden','false');
      if(typeof window.showSettingsTab === 'function') window.showSettingsTab('account');
    }
    var modal=document.getElementById('ah-profile-customizer');
    if(!modal) return;
    modal.classList.add('open');
    modal.setAttribute('aria-hidden','false');
    var close=modal.querySelector('.ah-profile-customizer-close');
    if(close) window.setTimeout(function(){ close.focus(); },0);
  };
  window.closeProfileCustomizer = function(force){
    var modal=document.getElementById('ah-profile-customizer');
    if(!modal || !modal.classList.contains('open')) return true;
    if(force !== true && typeof window.ahProfileHasUnsavedChanges === 'function' && window.ahProfileHasUnsavedChanges()){
      if(typeof window.ahShakeProfileUnsaved === 'function') window.ahShakeProfileUnsaved();
      return false;
    }
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden','true');
    return true;
  };
  window.showProfileCustomizerTab = function(tab){
    tab=tab || 'board';
    document.querySelectorAll('[data-customizer-tab]').forEach(function(button){
      button.classList.toggle('active',button.dataset.customizerTab===tab);
    });
    document.querySelectorAll('[data-customizer-panel]').forEach(function(panel){
      panel.classList.toggle('active',panel.dataset.customizerPanel===tab);
    });
  };
  window.focusProfileDisplayName = function(){
    var view=document.getElementById('ah-display-name-view');
    if(view) view.click();
  };
  window.openUserSettings = function(tab){
    var m=document.getElementById('ah-user-settings');
    if(!m) return;
    m.classList.add('open');
    m.setAttribute('aria-hidden','false');
    if(tab === 'profile'){
      showSettingsTab('account');
      window.requestAnimationFrame(window.openProfileCustomizer);
    }else{
      showSettingsTab(tab || 'account');
    }
  };
  window.closeUserSettings = function(force){
    var m=document.getElementById('ah-user-settings');
    if(!m) return;
    if(typeof window.closeProfileCustomizer === 'function' && window.closeProfileCustomizer(force) === false) return false;
    if(window.closeReleaseNotes) window.closeReleaseNotes();
    m.classList.remove('open');
    m.setAttribute('aria-hidden','true');
    return true;
  };
  window.showSettingsTab = function(tab){
    tab = tab || 'account';
    if(tab === 'profile'){
      window.openProfileCustomizer();
      tab='account';
    }
    document.querySelectorAll('[data-settings-tab]').forEach(function(b){b.classList.toggle('active', b.dataset.settingsTab===tab)});
    document.querySelectorAll('[data-settings-page]').forEach(function(p){p.classList.toggle('active', p.dataset.settingsPage===tab)});
    var active=document.querySelector('[data-settings-tab="'+tab+'"]');
    var h=document.getElementById('ah-settings-heading');
    if(active && h) h.textContent=active.textContent.trim();
  };
  window.ahSettingsFilter = function(value){
    value=(value||'').trim().toLowerCase();
    document.querySelectorAll('[data-settings-tab]').forEach(function(btn){
      var ok=!value || btn.textContent.toLowerCase().includes(value);
      btn.style.display=ok?'flex':'none';
    });
  };
  window.openReleaseNotes = function(){
    var notes=document.getElementById('ah-release-notes');
    if(!notes) return;
    notes.classList.add('open');
    notes.setAttribute('aria-hidden','false');
    var list=notes.querySelector('.ah-release-list');
    if(list) list.scrollTop=0;
    var close=notes.querySelector('.ah-release-close');
    if(close) close.focus();
  };
  window.closeReleaseNotes = function(){
    var notes=document.getElementById('ah-release-notes');
    if(!notes) return;
    notes.classList.remove('open');
    notes.setAttribute('aria-hidden','true');
  };
  window.settingsToast = function(text){
    var t=document.getElementById('ah-settings-toast');
    if(!t) return;
    t.textContent=text || window.AlexiHubI18n.t("common.text.sea_tea_filler");
    t.classList.remove('show'); void t.offsetWidth; t.classList.add('show');
  };
  document.addEventListener('keydown',function(e){
    if(e.key!=='Escape') return;
    var customizer=document.getElementById('ah-profile-customizer');
    if(customizer && customizer.classList.contains('open')){
      e.preventDefault();
      closeProfileCustomizer();
      return;
    }
    var notes=document.getElementById('ah-release-notes');
    if(notes && notes.classList.contains('open')) closeReleaseNotes();
    else closeUserSettings();
  });
  var customizer=document.getElementById('ah-profile-customizer');
  if(customizer) customizer.addEventListener('click',function(event){
    if(event.target===customizer) closeProfileCustomizer();
  });
})();

/* ===== script block #5 (id: ah-image-cropper-js) ===== */
(function(){
  if(window.__ahImageCropperReady) return;
  window.__ahImageCropperReady = true;

  const backdrop = document.getElementById('ah-image-editor-backdrop');
  const viewport = document.getElementById('ah-image-editor-viewport');
  const image = document.getElementById('ah-image-editor-image');
  const zoomInput = document.getElementById('ah-image-editor-zoom');
  const title = document.getElementById('ah-image-editor-title');
  const description = document.getElementById('ah-image-editor-description');
  const saveButton = document.getElementById('ah-image-editor-save');
  const resetButton = document.getElementById('ah-image-editor-reset');
  const centerButton = document.getElementById('ah-image-editor-center');
  const nitroText = document.getElementById('ah-image-editor-nitro-text');
  const nitroButton = document.getElementById('ah-image-editor-nitro-button');
  const state = {
    open:false, kind:'avatar', input:null, sourceUrl:'', naturalWidth:0,
    naturalHeight:0, baseScale:1, zoom:1, offsetX:0, offsetY:0,
    pointerId:null, lastX:0, lastY:0, token:0
  };

  function notify(message){
    if(typeof window.settingsToast === 'function') window.settingsToast(message);
  }

  function clamp(value,min,max){ return Math.min(max,Math.max(min,value)); }

  function dimensions(){
    return {width:viewport.clientWidth || 1,height:viewport.clientHeight || 1};
  }

  function cropBox(){
    const box = dimensions();
    if(state.kind === 'banner') return {x:0,y:0,width:box.width,height:box.height};
    const size = Math.min(350,box.width,box.height);
    return {x:(box.width-size)/2,y:(box.height-size)/2,width:size,height:size};
  }

  function constrain(){
    const crop = cropBox();
    const drawWidth = state.naturalWidth * state.baseScale * state.zoom;
    const drawHeight = state.naturalHeight * state.baseScale * state.zoom;
    state.offsetX = clamp(state.offsetX,-Math.max(0,(drawWidth-crop.width)/2),Math.max(0,(drawWidth-crop.width)/2));
    state.offsetY = clamp(state.offsetY,-Math.max(0,(drawHeight-crop.height)/2),Math.max(0,(drawHeight-crop.height)/2));
  }

  function render(){
    if(!state.naturalWidth || !state.naturalHeight) return;
    constrain();
    const crop = cropBox();
    const drawWidth = state.naturalWidth * state.baseScale * state.zoom;
    const drawHeight = state.naturalHeight * state.baseScale * state.zoom;
    image.style.width = drawWidth + 'px';
    image.style.height = drawHeight + 'px';
    image.style.left = (crop.x+(crop.width-drawWidth)/2 + state.offsetX) + 'px';
    image.style.top = (crop.y+(crop.height-drawHeight)/2 + state.offsetY) + 'px';
    zoomInput.value = String(state.zoom);
    const zoomPercent = ((state.zoom-1)/2)*100;
    zoomInput.style.background = 'linear-gradient(to right,#fff 0%,#fff '+zoomPercent+'%,#4e5058 '+zoomPercent+window.AlexiHubI18n.t("common.text.4e5058_100");
  }

  function resetPosition(){
    if(!state.naturalWidth || !state.naturalHeight) return;
    const crop = cropBox();
    state.baseScale = Math.max(crop.width/state.naturalWidth,crop.height/state.naturalHeight);
    state.zoom = 1;
    state.offsetX = 0;
    state.offsetY = 0;
    render();
  }

  function closeEditor(clearInput){
    if(!state.open) return;
    state.open = false;
    state.token += 1;
    state.pointerId = null;
    viewport.classList.remove('dragging');
    backdrop.classList.remove('open');
    backdrop.setAttribute('aria-hidden','true');
    if(clearInput && state.input) state.input.value = '';
    if(state.sourceUrl){
      URL.revokeObjectURL(state.sourceUrl);
      state.sourceUrl = '';
    }
    image.removeAttribute('src');
    saveButton.disabled = false;
    saveButton.textContent = window.AlexiHubI18n.t("common.label.stastoveti");
  }

  function openEditor(input,kind){
    const file = input && input.files && input.files[0];
    if(!file || !String(file.type || '').startsWith('image/')) return;
    state.open = true;
    state.token += 1;
    state.kind = kind;
    state.input = input;
    state.naturalWidth = 0;
    state.naturalHeight = 0;
    state.sourceUrl = URL.createObjectURL(file);
    viewport.classList.toggle('is-avatar',kind === 'avatar');
    viewport.classList.toggle('is-banner',kind === 'banner');
    const settings = document.getElementById('ah-user-settings');
    if(settings){
      settings.classList.add('open');
      settings.setAttribute('aria-hidden','false');
    }
    title.textContent = kind === 'avatar' ? window.AlexiHubI18n.t("common.text.redagwati_avatar") : window.AlexiHubI18n.t("common.text.redagwati_baner");
    description.textContent = kind === 'avatar'
      ? window.AlexiHubI18n.t("common.text.eve_a_part_of_the_photo_that_will_be_visible")
      : window.AlexiHubI18n.t("common.text.eve_a_part_of_the_photo_that_will_be_visible_96cb2ec");
    if(nitroText) nitroText.innerHTML = kind === 'avatar'
      ? window.AlexiHubI18n.t("common.profile.animated_avatar_nitro_html")
      : window.AlexiHubI18n.t("common.profile.animated_banner_nitro_html");
    backdrop.classList.add('open');
    backdrop.setAttribute('aria-hidden','false');
    image.onload = function(){
      state.naturalWidth = image.naturalWidth;
      state.naturalHeight = image.naturalHeight;
      requestAnimationFrame(resetPosition);
    };
    image.onerror = function(){
      notify(window.AlexiHubI18n.t("common.message.attty_to_open_this_image"));
      closeEditor(true);
    };
    image.src = state.sourceUrl;
  }

  function updatePreview(blob){
    const previewUrl = URL.createObjectURL(blob);
    if(state.kind === 'avatar'){
      document.querySelectorAll('#ah-profile-preview-avatar,#ah-customizer-avatar-tile').forEach(function(target){
        target.style.backgroundImage = 'url("' + previewUrl + '")';
        target.style.backgroundSize = 'cover';
        target.style.backgroundPosition = 'center';
        const fallback = target.querySelector('span:not(.edit),b');
        if(fallback) fallback.textContent = '';
      });
    }else{
      document.querySelectorAll('#ah-profile-preview-banner,#ah-customizer-banner-tile,.self-profile-banner').forEach(function(target){
        target.style.backgroundImage = 'url("' + previewUrl + '")';
        target.style.backgroundSize = 'cover';
        target.style.backgroundPosition = 'center';
      });
      document.querySelectorAll('.account-panel[data-mini-username],.user-panel[data-mini-username]').forEach(function(panel){
        panel.dataset.miniBanner = previewUrl;
      });
    }
  }

  function applyCrop(){
    if(!state.open || !state.input || !state.naturalWidth) return;
    const token = state.token;
    saveButton.disabled = true;
    saveButton.textContent = window.AlexiHubI18n.t("common.label.download");
    const crop = cropBox();
    const output = state.kind === 'avatar'
      ? {width:512,height:512,type:'image/png',extension:'png',quality:0.96}
      : {width:1500,height:600,type:'image/jpeg',extension:'jpg',quality:0.92};
    const canvas = document.createElement('canvas');
    canvas.width = output.width;
    canvas.height = output.height;
    const context = canvas.getContext('2d');
    if(!context){
      notify(window.AlexiHubI18n.t("common.message.the_browser_does_not_support_image_cutting"));
      saveButton.disabled = false;
      saveButton.textContent = window.AlexiHubI18n.t("common.label.stastoveti");
      return;
    }
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    if(output.type === 'image/jpeg'){
      context.fillStyle = '#111217';
      context.fillRect(0,0,output.width,output.height);
    }
    const drawWidth = state.naturalWidth * state.baseScale * state.zoom;
    const drawHeight = state.naturalHeight * state.baseScale * state.zoom;
    const left = crop.x+(crop.width-drawWidth)/2 + state.offsetX;
    const top = crop.y+(crop.height-drawHeight)/2 + state.offsetY;
    context.drawImage(
      image,
      (left-crop.x) * output.width/crop.width,
      (top-crop.y) * output.height/crop.height,
      drawWidth * output.width/crop.width,
      drawHeight * output.height/crop.height
    );
    canvas.toBlob(function(blob){
      if(!state.open || token !== state.token) return;
      if(!blob){
        notify(window.AlexiHubI18n.t("common.message.could_not_process_the_image"));
        saveButton.disabled = false;
        saveButton.textContent = window.AlexiHubI18n.t("common.label.stastoveti");
        return;
      }
      try{
        const originalName = (state.input.files[0] && state.input.files[0].name) || state.kind;
        const baseName = originalName.replace(/\.[^.]+$/,'') || state.kind;
        const croppedFile = new File([blob],baseName + '-cropped.' + output.extension,{type:output.type,lastModified:Date.now()});
        const transfer = new DataTransfer();
        transfer.items.add(croppedFile);
        state.input.files = transfer.files;
        updatePreview(blob);
        const profileForm = document.getElementById('ah-profile-edit-form');
        if(profileForm) profileForm.dispatchEvent(new Event('input',{bubbles:true}));
        closeEditor(false);
        notify(state.kind === 'avatar' ? window.AlexiHubI18n.t("common.message.the_position_of_the_avatar_is_preserved") : window.AlexiHubI18n.t("common.message.the_banner_position_saved"));
      }catch(error){
        notify(window.AlexiHubI18n.t("common.message.usable_to_prepare_a_file_for_download"));
        saveButton.disabled = false;
        saveButton.textContent = window.AlexiHubI18n.t("common.label.stastoveti");
      }
    },output.type,output.quality);
  }

  document.addEventListener('change',function(event){
    const input = event.target;
    if(!input || (input.id !== 'ah-avatar-file' && input.id !== 'ah-banner-file')) return;
    event.stopImmediatePropagation();
    openEditor(input,input.id === 'ah-avatar-file' ? 'avatar' : 'banner');
  },true);

  zoomInput.addEventListener('input',function(){
    state.zoom = Number(zoomInput.value) || 1;
    render();
  });

  viewport.addEventListener('wheel',function(event){
    if(!state.open) return;
    event.preventDefault();
    state.zoom = clamp(state.zoom - event.deltaY*0.0015,1,3);
    render();
  },{passive:false});

  viewport.addEventListener('pointerdown',function(event){
    if(!state.open || !state.naturalWidth) return;
    state.pointerId = event.pointerId;
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add('dragging');
  });
  viewport.addEventListener('pointermove',function(event){
    if(state.pointerId !== event.pointerId) return;
    state.offsetX += event.clientX-state.lastX;
    state.offsetY += event.clientY-state.lastY;
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    render();
  });
  function finishDrag(event){
    if(state.pointerId !== event.pointerId) return;
    state.pointerId = null;
    viewport.classList.remove('dragging');
  }
  viewport.addEventListener('pointerup',finishDrag);
  viewport.addEventListener('pointercancel',finishDrag);

  resetButton.addEventListener('click',resetPosition);
  if(centerButton) centerButton.addEventListener('click',resetPosition);
  if(nitroButton) nitroButton.addEventListener('click',function(){
    closeEditor(true);
    if(typeof window.closeUserSettings === 'function') window.closeUserSettings();
    if(typeof window.openNitroSubscriptionSettings === 'function') window.openNitroSubscriptionSettings();
    else window.location.href = '/community';
  });
  saveButton.addEventListener('click',applyCrop);
  document.querySelectorAll('[data-ah-image-editor-cancel]').forEach(function(button){
    button.addEventListener('click',function(){ closeEditor(true); });
  });
  backdrop.addEventListener('click',function(event){
    if(event.target === backdrop) closeEditor(true);
  });
  document.addEventListener('keydown',function(event){
    if(event.key === 'Escape' && state.open){
      event.preventDefault();
      event.stopImmediatePropagation();
      closeEditor(true);
    }
  },true);
  window.addEventListener('resize',function(){
    if(state.open && state.naturalWidth) resetPosition();
  });
})();

/* ===== script block #7 (id: ah-friend-menu-script) ===== */
(function(){
  let selectedUsername = '';
  let activeTrigger = null;
  let removing = false;

  const menu = () => document.getElementById('ah-friend-menu');
  const modal = () => document.getElementById('ah-unfriend-modal');

  function closeFriendMenu(){
    const el = menu();
    if(el){
      el.classList.remove('open');
      el.setAttribute('aria-hidden','true');
    }
    if(activeTrigger) activeTrigger.setAttribute('aria-expanded','false');
    activeTrigger = null;
  }

  function positionFriendMenu(button){
    const el = menu();
    if(!el || !button) return;
    el.classList.add('open');
    el.setAttribute('aria-hidden','false');

    const rect = button.getBoundingClientRect();
    const menuRect = el.getBoundingClientRect();
    let left = rect.right - menuRect.width;
    let top = rect.bottom + 7;

    left = Math.max(8, Math.min(left, window.innerWidth - menuRect.width - 8));
    if(top + menuRect.height > window.innerHeight - 8){
      top = Math.max(8, rect.top - menuRect.height - 7);
    }
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  }

  window.openFriendMenu = function(event, button){
    event.preventDefault();
    event.stopPropagation();

    const username = String(button?.dataset?.friendMenuUsername || '').trim();
    if(!username) return;

    const sameButton = activeTrigger === button && menu()?.classList.contains('open');
    closeFriendMenu();
    if(sameButton) return;

    selectedUsername = username;
    activeTrigger = button;
    button.setAttribute('aria-expanded','true');
    positionFriendMenu(button);
  };

  window.friendMenuPlaceholder = function(message){
    closeFriendMenu();
    if(typeof showActionToast === 'function') showActionToast(message);
  };

  window.openUnfriendConfirmation = function(){
    closeFriendMenu();
    if(!selectedUsername) return;

    const overlay = modal();
    const title = document.getElementById('ah-unfriend-title');
    const copy = document.getElementById('ah-unfriend-copy');

    if(title) title.textContent = window.AlexiHubI18n.t("common.text.delete_c5ed036") + selectedUsername + "'";
    if(copy) copy.textContent = window.AlexiHubI18n.t("common.text.you_are_sure_that_you_want_to_remove_the_user") + selectedUsername + window.AlexiHubI18n.t("common.text.a_list_of_friends");

    if(overlay){
      overlay.classList.add('open');
      overlay.setAttribute('aria-hidden','false');
    }
    setTimeout(() => document.getElementById('ah-unfriend-confirm')?.focus(), 20);
  };

  window.closeUnfriendConfirmation = function(){
    if(removing) return;
    const overlay = modal();
    if(overlay){
      overlay.classList.remove('open');
      overlay.setAttribute('aria-hidden','true');
    }
  };

  function friendRows(username){
    return Array.from(document.querySelectorAll('.friend-row[data-friend-username]'))
      .filter(row => row.dataset.friendUsername === username);
  }

  function updateFriendPage(pageName, label){
    const page = document.querySelector('[data-friend-page="' + pageName + '"]');
    if(!page) return;

    const list = page.querySelector('.friend-list');
    const count = page.querySelector('.friend-count');
    const rows = list ? list.querySelectorAll('.friend-row[data-friend-username]') : [];
    if(count) count.textContent = label + ' — ' + rows.length;

    if(!list) return;
    let empty = list.querySelector('.friend-empty');
    if(rows.length === 0 && !empty){
      empty = document.createElement('div');
      empty.className = 'friend-empty';
      empty.innerHTML = pageName === 'online'
        ? ('' + "<div><h2>" + window.AlexiHubI18n.t("common.text.aidny_of_no_friends_online_now") + "</h2><p>" + window.AlexiHubI18n.t("common.text.someone_is_online_he_will_appear_here") + "</p></div>")
        : ('' + "<div><h2>" + window.AlexiHubI18n.t("common.text.friend_list_is_empty") + "</h2><p>" + window.AlexiHubI18n.t("common.text.muide_a_first_friend_through_the_add_to_friends_tab") + "</p></div>");
      list.appendChild(empty);
    }else if(rows.length > 0 && empty){
      empty.remove();
    }
  }

  function updateActiveContacts(username){
    document.querySelectorAll('.active-contact-card[data-active-friend-username]').forEach(card => {
      if(card.dataset.activeFriendUsername === username) card.remove();
    });

    const right = document.querySelector('aside.right');
    if(!right) return;
    const remaining = right.querySelectorAll('.active-contact-card[data-active-friend-username]');
    const oldEmpty = right.querySelector('[data-active-contacts-empty="1"]');

    if(remaining.length === 0 && !oldEmpty){
      const empty = document.createElement('div');
      empty.className = 'active-contact-card';
      empty.dataset.activeContactsEmpty = '1';
      empty.innerHTML = ('' + "<div class=\"active-contact-top\"><span class=\"active-contact-title\"><b>" + window.AlexiHubI18n.t("common.text.show_quietly") + "</b><span>" + window.AlexiHubI18n.t("common.text.active_friends_will_appear_here") + "</span></span></div>");
      right.appendChild(empty);
    }else if(remaining.length > 0 && oldEmpty){
      oldEmpty.remove();
    }
  }

  window.confirmUnfriend = async function(){
    if(removing || !selectedUsername) return;
    const username = selectedUsername;
    const confirmButton = document.getElementById('ah-unfriend-confirm');

    removing = true;
    if(confirmButton){
      confirmButton.disabled = true;
      confirmButton.textContent = window.AlexiHubI18n.t("common.label.deleting");
    }

    try{
      const response = await fetch('/community/api/friends/remove/' + encodeURIComponent(username), {
        method:'POST',
        credentials:'same-origin',
        headers:{'Accept':'application/json'}
      });
      const data = await response.json().catch(() => ({}));
      if(!response.ok) throw new Error(data.error || 'remove_failed');

      const rows = friendRows(username);
      rows.forEach(row => row.classList.add('ah-friend-removing'));
      await new Promise(resolve => setTimeout(resolve, 190));
      rows.forEach(row => row.remove());

      updateFriendPage('online', window.AlexiHubI18n.t("status.online"));
      updateFriendPage('all', window.AlexiHubI18n.t("common.text.all_friends"));
      updateActiveContacts(username);

      document.getElementById('ah-v3-mini')?.classList.remove('open');

      const overlay = modal();
      if(overlay){
        overlay.classList.remove('open');
        overlay.setAttribute('aria-hidden','true');
      }
      selectedUsername = '';
      if(typeof showActionToast === 'function') showActionToast(window.AlexiHubI18n.t("common.message.user_removed_from_friends"));
    }catch(error){
      if(typeof showActionToast === 'function') showActionToast(window.AlexiHubI18n.t("common.message.nerguishment_of_removing_the_user_from_friends"));
    }finally{
      removing = false;
      if(confirmButton){
        confirmButton.disabled = false;
        confirmButton.textContent = window.AlexiHubI18n.t("common.action.delete_from_friends");
      }
    }
  };

  document.addEventListener('click', function(event){
    const menuElement = menu();
    if(menuElement?.classList.contains('open') &&
       !menuElement.contains(event.target) &&
       !event.target.closest('.friend-more-trigger')){
      closeFriendMenu();
    }

    const overlay = modal();
    if(overlay?.classList.contains('open') && event.target === overlay){
      closeUnfriendConfirmation();
    }
  });

  document.addEventListener('keydown', function(event){
    if(event.key !== 'Escape') return;
    if(modal()?.classList.contains('open')) closeUnfriendConfirmation();
    else closeFriendMenu();
  });

  window.addEventListener('resize', closeFriendMenu);
  window.addEventListener('scroll', closeFriendMenu, true);
})();

/* ===== script block #8 (id: anon-8) ===== */
(function(){
  window.showFriendTab = function(tab){
    document.querySelectorAll('[data-friend-tab]').forEach(b=>b.classList.toggle('active', b.dataset.friendTab===tab));
    document.querySelectorAll('[data-friend-page]').forEach(p=>p.classList.toggle('active', p.dataset.friendPage===tab));
    setTimeout(()=>document.querySelector('[data-friend-page="'+tab+'"] input')?.focus(), 20);
  };
  window.filterFriendRows = function(value){
    const q=(value||'').trim().toLowerCase();
    document.querySelectorAll('.friend-page.active [data-name]').forEach(row=>{
      row.style.display = !q || (row.dataset.name||'').includes(q) ? 'flex' : 'none';
    });
  };
  window.sendFriendRequestFromHome = async function(e){
    e.preventDefault();
    const input=document.getElementById('add-friend-username');
    const btn=document.getElementById('add-friend-btn');
    const msg=document.getElementById('add-friend-msg');
    const username=(input?.value||'').trim();
    if(!username){ if(msg){msg.className='friend-add-msg err';msg.textContent=window.AlexiHubI18n.t("common.label.enter_the_username");} return; }
    if(btn) btn.disabled=true;
    try{
      const res=await fetch('/community/api/friends/request/'+encodeURIComponent(username),{method:'POST'});
      const data=await res.json().catch(()=>({}));
      if(!res.ok) throw new Error(data.error || window.AlexiHubI18n.t("common.text.narity_not_to_send_the_application"));
      if(msg){msg.className='friend-add-msg ok';msg.textContent=window.AlexiHubI18n.t("common.text.an_application_has_been_sent_status")+(data.status||'pending_sent');}
      if(input) input.value='';
    }catch(err){ if(msg){msg.className='friend-add-msg err';msg.textContent=err.message || window.AlexiHubI18n.t("common.text.a_mismatch_of_sending");} }
    finally{ if(btn) btn.disabled=false; }
  };
  window.respondFriendRequest = async function(id, accept, el){
    try{
      const res=await fetch('/community/api/friends/respond/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({accept:!!accept})});
      if(!res.ok) throw new Error('request failed');
      el?.closest('.friend-row')?.remove();
      if(typeof showActionToast==='function') showActionToast(accept?window.AlexiHubI18n.t("common.message.application_accepted"):window.AlexiHubI18n.t("common.message.application_rejected"));
    }catch(e){ if(typeof showActionToast==='function') showActionToast(window.AlexiHubI18n.t("common.message.nergotially_managed_to_process_the_application")); }
  };
  window.cancelFriendRequest = async function(id, el){
    try{
      const res=await fetch('/community/api/friends/cancel/'+id,{method:'POST'});
      if(!res.ok) throw new Error('request failed');
      el?.closest('.friend-row')?.remove();
      if(typeof showActionToast==='function') showActionToast(window.AlexiHubI18n.t("common.message.application_revoked"));
    }catch(e){ if(typeof showActionToast==='function') showActionToast(window.AlexiHubI18n.t("common.message.failed_to_cancel_the_application")); }
  };
})();

/* ===== script block #14 (id: ah-nitro-badge-ui-fix-js) ===== */
(function(){
  if(window.__ahNitroBadgeUiFix) return;
  window.__ahNitroBadgeUiFix = true;

  function ensureTooltip(){
    let tip = document.getElementById('ah-nitro-floating-tooltip');
    if(!tip){
      tip = document.createElement('div');
      tip.id = 'ah-nitro-floating-tooltip';
      document.body.appendChild(tip);
    }
    return tip;
  }
  function placeTooltip(badge){
    const tip = ensureTooltip();
    const text = badge.getAttribute('data-title') || badge.getAttribute('title') || '';
    if(!text) return;
    // Native browser title duplicates the custom one and looks ugly in profile cards.
    if(badge.getAttribute('title')) badge.setAttribute('data-native-title', badge.getAttribute('title'));
    badge.removeAttribute('title');
    tip.textContent = text;
    tip.classList.add('show');
    tip.style.left = '0px';
    tip.style.top = '0px';
    const r = badge.getBoundingClientRect();
    const tw = Math.min(tip.offsetWidth || 220, 260);
    const th = tip.offsetHeight || 34;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    let top = r.top - th - 10;
    if(top < 8) top = r.bottom + 10;
    top = Math.max(8, Math.min(top, window.innerHeight - th - 8));
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function hideTooltip(){
    const tip = document.getElementById('ah-nitro-floating-tooltip');
    if(tip) tip.classList.remove('show');
  }
  document.addEventListener('mouseover', function(e){
    const badge = e.target.closest && e.target.closest('.ah-nitro-profile-badge');
    if(!badge) return;
    placeTooltip(badge);
  }, true);
  document.addEventListener('mousemove', function(e){
    const badge = e.target.closest && e.target.closest('.ah-nitro-profile-badge');
    if(!badge) return;
    placeTooltip(badge);
  }, true);
  document.addEventListener('mouseout', function(e){
    const badge = e.target.closest && e.target.closest('.ah-nitro-profile-badge');
    if(!badge) return;
    if(e.relatedTarget && badge.contains(e.relatedTarget)) return;
    hideTooltip();
  }, true);
  document.addEventListener('scroll', hideTooltip, true);
  window.addEventListener('resize', hideTooltip);
})();

/* ===== script block #15 (id: ah-nitro-rich-tooltip-js) ===== */
(function(){
  if(window.__ahNitroTooltipPortalInit) return;
  window.__ahNitroTooltipPortalInit = true;

  const labels={basic:'AlexiHub Nitro',gold:window.AlexiHubI18n.t("common.text.narior_nitro"),platinum:window.AlexiHubI18n.t("common.text.platinum_nitro"),diamond:window.AlexiHubI18n.t("common.text.damn_nitro_diamond"),emerald:window.AlexiHubI18n.t("common.text.emerald_nitro"),ruby:window.AlexiHubI18n.t("common.text.ruby_nitro")};
  let activeBadge=null;
  let tip=null;

  function safeTier(value){
    const tier=String(value||'basic').toLowerCase();
    return ['basic','gold','platinum','diamond','emerald','ruby'].includes(tier)?tier:'basic';
  }
  function ensureTip(){
    let node=document.getElementById('ah-nitro-tooltip-portal');
    if(!node){
      node=document.createElement('div');
      node.id='ah-nitro-tooltip-portal';
      node.setAttribute('aria-hidden','true');
      node.innerHTML='<div class="ah-nitro-hover-art"><span class="ah-nitro-hover-medal"><span class="ah-nitro-medal"></span></span></div><div class="ah-nitro-hover-title"></div><div class="ah-nitro-hover-since"></div>';
      document.body.appendChild(node);
    }
    return node;
  }
  function getBadge(node){return node&&node.closest?node.closest('.ah-nitro-profile-badge'):null}
  function readBadge(badge){
    const classTier=Array.from(badge.classList).find(c=>c.startsWith('tier-'))||'tier-basic';
    const tier=safeTier(badge.dataset.nitroTier||classTier.slice(5));
    const rawTitle=badge.dataset.title||badge.getAttribute('title')||'';
    const label=badge.dataset.nitroLabel||labels[tier]||labels.basic;
    let since=badge.dataset.nitroSince||'';
    if(!since&&rawTitle){
      const match=rawTitle.match(/(?:Подписчик|Підписник)\s+с\s+(.+)$/i);
      if(match) since=match[1].trim();
    }
    return {tier,label,since};
  }
  function placeTip(){
    if(!activeBadge||!tip) return;
    const rect=activeBadge.getBoundingClientRect();
    tip.style.left='0px';
    tip.style.top='0px';
    tip.dataset.placement='top';
    const tr=tip.getBoundingClientRect();
    let left=rect.left+rect.width/2-tr.width/2;
    left=Math.max(8,Math.min(left,window.innerWidth-tr.width-8));
    let top=rect.top-tr.height-12;
    let placement='top';
    if(top<8){top=rect.bottom+12;placement='bottom'}
    top=Math.max(8,Math.min(top,window.innerHeight-tr.height-8));
    tip.dataset.placement=placement;
    tip.style.left=Math.round(left)+'px';
    tip.style.top=Math.round(top)+'px';
  }
  function showTip(badge){
    const data=readBadge(badge);
    tip=ensureTip();
    activeBadge=badge;
    tip.className='tier-'+data.tier;
    tip.querySelector('.ah-nitro-hover-title').textContent=data.label;
    tip.querySelector('.ah-nitro-hover-since').textContent=data.since?window.AlexiHubI18n.t("common.text.subscriber_with_5d41795")+data.since:window.AlexiHubI18n.t("common.text.active_subscription_nitro");
    tip.setAttribute('aria-hidden','false');
    requestAnimationFrame(()=>{tip.classList.add('show');placeTip()});
  }
  function hideTip(){
    if(!tip) return;
    tip.classList.remove('show');
    tip.setAttribute('aria-hidden','true');
    activeBadge=null;
  }
  document.addEventListener('mouseover',function(e){
    const badge=getBadge(e.target);if(!badge||activeBadge===badge)return;showTip(badge);
  },true);
  document.addEventListener('mouseout',function(e){
    const badge=getBadge(e.target);if(!badge)return;
    if(e.relatedTarget&&badge.contains(e.relatedTarget))return;
    if(activeBadge===badge)hideTip();
  },true);
  document.addEventListener('focusin',function(e){const badge=getBadge(e.target);if(badge)showTip(badge)},true);
  document.addEventListener('focusout',function(e){const badge=getBadge(e.target);if(badge&&activeBadge===badge)hideTip()},true);
  window.addEventListener('scroll',placeTip,true);
  window.addEventListener('resize',placeTip);
})();

/* ===== script block #16 (id: ah-nitro-gifter-tooltip-js) ===== */
(function(){
  if(window.__ahNitroGifterTooltipReady)return;
  window.__ahNitroGifterTooltipReady=true;
  let current=null;
  function tip(){
    let node=document.getElementById('ah-gifter-tooltip');
    if(!node){node=document.createElement('div');node.id='ah-gifter-tooltip';document.body.appendChild(node)}
    return node;
  }
  function place(badge){
    const node=tip(),rect=badge.getBoundingClientRect();
    node.textContent=badge.dataset.title||window.AlexiHubI18n.t("settings.gifts");
    node.style.display='block';node.classList.remove('show');
    const width=node.offsetWidth||170,height=node.offsetHeight||30;
    let left=rect.left+rect.width/2-width/2;
    left=Math.max(8,Math.min(window.innerWidth-width-8,left));
    let top=rect.top-height-9;
    if(top<8)top=rect.bottom+9;
    node.style.left=Math.round(left)+'px';node.style.top=Math.round(top)+'px';
    requestAnimationFrame(function(){node.classList.add('show')});
  }
  function show(badge){current=badge;place(badge)}
  function hide(){current=null;const node=document.getElementById('ah-gifter-tooltip');if(node){node.classList.remove('show');setTimeout(function(){if(!current)node.style.display='none'},100)}}
  document.addEventListener('pointerover',function(e){const badge=e.target.closest&&e.target.closest('.ah-gifter-profile-badge');if(badge&&badge!==current)show(badge)});
  document.addEventListener('pointerout',function(e){const badge=e.target.closest&&e.target.closest('.ah-gifter-profile-badge');if(badge&&!badge.contains(e.relatedTarget))hide()});
  document.addEventListener('focusin',function(e){const badge=e.target.closest&&e.target.closest('.ah-gifter-profile-badge');if(badge)show(badge)});
  document.addEventListener('focusout',function(e){if(e.target.closest&&e.target.closest('.ah-gifter-profile-badge'))hide()});
  window.addEventListener('scroll',function(){if(current)place(current)},true);
  window.addEventListener('resize',function(){if(current)place(current)});
})();

/* ===== script block #18 (id: ah-community-blocking-inline-js) ===== */
(function(){
  'use strict';
  if(window.__alexiHubBlockingReady) return;
  window.__alexiHubBlockingReady = true;

  function text(){ return new Proxy({}, {get:(_, key)=>window.AlexiHubI18n.t('privacy.blocking.'+String(key))}); }
  function esc(value){ return String(value||'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }
  function safeImage(value){ const v=String(value||'').trim(); return /^(https?:\/\/|\/static\/)/i.test(v)?v:''; }
  function usernameFromMini(){ return (document.getElementById('ah-v3-handle')?.textContent||'').trim().replace(/^@/,''); }
  function notify(message){
    if(typeof window.showActionToast==='function') return window.showActionToast(message);
    if(typeof window.settingsToast==='function') return window.settingsToast(message);
  }
  async function api(url, options){
    const response=await fetch(url,Object.assign({credentials:'same-origin',headers:{Accept:'application/json'}},options||{}));
    const data=await response.json().catch(()=>({}));
    if(!response.ok||!data.ok) throw new Error(data.message||data.error||'request_failed');
    return data;
  }

  let selectedUser=null;
  let currentStatus=null;

  function ensureProfileMenu(){
    const mini=document.getElementById('ah-v3-mini');
    const banner=document.getElementById('ah-v3-banner');
    if(!mini||!banner||document.getElementById('ah-profile-more-btn')) return;
    const c=text();
    const button=document.createElement('button');
    button.id='ah-profile-more-btn'; button.className='ah-profile-more-btn'; button.type='button';
    button.setAttribute('aria-label',window.AlexiHubI18n.t("common.aria.more")); button.setAttribute('aria-expanded','false'); button.textContent='⋯';
    banner.appendChild(button);
    const menu=document.createElement('div');
    menu.id='ah-profile-action-menu'; menu.className='ah-profile-action-menu'; menu.setAttribute('aria-hidden','true');
    menu.innerHTML=`<button type="button" class="ah-profile-menu-item" data-block-action="full">${esc(c.full)}<span>›</span></button><button type="button" class="ah-profile-menu-item" data-block-action="message">${esc(c.message)}</button><div class="ah-profile-menu-separator"></div><button type="button" class="ah-profile-menu-item danger" data-block-action="toggle">${esc(c.block)}</button><div class="ah-profile-menu-separator"></div><button type="button" class="ah-profile-menu-item" data-block-action="copy">${esc(c.copy)}<span class="ah-profile-menu-id">ID</span></button>`;
    mini.appendChild(menu);
  }
  function closeProfileMenu(){
    const menu=document.getElementById('ah-profile-action-menu');
    const button=document.getElementById('ah-profile-more-btn');
    const mini=document.getElementById('ah-v3-mini');
    menu?.classList.remove('open'); menu?.setAttribute('aria-hidden','true');
    button?.setAttribute('aria-expanded','false'); mini?.classList.remove('ah-profile-menu-open');
  }
  async function openProfileMenu(){
    ensureProfileMenu();
    const menu=document.getElementById('ah-profile-action-menu');
    const button=document.getElementById('ah-profile-more-btn');
    const mini=document.getElementById('ah-v3-mini');
    if(menu?.classList.contains('open')) return closeProfileMenu();
    const username=usernameFromMini(); if(!username) return;
    menu?.classList.add('open'); menu?.setAttribute('aria-hidden','false');
    button?.setAttribute('aria-expanded','true'); mini?.classList.add('ah-profile-menu-open');
    try{
      currentStatus=await api('/community/api/blocks/'+encodeURIComponent(username));
      selectedUser=currentStatus.user;
      const own=!!currentStatus.is_self;
      const toggle=menu?.querySelector('[data-block-action="toggle"]');
      const direct=menu?.querySelector('[data-block-action="message"]');
      const copy=menu?.querySelector('[data-block-action="copy"]');
      if(toggle){ toggle.hidden=own; toggle.textContent=currentStatus.blocked_by_me?text().unblock:text().block; }
      if(direct) direct.hidden=own||currentStatus.blocked_by_me||currentStatus.blocked_me||currentStatus.can_message===false;
      if(copy) copy.hidden=own;
    }catch(e){
      closeProfileMenu(); notify(e.message||text().actionError);
    }
  }

  function ensureConfirmModal(){
    if(document.getElementById('ah-block-modal')) return;
    const c=text();
    const modal=document.createElement('div'); modal.id='ah-block-modal'; modal.className='ah-block-modal'; modal.setAttribute('aria-hidden','true');
    modal.innerHTML=`<div class="ah-block-dialog" role="dialog" aria-modal="true"><div class="ah-block-dialog-main"><h2>${esc(c.confirmTitle)}</h2><p><span id="ah-block-confirm-copy"></span><br><br>${esc(c.confirmHint)}</p></div><div class="ah-block-dialog-actions"><button type="button" class="ah-block-cancel">${esc(c.cancel)}</button><button type="button" class="ah-block-confirm">${esc(c.block)}</button></div></div>`;
    document.body.appendChild(modal);
  }
  function openConfirm(){
    ensureConfirmModal(); closeProfileMenu();
    const username=selectedUser?.username||usernameFromMini(); if(!username) return;
    const modal=document.getElementById('ah-block-modal');
    const copy=document.getElementById('ah-block-confirm-copy');
    if(copy) copy.innerHTML=esc(text().confirmText)+' <strong>@'+esc(username)+'</strong>?';
    modal?.classList.add('open'); modal?.setAttribute('aria-hidden','false');
  }
  function closeConfirm(){ const m=document.getElementById('ah-block-modal'); m?.classList.remove('open');m?.setAttribute('aria-hidden','true'); }
  function announceBlockChange(username,blockedByMe){
    document.dispatchEvent(new CustomEvent('alexihub:block-status-changed',{detail:{username,blocked_by_me:!!blockedByMe}}));
  }
  async function blockUser(username){
    const data=await api('/community/api/blocks/'+encodeURIComponent(username),{method:'POST'});
    currentStatus=Object.assign({},currentStatus||{},data,{blocked_by_me:true});
    notify(data.message||text().blockedOk); await loadBlockedUsers(); applyDmBlockState(username,currentStatus); announceBlockChange(username,true); return data;
  }
  async function unblockUser(username){
    const data=await api('/community/api/blocks/'+encodeURIComponent(username),{method:'DELETE'});
    currentStatus=Object.assign({},currentStatus||{},data,{blocked_by_me:false});
    notify(data.message||text().unblockedOk); await loadBlockedUsers(); applyDmBlockState(username,currentStatus); announceBlockChange(username,false); return data;
  }
  async function openBlockFor(username){
    try{
      currentStatus=await api('/community/api/blocks/'+encodeURIComponent(username));
      selectedUser=currentStatus.user;
      if(currentStatus.blocked_by_me){
        await unblockUser(username);
        return;
      }
      openConfirm();
    }catch(error){
      notify(error.message||text().actionError);
    }
  }

  function contentSettingsMarkup(c){
    return `<div class="ah-content-settings"><h1>${esc(c.content)}</h1><div class="ah-privacy-card"><h3>${esc(c.privacyTitle)}</h3><p>${esc(c.privacyText)}</p></div><div class="ah-blocked-heading"><h2>${esc(c.blocked)}</h2><p>${esc(c.intro)}</p></div><div class="ah-blocked-card"><div class="ah-blocked-card-head"><span class="ah-blocked-card-icon">⊘</span><div class="ah-blocked-card-title"><b>${esc(c.blocked)}</b><span id="ah-blocked-count"></span></div></div><div id="ah-blocked-list" class="ah-blocked-list"><div class="ah-blocked-loading">${esc(c.loading)}</div></div></div></div>`;
  }
  function ensureSettings(){
    const sidebar=document.querySelector('#ah-user-settings .ah-settings-sidebar');
    const main=document.querySelector('#ah-user-settings .ah-settings-main');
    if(!sidebar||!main||document.querySelector('[data-settings-tab="content"]')) return;
    const c=text();
    const privacy=document.querySelector('[data-settings-tab="privacy"]');
    const nav=document.createElement('button'); nav.type='button'; nav.className='ah-settings-nav'; nav.dataset.settingsTab='content';
    nav.innerHTML=`<svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" fill="none" stroke="currentColor" stroke-width="2"/><path d="m9 12 2 2 4-5" fill="none" stroke="currentColor" stroke-width="2"/></svg><span>${esc(c.content)}</span>`;
    nav.addEventListener('click',()=>window.showSettingsTab?.('content'));
    (privacy?.parentElement||sidebar).insertBefore(nav,privacy||null);
    const page=document.createElement('section'); page.className='ah-settings-page'; page.dataset.settingsPage='content';
    page.innerHTML=contentSettingsMarkup(c);
    main.appendChild(page);
    const original=window.showSettingsTab;
    if(typeof original==='function'&&!original.__blockingWrapped){
      const wrapped=function(tab){ original(tab); if(tab==='content') loadBlockedUsers(); };
      wrapped.__blockingWrapped=true; window.showSettingsTab=wrapped;
    }
  }
  async function loadBlockedUsers(){
    const list=document.getElementById('ah-blocked-list'); if(!list) return;
    const c=text(); list.innerHTML=`<div class="ah-blocked-loading">${esc(c.loading)}</div>`;
    try{
      const data=await api('/community/api/blocks'); const users=Array.isArray(data.users)?data.users:[];
      const count=document.getElementById('ah-blocked-count'); if(count) count.textContent=users.length+' '+c.count;
      if(!users.length){ list.innerHTML=`<div class="ah-blocked-empty">${esc(c.empty)}</div>`; return; }
      list.innerHTML=users.map(user=>{ const avatar=safeImage(user.avatar_url); return `<div class="ah-blocked-row" data-blocked-username="${esc(user.username)}"><div class="ah-blocked-avatar"${avatar?` style="background-image:url('${esc(avatar)}')"`:''}>${avatar?'':esc(String(user.username||'?').slice(0,1).toUpperCase())}</div><div class="ah-blocked-name"><b>${esc(user.username)}</b><span>@${esc(user.username)} · ID ${esc(user.id)}</span></div><button type="button" class="ah-unblock-btn" data-unblock-user="${esc(user.username)}">${esc(c.unblock)}</button></div>`; }).join('');
    }catch(e){ list.innerHTML=`<div class="ah-blocked-error">${esc(c.loadError)}</div>`; }
  }

  function dmUsername(){ const match=location.pathname.match(/^\/community\/dm\/([^/?#]+)/); try{return match?decodeURIComponent(match[1]):'';}catch(e){return match?match[1]:'';} }
  function applyDmBlockState(username,status){
    const dm=dmUsername(); if(!dm||dm.toLowerCase()!==String(username||'').toLowerCase()) return;
    const blocked=!!(status?.blocked_by_me||status?.blocked_me);
    document.querySelectorAll('.composer textarea,.composer button,.composer-wrap textarea,.composer-wrap button').forEach(el=>{
      if(blocked){ if(!el.disabled) el.dataset.ahBlockDisabled='1'; el.disabled=true; }
      else if(el.dataset.ahBlockDisabled==='1'){ el.disabled=false; delete el.dataset.ahBlockDisabled; }
    });
    let notice=document.getElementById('ah-dm-block-notice');
    if(!blocked){ notice?.remove(); return; }
    if(!notice){ notice=document.createElement('div');notice.id='ah-dm-block-notice';notice.className='ah-dm-block-notice'; const wrap=document.querySelector('.composer-wrap'); wrap?.parentElement?.insertBefore(notice,wrap); }
    if(notice){ notice.innerHTML='<b>'+esc(status.blocked_by_me?text().blockedByMe:text().blockedMe)+'</b>'+(status.blocked_by_me?' <button type="button" data-inline-unblock="'+esc(username)+'">'+esc(text().unblock)+'</button>':''); }
  }
  async function hydrateDmBlockState(){ const username=dmUsername(); if(!username)return; try{const status=await api('/community/api/blocks/'+encodeURIComponent(username));applyDmBlockState(username,status);}catch(e){} }

  document.addEventListener('click',async function(event){
    const more=event.target.closest('#ah-profile-more-btn');
    if(more){event.preventDefault();event.stopPropagation();return openProfileMenu();}
    const action=event.target.closest('[data-block-action]');
    if(action){
      event.preventDefault();event.stopPropagation();const kind=action.dataset.blockAction;const username=selectedUser?.username||usernameFromMini();
      if(kind==='full'){closeProfileMenu();document.getElementById('ah-v3-open-full')?.click();}
      if(kind==='message'&&username) location.href='/community/dm/'+encodeURIComponent(username);
      if(kind==='toggle'&&username){currentStatus?.blocked_by_me?unblockUser(username).then(closeProfileMenu).catch(e=>notify(e.message||text().actionError)):openConfirm();}
      if(kind==='copy'&&selectedUser?.id){try{await navigator.clipboard.writeText(String(selectedUser.id));notify(text().copied);}catch(e){}closeProfileMenu();}
      return;
    }
    if(!event.target.closest('#ah-profile-action-menu')) closeProfileMenu();
    if(event.target.closest('.ah-block-cancel')||event.target===document.getElementById('ah-block-modal')) return closeConfirm();
    const confirm=event.target.closest('.ah-block-confirm');
    if(confirm){const username=selectedUser?.username||usernameFromMini();if(!username)return;confirm.disabled=true;confirm.textContent=text().blocking;try{await blockUser(username);closeConfirm();document.getElementById('ah-v3-mini')?.classList.remove('open');}catch(e){notify(e.message||text().actionError);}finally{confirm.disabled=false;confirm.textContent=text().block;}return;}
    const unblock=event.target.closest('[data-unblock-user],[data-inline-unblock]');
    if(unblock){const username=unblock.dataset.unblockUser||unblock.dataset.inlineUnblock;unblock.disabled=true;try{await unblockUser(username);}catch(e){notify(e.message||text().actionError);}finally{unblock.disabled=false;}return;}
  },true);
  document.addEventListener('submit',async function(event){
    if(event.target.id!=='ah-block-by-name') return;event.preventDefault();const input=event.target.querySelector('input');const username=(input?.value||'').trim();if(!username)return;
    const button=event.target.querySelector('button');button.disabled=true;try{await blockUser(username);input.value='';}catch(e){notify(e.message||text().actionError);}finally{button.disabled=false;}
  });
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){closeProfileMenu();closeConfirm();}});

  function init(){ensureProfileMenu();ensureSettings();ensureConfirmModal();hydrateDmBlockState();}
  window.AlexiHubBlocking={refreshDm:hydrateDmBlockState,refreshList:loadBlockedUsers,openBlockFor};
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init); else init();
})();

/* ===== script block #20 (id: ah-home-server-boost-settings-js) ===== */
(function(){
  if(window.__ahHomeBoostSettingsReady) return;
  window.__ahHomeBoostSettingsReady=true;
  const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const notify=message=>window.settingsToast?.(message);
  async function boostApi(url,options){
    const response=await fetch(url,Object.assign({credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','Accept':'application/json'}},options||{}));
    const data=await response.json().catch(()=>({}));
    if(!response.ok||data.ok===false) throw new Error(data.error||data.detail||window.AlexiHubI18n.t("privacy.blocking.actionError"));
    return data;
  }
  function boostError(error){
    const code=String(error?.message||error||'');
    return ({nitro_required:window.AlexiHubI18n.t("common.text.for_bouts_you_need_an_active_subscription_nitro_or_a"),boost_required:window.AlexiHubI18n.t("common.text.activate_nitro_or_promo_code_of_the_busts"),no_boosts_left:window.AlexiHubI18n.t("common.text.all_available_posts_are_already_used"),nothing_to_remove:window.AlexiHubI18n.t("common.text.there_is_no_bus_on_that_server"),tag_unavailable:window.AlexiHubI18n.t("common.text.the_tag_of_this_server_has_not_yet_been_opened"),bad_code:window.AlexiHubI18n.t("common.text.ulffo_that_the_boost_code_is_correct"),not_found:window.AlexiHubI18n.t("common.text.no_boost_code_found"),used:window.AlexiHubI18n.t("common.text.this_boost_code_has_already_been_activated")})[code]||code||window.AlexiHubI18n.t("privacy.blocking.actionError");
  }
  function renderBoostSettings(data){
    const summary=document.getElementById('ah-user-boost-summary');
    const list=document.getElementById('ah-user-boost-list');
    if(!summary||!list) return;
    const active=!!data.subscription?.active;
    const label=data.subscription?.tier_label||'Nitro';
    const promo=Number(data.promo_boosts||0),nitro=Number(data.nitro_capacity||0),capacity=Number(data.capacity||0);
    const source=active?(promo?window.AlexiHubI18n.t("common.text.promo_code", {p0:(esc(label)),p1:(esc(nitro)),p2:(esc(promo))}):esc(label)):(promo?window.AlexiHubI18n.t("common.text.promo_code_8c01198", {p0:(esc(promo))}):window.AlexiHubI18n.t("common.text.activate_nitro_or_promo_code_to_support_servers"));
    summary.innerHTML=`<div class="ah-user-boost-summary-icon">◆</div><div><strong>${capacity?window.AlexiHubI18n.t("common.text.ally_limit_boosts", {p0:(esc(capacity))}):window.AlexiHubI18n.t("common.text.since_the_future_of_nitro_a_promo_code")}</strong><span>${capacity?window.AlexiHubI18n.t("common.text.used_used_free", {p0:(source),p1:(esc(data.allocated)),p2:(esc(data.remaining))}):source}</span></div>`;
    document.getElementById('ah-boost-code-generator')?.classList.toggle('show',!!data.can_generate);
    const servers=Array.isArray(data.servers)?data.servers:[];
    if(!servers.length){list.innerHTML=('' + "<div class=\"ah-user-boost-empty\">" + window.AlexiHubI18n.t("common.text.you_haven_t_joined_any_server_yet") + "</div>");return;}
    list.innerHTML=servers.map(server=>{
      const icon=server.icon_url?`style="background-image:url('${esc(server.icon_url)}')"`:'';
      const tag=server.tag?.active?`<span class="ah-user-server-tag${server.tag_selected?' selected':''}">${esc(server.tag.glyph)} ${esc(server.tag.text)}${server.tag_selected?window.AlexiHubI18n.t("common.text.vibrano"):''}</span>`:'';
      return ('' + "<div class=\"ah-user-boost-row\"><div class=\"ah-user-boost-server-icon\" " + String((icon)) + ">" + String((server.icon_url?'':esc(String(server.name||'?').slice(0,2).toUpperCase()))) + "</div><div class=\"ah-user-boost-server-copy\"><b>" + String((esc(server.name))) + "</b><span>" + window.AlexiHubI18n.t("common.text.bustives_yours", {p3:(esc(server.total_boosts)),p4:(server.level?esc(server.level)+window.AlexiHubI18n.t("common.text.ye_level"):window.AlexiHubI18n.t("common.text.interest_not_open")),p5:(esc(server.allocated_here))}) + "</span>" + String((tag)) + "</div><div class=\"ah-user-boost-actions\"><button class=\"ah-user-boost-btn\" type=\"button\" onclick=\"changeMyServerBoost(" + String((server.id)) + ",-1)\" " + String((server.allocated_here<1?'disabled':'')) + ">−</button><button class=\"ah-user-boost-btn primary\" type=\"button\" onclick=\"changeMyServerBoost(" + String((server.id)) + ",1)\" " + String((data.remaining<1?'disabled':'')) + ">" + window.AlexiHubI18n.t("common.text.datey_bush") + "</button>" + String((server.tag?.active?`<button class="ah-user-boost-btn ah-user-tag-btn" type="button" title="${server.tag_selected?window.AlexiHubI18n.t("common.text.accounting_tag"):window.AlexiHubI18n.t("common.text.a_show_this_tag_near_the_name")}" onclick="selectMyServerTag(${server.tag_selected?'null':server.id})">${server.tag_selected?'✓':'🏷'}</button>`:'')) + "</div></div>");
    }).join('');
  }
  window.loadMyBoostSettings=async function(){
    const list=document.getElementById('ah-user-boost-list');
    if(list) list.innerHTML=('' + "<div class=\"ah-user-boost-loading\">" + window.AlexiHubI18n.t("common.text.servail_download") + "</div>");
    try{renderBoostSettings(await boostApi('/community/api/boosts/me'));}catch(error){if(list)list.innerHTML=`<div class="ah-user-boost-empty">${esc(boostError(error))}</div>`;}
  };
  window.changeMyServerBoost=async function(serverId,delta){
    try{
      await boostApi(`/community/api/servers/${serverId}/boosts`,{method:'POST',body:JSON.stringify({delta:Number(delta)})});
      notify(delta>0?window.AlexiHubI18n.t("common.message.the_server_is_terrified_by_the_bush"):window.AlexiHubI18n.t("common.message.badthte_the_bust_is_turned_to_the_available"));
      await window.loadMyBoostSettings();
    }catch(error){notify(boostError(error));}
  };
  window.redeemBoostCode=async function(){
    const input=document.getElementById('ah-boost-code-input'),button=document.getElementById('ah-boost-redeem-btn'),status=document.getElementById('ah-boost-code-status');
    const code=String(input?.value||'').trim();
    if(!code){if(status){status.textContent=window.AlexiHubI18n.t("common.label.enter_the_bus_code");status.className='ah-boost-code-status err';}return;}
    if(button)button.disabled=true;if(status){status.textContent=window.AlexiHubI18n.t("common.label.check_the_code_0404e6f");status.className='ah-boost-code-status';}
    try{const data=await boostApi('/community/api/boosts/redeem',{method:'POST',body:JSON.stringify({code})});if(input)input.value='';if(status){status.textContent=window.AlexiHubI18n.t("common.label.ynatoe_added_jets_accessible", {p0:(data.boosts_added),p1:(data.remaining)});status.className='ah-boost-code-status ok';}await window.loadMyBoostSettings();}catch(error){if(status){status.textContent=boostError(error);status.className='ah-boost-code-status err';}}finally{if(button)button.disabled=false;}
  };
  window.generateBoostCode=async function(){
    const amount=document.getElementById('ah-boost-gen-amount'),note=document.getElementById('ah-boost-gen-note'),button=document.getElementById('ah-boost-generate-btn'),output=document.getElementById('ah-boost-code-output'),field=document.getElementById('ah-boost-generated-code');
    if(button)button.disabled=true;
    try{const data=await boostApi('/community/api/boosts/generate',{method:'POST',body:JSON.stringify({boosts:Number(amount?.value||2),note:String(note?.value||'')})});if(field)field.value=data.code?.code||'';output?.classList.add('show');notify(window.AlexiHubI18n.t("common.message.the_code_for_the_busts_was_created", {p0:(data.code?.boosts||amount?.value||2)}));}catch(error){notify(boostError(error));}finally{if(button)button.disabled=false;}
  };
  window.copyGeneratedBoostCode=async function(){
    const field=document.getElementById('ah-boost-generated-code'),value=String(field?.value||'');if(!value)return;
    try{await navigator.clipboard.writeText(value);notify(window.AlexiHubI18n.t("common.message.the_boost_code_is_copied"));}catch(_error){field?.select();document.execCommand('copy');notify(window.AlexiHubI18n.t("common.message.the_boost_code_is_copied"));}
  };
  window.selectMyServerTag=async function(serverId){
    try{
      await boostApi('/community/api/boosts/tag-selection',{method:'POST',body:JSON.stringify({server_id:serverId})});
      notify(serverId?window.AlexiHubI18n.t("common.message.a_server_tag_is_now_displayed_near_your_name"):window.AlexiHubI18n.t("common.message.the_server_tag_is_vimcanned"));
      await window.loadMyBoostSettings();
    }catch(error){notify(boostError(error));}
  };
  const oldShow=window.showSettingsTab;
  if(typeof oldShow==='function'&&!oldShow.__homeBoostWrapped){
    const wrapped=function(tab){oldShow(tab);if(tab==='boosts')window.loadMyBoostSettings();};
    wrapped.__homeBoostWrapped=true;window.showSettingsTab=wrapped;
  }
  document.getElementById('ah-boost-code-input')?.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();window.redeemBoostCode();}});
})();

/* ===== script block #22 (id: ah-account-scrollspy-js) ===== */
(function(){
  if(window.__ahAccountScrollspyReady)return;
  window.__ahAccountScrollspyReady=true;

  var modal=document.getElementById('ah-user-settings');
  if(!modal)return;
  var main=modal.querySelector('.ah-settings-main');
  var accountPage=modal.querySelector('[data-settings-page="account"]');
  var subnav=modal.querySelector('[data-account-subnav]');
  var buttons=Array.prototype.slice.call(modal.querySelectorAll('[data-account-section-target]'));
  var sections=Array.prototype.slice.call(modal.querySelectorAll('[data-account-section]'));
  if(!main||!accountPage||!subnav||!buttons.length||!sections.length)return;

  var frame=0;
  var reduceMotion=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function accountIsActive(){
    return accountPage.classList.contains('active');
  }
  function setActive(sectionId){
    buttons.forEach(function(button){
      var active=button.dataset.accountSectionTarget===sectionId;
      button.classList.toggle('active',active);
      if(active)button.setAttribute('aria-current','true');
      else button.removeAttribute('aria-current');
    });
  }
  function updateFromScroll(){
    frame=0;
    if(!accountIsActive())return;
    var topbar=modal.querySelector('.ah-settings-top');
    var marker=main.getBoundingClientRect().top+(topbar?topbar.offsetHeight:58)+72;
    var active=sections[0].id;
    sections.forEach(function(section){
      if(section.getBoundingClientRect().top<=marker)active=section.id;
    });
    if(main.scrollTop+main.clientHeight>=main.scrollHeight-3)active=sections[sections.length-1].id;
    setActive(active);
  }
  function queueUpdate(){
    if(!frame)frame=requestAnimationFrame(updateFromScroll);
  }
  function scrollToSection(sectionId,focus){
    var section=document.getElementById(sectionId);
    if(!section)return;
    var topbar=modal.querySelector('.ah-settings-top');
    var offset=(topbar?topbar.offsetHeight:58)+24;
    var target=section.getBoundingClientRect().top-main.getBoundingClientRect().top+main.scrollTop-offset;
    setActive(sectionId);
    main.scrollTo({top:Math.max(0,target),behavior:reduceMotion?'auto':'smooth'});
    if(focus)window.setTimeout(function(){section.focus({preventScroll:true})},reduceMotion?0:260);
  }

  buttons.forEach(function(button){
    button.addEventListener('click',function(){
      var sectionId=button.dataset.accountSectionTarget;
      if(!accountIsActive()){
        if(typeof window.showSettingsTab==='function')window.showSettingsTab('account');
        requestAnimationFrame(function(){scrollToSection(sectionId,true)});
      }else scrollToSection(sectionId,true);
    });
  });
  main.addEventListener('scroll',queueUpdate,{passive:true});
  window.addEventListener('resize',queueUpdate,{passive:true});

  var previousShow=window.showSettingsTab;
  if(typeof previousShow==='function'){
    window.showSettingsTab=function(tab){
      var result=previousShow.apply(this,arguments);
      var account=(!tab||tab==='account'||tab==='profile');
      subnav.hidden=!account;
      if(account){
        requestAnimationFrame(function(){
          if(tab!=='profile')main.scrollTo({top:0,behavior:'auto'});
          updateFromScroll();
        });
      }
      return result;
    };
  }

  subnav.hidden=!accountIsActive();
  updateFromScroll();
})();

/* ===== script block #23 (id: ah-data-privacy-settings-js) ===== */
(function(){
  'use strict';
  if(window.__ahDataPrivacyReady)return;
  window.__ahDataPrivacyReady=true;

  function t(){return new Proxy({}, {get:function(_,key){return window.AlexiHubI18n.t('privacy.messages.'+String(key))}})}
  function esc(v){return String(v==null?'':v).replace(/[&<>'"]/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]})}
  async function api(url,options){var response=await fetch(url,Object.assign({credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/json'}},options||{}));var data=await response.json().catch(function(){return {}});if(!response.ok||data.ok===false)throw new Error(data.message||data.error||'request_failed');return data}
  function toast(message){if(typeof window.settingsToast==='function')window.settingsToast(message);else if(typeof window.showActionToast==='function')window.showActionToast(message)}

  var modal=document.getElementById('ah-user-settings');
  if(!modal)return;
  var main=modal.querySelector('.ah-settings-main');
  var page=modal.querySelector('[data-settings-page="privacy"]');
  var nav=modal.querySelector('[data-settings-tab="privacy"]');
  if(!main||!page||!nav)return;
  var c=t(),loaded=false,busy=false,state=null,lastSaved=null,frame=0;

  function switchRow(field,title,copy){
    return '<div class="ah-privacy-setting"><div><b>'+esc(title)+'</b>'+(copy?'<p>'+esc(copy)+'</p>':'')+'</div><label class="ah-privacy-switch"><input type="checkbox" data-privacy-field="'+esc(field)+'" aria-label="'+esc(title)+'" disabled><span class="ah-privacy-switch-track"></span></label></div>';
  }
  function buildPage(){
    page.innerHTML='<div class="ah-data-privacy"><h1>'+esc(c.title)+'</h1><p class="ah-privacy-lead">'+esc(c.lead)+'</p><div id="ah-privacy-status" class="ah-privacy-status" role="status" aria-live="polite">'+esc(c.loading)+'</div>'+
      '<section id="privacy-direct-messages" class="ah-privacy-section" data-privacy-section tabindex="-1"><h2>'+esc(c.dmTitle)+'</h2><p class="ah-privacy-kicker">'+esc(c.dmKicker)+'</p><p class="ah-privacy-copy">'+esc(c.dmCopy)+'</p><div class="ah-privacy-scope"><span class="ah-privacy-scope-icon">AH</span><span>'+esc(c.allServers)+'</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"></path></svg></div>'+switchRow('allow_dm_from_server_members',c.dmServer,c.dmServerCopy)+'</section>'+
      '<section id="privacy-friend-requests" class="ah-privacy-section" data-privacy-section tabindex="-1"><h2>'+esc(c.friendTitle)+'</h2><p class="ah-privacy-kicker">'+esc(c.friendKicker)+'</p>'+switchRow('allow_friend_requests_everyone',c.everyone,'')+switchRow('allow_friend_requests_mutual_friends',c.mutual,'')+switchRow('allow_friend_requests_server_members',c.serverMembers,c.serverMembersCopy)+'</section>'+
      '<section id="privacy-blocked-accounts" class="ah-privacy-section" data-privacy-section tabindex="-1"><h2>'+esc(c.blockedTitle)+'</h2><p class="ah-privacy-block-copy">'+esc(c.blockedCopy)+'</p><div class="ah-blocked-card"><div class="ah-blocked-card-head"><span class="ah-blocked-card-icon">⊘</span><div class="ah-blocked-card-title"><b>'+esc(c.blockedTitle)+'</b><span id="ah-blocked-count"></span></div></div><div id="ah-blocked-list" class="ah-blocked-list"><div class="ah-blocked-loading">'+esc(c.loading)+'</div></div></div></section></div>';
  }
  function buildSubnav(){
    modal.querySelectorAll('[data-settings-tab="content"],[data-settings-page="content"]').forEach(function(el){el.remove()});
    var old=modal.querySelector('[data-privacy-subnav]');if(old)old.remove();
    var sub=document.createElement('div');sub.className='ah-privacy-subnav';sub.dataset.privacySubnav='';sub.hidden=true;sub.setAttribute('aria-label',c.title);
    [['privacy-direct-messages',c.dmNav],['privacy-friend-requests',c.friendsNav],['privacy-blocked-accounts',c.blockedNav]].forEach(function(item,index){var b=document.createElement('button');b.type='button';b.className='ah-privacy-subnav-btn'+(index===0?' active':'');b.dataset.privacySectionTarget=item[0];b.textContent=item[1];if(index===0)b.setAttribute('aria-current','true');sub.appendChild(b)});
    nav.insertAdjacentElement('afterend',sub);return sub;
  }
  buildPage();
  var subnav=buildSubnav();
  var buttons=Array.prototype.slice.call(subnav.querySelectorAll('[data-privacy-section-target]'));
  var sections=Array.prototype.slice.call(page.querySelectorAll('[data-privacy-section]'));
  var status=document.getElementById('ah-privacy-status');

  function setStatus(message,type){if(!status)return;status.textContent=message||'';status.className='ah-privacy-status'+(type?' '+type:'')}
  function setBusy(value){busy=!!value;page.setAttribute('aria-busy',busy?'true':'false');page.querySelectorAll('[data-privacy-field]').forEach(function(input){input.disabled=busy||!loaded})}
  function render(){if(!state)return;page.querySelectorAll('[data-privacy-field]').forEach(function(input){input.checked=!!state[input.dataset.privacyField]})}
  async function loadSettings(force){
    if(busy||loaded&&!force)return;
    setBusy(true);setStatus(c.loading,'');
    try{var data=await api('/community/api/privacy');state=Object.assign({},data.settings||{});lastSaved=Object.assign({},state);loaded=true;render();setStatus('','');window.AlexiHubBlocking&&window.AlexiHubBlocking.refreshList&&window.AlexiHubBlocking.refreshList()}
    catch(error){loaded=false;setStatus(c.loadError+' ','error');var retry=document.createElement('button');retry.type='button';retry.className='ah-privacy-retry';retry.textContent=c.retry;retry.addEventListener('click',function(){loadSettings(true)});status&&status.appendChild(retry)}
    finally{setBusy(false)}
  }
  async function saveField(input){
    if(busy||!loaded)return;
    var field=input.dataset.privacyField,previous=Object.assign({},lastSaved||state||{});state[field]=!!input.checked;render();setBusy(true);setStatus('','');
    try{var body={};body[field]=state[field];var data=await api('/community/api/privacy',{method:'PATCH',body:JSON.stringify(body)});state=Object.assign({},data.settings||state);lastSaved=Object.assign({},state);render();setStatus(c.saved,'ok');window.setTimeout(function(){if(status&&status.textContent===c.saved)setStatus('','')},1500)}
    catch(error){state=previous;lastSaved=Object.assign({},previous);render();setStatus(c.saveError,'error');toast(c.saveError)}
    finally{setBusy(false)}
  }
  page.addEventListener('change',function(event){var input=event.target.closest('[data-privacy-field]');if(input)saveField(input)});

  function activeSection(id){buttons.forEach(function(button){var active=button.dataset.privacySectionTarget===id;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','true');else button.removeAttribute('aria-current')})}
  function privacyActive(){return page.classList.contains('active')}
  function updateScroll(){frame=0;if(!privacyActive())return;var topbar=modal.querySelector('.ah-settings-top');var marker=main.getBoundingClientRect().top+(topbar?topbar.offsetHeight:58)+76;var id=sections[0].id;sections.forEach(function(section){if(section.getBoundingClientRect().top<=marker)id=section.id});if(main.scrollTop+main.clientHeight>=main.scrollHeight-3)id=sections[sections.length-1].id;activeSection(id)}
  function queueScroll(){if(!frame)frame=requestAnimationFrame(updateScroll)}
  function scrollTo(id){var section=document.getElementById(id);if(!section)return;var topbar=modal.querySelector('.ah-settings-top');var offset=(topbar?topbar.offsetHeight:58)+24;var top=section.getBoundingClientRect().top-main.getBoundingClientRect().top+main.scrollTop-offset;activeSection(id);main.scrollTo({top:Math.max(0,top),behavior:window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'})}
  buttons.forEach(function(button){button.addEventListener('click',function(){if(!privacyActive()&&typeof window.showSettingsTab==='function')window.showSettingsTab('privacy');requestAnimationFrame(function(){scrollTo(button.dataset.privacySectionTarget)})})});
  main.addEventListener('scroll',queueScroll,{passive:true});window.addEventListener('resize',queueScroll,{passive:true});

  var previousShow=window.showSettingsTab;
  if(typeof previousShow==='function')window.showSettingsTab=function(tab){var result=previousShow.apply(this,arguments);var active=tab==='privacy';subnav.hidden=!active;if(active){requestAnimationFrame(function(){main.scrollTo({top:0,behavior:'auto'});activeSection(sections[0].id);updateScroll()});loadSettings(false)}return result};
  subnav.hidden=!privacyActive();if(privacyActive())loadSettings(false);

  function dmUsername(){var match=location.pathname.match(/^\/community\/dm\/([^/?#]+)/);try{return match?decodeURIComponent(match[1]):''}catch(e){return match?match[1]:''}}
  function setDmDenied(denied){
    document.querySelectorAll('.composer textarea,.composer button,.composer-wrap textarea,.composer-wrap button').forEach(function(el){if(denied){if(!el.disabled)el.dataset.ahPrivacyDisabled='1';el.disabled=true}else if(el.dataset.ahPrivacyDisabled==='1'){el.disabled=false;delete el.dataset.ahPrivacyDisabled}});
    var notice=document.getElementById('ah-dm-privacy-notice');if(!denied){notice&&notice.remove();return}if(!notice){notice=document.createElement('div');notice.id='ah-dm-privacy-notice';var wrap=document.querySelector('.composer-wrap')||document.querySelector('.composer');if(wrap&&wrap.parentElement)wrap.parentElement.insertBefore(notice,wrap)}if(notice)notice.innerHTML='<b>'+esc(c.dmDenied)+'</b>';
  }
  async function hydrateDmPermission(){var username=dmUsername();if(!username)return;try{var data=await api('/community/api/privacy/message-permission/'+encodeURIComponent(username));if(data.reason!=='blocked_relationship')setDmDenied(!data.can_message)}catch(error){}}
  hydrateDmPermission();
})();

/* ===== script block #24 (id: ah-mini-profile-theme-js) ===== */
(function(){if(window.__ahMiniProfileThemesReady)return;window.__ahMiniProfileThemesReady=true;const cache=new Map();let version=0;const mini=()=>document.getElementById('ah-v3-mini');const safeUrl=value=>String(value||'').replace(/[\n\r'"\\()]/g,c=>encodeURIComponent(c));function apply(theme){const card=mini();if(!card)return;card.classList.toggle('ah-profile-theme',!!theme);if(!theme){card.style.removeProperty('--ah-theme-image');return;}card.style.setProperty('--ah-theme-image',`url('${safeUrl(theme.image_url)}')`);card.style.setProperty('--ah-theme-overlay',String(Math.max(0,Math.min(90,Number(theme.overlay_strength||58)))/100));card.style.setProperty('--ah-theme-text',theme.text_color||'#fff');card.style.setProperty('--ah-theme-accent',theme.accent_color||'#5865f2');}async function load(username){username=String(username||'').replace(/^@/,'').trim();if(!username)return;const token=++version,hit=cache.get(username.toLowerCase());if(hit&&Date.now()-hit.at<30000){apply(hit.theme);return;}try{const response=await fetch('/community/api/friend-status/'+encodeURIComponent(username),{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}}),data=await response.json();if(token!==version||!response.ok||!data.ok)return;cache.set(username.toLowerCase(),{theme:data.mini_profile_theme||null,at:Date.now()});apply(data.mini_profile_theme||null);}catch(_error){}}function sync(){const card=mini();if(!card||!card.classList.contains('open'))return;load(document.getElementById('ah-v3-name')?.dataset.profileUsername||document.getElementById('ah-v3-handle')?.textContent||'');}const observer=new MutationObserver(sync);document.addEventListener('DOMContentLoaded',()=>{const card=mini();if(card)observer.observe(card,{attributes:true,subtree:true,childList:true});});document.addEventListener('alexihub:full-profile-open',event=>load(event.detail?.name||event.detail?.username));window.AlexiHubProfileThemes={preview:apply,invalidate(){cache.clear();sync();}};})();

/* ===== script block #25 (id: ah-profile-theme-library-js) ===== */
(function(){
  if(window.__ahProfileThemeLibraryReady)return;window.__ahProfileThemeLibraryReady=true;
  let library=null,selectedId=null,currentId=null,loading=false;
  const $=(selector,root=document)=>root.querySelector(selector);
  const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const attr=value=>esc(value).replace(/`/g,'&#96;');
  const safeUrl=value=>String(value||'').replace(/[\n\r'"\\()]/g,ch=>encodeURIComponent(ch));
  function toast(message){if(typeof window.showActionToast==='function')window.showActionToast(message);else if(typeof window.settingsToast==='function')window.settingsToast(message);}
  async function api(path,options){const response=await fetch(path,Object.assign({credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}},options||{}));const data=await response.json().catch(()=>({}));if(!response.ok||data.ok===false)throw new Error(data.message||data.error||window.AlexiHubI18n.t("common.text.aidability_of_the_action_could_not_be_completed"));return data;}
  function ensureMenu(){
    if($('#ah-banner-action-menu'))return;
    const menu=document.createElement('div');menu.id='ah-banner-action-menu';menu.className='ah-banner-action-menu';menu.setAttribute('aria-hidden','true');
    menu.innerHTML=('' + "<button type=\"button\" data-banner-action=\"upload\">" + window.AlexiHubI18n.t("common.text.a_change_of_the_banner") + "</button><button type=\"button\" data-banner-action=\"theme\">" + window.AlexiHubI18n.t("common.text.a_change_the_theme_of_the_mini_profile") + "</button>");
    document.body.appendChild(menu);
    menu.addEventListener('click',event=>{const action=event.target.closest('[data-banner-action]')?.dataset.bannerAction;if(!action)return;closeMenu();if(action==='upload')$('#ah-banner-file')?.click();else openPicker();});
  }
  function closeMenu(){const menu=$('#ah-banner-action-menu');menu?.classList.remove('open');menu?.setAttribute('aria-hidden','true');}
  function openMenu(anchor){ensureMenu();const menu=$('#ah-banner-action-menu');if(!menu||!anchor)return;if(menu.classList.contains('open')){closeMenu();return;}const rect=anchor.getBoundingClientRect();menu.classList.add('open');menu.setAttribute('aria-hidden','false');const menuRect=menu.getBoundingClientRect();let left=Math.min(window.innerWidth-menuRect.width-8,rect.right-menuRect.width-8);let top=rect.top+36;if(top+menuRect.height>window.innerHeight-8)top=rect.bottom-menuRect.height-8;menu.style.left=Math.max(8,left)+'px';menu.style.top=Math.max(8,top)+'px';}
  function ensurePicker(){
    if($('#ah-profile-theme-picker'))return;
    const picker=document.createElement('div');picker.id='ah-profile-theme-picker';picker.className='ah-profile-theme-picker';picker.setAttribute('aria-hidden','true');
    picker.innerHTML=('' + "<div class=\"ah-profile-theme-dialog\" role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"ah-profile-theme-title\"><header class=\"ah-profile-theme-head\"><h2 id=\"ah-profile-theme-title\">" + window.AlexiHubI18n.t("common.text.a_change_the_theme_of_the_mini_profile") + "</h2><button class=\"ah-profile-theme-close\" type=\"button\" aria-label=\"" + window.AlexiHubI18n.t("common.actions.close") + "\">×</button></header><div class=\"ah-profile-theme-content\"><section id=\"ah-profile-theme-list\" class=\"ah-profile-theme-list\"></section><aside class=\"ah-profile-theme-preview-pane\"><div id=\"ah-profile-theme-preview\" class=\"ah-profile-theme-preview-card\"><div id=\"ah-profile-theme-preview-banner\" class=\"ah-profile-theme-preview-banner\"></div><div class=\"ah-profile-theme-preview-body\"><div id=\"ah-profile-theme-preview-avatar\" class=\"ah-profile-theme-preview-avatar\"></div><div id=\"ah-profile-theme-preview-name\" class=\"ah-profile-theme-preview-name\">" + window.AlexiHubI18n.t("settings.profile") + "</div><div id=\"ah-profile-theme-preview-handle\" class=\"ah-profile-theme-preview-handle\">@username</div><div class=\"ah-profile-theme-preview-badges\"><i></i><i></i><i></i></div><div class=\"ah-profile-theme-preview-button\">" + window.AlexiHubI18n.t("common.text.an_example_of_a_button") + "</div></div></div></aside></div><footer class=\"ah-profile-theme-foot\"><button class=\"ah-profile-theme-cancel\" type=\"button\">" + window.AlexiHubI18n.t("common.actions.cancel") + "</button><button id=\"ah-profile-theme-apply\" class=\"ah-profile-theme-apply\" type=\"button\" disabled>" + window.AlexiHubI18n.t("common.text.application") + "</button></footer></div>");
    document.body.appendChild(picker);
    picker.addEventListener('click',event=>{if(event.target===picker)closePicker();const tile=event.target.closest('[data-theme-id]');if(tile&&!tile.classList.contains('locked'))selectTheme(tile.dataset.themeId);if(event.target.closest('[data-theme-shop]'))$('#ah-profile-theme-store')?.scrollIntoView({behavior:'smooth',block:'start'});});
    $('.ah-profile-theme-close',picker).addEventListener('click',closePicker);$('.ah-profile-theme-cancel',picker).addEventListener('click',closePicker);$('#ah-profile-theme-apply',picker).addEventListener('click',applySelected);
  }
  function previewProfileData(){
    const avatar=$('#ah-profile-preview-avatar');const banner=$('#ah-profile-preview-banner');
    const avatarUrl=(avatar?.style.backgroundImage||'').replace(/^url\(["']?|["']?\)$/g,'');const bannerUrl=(banner?.style.backgroundImage||'').replace(/^url\(["']?|["']?\)$/g,'');
    const name=$('#ah-display-name-preview')?.textContent?.trim()||$('#ah-display-name-input')?.value?.trim()||window.AlexiHubI18n.t("settings.profile");
    const username=$('#ah-display-name-editor')?.dataset.profileUsername||document.querySelector('[data-mini-username]')?.dataset.miniUsername||'username';
    const av=$('#ah-profile-theme-preview-avatar'),bn=$('#ah-profile-theme-preview-banner');if(av)av.style.backgroundImage=avatarUrl?`url("${safeUrl(avatarUrl)}")`:'';if(bn)bn.style.backgroundImage=bannerUrl?`url("${safeUrl(bannerUrl)}")`:'';
    const nameNode=$('#ah-profile-theme-preview-name'),handle=$('#ah-profile-theme-preview-handle');if(nameNode)nameNode.textContent=name;if(handle)handle.textContent='@'+String(username).replace(/^@/,'');
  }
  function tileMarkup(theme,locked){
    const image=theme.image_url?`style="background-image:url('${attr(safeUrl(theme.image_url))}')"`:'';const lock=locked?'<span class="lock"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 9h-1V7a4 4 0 0 0-8 0v2H7a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2Zm-7-2a2 2 0 1 1 4 0v2h-4V7Z"/></svg></span>':'';
    return `<div class="ah-profile-theme-tile-wrap"><button type="button" class="ah-profile-theme-tile${locked?' locked':''}${Number(selectedId)===Number(theme.id)?' selected':''}" data-theme-id="${Number(theme.id)}" ${locked?'aria-disabled="true"':''} ${image}>${lock}<span class="mini-card"></span></button><span class="ah-profile-theme-label" title="${attr(theme.title)}">${esc(theme.title)}</span></div>`;
  }
  function render(){
    const list=$('#ah-profile-theme-list');if(!list)return;if(loading){list.innerHTML=('' + "<div class=\"ah-profile-theme-loading\">" + window.AlexiHubI18n.t("common.text.secess_topics") + "</div>");return;}if(!library){list.innerHTML=('' + "<div class=\"ah-profile-theme-error\">" + window.AlexiHubI18n.t("common.text.narious_not_to_upload_the_topics") + "<br><button type=\"button\" data-theme-retry>" + window.AlexiHubI18n.t("common.actions.retry") + "</button></div>");list.querySelector('[data-theme-retry]')?.addEventListener('click',loadLibrary);return;}
    const owned=library.owned_themes||[];const store=(library.store_themes||[]).filter(theme=>!theme.owned);
    list.innerHTML=('' + "<h3 class=\"ah-profile-theme-section-title\">" + window.AlexiHubI18n.t("common.text.your_themes") + "</h3><div class=\"ah-profile-theme-grid\"><div class=\"ah-profile-theme-tile-wrap\"><button type=\"button\" class=\"ah-profile-theme-tile none" + String((selectedId===null?' selected':'')) + "\" data-theme-id=\"\"><span></span></button><span class=\"ah-profile-theme-label\">" + window.AlexiHubI18n.t("common.text.down_with_void") + "</span></div><div class=\"ah-profile-theme-tile-wrap\"><button type=\"button\" class=\"ah-profile-theme-tile shop\" data-theme-shop aria-label=\"" + window.AlexiHubI18n.t("common.text.shop") + "\">▦</button><span class=\"ah-profile-theme-label\">" + window.AlexiHubI18n.t("common.text.shop") + "</span></div>" + String((owned.map(theme=>tileMarkup(theme,false)).join(''))) + "</div><h3 id=\"ah-profile-theme-store\" class=\"ah-profile-theme-section-title store\">" + window.AlexiHubI18n.t("common.text.visit_the_shop") + "</h3><div class=\"ah-profile-theme-grid\">" + String((store.length?store.map(theme=>tileMarkup(theme,true)).join(''):('' + "<div class=\"ah-profile-theme-empty\">" + window.AlexiHubI18n.t("common.text.all_themes_available_are_already_in_your_collection") + "</div>"))) + '</div>');
    updatePreview();
  }
  function findTheme(id){if(id===null)return null;return [...(library?.owned_themes||[]),...(library?.store_themes||[])].find(theme=>Number(theme.id)===Number(id))||null;}
  function selectTheme(raw){selectedId=raw===''?null:Number(raw);render();}
  function updatePreview(){previewProfileData();const card=$('#ah-profile-theme-preview');if(!card)return;const theme=findTheme(selectedId);card.style.setProperty('--picker-theme-image',theme?.image_url?`url("${safeUrl(theme.image_url)}")`:'none');card.style.setProperty('--picker-theme-overlay',String(Math.max(0,Math.min(90,Number(theme?.overlay_strength??0)))/100));card.style.setProperty('--picker-theme-accent',theme?.accent_color||'#5865f2');card.style.setProperty('--picker-theme-text',theme?.text_color||'#ffffff');const apply=$('#ah-profile-theme-apply');if(apply)apply.disabled=(selectedId===null&&currentId===null)||Number(selectedId)===Number(currentId);}
  async function loadLibrary(){if(loading)return;loading=true;render();try{library=await api('/community/api/profile-themes/library');currentId=library.equipped_theme_id?Number(library.equipped_theme_id):null;selectedId=currentId;}catch(error){library=null;toast(error.message);}finally{loading=false;render();}}
  function openPicker(){closeMenu();ensurePicker();const picker=$('#ah-profile-theme-picker');picker.classList.add('open');picker.setAttribute('aria-hidden','false');previewProfileData();loadLibrary();}
  function closePicker(){const picker=$('#ah-profile-theme-picker');picker?.classList.remove('open');picker?.setAttribute('aria-hidden','true');selectedId=currentId;}
  async function applySelected(){const button=$('#ah-profile-theme-apply');if(button?.disabled)return;if(button){button.disabled=true;button.textContent=window.AlexiHubI18n.t("common.label.used");}try{const data=await api('/community/api/profile-themes/equip',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({theme_id:selectedId})});currentId=selectedId;library.equipped_theme_id=currentId;(library.owned_themes||[]).forEach(theme=>theme.equipped=Number(theme.id)===Number(currentId));const themeApi=window.AlexiHubProfileThemes;if(themeApi?.clearPreview)themeApi.clearPreview();if(themeApi?.invalidate)themeApi.invalidate();if(!themeApi?.clearPreview&&themeApi?.preview)themeApi.preview(data.theme||null);toast(selectedId===null?window.AlexiHubI18n.t("common.message.the_theme_of_the_mini_profile_is_disabled"):window.AlexiHubI18n.t("common.message.the_theme_of_the_mini_profile_is_applied"));closePicker();}catch(error){toast(error.message);}finally{if(button){button.textContent=window.AlexiHubI18n.t("common.text.application");button.disabled=false;}updatePreview();}}
  function bindBanner(){const banner=$('#ah-profile-preview-banner');if(!banner||banner.dataset.themeMenuBound==='1')return;banner.dataset.themeMenuBound='1';banner.setAttribute('aria-haspopup','menu');banner.addEventListener('click',event=>{if(event.target.closest('#ah-banner-action-menu'))return;event.preventDefault();event.stopPropagation();openMenu(banner);},true);}
  document.addEventListener('click',event=>{if(!event.target.closest('#ah-banner-action-menu,#ah-profile-preview-banner'))closeMenu();});
  document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;if($('#ah-profile-theme-picker')?.classList.contains('open')){event.preventDefault();event.stopPropagation();closePicker();}else closeMenu();},true);
  document.addEventListener('alexihub:profile-theme-library-changed',()=>{library=null;if($('#ah-profile-theme-picker')?.classList.contains('open'))loadLibrary();});
  document.addEventListener('DOMContentLoaded',bindBanner);bindBanner();
  window.openProfileThemePicker=openPicker;
})();

/* ===== script block #26 (id: ah-global-overlay-js) ===== */
(function(){
  'use strict';
  if(window.AlexiHubGlobalOverlay) return;
  var messageHideTimer=null,messageRefreshTimer=null,pollHideTimer=null,activePollId='',myVote=null,voteBusy=false;
  function initial(author){var v=String((author&&author.display_name)||(author&&author.username)||'A').trim();return v?v.charAt(0).toUpperCase():'A'}
  function setAvatar(root,author){var fallback=root.querySelector('[data-fallback]'),img=root.querySelector('img'),url=String(author&&author.avatar_url||'').trim();if(fallback)fallback.textContent=initial(author);if(!img)return;img.hidden=true;img.onload=null;img.onerror=null;img.removeAttribute('src');if(url){img.onload=function(){img.hidden=false};img.onerror=function(){img.hidden=true;img.removeAttribute('src')};img.src=url}}
  function ensureMessage(){var node=document.getElementById('ah-global-message');if(node)return node;node=document.createElement('div');node.id='ah-global-message';node.setAttribute('role','status');node.setAttribute('aria-live','assertive');node.innerHTML='<div class="ah-global-message-avatar"><span data-fallback>A</span><img alt="" hidden></div><div class="ah-global-message-line"><span class="ah-global-message-name"></span><span class="ah-global-message-colon">:</span><span class="ah-global-message-text"></span></div>';document.body.appendChild(node);return node}
  function ensurePoll(){var node=document.getElementById('ah-global-poll');if(node)return node;node=document.createElement('section');node.id='ah-global-poll';node.setAttribute('aria-live','polite');node.innerHTML=('' + "<div class=\"ah-global-poll-author\"><div class=\"ah-global-poll-avatar\"><span data-fallback>A</span><img alt=\"\" hidden></div><span class=\"ah-global-poll-author-name\"></span></div><div class=\"ah-global-poll-question\"></div><div class=\"ah-global-poll-options\"><button class=\"ah-global-poll-option\" type=\"button\" data-vote=\"a\"><span class=\"ah-global-poll-label\"></span><b class=\"ah-global-poll-percent\">50%</b></button><button class=\"ah-global-poll-option\" type=\"button\" data-vote=\"b\"><span class=\"ah-global-poll-label\"></span><b class=\"ah-global-poll-percent\">50%</b></button></div><div class=\"ah-global-poll-track\"><div class=\"ah-global-poll-fill-a\"></div><i class=\"ah-global-poll-divider\"></i></div><div class=\"ah-global-poll-footer\"><span class=\"ah-global-poll-count\">" + window.AlexiHubI18n.t("common.text.votes_0") + "</span><span class=\"ah-global-poll-status\">" + window.AlexiHubI18n.t("common.text.voting_is_coming") + "</span></div>");node.querySelectorAll('[data-vote]').forEach(function(btn){btn.addEventListener('click',function(){vote(btn.dataset.vote)})});document.body.appendChild(node);return node}
  function toast(text){if(typeof window.showActionToast==='function'){window.showActionToast(text);return}var n=document.createElement('div');n.textContent=text;n.style.cssText='position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483647;background:#232428;color:#fff;border:1px solid #3b3d45;border-radius:10px;padding:9px 12px;font:700 12px Inter,system-ui;box-shadow:0 12px 34px rgba(0,0,0,.4)';document.body.appendChild(n);setTimeout(function(){n.remove()},2200)}
  function showMessage(payload){var item=payload&&payload.message?payload.message:(payload||{}),text=String(item.text||'').trim();if(!text)return;var author=item.author||{},node=ensureMessage();node.querySelector('.ah-global-message-name').textContent=String(author.display_name||author.username||'AlexiHub');node.querySelector('.ah-global-message-text').textContent=text;setAvatar(node.querySelector('.ah-global-message-avatar'),author);var visible=node.classList.contains('show');if(messageHideTimer)clearTimeout(messageHideTimer);if(messageRefreshTimer)clearTimeout(messageRefreshTimer);node.classList.remove('refresh');if(visible){void node.offsetWidth;node.classList.add('show','refresh');messageRefreshTimer=setTimeout(function(){node.classList.remove('refresh')},240)}else node.classList.add('show');messageHideTimer=setTimeout(function(){node.classList.remove('show')},7000)}
  function pct(value,fallback){var n=Number(value);if(!Number.isFinite(n))n=fallback;return Math.max(0,Math.min(100,n))}
  function schedulePollFallback(endsAt){if(pollHideTimer)clearTimeout(pollHideTimer);var when=Date.parse(String(endsAt||''));if(!Number.isFinite(when))return;pollHideTimer=setTimeout(function(){var node=document.getElementById('ah-global-poll');if(node)node.classList.remove('show');activePollId='';myVote=null},Math.max(0,when-Date.now()+2200))}
  function renderPoll(poll){if(!poll||!poll.id)return;var id=String(poll.id),same=id===activePollId;if(!same){activePollId=id;myVote=poll.my_vote||null}else if(poll.my_vote)myVote=poll.my_vote;var node=ensurePoll(),author=poll.author||{},a=pct(poll.percent_a,50),b=pct(poll.percent_b,100-a);node.classList.remove('ended');node.querySelector('.ah-global-poll-author-name').textContent=String(author.display_name||author.username||'AlexiHub');setAvatar(node.querySelector('.ah-global-poll-avatar'),author);node.querySelector('.ah-global-poll-question').textContent=String(poll.question||'');var btnA=node.querySelector('[data-vote="a"]'),btnB=node.querySelector('[data-vote="b"]');btnA.querySelector('.ah-global-poll-label').textContent=String(poll.option_a||window.AlexiHubI18n.t("common.text.option_1"));btnB.querySelector('.ah-global-poll-label').textContent=String(poll.option_b||window.AlexiHubI18n.t("common.text.option_2"));btnA.querySelector('.ah-global-poll-percent').textContent=Math.round(a)+'%';btnB.querySelector('.ah-global-poll-percent').textContent=Math.round(b)+'%';btnA.classList.toggle('selected',myVote==='a');btnB.classList.toggle('selected',myVote==='b');btnA.disabled=voteBusy;btnB.disabled=voteBusy;node.querySelector('.ah-global-poll-fill-a').style.width=a+'%';node.querySelector('.ah-global-poll-divider').style.left=a+'%';node.querySelector('.ah-global-poll-count').textContent=window.AlexiHubI18n.t("common.text.votes")+Number(poll.total_votes||0);node.querySelector('.ah-global-poll-status').textContent=window.AlexiHubI18n.t("common.text.voting_is_coming");node.classList.add('show');schedulePollFallback(poll.ends_at)}
  async function vote(side){if(voteBusy||!activePollId)return;voteBusy=true;var node=ensurePoll();node.querySelectorAll('[data-vote]').forEach(function(btn){btn.disabled=true});try{var response=await fetch('/community/api/global-console/poll/'+encodeURIComponent(activePollId)+'/vote',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({option:side})});var data=await response.json().catch(function(){return{}});if(!response.ok||!data.ok)throw new Error(data.error||'vote_failed');myVote=side;if(data.poll)renderPoll(data.poll)}catch(error){toast(error&&error.message==='poll_ended'?window.AlexiHubI18n.t("common.message.the_vote_is_now_in_place"):window.AlexiHubI18n.t("common.message.narity_could_not_save_the_voice"))}finally{voteBusy=false;if(activePollId){node.querySelectorAll('[data-vote]').forEach(function(btn){btn.disabled=false})}}
  }
  function endPoll(poll){if(!poll||!poll.id)return;var id=String(poll.id);if(activePollId&&activePollId!==id)return;renderPoll(poll);var node=ensurePoll();node.classList.add('ended');node.querySelectorAll('[data-vote]').forEach(function(btn){btn.disabled=true});node.querySelector('.ah-global-poll-status').textContent=window.AlexiHubI18n.t("common.label.voting_completed");if(pollHideTimer)clearTimeout(pollHideTimer);pollHideTimer=setTimeout(function(){node.classList.remove('show');activePollId='';myVote=null},1700)}
  function handle(data){if(!data||typeof data!=='object')return false;if(data.type==='global_console_message'){showMessage(data);return true}if(data.type==='global_poll_start'||data.type==='global_poll_update'||data.type==='global_poll_snapshot'){renderPoll(data.poll||{});return true}if(data.type==='global_poll_end'){endPoll(data.poll||{});return true}return false}
  window.AlexiHubGlobalOverlay={handle:handle,showMessage:showMessage,renderPoll:renderPoll};
})();

/* ===== script block #27 (id: anon-27) ===== */
(function(){
  'use strict';
  if(window.__alexTravelAnnouncementRealtimeStarted) return;
  window.__alexTravelAnnouncementRealtimeStarted=true;

  var socket=null;
  var reconnectTimer=null;
  var reconnectDelay=900;
  var pingTimer=null;
  var closing=false;

  function scheduleReconnect(){
    if(closing) return;
    clearTimeout(reconnectTimer);
    reconnectTimer=setTimeout(connect,reconnectDelay);
    reconnectDelay=Math.min(8000,Math.round(reconnectDelay*1.55));
  }

  function connect(){
    if(closing) return;
    clearTimeout(reconnectTimer);
    var protocol=location.protocol==='https:'?'wss:':'ws:';
    var nextSocket;
    try{
      nextSocket=new WebSocket(protocol+'//'+location.host+'/community/ws/account');
    }catch(error){
      scheduleReconnect();
      return;
    }
    socket=nextSocket;
    nextSocket.addEventListener('open',function(){
      if(socket!==nextSocket) return;
      reconnectDelay=900;
      clearInterval(pingTimer);
      pingTimer=setInterval(function(){
        if(socket===nextSocket&&nextSocket.readyState===WebSocket.OPEN){
          nextSocket.send(JSON.stringify({type:'ping'}));
        }
      },25000);
    });
    nextSocket.addEventListener('message',function(event){
      if(socket!==nextSocket) return;
      var data;
      try{data=JSON.parse(event.data);}catch(error){return;}
      if(data&&window.AlexiHubGlobalOverlay&&window.AlexiHubGlobalOverlay.handle(data)) return;
    });
    nextSocket.addEventListener('close',function(){
      if(socket===nextSocket) socket=null;
      clearInterval(pingTimer);
      scheduleReconnect();
    });
    nextSocket.addEventListener('error',function(){
      try{nextSocket.close();}catch(error){}
    });
  }

  connect();
  window.addEventListener('pagehide',function(){
    closing=true;
    clearTimeout(reconnectTimer);
    clearInterval(pingTimer);
    try{
      if(socket&&socket.readyState===WebSocket.OPEN){
        socket.send(JSON.stringify({type:'leave'}));
        socket.close(1000,'pagehide');
      }
    }catch(error){}
  });
})();

/* ===== script #28 (id: anon-28) ===== */
window.__AH_BOOST_V2_MARKUP=('' + "<svg width=\"0\" height=\"0\" style=\"position:absolute\" aria-hidden=\"true\" focusable=\"false\"><symbol id=\"ah-boost-crystal-v2\" viewBox=\"0 0 24 28\"><polygon points=\"12,1 21,8 18,21 12,27 6,21 3,8\" fill=\"#c337d8\"/><polygon points=\"12,1 21,8 12,10\" fill=\"#ff86f4\"/><polygon points=\"12,1 12,10 3,8\" fill=\"#e95eea\"/><polygon points=\"3,8 12,10 8,20 6,21\" fill=\"#9b2bc6\"/><polygon points=\"21,8 12,10 16,20 18,21\" fill=\"#e349df\"/><polygon points=\"12,10 16,20 12,27 8,20\" fill=\"#bd33e0\"/><polyline points=\"12,1 21,8 18,21 12,27 6,21 3,8 12,1\" fill=\"none\" stroke=\"rgba(255,255,255,.38)\" stroke-width=\".8\"/><path d=\"M3 8h18M12 1v9M12 10 8 20m4-10 4 10\" fill=\"none\" stroke=\"rgba(255,255,255,.18)\" stroke-width=\".7\"/></symbol></svg>\n<div class=\"ah-boost-page-v2\">\n  <div class=\"ah-boost-page-title\"><h1><svg class=\"ah-boost-crystal-v2 title\" viewBox=\"0 0 24 28\" aria-hidden=\"true\"><use href=\"#ah-boost-crystal-v2\"></use></svg><span>" + window.AlexiHubI18n.t("common.text.servan_bones_of_the_server") + "</span></h1><p>" + window.AlexiHubI18n.t("common.text.remain_loved_by_the_social_networks_and_open_the_bonuses") + "</p></div>\n\n  <section class=\"ah-boost-nitro-banner\">\n    <div class=\"ah-boost-nitro-icon\"><svg class=\"ah-boost-crystal-v2 nitro\" viewBox=\"0 0 24 28\" aria-hidden=\"true\"><use href=\"#ah-boost-crystal-v2\"></use></svg></div>\n    <div class=\"ah-boost-nitro-copy\"><strong>" + window.AlexiHubI18n.t("common.text.ycehale_2_boosts_with_nitro") + "</strong><span id=\"ah-boost-nitro-caption\">" + window.AlexiHubI18n.t("common.text.subscription_nitro_includes_2_boosts") + "</span></div>\n    <button class=\"ah-boost-nitro-action\" type=\"button\" onclick=\"window.showSettingsTab?.('subscriptions')\">" + window.AlexiHubI18n.t("common.text.subscribe_to_nitro") + "</button>\n  </section>\n\n  <section class=\"ah-boost-section-v2\">\n    <h2>" + window.AlexiHubI18n.t("common.text.these_servers_will_not_be_prevented_by_the_bus") + "</h2>\n    <div id=\"ah-user-boost-summary\" class=\"ah-boost-account-summary\"><div class=\"ah-boost-summary-gem\"><svg class=\"ah-boost-crystal-v2 small\" viewBox=\"0 0 24 28\" aria-hidden=\"true\"><use href=\"#ah-boost-crystal-v2\"></use></svg></div><div><strong>" + window.AlexiHubI18n.t("common.text.download_the_bets") + "</strong><span>" + window.AlexiHubI18n.t("common.text.ygiving_the_available_limit") + "</span></div></div>\n    <div id=\"ah-user-boost-list\" class=\"ah-boost-server-list-v2\"><div class=\"ah-user-boost-loading\">" + window.AlexiHubI18n.t("common.text.servail_download") + "</div></div>\n  </section>\n\n  <section class=\"ah-boost-section-v2 ah-boost-promo-section\">\n    <div class=\"ah-boost-section-heading\"><div><h2>" + window.AlexiHubI18n.t("common.text.promo_codes_of_the_bushes") + "</h2><p>" + window.AlexiHubI18n.t("common.text.activate_a_single_use_code_or_if_you_have_access") + "</p></div></div>\n    <div class=\"ah-boost-code-card\">\n      <div class=\"ah-boost-code-copy\"><b>" + window.AlexiHubI18n.t("common.text.activate_the_promo_code_of_busts") + "</b><span>" + window.AlexiHubI18n.t("common.text.each_code_is_one_time_bsite_boosts_will_forever_be") + "</span></div>\n      <div class=\"ah-boost-code-form\"><input id=\"ah-boost-code-input\" class=\"ah-boost-code-input code\" maxlength=\"40\" autocomplete=\"off\" placeholder=\"ALEXI-BOOST-AAAAA-BBBBB-CCCCC\"><button id=\"ah-boost-redeem-btn\" class=\"ah-boost-code-button\" type=\"button\" onclick=\"redeemBoostCode()\">" + window.AlexiHubI18n.t("common.text.activate") + "</button></div>\n      <div id=\"ah-boost-code-status\" class=\"ah-boost-code-status\"></div>\n      <div id=\"ah-boost-code-generator\" class=\"ah-boost-code-generator\"><div class=\"ah-boost-code-copy\"><b>" + window.AlexiHubI18n.t("common.text.generalize_disposable_code") + "</b><span>" + window.AlexiHubI18n.t("common.text.all_available_to_verified_users") + "</span></div><div class=\"ah-boost-code-generator-grid\"><input id=\"ah-boost-gen-amount\" class=\"ah-boost-code-input\" type=\"number\" min=\"1\" max=\"100\" value=\"2\" aria-label=\"" + window.AlexiHubI18n.t("common.aria.the_number_of_beads") + "\"><input id=\"ah-boost-gen-note\" class=\"ah-boost-code-input\" maxlength=\"255\" placeholder=\"" + window.AlexiHubI18n.t("common.placeholder.note_not_necessarily") + "\"><button id=\"ah-boost-generate-btn\" class=\"ah-boost-code-button\" type=\"button\" onclick=\"generateBoostCode()\">" + window.AlexiHubI18n.t("common.text.generate") + "</button></div><div id=\"ah-boost-code-output\" class=\"ah-boost-code-output\"><input id=\"ah-boost-generated-code\" class=\"ah-boost-code-input code\" readonly><button class=\"ah-boost-code-button secondary\" type=\"button\" onclick=\"copyGeneratedBoostCode()\">" + window.AlexiHubI18n.t("common.text.copy") + "</button></div></div>\n    </div>\n  </section>\n\n  <section class=\"ah-boost-section-v2\">\n    <h2>" + window.AlexiHubI18n.t("common.text.bonuses") + "</h2>\n    <div class=\"ah-boost-benefits-table\" role=\"table\" aria-label=\"" + window.AlexiHubI18n.t("common.aria.nerreserve_levels_bonuses") + "\">\n      <div class=\"head label\"><b>" + window.AlexiHubI18n.t("common.text.bonuses") + "</b></div><div class=\"head\"><b>" + window.AlexiHubI18n.t("common.text.level_1") + "</b><span>" + window.AlexiHubI18n.t("common.text.2_busts") + "</span></div><div class=\"head\"><b>" + window.AlexiHubI18n.t("common.text.level_2") + "</b><span>" + window.AlexiHubI18n.t("common.text.5_busts") + "</span></div><div class=\"head\"><b>" + window.AlexiHubI18n.t("common.text.level_3") + "</b><span>" + window.AlexiHubI18n.t("common.text.7_busts") + "</span></div>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.slots_for_emoji") + "</div><strong>100</strong><strong>150</strong><strong>250</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.slots_for_dockers") + "</div><strong>15</strong><strong>30</strong><strong>60</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.max_size_of_downloaded_files") + "</div><strong>" + window.AlexiHubI18n.t("common.text.25_mb") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.50_mb") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.100_mb") + "</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.quality_of_streams") + "</div><strong>" + window.AlexiHubI18n.t("common.text.720p_60_fps") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.1080p_60_fps") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.1080p_60_fps") + "</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.sound_quality") + "</div><strong>" + window.AlexiHubI18n.t("common.text.128_kbit_s") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.256_kbit_s") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.384_kbit_s") + "</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.animated_server_icon") + "</div><strong class=\"check\">✓</strong><strong class=\"check\">✓</strong><strong class=\"check\">✓</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.sightedness_of_user_roles") + "</div><strong class=\"muted\">—</strong><strong class=\"check\">✓</strong><strong class=\"check\">✓</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.server_banner") + "</div><strong class=\"muted\">—</strong><strong>" + window.AlexiHubI18n.t("common.text.statistic") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.animated") + "</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.user_link_invitation") + "</div><strong class=\"muted\">—</strong><strong class=\"muted\">—</strong><strong class=\"check\">✓</strong>\n      <div class=\"label\">" + window.AlexiHubI18n.t("common.text.the_server_tag") + "</div><strong>" + window.AlexiHubI18n.t("common.text.3_bust") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.accessible") + "</strong><strong>" + window.AlexiHubI18n.t("common.text.accessible") + "</strong>\n    </div>\n  </section>\n\n  <section class=\"ah-boost-section-v2\">\n    <h2>" + window.AlexiHubI18n.t("common.text.give_your_community_a_bush") + "</h2>\n    <div class=\"ah-boost-feature-grid\">\n      <article><div class=\"icon\"><svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M12 3 5 7v5c0 4.6 2.7 7.2 7 9 4.3-1.8 7-4.4 7-9V7l-7-4Z\"/><path d=\"m9 12 2 2 4-5\"/></svg></div><p>" + window.AlexiHubI18n.t("common.text.show_your_support_with_the_icon_in_the_list_of") + "</p></article>\n      <article><div class=\"icon sparkle\"><svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M12 3 9.8 7.5 5 8.2l3.5 3.4-.8 4.8 4.3-2.3 4.3 2.3-.8-4.8L19 8.2l-4.8-.7L12 3Z\"/><path d=\"M12 17v4\"/></svg></div><p>" + window.AlexiHubI18n.t("common.text.ygive_you_a_badge_in_the_profile_which_will_change") + "</p></article>\n      <article><div class=\"icon\"><svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M4 7h16v11H4z\"/><path d=\"m8 7 2-4h4l2 4M8 12h8\"/></svg></div><p>" + window.AlexiHubI18n.t("common.text.an_exclusive_role_of_a_booster_in_a_supported_community") + "</p></article>\n      <article><div class=\"icon heart\"><svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M12 21s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 5.5-7 10-7 10Z\"/></svg></div><p>" + window.AlexiHubI18n.t("common.text.support_your_community_and_help_us_to_open_up_new") + "</p></article>\n    </div>\n  </section>\n\n  <section class=\"ah-boost-section-v2 ah-boost-faq-section\">\n    <h2>" + window.AlexiHubI18n.t("common.text.chavo_by_bust") + "</h2>\n    <div class=\"ah-boost-faq\" data-boost-faq>\n      <article><button type=\"button\" data-boost-faq-toggle aria-expanded=\"false\"><span>" + window.AlexiHubI18n.t("common.text.what_is_a_server_boost") + "</span><svg viewBox=\"0 0 24 24\"><path d=\"m7 9 5 5 5-5\"/></svg></button><div class=\"answer\"><p>" + window.AlexiHubI18n.t("common.text.a_bush_is_a_participants_contribution_to_the_server_the") + "</p></div></article>\n      <article><button type=\"button\" data-boost-faq-toggle aria-expanded=\"false\"><span>" + window.AlexiHubI18n.t("common.text.how_to_give_the_server_a_boost") + "</span><svg viewBox=\"0 0 24 24\"><path d=\"m7 9 5 5 5-5\"/></svg></button><div class=\"answer\"><p>" + window.AlexiHubI18n.t("common.text.your_server_in_the_list_above_and_click_give_up") + "</p></div></article>\n      <article><button type=\"button\" data-boost-faq-toggle aria-expanded=\"false\"><span>" + window.AlexiHubI18n.t("common.text.yrpised_to_give_a_server_a_few_busts") + "</span><svg viewBox=\"0 0 24 24\"><path d=\"m7 9 5 5 5-5\"/></svg></button><div class=\"answer\"><p>" + window.AlexiHubI18n.t("common.text.yes_yes_the_account_has_several_free_jets_left_they") + "</p></div></article>\n      <article><button type=\"button\" data-boost-faq-toggle aria-expanded=\"false\"><span>" + window.AlexiHubI18n.t("common.text.what_happens_if_you_take_the_bus") + "</span><svg viewBox=\"0 0 24 24\"><path d=\"m7 9 5 5 5-5\"/></svg></button><div class=\"answer\"><p>" + window.AlexiHubI18n.t("common.text.the_bush_will_return_to_the_available_account_limit_the") + "</p></div></article>\n      <article><button type=\"button\" data-boost-faq-toggle aria-expanded=\"false\"><span>" + window.AlexiHubI18n.t("common.text.what_is_the_difference_between_zxq0xz_a_promo_code") + "</span><svg viewBox=\"0 0 24 24\"><path d=\"m7 9 5 5 5-5\"/></svg></button><div class=\"answer\"><p>" + window.AlexiHubI18n.t("common.text.the_nitro_gives_boosts_as_part_of_an_active_subscription") + "</p></div></article>\n    </div>\n  </section>\n</div>");

/* ===== script block #29 (id: ah-boost-page-v2-js) ===== */
(function(){
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const crystal=(cls='small')=>`<svg class="ah-boost-crystal-v2 ${cls}" viewBox="0 0 24 28" aria-hidden="true"><use href="#ah-boost-crystal-v2"></use></svg>`;
  function ensureV2(){const page=document.querySelector('[data-settings-page="boosts"]');if(!page)return null;if(!page.querySelector('.ah-boost-page-v2'))page.innerHTML=window.__AH_BOOST_V2_MARKUP||'';return page}
  function render(data){
    const page=ensureV2(),summary=document.getElementById('ah-user-boost-summary'),list=document.getElementById('ah-user-boost-list');if(!page||!summary||!list)return;
    const active=!!data.subscription?.active,promo=Number(data.promo_boosts||0),nitro=Number(data.nitro_capacity||0),capacity=Number(data.capacity||0),remaining=Number(data.remaining||0),label=data.subscription?.tier_label||'Nitro';
    const source=active?(promo?window.AlexiHubI18n.t("common.text.ycbod_promo_codes", {p0:(esc(label)),p1:(nitro),p2:(promo)}):esc(label)):(promo?window.AlexiHubI18n.t("common.text.promo_codes", {p0:(promo)}):window.AlexiHubI18n.t("common.text.activate_nitro_or_promo_code_to_get_boosts"));
    summary.innerHTML=`<div class="ah-boost-summary-gem">${crystal()}</div><div><strong>${capacity?window.AlexiHubI18n.t("common.text.available_limit_bus", {p0:(capacity)}):window.AlexiHubI18n.t("common.text.bit_boosts_with_nitro_or_promo_code")}</strong><span>${capacity?window.AlexiHubI18n.t("common.text.used_zxqxz_freely", {p0:(source),p1:(Number(data.allocated||0)),p2:(remaining)}):source}</span></div>`;
    const caption=document.getElementById('ah-boost-nitro-caption');if(caption)caption.textContent=active?window.AlexiHubI18n.t("common.text.s_zxqxz_zxqxz_zxqxz_buses_on_enabled", {p0:(label),p1:(nitro),p2:(remaining)}):window.AlexiHubI18n.t("common.text.subscription_nitro_includes_2_boosts");
    document.getElementById('ah-boost-code-generator')?.classList.toggle('show',!!data.can_generate);
    const servers=Array.isArray(data.servers)?data.servers:[];if(!servers.length){list.innerHTML=('' + "<div class=\"ah-user-boost-empty\">" + window.AlexiHubI18n.t("common.text.you_are_not_yet_a_single_server") + "</div>");return}
    list.innerHTML=servers.map(server=>{const id=Number(server.id)||0,icon=server.icon_url?`style="background-image:url('${esc(String(server.icon_url).replace(/'/g,'%27'))}')"`:'',tag=server.tag?.active?`<span class="ah-user-server-tag${server.tag_selected?' selected':''}">${esc(server.tag.icon_glyph||server.tag.glyph||'◆')} ${esc(server.tag.text||'TAG')}${server.tag_selected?window.AlexiHubI18n.t("common.text.selected"):''}</span>`:'';return ('' + "<div class=\"ah-boost-server-row-v2\"><div class=\"ah-boost-server-icon-v2\" " + String((icon)) + ">" + String((server.icon_url?'':esc(String(server.name||'?').slice(0,2).toUpperCase()))) + "</div><div class=\"ah-boost-server-copy-v2\"><b>" + String((esc(server.name))) + "</b><small>" + window.AlexiHubI18n.t("common.text.cast_zxqqxz_ruspers_yours", {p3:(crystal()),p4:(Number(server.total_boosts||0)),p5:(server.level?Number(server.level)+window.AlexiHubI18n.t("common.text.a_level"):window.AlexiHubI18n.t("common.text.numn_level_not_open")),p6:(Number(server.allocated_here||0))}) + "</small>" + String((tag)) + "</div><div class=\"ah-boost-server-actions-v2\"><button type=\"button\" onclick=\"window.__ahBoostV2Change(" + String((id)) + ",-1)\" " + String((Number(server.allocated_here||0)<1?'disabled':'')) + ">" + window.AlexiHubI18n.t("common.text.picked") + "</button><button class=\"primary\" type=\"button\" onclick=\"window.__ahBoostV2Change(" + String((id)) + ",1)\" " + String((remaining<1?'disabled':'')) + ">" + window.AlexiHubI18n.t("common.text.supposce_the_belt_of_this_server") + "</button>" + String((server.tag?.active?`<button type="button" title="${server.tag_selected?window.AlexiHubI18n.t("common.text.sume_tagging"):window.AlexiHubI18n.t("common.text.show_tag_near_the_name")}" onclick="window.__ahBoostV2Tag(${server.tag_selected?'null':id})">${server.tag_selected?window.AlexiHubI18n.t("common.text.tag"):window.AlexiHubI18n.t("common.text.select_tag")}</button>`:'')) + "</div></div>")}).join('');
  }
  async function api(url,options){const r=await fetch(url,Object.assign({credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json',Accept:'application/json'}},options||{})),d=await r.json().catch(()=>({}));if(!r.ok||d.ok===false){const e=new Error(d.error||d.detail||'request_failed');e.code=d.error;e.userMessage=d.message;throw e}return d}
  const oldLoad=window.loadMyBoostSettings;
  window.loadMyBoostSettings=async function(){const page=ensureV2(),list=document.getElementById('ah-user-boost-list');if(list)list.innerHTML=('' + "<div class=\"ah-user-boost-loading\">" + window.AlexiHubI18n.t("common.text.servail_download") + "</div>");try{render(await api('/community/api/boosts/me'))}catch(e){if(list)list.innerHTML=('' + "<div class=\"ah-user-boost-empty\">" + window.AlexiHubI18n.t("common.text.ner_efforts_to_load_the_bus_ynain_s_trying_again") + "</div>")}};
  window.__ahBoostV2Change=async function(serverId,delta){try{await api(`/community/api/servers/${serverId}/boosts`,{method:'POST',body:JSON.stringify({delta:Number(delta)})});if(typeof window.refreshCurrentServerBoostStatus==='function')window.refreshCurrentServerBoostStatus().catch(()=>{});await window.loadMyBoostSettings()}catch(e){const map={nitro_required:window.AlexiHubI18n.t("common.text.active_subscription_nitro_or_promo_code_of_bets_is_needed"),no_boosts_left:window.AlexiHubI18n.t("common.text.all_available_busts_are_already_distributed"),nothing_to_remove:window.AlexiHubI18n.t("common.text.nude_your_boost_on_this_server")};(window.showActionToast||window.settingsToast||console.info)(e.userMessage||map[e.code]||window.AlexiHubI18n.t("common.text.never_it_was_possible_to_change_the_bust"))}};
  window.__ahBoostV2Tag=async function(serverId){try{await api('/community/api/boosts/tag-selection',{method:'POST',body:JSON.stringify({server_id:serverId})});await window.loadMyBoostSettings()}catch(e){(window.showActionToast||window.settingsToast||console.info)(window.AlexiHubI18n.t("common.text.this_tag_is_now_unavailable"))}};
  document.addEventListener('click',e=>{const b=e.target.closest('[data-boost-faq-toggle]');if(!b)return;const item=b.closest('article'),open=b.getAttribute('aria-expanded')==='true';b.setAttribute('aria-expanded',open?'false':'true');item?.classList.toggle('open',!open)});
  document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target?.id==='ah-boost-code-input'){e.preventDefault();window.redeemBoostCode?.()}});
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ensureV2);else ensureV2();
})();

/* ===== script block #31 (id: anon-31) ===== */
/*!
 * AlexiHub — контекстне меню сервера по правому кліку на іконці в рейці (.server-rail)
 * Підключення: <script src="/static/js/server-context-menu.js" defer><\/script>
 * (або вставити цей файл прямо перед <\/body>)
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

  function openSubPanel(anchorEl, panelEl, id) {
    if (state.subFor === id) return;
    sub.innerHTML = '';
    sub.appendChild(panelEl);
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
    return panel;
  }

  function refreshNotifSubmenu(id, dot) {
    sub.innerHTML = '';
    sub.appendChild(buildNotifSubmenu(id, dot));
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
        refreshNotifSubmenu(id, dot);
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
        refreshNotifSubmenu(id, dot);
      });
      panel.appendChild(it);
    });

    var divider2 = document.createElement('div'); divider2.className = 'ahctx-divider'; panel.appendChild(divider2);

    var pushIt = makeItem({ label: t('server.ctx.push_mobile', 'Мобільні Push-сповіщення'), right: checkBox(!!p.pushMobile) });
    pushIt.addEventListener('click', function () {
      p = setServerPrefs(id, { pushMobile: !p.pushMobile });
      refreshNotifSubmenu(id, dot);
    });
    panel.appendChild(pushIt);

    return panel;
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


/* ===== script block #32 (id: ah-appearance-v2-js) ===== */
(function(){
  var SWATCH_KEY='ah_theme_swatch',SIDEBAR_KEY='ah_dark_sidebar',SYNC_DEVICES_KEY='ah_theme_sync_devices';
  var COLOR_THEMES=[
    {c:'#8fd6b0'},{c:'#f7b06b'},{c:'#8fb8ec'},{c:'#a9e2c9'},{c:'#f0b7cf'},
    {c:'#a9d8ef'},{c:'#f0e0b8'},{c:'linear-gradient(135deg,#5b2a7a,#3a2ab8)'},{c:'linear-gradient(135deg,#7a3ac9,#3ac9e6)'},
    {c:'#1f4d33'},{c:'#5c1f24'},{c:'#28245c'},{c:'#5c3a2e'},{c:'#4a5566'},
    {c:'#1f5c50'},{c:'#274a70'},{c:'linear-gradient(135deg,#2d7a6b,#7a2d6b)'},{c:'#c97b2e'},{c:'#1f3f6b'},{c:'#5c4526'}
  ];
  function buildGrid(){
    var grid=document.getElementById('ah-color-themes-grid');
    if(!grid||grid.dataset.built)return;
    grid.dataset.built='1';
    var html='<button type="button" class="ah-color-theme-tile wand" aria-label="Создать тему" onclick="window.showSettingsTab?.(\'subscriptions\')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 4V2m0 14v-2M8 9h2M20 9h2M17.8 11.8 19 13M15 9h.01M17.8 6.2 19 5M3 21l9-9M12.2 6.2 11 5"/></svg></button>';
    COLOR_THEMES.forEach(function(t,i){
      html+='<button type="button" class="ah-color-theme-tile" style="background:'+t.c+'" aria-label="Тема '+(i+1)+' (Nitro)" onclick="settingsToast(\'Доступно с подпиской Nitro\')"><span class="lock"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 9h-1V7a4 4 0 0 0-8 0v2H7a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2Zm-7-2a2 2 0 1 1 4 0v2h-4V7Z"/></svg></span></button>';
    });
    grid.innerHTML=html;
  }

  function syncUI(){
    var mode=(window.AhTheme&&window.AhTheme.get())||'dark';
    var eff=(window.AhTheme&&window.AhTheme.effective())||'dark';
    var isSystem=mode==='system';

    var sysToggle=document.getElementById('ah-theme-sync-system');
    if(sysToggle)sysToggle.checked=isSystem;
    var syncIcon=document.getElementById('ah-theme-sync-icon');
    if(syncIcon)syncIcon.classList.toggle('active',isSystem);

    var swatch=isSystem?null:(localStorage.getItem(SWATCH_KEY)||eff);
    if(!isSystem&&swatch!=='light'&&eff==='light')swatch='light';
    if(!isSystem&&swatch!=='light'&&swatch!=='dark'&&swatch!=='darker'&&swatch!=='amoled')swatch=eff;
    document.querySelectorAll('.ah-theme-swatch-btn').forEach(function(btn){
      var on=!isSystem&&btn.dataset.swatch===swatch;
      btn.classList.toggle('selected',on);
      btn.setAttribute('aria-checked',on?'true':'false');
    });

    var sidebarToggle=document.getElementById('ah-theme-dark-sidebar');
    if(sidebarToggle)sidebarToggle.checked=localStorage.getItem(SIDEBAR_KEY)==='1';

    var devicesToggle=document.getElementById('ah-theme-sync-devices');
    if(devicesToggle)devicesToggle.checked=localStorage.getItem(SYNC_DEVICES_KEY)!=='0';
  }

  function pickSwatch(name){
    if(!window.AhTheme)return;
    var family=name==='light'?'light':'dark';
    localStorage.setItem(SWATCH_KEY,name);
    window.AhTheme.set(family);
    syncUI();
  }

  function applyDarkSidebar(on){
    if(on)document.documentElement.setAttribute('data-dark-sidebar','1');
    else document.documentElement.removeAttribute('data-dark-sidebar');
    localStorage.setItem(SIDEBAR_KEY,on?'1':'0');
  }

  document.addEventListener('click',function(e){
    var swatchBtn=e.target.closest('.ah-theme-swatch-btn');
    if(swatchBtn){pickSwatch(swatchBtn.dataset.swatch);return;}
    if(e.target.closest('#ah-theme-sync-icon')){if(window.AhTheme)window.AhTheme.set('system');syncUI();return;}
  });

  document.addEventListener('change',function(e){
    if(e.target.id==='ah-theme-sync-system'){
      if(!window.AhTheme)return;
      if(e.target.checked){window.AhTheme.set('system');}
      else{window.AhTheme.set(window.AhTheme.effective());}
      syncUI();
    }else if(e.target.id==='ah-theme-dark-sidebar'){
      applyDarkSidebar(e.target.checked);
    }else if(e.target.id==='ah-theme-sync-devices'){
      localStorage.setItem(SYNC_DEVICES_KEY,e.target.checked?'1':'0');
    }
  });

  window.addEventListener('ah-theme-change',syncUI);

  function init(){
    applyDarkSidebar(localStorage.getItem(SIDEBAR_KEY)==='1');
    buildGrid();
    syncUI();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
