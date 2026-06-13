#!/usr/bin/env python3
"""record_player.py — a virtual turntable for VREC vinyl PNGs.

Usage:
    python record_player.py my_record.png

Controls:
    SPACE        play / pause (with a vinyl-brake spin-down)
    drag disc    scratch (grab the record with the mouse)
    LEFT/RIGHT   seek -/+ 5 s
    UP/DOWN      nudge speed +/- 2%
    1 / 2        speed presets: 33 1/3  /  45 (x1.35)
    L            toggle loop
    R            back to the start
    ESC / Q      quit

The needle genuinely tracks the groove: its radial position is computed from
the same spiral formula the decoder uses, so it always sits over the pixels
that are currently playing.
"""
import math
import sys

import numpy as np

try:
    import pygame
except ImportError:
    sys.exit("pygame is required:  pip install pygame")
try:
    import sounddevice as sd
except ImportError:
    sys.exit("sounddevice is required:  pip install sounddevice")

import vinyl_codec as vc

# If scratching feels direction-inverted on your platform, flip this to -1.0.
SCRATCH_SIGN = +1.0
SCRATCH_MAX_SPEED = 48.0      # max |speed| while chasing the hand (x normal)
VOLUME = 0.9

WIN_W, WIN_H = 1000, 700
DISC_PX = 580                 # on-screen disc diameter
DISC_CENTER = (340, 352)

BG = (24, 22, 21)
DECK = (38, 33, 30)
TEXT = (228, 220, 205)
DIM = (140, 132, 120)
ACCENT = (205, 92, 60)


class Turntable:
    def __init__(self, png_path: str):
        print("Lowering the needle (LIVE optical pickup) ...")
        self.reader = vc.GrooveReader(png_path)
        self.n = self.reader.n
        self.rate = int(self.reader.rate)
        self.hdr = self.reader.hdr
        self.path = png_path
        print(f"  {self.n:,} samples at {self.rate} Hz "
              f"({self.n / self.rate:.1f} s) -- pixels are decoded as the "
              "needle reaches them")

        # --- playback state (audio callback <-> UI thread) -----------------
        self.pos = 0.0            # fractional sample index = needle position
        self.vel = 0.0            # current speed (source samples per output sample)
        self.speed_mult = 1.0     # user speed (1.0 = nominal, 1.35 = "45 RPM")
        self.playing = False
        self.loop = False
        self.scratch = False
        self.scratch_pos = 0.0    # where the hand says the needle should be

        # --- audio stream (fall back to the device's native rate) ----------
        self.out_rate = self.rate
        try:
            self.stream = sd.OutputStream(samplerate=self.rate, channels=1,
                                          dtype="float32",
                                          callback=self._callback)
        except Exception:
            dev = sd.query_devices(kind="output")
            self.out_rate = int(dev["default_samplerate"])
            print(f"  device prefers {self.out_rate} Hz; resampling on the fly")
            self.stream = sd.OutputStream(samplerate=self.out_rate, channels=1,
                                          dtype="float32",
                                          callback=self._callback)
        self.base_speed = self.rate / self.out_rate
        self.kp = 1.0 / (0.045 * self.out_rate)   # scratch follower gain
        self.stream.start()

    # ------------------------------------------------------------ geometry
    def S(self) -> int:
        return self.img_S

    def r_of(self, pos: float) -> float:
        return vc.groove_radius(self.hdr, self.img_S, pos)

    def th_of(self, pos: float) -> float:
        return vc.groove_theta(self.hdr, self.img_S, pos)

    # ------------------------------------------------------- audio callback
    def _callback(self, out, frames, time_info, status):
        if self.scratch:
            target_v = float(np.clip((self.scratch_pos - self.pos) * self.kp,
                                     -SCRATCH_MAX_SPEED, SCRATCH_MAX_SPEED))
        elif self.playing:
            target_v = self.base_speed * self.speed_mult
        else:
            target_v = 0.0

        v1 = self.vel + (target_v - self.vel) * 0.25       # smooth slew
        sp = np.linspace(self.vel, v1, frames, dtype=np.float64)
        idx = self.pos + np.cumsum(sp)
        self.vel = v1
        self.pos = float(idx[-1])

        out[:, 0] = self.reader.read(idx) * VOLUME   # live from the image

        if not self.scratch:
            if self.pos >= self.n - 2:
                if self.loop and self.playing:
                    self.pos = 0.0
                else:
                    self.pos = float(self.n - 2)
                    self.playing = False
            elif self.pos < 0:
                self.pos = 0.0

    # ------------------------------------------------------------------ UI
    def run(self):
        pygame.init()
        screen = pygame.display.set_mode((WIN_W, WIN_H))
        pygame.display.set_caption(f"VREC turntable — {self.path}")
        clock = pygame.time.Clock()
        font = pygame.font.SysFont("dejavusansmono,consolas,menlo,monospace", 17)
        small = pygame.font.SysFont("dejavusansmono,consolas,menlo,monospace", 14)

        # display square cut around the disc itself (crop-tolerant)
        rd = self.reader
        self.img_S = rd.S
        src_side = int(round(2.0 * vc.F_DISC * rd.S)) + 2
        half = src_side // 2
        canvas = np.zeros((src_side, src_side, 4), dtype=np.uint8)
        x0 = int(round(rd.cx)) - half
        y0 = int(round(rd.cy)) - half
        sx0, sy0 = max(0, x0), max(0, y0)
        sx1 = min(rd.w, x0 + src_side)
        sy1 = min(rd.h, y0 + src_side)
        canvas[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = \
            rd.rgba[sy0:sy1, sx0:sx1]
        self.disp_scale = DISC_PX / (vc.F_DISC * self.img_S * 2.0)
        side = int(round(src_side * self.disp_scale))
        from PIL import Image as _Img
        small = _Img.fromarray(canvas, "RGBA").resize((side, side),
                                                      _Img.LANCZOS)
        record = pygame.image.frombuffer(small.tobytes(), small.size,
                                         "RGBA").convert_alpha()

        # visual slowdown so the OUTER groove spins at exactly 33 1/3 RPM
        ro = self.hdr["f_outer"] * self.img_S
        outer_rps = self.rate * self.hdr["step"] / (2.0 * math.pi * ro)
        self.vis_slow = outer_rps / (100.0 / 3.0 / 60.0)

        cx, cy = DISC_CENTER
        pivot = (cx + DISC_PX * 0.66, cy - DISC_PX * 0.60)
        d_pc = math.hypot(pivot[0] - cx, pivot[1] - cy)
        arm_len = d_pc - (self.hdr["f_inner"] * self.img_S * self.disp_scale) * 0.85

        grab_prev = 0.0
        while True:
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self.quit()
                elif e.type == pygame.KEYDOWN:
                    if e.key in (pygame.K_ESCAPE, pygame.K_q):
                        self.quit()
                    elif e.key == pygame.K_SPACE:
                        if not self.playing and self.pos >= self.n - 3:
                            self.pos = 0.0
                        self.playing = not self.playing
                    elif e.key == pygame.K_LEFT:
                        self.pos = max(0.0, self.pos - 5 * self.rate)
                    elif e.key == pygame.K_RIGHT:
                        self.pos = min(self.n - 2.0, self.pos + 5 * self.rate)
                    elif e.key == pygame.K_UP:
                        self.speed_mult = min(2.0, self.speed_mult + 0.02)
                    elif e.key == pygame.K_DOWN:
                        self.speed_mult = max(0.25, self.speed_mult - 0.02)
                    elif e.key == pygame.K_1:
                        self.speed_mult = 1.0
                    elif e.key == pygame.K_2:
                        self.speed_mult = 1.35      # ~ 45 / 33.33
                    elif e.key == pygame.K_l:
                        self.loop = not self.loop
                    elif e.key == pygame.K_r:
                        self.pos = 0.0
                elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                    mx, my = e.pos
                    if math.hypot(mx - cx, my - cy) <= DISC_PX / 2:
                        self.scratch_pos = self.pos
                        grab_prev = math.atan2(my - cy, mx - cx)
                        self.scratch = True
                elif e.type == pygame.MOUSEMOTION and self.scratch:
                    mx, my = e.pos
                    a = math.atan2(my - cy, mx - cx)
                    da = (a - grab_prev + math.pi) % (2 * math.pi) - math.pi
                    grab_prev = a
                    r_full = self.r_of(self.scratch_pos)
                    self.scratch_pos = float(np.clip(
                        self.scratch_pos +
                        SCRATCH_SIGN * self.vis_slow * r_full * da
                        / self.hdr["step"],
                        0.0, self.n - 2.0))
                elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                    self.scratch = False

            # ---------------------------------------------------------- draw
            screen.fill(BG)
            pygame.draw.rect(screen, DECK, (24, 24, WIN_W - 48, WIN_H - 48),
                             border_radius=18)
            # platter
            pygame.draw.circle(screen, (16, 14, 13), (cx + 5, cy + 7),
                               DISC_PX // 2 + 14)
            pygame.draw.circle(screen, (52, 46, 42), (cx, cy),
                               DISC_PX // 2 + 12)
            pygame.draw.circle(screen, (30, 26, 24), (cx, cy),
                               DISC_PX // 2 + 6)

            pos = self.pos
            angle = math.degrees(self.th_of(pos)) / self.vis_slow
            disc = pygame.transform.rotozoom(record, -angle, 1.0)
            screen.blit(disc, disc.get_rect(center=(cx, cy)))
            pygame.draw.circle(screen, (210, 205, 195), (cx, cy), 5)

            # ------------------------------------------------------- tonearm
            stopped = (not self.playing and not self.scratch
                       and abs(self.vel) < 1e-3)
            rd = self.r_of(pos) * self.disp_scale + (16 if stopped else 0)
            cosA = (d_pc * d_pc + arm_len * arm_len - rd * rd) \
                   / (2.0 * d_pc * arm_len)
            A = math.acos(max(-1.0, min(1.0, cosA)))
            beta = math.atan2(cy - pivot[1], cx - pivot[0])
            phi = beta - A
            tip = (pivot[0] + arm_len * math.cos(phi),
                   pivot[1] + arm_len * math.sin(phi))
            tail = (pivot[0] - 52 * math.cos(phi),
                    pivot[1] - 52 * math.sin(phi))
            pygame.draw.line(screen, (15, 13, 12), tail, tip, 11)
            pygame.draw.line(screen, (185, 178, 168), tail, tip, 7)
            pygame.draw.circle(screen, (90, 84, 78), tail, 13)        # weight
            pygame.draw.circle(screen, (60, 55, 50),
                               (int(pivot[0]), int(pivot[1])), 17)
            pygame.draw.circle(screen, (120, 112, 104),
                               (int(pivot[0]), int(pivot[1])), 17, 3)
            pygame.draw.circle(screen, (25, 22, 20), tip, 9)          # head
            pygame.draw.circle(screen, ACCENT, tip, 4)

            # ----------------------------------------------------------- HUD
            px = 690
            t = pos / self.rate
            total = self.n / self.rate
            rpm = (60.0 * self.rate * abs(self.vel) / self.base_speed
                   * self.hdr["step"]
                   / (2.0 * math.pi * self.r_of(pos))) / self.vis_slow
            lines = [
                ("NOW PLAYING", DIM),
                (self.path.split("/")[-1].split("\\")[-1][:26], TEXT),
                ("", TEXT),
                (f"{int(t // 60)}:{int(t % 60):02d} / "
                 f"{int(total // 60)}:{int(total % 60):02d}", TEXT),
                (f"speed {self.speed_mult * 100:5.1f}%   "
                 f"{rpm:5.1f} rpm", TEXT),
                (f"loop {'ON' if self.loop else 'off'}", DIM),
                ("", TEXT),
                ("[space] play/pause", DIM),
                ("[drag disc] scratch", DIM),
                ("[arrows] seek/speed  [1/2] 33/45", DIM),
                ("[L]oop  [R]estart  [Q]uit", DIM),
                ("", TEXT),
                ("LIVE OPTICAL PICKUP", ACCENT),
            ]
            y = 80
            for txt, col in lines:
                if txt:
                    screen.blit((font if col is TEXT else small)
                                .render(txt, True, col), (px, y))
                y += 26

            # progress bar
            pygame.draw.rect(screen, (55, 50, 46), (px, 396, 250, 8),
                             border_radius=4)
            pygame.draw.rect(screen, ACCENT,
                             (px, 396, max(2, int(250 * pos / self.n)), 8),
                             border_radius=4)

            state = "SCRATCHING" if self.scratch else \
                    ("PLAYING" if self.playing else "STOPPED")
            screen.blit(font.render(state, True, ACCENT), (px, 420))

            pygame.display.flip()
            clock.tick(60)

    def quit(self):
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            pygame.quit()
            sys.exit(0)


def main():
    if len(sys.argv) == 2:
        path = sys.argv[1]
    else:                                   # no args: offer a file picker
        path = None
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw()
            try: root.attributes("-topmost", True)
            except Exception: pass
            path = filedialog.askopenfilename(
                title="Choose a VREC record PNG",
                filetypes=[("VREC record", "*.png")])
            root.destroy()
        except Exception:
            pass
        if not path:
            print(__doc__)
            print("Tip: for the full point-and-click app, run "
                  "vinyl_studio.py instead.")
            sys.exit(1)
    Turntable(path).run()


if __name__ == "__main__":
    main()
