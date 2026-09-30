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
TransactionData. app.investigation_agent.payload.build_payload assembles
this shape from a flagged transaction plus app.rules.engine.FlagHit.values
(SCRUM-68); SCRUM-53 is responsible for actually calling it once this
validator is wired into the graph:

    {
        "transaction": {  # the flagged transaction's own citable fields
            "id": int, "user_id": int, "merchant": str, "category": str,
            "amount": Decimal, "location_label": str,
            "latitude": float, "longitude": float,
        },
        "rule_names": ["new_merchant_risk", "amount_deviation", ...],
        "rules": {
            # Per-rule numeric/string facts the rule itself already computed
            # (RuleHit.values, SCRUM-68) -- shape varies by rule, see each
            # rule module's RuleHit.values comment for exactly which keys it
            # populates and why:
            "amount_deviation": {
                "amount": Decimal, "category_mean": Decimal, "category_stdev": Decimal,
                "percent_above_mean": Decimal,
            },
            "new_merchant_risk": {
                "amount": Decimal, "typical_first_purchase_mean": Decimal,
                "typical_first_purchase_stdev": Decimal, "percent_above_mean": Decimal,
            },
            "geographic_anomaly": {
                # distance_miles here is the same great-circle figure
                # get_geo_distance (this rule's mapped tool) also returns as
                # evidence["distance_miles"] -- both are legitimate citation
                # sources per the Design Doc (SS5: payload OR tool output).
                # "No tool call, no citation" governs facts ONLY a tool
                # produces -- e.g. this same tool's distance_km and
                # typical_location_label, which this rule never computes.
                "location_label": str, "distance_miles": float,
                "distance_mean_miles": float, "distance_stdev_miles": float,
            },
            "velocity": {"transaction_count": int, "window_minutes": int},
        },
    }

`evidence` is exactly `InvestigationState["evidence"]`: a dict keyed by tool
name (e.g. "get_geo_distance"), each value the tool's own evidence dict.

--- key -> unit mapping (numeric grounding) ---
Units are inferred from the trailing key name in a fact's flattened path,
via a small explicit table (_UNIT_BY_KEY below) rather than a suffix guess
over arbitrary keys -- a key not in that table is left unitless rather than
guessed at, per the ticket's instruction. Current mapping:

    amount, category_mean, category_stdev,
    typical_first_purchase_mean, typical_first_purchase_stdev,
    percent_above_mean                                          -> "$" / "$" / "%"
    distance_km                                                  -> "km"
    distance_miles, distance_mean_miles, distance_stdev_miles    -> "mi"
    everything else (ids, lat/lon, counts, booleans excluded)   -> None (unitless)

A number written in the rationale with no recognizable unit token (no "$"
prefix, no "%" suffix, no "miles"/"mi"/"km" word) is checked against every
citable numeric fact regardless of that fact's unit -- we don't know what
the author meant it to be, so being unit-agnostic there is the conservative
choice; a number that DOES carry a unit token is only checked against facts
tagged with that same unit (this is what makes "10,864 miles" fail even
though 10864.40 exists as km).

--- named-entity heuristic (v2, SCRUM-53 tightening) ---
Flags three shapes of proper-noun-like text that don't match any citable
string (case-insensitive): (1) a "City, Region"-style span -- one or more
capitalized words, a comma, one or more capitalized words -- e.g. the
location_label format this codebase already uses ("Seattle, WA", "Manila,
Philippines"); (2) two-or-more consecutive capitalized words with no comma,
e.g. a merchant name ("Meridian Duty-Free Traders"); (3) SCRUM-53: a single
capitalized word that isn't otherwise exempt and isn't already covered by
(1) or (2) -- the v1 hole this closes, e.g. a bare invented "Tokyo" with no
", Japan" to trip the two-word/comma heuristic above.

SCRUM-53 tightening, round 2: v1 of heuristic (3) exempted ANY capitalized
word purely by position (sentence-initial, or right after '.', '!', '?', or
':'), which reopened the same hole it was meant to close -- a model tends to
put an invented place name exactly at the start of a sentence ("Singapore is
9,000 miles from Seattle."), and the old rule waved every one of those
through unchecked. Round 2 gated that exemption by content instead: a word
was only exempt for being sentence-initial if it was ALSO in a short,
hand-picked allowlist of words this codebase's rule rationales and live
model output happened to open sentences with. That approach traded one hole
for another: 839's own live composition failed validation on "Together"
opening a sentence -- a perfectly ordinary word, just one nobody had
enumerated -- and any other natural opener (However, Additionally, Because,
Overall, ...) not already on the list would fail the same way, forever,
since the list only grows by someone noticing another miss.

SCRUM-53 round 3: replaced the hand-picked allowlist with a real dictionary
check (_is_common_word) against the bundled New General Service List (NGSL,
see data/NGSL_LICENSE.md) -- a ~2,800-word pedagogical vocabulary list,
verified (not assumed) to contain zero of a sample set of common place names
before adoption, unlike several raw web/news-corpus word-frequency lists
that were checked first and rejected for exactly that reason (see
NGSL_LICENSE.md's own note). A sentence-initial capitalized word is now
exempt if its lowercased form is a common English word per that list --
breadth that covers any natural sentence opener, not just ones someone
happened to enumerate, while a genuinely invented proper noun (not a
dictionary word, by construction) still isn't exempt just for showing up
first in a sentence. Because NGSL is a LEMMA list (one base form per word
family -- "you" but not its possessive "your", "that" but not its plural
"these", "additional" but not the regularly-derived adverb "additionally"),
_is_common_word also checks a small, closed, hand-maintained set of
grammatical variant forms (_GRAMMATICAL_VARIANT_LEMMAS: possessives,
plurals, a few pronoun case-forms) and strips a regular "-ly" adverb suffix
back to its adjective lemma -- neither of which can introduce a proper noun,
since (a) that set is a fixed, exhaustively-enumerable closed class of
English function words, not a growing list of "words we've seen," and (b)
the "-ly" strip only ever tests the SAME proper-noun-free dictionary against
the stripped form. A contraction ("it's", "that's") is checked by its
pre-apostrophe stem the same way, for the same reason.

SCRUM-53 round 3 follow-up: NGSL being a lemma list also meant an ordinary
INFLECTED word opening a sentence -- "Purchases", "Looking", "Based" -- was
still failing, for the same reason "additionally" needed the "-ly" strip.
_is_common_word now also tries a small set of regular-inflection candidates
(_regular_inflection_candidates: plural/-s/-es/-ies, past tense -ed,
progressive -ing, including doubled-consonant and e-drop spelling) stripped
back to a lemma, checked against the same dictionary -- same non-proper-noun
guarantee as everything else here, since it only ever re-checks the existing
list against a normalized form. This does NOT rescue every inflected word,
only ones whose lemma is actually in NGSL: "transaction" itself isn't an
NGSL entry at all (a domain/financial term outside this general pedagogical
vocabulary), so "Transaction"/"Transactions" still fail sentence-initially
regardless of inflection -- a real, narrow, documented gap, not a stripping
bug (see this module's tests for the exact case).

"Sentence-initial" itself is still positional (_is_sentence_initial): a word
at position `start` qualifies if nothing precedes it, if the nearest
preceding non-space character ends a clause ('.', '!', or '?'), OR if the
immediately preceding text is the interim formatter's own literal "Flagged:"
marker (every app.rules.* rationale reads "Flagged: <Subject> ...", with the
subject varying by rule -- "This", "It's", "You"). That last case is handled
as an explicit, named literal string match, not a general "any colon is a
clause boundary" rule -- it was exactly that general rule that let "Flagged:
Singapore ..." slip through in the v1 (round 1) heuristic, since it exempted
whatever followed ANY colon. The literal word "Flagged" immediately followed
by ':' is separately exempted outright (never checked for citability, at any
position) since it's the interim formatter's own template keyword, not a
proper noun -- see the dedicated check in _check_entities. _SINGLE_WORD_STOPWORDS
covers just "I" on top of all of this -- the one common English word that's
capitalized regardless of position and so can't be exempted positionally at
all, unlike the dictionary check above (which only ever applies at
sentence-initial position).

A short sentence-initial stopword list (_ENTITY_STOPWORDS) avoids flagging
the first word of a sentence when it happens to precede another capitalized
word, for heuristic (2) above. This is unrelated to the dictionary check:
heuristic (2)'s list exists to strip a leading non-entity word off the front
of a captured multi-word span so the real phrase underneath still gets
checked (it is never itself a reason to skip checking), so it isn't gated by
position the way heuristic (3)'s exemption is.

Known limitations (documented, not fixed here):
  - False negatives: a genuine invented entity that's lowercase or only
    partially capitalized is never flagged by any of the three shapes above
    -- they all require Title Case. A genuinely invented entity that happens
    to share a spelling with an NGSL word (e.g. a model inventing "Will" or
    "May" as a person's name -- both common English words on their own)
    would also slip through in sentence-initial position -- a real gap, but
    one shared by any dictionary-based approach and far narrower than v1
    (round 1)'s "any word, any position" hole or round 2's "only words
    someone happened to list" gap.
  - False positives: any other legitimately-capitalized word or multi-word
    phrase not in a stopword list, not an NGSL word, and not a citable fact
    (e.g. a proper adjective, a holiday name, a rule name written in title
    case, a genuinely novel word the model title-cases for emphasis) gets
    flagged even though it isn't really a fabricated entity. SCRUM-53's
    tightening makes this heuristic strictly more aggressive than v1 (bare
    single words are now in scope too, and sentence-initial position no
    longer grants an unconditional pass), so this false-positive rate is
    correspondingly higher than v1's -- an intentional trade-off per the
    ticket's instruction to close the false-negative hole, not to preserve
    v1's precision.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.investigation_agent.derived_facts import compute_derived_facts
from app.investigation_agent.prompts import MAX_RATIONALE_CHARS

# SCRUM-53 follow-up. Bumps on any change to this module's entity/citation
# heuristic that could change which rationales pass or fail -- same
# bump-on-behavior-change convention as app.investigation_agent.prompts
# .PROMPT_VERSION, recorded onto every app.models.AgentRationale row
# (scripts.compose_rationales) so a later audit can tell "this failed under
# an old, since-improved validator" apart from "this failed under the
# current one". History: v1 -- the original blanket sentence-initial
# exemption (any capitalized word, any position rule, closed by SCRUM-53's
# first pass); v2 -- a hand-picked sentence-starter allowlist (closed by
# this same ticket's live check surfacing "Together" as a false positive);
# v3 -- the current NGSL dictionary check, extended with regular-inflection
# stripping (this version).
VALIDATOR_VERSION = "v3"

# SCRUM-56. Caps how many *validation-failed* composition attempts
# scripts.compose_rationales will make per (transaction_id, fact_fingerprint,
# prompt_version, VALIDATOR_VERSION) budget key before skipping that
# transaction and leaving it on the interim rationale -- model output
# varies, so unbounded retries could eventually push a borderline,
# ungrounded rationale through validation by chance. A composition_error
# attempt (a tool or model failure, not a validation failure) never
# consumes this budget -- see scripts.compose_rationales._collect_pending.
# A PROMPT_VERSION or VALIDATOR_VERSION bump resets the budget for every
# transaction, same as it resets the pass/fail cache itself.
MAX_VALIDATION_ATTEMPTS = 2

# --- common-word dictionary (NGSL) -----------------------------------------

_DATA_DIR = Path(__file__).parent / "data"


def _load_common_words() -> frozenset[str]:
    """The bundled NGSL word list (see data/NGSL_LICENSE.md for source,
    license, and why this particular list was chosen), lowercased into a
    set for O(1) lookup. Loaded once at import time -- it's a fixed,
    ~2,800-word static asset, not something that changes at runtime.
    """
    text = (_DATA_DIR / "ngsl_common_words.txt").read_text()
    return frozenset(word.lower() for word in text.split())


_COMMON_WORDS = _load_common_words()

# NGSL is a LEMMA list (one base form per word family), so it doesn't
# separately enumerate every grammatical variant of a word it does list --
# e.g. "you" but not the possessive "your", "that" but not the plural
# "these". This is a fixed, closed, exhaustively-enumerable set of English
# determiner/pronoun forms -- NOT a hand-picked or growing list of "words
# we've seen" -- so it can never introduce a proper noun: there are only so
# many possessive/plural/case forms of English pronouns and demonstratives.
_GRAMMATICAL_VARIANT_LEMMAS = {
    "an": "a",
    "these": "that",
    "those": "that",
    "your": "you",
    "yours": "you",
    "its": "it",
    "my": "i",
    "mine": "i",
    "our": "we",
    "ours": "we",
    "his": "he",
    "her": "she",
    "hers": "she",
    "their": "they",
    "theirs": "they",
}


def _regular_inflection_candidates(word: str) -> list[str]:
    """Candidate base (lemma) forms for `word` under simple, regular English
    inflection -- plural/3rd-person -s/-es/-ies, past tense -ed, and
    progressive -ing -- including the two common spelling adjustments that
    go with them (a doubled final consonant before -ed/-ing, e.g.
    "stopped"/"running"; a dropped final "e" before -ing, e.g. "making").
    Not a general stemmer: only the handful of mechanical, deterministic
    patterns regular English inflection actually uses, in a fixed order,
    most-specific first. Every candidate is checked by the caller against
    the SAME proper-noun-free NGSL dictionary (_is_common_word) -- so, like
    the grammatical-variant map and "-ly" strip there, this can only ever
    re-discover a word already in that list, never introduce a new one:
    "Londons" -> "london" is still absent, "Tokyo's" was already handled by
    the apostrophe-stem check before this function is even tried.
    """
    candidates = []

    if word.endswith("ing") and len(word) > 4:
        stem = word[:-3]
        candidates.append(stem)  # look+ing -> look
        candidates.append(stem + "e")  # mak+ing -> make (e-drop)
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
            candidates.append(stem[:-1])  # runn+ing -> run (doubled consonant)

    if word.endswith("ed") and len(word) > 3:
        stem_drop_d = word[:-1]
        candidates.append(stem_drop_d)  # base+d -> base
        stem_drop_ed = word[:-2]
        candidates.append(stem_drop_ed)  # look+ed -> look
        if len(stem_drop_ed) > 2 and stem_drop_ed[-1] == stem_drop_ed[-2] and stem_drop_ed[-1] not in "aeiou":
            candidates.append(stem_drop_ed[:-1])  # stopp+ed -> stop (doubled consonant)

    if word.endswith("ies") and len(word) > 4:
        candidates.append(word[:-3] + "y")  # cit+ies -> city

    if word.endswith("es") and len(word) > 3:
        candidates.append(word[:-1])  # purchase+s -> purchase
        candidates.append(word[:-2])  # box+es -> box

    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        candidates.append(word[:-1])  # word+s -> word

    return candidates


def _is_common_word(word: str) -> bool:
    """Whether `word` (case-insensitive) is a common English word, per
    module docstring's "SCRUM-53 round 3" section -- used ONLY to decide
    whether a SENTENCE-INITIAL capitalized word is exempt from the entity
    check (see _check_entities). A common word appearing mid-sentence still
    only grounds by being a citable fact, exactly as before; this function
    is never consulted there.

    Checks, in order: (1) the word itself against the NGSL list; (2) a
    contraction's pre-apostrophe stem ("it's" -> "it", "that's" -> "that")
    against the same list, since a real contraction of a real common word is
    still a common word -- and a contraction of an invented proper noun
    ("Tokyo's") still correctly fails, because "tokyo" isn't in the list
    either; (3) the closed grammatical-variant map above; (4) a regular "-ly"
    adverb stripped back to its adjective lemma ("additionally" ->
    "additional"); (5) a regular inflection (plural, past tense, or
    progressive -- see _regular_inflection_candidates) stripped back to its
    lemma ("purchases" -> "purchase", "looking" -> "look", "based" ->
    "base"), since NGSL lists lemmas, not every inflected surface form. None
    of these steps can introduce a proper noun: (2)-(5) only ever re-check
    the SAME proper-noun-free dictionary against a normalized form, never a
    separately-maintained word list -- a word whose lemma genuinely isn't in
    NGSL (e.g. "transaction", a domain-specific term outside this general
    pedagogical vocabulary list) still fails no matter how it's inflected.
    """
    lowered = word.lower()
    if lowered in _COMMON_WORDS:
        return True
    if "'" in lowered:
        stem = lowered.split("'", 1)[0]
        if stem in _COMMON_WORDS:
            return True
    if lowered in _GRAMMATICAL_VARIANT_LEMMAS:
        return True
    if lowered.endswith("ly") and len(lowered) > 3 and lowered[:-2] in _COMMON_WORDS:
        return True
    return any(candidate in _COMMON_WORDS for candidate in _regular_inflection_candidates(lowered))

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
    # SCRUM-68: app.rules.new_merchant_risk.RuleHit.values' equivalent of
    # category_mean/category_stdev -- kept as separate keys rather than
    # reusing those two since it's a mean/stdev over first-time purchases
    # across merchants, not a spend category; same unit ($), different fact.
    "typical_first_purchase_mean": "$",
    "typical_first_purchase_stdev": "$",
    "percent_above_mean": "%",
    "distance_km": "km",
    "distance_miles": "mi",
    # SCRUM-68: app.rules.geographic_anomaly.RuleHit.values' historical
    # mean/stdev distance-from-centroid this hit was compared against --
    # same unit as distance_miles (miles), different fact.
    "distance_mean_miles": "mi",
    "distance_stdev_miles": "mi",
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
_SINGLE_WORD_RE = re.compile(rf"\b{_WORD}\b")

# Sentence-initial capitalized words common enough in generated prose that
# they'd otherwise be caught by _MULTIWORD_RE when immediately followed by
# another capitalized word (e.g. a citable entity at the very start of a
# clause). Deliberately short -- see the module docstring's known-limitations
# note on this heuristic's false-positive rate. Not position-gated itself
# (see module docstring): this strips a leading stopword off a captured span
# so the phrase underneath still gets checked, it never skips checking.
_ENTITY_STOPWORDS = {"This", "The", "A", "An", "It", "Flagged", "Note"}

# SCRUM-53 round 3: sentence-initial exemption is now a dictionary check
# (_is_common_word, above) instead of a hand-picked allowlist -- see module
# docstring's "round 3" section for why (round 2's allowlist kept missing
# ordinary words like "Together", which is exactly what 839's live
# composition hit). "I" is the one common English word that's capitalized
# regardless of position and so can't be exempted positionally at all --
# the only entry this list needs, independent of the dictionary check.
_SINGLE_WORD_STOPWORDS = {"I"}

# The interim formatter's own template keyword (app.rules.*'s "Flagged: "
# prefix) -- handled as a named literal, not via a general "colon is a
# clause boundary" rule (see module docstring for why the general rule was
# removed).
_INTERIM_FLAG_MARKER = "Flagged:"


def _is_sentence_initial(rationale: str, start: int) -> bool:
    """A word at `start` is sentence-initial if nothing precedes it, if the
    nearest preceding non-whitespace character ends a clause ('.', '!', or
    '?'), or if the text immediately before it is the interim formatter's
    literal "Flagged:" marker -- see module docstring for why that's a named
    literal rather than a general "any colon ends a clause" rule (the
    general rule is what let "Flagged: Singapore ..." slip through in round
    1). This function only decides POSITION; the caller still gates the
    actual exemption on _is_common_word.
    """
    prefix = rationale[:start].rstrip()
    return prefix == "" or prefix[-1] in ".!?" or prefix.endswith(_INTERIM_FLAG_MARKER)


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
        claimed_spans.add(match.span())

    # SCRUM-53: closes the v1 hole where a bare single capitalized word (no
    # comma, no second capitalized word beside it -- e.g. an invented
    # "Tokyo") was never checked at all. Only runs against words not already
    # covered by a location or multi-word match above, since those are
    # already handled (whether they turned into a violation or not).
    for match in _SINGLE_WORD_RE.finditer(rationale):
        if any(match.start() >= s and match.end() <= e for s, e in claimed_spans):
            continue  # already covered by a location or multi-word match
        span_text = match.group(0)
        if span_text in _SINGLE_WORD_STOPWORDS:
            continue
        if span_text == "Flagged" and rationale[match.end() : match.end() + 1] == ":":
            continue  # the interim formatter's own template keyword, not a proper noun
        if _is_sentence_initial(rationale, match.start()) and _is_common_word(span_text):
            continue
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
    module derives from `payload` -- see derived_facts.py), and that
    `rationale` doesn't exceed MAX_RATIONALE_CHARS (SCRUM-53: the same
    ~500-char cap communicated to the model in prompts.py, shared from there
    rather than redefined here). An over-cap rationale is a validation
    failure, never truncated here or anywhere else -- truncating a rationale
    that already passed citation grounding could cut a cited number or
    entity mid-string, which would either break the citation this module
    already confirmed or read as obviously broken to the account holder;
    the caller's only recourse is to fall back to the interim formatter.
    Returns every violation found, not just the first, so a caller (or
    SCRUM-56's log) sees the whole picture in one pass.
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
    if len(rationale) > MAX_RATIONALE_CHARS:
        violations.append(
            Violation(
                span="",
                violation_type="rationale_too_long",
                reason=(
                    f"Rationale is {len(rationale)} characters, over the {MAX_RATIONALE_CHARS}-character "
                    "cap. Never truncated -- an over-cap rationale fails validation and falls back to the "
                    "interim formatter instead."
                ),
            )
        )
    for token in _extract_numbers(rationale):
        violation = _check_number(token, numeric_facts)
        if violation is not None:
            violations.append(violation)
    violations.extend(_check_entities(rationale, string_facts))

    return ValidationResult(passed=len(violations) == 0, violations=violations)
