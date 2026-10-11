#!/usr/bin/env python3
"""Upscale 220 to 4K with Topaz Video's own ffmpeg (its tvai_up filter):  python3 scripts/upscale.py <command>

  doctor                               find Topaz, print its version and this host's models, prove it renders
  bench --source FILE [--from cut|clip] [--shots a,b] [--variants x,y]
                                       render every shot of release/upscale.yaml through every variant
  sheet [--shots a,b]                  per shot: every variant's crop at one pixel per pixel, and a shimmer figure
  reel --variants a,b,c [--out FILE]   the A/B reel: each shot's crop, labelled, over one sound track
  render IN OUT [--variant V]          one file to 4K ProRes 422 HQ, checked frame by frame against IN
  film EDL... [--variant V] [--dry-run] the footage those cut lists use, as checked 4K spans for
                                       scripts/cut.py --size 3840x2160 (its $UPSCALED: <work>/film/<variant>)

The settings are release/upscale.yaml (the variants, the bench's shots, the pick); this script holds
no model choice of its own. Topaz is the user's own install, found at $TOPAZ_FFMPEG or macOS's
default place; its models are read from the app and downloaded by Topaz into $TVAI_MODEL_DATA_DIR
(default: Topaz's own folder in the user's Application Support). Topaz must be signed in: an
install that is not renders with a watermark or fails, so `doctor`, `render` and `film` first render
half a second of flat grey and stop on anything but flat grey (a watermark is the only detail there).
Renders and sheets go to $UPSCALE_WORK, else $WORK/upscale; nothing here names a machine's disk.
Which host renders which models is $UPSCALE_HOSTS, else release/upscale-hosts.yaml when it exists
(`<host>: {models: [...]}`, kept by whoever runs the renders); without either, a host renders
every variant its Topaz has. The host is $UPSCALE_HOST, else the machine's short name. Every render
appends a line to $UPSCALE_WORK/runs.tsv: host, Topaz version, variant, shot, frames, seconds,
so renders from different machines compare.

`bench` reads the camera originals (`--source <folder> --from clip`) or a rendered cut (`--source
<file> --from cut`, the shots' `cut` times), and skips any render that already exists, so a long bench resumes.
Needs Python 3 with PyYAML, Pillow; numpy and scipy for `reel`; Topaz Video and a stock ffmpeg.
"""
import argparse
import glob
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import cut  # noqa: E402  the cut lists' times and footage ranges, read exactly as the renderer reads them
SETTINGS = os.path.join(HERE, '..', 'release', 'upscale.yaml')
HOSTS = os.path.join(HERE, '..', 'release', 'upscale-hosts.yaml')  # optional, kept by whoever renders
MAC_TOPAZ = '/Applications/Topaz Video.app/Contents/MacOS/ffmpeg'
FPS = cut.FPS
PRORES = ['-c:v', 'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le', '-vendor', 'apl0']
# Topaz writes its sign-in to the log; none of it is kept. A span's file name (<clip>@<start>-<end>.mov)
# has an @ and dots, so it is taken out of a line before the test, not exempted by narrowing what an address is.
SECRET = re.compile(r'auth|token|eyJ|username|refresh|password|[\w.+-]+@[\w-]+\.\w+', re.I)
SPAN_NAME = re.compile(r'[\w.-]+@\d+\.\d+-(\d+\.\d+|end)\.mov(\.part\.mov)?')


def die(msg):
    sys.exit(f'upscale: {msg}')


def settings():
    return yaml.safe_load(open(SETTINGS))


def work():
    d = os.environ.get('UPSCALE_WORK') or (os.path.join(os.environ['WORK'], 'upscale') if os.environ.get('WORK') else '')
    if not d:
        die('set $UPSCALE_WORK (or $WORK) to the folder renders go to')
    os.makedirs(d, exist_ok=True)
    return d


class Topaz:
    def __init__(self):
        self.ff = os.environ.get('TOPAZ_FFMPEG') or MAC_TOPAZ
        if not os.path.exists(self.ff):
            die(f'no Topaz ffmpeg at {self.ff}; set $TOPAZ_FFMPEG to the one inside your Topaz Video install')
        macos = os.path.dirname(self.ff)
        self.fp = os.path.join(macos, 'ffprobe')
        self.models_dir = os.path.normpath(os.path.join(macos, '..', 'Resources', 'models'))
        plist = os.path.normpath(os.path.join(macos, '..', 'Info.plist'))
        self.version = plistlib.load(open(plist, 'rb')).get('CFBundleShortVersionString', '?') if os.path.exists(plist) else '?'
        data = os.environ.get('TVAI_MODEL_DATA_DIR') or os.path.expanduser(
            os.path.join('~', 'Library', 'Application Support', 'Topaz Labs LLC', 'Topaz Video', 'models'))
        os.makedirs(data, exist_ok=True)
        self.env = dict(os.environ, TVAI_MODEL_DIR=self.models_dir, TVAI_MODEL_DATA_DIR=data)

    def models(self):
        return sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(self.models_dir, '*-*.json')))


def host():
    return os.environ.get('UPSCALE_HOST') or socket.gethostname().split('.')[0].lower()


def host_models(tz):
    """The models this host renders: $UPSCALE_HOSTS' list for it (an empty list: none yet, run doctor),
    else, for a host not listed, everything its Topaz has."""
    f = os.environ.get('UPSCALE_HOSTS') or HOSTS
    if os.path.exists(f):
        h = (yaml.safe_load(open(f)) or {}).get(host())
        if h is not None:
            return set((h or {}).get('models') or [])
    return set(tz.models())


def chain(v, w, h, matrix=None):
    """The filter for a variant: tvai_up at the target size, then an exact resize (a model's own
    scale can land a few pixels off). `matrix` (swscale's name: bt709, bt2020, bt601) pins the
    YUV-RGB conversion Topaz needs to the source's own matrix both ways, so no colour shifts."""
    if not v.get('model'):
        return f'scale={w}:{h}:flags=lanczos'
    p = dict(v.get('params') or {})
    if p.get('scale', 0) == 0:
        p.update(w=w, h=h)
    opts = ':'.join(f'{k}={x}' for k, x in p.items())
    to_rgb, to_yuv = ((f'scale=in_color_matrix={matrix}:flags=lanczos,format=rgb48le',
                       f':out_color_matrix={matrix}') if matrix else ('format=rgb48le', ''))
    return (f'{to_rgb},tvai_up=model={v["model"]}:{opts}:device=-2:vram=1:instances=1,'
            f'scale=w={w}:h={h}:flags=lanczos{to_yuv}')


def probe(path, ff='ffprobe'):
    r = subprocess.run([ff, '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries',
                        'stream=width,height,nb_read_packets,r_frame_rate,codec_name,pix_fmt,field_order:format=duration',
                        '-of', 'json', path], capture_output=True, text=True)
    j = json.loads(r.stdout or '{}')
    s = (j.get('streams') or [{}])[0]
    s['duration'] = float(j.get('format', {}).get('duration', 0))
    s['frames'] = int(s.get('nb_read_packets', 0))
    return s


def pts(path, interval=None, decoded=False):
    """The video frames' presentation times, in order: the packets' (fast), or with `decoded` the frames
    a decode returns, which leaves out what an edit list discards (a clip trimmed in an editor)."""
    what = 'frame=best_effort_timestamp_time' if decoded else 'packet=pts_time'
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', *(['-read_intervals', interval] if interval else []),
                        '-show_entries', what, '-of', 'csv=p=0', path], capture_output=True, text=True)
    if r.returncode:
        die(f'ffprobe could not read the frame times of {path}: {r.stderr.strip()[:200]}')
    # a frame's line can carry empty side-data fields after the time ("0.500000,"): the first field is it
    return sorted(float(x) for x in (l.split(',')[0].strip() for l in r.stdout.splitlines()) if x not in ('', 'N/A'))


def run_topaz(tz, argv, log):
    t0 = time.time()
    r = subprocess.run([tz.ff, '-hide_banner', '-nostdin', '-y'] + argv, env=tz.env, capture_output=True, text=True)
    with open(log, 'w') as f:
        f.write('\n'.join(l for l in (r.stdout + r.stderr).splitlines() if not SECRET.search(SPAN_NAME.sub('', l))) + '\n')
    return r.returncode, time.time() - t0


def record(row):
    path = os.path.join(work(), 'runs.tsv')
    new = not os.path.exists(path)
    with open(path, 'a') as f:
        if new:
            f.write('when\thost\ttopaz\tvariant\tmodel\tshot\tframes\tseconds\ts_per_frame\tsource\tout\n')
        f.write('\t'.join(str(x) for x in row) + '\n')


FLAT = 12  # luma levels (8-bit) prob-4 may vary flat grey by (measured: 1 tuned, 3 auto); a logo spans far more


def preflight(tz):
    """Half a second of flat grey through Proteus (prob-4) at 2x. It must render 15 frames at 1280x720
    and stay flat: an install that is not signed in fails or stamps a watermark on every model, and on
    flat grey a watermark is the only thing with an edge. Proteus, not the variant's own model, so the
    bar (FLAT) is the one measured; a generative model may well texture flat grey. Returns (seconds,
    spread in levels)."""
    v = {'model': 'prob-4', 'params': {'scale': 0}}
    out = os.path.join(work(), 'preflight.mov')
    code, took = run_topaz(tz, ['-f', 'lavfi', '-i', 'color=c=0x808080:s=640x360:r=30:d=0.5',
                                '-vf', chain(v, 1280, 720)] + PRORES + [out], out + '.log.txt')
    p = probe(out) if code == 0 else {}
    if code or p.get('frames') != 15 or p.get('width') != 1280:
        die(f'Topaz did not render (exit {code}); is it signed in? Open Topaz Video and sign in. Log: {out}.log.txt')
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', out, '-vf', 'format=gray,signalstats,'
                        'metadata=print:key=lavfi.signalstats.YMIN:file=-,metadata=print:key=lavfi.signalstats.YMAX:file=-',
                        '-f', 'null', '-'], capture_output=True, text=True)
    lo = [float(x) for x in re.findall(r'YMIN=([\d.]+)', r.stdout)]
    hi = [float(x) for x in re.findall(r'YMAX=([\d.]+)', r.stdout)]
    spread = max((b - a for a, b in zip(lo, hi)), default=-1)
    if not lo or spread > FLAT:
        die(f'{out}: flat grey came back with {spread:.0f} levels of detail (at most {FLAT}): a watermark? '
            'Sign in to Topaz Video, then look at one frame')
    return took, spread


def cmd_doctor(a):
    tz, s = Topaz(), settings()
    have, mine = set(tz.models()), host_models(tz)
    print(f'topaz    {tz.version}  {tz.ff}')
    print(f'host     {host()}')
    for name, v in s['variants'].items():
        m = v.get('model')
        state = 'stock ffmpeg' if not m else ('ok' if m in have and m in mine else
                                              'not on this host' if m in have else 'not in this Topaz')
        print(f'  {name:16} {m or "-":10} {state}')
    took, spread = preflight(tz)
    print(f'render   ok: 15 frames at 1280x720 in {took:.1f} s, flat grey within {spread:.0f} levels: no watermark')


def shot_source(a, s, shot, d):
    """Cut the shot out of the source once, to ProRes, so every variant reads the same frames."""
    os.makedirs(d, exist_ok=True)
    src, wav = os.path.join(d, 'source.mov'), os.path.join(d, 'source.wav')
    if os.path.exists(src) and os.path.exists(wav):
        return src, wav
    if a.from_ == 'cut':
        inp, at = a.source, shot['cut']
    else:
        found = glob.glob(os.path.join(a.source, shot['clip'] + '.*'))
        if not found:
            die(f'{shot["clip"]} not in {a.source}')
        inp, at = found[0], shot['src']
    n = round(shot['dur'] * FPS)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{at}', '-i', inp, '-an', '-vf', f'fps={FPS}',
                    '-frames:v', str(n)] + PRORES + [src + '.part.mov'], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{at}', '-i', inp, '-vn', '-t', f'{n / FPS}',
                    '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', wav], check=True)
    os.replace(src + '.part.mov', src)
    return src, wav


def pick(names, allowed, what):
    if not names:
        return list(allowed)
    want = names.split(',')
    bad = [x for x in want if x not in allowed]
    if bad:
        die(f'no {what} {", ".join(bad)} (has: {", ".join(allowed)})')
    return want


def cmd_bench(a):
    tz, s = Topaz(), settings()
    w, h = s['target']['w'], s['target']['h']
    mine = host_models(tz)
    shots = {x['id']: x for x in s['shots']}
    for sid in pick(a.shots, shots, 'shot'):
        shot = shots[sid]
        d = os.path.join(work(), 'bench', sid)
        src, _ = shot_source(a, s, shot, d)
        n = probe(src)['frames']
        for name in pick(a.variants, s['variants'], 'variant'):
            v = s['variants'][name]
            out = os.path.join(d, f'{name}.mov')
            if os.path.exists(out):
                continue
            if v.get('shots') and sid not in v['shots'] and not a.variants:
                continue
            if v.get('model') and v['model'] not in mine:
                print(f'{sid:12} {name:16} skipped: {v["model"]} is not on {host()}')
                continue
            argv = ['-i', src, '-vf', chain(v, w, h)] + PRORES + [out + '.part.mov']
            if v.get('model'):
                code, sec = run_topaz(tz, argv, os.path.join(d, f'{name}.log.txt'))
            else:
                t0 = time.time()
                code = subprocess.run(['ffmpeg', '-v', 'error', '-y'] + argv).returncode
                sec = time.time() - t0
            p = probe(out + '.part.mov') if code == 0 else {}
            if code or p.get('frames') != n or (p.get('width'), p.get('height')) != (w, h):
                print(f'{sid:12} {name:16} FAILED (exit {code}, {p.get("frames")} of {n} frames); log in {d}')
                continue
            os.replace(out + '.part.mov', out)
            record([time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), host(), tz.version, name,
                    v.get('model', '-'), sid, n, f'{sec:.0f}', f'{sec / n:.2f}', os.path.basename(a.source), out])
            print(f'{sid:12} {name:16} {n} frames in {sec:.0f} s ({sec / n:.2f} s/frame)')


def shimmer(path, crop):
    """Mean luma change from one frame to the next inside the crop: a model that shimmers or
    boils raises it above the reference's, on the same frames."""
    x, y = crop
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf',
                        f'crop=1920:1080:{x}:{y},format=gray,tblend=all_mode=difference,signalstats,'
                        'metadata=print:key=lavfi.signalstats.YAVG:file=-', '-f', 'null', '-'],
                       capture_output=True, text=True)
    vals = [float(m) for m in re.findall(r'YAVG=([\d.]+)', r.stdout)][1:]
    return sum(vals) / len(vals) if vals else float('nan')


def sharpest(path):
    """The frame with the most edge energy: a handheld shot's middle frame is often motion blur,
    where every model looks alike."""
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf', 'format=gray,edgedetect=mode=wires,signalstats,'
                        'metadata=print:key=lavfi.signalstats.YAVG:file=-', '-f', 'null', '-'],
                       capture_output=True, text=True)
    vals = [float(m) for m in re.findall(r'YAVG=([\d.]+)', r.stdout)]
    return max(range(len(vals)), key=vals.__getitem__) if vals else 0


def font():
    for f in ('/System/Library/Fonts/Supplemental/Trebuchet MS Bold.ttf', '/System/Library/Fonts/Menlo.ttc'):
        p = os.path.join(os.environ['FONTS'], os.path.basename(f)) if os.environ.get('FONTS') else f
        if os.path.exists(p):
            return p
    die('no font: set $FONTS to a folder with "Trebuchet MS Bold.ttf"')


def esc(t):
    return t.replace('\\', '\\\\').replace(':', '\\:').replace("'", '’').replace('%', '\\%')


def cmd_sheet(a):
    from PIL import Image, ImageDraw, ImageFont
    s = settings()
    shots = {x['id']: x for x in s['shots']}
    for sid in pick(a.shots, shots, 'shot'):
        shot, d = shots[sid], os.path.join(work(), 'bench', sid)
        names = [n for n in s['variants'] if os.path.exists(os.path.join(d, f'{n}.mov'))]
        x, y = shot['crop']
        n = sharpest(os.path.join(d, 'source.mov'))
        tiles, rows = [], []
        for name in names:
            png = os.path.join(d, f'{name}-f{n}.png')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', os.path.join(d, f'{name}.mov'), '-vf',
                            f'select=eq(n\\,{n}),crop=1920:1080:{x}:{y}', '-frames:v', '1', png], check=True)
            tiles.append((name, png))
            rows.append((name, shimmer(os.path.join(d, f'{name}.mov'), shot['crop'])))
        # The sheet shows the middle quarter of each crop window, 960x540, at one pixel per pixel.
        cols = 3
        sheet = Image.new('RGB', (960 * cols, 540 * -(-len(tiles) // cols)), (21, 23, 28))
        f = ImageFont.truetype(font(), 34)
        for i, (name, png) in enumerate(tiles):
            im = Image.open(png).crop((480, 270, 1440, 810))
            ImageDraw.Draw(im).text((14, 10), s['variants'][name].get('label', name), font=f, fill=(255, 220, 0),
                                    stroke_width=3, stroke_fill=(0, 0, 0))
            sheet.paste(im, ((i % cols) * 960, (i // cols) * 540))
        out = os.path.join(work(), 'bench', f'sheet-{sid}.png')
        sheet.save(out)
        ref = dict(rows).get('lanczos')
        print(f'{sid}: {out}')
        for name, v in rows:
            print(f'  {name:16} shimmer {v:6.3f}' + (f'  ({v / ref:4.2f}x the plain resize)' if ref else ''))


def pcm(path):
    import numpy as np
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 'f32le', '-ac', '2', '-ar', '48000', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2)


def cmd_reel(a):
    """The ab-review-reel method: each piece cut on whole frames and labelled, 10 frames of black
    between pieces, the sound built as one PCM track and encoded once, then every piece's sound
    checked against its place."""
    import tempfile
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import fftconvolve
    s = settings()
    shots = {x['id']: x for x in s['shots']}
    vers = pick(a.variants, s['variants'], 'variant')
    sr, gap, secs = 48000, 10, a.seconds
    tmp = tempfile.mkdtemp(prefix='upscale-reel-')
    parts, snd, where, frames = [], [], [], 0
    fn, letters = font(), 'ABCDEFGHIJ'
    for sid in pick(a.shots, shots, 'shot'):
        shot, d = shots[sid], os.path.join(work(), 'bench', sid)
        x, y = shot['crop']
        nf = round(secs * FPS)
        audio = pcm(os.path.join(d, 'source.wav'))
        for i, name in enumerate(vers):
            src = os.path.join(d, f'{name}.mov')
            if not os.path.exists(src):
                die(f'{sid}: no {name} render; run bench first')
            label = esc(f'{shot["what"]}  ·  {letters[i]}  ·  {s["variants"][name].get("label", name)}')
            p = os.path.join(tmp, f'p{len(parts):03d}.mp4')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-an', '-vf',
                            f'crop=1920:1080:{x}:{y},trim=end_frame={nf},setpts=PTS-STARTPTS,'
                            f"drawtext=fontfile='{fn}':text='{label}':x=(w-tw)/2:y=h-th-48:fontsize=40:"
                            'fontcolor=yellow:box=1:boxcolor=black@0.7:boxborderw=14',
                            '-c:v', 'libx264', '-crf', '14', '-preset', 'slow', '-pix_fmt', 'yuv420p',
                            '-r', str(FPS), p], check=True)
            parts.append(p)
            n = nf * sr // FPS
            seg = audio[:n].copy()
            if len(seg) < n:
                seg = np.concatenate([seg, np.zeros((n - len(seg), 2), np.float32)])
            fi, fo = int(0.02 * sr), int(0.05 * sr)
            seg[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)[:, None]
            seg[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)[:, None]
            where.append((sum(len(z) for z in snd), audio, n))
            snd.append(seg)
            g = os.path.join(tmp, f'p{len(parts):03d}.mp4')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=black:s=1920x1080:r={FPS}',
                            '-vf', f'trim=end_frame={gap}', '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', g],
                           check=True)
            parts.append(g)
            snd.append(np.zeros((gap * sr // FPS, 2), np.float32))
            frames += nf + gap
    wav = os.path.join(tmp, 'sound.wav')
    wavfile.write(wav, sr, np.concatenate(snd))
    lst = os.path.join(tmp, 'list.txt')
    open(lst, 'w').write(''.join(f"file '{p}'\n" for p in parts))
    vid = os.path.join(tmp, 'picture.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', vid], check=True)
    out = a.out or os.path.join(work(), 'reel.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', vid, '-i', wav, '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
                    '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', out], check=True)
    r, worst = pcm(out).mean(axis=1), 0.0
    for at, audio, n in where:
        m = audio.mean(axis=1)
        if np.abs(m).max() < 1e-4:
            continue  # a silent shot has nothing to align
        W = sr // 10
        seg = r[at + W: at + n - W]
        big = np.concatenate([np.zeros(W, np.float32), m[:n], np.zeros(W, np.float32)])[W:W + len(seg) + 2 * W]
        lag = (np.argmax(fftconvolve(big, seg[::-1], 'valid')) - W) / sr * 1000
        worst = max(worst, abs(lag))
        if abs(lag) > 2:
            die(f'{out}: a piece at {at / sr:.2f} s sits {lag:+.1f} ms off')
    got = probe(out)['frames']
    if got != frames:
        die(f'{out}: {got} frames, expected {frames}')
    shutil.rmtree(tmp)
    print(f'{out}: {len(where)} pieces, {frames} frames ({frames / FPS:.1f} s); every piece within {worst:.1f} ms')


def fit(i, tw, th):
    """The largest even size of the source's shape inside the target: 2x for 1920x1080, less for a vertical clip."""
    k = min(tw / i['width'], th / i['height'])
    return int(i['width'] * k) // 2 * 2, int(i['height'] * k) // 2 * 2


def source_info(path):
    """A source's size, average frame rate, colour tags and duration; a quarter-turn rotation stops."""
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=width,height,avg_frame_rate,r_frame_rate,color_space,color_primaries,color_transfer:'
                        'stream_side_data=rotation:format=duration,start_time', '-of', 'json', path],
                       capture_output=True, text=True)
    j = json.loads(r.stdout or '{}')
    if not j.get('streams'):
        die(f'{path}: no video stream')
    st = j['streams'][0]
    if any(int(d.get('rotation') or 0) % 180 for d in st.get('side_data_list', [])):
        # ffmpeg turns the decoded frames; the size here is the coded one, so the upscale would stretch
        die(f'{path} is stored rotated a quarter turn: not handled yet')
    # the average, not r_frame_rate: a phone clip can say 120/1 and run at 30; r_frame_rate only where
    # there is no average (0/0: a single frame, a damaged header)
    num, den = (int(x) for x in st['avg_frame_rate'].split('/'))
    if not num or not den:
        num, den = (int(x) for x in st.get('r_frame_rate', '0/0').split('/'))
    if not num or not den:
        die(f'{path}: no frame rate')
    # ffmpeg's -ss and its output times count from the format's start, not the video's first frame
    return dict(st, rate=num / den, duration=float(j['format']['duration']),
                start=float(j['format'].get('start_time') or 0))


def upscale_args(v, i, w, h):
    """(pre, vf, sync, tags) for a source. Every frame keeps the source's own timestamp, at any rate: a
    phone clip is variable (1/30 s apart with a 0.035 s gap every few hundred; a constant 29.97 wandered up
    to 1.2 frames from it, so cut.py read the neighbouring frame), and a faster clip resampled here and
    again by cut.py's fps=30, on another grid, picked other frames than the 1080p cut's single fps=30.
    `pre` stays as the hook for a filter before the model (none now). The YUV-RGB matrix is the source's
    own both ways, and its colour tags are kept."""
    pre = ''
    vf = pre + chain(v, w, h, MATRIX.get(i.get('color_space')))
    sync = ['-fps_mode', 'passthrough', '-enc_time_base', 'demux']
    tags = [x for k, f in (('color_space', '-colorspace'), ('color_primaries', '-color_primaries'),
                           ('color_transfer', '-color_trc')) if i.get(k) and i[k] != 'unknown' for x in (f, i[k])]
    return pre, vf, sync, tags


def check_upscale(part, src_in, pre, w, h, sync, span_dur, t0, interval=None, decoded=False):
    """An upscale checked against its source: size, the frame count a stock decode of the same input
    gives, duration, every frame's time to 1.5 ms (passthrough), and three frames against a plain resize
    of the same source frames, luma and chroma apart (a colour-matrix slip shows in chroma), all three
    in one decode. `src_in` is the input as the render read it (any -ss/-t, then -i path); `t0` is where
    the output's time 0 sits in the source's own times (the format's start plus any -ss), `interval` a
    -read_intervals around it, and `decoded` reads the source's frame times by decoding (edit lists)."""
    errs, got = [], probe(part)
    r = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', *src_in, '-map', '0:v:0', *(['-vf', pre[:-1]] if pre else []),
                        *sync[:2], '-f', 'null', '-'], capture_output=True, text=True)
    want = int((re.findall(r'frame=\s*(\d+)', r.stderr) or ['0'])[-1])
    for k, x in (('width', w), ('height', h), ('frames', want)):
        if got.get(k) != x:
            errs.append(f'{k}: {got.get(k)} vs {x}')
    # the frame count above is the strict check; the duration may run up to a frame long at each end:
    # the first source frame after the start, and the rate rounding (a span 1/30 s apart from +0.026 s,
    # written at 29.97 from 0.033 s, came to 721 frames as stock ffmpeg: +2.4)
    if abs(got['duration'] - span_dur) > 3 / FPS:
        errs.append(f'duration: {got["duration"]:.3f} vs {span_dur:.3f}')
    if sync:  # every frame at its source time (an input -t keeps the frame that straddles the end, so the
        # upscale may hold one past it: compare the first len(out) source frames)
        out_t = pts(part)
        src_t = [t - t0 for t in pts(src_in[-1], interval, decoded) if t >= t0 - 1e-4][:len(out_t)]
        off = max((abs(x - y) for x, y in zip(src_t, out_t)), default=0)
        if len(src_t) != len(out_t) or off > 0.0015:
            errs.append(f'timestamps: off the source by up to {off * 1000:.1f} ms')
    # setpts=N on both sides: psnr pairs frames by time, and a phone clip's irregular timestamps
    # (193 frames at an average 29.99 fps) left frame n of the two sides unpaired
    ns = [int(want * frac) for frac in (0.1, 0.5, 0.9)]
    sel = 'select=' + '+'.join(f'eq(n\\,{n})' for n in ns) + f',setpts=N/({FPS}*TB)'
    stats = part + '.psnr.txt'
    subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', part, *src_in, '-filter_complex',
                    f'[0:v]{sel},format=yuv444p10le[a];[1:v]{pre}{sel},scale={w}:{h}:flags=lanczos,format=yuv444p10le[b];'
                    f"[a][b]psnr=stats_file='{cut.esc_path(stats)}'", '-f', 'null', '-'], capture_output=True, text=True)
    rows = open(stats).read().splitlines() if os.path.exists(stats) else []
    if os.path.exists(stats):
        os.remove(stats)
    if len(rows) != len(ns):
        errs.append(f'psnr: {len(rows)} of {len(ns)} frames compared')
    for n, row in zip(ns, rows):
        db = dict(re.findall(r'psnr_([yuv]):([\d.inf]+)', row))
        y, u, vv = (float(db.get(c, 0)) for c in 'yuv')
        if y < 30 or min(u, vv) < 36:
            errs.append(f'frame {n}: y {y:.1f} u {u:.1f} v {vv:.1f} dB')
    return errs, got


def cmd_render(a):
    """One file to 4K by `film`'s recipe and checks, at the largest size of its shape that fits, its sound copied."""
    tz, s = Topaz(), settings()
    name = a.variant or s.get('pick')
    if not name:
        die('no variant: release/upscale.yaml has no pick yet; pass --variant')
    v = s['variants'][name]
    if v.get('model'):
        preflight(tz)
    i = source_info(a.input)
    w, h = fit(i, s['target']['w'], s['target']['h'])
    pre, vf, sync, tags = upscale_args(v, i, w, h)
    part = a.output + '.part.mov'
    argv = ['-i', a.input, '-map', '0:v:0', '-map', '0:a?', '-vf', vf, *sync, *PRORES, *tags, '-c:a', 'copy', '-f', 'mov', part]
    code, took = run_topaz(tz, argv, a.output + '.log.txt')
    if code:
        die(f'Topaz exit {code}; log: {a.output}.log.txt')
    errs, out = check_upscale(part, ['-i', a.input], pre, w, h, sync, i['duration'], i['start'], decoded=True)
    if errs:
        die(f'{part} kept for a look; it does not match its source: ' + '; '.join(errs))
    os.replace(part, a.output)
    record([time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), host(), tz.version, name, v.get('model', '-'),
            '-', out['frames'], f'{took:.0f}', f'{took / out["frames"]:.2f}', os.path.basename(a.input), a.output])
    print(f'{a.output}: {w}x{h}, {out["frames"]} frames, {out["duration"]:.3f} s, as the source; {took / 60:.0f} min')


def used_spans(edls, margin, join, durs):
    """Per clip, the time ranges the cut lists read (cut.footage_ranges: a clip's in..out, a freeze's
    frame), each widened by `margin` and merged where less than `join` seconds apart: one Topaz run per
    span, so a model loads once for neighbouring shots. Returns [(clip, start, end)], clamped to the clip."""
    used = {}
    for p in edls:
        for clip, a, b in cut.footage_ranges(yaml.safe_load(open(p)) or []):
            used.setdefault(clip, []).append((a, b))
    out = []
    for clip in sorted(used):
        cur = None
        for a, b in sorted(used[clip]):
            a, b = max(a - margin, 0.0), min(b + margin, durs(clip))
            if cur and a - cur[1] < join:
                cur[1] = max(cur[1], b)
            else:
                cur and out.append((clip, *cur))
                cur = [a, b]
        out.append((clip, *cur))
    return out


MATRIX = {'bt709': 'bt709', 'bt2020nc': 'bt2020', 'bt2020c': 'bt2020', 'smpte170m': 'bt601', 'bt470bg': 'bt601'}


def cmd_film(a):
    """Upscale the footage a cut uses, as spans scripts/cut.py --size reads from $UPSCALED: each span
    `<clip>@<start>-<end>.mov` (`-end` when it runs to the clip's end), its first frame the source frame at <start>, at the largest size that
    fits the target (2x for a 1920x1080 clip, less for a vertical one), its own frame rate kept up to
    30 fps (cut.py makes everything 30), the camera's sound as PCM, the camera's colour tags kept.
    Each span is checked like `render`: size, frame count, duration, and three frames against a plain
    resize of the same source frames, luma and chroma apart (a colour-matrix slip shows in chroma)."""
    s = settings()
    name = a.variant or s.get('pick')
    if not name:
        die('no variant: release/upscale.yaml has no pick yet; pass --variant')
    v, (tw, th) = s['variants'][name], (s['target']['w'], s['target']['h'])
    film = s.get('film') or {}
    inc = os.environ.get('INCOMING', '')
    if not os.path.isdir(inc):
        die('set $INCOMING to the folder of camera originals')
    out_dir = os.path.join(work(), 'film', name)
    os.makedirs(out_dir, exist_ok=True)
    srcs, info = {}, {}

    def source(clip):
        if clip not in srcs:
            found = [p for p in glob.glob(os.path.join(inc, clip + '.*')) if os.path.splitext(p)[1].lower() in ('.mov', '.mp4')]
            if not found:
                if a.skip_missing:
                    return None
                die(f'{clip} not in {inc}')
            srcs[clip] = found[0]
            info[clip] = source_info(found[0])
        return srcs[clip]

    spans = used_spans(a.edl, film.get('margin', 0.5), film.get('join', 3.0),
                       lambda c: info[c]['duration'] if source(c) else float('inf'))
    missing = sorted({c for c, *_ in spans if not source(c)})
    spans = [x for x in spans if source(x[0])]
    profile = int(film.get('prores_profile', 1))
    prores = ['-c:v', 'prores_ks', '-profile:v', str(profile), '-pix_fmt', 'yuv422p10le', '-vendor', 'apl0']
    plan = []
    for clip, s0, s1 in spans:
        i = info[clip]
        # snapped down to the source frame at or before s0, and named to the microsecond: cut.py seeks
        # a - s0 into the span, and a seek a millisecond off moved fps=30's rounding where a frame sits
        # half a frame from the in point (stretches of up to 2 s one frame off)
        near = [t for t in pts(source(clip), f'{max(s0 - 3, 0):.3f}%{s0 + 0.1:.3f}') if t <= s0 + 1e-6]
        if not near and s0 > 0.1:  # a read that found nothing never widens a span back to the clip's start
            die(f'{clip}: no frame found at or before {s0:.3f} s; is the file whole and readable?')
        s0 = max(near or [i['start']])
        w, h = fit(i, tw, th)
        frames = round((s1 - s0) * min(i['rate'], FPS))
        # a span clamped to the clip's end is named `-end`: cut.py lets it hold a list's out past the end,
        # as the original does (a cut list's out of 35.0 on a 34.902 s clip)
        tag = f'{s0:.6f}-' + ('end' if s1 >= i['duration'] - 1e-6 else f'{s1:.3f}')
        plan.append((clip, s0, s1, w, h, frames, i, tag))
    total = sum(p[5] for p in plan)
    if missing:
        print(f'not in $INCOMING yet, skipped: {" ".join(missing)}')
    print(f'{len(plan)} spans of {len({p[0] for p in plan})} clips, {total} frames ({total / FPS / 60:.1f} min) '
          f'with {name}, to {out_dir}')
    if a.dry_run:
        for clip, s0, s1, w, h, frames, i, tag in plan:
            print(f'  {clip}@{tag}  {w}x{h}  {frames} frames{"  (to 30 fps)" if i["rate"] > FPS + 0.5 else ""}')
        return
    tz = Topaz()
    if v.get('model') and v['model'] not in host_models(tz):
        die(f'{name} needs {v["model"]}, which is not on {host()}')
    # Before any render: every span already here must be one of this plan's, made by this recipe.
    # cut.py reads whichever span holds an item's range, so a stale or foreign span is moved aside
    # first, never left beside the new ones; and a mismatch stops the run now, not hours in.
    jobs, bad = [], []
    for clip, s0, s1, w, h, frames, i, tag in plan:
        pre, vf, sync, tags = upscale_args(v, i, w, h)
        out = os.path.join(out_dir, f'{clip}@{tag}.mov')
        # what made the pixels, beside each span: a span is reused only when this matches, so a changed
        # variant, profile, Topaz version or timing never passes old pixels off as new
        recipe = dict(variant=name, vf=vf, prores=profile, topaz=tz.version, timing='passthrough')
        if os.path.exists(out):
            try:
                had = json.load(open(out + '.json'))
            except (OSError, ValueError):
                had = None
            if had != recipe:
                bad.append(f'{os.path.basename(out)} (another recipe: '
                           f'{"none recorded" if had is None else ", ".join(k for k in recipe if had.get(k) != recipe[k])})')
        jobs.append((clip, s0, s1, w, h, i, pre, vf, sync, tags, out, recipe))
    mine = {os.path.basename(j[10]) for j in jobs}
    bad += [f'{f} (not in this plan)' for f in sorted(os.listdir(out_dir))
            if cut.SPAN.fullmatch(f) and f not in mine and cut.SPAN.fullmatch(f).group(1) not in missing]
    if bad:
        die(f'move these aside in {out_dir} to render: ' + '; '.join(bad))
    if v.get('model'):
        preflight(tz)
    done = 0
    for clip, s0, s1, w, h, i, pre, vf, sync, tags, out, recipe in jobs:
        done += 1
        if os.path.exists(out):
            print(f'[{done}/{len(plan)}] {os.path.basename(out)} exists')
            continue
        part = out + '.part.mov'
        cut_in = ['-ss', f'{s0 - i["start"]:.6f}', '-t', f'{s1 - s0:.6f}', '-i', source(clip)]
        argv = cut_in + ['-map', '0:v:0', '-map', '0:a:0?', '-vf', vf, *sync, *prores, *tags, '-c:a', 'pcm_s24le', '-f', 'mov', part]
        code, took = run_topaz(tz, argv, out + '.log.txt')
        if code:
            die(f'{clip}: Topaz exit {code}; log: {out}.log.txt')
        errs, got = check_upscale(part, cut_in, pre, w, h, sync, s1 - s0, s0, f'{max(s0 - 2, 0):.3f}%{s1 + 2:.3f}')
        if errs:
            die(f'{part} kept for a look; it does not match its source: ' + '; '.join(errs))
        # the recipe lands first and whole: a span without one is refused, so a crash between the two
        # leaves a recipe with no span (rendered again), never a checked span nobody may reuse
        json.dump(recipe, open(out + '.json.part', 'w'), indent=1)
        os.replace(out + '.json.part', out + '.json')
        os.replace(part, out)
        record([time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), host(), tz.version, name, v.get('model', '-'),
                f'film:{clip}@{s0:.3f}', got['frames'], f'{took:.0f}', f'{took / got["frames"]:.2f}', clip, out])
        print(f'[{done}/{len(plan)}] {os.path.basename(out)}  {w}x{h}, {got["frames"]} frames in {took:.0f} s '
              f'({took / got["frames"]:.2f} s/frame), checked', flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('doctor')
    b = sub.add_parser('bench')
    b.add_argument('--source', required=True, help='a rendered cut (--from cut) or the camera originals folder')
    b.add_argument('--from', dest='from_', choices=('cut', 'clip'), default='clip')
    b.add_argument('--shots')
    b.add_argument('--variants', help='these only (also runs a variant on shots its `shots` leaves out)')
    sh = sub.add_parser('sheet')
    sh.add_argument('--shots')
    r = sub.add_parser('reel')
    r.add_argument('--variants', required=True, help='in the order of the question, the reference first')
    r.add_argument('--shots')
    r.add_argument('--seconds', type=float, default=4)
    r.add_argument('--out')
    rn = sub.add_parser('render')
    rn.add_argument('input')
    rn.add_argument('output')
    rn.add_argument('--variant')
    fm = sub.add_parser('film')
    fm.add_argument('edl', nargs='+', help='the cut lists whose footage to upscale')
    fm.add_argument('--variant')
    fm.add_argument('--dry-run', action='store_true', help='list the spans and their frames; render nothing')
    fm.add_argument('--skip-missing', action='store_true', help='render the spans whose clip is here; name the rest')
    a = ap.parse_args()
    {'doctor': cmd_doctor, 'bench': cmd_bench, 'sheet': cmd_sheet, 'reel': cmd_reel, 'render': cmd_render,
     'film': cmd_film}[a.cmd](a)


if __name__ == '__main__':
    main()
