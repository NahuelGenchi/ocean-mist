// The page's and the playback engine helper's rules, read from their files
// (which need a browser or Electron to run whole): node --test tests/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

// the source of one definition, found by the line it starts with
function definition(file, start) {
  const line = read(file).split('\n').find((l) => l.trim().startsWith(start));
  assert.ok(line, `${start} is in ${file}`);
  return line.trim();
}

test('esc() makes text safe inside an element and inside an attribute\'s quotes (#74)', () => {
  const esc = new Function(`${definition('ui/app.js', 'const ESCAPES =')}\n${definition('ui/app.js', 'function esc(')}\nreturn esc;`)();
  assert.equal(esc('a"b\'c<d>e&f'), 'a&quot;b&#39;c&lt;d&gt;e&amp;f');
  assert.equal(esc('x" onmouseover="alert(1)'), 'x&quot; onmouseover=&quot;alert(1)');
  assert.equal(esc(null), '');
  assert.equal(esc(undefined), '');
  assert.equal(esc(12), '12');
});

test('the page has no inline script and no event attribute, which its policy would refuse (#74)', () => {
  const html = read('ui/index.html');
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
  assert.ok(scripts.length > 0);
  for (const [, attrs, body] of scripts) {
    assert.match(attrs, /\bsrc="ui\/[a-z]+\.js"/);
    assert.equal(body.trim(), '');
  }
  assert.doesNotMatch(html, /\son[a-z]+\s*=/i);
  assert.doesNotMatch(html, /javascript:/i);
});

test('every script the page loads is there', () => {
  for (const [, src] of read('ui/index.html').matchAll(/<script\b[^>]*\bsrc="([^"]+)"/gi)) {
    assert.ok(fs.existsSync(path.join(ROOT, src)), src);
  }
});

// apple.py reads the library through the engine's page while the engine is up for Apple Music:
// only the library's own requests may be sent there
const rule = (name) => new Function(`${definition('engine/helper/main.js', `const ${name} =`)}\nreturn ${name};`)();
const LIBRARY_PATH = rule('LIBRARY_PATH');
const QUERY_KEY = rule('QUERY_KEY');

test('the helper sends only the library\'s own requests', () => {
  for (const ok of ['/v1/me/library/songs', '/v1/me/library/search', '/v1/me/library/recently-added',
    '/v1/me/library/playlists/p.AbC123', '/v1/me/library/albums/l.x-y_z/tracks', '/v1/me/library/artists/r.1/albums']) {
    assert.ok(LIBRARY_PATH.test(ok), ok);
  }
  for (const bad of ['/v1/me/account', '/v1/me/storefront', '/v1/catalog/us/songs', '/v1/me/library',
    '/v1/me/library/', '/v1/me/library/songs/', '/v1/me/library/../account', '/v1/me/library/songs?x=1',
    '/v1/me/library/playlists/../../account', '/v1/me/library/playlists/p.1/tracks/more',
    'https://api.music.apple.com/v1/me/library/songs', '/v1/me/library/SONGS', '/v1/me/library/songs\n']) {
    assert.ok(!LIBRARY_PATH.test(bad), JSON.stringify(bad));
  }
});

test('every library request of apple.py is one the helper sends', () => {
  const source = read('apple.py');
  const paths = [...source.matchAll(/"(\/v1\/me\/library\/[a-z-]+)"/g)].map((m) => m[1]);
  assert.ok(paths.length >= 6, 'the library lists and the search');
  for (const p of paths) assert.ok(LIBRARY_PATH.test(p), p);
  const pages = source.match(/^PAGES = (\{.*\})$/m);
  assert.ok(pages, 'PAGES is in apple.py');
  for (const kind of Object.values(JSON.parse(pages[1]))) {
    assert.ok(LIBRARY_PATH.test(`/v1/me/library/${kind}/p.Abc-1`), kind);
    assert.ok(LIBRARY_PATH.test(`/v1/me/library/${kind}/p.Abc-1/tracks`), kind);
  }
});

test('the helper passes only plain query keys', () => {
  for (const ok of ['term', 'types', 'limit', 'offset', 'include', 'fields[songs]', 'l']) assert.ok(QUERY_KEY.test(ok), ok);
  for (const bad of ['', 'Term', '1limit', 'a b', 'a&b', 'a=b', '__proto__', 'x'.repeat(42)]) {
    assert.ok(!QUERY_KEY.test(bad), JSON.stringify(bad));
  }
});
