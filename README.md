<div align="center">

# VREC Studio By Joel Lagace — Virtual Vinyl Records

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20·%20Linux%20·%20macOS-lightgrey)](#requirements)

Press a WAV file onto a PNG that *looks* like a vinyl record and *is* the
audio. The fine groove texture in the image is the actual sample data: every
audio sample owns exactly one pixel along an Archimedean spiral, with its
brightness encoding the μ-law amplitude. Send someone the PNG and they can
play it — or scratch it — on the virtual turntable.

```
python vinyl_studio.py        ← the app. No command line beyond this.
```

**Vinyl Studio** is a windowed app suite: a home menu, a *Press a Record*
screen (browse for a WAV, type the title, click size / quality / label
color, watch a live capacity readout, hit PRESS), and a turntable *Deck*
where the record visibly spins under a tracking tonearm with clickable
transport buttons, speed & volume sliders, a seekable progress bar, and
mouse scratching. You can drag & drop a `.wav` or a record `.png` straight
onto the window. On Windows double-click `run_studio.bat`; on macOS/Linux
run `./run_studio.sh` (or just `python vinyl_studio.py`).

The pipeline underneath, also usable as plain scripts:

```
demo.wav ──▶ make_record.py ──▶ demo_record.png ──▶ record_player.py ──▶ 🔊
```

Two pressings are supported: **v1** (8-bit μ-law — max playtime, lo-fi warmth)
and **v2** (16-bit linear PCM — CD quality with `--cd`). Both put one sample
on one pixel; v2 hides the extra byte in the low nibbles of the red and blue
channels, so the disc still looks like gray vinyl.

The codec round-trips **bit-exactly**: decode reproduces every byte the
encoder wrote. Nothing is estimated or "read optically with tolerance" — the
spiral geometry is the file format, and encoder and decoder evaluate the same
closed-form curve.

## It really is live

Playback is a **live optical pickup** (`GrooveReader`): nothing is
pre-decoded. Every audio block, the player computes which pixels lie under
the needle *right now* and decodes those pixels on the spot — measured at
~0.13 ms per 512-frame block, ≈85× faster than real time even while
scratching at ±48× speed. The spinning image is not a visualization of a
RAM buffer; the image **is** the source.

Proof you can hear: on the Deck, toggle **MARKER ✎** and draw a line across
the spinning record. Each groove the line crosses gets damaged pixels, and
on the next revolution the needle reads them — *tick… tick… tick*, faster
toward the center, exactly like a scratched LP (this format is
constant-linear-velocity, so inner revolutions come around sooner). The
header ring and label are protected, so a scratched disc always still
loads. **SAVE COPY…** writes the damage into a new PNG — a shareable,
playable, scratched record.

## Postcard crops

The decoder now locates the disc itself instead of trusting the canvas:
losslessly **crop** the PNG (asymmetrically is fine), **pad** it onto a
bigger canvas, even strip the alpha channel — as long as the *whole disc*
stays visible, it still decodes bit-exactly (the header ring's ~300
redundant copies are used to lock the exact geometry, and over-cropped
discs are refused rather than mis-read). What still kills a record: JPEG,
resizing, screenshots, and photographs — see the honesty section below.

## Install

```
pip install numpy pillow pygame sounddevice
```

(`numpy` + `pillow` are enough for encoding/decoding; `pygame` + `sounddevice`
are only needed by the player.)

## Press a record

The Studio's PRESS screen does all of this with buttons. The equivalent CLI:

```
python make_record.py song.wav my_record.png --title "MY SONG" --artist "ME"
python make_record.py song.wav my_record.png --title "MY SONG" --cd   # CD quality
```

Options:

| flag        | default | meaning                                          |
|-------------|---------|--------------------------------------------------|
| `--size N`  | 4096    | image is N×N pixels                              |
| `--rate Hz` | 22050   | pressing sample rate (auto-lowered to fit)       |
| `--bits B`  | 8       | 8 = μ-law (v1), 16 = linear PCM (v2)             |
| `--cd`      | off     | preset: `--bits 16 --rate 44100`                 |
| `--pitch P` | 2.0     | px between groove revolutions (min 2.0)          |
| `--step S`  | 1.5     | px of arc per sample (min ≈1.415)                |
| `--color R,G,B` | 173,44,38 | label color                                |

If the song is too long for the disc, the encoder first lowers the sample
rate (down to 8000 Hz), then trims — and tells you it did.

### Capacity at default pitch/step

Capacity is counted in *samples* (one pixel each), so bit depth doesn't
change it — only the rate does:

| size   | 22050 Hz | 44100 Hz (`--cd`) |
|--------|----------|-------------------|
| 2048²  | ~0:32    | ~0:16             |
| 4096²  | ~2:08    | ~1:04             |
| 6144²  | ~4:49    | ~2:24             |
| 8192²  | ~8:33    | ~4:16             |
| 12288² | ~19:14   | ~9:37             |

Halving the rate doubles playtime. 16-bit PNGs are larger on disk (the
hidden low bytes are high-entropy and compress poorly): the 2-minute CD demo
at 6144² is ~42 MB vs ~14 MB for the 8-bit 4096² version.

## Play it

The Studio's DECK screen has on-screen buttons for everything below. The
lightweight standalone player still exists too — run it with a path, or with
no arguments to get a file picker:

```
python record_player.py my_record.png
```

| control      | action                                  |
|--------------|-----------------------------------------|
| `SPACE`      | play / pause (motor spins down)         |
| drag disc    | scratch                                 |
| `←` / `→`    | seek ±5 s                               |
| `↑` / `↓`    | speed ±2 %                              |
| `1` / `2`    | speed presets (33⅓ / "45")              |
| `L`          | toggle loop                             |
| MARKER ✎     | draw real, audible scratches onto the disc |
| SAVE COPY…   | save the scratched disc as a playable PNG  |
| `R`          | restart                                 |
| `ESC` / `Q`  | quit                                    |

The platter is rendered so the **outer groove** turns at a true 33⅓ RPM, and
the needle's radius is computed from the same spiral formula the decoder
uses — it genuinely sits on the groove that is playing.

If scratching feels direction-inverted on your setup, flip `SCRATCH_SIGN`
at the top of `record_player.py`.

## ⚠️ Sharing rules

The PNG **is** the audio. It survives anything lossless (copy, zip, most
file-transfer services). It does **not** survive:

- JPEG conversion / re-compression
- resizing or rotating
- screenshots
- chat apps that recompress images (send as a *file*, not a photo)

The decoder detects a damaged/transcoded disc (header ring unreadable) and
says so rather than playing noise.

## Format spec (VREC v1)

Everything is derived from the image size `S = min(width, height)`; center is
`(W/2, H/2)`. Radii as fractions of `S`: disc edge 0.495, **header ring
0.478**, data band **0.460 → 0.225**, label 0.165, spindle 0.012.

**Data spiral.** Archimedean spiral, outside → in, constant arc-length step.
With `k = pitch / 2π`, `r₀ = 0.460·S`, arc position `s = n·step` for sample
`n`:

```
r(s) = sqrt(r₀² − 2ks)        θ(s) = (r₀ − r(s)) / k
x = round(W/2 + r·cosθ)       y = round(H/2 + r·sinθ)     (round half up)
```

**v1 (8-bit, version=1, flags bit0):** sample value = Green channel = μ-law
code (μ=255, 0–255, code 128 ≈ silence); pixels are gray (R=G=B).

**v2 (16-bit, version=2, flags bit1):** sample is a uint16 offset-binary PCM
code `u` (32768 = silence). With `hi = u >> 8`, `lo = u & 0xFF`:

```
G = hi                      (brightness still IS the waveform)
R = (hi & 0xF0) | (lo >> 4)
B = (hi & 0xF0) | (lo & 0x0F)
decode:  u = (G << 8) | ((R & 0x0F) << 4) | (B & 0x0F)
```

Every channel stays within 15 of gray, so v2 discs keep the vinyl look with
a faint shimmer up close. Either way, `step ≥ √2` guarantees consecutive samples
never round to the same pixel, so one damaged pixel damages exactly one
sample. Extra "cosmetic" pixels between samples make grooves look solid;
the decoder never reads them because it regenerates the exact sample-pixel
sequence from the formula.

**Header ring.** A perfect circle at `r = 0.478·S` walked with the same
1.5 px arc step, carrying ~300 repeats of a 26-byte little-endian record:

```
4s  magic  b"VREC"
B   version (1 = μ-law disc, 2 = 16-bit PCM disc)
B   flags   (bit0 = μ-law, bit1 = 16-bit PCM)
I   sample_rate
I   n_samples
H   pitch  ×1000
H   step   ×1000
H   f_outer×10000
H   f_inner×10000
I   crc32 of the 22 bytes above
```

A decoder needs only the image: compute the ring from `S`, slide a 26-byte
window until magic + CRC match, then walk the spiral. Mono only, both
versions. The player needs no version logic at all — it just consumes the
decoded float audio.


## Troubleshooting

- **"not a VREC disc"** — the PNG was resized/recompressed somewhere; get the
  original file.
- **No sound / device errors** — `sounddevice` uses PortAudio; on Linux you
  may need `sudo apt install libportaudio2`. The player falls back to the
  device's default sample rate automatically.
- **No native file dialog** — the Studio prefers your OS picker (tkinter,
  bundled with python.org installers; on Linux `sudo apt install
  python3-tk`). If it's missing, a built-in browser screen takes over
  automatically, so nothing breaks.
- **Want a real double-clickable app?** `pip install pyinstaller` then
  `pyinstaller --onefile --windowed vinyl_studio.py` produces a standalone
  executable in `dist/`.
- **Choppy audio while dragging the window** — that's pygame on some
  platforms; audio runs in a separate callback and recovers instantly.

## Honest limits & ideas for v3

- **Reed–Solomon vs JPEG:** RS on the *current* fine-pitch format will not
  survive JPEG. The grooves are 2 px features — exactly the high-frequency
  detail JPEG quantizes hardest — so *every* sample comes back slightly
  wrong (and 4:2:0 chroma subsampling erases the v2 low-nibbles entirely).
  RS fixes a few bad symbols among many good ones, not "everything off by a
  little." What *would* work: a separate **postcard mode** — chunky 3×3/4×4
  px cells, gray-only, ~64 levels, pilot rings for level calibration, plus
  RS for residual errors. Cost: roughly 10–16× less capacity (a 4096² disc
  ≈ 15–30 s of lo-fi audio) in exchange for surviving mild JPEG.
- **Photographing a record:** needs the above *plus* perspective
  rectification from the disc's circular fiducials, deblurring, and
  brightness calibration. Screen photos of a postcard-mode disc are
  plausible future work; a photographed fine-pitch disc is not realistic.
- multiple "tracks": silence gaps render as visibly smooth rings (real
  LP-style separators); a small track-index ring inside the data band gives
  the deck NEXT/PREV buttons
- stereo: two interleaved spirals, or L/R via a second nibble pair
- constant-angular-velocity variant (authentic inner-groove distortion!)
- album art ghosted into the groove brightness floor
