"""
vinyl_codec.py — encode audio into a PNG that looks like a vinyl record,
and decode it back, bit-exactly.

FORMAT "VREC v1"
================
Geometry (all derived from S = min(image width, height)):
    disc edge      r = 0.495 * S      (alpha = 0 outside; the PNG is a round disc)
    header ring    r = 0.478 * S      (a perfect circle of header bytes — the "lead-in groove")
    data band      r = 0.460*S  ...  0.225*S   (the spiral grooves live here)
    label          r = 0.165 * S
    spindle hole   r = 0.012 * S

Data encoding:
    * Audio is mono, one sample per pixel. Two depths:
        v1 (8-bit):  mu-law code (mu=255) written gray, R = G = B = code.
        v2 (16-bit): linear PCM, offset-binary (32768 = silence).
                     G = high byte (so brightness still IS the waveform);
                     the low byte hides in the low nibbles of R and B:
                         R = (hi & 0xF0) | (lo >> 4)
                         B = (hi & 0xF0) | (lo & 0x0F)
                     Every channel stays within 15 of gray, so the disc still
                     looks like vinyl, with a faint color shimmer if you zoom.
    * Sample i sits at arc length s = i*STEP along an Archimedean spiral that
      starts at r_outer and tightens by PITCH pixels per revolution.
      Closed form (k = PITCH / 2*pi):
            r(s)     = sqrt(r_outer^2 - 2*k*s)
            theta(s) = (r_outer - r(s)) / k
            pixel    = round(center + r * (cos theta, sin theta))
      Both encoder and decoder evaluate this same formula, so the geometry IS
      the file format. No groove tracking, no interpolation, no drift.
    * STEP = 1.5 px guarantees consecutive samples can never round to the same
      pixel (max rounding displacement is sqrt(2) < 1.5), so every sample owns
      a unique pixel and a single damaged pixel damages a single sample.
    * Cosmetic midpoint pixels (ignored by the decoder) fill the 1.5px gaps so
      grooves render as solid lines.

Header ring (radius 0.478*S, walked with the same 1.5px arc step):
    Repeated 26-byte records:
        <4s  magic   b"VREC"
         B   version 1 = 8-bit mu-law disc, 2 = 16-bit PCM disc
         B   flags   bit0 = mu-law, bit1 = 16-bit linear PCM
         I   sample_rate (Hz)
         I   n_samples
         H   pitch  * 1000
         H   step   * 1000
         H   f_outer* 10000     (data band outer radius as fraction of S)
         H   f_inner* 10000
         I   crc32 of the previous 22 bytes>
    The ring's radius depends only on image size, so a decoder can always find
    the header; everything else (pitch, step, band, length, rate) comes from
    the header itself.

IMPORTANT: the image must be shared losslessly (PNG). JPEG re-compression,
resizing or "enhancing" destroys the audio.
"""

from __future__ import annotations

import math
import struct
import wave
import zlib

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------- constants

MAGIC = b"VREC"
VERSION = 1                          # version byte for 8-bit mu-law discs
VERSION_PCM16 = 2                    # version byte for 16-bit PCM discs
MAX_VERSION = 2
FLAG_MULAW = 0x01
FLAG_PCM16 = 0x02
HEADER_FMT = "<4sBBIIHHHH"          # 22 bytes, + 4-byte CRC32 = 26
HEADER_SIZE = struct.calcsize(HEADER_FMT) + 4

F_DISC, F_HEADER = 0.495, 0.478
F_OUTER, F_INNER = 0.460, 0.225
F_LABEL, F_HOLE = 0.165, 0.012

DEFAULT_SIZE = 4096
DEFAULT_RATE = 22050
DEFAULT_PITCH = 2.0                  # px per revolution between grooves
DEFAULT_STEP = 1.5                   # px of arc per audio sample
MIN_RATE = 8000
MU = 255.0


# ------------------------------------------------------------------ helpers

def _round_half_up(v: np.ndarray) -> np.ndarray:
    """Deterministic half-up rounding (np.round does banker's rounding)."""
    return np.floor(v + 0.5).astype(np.int64)


def quantize_params(pitch: float, step: float) -> tuple[float, float]:
    """Quantize pitch/step exactly the way the header stores them."""
    return round(pitch * 1000) / 1000.0, round(step * 1000) / 1000.0


def spiral_capacity(size: int, pitch: float = DEFAULT_PITCH,
                    step: float = DEFAULT_STEP,
                    f_outer: float = F_OUTER, f_inner: float = F_INNER) -> int:
    """How many samples fit on a disc of this size."""
    pitch, step = quantize_params(pitch, step)
    ro, ri = f_outer * size, f_inner * size
    k = pitch / (2.0 * math.pi)
    return int((ro * ro - ri * ri) / (2.0 * k * step))


def spiral_xy(w: int, h: int, f_outer: float, f_inner: float,
              pitch: float, step: float, n: int | None = None,
              cx: float | None = None, cy: float | None = None,
              S: float | None = None):
    """Pixel coordinates for samples 0..n-1 along the spiral. Closed form.
    cx/cy/S default to the canvas (center of image, S = min side); pass them
    explicitly to decode discs that were cropped or padded."""
    S = min(w, h) if S is None else S
    cx = w / 2.0 if cx is None else cx
    cy = h / 2.0 if cy is None else cy
    ro, ri = f_outer * S, f_inner * S
    k = pitch / (2.0 * math.pi)
    cap = int((ro * ro - ri * ri) / (2.0 * k * step))
    n = cap if n is None else min(int(n), cap)
    s = np.arange(n, dtype=np.float64) * step
    r = np.sqrt(ro * ro - 2.0 * k * s)
    th = (ro - r) / k
    x = _round_half_up(cx + r * np.cos(th))
    y = _round_half_up(cy + r * np.sin(th))
    return x, y, r, th, cap


def ring_xy(w: int, h: int, radius: float, step: float = DEFAULT_STEP,
            cx: float | None = None, cy: float | None = None):
    """Pixel coordinates around a perfect circle (used for the header ring)."""
    cx = w / 2.0 if cx is None else cx
    cy = h / 2.0 if cy is None else cy
    n = int(2.0 * math.pi * radius / step)
    th = np.arange(n, dtype=np.float64) * (step / radius)
    x = _round_half_up(cx + radius * np.cos(th))
    y = _round_half_up(cy + radius * np.sin(th))
    return x, y


# ------------------------------------------------------------------- mu-law

def mulaw_encode(x: np.ndarray) -> np.ndarray:
    """float in [-1, 1]  ->  uint8 mu-law code."""
    x = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    y = np.sign(x) * np.log1p(MU * np.abs(x)) / np.log1p(MU)
    return _round_half_up((y + 1.0) * 127.5).clip(0, 255).astype(np.uint8)


def mulaw_decode(b: np.ndarray) -> np.ndarray:
    """uint8 mu-law code  ->  float in [-1, 1]."""
    y = b.astype(np.float64) / 127.5 - 1.0
    return np.sign(y) * ((1.0 + MU) ** np.abs(y) - 1.0) / MU


# ------------------------------------------------------------------- 16-bit

def pcm16_encode(x: np.ndarray) -> np.ndarray:
    """float in [-1, 1]  ->  uint16 offset-binary code (32768 = silence)."""
    x = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    q = _round_half_up(x * 32767.0).clip(-32767, 32767)
    return (q + 32768).astype(np.uint16)


def pcm16_decode(u: np.ndarray) -> np.ndarray:
    """uint16 offset-binary code  ->  float in [-1, 1]."""
    return (u.astype(np.float64) - 32768.0) / 32767.0


# ------------------------------------------------------------------- wav io

def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Read a PCM WAV (8/16/32-bit int) -> (mono float64 in [-1,1], rate)."""
    with wave.open(path, "rb") as wf:
        nch, sw, rate, n = (wf.getnchannels(), wf.getsampwidth(),
                            wf.getframerate(), wf.getnframes())
        raw = wf.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sw*8}-bit. "
                         "Please convert to 16-bit PCM.")
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a, rate


def write_wav(path: str, audio: np.ndarray, rate: int) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(pcm.tobytes())


def resample(x: np.ndarray, sr: int, target: int) -> np.ndarray:
    """Band-limited FFT resampling (numpy-only, exact brickwall)."""
    if sr == target or len(x) == 0:
        return x.astype(np.float64)
    m = int(round(len(x) * target / sr))
    X = np.fft.rfft(x)
    Y = np.zeros(m // 2 + 1, dtype=complex)
    c = min(len(X), len(Y))
    Y[:c] = X[:c]
    if c == len(Y) and m % 2 == 0:      # don't double-count Nyquist bin
        Y[-1] = Y[-1].real
    return np.fft.irfft(Y, m) * (m / len(x))


# ------------------------------------------------------------------- header

def pack_header(rate: int, n_samples: int, pitch: float, step: float,
                version: int = VERSION, flags: int = FLAG_MULAW) -> bytes:
    body = struct.pack(HEADER_FMT, MAGIC, version, flags,
                       int(rate), int(n_samples),
                       int(round(pitch * 1000)), int(round(step * 1000)),
                       int(round(F_OUTER * 10000)), int(round(F_INNER * 10000)))
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def parse_header(buf: bytes) -> dict | None:
    if len(buf) < HEADER_SIZE or buf[:4] != MAGIC:
        return None
    body = buf[:HEADER_SIZE - 4]
    (crc,) = struct.unpack("<I", buf[HEADER_SIZE - 4:HEADER_SIZE])
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        return None
    _, ver, flags, rate, n, pm, sm, fo, fi = struct.unpack(HEADER_FMT, body)
    return {"version": ver, "flags": flags, "rate": rate, "n_samples": n,
            "pitch": pm / 1000.0, "step": sm / 1000.0,
            "f_outer": fo / 10000.0, "f_inner": fi / 10000.0}


def _header_copies(gray: np.ndarray, cx: float | None, cy: float | None,
                   S: float) -> tuple[dict | None, int]:
    """Scan one candidate ring: (first parsed header, number of valid copies)."""
    h, w = gray.shape
    hx, hy = ring_xy(w, h, F_HEADER * S, cx=cx, cy=cy)
    hx = np.clip(hx, 0, w - 1)
    hy = np.clip(hy, 0, h - 1)
    stream = gray[hy, hx].tobytes()
    stream += stream[:HEADER_SIZE]
    first, copies = None, 0
    i = stream.find(MAGIC)
    while i != -1:
        hdr = parse_header(stream[i:i + HEADER_SIZE])
        if hdr is not None:
            copies += 1
            if first is None:
                first = hdr
        i = stream.find(MAGIC, i + 1)
    return first, copies


def find_header(gray: np.ndarray, cx: float | None = None,
                cy: float | None = None, S: float | None = None,
                min_copies: int = 3) -> dict | None:
    """Read the header ring and scan its redundant copies.

    The ring carries ~300 identical records; requiring several matching
    copies (min_copies) rejects geometry guesses whose circle merely grazes
    the true ring and catches one record by luck."""
    h, w = gray.shape
    S = min(w, h) if S is None else S
    hdr, copies = _header_copies(gray, cx, cy, S)
    return hdr if copies >= min_copies else None


def find_geometry(gray: np.ndarray, alpha: np.ndarray | None = None):
    """Locate the disc and its header even if the PNG was cropped or padded
    (losslessly) with the whole disc still visible.

    Returns (hdr, cx, cy, S) or None. Tries canvas geometry first, then
    derives the disc from its bounding box (alpha if present, else
    luminance) and refines with a small search."""
    h, w = gray.shape
    hdr = find_header(gray)
    if hdr is not None:
        return hdr, w / 2.0, h / 2.0, float(min(w, h))

    if alpha is not None and bool(alpha.any()) and not bool(alpha.all()):
        mask = alpha > 0
    else:
        mask = gray >= 8
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    cx0 = (x0 + x1 + 1) / 2.0
    cy0 = (y0 + y1 + 1) / 2.0
    d = max(x1 - x0 + 1, y1 - y0 + 1)          # disc pixel diameter
    S0 = int(round(d / (2.0 * F_DISC)))        # encoder sizes are integers

    # Exact geometry matters: a half-pixel center error flips rounded data
    # pixels. The true geometry yields ~all header copies, near-misses only
    # a few -- so scan a small grid and keep the candidate with the MOST
    # valid copies (early exit once a clear winner appears).
    best = (0, None, 0.0, 0.0, 0.0)
    expected = max(6, int(2.0 * math.pi * F_HEADER * S0
                          / DEFAULT_STEP / HEADER_SIZE))
    for dS in (0, 1, -1, 2, -2):
        for dx in (0.0, 0.5, -0.5, 1.0, -1.0):
            for dy in (0.0, 0.5, -0.5, 1.0, -1.0):
                cand = (cx0 + dx, cy0 + dy, float(S0 + dS))
                hdr, copies = _header_copies(gray, *cand)
                if hdr is not None and copies > best[0]:
                    best = (copies, hdr, *cand)
                    if copies >= expected // 2:
                        return best[1], best[2], best[3], best[4]
    if best[0] >= 3:
        return best[1], best[2], best[3], best[4]
    return None


# --------------------------------------------------------------- rendering

def _vinyl_background(w: int, h: int, seed: int = 20260612):
    """Near-black disc with two soft sheen highlights and per-ring noise.
    Painted FIRST, so the data spiral overwrites it; the sheen survives only
    in the gaps between grooves -> the disc looks glossy, the data stays exact."""
    S = min(w, h)
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    dx, dy = xx - w / 2.0, yy - h / 2.0
    R = np.hypot(dx, dy)
    disc = R <= F_DISC * S
    g = np.where(disc, 15.0, 0.0)
    ang = np.arctan2(dy, dx)
    for a0 in (2.35, -0.79):                                   # two highlights
        d = np.angle(np.exp(1j * (ang - a0)))
        g += np.where(disc, 34.0 * np.exp(-(d * d) / (2 * 0.30 ** 2)), 0.0) \
             * np.clip(R / (F_DISC * S), 0.0, 1.0)
    ring_noise = rng.normal(0.0, 2.4, int(F_DISC * S) + 3)
    g += np.where(disc, ring_noise[np.minimum(R.astype(np.int64),
                                              len(ring_noise) - 1)], 0.0)
    return np.clip(g, 0, 255).astype(np.uint8), R, disc


def _load_font(px: int) -> ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "DejaVuSans-Bold.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def _draw_label(im: Image.Image, title: str, artist: str | None,
                rate: int, duration: float, depth: str = "8-BIT \u03bc-LAW",
                color=(173, 44, 38), text_color=(245, 233, 205)) -> None:
    S = min(im.size)
    cx, cy = im.size[0] / 2.0, im.size[1] / 2.0
    d = ImageDraw.Draw(im)
    rl = F_LABEL * S
    lw = max(2, S // 500)

    d.ellipse([cx - rl, cy - rl, cx + rl, cy + rl], fill=color + (255,))
    d.ellipse([cx - rl, cy - rl, cx + rl, cy + rl],
              outline=(232, 210, 160, 255), width=lw)
    r2 = rl * 0.94
    d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2],
              outline=(232, 210, 160, 180), width=max(1, lw // 2))

    def text(t, y, px, fill=text_color):
        f = _load_font(px)
        try:
            d.text((cx, y), t, font=f, fill=fill + (255,), anchor="mm")
        except TypeError:                       # very old Pillow: no anchor
            bb = d.textbbox((0, 0), t, font=f)
            d.text((cx - (bb[2] - bb[0]) / 2, y - (bb[3] - bb[1]) / 2),
                   t, font=f, fill=fill + (255,))

    title = (title or "UNTITLED").upper()
    lines = [title]
    if len(title) > 16:                          # naive 2-line wrap
        words, a, b = title.split(), "", ""
        for wd in words:
            if len(a) + len(wd) < max(10, len(title) // 2):
                a += (" " if a else "") + wd
            else:
                b += (" " if b else "") + wd
        lines = [a, b] if b else [a]
    tpx = int(S * (0.030 if len(lines) == 1 else 0.026))
    ty = cy - rl * 0.30
    for i, ln in enumerate(lines):
        text(ln, ty + i * tpx * 1.25, tpx)
    if artist:
        text(artist.upper(), cy - rl * 0.62, int(S * 0.018))
    text("33\u2153 RPM  \u00b7  MICROGROOVE", cy + rl * 0.40, int(S * 0.015))
    m, s = divmod(int(round(duration)), 60)
    text(f"{m}:{s:02d}  \u00b7  {rate} Hz  \u00b7  {depth}",
         cy + rl * 0.56, int(S * 0.013))
    text("VREC \u00b7 SIDE A", cy + rl * 0.72, int(S * 0.012),
         fill=(232, 210, 160))

    rh = F_HOLE * S                              # spindle hole
    d.ellipse([cx - rh * 1.6, cy - rh * 1.6, cx + rh * 1.6, cy + rh * 1.6],
              fill=(20, 16, 14, 255))
    d.ellipse([cx - rh, cy - rh, cx + rh, cy + rh], fill=(0, 0, 0, 0))


# ------------------------------------------------------------------ encode

def fit_plan(duration_s: float, rate: int, capacity: int) -> tuple[int, bool]:
    """Shared fit logic: returns (use_rate, trimmed). Lowers the rate down to
    MIN_RATE before resorting to trimming. Used by encode() and the GUI."""
    use_rate = int(rate)
    trimmed = False
    if duration_s > 0 and duration_s * use_rate > capacity:
        use_rate = max(MIN_RATE, min(use_rate, int(capacity / duration_s)))
        if duration_s * use_rate > capacity:
            trimmed = True
    return use_rate, trimmed


def encode(audio: np.ndarray, sr: int, out_png: str, title: str = "",
           artist: str | None = None, size: int = DEFAULT_SIZE,
           rate: int = DEFAULT_RATE, pitch: float = DEFAULT_PITCH,
           step: float = DEFAULT_STEP, bits: int = 8,
           label_color=(173, 44, 38)) -> dict:
    """Encode a mono float waveform into a vinyl-record PNG. Returns info.

    bits=8  -> VREC v1, 8-bit mu-law (max playtime, lo-fi warmth)
    bits=16 -> VREC v2, 16-bit linear PCM (CD-quality depth, same capacity
               in samples; pair with rate=44100 for full CD quality)
    """
    if bits not in (8, 16):
        raise ValueError("bits must be 8 or 16")
    pitch, step = quantize_params(pitch, step)
    if pitch < 2.0:
        raise ValueError("pitch must be >= 2.0 px so grooves cannot collide")
    if step < 1.415:
        raise ValueError("step must be >= 1.415 px so consecutive samples "
                         "cannot share a pixel")
    w = h = int(size)
    S = min(w, h)
    cap = spiral_capacity(S, pitch, step)
    duration_in = len(audio) / sr if sr else 0.0

    # ---- fit: lower the sample rate (>= MIN_RATE) before trimming audio
    use_rate, trimmed = fit_plan(duration_in, rate, cap)
    a = resample(audio, sr, use_rate)
    if len(a) > cap:
        a = a[:cap]
    peak = np.max(np.abs(a)) if len(a) else 0.0
    if peak > 0:
        a = a * (0.95 / peak)

    if bits == 16:
        codes = pcm16_encode(a)
        hi = (codes >> 8).astype(np.uint8)
        version, flags = VERSION_PCM16, FLAG_PCM16
        depth_text = "16-BIT PCM"
    else:
        codes = mulaw_encode(a)
        hi = codes
        version, flags = VERSION, FLAG_MULAW
        depth_text = "8-BIT \u03bc-LAW"
    n = len(codes)

    # ---- canvas; mark sample pixels, then cosmetics, then data
    g, R, disc = _vinyl_background(w, h)
    x, y, _, _, _ = spiral_xy(w, h, F_OUTER, F_INNER, pitch, step, n)
    occupied = np.zeros((h, w), dtype=bool)
    occupied[y, x] = True

    # ---- cosmetic midpoints (decoder never reads these; brightness only)
    if n > 1:
        k = pitch / (2.0 * math.pi)
        ro = F_OUTER * S
        sm = (np.arange(n - 1, dtype=np.float64) + 0.5) * step
        rm = np.sqrt(ro * ro - 2.0 * k * sm)
        tm = (ro - rm) / k
        xm = _round_half_up(w / 2.0 + rm * np.cos(tm))
        ym = _round_half_up(h / 2.0 + rm * np.sin(tm))
        vm = ((hi[:-1].astype(np.uint16) + hi[1:]) // 2).astype(np.uint8)
        free = ~occupied[ym, xm]
        g[ym[free], xm[free]] = vm[free]

    # ---- header ring (lead-in groove)
    hx, hy = ring_xy(w, h, F_HEADER * S)
    hdr = pack_header(use_rate, n, pitch, step, version, flags)
    reps = (hdr * (len(hx) // len(hdr) + 2))[:len(hx)]
    g[hy, hx] = np.frombuffer(reps, dtype=np.uint8)

    # ---- write data pixels (per channel for 16-bit), compose, label, save
    if bits == 16:
        rch, bch = g.copy(), g.copy()
        lo = (codes & 0xFF).astype(np.uint8)
        g[y, x] = hi                                  # G = high byte
        rch[y, x] = (hi & 0xF0) | (lo >> 4)           # R low nibble = lo hi
        bch[y, x] = (hi & 0xF0) | (lo & 0x0F)         # B low nibble = lo lo
    else:
        g[y, x] = hi
        rch = bch = g

    alpha = np.where(disc, 255, 0).astype(np.uint8)
    rgba = np.dstack([rch, g, bch, alpha])
    im = Image.fromarray(rgba, "RGBA")
    _draw_label(im, title, artist, use_rate, n / use_rate,
                depth=depth_text, color=label_color)
    im.save(out_png, optimize=True)

    return {"png": out_png, "size": S, "rate": use_rate, "n_samples": n,
            "duration": n / use_rate, "capacity": cap,
            "fill": n / cap, "pitch": pitch, "step": step, "bits": bits,
            "rate_reduced": use_rate < rate, "trimmed": trimmed}


def encode_wav(wav_path: str, out_png: str, **kw) -> dict:
    audio, sr = read_wav(wav_path)
    return encode(audio, sr, out_png, **kw)


# ------------------------------------------------------------------ decode

def decode_record(png_path: str):
    """Decode a record PNG -> (audio float64, rate, header, raw codes).

    Raw codes are uint8 mu-law for v1 discs, uint16 PCM for v2 discs.
    Tolerates lossless crops/padding as long as the whole disc is visible.
    """
    im = Image.open(png_path).convert("RGBA")
    arr = np.asarray(im)
    gray = arr[:, :, 1]                          # header + v1 data live in G
    h, w = gray.shape
    geo = find_geometry(gray, arr[:, :, 3])
    if geo is None:
        raise ValueError(
            "No valid VREC header found. Either this image is not a "
            "virtual record, it was resized / re-compressed (e.g. saved "
            "as JPEG), or it was cropped into the disc itself. Lossless "
            "crops are fine as long as the WHOLE disc stays visible.")
    hdr, cx, cy, S = geo
    if hdr["version"] > MAX_VERSION:
        raise ValueError(
            f"This disc is VREC v{hdr['version']}, made by a newer encoder "
            f"than this decoder (max v{MAX_VERSION}). Update vinyl_codec.py.")
    x, y, _, _, _ = spiral_xy(w, h, hdr["f_outer"], hdr["f_inner"],
                              hdr["pitch"], hdr["step"], hdr["n_samples"],
                              cx=cx, cy=cy, S=S)
    x = np.clip(x, 0, w - 1)
    y = np.clip(y, 0, h - 1)
    if hdr["flags"] & FLAG_PCM16:
        hi = arr[y, x, 1].astype(np.uint16)
        lo = ((arr[y, x, 0] & 0x0F) << 4) | (arr[y, x, 2] & 0x0F)
        codes = (hi << 8) | lo
        audio = pcm16_decode(codes)
    elif hdr["flags"] & FLAG_MULAW:
        codes = gray[y, x]
        audio = mulaw_decode(codes)
    else:
        codes = gray[y, x]
        audio = codes.astype(np.float64) / 127.5 - 1.0
    return audio, hdr["rate"], hdr, codes


# ------------------------------------------------------- live optical pickup

class GrooveReader:
    """A virtual needle that reads the IMAGE in real time.

    Nothing is pre-decoded: every call to read() computes which pixels lie
    under the requested groove positions (closed-form spiral) and decodes
    those pixels on the spot. Draw on the image array (stamp()) while it
    plays and you will hear it on the next pass -- exactly like scratching
    a real record.
    """

    def __init__(self, png_path: str):
        im = Image.open(png_path).convert("RGBA")
        self.rgba = np.array(im)                 # writable H x W x 4
        gray = self.rgba[:, :, 1]
        geo = find_geometry(gray, self.rgba[:, :, 3])
        if geo is None:
            raise ValueError(
                "No valid VREC header found. Either this image is not a "
            "virtual record, it was resized / re-compressed (e.g. saved "
            "as JPEG), or it was cropped into the disc itself. Lossless "
            "crops are fine as long as the WHOLE disc stays visible.")
        self.hdr, self.cx, self.cy, self.S = geo
        if self.hdr["version"] > MAX_VERSION:
            raise ValueError(
                f"This disc is VREC v{self.hdr['version']}; this decoder "
                f"reads up to v{MAX_VERSION}. Update vinyl_codec.py.")
        self.h, self.w = gray.shape
        self.n = self.hdr["n_samples"]
        self.rate = self.hdr["rate"]
        self.step = self.hdr["step"]
        self.k = self.hdr["pitch"] / (2.0 * math.pi)
        self.ro = self.hdr["f_outer"] * self.S
        self.is16 = bool(self.hdr["flags"] & FLAG_PCM16)
        self._lut = mulaw_decode(np.arange(256)).astype(np.float32)

    def _vals(self, i: np.ndarray) -> np.ndarray:
        """Decoded float32 values at integer sample indices (live pixels)."""
        s = i.astype(np.float64) * self.step
        r = np.sqrt(np.maximum(self.ro * self.ro - 2.0 * self.k * s, 0.0))
        th = (self.ro - r) / self.k
        x = np.floor(self.cx + r * np.cos(th) + 0.5).astype(np.int64)
        y = np.floor(self.cy + r * np.sin(th) + 0.5).astype(np.int64)
        np.clip(x, 0, self.w - 1, out=x)
        np.clip(y, 0, self.h - 1, out=y)
        if self.is16:
            hi = self.rgba[y, x, 1].astype(np.uint16)
            lo = ((self.rgba[y, x, 0] & 0x0F) << 4) \
                | (self.rgba[y, x, 2] & 0x0F)
            u = (hi << 8) | lo
            return (((u.astype(np.float64)) - 32768.0)
                    / 32767.0).astype(np.float32)
        return self._lut[self.rgba[y, x, 1]]

    def read(self, idx: np.ndarray) -> np.ndarray:
        """Audio at fractional groove positions idx (linear interpolation)."""
        ii = np.clip(idx, 0.0, self.n - 1.0)
        i0 = np.minimum(ii.astype(np.int64), self.n - 2)
        fr = (ii - i0).astype(np.float32)
        return self._vals(i0) * (1.0 - fr) + self._vals(i0 + 1) * fr

    def stamp(self, x0: float, y0: float, x1: float, y1: float,
              width: int = 3, value: int = 255) -> bool:
        """Scratch the record: write a line of damaged pixels into the data
        band (image coordinates). The header ring and label are protected.
        Returns True if any pixel was written."""
        num = int(math.hypot(x1 - x0, y1 - y0)) + 1
        xs = np.linspace(x0, x1, num)
        ys = np.linspace(y0, y1, num)
        rr = np.hypot(xs - self.cx, ys - self.cy)
        lo_r = self.hdr["f_inner"] * self.S - 4.0
        hi_r = self.hdr["f_outer"] * self.S + 4.0
        m = (rr > lo_r) & (rr < hi_r)
        if not m.any():
            return False
        xi = np.rint(xs[m]).astype(np.int64)
        yi = np.rint(ys[m]).astype(np.int64)
        half = max(0, width // 2)
        for dx in range(-half, width - half):
            for dy in range(-half, width - half):
                xx = np.clip(xi + dx, 0, self.w - 1)
                yy = np.clip(yi + dy, 0, self.h - 1)
                self.rgba[yy, xx, 0:3] = value
        return True

    def save(self, path: str) -> None:
        """Save the record, damage and all, as a playable PNG."""
        Image.fromarray(self.rgba, "RGBA").save(path)


# ------------------------------------------------- playback-geometry helpers

def groove_radius(hdr: dict, S: int, pos: float) -> float:
    """Groove radius (px) at fractional sample position `pos`."""
    ro = hdr["f_outer"] * S
    k = hdr["pitch"] / (2.0 * math.pi)
    v = ro * ro - 2.0 * k * hdr["step"] * max(0.0, pos)
    return math.sqrt(max(v, (hdr["f_inner"] * S) ** 2 * 0.25))


def groove_theta(hdr: dict, S: int, pos: float) -> float:
    """Spiral angle (rad) at fractional sample position `pos`."""
    ro = hdr["f_outer"] * S
    k = hdr["pitch"] / (2.0 * math.pi)
    return (ro - groove_radius(hdr, S, pos)) / k
