// Skin Chooser: the compact skin shrinks the player to a small bar with only
// the status line and the controls (the host resizes the window to match); the
// classic and dark skins only change the colors, so compact keeps either.
(() => {
  const { $, state, api, on, setDark } = Mist;
  const SIZE = Frame.MINI;
  const body = document.body;
  const INFO = {
    classic: {
      title: 'Classic',
      text: 'The blue look the player starts with: the sculpted frame, with light lists and menus.' +
        '<br>Like Dark, it changes only the colors and leaves the size as it is.',
    },
    compact: {
      title: 'Compact',
      text: 'Only the status line and the controls, in a small bar that stays out of the way.' +
        '<br>The arrow button at its top right (or Ctrl+1) brings the full player back.',
    },
    dark: {
      title: 'Dark',
      text: 'A dark color scheme for the whole player: the frame, menus and lists use dark surfaces,' +
        '<br>while the album art and the green accents keep their own colors.',
    },
  };
  let chosen = 'compact';

  function select(name) {
    chosen = name;
    for (const el of document.querySelectorAll('#skin-list [data-skin]')) {
      el.classList.toggle('sel', el.dataset.skin === name);
    }
    $('skin-title').textContent = INFO[name].title;
    $('skin-desc').innerHTML = INFO[name].text;
    const compact = name === 'compact';
    $('skin-shot').hidden = !compact;
    if (compact) $('skin-shot').innerHTML = Frame.preview();
  }

  function compact() {
    if (body.classList.contains('compact')) return;
    if (body.classList.contains('fullscreen')) api('fullscreen');
    Mist.closeMenus();
    body.classList.add('compact');
    state.settings.mini = true;
    api('mini', { on: true, width: SIZE.width, height: SIZE.height });
    Mist.layoutChanged();
  }
  function leave() {
    if (!body.classList.contains('compact')) return;
    state.settings.mini = false;
    api('mini', { on: false });
    // keep the small look until the window has really grown back
    const done = () => {
      window.removeEventListener('resize', check); clearTimeout(timer);
      body.classList.remove('compact');
      Mist.layoutChanged();
    };
    const check = () => { if (innerWidth > SIZE.width + 60) done(); };
    const timer = setTimeout(done, 700);
    window.addEventListener('resize', check);
  }

  function apply() {
    if (chosen === 'compact') compact();
    else setDark(chosen === 'dark');
  }

  $('skin-apply').addEventListener('click', apply);
  $('skin-shot').addEventListener('dblclick', () => { select('compact'); apply(); });
  for (const el of document.querySelectorAll('#skin-list [data-skin]')) {
    el.addEventListener('click', () => select(el.dataset.skin));
    el.addEventListener('dblclick', () => { select(el.dataset.skin); apply(); });
  }
  $('w-full').addEventListener('click', leave);
  select('compact');
  on('settings', () => { if (state.settings.mini) compact(); });

  Mist.skins = { apply, leave, compact };
})();
