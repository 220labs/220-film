# 220 — an AI autobiography

*A film about AI, edited by AI. The AI in it is real.*

## Film as Code

220 is developed like software. The cut lists, the render scripts, the music prompts and the
credits live here. Anyone can improve the code; a few trusted contributors edit the film itself,
from review copies of the footage.

Every push to `main` renders the film and its teaser, and both go up on the 220 YouTube
channel, unlisted. At 05:55 PT the day's version is cut: the latest build from `main` becomes the
next minor version, its teaser goes public and its film premieres at 11:11 PT. Anything that lands
after 05:55 goes into the next day's version. Until the daily build runs, each version is cut by
hand on the same clock.

## Where it stands

v0.0 premiered on 2026-10-09. v0.1 is this recipe, public and tagged since 2026-10-10: the cut
lists, the scripts, the music prompts and the credits. Its film goes up once the build uploads
it, which is not wired yet.

## What is here

| Path | What it is |
| --- | --- |
| `edl/clean-v1.yaml` | the film, the edition on YouTube: every clip with its in and out point, every card, every machine line, the credit roll |
| `edl/teaser-v1.yaml` | the teaser, in the same format |
| `scripts/cut.py` | renders a cut list into a film with ffmpeg; `--srt` writes its captions |
| `scripts/ingest.sh`, `scripts/notices.py` | the transcripts the subtitles come from; the third-party notices |
| `release/` | the colour grade, the dialogue pass and the roll's switches that `cut.py` reads |
| `music/` | the prompts of the film's AI music, and its provenance |
| `CREDITS.yaml` | the people in the film, as the roll shows them |
| `docs/THIRD-PARTY-NOTICES.md`, `edl/credits-software.yaml` | the open-source software the film was made with |

The footage is not here and never will be.

## The rules of the documentary

From the brief, 2026-10-08:

1. **Nothing in the source is edited.** Footage is cut, never altered in content: no
   generated shot posing as footage, no re-enactment, no reframed meaning. Session text,
   hook messages, commits and pull requests appear verbatim, with their real timestamps; a
   screen grab is of the real page or a typeset copy of the real line, labelled with its
   source and time.
2. **Claude and Ilya's work is the centerpiece**, not a cutaway: the agents' day is a second
   protagonist with its own chapters.
3. **Generated material is labelled as such** and used only where it cannot be mistaken for
   the record: title cards, chapter cards, music.
4. **Privacy is part of the cut**: no credentials, no employer matters, no third party's
   private words, nobody on screen who has not agreed.

So a cut list names clips by their ids and never quotes what anyone says on camera; the machine
cards quote typed prompts and the agents' replies, verbatim. The subtitles come from transcripts
that stay with the footage.

## Rendering

`cut.py` needs Python 3 with PyYAML and Pillow, ffmpeg, and three fonts (Trebuchet MS Bold,
Menlo, Apple Color Emoji: macOS has them; elsewhere put them in a folder named by `$FONTS`).
Two folders are set by whoever renders: `$INCOMING` (the camera originals) and `$WORK`
(transcripts, music, the segment cache). The roll renders from the cut list and `CREDITS.yaml`
alone; most cards carry a music bed, which they read from `$WORK/music/` (not in this repo), and
the clips need the footage, which is not public. The people's hand-checked subtitles live in
`$WORK/lines/`, with the footage's kit, so a render without them takes `--no-sidecar` and shows
the transcripts as whisper heard them — including any name the hand cues keep off screen.

```
INCOMING=/path/to/footage WORK=/path/to/work python3 scripts/cut.py edl/clean-v1.yaml 220.mp4
```

## Licence

The recipe is MIT: the scripts, the cut lists, the render settings and the music prompts, as
works. The words shown on screen (every quoted string under `machine:`, `card:` and `roll:`) and
the names in `CREDITS.yaml` are part of the film. The film, its footage, and the faces, voices
and words of the people in it are all rights reserved: no reuse, no redistribution, no training.
Films built from contributions are shown only on the 220 channel, at our discretion. The AI
music is in the public domain, CC0 ([music/PROVENANCE.md](music/PROVENANCE.md)). The full text: [LICENSE.md](LICENSE.md).

## Contributing

- **Your credit.** Everyone who appears in the film can be listed in its credits. Open a pull
  request that adds or corrects your own line in [CREDITS.yaml](CREDITS.yaml), with your name
  as you want it shown. A merged credit goes into the YouTube description and a pinned comment
  the same day, and onto the roll at the next upload. To be removed or blurred instead, write to
  the contact below; that is not a pull request.
- **The code.** Improvements are welcome by pull request. The contributor terms are being
  reviewed and will be published here; until then, write to the contact before a large change.
- **The film.** Editing starts with a few trusted contributors, who receive review copies of the
  footage under a no-distribution licence; to ask, write to the contact.

## Roadmap

- v0.0: premiered on 2026-10-09.
- v0.1: the recipe and the credits, public and tagged since 2026-10-10; its film goes up once
  the build uploads it.
- Next: the making-of and the rough cut lists.
- Next: review copies of the footage for trusted contributors.
- Next: the daily build from `main`: a teaser on every push, a version cut at 05:55 PT, its
  premiere at 11:11 PT.
- 1.0: a checklist, not a date.

The full list is in [ROADMAP.md](ROADMAP.md).

Contact: hello@ffpp.pictures · 220labs.dev
