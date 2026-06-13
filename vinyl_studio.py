#!/usr/bin/env python3
"""
vinyl_studio.py — the VREC app suite. No command line needed: just run it.

    python vinyl_studio.py          (or double-click it)

HOME      pick what to do
PRESS     browse for a WAV, type a title, click options, press the record
DECK      a turntable: the disc visibly spins, the needle tracks the groove,
          everything is a clickable button (and the old keyboard shortcuts
          still work). Drag the disc to scratch.

You can also drag & drop a .wav (to press) or a record .png (to play)
straight onto the window at any time.

File dialogs use your OS's native picker (tkinter); if tkinter isn't
installed, a built-in browser screen takes over, so the app always works.
"""

from __future__ import annotations

import math
import os
import sys
import threading

import numpy as np

try:
    import pygame
except ImportError:
    sys.exit("pygame is required:  pip install pygame")
try:
    import sounddevice as sd
except ImportError:
    sys.exit("sounddevice is required:  pip install sounddevice")

from PIL import Image

import vinyl_codec as vc

# ----------------------------------------------------------------- look & feel
WIN_W, WIN_H = 1080, 720
BG = (24, 22, 21)
DECK_COL = (38, 33, 30)
PANEL = (45, 40, 36)
TEXT = (232, 224, 209)
DIM = (150, 141, 128)
FAINT = (95, 88, 80)
ACCENT = (205, 92, 60)
GOOD = (118, 168, 100)
WARN = (214, 160, 70)
BAD = (210, 90, 80)

SCRATCH_SIGN = +1.0          # flip to -1.0 if scratching feels inverted
SCRATCH_MAX_SPEED = 48.0

LABEL_COLORS = [("CLASSIC RED", (173, 44, 38)), ("MIDNIGHT", (30, 38, 58)),
                ("FOREST", (28, 74, 52)), ("GOLD", (160, 122, 36)),
                ("PLUM", (88, 40, 92)), ("CHARCOAL", (40, 38, 36))]
SIZES = [2048, 4096, 6144, 8192]
QUALITIES = [("CLASSIC  8-BIT \u00b7 22 kHz", 8, 22050),
             ("CD  16-BIT \u00b7 44.1 kHz", 16, 44100)]


def fmt_time(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def native_dialog(mode: str, **kw):
    """(path|None, dialog_worked). dialog_worked=False -> use in-app browser."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None, False
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        if mode == "open":
            p = filedialog.askopenfilename(**kw)
        else:
            p = filedialog.asksaveasfilename(**kw)
        root.update()
        root.destroy()
        return (p or None), True
    except Exception:
        return None, False


# ------------------------------------------------------------------- widgets
class Button:
    def __init__(self, rect, label, cb, *, kind="normal", enabled=True):
        self.rect = pygame.Rect(rect)
        self.label = label            # str or callable -> str
        self.cb = cb
        self.kind = kind              # normal | primary | ghost
        self.enabled = enabled        # bool or callable -> bool
        self.selected = False         # bool or callable -> bool

    def _on(self, v):
        return v() if callable(v) else v

    def draw(self, surf, fonts):
        en = self._on(self.enabled)
        sel = self._on(self.selected)
        hov = en and self.rect.collidepoint(pygame.mouse.get_pos())
        if self.kind == "primary":
            base = ACCENT if en else (90, 60, 50)
            col = tuple(min(255, c + (22 if hov else 0)) for c in base)
            pygame.draw.rect(surf, col, self.rect, border_radius=10)
            tcol = (28, 20, 18) if en else (60, 48, 44)
        else:
            col = (66, 58, 52) if (hov or sel) else (52, 46, 41)
            if not en:
                col = (42, 38, 35)
            pygame.draw.rect(surf, col, self.rect, border_radius=10)
            if sel:
                pygame.draw.rect(surf, ACCENT, self.rect, 2, border_radius=10)
            tcol = TEXT if en else FAINT
        t = fonts["ui"].render(self._on(self.label), True, tcol)
        surf.blit(t, t.get_rect(center=self.rect.center))

    def click(self, pos) -> bool:
        if self._on(self.enabled) and self.rect.collidepoint(pos):
            self.cb()
            return True
        return False


class TextInput:
    def __init__(self, rect, placeholder="", text=""):
        self.rect = pygame.Rect(rect)
        self.text = text
        self.placeholder = placeholder
        self.focus = False

    def draw(self, surf, fonts, tick):
        pygame.draw.rect(surf, (30, 27, 25), self.rect, border_radius=8)
        pygame.draw.rect(surf, ACCENT if self.focus else (70, 63, 56),
                         self.rect, 2, border_radius=8)
        shown = self.text if (self.text or self.focus) else self.placeholder
        col = TEXT if (self.text or self.focus) else FAINT
        t = fonts["ui"].render(shown, True, col)
        clip = surf.get_clip()
        surf.set_clip(self.rect.inflate(-16, -8))
        x = self.rect.x + 12
        if t.get_width() > self.rect.w - 24:          # keep caret in view
            x = self.rect.right - 12 - t.get_width()
        surf.blit(t, (x, self.rect.centery - t.get_height() // 2))
        if self.focus and (tick // 30) % 2 == 0:
            cx = min(self.rect.right - 10,
                     x + fonts["ui"].size(self.text)[0] + 2)
            pygame.draw.line(surf, TEXT, (cx, self.rect.y + 9),
                             (cx, self.rect.bottom - 9), 2)
        surf.set_clip(clip)

    def key(self, e):
        if not self.focus:
            return
        if e.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
        elif e.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE,
                       pygame.K_TAB):
            self.focus = False

    def type(self, s: str):
        if self.focus and s and s.isprintable():
            self.text += s


class Slider:
    def __init__(self, rect, lo, hi, val):
        self.rect = pygame.Rect(rect)
        self.lo, self.hi, self.val = lo, hi, val
        self.drag = False

    def draw(self, surf):
        y = self.rect.centery
        pygame.draw.rect(surf, (55, 50, 46),
                         (self.rect.x, y - 3, self.rect.w, 6), border_radius=3)
        f = (self.val - self.lo) / (self.hi - self.lo)
        kx = self.rect.x + int(f * self.rect.w)
        pygame.draw.rect(surf, ACCENT, (self.rect.x, y - 3, kx - self.rect.x, 6),
                         border_radius=3)
        pygame.draw.circle(surf, (230, 222, 208), (kx, y), 9)
        pygame.draw.circle(surf, (60, 50, 44), (kx, y), 9, 2)

    def handle(self, e) -> bool:
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 \
                and self.rect.inflate(0, 18).collidepoint(e.pos):
            self.drag = True
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            self.drag = False
        elif e.type == pygame.MOUSEMOTION and self.drag:
            pass
        else:
            return False
        if self.drag:
            f = (pygame.mouse.get_pos()[0] - self.rect.x) / self.rect.w
            self.val = self.lo + (self.hi - self.lo) * min(1.0, max(0.0, f))
            return True
        return False


# --------------------------------------------------------------- audio deck
class Deck:
    """LIVE audio engine: the callback decodes pixels straight off the
    record image (vinyl_codec.GrooveReader) as the needle reaches them."""

    def __init__(self, reader: "vc.GrooveReader"):
        self.reader = reader
        self.n = reader.n
        self.rate = int(reader.rate)
        self.hdr = reader.hdr
        self.img_S = reader.S
        self.pos = 0.0
        self.vel = 0.0
        self.speed_mult = 1.0
        self.volume = 0.9
        self.playing = False
        self.loop = False
        self.scratch = False
        self.scratch_pos = 0.0

        self.out_rate = self.rate
        try:
            self.stream = sd.OutputStream(samplerate=self.rate, channels=1,
                                          dtype="float32",
                                          callback=self._callback)
        except Exception:
            dev = sd.query_devices(kind="output")
            self.out_rate = int(dev["default_samplerate"])
            self.stream = sd.OutputStream(samplerate=self.out_rate, channels=1,
                                          dtype="float32",
                                          callback=self._callback)
        self.base_speed = self.rate / self.out_rate
        self.kp = 1.0 / (0.045 * self.out_rate)
        self.stream.start()

    # geometry helpers
    def r_of(self, pos):
        return vc.groove_radius(self.hdr, self.img_S, pos)

    def th_of(self, pos):
        return vc.groove_theta(self.hdr, self.img_S, pos)

    def _callback(self, out, frames, time_info, status):
        if self.scratch:
            target_v = float(np.clip((self.scratch_pos - self.pos) * self.kp,
                                     -SCRATCH_MAX_SPEED, SCRATCH_MAX_SPEED))
        elif self.playing:
            target_v = self.base_speed * self.speed_mult
        else:
            target_v = 0.0
        v1 = self.vel + (target_v - self.vel) * 0.25
        sp = np.linspace(self.vel, v1, frames, dtype=np.float64)
        idx = self.pos + np.cumsum(sp)
        self.vel = v1
        self.pos = float(idx[-1])
        out[:, 0] = self.reader.read(idx) * self.volume   # live pixels
        if not self.scratch:
            if self.pos >= self.n - 2:
                if self.loop and self.playing:
                    self.pos = 0.0
                else:
                    self.pos = float(self.n - 2)
                    self.playing = False
            elif self.pos < 0:
                self.pos = 0.0

    # transport
    def toggle_play(self):
        if not self.playing and self.pos >= self.n - 3:
            self.pos = 0.0
        self.playing = not self.playing

    def seek(self, ds: float):
        self.pos = float(np.clip(self.pos + ds * self.rate, 0.0, self.n - 2.0))

    def seek_frac(self, f: float):
        self.pos = float(np.clip(f, 0.0, 1.0)) * (self.n - 2.0)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


def disc_display(reader: "vc.GrooveReader", disp_px: int):
    """Square RGBA view centred on the disc + (origin, scale) so on-screen
    marks can be mapped back into record-image coordinates."""
    src_side = int(round(2.0 * vc.F_DISC * reader.S)) + 2
    half = src_side // 2
    canvas = np.zeros((src_side, src_side, 4), dtype=np.uint8)
    x0 = int(round(reader.cx)) - half
    y0 = int(round(reader.cy)) - half
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1 = min(reader.w, x0 + src_side)
    sy1 = min(reader.h, y0 + src_side)
    canvas[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = reader.rgba[sy0:sy1,
                                                               sx0:sx1]
    side = max(8, int(round(src_side * disp_px / (2.0 * vc.F_DISC
                                                  * reader.S))))
    im = Image.fromarray(canvas, "RGBA").resize((side, side), Image.LANCZOS)
    f = side / src_side
    return im.tobytes(), im.size, x0, y0, f


# -------------------------------------------------------------------- studio
class Studio:
    def __init__(self):
        pygame.init()
        pygame.key.set_repeat(320, 38)
        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        pygame.display.set_caption("VREC Studio By Joel Lagace — virtual vinyl")
        self.clock = pygame.time.Clock()
        self.fonts = {
            "big": pygame.font.SysFont("dejavusans,segoeui,arial", 46, bold=True),
            "h2": pygame.font.SysFont("dejavusans,segoeui,arial", 24, bold=True),
            "ui": pygame.font.SysFont("dejavusans,segoeui,arial", 18),
            "mono": pygame.font.SysFont(
                "dejavusansmono,consolas,menlo,monospace", 16),
            "small": pygame.font.SysFont("dejavusans,segoeui,arial", 14),
        }
        self.tick = 0
        self.state = "home"            # home | press | deck | browse
        self.status = ""               # transient message (errors etc.)
        self.status_col = DIM

        # press-screen state
        self.wav_path = None
        self.wav_dur = 0.0
        self.wav_rate = 0
        self.out_path = None
        self.in_title = TextInput((70, 218, 430, 42), "record title")
        self.in_artist = TextInput((70, 296, 430, 42), "artist (optional)")
        self.size_i = 1
        self.quality_i = 0
        self.color_i = 0
        self.pressing = False
        self.press_result = None       # info dict after a successful press
        self.preview = None            # pygame surface of the pressed disc

        # deck state
        self.deck: Deck | None = None
        self.marker = False
        self._mk_prev = None
        self.record_surf = None
        self.record_path = ""
        self.loading = False
        self.load_error = ""
        self._jobs = []

        # in-app browser state
        self.br = None                 # dict while browsing

        self._build_buttons()

    # ------------------------------------------------------------- buttons
    def _build_buttons(self):
        cx = WIN_W // 2
        self.b_home = [
            Button((cx - 190, 300, 380, 58), "OPEN A RECORD  (.png)",
                   lambda: self.pick_record(), kind="primary"),
            Button((cx - 190, 376, 380, 58), "PRESS A NEW RECORD  (.wav)",
                   lambda: self.goto_press()),
            Button((cx - 190, 452, 380, 58), "QUIT", self.quit),
        ]

        y = 132
        self.b_press = [
            Button((70, y, 200, 44), "BROWSE WAV\u2026", self.pick_wav),
            Button((70, 560, 430, 54),
                   lambda: "PRESSING\u2026" if self.pressing else "PRESS RECORD",
                   self.do_press, kind="primary",
                   enabled=lambda: (self.wav_path is not None
                                    and not self.pressing)),
            Button((70, 632, 130, 40), "BACK", lambda: self.goto("home")),
            Button((210, 632, 200, 40), "SAVE AS\u2026", self.pick_out,
                   enabled=lambda: self.wav_path is not None),
        ]
        self.b_size = []
        for i, s in enumerate(SIZES):
            b = Button((70 + i * 110, 392, 100, 40), f"{s}",
                       (lambda i=i: self.set_size(i)))
            b.selected = (lambda i=i: self.size_i == i)
            self.b_size.append(b)
        self.b_quality = []
        for i, (lab, _, _) in enumerate(QUALITIES):
            b = Button((70 + i * 222, 462, 212, 40), lab,
                       (lambda i=i: self.set_quality(i)))
            b.selected = (lambda i=i: self.quality_i == i)
            self.b_quality.append(b)
        self.b_play_now = Button((600, 560, 220, 54), "PLAY IT NOW",
                                 self.play_pressed, kind="primary")

        # deck controls (right panel)
        px = 716
        self.b_deck = [
            Button((px, 300, 168, 48),
                   lambda: "PAUSE" if (self.deck and self.deck.playing)
                   else "PLAY",
                   lambda: self.deck and self.deck.toggle_play(),
                   kind="primary"),
            Button((px + 178, 300, 84, 48), "RESTART",
                   lambda: self.deck and setattr(self.deck, "pos", 0.0)),
            Button((px, 358, 80, 40), "-5 s",
                   lambda: self.deck and self.deck.seek(-5)),
            Button((px + 90, 358, 80, 40), "+5 s",
                   lambda: self.deck and self.deck.seek(+5)),
            Button((px + 180, 358, 82, 40),
                   lambda: "LOOP \u25cf" if (self.deck and self.deck.loop)
                   else "LOOP \u25cb",
                   lambda: self.deck and setattr(self.deck, "loop",
                                                 not self.deck.loop)),
            Button((px, 470, 80, 36), "33\u2153",
                   lambda: self.set_speed(1.0)),
            Button((px + 90, 470, 80, 36), "45",
                   lambda: self.set_speed(1.35)),
            Button((px, 580, 128, 40),
                   lambda: "MARKER \u270e ON" if self.marker else
                   "MARKER \u270e",
                   self.toggle_marker),
            Button((px + 138, 580, 124, 40), "SAVE COPY\u2026",
                   self.save_scratched),
            Button((px, 630, 100, 40), "EJECT", self.eject),
            Button((px + 110, 630, 152, 40), "OPEN\u2026", self.pick_record),
        ]
        self.sl_speed = Slider((px, 446, 262, 14), 0.5, 1.5, 1.0)
        self.sl_vol = Slider((px, 540, 262, 14), 0.0, 1.0, 0.9)
        self.progress_rect = pygame.Rect(px, 252, 262, 10)

    # ------------------------------------------------------------ nav/state
    def goto(self, state):
        self.state = state
        self.status = ""

    def goto_press(self):
        self.press_result = None
        self.preview = None
        self.goto("press")

    def set_size(self, i):
        self.size_i = i

    def set_quality(self, i):
        self.quality_i = i

    def toggle_marker(self):
        self.marker = not self.marker
        self._mk_prev = None
        if self.deck:
            self.deck.scratch = False

    def save_scratched(self):
        if not self.deck:
            return
        init = os.path.splitext(os.path.basename(self.record_path))[0] \
            + "_scratched.png"
        p, ok = native_dialog("save", title="Save scratched copy as\u2026",
                              defaultextension=".png", initialfile=init,
                              filetypes=[("PNG image", "*.png")])
        if not ok:
            self.open_browser("save", self._do_save_scratched)
        elif p:
            self._do_save_scratched(p)

    def _do_save_scratched(self, path):
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            self.deck.reader.save(path)
            self.say("Saved scratched copy \u2014 pops and all.", GOOD)
        except Exception as e:
            self.say(f"Save failed: {e}", BAD)

    MARKER_ROT_SIGN = +1.0   # flip if drawn marks land rotated oddly

    def screen_to_record(self, mx, my, cx, cy):
        """Map a screen point to record-image coordinates, undoing the
        current platter rotation and display scaling."""
        dx, dy = mx - cx, my - cy
        A = math.radians(self._cur_angle) * self.MARKER_ROT_SIGN
        rx = dx * math.cos(A) + dy * math.sin(A)
        ry = -dx * math.sin(A) + dy * math.cos(A)
        rd = self.deck.reader
        return (rd.cx + rx / self.disp_scale,
                rd.cy + ry / self.disp_scale)

    def set_speed(self, v):
        if self.deck:
            self.deck.speed_mult = v
        self.sl_speed.val = v

    def say(self, msg, col=DIM):
        self.status, self.status_col = msg, col

    def quit(self):
        if self.deck:
            self.deck.close()
        pygame.quit()
        sys.exit(0)

    # ------------------------------------------------------------- file io
    def pick_wav(self):
        p, ok = native_dialog("open", title="Choose a WAV to press",
                              filetypes=[("WAV audio", "*.wav")])
        if not ok:
            self.open_browser("wav", self.set_wav)
        elif p:
            self.set_wav(p)

    def pick_record(self):
        p, ok = native_dialog("open", title="Choose a VREC record PNG",
                              filetypes=[("VREC record", "*.png")])
        if not ok:
            self.open_browser("png", self.load_record)
        elif p:
            self.load_record(p)

    def pick_out(self):
        init = os.path.basename(self.out_path or "record.png")
        p, ok = native_dialog("save", title="Save record as\u2026",
                              defaultextension=".png",
                              initialfile=init,
                              filetypes=[("PNG image", "*.png")])
        if not ok:
            self.open_browser("save", self.set_out)
        elif p:
            self.set_out(p)

    def set_wav(self, path):
        import wave
        try:
            with wave.open(path, "rb") as wf:
                self.wav_rate = wf.getframerate()
                self.wav_dur = wf.getnframes() / max(1, self.wav_rate)
        except Exception as e:
            self.say(f"Could not read WAV: {e}", BAD)
            return
        self.wav_path = path
        stem = os.path.splitext(os.path.basename(path))[0]
        self.out_path = os.path.join(os.path.dirname(path) or ".",
                                     stem + "_record.png")
        if not self.in_title.text:
            self.in_title.text = stem.replace("_", " ").upper()
        self.press_result = None
        self.preview = None
        self.say("")
        self.goto("press")

    def set_out(self, path):
        if not path.lower().endswith(".png"):
            path += ".png"
        self.out_path = path

    # --------------------------------------------------------- press worker
    def plan(self):
        """Live fit prediction, identical math to the encoder."""
        _, bits, rate = QUALITIES[self.quality_i]
        size = SIZES[self.size_i]
        cap = vc.spiral_capacity(size)
        use_rate, trimmed = vc.fit_plan(self.wav_dur, rate, cap)
        pressed = min(self.wav_dur, cap / use_rate) if use_rate else 0.0
        fill = (pressed * use_rate) / cap if cap else 0.0
        return dict(bits=bits, rate=rate, size=size, cap=cap,
                    use_rate=use_rate, trimmed=trimmed,
                    pressed=pressed, fill=fill)

    def do_press(self):
        if not self.wav_path or self.pressing:
            return
        self.pressing = True
        self.press_result = None
        self.preview = None
        self.say("")
        pl = self.plan()
        title = self.in_title.text or "UNTITLED"
        artist = self.in_artist.text or None
        out = self.out_path
        color = LABEL_COLORS[self.color_i][1]

        def work():
            try:
                info = vc.encode_wav(self.wav_path, out, title=title,
                                     artist=artist, size=pl["size"],
                                     rate=pl["rate"], bits=pl["bits"],
                                     label_color=color)
                im = Image.open(out).convert("RGBA").resize(
                    (320, 320), Image.LANCZOS)
                self._jobs.append({"kind": "pressed", "info": info,
                                   "thumb": (im.tobytes(), im.size)})
            except Exception as e:
                self._jobs.append({"kind": "error",
                                   "msg": f"Pressing failed: {e}"})
        threading.Thread(target=work, daemon=True).start()

    def play_pressed(self):
        if self.press_result:
            self.load_record(self.press_result["png"])

    # ---------------------------------------------------------- record load
    def load_record(self, path):
        if self.loading:
            return
        self.loading = True
        self.load_error = ""
        self.record_path = path
        self.goto("deck")

        def work():
            try:
                reader = vc.GrooveReader(path)
                buf, size, ox, oy, f = disc_display(reader, 560)
                self._jobs.append({"kind": "record", "reader": reader,
                                   "disp": (buf, size, ox, oy, f)})
            except Exception as e:
                self._jobs.append({"kind": "error", "msg": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def eject(self):
        if self.deck:
            self.deck.close()
            self.deck = None
        self.record_surf = None
        self.goto("home")

    # ----------------------------------------------------- worker results
    def poll_worker(self):
        if not self._jobs:
            return
        out = self._jobs.pop(0)
        if out["kind"] == "error":
            self.pressing = False
            self.loading = False
            if self.state == "deck" and self.deck is None:
                self.load_error = out["msg"]
            else:
                self.say(out["msg"], BAD)
        elif out["kind"] == "pressed":
            self.pressing = False
            self.press_result = out["info"]
            buf, size = out["thumb"]
            self.preview = pygame.image.frombuffer(buf, size, "RGBA")
            self.say("Pressed! The PNG is the record \u2014 share it as-is.",
                     GOOD)
        elif out["kind"] == "record":
            if self.deck:
                self.deck.close()
            rd = out["reader"]
            self.deck = Deck(rd)
            buf, size, self.disp_ox, self.disp_oy, self.disp_f = out["disp"]
            self.record_surf = pygame.image.frombuffer(
                buf, size, "RGBA").convert_alpha()
            ro = rd.hdr["f_outer"] * rd.S
            outer_rps = (rd.rate * rd.hdr["step"]
                         / (2.0 * math.pi * ro))
            self.vis_slow = outer_rps / (100.0 / 3.0 / 60.0)
            self.disp_scale = 560 / (vc.F_DISC * rd.S * 2.0)
            self.marker = False
            self.sl_speed.val = self.deck.speed_mult
            self.sl_vol.val = self.deck.volume
            self.loading = False

    # ----------------------------------------------------- in-app browser
    def open_browser(self, mode, cb):
        start = os.path.dirname(self.wav_path or self.record_path or "") \
            or os.path.expanduser("~")
        self.br = {"mode": mode, "cb": cb, "cwd": os.path.abspath(start),
                   "back": self.state, "scroll": 0,
                   "name": TextInput((290, WIN_H - 96, 420, 40),
                                     "file name.png",
                                     os.path.basename(self.out_path or ""))}
        self._br_list()
        self.goto("browse")

    def _br_list(self):
        br = self.br
        ext = ".wav" if br["mode"] == "wav" else ".png"
        dirs, files = [], []
        try:
            for e in sorted(os.listdir(br["cwd"]), key=str.lower):
                if e.startswith("."):
                    continue
                full = os.path.join(br["cwd"], e)
                if os.path.isdir(full):
                    dirs.append(e)
                elif br["mode"] != "save" and e.lower().endswith(ext):
                    files.append(e)
                elif br["mode"] == "save" and e.lower().endswith(".png"):
                    files.append(e)
        except Exception:
            pass
        br["entries"] = [("dir", d) for d in dirs] + [("file", f)
                                                      for f in files]
        br["scroll"] = 0

    def br_click_row(self, i):
        br = self.br
        kind, name = br["entries"][i]
        full = os.path.join(br["cwd"], name)
        if kind == "dir":
            br["cwd"] = full
            self._br_list()
        elif br["mode"] == "save":
            br["name"].text = name
        else:
            cb = br["cb"]
            back = br["back"]
            self.br = None
            self.goto(back)
            cb(full)

    def br_ok(self):
        br = self.br
        if br["mode"] == "save":
            name = br["name"].text.strip()
            if name:
                cb = br["cb"]
                back = br["back"]
                path = os.path.join(br["cwd"], name)
                self.br = None
                self.goto(back)
                cb(path)
        # open modes select by clicking a file directly

    # ============================================================== drawing
    def draw_home(self):
        s = self.screen
        t = self.fonts["big"].render("VREC  STUDIO By Joel Lagace", True, TEXT)
        s.blit(t, t.get_rect(center=(WIN_W // 2, 120)))
        t = self.fonts["ui"].render(
            "virtual vinyl \u2014 the image IS the music", True, DIM)
        s.blit(t, t.get_rect(center=(WIN_W // 2, 168)))
        # decorative spinning mini-disc
        cx, cy, r = WIN_W // 2, 232, 38
        pygame.draw.circle(s, (12, 11, 10), (cx, cy), r)
        for rr in range(10, r - 4, 4):
            pygame.draw.circle(s, (34, 31, 29), (cx, cy), rr, 1)
        pygame.draw.circle(s, ACCENT, (cx, cy), 9)
        a = self.tick * 0.045
        pygame.draw.line(s, (70, 64, 58),
                         (cx + 12 * math.cos(a), cy + 12 * math.sin(a)),
                         (cx + (r - 5) * math.cos(a),
                          cy + (r - 5) * math.sin(a)), 2)
        for b in self.b_home:
            b.draw(s, self.fonts)
        t = self.fonts["small"].render(
            "tip: drag & drop a .wav or a record .png anywhere onto this "
            "window", True, FAINT)
        s.blit(t, t.get_rect(center=(WIN_W // 2, 545)))
        if self.status:
            t = self.fonts["ui"].render(self.status, True, self.status_col)
            s.blit(t, t.get_rect(center=(WIN_W // 2, 590)))

    def draw_press(self):
        s = self.screen
        f = self.fonts
        s.blit(f["h2"].render("PRESS A NEW RECORD", True, TEXT), (70, 64))

        # wav row
        for b in self.b_press:
            b.draw(s, f)
        wl = (os.path.basename(self.wav_path) + 
              f"   \u00b7   {fmt_time(self.wav_dur)} @ {self.wav_rate} Hz"
              ) if self.wav_path else "no WAV loaded yet"
        s.blit(f["ui"].render(wl[:58], True,
                              TEXT if self.wav_path else FAINT), (286, 144))

        s.blit(f["small"].render("TITLE (printed on the label)", True, DIM),
               (70, 196))
        self.in_title.draw(s, f, self.tick)
        s.blit(f["small"].render("ARTIST", True, DIM), (70, 274))
        self.in_artist.draw(s, f, self.tick)

        s.blit(f["small"].render("DISC SIZE (pixels)", True, DIM), (70, 370))
        for b in self.b_size:
            b.draw(s, f)
        s.blit(f["small"].render("QUALITY", True, DIM), (70, 440))
        for b in self.b_quality:
            b.draw(s, f)

        s.blit(f["small"].render("LABEL COLOR", True, DIM), (70, 514))
        for i, (_, col) in enumerate(LABEL_COLORS):
            r = pygame.Rect(70 + i * 46, 530, 38, 24)
            pygame.draw.rect(s, col, r, border_radius=6)
            if i == self.color_i:
                pygame.draw.rect(s, TEXT, r, 2, border_radius=6)

        # save-as path line
        if self.out_path:
            s.blit(f["small"].render("\u2192 " + self.out_path[-60:], True,
                                     FAINT), (70, 690))

        # right column: live plan / result
        rx = 600
        pygame.draw.rect(s, PANEL, (rx - 20, 120, 460, 410),
                         border_radius=14)
        if self.wav_path:
            pl = self.plan()
            rows = [
                (f"capacity   {fmt_time(pl['cap'] / pl['use_rate'])} at "
                 f"{pl['use_rate']} Hz", TEXT),
                (f"will press {fmt_time(pl['pressed'])}  "
                 f"({pl['fill']:.0%} of the grooves)", TEXT),
                (f"depth      {pl['bits']}-bit "
                 + ("PCM" if pl["bits"] == 16 else "\u03bc-law"), TEXT),
            ]
            if pl["use_rate"] < pl["rate"]:
                rows.append((f"rate lowered to {pl['use_rate']} Hz to fit "
                             f"\u2014 pick a bigger disc", WARN))
            if pl["trimmed"]:
                rows.append(("audio will be TRIMMED \u2014 pick a bigger "
                             "disc", BAD))
            if pl["size"] >= 6144:
                rows.append(("big disc: pressing can take a minute", DIM))
            y = 150
            for txt, col in rows:
                s.blit(f["mono"].render(txt, True, col), (rx, y))
                y += 30
        else:
            s.blit(f["ui"].render("Browse for a WAV to see the plan.",
                                  True, FAINT), (rx, 150))

        if self.pressing:
            a = self.tick * 0.12
            pygame.draw.circle(s, (15, 13, 12), (rx + 210, 380), 52)
            pygame.draw.circle(s, ACCENT, (rx + 210, 380), 14)
            pygame.draw.line(s, (90, 82, 74),
                             (rx + 210 + 18 * math.cos(a),
                              380 + 18 * math.sin(a)),
                             (rx + 210 + 48 * math.cos(a),
                              380 + 48 * math.sin(a)), 3)
            s.blit(f["ui"].render("pressing\u2026", True, DIM),
                   (rx + 168, 446))
        elif self.preview is not None:
            s.blit(self.preview, (rx + 60, 200))
            self.b_play_now.draw(s, f)

        if self.status:
            s.blit(f["ui"].render(self.status, True, self.status_col),
                   (600, 640))

    def draw_deck(self):
        s = self.screen
        f = self.fonts
        cx, cy, D = 350, 372, 560
        pygame.draw.rect(s, DECK_COL, (24, 24, WIN_W - 48, WIN_H - 48),
                         border_radius=18)
        pygame.draw.circle(s, (16, 14, 13), (cx + 5, cy + 7), D // 2 + 14)
        pygame.draw.circle(s, (52, 46, 42), (cx, cy), D // 2 + 12)
        pygame.draw.circle(s, (30, 26, 24), (cx, cy), D // 2 + 6)

        if self.loading or self.deck is None:
            msg = self.load_error or "READING THE GROOVES\u2026"
            col = BAD if self.load_error else DIM
            for i, line in enumerate(self._wrap(msg, f["ui"], 420)):
                t = f["ui"].render(line, True, col)
                s.blit(t, t.get_rect(center=(cx, cy - 10 + i * 26)))
            if self.load_error:
                Button((cx - 70, cy + 60, 140, 44), "BACK",
                       self.eject).draw(s, f)
                self._tmp_back = pygame.Rect(cx - 70, cy + 60, 140, 44)
            return

        dk = self.deck
        pos = dk.pos
        angle = math.degrees(dk.th_of(pos)) / self.vis_slow
        self._cur_angle = angle
        disc = pygame.transform.rotozoom(self.record_surf, -angle, 1.0)
        s.blit(disc, disc.get_rect(center=(cx, cy)))
        pygame.draw.circle(s, (210, 205, 195), (cx, cy), 5)

        # tonearm
        pivot = (cx + D * 0.66, cy - D * 0.60)
        d_pc = math.hypot(pivot[0] - cx, pivot[1] - cy)
        arm_len = d_pc - (dk.hdr["f_inner"] * dk.img_S * self.disp_scale) * 0.85
        stopped = not dk.playing and not dk.scratch and abs(dk.vel) < 1e-3
        rd = dk.r_of(pos) * self.disp_scale + (16 if stopped else 0)
        cosA = (d_pc * d_pc + arm_len * arm_len - rd * rd) \
            / (2.0 * d_pc * arm_len)
        A = math.acos(max(-1.0, min(1.0, cosA)))
        beta = math.atan2(cy - pivot[1], cx - pivot[0])
        phi = beta - A
        tip = (pivot[0] + arm_len * math.cos(phi),
               pivot[1] + arm_len * math.sin(phi))
        tail = (pivot[0] - 52 * math.cos(phi),
                pivot[1] - 52 * math.sin(phi))
        pygame.draw.line(s, (15, 13, 12), tail, tip, 11)
        pygame.draw.line(s, (185, 178, 168), tail, tip, 7)
        pygame.draw.circle(s, (90, 84, 78), tail, 13)
        pygame.draw.circle(s, (60, 55, 50),
                           (int(pivot[0]), int(pivot[1])), 17)
        pygame.draw.circle(s, (120, 112, 104),
                           (int(pivot[0]), int(pivot[1])), 17, 3)
        pygame.draw.circle(s, (25, 22, 20), tip, 9)
        pygame.draw.circle(s, ACCENT, tip, 4)

        # right panel
        px = 716
        name = os.path.basename(self.record_path)[:24]
        s.blit(f["small"].render("NOW PLAYING", True, DIM), (px, 70))
        s.blit(f["h2"].render(name, True, TEXT), (px, 90))
        t = pos / dk.rate
        total = dk.n / dk.rate
        rpm = (60.0 * dk.rate * abs(dk.vel) / dk.base_speed * dk.hdr["step"]
               / (2.0 * math.pi * dk.r_of(pos))) / self.vis_slow
        s.blit(f["mono"].render(
            f"{fmt_time(t)} / {fmt_time(total)}    {rpm:5.1f} rpm",
            True, TEXT), (px, 138))
        s.blit(f["mono"].render(
            f"{dk.rate} Hz \u00b7 "
            f"{'16-bit' if dk.hdr['flags'] & vc.FLAG_PCM16 else '8-bit'} "
            f"\u00b7 v{dk.hdr['version']}", True, DIM), (px, 166))
        state = ("SCRATCHING" if dk.scratch else
                 "PLAYING" if dk.playing else "STOPPED")
        s.blit(f["ui"].render(state, True, ACCENT), (px, 206))
        s.blit(f["small"].render("LIVE OPTICAL PICKUP \u2014 the needle "
                                 "decodes pixels", True, GOOD), (px, 228))

        # progress (clickable)
        pr = self.progress_rect
        pygame.draw.rect(s, (55, 50, 46), pr, border_radius=5)
        pygame.draw.rect(s, ACCENT,
                         (pr.x, pr.y, max(3, int(pr.w * pos / dk.n)), pr.h),
                         border_radius=5)

        for b in self.b_deck:
            b.draw(s, f)
        s.blit(f["small"].render(
            f"SPEED  {dk.speed_mult * 100:.0f} %", True, DIM), (px, 422))
        self.sl_speed.draw(s)
        s.blit(f["small"].render(
            f"VOLUME  {dk.volume * 100:.0f} %", True, DIM), (px, 516))
        self.sl_vol.draw(s)
        hint = ("marker on: draw on the disc to scratch it \u2014 "
                "you'll HEAR it" if self.marker
                else "drag the disc to scratch (DJ style)")
        s.blit(f["small"].render(hint, True,
                                 WARN if self.marker else FAINT), (px, 690))

    def draw_browse(self):
        s = self.screen
        f = self.fonts
        br = self.br
        title = {"wav": "CHOOSE A WAV", "png": "CHOOSE A RECORD PNG",
                 "save": "SAVE RECORD AS"}[br["mode"]]
        s.blit(f["h2"].render(title, True, TEXT), (70, 50))
        s.blit(f["mono"].render(br["cwd"][-78:], True, DIM), (70, 92))
        Button((WIN_W - 200, 46, 130, 40), "CANCEL",
               self._br_cancel).draw(s, f)
        Button((70, 120, 110, 36), "\u2191 UP", self._br_up).draw(s, f)
        self._br_rows = []
        y0, rh = 170, 34
        max_rows = (WIN_H - 200 - (60 if br["mode"] == "save" else 0)) // rh
        ent = br["entries"][br["scroll"]:br["scroll"] + max_rows]
        for i, (kind, name) in enumerate(ent):
            r = pygame.Rect(70, y0 + i * rh, WIN_W - 140, rh - 4)
            hov = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, (52, 46, 41) if hov else (34, 31, 29), r,
                             border_radius=6)
            icon = "\u25a0 " if kind == "dir" else "  "
            col = WARN if kind == "dir" else TEXT
            s.blit(f["ui"].render(icon + name[:70], True, col),
                   (r.x + 10, r.y + 4))
            self._br_rows.append((r, br["scroll"] + i))
        if not br["entries"]:
            s.blit(f["ui"].render("(nothing here)", True, FAINT), (90, y0))
        if br["mode"] == "save":
            s.blit(f["small"].render("FILE NAME", True, DIM),
                   (290, WIN_H - 118))
            br["name"].draw(s, f, self.tick)
            Button((730, WIN_H - 96, 110, 40), "SAVE",
                   self.br_ok, kind="primary").draw(s, f)
            self._br_save = pygame.Rect(730, WIN_H - 96, 110, 40)
        self._br_cancel_r = pygame.Rect(WIN_W - 200, 46, 130, 40)
        self._br_up_r = pygame.Rect(70, 120, 110, 36)

    def _br_cancel(self):
        back = self.br["back"] if self.br else "home"
        self.br = None
        self.goto(back)

    def _br_up(self):
        self.br["cwd"] = os.path.dirname(self.br["cwd"]) or self.br["cwd"]
        self._br_list()

    def _wrap(self, text, font, width):
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if font.size(t)[0] <= width:
                cur = t
            else:
                lines.append(cur)
                cur = w
        return lines + [cur]

    # ================================================================ events
    def handle(self, e):
        if e.type == pygame.QUIT:
            self.quit()
        elif e.type == pygame.DROPFILE:
            p = e.file
            if p.lower().endswith(".png"):
                self.load_record(p)
            elif p.lower().endswith(".wav"):
                self.set_wav(p)
            return
        if self.state == "home":
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                for b in self.b_home:
                    b.click(e.pos)
        elif self.state == "press":
            self.handle_press(e)
        elif self.state == "deck":
            self.handle_deck(e)
        elif self.state == "browse":
            self.handle_browse(e)

    def handle_press(self, e):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            self.in_title.focus = self.in_title.rect.collidepoint(e.pos)
            self.in_artist.focus = self.in_artist.rect.collidepoint(e.pos)
            for b in (self.b_press + self.b_size + self.b_quality):
                if b.click(e.pos):
                    return
            if self.preview is not None and not self.pressing:
                self.b_play_now.click(e.pos)
            for i in range(len(LABEL_COLORS)):
                if pygame.Rect(70 + i * 46, 530, 38, 24).collidepoint(e.pos):
                    self.color_i = i
        elif e.type == pygame.TEXTINPUT:
            self.in_title.type(e.text)
            self.in_artist.type(e.text)
        elif e.type == pygame.KEYDOWN:
            if self.in_title.focus or self.in_artist.focus:
                self.in_title.key(e)
                self.in_artist.key(e)
            elif e.key == pygame.K_ESCAPE:
                self.goto("home")

    def handle_deck(self, e):
        dk = self.deck
        if dk is None:
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 \
                    and self.load_error \
                    and getattr(self, "_tmp_back",
                                pygame.Rect(0, 0, 0, 0)).collidepoint(e.pos):
                self.eject()
            return
        cx, cy, D = 350, 372, 560
        if self.sl_speed.handle(e):
            dk.speed_mult = self.sl_speed.val
            return
        if self.sl_vol.handle(e):
            dk.volume = self.sl_vol.val
            return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            if self.progress_rect.inflate(0, 12).collidepoint(e.pos):
                dk.seek_frac((e.pos[0] - self.progress_rect.x)
                             / self.progress_rect.w)
                return
            for b in self.b_deck:
                if b.click(e.pos):
                    return
            if math.hypot(e.pos[0] - cx, e.pos[1] - cy) <= D / 2:
                if self.marker:
                    self._mk_prev = e.pos
                else:
                    dk.scratch_pos = dk.pos
                    self._grab_prev = math.atan2(e.pos[1] - cy,
                                                 e.pos[0] - cx)
                    dk.scratch = True
        elif e.type == pygame.MOUSEMOTION and self.marker \
                and self._mk_prev is not None:
            x0, y0 = self.screen_to_record(*self._mk_prev, cx, cy)
            x1, y1 = self.screen_to_record(*e.pos, cx, cy)
            if dk.reader.stamp(x0, y0, x1, y1, width=3, value=255):
                p0 = ((x0 - self.disp_ox) * self.disp_f,
                      (y0 - self.disp_oy) * self.disp_f)
                p1 = ((x1 - self.disp_ox) * self.disp_f,
                      (y1 - self.disp_oy) * self.disp_f)
                pygame.draw.line(self.record_surf, (232, 232, 232, 255),
                                 p0, p1, 1)
            self._mk_prev = e.pos
        elif e.type == pygame.MOUSEMOTION and dk.scratch:
            a = math.atan2(e.pos[1] - cy, e.pos[0] - cx)
            da = (a - self._grab_prev + math.pi) % (2 * math.pi) - math.pi
            self._grab_prev = a
            dk.scratch_pos = float(np.clip(
                dk.scratch_pos + SCRATCH_SIGN * self.vis_slow
                * dk.r_of(dk.scratch_pos) * da / dk.hdr["step"],
                0.0, dk.n - 2.0))
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
            dk.scratch = False
            self._mk_prev = None
        elif e.type == pygame.KEYDOWN:
            k = e.key
            if k == pygame.K_SPACE:
                dk.toggle_play()
            elif k == pygame.K_LEFT:
                dk.seek(-5)
            elif k == pygame.K_RIGHT:
                dk.seek(+5)
            elif k == pygame.K_UP:
                self.set_speed(min(1.5, dk.speed_mult + 0.02))
            elif k == pygame.K_DOWN:
                self.set_speed(max(0.5, dk.speed_mult - 0.02))
            elif k == pygame.K_1:
                self.set_speed(1.0)
            elif k == pygame.K_2:
                self.set_speed(1.35)
            elif k == pygame.K_l:
                dk.loop = not dk.loop
            elif k == pygame.K_r:
                dk.pos = 0.0
            elif k in (pygame.K_ESCAPE, pygame.K_q):
                self.eject()

    def handle_browse(self, e):
        br = self.br
        if br is None:
            return
        if e.type == pygame.MOUSEWHEEL:
            br["scroll"] = max(0, min(max(0, len(br["entries"]) - 5),
                                      br["scroll"] - e.y * 3))
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            if self._br_cancel_r.collidepoint(e.pos):
                self._br_cancel()
                return
            if self._br_up_r.collidepoint(e.pos):
                self._br_up()
                return
            if br["mode"] == "save":
                br["name"].focus = br["name"].rect.collidepoint(e.pos)
                if getattr(self, "_br_save",
                           pygame.Rect(0, 0, 0, 0)).collidepoint(e.pos):
                    self.br_ok()
                    return
            for r, i in self._br_rows:
                if r.collidepoint(e.pos):
                    self.br_click_row(i)
                    return
        elif e.type == pygame.TEXTINPUT and br["mode"] == "save":
            br["name"].type(e.text)
        elif e.type == pygame.KEYDOWN:
            if br["mode"] == "save" and br["name"].focus:
                if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self.br_ok()
                else:
                    br["name"].key(e)
            elif e.key == pygame.K_ESCAPE:
                self._br_cancel()

    # ================================================================== run
    def run(self):
        while True:
            for e in pygame.event.get():
                self.handle(e)
            self.poll_worker()
            self.screen.fill(BG)
            if self.state == "home":
                self.draw_home()
            elif self.state == "press":
                self.draw_press()
            elif self.state == "deck":
                self.draw_deck()
            elif self.state == "browse" and self.br:
                self.draw_browse()
            pygame.display.flip()
            self.tick += 1
            self.clock.tick(60)


if __name__ == "__main__":
    Studio().run()
