// Visualizations for the Now Playing stage. Each preset draws one frame from
// the current audio snapshot; "feedback" presets smear the previous frame to
// get the swirling look of the original player's Battery collection.
window.Visuals = (function () {
  const TAU = Math.PI * 2;
  let canvas, ctx, buffer, bctx, width = 0, height = 0;
  let analyser = null, freq = new Uint8Array(1024), wave = new Uint8Array(2048);
  let running = false, raf = 0, last = 0, clock = 0, index = 0;
  let randomPick = 0, randomUntil = 0, bassAvg = 0;
  const peaks = [];
  // the playback engine's levels, for an Apple Music or Spotify song (whose sound never reaches the page): the low
  // bands of the same spectrum the analyser gives, about 30 times a second, and no wave
  const LIVE_FOR = 400;   // ms a reading counts for
  let outside = null, outsideAt = -1e9, wasLive = false, onLive = null;
  const isLive = () => performance.now() - outsideAt < LIVE_FOR;

  function hsl(h, s, l, a) { return `hsla(${((h % 360) + 360) % 360},${s}%,${l}%,${a === undefined ? 1 : a})`; }

  function smear(o) {
    ctx.save();
    ctx.globalAlpha = o.fade;
    ctx.translate(width / 2 + (o.dx || 0), height / 2 + (o.dy || 0));
    ctx.rotate(o.rot || 0);
    ctx.scale(o.zoom || 1, o.zoomY || o.zoom || 1);
    ctx.drawImage(buffer, -width / 2, -height / 2, width, height);
    ctx.restore();
  }

  function ringWave(a, radius, amp, color, lineWidth, turns) {
    ctx.beginPath();
    const n = 180;
    for (let i = 0; i <= n; i++) {
      const s = (a.wave[(i * 5) % a.wave.length] - 128) / 128;
      const petals = turns ? Math.sin((i / n) * TAU * turns + a.t * 1.3) * amp * 0.5 : 0;
      const r = radius + s * amp + petals;
      const ang = (i / n) * TAU + a.t * 0.2;
      const x = width / 2 + Math.cos(ang) * r, y = height / 2 + Math.sin(ang) * r * 0.9;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.closePath();
    ctx.strokeStyle = color; ctx.lineWidth = lineWidth; ctx.stroke();
  }

  // ---- Battery (feedback) ----
  function flower(a) {
    smear({ fade: 0.965, zoom: 1.035 + a.bass * 0.03, rot: 0.012 + a.mid * 0.02 });
    const m = Math.min(width, height);
    const hue = a.t * 38;
    ctx.globalCompositeOperation = 'lighter';
    ringWave(a, m * (0.10 + a.bass * 0.10), m * 0.09, hsl(hue + 300, 90, 62, 0.9), 2.2, 7);
    ringWave(a, m * 0.05, m * 0.05, hsl(hue + 60, 85, 55, 0.8), 1.6, 5);
    ctx.globalCompositeOperation = 'source-over';
    const g = ctx.createRadialGradient(width / 2, height / 2, 0, width / 2, height / 2, m * 0.06);
    g.addColorStop(0, hsl(hue + 320, 80, 30, 0.9)); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, width, height);
  }

  function swirl(a) {
    smear({ fade: 0.95, zoom: 1.02, rot: -0.03 - a.bass * 0.03 });
    const m = Math.min(width, height), arms = 5;
    ctx.globalCompositeOperation = 'lighter';
    for (let k = 0; k < arms; k++) {
      const level = a.freq[4 + k * 9] / 255;
      const ang = a.t * 0.9 + (k / arms) * TAU;
      const r = m * (0.12 + level * 0.3);
      const x = width / 2 + Math.cos(ang) * r, y = height / 2 + Math.sin(ang) * r;
      const size = m * (0.02 + level * 0.05);
      const g = ctx.createRadialGradient(x, y, 0, x, y, size);
      g.addColorStop(0, hsl(a.t * 50 + k * 72, 95, 65, 0.95)); g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, size, 0, TAU); ctx.fill();
    }
    ctx.globalCompositeOperation = 'source-over';
  }

  function tunnel(a) {
    smear({ fade: 0.94, zoom: 1.07 + a.bass * 0.05, rot: Math.sin(a.t * 0.4) * 0.02 });
    const m = Math.min(width, height);
    ctx.globalCompositeOperation = 'lighter';
    ringWave(a, m * 0.07, m * 0.035, hsl(a.t * 60 + 180, 90, 60, 0.95), 2);
    if (a.beat) ringWave(a, m * 0.12, m * 0.02, hsl(a.t * 60, 90, 70, 0.9), 3);
    ctx.globalCompositeOperation = 'source-over';
  }

  function drift(a) {
    smear({ fade: 0.97, zoom: 1.012, zoomY: 1.03, rot: 0.004, dx: Math.sin(a.t * 0.7) * 2 });
    ctx.globalCompositeOperation = 'lighter';
    ctx.beginPath();
    const n = 160;
    for (let i = 0; i <= n; i++) {
      const s = (a.wave[(i * 6) % a.wave.length] - 128) / 128;
      const x = (i / n) * width, y = height / 2 + s * height * 0.28 + Math.sin(i / 14 + a.t * 2) * height * 0.05;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = hsl(a.t * 45 + 20, 95, 60, 0.9); ctx.lineWidth = 2; ctx.stroke();
    ctx.globalCompositeOperation = 'source-over';
  }

  const battery = [flower, swirl, tunnel, drift];

  function randomization(a) {
    if (clock > randomUntil) {
      randomPick = Math.floor(Math.random() * battery.length);
      randomUntil = clock + 14 + Math.random() * 8;
    }
    battery[randomPick](a);
  }

  // ---- Bars and Waves ----
  function bars(a) {
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, width, height);
    const count = Math.max(16, Math.min(64, Math.floor(width / 9)));
    const gap = 2, bw = (width - gap * (count + 1)) / count;
    const grad = ctx.createLinearGradient(0, height, 0, height * 0.15);
    grad.addColorStop(0, '#1d7a10'); grad.addColorStop(0.6, '#6fdc2c'); grad.addColorStop(1, '#eaffb0');
    for (let i = 0; i < count; i++) {
      const lo = Math.floor(Math.pow(i / count, 1.7) * 380), hi = Math.max(lo + 1, Math.floor(Math.pow((i + 1) / count, 1.7) * 380));
      let v = 0; for (let j = lo; j < hi; j++) v = Math.max(v, a.freq[j]);
      const h = (v / 255) * (height - 6);
      peaks[i] = Math.max((peaks[i] || 0) - height * 0.006, h);
      const x = gap + i * (bw + gap);
      ctx.fillStyle = grad; ctx.fillRect(x, height - h, bw, h);
      ctx.fillStyle = '#d7e4ff'; ctx.fillRect(x, height - peaks[i] - 2, bw, 2);
    }
  }

  function scope(a) {
    ctx.fillStyle = 'rgba(0,0,0,0.35)'; ctx.fillRect(0, 0, width, height);
    ctx.beginPath();
    const n = Math.min(a.wave.length, Math.max(200, Math.floor(width)));
    for (let i = 0; i < n; i++) {
      const s = (a.wave[Math.floor((i / n) * a.wave.length)] - 128) / 128;
      const x = (i / (n - 1)) * width, y = height / 2 + s * height * 0.42;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = '#7fd13b'; ctx.lineWidth = 1.6; ctx.shadowColor = '#7fd13b'; ctx.shadowBlur = 6; ctx.stroke();
    ctx.shadowBlur = 0;
  }

  // ---- Ambience ----
  function ambience(a) {
    ctx.fillStyle = 'rgba(0,0,10,0.18)'; ctx.fillRect(0, 0, width, height);
    ctx.globalCompositeOperation = 'lighter';
    for (let k = 0; k < 6; k++) {
      const level = a.freq[2 + k * 14] / 255;
      const x = width * (0.5 + 0.38 * Math.sin(a.t * (0.21 + k * 0.05) + k * 1.7));
      const y = height * (0.5 + 0.36 * Math.cos(a.t * (0.17 + k * 0.04) + k * 2.3));
      const r = Math.min(width, height) * (0.14 + level * 0.26);
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, hsl(200 + k * 28 + a.t * 9, 85, 55, 0.20 + level * 0.25)); g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = g; ctx.fillRect(x - r, y - r, r * 2, r * 2);
    }
    ctx.globalCompositeOperation = 'source-over';
  }

  const presets = [
    { name: 'Battery : Randomization', draw: randomization, feedback: true },
    { name: 'Battery : Kaleidovision', draw: flower, feedback: true },
    { name: 'Battery : Spider\'s Last Moment', draw: swirl, feedback: true },
    { name: 'Battery : Event Horizon', draw: tunnel, feedback: true },
    { name: 'Battery : Sepiaswirl', draw: drift, feedback: true },
    { name: 'Bars and Waves : Bars', draw: bars },
    { name: 'Bars and Waves : Scope', draw: scope },
    { name: 'Ambience : Water', draw: ambience },
    { name: 'Album Art', art: true },
    { name: 'No Visualization', none: true },
  ];

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const most = Math.min(1280, 720 * (api.scale || 1));   // a zoomed player gets more pixels
    const w = Math.max(2, Math.round(Math.min(rect.width * (api.scale || 1), most))), h = Math.max(2, Math.round(rect.height * (w / Math.max(1, rect.width))));
    if (w === width && h === height) return;
    width = canvas.width = w; height = canvas.height = h;
    buffer.width = w; buffer.height = h;
    peaks.length = 0;
  }

  // a wave made of the engine's bands: sixteen of them, each at its own pitch and level, as the wave the analyser
  // would have shown is made of them too
  function waveOfBands() {
    const parts = [];
    let total = 0;
    for (let k = 0; k < 16; k++) {
      const lo = Math.floor(Math.pow(383, k / 16)), hi = Math.max(lo + 1, Math.floor(Math.pow(383, (k + 1) / 16)));
      let v = 0; for (let j = lo; j < hi; j++) v = Math.max(v, freq[j]);
      const amp = Math.pow(v / 255, 3);   // bytes are decibels: back to something like an amplitude
      parts.push([amp, (TAU * (lo + hi)) / 2 / wave.length, clock * (3 + k * 1.7)]);
      total += amp;
    }
    const scale = 118 / Math.max(1, total);
    for (let i = 0; i < wave.length; i++) {
      let s = 0; for (const [amp, step, phase] of parts) s += amp * Math.sin(i * step + phase);
      wave[i] = Math.max(0, Math.min(255, Math.round(128 + s * scale)));
    }
  }

  function sample() {
    if (isLive()) {
      freq.fill(0);
      freq.set(outside.subarray(0, Math.min(outside.length, freq.length)));
      waveOfBands();
    } else if (analyser) {
      analyser.getByteFrequencyData(freq);
      analyser.getByteTimeDomainData(wave);
    }
    let silent = true;
    for (let i = 0; i < wave.length; i += 16) if (wave[i] !== 128 && wave[i] !== 0) { silent = false; break; }
    if (!analyser || silent) {  // no analyser data: keep things moving with a synthetic signal
      for (let i = 0; i < wave.length; i++) wave[i] = 128 + 46 * Math.sin(i / 21 + clock * 5) * Math.sin(i / 173 + clock * 1.3);
      for (let i = 0; i < freq.length; i++) freq[i] = Math.max(0, 150 * Math.exp(-i / 90) * (0.6 + 0.4 * Math.sin(clock * 3 + i / 7)));
    }
    const avg = (a, b) => { let s = 0; for (let i = a; i < b; i++) s += freq[i]; return s / (b - a) / 255; };
    const bass = avg(1, 12), mid = avg(12, 90), treble = avg(90, 300);
    bassAvg = bassAvg * 0.93 + bass * 0.07;
    return { freq, wave, bass, mid, treble, beat: bass > bassAvg * 1.25 && bass > 0.25, t: clock };
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    const live = isLive();
    if (live !== wasLive) { wasLive = live; if (onLive) onLive(live); }
    const dt = Math.min(0.1, (now - last) / 1000 || 0); last = now;
    if (!running) return;
    clock += dt;
    resize();
    const preset = presets[index];
    if (!preset.draw) return;
    if (preset.feedback) {
      bctx.clearRect(0, 0, width, height); bctx.drawImage(canvas, 0, 0);
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, width, height);
    }
    preset.draw(sample());
  }

  const api = {
    presets,
    scale: 1,
    init(el) {
      canvas = el; ctx = canvas.getContext('2d');
      buffer = document.createElement('canvas'); bctx = buffer.getContext('2d');
      resize(); raf = requestAnimationFrame(frame);
    },
    // the engine's levels (base64 of a byte a band), and who wants to know when they come and when they stop
    feed(levels) {
      try {
        const raw = atob(String(levels));
        if (!outside || outside.length !== raw.length) outside = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) outside[i] = raw.charCodeAt(i);
        outsideAt = performance.now();
      } catch (e) { /* not levels: the last ones run out */ }
    },
    get live() { return isLive(); },
    get since() { return performance.now() - outsideAt; },
    onLive(fn) { onLive = fn; },
    setAnalyser(node) {
      analyser = node;
      if (node) { freq = new Uint8Array(node.frequencyBinCount); wave = new Uint8Array(node.fftSize); }
    },
    select(i) { index = ((i % presets.length) + presets.length) % presets.length; this.clear(); return index; },
    get index() { return index; },
    start() { running = true; },
    pause() { running = false; },
    clear() { if (ctx) { ctx.fillStyle = '#000'; ctx.fillRect(0, 0, width, height); } peaks.length = 0; },
    stop() { running = false; this.clear(); },
  };
  return api;
})();
