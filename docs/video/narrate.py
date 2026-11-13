#!/usr/bin/env python3
"""The narration audio of the videos: text to speech with Piper, English IT terms said in English.

    python3 docs/video/narrate.py --voice de_DE-thorsten-high.onnx --say "Die Sandbox startet den Workspace."
    python3 docs/video/narrate.py --voice VOICE.onnx --phonemes "Die Sandbox ..."   # show the phonemes only
    python3 docs/video/narrate.py --voice VOICE.onnx --lexicon                      # every term with its phonemes

make_video.py uses it with --voice. The mechanism, deterministic for one Piper release and one voice:

1. spoken(): the caption as it is to be said (no backticks, "&" said as "und", ...).
2. segments(): the text cut into the voice's own language and the terms of pronunciation.LEXICON (longest
   first, whole words only; a hyphen between two parts is a boundary, so "MCP-Server" is two terms).
3. Phonemes: every stretch of the voice's language through espeak-ng in that language (Piper's own espeak-ng),
   every term through espeak-ng's British English voice ("en") from its English spelling in the lexicon, or the
   lexicon's own IPA (a value written /like this/). Punctuation between stretches is kept as Piper's voices know
   it: , ; : as a short break, . ? ! as the end of a sentence.
4. Piper's voice model turns each sentence's phonemes into audio; sentences are joined with a short pause.

The German voice suggested (de_DE-thorsten-high) was fine-tuned from an American English voice, so the English
phonemes (w, ɹ, æ, ɜː, ...) are ones it was trained on. No part of this is a dependency of sandbox-ai: Piper
(`pip install piper-tts`, GPL-3.0-or-later) goes into the same throwaway environment as Pillow and imageio-ffmpeg.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
import wave

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pronunciation  # noqa: E402

SENTENCE_PAUSE = 0.28  # seconds between two sentences of one cue
BREAK = {",", ";", ":"}
END = {".", "?", "!"}
WORD = r"[0-9A-Za-zÄÖÜäöüß]"
ENGLISH_VOICE = "en"  # espeak-ng's British English: the English espeak-ng's German voice itself switches to


def spoken(text):
    """TEXT as it is said: no `code` marks, symbols said as words, dashes and quotes as breaks."""
    text = text.replace("`", "")
    for old, new in pronunciation.SPOKEN_SYMBOLS:
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def _term_pattern():
    keys = sorted(pronunciation.LEXICON, key=len, reverse=True)
    alternatives = "|".join(re.escape(key) for key in keys)
    return re.compile(rf"(?<!{WORD})(?:{alternatives})(?!{WORD})")


_PATTERN = None


def segments(text):
    """TEXT (already spoken()) as [(kind, part)]: kind "native" (the voice's language) or "term" (a key of
    pronunciation.LEXICON). A hyphen joining a term to a word is said as a short pause-free space."""
    global _PATTERN
    if _PATTERN is None:
        _PATTERN = _term_pattern()
    found, at = [], 0
    for match in _PATTERN.finditer(text):
        if match.start() > at:
            found.append(("native", text[at:match.start()]))
        found.append(("term", match.group(0)))
        at = match.end()
    if at < len(text):
        found.append(("native", text[at:]))
    # "MCP-Server", "Tor-Profil": the hyphen next to a term is just a word boundary
    result = []
    for kind, part in found:
        if kind == "native":
            part = re.sub(r"^-(?=\S)", " ", part)
            part = re.sub(r"(?<=\S)-$", " ", part)
            if part == "-":
                part = " "
        result.append((kind, part))
    return result


def unknown_words(text):
    """The words of TEXT that are no lexicon term (for the checks of the test): [word]."""
    words = []
    for kind, part in segments(spoken(text)):
        if kind == "native":
            words += re.findall(rf"{WORD}+(?:[.'’]{WORD}+)*", part)
    return words


def english_words(texts):
    """The lower-case words of TEXTS (the English texts of the storyboard)."""
    found = set()
    for value in texts:
        found.update(word.lower() for word in re.findall(rf"{WORD}+", value.replace("`", " ")))
    return found


def coverage_problems(narrations, english):
    """[(where, word, why)] for the words of NARRATIONS ([(where, text)] in the voice's language) that would be
    read the voice's way although they are English IT terms: a word the English texts share (ENGLISH: their
    lower-case words), an abbreviation in capitals, a word with a capital inside, or a longer word that starts
    with a term. Each must be a term of pronunciation.LEXICON or be listed in pronunciation.GERMAN."""
    terms = [key for key in pronunciation.LEXICON if len(key) >= 3 and key[:1].isalpha()]
    problems = []
    for where, value in narrations:
        for word in unknown_words(value):
            if word in pronunciation.GERMAN or word.isdigit():
                continue
            if word.lower() in english:
                problems.append((where, word, "also an English word: a LEXICON term, or GERMAN"))
            elif re.fullmatch(r"[A-ZÄÖÜ]{2,}s?", word) or re.search(r"[a-z][A-Z]", word):
                problems.append((where, word, "an abbreviation or a name: a LEXICON term, or GERMAN"))
            else:
                prefix = next((t for t in terms if word.startswith(t) and len(word) > len(t)), None)
                if prefix:
                    problems.append((where, word, f"starts with the term {prefix}: write it with a hyphen"))
    return problems


def lexicon_ipa(term, phonemize_english):
    """The English phonemes of TERM: the lexicon's /IPA/ as it is, else its English spelling through
    PHONEMIZE_ENGLISH (a function: text -> phoneme string)."""
    value = pronunciation.LEXICON[term]
    if value.startswith("/") and value.endswith("/"):
        return value[1:-1]
    return phonemize_english(value)


def phoneme_sentences(text, phonemize_native, phonemize_english):
    """TEXT (already spoken()) as sentences of phonemes, each a string Piper's voice takes.

    PHONEMIZE_NATIVE(text) -> [(phonemes, terminator, end_of_sentence)] as Piper's espeakbridge.get_phonemes
    returns them; PHONEMIZE_ENGLISH(text) -> a phoneme string. Both are passed in, so the splicing is tested
    without espeak-ng."""
    sentences, current = [], []

    def close():
        line = "".join(current).strip()
        if line:
            sentences.append(re.sub(r" {2,}", " ", line))
        current.clear()

    def punctuation(mark):
        if mark in END:
            current.append(mark)
            close()
        elif mark in BREAK:
            if current and current[-1].strip():
                current.append(mark + " ")

    for kind, part in segments(text):
        if kind == "term":
            if current and not current[-1].endswith(" "):
                current.append(" ")
            current.append(lexicon_ipa(part, phonemize_english))
            continue
        lead = re.match(r"^[\s,;:.?!]*", part).group(0)
        for mark in lead:
            punctuation(mark)
        rest = part[len(lead):]
        if lead.endswith(" ") or (rest and part[:1] == " "):
            current.append(" ")
        if not rest.strip():
            if part.endswith(" "):
                current.append(" ")
            continue
        for phonemes, terminator, _end in phonemize_native(rest):
            phonemes = re.sub(r"\([^)]+\)", "", phonemes)  # espeak-ng's language switch flags
            if phonemes.strip():
                if current and not current[-1].endswith(" "):
                    current.append(" ")
                current.append(phonemes.strip())
            if terminator:  # (a stretch's end without one is not a sentence's end: a term follows)
                punctuation(terminator)
        if rest.endswith(" ") or part.endswith(" "):
            current.append(" ")
    close()
    return sentences


class Speaker:
    """A Piper voice and the cache of what it said (WAV files named by the hash of voice, phonemes and settings)."""

    def __init__(self, voice, cache_dir, length_scale=None, threads=4):
        self.voice_path = Path(voice)
        self.config = json.loads(Path(f"{voice}.json").read_text())
        self.language = self.config["espeak"]["voice"].split("-")[0]
        self.rate = int(self.config["audio"]["sample_rate"])
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.length_scale = length_scale
        self.threads = threads  # onnxruntime's own default (every core) is slower on a busy machine
        digest = hashlib.sha256(Path(f"{voice}.json").read_bytes())
        digest.update(str(self.voice_path.stat().st_size).encode())
        self.voice_id = digest.hexdigest()[:16]
        self._voice = None
        self._english = {}

    def _bridge(self):
        from piper import espeakbridge
        from piper.phonemize_espeak import ESPEAK_DATA_DIR
        if not getattr(Speaker, "_initialised", False):
            espeakbridge.initialize(str(ESPEAK_DATA_DIR))
            Speaker._initialised = True
        return espeakbridge

    def native(self, text):
        bridge = self._bridge()
        bridge.set_voice(self.config["espeak"]["voice"])
        return bridge.get_phonemes(text)

    def english(self, text):
        if text not in self._english:
            bridge = self._bridge()
            bridge.set_voice(ENGLISH_VOICE)
            parts = [re.sub(r"\([^)]+\)", "", p) for p, _t, _e in bridge.get_phonemes(text)]
            self._english[text] = " ".join(p.strip() for p in parts if p.strip())
        return self._english[text]

    def sentences(self, text):
        return phoneme_sentences(spoken(text), self.native, self.english)

    def missing_symbols(self, phonemes):
        known = self.config["phoneme_id_map"]
        return sorted({c for c in unicodedata.normalize("NFD", phonemes) if c not in known})

    def say(self, text):
        """The audio of TEXT: (path of a mono 16-bit WAV, seconds)."""
        sentences = self.sentences(text)
        key = hashlib.sha256(json.dumps([self.voice_id, self.length_scale, SENTENCE_PAUSE, sentences],
                                        ensure_ascii=False).encode()).hexdigest()[:24]
        path = self.cache / f"{key}.wav"
        if not path.exists():
            audio = self.synthesize(sentences)
            tmp = path.with_suffix(".tmp")
            with wave.open(str(tmp), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(self.rate)
                out.writeframes(audio)
            tmp.replace(path)
        with wave.open(str(path), "rb") as f:
            return path, f.getnframes() / f.getframerate()

    def synthesize(self, sentences):
        from piper import PiperVoice, SynthesisConfig
        from piper.config import PiperConfig
        from piper.phonemize_espeak import ESPEAK_DATA_DIR
        import numpy
        import onnxruntime
        if self._voice is None:
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = self.threads
            options.inter_op_num_threads = 1
            session = onnxruntime.InferenceSession(str(self.voice_path), sess_options=options,
                                                   providers=["CPUExecutionProvider"])
            self._voice = PiperVoice(config=PiperConfig.from_dict(self.config), session=session,
                                     espeak_data_dir=Path(ESPEAK_DATA_DIR), download_dir=self.cache)
        config = SynthesisConfig(length_scale=self.length_scale)
        pause = bytes(int(self.rate * SENTENCE_PAUSE) * 2)
        parts = []
        for line in sentences:
            phonemes = list(unicodedata.normalize("NFD", line))
            missing = self.missing_symbols(line)
            if missing:
                print(f"narrate: warning: the voice has no {' '.join(missing)} (in {line!r})", file=sys.stderr)
            audio = self._voice.phoneme_ids_to_audio(self._voice.phonemes_to_ids(phonemes), config)
            if isinstance(audio, tuple):
                audio = audio[0]
            peak = float(numpy.max(numpy.abs(audio))) or 1.0
            audio = numpy.clip(audio / peak * 0.9, -1, 1)
            parts.append((audio * 32767).astype("<i2").tobytes())
            parts.append(pause)
        return b"".join(parts[:-1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--voice", required=True, help="a Piper voice (.onnx, its .onnx.json beside it)")
    parser.add_argument("--say", help="speak TEXT into --out")
    parser.add_argument("--out", default="narration.wav")
    parser.add_argument("--phonemes", help="print the phonemes of TEXT, sentence by sentence")
    parser.add_argument("--lexicon", action="store_true", help="print every lexicon term with its phonemes")
    parser.add_argument("--cache", default=".narration-cache")
    args = parser.parse_args(argv)
    speaker = Speaker(args.voice, args.cache)
    if args.lexicon:
        for term in sorted(pronunciation.LEXICON, key=str.lower):
            ipa = lexicon_ipa(term, speaker.english)
            missing = speaker.missing_symbols(ipa)
            print(f"{term:28s} {ipa}" + (f"   MISSING {' '.join(missing)}" if missing else ""))
        return 0
    if args.phonemes:
        for line in speaker.sentences(args.phonemes):
            print(line)
        return 0
    if args.say:
        path, seconds = speaker.say(args.say)
        Path(args.out).write_bytes(path.read_bytes())
        print(f"{args.out}: {seconds:.2f} s")
        return 0
    parser.error("give --say, --phonemes or --lexicon")
    return 2


if __name__ == "__main__":
    sys.exit(main())
