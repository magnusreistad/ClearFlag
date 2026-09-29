"""SCRUM-52. The post-generation guardrail: checks that a composed rationale
only references values present in (a) the rules-engine payload for this
invocation or (b) evidence returned by a tool call in this same run
(Investigation Agent Design Doc SS5). No tool call, no citation -- an
errored tool's evidence key is absent from `evidence` (see
app.investigation_agent.graph.assemble_rationale), so a rationale citing a
value that only that tool could have supplied fails to ground, with no
special-casing needed here.

Pure, deterministic, offline: no LLM call, no DB query, no network request.
SCRUM-53 wires this in as the post-composition check and tunes the entity
heuristic below against real model output; SCRUM-56 owns what happens on
failure (fallback to the interim formatter, logging). Not wired into the
graph yet.

--- payload shape this module expects ---
`payload` is a dict describing this invocation's rules-engine facts,
independent of (and richer than) app.investigation_agent.state.
TransactionData -- SCRUM-53 is responsible for actually assembling it when
this validator is wired into the graph:

    {
        "transaction": {  # the flagged transaction's own citable fields
            "id": int, "user_id": int, "merchant": str, "category": str,
            "amount": Decimal, "location_label": str,
            "latitude": float, "longitude": float,
        },
        "rule_names": ["new_merchant_risk", "amount_deviation", ...],
        "rules": {
            # Per-rule numeric facts the rules engine computed but today only
            # bakes into RuleHit.rationale's prose (see app.rules.
            # amount_deviation) rather than exposing structurally. Until
            # SCRUM-53 extends the rules engine to expose these, this is the
            # documented gap: whoever builds `payload` must source these the
            # same way amount_deviation.py itself does.
            "amount_deviation": {
                "amount": Decimal, "category_mean": Decimal, "category_stdev": Decimal,
            },
        },
    }

`evidence` is exactly `InvestigationState["evidence"]`: a dict keyed by tool
name (e.g. "get_geo_distance"), each value the tool's own evidence dict.

--- key -> unit mapping (numeric grounding) ---
Units are inferred from the trailing key name in a fact's flattened path,
via a small explicit table (_UNIT_BY_KEY below) rather than a suffix guess
over arbitrary keys -- a key not in that table is left unitless rather than
guessed at, per the ticket's instruction. Current mapping:

    amount, category_mean, category_stdev, percent_above_mean  -> "$" / "%"
    distance_km                                                -> "km"
    distance_miles                                              -> "mi"
    everything else (ids, lat/lon, counts, booleans excluded)   -> None (unitless)

A number written in the rationale with no recognizable unit token (no "$"
prefix, no "%" suffix, no "miles"/"mi"/"km" word) is checked against every
citable numeric fact regardless of that fact's unit -- we don't know what
the author meant it to be, so being unit-agnostic there is the conservative
choice; a number that DOES carry a unit token is only checked against facts
tagged with that same unit (this is what makes "10,864 miles" fail even
though 10864.40 exists as km).

--- named-entity heuristic (v1, conservative) ---
Flags two shapes of proper-noun-like text that don't match any citable
string (case-insensitive): (1) a "City, Region"-style span -- one or more
capitalized words, a comma, one or more capitalized words -- e.g. the
location_label format this codebase already uses ("Seattle, WA", "Manila,
Philippines"); (2) two-or-more consecutive capitalized words with no comma,
e.g. a merchant name ("Meridian Duty-Free Traders"). A short sentence-initial
stopword list (_ENTITY_STOPWORDS) avoids flagging the first word of a
sentence when it happens to precede another capitalized word.

Known limitations (documented for SCRUM-53 to tune, not fixed here):
  - False negatives: a genuine invented entity that's a single capitalized
    word with no comma (e.g. a bare "Tokyo" with no ", Japan") is never
    flagged -- the heuristic requires two words or a comma. Same for a
    lowercase or partially-capitalized fabrication.
  - False positives: any other legitimately-capitalized multi-word phrase
    not in the stopword list and not a citable fact (e.g. a proper adjective
    phrase, a holiday name, a rule name written in title case) gets flagged
    even though it isn't really a fabricated entity.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.investigation_agent.derived_facts import compute_derived_facts

# --- unit inference -------------------------------------------------------

# Trailing flattened-path key -> the unit token that key's value is denominated
# in, using the same tags numbers are recognized under in rationale text (see
# _NUMBER_RE / _TEXT_UNIT_ALIASES below). A key not listed here is unitless --
# deliberately not a suffix/substring guess, so an ambiguous or unfamiliar key
# never gets a silently-wrong unit assigned to it.
_UNIT_BY_KEY: dict[str, str] = {
    "amount": "$",
    "category_mean": "$",
    "category_stdev": "$",
    "percent_above_mean": "%",
    "distance_km": "km",
    "distance_miles": "mi",
}

# Flattened-path keys that are real values but never citable in a rationale's
# prose (identifiers, coordinates, and any boolean/None) -- excluded from both
# numeric and string fact sets rather than surfaced as unitless noise.
_EXCLUDED_KEYS = {"id", "user_id", "latitude", "longitude", "risk_tier"}


@dataclass(frozen=True)
class NumericFact:
    value: Decimal
    unit: str | None
    source: str


@dataclass(frozen=True)
class StringFact:
    value: str
    source: str


@dataclass(frozen=True)
class Violation:
    span: str
    violation_type: str
    reason: str


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    violations: list[Violation]


def _last_key(path: str) -> str:
    """The trailing key name of a flattened path, stripping any list index
    suffix (e.g. "evidence.get_transaction_history.transactions[0].amount"
    -> "amount")."""
    return path.rsplit(".", 1)[-1].split("[", 1)[0]


def _flatten_facts(prefix: str, obj: Any, numeric: list[NumericFact], strings: list[StringFact]) -> None:
    """Walks a nested payload/evidence/derived structure, collecting every
    leaf value as a citable fact tagged with its source path. Booleans and
    None are never citable (a rationale saying "true" or "first" isn't
    citing a value, it's paraphrasing one); datetimes are left out too --
    nothing in this codebase's rationales cites a raw timestamp.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            _flatten_facts(f"{prefix}.{key}" if prefix else key, value, numeric, strings)
        return
    if isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            _flatten_facts(f"{prefix}[{i}]", value, numeric, strings)
        return

    key = _last_key(prefix)
    if key in _EXCLUDED_KEYS or isinstance(obj, bool):
        return
    if isinstance(obj, (int, float, Decimal)):
        numeric.append(NumericFact(value=Decimal(str(obj)), unit=_UNIT_BY_KEY.get(key), source=prefix))
    elif isinstance(obj, str):
        strings.append(StringFact(value=obj, source=prefix))
    # None, datetime, and anything else: not citable, silently skipped.


def _build_citable_facts(payload: dict[str, Any], evidence: dict[str, Any]) -> tuple[list[NumericFact], list[StringFact]]:
    numeric: list[NumericFact] = []
    strings: list[StringFact] = []
    _flatten_facts("transaction", payload.get("transaction", {}), numeric, strings)
    _flatten_facts("rules", payload.get("rules", {}), numeric, strings)
    _flatten_facts("evidence", evidence, numeric, strings)
    _flatten_facts("derived", compute_derived_facts(payload), numeric, strings)
    return numeric, strings


# --- numeric grounding ------------------------------------------------------

# $3,200 / 3,200 / 3200.00 / 6,750.82 miles / 10,864 miles / 1,873% / -12.5
_NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<sign>-)?"
    r"(?P<dollar>\$)?"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<percent>%)?"
    r"(?:[ \t](?P<unit>miles?|mi|km)\b)?"
)

_TEXT_UNIT_ALIASES = {"mile": "mi", "miles": "mi", "mi": "mi", "km": "km"}


def _round_to(value: Decimal, precision: int, rounding: str) -> Decimal:
    quantum = Decimal(1).scaleb(-precision)
    return value.quantize(quantum, rounding=rounding)


def _extract_numbers(rationale: str) -> list[dict[str, Any]]:
    tokens = []
    for match in _NUMBER_RE.finditer(rationale):
        digits = match.group("number")
        try:
            value = Decimal(digits.replace(",", ""))
        except InvalidOperation:
            continue
        if match.group("sign"):
            value = -value
        precision = len(digits.split(".", 1)[1]) if "." in digits else 0

        if match.group("dollar"):
            unit = "$"
        elif match.group("percent"):
            unit = "%"
        elif match.group("unit"):
            unit = _TEXT_UNIT_ALIASES[match.group("unit").lower()]
        else:
            unit = None

        tokens.append(
            {"span": match.group(0), "value": value, "precision": precision, "unit": unit}
        )
    return tokens


def _check_number(token: dict[str, Any], facts: list[NumericFact]) -> Violation | None:
    value, precision, unit = token["value"], token["precision"], token["unit"]
    same_unit_facts = [f for f in facts if unit is None or f.unit == unit]

    for fact in same_unit_facts:
        if _round_to(fact.value, precision, ROUND_HALF_UP) == value:
            return None  # grounded

    other_unit_facts = [f for f in facts if unit is not None and f.unit is not None and f.unit != unit]
    for fact in other_unit_facts:
        if _round_to(fact.value, precision, ROUND_HALF_UP) == value:
            return Violation(
                span=token["span"],
                violation_type="wrong_unit",
                reason=(
                    f"{token['span']!r} matches {fact.source} ({fact.value}) rounded to "
                    f"{precision} decimal place(s), but that fact is denominated in "
                    f"{fact.unit!r}, not {unit!r}."
                ),
            )

    for fact in same_unit_facts:
        truncated = _round_to(fact.value, precision, ROUND_DOWN)
        if truncated == value and truncated != _round_to(fact.value, precision, ROUND_HALF_UP):
            return Violation(
                span=token["span"],
                violation_type="truncated_number",
                reason=(
                    f"{token['span']!r} is {fact.source} ({fact.value}) truncated rather than "
                    f"rounded -- correctly rounded it would read "
                    f"{_round_to(fact.value, precision, ROUND_HALF_UP)}."
                ),
            )

    return Violation(
        span=token["span"],
        violation_type="ungrounded_number",
        reason=f"{token['span']!r} does not match any citable payload/evidence fact"
        + (f" with unit {unit!r}." if unit else "."),
    )


# --- named-entity grounding -------------------------------------------------

_WORD = r"[A-Z][a-zA-Z'&-]*"
_LOCATION_RE = re.compile(rf"\b({_WORD}(?:\s{_WORD})*),\s({_WORD}(?:\s{_WORD})*)\b")
_MULTIWORD_RE = re.compile(rf"\b{_WORD}(?:\s{_WORD})+\b")

# Sentence-initial capitalized words common enough in generated prose that
# they'd otherwise be caught by _MULTIWORD_RE when immediately followed by
# another capitalized word (e.g. a citable entity at the very start of a
# clause). Deliberately short -- see the module docstring's known-limitations
# note on this heuristic's false-positive rate.
_ENTITY_STOPWORDS = {"This", "The", "A", "An", "It", "Flagged", "Note"}


def _strip_stopword_prefix(span: str) -> str:
    words = span.split(" ")
    while len(words) > 1 and words[0] in _ENTITY_STOPWORDS:
        words = words[1:]
    return " ".join(words)


def _is_citable_string(candidate: str, strings: list[StringFact]) -> bool:
    lowered = candidate.strip(" .,").lower()
    return any(lowered in fact.value.lower() or fact.value.lower() in lowered for fact in strings)


def _check_entities(rationale: str, strings: list[StringFact]) -> list[Violation]:
    violations: list[Violation] = []
    claimed_spans: set[tuple[int, int]] = set()

    for match in _LOCATION_RE.finditer(rationale):
        span_text = match.group(0)
        if not _is_citable_string(span_text, strings):
            violations.append(
                Violation(
                    span=span_text,
                    violation_type="unsupported_entity",
                    reason=f"{span_text!r} does not match any citable payload/evidence string.",
                )
            )
        claimed_spans.add(match.span())

    for match in _MULTIWORD_RE.finditer(rationale):
        if any(match.start() >= s and match.end() <= e for s, e in claimed_spans):
            continue  # already covered by a location match
        span_text = _strip_stopword_prefix(match.group(0))
        if len(span_text.split(" ")) < 2:
            continue  # was only a stopword followed by one real word
        if not _is_citable_string(span_text, strings):
            violations.append(
                Violation(
                    span=span_text,
                    violation_type="unsupported_entity",
                    reason=f"{span_text!r} does not match any citable payload/evidence string.",
                )
            )

    return violations


# --- entry point -------------------------------------------------------------


def validate_rationale(rationale: str, payload: dict[str, Any], evidence: dict[str, Any]) -> ValidationResult:
    """Checks that every number and proper-noun-like phrase in `rationale`
    traces back to a value in `payload` or `evidence` (or a value this
    module derives from `payload` -- see derived_facts.py). Returns every
    violation found, not just the first, so a caller (or SCRUM-56's log)
    sees the whole picture in one pass.
    """
    if not rationale.strip():
        return ValidationResult(
            passed=False,
            violations=[
                Violation(span="", violation_type="empty_rationale", reason="Rationale is empty or whitespace-only.")
            ],
        )

    numeric_facts, string_facts = _build_citable_facts(payload, evidence)

    violations: list[Violation] = []
    for token in _extract_numbers(rationale):
        violation = _check_number(token, numeric_facts)
        if violation is not None:
            violations.append(violation)
    violations.extend(_check_entities(rationale, string_facts))

    return ValidationResult(passed=len(violations) == 0, violations=violations)
