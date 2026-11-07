# The sandbox-ai learning video

[Deutsch](#deutsch) below.

A course of about eighteen minutes in fourteen chapters, from "what is it and why" to agent teams, rendered from
the storyboard in this folder: an English and a German video, each with WebVTT subtitles and a chapter track.
The rendered files are not kept in Git; render them as below, or take them from a release.

| File | What it is |
|---|---|
| `storyboard.py` | Every chapter, scene and text, in English and German (data only, no rendering) |
| `make_video.py` | The generator: storyboard → frames (Pillow) → MP4 (ffmpeg), plus the `.vtt` files |
| `capture_app.py` | Optional: real screenshots of the desktop app's pages, rendered offscreen |
| `tests/test_video_storyboard.py` | Every text in both languages, every command exists in `sandbox-ai --help`, the chapters cover the course |

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
- **Sound.** The video has no narration audio: the narration is on screen (and in the `.vtt` files).

App screenshots, before the render (any Python with PySide6, a checkout of sandbox-ai-app):

```bash
python3 docs/video/capture_app.py --app ~/src/sandbox-ai-app --out ~/video-out/app-shots
```

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

Ein Kurs von etwa achtzehn Minuten in vierzehn Kapiteln, von „was ist das und warum“ bis zu Agenten-Teams,
erzeugt aus dem Storyboard in diesem Ordner: ein englisches und ein deutsches Video, jeweils mit
WebVTT-Untertiteln und einer Kapitelspur. Die fertigen Dateien liegen nicht in Git.

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

**Was echt ist.** Terminal-Schritte mit `"live": True` führt `make_video.py` mit dem `sandbox-ai` dieses
Checkouts in einem Wegwerf-HOME aus (angezeigt als `/home/me`; nichts von Ihnen wird gelesen oder geändert); sie
tragen das Abzeichen *echte Ausgabe*. Was ohne eingerichteten Host nicht läuft (eine Agenten-Session, `doctor`
mit Podman, der Wizard, eine offene Rückfrage), steht als *Beispielausgabe* im Storyboard und ist im Bild so
markiert. Die App-Szenen zeigen echte Screenshots aus `capture_app.py` (mit gestellten Demodaten), sonst ein
Schema der Seite. Das Video hat keinen Sprecher-Ton: Die Erzählung steht im Bild und in den `.vtt`-Dateien.

**Aktualisieren.** Texte in `storyboard.py` ändern (jeder Text ist `T("English", "Deutsch")`), dann
`python3 -m pytest tests/test_video_storyboard.py`: Der Test schlägt fehl, wenn ein gezeigter Befehl oder eine
Option nicht mehr in `sandbox-ai --help` steht, eine Sprache fehlt oder der Kurs den Rahmen von zwölf bis zwanzig
Minuten verlässt. Echte Ausgaben folgen dem Code von selbst: nach einem Release neu erzeugen.

**Veröffentlichen.** Mit dem Checkout des Releases erzeugen und MP4 und VTT an das Release hängen (oder auf eine
Videoplattform laden, dort mit `--captions off` und den `.vtt`-Dateien als Untertitel). Die Videos nicht in Git
einchecken.
