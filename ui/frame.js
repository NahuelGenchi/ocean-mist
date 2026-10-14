// The player's sculpted frame: a curved tab at the top right, a slanted task
// bar with a blue lobe under it and a control deck that bulges at the bottom
// left. It is one SVG, redrawn to fit the window. All numbers are window px.
// The compact skin has a small frame of its own, further down.
window.Frame = (() => {
  const TOP = 17.5;        // top of the blue band (the tab rises above it)
  const BAND = 28.3;       // bottom of the thin part of the band
  const BLOCK = 44;        // bottom of the thick left block
  const STRIP = 35;        // bottom of the light strip = top of the stage
  const SIDE = 89;         // right edge of the task bar = left of the stage
  const DECK = 60;         // height of the control deck
  const RIDGE = 44;        // the deck's highlight, measured from the bottom
  const DROP = 16;         // how far the bulge hangs below the right part
  const f = (n) => Math.round(n * 100) / 100;

  // the part of a cubic bezier from t to its end, as "C ..." arguments
  function tail(p, t) {
    const mix = (a, b) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    const a = mix(p[0], p[1]), b = mix(p[1], p[2]), c = mix(p[2], p[3]);
    const d = mix(a, b), e = mix(b, c), start = mix(d, e);
    return { start, rest: `C${f(e[0])},${f(e[1])} ${f(c[0])},${f(c[1])} ${f(p[3][0])},${f(p[3][1])}` };
  }
  function tabCurve(W) { return [[W - 246, BAND], [W - 226, BAND], [W - 227, 0.5], [W - 207, 0.5]]; }
  function tabCross(W) {  // where the band's top edge meets the tab's curve
    const p = tabCurve(W);
    let lo = 0, hi = 1;
    for (let i = 0; i < 24; i++) {
      const t = (lo + hi) / 2, u = 1 - t;
      const y = u * u * u * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t * t * t * p[3][1];
      if (y > TOP) lo = t; else hi = t;
    }
    return tail(p, (lo + hi) / 2);
  }

  function outline(W, H) {
    const B = H - DROP, cross = tabCross(W);
    return `M0.5,23.5 Q0.5,${TOP} 6,${TOP} L${f(cross.start[0])},${TOP} ${cross.rest} ` +
      `L${W - 10.5},0.5 A10,10 0 0 1 ${W - 0.5},10.5 L${W - 0.5},${B - 8.5} A8,8 0 0 1 ${W - 8.5},${B - 0.5} ` +
      `L338,${B - 0.5} C327,${B - 0.5} 320,${H - 0.5} 307,${H - 0.5} L102,${H - 0.5} ` +
      `C92,${H - 0.5} 86,${H - 6} 81,${H - 13} L7,${H - 32.5} Q0.5,${H - 34.5} 0.5,${H - 41.5} Z`;
  }
  const lobeTop = (H) => `L2.5,${H - 85.5} L60,${H - 67.5} C72,${H - 63.7} 80.600,${H - 56} 80.600,${H - 44}`;

  function svg(W, H) {
    const B = H - DROP, deckTop = H - DECK, ridge = H - RIDGE, cy = H - 23;
    const tab = tabCurve(W);
    const tabPath = `M${tab[0]} C${tab[1]} ${tab[2]} ${tab[3]}`;
    const sweep = `M358,0 C350,0 347,4 343,10 C339,18 336,25 331,29 C324,35 318,44 306,44`;
    const edge = `M0,${BLOCK} H54 C68,${BLOCK} 70,${BAND} 85,${BAND}`;
    return `
<defs>
  <clipPath id="f-clip"><path d="${outline(W, H)}"/></clipPath>
  <linearGradient id="g-tab" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${BAND}">
    <stop offset="0" stop-color="#ffffff"/><stop offset=".12" stop-color="#f6fafd"/><stop offset=".4" stop-color="#edf5fb"/>
    <stop offset=".62" stop-color="#e0e7f3"/><stop offset=".8" stop-color="#d3daea"/><stop offset="1" stop-color="#cdd5eb"/>
  </linearGradient>
  <linearGradient id="g-strip" gradientUnits="userSpaceOnUse" x1="0" y1="${BAND}" x2="0" y2="${STRIP}">
    <stop offset="0" stop-color="#d6e5ff"/><stop offset=".5" stop-color="#c4d5ee"/><stop offset="1" stop-color="#a3b0c6"/>
  </linearGradient>
  <linearGradient id="g-band" gradientUnits="userSpaceOnUse" x1="0" y1="${TOP}" x2="0" y2="${BLOCK}">
    <stop offset="0" stop-color="#c3d8ff"/><stop offset=".06" stop-color="#a5c2fd"/><stop offset=".15" stop-color="#90a6e3"/>
    <stop offset=".27" stop-color="#7d8ecd"/><stop offset=".4" stop-color="#7485c4"/><stop offset="1" stop-color="#7182c1"/>
  </linearGradient>
  <linearGradient id="g-thin" gradientUnits="userSpaceOnUse" x1="0" y1="${TOP}" x2="0" y2="${BAND}">
    <stop offset="0" stop-color="#b9d0ff"/><stop offset=".14" stop-color="#a0baf8"/><stop offset=".34" stop-color="#7f97d6"/>
    <stop offset=".55" stop-color="#6c80c0"/><stop offset=".78" stop-color="#5e6dae"/><stop offset="1" stop-color="#5663a1"/>
  </linearGradient>
  <linearGradient id="g-fade" gradientUnits="userSpaceOnUse" x1="52" y1="0" x2="112" y2="0">
    <stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff"/>
  </linearGradient>
  <mask id="m-thin" maskUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${BLOCK}"><rect width="${W}" height="${BLOCK}" fill="url(#g-fade)"/></mask>
  <linearGradient id="g-fade2" gradientUnits="userSpaceOnUse" x1="70" y1="0" x2="${SIDE + 14}" y2="0">
    <stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff"/>
  </linearGradient>
  <mask id="m-strip" maskUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${BLOCK}"><rect width="${W}" height="${BLOCK}" fill="url(#g-fade2)"/></mask>
  <linearGradient id="g-block" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="70" y2="0">
    <stop offset="0" stop-color="#fff" stop-opacity=".24"/><stop offset=".45" stop-color="#fff" stop-opacity=".04"/>
    <stop offset=".55" stop-color="#27347f" stop-opacity=".03"/><stop offset="1" stop-color="#27347f" stop-opacity=".2"/>
  </linearGradient>
  <linearGradient id="g-panel" gradientUnits="userSpaceOnUse" x1="3" y1="0" x2="${SIDE}" y2="0">
    <stop offset="0" stop-color="#f4fcff"/><stop offset=".34" stop-color="#f1faff"/><stop offset=".46" stop-color="#e7eefa"/>
    <stop offset=".55" stop-color="#dbe2ee"/><stop offset=".7" stop-color="#d2daec"/><stop offset=".85" stop-color="#c8d4e9"/><stop offset="1" stop-color="#c1d2e7"/>
  </linearGradient>
  <linearGradient id="g-lobe" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="81" y2="0">
    <stop offset="0" stop-color="#93a6df"/><stop offset=".3" stop-color="#8596d3"/><stop offset=".65" stop-color="#6b7abc"/><stop offset="1" stop-color="#5661a6"/>
  </linearGradient>
  <linearGradient id="g-lobe-shade" gradientUnits="userSpaceOnUse" x1="0" y1="${H - 86}" x2="0" y2="${H - 13}">
    <stop offset="0" stop-color="#fff" stop-opacity=".14"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#1c2560" stop-opacity=".16"/>
  </linearGradient>
  <linearGradient id="g-right" gradientUnits="userSpaceOnUse" x1="${W - 7}" y1="0" x2="${W}" y2="0">
    <stop offset="0" stop-color="#b5c1d9"/><stop offset=".3" stop-color="#cad5e8"/><stop offset=".72" stop-color="#d6e0f0"/><stop offset=".9" stop-color="#ffffff"/><stop offset="1" stop-color="#c9d3e6"/>
  </linearGradient>
  <linearGradient id="g-deck" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${RIDGE}">
    <stop offset="0" stop-color="#dfe6f2"/><stop offset=".1" stop-color="#e9eef8"/><stop offset=".32" stop-color="#f1f5fb"/>
    <stop offset=".365" stop-color="#fcffff"/><stop offset=".43" stop-color="#cad6e2"/><stop offset=".5" stop-color="#a6b0c6"/>
    <stop offset=".6" stop-color="#b9c1d7"/><stop offset=".72" stop-color="#d5deef"/><stop offset=".82" stop-color="#dce5f6"/>
    <stop offset=".95" stop-color="#d2ddee"/><stop offset="1" stop-color="#a9b4c6"/>
  </linearGradient>
  <linearGradient id="g-hump" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${RIDGE}">
    <stop offset="0" stop-color="#fcfffb"/><stop offset=".08" stop-color="#fffefd"/><stop offset=".2" stop-color="#eef4fc"/>
    <stop offset=".31" stop-color="#dbe1f4"/><stop offset=".42" stop-color="#c3cfe9"/><stop offset=".53" stop-color="#b0c0df"/>
    <stop offset=".62" stop-color="#b3c6e2"/><stop offset=".73" stop-color="#c6d8f1"/><stop offset=".85" stop-color="#d2e8fd"/>
    <stop offset=".93" stop-color="#cbdffa"/><stop offset=".975" stop-color="#bcc9e5"/><stop offset="1" stop-color="#98a6bc"/>
  </linearGradient>
  <linearGradient id="g-hump-edge" gradientUnits="userSpaceOnUse" x1="300" y1="0" x2="362" y2="0">
    <stop offset="0" stop-color="#6f7d9c" stop-opacity="0"/><stop offset=".55" stop-color="#6f7d9c" stop-opacity=".0"/>
    <stop offset=".8" stop-color="#6f7d9c" stop-opacity=".34"/><stop offset="1" stop-color="#6f7d9c" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="g-well" gradientUnits="userSpaceOnUse" x1="0" y1="${cy - 19}" x2="0" y2="${cy + 19}">
    <stop offset="0" stop-color="#6c7ea9" stop-opacity=".62"/><stop offset=".5" stop-color="#8496bf" stop-opacity=".5"/><stop offset="1" stop-color="#ffffff" stop-opacity=".75"/>
  </linearGradient>
  <filter id="f-soft" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="1.1"/></filter>
  <filter id="f-softer" x="-30%" y="-60%" width="160%" height="220%"><feGaussianBlur stdDeviation="2.2"/></filter>
</defs>
<g clip-path="url(#f-clip)">
  <rect width="${W}" height="${H}" fill="#ccd5e9"/>
  <rect x="${W - 7}" y="${BAND}" width="7" height="${H}" fill="url(#g-right)"/>
  <rect x="0" y="${BAND}" width="${SIDE}" height="${f(H - RIDGE - BAND)}" fill="url(#g-panel)"/>
  <rect x="60" y="${BAND}" width="${W}" height="${f(STRIP - BAND)}" fill="url(#g-strip)" mask="url(#m-strip)"/>
  <path d="M${SIDE - 0.5},${STRIP} V${deckTop}" stroke="#8b98b3" stroke-opacity=".8"/>

  <g transform="translate(0,${deckTop})">
    <rect x="60" y="0" width="${W}" height="${DECK}" fill="url(#g-deck)"/>
  </g>
  <g transform="translate(0,${ridge})">
    <path d="${sweep} H60 V0 Z" fill="url(#g-hump)"/>
    <path d="${sweep}" fill="none" stroke="#69779a" stroke-opacity=".5" stroke-width="3" filter="url(#f-softer)" transform="translate(3.500,1)"/>
    <path d="${sweep}" fill="none" stroke="#ffffff" stroke-opacity=".85" stroke-width="1.200"/>
  </g>
  <g filter="url(#f-soft)" fill="url(#g-well)">
    <path d="M105,${cy - 18.5} a18.500,18.500 0 1 0 0,37 c9,0 14,-5 19,-7.500 a13.800,13.800 0 1 0 0,-22 c-5,-2.500 -10,-7.500 -19,-7.500 z"/>
    <rect x="${172 - 13.8}" y="${cy - 13.8}" width="${25.5 + 27.6}" height="27.600" rx="13.800"/>
    <circle cx="234" cy="${cy}" r="13.800"/>
  </g>
  <path d="M82,${ridge + 0.5} V${H}" stroke="#ffffff" stroke-opacity=".9"/>

  <path d="M0,${TOP} H${W - 200} V${BAND} H85 C70,${BAND} 68,${BLOCK} 54,${BLOCK} H0 Z" fill="url(#g-band)"/>
  <rect x="40" y="${TOP}" width="${W - 240}" height="${f(BAND - TOP)}" fill="url(#g-thin)" mask="url(#m-thin)"/>
  <path d="M0,${TOP} H78 C70,${BAND + 2} 66,${BLOCK} 54,${BLOCK} H0 Z" fill="url(#g-block)"/>
  <path d="M85,${BAND + 0.1} H${W - 240}" stroke="#46558a" stroke-opacity=".9" stroke-width="1.200"/>
  <path d="${edge}" fill="none" stroke="#5b6897" stroke-opacity=".95" stroke-width="1.200"/>
  <path d="${edge}" fill="none" stroke="#ffffff" stroke-opacity=".8" stroke-width="1" transform="translate(0,1.200)"/>
  <path d="M1,${TOP + 1} H${W - 232}" stroke="#d9e6ff" stroke-opacity=".75"/>

  <path d="${tabPath} H${W} V${BAND} Z" fill="url(#g-tab)"/>
  <path d="${tabPath} H${W}" fill="none" stroke="#ffffff" stroke-width="1.400" transform="translate(0,1.200)"/>

  <rect x="0" y="${BLOCK}" width="2.500" height="${H}" fill="#8ca5dd"/>
  <path d="M2.500,${BLOCK + 1} V${H - 85}" stroke="#627398" stroke-opacity=".8" stroke-width=".800"/>

  <path d="M0,${H - 88} ${lobeTop(H)} L81,${H - 13} L81,${H} L0,${H} Z" fill="url(#g-lobe)"/>
  <path d="M0,${H - 88} ${lobeTop(H)} L81,${H - 13} L81,${H} L0,${H} Z" fill="url(#g-lobe-shade)"/>
  <path d="M2.500,${H - 85.5} ${lobeTop(H).slice(lobeTop(H).indexOf('L', 1))} V${H - 13}" fill="none" stroke="#4c5896" stroke-opacity=".75"/>
  <path d="M7,${H - 33.5} L81,${H - 14}" stroke="#b9c7ee" stroke-opacity=".55"/>
</g>
<path d="${outline(W, H)}" fill="none" stroke="#3d4a78" stroke-opacity=".62"/>`;
  }

  // ---- the compact skin: a small rounded bar, always the same size ----
  const MINI = { width: 360, height: 104, title: 22.5 };
  const SHIFT = 81;        // the deck's controls sit this much further left there
  function miniOutline() {
    const W = MINI.width, H = MINI.height, r = 9.5;
    return `M0.5,${r} Q0.5,0.5 ${r},0.5 L${W - r},0.5 Q${W - 0.5},0.5 ${W - 0.5},${r} L${W - 0.5},${H - r} ` +
      `Q${W - 0.5},${H - 0.5} ${W - r},${H - 0.5} L${r},${H - 0.5} Q0.5,${H - 0.5} 0.5,${H - r} Z`;
  }
  // p: a prefix for the ids, so a second copy (the Skin Chooser's picture) keeps its own gradients
  function miniSvg(p) {
    const W = MINI.width, H = MINI.height, T = MINI.title;
    const deckTop = H - DECK, ridge = H - RIDGE, cy = H - 23;
    return `
<defs>
  <clipPath id="${p}clip"><path d="${miniOutline()}"/></clipPath>
  <linearGradient id="${p}band" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${T}">
    <stop offset="0" stop-color="#c3d8ff"/><stop offset=".1" stop-color="#a5c2fd"/><stop offset=".3" stop-color="#90a6e3"/>
    <stop offset=".6" stop-color="#7d8ecd"/><stop offset="1" stop-color="#6878b8"/>
  </linearGradient>
  <linearGradient id="${p}body" gradientUnits="userSpaceOnUse" x1="0" y1="${T}" x2="0" y2="${ridge}">
    <stop offset="0" stop-color="#c4cfe6"/><stop offset=".5" stop-color="#dfe6f2"/><stop offset=".8" stop-color="#eef3fa"/><stop offset="1" stop-color="#fcffff"/>
  </linearGradient>
  <linearGradient id="${p}hump" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${RIDGE}">
    <stop offset="0" stop-color="#fcfffb"/><stop offset=".08" stop-color="#fffefd"/><stop offset=".2" stop-color="#eef4fc"/>
    <stop offset=".31" stop-color="#dbe1f4"/><stop offset=".42" stop-color="#c3cfe9"/><stop offset=".53" stop-color="#b0c0df"/>
    <stop offset=".62" stop-color="#b3c6e2"/><stop offset=".73" stop-color="#c6d8f1"/><stop offset=".85" stop-color="#d2e8fd"/>
    <stop offset=".93" stop-color="#cbdffa"/><stop offset=".975" stop-color="#bcc9e5"/><stop offset="1" stop-color="#98a6bc"/>
  </linearGradient>
  <linearGradient id="${p}well" gradientUnits="userSpaceOnUse" x1="0" y1="${cy - 19}" x2="0" y2="${cy + 19}">
    <stop offset="0" stop-color="#6c7ea9" stop-opacity=".62"/><stop offset=".5" stop-color="#8496bf" stop-opacity=".5"/><stop offset="1" stop-color="#ffffff" stop-opacity=".75"/>
  </linearGradient>
  <filter id="${p}soft" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="1.1"/></filter>
</defs>
<g clip-path="url(#${p}clip)">
  <rect width="${W}" height="${H}" fill="#ccd5e9"/>
  <rect y="${T}" width="${W}" height="${f(ridge - T)}" fill="url(#${p}body)"/>
  <rect width="${W}" height="${T}" fill="url(#${p}band)"/>
  <path d="M0,1.500 H${W}" stroke="#e4eeff" stroke-opacity=".8"/>
  <path d="M0,${T} H${W}" stroke="#46558a" stroke-opacity=".9"/>
  <g transform="translate(0,${ridge})">
    <rect width="${W}" height="${RIDGE}" fill="url(#${p}hump)"/>
    <path d="M0,.5 H${W}" stroke="#ffffff" stroke-opacity=".9"/>
  </g>
  <g filter="url(#${p}soft)" fill="url(#${p}well)">
    <path d="M${105 - SHIFT},${cy - 18.5} a18.500,18.500 0 1 0 0,37 c9,0 14,-5 19,-7.500 a13.800,13.800 0 1 0 0,-22 c-5,-2.500 -10,-7.500 -19,-7.500 z"/>
    <rect x="${f(172 - SHIFT - 13.8)}" y="${cy - 13.8}" width="${25.5 + 27.6}" height="27.600" rx="13.800"/>
    <circle cx="${234 - SHIFT}" cy="${cy}" r="13.800"/>
  </g>
</g>
<path d="${miniOutline()}" fill="none" stroke="#3d4a78" stroke-opacity=".7"/>`;
  }
  // a picture of the compact skin for the Skin Chooser
  function preview() {
    const W = MINI.width, H = MINI.height, cy = H - 23;
    const orb = (cx, r) => `<circle cx="${cx}" cy="${cy}" r="${r}" fill="url(#pv-orb)" stroke="#98a2ae"/>`;
    return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg">${miniSvg('pv-')}
<defs><radialGradient id="pv-orb" cx=".5" cy=".28" r=".8"><stop offset="0" stop-color="#fff"/><stop offset=".5" stop-color="#fff"/><stop offset="1" stop-color="#c6e0ea"/></radialGradient></defs>
<text x="27" y="15" font-size="11" font-weight="700" fill="#fff">Ocean Mist</text>
<rect x="7" y="24" width="${W - 14}" height="17" rx="3" fill="#000"/>
<path d="M16,29 l6,3.500 -6,3.500z" fill="#8be02e"/>
<text x="30" y="36.500" font-size="11" fill="#8be02e">Song: Like Humans Do (radio edit)</text>
<text x="${W - 12}" y="36.500" font-size="11" fill="#8be02e" text-anchor="end">00:20</text>
<rect x="38" y="${H - 56}" width="${W - 73}" height="10" fill="#b4bace"/><rect x="38" y="${H - 55}" width="60" height="8" fill="#a5d8a4"/>
${orb(24, 14.5)}${orb(54, 10)}${orb(91, 10)}${orb(116.5, 10)}${orb(153, 10)}
<path d="M20,${cy - 6} l10,6 -10,6z" fill="#2b3c4e"/><rect x="50.700" y="${cy - 3.3}" width="6.600" height="6.600" fill="#2b3c4e"/>
<path d="M173,${cy + 6} L237,${cy - 3} V${cy + 6} Z" fill="#dde2ec"/><path d="M173,${cy + 6} L205,${cy + 1.5} V${cy + 6} Z" fill="#8fce8c"/>
${orb(277, 8.5)}${orb(316, 8.5)}${orb(335, 8.5)}
</svg>`;
  }
  const isMini = () => document.body.classList.contains('compact');

  // one {x, y, w, h} per run of rows the frame covers, for the host's input
  // region, so clicks on the transparent corners reach whatever is below
  function shape(W, H) {
    const mini = isMini();
    if (mini) { W = MINI.width; H = MINI.height; }
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d'), path = new Path2D(mini ? miniOutline() : outline(W, H));
    const inside = (x, y) => ctx.isPointInPath(path, x, y);
    const rects = [];
    let run = null;
    for (let y = 0; y < H; y++) {
      const mid = y + 0.5;
      let probe = W - 30;
      if (!inside(probe, mid)) probe = 200;
      if (!inside(probe, mid)) { run = null; continue; }
      let a = 0, b = probe;
      while (b - a > 0.5) { const m = (a + b) / 2; if (inside(m, mid)) b = m; else a = m; }
      const left = Math.floor(a);
      a = probe; b = W;
      while (b - a > 0.5) { const m = (a + b) / 2; if (inside(m, mid)) a = m; else b = m; }
      const right = Math.ceil(b);
      if (run && run.x === left && run.w === right - left) run.h += 1;
      else { run = { x: left, y, w: right - left, h: 1 }; rects.push(run); }
    }
    return rects;
  }

  // the strips of the frame that resize the window when dragged
  function edges(W, H) {
    if (isMini()) return [];
    const B = H - DROP, t = 4, c = 12;
    return [
      { x: 0, y: TOP, w: c, h: c, edge: 'nw' },
      { x: W - c, y: 0, w: c, h: c, edge: 'ne' },
      { x: W - c, y: B - c, w: c, h: c, edge: 'se' },
      { x: 0, y: H - 46, w: c, h: 16, edge: 'sw' },
      { x: c, y: TOP, w: W - 246 - c, h: t, edge: 'n' },
      { x: W - 210, y: 0, w: 210 - c, h: t, edge: 'n' },
      { x: 0, y: TOP + c, w: t, h: H - 46 - TOP - c, edge: 'w' },
      { x: W - t, y: c, w: t, h: B - c * 2, edge: 'e' },
      { x: 340, y: B - t, w: W - 340 - c, h: t, edge: 's' },
      { x: 100, y: H - t, w: 208, h: t, edge: 's' },
    ];
  }

  let last = '';
  function draw() {
    const mini = isMini();
    const W = mini ? MINI.width : Math.max(400, Math.round(innerWidth));
    const H = mini ? MINI.height : Math.max(300, Math.round(innerHeight));
    const key = (mini ? 'mini' : '') + W + 'x' + H;
    if (last === key) return false;
    last = key;
    const el = document.getElementById('frame');
    el.setAttribute('viewBox', `0 0 ${W} ${H}`);
    el.setAttribute('width', W); el.setAttribute('height', H);
    el.innerHTML = mini ? miniSvg('m-') : svg(W, H);
    return true;
  }

  return { draw, outline, shape, edges, preview, MINI };
})();
