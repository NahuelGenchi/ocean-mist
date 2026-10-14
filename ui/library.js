// Media Library: everything in the music folders, by artist, album or genre,
// plus the saved playlists. The files stay where they are; this only lists them.
(() => {
  const { $, state, api, on, esc, clock, menu, base } = Mist;
  const PAGE = 400;   // rows drawn at a time; more are added while scrolling
  const KINDS = { artist: 'Artist', album: 'Album', genre: 'Genre' };
  const UNKNOWN = { artist: 'Unknown Artist', album: 'Unknown Album', genre: 'Unknown Genre' };
  const lib = {
    tracks: [], loaded: false, stale: true, scanning: false,
    node: { kind: 'all' },      // what the table shows: now | all | artist | album | genre | list
    open: { artist: false, album: false, genre: false, lists: true },
    lists: {},                  // playlist path -> its tracks
    sel: null, filter: '', sort: null, rows: [], shown: 0, tree: [],
  };
  const compare = (a, b) => String(a).localeCompare(String(b), undefined, { sensitivity: 'base', numeric: true });
  const same = (a, b) => a && b && a.kind === b.kind && a.value === b.value;
  const plain = (text) => text.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');   // so "song" finds "Söng"

  async function load() {
    lib.stale = false;
    const data = await (await fetch(base + 'api/library')).json();
    lib.tracks = data.tracks; lib.scanning = data.scanning; lib.loaded = true;
    draw();
  }
  function loadList(path) {
    api('playlist-tracks', { path }).then((r) => { lib.lists[path] = r.tracks || []; if (same(lib.node, { kind: 'list', value: path })) drawRows(); });
  }

  // ---- the tree on the left ----
  function drawTree() {
    const rows = [];
    const add = (depth, label, node, extra) => rows.push(Object.assign({ depth, label, node }, extra));
    add(0, 'Now Playing', { kind: 'now' });
    add(0, 'All Music', { kind: 'all' });
    for (const key of Object.keys(KINDS)) {
      add(1, KINDS[key], null, { fold: key });
      if (!lib.open[key]) continue;
      const names = [...new Set(lib.tracks.map((t) => t[key] || ''))].sort(compare);
      for (const name of names) add(2, name || UNKNOWN[key], { kind: key, value: name });
    }
    add(0, 'My Playlists', null, { fold: 'lists' });
    if (lib.open.lists) {
      for (const p of state.playlists) add(1, p.name, { kind: 'list', value: p.path });
      if (!state.playlists.length) add(1, '(none saved yet)', null, { dim: true });
    }
    lib.tree = rows;
    $('lib-tree').innerHTML = rows.map((r, i) =>
      `<div class="node${same(r.node, lib.node) ? ' sel' : ''}${r.dim ? ' dim' : ''}" data-i="${i}" style="padding-left:${3 + r.depth * 11}px">` +
      `<i class="${r.fold ? (lib.open[r.fold] ? 'minus' : 'plus') : ''}"></i><span>${esc(r.label)}</span></div>`).join('');
  }
  $('lib-tree').addEventListener('click', (e) => {
    const el = e.target.closest('.node');
    const row = el && lib.tree[Number(el.dataset.i)];
    if (!row) return;
    if (row.fold) { lib.open[row.fold] = !lib.open[row.fold]; drawTree(); return; }
    if (!row.node) return;
    lib.node = row.node; lib.sel = null; lib.sort = null;
    if (row.node.kind === 'list') loadList(row.node.value);
    draw();
  });
  $('lib-tree').addEventListener('dblclick', (e) => {
    const el = e.target.closest('.node');
    const row = el && lib.tree[Number(el.dataset.i)];
    if (row && row.node && row.node.kind !== 'now') playRows(null);
  });

  // ---- the table ----
  function compute() {
    const node = lib.node;
    let rows = node.kind === 'now' ? state.tracks
      : node.kind === 'list' ? (lib.lists[node.value] || [])
      : node.kind === 'all' ? lib.tracks
      : lib.tracks.filter((t) => (t[node.kind] || '') === node.value);
    const q = plain(lib.filter.trim());
    if (q) rows = rows.filter((t) => plain(`${t.title}\n${t.artist}\n${t.album}`).includes(q));
    const ordered = node.kind === 'now' || node.kind === 'list';   // these have an order of their own
    const sort = lib.sort || (ordered ? null : { key: 'artist', dir: 1 });
    if (sort) {
      const shelf = (a, b) => compare(a.artist, b.artist) || compare(a.album, b.album) || (a.track - b.track) || compare(a.title, b.title);
      const by = {
        title: (a, b) => compare(a.title, b.title) || shelf(a, b),
        artist: shelf,
        album: (a, b) => compare(a.album, b.album) || (a.track - b.track) || compare(a.title, b.title),
        duration: (a, b) => (a.duration - b.duration) || shelf(a, b),
      }[sort.key];
      rows = rows.slice().sort((a, b) => by(a, b) * sort.dir);
    }
    lib.rows = rows;
  }
  const rowHtml = (t) =>
    `<tr class="${t.id === state.cur ? 'cur ' : ''}${t.id === lib.sel ? 'sel' : ''}" data-id="${t.id}" title="${esc(t.folder + '/' + t.file)}">` +
    `<td>${esc(t.title)}</td><td>${esc(t.artist)}</td><td>${esc(t.album)}</td><td class="num">${t.duration ? clock(t.duration) : ''}</td></tr>`;
  function emptyText() {
    if (lib.filter.trim()) return 'Nothing matches the search.';
    if (lib.node.kind === 'now') return 'Nothing is in the Now Playing list.';
    if (lib.node.kind === 'list') return 'This playlist has no songs that can be found.';
    if (lib.scanning || !lib.loaded) return 'Looking for music…';
    return 'No music found. Use “Add” to tell the library where your music is.';
  }
  function drawRows(keep) {
    compute();
    lib.shown = Math.min(lib.rows.length, keep ? Math.max(lib.shown, PAGE) : PAGE);
    $('lib-rows').innerHTML = lib.rows.length ? lib.rows.slice(0, lib.shown).map(rowHtml).join('')
      : `<tr class="lib-empty"><td colspan="4">${emptyText()}</td></tr>`;
    if (!keep) $('lib-scroll').scrollTop = 0;
    const total = lib.rows.reduce((sum, t) => sum + (t.duration || 0), 0);
    $('lib-count').textContent = `${lib.rows.length} ${lib.rows.length === 1 ? 'song' : 'songs'}` +
      (total ? `, ${clock(total)}` : '') + (lib.scanning ? ' - looking for music…' : '');
    for (const th of document.querySelectorAll('#view-library th')) {
      th.classList.toggle('up', !!lib.sort && lib.sort.key === th.dataset.sort && lib.sort.dir > 0);
      th.classList.toggle('down', !!lib.sort && lib.sort.key === th.dataset.sort && lib.sort.dir < 0);
    }
    const now = lib.node.kind === 'now';
    $('lib-queue').hidden = now; $('lib-remove').hidden = !now; $('lib-clear').hidden = !now;
  }
  function draw() { drawTree(); drawRows(); }
  $('lib-scroll').addEventListener('scroll', (e) => {
    const el = e.currentTarget;
    if (lib.shown >= lib.rows.length || el.scrollTop + el.clientHeight < el.scrollHeight - 300) return;
    $('lib-rows').insertAdjacentHTML('beforeend', lib.rows.slice(lib.shown, lib.shown + PAGE).map(rowHtml).join(''));
    lib.shown = Math.min(lib.rows.length, lib.shown + PAGE);
  });
  for (const th of document.querySelectorAll('#view-library th')) {
    th.addEventListener('click', () => {
      const key = th.dataset.sort, was = lib.sort;
      lib.sort = !was || was.key !== key ? { key, dir: 1 } : was.dir > 0 ? { key, dir: -1 } : null;
      drawRows();
    });
  }

  function rowId(e) { const row = e.target.closest('tr[data-id]'); return row ? Number(row.dataset.id) : null; }
  function select(id) {
    lib.sel = id;
    for (const row of $('lib-rows').children) row.classList.toggle('sel', Number(row.dataset.id) === id);
  }
  // play what the table shows, from one song on (or from the top)
  function playRows(id) {
    if (!lib.rows.length) return;
    if (id === null || !lib.rows.some((t) => t.id === id)) id = lib.rows[0].id;
    if (lib.node.kind === 'now') { Mist.play(id); return; }
    const untouched = lib.node.kind === 'list' && !lib.sort && !lib.filter.trim();
    if (untouched) api('play-playlist', { path: lib.node.value, start: id, stay: true });   // keeps the playlist's name
    else api('play-set', { ids: lib.rows.map((t) => t.id), start: id });
  }
  const addToList = (ids) => api('add-ids', { ids });
  $('lib-rows').addEventListener('click', (e) => { const id = rowId(e); if (id !== null) select(id); });
  $('lib-rows').addEventListener('dblclick', (e) => { const id = rowId(e); if (id !== null) playRows(id); });
  $('lib-rows').addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const id = rowId(e); if (id === null) return;
    select(id);
    if (lib.node.kind === 'now') { Mist.trackMenu(e, id); return; }
    menu(e.clientX, e.clientY, [
      { label: 'Play', run: () => playRows(id) },
      { label: 'Play next', run: () => addToList([id]).then(() => Mist.queueNext(id)) },
      { label: 'Add to Now Playing', run: () => addToList([id]) },
    ]);
  });

  // ---- tool bar ----
  $('lib-add').addEventListener('click', (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const short = (path) => (path.length > 46 ? '…' + path.slice(-45) : path);
    menu(rect.left, rect.bottom + 1, [
      { label: 'Add folder to the library…', run: () => api('add-folder') },
      { label: 'Look for new music', run: () => api('rescan') },
      { label: 'Take a folder out of the library', disabled: !state.folders.length,
        items: state.folders.map((path) => ({ label: short(path), run: () => api('folder-remove', { path }) })) },
      null,
      { label: 'Add files to Now Playing…', run: () => api('open-files', { play: false }) },
      { label: 'Add folder to Now Playing…', run: () => api('open-folder', { play: false }) },
    ]);
  });
  $('lib-play').addEventListener('click', () => playRows(lib.sel));
  $('lib-queue').addEventListener('click', () => addToList(lib.sel !== null ? [lib.sel] : lib.rows.map((t) => t.id)));
  $('lib-remove').addEventListener('click', () => { if (lib.sel !== null) api('remove', { ids: [lib.sel] }); });
  $('lib-clear').addEventListener('click', () => api('clear'));
  $('lib-search').addEventListener('input', (e) => { lib.filter = e.target.value; drawRows(); });

  // ---- keeping up ----
  on('view', (view) => { if (view !== 'library') return; if (lib.stale || !lib.loaded) load(); else draw(); });
  on('event', (msg) => { if (msg.event !== 'library') return; if (state.view === 'library') load(); else lib.stale = true; });
  on('state', () => {
    if (state.view !== 'library') return;
    drawTree();
    if (lib.node.kind === 'list') loadList(lib.node.value);
  });
  on('render', () => {
    if (state.view !== 'library') return;
    if (lib.node.kind === 'now') { drawRows(true); return; }
    for (const row of $('lib-rows').children) row.classList.toggle('cur', Number(row.dataset.id) === state.cur);
  });

  Mist.library = lib;
})();
