'use strict';
/*
 * Ocean Mist's playback helper: Apple Music's own web player in a window that is never shown, so that a
 * song of Apple Music plays as Apple's audio. engine.py starts it with the Chromium-based runtime that has
 * Widevine (the content-protection module Apple's audio needs), signed in with the one cookie Ocean Mist
 * read from Firefox, and tells it what to do on its standard input, a JSON object a line. It answers on its
 * standard output, in lines that start with "OMP ". It says codes and numbers only: no title, name, address
 * or sign-in. Everything it writes to disk goes to the folder given as --data (Chromium's own files and
 * Google's Widevine module); nothing of Apple's does, as the page's session lives in memory.
 *
 *   electron <this folder> --data=<folder>              the helper
 *   electron <this folder> --data=<folder> --selftest   starts, checks that Chromium's sandbox is on, ends
 *
 * in   {"cmd":"start","cookie":{"value","host","expires","secure","http_only","same_site"},"volume":0.5,"muted":false}
 *        always the first line: the sign-in is never in an argument or an environment variable
 *      {"cmd":"play","serial":7,"ids":["i.AbC","1234"],"from":0}    the first id that Apple accepts plays
 *      {"cmd":"pause"} {"cmd":"resume"} {"cmd":"seek","to":12.5} {"cmd":"stop"} {"cmd":"quit"}
 *      {"cmd":"volume","value":0.3,"muted":false}
 * out  start, widevine, ready, state, problem, denied, fail, selftest
 *
 * What may need updating when Apple changes its page is kept together, in "what the page is asked".
 */
const { app, BrowserWindow, session, components } = require('electron');
const fs = require('fs');

const T0 = Date.now();
const arg = (name) => {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : '';
};
const DATA = arg('data');
const SELFTEST = process.argv.includes('--selftest');
const APPLE_SITE = 'https://music.apple.com/';
// a stand-in for Apple's page, for checks: an address on a ".localhost" name, which never leaves the machine
const SITE = /^http:\/\/[a-z0-9.-]+\.localhost:\d{2,5}\/$/.test(arg('site')) ? arg('site') : APPLE_SITE;
const SITE_HOST = new URL(SITE).hostname;
const APPLE = /(^|\.)apple\.com$/i;
const SIGN_OUT = /log.?out|sign.?out|log.?off|sign.?off|unauthori[sz]e/i;   // the same as apple.py blocks: a sign-out here would end Firefox's sign-in too
const PERMITTED = new Set(['protected-media-identifier']);   // the only permission Apple's player may be given
const READY_WITHIN = 45000;       // Apple's page gets this long to open and to say it is signed in
const SIGNED_WITHIN = 10000;      // ...and this long, once its player is up, to say it is signed in
const WIDEVINE_WITHIN = 240000;   // Google's module can have to come over the network on the first run
const LOADING_WITHIN = 40000;     // a song that has not started by then did not
const TICK = 500;                 // how often the page is asked how the song is doing
const BEAT = 1000;                // how often the position is said while a song plays

const say = (event, data) => process.stdout.write(
  'OMP ' + JSON.stringify({ event, t: Math.round((Date.now() - T0) / 100) / 10, ...(data || {}) }) + '\n');
let secret = '';   // the cookie's value, so that nothing said can carry it out
const mask = (text) => {
  let out = String(text || '');
  if (secret) out = out.split(secret).join('<token>');
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
// These run inside Apple's page (they are sent as text), so each stands alone. MusicKit is Apple's own player
// object there; the requests it makes are Apple's, not a published way in, so they can change at any time.

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
  const om = window.__om = { ended: 0, error: '', serial: 0 };
  document.addEventListener('ended', () => { om.ended += 1; }, true);   // media events do not bubble, but they are captured
  try {
    MusicKit.getInstance().addEventListener('mediaPlaybackError', (e) => {
      const why = e && ((e.error && (e.error.errorCode || e.error.name)) || e.errorCode || e.name || e.message);
      om.error = String(why || 'UNKNOWN').slice(0, 40);
    });
  } catch (e) { /* the page still tells its end and its state */ }
  return 'hooked';
}

// plays one song: the first of `ids` that Apple takes (the library's id, then the catalogue's); a newer
// song asked for meanwhile (`serial`) ends this try
async function pagePlay(ids, volume, serial) {
  const om = window.__om, mk = MusicKit.getInstance();
  om.serial = serial; om.error = '';
  const code = (e) => String((e && (e.errorCode || e.name || e.message)) || e).replace(/\s+/g, ' ').slice(0, 40);
  let last = 'NO_SONG';
  for (const id of ids) {
    try {
      await mk.setQueue({ song: String(id) });
      if (om.serial !== serial) return JSON.stringify({ ok: false, code: 'SUPERSEDED' });
      mk.volume = volume;
      await mk.play();
      return JSON.stringify({ ok: om.serial === serial, code: om.serial === serial ? '' : 'SUPERSEDED' });
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

async function pagePause() { await MusicKit.getInstance().pause(); return 'ok'; }
async function pageResume() { await MusicKit.getInstance().play(); return 'ok'; }
async function pageSeek(to) { await MusicKit.getInstance().seekToTime(to); return 'ok'; }
function pageVolume(value) { MusicKit.getInstance().volume = value; return 'ok'; }
async function pageStop() { try { const mk = MusicKit.getInstance(); await mk.pause(); await mk.stop(); } catch (e) { /* not playing */ } return 'ok'; }

// ---- the page ----
let page = null;                    // the window's contents
const inPage = async (fn, ...args) => {
  const said = await page.executeJavaScript(`(${fn})(...${JSON.stringify(args)})`, true);   // as a user's click: no autoplay block
  return typeof said === 'string' && said[0] === '{' ? JSON.parse(said) : said;
};

// wait for Apple's player to be up and to say that it is signed in
async function waitReady() {
  let up = 0;
  const until = Date.now() + READY_WITHIN;
  while (Date.now() < until) {
    const said = await inPage(pageReady).catch(() => ({}));
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
let cur = null;                     // { serial, phase, armed, seekTo, endedBase, said, stalled, stopped }
let last = { time: 0, duration: 0 };
let volume = 0.5, muted = false;
let unsigned = 0;                   // polls in a row that said the sign-in is gone
let watching = false;

const reportState = (state, extra) => {
  cur.phase = state; cur.said = Date.now();
  say('state', { serial: cur.serial, state, position: round(last.time), duration: round(last.duration), ...extra });
};

// what Apple's error says, as a code engine.py words
function problemOf(code) {
  const c = String(code || '').toUpperCase();
  if (/AUTHORI|TOKEN|ACCESS_DENIED/.test(c)) return 'signed-out';
  if (/SUBSCRIPTION/.test(c)) return 'no-subscription';
  if (/UNAVAILABLE|RESTRICT|GEO|NOT_FOUND|UNSUPPORTED|EQUIVALENT|NO_SONG/.test(c)) return 'unavailable';
  if (/DEVICE_LIMIT|QUOTA/.test(c)) return 'limit';
  if (/NETWORK|REQUEST|SERVER|TIMEOUT/.test(c)) return 'offline';
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
  const song = cur = { serial, phase: 'loading', armed: false, seekTo: from, endedBase: 0, said: 0, stalled: 0, stopped: 0 };
  say('state', { serial, state: 'loading', position: 0, duration: 0 });
  if (!ids.length) { problem(serial, 'NO_SONG'); return; }
  const before = await inPage(pageWatch).catch(() => null);
  if (before) song.endedBase = before.ended;   // the ends of earlier songs do not count
  let result;
  try { result = await inPage(pagePlay, ids, volume, serial); } catch (e) { result = { ok: false, code: 'PAGE_ERROR' }; }
  if (cur !== song || result.code === 'SUPERSEDED') return;   // another song was asked for meanwhile
  if (!result.ok) { problem(serial, result.code); return; }
  song.armed = true;
}

async function tick() {
  if (watching || !cur || !cur.armed) return;
  watching = true;
  try {
    const w = await inPage(pageWatch);
    const song = cur;
    if (!song || !song.armed) return;
    unsigned = w.signed ? 0 : unsigned + 1;
    if (unsigned >= 3) throw new Fail('signed-out', 'Apple says the sign-in is gone');
    if (w.state === 'playing' || w.state === 'paused') last = { time: w.time, duration: w.duration || last.duration };
    if (w.error) { problem(song.serial, w.error); return; }
    const over = w.ended > song.endedBase || w.state === 'ended' || w.state === 'completed';
    const finished = song.phase === 'playing' && w.state !== 'playing' && w.duration > 5 && w.time >= w.duration - 0.6;
    if (over || finished) {
      reportState('ended');
      cur = null;
      return;
    }
    song.stopped = w.state === 'stopped' ? song.stopped + 1 : 0;
    if (w.state === 'playing' || w.state === 'paused') song.stalled = 0;
    if (w.state === 'playing') {
      if (song.seekTo) {   // where a song restored from earlier goes on from
        const to = song.seekTo; song.seekTo = 0;
        await inPage(pageSeek, to).catch(() => null);
        last.time = to;
      }
      if (song.phase !== 'playing' || Date.now() - song.said >= BEAT) reportState('playing');
    } else if (w.state === 'paused') {
      if (song.phase !== 'paused') reportState('paused');
    } else {
      song.stalled = song.stalled || Date.now();
      if (song.stopped >= 3) { problem(song.serial, 'STOPPED'); return; }   // Apple's player stopped by itself
      if (song.phase === 'playing') reportState('loading');   // it ran dry: Apple's player is fetching more
      else if (Date.now() - song.stalled > LOADING_WITHIN) problem(song.serial, 'TIMEOUT');
    }
  } catch (e) {
    if (e instanceof Fail) fatal(e);
  } finally { watching = false; }
}

// ---- commands ----
async function command(msg) {
  try {
    if (msg.cmd === 'play') await play(msg);
    else if (msg.cmd === 'pause') await inPage(pagePause);
    else if (msg.cmd === 'resume') await inPage(pageResume);
    else if (msg.cmd === 'seek') {
      const to = Math.max(0, Number(msg.to) || 0);
      if (cur && cur.phase === 'loading') cur.seekTo = to;   // when it has started
      else { await inPage(pageSeek, to); last.time = to; if (cur) cur.said = 0; }
    } else if (msg.cmd === 'volume') {
      volume = Math.max(0, Math.min(1, Number(msg.value) || 0)); muted = !!msg.muted;
      page.setAudioMuted(muted);
      await inPage(pageVolume, volume);
    } else if (msg.cmd === 'stop') {
      cur = null;
      await inPage(pageStop);
    }
  } catch (e) { /* a command that cannot be done leaves the song as it was; the next look at it says how it is */ }
}

let leaving = false;
async function quit() {
  if (leaving) return;
  leaving = true;
  try { if (page) await Promise.race([inPage(pageStop), delay(1500)]); } catch (e) { /* going anyway */ }
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
  const cookie = first && first.cmd === 'start' && first.cookie;
  if (!cookie || !cookie.value) throw new Fail('no-cookie', 'no sign-in arrived on stdin');
  secret = String(cookie.value);
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

  const ses = session.fromPartition('ocean-mist-apple');   // no "persist:": in memory only
  ses.setUserAgent(`Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${process.versions.chrome} Safari/537.36`);
  ses.webRequest.onBeforeRequest((details, callback) => {
    callback(SIGN_OUT.test(details.url) ? { cancel: true } : {});
  });
  const denied = new Set();
  ses.setPermissionRequestHandler((_contents, permission, callback) => {
    if (!PERMITTED.has(permission) && !denied.has(permission)) { denied.add(permission); say('denied', { permission: mask(permission) }); }
    callback(PERMITTED.has(permission));
  });
  ses.setPermissionCheckHandler((_contents, permission) => PERMITTED.has(permission));
  await ses.cookies.set({
    url: SITE, name: 'media-user-token', value: secret, domain: cookie.host, path: '/',
    secure: !!cookie.secure, httpOnly: !!cookie.http_only, sameSite: cookie.same_site || 'no_restriction',
    ...(cookie.expires ? { expirationDate: Number(cookie.expires) } : {}),
  });
  cookie.value = null;

  const win = new BrowserWindow({
    show: false, width: 1000, height: 700, skipTaskbar: true, focusable: false,
    webPreferences: {
      session: ses, backgroundThrottling: false, contextIsolation: true, nodeIntegration: false,
      sandbox: true, images: false, spellcheck: false, autoplayPolicy: 'no-user-gesture-required',
    },
  });
  page = win.webContents;
  page.setAudioMuted(muted);
  page.setWindowOpenHandler(() => ({ action: 'deny' }));
  page.on('will-navigate', (event, url) => {
    let host = '';
    try { host = new URL(url).hostname; } catch (_) { /* not an address */ }
    if (!APPLE.test(host) && host !== SITE_HOST) event.preventDefault();
  });
  page.on('render-process-gone', () => fatal(new Fail('crash', 'the page\'s process ended')));

  await open(SITE);
  await waitReady();

  // Apple's player refuses to queue anything while the page is open on another store (country) than the account's
  let account = await inPage(pageAccount);
  if (!account.active) throw new Fail('no-subscription', 'the account has no active Apple Music subscription');
  if (account.storefront && account.storefront !== account.current && /^[a-z]{2}$/.test(account.storefront)) {
    await open(`${SITE}${account.storefront}/browse`);
    await waitReady();
    account = await inPage(pageAccount);
  }
  await inPage(pageHook);
  page.on('did-navigate', () => { inPage(pageHook).catch(() => null); });
  say('ready', { store: /^[a-z]{2}$/.test(account.storefront) ? account.storefront : '' });

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
