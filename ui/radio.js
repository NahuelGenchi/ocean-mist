// Radio Tuner: internet radio stations, kept as a name and an address.
(() => {
  const { $, state, api, on, onPlay, esc, menu } = Mist;
  let sel = null, editing = null;

  function draw() {
    $('radio-rows').innerHTML = state.stations.length ? state.stations.map((s) =>
      `<tr class="${s.id === state.station ? 'cur ' : ''}${s.id === sel ? 'sel' : ''}" data-id="${s.id}">` +
      `<td>${esc(s.name)}</td><td>${esc(s.url)}</td></tr>`).join('')
      : '<tr class="lib-empty"><td colspan="2">No stations yet. Choose “Add station…” and paste the address of a stream.</td></tr>';
  }
  function edit(station) {
    editing = station ? station.id : null;
    $('radio-name').value = station ? station.name : '';
    $('radio-url').value = station ? station.url : '';
    $('radio-form').hidden = false;
    (station ? $('radio-name') : $('radio-url')).focus();
  }
  function remove(id) {
    if (id === null) return;
    if (id === sel) sel = null;
    api('station-remove', { id });
  }
  const rowId = (e) => { const row = e.target.closest('tr[data-id]'); return row ? Number(row.dataset.id) : null; };

  $('radio-rows').addEventListener('click', (e) => { const id = rowId(e); if (id !== null) { sel = id; draw(); } });
  $('radio-rows').addEventListener('dblclick', (e) => { const id = rowId(e); if (id !== null) Mist.playStation(id); });
  $('radio-rows').addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const id = rowId(e); if (id === null) return;
    sel = id; draw();
    menu(e.clientX, e.clientY, [
      { label: 'Play', run: () => Mist.playStation(id) },
      { label: 'Edit…', run: () => edit(Mist.station(id)) },
      { label: 'Remove', run: () => remove(id) },
    ]);
  });
  $('radio-play').addEventListener('click', () => {
    const id = sel !== null ? sel : (state.stations[0] || {}).id;
    if (id !== undefined) Mist.playStation(id);
  });
  onPlay('radio', () => { if (!Mist.station(sel)) return false; Mist.playStation(sel); return true; });
  $('radio-add').addEventListener('click', () => edit(null));
  $('radio-edit').addEventListener('click', () => { if (Mist.station(sel)) edit(Mist.station(sel)); });
  $('radio-remove').addEventListener('click', () => remove(sel));
  $('radio-cancel').addEventListener('click', () => { $('radio-form').hidden = true; });
  $('radio-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const url = $('radio-url').value.trim();
    if (!url) { $('radio-url').focus(); return; }
    api('station-save', { id: editing, name: $('radio-name').value, url }).then((r) => { if (r.id) sel = r.id; draw(); });
    $('radio-form').hidden = true;
  });
  $('radio-form').addEventListener('keydown', (e) => { if (e.key === 'Escape') $('radio-form').hidden = true; });

  on('view', (view) => { if (view === 'radio') draw(); });
  on('state', draw);
  on('track', draw);
  on('mode', draw);
})();
