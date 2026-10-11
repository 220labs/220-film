"""A published version is never altered: each video listed in release/frozen.tsv must keep, in release/youtube.yaml,
exactly what it was published with. check.sh runs this, and so does the YouTube workflow before an apply or an upload;
it exits 1 naming each one that moved, so a branch that edits a frozen entry fails before it lands, and a run from a
stale branch never sends it. A laptop `bin/tool youtube apply` from an unlanded worktree does not run it: run it first.

    python3 scripts/frozen.py            # check
    python3 scripts/frozen.py --print    # the hash of every entry now, to freeze a newly published version
    python3 scripts/frozen.py --no-media # the entries only (CI, where the state file comes from a cache)

The whole entry is frozen, privacy and playlists included, not only what the viewer reads: the hash is sha256 of
what apply would send, the config's `defaults:` filled in under the entry (apply falls back to them; `notify` is
upload-only) and a `description_file:` read in place of its path, as sorted JSON; a comment or a quoting style may
change, a value may not. A thumbnail or a caption file is named by path, and apply sends it when its sha256 differs
from the one recorded in release/youtube.state.json; so for a frozen entry the file's sha256 in MEDIA-MANIFEST.tsv
must equal the recorded one. Where the manifest is absent (the public twin) that half is skipped with a note.
A version is frozen by appending its keys' lines to release/frozen.tsv in the commit that records it published; a
public entry with an id that is not frozen is named on every run. check.sh also refuses a line of main's frozen.tsv
that this branch changed or dropped, so re-hashing an edited entry is no way past this.
"""
import hashlib
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "release" / "youtube.yaml"
FROZEN = REPO / "release" / "frozen.tsv"
STATE = REPO / "release" / "youtube.state.json"
MANIFEST = REPO / "MEDIA-MANIFEST.tsv"


def sent(cfg, key):
    defaults = {k: v for k, v in (cfg.get("defaults") or {}).items() if k != "notify"}
    entry = {**defaults, **cfg["videos"][key]}
    if entry.get("description_file"):
        entry["description_file"] = (REPO / entry["description_file"]).read_text(encoding="utf-8")
    return entry


def digest(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def media_faults(key, entry, state, manifest):
    """Each thumbnail or caption file of a frozen entry whose manifest sha256 is not the one apply recorded sending."""
    rec = state.get(key, {})
    files = []
    if entry.get("thumbnail"):
        files.append((entry["thumbnail"], rec.get("thumb_sha")))
    for lang, path in (entry.get("captions") or {}).items():
        files.append((path, (rec.get("captions") or {}).get(lang)))
    return [f"{key}: {path} is {manifest.get(path, 'not in the manifest')}, sent as {want}"
            for path, want in files if manifest.get(path) != want]


def frozen_lines():
    """{key: hash} from release/frozen.tsv; a line that is not key<TAB>hash is an error, never a skipped line."""
    out = {}
    for n, line in enumerate(FROZEN.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or len(parts[1]) != 64:
            sys.exit(f"frozen.py: {FROZEN.relative_to(REPO)} line {n} is not key<TAB>sha256: {line!r}")
        out[parts[0]] = parts[1]
    return out


def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if "--print" in sys.argv[1:]:
        for key in cfg["videos"]:
            print(f"{key}\t{digest(sent(cfg, key))}")
        return 0
    frozen = frozen_lines()
    manifest = None
    if "--no-media" in sys.argv[1:]:
        pass
    elif MANIFEST.exists():
        rows = [ln.split("\t") for ln in MANIFEST.read_text(encoding="utf-8").splitlines()[1:] if ln.strip()]
        manifest = {r[0]: r[2] for r in rows if len(r) > 2}
    else:
        print(f"frozen.py: no {MANIFEST.name}: thumbnail and caption files not checked")
    state = json.loads(STATE.read_text(encoding="utf-8")) if manifest is not None and STATE.exists() else {}
    bad = 0
    for key, want in frozen.items():
        if key not in cfg["videos"]:
            print(f"frozen.py: {key} is frozen but gone from {CONFIG.relative_to(REPO)}")
            bad += 1
            continue
        if digest(sent(cfg, key)) != want:
            print(f"frozen.py: {key} ({cfg['videos'][key].get('id')}) differs from what it was published with")
            bad += 1
        if manifest is not None:
            for fault in media_faults(key, cfg["videos"][key], state, manifest):
                print(f"frozen.py: {fault}")
                bad += 1
    for key, v in cfg["videos"].items():
        if key not in frozen and v.get("id") and sent(cfg, key).get("privacy") == "public":
            print(f"frozen.py: note: {key} ({v['id']}) is public and not frozen: freeze it once its version is published")
    if not bad:
        print(f"frozen.py: {len(frozen)} frozen entries, as published")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
