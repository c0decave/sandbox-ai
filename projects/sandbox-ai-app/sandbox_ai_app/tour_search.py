"""Tour about…: find the tours and Help sections about a topic the human types, in English or German. No Qt.

Everything searchable is built from what the app already has: the tours through a task (tours.TASK_TOURS: title,
summary, every step's title and text), the tours of single pages (tours.AREA_TOURS with the page's title from
guide.TOUR) and the Help sections (help_content.SECTIONS: title, text, commands). Both languages are searched
whatever language the app shows.

The ranking is plain and predictable: each word of the query scores where it is found - in a title more than in
a text, a whole word more than the start of one, the start of one more than a part (German compounds:
"Netzwerk" finds "Netzwerkprofile") or a near miss ("netwrok"). A result must match at least one word; results
matching more of the words come first. A tour ranks a little above a Help section of the same score, and a
section that matches lends its score to the tours it names (a section about canaries offers its tour).
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import re
import unicodedata

from . import guide, help_content, i18n, tours

TITLE, TEXT = 3.0, 1.0           # where a word is found
WHOLE, PREFIX, PART, NEAR = 2.0, 1.5, 0.8, 0.6  # how
TOUR_BONUS = 1.15               # a tour before a section of the same score
LENT = 0.8                      # what a matching section lends to its tours
NEAR_RATIO = 0.8                # difflib's ratio for a near miss
STOP = frozenset("a an and the of to in on for with how what is are about tour tours me give show "
                 "der die das ein eine und zu im in am an auf fuer mit wie was ist sind ueber tour touren "
                 "zeig zeige mir bitte".split())


@dataclass(frozen=True)
class Hit:
    kind: str     # "task" (a tour through a task), "area" (the tour of a page) or "section" (a Help section)
    ident: str    # the task tour's id, "area:PAGE", or the section's id
    title: tours.Text
    summary: tours.Text
    score: float

    @property
    def is_tour(self):
        return self.kind in ("task", "area")


def fold(text):
    """TEXT lower-cased, umlauts and ß spelt out, other accents dropped: "Köder" and "koeder" are one word."""
    text = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def words(text):
    return re.findall(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]", fold(text))


def stem(word):
    """WORD without a plural or inflection ending (English and German, roughly): presets/preset, Vorlagen/Vorlage."""
    for suffix in ("ies", "es", "en", "er", "s", "e", "n"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def with_stems(found):
    return frozenset(found) | frozenset(stem(w) for w in found)


def query_words(query):
    found = words(query)
    meaningful = [w for w in found if w not in STOP]
    return list(dict.fromkeys(meaningful or found))


@dataclass(frozen=True)
class Entry:
    kind: str
    ident: str
    title: tours.Text
    summary: tours.Text
    title_words: frozenset
    text_words: frozenset
    names: tuple  # tours a section names


def both(text):
    return f"{text.en} {text.de}"


def page_title(page):
    """The page's title in both languages (guide.TOUR's, through i18n)."""
    key = next((step.title for step in guide.TOUR if step.page == page), page)
    entry = i18n.TEXTS.get(key) or {}
    return tours.Text(key, entry.get("de") or key)


def first_sentence(text, limit=160):
    sentence = re.split(r"(?<=[.:])\s", text.strip(), maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"


def entries():
    """Every tour and Help section, with its words."""
    found = []
    for tour in tours.TASK_TOURS.values():
        steps = " ".join(f"{both(s.title)} {both(s.text)} {both(s.try_)}" for s in tour.steps)
        found.append(Entry("task", tour.id, tour.title, tour.summary, with_stems(words(both(tour.title))),
                           with_stems(words(f"{both(tour.summary)} {steps}")), ()))
    for page, area_steps in tours.AREA_TOURS.items():
        title = page_title(page)
        shown = tours.Text(f"Tour of the page {title.en}", f"Tour der Seite {title.de}")
        first = area_steps[0]
        steps = " ".join(f"{both(s.title)} {both(s.text)} {both(s.try_)}" for s in area_steps)
        found.append(Entry("area", f"area:{page}", shown, first.text,
                           with_stems(words(f"{both(title)} {page} {both(first.title)}")), with_stems(words(steps)), ()))
    for section in help_content.SECTIONS:
        summary = tours.Text(first_sentence(section.body.en), first_sentence(section.body.de))
        found.append(Entry("section", section.id, section.title, summary,
                           with_stems(words(f"{both(section.title)} {section.id.replace('-', ' ')}")),
                           with_stems(words(f"{both(section.body)} {' '.join(section.cli)}")), section.tours))
    return found


_ENTRIES = None


def index():
    global _ENTRIES
    if _ENTRIES is None:
        _ENTRIES = entries()
    return _ENTRIES


def word_score(word, title_words, text_words):
    """How well WORD is found: the best of every kind of match, in the title or the text."""
    best = 0.0
    for where, weight in ((title_words, TITLE), (text_words, TEXT)):
        if word in where or stem(word) in where:
            best = max(best, weight * WHOLE)
            continue
        if len(word) >= 3 and any(w.startswith(word) for w in where):
            best = max(best, weight * PREFIX)
        elif len(word) >= 4 and any(word in w for w in where):
            best = max(best, weight * PART)
        elif len(word) >= 4 and difflib.get_close_matches(word, [w for w in where if abs(len(w) - len(word)) <= 2],
                                                          n=1, cutoff=NEAR_RATIO):
            best = max(best, weight * NEAR)
    return best


def search(query, limit=12):
    """The tours and Help sections about QUERY, best first (an empty list for nothing or no match)."""
    wanted = query_words(query)
    if not wanted:
        return []
    scores = {}
    for entry in index():
        found = [word_score(word, entry.title_words, entry.text_words) for word in wanted]
        matched = sum(1 for value in found if value)
        if not matched:
            continue
        score = sum(found) * (matched / len(wanted)) ** 2
        if entry.kind != "section":
            score *= TOUR_BONUS
        scores[(entry.kind, entry.ident)] = (max(score, scores.get((entry.kind, entry.ident), (0, None))[0]), entry)
    by_ident = {e.ident: e for e in index() if e.kind != "section"}
    for (kind, _), (score, entry) in list(scores.items()):
        if kind != "section":
            continue
        for named in entry.names:
            tour = by_ident.get(named)
            if tour is None:
                continue
            key = (tour.kind, tour.ident)
            lent = score * LENT
            if lent > scores.get(key, (0, None))[0]:
                scores[key] = (lent, tour)
    ranked = sorted(scores.values(), key=lambda pair: (-pair[0], pair[1].kind == "section", pair[1].ident))
    return [Hit(e.kind, e.ident, e.title, e.summary, round(s, 3)) for s, e in ranked[:limit]]
