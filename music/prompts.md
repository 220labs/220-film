# 220 — the music prompts

The music on the film's AI cards is seven AI-generated stand-ins, made with Google Flow Music on
2026-10-09, one text message per track, one take each. These are the messages, verbatim: eight
written, seven sent. Each names styles and a tempo, never an artist. The editor, Claude, wrote
them; where each track sits in the film is the `music:` key on a card in `edl/`.

The tracks are AI output and no copyright is claimed in them ([PROVENANCE.md](PROVENANCE.md)). This file is MIT, like the rest of the recipe
([LICENSE.md](../LICENSE.md)).

| Slot | Prompt | djb2 | Characters | Sent |
|---|---|---|---|---|
| A · night shift | `music-A-night-v1` | `8647416a` | 359 | yes |
| B · the outage | `music-B-outage-v1` | `49215ef8` | 308 | yes |
| C · waiting for Ilya | `music-C-waiting-v1` | `8f95671a` | 293 | yes |
| D · the breaker is built | `music-D-breaker-v1` | `3536f349` | 450 | yes |
| E · going to sleep | `music-E-sleep-v1` | `467f49d2` | 261 | yes |
| F · the film is made | `music-F-made-v1` | `dd2680af` | 265 | yes |
| S · chapter stings | `music-S-stings-v1` | `a8f0b5d2` | 324 | no, withdrawn |
| S · chapter stings | `music-S-stings-v2` | `60bb52d2` | 415 | yes |

## `music-A-night-v1` — slot A, night shift

text to song, instrumental, ~90 s asked; sent.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Late-night acoustic piano trio with subtle electronic textures. A hypnotic repeating piano figure in a minor key, upright bass ostinato, brushed drums with a soft breakbeat feel, warm tape hiss. Calm and focused, like a machine working alone through the night. 80 bpm.

## `music-B-outage-v1` — slot B, the outage

text to song, instrumental, ~90 s asked; sent.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Tight, patient instrumental funk groove: clavinet and Rhodes, syncopated electric bass, dry pocket drums, short muted horn stabs. The groove keeps stopping and starting again, as if it is waiting for a signal. 96 bpm.

## `music-C-waiting-v1` — slot C, waiting for Ilya

text to song, instrumental, ~90 s asked; sent.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Minimalist piano trio: one insistent four-note piano cell repeated with small changes, a double bass pedal note, drums playing only hi-hat and rim clicks. Mechanical, patient, a little anxious. 110 bpm.

## `music-D-breaker-v1` — slot D, the breaker is built

text to song, instrumental, ~90 s asked; sent.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Driving acoustic piano trio played like electronic music: fast live breakbeat drums, a distorted upright bass riff, cascading arpeggiated piano in 7/8. It builds for a minute, then plays at full intensity, piano hammering an odd-meter riff with bass and drums locked, and ends on one hard unison hit followed by silence. Energetic, precise, euphoric. 140 bpm.

## `music-E-sleep-v1` — slot E, going to sleep

text to song, instrumental, ~90 s asked; sent.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Slow, sparse ambient piano and bowed double bass, tape hiss and soft vinyl crackle. A melody that slows down and fades, as if a computer is drifting off to sleep. 60 bpm.

## `music-F-made-v1` — slot F, the film is made

text to song, instrumental, ~90 s asked; sent.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Progressive jazz piano trio: a folk-modal piano melody in 7/8 and 5/4, heavy syncopated drums, a building groove that resolves into a bright, triumphant major chord. 120 bpm.

## `music-S-stings-v1` — slot S, chapter stings

text to song, instrumental, ~90 s asked; withdrawn before sending; replaced by `music-S-stings-v2`.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Raw punk rock electric guitar riffs: short, loud, distorted power-chord bursts of one to two seconds each with two seconds of silence between them, a crash cymbal on the last chord of each burst. Garage recording, energetic. 170 bpm.

## `music-S-stings-v2` — slot S, chapter stings

text to song, instrumental, ~90 s asked; sent.

> Make one instrumental track, about 90 seconds long. No vocals, no lyrics, no spoken words. Heavy 1990s funk-metal riffs: a drop-D distorted electric guitar riff locked with a heavy bass and a tight funk drum groove, sudden full stops, guitar noise effects that sound like a scratched turntable and a whammy-pedal squeal. Short, explosive two-bar riffs separated by one bar of silence. Defiant and energetic. 92 bpm.
