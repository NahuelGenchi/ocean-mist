# Agent instructions

## The project

Ocean Mist is a music player for Linux with the look of Windows Media Player 9: the sculpted frame, the visualizations, a 10-band equalizer, a media library, internet radio and playlists. The interface is a local web page (`ui/`) shown in a frameless WebKitGTK window; `player.py` owns the window and a small HTTP server bound to 127.0.0.1 that feeds the page. There is no build step and nothing to install beyond GTK 3, WebKitGTK 4.1 and GStreamer; the Python side uses the standard library only. `README.md` describes the features.

It is an unofficial fan project, not affiliated with Microsoft.

Never describe something as working or complete unless a recorded check establishes it. A syntax check is not proof that the player behaves.

## Layout

| Path | Contents |
| :--- | :--- |
| `player.py` | The host: window, input shape, HTTP server, file dialogs, zoom, compact mode, notifications |
| `library.py` | Now Playing list, media library scan, saved playlists, radio stations, settings, resume point |
| `tags.py` | Titles, covers and lengths from MP3, FLAC, Ogg Vorbis, Opus, M4A and WAV, standard library only |
| `radio.py` | Relays an internet radio stream to the page and reads its song titles |
| `mpris.py` | The desktop's media controls and media keys (MPRIS) |
| `ui/frame.js` | The sculpted frame and the compact frame, drawn as SVG |
| `ui/app.js` | Playback core, menus, queue, keyboard; exposes `window.Mist` to the other scripts |
| `ui/eq.js`, `ui/library.js`, `ui/radio.js`, `ui/skins.js`, `ui/visuals.js` | Equalizer, Media Library, Radio Tuner, Skin Chooser, visualizations |
| `ui/fonts/` | Tahoma look-alike fonts from the Wine project (LGPL 2.1) |
| `data/` | Application icon and desktop entry template |
| `docs/` | Tracking guide and README images |

## Commands

- `python3 player.py [FILES OR FOLDERS...]` runs the player.
- `python3 player.py --serve PORT [FILES...]` serves the interface without a window, at `http://127.0.0.1:PORT/dev/`, for work on the page alone.
- `python3 -m py_compile player.py library.py tags.py radio.py mpris.py` and `for f in ui/*.js; do node --check "$f"; done` are the source checks. There is no test suite yet.
- `OCEAN_MIST_DEBUG=1` logs the HTTP requests and enables the web inspector.

## Working well here

- Start from the active issue, then read only the code involved.
- The look is the point. The frame is measured against the original player, so compare with a reference before changing its shape, sizes or colours, and say in the PR what was compared.
- Keep it dependency-free: no build step, no Python packages, no JavaScript libraries, unless the issue says otherwise.
- Never run a check against the owner's own player or settings. Use a separate instance and scratch folders, and keep it muted:

  ```sh
  OCEAN_MIST_APP_ID=io.github.nahuelgenchi.OceanMistTest \
  XDG_CONFIG_HOME=/tmp/ocean-mist-test/config XDG_CACHE_HOME=/tmp/ocean-mist-test/cache \
  python3 player.py
  ```

  Settings live in `$XDG_CONFIG_HOME/ocean-mist/state.json`; put `"muted": true` in its `"settings"` object before a check that plays anything.
- WebKitGTK behaviour worth knowing: script run from the host counts as a user gesture, so it hides autoplay problems; a seek on a song that has never played is ignored; a cross-origin stream is silent through Web Audio, which is why radio goes through the local relay; media only loads in a real, visible window.
- Record what a check covered and what it did not, in the PR.

## Private data

Music files, personal playlists, `state.json`, the tag and cover cache, and any account credentials or tokens stay out of commits, issues and PRs. Screenshots use generated sample music with invented names, never a real library.

## Tracking and delivery

Every change (code, bug fixes, enhancements, docs, research and repository operations) belongs to an issue with a milestone in `NahuelGenchi/ocean-mist`, reused or created before editing. Every new feature, and every other work item, gets its own new branch created from the latest `origin/main` for that issue: `<type>/<issue>-<slug>` (`feat`, `fix`, `enh`, `docs`, `research`, `ops`). Never reuse a branch for a second feature, never mix two issues on one branch, and never push to `main`. Use `type: summary (#N)` commits. Delete the branch after its PR merges. PRs follow `.github/pull_request_template.md`: a standalone `Closes #N` line, the issue's milestone, and `Refs #N` for unfinished parent work. Decisions, exact changed paths and blockers go in the issue or PR. See `CONTRIBUTING.md` and `docs/TRACKING.md`.

## Final report

Report the issue, the PR and what changed. Keep these separate: source checks (exact commands and outcomes, or "none run"), live player checks (the same), and source/diff review scope.
