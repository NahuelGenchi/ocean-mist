'use strict';
/*
 * The preload of the helper's listening page (listen.html, a file of the helper's own). It gives that page the
 * source to capture, the volume the service's page plays at and a way to hand back the levels it reads, and
 * nothing else: no ipcRenderer, no Node.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('omListen', {
  onSource: (fn) => ipcRenderer.on('listen-source', (_event, id) => fn(String(id))),
  onVolume: (fn) => ipcRenderer.on('listen-volume', (_event, value) => fn(Number(value))),
  levels: (bands) => ipcRenderer.send('listen-levels', bands),
  said: (what) => ipcRenderer.send('listen-said', String(what).slice(0, 60)),
});
