#!/usr/bin/env python3
"""
test_live.py — prove the live optical pickup is exact, fast, and scratchable.
"""

import time
import numpy as np
from PIL import Image
import vinyl_codec as vc

# ---------------------------------------------------------------- exactness
print("== live pickup reads the same audio as the offline decoder ==")
for png in ("/tmp/test_v1.png", "/tmp/test_v2.png",
            "demo_record.png", "demo_record_cd.png"):
    try:
        audio, rate, hdr, _ = vc.decode_record(png)
    except FileNotFoundError:
        continue
    rd = vc.GrooveReader(png)
    live = rd.read(np.arange(rd.n, dtype=np.float64))
    diff = np.max(np.abs(live - audio.astype(np.float32)))
    assert rd.n == len(audio) and rd.rate == rate
    assert diff == 0.0, f"{png}: live differs by {diff}"
    print(f"  {png}: {rd.n:,} samples, live == offline exactly "
          f"(v{hdr['version']})")

# ---------------------------------------------------------------- benchmark
print("\n== real-time budget (block = 512 frames) ==")
rd = vc.GrooveReader("demo_record_cd.png")        # 44.1 kHz, 6144px disc
budget_ms = 512 / rd.rate * 1000.0
rng = np.random.default_rng(1)

def bench(label, make_idx, blocks=400):
    t0 = time.perf_counter()
    pos = 1000.0
    for _ in range(blocks):
        idx = make_idx(pos)
        rd.read(idx)
        pos = float(idx[-1]) % (rd.n - 600)
    ms = (time.perf_counter() - t0) / blocks * 1000.0
    print(f"  {label:34s} {ms:6.3f} ms/block  "
          f"({budget_ms / ms:5.1f}x faster than real time)")
    assert ms < budget_ms / 3, "too slow for live playback!"

bench("normal play (1x)", lambda p: p + np.arange(512))
bench("scratch sweep (\u00b148x)",
      lambda p: p + np.cumsum(rng.uniform(-48, 48, 512)))

# ------------------------------------------------- crop / pad tolerance
print("\n== postcard crops (lossless) still play ==")
ref_audio, ref_rate, ref_hdr, ref_codes = vc.decode_record("demo_record.png")
im = Image.open("demo_record.png")

crop = im.crop((17, 6, im.width - 3, im.height - 19))       # asymmetric
crop.save("/tmp/cropped.png")
a2, r2, h2, c2 = vc.decode_record("/tmp/cropped.png")
assert np.array_equal(c2, ref_codes)
print(f"  asymmetric crop ({crop.size[0]}x{crop.size[1]}): bit-exact")

rgb = crop.convert("RGB")                                    # alpha stripped
rgb.save("/tmp/cropped_rgb.png")
a3, r3, h3, c3 = vc.decode_record("/tmp/cropped_rgb.png")
assert np.array_equal(c3, ref_codes)
print("  same crop with alpha stripped (RGB re-save): bit-exact")

big = Image.new("RGBA", (im.width + 300, im.height + 188), (0, 0, 0, 0))
big.paste(crop, (211, 97))
big.save("/tmp/padded.png")
a4, r4, h4, c4 = vc.decode_record("/tmp/padded.png")
assert np.array_equal(c4, ref_codes)
print("  cropped then padded onto a bigger canvas: bit-exact")

# ------------------------------------------------------------- scratching
bad = im.crop((60, 60, im.width, im.height))            # cuts INTO the disc
bad.save("/tmp/amputated.png")
try:
    vc.decode_record("/tmp/amputated.png")
    raise SystemExit("amputated disc decoded?!")
except ValueError:
    print("  over-cropped disc correctly REFUSED (no lucky-header fluke)")

print("\n== a drawn scratch becomes sound ==")
rd = vc.GrooveReader("/tmp/test_v1.png")
before = rd.read(np.arange(rd.n, dtype=np.float64))
# radial scratch from inside the label out past the rim (gets clamped)
ok = rd.stamp(rd.cx, rd.cy, rd.cx + 0.60 * rd.S, rd.cy, width=3, value=255)
assert ok
after = rd.read(np.arange(rd.n, dtype=np.float64))
changed = np.nonzero(after != before)[0]
pops = np.count_nonzero(after[changed] > 0.95)
print(f"  damaged samples: {len(changed):,}; full-scale pops: {pops:,}")
assert pops > 50

# pops recur once per revolution along the radial line
revs = np.diff(vc.groove_theta(rd.hdr, rd.S, changed.max())
               - vc.groove_theta(rd.hdr, rd.S, changed.min())) if False else 0
gaps = np.diff(changed)
big_gaps = gaps[gaps > 10]
ro_px = rd.hdr["f_outer"] * rd.S
expect_outer = 2 * np.pi * ro_px / rd.step          # samples per outer rev
print(f"  spacing between pops: {big_gaps.min():,} .. {big_gaps.max():,} "
      f"samples (one outer revolution = {expect_outer:,.0f})")
assert big_gaps.max() <= expect_outer * 1.05

# header survives even though the stamp line crossed its radius
hdr_after = vc.find_header(rd.rgba[:, :, 1])
assert hdr_after is not None and hdr_after["n_samples"] == rd.n
print("  header ring untouched (stamp is clamped to the data band)")

# damaged disc round-trips: save it, reload it, pops persist
rd.save("/tmp/scratched.png")
rd2 = vc.GrooveReader("/tmp/scratched.png")
again = rd2.read(np.arange(rd2.n, dtype=np.float64))
assert np.array_equal(again, after)
print("  saved scratched copy reloads identically (shareable damage!)")

print("\nALL LIVE-PICKUP TESTS PASSED")
