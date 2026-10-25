// Premium Services: YouTube Music inside the player. The host (ytmusic.py)
// searches and opens albums, artists and playlists; their songs go into the
// YouTube Music list, a Now Playing list of its own beside My Music, and play
// like files. The streaming lists, like what is searched and opened here, are
// kept in memory only. Signed in through Firefox (firefox.py), the YouTube Music branch
// shows your playlists and Liked music, and songs can be liked and put in your
// playlists. Apple Music and Spotify have branches of the tree too: the hosts
// (apple.py, spotify.py) read your libraries there, signed in through Firefox
// as well, and their songs go into lists of their own, Apple Music's and
// Spotify's. Their audio is protected, so the playback engine (engine.py)
// plays it: its page here, under both branches, installs it and says what it
// needs. The search box searches the service being browsed: YouTube Music, or
// your Apple Music or Spotify library.
(() => {
  const { $, state, api, on, onPlay, esc, clock, menu, base, sentence } = Mist;
  const SEARCHES = [['all', 'Top results'], ['songs', 'Songs'], ['albums', 'Albums'], ['artists', 'Artists'], ['playlists', 'Playlists']];
  const NOUNS = { all: ['result', 'results'], songs: ['song', 'songs'], albums: ['album', 'albums'], artists: ['artist', 'artists'], playlists: ['playlist', 'playlists'], recent: ['item', 'items'] };
  const APPLE = [['recent', 'Recently added'], ['playlists', 'Playlists'], ['albums', 'Albums'], ['artists', 'Artists'], ['songs', 'Songs']];
  const APPLE_PICTURES = /^https:\/\/([a-z0-9-]+\.)*(mzstatic\.com|blobstore\.apple\.com)\//i;   // the pictures the host fetches for Apple Music
  const SPOTIFY = [['songs', 'Liked Songs'], ['playlists', 'Playlists'], ['albums', 'Albums'], ['artists', 'Artists']];
  const SPOTIFY_PICTURES = /^https:\/\/([a-z0-9-]+\.)*(scdn\.co|spotifycdn\.com|spotify\.com)\//i;   // ...and for Spotify
  const BOXES = { youtube: 'Search YouTube Music', apple: 'Search your Apple Music library', spotify: 'Search your Spotify library' };
  const yt = {
    words: '',                // what was searched for
    kind: 'songs',            // the search the box runs
    within: 'youtube',        // ...and the service it searches: 'youtube', 'apple' or 'spotify'
    // what the table shows: the streaming list in view ('now'), a search, a page
    // ('album:ID', or 'mine:ID' for one of your playlists), a saved playlist
    // ('list', its path), your library ('account'), Apple Music's or Spotify's ('apple',
    // 'spotify', a part of it) or a search of it ('apple-search', 'spotify-search', a kind)
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
    open: { recent: true, lists: true, library: true, apple: true, spotify: true },   // the tree's branches
    found: null, rows: [], sel: -1, tree: [],
  };
  const ap = {                // Apple Music
    lists: {},                // 'songs'… -> { items, more, total, at, busy } or { error, signed_out } or { loading }
    words: '',                // what was searched for in the library
    found: {},                // 'kind\nwords' -> { items, more, busy } or { error, signed_out } or { loading }
    profile: '',              // the Firefox profile it is read through
    out: '',                  // why Apple Music no longer accepts the sign-in, when it said so
    waiting: false,           // signing in, as for YouTube Music
    choices: null,
    note: '',
  };
  const sp = {                // Spotify, the same way (a search finds the whole list's matches at once:
    lists: {},                // { items, whole, read }, `whole` false when the list goes on past the `read` rows read)
    words: '',
    found: {},
    profile: '',
    out: '',
    waiting: false,
    choices: null,
    note: '',
  };
  const same = (a, b) => a && b && a.kind === b.kind && a.value === b.value;
  const fetchJson = (path, who) => fetch(base + path).then((r) => r.json()).catch(() => ({ error: `the player could not ask ${who}` }));
  const get = (path) => fetchJson('yt/' + path, 'YouTube Music');
  const getApple = (path) => fetchJson('apple/' + path, 'Apple Music');
  const getSpotify = (path) => fetchJson('spotify/' + path, 'Spotify');
  // an album's, an artist's or a playlist's page, whichever service it is of
  const fetchPage = (kind, id) => (kind.startsWith('apple-') ? getApple(`page?kind=${kind.slice(6)}&id=${encodeURIComponent(id)}`)
    : kind.startsWith('spotify-') ? getSpotify(`page?kind=${kind.slice(8)}&id=${encodeURIComponent(id)}`)
    : get(`${kind}?id=${encodeURIComponent(id)}`));
  // a song of Apple Music or Spotify has no video: the playback engine plays it by its own id
  const playable = (item) => !!item && (item.kind === 'song' || item.kind === 'video') && !!(item.video || item.apple || item.spotify);
  const song = (item) => ({ video: item.video, apple: item.apple, apple_lib: item.apple_lib, spotify: item.spotify, title: item.title, artist: item.artist, album: item.album, year: item.year,
    duration: item.duration, cover: item.cover, artist_id: item.artist_id, album_id: item.album_id });
  const songKey = (item) => (item.apple ? 'apple:' + item.apple : item.spotify ? 'spotify:' + item.spotify : item.video);   // the host's name for it, and its row's mark
  const pictureUrl = (url) => base + (APPLE_PICTURES.test(url) ? 'apple/' : SPOTIFY_PICTURES.test(url) ? 'spotify/' : 'yt/') + 'picture?u=' + encodeURIComponent(url);
  // a song of a streaming list or of a saved playlist, as a row like a search result's
  const asItem = (t) => Object.assign(song(t), { kind: 'song', track: t.id });
  const savedName = (path) => (state.playlists.find((p) => p.path === path) || {}).name || '';
  const playing = () => { const t = Mist.track(state.cur); return t && songKey(t); };
  const shownPage = () => (yt.node.kind === 'page' && yt.found && !yt.found.loading && !yt.found.error ? yt.found : null);
  const signedIn = () => !!state.settings.yt_account;
  const appleReady = () => !!state.settings.apple_account && !ap.out;
  const spotifyReady = () => !!state.settings.spotify_account && !sp.out;
  // your playlists are read signed in; Apple Music's and Spotify's pages have their own host routes
  const keyOf = (item) => (item.service === 'apple' ? 'apple-' + item.kind
    : item.service === 'spotify' ? 'spotify-' + item.kind
    : item.mine ? 'mine' : item.kind) + ':' + item.id;
  const learn = (tracks) => { for (const t of tracks || []) if (t.liked !== undefined) yt.liked[t.video] = t.liked; };
  // which branch asks for the sign-in again when a page of a service says it went away
  const outFor = (kind) => (kind.startsWith('apple-') ? appleOut : kind.startsWith('spotify-') ? spotifyOut : signedOut);

  // ---- getting results and pages ----
  function load(node) {
    if (node.kind === 'now') return;
    if (node.kind === 'list') { loadSaved(node.value); return; }
    if (node.kind === 'account') { loadLibrary(); return; }
    if (node.kind === 'apple') {
      if (node.value === 'engine') loadEngine();
      else if (node.value !== 'sign-in') loadApple(node.value);
      return;
    }
    if (node.kind === 'spotify') {
      if (node.value !== 'sign-in') loadSpotify(node.value);
      return;
    }
    if (node.kind === 'apple-search' || node.kind === 'spotify-search') { find(node); return; }
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
      if (page.error) { if (page.signed_out) outFor(kind)(page); }
      else if (kind === 'mine') learn(page.tracks);   // the YouTube Music branch lists it already
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
  // a page of a part of your Spotify library. Spotify's page may send the kinds of the library mixed together, the
  // host keeping those of the kind asked for, so a page can bring none of them: the next one is asked for then,
  // until one brings some or the host says there are no more (and after EMPTY_PAGES of them, Show more carries on)
  const EMPTY_PAGES = 10;
  async function spotifyPage(kind, offset) {
    let r = await getSpotify(`library?kind=${kind}` + (offset ? `&offset=${offset}` : ''));
    for (let empty = 1; !r.error && !(r.items || []).length && r.more != null && r.more > (offset || 0) && empty < EMPTY_PAGES; empty++) {
      offset = r.more;
      r = await getSpotify(`library?kind=${kind}&offset=${offset}`);
    }
    return r;
  }
  // a part of your Spotify library, the same way as Apple Music's
  function loadSpotify(kind, more) {
    if (!spotifyReady()) return;
    const list = sp.lists[kind];
    if (more) {
      if (!list || !list.items || list.more == null || list.busy) return;
      list.busy = true; drawRows(true);
      spotifyPage(kind, list.more).then((r) => {
        list.busy = false;
        if (r.error) { Mist.flash(sentence(r.error), 6000); if (r.signed_out) spotifyOut(r); }
        else { list.items = list.items.concat(r.items || []); list.more = r.more; list.total = r.total ?? list.total; }
        if (sp.lists[kind] === list && same(yt.node, { kind: 'spotify', value: kind })) drawRows(true);
      });
      return;
    }
    if (list && (list.loading || (!list.error && Date.now() - list.at < 10 * 60 * 1000))) return;
    sp.lists[kind] = { loading: true };
    spotifyPage(kind, 0).then((r) => {
      if (r.error) {
        sp.lists[kind] = { error: r.error, signed_out: r.signed_out };
        if (r.signed_out) spotifyOut(r);
      } else {
        sp.lists[kind] = { items: r.items || [], more: r.more, total: r.total, at: Date.now() };
        sp.profile = r.profile || sp.profile;
      }
      drawTree();
      if (same(yt.node, { kind: 'spotify', value: kind })) drawRows(true);
    });
  }
  // a search of your Apple Music or Spotify library; `more` carries on from where Apple's results ended (Spotify
  // sends all of them at once)
  function find(node, more) {
    const apple = node.kind === 'apple-search', of = apple ? ap : sp;
    if (!(apple ? appleReady() : spotifyReady())) return;
    const words = of.words, key = node.value + '\n' + words, found = of.found[key];
    const ask = (offset) => (apple ? getApple : getSpotify)(`search?kind=${node.value}&q=${encodeURIComponent(words)}` + (offset ? `&offset=${offset}` : ''));
    if (more) {
      if (!apple || !found || !found.items || found.more == null || found.busy) return;
      found.busy = true; drawRows(true);
      ask(found.more).then((r) => {
        found.busy = false;
        if (r.error) { Mist.flash(sentence(r.error), 6000); if (r.signed_out) appleOut(r); }
        else { found.items = found.items.concat(r.items || []); found.more = r.more; }
        if (of.found[key] === found && same(yt.node, node)) drawRows(true);
      });
      return;
    }
    if (!words || (found && !found.error)) return;
    of.found[key] = { loading: true };
    ask(0).then((r) => {
      of.found[key] = r.error ? { error: r.error, signed_out: r.signed_out } : { items: r.items || [], more: r.more, whole: r.whole !== false, read: r.read };
      if (r.signed_out) (apple ? appleOut : spotifyOut)(r);
      else if (same(yt.node, node) && of.words === words) drawRows(true);
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
  // an album's or a playlist's songs, for a row that was not opened (`quiet`: not one of the pages you opened, so it is
  // not remembered, and a failure is not announced)
  async function pageOf(item, quiet) {
    const key = keyOf(item), [kind, id] = split(key);
    let page = yt.pages[key];
    if (!page || page.error || page.loading) {
      page = await fetchPage(kind, id);
      if (page.error) { if (!quiet) failed(page, kind); else if (page.signed_out) outFor(kind)(page); return null; }
      yt.pages[key] = page;
      if (kind === 'mine') learn(page.tracks); else if (!quiet) remember(key, page.title);
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
  // the service of what the table shows, which the search box searches: none for Now Playing, a saved playlist
  // or the Playback engine, which keep the last one
  function serviceOf(node) {
    if (node.kind === 'apple') return node.value === 'engine' ? null : 'apple';
    if (node.kind === 'apple-search') return 'apple';
    if (node.kind === 'spotify' || node.kind === 'spotify-search') return 'spotify';
    if (node.kind === 'page') return node.value.startsWith('apple-') ? 'apple' : node.value.startsWith('spotify-') ? 'spotify' : 'youtube';
    return node.kind === 'search' || node.kind === 'account' ? 'youtube' : null;
  }
  const wordsOf = (within) => (within === 'apple' ? ap : within === 'spotify' ? sp : yt).words;
  // the search box says which service it searches, and holds what was last searched there
  function drawSearch() {
    const within = serviceOf(yt.node) || yt.within, box = $('yt-search');
    if (within === yt.within) return;
    yt.within = within;
    box.placeholder = BOXES[within];
    box.value = wordsOf(within);
  }
  function search(words) {
    words = words.trim().replace(/\s+/g, ' ');
    if (!words) return;
    const node = yt.node;
    if (yt.within === 'apple') {   // in the kind of the list in view; every kind from Recently added or a page
      ap.words = words;
      const kind = (node.kind === 'apple' || node.kind === 'apple-search') && NOUNS[node.value] && node.value !== 'recent' ? node.value : 'all';
      show({ kind: 'apple-search', value: kind });
      return;
    }
    if (yt.within === 'spotify') {   // ...and Liked Songs from a page
      sp.words = words;
      const kind = (node.kind === 'spotify' || node.kind === 'spotify-search') && SPOTIFY.some((s) => s[0] === node.value) ? node.value : 'songs';
      show({ kind: 'spotify-search', value: kind });
      return;
    }
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
      : node.kind === 'apple' ? (appleReady() && node.value !== 'sign-in' && node.value !== 'engine' ? ap.lists[node.value] : null)
      : node.kind === 'spotify' ? (spotifyReady() && node.value !== 'sign-in' ? sp.lists[node.value] : null)
      : node.kind === 'apple-search' ? (appleReady() ? ap.found[node.value + '\n' + ap.words] : null)
      : node.kind === 'spotify-search' ? (spotifyReady() ? sp.found[node.value + '\n' + sp.words] : null)
      : { items: state.lists[Mist.streamList()].map(asItem) };
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
    if (node.kind === 'apple' && node.value === 'engine') return engineText();
    if (node.kind === 'account' && (!signedIn() || (found && found.signed_out))) return signInText(found && found.error);
    if ((node.kind === 'apple' || node.kind === 'apple-search') && (!appleReady() || node.value === 'sign-in' || (found && found.signed_out))) return appleSignInText(found && found.error);
    if ((node.kind === 'spotify' || node.kind === 'spotify-search') && (!spotifyReady() || node.value === 'sign-in' || (found && found.signed_out))) return spotifySignInText(found && found.error);
    if (node.kind === 'search' && !yt.words) return 'Type what you are looking for in the search box at the top right, then press Enter.';
    if (!found || found.loading) return node.kind === 'search' ? 'Searching YouTube Music…'
      : node.kind === 'apple-search' ? 'Searching your Apple Music library…'
      : node.kind === 'spotify-search' ? 'Searching your Spotify library…'
      : node.kind === 'apple' ? 'Reading your Apple Music library…'
      : node.kind === 'spotify' ? 'Reading your Spotify library…' : 'Opening…';
    if (found.error) return esc(sentence(found.error));
    if (node.kind === 'apple-search') return `Nothing in your Apple Music library matches “${esc(ap.words)}”.`;
    if (node.kind === 'spotify-search') {
      const what = node.value === 'songs' ? 'Liked Songs' : NOUNS[node.value][1];
      return found.whole ? `Nothing in your Spotify ${what} matches “${esc(sp.words)}”.`
        : `Nothing in the first ${found.read} of your Spotify ${what} matches “${esc(sp.words)}”.`;
    }
    if (node.kind === 'apple') return node.value === 'recent' ? 'Nothing was added to your Apple Music library lately.' : `There are no ${NOUNS[node.value][1]} in your Apple Music library.`;
    if (node.kind === 'spotify') {
      const what = (SPOTIFY.find((s) => s[0] === node.value) || ['', NOUNS[node.value][1]])[1].toLowerCase();
      // the pages read so far held none of the kind, but the library goes on: Show more reads further
      if (found.more != null) return `No ${what} were found in the first part of your Spotify library. Show more looks further.`;
      return `There are no ${what} in your Spotify library.`;
    }
    if (node.kind === 'now') return `Nothing is in the ${Mist.listName(Mist.streamList())} list. The songs you play here go into their service's list, and the songs you add join the list that is playing.`;
    if (node.kind === 'list') return 'This playlist has no songs that can be found.';
    if (node.kind === 'account') return 'There are no playlists in your library yet.';
    return node.kind === 'search' ? `YouTube Music found nothing for “${esc(yt.words)}”.` : 'There is nothing to play here.';
  }
  // the YouTube Music branch when you are not signed in: what signing in does, and its button
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
  // the Apple Music branch when it is not signed in: what signing in does, and its button
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
      'Apple’s songs are protected, so they play through Ocean Mist’s playback engine: a download you accept first, under ' +
      'Playback engine. Playing them needs an active Apple Music subscription, which the engine checks.' +
      '<br><button id="ap-signin">Sign in with Firefox</button></div>';
  }
  // the Spotify branch when it is not signed in: what signing in does, and its button
  function spotifySignInText(error) {
    if (sp.waiting) {
      return '<div class="yt-sign">Sign in to Spotify in the Firefox window that opened. Ocean Mist goes on as soon as you have.' +
        '<br><button id="sp-cancel">Cancel</button></div>';
    }
    if (sp.choices) {
      return '<div class="yt-sign">Firefox is signed in to Spotify in more than one profile. Which one should Ocean Mist use?<br>' +
        sp.choices.map((c, i) => `<button class="sp-profile" data-i="${i}">${esc(c.name)}</button>`).join(' ') + '</div>';
    }
    const why = sp.note || sp.out || error;
    return '<div class="yt-sign">' + (why ? `${esc(sentence(why))}<br><br>` : '') +
      'Sign in to see your Spotify library here: your playlists, albums, artists and Liked Songs.<br><br>' +
      'You sign in at open.spotify.com in Firefox, on Spotify’s own page. Ocean Mist reads that sign-in from Firefox, uses it ' +
      'in memory only, and changes nothing in your account.<br><br>' +
      'Spotify’s songs are protected, so they play through Ocean Mist’s playback engine: a download you accept first, under ' +
      'Playback engine. Playing them needs an active Spotify Premium subscription, which the engine checks.' +
      '<br><button id="sp-signin">Sign in with Firefox</button></div>';
  }
  // the table's three column titles
  function heads(node) {
    const service = ['apple', 'spotify', 'apple-search', 'spotify-search'].includes(node.kind);
    const kind = node.kind === 'search' || service ? node.value : node.kind === 'account' ? 'playlists' : 'page';
    return [{ albums: 'Album', artists: 'Artist', playlists: 'Playlist' }[kind] || 'Title',
      service && kind === 'playlists' ? '' : { artists: '', playlists: 'Made by' }[kind] ?? 'Artist',
      ['albums', 'artists', 'playlists', 'recent'].includes(kind) ? 'Details' : 'Album'];
  }
  // the title of a search of a library: what it found, of which kind
  function searched(node, words) {
    if (node.value === 'all') return `Results for “${words}”`;
    const kind = node.kind === 'spotify-search' && node.value === 'songs' ? 'Liked Songs' : NOUNS[node.value][1];
    return `${kind.charAt(0).toUpperCase() + kind.slice(1)} matching “${words}”`;
  }
  function drawRows(keep) {
    compute();
    drawSearch();
    const node = yt.node;
    [$('yt-c1').textContent, $('yt-c2').textContent, $('yt-c3').textContent] = heads(node);
    $('yt-rows').innerHTML = yt.rows.length ? yt.rows.map(rowHtml).join('') : `<tr class="lib-empty"><td colspan="4">${emptyText()}</td></tr>`;
    if (!keep) $('yt-scroll').scrollTop = 0;
    // the header of an open album, artist or playlist
    const page = shownPage(), songs = yt.rows.filter((r) => playable(r.item)).map((r) => r.item);
    const total = songs.reduce((sum, t) => sum + (t.duration || 0), 0);
    const library = node.kind === 'account' && signedIn() && !(yt.found && yt.found.signed_out);
    const mine = (node.kind === 'apple' || node.kind === 'apple-search') && appleReady() && node.value !== 'sign-in' && node.value !== 'engine' && !(yt.found && yt.found.signed_out);
    const ours = (node.kind === 'spotify' || node.kind === 'spotify-search') && spotifyReady() && node.value !== 'sign-in' && !(yt.found && yt.found.signed_out);
    $('yt-head').hidden = !page && !library && !mine && !ours;
    if (mine) {
      const profile = ap.profile ? ` profile “${esc(ap.profile)}”` : '';
      $('yt-head').innerHTML = `<b>${esc(node.kind === 'apple-search' ? searched(node, ap.words) : APPLE.find((a) => a[0] === node.value)[1])}</b>` +
        `<span>Apple Music, signed in through Firefox${profile} · <a id="ap-signout">Sign out</a></span>`;
    }
    if (ours) {
      const profile = sp.profile ? ` profile “${esc(sp.profile)}”` : '';
      $('yt-head').innerHTML = `<b>${esc(node.kind === 'spotify-search' ? searched(node, sp.words) : SPOTIFY.find((a) => a[0] === node.value)[1])}</b>` +
        `<span>Spotify, signed in through Firefox${profile} · <a id="sp-signout">Sign out</a></span>`;
    }
    if (library) {
      const profile = yt.library && yt.library.profile ? ` profile “${esc(yt.library.profile)}”` : '';
      $('yt-head').innerHTML = `<b>YouTube Music</b><span>Signed in to YouTube Music through Firefox${profile} · <a id="yt-signout">Sign out</a></span>`;
    }
    if (page) {
      const bits = [page.kind === 'artist' ? page.info : page.artist, page.kind === 'album' ? page.label : '', page.year,
        page.kind !== 'artist' && songs.length ? `${songs.length} ${songs.length === 1 ? 'song' : 'songs'}${total ? ', ' + clock(total) : ''}` : ''];
      $('yt-head').innerHTML = `<b>${esc(page.title)}</b><span>${esc(bits.filter(Boolean).join(' · '))}</span>`;
    }
    // the count, and more results for a search
    const found = yt.found, items = yt.rows.filter((r) => r.item);
    const listing = !['search', 'account', 'apple', 'spotify', 'apple-search', 'spotify-search'].includes(node.kind);   // the songs of a page, a saved playlist or the list
    let text = '';
    if (found && found.loading) text = ['search', 'apple-search', 'spotify-search'].includes(node.kind) ? 'Searching…' : 'Opening…';
    else if (items.length) {
      const noun = listing ? NOUNS.songs : NOUNS[node.kind === 'account' ? 'playlists' : node.value], n = listing ? songs.length : items.length;
      text = `${n} ${n === 1 ? noun[0] : noun[1]}` + (listing && total ? `, ${clock(total)}` : '');
      if (node.kind === 'apple' && found.more != null) text = `${n} of ${found.total || 'more'} ${noun[1]}`;
      if (node.kind === 'spotify' && found.more != null && found.total > n) text = `${n} of ${found.total} ${noun[1]}`;   // (without a total, the count alone)
      if (node.kind === 'spotify-search' && !found.whole) text += ` among the first ${found.read} of your ${node.value === 'songs' ? 'Liked Songs' : noun[1]}`;
    }
    $('yt-count').textContent = text;
    const mayMore = ['search', 'apple', 'spotify', 'apple-search'].includes(node.kind);
    $('yt-more').hidden = !(mayMore && found && found.more);
    $('yt-more').textContent = found && found.busy ? 'Getting more…' : node.kind === 'search' || node.kind === 'apple-search' ? 'More results' : 'Show more';
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
      const saved = state.playlists.filter((p) => p.list !== 'local');   // the streaming services' saved playlists
      for (const p of saved) rows.push({ depth: 1, label: p.name, node: { kind: 'list', value: p.path } });
      if (!saved.length) rows.push({ depth: 1, label: '(none saved yet)', dim: true });
    }
    rows.push({ depth: 0, label: 'YouTube Music', fold: 'library', node: { kind: 'account' } });   // your YouTube Music library
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
      rows.push({ depth: 1, label: 'Playback engine', node: { kind: 'apple', value: 'engine' } });
    }
    rows.push({ depth: 0, label: 'Spotify', fold: 'spotify' });
    if (yt.open.spotify) {
      if (!spotifyReady()) rows.push({ depth: 1, label: 'Sign in…', node: { kind: 'spotify', value: 'sign-in' } });
      else for (const [value, label] of SPOTIFY) rows.push({ depth: 1, label, node: { kind: 'spotify', value } });
      rows.push({ depth: 1, label: 'Playback engine', node: { kind: 'apple', value: 'engine' } });
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
    if (row.node.kind !== 'list') { choose(null); return; }
    api('play-playlist', { path: row.node.value, stay: true })
      .then((r) => { if (!r.ok) Mist.flash('That playlist has no songs that can be found'); });
  });

  // ---- playing ----
  // the songs the table shows become their service's list, from one of them on
  function playRows(item) {
    const songs = yt.rows.map((r) => r.item).filter(playable);
    if (!songs.length) return;
    queueTurn++;
    const start = playable(item) ? item : songs[0];
    if (yt.node.kind === 'now') { Mist.play(start.track); return; }   // the list itself: play on from there
    const page = shownPage();
    const name = page ? page.title : yt.node.kind === 'list' ? savedName(yt.node.value)
      : yt.node.kind === 'apple' ? 'Apple Music: ' + APPLE.find((a) => a[0] === yt.node.value)[1]
      : yt.node.kind === 'spotify' ? 'Spotify: ' + SPOTIFY.find((a) => a[0] === yt.node.value)[1]
      : yt.node.kind === 'apple-search' ? `Apple Music: “${ap.words}”`
      : yt.node.kind === 'spotify-search' ? `Spotify: “${sp.words}”` : '';
    api('yt-play', { items: songs.map(song), start: songKey(start), name });
  }
  // Recently added is a feed of albums and playlists, and Play on a row of it makes the whole feed the list, as Play
  // on a row of songs does: the chosen one plays now, and the songs of the others follow, in the table's order
  const CARRIES = new Set(['recent']);
  const QUEUE_LIMIT = 1000;   // the most songs the host keeps in the list
  const READ_AT_ONCE = 4;     // rows of the feed read at the same time
  let queueTurn = 0;          // each play of a list has its number; the one that fills the list behind it stops when it is not the latest
  const holder = (item) => !!item && !playable(item) && (item.kind === 'album' || item.kind === 'playlist');
  const entry = (item) => holder(item) || playable(item);
  const carries = () => yt.node.kind === 'apple' && CARRIES.has(yt.node.value);
  // the songs of one row of the feed: the song itself, or those of the album or playlist (null: they could not be read)
  async function songsAt(item, quiet) {
    if (playable(item)) return [item];
    const page = await pageOf(item, quiet);
    return page ? (page.tracks || []) : null;
  }
  async function playFrom(item) {
    const mine = ++queueTurn;
    const rows = yt.rows.map((r) => r.item).filter(entry);
    const at = Math.max(0, rows.includes(item) ? rows.indexOf(item) : rows.findIndex((r) => keyOf(r) === keyOf(item)));
    const name = 'Apple Music: ' + APPLE.find((a) => a[0] === yt.node.value)[1];
    const first = await songsAt(rows[at]);
    if (queueTurn !== mine || !first || !first.length) return;
    // the rows above it come first in the list: read a few at a time, so that the song starts soon
    const before = [];
    for (let i = 0; i < at && queueTurn === mine; i += READ_AT_ONCE) {
      const got = await Promise.all(rows.slice(i, Math.min(at, i + READ_AT_ONCE)).map((r) => songsAt(r, true)));
      for (const songs of got) before.push(...(songs || []));
    }
    if (queueTurn !== mine) return;
    const head = [...before, ...first];
    const played = await api('yt-play', { items: head.map(song), start: songKey(first[0]), name });
    if (!played.ok) return;
    // something else was chosen meanwhile: another Play here, or the Apple Music list has another name. The page
    // learns the name the host just gave the list a moment after its answer, so until it has seen that name, the one
    // it shows is the list's name from before and says nothing (the host refuses the additions anyway)
    let seen = false;
    const replaced = () => {
      seen = seen || state.names.apple === name;
      return queueTurn !== mine || (seen && state.names.apple !== name);
    };
    let count = Math.min(head.length, QUEUE_LIMIT), failures = 0;
    for (let i = at + 1; i < rows.length; i += READ_AT_ONCE) {   // the rest follow behind it, as they are read
      if (replaced() || count >= QUEUE_LIMIT || failures >= 3) return;
      const got = await Promise.all(rows.slice(i, i + READ_AT_ONCE).map((r) => songsAt(r, true)));
      if (replaced()) return;
      const songs = [];
      for (const some of got) {
        if (some) { failures = 0; songs.push(...some); } else failures++;   // (three in a row that fail: the rest is left)
      }
      if (!songs.length) continue;
      const added = await api('yt-add', { items: songs.slice(0, QUEUE_LIMIT - count).map(song), named: name });   // (the host adds only while the list is still this one)
      if (!added.ok || !(added.ids || []).length) return;
      count += Math.min(songs.length, QUEUE_LIMIT - count);
    }
  }
  // songs added from here join the list that is playing when that is a streaming one, whatever their service; while
  // a song of My Music is on (it keeps no streaming songs), they go into their service's list and into the queue, so
  // that they play after it. `next`: the one song plays next (in a streaming list, it goes right after the current
  // one too). Answers what the host said
  function addSongs(items, next) {
    const into = Mist.playingList(), streaming = into && into !== 'local';
    return api('yt-add', { items, into: streaming ? into : undefined, after: streaming && next ? state.cur : undefined }).then((r) => {
      const ids = r.ids || [];
      if (next) { if (ids.length) Mist.queueNext(ids[0]); }
      else if (into === 'local') for (const id of ids) Mist.queueLast(id);
      return r;
    });
  }
  async function playItem(item, add) {   // an album or a playlist, from its row
    if (!add) queueTurn++;
    const page = await pageOf(item);
    if (!page || !(page.tracks || []).length) return;
    if (add) addSongs(page.tracks.map(song));
    else api('yt-play', { items: page.tracks.map(song), start: songKey(page.tracks[0]), name: page.title });
  }
  function playNext(item) { addSongs([song(item)], true); }
  function similar(item) {
    queueTurn++;
    Mist.flash('Finding similar songs…', 10000);
    api('yt-play', { items: [song(item)], start: item.video, similar: true, name: 'Similar to ' + item.title })
      .then((r) => { if (r.notice) Mist.flash(sentence(r.notice), 6000); });
  }
  function add(item) {
    if (playable(item)) addSongs([song(item)]);
    else if (item && (item.kind === 'album' || item.kind === 'playlist')) playItem(item, true);
    else if (!item) {
      const songs = yt.rows.map((r) => r.item).filter(playable);
      if (songs.length) addSongs(songs.map(song)).then((r) => { if (r.limit) Mist.flash(`Only the first ${r.limit} songs were added`, 6000); });
    }
  }
  function choose(item) {   // Play in the tool bar, or in a row's menu
    if (carries() && (!item || entry(item))) {
      const first = item || yt.rows.map((r) => r.item).find(entry);
      if (first) playFrom(first);
      return;
    }
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
    if (e.target.id === 'sp-signin') { spotifySignIn(); return; }
    if (e.target.classList.contains('sp-profile')) { if (sp.choices) spotifySignIn(sp.choices[Number(e.target.dataset.i)].profile); return; }
    if (e.target.id === 'sp-cancel') { sp.waiting = false; drawRows(true); return; }
    if (e.target.closest('.en')) { engineClick(e.target); return; }
    const i = rowAt(e); if (i >= 0) select(i);
  });
  $('yt-rows').addEventListener('change', (e) => {   // the licence box of the engine's page
    if (e.target.id !== 'en-ok') return;
    eng.ticked = e.target.checked;
    $('en-install').disabled = !eng.ticked;
  });
  $('yt-head').addEventListener('click', (e) => {
    if (e.target.id === 'yt-signout') signOut();
    else if (e.target.id === 'ap-signout') appleSignOut();
    else if (e.target.id === 'sp-signout') spotifySignOut();
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
    if (playable(item) && (item.apple || item.spotify)) {   // nothing of the service's is changed from here, and there is no radio for it
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
        { label: 'Play', run: () => choose(item) },
        { label: 'Add to Now Playing', run: () => playItem(item, true) },
      ]);
    }
  });
  $('yt-scroll').addEventListener('scroll', () => {   // Apple Music's and Spotify's lists carry on by themselves near their end
    const el = $('yt-scroll'), near = el.scrollHeight - el.scrollTop - el.clientHeight < 240;
    if (!near) return;
    if (yt.node.kind === 'apple' && yt.node.value !== 'engine') loadApple(yt.node.value, true);
    else if (yt.node.kind === 'spotify') loadSpotify(yt.node.value, true);
    else if (yt.node.kind === 'apple-search') find(yt.node, true);
  });
  $('yt-more').addEventListener('click', () => {
    const node = yt.node, found = yt.found;
    if (node.kind === 'apple') { loadApple(node.value, true); return; }
    if (node.kind === 'spotify') { loadSpotify(node.value, true); return; }
    if (node.kind === 'apple-search') { find(node, true); return; }
    if (node.kind !== 'search' || !found || !found.more || found.busy) return;
    found.busy = true; drawRows(true);
    get(`more?token=${encodeURIComponent(found.more)}&kind=${node.value}`).then((r) => {
      found.busy = false;
      if (r.error) Mist.flash(sentence(r.error), 6000);
      else { found.items = found.items.concat(r.items || []); found.more = r.more; }
      if (yt.found === found) drawRows(true);
    });
  });

  // ---- the playback engine ----
  // Apple Music's and Spotify's songs are played by a download (Electron, with Google's Widevine) that the
  // player runs without a window, only while one plays. This page is where it is accepted, installed,
  // checked and removed; `eng.s` is what the host last said about it (engine-status, and the "engine-setup"
  // events).
  const eng = { s: null, ticked: false, loading: false, checking: false, copied: '', sure: 0 };
  const megabytes = (bytes) => Math.round(bytes / 1048576);
  function loadEngine() {
    if (eng.loading) return;
    eng.loading = true;
    api('engine-status').then((r) => { eng.loading = false; if (r.ok) engineSeen(r); });
  }
  function engineSeen(status) {
    const was = eng.s && eng.s.installing, now = status.installing;
    eng.s = status;
    eng.checking = false;
    if (!same(yt.node, { kind: 'apple', value: 'engine' })) return;
    const bar = $('en-bar');
    if (bar && was && now && was.step === now.step && now.total) {   // only the bar moves while it downloads
      bar.style.width = Math.min(100, Math.round(100 * now.done / now.total)) + '%';
      $('en-note').textContent = installNote(now);
      return;
    }
    drawRows(true);
  }
  function installNote(now) {
    if (now.step === 'downloading') return `Downloading… ${megabytes(now.done)} of ${megabytes(now.total)} MB`;
    if (now.step === 'unpacking') return 'Unpacking…';
    return 'Checking that it can start its sandbox…';
  }
  function engineText() {
    const s = eng.s;
    if (!s) { loadEngine(); return 'Looking at the playback engine…'; }
    const warn = s.error ? `<p class="warn">${esc(sentence(s.error))}</p>` : '';
    // the profile the one-time step adds is the administrator's to take away, so Remove leaves it: how to take it away stays on the page while it is there
    const ask = !!s.ask;   // the system's password window can be used (else the step is a command for a terminal)
    const undoCommand = `<code>${esc(s.undo)}</code><button id="en-copy-undo">${eng.copied === 'undo' ? 'Copied' : 'Copy the command'}</button>`;
    const profile = s.profile
      ? '<p>This system has the profile that lets the engine’s file start its sandbox. It names only that file and stays when the engine is removed ' +
        '(so installing it again needs no step). ' + (ask
          ? 'To take it away, your system asks for your password again:</p><button id="en-disallow">Take the permission away</button>' +
            (s.ask_failed ? '<p>Or run this in a terminal:</p>' + undoCommand : '')
          : 'To take it away:</p>' + undoCommand)
      : '';
    if (s.asking) {   // the system's password window is open
      return '<div class="yt-sign en"><b>Waiting for your password</b>' +
        '<p>Your system is asking for your password in its own window; Ocean Mist never sees it. ' +
        'If you do not see the window, it may be behind this one.</p>' +
        '<button id="en-ask-cancel">Cancel</button></div>';
    }
    if (s.installing) {
      const now = s.installing, pct = now.step === 'downloading' && now.total ? Math.min(100, Math.round(100 * now.done / now.total)) : 100;
      return '<div class="yt-sign en"><b>Installing the playback engine</b>' +
        `<div class="bar"><i id="en-bar" style="width:${pct}%"></i></div><p id="en-note">${esc(installNote(now))}</p>` +
        `<button id="en-cancel"${now.step === 'downloading' ? '' : ' disabled'}>Cancel</button></div>`;
    }
    if (!s.installed) {
      return '<div class="yt-sign en"><b>Playback engine</b>' + warn +
        '<p>Apple Music’s and Spotify’s songs are protected, so this page cannot play them. Ocean Mist plays them with an engine of its own: a copy of ' +
        'Chromium that runs in the background, with no window, only while a song of theirs plays. It opens the service’s web player and ' +
        'nothing else, signed in with the sign-in Ocean Mist reads from Firefox, which it keeps in memory only.</p>' +
        `<p><u>What is downloaded:</u> ${megabytes(s.size)} MB from ${esc(s.source)} (castlabs’s build of Electron ${esc(s.version.split('+')[0])}), ` +
        `checked against the checksum Ocean Mist was made with (SHA-256 ${esc(s.sha256.slice(0, 12))}…) before it is used. The first time the engine ` +
        `starts, it also gets Google’s Widevine module (about ${megabytes(s.extra)} MB) from Google, the way Chrome does.</p>` +
        `<p><u>Where it goes:</u> ${esc(s.folder)}, about ${megabytes(s.unpacked + s.extra)} MB. The download touches nothing else on your computer, and Remove deletes the engine and everything it kept.</p>` +
        (s.restricted && !s.covered ? '<p><u>One more step on this system:</u> it only lets programs that AppArmor has a profile for use the security sandbox the engine runs in (Ubuntu 24.04 and later do this), ' +
          (ask ? 'so once the engine is downloaded, your system asks for your password, once, in its own window, to add one profile for its file. Ocean Mist never sees the password.</p>'
            : 'so once the engine is downloaded an administrator has to add one profile for its file. The next page shows the command, and the one that takes the profile away again; Ocean Mist never runs either.</p>') : '') +
        '<p><u>Licences:</u> Electron is MIT licensed and Chromium has its own open-source licences (both come in the download); ' +
        'Widevine is Google’s module and comes under Google’s own terms.</p>' +
        `<label><input type="checkbox" id="en-ok"${eng.ticked ? ' checked' : ''}> I accept these licences and want the engine downloaded.</label>` +
        `<button id="en-install"${eng.ticked ? '' : ' disabled'}>Download and install</button>${profile}</div>`;
    }
    if (s.sandbox === 'ok') {
      return '<div class="yt-sign en"><b>Playback engine</b>' + warn +
        `<p>Installed (Electron ${esc(s.version.split('+')[0])}) and ready: its security sandbox is on. Songs of Apple Music and Spotify play through it. ` +
        'It starts when you play one, ends a minute after the music stops (ten if you paused), and never shows a window.</p>' +
        `<button id="en-check"${eng.checking ? ' disabled' : ''}>${eng.checking ? 'Checking…' : 'Check again'}</button>` +
        `<button id="en-remove">${eng.sure > Date.now() ? 'Click again to remove it' : 'Remove the engine'}</button>${profile}</div>`;
    }
    const checking = eng.checking ? ' disabled' : '', first = s.sandbox === 'unknown';
    const again = `<button id="en-check"${checking}>${eng.checking ? 'Checking…' : first ? 'Check it now' : 'Check again'}</button>`;
    if (s.sandbox === 'blocked' && s.restricted && ask) {
      const byHand = s.ask_failed
        ? '<p>If that does not work, run this in a terminal, then press Check again:</p>' +
          `<code>${esc(s.command)}</code><button id="en-copy">${eng.copied === 'command' ? 'Copied' : 'Copy the command'}</button>${again}`
        : '';
      return '<div class="yt-sign en"><b>One step is needed, once</b>' + (s.ask_failed ? warn : '') +
        '<p>Your system does not let a program start the security sandbox the engine runs in unless the system itself allows it ' +
        '(Ubuntu 24.04 and later do this for Chrome and Firefox too). Press Allow it: your system asks for your password, once, ' +
        'in its own window. Ocean Mist never sees the password.</p>' +
        '<button id="en-allow">Allow it</button>' +
        '<p>The engine keeps the service’s page in Chromium’s security sandbox, and it never runs without it. The step adds one AppArmor profile ' +
        'that names only the engine’s file and gives it one permission (“userns”). It is the script engine/allow-sandbox.sh in Ocean Mist’s ' +
        'folder; read it first if you like. This page can take the profile away again.</p>' + byHand + '</div>';
    }
    if (s.sandbox === 'blocked' && s.restricted) {
      return '<div class="yt-sign en"><b>One step is needed, once</b>' +
        '<p>The engine keeps the service’s page in Chromium’s security sandbox, and it never runs without it. Your system only lets programs ' +
        'that AppArmor has a profile for create that sandbox (Ubuntu 24.04 and later do this for Chrome and Firefox too). ' +
        'Ocean Mist cannot add the profile itself: it needs an administrator. Run this in a terminal, then press Check again:</p>' +
        `<code>${esc(s.command)}</code><button id="en-copy">${eng.copied === 'command' ? 'Copied' : 'Copy the command'}</button>${again}` +
        '<p>It adds one AppArmor profile that names only the engine’s file and gives it one permission (“userns”); read the script ' +
        'first if you like. To take the profile away again:</p>' +
        `<code>${esc(s.undo)}</code><button id="en-copy-undo">${eng.copied === 'undo' ? 'Copied' : 'Copy the command'}</button></div>`;
    }
    return '<div class="yt-sign en"><b>Playback engine</b>' + warn +
      `<p>${s.sandbox === 'blocked' ? 'The engine cannot start its security sandbox on this system, and it never runs without it.'
        : 'The engine is installed but has not been tried yet.'}</p>${again}` +
      `<button id="en-remove">${eng.sure > Date.now() ? 'Click again to remove it' : 'Remove the engine'}</button></div>`;
  }
  function engineClick(el) {
    const id = el.id;
    if (id === 'en-install' && eng.s && eng.ticked) {
      eng.s.error = '';
      api('engine-install', { accept: true, version: eng.s.version }).then((r) => {
        if (!r.ok) { Mist.flash(sentence(r.error || 'the engine could not be installed'), 8000); return; }
        loadEngine();
      });
    } else if (id === 'en-cancel') api('engine-cancel');
    else if (id === 'en-check') {
      eng.checking = true; drawRows(true); api('engine-check');
      setTimeout(() => { if (eng.checking) { eng.checking = false; loadEngine(); } }, 70000);   // the check gives up after a minute
    }
    else if (id === 'en-remove') {
      if (eng.sure > Date.now()) { eng.sure = 0; api('engine-remove').then(() => { eng.ticked = false; loadEngine(); }); return; }
      eng.sure = Date.now() + 4000;   // the first click only asks
      drawRows(true);
      setTimeout(() => { if (eng.sure && eng.sure <= Date.now()) { eng.sure = 0; if (same(yt.node, { kind: 'apple', value: 'engine' })) drawRows(true); } }, 4100);
    } else if (id === 'en-allow' || id === 'en-disallow') {   // the system asks for the password in its own window
      api(id === 'en-allow' ? 'engine-allow' : 'engine-disallow').then((r) => {
        if (!r.ok) Mist.flash(sentence(r.error || 'the system could not be asked'), 8000);
        loadEngine();
      });
    } else if (id === 'en-ask-cancel') {
      api('engine-ask-cancel').then(() => loadEngine());
    } else if (id === 'en-copy' || id === 'en-copy-undo') {
      const which = id === 'en-copy' ? 'command' : 'undo';
      api('engine-copy', { which }).then((r) => {   // the host puts it on the clipboard
        Mist.flash(r.ok ? 'Copied: paste it in a terminal' : 'Ocean Mist could not copy it; select the text and copy it', 5000);
        if (r.ok) { eng.copied = which; drawRows(true); setTimeout(() => { eng.copied = ''; if (same(yt.node, { kind: 'apple', value: 'engine' })) drawRows(true); }, 3000); }
      });
    }
  }
  on('event', (msg) => { if (msg.event === 'engine-setup') engineSeen(msg); });
  // a song needs the engine and it is not there: the page that installs it opens
  on('engine-needed', () => { Mist.show('premium'); show({ kind: 'apple', value: 'engine' }); });

  // ---- your account, signed in through Firefox ----
  // Waits for a sign-in in Firefox, for a service whose state is in `box` (waiting, choices, note) and whose
  // command is `command`; true once it is signed in. Each try has its number: a cancelled one stops, even when another starts.
  const tries = { yt: 0, apple: 0, spotify: 0 };
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
  // the engine ends with the sign-in it was given: a song of that service that is on stops with it (one of the
  // other service's plays on, as the engine keeps their sign-ins apart)
  function endSongOf(service) {
    const t = Mist.track(state.cur);
    if (t && (service === 'spotify' ? t.spotify : t.apple) && state.station === null) Mist.stop();
  }
  async function appleSignIn(profile) {
    if (await waitForSignIn(ap, 'apple', 'apple-sign-in', profile)) {
      endSongOf('apple');
      state.settings.apple_account = true;
      ap.lists = {}; ap.found = {}; ap.profile = ''; ap.out = '';
      Mist.flash('Signed in to Apple Music through Firefox');
      show({ kind: 'apple', value: 'recent' }, true);
      return;
    }
    draw();
  }
  async function spotifySignIn(profile) {
    if (await waitForSignIn(sp, 'spotify', 'spotify-sign-in', profile)) {
      endSongOf('spotify');
      state.settings.spotify_account = true;
      sp.lists = {}; sp.found = {}; sp.profile = ''; sp.out = '';
      Mist.flash('Signed in to Spotify through Firefox');
      show({ kind: 'spotify', value: 'songs' }, true);
      return;
    }
    draw();
  }
  function appleSignOut() {
    api('apple-sign-out').then(() => {
      endSongOf('apple');
      state.settings.apple_account = false;
      ap.lists = {}; ap.found = {}; ap.profile = ''; ap.out = ''; ap.note = ''; ap.choices = null;
      const apples = (n) => n.kind === 'apple' || n.kind === 'apple-search' || (n.kind === 'page' && n.value.startsWith('apple-'));
      for (const key of Object.keys(yt.pages)) if (key.startsWith('apple-')) delete yt.pages[key];
      yt.recent = yt.recent.filter((p) => !p.key.startsWith('apple-'));
      yt.back = yt.back.filter((n) => !apples(n));
      if (apples(yt.node)) yt.node = { kind: 'apple', value: 'sign-in' };
      draw();
      Mist.flash('Ocean Mist no longer uses your sign-in. Firefox is still signed in to Apple Music.', 6000);
    });
  }
  function spotifySignOut() {
    api('spotify-sign-out').then(() => {
      endSongOf('spotify');
      state.settings.spotify_account = false;
      sp.lists = {}; sp.found = {}; sp.profile = ''; sp.out = ''; sp.note = ''; sp.choices = null;
      const theirs = (n) => n.kind === 'spotify' || n.kind === 'spotify-search' || (n.kind === 'page' && n.value.startsWith('spotify-'));
      for (const key of Object.keys(yt.pages)) if (key.startsWith('spotify-')) delete yt.pages[key];
      yt.recent = yt.recent.filter((p) => !p.key.startsWith('spotify-'));
      yt.back = yt.back.filter((n) => !theirs(n));
      if (theirs(yt.node)) yt.node = { kind: 'spotify', value: 'sign-in' };
      draw();
      Mist.flash('Ocean Mist no longer uses your sign-in. Firefox is still signed in to Spotify.', 6000);
    });
  }
  // Apple Music no longer accepts the sign-in: the branch asks for it again
  function appleOut(r) { ap.out = r.error || 'Apple Music says you are signed out; sign in again in Firefox'; ap.lists = {}; ap.found = {}; draw(); }
  function spotifyOut(r) { sp.out = r.error || 'Spotify says you are signed out; sign in again in Firefox'; sp.lists = {}; sp.found = {}; draw(); }
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
  // `kind`: what was asked for, which says whose sign-in it was (YouTube Music's when there is none)
  function failed(r, kind = '') {
    const who = kind.startsWith('apple-') ? 'Apple Music' : kind.startsWith('spotify-') ? 'Spotify' : 'YouTube Music';
    Mist.flash(sentence(r.error || `${who} did not answer`), 6000);
    if (r.signed_out) outFor(kind)(r);
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
    if (!Mist.isStream(t) || t.apple || t.spotify || state.settings.yt_radio === false) return false;   // Apple Music and Spotify have no radio here
    const heard = Mist.track(state.played), seed = heard && !heard.apple && !heard.spotify ? heard : t;   // a service's song has no video to start from
    Mist.flash('Finding similar songs…', 15000);
    api('yt-similar', { id: seed.id, into: Mist.whereIs(t.id) }).then((r) => {
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
    if (!Mist.isStream(t) || t.apple || t.spotify) return;
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
    if (!wordsOf(yt.within)) $('yt-search').focus();
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
  Mist.spotify = sp;
})();
