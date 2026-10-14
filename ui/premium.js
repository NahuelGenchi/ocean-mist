// Premium Services: YouTube Music inside the player. The host (ytmusic.py)
// searches and opens albums, artists and playlists; their songs go into the
// Now Playing list beside the files and play like them.
(() => {
  const { $, state, api, on, esc, clock, menu, base, sentence } = Mist;
  const SEARCHES = [['all', 'Top results'], ['songs', 'Songs'], ['albums', 'Albums'], ['artists', 'Artists'], ['playlists', 'Playlists']];
  const NOUNS = { all: ['result', 'results'], songs: ['song', 'songs'], albums: ['album', 'albums'], artists: ['artist', 'artists'], playlists: ['playlist', 'playlists'] };
  const yt = {
    words: '',                // what was searched for
    kind: 'songs',            // the search the box runs
    node: { kind: 'search', value: 'songs' },   // what the table shows: a search, or a page ('album:ID')
    results: {},              // 'kind\nwords' -> { items, more } or { error } or { loading }
    pages: {},                // 'album:ID' -> the page, or { error } or { loading }
    recent: [],               // pages opened, the latest first: { key, title }
    back: [],                 // what the table showed before
    open: true,               // the "Recently opened" branch
    found: null, rows: [], sel: -1, tree: [],
  };
  const same = (a, b) => a && b && a.kind === b.kind && a.value === b.value;
  const get = (path) => fetch(base + 'yt/' + path).then((r) => r.json()).catch(() => ({ error: 'the player could not ask YouTube Music' }));
  const playable = (item) => !!item && (item.kind === 'song' || item.kind === 'video') && !!item.video;
  const song = (item) => ({ video: item.video, title: item.title, artist: item.artist, album: item.album, year: item.year,
    duration: item.duration, cover: item.cover, artist_id: item.artist_id, album_id: item.album_id });
  const playing = () => { const t = Mist.track(state.cur); return t && t.video; };
  const shownPage = () => (yt.node.kind === 'page' && yt.found && !yt.found.loading && !yt.found.error ? yt.found : null);

  // ---- getting results and pages ----
  function load(node) {
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
    get(`${kind}?id=${encodeURIComponent(id)}`).then((page) => {
      yt.pages[node.value] = page.error ? { error: page.error } : page;
      if (!page.error) remember(node.value, page.title);
      if (same(yt.node, node)) draw();
    });
  }
  const split = (key) => { const at = key.indexOf(':'); return [key.slice(0, at), key.slice(at + 1)]; };
  function remember(key, title) {
    yt.recent = [{ key, title: title || 'Untitled' }, ...yt.recent.filter((p) => p.key !== key)].slice(0, 12);
    drawTree();
  }
  // an album's or a playlist's songs, for a row that was not opened
  async function pageOf(item) {
    const key = item.kind + ':' + item.id;
    let page = yt.pages[key];
    if (!page || page.error || page.loading) {
      page = await get(`${item.kind}?id=${encodeURIComponent(item.id)}`);
      if (page.error) { Mist.flash(sentence(page.error), 6000); return null; }
      yt.pages[key] = page; remember(key, page.title);
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
  function openItem(item) { if (item && item.id) show({ kind: 'page', value: item.kind + ':' + item.id }); }
  function goTo(kind, id) { Mist.show('premium'); show({ kind: 'page', value: kind + ':' + id }); }

  function compute() {
    const node = yt.node;
    yt.found = node.kind === 'search' ? yt.results[node.value + '\n' + yt.words] : yt.pages[node.value];
    const rows = [], found = yt.found;
    if (found && !found.loading && !found.error) {
      if (node.kind === 'search') for (const item of found.items) rows.push({ item });
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
    const item = row.item, c = cells(item), now = playable(item) && item.video === playing();
    return `<tr class="${now ? 'cur ' : ''}${i === yt.sel ? 'sel' : ''}" data-i="${i}">` +
      `<td>${esc(c[0])}</td><td>${esc(c[1])}</td><td${playable(item) ? '' : ' class="dim"'}>${esc(c[2])}</td><td class="num">${c[3]}</td></tr>`;
  };
  function emptyText() {
    const found = yt.found, node = yt.node;
    if (node.kind === 'search' && !yt.words) return 'Type what you are looking for in the search box at the top right, then press Enter.';
    if (!found || found.loading) return node.kind === 'search' ? 'Searching YouTube Music…' : 'Opening…';
    if (found.error) return esc(sentence(found.error));
    return node.kind === 'search' ? `YouTube Music found nothing for “${esc(yt.words)}”.` : 'There is nothing to play here.';
  }
  function drawRows(keep) {
    compute();
    const node = yt.node, kind = node.kind === 'search' ? node.value : 'page';
    $('yt-c1').textContent = { albums: 'Album', artists: 'Artist', playlists: 'Playlist' }[kind] || 'Title';
    $('yt-c2').textContent = { artists: '', playlists: 'Made by' }[kind] ?? 'Artist';
    $('yt-c3').textContent = ['albums', 'artists', 'playlists'].includes(kind) ? 'Details' : 'Album';
    $('yt-rows').innerHTML = yt.rows.length ? yt.rows.map(rowHtml).join('') : `<tr class="lib-empty"><td colspan="4">${emptyText()}</td></tr>`;
    if (!keep) $('yt-scroll').scrollTop = 0;
    // the header of an open album, artist or playlist
    const page = shownPage(), songs = yt.rows.filter((r) => playable(r.item)).map((r) => r.item);
    const total = songs.reduce((sum, t) => sum + (t.duration || 0), 0);
    $('yt-head').hidden = !page;
    if (page) {
      const bits = [page.kind === 'artist' ? page.info : page.artist, page.kind === 'album' ? page.label : '', page.year,
        page.kind !== 'artist' && songs.length ? `${songs.length} ${songs.length === 1 ? 'song' : 'songs'}${total ? ', ' + clock(total) : ''}` : ''];
      $('yt-head').innerHTML = `<b>${esc(page.title)}</b><span>${esc(bits.filter(Boolean).join(' · '))}</span>`;
    }
    // the count, and more results for a search
    const found = yt.found, items = yt.rows.filter((r) => r.item);
    let text = '';
    if (found && found.loading) text = node.kind === 'search' ? 'Searching…' : 'Opening…';
    else if (items.length) {
      const noun = page ? NOUNS.songs : NOUNS[node.value], n = page ? songs.length : items.length;
      text = `${n} ${n === 1 ? noun[0] : noun[1]}` + (page && total ? `, ${clock(total)}` : '');
    }
    $('yt-count').textContent = text;
    $('yt-more').hidden = !(node.kind === 'search' && found && found.more);
    $('yt-more').textContent = found && found.busy ? 'Getting more…' : 'More results';
    $('yt-back').disabled = !yt.back.length;
    drawCover();
  }
  // the cover of the chosen row, or of the open page
  function drawCover() {
    const row = yt.rows[yt.sel], page = shownPage();
    const url = (row && row.item && row.item.cover) || (page && page.cover) || '';
    const img = $('yt-art'), src = url ? base + 'yt/picture?u=' + encodeURIComponent(url) : '';
    if (img.dataset.src !== src) { img.dataset.src = src; if (src) img.src = src; else img.removeAttribute('src'); }
    img.hidden = !src; $('yt-none').hidden = !!src;
  }
  $('yt-art').addEventListener('error', () => { $('yt-art').hidden = true; $('yt-none').hidden = false; });

  // ---- the tree on the left ----
  function drawTree() {
    const rows = SEARCHES.map(([value, label]) => ({ depth: 0, label, node: { kind: 'search', value } }));
    rows.push({ depth: 0, label: 'Recently opened', fold: true });
    if (yt.open) {
      for (const page of yt.recent) rows.push({ depth: 1, label: page.title, node: { kind: 'page', value: page.key } });
      if (!yt.recent.length) rows.push({ depth: 1, label: '(nothing yet)', dim: true });
    }
    yt.tree = rows;
    $('yt-tree').innerHTML = rows.map((r, i) =>
      `<div class="node${same(r.node, yt.node) ? ' sel' : ''}${r.dim ? ' dim' : ''}" data-i="${i}" style="padding-left:${3 + r.depth * 11}px">` +
      `<i class="${r.fold ? (yt.open ? 'minus' : 'plus') : ''}"></i><span>${esc(r.label)}</span></div>`).join('');
  }
  function draw() { drawTree(); drawRows(); }
  const treeRow = (e) => { const el = e.target.closest('.node'); return el && yt.tree[Number(el.dataset.i)]; };
  $('yt-tree').addEventListener('click', (e) => {
    const row = treeRow(e);
    if (!row) return;
    if (row.fold) { yt.open = !yt.open; drawTree(); return; }
    if (row.node) show(row.node);
  });
  $('yt-tree').addEventListener('dblclick', (e) => { const row = treeRow(e); if (row && row.node) playRows(null); });

  // ---- playing ----
  // the songs the table shows become the Now Playing list, from one of them on
  function playRows(item) {
    const songs = yt.rows.map((r) => r.item).filter(playable);
    if (!songs.length) return;
    const page = shownPage();
    api('yt-play', { items: songs.map(song), start: (playable(item) ? item : songs[0]).video, name: page ? page.title : '' });
  }
  async function playItem(item, add) {   // an album or a playlist, from its row
    const page = await pageOf(item);
    if (!page || !(page.tracks || []).length) return;
    if (add) api('yt-add', { items: page.tracks.map(song) });
    else api('yt-play', { items: page.tracks.map(song), start: page.tracks[0].video, name: page.title });
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
    else if (!item) { const songs = yt.rows.map((r) => r.item).filter(playable); if (songs.length) api('yt-add', { items: songs.map(song) }); }
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
  $('yt-rows').addEventListener('click', (e) => { const i = rowAt(e); if (i >= 0) select(i); });
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
    if (playable(item)) {
      menu(e.clientX, e.clientY, [
        { label: 'Play', run: () => playRows(item) },
        { label: 'Play next', run: () => playNext(item) },
        { label: 'Add to Now Playing', run: () => add(item) },
        { label: 'Play similar songs', run: () => similar(item) },
        null,
        { label: 'Go to album', disabled: !item.album_id, run: () => goTo('album', item.album_id) },
        { label: 'Go to artist', disabled: !item.artist_id, run: () => goTo('artist', item.artist_id) },
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
  $('yt-more').addEventListener('click', () => {
    const node = yt.node, found = yt.found;
    if (node.kind !== 'search' || !found || !found.more || found.busy) return;
    found.busy = true; drawRows(true);
    get(`more?token=${encodeURIComponent(found.more)}&kind=${node.value}`).then((r) => {
      found.busy = false;
      if (r.error) Mist.flash(sentence(r.error), 6000);
      else { found.items = found.items.concat(r.items || []); found.more = r.more; }
      if (yt.found === found) drawRows(true);
    });
  });

  // ---- tool bar ----
  const chosen = () => (yt.rows[yt.sel] || {}).item || null;
  $('yt-back').addEventListener('click', () => { if (yt.back.length) { const node = yt.back[yt.back.length - 1]; yt.back = yt.back.slice(0, -1); show(node, true); } });
  $('yt-play').addEventListener('click', () => choose(chosen()));
  $('yt-queue').addEventListener('click', () => add(chosen()));
  $('yt-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); search(e.target.value); } });

  // ---- YouTube Music songs elsewhere in the player ----
  // when the list runs out on one, similar songs carry on (a setting); they
  // follow the last one heard, which is another when this one could not play
  Mist.carryOn = (t) => {
    if (!Mist.isStream(t) || state.settings.yt_radio === false) return false;
    const seed = Mist.track(state.played) || t;
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
    if (!Mist.isStream(t)) return;
    items.push(null,
      { label: 'Play similar songs', run: () => similar(t) },
      { label: 'Go to album', disabled: !t.album_id, run: () => goTo('album', t.album_id) },
      { label: 'Go to artist', disabled: !t.artist_id, run: () => goTo('artist', t.artist_id) });
  });
  on('view', (view) => { if (view !== 'premium') return; draw(); if (!yt.words) $('yt-search').focus(); });
  on('render', () => {
    if (state.view !== 'premium') return;
    const video = playing();
    for (const tr of $('yt-rows').children) {
      const row = yt.rows[Number(tr.dataset.i)], item = row && row.item;
      tr.classList.toggle('cur', playable(item) && item.video === video);
    }
  });

  Mist.premium = yt;
})();
