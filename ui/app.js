// The player itself: playback, the Now Playing pane, menus and the talk with
// the host. The other files (eq.js, library.js, radio.js, skins.js,
// premium.js) add their parts through window.Mist; Mist.boot() at the end of
// the page starts it all.
(() => {
  const $ = (id) => document.getElementById(id);
  const base = location.pathname.replace(/[^/]*$/, '');
  const audio = $('audio');
  const probe = new Audio(); probe.preload = 'metadata';

  const LISTS = { local: 'My Music', youtube: 'YouTube Music' };   // the Now Playing lists (the second holds Apple Music's and Spotify's songs too)
  const state = {
    lists: { local: [], youtube: [] },   // the songs of each, in order
    names: { local: '', youtube: '' },   // the saved playlist (or page) each came from
    show: 'local',      // the list the Now Playing pane shows
    settings: { volume: 0.5, muted: false, vis: 0, shuffle: false, repeat: false, hue: 0, zoom: 1 },
    stations: [], playlists: [], folders: [],
    cur: null,          // id of the loaded track
    station: null,      // id of the radio station tuned in (then cur is null)
    radioTitle: '',     // what the station says it is playing
    sel: null,          // id of the highlighted row
    mode: 'stopped',    // playing | paused | stopped
    view: 'now', history: [],
    heard: new Set(),   // tracks already played in this shuffle pass
    played: null,       // the last YouTube Music song that was heard
    queue: [],          // ids picked with "Play next", played before the list goes on
  };
  let ext = null;       // the Apple Music song the playback engine plays (see "Apple Music songs" below)
  const hooks = {};
  const on = (name, fn) => { (hooks[name] = hooks[name] || []).push(fn); };
  const fire = (name, ...args) => { for (const fn of hooks[name] || []) fn(...args); };
  // what Play starts in a view with a selection of its own, while nothing plays:
  // fn() plays the selection as the view's own Play does, or returns false with none
  const selections = {};
  const onPlay = (view, fn) => { selections[view] = fn; };

  // ---- host ----
  function api(cmd, extra) {
    return fetch(base + 'api/cmd', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ cmd }, extra || {})),
    }).then((r) => r.json()).catch(() => ({ ok: false }));
  }
  let saveTimer = 0;
  function saveSettings() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => api('settings', { values: state.settings }), 300);
  }
  async function refresh() {
    const data = await (await fetch(base + 'api/state')).json();
    const known = new Map([...state.lists.local, ...state.lists.youtube].map((t) => [t.id, t]));
    const looks = (t) => (t ? [t.title, t.artist, t.album, t.art].join('\n') : '');
    const before = looks(track(state.cur));
    for (const which of Object.keys(LISTS)) {
      const got = data.lists[which];
      state.lists[which] = got.tracks.map((t) => Object.assign(t, { duration: t.duration || (known.get(t.id) || {}).duration || 0 }));
      state.names[which] = got.name || '';
    }
    state.stations = data.stations || []; state.playlists = data.playlists || [];
    state.folders = data.folders || [];
    const first = !refresh.loaded;
    if (first) { Object.assign(state.settings, data.settings); refresh.loaded = true; applySettings(); }
    state.queue = state.queue.filter((id) => track(id) || !known.has(id));  // removed songs leave the queue
    if (state.cur !== null && !track(state.cur)) stop(true);
    else if (state.station !== null && !station(state.station)) stop(true);
    else if (state.station === null && looks(track(state.cur)) !== before) showTrack();  // its tags were read meanwhile
    render();
    probeNext();
    fire('state', first);
    if (data.play) startTarget(data.play);
    else if (data.resume) resume(data.resume);   // offered once, to whichever request came first
    if (first) report();
  }

  // ---- helpers ----
  const track = (id) => state.lists.local.find((t) => t.id === id) || state.lists.youtube.find((t) => t.id === id);
  const station = (id) => state.stations.find((s) => s.id === id);
  const listOf = (t) => (isStream(t) ? 'youtube' : 'local');   // the list a song belongs in
  const shown = () => state.lists[state.show];
  const inView = (id) => shown().find((t) => t.id === id);
  // what a list is called: the streaming one after the service its songs come from
  function listName(which) {
    if (which !== 'youtube') return LISTS[which];
    const songs = state.lists.youtube;
    const apple = songs.filter((t) => t.apple).length, spotify = songs.filter((t) => t.spotify).length;
    if (!apple && !spotify) return LISTS.youtube;
    if (!spotify) return apple === songs.length ? 'Apple Music' : 'YouTube Music and Apple Music';
    if (!apple && spotify === songs.length) return 'Spotify';
    if (apple && spotify && apple + spotify === songs.length) return 'Apple Music and Spotify';
    return `YouTube Music, Apple Music and Spotify`;
  }
  const listLabel = () => state.names[state.show] || listName(state.show);
  // the list playback goes through: the current song's, or else the one in view
  const playing = () => { const t = track(state.cur); return t ? state.lists[listOf(t)] : shown(); };
  function clock(seconds, pad) {
    if (!isFinite(seconds) || seconds < 0) seconds = 0;
    const s = Math.floor(seconds % 60), m = Math.floor(seconds / 60) % 60, h = Math.floor(seconds / 3600);
    const two = (n) => String(n).padStart(2, '0');
    if (h) return `${h}:${two(m)}:${two(s)}`;
    return `${pad ? two(m) : m}:${two(s)}`;
  }
  function esc(text) { const d = document.createElement('span'); d.textContent = text; return d.innerHTML; }
  const sentence = (text) => (text ? text[0].toUpperCase() + text.slice(1) : '');
  const isStream = (t) => !!t && t.source === 'youtube';
  // an Apple Music or Spotify song is not played by this page but by the playback engine (engine.py)
  const outside = (t) => !!t && !!(t.apple || t.spotify);
  const serviceName = (t) => (t && t.spotify ? 'Spotify' : 'Apple Music');
  const length = () => {   // 0 for a live stream
    const t = state.station === null ? track(state.cur) : null;
    if (outside(t)) return (ext && ext.dur) || t.duration || 0;
    return isFinite(audio.duration) ? audio.duration : 0;
  };

  // ---- audio graph (equalizer, analyser for the visualizations, gain for volume) ----
  let actx = null, gain = null, analyserNode = null, fade = 1;
  function ensureGraph() {
    if (actx) { if (actx.state === 'suspended') actx.resume().catch(() => {}); return; }
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      actx = new Ctx();
      const source = actx.createMediaElementSource(audio);
      const analyser = actx.createAnalyser();
      analyser.fftSize = 2048; analyser.smoothingTimeConstant = 0.78;
      gain = actx.createGain();
      const shaped = Mist.eq ? Mist.eq.attach(actx, source) : source;
      shaped.connect(analyser); analyser.connect(gain); gain.connect(actx.destination);
      audio.volume = 1;
      analyserNode = analyser;
      Visuals.setAnalyser(analyser);
      if (actx.state === 'suspended') actx.resume().catch(() => {});
    } catch (err) {
      actx = { state: 'unavailable', resume: () => Promise.resolve() }; gain = null;
    }
    applyVolume();
  }
  function applyVolume() {
    const level = (state.settings.muted ? 0 : Math.pow(state.settings.volume, 1.6)) * fade;
    if (gain) gain.gain.value = level; else audio.volume = level;
    if (ext) sendVolume();   // the engine's follows
    setSlider($('volume'), state.settings.volume);
    $('mute-waves').toggleAttribute('hidden', state.settings.muted);
    $('mute-x').toggleAttribute('hidden', !state.settings.muted);
    $('mute').classList.toggle('on', state.settings.muted);
  }
  function setVolume(value) {
    state.settings.volume = Math.max(0, Math.min(1, value)); state.settings.muted = false;
    applyVolume(); saveSettings();
  }

  // ---- transport ----
  let cueAt = 0;   // where a song restored from the last session should continue
  function play(id) {
    const t = track(id), was = track(state.cur);
    if (!t) return;
    if (!was || listOf(was) !== listOf(t)) state.show = listOf(t);   // the pane follows the music to its list
    if (state.cur !== null && state.cur !== id) state.history.push(state.cur);
    state.queue = state.queue.filter((q) => q !== id);
    state.station = null; state.radioTitle = ''; cueAt = 0;
    state.cur = id; state.sel = id; state.heard.add(id);
    ensureGraph();
    closeStream();
    leaveOutside(t);
    if (!isStream(t)) audio.src = base + 'media/' + id;   // a YouTube Music song is fed by start()
    else if (outside(t)) silence();                      // ...and an Apple Music or Spotify song is played by the engine
    start();
    setMode('playing');
    showTrack();
    render();
  }
  function playStation(id) {
    const s = station(id);
    if (!s) return;
    state.cur = null; state.station = id; state.radioTitle = ''; cueAt = 0; radioError = '';
    ensureGraph();
    closeStream();
    leaveOutside();
    audio.src = base + 'radio/' + id + '?t=' + Date.now();
    start();
    setMode('playing');
    showTrack();
    render();
    flash(`Connecting to ${s.name}…`, 15000);
  }
  function startTarget(target) { if (target.station != null) playStation(target.station); else play(target.id); }
  // the song (or station) that was on when the player was closed, ready to go on
  function resume(saved) {
    if (saved.station != null) {
      if (!station(saved.station)) return;
      state.cur = null; state.station = saved.station;
    } else {
      const t = track(saved.id);
      if (!t) return;
      state.cur = state.sel = t.id; state.heard.add(t.id);
      cueAt = saved.position > 2 && (!t.duration || saved.position < t.duration - 3) ? saved.position : 0;
      if (!isStream(t)) audio.src = base + 'media/' + t.id;   // nothing is fetched before Play
    }
    setMode(cueAt ? 'paused' : 'stopped');
    showTrack(); render(); updateTime();
  }
  function start() {
    const at = cueAt, t = track(state.cur);
    if (state.station === null && outside(t)) {   // the engine plays it: from where it was paused, or from where it should start
      if (ext && ext.t.id === t.id) extResume(); else extStart(t, at);
      return;
    }
    if (state.station === null && isStream(t)) {   // fed from where it should start
      if (!feed || feed.t.id !== t.id) openStream(t);
    } else if (at) {  // WebKit ignores a seek on a song that has not run yet: start it silently, jump, then let it be heard
      fade = 0; applyVolume();
      audio.addEventListener('playing', () => { if (cueAt === at) audio.currentTime = at; else { fade = 1; applyVolume(); } }, { once: true });
      audio.addEventListener('seeked', () => { if (cueAt === at) cueAt = 0; fade = 1; applyVolume(); }, { once: true });
    }
    audio.play().catch((err) => { if (err && err.name === 'NotAllowedError') setMode('paused'); });
  }
  const playSelection = () => state.mode === 'stopped' && !!selections[state.view] && selections[state.view]();
  function toggle() {
    if (playSelection()) return;   // what is selected in the view on screen
    if (state.station !== null) { if (state.mode === 'playing') stop(); else playStation(state.station); return; }
    if (state.mode === 'playing') { halt(); setMode('paused'); return; }
    if (state.cur === null || state.mode === 'stopped') {
      const first = inView(state.sel) || track(state.cur) || shown()[0];
      if (!first) { api('open-files'); return; }
      if (state.mode === 'stopped' && state.cur === first.id && audio.src) { ensureGraph(); start(); setMode('playing'); return; }
      play(first.id); return;
    }
    ensureGraph(); start(); setMode('playing');
  }
  function stop(forget) {
    audio.pause();
    cueAt = 0;
    leaveOutside();
    if (state.station !== null || feed) { closeStream(); audio.removeAttribute('src'); audio.load(); }   // hang up on the stream
    else { try { audio.currentTime = 0; } catch (e) { /* not loaded */ } }
    if (forget) { state.cur = null; state.station = null; state.radioTitle = ''; audio.removeAttribute('src'); audio.load(); showTrack(); }
    setMode('stopped');
    updateTime();
  }
  const position = () => (ext ? extPosition() : cueAt || audio.currentTime || 0);
  function seekTo(seconds) {
    if (!length()) return;
    const target = Math.max(0, Math.min(length() - 0.2, seconds));
    if (cueAt) { cueAt = Math.max(0.1, target); updateTime(); report(); } else if (ext) extSeek(target); else audio.currentTime = target;
  }
  function step(dir, auto) {
    if (state.station !== null) {  // on the radio, next and previous change the station
      const i = state.stations.findIndex((s) => s.id === state.station), n = state.stations.length;
      if (n > 1) playStation(state.stations[(i + dir + n) % n].id);
      return;
    }
    while (dir > 0 && state.queue.length) {   // "Play next" songs first, from either list
      const id = state.queue.shift();
      if (track(id)) { play(id); return; }
    }
    const tracks = playing();
    if (!tracks.length) return;
    let i = tracks.findIndex((t) => t.id === state.cur);
    if (dir < 0) {
      if (!auto && (ext ? extPosition() : audio.currentTime) > 3) { if (ext) extSeek(0); else audio.currentTime = 0; return; }
      if (state.settings.shuffle && state.history.length) { const back = state.history.pop(); const keep = state.history.slice(); play(back); state.history = keep; return; }
      i = i <= 0 ? tracks.length - 1 : i - 1;
    } else if (state.settings.shuffle && tracks.length > 1) {
      // every song once per pass; the list only starts over if repeat is on
      let left = tracks.filter((t) => !state.heard.has(t.id));
      if (!left.length) {
        if (auto && repeatMode() === 'off') { if (carryOn()) return; state.heard.clear(); stop(); render(); return; }
        state.heard.clear();
        left = tracks.filter((t) => t.id !== state.cur);
      }
      i = tracks.indexOf(left[Math.floor(Math.random() * left.length)]);
    } else {
      i += 1;
      if (i >= tracks.length) {
        if (auto && repeatMode() === 'off') { if (carryOn()) return; stop(); state.sel = tracks[0].id; render(); return; }
        i = 0;
      }
    }
    play(tracks[i].id);
  }
  // the list ran out by itself: premium.js may carry on with similar songs
  const carryOn = () => !!(Mist.carryOn && Mist.carryOn(track(state.cur)));
  // the song that plays after this one, when that is known (not in shuffle)
  function upcoming() {
    const queued = state.queue.find((id) => track(id));
    if (queued !== undefined) return queued;
    const tracks = playing(), i = tracks.findIndex((t) => t.id === state.cur);
    if (state.settings.shuffle || state.station !== null || i < 0) return null;
    if (i + 1 < tracks.length) return tracks[i + 1].id;
    return repeatMode() === 'all' ? tracks[0].id : null;
  }
  function setMode(mode) {
    state.mode = mode;
    $('ico-play').toggleAttribute('hidden', mode === 'playing');
    $('ico-pause').toggleAttribute('hidden', mode !== 'playing');
    $('play').title = mode === 'playing' ? 'Pause' : 'Play';
    const icon = { playing: 'M2 1l7 4-7 4z', paused: 'M2 1h2.4v8H2zM5.6 1H8v8H5.600z', stopped: 'M2 2h6v6H2z' }[mode];
    $('st-icon').firstElementChild.setAttribute('d', icon);
    $('seek').classList.toggle('off', mode === 'stopped' || state.cur === null);
    if (mode === 'playing') Visuals.start(); else if (mode === 'paused') Visuals.pause(); else Visuals.stop();
    flashUntil = 0;
    statusTick(true);
    report();
    fire('mode', mode);
  }

  // ---- YouTube Music songs ----
  // The host gets a song in segments of about seven seconds and repacks each
  // one as MP4; they go into a MediaSource here, about a minute ahead of the
  // music, and what was played a while ago is let go.
  const AHEAD = 60, BEHIND = 90;
  let feed = null;      // the song being fed: { t, source, url, buffer, info, busy, started, stalled, end, ctrl }
  let streamFails = 0;  // songs in a row that could not be played
  function openStream(t) {
    closeStream();
    const source = new MediaSource();
    const f = feed = { t, source, url: URL.createObjectURL(source), buffer: null, info: null, busy: false, started: false, stalled: false, end: 0, ctrl: new AbortController() };
    source.addEventListener('sourceopen', () => feedStart(f), { once: true });
    audio.src = f.url;
  }
  function closeStream() {
    if (!feed) return;
    feed.ctrl.abort();
    URL.revokeObjectURL(feed.url);
    feed = null;
  }
  async function need(f, part) {   // a part of the song from the host; its error message otherwise
    const r = await fetch(`${base}yt/song/${f.t.video}${part}`, { signal: f.ctrl.signal });
    if (r.ok) return r;
    const why = await r.json().catch(() => ({}));
    throw Object.assign(new Error(why.error || 'the player could not get this song'), { general: !!why.general });
  }
  function append(f, data) {
    return new Promise((ok, fail) => {
      const done = () => { f.buffer.removeEventListener('error', bad); ok(); };
      const bad = () => { f.buffer.removeEventListener('updateend', done); fail(new Error('the player could not read this song')); };
      f.buffer.addEventListener('updateend', done, { once: true });
      f.buffer.addEventListener('error', bad, { once: true });
      f.buffer.appendBuffer(data);
    });
  }
  async function feedStart(f) {
    try {
      const info = await (await need(f, '')).json();
      if (f !== feed) return;
      f.info = info;
      f.buffer = f.source.addSourceBuffer(info.type);
      f.source.duration = info.duration;
      await append(f, await (await need(f, '/init')).arrayBuffer());
      if (f !== feed) return;
      if (cueAt) { audio.currentTime = cueAt; cueAt = 0; }   // resuming where the last session stopped
      pump(f);
    } catch (err) { streamFailed(f, err); }
  }
  // where the music that is in ends, from this moment on (-1: none here)
  function edge(f) {
    const ranges = f.buffer.buffered, now = audio.currentTime;
    for (let i = 0; i < ranges.length; i++) if (ranges.start(i) <= now + 0.1 && now < ranges.end(i)) return ranges.end(i);
    return -1;
  }
  // the music that is in reaches the song's end (known once its last segment is in)
  const allIn = (f, until) => f.end > 0 && until >= f.end - 0.05;
  // the segment the music needs next, of those not tried yet this round; -1 while it is far enough ahead
  function wanted(f, tried) {
    const starts = f.info.starts, now = audio.currentTime, until = edge(f), ranges = f.buffer.buffered;
    const at = (time) => { let n = 0; while (n + 1 < starts.length && starts[n + 1] <= time) n++; return n; };
    let here = false;
    for (let i = 0; i < ranges.length; i++) here = here || (ranges.start(i) <= now && now < ranges.end(i));
    if (audio.ended || (here && (until - now > AHEAD || allIn(f, until)))) return -1;
    // nothing here yet: the segment this moment is in, then its neighbor in case the
    // playlist's times and the music's are a frame apart; otherwise the one after what is in
    const n = at(here ? until + 0.1 : now);
    const near = here ? [n] : [n, now - starts[n] < 0.1 ? n - 1 : n + 1];
    return near.find((i) => i >= 0 && i < starts.length && !tried.has(i)) ?? -1;
  }
  async function pump(f) {
    if (f.busy || f !== feed || !f.info) return;
    f.busy = true;
    try {
      const tried = new Set();
      for (let n = wanted(f, tried); n >= 0 && f === feed; n = wanted(f, tried)) {
        tried.add(n);
        const data = await (await need(f, '/' + n)).arrayBuffer();
        if (f !== feed) return;
        const ranges = f.buffer.buffered, old = audio.currentTime - BEHIND;
        if (ranges.length && ranges.start(0) < old - 30) {   // let go of what played a while ago
          await new Promise((ok) => { f.buffer.addEventListener('updateend', ok, { once: true }); f.buffer.remove(0, old); });
        }
        await append(f, data);
        if (n === f.info.starts.length - 1) { const r = f.buffer.buffered; f.end = r.end(r.length - 1); }   // where it really ends
      }
      if (f === feed && f.source.readyState === 'open' && allIn(f, edge(f))) f.source.endOfStream();   // so the song can end
    } catch (err) { streamFailed(f, err); } finally { f.busy = false; }
  }
  function streamFailed(f, err) {
    if (f !== feed || (err && err.name === 'AbortError')) return;
    closeStream(); audio.removeAttribute('src'); audio.load();
    const why = sentence((err && err.message) || 'the player could not get this song');
    if ((err && err.general) || ++streamFails >= 3) { streamFails = 0; stop(); flash(why, 30000); return; }   // the next one would fail too
    flash(why, 5000);
    setTimeout(() => { if (state.cur === f.t.id && state.mode === 'playing') step(1, true); }, 2500);
  }
  audio.addEventListener('timeupdate', () => { if (feed) pump(feed); });
  audio.addEventListener('seeking', () => { if (feed) pump(feed); });
  audio.addEventListener('waiting', () => {
    const f = feed;
    if (f) setTimeout(() => { if (f === feed && f.started && audio.readyState < 3 && !audio.paused) { f.stalled = true; statusTick(); } }, 700);
  });

  // ---- Apple Music and Spotify songs ----
  // Their audio is protected: it cannot be played in this page, and nothing here can touch it. The
  // playback engine (engine.py) plays it, in a helper that is never shown. This page tells the engine what
  // to do (engine-play, -pause, -resume, -seek, -volume, -stop) and shows what it says back (the "engine"
  // events): where the song is, how long it is, and when it ends. `ext` is the song it was asked to play:
  // { t, serial, phase, pos, stamp, dur, started, stalled, want, asked, sought }.
  let extSerial = 0, extTimer = 0, extVolumeTimer = 0;
  const extLevel = () => ({ value: Math.pow(state.settings.volume, 1.6) * fade, muted: !!state.settings.muted });
  // where the song is: the engine's last word, and the time since while it plays
  function extPosition() {
    if (!ext) return 0;
    const moving = state.mode === 'playing' && ext.phase === 'playing';
    const at = ext.pos + (moving ? (performance.now() - ext.stamp) / 1000 : 0);
    return ext.dur ? Math.min(at, ext.dur) : at;
  }
  function extStart(t, at) {
    const now = performance.now();
    const e = ext = { t, serial: ++extSerial, phase: 'starting', pos: at || 0, stamp: now, dur: t.duration || 0, started: false, stalled: false, want: 'playing', asked: now, sought: null };
    cueAt = 0;
    clearInterval(extTimer); extTimer = setInterval(updateTime, 250);
    const level = extLevel();
    api('engine-play', { id: t.id, serial: e.serial, from: e.pos, volume: level.value, muted: level.muted }).then((r) => {
      if (!r.ok && ext === e) extFailed(e, r.code, r.error);
    });
    statusTick(true);
  }
  function extPause() {
    const e = ext;
    if (!e) return;
    e.pos = extPosition(); e.stamp = e.asked = performance.now(); e.phase = 'paused'; e.want = 'paused'; e.sought = null;
    api('engine-pause');
  }
  function extResume() {
    const e = ext;
    e.stamp = e.asked = performance.now(); e.phase = e.started ? 'playing' : 'starting'; e.want = 'playing';
    // the engine goes on from the place, or starts again at it if it had ended meanwhile
    api('engine-resume').then((r) => { if (!r.ok && !r.code && ext === e) extStart(e.t, e.pos); });
    statusTick(true);
  }
  function extSeek(target) {
    const e = ext;
    e.pos = e.sought = target; e.stamp = e.asked = performance.now();
    api('engine-seek', { to: target });
    updateTime(); report();
  }
  // the engine's volume follows the player's, a few times a second at most
  function sendVolume() {
    if (extVolumeTimer) return;
    extVolumeTimer = setTimeout(() => {
      extVolumeTimer = 0;
      if (ext) { const level = extLevel(); api('engine-volume', { value: level.value, muted: level.muted }); }
    }, 90);
  }
  // pause whatever plays: the player's own audio, or the engine's song
  function halt() { if (ext) extPause(); else audio.pause(); }
  // the player's own audio lets go, for a song that the engine plays
  function silence() {
    audio.pause();
    if (audio.getAttribute('src')) { audio.removeAttribute('src'); audio.load(); }
  }
  // the Apple Music song is no longer the one that is on: the engine hears it, unless it is to play another
  function leaveOutside(next) {
    if (!ext) return;
    ext = null;
    clearInterval(extTimer); extTimer = 0;
    if (!outside(next)) api('engine-stop');
  }
  // what the engine says about the song it plays
  function extEvent(m) {
    const e = ext;
    if (!e || m.serial !== e.serial) return;   // about a song that is no longer the one
    const now = performance.now(), settled = now - e.asked > 1500;   // an answer to an earlier request may still be on its way
    if (m.phase === 'error') { extFailed(e, m.code, m.message); return; }
    if (m.phase === 'ended') { songEnded(); return; }
    if (m.duration > 0 && Math.abs(m.duration - e.dur) > 0.5) {
      e.dur = m.duration;
      if (Math.abs((e.t.duration || 0) - m.duration) > 0.5) { e.t.duration = m.duration; api('duration', { id: e.t.id, seconds: m.duration }); render(); }
    }
    const late = !settled && e.sought !== null && Math.abs(m.position - e.sought) > 3;   // from before the jump
    if (m.phase === 'playing') {
      if (!settled && e.want === 'paused') return;
      if (state.mode === 'paused') { if (settled) api('engine-pause'); return; }   // it did not hear the pause
      const first = !e.started, was = e.stalled;
      e.phase = 'playing'; e.started = true; e.stalled = false;
      if (!late) { e.pos = m.position; e.stamp = now; if (settled) e.sought = null; }
      if (first) { streamFails = 0; flashUntil = 0; report(); }   // the host can announce it now
      if (first || was) statusTick(true);
    } else if (m.phase === 'paused') {
      if (!settled && e.want === 'playing') return;
      e.phase = 'paused';
      if (!late) { e.pos = m.position; e.stamp = now; }
      if (state.mode === 'playing') { setMode('paused'); flash(`${serviceName(e.t)} paused the song`, 6000); }
    } else if (m.phase === 'loading' || m.phase === 'starting') {
      e.stalled = e.started; e.phase = m.phase;
      statusTick(true);
    }
    updateTime();
  }
  function extFailed(e, code, text) {
    if (ext !== e) return;
    ext = null;
    clearInterval(extTimer); extTimer = 0;
    const why = sentence(text || `${serviceName(e.t)} could not play this song`);
    // the engine itself needs something: the Library page says what
    if (code === 'not-installed' || code === 'sandbox') { stop(); fire('engine-needed', code); flash(why, 12000); return; }
    // what is wrong for every song: the next one would fail too
    const general = ['signed-out', 'no-subscription', 'limit', 'offline', 'widevine', 'crash', 'page', 'start'].includes(code);
    if (general || ++streamFails >= 3) { streamFails = 0; stop(); flash(why, 30000); return; }
    flash(why, 5000);
    setTimeout(() => { if (state.cur === e.t.id && state.mode === 'playing') step(1, true); }, 2500);
  }

  // what the host passes on from the keyboard's media keys and the desktop's controls
  function control(action, value) {
    const loaded = state.cur !== null || state.station !== null || state.lists.local.length > 0 || state.lists.youtube.length > 0;
    if (action === 'playpause') { if (loaded) toggle(); else playSelection(); }   // never the file dialog
    else if (action === 'play') { if (state.mode !== 'playing') { if (loaded) toggle(); else playSelection(); } }
    else if (action === 'pause') { if (state.mode === 'playing') toggle(); }
    else if (action === 'stop') stop();
    else if (action === 'next') step(1);
    else if (action === 'previous') step(-1);
    else if (action === 'seek') seekTo(position() + Number(value));
    else if (action === 'position') seekTo(Number(value));
    else if (action === 'volume') setVolume(Number(value));
    else if (action === 'shuffle') { if (!!value !== !!state.settings.shuffle) toggleShuffle(); }
    else if (action === 'repeat') setRepeat(value);
  }
  // tell the host what is on, so it can resume next time and feed the desktop's controls
  let reportTimer = 0;
  function report() {
    clearTimeout(reportTimer);
    reportTimer = setTimeout(() => {
      if (!refresh.loaded) return;
      api('now', {
        id: state.cur, station: state.station, mode: state.mode, title: state.radioTitle,
        position: state.mode === 'stopped' ? 0 : position(), duration: length(), next: upcoming(),
        // a YouTube Music, Apple Music or Spotify song has started once it is heard, so one that cannot play is not announced
        started: outside(track(state.cur)) ? !!(ext && ext.t.id === state.cur && ext.started)
          : !isStream(track(state.cur)) || !!(feed && feed.t.id === state.cur && feed.started),
      });
    }, 120);
  }
  setInterval(() => { if (state.mode === 'playing') report(); }, 5000);

  // ---- now playing pane ----
  function showTrack() {
    const t = track(state.cur), s = station(state.station);
    $('now-artist').textContent = s ? s.name : (t ? t.artist : '');
    $('now-title').textContent = s ? (state.radioTitle || 'Internet radio') : (t ? t.title : '');
    $('album-name').textContent = s ? s.name : (t ? (t.album || '') : '');
    const art = $('art');
    if (t && t.art) { art.src = base + 'art/' + t.id; art.hidden = false; $('art-none').hidden = true; }
    else { art.removeAttribute('src'); art.hidden = true; $('art-none').hidden = false; }
    showVis();
    const name = s ? (state.radioTitle || s.name) : (t ? t.title : '');
    document.title = name ? `${name} - Ocean Mist` : 'Ocean Mist';
    fire('track');
  }
  function showVis() {
    const preset = Visuals.presets[Visuals.index], t = track(state.cur);
    // the sound of an Apple Music or Spotify song never reaches the player, so a visualization cannot follow it
    const loose = !!preset.draw && state.station === null && outside(t);
    $('vis-name').textContent = preset.name + (loose ? ' · not in time with the music' : '');
    $('vis-name').title = loose ? `${serviceName(t)}’s audio is protected: the playback engine plays it, so the visualizations and the equalizer cannot work on it` : '';
    const useArt = preset.art && t && t.art;
    $('vis-art').hidden = !useArt;
    if (useArt) $('vis-art').src = base + 'art/' + t.id;
    $('vis').style.visibility = preset.draw ? 'visible' : 'hidden';
    emptyHint();
  }
  // "No music loaded", while nothing is on and the list in view is empty
  function emptyHint() {
    $('empty-hint').hidden = state.cur !== null || state.station !== null || shown().length > 0;
    $('hint-list').textContent = listLabel();
    $('hint-local').hidden = state.show !== 'local';   // files go to My Music only
    $('hint-youtube').hidden = state.show !== 'youtube';
  }
  function setVis(i) {
    state.settings.vis = Visuals.select(i);
    showVis(); saveSettings();
  }

  let statusStep = 0, statusTimer = 0, flashUntil = 0;
  function statusTick(reset) {
    if (reset) { statusStep = 0; clearInterval(statusTimer); statusTimer = setInterval(statusTick, 4000); }
    if (Date.now() < flashUntil) return;
    const t = track(state.cur), s = station(state.station);
    let text = 'Ready';
    if (state.mode === 'stopped') text = t || s ? 'Stopped' : 'Ready';
    else if (state.mode === 'paused') text = 'Paused';
    else if (feed && !feed.started) text = 'Connecting to YouTube Music…';
    else if (ext && !ext.started) text = `Connecting to ${serviceName(ext.t)}…`;
    else if ((feed && feed.stalled) || (ext && ext.stalled)) text = 'Buffering…';
    else if (s) {
      const lines = ['Station: ' + s.name];
      if (state.radioTitle) lines.push('Song: ' + state.radioTitle);
      text = lines[statusStep++ % lines.length];
    } else if (t) {
      const lines = [];
      if (t.artist) lines.push('Artist: ' + t.artist);
      if (t.album) lines.push('Album: ' + t.album);
      lines.push('Song: ' + t.title);
      text = lines[statusStep++ % lines.length];
    }
    $('st-text').textContent = text;
  }
  // a message in the status line that stays for a moment
  function flash(text, ms) {
    flashUntil = Date.now() + (ms || 4000);
    $('st-text').textContent = text;
  }
  function updateTime() {
    const now = state.mode === 'stopped' ? 0 : position();
    $('st-time').textContent = clock(now, true);
    const t = track(state.cur), whole = length() || (t && t.duration) || 0;
    if (!seeking) setSlider($('seek'), whole ? now / whole : 0);
    if (sleep.at && Date.now() >= sleep.at) fallAsleep();
  }

  // ---- the playlist pane ----
  const list = $('playlist');
  function render() {
    const tracks = shown();
    list.innerHTML = tracks.map((t) => {
      const queued = state.queue.indexOf(t.id);
      return `<div class="row${t.id === state.cur ? ' cur' : ''}${t.id === state.sel ? ' sel' : ''}" data-id="${t.id}">` +
        `<span class="t">${esc(t.title)}</span>${queued >= 0 ? `<span class="q" title="Plays next">${queued + 1}</span>` : ''}` +
        `<span class="d">${t.duration ? clock(t.duration) : ''}</span></div>`;
    }).join('');
    const total = tracks.reduce((sum, t) => sum + (t.duration || 0), 0);
    $('total-time').textContent = 'Total Time: ' + clock(total);
    $('np-label').textContent = listLabel();
    $('np-select').title = `Switch between My Music and ${listName('youtube')}, open music or a saved playlist`;
    emptyHint();
    const current = list.querySelector('.cur');
    if (current && render.lastCur !== state.cur) { current.scrollIntoView({ block: 'nearest' }); render.lastCur = state.cur; }
    fire('render');
  }
  function rowId(event) { const row = event.target.closest('[data-id]'); return row ? Number(row.dataset.id) : null; }
  function queueNext(id) { state.queue = [id, ...state.queue.filter((q) => q !== id)]; render(); }
  function queueLast(id) { state.queue = [...state.queue.filter((q) => q !== id), id]; render(); }
  function unqueue(id) { state.queue = state.queue.filter((q) => q !== id); render(); }
  function trackMenu(e, id, extra) {
    const queued = state.queue.includes(id);
    const items = [
      { label: 'Play', run: () => play(id) },
      { label: 'Play next', run: () => queueNext(id) },
      queued ? { label: 'Take out of the queue', run: () => unqueue(id) } : { label: 'Add to queue', run: () => queueLast(id) },
      null,
      { label: 'Remove from list', run: () => api('remove', { ids: [id] }) },
    ];
    fire('track-menu', id, items);   // the other parts may add theirs
    menu(e.clientX, e.clientY, [...items, ...(extra || [])]);
  }
  list.addEventListener('click', (e) => { const id = rowId(e); if (id !== null) { state.sel = id; render(); } });
  list.addEventListener('dblclick', (e) => { const id = rowId(e); if (id !== null) play(id); });
  list.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const id = rowId(e); if (id === null) return;
    state.sel = id; render();
    trackMenu(e, id, [null, { label: 'Clear playlist', run: () => api('clear', { list: state.show }) }]);
  });

  // drag a row to another place in the list
  let drag = null;
  list.addEventListener('pointerdown', (e) => {
    const id = rowId(e);
    if (e.button === 0 && id !== null) drag = { id, y: e.clientY, pointer: e.pointerId, active: false, before: null };
  });
  list.addEventListener('pointermove', (e) => {
    if (!drag) return;
    if (!drag.active) {
      if (Math.abs(e.clientY - drag.y) < 5) return;
      drag.active = true;
      try { list.setPointerCapture(drag.pointer); } catch (err) { /* the pointer is already gone */ }
      list.classList.add('dragging');
    }
    const box = list.getBoundingClientRect();
    if (e.clientY < box.top + 14) list.scrollTop -= 7; else if (e.clientY > box.bottom - 14) list.scrollTop += 7;
    const rows = [...list.querySelectorAll('.row')];
    const under = rows.find((row) => { const r = row.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
    drag.before = under ? Number(under.dataset.id) : null;
    const edge = under ? under.getBoundingClientRect().top : rows[rows.length - 1].getBoundingClientRect().bottom;
    const line = $('drop-line');
    line.style.top = Math.max(box.top, Math.min(box.bottom - 2, edge - 1)) - $('pane').getBoundingClientRect().top + 'px';
    line.hidden = false;
  });
  function drop() {
    const d = drag; drag = null;
    if (!d || !d.active) return;
    list.classList.remove('dragging');
    $('drop-line').hidden = true;
    if (d.before === d.id) return;
    const rest = shown().filter((t) => t.id !== d.id);
    const at = d.before === null ? rest.length : rest.findIndex((t) => t.id === d.before);
    state.lists[state.show] = [...rest.slice(0, at), track(d.id), ...rest.slice(at)];
    state.sel = d.id;
    render();
    api('move', { ids: [d.id], before: d.before });
  }
  list.addEventListener('pointerup', drop);
  list.addEventListener('pointercancel', drop);

  // lengths for tracks whose tags did not say
  let probing = false;
  function probeNext() {
    if (probing) return;
    const t = state.lists.local.find((x) => !x.duration && !x.probed);
    if (!t) return;
    probing = true; t.probed = true;
    const done = () => { probing = false; probe.onloadedmetadata = probe.onerror = null; probeNext(); };
    probe.onloadedmetadata = () => {
      if (isFinite(probe.duration)) { t.duration = probe.duration; api('duration', { id: t.id, seconds: t.duration }); render(); }
      done();
    };
    probe.onerror = done;
    probe.src = base + 'media/' + t.id;
  }

  // ---- sliders ----
  let seeking = false;
  function setSlider(el, fraction) {
    fraction = Math.max(0, Math.min(1, fraction || 0));
    const pad = el.id === 'volume' ? 10 : 12, span = el.clientWidth - pad * 2;
    const x = pad + span * fraction;
    el.querySelector('.thumb').style.left = x + 'px';
    el.querySelector('.fill').style.width = Math.max(0, x - 1) + 'px';
  }
  function slider(el, onChange) {
    const at = (e) => {
      const rect = el.getBoundingClientRect(), pad = el.id === 'volume' ? 10 : 12;
      return Math.max(0, Math.min(1, (e.clientX - rect.left - pad) / (rect.width - pad * 2)));
    };
    el.addEventListener('pointerdown', (e) => {
      if (el.classList.contains('off')) return;
      el.setPointerCapture(e.pointerId);
      let value = at(e); setSlider(el, value); onChange(value, false);
      const move = (ev) => { value = at(ev); setSlider(el, value); onChange(value, false); };
      const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up); onChange(value, true); };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    });
  }
  slider($('seek'), (value, done) => {
    seeking = !done;
    if (length()) {
      $('st-time').textContent = clock(value * length(), true);
      if (done) seekTo(value * length());
    }
  });
  slider($('volume'), (value) => setVolume(value));
  function jog(el, seconds) {
    let timer = 0;
    const nudge = () => seekTo(position() + seconds);
    el.addEventListener('pointerdown', () => { nudge(); timer = setInterval(nudge, 180); });
    for (const type of ['pointerup', 'pointerleave', 'pointercancel']) el.addEventListener(type, () => clearInterval(timer));
  }
  jog($('rew'), -5); jog($('ffw'), 5);

  // ---- menus ----
  // items: { label, run, checked, disabled, hint } - or { label, items } for a
  // menu that opens to the side - or null for a separator
  const openMenus = [];
  function closeMenus(from) { for (const el of openMenus.splice(from || 0)) el.remove(); }
  function menu(x, y, items, opts) { closeMenus(); return panel(x, y, items, opts || {}, 0); }
  function panel(x, y, items, opts, level) {
    const el = document.createElement('div');
    el.className = 'menu';
    for (const item of items) {
      const row = document.createElement('div');
      el.appendChild(row);
      if (!item) { row.className = 'sep'; continue; }
      row.textContent = item.label;
      if (item.checked) row.classList.add('check');
      if (item.disabled) row.classList.add('off');
      if (item.hint) { const key = document.createElement('kbd'); key.textContent = item.hint; row.appendChild(key); }
      if (item.items) {
        row.classList.add('sub');
        const open = () => {
          if (row.classList.contains('open')) return;
          closeMenus(level + 1);
          for (const other of el.children) other.classList.remove('open');
          row.classList.add('open');
          const r = row.getBoundingClientRect();
          panel(r.right - 3, r.top - 3, item.items, { flip: r.left + 3 }, level + 1);
        };
        row.addEventListener('mouseenter', open);
        row.addEventListener('click', open);
      } else {
        row.addEventListener('mouseenter', () => { closeMenus(level + 1); for (const other of el.children) other.classList.remove('open'); });
        if (!item.disabled) row.addEventListener('click', () => { closeMenus(); item.run(); });
      }
    }
    document.body.appendChild(el);
    openMenus.push(el);
    const w = el.offsetWidth, h = el.offsetHeight;
    if (opts.above) y -= h;
    if (opts.right) x -= w;
    if (opts.flip !== undefined && x + w > innerWidth - 2) x = opts.flip - w;
    el.style.left = Math.max(2, Math.min(x, innerWidth - w - 2)) + 'px';
    el.style.top = Math.max(2, Math.min(y, innerHeight - h - 2)) + 'px';
    return el;
  }
  document.addEventListener('pointerdown', (e) => { if (!e.target.closest('.menu')) closeMenus(); }, true);
  window.addEventListener('blur', () => closeMenus());

  // repeat works like the streaming apps: off -> whole list -> this song -> off
  function repeatMode() {
    const mode = state.settings.repeat;
    return mode === 'one' || mode === 'all' ? mode : (mode === true ? 'all' : 'off');  // true: the old on/off setting
  }
  function setRepeat(mode) { state.settings.repeat = mode; showToggles(); saveSettings(); }
  function cycleRepeat() { setRepeat({ off: 'all', all: 'one', one: 'off' }[repeatMode()]); }
  function toggleShuffle() { state.settings.shuffle = !state.settings.shuffle; state.heard.clear(); showToggles(); saveSettings(); }
  function showToggles() {
    const mode = repeatMode();
    $('shuffle').classList.toggle('on', !!state.settings.shuffle);
    $('shuffle').title = `Turn shuffle ${state.settings.shuffle ? 'off' : 'on'}`;
    $('repeat').classList.toggle('on', mode !== 'off');
    $('repeat-one').toggleAttribute('hidden', mode !== 'one');
    $('repeat').title = { off: 'Repeat is off (click to repeat the list)', all: 'Repeating the list (click to repeat this song)', one: 'Repeating this song (click to turn repeat off)' }[mode];
  }

  // sleep timer: fades the music out and pauses
  const sleep = { at: 0, song: false };
  function setSleep(choice) {
    sleep.at = typeof choice === 'number' && choice > 0 ? Date.now() + choice * 60000 : 0;
    sleep.song = choice === 'song';
    flash(sleep.at ? `Sleep timer: music stops in ${choice} minutes` : sleep.song ? 'Sleep timer: music stops after this song' : 'Sleep timer is off');
  }
  function fallAsleep() {
    sleep.at = 0; sleep.song = false;
    if (state.mode !== 'playing') return;
    const began = Date.now();
    const timer = setInterval(() => {
      fade = Math.max(0, 1 - (Date.now() - began) / 8000);
      if (fade > 0 && state.mode === 'playing') { applyVolume(); return; }
      clearInterval(timer);
      if (state.mode === 'playing') { if (state.station !== null) stop(); else { halt(); setMode('paused'); } }
      fade = 1; applyVolume();
      flash('Sleep timer: the music was stopped', 3600000);
    }, 100);
  }
  function sleepItems() {
    const items = [{ label: 'Off', checked: !sleep.at && !sleep.song, run: () => setSleep(0) }];
    if (sleep.at) items.push({ label: `${Math.max(1, Math.round((sleep.at - Date.now()) / 60000))} min left`, checked: true, disabled: true });
    items.push(null);
    for (const minutes of [15, 30, 45, 60, 90]) items.push({ label: `${minutes} minutes`, run: () => setSleep(minutes) });
    items.push({ label: 'End of this song', checked: sleep.song, run: () => setSleep('song') });
    return items;
  }

  function setZoom(zoom) {
    state.settings.zoom = zoom; Visuals.scale = zoom;
    api('zoom', { value: zoom });
  }
  const fileItems = () => [
    { label: 'Open files…', hint: 'Ctrl+O', run: () => api('open-files') },
    { label: 'Open folder…', run: () => api('open-folder') },
    { label: 'Open playlist…', run: () => api('open-playlist') },
    { label: 'Save playlist as…', disabled: !shown().length, run: () => api('save-playlist', { list: state.show }) },
  ];
  function mainMenu(anchor, above) {
    const rect = anchor.getBoundingClientRect(), mode = repeatMode(), zoom = state.settings.zoom || 1;
    menu(rect.left, above ? rect.top - 2 : rect.bottom + 1, [
      ...fileItems(),
      null,
      { label: 'Shuffle', hint: 'Ctrl+H', checked: state.settings.shuffle, run: toggleShuffle },
      { label: 'Repeat list', hint: 'Ctrl+T', checked: mode === 'all', run: () => setRepeat(mode === 'all' ? 'off' : 'all') },
      { label: 'Repeat song', checked: mode === 'one', run: () => setRepeat(mode === 'one' ? 'off' : 'one') },
      { label: 'Autoplay similar songs', checked: state.settings.yt_radio !== false, run: () => { state.settings.yt_radio = state.settings.yt_radio === false; saveSettings(); } },
      null,
      { label: 'Graphic equalizer', checked: !!state.settings.eq_open, run: () => Mist.eq.toggle() },
      { label: 'Player size', items: [1, 1.25, 1.5, 1.75, 2].map((z) => ({ label: `${z * 100}%`, checked: zoom === z, run: () => setZoom(z) })) },
      { label: 'Sleep timer', checked: !!(sleep.at || sleep.song), items: sleepItems() },
      { label: 'Notify on song change', checked: !!state.settings.notify, run: () => { state.settings.notify = !state.settings.notify; saveSettings(); } },
      { label: 'Compact mode', hint: 'Ctrl+2', run: () => Mist.skins.apply() },
      null,
      { label: 'Clear playlist', disabled: !shown().length, run: () => api('clear', { list: state.show }) },
    ], { above });
  }
  // the drop-down at the top right: the two lists, and the saved playlists of the one in view
  function playPlaylist(path) {
    api('play-playlist', { path }).then((r) => { if (!r.ok) flash('That playlist has no songs that can be found'); });
  }
  function showList(which) { state.show = which; render(); }
  function listMenu(anchor) {
    const rect = anchor.getBoundingClientRect();
    const lists = Object.keys(LISTS).map((which) => ({ label: listName(which), checked: state.show === which, run: () => showList(which) }));
    const saved = state.playlists.filter((p) => p.list === state.show)
      .map((p) => ({ label: p.name, checked: p.name === state.names[state.show], run: () => playPlaylist(p.path) }));
    menu(rect.left, rect.bottom + 1, [...lists, null, ...fileItems(), null,
      ...(saved.length ? saved : [{ label: '(no saved playlists)', disabled: true }])]);
  }
  const colors = [['Blue (original)', 0], ['Purple', 40], ['Pink', 90], ['Red', 130], ['Orange', 165], ['Green', 250], ['Teal', 300]];
  function applyHue() {
    const hue = ((Number(state.settings.hue) || 0) % 360 + 360) % 360;
    document.documentElement.style.setProperty('--hue', hue + 'deg');
    document.body.classList.toggle('tinted', hue !== 0);
  }
  function setHue(hue) { state.settings.hue = hue; applyHue(); saveSettings(); }
  function hueMenu(anchor) {
    if (document.body.classList.contains('compact')) {   // no room for a menu there: step through the colours
      const at = colors.findIndex(([, hue]) => hue === (state.settings.hue || 0));
      setHue(colors[(at + 1) % colors.length][1]);
      return;
    }
    const rect = anchor.getBoundingClientRect();
    menu(rect.right, rect.top - 6, colors.map(([label, hue]) => ({
      label, checked: (state.settings.hue || 0) === hue, run: () => setHue(hue),
    })), { above: true, right: true });
  }
  function applySettings() {
    Visuals.scale = state.settings.zoom || 1;
    Visuals.select(state.settings.vis || 0);
    showToggles();
    applyHue(); applyVolume(); showVis();
    fire('settings');
  }

  // ---- views ----
  const views = ['now', 'library', 'radio', 'skins', 'premium', 'na'];
  function show(view, name) {
    state.view = view;
    for (const v of views) $('view-' + v).hidden = v !== view;
    if (view === 'na') $('na-title').textContent = name;
    for (const a of document.querySelectorAll('.task')) {
      a.classList.toggle('sel', a.dataset.view === view && (view !== 'na' || a.dataset.name === name));
    }
    fire('view', view);
    requestAnimationFrame(() => { updateTime(); regions(); });
  }
  for (const a of document.querySelectorAll('.task')) a.addEventListener('click', () => show(a.dataset.view, a.dataset.name));
  function layoutChanged() {
    Frame.draw();
    requestAnimationFrame(() => { updateTime(); setSlider($('volume'), state.settings.volume); regions(); });
  }

  // tell the host which parts of the frame move or resize the window
  function regions() {
    const box = (el) => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; };
    const seen = (el) => el.getClientRects().length > 0;
    const W = Math.round(innerWidth), H = Math.round(innerHeight);
    api('regions', {
      drag: [...document.querySelectorAll('.drag')].filter(seen).map(box),
      nodrag: [...document.querySelectorAll('.nodrag')].filter(seen).map(box),
      grip: seen($('grip')) ? box($('grip')) : null,
      edges: Frame.edges(W, H),
      shape: Frame.shape(W, H),
      width: W, height: H,
    });
  }

  // ---- wiring ----
  $('play').addEventListener('click', toggle);
  $('stop').addEventListener('click', () => stop());
  $('prev').addEventListener('click', () => step(-1));
  $('next').addEventListener('click', () => step(1));
  $('mute').addEventListener('click', () => { state.settings.muted = !state.settings.muted; applyVolume(); saveSettings(); });
  $('shuffle').addEventListener('click', toggleShuffle);
  $('repeat').addEventListener('click', cycleRepeat);
  $('np-select').addEventListener('click', (e) => listMenu(e.currentTarget));
  $('list-menu').addEventListener('click', (e) => mainMenu(e.currentTarget, true));
  $('shade').addEventListener('click', (e) => mainMenu(e.currentTarget));
  $('tint').addEventListener('click', (e) => hueMenu(e.currentTarget));
  $('vis-prev').addEventListener('click', () => setVis(Visuals.index - 1));
  $('vis-next').addEventListener('click', () => setVis(Visuals.index + 1));
  $('vis-menu').addEventListener('click', (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const items = Visuals.presets.map((p, i) => ({ label: p.name, checked: i === Visuals.index, run: () => setVis(i) }));
    items.push(null, { label: 'Graphic equalizer', checked: !!state.settings.eq_open, run: () => Mist.eq.toggle() });
    menu(rect.left, rect.top - 6, items, { above: true });
  });
  $('pane-toggle').addEventListener('click', () => { document.body.classList.toggle('no-pane'); layoutChanged(); });
  $('fullscreen').addEventListener('click', () => api('fullscreen'));
  $('stage').addEventListener('dblclick', (e) => { if (!e.target.closest('#eq')) api('fullscreen'); });
  $('w-min').addEventListener('click', () => api('minimize'));
  $('w-max').addEventListener('click', () => api('maximize'));
  $('w-close').addEventListener('click', () => api('close'));
  document.addEventListener('contextmenu', (e) => e.preventDefault());

  audio.addEventListener('timeupdate', updateTime);
  audio.addEventListener('seeked', report);
  audio.addEventListener('loadedmetadata', () => {
    const t = track(state.cur);
    if (t && length() && Math.abs((t.duration || 0) - audio.duration) > 0.5) {
      t.duration = audio.duration; api('duration', { id: t.id, seconds: t.duration }); render();
    }
  });
  audio.addEventListener('playing', () => {
    streamFails = 0;
    if (feed) {
      const first = !feed.started;
      feed.started = true; feed.stalled = false; state.played = feed.t.id;
      if (first) report();   // the host can announce it now
    }
    if (state.station !== null || feed) { flashUntil = 0; statusTick(true); }
  });
  // the song ran to its end by itself (the player's own audio, or the engine's song)
  function songEnded() {
    if (state.station !== null) { stop(); flash('The station stopped sending', 8000); return; }
    if (sleep.song) { sleep.song = false; stop(); flash('Sleep timer: the music was stopped', 3600000); return; }
    if (repeatMode() === 'one' && track(state.cur)) {
      if (ext) { const t = ext.t; ext = null; extStart(t, 0); } else { audio.currentTime = 0; start(); }
      return;
    }
    step(1, true);
  }
  audio.addEventListener('ended', songEnded);
  let radioError = '';
  audio.addEventListener('error', () => {
    if (!audio.src) return;
    if (feed) { streamFailed(feed, new Error('the player could not read this song')); return; }
    if (state.station !== null) {
      if (state.mode === 'playing') { stop(); flash('Radio: ' + (radioError || 'this station cannot be played'), 10000); }
    } else if (state.mode === 'playing') { flash('Cannot play this file', 1500); setTimeout(() => step(1, true), 1200); }
  });

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    const key = e.key.toLowerCase();
    let handled = true;
    if (e.ctrlKey && key === 'o') api('open-files');
    else if (e.ctrlKey && key === 'p' || key === ' ') toggle();
    else if (e.ctrlKey && key === 's') stop();
    else if (e.ctrlKey && key === 'b') step(-1);
    else if (e.ctrlKey && key === 'f') step(1);
    else if (e.ctrlKey && key === 't') cycleRepeat();
    else if (e.ctrlKey && key === 'h') toggleShuffle();
    else if (e.ctrlKey && key === '1') Mist.skins.leave();
    else if (e.ctrlKey && key === '2') Mist.skins.apply();
    else if (key === 'arrowleft') seekTo(position() - 5);
    else if (key === 'arrowright') seekTo(position() + 5);
    else if (key === 'arrowup' || key === 'arrowdown') setVolume(state.settings.volume + (key === 'arrowup' ? 0.05 : -0.05));
    else if (key === 'delete' && inView(state.sel) && state.view === 'now') api('remove', { ids: [state.sel] });
    else if (key === 'enter' && inView(state.sel) && state.view === 'now') play(state.sel);
    else if (key === 'f11' || (e.altKey && key === 'enter')) api('fullscreen');
    else if (key === 'escape') { closeMenus(); if (document.body.classList.contains('fullscreen')) api('fullscreen'); }
    else handled = false;
    if (handled) e.preventDefault();
  });

  window.addEventListener('resize', layoutChanged);

  function boot() {
    const events = new EventSource(base + 'api/events');
    events.onopen = () => refresh();  // anything that changed before we were listening
    events.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.event === 'playlist') refresh();
      else if (msg.event === 'play') refresh().then(() => { if (!msg.stay) show('now'); startTarget(msg); });
      else if (msg.event === 'control') control(msg.action, msg.value);
      else if (msg.event === 'notice') flash(msg.text, 6000);
      else if (msg.event === 'engine') extEvent(msg);
      else if (msg.event === 'radio-title' && msg.id === state.station && msg.title !== state.radioTitle) {
        state.radioTitle = msg.title; showTrack(); flashUntil = 0; statusTick(true); report();
      } else if (msg.event === 'radio-error' && msg.id === state.station) radioError = msg.message;
      fire('event', msg);
    };
    Visuals.init($('vis'));
    setMode('stopped');
    showTrack();
    refresh().then(layoutChanged);
  }

  window.Mist = {
    $, base, audio, state, api, on, fire, onPlay, boot, refresh, saveSettings, track, station, clock, esc, sentence, isStream, outside,
    play, playStation, toggle, stop, step, setVis, applyVolume, cycleRepeat, repeatMode, control,
    menu, closeMenus, trackMenu, queueNext, queueLast, flash, render, show, layoutChanged, regions, setSleep, setZoom,
    probe() {  // handy for debugging
      const data = new Uint8Array(analyserNode ? analyserNode.frequencyBinCount : 1);
      const wave = new Uint8Array(analyserNode ? analyserNode.fftSize : 1).fill(128);
      if (analyserNode) { analyserNode.getByteFrequencyData(data); analyserNode.getByteTimeDomainData(wave); }
      return JSON.stringify({ ctx: actx && actx.state, time: audio.currentTime, duration: audio.duration,
        peak: Math.max.apply(null, data), amp: Math.max.apply(null, [...wave].map((v) => Math.abs(v - 128))), paused: audio.paused, error: audio.error && audio.error.code, mode: state.mode,
        cur: state.cur, station: state.station, queue: state.queue,
        engine: ext && { serial: ext.serial, phase: ext.phase, started: ext.started, at: Math.round(extPosition() * 10) / 10, length: ext.dur } });
    },
  };
})();
