"""SCRUM-53 Phase B. The cache/audit key for app.models.AgentRationale rows.

compute_fact_fingerprint hashes app.investigation_agent.payload.build_payload's
output (the flagged transaction's own snapshot plus each fired rule's own
RuleHit.values, SCRUM-68) together with PROMPT_VERSION -- deliberately NOT
InvestigationState["evidence"] (the tool-gathered evidence). That's what lets
both the refresh path (app.routers.transactions._refresh_flags) and
scripts.compose_rationales's own "is this already composed?" check compute a
fingerprint with NO tool call and NO model call, from data they already have
on hand (a flagged transaction plus the rules engine's own per-hit values).

Known limitation, documented rather than fixed here: because tool evidence
isn't part of the hash, a user's transaction history can change in a way
that alters what a tool would report (e.g. get_geo_distance's
typical_location_label, or get_transaction_history's window contents)
without changing any rule's own computed values -- in which case a cached
rationale can go stale without the fingerprint catching it. In practice this
gap is narrow: the same per-rule values that feed the fingerprint
(amount_deviation's category_mean/category_stdev, geographic_anomaly's
distance_mean_miles/distance_stdev_miles, ...) are themselves recomputed
from that same history on every rules-engine run, so a history change large
enough to move a tool's evidence usually moves the rule's own values too,
which does change the fingerprint.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any

from app.investigation_agent.prompts import PROMPT_VERSION


def _canonical_json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def compute_fact_fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 hex digest of a canonical (sorted-keys, stable-number-format)
    JSON serialization of `payload` (build_payload's output) plus
    PROMPT_VERSION -- deterministic across runs for the same payload, and
    changes whenever a rule's own value, the transaction snapshot, or
    PROMPT_VERSION changes (see module docstring for what deliberately does
    NOT affect it: tool evidence).
    """
    canonical = json.dumps(payload, sort_keys=True, default=_canonical_json_default)
    return hashlib.sha256(f"{canonical}|{PROMPT_VERSION}".encode()).hexdigest()
