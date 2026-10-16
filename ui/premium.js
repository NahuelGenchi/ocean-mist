// Premium Services: YouTube Music inside the player. The host (ytmusic.py)
// searches and opens albums, artists and playlists; their songs go into the
// YouTube Music list, a Now Playing list of its own beside My Music, and play
// like files. That list, like what is searched and opened here, is kept in
// memory only. Signed in through Firefox (firefox.py), the Library shows your
// playlists and Liked music, and songs can be liked and put in your playlists.
// Apple Music has a branch of the tree too: the host (apple.py) reads your
// library there, signed in through Firefox as well, and its songs join the
// same list. Apple's own audio is protected, so they play from YouTube Music,
// where the host finds each one when its turn comes.
(() => {
  const { $, state, api, on, onPlay, esc, clock, menu, base, sentence } = Mist;
  const SEARCHES = [['all', 'Top results'], ['songs', 'Songs'], ['albums', 'Albums'], ['artists', 'Artists'], ['playlists', 'Playlists']];
  const NOUNS = { all: ['result', 'results'], songs: ['song', 'songs'], albums: ['album', 'albums'], artists: ['artist', 'artists'], playlists: ['playlist', 'playlists'], recent: ['item', 'items'] };
  const APPLE = [['recent', 'Recently added'], ['playlists', 'Playlists'], ['albums', 'Albums'], ['artists', 'Artists'], ['songs', 'Songs']];
  const APPLE_PICTURES = /^https:\/\/([a-z0-9-]+\.)*(mzstatic\.com|blobstore\.apple\.com)\//i;   // the pictures the host fetches for Apple Music
  const yt = {
    words: '',                // what was searched for
    kind: 'songs',            // the search the box runs
    // what the table shows: the YouTube Music list ('now'), a search, a page
    // ('album:ID', or 'mine:ID' for one of your playlists), a saved playlist
    // ('list', its path), your library ('account') or Apple Music's ('apple', a part of it)
    node: { kind: 'search', value: 'songs' },
    results: {},              // 'kind\nwords' -> { items, more } or { error } or { loading }
    pages: {},                // 'album:ID' -> the page, or { error } or { loading }
    saved: {},                // playlist path -> { items } or { loading }
    recent: [],               // pages opened, the latest first: { key, title }
    back: [],                 // what the table showed before
    library: null,            // your playlists, signed in: { items } or { error, signed_out } or { loading }
    liked: {},                // video id -> whether you like it, as your playlists say and as you change it
    waiting: false,           // signing in: Firefox is open on the sign-in page
    choices: null,            // signing in: the Firefox profiles to choose from, [{ profile, name }]
    note: '',                 // why the last try to sign in did not
    open: { recent: true, lists: true, library: true, apple: true },   // the tree's branches
    found: null, rows: [], sel: -1, tree: [],
  };
  const ap = {                // Apple Music
    lists: {},                // 'songs'… -> { items, more, total, at, busy } or { error, signed_out } or { loading }
    profile: '',              // the Firefox profile it is read through
    out: '',                  // why Apple Music no longer accepts the sign-in, when it said so
    waiting: false,           // signing in, as for YouTube Music
    choices: null,
    note: '',
  };
  const same = (a, b) => a && b && a.kind === b.kind && a.value === b.value;
  const fetchJson = (path, who) => fetch(base + path).then((r) => r.json()).catch(() => ({ error: `the player could not ask ${who}` }));
  const get = (path) => fetchJson('yt/' + path, 'YouTube Music');
  const getApple = (path) => fetchJson('apple/' + path, 'Apple Music');
  // an album's, an artist's or a playlist's page, whichever service it is of
  const fetchPage = (kind, id) => (kind.startsWith('apple-')
    ? getApple(`page?kind=${kind.slice(6)}&id=${encodeURIComponent(id)}`) : get(`${kind}?id=${encodeURIComponent(id)}`));
  // a song of Apple Music has no video until the host finds it on YouTube Music, when its turn comes
  const playable = (item) => !!item && (item.kind === 'song' || item.kind === 'video') && !!(item.video || item.apple);
  const song = (item) => ({ video: item.video, apple: item.apple, title: item.title, artist: item.artist, album: item.album, year: item.year,
    duration: item.duration, cover: item.cover, artist_id: item.artist_id, album_id: item.album_id });
  const songKey = (item) => (item.apple ? 'apple:' + item.apple : item.video);   // the host's name for it, and its row's mark
  const pictureUrl = (url) => base + (APPLE_PICTURES.test(url) ? 'apple/' : 'yt/') + 'picture?u=' + encodeURIComponent(url);
  // a song of the YouTube Music list or of a saved playlist, as a row like a search result's
  const asItem = (t) => Object.assign(song(t), { kind: 'song', track: t.id });
  const savedName = (path) => (state.playlists.find((p) => p.path === path) || {}).name || '';
  const playing = () => { const t = Mist.track(state.cur); return t && songKey(t); };
  const shownPage = () => (yt.node.kind === 'page' && yt.found && !yt.found.loading && !yt.found.error ? yt.found : null);
  const signedIn = () => !!state.settings.yt_account;
  const appleReady = () => !!state.settings.apple_account && !ap.out;
  // your playlists are read signed in; Apple Music's pages have their own host route
  const keyOf = (item) => (item.service === 'apple' ? 'apple-' + item.kind : item.mine ? 'mine' : item.kind) + ':' + item.id;
  const learn = (tracks) => { for (const t of tracks || []) if (t.liked !== undefined) yt.liked[t.video] = t.liked; };

  // ---- getting results and pages ----
  function load(node) {
    if (node.kind === 'now') return;
    if (node.kind === 'list') { loadSaved(node.value); return; }
    if (node.kind === 'account') { loadLibrary(); return; }
    if (node.kind === 'apple') { if (node.value !== 'sign-in') loadApple(node.value); return; }
    if (node.kind === 'search') {
      const words = yt.words, key = node.value + '\n' + words;
      if (!words || (yt.results[key] && !yt.results[key].error)) return;
      yt.results[key] = { loading: true };
      get(`search?q=${encodeURIComponent(words)}&kind=${node.value}`).then((r) => {
        yt.results[key] = r.error ? { error: r.error } : { items: r.items || [], more: r.more };
        if (same(yt.node, node) && yt.words === words) draw();
      });
      return;
    }
    if (yt.pages[node.value] && !yt.pages[node.value].error) return;
    const [kind, id] = split(node.value);
    yt.pages[node.value] = { loading: true };
    fetchPage(kind, id).then((page) => {
      yt.pages[node.value] = page.error ? { error: page.error, signed_out: page.signed_out } : page;
      if (page.error) { if (page.signed_out) (kind.startsWith('apple-') ? appleOut : signedOut)(page); }
      else if (kind === 'mine') learn(page.tracks);   // the Library lists it already
      else remember(node.value, page.title);
      if (same(yt.node, node)) draw();
    });
  }
  function loadLibrary(again) {
    const lib = yt.library;
    if (!signedIn() || (lib && (lib.loading || (lib.items && !again)))) return;
    yt.library = { loading: true };
    get('library').then((r) => {
      yt.library = r.error ? { error: r.error, signed_out: r.signed_out } : { items: r.items || [], profile: r.profile || '' };
      drawTree();
      if (yt.node.kind === 'account') drawRows(true);
    });
  }
  // a part of your Apple Music library; `more` carries on from where the list ended
  function loadApple(kind, more) {
    if (!appleReady()) return;
    const list = ap.lists[kind];
    if (more) {
      if (!list || !list.items || list.more == null || list.busy) return;
      list.busy = true; drawRows(true);
      getApple(`library?kind=${kind}&offset=${list.more}`).then((r) => {
        list.busy = false;
        if (r.error) { Mist.flash(sentence(r.error), 6000); if (r.signed_out) appleOut(r); }
        else { list.items = list.items.concat(r.items || []); list.more = r.more; list.total = r.total ?? list.total; }
        if (ap.lists[kind] === list && same(yt.node, { kind: 'apple', value: kind })) drawRows(true);
      });
      return;
    }
    // read again when it is old: songs were added meanwhile
    if (list && (list.loading || (!list.error && Date.now() - list.at < 10 * 60 * 1000))) return;
    ap.lists[kind] = { loading: true };
    getApple(`library?kind=${kind}`).then((r) => {
      if (r.error) {
        ap.lists[kind] = { error: r.error, signed_out: r.signed_out };
        if (r.signed_out) appleOut(r);
      } else {
        ap.lists[kind] = { items: r.items || [], more: r.more, total: r.total, at: Date.now() };
        ap.profile = r.profile || ap.profile;
      }
      drawTree();
      if (same(yt.node, { kind: 'apple', value: kind })) drawRows(true);
    });
  }
  const split = (key) => { const at = key.indexOf(':'); return [key.slice(0, at), key.slice(at + 1)]; };
  function loadSaved(path) {   // read again each time, as the file may have changed
    if (!yt.saved[path]) yt.saved[path] = { loading: true };
    api('playlist-tracks', { path }).then((r) => {
      yt.saved[path] = { items: (r.tracks || []).filter(Mist.isStream).map(asItem) };
      if (same(yt.node, { kind: 'list', value: path })) drawRows(true);
    });
  }
  function remember(key, title) {
    yt.recent = [{ key, title: title || 'Untitled' }, ...yt.recent.filter((p) => p.key !== key)].slice(0, 12);
    drawTree();
  }
  // an album's or a playlist's songs, for a row that was not opened
  async function pageOf(item) {
    const key = keyOf(item), [kind, id] = split(key);
    let page = yt.pages[key];
    if (!page || page.error || page.loading) {
      page = await fetchPage(kind, id);
      if (page.error) { failed(page, kind.startsWith('apple-')); return null; }
      yt.pages[key] = page;
      if (kind === 'mine') learn(page.tracks); else remember(key, page.title);
    }
    return page;
  }

  // ---- what the table shows ----
  function show(node, back) {
    if (!back && !same(node, yt.node)) yt.back = [...yt.back, yt.node].slice(-30);
    if (node.kind === 'search') yt.kind = node.value;
    yt.node = node; yt.sel = -1;
    load(node);
    draw();
  }
  function search(words) {
    words = words.trim().replace(/\s+/g, ' ');
    if (!words) return;
    yt.words = words;
    show({ kind: 'search', value: yt.kind });
  }
  function openItem(item) { if (item && item.id) show({ kind: 'page', value: keyOf(item) }); }
  function goTo(kind, id) { Mist.show('premium'); show({ kind: 'page', value: kind + ':' + id }); }

  function compute() {
    const node = yt.node;
    yt.found = node.kind === 'search' ? yt.results[node.value + '\n' + yt.words]
      : node.kind === 'page' ? yt.pages[node.value]
      : node.kind === 'list' ? yt.saved[node.value]
      : node.kind === 'account' ? (signedIn() ? yt.library : null)
      : node.kind === 'apple' ? (appleReady() && node.value !== 'sign-in' ? ap.lists[node.value] : null)
      : { items: state.lists.youtube.map(asItem) };
    const rows = [], found = yt.found;
    if (found && !found.loading && !found.error) {
      if (node.kind !== 'page') for (const item of found.items) rows.push({ item });
      else {
        const groups = found.groups || [];
        if (groups.length && (found.tracks || []).length) rows.push({ group: 'Songs' });
        for (const item of found.tracks || []) rows.push({ item });
        for (const group of groups) { rows.push({ group: group.title }); for (const item of group.items) rows.push({ item }); }
      }
    }
    yt.rows = rows;
  }
  function cells(item) {
    if (playable(item)) return [item.title, item.artist, item.album, item.duration ? clock(item.duration) : ''];
    const what = { album: item.label || 'Album', artist: 'Artist', playlist: 'Playlist' }[item.kind];
    return [item.title, item.kind === 'artist' ? '' : item.artist, [what, item.year, item.info].filter(Boolean).join(' · '), ''];
  }
  const rowHtml = (row, i) => {
    if (row.group) return `<tr class="yt-group"><td colspan="4">${esc(row.group)}</td></tr>`;
    const item = row.item, c = cells(item), now = playable(item) && songKey(item) === playing();
    return `<tr class="${now ? 'cur ' : ''}${i === yt.sel ? 'sel' : ''}" data-i="${i}">` +
      `<td>${esc(c[0])}</td><td>${esc(c[1])}</td><td${playable(item) ? '' : ' class="dim"'}>${esc(c[2])}</td><td class="num">${c[3]}</td></tr>`;
  };
  function emptyText() {
    const found = yt.found, node = yt.node;
    if (node.kind === 'account' && (!signedIn() || (found && found.signed_out))) return signInText(found && found.error);
    if (node.kind === 'apple' && (!appleReady() || node.value === 'sign-in' || (found && found.signed_out))) return appleSignInText(found && found.error);
    if (node.kind === 'search' && !yt.words) return 'Type what you are looking for in the search box at the top right, then press Enter.';
    if (!found || found.loading) return node.kind === 'search' ? 'Searching YouTube Music…' : node.kind === 'apple' ? 'Reading your Apple Music library…' : 'Opening…';
    if (found.error) return esc(sentence(found.error));
    if (node.kind === 'apple') return node.value === 'recent' ? 'Nothing was added to your Apple Music library lately.' : `There are no ${NOUNS[node.value][1]} in your Apple Music library.`;
    if (node.kind === 'now') return 'Nothing is in the YouTube Music list. The songs you play or add here go into it.';
    if (node.kind === 'list') return 'This playlist has no songs that can be found.';
    if (node.kind === 'account') return 'There are no playlists in your library yet.';
    return node.kind === 'search' ? `YouTube Music found nothing for “${esc(yt.words)}”.` : 'There is nothing to play here.';
  }
  // the Library when you are not signed in: what signing in does, and its button
  function signInText(error) {
    if (yt.waiting) {
      return '<div class="yt-sign">Sign in to YouTube Music in the Firefox window that opened. Ocean Mist goes on as soon as you have.' +
        '<br><button id="yt-cancel">Cancel</button></div>';
    }
    if (yt.choices) {
      return '<div class="yt-sign">Firefox is signed in to YouTube in more than one profile. Which one should Ocean Mist use?<br>' +
        yt.choices.map((c, i) => `<button class="yt-profile" data-i="${i}">${esc(c.name)}</button>`).join(' ') + '</div>';
    }
    const why = yt.note || error;
    return '<div class="yt-sign">' + (why ? `${esc(sentence(why))}<br><br>` : '') +
      'Sign in to see your playlists and Liked music here, to like songs and to put them in your playlists.<br><br>' +
      'You sign in to YouTube Music in Firefox, on Google’s own page. Ocean Mist reads that sign-in from Firefox ' +
      'each time it asks for something of your account, and keeps nothing of it. Searches and the music itself stay anonymous.' +
      '<br><button id="yt-signin">Sign in with Firefox</button></div>';
  }
  // the Library when Apple Music is not signed in: what signing in does, and its button
  function appleSignInText(error) {
    if (ap.waiting) {
      return '<div class="yt-sign">Sign in to Apple Music in the Firefox window that opened. Ocean Mist goes on as soon as you have.' +
        '<br><button id="ap-cancel">Cancel</button></div>';
    }
    if (ap.choices) {
      return '<div class="yt-sign">Firefox is signed in to Apple Music in more than one profile. Which one should Ocean Mist use?<br>' +
        ap.choices.map((c, i) => `<button class="ap-profile" data-i="${i}">${esc(c.name)}</button>`).join(' ') + '</div>';
    }
    const why = ap.note || ap.out || error;
    return '<div class="yt-sign">' + (why ? `${esc(sentence(why))}<br><br>` : '') +
      'Sign in to see your Apple Music library here: what you added lately, your playlists, albums, artists and songs.<br><br>' +
      'You sign in at music.apple.com in Firefox, on Apple’s own page. Ocean Mist reads that sign-in from Firefox, uses it ' +
      'in memory only, and changes nothing in your account.<br><br>' +
      'Apple’s own audio is protected, so a song of yours plays from YouTube Music: Ocean Mist looks up the same title and artist ' +
      'there when its turn comes, and says so if it finds nothing.' +
      '<br><button id="ap-signin">Sign in with Firefox</button></div>';
  }
  // the table's three column titles
  function heads(node) {
    const kind = node.kind === 'search' || node.kind === 'apple' ? node.value : node.kind === 'account' ? 'playlists' : 'page';
    return [{ albums: 'Album', artists: 'Artist', playlists: 'Playlist' }[kind] || 'Title',
      node.kind === 'apple' && kind === 'playlists' ? '' : { artists: '', playlists: 'Made by' }[kind] ?? 'Artist',
      ['albums', 'artists', 'playlists', 'recent'].includes(kind) ? 'Details' : 'Album'];
  }
  function drawRows(keep) {
    compute();
    const node = yt.node;
    [$('yt-c1').textContent, $('yt-c2').textContent, $('yt-c3').textContent] = heads(node);
    $('yt-rows').innerHTML = yt.rows.length ? yt.rows.map(rowHtml).join('') : `<tr class="lib-empty"><td colspan="4">${emptyText()}</td></tr>`;
    if (!keep) $('yt-scroll').scrollTop = 0;
    // the header of an open album, artist or playlist
    const page = shownPage(), songs = yt.rows.filter((r) => playable(r.item)).map((r) => r.item);
    const total = songs.reduce((sum, t) => sum + (t.duration || 0), 0);
    const library = node.kind === 'account' && signedIn() && !(yt.found && yt.found.signed_out);
    const mine = node.kind === 'apple' && appleReady() && node.value !== 'sign-in' && !(yt.found && yt.found.signed_out);
    $('yt-head').hidden = !page && !library && !mine;
    if (mine) {
      const profile = ap.profile ? ` profile “${esc(ap.profile)}”` : '';
      $('yt-head').innerHTML = `<b>${esc(APPLE.find((a) => a[0] === node.value)[1])}</b>` +
        `<span>Apple Music, signed in through Firefox${profile} · <a id="ap-signout">Sign out</a></span>`;
    }
    if (library) {
      const profile = yt.library && yt.library.profile ? ` profile “${esc(yt.library.profile)}”` : '';
      $('yt-head').innerHTML = `<b>Library</b><span>Signed in to YouTube Music through Firefox${profile} · <a id="yt-signout">Sign out</a></span>`;
    }
    if (page) {
      const bits = [page.kind === 'artist' ? page.info : page.artist, page.kind === 'album' ? page.label : '', page.year,
        page.kind !== 'artist' && songs.length ? `${songs.length} ${songs.length === 1 ? 'song' : 'songs'}${total ? ', ' + clock(total) : ''}` : ''];
      $('yt-head').innerHTML = `<b>${esc(page.title)}</b><span>${esc(bits.filter(Boolean).join(' · '))}</span>`;
    }
    // the count, and more results for a search
    const found = yt.found, items = yt.rows.filter((r) => r.item);
    const listing = !['search', 'account', 'apple'].includes(node.kind);   // the songs of a page, a saved playlist or the list
    let text = '';
    if (found && found.loading) text = node.kind === 'search' ? 'Searching…' : 'Opening…';
    else if (items.length) {
      const noun = listing ? NOUNS.songs : NOUNS[node.kind === 'account' ? 'playlists' : node.value], n = listing ? songs.length : items.length;
      text = `${n} ${n === 1 ? noun[0] : noun[1]}` + (listing && total ? `, ${clock(total)}` : '');
      if (node.kind === 'apple' && found.more != null) text = `${n} of ${found.total || 'more'} ${noun[1]}`;
    }
    $('yt-count').textContent = text;
    $('yt-more').hidden = !((node.kind === 'search' || node.kind === 'apple') && found && found.more);
    $('yt-more').textContent = found && found.busy ? 'Getting more…' : node.kind === 'apple' ? 'Show more' : 'More results';
    $('yt-back').disabled = !yt.back.length;
    $('yt-queue').hidden = node.kind === 'now';   // already in it
    drawCover();
  }
  // the cover of the chosen row, or of the open page
  function drawCover() {
    const row = yt.rows[yt.sel], page = shownPage();
    const url = (row && row.item && row.item.cover) || (page && page.cover) || '';
    const img = $('yt-art'), src = url ? pictureUrl(url) : '';
    if (img.dataset.src !== src) { img.dataset.src = src; if (src) img.src = src; else img.removeAttribute('src'); }
    img.hidden = !src; $('yt-none').hidden = !!src;
  }
  $('yt-art').addEventListener('error', () => { $('yt-art').hidden = true; $('yt-none').hidden = false; });

  // ---- the tree on the left ----
  function drawTree() {
    const rows = [{ depth: 0, label: 'Now Playing', node: { kind: 'now' } }];
    for (const [value, label] of SEARCHES) rows.push({ depth: 0, label, node: { kind: 'search', value } });
    rows.push({ depth: 0, label: 'Recently opened', fold: 'recent' });
    if (yt.open.recent) {
      for (const page of yt.recent) rows.push({ depth: 1, label: page.title, node: { kind: 'page', value: page.key } });
      if (!yt.recent.length) rows.push({ depth: 1, label: '(nothing yet)', dim: true });
    }
    rows.push({ depth: 0, label: 'My Playlists', fold: 'lists' });
    if (yt.open.lists) {
      const saved = state.playlists.filter((p) => p.list === 'youtube');
      for (const p of saved) rows.push({ depth: 1, label: p.name, node: { kind: 'list', value: p.path } });
      if (!saved.length) rows.push({ depth: 1, label: '(none saved yet)', dim: true });
    }
    rows.push({ depth: 0, label: 'Library', fold: 'library', node: { kind: 'account' } });
    if (yt.open.library) {
      const lib = yt.library;
      if (!signedIn()) rows.push({ depth: 1, label: 'Sign in…', node: { kind: 'account', value: 'sign-in' } });
      else if (lib && lib.items) {
        for (const p of lib.items) rows.push({ depth: 1, label: p.title, node: { kind: 'page', value: keyOf(p) } });
        if (!lib.items.length) rows.push({ depth: 1, label: '(no playlists yet)', dim: true });
      } else rows.push({ depth: 1, label: lib && lib.error ? '(not signed in)' : '(opening…)', dim: true });
    }
    rows.push({ depth: 0, label: 'Apple Music', fold: 'apple' });
    if (yt.open.apple) {
      if (!appleReady()) rows.push({ depth: 1, label: 'Sign in…', node: { kind: 'apple', value: 'sign-in' } });
      else for (const [value, label] of APPLE) rows.push({ depth: 1, label, node: { kind: 'apple', value } });
    }
    yt.tree = rows;
    $('yt-tree').innerHTML = rows.map((r, i) =>
      `<div class="node${same(r.node, yt.node) ? ' sel' : ''}${r.dim ? ' dim' : ''}" data-i="${i}" style="padding-left:${3 + r.depth * 11}px">` +
      `<i class="${r.fold ? (yt.open[r.fold] ? 'minus' : 'plus') : ''}"></i><span>${esc(r.label)}</span></div>`).join('');
  }
  function draw() { drawTree(); drawRows(); }
  const treeRow = (e) => { const el = e.target.closest('.node'); return el && yt.tree[Number(el.dataset.i)]; };
  $('yt-tree').addEventListener('click', (e) => {
    const row = treeRow(e);
    if (!row) return;
    if (row.fold && (!row.node || e.target.tagName === 'I')) { yt.open[row.fold] = !yt.open[row.fold]; drawTree(); return; }
    if (row.node) show(row.node);
  });
  $('yt-tree').addEventListener('dblclick', (e) => {
    const row = treeRow(e);
    if (!row || !row.node || row.node.kind === 'now') return;
    if (row.node.kind !== 'list') { playRows(null); return; }
    api('play-playlist', { path: row.node.value, stay: true })
      .then((r) => { if (!r.ok) Mist.flash('That playlist has no songs that can be found'); });
  });

  // ---- playing ----
  // the songs the table shows become the YouTube Music list, from one of them on
  function playRows(item) {
    const songs = yt.rows.map((r) => r.item).filter(playable);
    if (!songs.length) return;
    const start = playable(item) ? item : songs[0];
    if (yt.node.kind === 'now') { Mist.play(start.track); return; }   // the list itself: play on from there
    const page = shownPage();
    const name = page ? page.title : yt.node.kind === 'list' ? savedName(yt.node.value)
      : yt.node.kind === 'apple' ? 'Apple Music: ' + APPLE.find((a) => a[0] === yt.node.value)[1] : '';
    api('yt-play', { items: songs.map(song), start: songKey(start), name });
  }
  async function playItem(item, add) {   // an album or a playlist, from its row
    const page = await pageOf(item);
    if (!page || !(page.tracks || []).length) return;
    if (add) api('yt-add', { items: page.tracks.map(song) });
    else api('yt-play', { items: page.tracks.map(song), start: songKey(page.tracks[0]), name: page.title });
  }
  function playNext(item) { api('yt-add', { items: [song(item)] }).then((r) => { if (r.ids && r.ids.length) Mist.queueNext(r.ids[0]); }); }
  function similar(item) {
    Mist.flash('Finding similar songs…', 10000);
    api('yt-play', { items: [song(item)], start: item.video, similar: true, name: 'Similar to ' + item.title })
      .then((r) => { if (r.notice) Mist.flash(sentence(r.notice), 6000); });
  }
  function add(item) {
    if (playable(item)) api('yt-add', { items: [song(item)] });
    else if (item && (item.kind === 'album' || item.kind === 'playlist')) playItem(item, true);
    else if (!item) {
      const songs = yt.rows.map((r) => r.item).filter(playable);
      if (songs.length) api('yt-add', { items: songs.map(song) }).then((r) => { if (r.limit) Mist.flash(`Only the first ${r.limit} songs were added`, 6000); });
    }
  }
  function choose(item) {   // a double-click, or Play in the tool bar
    if (!item || playable(item)) playRows(item);
    else if (item.kind === 'artist') openItem(item);
    else playItem(item);
  }

  // ---- the table ----
  const rowAt = (e) => { const tr = e.target.closest('tr[data-i]'); return tr ? Number(tr.dataset.i) : -1; };
  function select(i) {
    yt.sel = i;
    for (const tr of $('yt-rows').children) tr.classList.toggle('sel', Number(tr.dataset.i) === i);
    drawCover();
  }
  $('yt-rows').addEventListener('click', (e) => {
    if (e.target.id === 'yt-signin') { signIn(); return; }
    if (e.target.classList.contains('yt-profile')) { if (yt.choices) signIn(yt.choices[Number(e.target.dataset.i)].profile); return; }
    if (e.target.id === 'yt-cancel') { yt.waiting = false; drawRows(true); return; }
    if (e.target.id === 'ap-signin') { appleSignIn(); return; }
    if (e.target.classList.contains('ap-profile')) { if (ap.choices) appleSignIn(ap.choices[Number(e.target.dataset.i)].profile); return; }
    if (e.target.id === 'ap-cancel') { ap.waiting = false; drawRows(true); return; }
    const i = rowAt(e); if (i >= 0) select(i);
  });
  $('yt-head').addEventListener('click', (e) => {
    if (e.target.id === 'yt-signout') signOut();
    else if (e.target.id === 'ap-signout') appleSignOut();
  });
  $('yt-rows').addEventListener('dblclick', (e) => {
    const i = rowAt(e), item = i >= 0 && yt.rows[i].item;
    if (!item) return;
    if (playable(item)) playRows(item); else openItem(item);
  });
  $('yt-rows').addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const i = rowAt(e), item = i >= 0 && yt.rows[i].item;
    if (!item) return;
    select(i);
    if (yt.node.kind === 'now') { Mist.trackMenu(e, item.track); return; }   // the list's own menu, as in Now Playing
    if (playable(item) && item.apple) {   // nothing of Apple's is changed from here, and there is no radio for it
      menu(e.clientX, e.clientY, [
        { label: 'Play', run: () => playRows(item) },
        { label: 'Play next', run: () => playNext(item) },
        { label: 'Add to Now Playing', run: () => add(item) },
      ]);
    } else if (playable(item)) {
      menu(e.clientX, e.clientY, [
        { label: 'Play', run: () => playRows(item) },
        { label: 'Play next', run: () => playNext(item) },
        { label: 'Add to Now Playing', run: () => add(item) },
        { label: 'Play similar songs', run: () => similar(item) },
        null,
        { label: 'Go to album', disabled: !item.album_id, run: () => goTo('album', item.album_id) },
        { label: 'Go to artist', disabled: !item.artist_id, run: () => goTo('artist', item.artist_id) },
        ...accountItems(item, shownPage()),
      ]);
    } else if (item.kind === 'artist') {
      menu(e.clientX, e.clientY, [{ label: 'Open', run: () => openItem(item) }]);
    } else {
      menu(e.clientX, e.clientY, [
        { label: 'Open', run: () => openItem(item) },
        { label: 'Play', run: () => playItem(item) },
        { label: 'Add to Now Playing', run: () => playItem(item, true) },
      ]);
    }
  });
  $('yt-scroll').addEventListener('scroll', () => {   // Apple Music lists carry on by themselves near their end
    const el = $('yt-scroll');
    if (yt.node.kind === 'apple' && el.scrollHeight - el.scrollTop - el.clientHeight < 240) loadApple(yt.node.value, true);
  });
  $('yt-more').addEventListener('click', () => {
    const node = yt.node, found = yt.found;
    if (node.kind === 'apple') { loadApple(node.value, true); return; }
    if (node.kind !== 'search' || !found || !found.more || found.busy) return;
    found.busy = true; drawRows(true);
    get(`more?token=${encodeURIComponent(found.more)}&kind=${node.value}`).then((r) => {
      found.busy = false;
      if (r.error) Mist.flash(sentence(r.error), 6000);
      else { found.items = found.items.concat(r.items || []); found.more = r.more; }
      if (yt.found === found) drawRows(true);
    });
  });

  // ---- your account, signed in through Firefox ----
  // Waits for a sign-in in Firefox, for a service whose state is in `box` (waiting, choices, note) and whose
  // command is `command`; true once it is signed in. Each try has its number: a cancelled one stops, even when another starts.
  const tries = { yt: 0, apple: 0 };
  async function waitForSignIn(box, key, command, profile) {
    const turn = ++tries[key], current = () => box.waiting && tries[key] === turn;
    box.waiting = true; box.note = ''; box.choices = null; drawRows(true);
    let r = await api(command, { open: true, profile });   // Firefox opens on the sign-in page if it has none
    const until = Date.now() + 10 * 60 * 1000;
    while (r.ok && !r.signed_in && !r.choices && current() && Date.now() < until) {
      await new Promise((done) => setTimeout(done, 3000));
      if (current()) r = await api(command, {});
    }
    if (tries[key] !== turn) return false;   // a newer try carries on
    const waited = box.waiting;
    box.waiting = false;
    if (r.ok && r.signed_in) return true;
    if (r.ok && r.choices && waited) box.choices = r.choices;   // not after Cancel
    else if (waited) box.note = r.ok ? 'Ocean Mist stopped waiting for the sign-in; try again when you are ready' : r.error;
    return false;
  }
  // `profile`: the Firefox profile you chose, when more than one is signed in
  async function signIn(profile) {
    if (await waitForSignIn(yt, 'yt', 'yt-sign-in', profile)) {
      state.settings.yt_account = true;
      yt.library = null; yt.liked = {};
      loadLibrary();
      Mist.flash('Signed in to YouTube Music through Firefox');
    }
    draw();
  }
  async function appleSignIn(profile) {
    if (await waitForSignIn(ap, 'apple', 'apple-sign-in', profile)) {
      state.settings.apple_account = true;
      ap.lists = {}; ap.profile = ''; ap.out = '';
      Mist.flash('Signed in to Apple Music through Firefox');
      show({ kind: 'apple', value: 'recent' }, true);
      return;
    }
    draw();
  }
  function appleSignOut() {
    api('apple-sign-out').then(() => {
      state.settings.apple_account = false;
      ap.lists = {}; ap.profile = ''; ap.out = ''; ap.note = ''; ap.choices = null;
      const apples = (n) => n.kind === 'apple' || (n.kind === 'page' && n.value.startsWith('apple-'));
      for (const key of Object.keys(yt.pages)) if (key.startsWith('apple-')) delete yt.pages[key];
      yt.recent = yt.recent.filter((p) => !p.key.startsWith('apple-'));
      yt.back = yt.back.filter((n) => !apples(n));
      if (apples(yt.node)) yt.node = { kind: 'apple', value: 'sign-in' };
      draw();
      Mist.flash('Ocean Mist no longer uses your sign-in. Firefox is still signed in to Apple Music.', 6000);
    });
  }
  // Apple Music no longer accepts the sign-in: the branch asks for it again
  function appleOut(r) { ap.out = r.error || 'Apple Music says you are signed out; sign in again in Firefox'; ap.lists = {}; draw(); }
  function signOut() {
    api('yt-sign-out').then(() => {
      state.settings.yt_account = false;
      yt.library = null; yt.liked = {}; yt.note = ''; yt.choices = null;
      for (const key of Object.keys(yt.pages)) if (key.startsWith('mine:')) delete yt.pages[key];
      const yours = (n) => n.kind === 'page' && n.value.startsWith('mine:');
      yt.back = yt.back.filter((n) => !yours(n));
      if (yours(yt.node)) yt.node = { kind: 'account' };
      draw();
      Mist.flash('Ocean Mist no longer uses your sign-in. Firefox is still signed in to YouTube.', 6000);
    });
  }
  function signedOut(r) { yt.library = { error: r.error, signed_out: true }; drawTree(); }
  function failed(r, apple) {
    Mist.flash(sentence(r.error || (apple ? 'Apple Music did not answer' : 'YouTube Music did not answer')), 6000);
    if (r.signed_out) (apple ? appleOut : signedOut)(r);
  }
  // a page of yours that changed: read again the next time it is shown
  function stale(key) {
    delete yt.pages[key];
    if (same(yt.node, { kind: 'page', value: key })) { load(yt.node); drawRows(true); }
  }
  function rate(item, like) {
    api('yt-like', { video: item.video, like }).then((r) => {
      if (!r.ok) { failed(r); return; }
      yt.liked[item.video] = like;
      Mist.flash(like ? 'Added to Liked music' : 'Removed from Liked music');
      const liked = yt.pages['mine:VLLM'];
      if (like || !liked || !liked.tracks) stale('mine:VLLM');
      else {
        liked.tracks = liked.tracks.filter((t) => t.video !== item.video);
        if (shownPage() === liked) drawRows(true);
      }
    });
  }
  function addTo(playlist, item) {
    api('yt-playlist-add', { playlist: playlist.id, video: item.video }).then((r) => {
      if (!r.ok) { failed(r); return; }   // a song that is there already, for one: YouTube Music says so
      Mist.flash(`Added to “${playlist.title}”`, 5000);
      stale(keyOf(playlist));
    });
  }
  function takeOut(page, item) {
    api('yt-playlist-remove', { playlist: page.id, video: item.video, set: item.set }).then((r) => {
      if (!r.ok) { failed(r); return; }
      page.tracks = page.tracks.filter((t) => t.set !== item.set);
      Mist.flash(`Removed from “${page.title}”`);
      if (shownPage() === page) drawRows(true);
    });
  }
  function playlistChoices(item) {
    const lib = yt.library;
    if (!lib || !lib.items) {
      loadLibrary();
      return [{ label: lib && lib.error ? '(your playlists could not be read)' : '(reading your playlists…)', disabled: true }];
    }
    const own = lib.items.filter((p) => p.own);
    if (!own.length) return [{ label: '(no playlists of your own)', disabled: true }];
    return own.map((p) => ({ label: p.title, run: () => addTo(p, item) }));
  }
  // what a song's menu gets while you are signed in; `page` is the page it is on
  function accountItems(item, page) {
    if (!signedIn() || !playable(item)) return [];
    const items = [null,
      yt.liked[item.video] ? { label: 'Remove like', run: () => rate(item, false) } : { label: 'Like', run: () => rate(item, true) },
      { label: 'Add to playlist', items: playlistChoices(item) }];
    if (page && page.own && item.set) items.push({ label: 'Remove from playlist', run: () => takeOut(page, item) });
    return items;
  }

  // ---- tool bar ----
  const chosen = () => (yt.rows[yt.sel] || {}).item || null;
  $('yt-back').addEventListener('click', () => { if (yt.back.length) { const node = yt.back[yt.back.length - 1]; yt.back = yt.back.slice(0, -1); show(node, true); } });
  $('yt-play').addEventListener('click', () => choose(chosen()));
  onPlay('premium', () => { const item = chosen(); if (item) choose(item); return !!item; });
  $('yt-queue').addEventListener('click', () => add(chosen()));
  $('yt-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); search(e.target.value); } });

  // ---- YouTube Music songs elsewhere in the player ----
  // when the list runs out on one, similar songs carry on (a setting); they
  // follow the last one heard, which is another when this one could not play
  Mist.carryOn = (t) => {
    if (!Mist.isStream(t) || t.apple || state.settings.yt_radio === false) return false;   // Apple Music has no radio here
    const heard = Mist.track(state.played), seed = heard && !heard.apple ? heard : t;   // an Apple Music song has no video to start from
    Mist.flash('Finding similar songs…', 15000);
    api('yt-similar', { id: seed.id }).then((r) => {
      if (state.cur !== t.id || state.station !== null || state.mode !== 'playing') return;   // something else was chosen meanwhile
      if (r.ok && r.first != null) { Mist.refresh().then(() => Mist.play(r.first)); return; }
      Mist.stop();
      Mist.flash(r.error ? sentence(r.error) : 'YouTube Music has no more songs like these', 8000);
    });
    return true;
  };
  // the Now Playing list's menu gets what only they can do
  on('track-menu', (id, items) => {
    const t = Mist.track(id);
    if (!Mist.isStream(t) || t.apple) return;
    items.push(null,
      { label: 'Play similar songs', run: () => similar(t) },
      { label: 'Go to album', disabled: !t.album_id, run: () => goTo('album', t.album_id) },
      { label: 'Go to artist', disabled: !t.artist_id, run: () => goTo('artist', t.artist_id) },
      ...accountItems(asItem(t)));
  });
  on('view', (view) => {
    if (view !== 'premium') return;
    loadLibrary();
    draw();
    if (!yt.words) $('yt-search').focus();
  });
  on('state', () => {
    if (state.view !== 'premium') return;
    drawTree();
    if (yt.node.kind === 'list') loadSaved(yt.node.value);
  });
  on('render', () => {
    if (state.view !== 'premium') return;
    if (yt.node.kind === 'now') { drawRows(true); return; }
    const now = playing();
    for (const tr of $('yt-rows').children) {
      const row = yt.rows[Number(tr.dataset.i)], item = row && row.item;
      tr.classList.toggle('cur', playable(item) && songKey(item) === now);
    }
  });

  Mist.premium = yt;
  Mist.apple = ap;
})();
