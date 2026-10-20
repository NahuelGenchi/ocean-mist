// Graphic equalizer: ten bands between the player and the speakers, shown as
// a pane under the visualization.
(() => {
  const { $, state, saveSettings, menu, on } = Mist;
  const BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
  const LABELS = ['31', '62', '125', '250', '500', '1K', '2K', '4K', '8K', '16K'];
  const RANGE = 12;   // dB up or down
  const PRESETS = {
    'Flat': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    'Rock': [5, 4, 3, 1, -1, -1, 1, 3, 4, 5],
    'Pop': [-1, 1, 3, 4, 4, 2, 0, -1, -1, -1],
    'Jazz': [3, 2, 1, 2, -1, -1, 0, 1, 3, 4],
    'Classical': [4, 3, 2, 1, -1, -1, 0, 2, 3, 4],
    'Dance': [6, 5, 3, 0, 0, -2, -1, 0, 3, 4],
    'Techno': [5, 4, 0, -2, -1, 0, 2, 4, 5, 5],
    'Acoustic': [4, 4, 3, 1, 2, 2, 3, 3, 3, 2],
    'Bass boost': [7, 6, 4, 2, 0, 0, 0, 0, 0, 0],
    'Treble boost': [0, 0, 0, 0, 0, 1, 2, 4, 6, 7],
    'Speech': [-4, -3, -1, 1, 3, 4, 4, 3, 1, -1],
  };

  let filters = [], pre = null;
  function values() {
    const saved = state.settings.eq;
    return BANDS.map((_, i) => Math.max(-RANGE, Math.min(RANGE, Number(Array.isArray(saved) ? saved[i] : 0) || 0)));
  }
  // called once by the player when it builds its audio graph
  function attach(ctx, source) {
    pre = ctx.createGain();
    source.connect(pre);
    let node = pre;
    filters = BANDS.map((hz, i) => {
      const filter = ctx.createBiquadFilter();
      filter.type = i === 0 ? 'lowshelf' : i === BANDS.length - 1 ? 'highshelf' : 'peaking';
      filter.frequency.value = hz; filter.Q.value = 1.4;
      node.connect(filter); node = filter;
      return filter;
    });
    apply();
    return node;
  }
  function apply() {
    const gains = values(), active = !!state.settings.eq_on;
    filters.forEach((filter, i) => { filter.gain.value = active ? gains[i] : 0; });
    // boosted bands would clip a loud song, so the whole signal steps back a little
    if (pre) pre.gain.value = active ? Math.pow(10, -Math.max(0, ...gains) / 2 / 20) : 1;
  }

  const bands = BANDS.map((hz, i) => {
    const el = document.createElement('div');
    el.className = 'eq-band';
    el.innerHTML = `<div class="eq-slot"><div class="eq-thumb"></div></div><label>${LABELS[i]}</label>`;
    $('eq-bands').appendChild(el);
    const slot = el.firstChild;
    const at = (e) => {
      const rect = slot.getBoundingClientRect();
      const part = 1 - Math.max(0, Math.min(1, (e.clientY - rect.top - 4) / (rect.height - 8)));
      return Math.round((part * 2 - 1) * RANGE);   // whole dB, so the middle is easy to hit
    };
    slot.addEventListener('pointerdown', (e) => {
      slot.setPointerCapture(e.pointerId);
      const move = (ev) => set(i, at(ev));
      const up = () => { slot.removeEventListener('pointermove', move); slot.removeEventListener('pointerup', up); slot.removeEventListener('pointercancel', up); };
      slot.addEventListener('pointermove', move); slot.addEventListener('pointerup', up); slot.addEventListener('pointercancel', up);
      move(e);
    });
    slot.addEventListener('dblclick', (e) => { e.stopPropagation(); set(i, 0); });
    return el;
  });

  function set(i, gain) {
    const gains = values();
    if (gains[i] === gain && state.settings.eq_on) return;
    gains[i] = gain;
    const match = Object.keys(PRESETS).find((name) => PRESETS[name].every((v, n) => v === gains[n]));
    Object.assign(state.settings, { eq: gains, eq_on: true, eq_preset: match || 'Custom' });
    changed();
  }
  function changed() { apply(); show(); saveSettings(); }
  function show() {
    const gains = values(), open = !!state.settings.eq_open;
    // an Apple Music or Spotify song's audio is protected: the playback engine plays it, not the player, so these bands never touch it
    const apart = state.station === null && Mist.outside(Mist.track(state.cur));
    $('eq').hidden = !open;
    $('stage').classList.toggle('with-eq', open);
    $('eq').classList.toggle('off', !state.settings.eq_on || apart);
    $('eq').title = apart ? 'The equalizer cannot shape Apple Music or Spotify songs: their audio is protected' : '';
    $('eq-on').textContent = state.settings.eq_on ? 'Turn off' : 'Turn on';
    $('eq-preset-name').textContent = state.settings.eq_preset || 'Flat';
    bands.forEach((el, i) => {
      el.querySelector('.eq-thumb').style.setProperty('--at', 1 - (gains[i] + RANGE) / (2 * RANGE));
      el.title = `${LABELS[i]} Hz: ${gains[i] > 0 ? '+' : ''}${gains[i]} dB`;
    });
  }
  function toggle() {
    state.settings.eq_open = !state.settings.eq_open;
    if (state.settings.eq_open && state.view !== 'now') Mist.show('now');
    show(); saveSettings();
  }

  $('eq-on').addEventListener('click', () => { state.settings.eq_on = !state.settings.eq_on; changed(); });
  $('eq-reset').addEventListener('click', () => { Object.assign(state.settings, { eq: PRESETS.Flat.slice(), eq_preset: 'Flat' }); changed(); });
  $('eq-close').addEventListener('click', toggle);
  $('eq-preset').addEventListener('click', (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    menu(rect.left, rect.top - 2, Object.keys(PRESETS).map((name) => ({
      label: name, checked: state.settings.eq_preset === name,
      run: () => { Object.assign(state.settings, { eq: PRESETS[name].slice(), eq_preset: name, eq_on: true }); changed(); },
    })), { above: true });
  });
  on('settings', () => { apply(); show(); });
  on('track', show);

  Mist.eq = { attach, toggle, values, PRESETS };
})();
