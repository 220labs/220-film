#!/usr/bin/env python3
"""Render a cut of 220 from a text EDL:  python3 scripts/cut.py edl/<cut>.yaml <out.mp4>

The EDL is a list; each item is one of
  clip:    {clip: IMG_5781, in: "0:12.5", out: "0:26", subs: auto|en|tr|none, gain: dB, label: "..."}
  card:    {card: "CHAPTER ONE\\nIN WHICH ...", dur: 3, kind: chapter|title|plain}
  machine: {machine: [{t: "15:24:22", who: "Ilya", text: "..."}, ...], dur: 6, source: "..."}
  freeze:  {freeze: IMG_5840, at: "1:02", dur: 2, text: "..."}
  roll:    {roll: [...], dur: 60}
  music:   on a card or machine item only (never under talk): {file: music/A.wav, at: 0, gain: -3, credit: "..."}
Captions:  python3 scripts/cut.py --srt edl/<cut>.yaml <out.srt>  (renders nothing; YouTube SRT, normal case)
Media: camera originals in $INCOMING (read only; rule 1 of the brief: the source is never
edited, only cut), transcripts and audio in $WORK (scripts/ingest.sh). Both are set by whoever
renders; neither has a default, so no machine's disk is named here. Subtitles are burned in
from the whisper transcript of the same clip (subs: en = as spoken, tr = whisper's English
translation, auto = tr for Russian, en otherwise), shifted to the cut's in point.
The people's own words never sit in a cut list: hand subtitles (`lines:`) and whisper corrections
(`fix:`) live in a private sidecar, $WORK/lines/<cut>.yaml, merged in before rendering (with_sidecar).
The roll's "With" and "Also in the film" come from CREDITS.yaml (`@credits with`, `@credits also`).
Check:     python3 scripts/cut.py --lint edl/<cut>.yaml  (sources, in/out inside each clip, no lines:/fix:)
4K:        python3 scripts/cut.py --size 3840x2160 edl/<cut>.yaml <out.mp4>  draws the cards, captions, labels
           and the roll natively at that size (the layout is in 1080p units, times H/1080) and reads the
           footage from $UPSCALED, the upscaled spans `scripts/upscale.py film` writes, never a resize of the
           originals; encoded HEVC 10-bit (VideoToolbox where ffmpeg has it, else x265).
--no-sidecar renders a cut whose sidecar is missing with whisper's words (otherwise it stops).
Fonts: Trebuchet MS Bold, Menlo, Apple Color Emoji, from macOS's system folders, or from $FONTS (a
folder holding files of those names) wherever they are not; emoji only when a line has one. The
subtitles need a font whose family name is "Trebuchet MS" (libass matches the name, not the file).
The work folder of a render is deleted afterwards (--keep-tmp keeps it); segments are cached in
$WORK/segcache (at --size WxH: $WORK/segcache-WxH, so one edition's renders never evict the
other's), keyed by item, inputs and this script's bytes, and capped at 10 GB at 1080p and 10 GB times
(H/1080) squared at a larger size (40 GB at 4K, beside the 1080p cache), oldest out.
Every segment is rendered to one format (1920x1080, 30 fps, yuv420p, AAC 48 kHz stereo; at --size
3840x2160 HEVC yuv420p10le) so the
concat copies streams; the whole film then gets one two-pass loudnorm to -14 LUFS, -1 dBTP
(YouTube). Vertical clips sit centred on Ink (#15171C), never stretched or cropped.
Palette and type: 220 Labs brand.md (Ink, Paper, Mist; no other colour). Chapter cards after
Dogville's "in which" cards (docs/verite-style.md).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.realpath(__file__))
# No fallback to a checkout's folders or a home path: the script is public, the disks are not.
# INCOMING is needed only by a clip or a freeze, so the cards and the roll render without footage.
INCOMING = os.environ.get('INCOMING', '')
WORK = os.environ.get('WORK', '')


def font_path(mac):
    """A font by its macOS system path, or the file of the same name in $FONTS when that is set: on
    Linux (or a runner) the renderer supplies the fonts under these names. The file name is enough for
    cards, labels and the roll; the burned-in subtitles find their font by family name (libass reads
    the font's name table), so a stand-in for them must call itself "Trebuchet MS". A font missing
    from $FONTS falls back to the macOS path."""
    d = os.environ.get('FONTS')
    p = os.path.join(d, os.path.basename(mac)) if d else ''
    return p if p and os.path.exists(p) else mac


FONT = font_path('/System/Library/Fonts/Supplemental/Trebuchet MS Bold.ttf')
MONO = font_path('/System/Library/Fonts/Menlo.ttc')
EMOJI = font_path('/System/Library/Fonts/Apple Color Emoji.ttc')  # loaded only when a line has emoji
CACHE_CAP = 10 * 2 ** 30  # the segment cache at 1080p, oldest segments out past this (a cold film is ~1.5 GB;
# times K squared at a larger --size, whose cache is its own folder: a 4K film is ~12 GB)
with open(os.path.realpath(__file__), 'rb') as _f:
    SELF = hashlib.sha1(_f.read()).hexdigest()  # this script's bytes: part of every segment's cache key
# the fonts in use, by path, size and mtime (the emoji font is ~180 MB: not read), also in every key
FONTKEY = '|'.join(f'{f}:{os.path.getsize(f)}:{os.path.getmtime(f)}' if os.path.exists(f) else f
                   for f in (FONT, MONO, EMOJI)) + '|' + os.environ.get('FONTS', '')


def need_dir(var, what):
    p = os.environ.get(var, '')
    if not p or not os.path.isdir(p):
        raise SystemExit(f'cut: set ${var} to {what}' + (f' ({p} is not a folder)' if p else ''))
    return p


def need_fonts():
    for f in (FONT, MONO):
        if not os.path.exists(f):
            raise SystemExit(f'cut: no font {f}; set $FONTS to a folder holding "{os.path.basename(FONT)}" '
                             f'and "{os.path.basename(MONO)}" (and "{os.path.basename(EMOJI)}" for emoji)')
INK, PAPER, MIST = '0x15171C', '0xFFFFFF', '0xA9ADB6'
W, H, FPS = 1920, 1080, 30
# The layout is drawn in 1080p units: every margin, font size and blur box below is in pixels of a
# 1920x1080 frame, and px() scales it to the frame being rendered (--size), so a 4K cut draws its text
# at 4K rather than resizing a 1080p picture. The ASS captions keep PlayRes 1920x1080; libass scales them,
# the box (Outline) too only with ScaledBorderAndShadow: without it the 4K box was 144 px tall, not 172.
DW, DH = 1920, 1080
K = 1.0
PIXF = 'yuv420p'  # the pixel format every segment's filter chain ends in
UPSCALED = ''     # --size other than 1080p: the folder of upscaled spans, from $UPSCALED


def px(v):
    return int(round(v * K))


RENDER_VERSION = {'clip': '3', 'card': '2', 'machine': '3', 'freeze': '1', 'roll': '1'}
MIN_CUE = 1.0     # a wrapped piece shorter than this joins its neighbour (no flicker)
MIN_SLIVER = 0.4  # a cue left this short by an item's in or out point is dropped, not flashed
LABEL_SIZE = 40   # the clock: clip labels and machine-card times (the one-clock device)
AENC = ['-c:a', 'aac', '-b:a', '256k', '-ar', '48000', '-ac', '2']
ENC = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19', '-pix_fmt', 'yuv420p', '-r', str(FPS), *AENC]


def set_size(spec):
    """--size WxH: the frame, the layout's scale, and for anything above 1080p the HEVC 10-bit encode
    and the upscaled footage. 16:9 only, so the 1080p layout scales without moving anything."""
    global W, H, K, PIXF, ENC, UPSCALED
    m = re.fullmatch(r'(\d+)x(\d+)', spec or '')
    if not m or int(m.group(1)) * 9 != int(m.group(2)) * 16 or int(m.group(2)) % 2:
        raise SystemExit(f'cut: --size {spec!r}: give a 16:9 size such as 3840x2160')
    W, H = int(m.group(1)), int(m.group(2)); K = H / DH
    if H < DH:
        raise SystemExit(f'cut: --size {spec}: 1080p or larger (a smaller proxy is a resize of the 1080p cut)')
    if H == DH:
        return
    UPSCALED = need_dir('UPSCALED', f'the upscaled spans for a {W}x{H} cut (scripts/upscale.py film)')
    PIXF = 'yuv420p10le'
    encs = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True).stdout
    if os.environ.get('CUT_HEVC', 'videotoolbox' if 'hevc_videotoolbox' in encs else 'x265') == 'videotoolbox':
        # about 80 Mbit/s at 4K30: YouTube re-encodes whatever it gets; this keeps the 10-bit grade clean
        v = ['-c:v', 'hevc_videotoolbox', '-profile:v', 'main10', '-pix_fmt', 'p010le', '-b:v', f'{int(80 * K * K / 4)}M']
    else:
        v = ['-c:v', 'libx265', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p10le', '-x265-params', 'log-level=error']
    ENC = [*v, '-tag:v', 'hvc1', '-r', str(FPS), *AENC]


def sec(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).split(':')]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def find_src(clip):
    need_dir('INCOMING', f'the folder of camera originals ({clip} is a clip)')
    for ext in ('.MOV', '.mov', '.mp4', '.MP4'):
        p = os.path.join(INCOMING, clip + ext)
        if os.path.exists(p):
            return p
    return None


def src(clip):
    return find_src(clip) or sys.exit(f'cut: no source for {clip} in {INCOMING}')


SPAN = re.compile(r'(.+)@(\d+\.\d+)-(\d+\.\d+|end)\.mov')


def footage_ranges(edl):
    """(clip, a, b) for every piece of footage a cut list reads, exactly as footage() is asked for it:
    a clip's in..out, a freeze's one frame. scripts/upscale.py film upscales these ranges."""
    for it in edl:
        if 'clip' in it:
            yield it['clip'], sec(it.get('in', 0)), sec(it['out'])
        elif 'freeze' in it:
            at = sec(it.get('at', 0))
            yield it['freeze'], at, at + 1 / FPS


def seg_cache():
    """The segment cache of this frame size: one folder per edition, each under its own cap, so
    rendering the 1080p and the 4K cut in turn never prunes the other's segments."""
    return os.path.join(WORK, 'segcache' if H == DH else f'segcache-{W}x{H}')


def footage(clip, a, b):
    """(file, seek) for clip time a..b: the original at 1080p; at a larger --size the upscaled span in
    $UPSCALED that holds a..b (`<clip>@<start>-<end>.mov`, its first frame at <start> of the clip), so
    the seek is a - start. A span that runs to the clip's last frame is named `-end` and holds any b, as
    the original does when a list's out passes the clip's end. No span is an error, never a quiet
    fall-back to the 1080p original; two spans that both hold a..b are an error too (one is left from
    another plan or recipe), never a choice by name."""
    if not UPSCALED:
        return src(clip), a
    hits = [(f, float(m.group(2))) for f, m in ((f, SPAN.fullmatch(f)) for f in sorted(os.listdir(UPSCALED)))
            if m and m.group(1) == clip and float(m.group(2)) <= a + 1e-3 and (m.group(3) == 'end' or b <= float(m.group(3)) + 1e-3)]
    if len(hits) > 1:
        sys.exit(f'cut: {len(hits)} upscaled spans of {clip} hold {a:.3f}-{b:.3f} in {UPSCALED} ({", ".join(f for f, _ in hits)}); '
                 'move the stale ones aside (scripts/upscale.py film names them)')
    if hits:
        return os.path.join(UPSCALED, hits[0][0]), a - hits[0][1]
    sys.exit(f'cut: no upscaled span of {clip} holds {a:.3f}-{b:.3f} in {UPSCALED}; run scripts/upscale.py film')


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit(f'cut: failed: {" ".join(cmd[:6])} ...')
    return r


def ts(s):
    h, m, x = s.split(':')
    a, ms = x.split(',')
    return int(h) * 3600 + int(m) * 60 + int(a) + int(ms) / 1000


def lang(clip):
    p = os.path.join(WORK, 'transcripts', clip + '.json')
    try:
        return json.load(open(p))['result']['language']
    except Exception:
        return 'en'


def apply_fix(t, fix):
    """An item's `fix:` list, [[heard, said], ...]: whisper's mis-hearings corrected by ear, applied to
    each transcript segment before it is wrapped (the timing stays whisper's)."""
    for old, new in fix or []:
        t = t.replace(old, new)
    return t


def segments(clip, mode, fix=None):
    if mode == 'auto':
        mode = 'tr' if lang(clip) in ('ru', 'uk') else 'en'
    if mode == 'none':
        return []
    p = os.path.join(WORK, 'en' if mode == 'tr' else 'transcripts', clip + '.json')
    if not os.path.exists(p):
        sys.stderr.write(f'cut: WARN no {mode} transcript for {clip}; no subtitles\n')
        return []
    out, last = [], None
    for s in json.load(open(p))['transcription']:
        t = s['text'].strip()
        # whisper repeats itself on noise; a repeated line is noise, not speech
        if not t or t == last or re.fullmatch(r'[\W_]*', t):
            continue
        last = t
        out.append((ts(s['timestamps']['from']), ts(s['timestamps']['to']), apply_fix(t, fix)))
    return split_timed(out)


def split_timed(raw):
    """At most two lines on screen: a long segment is split at word boundaries into pieces of
    <= 84 characters, each shown for its share of the segment's time; and no segment runs past
    the start of the next (whisper's segments overlap without context, and overlapping events
    stack on screen)."""
    raw = sorted(raw)
    out = []
    for k, (a, b, t) in enumerate(raw):
        if k + 1 < len(raw):
            b = min(b, raw[k + 1][0])
        if b <= a:
            continue
        pieces = wrap(t, 84)
        total = sum(len(p) for p in pieces) or 1
        t0 = a; cur = []
        for p in pieces:
            t1 = t0 + (b - a) * len(p) / total
            cur.append([t0, t1, p])
            t0 = t1
        # a piece too short to read joins its neighbour (the tail joins the piece before it)
        k = 0
        while len(cur) > 1 and k < len(cur):
            if cur[k][1] - cur[k][0] < MIN_CUE:
                j = k - 1 if k > 0 else k + 1
                lo, hi = sorted((j, k))
                cur[lo:hi + 1] = [[cur[lo][0], cur[hi][1], cur[lo][2] + ' ' + cur[hi][2]]]
                k = 0
            else:
                k += 1
        out.extend(tuple(c) for c in cur)
    return out


def ass_time(t):
    t = max(t, 0)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def write_ass(path, subs):
    head = f"""[Script Info]
ScriptType: v4.00+
ScaledBorderAndShadow: yes
PlayResX: {DW}
PlayResY: {DH}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Trebuchet MS,58,&H00FFFFFF,&H00FFFFFF,&H001C1715,&H801C1715,-1,0,0,0,100,100,0,0,3,14,0,1,140,140,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, 'w') as f:
        f.write(head)
        for a, b, t in subs:
            t = t.replace('\n', ' ').replace('{', '(').replace('}', ')')
            f.write(f'Dialogue: 0,{ass_time(a)},{ass_time(b)},Sub,,0,0,0,,{t.upper()}\n')


def esc_path(p):
    return p.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'").replace(',', '\\,')


def clip_subs(it):
    """The subtitle pieces of a clip item, (start, end, text) in seconds from the segment's start;
    shared by render_clip (burned in, upper-cased there) and the --srt export."""
    clip = it['clip']; a = sec(it.get('in', 0)); b = sec(it['out'])
    subs = []
    if it.get('subs_file'):
        # a slice re-transcribed on its own (e.g. English speech whisper took for Russian):
        # its times are relative to subs_offset in the clip
        off = sec(it.get('subs_offset', 0)); raw = []; last = None
        p = os.path.join(WORK, it['subs_file'])
        if not os.path.exists(p):  # as segments(): a missing transcript is a cut without subtitles
            sys.stderr.write(f'cut: WARN no transcript {it["subs_file"]} for {clip}; no subtitles\n')
            return []
        for s in json.load(open(p))['transcription']:
            t = s['text'].strip()
            if t and t != last:
                raw.append((ts(s['timestamps']['from']) + off, ts(s['timestamps']['to']) + off, apply_fix(t, it.get('fix'))))
            last = t
        segs = split_timed(raw)
    elif it.get('lines'):
        # explicit subtitles, [start, end, text] in clip time: for lines no transcript holds
        # rightly (a Russian tail in an English clip), translated by the editing session
        segs = [(sec(x0), sec(x1), t) for x0, x1, t in it['lines']]
    else:
        segs = segments(clip, it.get('subs', 'auto'), it.get('fix'))
    stop = sec(it['subs_to']) if 'subs_to' in it else b
    for s0, s1, t in segs:
        s1 = min(s1, stop)
        if s1 <= a or s0 >= b or s0 >= stop:
            continue
        if min(s1, b) - max(s0, a) < MIN_SLIVER:
            continue  # a sentence the in or out point cut to a flash; its words are not in the shot
        subs.append((max(s0, a) - a, min(s1, b) - a, t))
    return subs


LOOK_CFG = os.path.join(HERE, '..', 'release', 'look.yaml')


def grade_filter():
    """The colour grade of the footage is switched in release/look.yaml (`grade: <name>` picks one of
    `looks`; `none` leaves the camera's colour). Cards, machine screens, captions and labels are not
    graded. Returns an ffmpeg filter chain, or '' for none; the chain is part of each clip's cache key."""
    cfg = yaml.safe_load(open(LOOK_CFG)) if os.path.exists(LOOK_CFG) else {}
    name = str(cfg.get('grade', 'none'))
    if name == 'none':
        return ''
    looks = cfg.get('looks') or {}
    if name not in looks:
        raise SystemExit(f'cut: release/look.yaml picks grade {name!r}, which is not under looks')
    # `{looks}` in a chain is release/looks/ (LUT files live there, as text), wherever the checkout is
    chain = str(looks[name]).replace('{looks}', esc_path(os.path.join(HERE, '..', 'release', 'looks')))
    # a chain is written for 1080p, like the layout: at a larger --size an unsharp kernel grows with the
    # frame (odd, at most 23, ffmpeg's limit), so the grade sharpens the same detail rather than finer
    return re.sub(r'unsharp=(\d+):(\d+)', lambda m: 'unsharp=' + ':'.join(
        str(min(23, (int(round(int(n) * K)) - 1) // 2 * 2 + 1)) for n in m.groups()), chain) if K != 1 else chain


def track_cmds(tr, a, d, path):
    """`blur_track: {file, pad}` blurs a face that moves: the file (JSON under $WORK, out of git like
    every other per-person datum) lists [t, x, y, w, h] samples in the clip's source time and output
    pixels, made by a face tracker. One box, sized to the largest face times `pad`, follows the samples
    frame by frame (linear between samples); across a gap longer than `gap` seconds (the face is out of
    shot) the box is parked off-frame. Writes an ffmpeg sendcmd file; returns (box w, box h, path)."""
    pts = sorted([p[0], *(v * K for v in p[1:5])] for p in json.load(open(os.path.join(WORK, tr['file']))))
    pad = float(tr.get('pad', 1.6)); gap = float(tr.get('gap', 1.0))
    bw = min(W, int(max(p[3] for p in pts) * pad) // 2 * 2); bh = min(H, int(max(p[4] for p in pts) * pad) // 2 * 2)
    lines = []
    for k in range(int(d * FPS) + 1):
        t = a + k / FPS
        j = next((i for i, p in enumerate(pts) if p[0] >= t), len(pts))
        prv = pts[j - 1] if j > 0 else None; nxt = pts[j] if j < len(pts) else None
        if prv and nxt and nxt[0] - prv[0] <= gap:
            f = (t - prv[0]) / max(nxt[0] - prv[0], 1e-6)
            cx = prv[1] + prv[3] / 2 + f * (nxt[1] + nxt[3] / 2 - prv[1] - prv[3] / 2)
            cy = prv[2] + prv[4] / 2 + f * (nxt[2] + nxt[4] / 2 - prv[2] - prv[4] / 2)
        else:
            near = min((p for p in (prv, nxt) if p), key=lambda p: abs(p[0] - t), default=None)
            if near is None or abs(near[0] - t) > gap / 2:
                lines.append(f'{k / FPS:.4f} overlay@bo x {W};'); continue
            cx = near[1] + near[3] / 2; cy = near[2] + near[4] / 2
        x = int(min(max(cx - bw / 2, 0), W - bw)); y = int(min(max(cy - bh / 2, 0), H - bh))
        lines.append(f'{k / FPS:.4f} crop@bc x {x}, crop@bc y {y}, overlay@bo x {x}, overlay@bo y {y};')
    open(path, 'w').write('\n'.join(lines) + '\n')
    return bw, bh, path


SOUND_CFG = os.path.join(HERE, '..', 'release', 'sound.yaml')


def voice_filter():
    """The dialogue pass on every clip's sound is switched in release/sound.yaml (`voice: <name>` picks one
    of `chains`; `none` leaves the camera's sound). `{work}` in a chain is $WORK (model files stay out of
    git). Returns an ffmpeg audio chain or ''; the chain is part of each clip's cache key."""
    cfg = yaml.safe_load(open(SOUND_CFG)) if os.path.exists(SOUND_CFG) else {}
    name = str(cfg.get('voice', 'none'))
    if name == 'none':
        return ''
    chains = cfg.get('chains') or {}
    if name not in chains:
        raise SystemExit(f'cut: release/sound.yaml picks voice {name!r}, which is not under chains')
    return str(chains[name]).replace('{work}', esc_path(WORK))


def render_clip(it, out, tmp):
    """`blur: [[t0, t1, x, y, w, h], ...]` blurs a box (output pixels, W x H frame) between two source
    times of the clip: a face of someone who has not agreed to be in the film (brief rule 4)."""
    clip = it['clip']; a = sec(it.get('in', 0)); b = sec(it['out']); d = b - a
    subs = clip_subs(it)
    zm = float(it.get('zoom', 1.0))
    zfilt = f',scale=iw*{zm:.4f}:ih*{zm:.4f},crop={W}:{H}:(iw-{W})/2:(ih-{H})/2' if zm > 1.001 else ''
    graph = [f'[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={INK}'
             f'{zfilt},setsar=1,fps={FPS},format={PIXF}[b0]']
    cur = 'b0'
    for i, (t0, t1, x, y, w, h) in enumerate(it.get('blur') or []):
        t0 = sec(t0) - a; t1 = sec(t1) - a; x, y, w, h = (px(v) for v in (x, y, w, h))
        graph.append(f'[{cur}]split[m{i}][c{i}]')
        graph.append(f'[c{i}]crop={w}:{h}:{x}:{y},boxblur=luma_radius={px(40)}:luma_power=3[k{i}]')
        graph.append(f"[m{i}][k{i}]overlay=x={x}:y={y}:enable='between(t,{t0:.3f},{t1:.3f})'[b{i + 1}]")
        cur = f'b{i + 1}'
    tr = it.get('blur_track')
    if tr:
        bw, bh, cmds = track_cmds(tr, a, d, os.path.join(tmp, os.path.basename(out) + '.blur.cmd'))
        graph.append(f"[{cur}]sendcmd=f='{esc_path(cmds)}',split[tm][tc]")
        graph.append(f'[tc]crop@bc={bw}:{bh}:0:0,boxblur=luma_radius={max(4, min(bw, bh) // 5)}:luma_power=3[tk]')
        graph.append(f'[tm][tk]overlay@bo=x={W}:y=0[bt]')
        cur = 'bt'
    post = [grade_filter() or 'null']
    if subs:
        ass = os.path.join(tmp, os.path.basename(out) + '.ass')
        write_ass(ass, subs)
        # libass finds "Trebuchet MS" through the system's fonts; with $FONTS it looks there first
        fd = f":fontsdir='{esc_path(os.environ['FONTS'])}'" if os.environ.get('FONTS') else ''
        post.append(f"subtitles='{esc_path(ass)}'{fd}")
    if it.get('label'):
        lf = os.path.join(tmp, os.path.basename(out) + '.label.txt')
        open(lf, 'w').write(it['label'])
        post.append(f"drawtext=fontfile='{esc_path(MONO)}':textfile='{esc_path(lf)}':fontcolor={MIST}:fontsize={px(LABEL_SIZE)}:"
                    f"x={px(60)}:y={px(50)}:box=1:boxcolor={INK}@0.7:boxborderw={px(12)}")
    graph.append(f'[{cur}]' + ','.join(post) + f',format={PIXF}[v]')
    g = float(it.get('gain', 0))
    vc = voice_filter()
    af = (f'aresample=48000,{vc + "," if vc else ""}volume={g}dB,afade=t=in:d=0.04,'
          f'afade=t=out:st={max(d - 0.04, 0):.3f}:d=0.04')
    path, seek = footage(clip, a, b)
    ain, mb = audio_bed(it.get('music'), d, input_idx=1)
    mb_bed = mb[:-3] + '[abed]'
    run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{seek:.6f}', '-t', f'{d:.3f}', '-i', path,
         *ain,
         '-filter_complex', ';'.join(graph) + f';[0:a]{af}[a0];{mb_bed};[a0][abed]amix=inputs=2:duration=first:normalize=0[a]',
         '-map', '[v]', '-map', '[a]', '-t', f'{d:.3f}', *ENC, out])


def audio_bed(music, dur, input_idx=1):
    """The audio of a card: silence, or a music bed (`music: {file, at, gain, fade, credit}`, the file
    relative to $WORK, never under talk: only cards, machine cards and the roll take one). Returns the
    input arguments for ffmpeg's music input and the filter that turns it into [a]."""
    if not music:
        return ['-f', 'lavfi', '-t', str(dur), '-i', 'anullsrc=r=48000:cl=stereo'], f'[{input_idx}:a]anull[a]'
    at = sec(music.get('at', 0)); g = float(music.get('gain', 0)); fd = float(music.get('fade', 0.25))
    fi = float(music.get('fade_in', fd)); fo = float(music.get('fade_out', fd))
    path = os.path.join(WORK, music['file'])
    if not os.path.exists(path):
        raise SystemExit(f'cut: no music file {path}')
    fades = []
    if fi > 0:
        fades.append(f'afade=t=in:st=0:d={fi:.3f}')
    if fo > 0:
        fades.append(f'afade=t=out:st={max(dur - fo, 0):.3f}:d={fo:.3f}')
    fade_chain = (',' + ','.join(fades)) if fades else ''
    af = (f'[{input_idx}:a]aresample=48000,aformat=channel_layouts=stereo,'
          f'atrim=start={at:.4f}:end={at + dur:.4f},asetpts=PTS-STARTPTS,'
          f'volume={g}dB,apad,atrim=0:{dur}{fade_chain}[a]')
    return ['-i', path], af


def text_card(out, tmp, lines, dur, size=84, font=FONT, color=PAPER, bg=INK, align='center', small=None, music=None):
    """lines: list of strings, one drawtext each, centred block."""
    vf = [f'color=c={bg}:s={W}x{H}:r={FPS}:d={dur}']
    n = len(lines); lh = int(size * 1.35); top = (H - n * lh) // 2  # size is in frame pixels already
    filt = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue  # a blank line keeps its place in the block, nothing is drawn
        tf = os.path.join(tmp, f'{os.path.basename(out)}.{i}.txt')
        open(tf, 'w').write(line)
        x = '(w-text_w)/2' if align == 'center' else str(px(180))
        filt.append(f"drawtext=fontfile='{esc_path(font)}':textfile='{esc_path(tf)}':fontcolor={color}:fontsize={size}:"
                    f'x={x}:y={top + i * lh}')
    if small:
        tf = os.path.join(tmp, f'{os.path.basename(out)}.small.txt')
        open(tf, 'w').write(small)
        filt.append(f"drawtext=fontfile='{esc_path(MONO)}':textfile='{esc_path(tf)}':fontcolor={MIST}:fontsize={px(30)}:"
                    f'x=(w-text_w)/2:y=h-{px(120)}')
    chain = ','.join(filt) if filt else 'null'
    ain, af = audio_bed(music, dur)
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', vf[0], *ain,
         '-filter_complex', f'[0:v]{chain},format={PIXF}[v];{af}',
         '-map', '[v]', '-map', '[a]', '-t', str(dur), *ENC, out])


def render_card(it, out, tmp):
    kind = it.get('kind', 'chapter'); dur = float(it.get('dur', 3))
    lines = str(it['card']).upper().split('\n') if kind != 'plain' else str(it['card']).split('\n')
    size = px(int(it.get('size', {'title': 260, 'chapter': 78, 'plain': 64}[kind])))
    # never wider than the frame less its margins: shrink the whole card to its longest line
    from PIL import ImageFont
    widest = max((ImageFont.truetype(FONT, size).getlength(l) for l in lines if l.strip()), default=0)
    if widest > W - px(240):
        size = int(size * (W - px(240)) / widest)
    text_card(out, tmp, lines, dur, size=size, color=it.get('color', PAPER), bg=it.get('bg', INK),
              small=it.get('small'), music=it.get('music'))


def wrap(t, n):
    words, lines, cur = t.split(), [], ''
    for w in words:
        if len(cur) + len(w) + 1 > n:
            lines.append(cur); cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        lines.append(cur)
    return lines


CREDITS_CFG = os.path.join(HERE, '..', 'release', 'credits.yaml')


def credit_logo(name):
    """A logo on the roll is switched in release/credits.yaml (`logos: {name: {show, file, height}}`), so
    showing it is a one-line pull request, not an edit to the cut. The file is a PNG under $WORK
    (binaries stay out of git). Returns (Image, height) or None when it is switched off."""
    cfg = yaml.safe_load(open(CREDITS_CFG)) if os.path.exists(CREDITS_CFG) else {}
    ent = (cfg.get('logos') or {}).get(name) or {}
    if not ent.get('show'):
        return None
    from PIL import Image
    path = os.path.join(WORK, ent['file'])
    if not os.path.exists(path):
        raise SystemExit(f'cut: logo {name} is switched on in release/credits.yaml but {path} is missing')
    im = Image.open(path).convert('RGBA'); h = px(int(ent.get('height', 140)))
    return im.resize((max(1, int(im.width * h / im.height)), h)), h


def _is_emoji(ch):
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or o in (0xFE0F, 0x200D)


def _draw_runs(im, d, x, y, text, font, size, color):
    """Menlo for text, Apple Color Emoji for emoji (drawtext cannot draw colour emoji), so a
    line like "🚢 Merged." is shown as it was written."""
    from PIL import Image, ImageDraw, ImageFont
    efont = None
    i = 0
    while i < len(text):
        j = i
        emo = _is_emoji(text[i])
        while j < len(text) and _is_emoji(text[j]) == emo:
            j += 1
        run_ = text[i:j]
        if emo:
            if efont is None:
                if not os.path.exists(EMOJI):
                    raise SystemExit(f'cut: {text!r} has emoji and there is no {EMOJI}; put it in $FONTS')
                efont = ImageFont.truetype(EMOJI, 160)  # the colour bitmap strikes exist at fixed sizes
            for ch in run_:
                if ord(ch) in (0xFE0F, 0x200D):
                    continue
                g = Image.new('RGBA', (200, 200), (0, 0, 0, 0))
                ImageDraw.Draw(g).text((0, 0), ch, font=efont, embedded_color=True)
                bb = g.getbbox()
                if bb:
                    g = g.crop(bb)
                    s = int(size * 1.05)
                    g = g.resize((s, int(s * g.height / max(g.width, 1))))
                    im.paste(g, (int(x), int(y + size * 0.1)), g)
                    x += s + size * 0.25
        else:
            d.text((x, y), run_, font=font, fill=color)
            x += d.textlength(run_, font=font)
        i = j


def render_machine(it, out, tmp):
    """The machine's words, verbatim: each entry appears at its turn and stays, set in Menlo on Ink,
    with its real time and speaker above it in Mist; the source sits small at the foot. One PNG per
    reveal step (Pillow, so emoji keep their colour), held for its share of the duration."""
    from PIL import Image, ImageDraw, ImageFont
    entries = it['machine']; dur = float(it.get('dur', 2.5 * len(entries) + 1))
    size = px(int(it.get('size', 46))); hsize = max(int(size * 0.6), px(LABEL_SIZE)); gap = px(34)
    mono = ImageFont.truetype(MONO, size); monoh = ImageFont.truetype(MONO, hsize)
    srcf = ImageFont.truetype(MONO, px(26))
    rgb = lambda c: tuple(int(c[k:k + 2], 16) for k in (2, 4, 6))
    blocks = []
    for e in entries:
        head = f"{e.get('t', '')}  {e.get('who', '')}".strip()
        blocks.append((head, wrap(str(e['text']), int(it.get('wrap', int(px(1500) / (size * 0.6)))))))
    height = lambda hs: sum(int(hs * 1.5) + len(ls) * int(size * 1.4) for _, ls in blocks) + gap * (len(blocks) - 1)
    if height(hsize) > H - px(180):  # a full card keeps its clock at the old ratio rather than run into the source line
        hsize = int(size * 0.6); monoh = ImageFont.truetype(MONO, hsize)
    total = height(hsize)
    y0 = max((H - total) // 2, px(70))
    lst = os.path.join(tmp, os.path.basename(out) + '.frames.txt')
    lead = 0.3; step = (dur - lead) / len(blocks)
    with open(lst, 'w') as f:
        for k in range(len(blocks) + 1):
            im = Image.new('RGB', (W, H), rgb(INK)); d = ImageDraw.Draw(im)
            y = y0
            for i, (head, ls) in enumerate(blocks[:k]):
                d.text((px(180), y), head, font=monoh, fill=rgb(MIST)); y += int(hsize * 1.5)
                for ln in ls:
                    _draw_runs(im, d, px(180), y, ln, mono, size, rgb(PAPER)); y += int(size * 1.4)
                y += gap
            if it.get('source'):
                d.text((px(180), H - px(90)), it['source'], font=srcf, fill=rgb(MIST))
            if (it.get('music') or {}).get('credit'):  # its own line: beside the source it ran off the frame
                d.text((px(180), H - px(130)), it['music']['credit'], font=srcf, fill=rgb(MIST))
            png = os.path.join(tmp, f'{os.path.basename(out)}.{k}.png')
            im.save(png)
            hold = lead if k == 0 else step
            f.write(f"file '{png}'\nduration {hold:.3f}\n")
        f.write(f"file '{png}'\n")  # the concat demuxer drops the last duration otherwise
    ain, af = audio_bed(it.get('music'), dur)
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, *ain,
         '-filter_complex', f'[0:v]fps={FPS},format={PIXF}[v];{af}', '-map', '[v]', '-map', '[a]',
         '-t', str(dur), *ENC, out])


def render_roll(it, out, tmp):
    """End credits that scroll: one tall Pillow image (headings in Trebuchet, entries in Menlo,
    emoji kept), moved up through the frame at a steady speed over `dur` seconds."""
    from PIL import Image, ImageDraw, ImageFont
    rgb = lambda c: tuple(int(c[k:k + 2], 16) for k in (2, 4, 6))
    hs, bs, ns = px(54), px(36), px(28)
    head = ImageFont.truetype(FONT, hs); body = ImageFont.truetype(MONO, bs); note = ImageFont.truetype(MONO, ns)
    rows = []
    for line in it['roll']:
        line = str(line)
        if line.startswith('@logo '):
            lg = credit_logo(line[6:].strip())
            if lg: rows.append(('i', lg[0], None, lg[1], None))
            continue
        if line.startswith('# '): rows.append(('h', line[2:].upper(), head, hs, PAPER))
        elif line.startswith('~ '): rows.append(('n', line[2:], note, ns, MIST))
        elif not line.strip(): rows.append(('g', '', note, ns, INK))
        else: rows.append(('b', line, body, bs, PAPER))
    hgt = H + sum(r[3] + px(40) if r[0] == 'i' else int(r[3] * (2.0 if r[0] == 'h' else 1.5)) for r in rows) + H // 2
    im = Image.new('RGB', (W, hgt), rgb(INK)); d = ImageDraw.Draw(im)
    y = H
    for k, text, font, size, color in rows:
        if k == 'i':
            im.paste(text, (int((W - text.width) / 2), y), text); y += size + px(40)
            continue
        if k == 'h': y += int(size * 0.5)
        if text:
            x = (W - d.textlength(text, font=font)) / 2
            _draw_runs(im, d, x, y, text, font, size, rgb(color))
        y += int(size * (1.5 if k == 'h' else 1.5))
    png = os.path.join(tmp, os.path.basename(out) + '.roll.png'); im.save(png)
    dur = float(it.get('dur', 60)); travel = hgt - H
    ain, af = audio_bed(it.get('music'), dur, input_idx=2)
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={INK}:s={W}x{H}:r={FPS}:d={dur}',
         '-loop', '1', '-i', png, *ain,
         '-filter_complex', f"[0:v][1:v]overlay=x=0:y='-{travel}*t/{dur}':shortest=1,format={PIXF}[v];{af}",
         '-map', '[v]', '-map', '[a]', '-t', str(dur), *ENC, out])


def render_freeze(it, out, tmp):
    clip = it['freeze']; at = sec(it.get('at', 0)); dur = float(it.get('dur', 2))
    png = os.path.join(tmp, os.path.basename(out) + '.png')
    path, seek = footage(clip, at, at + 1 / FPS)
    zm = float(it.get('zoom', 1.0))
    zfilt = f',scale=iw*{zm:.4f}:ih*{zm:.4f},crop={W}:{H}:(iw-{W})/2:(ih-{H})/2' if zm > 1.001 else ''
    gf = f',{grade_filter()}' if grade_filter() else ''
    run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{seek:.6f}', '-i', path, '-frames:v', '1',
         '-vf', f'scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={INK}{zfilt},setsar=1{gf}', png])
    filt = f'format={PIXF}'
    if it.get('text'):
        tf = os.path.join(tmp, os.path.basename(out) + '.txt')
        open(tf, 'w').write(str(it['text']).upper())
        bc = it.get('boxcolor', INK)
        fs = px(int(it.get('size', 72)))
        filt = (f"drawtext=fontfile='{esc_path(FONT)}':textfile='{esc_path(tf)}':fontcolor={PAPER}:fontsize={fs}:"
                f"x=(w-text_w)/2:y=h-{px(200)}:box=1:boxcolor={bc}:boxborderw={px(24)},format={PIXF}")
    ain, af = audio_bed(it.get('music'), dur, input_idx=1)
    run(['ffmpeg', '-v', 'error', '-y', '-loop', '1', '-framerate', str(FPS), '-t', str(dur), '-i', png,
         *ain,
         '-filter_complex', f'[0:v]{filt}[v];{af}', '-map', '[v]', '-map', '[a]', '-t', str(dur), *ENC, out])


# ---- YouTube captions (--srt): renders nothing, walks the EDL on the film's timeline ----

def item_dur(it):
    """An item's duration exactly as its renderer computes it."""
    if 'clip' in it: return sec(it['out']) - sec(it.get('in', 0))
    if 'card' in it: return float(it.get('dur', 3))
    if 'machine' in it: return float(it.get('dur', 2.5 * len(it['machine']) + 1))
    if 'freeze' in it: return float(it.get('dur', 2))
    if 'roll' in it: return float(it.get('dur', 60))
    raise SystemExit(f'cut: item has no kind: {it}')


def caption_cues(edl):
    """[(start, end, text)] for the whole film, text in normal case (not the burned-in upper case)."""
    cues, t0, last_card = [], 0.0, None
    for it in edl:
        d = item_dur(it)
        if 'clip' in it:
            for a, b, t in clip_subs(it):
                cues.append((t0 + a, t0 + b, t))
        elif 'machine' in it:
            ents = it['machine']; lead = 0.3; step = (d - lead) / len(ents)
            for k, e in enumerate(ents):
                who = str(e.get('who', '')).strip()
                txt = ' '.join(str(e['text']).split())
                cues.append((t0 + lead + k * step, t0 + d, f'{who}: {txt}' if who else txt))
        elif 'card' in it:
            lines = [l.strip() for l in str(it['card']).split('\n') if l.strip()]
            if lines and lines != last_card:  # the title card's repeats and empty cards add nothing
                cues.append((t0, t0 + d, '\n'.join(lines)))
            last_card = lines
        elif 'freeze' in it:
            if it.get('text'): cues.append((t0, t0 + d, ' '.join(str(it['text']).split())))
        elif 'roll' in it:
            cues.append((t0, t0 + d, '[credits]'))
        t0 += d
    return cues, t0


def srt_time(t):
    ms = int(round(max(t, 0) * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def write_srt(path, cues):
    """Strict SRT: no overlaps (an end is clipped to the next start); at most 2 lines of <= 42
    characters, a longer text split into consecutive cues sharing its time by characters."""
    cues = sorted(cues)
    out = []
    for k, (a, b, t) in enumerate(cues):
        if k + 1 < len(cues): b = min(b, cues[k + 1][0])
        if b - a < 0.05: continue
        src_lines = t.split('\n')
        lines = src_lines if len(src_lines) <= 2 and all(len(l) <= 42 for l in src_lines) else wrap(' '.join(t.split()), 42)
        # a single word longer than 42 stays whole; nothing sane to do with it
        chunks = [lines[i:i + 2] for i in range(0, len(lines), 2)]
        total = sum(len(' '.join(c)) for c in chunks) or 1
        c0 = a
        for c in chunks:
            c1 = c0 + (b - a) * len(' '.join(c)) / total
            out.append((c0, c1, '\n'.join(c)))
            c0 = c1
    # rounding to ms must not make neighbours overlap or touch backwards
    prev = 0
    with open(path, 'w') as f:
        for n, (a, b, t) in enumerate(out, 1):
            ia = max(int(round(a * 1000)), prev); ib = max(int(round(b * 1000)), ia + 1); prev = ib
            f.write(f'{n}\n{srt_time(ia / 1000)} --> {srt_time(ib / 1000)}\n{t}\n\n')
    return len(out)


# ---- what a cut list leaves out: the people's words (sidecar) and the credits (CREDITS.yaml) ----

SIDE_KEYS = ('lines', 'fix')
NO_SIDECAR = False  # --no-sidecar
CREDITS = os.path.join(HERE, '..', 'CREDITS.yaml')


def with_sidecar(edl, edl_path):
    """Merge $WORK/lines/<cut>.yaml into the cut list: a list of {item: <index>, clip: <id>, lines:,
    fix:}, each entry checked against the item it names (index and clip id both), never overriding a
    key the cut list already has. Merged before anything is hashed, so a segment's cache key, and so
    its render, is the same as when the words sat in the cut list. A cut list with clips and no
    sidecar stops the run: a hand cue may exist to keep a name off the screen, and whisper's words
    would burn it in. `--no-sidecar` renders with the transcripts as heard, on purpose.
    Limit: two items of the same clip swapped in the cut list (IMG_5842 at 66-69) still pass the check
    and take each other's words; move a clip's sidecar entries with its items."""
    path = os.path.join(WORK, 'lines', os.path.splitext(os.path.basename(edl_path))[0] + '.yaml')
    if not os.path.exists(path):
        if any('clip' in it for it in edl) and not any(k in it for it in edl for k in SIDE_KEYS):
            if not NO_SIDECAR:
                raise SystemExit(f'cut: no sidecar {path}; without it the clips show whisper\'s words, names '
                                 'included. Restore it, or pass --no-sidecar to render them anyway')
            sys.stderr.write(f'cut: WARN no sidecar {path}; every clip shows its transcript as whisper heard it\n')
        return edl
    for e in yaml.safe_load(open(path)) or []:
        n, clip = e.get('item'), e.get('clip')
        if not isinstance(n, int) or not isinstance(clip, str) or not 0 <= n < len(edl) or edl[n].get('clip') != clip:
            raise SystemExit(f'cut: sidecar {path}: item {n} is not clip {clip} in this cut list; '
                             'the cut list moved, so the sidecar must move with it')
        extra = set(e) - {'item', 'clip', *SIDE_KEYS}
        if extra:
            raise SystemExit(f'cut: sidecar {path}: item {n} carries {sorted(extra)}; only {SIDE_KEYS}')
        for k in SIDE_KEYS:
            if k in e:
                if k in edl[n]:
                    raise SystemExit(f'cut: item {n} ({clip}) has `{k}` in the cut list and in the sidecar')
                edl[n][k] = e[k]
    return edl


def with_credits(edl):
    """A roll line `@credits <section>` becomes the people of that section in CREDITS.yaml: `with`
    as one line of names joined by " · ", `also` as one line per person and its `~ note`. Expanded
    before hashing, so a merged credit re-renders the roll and nothing else."""
    people = None
    for it in edl:
        if 'roll' not in it:
            continue
        out = []
        for line in it['roll']:
            m = re.fullmatch(r'@credits (\w+)', str(line).strip())
            if not m:
                out.append(line); continue
            if people is None:
                people = (yaml.safe_load(open(CREDITS)) or {}).get('people') or []
            for p in people:
                name = str(p.get('name', '')).strip() if isinstance(p, dict) else ''
                if not name or name[0] in '#~@' or str(p.get('note', '')).startswith('@'):
                    raise SystemExit(f'cut: CREDITS.yaml entry {p!r} needs a name that does not start with # ~ or @')
            sect = [p for p in people if p.get('section') == m.group(1)]
            if not sect:
                raise SystemExit(f'cut: CREDITS.yaml has nobody under section {m.group(1)!r}')
            if m.group(1) == 'with':
                out.append(' · '.join(str(p['name']) for p in sect))
            else:
                for p in sect:
                    out.append(str(p['name']))
                    if p.get('note'):
                        out.append(f"~ {p['note']}")
        it['roll'] = out
    return edl


def with_continuous_music(edl):
    """When two adjacent items play the same music file continuously (`at_next == at_prev + dur_prev`),
    suppress the tail fade-out on the first and the head fade-in on the second so the music plays
    across the splice without dipping."""
    for i in range(len(edl) - 1):
        m0 = edl[i].get('music'); m1 = edl[i + 1].get('music')
        if m0 and m1 and m0.get('file') == m1.get('file'):
            end0 = sec(m0.get('at', 0)) + item_dur(edl[i])
            if abs(sec(m1.get('at', 0)) - end0) < 0.05:
                m0.setdefault('fade_out', 0.0)
                m1.setdefault('fade_in', 0.0)
    return edl


def load(edl_path):
    return with_continuous_music(with_credits(with_sidecar(yaml.safe_load(open(edl_path)), edl_path)))


def lint(edl_path):
    """`--lint`: the cut list as published, before any sidecar: every clip and freeze has its source in
    $INCOMING, every in, out and at lies inside the clip, and no item carries `lines:` or `fix:` (the
    people's words go in the sidecar). Reads the footage's durations; renders nothing."""
    edl = yaml.safe_load(open(edl_path)) or []
    bad, durs = [], {}
    for n, it in enumerate(edl):
        for k in SIDE_KEYS:
            if k in it:
                bad.append(f'item {n}: `{k}:` belongs in the sidecar $WORK/lines/, not the cut list')
        c = it.get('clip') or it.get('freeze')
        if not c:
            continue
        if c not in durs:
            p = find_src(c)
            durs[c] = p and float(run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                       '-of', 'csv=p=0', p]).stdout)
        d = durs[c]
        if d is None:
            bad.append(f'item {n}: no source for {c} in $INCOMING'); continue
        if 'clip' in it:
            a, b = sec(it.get('in', 0)), sec(it['out'])
            if not 0 <= a < b <= d + 0.001:
                bad.append(f'item {n}: {c} {a:g}-{b:g} is not inside 0-{d:.2f}')
        elif not 0 <= sec(it.get('at', 0)) < d:
            bad.append(f'item {n}: {c} at {sec(it.get("at", 0)):g} is not inside 0-{d:.2f}')
    for b in bad:
        print(f'cut: lint {edl_path}: {b}')
    print(f'cut: lint {edl_path}: {len(edl)} items, {len(durs)} clips, ' + (f'{len(bad)} problem(s)' if bad else 'ok'))
    return 1 if bad else 0


def prune_cache(cache, keep):
    """Oldest segments out (by mtime; a cache hit touches its file) until the cache is under CACHE_CAP * K * K,
    never one this render used nor one touched in the last 6 h (another render may be using it)."""
    files = sorted((os.path.getmtime(p), os.path.getsize(p), p) for p in
                   (os.path.join(cache, f) for f in os.listdir(cache)) if os.path.isfile(p))
    total = sum(f[1] for f in files)
    for _, size, p in files:
        if total <= CACHE_CAP * K * K:
            break
        if p not in keep and time.time() - os.path.getmtime(p) > 6 * 3600:
            os.remove(p); total -= size


def main(argv):
    global NO_SIDECAR
    keep = '--keep-tmp' in argv
    NO_SIDECAR = '--no-sidecar' in argv
    argv = [a for a in argv if a not in ('--keep-tmp', '--no-sidecar')]
    if '--size' in argv:
        i = argv.index('--size')
        size = argv[i + 1] if i + 1 < len(argv) else ''
        argv = argv[:i] + argv[i + 2:]
        if argv[:1] not in (['--srt'], ['--lint']):  # captions and the lint do not depend on the frame
            set_size(size)
    if len(argv) == 2 and argv[0] == '--lint':
        return lint(argv[1])
    if len(argv) not in (2, 3) or (len(argv) == 3) != (argv[0] == '--srt'):
        print(__doc__.strip()); return 2
    need_dir('WORK', 'the work folder (transcripts, music, sidecars, the segment cache)')
    if argv[0] == '--srt':
        cues, total = caption_cues(load(argv[1]))
        n = write_srt(argv[2], cues)
        print(f'cut: {argv[2]}  {n} cues; film duration {total:.2f} s')
        return 0
    need_fonts()
    edl = load(argv[0])
    final = argv[1]
    tmp = tempfile.mkdtemp(prefix='220cut-', dir=WORK)
    try:
        segs = render_all(edl, tmp)
        lst = os.path.join(tmp, 'list.txt')
        open(lst, 'w').write(''.join(f"file '{s}'\n" for s in segs))
        joined = os.path.join(tmp, 'joined.mp4')
        run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', joined])
        # two-pass loudnorm on the whole film: measure, then apply with the measured values
        r = run(['ffmpeg', '-hide_banner', '-i', joined, '-af', 'loudnorm=I=-14:TP=-1:LRA=11:print_format=json',
                 '-f', 'null', '-'])
        m = json.loads(r.stderr[r.stderr.rindex('{'):r.stderr.rindex('}') + 1])
        ln = (f"loudnorm=I=-14:TP=-1:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
              f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        run(['ffmpeg', '-v', 'error', '-y', '-i', joined, '-c:v', 'copy', *(['-tag:v', 'hvc1'] if H != DH else []),
             '-af', ln + ',aresample=48000', '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', final])
        d = float(run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', final]).stdout)
        prune_cache(seg_cache(), set(segs))
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)  # a joined copy of the whole film; the segments stay cached
    print(f'cut: {final}  {int(d // 60)}:{d % 60:04.1f}  ({len(segs)} segments; work in {tmp if keep else "deleted, --keep-tmp keeps it"})')


def render_all(edl, tmp):
    """Each item to one cached segment; returns their paths in order."""
    cache = seg_cache(); os.makedirs(cache, exist_ok=True)
    segs = []
    for n, it in enumerate(edl):
        # A segment is re-rendered only when its item, this script, or a file it reads has changed:
        # the key hashes all three (by content for the item, the script, the grade and voice chains and
        # release/credits.yaml; by mtime for the transcripts and music; by path, size and mtime for the
        # fonts). Not keyed: the LUT files under release/looks/, logo PNGs and voice-model files; after
        # changing one, empty the cache folder (seg_cache). Any edit to this script re-renders every segment, so
        # RENDER_VERSION is a label now, not a way to re-render one kind alone.
        deps = []
        c = it.get('clip')
        if c:
            for d in ('transcripts', 'en'):
                p = os.path.join(WORK, d, c + '.json')
                if os.path.exists(p): deps.append(str(os.path.getmtime(p)))
            if it.get('subs_file'):  # missing: rendered without subtitles, and re-rendered once it appears
                sf = os.path.join(WORK, it['subs_file'])
                deps.append(str(os.path.getmtime(sf)) if os.path.exists(sf) else 'missing')
            if grade_filter():
                deps.append(grade_filter())
            if voice_filter():
                deps.append(voice_filter())
        if it.get('blur_track'):
            deps.append(hashlib.sha1(open(os.path.join(WORK, it['blur_track']['file']), 'rb').read()).hexdigest())
        if 'roll' in it and os.path.exists(CREDITS_CFG):
            deps.append(hashlib.sha1(open(CREDITS_CFG, 'rb').read()).hexdigest())
        if it.get('music') and os.path.exists(os.path.join(WORK, it['music']['file'])):
            deps.append(str(os.path.getmtime(os.path.join(WORK, it['music']['file']))))
        kind = next((k for k in ('clip', 'card', 'machine', 'freeze', 'roll') if k in it), None)
        if not kind:
            raise SystemExit(f'cut: item {n} has no kind: {it}')
        ver = RENDER_VERSION[kind].encode() + SELF.encode() + FONTKEY.encode() + f'|{W}x{H}|{" ".join(ENC)}'.encode()
        if UPSCALED and kind in ('clip', 'freeze'):
            f, _ = footage(*next(footage_ranges([it])))
            deps.append(f'{os.path.basename(f)}:{os.path.getsize(f)}:{os.path.getmtime(f)}')
        key = hashlib.sha1(ver + json.dumps(it, sort_keys=True, default=str).encode() + '|'.join(deps).encode()).hexdigest()[:16]
        out = os.path.join(cache, f'{key}.mp4')
        hit = os.path.exists(out)
        if hit:
            os.utime(out)  # recently used: the last to leave when the cache is pruned
        else:
            part = os.path.join(tmp, f'seg{n:03d}.mp4')
            {'clip': render_clip, 'card': render_card, 'machine': render_machine,
             'freeze': render_freeze, 'roll': render_roll}[kind](it, part, tmp)
            os.replace(part, out)
        segs.append(out)
        print(f'seg {n:03d} {"cached " if hit else ""}{kind} {it.get("clip") or it.get("freeze") or ""}', flush=True)
    return segs


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
