'use strict';
/*
 * The playback helper's preload for Spotify's page: main.js gives it to the window of a Spotify run only, where
 * it runs in every document before the page's own scripts. It does one thing: it puts the hook below into the
 * page's own world (contextBridge.executeInMainWorld), so that the page's first answers and the media elements its
 * scripts make are seen from the start. It hands the page nothing of the helper's: no ipcRenderer, no function the
 * page could call. Where the hook could not go in, main.js finds no window.__om in the page and ends.
 */
const { contextBridge } = require('electron');

// Runs in Spotify's page (it is sent as text), so it stands alone. It keeps, for the page functions in main.js:
// the account's plan, the one string of the page's own accountAttributes answer (nothing else of that answer, of
// any other answer, or of any request); the media elements the page's scripts play, even the ones that are never
// in the document; and which of them is the song Ocean Mist asked for. Nothing may play that Ocean Mist did not
// ask for: before its play, after its stop and after the song it asked for is over, an element that starts is
// paused at once (Spotify's page goes on with its own queue and autoplay when a song ends).
function pageHookSpotify() {
  if (window.__om) return;
  // serial: Ocean Mist's song in this document (0 none yet, -1 stopped); song: its element; done: the serial whose
  // song is over; length: the song's own length (the element's can grow when the page adds the next track to it)
  const om = { ended: 0, error: '', serial: 0, pauseFor: null, outside: false, product: '', song: null, done: 0, length: 0, last: 0, seekAt: 0, seen: [] };
  Object.defineProperty(window, '__om', { value: om });   // not writable: no element of the page can stand in for it

  // ---- no service worker ----
  // In a session that keeps nothing, the page's worker only fetched a second copy of its files and held it, with
  // the worker, in the engine's processes: some 120 MB while a song played. The page plays as without one
  try {
    if (navigator.serviceWorker) Object.defineProperty(navigator.serviceWorker, 'register', {
      value: () => Promise.reject(new DOMException('no service worker here', 'SecurityError')) });
  } catch (e) { /* it registers as before */ }

  // ---- the account's plan ----
  const own = (raw) => {   // a request to Spotify's own API (or, in a stand-in, to the stand-in itself)
    try {
      const url = new URL(String(raw), location.href);
      if (url.protocol === 'https:') return /^(api|api-partner)\.spotify\.com$/.test(url.hostname);
      return /\.localhost$/.test(location.hostname) && url.host === location.host;
    } catch (e) { return false; }
  };
  const nameOf = (url, body) => {   // the operation: in the body of a POST, or in the address of a GET
    try { const name = (JSON.parse(String(body)) || {}).operationName; if (name) return String(name); } catch (e) { /* not JSON */ }
    try { return String(new URL(String(url), location.href).searchParams.get('operationName') || ''); } catch (e) { return ''; }
  };
  const keep = (body) => {
    try {
      const product = body.data.me.account.product;
      if (typeof product === 'string' && product) om.product = product.slice(0, 40);
    } catch (e) { /* another shape: the plan stays unknown */ }
  };
  try {
    const realFetch = window.fetch;
    window.fetch = function (input, init) {
      let wanted = false;
      try {
        const url = input instanceof Request ? input.url : input;
        wanted = own(url) && nameOf(url, init && init.body) === 'accountAttributes';
      } catch (e) { wanted = false; }
      const reply = realFetch.apply(this, arguments);
      if (!wanted) return reply;
      return reply.then((answer) => {   // a copy is read before the page reads its own
        try { answer.clone().json().then(keep).catch(() => null); } catch (e) { /* not read */ }
        return answer;
      });
    };
    const realOpen = XMLHttpRequest.prototype.open, realSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) { try { this.__omUrl = String(url || ''); } catch (e) { /* fine */ } return realOpen.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function (body) {
      try {
        const xhr = this;
        if (own(xhr.__omUrl) && nameOf(xhr.__omUrl, body) === 'accountAttributes') {
          xhr.addEventListener('load', () => { try { keep(JSON.parse(xhr.responseText)); } catch (e) { /* not JSON */ } });
        }
      } catch (e) { /* the page's request goes on as it was */ }
      return realSend.apply(this, arguments);
    };
  } catch (e) { /* no plan is read, and the helper stops before playing */ }

  // ---- the media elements ----
  // the song is never the canvas: a muted, looping video with pictures
  const songLike = (el) => !!el && !el.muted && !el.loop && !(el.videoWidth > 0);
  const finite = (n) => (Number.isFinite(Number(n)) && Number(n) > 0 ? Number(n) : 0);
  const asked = () => om.serial > 0 && om.done !== om.serial;
  const over = (el) => {   // the song Ocean Mist asked for is over: say so, and let nothing go on after it
    if (!asked()) return;   // no song is asked for (the page goes to another's), or it is over already
    om.done = om.serial; om.ended += 1;
    try { if (!el.paused) el.pause(); } catch (e) { /* it stopped by itself */ }
  };
  const errors = { 1: 'MEDIA_ERR_ABORTED', 2: 'MEDIA_ERR_NETWORK', 3: 'MEDIA_ERR_DECODE', 4: 'MEDIA_ERR_SRC_NOT_SUPPORTED' };
  // the listeners go on each element itself: the song's is not in the document, so the document hears nothing of it
  const track = (el) => {
    try {
      if (!el || om.seen.indexOf(el) >= 0) return;
      om.seen.push(el);
      el.addEventListener('playing', () => {
        if (!asked()) { if (!el.muted) el.pause(); return; }   // nothing Ocean Mist asked for, or its song is over
        if (!songLike(el)) return;
        if (el !== om.song && om.song && (Number(om.song.currentTime) || 0) >= 3) { over(om.song); el.pause(); return; }   // another track, in an element of its own
        if (el !== om.song) {   // the first to start (or one that took over within its first seconds) is the song
          if (om.song) { try { om.song.pause(); } catch (e) { /* gone */ } }
          om.song = el; om.length = finite(el.duration); om.last = Number(el.currentTime) || 0;
        }
        if (om.pauseFor === om.serial) el.pause();   // paused meanwhile, by Ocean Mist or from elsewhere
      });
      el.addEventListener('pause', () => {
        if (el !== om.song || el.ended || om.done === om.serial || om.serial <= 0 || om.pauseFor === om.serial) return;
        om.outside = true; om.pauseFor = om.serial;   // a pause Ocean Mist did not ask for (another device, say)
      });
      el.addEventListener('ended', () => { if (el === om.song) over(el); });
      // a new address in the same element: the page went on to another track. Not before its first second:
      // Spotify's player sets its element up again just after it starts (an "emptied" at 0 s), and the song goes on
      for (const kind of ['emptied', 'loadstart']) {
        el.addEventListener(kind, () => {
          if (el !== om.song) return;
          if ((Number(om.last) || 0) >= 1) over(el);
          else om.length = 0;   // its length is read again once it is known
        });
      }
      el.addEventListener('durationchange', () => {
        if (el !== om.song || om.done === om.serial) return;
        const length = finite(el.duration), time = Number(el.currentTime) || 0;
        if (!length || Math.abs(length - om.length) <= 1) return;
        if (!om.length || time < 5) om.length = length;   // the page is still settling the song's own length
        else if (length < om.length || time >= om.length - 0.5) over(el);   // another track in the same element
        // longer, later on: the next track was added after this one, which still ends at its own length (below)
      });
      el.addEventListener('timeupdate', () => {
        if (el !== om.song || om.done === om.serial) return;
        const time = Number(el.currentTime) || 0;
        if (om.length && finite(el.duration) > om.length + 1 && time >= om.length - 0.25) over(el);   // the next track was added on
        else if (om.last - time > 3 && Date.now() - om.seekAt > 2000) over(el);   // it went back to a start Ocean Mist did not ask for
        om.last = time;
      });
      el.addEventListener('error', () => {
        if (el !== om.song && (om.song || !songLike(el) || om.serial <= 0)) return;
        om.error = (el.error && errors[el.error.code]) || 'MEDIA_ERR';
      });
    } catch (e) { /* this one is not followed */ }
  };
  // the song's element: the one Ocean Mist's play started; until it has, the likeliest of the others
  om.pick = function () {
    if (om.song) return om.song;
    const all = om.seen.slice();
    try { for (const cand of document.querySelectorAll('video,audio')) if (all.indexOf(cand) < 0) all.push(cand); } catch (e) { /* nothing to add */ }
    let best = null, rank = -1;
    for (const cand of all) {
      if (!songLike(cand)) continue;
      const r = (cand.paused ? 0 : 2) + (finite(cand.duration) ? 1 : 0);
      if (r > rank) { rank = r; best = cand; }
    }
    return best;
  };
  try {
    const realPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () { track(this); return realPlay.apply(this, arguments); };
    document.addEventListener('play', (e) => track(e.target), true);   // one in the document that plays by itself
  } catch (e) { /* pick() still looks at the document */ }
}

try { contextBridge.executeInMainWorld({ func: pageHookSpotify }); } catch (e) { /* main.js finds no hook, and ends */ }
