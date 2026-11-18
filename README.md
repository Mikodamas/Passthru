<p align="center">
  <img src="io.github.mikodamas.Passthru.svg" width="128" alt="Passthru logo">
</p>

<h1 align="center">Passthru</h1>

<p align="center">A simple, no-nonsense capture card viewer for Linux — video <i>and</i> audio, with as little latency as possible.</p>

---

Made for playing consoles through a capture card on a Linux desktop without having to set up OBS
and monitor the audio separately. Plug in the card, open Passthru, play.

## Features

- **Low latency** — always shows the newest frame; old frames are dropped instead of queued.
- **Audio just works** — the capture card's audio is played to your speakers automatically.
- **Picks the best mode for you** — prefers uncompressed 1080p60 (NV12/YUY2) when the card offers it,
  otherwise MJPEG with multi-threaded decoding.
- **Hotplug** — unplug and replug the card and it picks up where it left off.
- **Remembers** your device, mode, audio input and volume.
- Fullscreen with auto-hiding cursor, stats overlay, and the screen won't go to sleep while you play.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `F11` / `F` / double-click | Fullscreen |
| `Esc` | Leave fullscreen |
| `M` | Mute |
| `H` | Show/hide toolbar |
| `I` | Stats overlay (capture / decoded / display fps) |
| `F5` | Rescan devices |
| `Ctrl+Q` | Quit |

## Install

### Flatpak (recommended)

```bash
git clone https://github.com/mikodamas/passthru.git
cd passthru
./build-flatpak.sh
```

This builds and installs Passthru for your user and also produces `Passthru.flatpak`, a single file
you can install on other machines with `flatpak install --user Passthru.flatpak`.

### Without Flatpak (Fedora)

```bash
./install.sh            # installs dependencies with dnf, adds Passthru to your app menu
# or just run it from the folder:
python3 passthru.py
```

### Try it without installing anything (Fedora toolbox)

```bash
./run-in-toolbox.sh
```

## Tips for the lowest latency

- Use an **uncompressed** mode (`[NV12]` or `[YUY2]`) if your card offers it at the resolution you want.
  These need a USB 3 port — check with `lsusb -t` (look for `5000M`).
- Play in **fullscreen**, and enable **Adaptive Sync** in your display settings if your monitor supports it.
- `passthru --no-vsync` can shave off a little more, at the cost of possible tearing.

## Requirements

Python 3, PyQt6, PyGObject, GStreamer (base + good plugins), PipeWire/PulseAudio.
All of this is handled for you by the Flatpak build.
