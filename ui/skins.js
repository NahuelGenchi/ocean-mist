// Skin Chooser: the compact skin shrinks the player to a small bar with only
// the status line and the controls. The host resizes the window to match.
(() => {
  const { $, state, api, on } = Mist;
  const SIZE = Frame.MINI;
  const body = document.body;

  function apply() {
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

  $('skin-apply').addEventListener('click', apply);
  $('skin-shot').addEventListener('dblclick', apply);
  $('w-full').addEventListener('click', leave);
  $('skin-shot').innerHTML = Frame.preview();
  on('settings', () => { if (state.settings.mini) apply(); });

  Mist.skins = { apply, leave };
})();
