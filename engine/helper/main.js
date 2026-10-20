'use strict';
/*
 * Ocean Mist's playback helper: Apple Music's or Spotify's own web player in a window that is never shown,
 * so that a song of either plays as the service's own audio. engine.py starts it with the Chromium-based
 * runtime that has Widevine (the content-protection module the audio needs), signed in with the sign-in
 * Ocean Mist read from Firefox (Apple's one cookie, Spotify's cookies), and tells it what to do on its
 * standard input, a JSON object a line. It answers on its standard output, in lines that start with "OMP ":
 * codes and numbers, and short error texts in which the sign-in and every address are masked (engine.py
 * reads the codes and keeps nothing else). Everything it writes to disk goes to the folder given as --data
 * (Chromium's own files and Google's Widevine module); nothing of either service's does, as the page's
 * session lives in memory.
 *
 *   electron <this folder> --data=<folder>              the helper
 *   electron <this folder> --data=<folder> --selftest   starts, checks that Chromium's sandbox is on, ends
 *
 * in   {"cmd":"start","cookie":{"value","host","expires","secure","http_only","same_site"},"volume":0.5,"muted":false}
 *        or {"cmd":"start","service":"spotify","cookies":[{"name","value","host","expires","secure","http_only","same_site"},...],...}
 *        always the first line: the sign-in is never in an argument or an environment variable
 *      {"cmd":"play","serial":7,"ids":["i.AbC","1234"],"from":0}    the first id that the service accepts plays
 *      {"cmd":"pause"} {"cmd":"resume"} {"cmd":"seek","to":12.5} {"cmd":"stop"} {"cmd":"quit"}
 *      {"cmd":"volume","value":0.3,"muted":false}
 * out  start, widevine, ready, state, problem, denied, fail, selftest
 *
 * What may need updating when a service changes its page is kept together, in "what the page is asked" and,
 * for Spotify, in spotify-preload.js (the hook it puts into Spotify's page before the page's own scripts).
 */
const { app, BrowserWindow, session, components } = require('electron');
const fs = require('fs');
const path = require('path');

const T0 = Date.now();
const arg = (name) => {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : '';
};
const DATA = arg('data');
const SELFTEST = process.argv.includes('--selftest');
const APPLE_SITE = 'https://music.apple.com/';
const SPOTIFY_SITE = 'https://open.spotify.com/';
// a stand-in for a service's page, for checks: an address on a ".localhost" name, which never leaves the machine
const STAND_IN = /^http:\/\/[a-z0-9.-]+\.localhost:\d{2,5}\/$/.test(arg('site')) ? arg('site') : '';
let service = 'apple';               // what the start line says: which service's page this helper serves
let site = APPLE_SITE;
let SITE_HOST = new URL(APPLE_SITE).hostname;
const APPLE = /(^|\.)apple\.com$/i;
const SPOTIFY = /(^|\.)spotify\.com$/i;
// may the page, or a frame of it, go to this address? the service's own over https (and, for checks, the
// stand-in's name); a blank frame or an object the page made itself is not another site
const mayGo = (raw) => {
  try {
    const url = new URL(raw);
    if (url.protocol === 'https:') return service === 'spotify' ? SPOTIFY.test(url.hostname) : APPLE.test(url.hostname);
    if (url.protocol === 'http:') return !!STAND_IN && url.hostname === SITE_HOST;
    return url.protocol === 'about:' || url.protocol === 'blob:';
  } catch (_) { return false; }
};
const SIGN_OUT = /log.?out|sign.?out|log.?off|sign.?off|unauthori[sz]e/i;   // the same as apple.py and spotify.py block: a sign-out here would end Firefox's sign-in too
const PERMITTED = new Set(['protected-media-identifier']);   // the only permission the service's player may be given
const READY_WITHIN = 45000;       // the service's page gets this long to open and to say it is signed in
const SIGNED_WITHIN = 10000;      // ...and this long, once its player is up, to say it is signed in
const WIDEVINE_WITHIN = 240000;   // Google's module can have to come over the network on the first run
const LOADING_WITHIN = 40000;     // a song that has not started by then did not
const PAGE_WITHIN = 10000;        // the page gets this long to answer a look or a command (a song being queued gets LOADING_WITHIN)
const PLAN_WITHIN = 15000;        // Spotify's page gets this long, once it is up, to have said the account's plan
const PAGE_DEAD_AFTER = 3;        // looks in a row that were not answered in time: the page is hung, and the helper ends
const TICK = 500;                 // how often the page is asked how the song is doing
const BEAT = 1000;                // how often the position is said while a song plays

const say = (event, data) => process.stdout.write(
  'OMP ' + JSON.stringify({ event, t: Math.round((Date.now() - T0) / 100) / 10, ...(data || {}) }) + '\n');
const secrets = [];   // the sign-in's values, so that nothing said can carry them out
const mask = (text) => {
  let out = String(text || '');
  for (const secret of secrets) if (secret) out = out.split(secret).join('<token>');
  return out.replace(/https?:\/\/\S+/g, '<address>').replace(/\s+/g, ' ').slice(0, 160);
};
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const round = (n) => Math.round((Number(n) || 0) * 10) / 10;

class Fail extends Error {
  constructor(code, why) { super(why || code); this.code = code; }
}

if (!DATA) {
  say('fail', { code: 'page', why: 'no --data folder' });
  process.exit(2);
}
app.setPath('userData', DATA);
app.setPath('sessionData', DATA);
app.disableHardwareAcceleration();   // it only plays: no graphics card, and no pictures below
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
// no second media player on the desktop: Ocean Mist has its own, and the media keys are its
app.commandLine.appendSwitch('disable-features', 'MediaSessionService,HardwareMediaKeyHandling');

function finish(code) {
  process.stdout.write('', () => app.exit(code));
  setTimeout(() => app.exit(code), 1500).unref();
}
// the player went away and took the pipe with it: there is nobody to tell, and no box to show
process.stdout.on('error', () => app.exit(0));
process.on('uncaughtException', (e) => {
  try { say('fail', { code: 'page', why: mask(e && e.message) }); } catch (_) { /* the pipe is gone */ }
  app.exit(1);
});

// ---- what arrives on stdin ----
let started = null;                 // the "start" line, once it came
let onStart = () => {};
const hello = new Promise((resolve) => { onStart = resolve; });
const held = [];                    // commands that came before the page was ready
let handle = null;                  // set when it is
let pending = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  pending += chunk;
  let at;
  while ((at = pending.indexOf('\n')) >= 0) {
    const line = pending.slice(0, at).trim();
    pending = pending.slice(at + 1);
    let msg;
    try { msg = JSON.parse(line); } catch (_) { continue; }
    if (!msg || typeof msg !== 'object' || Array.isArray(msg)) continue;
    if (!started) { if (msg.cmd === 'start') { started = msg; onStart(msg); } } else if (msg.cmd === 'quit') quit(); else if (handle) handle(msg); else held.push(msg);
  }
});
process.stdin.on('end', () => { if (!SELFTEST) quit(); });   // the player is gone: so is the helper

// ---- what the page is asked ----
// These run inside the service's page (they are sent as text), so each stands alone. On Apple's page MusicKit
// is Apple's own player object; on Spotify's page the player is the page itself and its testids. The requests
// either page makes are the service's own, not a published way in, so they can change at any time.

// is Apple's player up, and does it say you are signed in?
function pageReady() {
  try {
    const mk = MusicKit.getInstance();
    return JSON.stringify({ host: location.host, api: !!(mk.api && typeof mk.api.music === 'function'), signed: !!mk.isAuthorized });
  } catch (e) { return JSON.stringify({ host: location.host, api: false, signed: false }); }
}

// the account's subscription, and the store (country) it belongs to
async function pageAccount() {
  const mk = MusicKit.getInstance();
  const reply = await mk.api.music('/v1/me/account', { meta: 'subscription' });
  const body = reply && reply.data !== undefined ? reply.data : reply;
  const sub = body && body.meta && body.meta.subscription;
  return JSON.stringify({
    active: !!(sub && sub.active),
    storefront: sub && sub.storefront ? String(sub.storefront).toLowerCase() : '',
    current: String(mk.storefrontId || mk.storefrontCountryCode || '').toLowerCase(),
  });
}

// once per page: count the media element's ends, and keep Apple's own playback errors
function pageHook() {
  if (window.__om) return 'hooked';
  const om = window.__om = { ended: 0, error: '', serial: 0, pauseFor: null };
  document.addEventListener('ended', () => { om.ended += 1; }, true);   // media events do not bubble, but they are captured
  try {
    MusicKit.getInstance().addEventListener('mediaPlaybackError', (e) => {
      const why = e && ((e.error && (e.error.errorCode || e.error.name)) || e.errorCode || e.name || e.message);
      om.error = String(why || 'UNKNOWN').slice(0, 40);
    });
  } catch (e) { /* the page still tells its end and its state */ }
  return 'hooked';
}

// plays one song: the first of `ids` that Apple takes (the library's id, then the catalogue's). A newer song
// asked for meanwhile (`serial`) ends this try; so does a stop (it sets the serial to -1), and then nothing
// may be left playing; a pause that came while the song was being queued lets it start paused
async function pagePlay(ids, volume, serial) {
  const om = window.__om, mk = MusicKit.getInstance();
  om.serial = serial; om.error = ''; om.pauseFor = null;
  const code = (e) => String((e && (e.errorCode || e.name || e.message)) || e).replace(/\s+/g, ' ').slice(0, 40);
  const superseded = () => JSON.stringify({ ok: false, code: 'SUPERSEDED' });
  let last = 'NO_SONG';
  for (const id of ids) {
    try {
      await mk.setQueue({ song: String(id) });
      if (om.serial !== serial) return superseded();
      mk.volume = volume;
      await mk.play();
      if (om.serial !== serial) {
        if (om.serial === -1) { try { await mk.pause(); await mk.stop(); } catch (_) { /* nothing to stop */ } }
        return superseded();
      }
      if (om.pauseFor === serial) { try { await mk.pause(); } catch (_) { /* it plays then, and the next look says so */ } }
      return JSON.stringify({ ok: true, code: '' });
    } catch (e) {
      if (om.serial !== serial) return JSON.stringify({ ok: false, code: 'SUPERSEDED' });
      last = code(e);
      try { await mk.stop(); } catch (_) { /* nothing was queued */ }
    }
  }
  return JSON.stringify({ ok: false, code: last });
}

// how the song is doing
function pageWatch() {
  const mk = MusicKit.getInstance(), om = window.__om || {};
  const names = {};
  for (const [name, value] of Object.entries(MusicKit.PlaybackStates || {})) names[value] = name;
  return JSON.stringify({
    signed: !!mk.isAuthorized,
    state: String(names[mk.playbackState] || 'unknown').toLowerCase(),
    time: Number(mk.currentPlaybackTime) || 0,
    duration: Number(mk.currentPlaybackDuration) || 0,
    ended: om.ended || 0,
    error: om.error || '',
  });
}

async function pagePause() { const om = window.__om; if (om) om.pauseFor = om.serial; await MusicKit.getInstance().pause(); return 'ok'; }
async function pageResume() { const om = window.__om; if (om) om.pauseFor = null; await MusicKit.getInstance().play(); return 'ok'; }
async function pageSeek(to) { await MusicKit.getInstance().seekToTime(to); return 'ok'; }
function pageVolume(value) { MusicKit.getInstance().volume = value; return 'ok'; }
async function pageStop() {
  try { const om = window.__om; if (om) om.serial = -1; const mk = MusicKit.getInstance(); await mk.pause(); await mk.stop(); } catch (e) { /* not playing */ }
  return 'ok';
}

// ---- Spotify's page ----
// Spotify has no player object to ask: a track's page offers a Play button, and the page plays through a media
// element its own scripts made, which is never in the document. The hook that follows those elements and keeps the
// account's plan (the product of the page's own "accountAttributes" answer) is put in before the page's scripts run,
// by spotify-preload.js, in every document; these functions read what it keeps. The testids and the request shapes
// may change when Spotify changes its page.

// is Spotify's player up, and does it say you are signed in?
function pageReadySpotify() {
  try {
    const widget = !!document.querySelector('[data-testid="user-widget-link"]');
    const login = !!document.querySelector('[data-testid="login-button"]');
    return JSON.stringify({ host: location.host, api: widget || login, signed: widget && !login });
  } catch (e) { return JSON.stringify({ host: location.host, api: false, signed: false }); }
}

// the account's plan as the page's own accountAttributes answer gave it, '' while it has not come (a stand-in may
// say it with window.__omProduct, which counts only when the helper runs against one)
function pageAccountSpotify(standIn) {
  const om = window.__om;
  if (!om) return JSON.stringify({ hooked: false, product: '' });
  const product = standIn && typeof window.__omProduct === 'string' ? window.__omProduct : om.product;
  return JSON.stringify({ hooked: true, product: String(product || '').slice(0, 40) });
}

// goes to another song's page inside the page that is open, as a link of Spotify's own does (no new document, so
// nothing has to start up again): the song that plays stops first, and the page is there once its address and title
// are the new song's and its Play button is a new one (the old ones are marked, so that none of them is clicked).
// 'stuck' when that does not come in time, or when the song's page is the one that is open already (its button
// would pause it): the helper then opens the song's page as a document of its own
async function pageMoveSpotify(path) {
  const om = window.__om;
  if (!om || location.pathname === path) return 'stuck';
  om.serial = -1; om.song = null;   // what the element of the song it leaves does from here on is not a song's
  for (const el of om.seen) { try { if (!el.muted && !el.paused) el.pause(); } catch (e) { /* not playing */ } }
  const before = document.title;
  for (const b of document.querySelectorAll('[data-testid="play-button"]')) b.setAttribute('data-om-old', '');
  history.pushState({}, '', path);
  window.dispatchEvent(new PopStateEvent('popstate', { state: history.state }));
  const until = Date.now() + 3000;
  while (Date.now() < until) {
    if (location.pathname === path && document.title !== before
        && document.querySelector('[data-testid="track-page"] [data-testid="action-bar-row"] [data-testid="play-button"]:not([data-om-old])')) return 'moved';
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return 'stuck';
}

// is the preload's hook in this document?
function pageHookSpotify() { return window.__om ? 'hooked' : 'missing'; }

// plays one song: its page is already open (the helper went to it); wait for the Play button, click it (a real
// click: no autoplay block) and wait for the song's element to start. A newer song asked for meanwhile (serial)
// ends this try; so does a stop (it sets the serial to -1), and then nothing may be left playing. `paused`: a pause
// came while the song's page loaded (main.js kept it, as this document did not exist yet), and the song starts
// paused, as it does after a pause that comes while this waits for it. Where a song goes on from is applied by the
// watch.
async function pagePlaySpotify(id, volume, serial, paused, within) {
  const om = window.__om;
  om.serial = serial; om.error = ''; om.pauseFor = paused ? serial : null; om.outside = false; om.song = null; om.done = 0; om.length = 0;
  const quiet = () => { for (const el of om.seen) { try { if (!el.muted && !el.paused) el.pause(); } catch (e) { /* gone */ } } };
  const failed = (code) => { om.error = code; quiet(); return JSON.stringify({ ok: false, code }); };
  const until = Date.now() + 15000, others = Date.now() + 4000;   // the top bar's, where the song's own does not come
  let button = null;
  while (Date.now() < until) {
    if (document.querySelector('[data-testid="login-button"]')) return JSON.stringify({ ok: false, code: 'SIGNED_OUT' });
    if (om.serial !== serial) break;
    button = document.querySelector('[data-testid="track-page"] [data-testid="action-bar-row"] [data-testid="play-button"]:not([data-om-old])')
      || (Date.now() > others ? document.querySelector('[data-testid="topbar"] [data-testid="play-button"]:not([data-om-old])') : null);   // the song's own (the top bar's can still be the page's before), not one of a page it left
    if (button) break;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (om.serial !== serial) return JSON.stringify({ ok: false, code: 'SUPERSEDED' });
  if (!button) return failed('PLAY_BUTTON');
  try { button.click(); } catch (e) { return failed('PLAY_BUTTON'); }
  const started = Date.now() + (Number(within) || 20000);   // the song's element has this long to start: after it, it did not
  while (Date.now() < started && !om.song && !om.error) {
    if (om.serial !== serial) {
      if (om.serial === -1) quiet();
      return JSON.stringify({ ok: false, code: 'SUPERSEDED' });
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  if (om.serial !== serial) { if (om.serial === -1) quiet(); return JSON.stringify({ ok: false, code: 'SUPERSEDED' }); }
  if (om.error) return failed(om.error);
  if (!om.song) return failed('NO_AUDIO');
  if (om.pauseFor === serial) { try { om.song.pause(); } catch (e) { /* the next look says so */ } }
  try { om.song.volume = volume; } catch (e) { /* the page's own volume is close enough */ }
  return JSON.stringify({ ok: true, code: '' });
}

// how the song is doing; "elsewhere" when a pause Ocean Mist did not ask for came with the page saying that the
// account plays on another device
function pageWatchSpotify() {
  const om = window.__om || {};
  const signed = !!document.querySelector('[data-testid="user-widget-link"]');
  let el = null;
  try { el = om.pick ? om.pick() : null; } catch (e) { el = null; }
  const time = el ? Number(el.currentTime) || 0 : 0;
  const duration = om.length || (el && Number.isFinite(Number(el.duration)) ? Number(el.duration) : 0);
  let state = 'loading';
  if (om.done && om.done === om.serial) state = 'ended';
  else if (el) {
    if (el.ended) state = 'ended';
    else if (!el.paused) state = 'playing';
    else if (time > 0 || (el === om.song && om.pauseFor === om.serial)) state = 'paused';
  }
  let elsewhere = false;
  try { elsewhere = state === 'paused' && !!om.outside && !!document.querySelector('[data-testid="connect-bar"],[data-testid="connect-device-bar"]'); } catch (e) { /* not known */ }
  return JSON.stringify({ signed, state, time: duration ? Math.min(time, duration) : time, duration, ended: om.ended || 0, error: om.error || '', elsewhere, serial: om.serial || 0 });
}

async function pagePauseSpotify() {
  const om = window.__om; if (om) om.pauseFor = om.serial;
  try { const el = om && om.pick(); if (el) el.pause(); } catch (e) { /* not playing */ }
  return 'ok';
}
async function pageResumeSpotify() {
  const om = window.__om; if (!om || om.done === om.serial) return 'ok';   // a song that is over does not start again
  om.pauseFor = null; om.outside = false;
  try { const el = om.pick(); if (el) await el.play(); } catch (e) { /* not playing */ }
  return 'ok';
}
async function pageSeekSpotify(to) {
  const om = window.__om;
  try { const el = om && om.pick(); if (el) { om.seekAt = Date.now(); el.currentTime = Math.max(0, Number(to) || 0); om.last = el.currentTime; } } catch (e) { /* not playing */ }
  return 'ok';
}
function pageVolumeSpotify(value) {
  try { const el = window.__om && window.__om.pick(); if (el) el.volume = Math.max(0, Math.min(1, Number(value) || 0)); } catch (e) { /* not playing */ }
  return 'ok';
}
async function pageStopSpotify() {
  const om = window.__om; if (!om) return 'ok';
  om.serial = -1;
  for (const el of om.seen) { try { if (!el.muted && !el.paused) el.pause(); } catch (e) { /* not playing */ } }
  return 'ok';
}

// ---- the page ----
let page = null;                    // the window's contents
class Late extends Error {}         // the page did not answer in time
const inPageFor = async (ms, fn, ...args) => {
  const said = await Promise.race([
    page.executeJavaScript(`(${fn})(...${JSON.stringify(args)})`, true),   // as a user's click: no autoplay block
    delay(ms).then(() => { throw new Late('the page did not answer in time'); }),
  ]);
  return typeof said === 'string' && said[0] === '{' ? JSON.parse(said) : said;
};
const inPage = (fn, ...args) => inPageFor(PAGE_WITHIN, fn, ...args);

// which page functions this run asks: Spotify's, or (the default) Apple's. "go" means each song's page is a
// document of its own, which the helper goes to before playing from it
let P = null;
const pageSet = () => service === 'spotify'
  ? { ready: pageReadySpotify, account: pageAccountSpotify, hook: pageHookSpotify, move: pageMoveSpotify, play: pagePlaySpotify, watch: pageWatchSpotify,
      pause: pagePauseSpotify, resume: pageResumeSpotify, seek: pageSeekSpotify, volume: pageVolumeSpotify, stop: pageStopSpotify, go: true }
  : { ready: pageReady, account: pageAccount, hook: pageHook, play: pagePlay, watch: pageWatch,
      pause: pagePause, resume: pageResume, seek: pageSeek, volume: pageVolume, stop: pageStop, go: false };

// wait for the service's player to be up and to say that it is signed in
async function waitReady() {
  let up = 0;
  const until = Date.now() + READY_WITHIN;
  while (Date.now() < until) {
    const said = await inPage(P.ready).catch(() => ({}));
    if (said.api && said.signed) return;
    if (said.host && said.host !== SITE_HOST) throw new Fail('signed-out', 'the page moved to another host');
    if (said.api) {
      up = up || Date.now();
      if (Date.now() - up > SIGNED_WITHIN) throw new Fail('signed-out', 'the player is up but not signed in');
    }
    await delay(400);
  }
  throw new Fail('offline', 'the player was not ready in time');
}

const open = (address) => Promise.race([
  page.loadURL(address), delay(READY_WITHIN).then(() => { throw new Fail('offline', 'the page did not load in time'); })]);

// ---- the song ----
// { serial, phase, armed, seekTo, endedBase, said, stalled, stopped, placed, paused, volume }: for Spotify, placed
// once the song's play is under way in its own document; until then a pause, a resume and a volume wait here
// (paused, and the volume below), as the document that is open is still the one the song leaves. volume: the one
// the song's play was given
let cur = null;
let last = { time: 0, duration: 0 };
let volume = 0.5, muted = false;
let unsigned = 0;                   // polls in a row that said the sign-in is gone
let unanswered = 0;                 // looks in a row that the page did not answer in time
let watching = false;
let moving = null;                  // Spotify: the page's move to a song's page, while it is under way
let clicked = false;                // Spotify: a song was asked of this page and has not started (it may yet)

const reportState = (state, extra) => {
  cur.phase = state; cur.said = Date.now();
  say('state', { serial: cur.serial, state, position: round(last.time), duration: round(last.duration), ...extra });
};

// what the player's error says, as a code engine.py words
function problemOf(code) {
  const c = String(code || '').toUpperCase();
  if (/AUTHORI|TOKEN|ACCESS_DENIED|SIGNED_OUT/.test(c)) return 'signed-out';
  if (/SUBSCRIPTION|PREMIUM/.test(c)) return 'no-subscription';
  if (/UNAVAILABLE|RESTRICT|GEO|NOT_FOUND|UNSUPPORTED|EQUIVALENT|NO_SONG|PLAY_BUTTON/.test(c)) return 'unavailable';
  if (/DEVICE_LIMIT|QUOTA|LIMIT/.test(c)) return 'limit';
  if (/NETWORK|REQUEST|SERVER|TIMEOUT|OFFLINE/.test(c)) return 'offline';
  return 'play';
}

function problem(serial, code) {
  say('problem', { serial, code: problemOf(code), detail: mask(code) });
  if (cur && cur.serial === serial) cur = null;
}

async function play(cmd) {
  const serial = Number(cmd.serial) || 0;
  const ids = (Array.isArray(cmd.ids) ? cmd.ids : []).map(String).filter((id) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$/.test(id)).slice(0, 4);
  const from = Number(cmd.from) > 1 ? Number(cmd.from) : 0;
  last = { time: 0, duration: 0 };
  const song = cur = { serial, phase: 'loading', armed: false, seekTo: from, endedBase: 0, said: 0, stalled: 0, stopped: 0, placed: !P.go, paused: false, volume };
  say('state', { serial, state: 'loading', position: 0, duration: 0 });
  if (!ids.length) { problem(serial, 'NO_SONG'); return; }
  let result;
  if (P.go) {
    // Spotify: the song plays from its own page. The first try goes there inside the page that is open, which
    // starts the song in about a second; where that does not work, the song's page is opened as a document of its
    // own (the preload puts the hook in). Now and then Spotify's page takes the click and never makes the song's
    // element (its buttons say it plays, and nothing loads), and now and then the page does not open: the song's
    // page is then opened once more, as a document of its own, and asked again
    for (let attempt = 1; ; attempt++) {
      const again = attempt < 2;
      song.placed = false;
      let hooked;
      try {
        // not while an earlier song may yet start in this page, or its page is still being drawn: Spotify can
        // start that song after this one's click (live, at 0.3 s apart). Its own document leaves nothing of it behind
        let moved = 'stuck';
        if (again && !clicked && !moving && page.getURL().startsWith(site)) {
          const going = moving = inPageFor(5000, P.move, `/track/${ids[0]}`).catch(() => 'stuck');
          moved = await going;
          if (moving === going) moving = null;
        }
        if (cur !== song) return;
        if (moved !== 'moved') { await open(`${site}track/${ids[0]}`); clicked = false; }
        hooked = await inPage(P.hook);
      } catch (e) {
        if (cur !== song) return;
        if (again && !(e instanceof Late)) continue;
        problem(serial, e instanceof Late ? 'TIMEOUT' : 'offline');
        return;
      }
      if (cur !== song) return;   // another song was asked for while this one's page loaded
      if (hooked !== 'hooked') { fatal(new Fail('page', 'the page has no hook')); return; }
      const before = await inPage(P.watch).catch(() => null);
      if (before) song.endedBase = before.ended;   // the ends of earlier songs do not count
      try {   // from here on what is asked for goes to the song's document, which this play makes its own
        song.placed = true; song.volume = volume;
        // a song that starts makes its element about a second after the click (0.9 to 1.3 s, live): 5 s without one is a stall
        clicked = true;
        result = await inPageFor(LOADING_WITHIN, P.play, ids[0], volume, serial, song.paused, again ? 5000 : 20000);
        if (result.ok) clicked = false;
      } catch (e) {
        result = { ok: false, code: e instanceof Late ? 'TIMEOUT' : 'PAGE_ERROR' };
        if (e instanceof Late) inPage(P.stop).catch(() => null);   // it may still start by itself later, and must not
      }
      if (cur !== song || result.code === 'SUPERSEDED') return;
      if (again && result.code === 'NO_AUDIO') continue;
      break;
    }
  } else {
    const before = await inPage(P.watch).catch(() => null);
    if (before) song.endedBase = before.ended;   // the ends of earlier songs do not count
    try {
      result = await inPageFor(LOADING_WITHIN, P.play, ids, volume, serial);
    } catch (e) {
      result = { ok: false, code: e instanceof Late ? 'TIMEOUT' : 'PAGE_ERROR' };
      if (e instanceof Late) inPage(P.stop).catch(() => null);   // it may still start by itself later, and must not
    }
  }
  if (cur !== song || result.code === 'SUPERSEDED') return;   // another song was asked for meanwhile
  if (!result.ok) { problem(serial, result.code); return; }
  song.armed = true;
  // a volume that came while the song was waited for: the play gave the song the one before
  if (P.go && volume !== song.volume) inPage(P.volume, volume).catch(() => null);
}

async function tick() {
  if (watching || !cur || !cur.armed) return;
  watching = true;
  const song = cur;
  try {
    const w = await inPage(P.watch);
    if (cur !== song) return;   // another song was asked for, or this one is over, while the page was looked at
    unanswered = 0;
    // the song's document is gone (only once it is there: while the song's page loads, the one it leaves answers)
    if (P.go && song.armed && w.serial !== song.serial) { reportState('ended'); cur = null; return; }
    unsigned = w.signed ? 0 : unsigned + 1;
    if (unsigned >= 3) throw new Fail('signed-out', 'the page says the sign-in is gone');
    if (w.state === 'playing' || w.state === 'paused') last = { time: w.time, duration: w.duration || last.duration };
    if (w.error) { problem(song.serial, w.error); return; }
    if (w.elsewhere) { problem(song.serial, 'DEVICE_LIMIT'); return; }   // the account plays on another device now
    const over = w.ended > song.endedBase || w.state === 'ended' || w.state === 'completed';
    const finished = song.phase === 'playing' && w.state !== 'playing' && w.duration > 5 && w.time >= w.duration - 0.6;
    if (over || finished) {
      reportState('ended');
      cur = null;
      return;
    }
    song.stopped = w.state === 'stopped' ? song.stopped + 1 : 0;
    if (w.state === 'playing' || w.state === 'paused') song.stalled = 0;
    // where a song restored from earlier goes on from (a Spotify song that starts paused gets it at once)
    if (song.seekTo && (w.state === 'playing' || (P.go && w.state === 'paused'))) {
      const to = song.seekTo; song.seekTo = 0;
      await inPage(P.seek, to).catch(() => null);
      if (cur !== song) return;   // another song was asked for meanwhile: its place is not this one's
      last.time = to;
    }
    if (w.state === 'playing') {
      if (song.phase !== 'playing' || Date.now() - song.said >= BEAT) reportState('playing');
    } else if (w.state === 'paused') {
      if (song.phase !== 'paused') reportState('paused');
    } else {
      song.stalled = song.stalled || Date.now();
      if (song.stopped >= 3) { problem(song.serial, 'STOPPED'); return; }   // the player stopped by itself
      if (song.phase === 'playing') reportState('loading');   // it ran dry: the player is fetching more
      else if (Date.now() - song.stalled > LOADING_WITHIN) problem(song.serial, 'TIMEOUT');
    }
  } catch (e) {
    if (e instanceof Fail) fatal(e);
    else if (e instanceof Late && (unanswered += 1) >= PAGE_DEAD_AFTER) fatal(new Fail('crash', 'the page stopped answering'));
  } finally { watching = false; }
}

// ---- commands ----
async function command(msg) {
  try {
    const song = cur;
    const waits = !!song && !song.placed;   // Spotify's song whose page is loading: its play takes this with it
    if (msg.cmd === 'play') await play(msg);
    else if (msg.cmd === 'pause') { if (waits) song.paused = true; await inPage(P.pause); }   // what plays now pauses too
    else if (msg.cmd === 'resume') { if (waits) song.paused = false; else await inPage(P.resume); }
    else if (msg.cmd === 'seek') {
      const to = Math.max(0, Number(msg.to) || 0);
      if (song && song.phase === 'loading') song.seekTo = to;   // when it has started
      else { await inPage(P.seek, to); if (cur === song) { last.time = to; if (cur) cur.said = 0; } }
    } else if (msg.cmd === 'volume') {
      volume = Math.max(0, Math.min(1, Number(msg.value) || 0)); muted = !!msg.muted;
      page.setAudioMuted(muted);
      if (!waits) await inPage(P.volume, volume);
    } else if (msg.cmd === 'stop') {
      cur = null;
      await inPage(P.stop);
    }
  } catch (e) { /* a command that cannot be done leaves the song as it was; the next look at it says how it is */ }
}

let leaving = false;
async function quit() {
  if (leaving) return;
  leaving = true;
  try { if (page) await Promise.race([inPage(P.stop), delay(1500)]); } catch (e) { /* going anyway */ }
  finish(0);
}

function fatal(e) {
  if (leaving) return;
  leaving = true;
  say('fail', { code: e instanceof Fail ? e.code : 'page', why: mask(e && e.message) });
  finish(1);
}

const summary = (status) => {
  const out = [];
  for (const item of Object.values(status || {})) {
    if (item && typeof item === 'object') out.push({ version: mask(item.version), status: mask(item.status) });
  }
  return out;
};

// ---- the run ----
async function run() {
  say('start', { electron: process.versions.electron, chrome: process.versions.chrome });
  const first = await Promise.race([hello, delay(20000).then(() => null)]);
  if (!first || first.cmd !== 'start') throw new Fail('no-cookie', 'no start line arrived on stdin');
  service = first.service === 'spotify' ? 'spotify' : 'apple';
  site = STAND_IN || (service === 'spotify' ? SPOTIFY_SITE : APPLE_SITE);
  SITE_HOST = new URL(site).hostname;
  P = pageSet();
  const cookie = first.cookie;
  const cookies = service === 'spotify' && Array.isArray(first.cookies) ? first.cookies : [];
  if (service === 'apple') {
    if (!cookie || !cookie.value) throw new Fail('no-cookie', 'no sign-in arrived on stdin');
    secrets.push(String(cookie.value));
  } else if (!cookies.some((one) => one && one.name && one.value)) {
    throw new Fail('no-cookie', 'no sign-in arrived on stdin');
  }
  volume = Math.max(0, Math.min(1, Number(first.volume) || 0)); muted = !!first.muted;

  // Widevine comes from Google, once
  const began = Date.now();
  try {
    if (!components || typeof components.whenReady !== 'function') throw new Error('this runtime has no Widevine support');
    await Promise.race([components.whenReady(), delay(WIDEVINE_WITHIN).then(() => { throw new Error('the Widevine module did not arrive in time'); })]);
  } catch (e) {
    say('widevine', { ready: false, why: mask(e && e.message), components: summary(components && components.status && components.status()) });
    throw new Fail('widevine', e && e.message);
  }
  say('widevine', { ready: true, seconds: round((Date.now() - began) / 1000), components: summary(components.status()) });

  const ses = session.fromPartition(service === 'spotify' ? 'ocean-mist-spotify' : 'ocean-mist-apple');   // no "persist:": in memory only
  ses.setUserAgent(`Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${process.versions.chrome} Safari/537.36`);
  ses.webRequest.onBeforeRequest((details, callback) => {
    callback(SIGN_OUT.test(details.url) ? { cancel: true } : {});
  });
  const denied = new Set();
  ses.setPermissionRequestHandler((contents, permission, callback, details) => {
    const asked = (details && details.requestingUrl) || contents.getURL();
    if (!PERMITTED.has(permission) && !denied.has(permission)) { denied.add(permission); say('denied', { permission: mask(permission) }); }
    callback(PERMITTED.has(permission) && mayGo(asked));
  });
  ses.setPermissionCheckHandler((_contents, permission, origin) => PERMITTED.has(permission) && mayGo(origin));
  if (service === 'apple') {
    await ses.cookies.set({
      url: site, name: 'media-user-token', value: String(cookie.value), domain: cookie.host, path: '/',
      secure: site.startsWith('https://') || !!cookie.secure, httpOnly: !!cookie.http_only, sameSite: cookie.same_site || 'no_restriction',
      ...(cookie.expires ? { expirationDate: Number(cookie.expires) } : {}),
    });
    cookie.value = null;
  } else {
    for (const one of cookies) {   // each of Spotify's cookies, in memory only, and only here
      try {
        await ses.cookies.set({
          url: site, name: String(one.name), value: String(one.value), path: '/',
          ...(one.host ? { domain: String(one.host) } : {}),
          secure: site.startsWith('https://') || !!one.secure, httpOnly: !!one.http_only, sameSite: one.same_site || 'no_restriction',
          ...(one.expires ? { expirationDate: Number(one.expires) } : {}),
        });
      } catch (e) { throw new Fail('no-cookie', `the sign-in could not be set: ${mask(e && e.message)}`); }
      secrets.push(String(one.value));
      one.value = null;
    }
  }

  const win = new BrowserWindow({
    show: false, width: 1000, height: 700, skipTaskbar: true, focusable: false,
    webPreferences: {
      session: ses, backgroundThrottling: false, contextIsolation: true, nodeIntegration: false,
      sandbox: true, images: false, spellcheck: false, autoplayPolicy: 'no-user-gesture-required',
      // Spotify's: the hook goes into each document before the page's own scripts (it gives the page nothing)
      ...(service === 'spotify' ? { preload: path.join(__dirname, 'spotify-preload.js') } : {}),
    },
  });
  page = win.webContents;
  page.setAudioMuted(muted);
  page.setWindowOpenHandler(() => ({ action: 'deny' }));
  for (const kind of ['will-navigate', 'will-redirect', 'will-frame-navigate']) {   // a page's own, a server's redirect, a frame's
    page.on(kind, (event, url) => { if (!mayGo((event && event.url) || url)) event.preventDefault(); });
  }
  page.on('render-process-gone', () => fatal(new Fail('crash', 'the page\'s process ended')));

  await open(site);
  await waitReady();

  if (service === 'spotify') {
    // a song plays only for an account known to have Premium: a plan whose name starts with "premium"
    const until = Date.now() + PLAN_WITHIN;
    let plan = '';
    while (!plan) {
      const said = await inPage(P.account, !!STAND_IN);
      if (!said.hooked) throw new Fail('page', 'the page has no hook');
      plan = said.product;
      if (!plan && Date.now() > until) throw new Fail('no-plan', 'the page did not say the account\'s plan in time');
      if (!plan) await delay(300);
    }
    if (!/^premium/i.test(plan)) throw new Fail('no-subscription', 'the account has no active Spotify Premium subscription');
    // a document Ocean Mist did not go to: the song in the one it left is over. Only a navigation that commits counts
    // ("did-navigate" comes once the window has a new document of the page's): one that is blocked, or never commits,
    // leaves the page and its song where they are. An error page that takes the document's place says no
    // "did-navigate"; the watch sees that the song's document is gone
    page.on('did-navigate', () => { if (cur && cur.armed && !leaving) { reportState('ended'); cur = null; } });
    say('ready', { store: '', premium: 'yes' });
  } else {
    // Apple's player refuses to queue anything while the page is open on another store (country) than the account's
    let account = await inPage(P.account);
    if (!account.active) throw new Fail('no-subscription', 'the account has no active Apple Music subscription');
    if (account.storefront && account.storefront !== account.current && /^[a-z]{2}$/.test(account.storefront)) {
      await open(`${site}${account.storefront}/browse`);
      await waitReady();
      account = await inPage(P.account);
    }
    await inPage(P.hook);
    page.on('did-navigate', () => { inPage(P.hook).catch(() => null); });
    say('ready', { store: /^[a-z]{2}$/.test(account.storefront) ? account.storefront : '' });
  }

  handle = command;
  while (held.length) command(held.shift());
  setInterval(tick, TICK);
}

// ---- the check that the sandbox is on ----
// Chromium keeps its pages in a sandbox, which a system can forbid it (Ubuntu does, for a program it has no
// profile for): then it does not start at all. This starts it, opens a blank page and looks at how its
// processes are kept.
const field = (text, key) => { const m = new RegExp('^' + key + ':\\s*(.*)$', 'm').exec(text); return m ? m[1].trim() : null; };
const link = (pid, what) => { try { return fs.readlinkSync(`/proc/${pid}/ns/${what}`); } catch (e) { return null; } };
async function selfTest() {
  say('start', { electron: process.versions.electron, chrome: process.versions.chrome });
  const win = new BrowserWindow({ show: false, skipTaskbar: true, focusable: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
  await win.loadURL('about:blank');
  await delay(800);
  const me = process.pid, list = [];
  for (const m of app.getAppMetrics()) {
    let status = '';
    try { status = fs.readFileSync(`/proc/${m.pid}/status`, 'utf8'); } catch (e) { /* gone */ }
    list.push({ type: m.type, seccomp: field(status, 'Seccomp'), own_pid_ns: link(m.pid, 'pid') !== link(me, 'pid'), own_user_ns: link(m.pid, 'user') !== link(me, 'user') });
  }
  const tab = list.find((p) => p.type === 'Tab');
  say('selftest', {
    ok: !!(tab && tab.seccomp === '2'),   // the page's process has its system-call filter
    seccomp: tab ? tab.seccomp : null, own_pid_ns: !!(tab && tab.own_pid_ns), own_user_ns: !!(tab && tab.own_user_ns),
    widevine_api: !!(components && typeof components.whenReady === 'function'),
  });
  finish(0);
}

app.whenReady().then(SELFTEST ? selfTest : run).catch((e) => fatal(e));
