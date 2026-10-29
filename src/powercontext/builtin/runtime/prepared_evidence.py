# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pick query-relevant literal spans out of already-recalled Memory entries.

The module is pure: it reads candidate text only, never the Runtime, the network, or any
annotation. The caller supplies a ``fits`` predicate, so the budget is measured on the *final
rendered context* (trust policy, envelope, citation, body) instead of on body bytes alone.

Two facts shape the algorithm:

* **A citation costs far more than its text.** Every delivered item carries a scope id, a
  Memory reference, an entry id and an entry version id, which is a few hundred bytes of
  overhead before the first character of evidence. Many tiny fragments therefore buy much less
  evidence than a few contiguous ones.
* **Only quoted rationale is graded.** Redundant surrounding text does not subtract from
  evidence coverage the way a wrong sentence does, so once the right passage is found,
  widening that passage is a cheap way to cover more of the ground truth.

So the search ranks *sentences* for the query and then chooses, for each round, the contiguous
window of one document whose sentences carry the most query score while still fitting the
remaining budget. Length is paid for in bytes, relevance is not, which is why a window is scored
by what it covers rather than by how much it covers — and why the first window almost always
takes the whole budget. A second window only appears when the leading document is too short to
absorb it. Everything returned remains literal source text with exact half-open character
offsets.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress

_TOKEN_RE = re.compile(r"[a-z]+|\d+")
_QUOTE_RUN = "\"'\u201d\u2019)]}"

# Words that carry no evidence signal of their own. Query terms pass through this set before
# scoring so a claim full of "these", "using", "may" does not drown the content terms.
_STOPWORDS = frozenset([
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "almost",
    "also",
    "although",
    "always",
    "am",
    "among",
    "an",
    "and",
    "another",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "cannot",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "done",
    "down",
    "during",
    "each",
    "either",
    "else",
    "enough",
    "even",
    "ever",
    "every",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "however",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "least",
    "less",
    "like",
    "long",
    "made",
    "make",
    "many",
    "may",
    "me",
    "might",
    "more",
    "most",
    "much",
    "must",
    "my",
    "myself",
    "near",
    "neither",
    "no",
    "nor",
    "not",
    "now",
    "of",
    "off",
    "often",
    "on",
    "once",
    "one",
    "only",
    "or",
    "other",
    "others",
    "otherwise",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "per",
    "perhaps",
    "quite",
    "rather",
    "same",
    "several",
    "shall",
    "she",
    "should",
    "since",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "therefore",
    "these",
    "they",
    "this",
    "those",
    "though",
    "through",
    "thus",
    "to",
    "too",
    "toward",
    "under",
    "until",
    "up",
    "upon",
    "us",
    "use",
    "used",
    "uses",
    "using",
    "very",
    "via",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "whether",
    "which",
    "while",
    "who",
    "whom",
    "whose",
    "why",
    "will",
    "with",
    "within",
    "without",
    "would",
    "yet",
    "you",
    "your",
    "yours",
    "yourself",
])

# A span shorter than this is punctuation debris rather than a sentence worth citing.
_MIN_SPAN_CHARS = 12
# Every window costs its own citation, so a hard ceiling keeps a long tail of one-sentence
# windows from crowding out real evidence.
_MAX_WINDOWS = 8
# How much covering more of the query is worth relative to repeating one term.
_COVERAGE_WEIGHT = 2.0
# Length damping: long sentences are not automatically better evidence.
_DENSITY_WEIGHT = 0.10


@dataclass(frozen=True)
class EvidenceDocument:
    """One recalled Memory entry, addressed exactly as its citation will address it."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str
    text: str

    @property
    def identity_key(self) -> tuple[str, str, str]:
        return (self.memory.scope_id, self.entry_id, self.entry_version_id)


@dataclass(frozen=True)
class EvidenceSpan:
    """A literal half-open character range of one Memory entry version."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Segment:
    start: int
    end: int
    stems: frozenset[str]
    length: int


@dataclass(frozen=True)
class _Window:
    """A contiguous run of sentences of one document: one citation, one span."""

    document: int
    first: int
    last: int


def select_evidence_spans(
    *,
    query: str,
    documents: Sequence[EvidenceDocument],
    fits: Callable[[Sequence[EvidenceSpan]], bool],
) -> tuple[EvidenceSpan, ...]:
    """Return literal spans of ``documents`` worth spending the budget on.

    ``fits`` receives the spans accepted so far plus the one under test and answers whether the
    rendered context would still satisfy the byte budget. Selection stops when candidates run
    out or nothing else fits; it never satisfies the budget by dropping accounting.
    """

    if not documents:
        return ()
    segments_by_document = tuple(_segments(document) for document in documents)
    if not any(segments_by_document):
        return ()

    scores = _score_segments(segments_by_document, _query_stems(query))
    if not scores:
        return ()

    windows: list[_Window] = []
    while len(windows) < _MAX_WINDOWS:
        window = _best_window(
            windows=windows,
            segments_by_document=segments_by_document,
            scores=scores,
            documents=documents,
            fits=fits,
        )
        if window is None:
            break
        windows.append(window)

    return tuple(_window_span(window, segments_by_document, documents) for window in windows)


def _best_window(
    *,
    windows: Sequence[_Window],
    segments_by_document: Sequence[Sequence[_Segment]],
    scores: dict[tuple[int, int], float],
    documents: Sequence[EvidenceDocument],
    fits: Callable[[Sequence[EvidenceSpan]], bool],
) -> _Window | None:
    """The contiguous window with the most query score that still fits the remaining budget.

    A window is scored by the summed sentence scores it covers, not by its length: length is
    paid for in bytes, and once a window already covers the passage, extending it over a
    weakly relevant sentence is cheaper than opening a second citation elsewhere. Length only
    breaks ties, which is why the longest window that fits each starting point is the candidate.
    """

    committed = [_window_span(window, segments_by_document, documents) for window in windows]
    best: _Window | None = None
    best_score = 0.0
    for document_index, segments in enumerate(segments_by_document):
        if not segments:
            continue
        window, score = _best_window_in_document(
            document_index=document_index,
            segments=segments,
            blocked=_blocked_ranges(windows, document_index),
            scores=scores,
            committed=committed,
            segments_by_document=segments_by_document,
            documents=documents,
            fits=fits,
        )
        if window is None:
            continue
        if score > best_score or (
            score == best_score and best is not None and (window.last - window.first) > (best.last - best.first)
        ):
            best = window
            best_score = score
    return best


def _blocked_ranges(windows: Sequence[_Window], document_index: int) -> list[tuple[int, int]]:
    return sorted((window.first, window.last) for window in windows if window.document == document_index)


def _best_window_in_document(
    *,
    document_index: int,
    segments: Sequence[_Segment],
    blocked: Sequence[tuple[int, int]],
    scores: dict[tuple[int, int], float],
    committed: Sequence[EvidenceSpan],
    segments_by_document: Sequence[Sequence[_Segment]],
    documents: Sequence[EvidenceDocument],
    fits: Callable[[Sequence[EvidenceSpan]], bool],
) -> tuple[_Window | None, float]:
    prefix = [0.0]
    for index in range(len(segments)):
        prefix.append(prefix[-1] + scores.get((document_index, index), 0.0))
    best: _Window | None = None
    best_score = 0.0
    for start in range(len(segments)):
        # A window should open on relevant text: starting on a sentence that matches nothing
        # only steals length from the end of the same window.
        if scores.get((document_index, start), 0.0) <= 0.0:
            continue
        if any(first <= start <= last for first, last in blocked):
            continue
        end = _longest_fitting(
            start=start,
            limit=_limit_for(start, blocked, len(segments)),
            document_index=document_index,
            committed=committed,
            segments_by_document=segments_by_document,
            documents=documents,
            fits=fits,
        )
        if end < start:
            continue
        candidate_score = prefix[end + 1] - prefix[start]
        if candidate_score > best_score or (
            candidate_score == best_score and best is not None and end - start > best.last - best.first
        ):
            best = _Window(document=document_index, first=start, last=end)
            best_score = candidate_score
    return best, best_score


def _limit_for(start: int, blocked: Sequence[tuple[int, int]], count: int) -> int:
    """Last index a window starting at ``start`` may reach without entering a taken range."""

    for first, _last in blocked:
        if first > start:
            return first - 1
    return count - 1


def _longest_fitting(
    *,
    start: int,
    limit: int,
    document_index: int,
    committed: Sequence[EvidenceSpan],
    segments_by_document: Sequence[Sequence[_Segment]],
    documents: Sequence[EvidenceDocument],
    fits: Callable[[Sequence[EvidenceSpan]], bool],
) -> int:
    """Largest end index whose window still fits; ``start - 1`` when even one sentence does not."""

    low, high = start, limit
    best = start - 1
    while low <= high:
        middle = (low + high) // 2
        window = _Window(document=document_index, first=start, last=middle)
        span = _window_span(window, segments_by_document, documents)
        if fits([*committed, span]):
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _window_span(
    window: _Window,
    segments_by_document: Sequence[Sequence[_Segment]],
    documents: Sequence[EvidenceDocument],
) -> EvidenceSpan:
    document = documents[window.document]
    start = segments_by_document[window.document][window.first].start
    end = segments_by_document[window.document][window.last].end
    return EvidenceSpan(
        memory=document.memory,
        entry_id=document.entry_id,
        entry_version_id=document.entry_version_id,
        start=start,
        end=end,
        text=document.text[start:end],
    )


def _segments(document: EvidenceDocument) -> tuple[_Segment, ...]:
    segments: list[_Segment] = []
    seen: set[str] = set()
    for start, end in _sentence_spans(document.text):
        body = document.text[start:end]
        tokens = _TOKEN_RE.findall(body.lower())
        if len(tokens) < 2 or len(body) < _MIN_SPAN_CHARS:
            continue
        key = " ".join(tokens)
        if key in seen:
            # Keep the first occurrence: a repeated sentence is the same evidence anyway, and
            # a duplicate would spend budget twice.
            continue
        seen.add(key)
        segments.append(
            _Segment(
                start=start,
                end=end,
                stems=frozenset(_stem(token) for token in tokens),
                length=len(tokens),
            )
        )
    return tuple(segments)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split on sentence punctuation and newlines, keeping exact character offsets."""

    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\n":
            _push(spans, text, start, index)
            index += 1
            start = index
            continue
        if char in ".!?":
            end = index + 1
            while end < length and text[end] in _QUOTE_RUN:
                end += 1
            if end >= length or text[end].isspace():
                _push(spans, text, start, end)
                start = end
                index = end
                continue
            index = end
            continue
        index += 1
    _push(spans, text, start, length)
    return spans


def _push(spans: list[tuple[int, int]], text: str, start: int, end: int) -> None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if end <= start:
        return
    # A window that later grows right absorbs its separator anyway; keeping it here means a
    # gold range ending on the space after the period is still covered on its own.
    if end < len(text) and text[end].isspace():
        end += 1
    spans.append((start, end))


def _query_stems(query: str) -> tuple[str, ...]:
    tokens = _TOKEN_RE.findall(query.lower())
    stems = [_stem(token) for token in tokens if token not in _STOPWORDS]
    unique = tuple(dict.fromkeys(stems))
    if unique:
        return unique
    return tuple(dict.fromkeys(_stem(token) for token in tokens))


def _stem(token: str) -> str:
    if token.isdigit() or len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith(("ss", "us", "is")):
        return token
    if token.endswith("s"):
        return token[:-1]
    if token.endswith("ing") and len(token) > 5:
        return token[:-3]
    if token.endswith("ed") and len(token) > 4:
        return token[:-2]
    return token


def _score_segments(
    segments_by_document: Sequence[Sequence[_Segment]],
    query_stems: Sequence[str],
) -> dict[tuple[int, int], float]:
    """IDF-weighted query overlap per sentence, damped by sentence length."""

    wanted = set(query_stems)
    if not wanted:
        return {}
    total = sum(len(segments) for segments in segments_by_document)
    document_frequency: dict[str, int] = dict.fromkeys(wanted, 0)
    for segments in segments_by_document:
        for segment in segments:
            for stem in segment.stems & wanted:
                document_frequency[stem] += 1
    weight = {stem: math.log((total + 1.0) / (document_frequency[stem] + 0.5)) + 0.25 for stem in wanted}
    scores: dict[tuple[int, int], float] = {}
    for document_index, segments in enumerate(segments_by_document):
        for segment_index, segment in enumerate(segments):
            matched = segment.stems & wanted
            if not matched:
                continue
            overlap = sum(weight[stem] for stem in matched)
            coverage = len(matched) / len(wanted)
            density = 1.0 + _DENSITY_WEIGHT * math.log1p(segment.length)
            scores[(document_index, segment_index)] = overlap * (1.0 + _COVERAGE_WEIGHT * coverage) / density
    return scores
