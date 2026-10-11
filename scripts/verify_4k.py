#!/usr/bin/env python3
"""Verify a 4K cut against the 1080p cut of the same cut list: size, codec, frame count, duration,
every frame for a slip (both cuts at 64x36 grey), spot frames (the 4K frame scaled down against the
1080p frames around it, PSNR), the sound (offset and level against the 1080p cut's, window by window),
plus five 1:1 crops of the 4K frame to look at. Both cuts rendered by cut.py from the list, the 4K with --size 3840x2160.
Usage: verify_4k.py 1080.mp4 4k.mp4 outdir edl.yaml. Prints VERIFY OK or VERIFY FAILED (exit 1),
writes outdir/verify.json. Env: SPOTS (48) spot frames; SHIFT (0 or more) reads the 4K (picture and sound)
that many frames late, to test the checks' teeth."""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
import numpy as np

if len(sys.argv) != 5:
    sys.exit(__doc__)
a, b, out, edl = sys.argv[1:5]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cut  # noqa: E402  item_dur: each item as the renderer times it
import yaml  # noqa: E402

SPOTS = int(os.environ.get('SPOTS', 48))
SHIFT = int(os.environ.get('SHIFT', 0))
if SHIFT < 0:
    sys.exit('verify_4k: SHIFT is 0 or more (a negative slice would leave one frame to compare)')
spans, t = [], 0.0
for it in yaml.safe_load(open(edl)):
    d = cut.item_dur(it)
    spans.append((t, t + d, 'footage' if ('clip' in it or 'freeze' in it) else 'text'))
    t += d


def kind(sec):
    return next((k for a0, a1, k in spans if a0 <= sec < a1), 'text')


def probe(p):
    j = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-count_packets', '-show_entries',
                                   'stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,nb_read_packets,sample_rate,channels'
                                   ':format=duration,size,bit_rate', '-of', 'json', p], capture_output=True, text=True).stdout)
    v = next(s for s in j['streams'] if s['codec_type'] == 'video')
    au = next(s for s in j['streams'] if s['codec_type'] == 'audio')
    return dict(codec=v['codec_name'], w=v['width'], h=v['height'], pix=v['pix_fmt'], fps=v['r_frame_rate'],
                frames=int(v['nb_read_packets']), dur=float(j['format']['duration']),
                gb=int(j['format']['size']) / 1e9, mbit=int(j['format']['bit_rate']) / 1e6,
                audio=f"{au['codec_name']} {au['sample_rate']} {au['channels']}ch")


def still(path, f, png, vf):
    # by time, one file at a time: in one filter graph the cut's changing colour tags make ffmpeg rebuild
    # the graph between segments, which restarts select's frame counter. An input seek returns the first
    # frame at or after the time, so half a frame before f is frame f
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{max(f - 0.5, 0) / cut.FPS:.4f}', '-i', path, '-frames:v', '1',
                    '-vf', vf, png], check=True)


def frames(path, f, n, w, h):
    """n frames from frame f on, at w x h RGB, in one seek and one decode (as still() finds frame f).
    Passthrough: at a constant rate the raw output repeats the first frame to fill the half frame
    before it, and every frame after it comes out one late."""
    r = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{max(f - 0.5, 0) / cut.FPS:.4f}', '-i', path, '-frames:v', str(n),
                        '-vf', f'scale={w}:{h}:flags=area,format=rgb24', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-'],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, 3).astype(np.float64)


def psnr(x, y):
    """PSNR over all three channels, as ffmpeg's psnr filter averages an RGB pair."""
    mse = ((x - y) ** 2).mean()
    return float('inf') if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def tiny(path):
    """Every frame at 64x36 grey, in order (a graph rebuild keeps the order; only select's n restarts).
    Decoded on the GPU where ffmpeg can (the 4K HEVC is most of the time); at 64x36 the two decoders agree."""
    r = subprocess.run(['ffmpeg', '-v', 'error', '-hwaccel', 'auto', '-i', path, '-vf', 'scale=64:36:flags=area,format=gray',
                        '-f', 'rawvideo', '-'], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, 36, 64)  # uint8: 100 MB a film, made float a chunk at a time


def slips(hd, uhd):
    """Runs of 3 or more frames where the 4K frame matches the 1080p frame before or after clearly better
    than its own (mean squared error under half), on frames that move (a still shot ties). At 64x36 an
    upscale's own detail averages out; a slip does not. On an earlier render it found 13 runs (0.1-2 s, each a
    real one-frame slip at 480x270) that 48 spot frames 30 s apart all missed."""
    n = min(len(hd), len(uhd))
    own, near, moving = [], [], []
    for c0 in range(1, n - 1, 4096):  # in chunks, so the float copies stay small
        c1 = min(c0 + 4096, n - 1)
        h0, hm, hp = (hd[c0 + d:c1 + d].astype(np.float32) for d in (0, -1, 1))
        u0 = uhd[c0:c1].astype(np.float32)
        own.append(((u0 - h0) ** 2).mean((1, 2)))
        near.append(np.minimum(((u0 - hm) ** 2).mean((1, 2)), ((u0 - hp) ** 2).mean((1, 2))))
        moving.append(((h0 - hm) ** 2).mean((1, 2)) > 4)
    own, near, moving = (np.concatenate(x) if x else np.zeros(0) for x in (own, near, moving))
    bad = np.flatnonzero((near < own / 2) & moving) + 1
    runs, start = [], None
    for i, f in enumerate(bad):
        if start is None:
            start = f
        if i + 1 == len(bad) or bad[i + 1] > f + 2:  # a gap of one frame (a still frame inside) stays one run
            if f - start + 1 >= 3:
                runs.append((int(start), int(f)))
            start = None
    return runs, int(moving.sum())


SR, WIN, LAG = 16000, 4.0, 0.050  # the sound at 16 kHz mono, compared in 4 s windows, searched to +-50 ms


def pcm(path):
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vn', '-ac', '1', '-ar', str(SR), '-f', 'f32le', '-'],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32).astype(np.float64)


def sound(hd, uhd, every=30.0):
    """The 4K cut's sound against the 1080p cut's: both come from the same camera sound (the 4K's through
    the spans' PCM copy) and each film gets its own loudnorm, so they match to a sample and a fraction of
    a dB. A window in every item of the cut list (a span's sound could only go wrong there) and one every
    `every` seconds inside a long one; a window silent in both is skipped, one silent in only one fails on
    its level. In each, the offset that lines the two up best (the cross-correlation normalised by the
    4K window's energy at each lag, so a held or swelling tone does not peak at the edge) and the level
    difference. A slip of one video frame is 33 ms."""
    rows, k, m = [], int(WIN * SR), int(LAG * SR)
    quiet = 10 ** (-50 / 20)
    centres = sorted({round(t0 + (t1 - t0) * (j + 0.5) / max(1, int((t1 - t0) // every) + 1), 3)
                      for t0, t1, _ in spans for j in range(max(1, int((t1 - t0) // every) + 1))})
    for c_ in centres:
        at = min(max(int(c_ * SR) - k // 2, m), len(hd) - k - m)
        x, y, y0 = hd[at:at + k], uhd[at - m:at + k + m], uhd[at:at + k]
        if len(y) < k + 2 * m or len(x) < k:
            continue
        rx, ry = np.sqrt((x ** 2).mean()), np.sqrt((y0 ** 2).mean())
        if rx < quiet and ry < quiet:
            continue
        c = np.fft.irfft(np.fft.rfft(y, 2 * len(y)) * np.conj(np.fft.rfft(x, 2 * len(y))))[:2 * m + 1]
        e = np.concatenate([[0.0], np.cumsum(y ** 2)])
        c = c / np.sqrt(e[k:k + 2 * m + 1] - e[:2 * m + 1] + 1e-20)
        lag = (int(np.argmax(c)) - m) / SR
        db = 20 * np.log10((ry + 1e-12) / (rx + 1e-12))
        rows.append((at / SR, lag, db))
    return rows


os.makedirs(out, exist_ok=True)
pa, pb = probe(a), probe(b)
print('1080p', pa)
print('4K   ', pb)
errs, rows = [], []
if (pb['w'], pb['h']) != (3840, 2160):
    errs.append(f"4K size {pb['w']}x{pb['h']}")
if pb['frames'] != pa['frames']:
    errs.append(f"frames {pb['frames']} vs {pa['frames']}")
if abs(pb['dur'] - pa['dur']) > 1 / cut.FPS:
    errs.append(f"duration {pb['dur']:.3f} vs {pa['dur']:.3f}")
if (pb['codec'], pb['pix']) != ('hevc', 'yuv420p10le'):
    errs.append(f"4K codec {pb['codec']} {pb['pix']}")
# the four full decodes at once: they share nothing, and the 4K's is most of the time
with ThreadPoolExecutor(4) as ex:
    ta, tb, sa, sb = (f.result() for f in [ex.submit(tiny, a), ex.submit(tiny, b), ex.submit(pcm, a), ex.submit(pcm, b)])
for name, got, want in (('1080p', len(ta), pa['frames']), ('4K', len(tb), pb['frames'])):
    if got != want:  # a decode that stopped early would leave the tail unchecked
        errs.append(f'{name} decoded {got} frames of {want}')
runs, moving = slips(ta, tb[SHIFT:])
print(f'slips: {len(runs)} runs over {moving} moving frames' + ''.join(
    f'\n  frames {s}-{e} ({s / cut.FPS:.1f}-{e / cut.FPS:.1f} s)' for s, e in runs), flush=True)
for s_, e_ in runs:
    errs.append(f'slip at frames {s_}-{e_} ({s_ / cut.FPS:.1f} s)')
n = pa['frames']
sb = sb[SHIFT * SR // cut.FPS:]
if abs(len(sa) - len(sb)) > SR / cut.FPS:
    errs.append(f'sound length {len(sb) / SR:.3f} vs {len(sa) / SR:.3f} s')
snd = sound(sa, sb)
worst_lag = max((abs(r[1]) for r in snd), default=0)
worst_db = max((abs(r[2]) for r in snd), default=0)
print(f'sound: {len(snd)} windows, offset up to {worst_lag * 1000:.2f} ms, level within {worst_db:.2f} dB', flush=True)
if not snd:
    errs.append('sound: no window with sound to compare')
for t_, lag, db in snd:
    if abs(lag) > 0.002 or abs(db) > 1.0:
        errs.append(f'sound at {t_:.1f} s: {lag * 1000:+.1f} ms, {db:+.2f} dB')
for k in range(1, SPOTS + 1):
    f = int(n * k / (SPOTS + 1))
    # footage at 480x270 against the 1080p frames f-2..f+2: the same frame must match best (an upscale's own
    # detail puts an aligned frame anywhere from 23 to 39 dB, so no fixed bar tells detail from a slip;
    # a 1-frame slip on an earlier render: 16.6 dB aligned, 31.5 one frame over), and 20 dB at least (another shot).
    # Text at 96x54, where a native line's glyph rounding (Pillow rounds each advance to a pixel: about 1%
    # at 1080p) blurs out but a wrong card does not: 27 dB (aligned 28.8+, a wrong reveal step 25.5)
    k_ = kind((f + 0.5) / cut.FPS)
    w, h = (480, 270) if k_ == 'footage' else (96, 54)
    x = frames(b, f + SHIFT, 1, w, h)[0]
    if k_ == 'footage':
        ys = frames(a, f - 2, 5, w, h) if f >= 2 else None
        near = {d: psnr(x, y) for d, y in zip((-2, -1, 0, 1, 2), ys)} if ys is not None and len(ys) == 5 else {0: 0.0, 1: 99.0}
        db, rival = near[0], max(v for d, v in near.items() if d)
        bad, note = db < 20 or db < rival - 0.3, f'(best of the neighbours {rival:.1f})'
    else:
        db = psnr(x, frames(a, f, 1, w, h)[0])
        bad, note = db < 27, ''
    rows.append((f, k_, db))
    print(f'frame {f:6d} ({f / cut.FPS:7.1f} s) {k_:7}: {db:5.1f} dB {note}{"  FAIL" if bad else ""}', flush=True)
    if bad:
        errs.append(f'frame {f} ({k_}): {db:.1f} dB {note}')
for k, f in enumerate(int(n * x) for x in (0.12, 0.31, 0.47, 0.66, 0.83)):
    still(b, f, os.path.join(out, f'spot-{k}-{f}.png'), 'crop=1920:1080:960:540')
open(os.path.join(out, 'verify.json'), 'w').write(json.dumps(dict(hd=pa, uhd=pb, slips=runs, sound=snd, spots=rows,
                                                                  errors=errs), indent=1))
print('VERIFY ' + ('OK' if not errs else 'FAILED: ' + '; '.join(errs)))
sys.exit(1 if errs else 0)
