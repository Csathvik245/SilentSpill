"""Guardrails applied in code to everything the model writes.

strip_accusatory rewrites or drops blaming language; clean_text also drops any sentence that names
a facility or cites a number not returned by a tool (see ToolLedger).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from .tools import ToolLedger

M = TypeVar("M", bound=BaseModel)

# Word boundaries that also treat hyphens as part of a word, so model ids such as
# "illegal-dumping-detection" are left alone.
_L = r"(?<![\w-])"
_R = r"(?![\w-])"

# Regex of every accusatory term (case-insensitive). Anything still matching after the rewrites
# drops its sentence.
ACCUSATORY: list[re.Pattern[str]] = [
    re.compile(_L + p + _R, re.IGNORECASE)
    for p in (
        r"caused",
        r"cause\s+of",
        r"responsible",
        r"responsibility",
        r"polluters?",
        r"source\s+of\s+the\s+spill",
        r"culprits?",
        r"to\s+blame",
        r"dumped",
        r"dumping",
        r"guilty",
    )
]

_ADJ = r"(?:(?:likely|probable|probably|possible|possibly|main|primary|root|suspected|apparent|definite|clear)\s+)?"
_BE = r"(?:is|are|was|were|may\s+be|might\s+be|could\s+be|must\s+be|appears\s+to\s+be|seems\s+to\s+be)"
_SPILL_NOUN = r"(?:spill|contamination|pollution|release|discharge|incident|anomaly|plume|spike)"

# (pattern, replacement) applied in order. Replacements never match ACCUSATORY, so this is idempotent.
_REWRITES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_L + r"(?:(?:the|a)\s+)?" + _ADJ + r"sources?\s+of\s+the\s+" + _SPILL_NOUN + _R, re.I),
     "a facility with reported violations near this reach"),
    (re.compile(_L + _BE + r"\s+" + r"(?:(?:the|a)\s+)?" + _ADJ + r"cause\s+of" + _R, re.I),
     "is consistent with"),
    (re.compile(_L + r"(?:(?:likely|probably|possibly|apparently)\s+)?caused\s+by" + _R, re.I),
     "consistent with"),
    (re.compile(_L + r"(?:(?:has|have|had|may\s+have|might\s+have|could\s+have|must\s+have|likely|probably|possibly|apparently)\s+)*caused" + _R, re.I),
     "is consistent with"),
    (re.compile(_L + _BE + r"\s+" + _ADJ + r"responsible\s+for" + _R, re.I),
     "has reported violations near"),
    (re.compile(_L + r"responsible\s+for" + _R, re.I), "with reported violations near"),
    (re.compile(_L + r"polluters" + _R, re.I), "facilities"),
    (re.compile(_L + r"polluter" + _R, re.I), "facility"),
    (re.compile(_L + r"dumping" + _R, re.I), "release"),
    (re.compile(_L + r"dumped" + _R, re.I), "released"),
]

# ---- sentences --------------------------------------------------------------------------------
_ABBREV = frozenset(
    "e.g i.e u.s etc approx no st inc co corp ltd vs dr mr ms mrs jr sr fig est min max avg".split()
)
_SENT_END = re.compile(r"[.!?]+[\"')\]]*\s+")


def split_sentences(text: str) -> list[str]:
    """Split one line of prose into sentences, keeping common abbreviations intact."""
    out, start = [], 0
    for m in _SENT_END.finditer(text):
        before = text[start:m.start()].rsplit(None, 1)
        last = before[-1].lower().rstrip(".") if before else ""
        nxt = text[m.end():m.end() + 1]
        if last in _ABBREV or (nxt and not (nxt.isupper() or nxt.isdigit() or nxt in "\"'([")):
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _map_sentences(text: str, fn) -> str:
    """Apply fn to each sentence (returns new sentence or None to drop); keeps line structure."""
    lines = []
    for line in text.splitlines():
        if not line.strip():
            lines.append(line)
            continue
        kept = [s for s in (fn(s) for s in split_sentences(line)) if s]
        if kept:
            lines.append(" ".join(kept))
    return "\n".join(lines).strip()



def _match_case(src: str, repl: str) -> str:
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl


def strip_accusatory(text: str) -> tuple[str, list[str]]:
    """Rewrite blaming phrases to neutral wording; drop sentences that cannot be rewritten."""
    notes: list[str] = []

    def fix(sentence: str) -> str | None:
        s = sentence
        for pat, repl in _REWRITES:
            def sub(m: re.Match[str], repl: str = repl) -> str:
                new = _match_case(m.group(0), repl)
                notes.append(f"rewrote accusatory wording '{m.group(0)}' as '{new}'")
                return new
            s = pat.sub(sub, s)
        for pat in ACCUSATORY:
            m = pat.search(s)
            if m:
                # The dropped sentence is not quoted, so rejected wording never reaches the output.
                notes.append(f"dropped a sentence with accusatory wording '{m.group(0)}'")
                return None
        return s

    if not text:
        return text, notes
    return _map_sentences(text, fix), notes


# ---- numbers ----------------------------------------------------------------------------------
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# Spans removed before numbers are read: dates, times, years, and ID-like alphanumeric tokens.
_IGNORE = [
    re.compile(
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2}|\s*UTC)?",
        re.I,
    ),
    re.compile(r"\b\d{4}\d{2}\d{2}T\d{2,4}Z?\b", re.I),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:UTC|Z|GMT|EDT|EST|[ap]\.?m\.?))?", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s+\d{4})?", re.I),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"(?:\s+\d{4})?", re.I),
    re.compile(r"\b(?:19|20)\d{2}\s*[–—-]\s*(?:19|20)?\d{2}\b"),
    re.compile(r"(?<![\d.,])\b(?:19|20)\d{2}\b(?![.,]?\d)"),
]
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")


def normalize_number(s: str) -> str:
    """'7.40' -> '7.4', '2.0' -> '2', '1,200' -> '1200', '007' -> '7'."""
    s = s.replace(",", "").lstrip("+-")
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac.rstrip("0")
    else:
        whole, frac = s, ""
    whole = whole.lstrip("0") or "0"
    return f"{whole}.{frac}" if frac else whole


def number_tokens(text: str) -> list[str]:
    """Numbers in text, normalized; dates, times, years and numbers inside IDs are ignored."""
    if not text:
        return []
    for pat in _IGNORE:
        text = pat.sub(" ", text)
    return [normalize_number(m.group(1) + (m.group(2) or "")) for m in _NUMBER.finditer(text)]


# Small counts are allowed without a tool value ("one candidate", "rank 1").
ALWAYS_ALLOWED_NUMBERS = frozenset({"0", "1"})


def unknown_numbers(text: str, allowed: set[str]) -> list[str]:
    """Numbers in text that are not in allowed (normalized), in order, without repeats."""
    allowed_n = {normalize_number(a) for a in allowed} | ALWAYS_ALLOWED_NUMBERS
    out: list[str] = []
    for t in number_tokens(text):
        if t not in allowed_n and t not in out:
            out.append(t)
    return out


# ---- facilities -------------------------------------------------------------------------------
_ID_PATTERNS = [
    re.compile(r"\b(?=[A-Z0-9]*\d)(?:VA|VAG|VAR|VAL)[A-Z0-9]{5,8}\b"),
    re.compile(r"\b(?=[A-Z0-9]*\d)[A-Z]{2}[A-Z0-9]{7}\b"),
]
_SUFFIX = r"(?i:LLC|INC|CORP|CORPORATION|COMPANY|CO|LP|LTD|PLANT|STP|WWTP|FACILITY|ENTERPRISES|PRODUCTS)"
_WORD = r"[A-Z0-9][A-Za-z0-9&'’-]*"
_NAME = re.compile(
    r"(?<![\w-])((?:" + _WORD + r",?\s+(?:(?:and|of|&)\s+)?){1,7}" + _SUFFIX + r")\.?(?![\w-])"
)
# Leading words that start a sentence or describe a facility rather than name one.
_LEAD_STOP = frozenset(
    """THE THIS THAT THESE THOSE A AN EACH ONE NO ANY EVERY ALL SOME ITS THEIR OUR UPSTREAM DOWNSTREAM
    CANDIDATE CANDIDATES NEARBY NEAREST REPORTED PERMITTED SAME OTHER ANOTHER FIRST SECOND THIRD FOURTH
    FIFTH LISTED RANKED NAMED AT BY FOR FROM IN ON OF AND OR WITH NEAR TO AS IS ARE WAS WERE BUT IF
    WHILE ONLY NOTE SEE RANK FACILITY FACILITIES PERMIT PERMITS NPDES ECHO TRI""".split()
)


def _norm_name(s: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9&]+", " ", s.upper()).split())


def unknown_facilities(text: str, allowed_ids: set[str], allowed_names: set[str]) -> list[str]:
    """Permit-like IDs and company-like names in text that no tool returned."""
    if not text:
        return []
    out: list[str] = []
    ids = {i.strip().upper() for i in allowed_ids}
    for pat in _ID_PATTERNS:
        for m in pat.finditer(text):
            if m.group(0) not in ids and m.group(0) not in out:
                out.append(m.group(0))
    names = [_norm_name(n) for n in allowed_names]
    for m in _NAME.finditer(text):
        for words in _name_segments(m.group(1).replace(",", " ").split()):
            while words and (words[0].upper().strip("'’") in _LEAD_STOP or any(ch.isdigit() for ch in words[0])):
                words.pop(0)
            if len(words) < 2 or words[-1].upper() not in _SUFFIX_WORDS:
                continue  # only a suffix left ("The plant"), or no suffix at all
            phrase = " ".join(words)
            key = _norm_name(phrase)
            if not any(key in n for n in names) and phrase not in out:
                out.append(phrase)
    return out


_SUFFIX_WORDS = frozenset(
    "LLC INC CORP CORPORATION COMPANY CO LP LTD PLANT STP WWTP FACILITY ENTERPRISES PRODUCTS".split()
)


def _name_segments(words: list[str]) -> list[list[str]]:
    """Split 'ACME CORP and Valley Milk Products LLC' into one list of words per name."""
    segs, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if w.upper() in _SUFFIX_WORDS and nxt is not None and nxt.lower() in ("and", "of", "&"):
            segs.append(cur)
            cur = []
        elif w.lower() in ("and", "of", "&") and not cur[:-1]:
            cur = []  # a connector left at the start of a new segment
    if cur:
        segs.append(cur)
    return segs


# ---- combined ---------------------------------------------------------------------------------
def clean_text(text: str, ledger: ToolLedger, extra_numbers: Iterable[str] = frozenset()) -> tuple[str, list[str]]:
    """strip_accusatory, then drop sentences naming unknown facilities or citing unknown numbers."""
    text, notes = strip_accusatory(text or "")
    allowed_numbers = set(ledger.numbers()) | {normalize_number(n) for n in extra_numbers}
    ids, names = ledger.facility_ids(), ledger.facility_names()

    def check(sentence: str) -> str | None:
        bad_fac = unknown_facilities(sentence, ids, names)
        if bad_fac:
            # Never echo the rejected names or ids: they would reappear in the report output.
            notes.append(f"dropped a sentence naming {len(bad_fac)} facility reference(s) not in the tool data")
            return None
        bad_num = unknown_numbers(sentence, allowed_numbers)
        if bad_num:
            notes.append(f"dropped a sentence with numbers not in the tool data ({', '.join(bad_num)})")
            return None
        return sentence

    return _map_sentences(text, check), notes


def enforce_flags(model: M) -> M:
    """Set requires_human_review (and simulated, in simulation mode) on model and nested responses."""
    from ..schemas import ResponseBase, _simulated_default

    sim = _simulated_default()
    seen: set[int] = set()

    def walk(obj: Any) -> None:
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, BaseModel):
            if isinstance(obj, ResponseBase):
                obj.requires_human_review = True
                if sim:
                    obj.simulated = True
            for name in type(obj).model_fields:
                walk(getattr(obj, name, None))
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                walk(v)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)

    walk(model)
    return model
