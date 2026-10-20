'use strict';
// The helper's listening page. It captures the sound of the service's page inside Chromium (a capture of the
// page, which opens no recording stream on the system, so the desktop shows no microphone in use and no other
// sound is read), and reads it the way the player reads a song of its own for the visualizations: the spectrum
// of an AnalyserNode, of which it hands back the low bands about 30 times a second. Chromium plays a captured
// page's sound only through its capture, so this page plays it on to the speakers; the sound goes nowhere else.
const BANDS = 384;        // the bands the visualizations read (0 to 9 kHz, at 48 kHz)
const EVERY = 33;         // ms between two readings
let volume = 1, undo = null;   // the service's page plays at `volume`: the levels are read as at full volume

// what the volume took away, given back for the levels only (down to 1 %; below that there is little to read)
const fullVolume = () => { if (undo) undo.gain.value = 1 / Math.max(0.01, volume); };
window.omListen.onVolume((value) => { volume = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 1; fullVolume(); });

window.omListen.onSource(async (id) => {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: id } }, video: false });
  } catch (e) {
    window.omListen.said(`refused ${e && e.name}`);
    return;
  }
  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  source.connect(ctx.destination);   // the sound, on to the speakers as the page would have played it
  undo = ctx.createGain();
  fullVolume();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;           // the player's own: the same bands, the same smoothing
  analyser.smoothingTimeConstant = 0.78;
  source.connect(undo);
  undo.connect(analyser);            // an analyser is read without a way out
  const bins = new Uint8Array(analyser.frequencyBinCount);
  setInterval(() => {
    analyser.getByteFrequencyData(bins);
    window.omListen.levels(bins.slice(0, BANDS));
  }, EVERY);
  for (const track of stream.getAudioTracks()) track.addEventListener('ended', () => window.omListen.said('ended'));
  window.omListen.said(`listening ${ctx.sampleRate}`);
});
