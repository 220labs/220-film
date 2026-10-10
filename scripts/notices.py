#!/usr/bin/env python3
"""Write the open-source notices for 220 from what this Mac actually ran:
    python3 scripts/notices.py            -> docs/THIRD-PARTY-NOTICES.md and edl/credits-software.yaml

Read, never typed: the dynamic libraries ffmpeg, ffprobe and whisper-cli load (otool -L, walked to
the leaves), each one's Homebrew formula (version, SPDX licence, homepage from `brew info --json`),
its copyright lines and full licence text from the files the package installed in its Cellar
keg, and the Python side (interpreter, PyYAML, Pillow and the libraries Pillow's wheel bundles).
Rendering a film with these tools does not make the film a derivative of them — GPL and LGPL
terms bind distribution of the software, not of its output — so this is credit and thanks,
done the way open-source projects ask to be credited: name, version, licence, copyright, text.
"""
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
BINS = ['ffmpeg', 'ffprobe', 'whisper-cli']
LIC_NAMES = re.compile(r'^(COPYING|LICEN[CS]E|COPYRIGHT|NOTICE)(\.|$|-|_)', re.I)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def walk_libs():
    todo, seen = [], set()
    for b in BINS:
        p = sh('which', b).strip()
        if p:
            todo.append(p)
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        real = p.replace('@rpath/', '/opt/homebrew/lib/')
        for line in sh('otool', '-L', real).splitlines()[1:]:
            x = line.strip().split(' ')[0]
            if x and not x.startswith(('/System', '/usr/lib')):
                todo.append(x)
    kegs = set()
    for p in seen:
        r = os.path.realpath(p.replace('@rpath/', '/opt/homebrew/lib/'))
        if '/Cellar/' in r:
            kegs.add(r.split('/Cellar/')[1].split('/')[0])
    return sorted(kegs)


def keg_dir(name, version):
    for cand in glob.glob(f'/opt/homebrew/Cellar/{name}/*'):
        if os.path.basename(cand).startswith(version.split('_')[0]):
            return cand
    c = sorted(glob.glob(f'/opt/homebrew/Cellar/{name}/*'))
    return c[-1] if c else None


def licence_files(root):
    if not root:
        return []
    out = []
    for dp, dn, fn in os.walk(root):
        depth = dp[len(root):].count(os.sep)
        if depth > 3:
            continue
        for f in fn:
            if LIC_NAMES.match(f):
                out.append(os.path.join(dp, f))
    return sorted(out)[:4]


def copyright_lines(texts):
    lines = []
    for t in texts:
        for ln in t.splitlines():
            s = ln.strip(' *#/\t')
            if re.search(r'(Copyright|\(C\)|©)', s, re.I) and re.search(r'\d{4}', s) and len(s) < 160:
                if 'copyright notice' in s.lower() or 'copyright holder' in s.lower():
                    continue
                if s not in lines:
                    lines.append(s)
    return lines[:6]


def main():
    formulae = walk_libs()
    if 'whisper-cpp' not in formulae:
        formulae.append('whisper-cpp')
    info = json.loads(sh('brew', 'info', '--json=v2', *formulae))['formulae']
    comps = []
    for f in info:
        v = (f.get('installed') or [{}])[0].get('version') or f['versions']['stable']
        root = keg_dir(f['name'], v)
        files = licence_files(root)
        texts = []
        for p in files:
            try:
                texts.append(open(p, errors='replace').read())
            except OSError:
                pass
        comps.append(dict(name=f['name'], version=v, licence=f.get('license') or 'see text',
                          home=f.get('homepage', ''), files=files, texts=texts,
                          copy=copyright_lines(texts)))
    # Python side
    import PIL
    import yaml
    py = sys.version.split()[0]
    pil_dir = os.path.dirname(PIL.__file__)
    bundled = sorted(os.path.basename(p).split('.')[0] for p in glob.glob(os.path.join(pil_dir, '.dylibs', '*.dylib')))
    pycomps = [dict(name='Python', version=py, licence='PSF-2.0', home='https://www.python.org/'),
               dict(name='PyYAML', version=yaml.__version__, licence='MIT', home='https://pyyaml.org/'),
               dict(name='Pillow', version=PIL.__version__, licence='MIT-CMU', home='https://python-pillow.org/',
                    note='its wheel bundles: ' + ', '.join(bundled) if bundled else '')]
    for c in pycomps:
        sp = os.path.dirname(pil_dir)
        dist = [d for d in glob.glob(os.path.join(sp, '*.dist-info')) if os.path.basename(d).lower().startswith(c['name'].lower() + '-')]
        texts = []
        for d in dist:
            for p in glob.glob(os.path.join(d, '**', 'LICEN*'), recursive=True):
                texts.append(open(p, errors='replace').read())
        c['texts'] = texts; c['copy'] = copyright_lines(texts); c['files'] = []
    git = sh('git', '--version').split()[2]

    out = ['# Third-party notices — 220', '',
           'Generated by `scripts/notices.py` from what this Mac ran to make the film; do not edit by hand.',
           'Rendering with these tools does not make the film a derivative of them; this is credit, the way',
           'open-source projects ask for it: name, version, licence, copyright, and the licence text.', '',
           '| Component | Version | Licence (SPDX) | Home |', '| --- | --- | --- | --- |']
    for c in comps + pycomps:
        out.append(f"| {c['name']} | {c['version']} | {c['licence']} | {c['home']} |")
    out += [f'| Git | {git} | GPL-2.0-only | https://git-scm.com/ |',
            '| Whisper large-v3-turbo (model weights) | 2024 | MIT | https://github.com/openai/whisper |', '',
            'Not open source, used under their own terms: Claude Opus 5.5, Claude Sonnet 5.5 and Claude Code',
            '(Anthropic); macOS and its fonts Menlo and Apple Color Emoji (Apple); Trebuchet MS (Microsoft,',
            'shipped with macOS).', '']
    for c in comps + pycomps:
        out += [f"## {c['name']} {c['version']}", '', f"Licence: {c['licence']} · {c['home']}"]
        if c.get('note'):
            out += ['', c['note']]
        if c['copy']:
            out += [''] + [f'- {x}' for x in c['copy']]
        if not c['texts']:
            out += ['', f"Licence text: see {c['home']} (not shipped in the installed package)"]
        for p, t in zip(c.get('files') or [None] * len(c['texts']), c['texts']):
            label = os.path.basename(p) if p else 'licence'
            out += ['', f'<details><summary>{label}</summary>', '', '```', t.strip()[:60000], '```', '', '</details>']
        out.append('')
    open(os.path.join(REPO, 'docs', 'THIRD-PARTY-NOTICES.md'), 'w').write('\n'.join(out) + '\n')

    roll = ['# Open source', '~ with thanks to everyone who writes and maintains it', '']
    for c in sorted(comps + pycomps, key=lambda c: c['name'].lower()):
        line = f"{c['name']} {c['version']} · {c['licence']}"
        # roll lines stay under 70 characters: wrap long SPDX expressions
        while len(line) > 68:
            cut = line.rfind(' ', 0, 66)
            roll.append(line[:cut])
            line = '  ' + line[cut + 1:]
        roll.append(line)
    roll += [f'Git {git} · GPL-2.0-only', 'Whisper large-v3-turbo weights · MIT', '',
             '~ full licence texts: THIRD-PARTY-NOTICES.md']
    with open(os.path.join(REPO, 'edl', 'credits-software.yaml'), 'w') as f:
        f.write('# generated by scripts/notices.py — the open-source block of the credit roll\n')
        for r in roll:
            f.write(f'- {json.dumps(r, ensure_ascii=False)}\n')
    print('no text:', [c['name'] for c in comps + pycomps if not c['texts']])
    print(f'{len(comps)} Homebrew packages, {len(pycomps)} Python, '
          f'{sum(1 for c in comps if c["texts"])} with licence text found in their keg')


if __name__ == '__main__':
    main()
