"""Error-tolerant ASR comparison with explicit content and boundary checks.

The transcript stays untouched. Canonical and pronunciation forms are used
only for matching; neither can supply words missing from the source audio.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from pypinyin import lazy_pinyin
from rapidfuzz.distance import Levenshtein


_EN_NUMBERS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
    range(20),
))
_EN_NUMBERS.update(dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10))))
_EN_NUMBER_WORD = "|".join((*_EN_NUMBERS, "hundred", "thousand"))
_EN_NUMBER_RE = re.compile(rf"\b(?:{_EN_NUMBER_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_EN_NUMBER_WORD}))*\b")
# ASR runs with inverse text normalization, so "nineteenth" is transcribed as
# "19th" while papers usually spell it out. Both forms are the same spoken
# number and must reach the number check identically; otherwise the passage
# is rejected as a content change.
_EN_ORDINALS = dict(zip(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth "
    "twentieth thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth".split(),
    "one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen "
    "twenty thirty forty fifty sixty seventy eighty ninety".split(),
))
_EN_ORDINAL_TOKEN = r"\b(?:" + "|".join(_EN_ORDINALS) + r")\b"
_EN_ORDINAL_RE = re.compile(_EN_ORDINAL_TOKEN)
_DIGIT_ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b", re.IGNORECASE)
# "First, I took a boat." is a sequencing word, not a quantity, and ASR often
# splits it into its own segment ("first.") and drops the previous full stop.
# Dropping it must stay an ordinary edit. Only this narrow set of connector
# ordinals is exempt; any other ordinal ("the nineteenth century") remains a
# critical number, because a changed century is a content change.
_DISCOURSE_ORDINALS = ("first", "second", "third", "next", "then", "last", "finally")
_DISCOURSE_ORDINAL_RE = re.compile(
    r"\b(?:" + "|".join(_DISCOURSE_ORDINALS) + r")\b(?=[\s\"']*(?:[,，.。;；!！?？]|$))",
    re.IGNORECASE,
)
_HAN_DIGITS = {char: value for value, chars in enumerate(("零〇", "一", "二两", "三", "四", "五", "六", "七", "八", "九")) for char in chars}
_HAN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CONTRACTIONS = {
    "can't": "can not", "cannot": "can not", "won't": "will not",
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "isn't": "is not", "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "haven't": "have not", "hasn't": "has not", "hadn't": "had not",
    "couldn't": "could not", "wouldn't": "would not", "shouldn't": "should not",
    "i'm": "i am", "you're": "you are", "we're": "we are", "they're": "they are",
    "i've": "i have", "we've": "we have", "they've": "they have",
    "i'll": "i will", "you'll": "you will", "we'll": "we will", "they'll": "they will",
    "it's": "it is", "he's": "he is", "she's": "she is", "that's": "that is",
    "what's": "what is", "who's": "who is", "where's": "where is",
}
_CONTRACTION_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, _CONTRACTIONS)) + r")\b")
_QUESTION_RE = re.compile(r"\b(?:who|what|when|where|why|how|which|whose)\b")
_NEGATION_RE = re.compile(r"\b(?:not|no|never|neither|without)\b|[不没未无]")


def _english_number(match: re.Match[str]) -> str:
    words = re.findall(r"[a-z]+", match.group())
    # Adjacent independent numbers (e.g. "seven thirty" in a clock time)
    # must not be added together as if they said "thirty seven".
    if len(words) > 1 and all(word in _EN_NUMBERS for word in words):
        if not (len(words) == 2 and _EN_NUMBERS[words[0]] >= 20 and _EN_NUMBERS[words[0]] % 10 == 0 and _EN_NUMBERS[words[1]] < 10):
            return " ".join(str(_EN_NUMBERS[word]) for word in words)
    total = current = 0
    for word in words:
        if word in _EN_NUMBERS:
            current += _EN_NUMBERS[word]
        elif word == "hundred":
            current = max(1, current) * 100
        elif word == "thousand":
            total += max(1, current) * 1000
            current = 0
    return str(total + current)


def _chinese_number(match: re.Match[str]) -> str:
    text = match.group()
    if not any(char in _HAN_UNITS for char in text):
        return "".join(str(_HAN_DIGITS[char]) for char in text)
    total = section = digit = 0
    for char in text:
        if char in _HAN_DIGITS:
            digit = _HAN_DIGITS[char]
        elif char == "万":
            total += (section + digit or 1) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * _HAN_UNITS[char]
            digit = 0
    return str(total + section + digit)


def _compact(text: str) -> str:
    return "".join(char for char in text if char.isalnum())


@dataclass(frozen=True)
class TextProfile:
    text: str
    phonetic: str
    numbers: tuple[str, ...]
    negations: tuple[str, ...]
    questions: tuple[str, ...]


@lru_cache(maxsize=8192)
def text_profile(value: str) -> TextProfile:
    text = unicodedata.normalize("NFKC", value).casefold().replace("’", "'").replace("‘", "'")
    text = re.sub(r"<\|[^|]{1,64}\|>", "", text)
    text = _CONTRACTION_RE.sub(lambda match: _CONTRACTIONS[match.group()], text)
    text = re.sub(r"\b(?:mister|missus|doctor)\b", lambda match: {"mister": "mr", "missus": "mrs", "doctor": "dr"}[match.group()], text)
    # Ordinals join the cardinal pipeline so "nineteenth" and the ASR's
    # "19th" are the same number. A century/year-like ordinal stays in the
    # critical-number gate; a sentence-opening discourse ordinal does not.
    number_source = _DISCOURSE_ORDINAL_RE.sub(" ", text)
    text = _EN_ORDINAL_RE.sub(lambda match: _EN_ORDINALS[match.group()], text)
    text = _DIGIT_ORDINAL_RE.sub(r"\1", text)
    text = _EN_NUMBER_RE.sub(_english_number, text)
    text = re.sub(r"[零〇一二两三四五六七八九十百千万]+", _chinese_number, text)
    text = re.sub(r"\b(\d{1,2}):00\b", r"\1", text)
    text = re.sub(r"\bo'?clock\b", "", text)
    canonical = _compact(text)
    phonetic = _compact("".join(lazy_pinyin(canonical))) if re.search(r"[\u4e00-\u9fff]", canonical) else canonical
    number_source = _EN_ORDINAL_RE.sub(lambda match: _EN_ORDINALS[match.group()], number_source)
    number_source = _DIGIT_ORDINAL_RE.sub(r"\1", number_source)
    number_source = _EN_NUMBER_RE.sub(_english_number, number_source)
    number_source = re.sub(r"[零〇一二两三四五六七八九十百千万]+", _chinese_number, number_source)
    number_source = re.sub(r"\b(\d{1,2}):00\b", r"\1", number_source)
    return TextProfile(
        canonical, phonetic,
        tuple(re.findall(r"\d+(?:\.\d+)?", number_source)),
        tuple(_NEGATION_RE.findall(text)), tuple(_QUESTION_RE.findall(text)),
    )


@dataclass(frozen=True)
class TextMatch:
    score: float
    coverage: float
    complete: bool
    compatible: bool
    method: str


def _compatible(source: TextProfile, observed: TextProfile, *, opening: bool) -> bool:
    if opening:
        return (
            source.numbers[:len(observed.numbers)] == observed.numbers
            and source.questions[:len(observed.questions)] == observed.questions
            and source.negations[:len(observed.negations)] == observed.negations
        )
    return (source.numbers, source.questions, source.negations) == (
        observed.numbers, observed.questions, observed.negations,
    )


def _whole_comparison(source: str, observed: str, penalty: float) -> tuple[float, float, bool]:
    if not source or not observed or not 0.72 <= len(observed) / len(source) <= 1.35:
        return 0.0, 0.0, False
    score = Levenshtein.normalized_similarity(source, observed, score_cutoff=0.72)
    if not score:
        return 0.0, 0.0, False
    # An optimal edit alignment preserves local matches across misspellings.
    # Substitutions get limited evidence credit; deleted source words get
    # none, so an omitted ending cannot masquerade as a complete clip.
    evidence = [1.0] * len(source)
    for tag, position, _destination in Levenshtein.editops(source, observed):
        if tag == "delete":
            evidence[position] = 0.0
        elif tag == "replace":
            evidence[position] = 0.35
    coverage = sum(evidence) / len(source)
    anchor = min(20, max(5, len(source) // 6), len(source))
    complete = (
        coverage >= 0.86
        and sum(evidence[:anchor]) / anchor >= 0.60
        and sum(evidence[-anchor:]) / anchor >= 0.60
    )
    return score * penalty, coverage, complete


def compare_transcript(source: TextProfile, observed: TextProfile, *, opening: bool = False) -> TextMatch:
    compatible = _compatible(source, observed, opening=opening)
    if not source.text or not observed.text:
        return TextMatch(0.0, 0.0, False, compatible, "edit_distance")
    channels = [(source.text, observed.text, 1.0, "edit_distance")]
    if source.phonetic != source.text or observed.phonetic != observed.text:
        # Exact spelling must outrank a pronunciation-only match, particularly
        # when two document items differ only by a name or homophone.
        channels.append((source.phonetic, observed.phonetic, 0.97, "pronunciation"))
    best = TextMatch(0.0, 0.0, False, compatible, "edit_distance")
    for expected, actual, penalty, method in channels:
        if opening:
            length = min(len(expected), len(actual))
            sizes = {max(1, length - 3), length, length + 4, length + 12}
            score = max((
                Levenshtein.normalized_similarity(expected[:size], actual) * penalty
                for size in sizes
            ), default=0.0)
            coverage, complete = 0.0, False
        else:
            score, coverage, complete = _whole_comparison(expected, actual, penalty)
        if score > best.score:
            best = TextMatch(score, coverage, complete and compatible, compatible, method)
    return best
