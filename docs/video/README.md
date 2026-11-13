# The sandbox-ai learning video

[Deutsch](#deutsch) below.

A course of about nineteen minutes in fourteen chapters, from "what is it and why" to agent teams, rendered from
the storyboard in this folder: an English and a German video, each with WebVTT subtitles and a chapter track.
Beside it, a **series of eighteen short videos, one per page of the desktop app** (about a minute or two each),
with a spoken German narration (see [The app series, spoken](#the-app-series-spoken)). The rendered files are not
kept in Git; render them as below, or take them from a release. The desktop app plays the series itself (Help >
Videos about the pages) and downloads it once from its release, `sandbox-ai-videos.tar`, checked against the signed
`SHA256SUMS` (`scripts/make_dist.py --videos DIR` makes that file; see RELEASING.md).

| File | What it is |
|---|---|
| `storyboard.py` | Every chapter, scene and text of the course and of the app series, in English and German (data only, no rendering) |
| `make_video.py` | The generator: storyboard → frames (Pillow) → MP4 (ffmpeg), plus the `.vtt` files; with `--voice` also the spoken narration |
| `narrate.py` | The narration's speech: Piper text to speech, the English IT terms spliced in as English phonemes |
| `pronunciation.py` | The lexicon of English IT terms (data only; easy to extend) and the words said the German way on purpose |
| `capture_app.py` | Optional: real screenshots of the desktop app's pages and of its dialogs, rendered offscreen |
| `tests/test_video_storyboard.py` | Every text in both languages, every command exists in `sandbox-ai --help`, the chapters cover the course, every page has its video, every English term of the German narration is in the lexicon, the timing follows the speech |

## Render it

The tools are not dependencies of sandbox-ai: put them into a throwaway virtual environment outside the
checkout.

```bash
python3 -m venv /tmp/video-venv
/tmp/video-venv/bin/pip install pillow imageio-ffmpeg   # imageio-ffmpeg brings an ffmpeg binary
/tmp/video-venv/bin/python docs/video/make_video.py --out ~/video-out
```

This writes, into `~/video-out`:

- `sandbox-ai-course-en.mp4`, `sandbox-ai-course-de.mp4`: 1920x1080, 25 fps, H.264, no sound;
- `sandbox-ai-course-en.vtt`, `sandbox-ai-course-de.vtt`: the narration as subtitles;
- `sandbox-ai-course-chapters-en.vtt`, `sandbox-ai-course-chapters-de.vtt`: one cue per chapter, for players
  that show chapters.

Both languages share one timing (the longer text of the two decides how long a scene stays), so either
subtitle file fits either video.

| Option | Effect |
|---|---|
| `--lang en\|de\|both` | which video (default: both, rendered in parallel) |
| `--size 1280x720` | a smaller video; renders faster (any 16:9 size) |
| `--captions off` | do not burn the narration into the picture; leave it to the `.vtt` files (for players and platforms where viewers choose subtitles) |
| `--stills` | also save the scenes listed in `STILLS` as PNG files |
| `--plan` | print the chapters' timing and exit (needs neither Pillow nor ffmpeg; without live output, so a little shorter than the render) |
| `--no-live` | run no command; show the fallback text of the storyboard |
| `--app-shots DIR` | the app screenshots from `capture_app.py` (default: `OUT/app-shots`) |
| `--ffmpeg PATH`, `--font-dir DIR` | another ffmpeg; a folder with the DejaVu fonts |

The generator needs Python 3.9 or newer, Pillow, an ffmpeg (from `imageio-ffmpeg`, on `PATH` or `--ffmpeg`), the
DejaVu fonts (Linux packages `fonts-dejavu`/`ttf-dejavu`; on macOS it falls back to Arial and Menlo) and, for the
privacy-check demo, `git`. It runs on Linux and macOS. Frames go straight into ffmpeg through a pipe; nothing is
written to disk frame by frame, and a frame that does not change is not drawn again. Rendering both languages at
1920x1080 in parallel took about half an hour on a busy 20-core machine (ffmpeg is the slow part) and about 1 GB of
memory in all; `--size 1280x720` or `--lang en` is much quicker.

## What is real in it

- **Real output.** Terminal steps marked `"live": True` are run by `make_video.py` with this checkout's own
  `sandbox-ai`, in a throwaway HOME and runtime folder (shown as `/home/me`; nothing of yours is read or
  changed): `--dry-run` plans, the refusal of `--mode high --allow-github`, `privacy-check` on a staged
  repository, `git-logins`, `kits`, `llm`, `hosts`, `presets`, `defaults`, `mcp list`. They carry the badge
  *real output*.
- **Example output.** What cannot run without a configured host (an agent session, `doctor` with Podman, the
  wizard's questions, an open approval) is written in the storyboard with `"example": True`: the terminal shows
  *example output* and a comment line says so.
- **Commands only.** Steps without output show the command and its comment.
- **The app.** With screenshots from `capture_app.py` the app scenes show the real pages, with a staged demo
  project and a staged run that asks two questions (*real app screenshot, staged demo data*); without them the
  pages are drawn as schematics (*schematic of the app page*).
- **Diagrams** are drawn from the boxes and arrows in `DIAGRAMS`.
- **Sound.** The course has no narration audio by default: the narration is on screen (and in the `.vtt` files).
  With `--voice` it is spoken (the app series is made for that, below).

App screenshots, before the render (any Python with PySide6; the app is `projects/sandbox-ai-app` of this
checkout):

```bash
python3 docs/video/capture_app.py --app projects/sandbox-ai-app --out ~/video-out/app-shots
```

The README's screenshots in `docs/screenshots/` are these pages in English, staged under `/home/me`
(`--lang en --home /home/me`, then `PAGE-en.png` renamed to `PAGE.png`); `help.png`, `notifications.png` and
`web.png` are taken by hand. Refresh them before a release when a page they show has changed.

## The app series, spoken

One video per page of the app (Start, Board, Sandboxes & sessions, ... Help: `SERIES` in `storyboard.py`), each
with a title card, the real page and its dialogs (`capture_app.py`), what the page is for, what you do there and
its safety points, and a closing card that names the next page. The German narration is spoken by an offline voice:
German text, but every English IT term (Sandbox, Proxy, Workspace, Session, Board, Plugin, Hook, MCP server,
Token, Kill-Switch, Tor, VPN, Podman, ...) said in English, not Germanised.

The tools go into the same throwaway environment, outside the checkout (none of them is a dependency of
sandbox-ai). Use releases at least 14 days old; these were checked on 2026-11-12:

```bash
python3 -m venv /tmp/video-venv
/tmp/video-venv/bin/pip install pillow imageio-ffmpeg piper-tts==1.8.0
mkdir -p /tmp/piper-voices && cd /tmp/piper-voices
base=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/high
curl -LO $base/de_DE-thorsten-high.onnx -LO $base/de_DE-thorsten-high.onnx.json -LO $base/MODEL_CARD
sha256sum de_DE-thorsten-high.onnx   # 9df1c43c61149ef9b39e618e2b861fbe41e1fcea9390b2dac62e8761573ea4f1
cd -
python3 docs/video/capture_app.py --app projects/sandbox-ai-app --out ~/video-out/app-shots --lang de
/tmp/video-venv/bin/python docs/video/make_video.py --out ~/video-out --series pages --lang de \
    --voice /tmp/piper-voices/de_DE-thorsten-high.onnx
```

This writes `sandbox-ai-app-01-launch-de.mp4` ... `sandbox-ai-app-18-help-de.mp4` (H.264 with AAC sound) and a
`.vtt` beside each, numbered in the order of the app's navigation. `--only files,board` renders some of them
(capture again first when a page changed: `capture_app.py --only files,files-remote`; the file manager's remote
pane is captured against the app's own test SFTP server, so no SSH connection is needed), `--jobs N` N at a time,
`--plan` prints each one's length (with `--voice` the spoken length; that speaks the narration once). Without `--voice` the series renders
silent, timed for reading, in either language (`--lang en`).

- **The voice.** `de_DE-thorsten-high` from rhasspy/piper-voices (the repository: MIT), trained on Thorsten
  Müller's voice dataset (licence CC0, see its `MODEL_CARD`); the voice model was fine-tuned from Piper's American
  English *lessac* voice, so the English phonemes are ones it was trained on. Piper itself (`piper-tts`,
  GPL-3.0-or-later) brings its own espeak-ng. Any Piper voice works: its `.onnx.json` says its language, and that
  language's videos are spoken.
- **English terms.** `narrate.py` cuts each sentence into the voice's language and the terms of
  `pronunciation.LEXICON`; the German parts go through espeak-ng's German voice, each term through its British
  English voice (the English that espeak-ng's German itself switches to), or the lexicon's own IPA, and the
  phonemes go to the voice model together. The same text gives the same phonemes every time; the audio of each
  cue is kept in `OUT/.narration-cache` by a hash of voice, settings and phonemes (`--narration-cache DIR`).
  `python3 docs/video/narrate.py --voice VOICE --lexicon` prints every term with its phonemes, `--phonemes TEXT`
  the phonemes of a sentence, `--say TEXT --out x.wav` speaks it.
- **Adding a term.** Write it as the text has it, with an English spelling for espeak-ng (`"Sandboxen":
  "sandboxes"`) or IPA between slashes (`"Podman": "/pˈɒdmən/"`). The test fails for every word of the German
  narration that the English texts share, every abbreviation in capitals and every word with an inner capital
  that is neither a term nor listed in `GERMAN` (words said the German way on purpose), and for a longer word
  that begins with a term ("Sandboxwelt": write "Sandbox-Welt"). Inflected forms are terms of their own. Nobody
  listens in the test: it checks that the mechanism is deterministic and that the lexicon covers the narration;
  listen to a video after a larger change of the lexicon.
- **Timing.** A spoken scene lasts as long as its speech plus a short pause after each cue (and at least as
  long as its pictures need); the subtitles follow the speech. `--speech-pace` (default 1.08) slows the voice
  down or speeds it up.
- **Writing for the voice.** The spoken narration holds no paths, options or symbols (the test checks it): what
  is typed belongs into the bullets on screen. Name a button or page as the German app shows it (`i18n.py`).

Rendering all eighteen German videos at 1920x1080 took about ten minutes with `--jobs 4` on a busy machine
limited to ten CPUs, after about five minutes of speaking the narration once (`--tts-threads 4`); one at a time
(`--jobs 1`, where disk or memory is short) it takes about half an hour. Together they last about 28 minutes
(0.1.94: one to two and a half minutes each) and take about 40 MB.

## Update it when sandbox-ai changes

1. Change the texts in `storyboard.py`: a scene's `heading`, `bullets`, `captions` (the narration, one cue each)
   and terminal `steps`. Every text is `T("English", "Deutsch")`; text in `` `backticks` `` is drawn as code.
   Add a scene by appending a dictionary of one of the kinds described at the top of the file.
2. Run the test: `python3 -m pytest tests/test_video_storyboard.py`. It fails when a command or option shown in
   the video no longer exists in `sandbox-ai --help`, when a text lacks its German or English half, when the
   narration is not translated, or when the course leaves the twelve-to-twenty-minute frame.
3. Look at the timing with `--plan`, then render (`--size 1280x720 --lang en` is quick for a check).

Live output follows the code by itself: render again after a release and the plans, refusals and listings show
what that version prints.

## Publish it

Render with the release's checkout, then attach the MP4 and VTT files to the release (or upload them to a video
platform: there use `--captions off` and upload the `.vtt` files as the English and German subtitles, and the
chapter file where chapters are supported). Do not commit the videos to Git; they are rebuilt from the
storyboard.

## Deutsch

Ein Kurs von etwa neunzehn Minuten in vierzehn Kapiteln, von „was ist das und warum“ bis zu Agenten-Teams,
erzeugt aus dem Storyboard in diesem Ordner: ein englisches und ein deutsches Video, jeweils mit
WebVTT-Untertiteln und einer Kapitelspur. Dazu eine Serie von achtzehn kurzen Videos, eines je Seite der
Desktop-App, mit gesprochener deutscher Erzählung. Die fertigen Dateien liegen nicht in Git. Die Desktop-App spielt
die Reihe selbst ab (Hilfe > Videos zu den Seiten) und lädt sie einmal aus ihrem Release, `sandbox-ai-videos.tar`,
geprüft gegen die signierte `SHA256SUMS` (`scripts/make_dist.py --videos DIR` erzeugt die Datei; siehe RELEASING.md).

**Erzeugen.** Die Werkzeuge sind keine Abhängigkeiten von sandbox-ai; legen Sie sie in eine Wegwerf-Umgebung
außerhalb des Checkouts:

```bash
python3 -m venv /tmp/video-venv
/tmp/video-venv/bin/pip install pillow imageio-ffmpeg   # imageio-ffmpeg bringt ein ffmpeg mit
/tmp/video-venv/bin/python docs/video/make_video.py --out ~/video-out
```

Das schreibt `sandbox-ai-course-en.mp4` und `sandbox-ai-course-de.mp4` (1920x1080, 25 fps, H.264, ohne Ton), die
Erzählung als `sandbox-ai-course-en.vtt` und `sandbox-ai-course-de.vtt` sowie die Kapitel als
`sandbox-ai-course-chapters-en.vtt` und `-de.vtt`. Beide Sprachen haben dieselbe Zeitleiste, jede Untertiteldatei
passt also zu jedem Video. `--lang de` erzeugt nur das deutsche Video, `--size 1280x720` ein kleineres und
schnelleres, `--captions off` brennt die Erzählung nicht ins Bild (für Plattformen, auf denen Zuschauer Untertitel
wählen), `--plan` zeigt nur die Zeitleiste, `--stills` speichert zusätzlich Standbilder.

**Die App-Serie, gesprochen.** Achtzehn kurze Videos, eines je Seite der Desktop-App (`SERIES` in
`storyboard.py`), mit echten Screenshots der Seite und ihrer Dialoge und einer gesprochenen deutschen Erzählung:
deutscher Text, aber jeder englische IT-Begriff (Sandbox, Proxy, Workspace, Session, Board, Plugin, Hook,
MCP-Server, Token, Kill-Switch, Tor, VPN, Podman, ...) englisch ausgesprochen, nicht eingedeutscht. Zusätzlich
`piper-tts==1.8.0` in dieselbe Wegwerf-Umgebung und die Stimme `de_DE-thorsten-high` (rhasspy/piper-voices,
Datensatz CC0, Repository MIT, Piper selbst GPL-3.0-or-later) laden, siehe oben; dann
`make_video.py --out ~/video-out --series pages --lang de --voice /tmp/piper-voices/de_DE-thorsten-high.onnx`.
`narrate.py` zerlegt jeden Satz in deutsche Teile (espeak-ng Deutsch) und die Begriffe aus
`pronunciation.LEXICON` (espeak-ng britisches Englisch, oder IPA zwischen Schrägstrichen) und gibt die Phoneme
zusammen an das Stimmmodell; gleicher Text, gleiche Phoneme. Einen Begriff ergänzen: so schreiben, wie er im Text
steht, mit englischer Schreibung (`"Sandboxen": "sandboxes"`) oder IPA (`"Podman": "/pˈɒdmən/"`); der Test meldet
jedes Wort der deutschen Erzählung, das auch in den englischen Texten vorkommt, jede Abkürzung und jedes Wort mit
Binnen-Großbuchstaben, das weder Begriff ist noch in `GERMAN` steht (bewusst deutsch gesprochen). Eine gesprochene
Szene dauert so lange wie ihre Sprache plus kurze Pausen; die Untertitel folgen ihr. Die gesprochene Erzählung
enthält keine Pfade, Optionen oder Zeichen; Knöpfe und Seiten heißen so, wie die deutsche App sie zeigt.

**Was echt ist.** Terminal-Schritte mit `"live": True` führt `make_video.py` mit dem `sandbox-ai` dieses
Checkouts in einem Wegwerf-HOME aus (angezeigt als `/home/me`; nichts von Ihnen wird gelesen oder geändert); sie
tragen das Abzeichen *echte Ausgabe*. Was ohne eingerichteten Host nicht läuft (eine Agenten-Session, `doctor`
mit Podman, der Wizard, eine offene Rückfrage), steht als *Beispielausgabe* im Storyboard und ist im Bild so
markiert. Die App-Szenen zeigen echte Screenshots aus `capture_app.py` (mit gestellten Demodaten), sonst ein
Schema der Seite (`python3 docs/video/capture_app.py --app projects/sandbox-ai-app --out ~/video-out/app-shots`;
die Screenshots der README in `docs/screenshots/` sind dieselben Seiten auf Englisch, mit `--lang en --home
/home/me`, `PAGE-en.png` umbenannt in `PAGE.png`; `help.png`, `notifications.png` und `web.png` entstehen von
Hand). Der Kurs hat ohne `--voice` keinen Sprecher-Ton: Die Erzählung steht im Bild und in den `.vtt`-Dateien.

**Aktualisieren.** Texte in `storyboard.py` ändern (jeder Text ist `T("English", "Deutsch")`), dann
`python3 -m pytest tests/test_video_storyboard.py`: Der Test schlägt fehl, wenn ein gezeigter Befehl oder eine
Option nicht mehr in `sandbox-ai --help` steht, eine Sprache fehlt oder der Kurs den Rahmen von zwölf bis zwanzig
Minuten verlässt. Echte Ausgaben folgen dem Code von selbst: nach einem Release neu erzeugen.

**Veröffentlichen.** Mit dem Checkout des Releases erzeugen und MP4 und VTT an das Release hängen (oder auf eine
Videoplattform laden, dort mit `--captions off` und den `.vtt`-Dateien als Untertitel). Die Videos nicht in Git
einchecken.
