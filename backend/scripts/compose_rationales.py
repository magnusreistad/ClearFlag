"""SCRUM-53 Phase B / SCRUM-56. Runs the Investigation Agent graph for flagged
transactions that don't yet have a passing agent_rationales row for their
current fact fingerprint + PROMPT_VERSION, and persists exactly one new row
per attempt (pass or fail). This is the ONLY place a model call happens for
this pipeline -- GET /transactions (app/routers/transactions.py's
_refresh_flags) only ever reads agent_rationales, never composes.

Honors INVESTIGATION_AGENT_LLM_MODE (mock/live, see
app/investigation_agent/llm.py) exactly like every other caller of the
graph -- nothing here forces live mode.

Idempotent: a transaction that already has a passing row for its current
fact fingerprint + PROMPT_VERSION is skipped on every subsequent run, so this
is safe to re-run as often as needed (new transactions landing, a
PROMPT_VERSION bump, retrying after a transient failure) without duplicating
model calls or rows.

SCRUM-56 retry cap: a transaction whose validation-failed attempts (for its
current (transaction_id, fact_fingerprint, prompt_version, VALIDATOR_VERSION)
budget key -- see MAX_VALIDATION_ATTEMPTS) have reached the cap is skipped
too, so an unlucky model that keeps producing borderline-ungrounded output
doesn't get unlimited chances to slip past validation. Only validation
failures consume that budget: an attempt that never produced text at all
(composition_error -- a tool or model failure) doesn't count against it, and
is always safe to retry on the next run.

Run with: python -m scripts.compose_rationales
Scope to one user: python -m scripts.compose_rationales --user-id 2
Preview without calling the model: python -m scripts.compose_rationales --dry-run
Compose more than the default 25 per run: python -m scripts.compose_rationales --limit 100
"""

import argparse
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.investigation_agent import graph as graph_module
from app.investigation_agent.fingerprint import compute_fact_fingerprint
from app.investigation_agent.llm import current_model_id
from app.investigation_agent.payload import build_payload
from app.investigation_agent.prompts import PROMPT_VERSION
from app.investigation_agent.state import InvestigationState, transaction_data_from_orm
from app.investigation_agent.validation import (
    MAX_VALIDATION_ATTEMPTS,
    VALIDATOR_VERSION,
)
from app.models import AgentRationale, Transaction
from app.rules.engine import (
    evaluate_all_rules,
    rule_names_by_transaction,
    rule_values_by_transaction,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 25

# SCRUM-56 outcome taxonomy -- every transaction this script looks at lands
# in exactly one of these each run, and each is both a per-transaction log
# line's `outcome` field and a key in the run's final summary line.
OUTCOME_PASSED = "passed"
OUTCOME_VALIDATION_FAILED = "validation_failed"
OUTCOME_COMPOSITION_ERROR = "composition_error"
OUTCOME_SKIPPED_CAPPED = "skipped_capped"
OUTCOME_SKIPPED_ALREADY_PASSING = "skipped_already_passing"
_OUTCOME_ORDER = (
    OUTCOME_PASSED,
    OUTCOME_VALIDATION_FAILED,
    OUTCOME_COMPOSITION_ERROR,
    OUTCOME_SKIPPED_CAPPED,
    OUTCOME_SKIPPED_ALREADY_PASSING,
)


@dataclass
class PendingComposition:
    transaction: Transaction
    rule_names: list[str]
    rule_values: dict[str, dict]
    fingerprint: str
    # Validation-failed attempts already recorded for this transaction's
    # current budget key, before this run -- used to report "attempt N of
    # MAX_VALIDATION_ATTEMPTS" in the per-attempt log line.
    attempts_used: int


@dataclass
class SkippedAlreadyPassing:
    transaction_id: int
    fingerprint: str


@dataclass
class SkippedCapped:
    transaction_id: int
    fingerprint: str
    attempts_used: int


def _collect_pending(
    db: Session, user_id: int | None
) -> tuple[list[PendingComposition], list[SkippedAlreadyPassing], list[SkippedCapped]]:
    """Every flagged transaction in scope (optionally narrowed to one user),
    grouped by user since each rule needs that user's full transaction
    history to evaluate correctly (the same requirement
    app.routers.transactions._refresh_flags has), split three ways:

    - already passing (a validation_passed=true row for the current fact
      fingerprint + PROMPT_VERSION already exists) -- never retried;
    - capped (SCRUM-56: MAX_VALIDATION_ATTEMPTS validation-failed rows
      already exist for (transaction_id, fact_fingerprint, PROMPT_VERSION,
      VALIDATOR_VERSION) -- a composition_error attempt never counts here,
      and a row with validator_version IS NULL never matches VALIDATOR_VERSION
      by exact-equality, so legacy rows written before that column existed
      don't count either) -- filtered out here, BEFORE --limit is applied,
      so a capped transaction never takes up a run slot that could have gone
      to a transaction still worth attempting;
    - pending -- everything else, oldest-transaction-id-first.

    One query per user for each of the "already passing" / "attempts used"
    checks (not one per transaction) -- a user's flagged-transaction count is
    small enough at this project's scale that this isn't the N+1 the
    SCRUM-53 ticket's "one query" requirement is aimed at (that requirement
    targets _refresh_flags's per-request path, which this offline script
    isn't).
    """
    query = db.query(Transaction)
    if user_id is not None:
        query = query.filter(Transaction.user_id == user_id)

    transactions_by_user: dict[int, list[Transaction]] = defaultdict(list)
    for transaction in query.all():
        transactions_by_user[transaction.user_id].append(transaction)

    pending: list[PendingComposition] = []
    skipped_already_passing: list[SkippedAlreadyPassing] = []
    skipped_capped: list[SkippedCapped] = []

    for user_transactions in transactions_by_user.values():
        hits = evaluate_all_rules(user_transactions)
        rule_names_by_id = rule_names_by_transaction(hits)
        if not rule_names_by_id:
            continue
        rule_values_by_id = rule_values_by_transaction(hits)

        transactions_by_id = {t.id: t for t in user_transactions}
        fingerprint_by_id = {
            transaction_id: compute_fact_fingerprint(
                build_payload(
                    transaction_data_from_orm(transactions_by_id[transaction_id]),
                    rule_names,
                    rule_values_by_id.get(transaction_id, {}),
                )
            )
            for transaction_id, rule_names in rule_names_by_id.items()
        }
        fingerprint_pairs = list(fingerprint_by_id.items())

        already_passing = set(
            db.query(AgentRationale.transaction_id, AgentRationale.fact_fingerprint).filter(
                tuple_(AgentRationale.transaction_id, AgentRationale.fact_fingerprint).in_(fingerprint_pairs),
                AgentRationale.prompt_version == PROMPT_VERSION,
                AgentRationale.validation_passed.is_(True),
            )
        )

        attempts_used_by_id = dict(
            db.query(AgentRationale.transaction_id, func.count(AgentRationale.id))
            .filter(
                tuple_(AgentRationale.transaction_id, AgentRationale.fact_fingerprint).in_(fingerprint_pairs),
                AgentRationale.prompt_version == PROMPT_VERSION,
                AgentRationale.validator_version == VALIDATOR_VERSION,
                AgentRationale.validation_passed.is_(False),
                AgentRationale.composition_error.is_(None),
            )
            .group_by(AgentRationale.transaction_id)
            .all()
        )

        for transaction_id, rule_names in rule_names_by_id.items():
            fingerprint = fingerprint_by_id[transaction_id]

            if (transaction_id, fingerprint) in already_passing:
                skipped_already_passing.append(SkippedAlreadyPassing(transaction_id, fingerprint))
                continue

            attempts_used = attempts_used_by_id.get(transaction_id, 0)
            if attempts_used >= MAX_VALIDATION_ATTEMPTS:
                skipped_capped.append(SkippedCapped(transaction_id, fingerprint, attempts_used))
                continue

            pending.append(
                PendingComposition(
                    transaction=transactions_by_id[transaction_id],
                    rule_names=rule_names,
                    rule_values=rule_values_by_id.get(transaction_id, {}),
                    fingerprint=fingerprint,
                    attempts_used=attempts_used,
                )
            )

    pending.sort(key=lambda p: p.transaction.id)
    skipped_already_passing.sort(key=lambda s: s.transaction_id)
    skipped_capped.sort(key=lambda s: s.transaction_id)
    return pending, skipped_already_passing, skipped_capped


def _compose_one(pending: PendingComposition) -> tuple[AgentRationale, str | None]:
    """Runs the compiled investigation graph for one transaction (via
    app.investigation_agent.graph.invoke, so LangSmith trace metadata is
    attached exactly like every other caller) and builds the row to persist
    -- pass or fail. Never raises on a model/composition failure: the graph
    itself never crashes on one (see compose_rationale's own docstring), and
    composition_error already carries what went wrong.

    rationale vs. composed_text (SCRUM-53 follow-up): `rationale` comes from
    state["rationale"], which validate() wipes to "" on a failed validation
    -- so it's None here on any failure, exactly as before. composed_text
    comes from state["composed_rationale"], which compose_rationale writes
    once and validate() never touches (see InvestigationState's docstring),
    so it holds the model's actual output on a failed attempt too -- the
    audit record app.models.AgentRationale.composed_text exists for. On a
    SCRUM-56 tool-error short circuit, compose_rationale never calls the
    model at all, so composed_rationale stays "" and composed_text is None
    here, same as a model failure.
    """
    transaction_data = transaction_data_from_orm(pending.transaction)
    state: InvestigationState = {
        "transaction": transaction_data,
        "rule_names": pending.rule_names,
        "rule_values": pending.rule_values,
        "evidence": {},
        "rationale": "",
        "composed_rationale": "",
    }
    result = graph_module.invoke(state)

    row = AgentRationale(
        transaction_id=pending.transaction.id,
        fact_fingerprint=pending.fingerprint,
        prompt_version=PROMPT_VERSION,
        model_id=current_model_id(),
        rationale=result.get("rationale") or None,
        validation_passed=result.get("rationale_source") == "agent",
        violations=[
            {"span": v.span, "violation_type": v.violation_type, "reason": v.reason}
            for v in result.get("violations", [])
        ],
        composition_error=result.get("composition_error"),
        composed_text=result.get("composed_rationale") or None,
        validator_version=VALIDATOR_VERSION,
    )
    return row, result.get("composition_error_label")


def _outcome_for_row(row: AgentRationale) -> str:
    if row.composition_error is not None:
        return OUTCOME_COMPOSITION_ERROR
    if row.validation_passed:
        return OUTCOME_PASSED
    return OUTCOME_VALIDATION_FAILED


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", type=int, default=None, help="scope composition to this user only")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"max transactions to compose (default {DEFAULT_LIMIT})")
    parser.add_argument("--dry-run", action="store_true", help="list what would be composed, without calling the model")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        full_pending, skipped_already_passing, skipped_capped = _collect_pending(db, args.user_id)
        pending = full_pending[: args.limit]

        if args.dry_run:
            skipped = len(skipped_already_passing) + len(skipped_capped)
            print(f"{len(pending)} transaction(s) would be composed (dry run, no model calls):")
            for p in pending:
                print(f"  transaction {p.transaction.id}: rules={p.rule_names} fingerprint={p.fingerprint[:12]}...")
            for s in skipped_capped:
                print(f"  transaction {s.transaction_id}: SKIPPED (capped at {MAX_VALIDATION_ATTEMPTS} validation failures)")
            print(f"composed=0 passed=0 failed=0 skipped={skipped} (dry run)")
            return

        outcome_counts: Counter[str] = Counter()

        for skip in skipped_already_passing:
            outcome_counts[OUTCOME_SKIPPED_ALREADY_PASSING] += 1
            logger.info(
                "transaction=%s fingerprint=%s outcome=%s",
                skip.transaction_id,
                skip.fingerprint[:12],
                OUTCOME_SKIPPED_ALREADY_PASSING,
            )

        for skip in skipped_capped:
            outcome_counts[OUTCOME_SKIPPED_CAPPED] += 1
            logger.info(
                "transaction=%s fingerprint=%s outcome=%s attempt=%s/%s",
                skip.transaction_id,
                skip.fingerprint[:12],
                OUTCOME_SKIPPED_CAPPED,
                skip.attempts_used,
                MAX_VALIDATION_ATTEMPTS,
            )

        for p in pending:
            row, error_label = _compose_one(p)
            db.add(row)
            db.commit()

            outcome = _outcome_for_row(row)
            outcome_counts[outcome] += 1

            if outcome == OUTCOME_COMPOSITION_ERROR:
                # composition_error attempts don't consume the validation-failure
                # budget, so the attempt count shown here is unchanged by this run.
                attempt_display = f"{p.attempts_used}/{MAX_VALIDATION_ATTEMPTS}"
                detail = f"error_class={error_label}"
            else:
                attempt_display = f"{p.attempts_used + 1}/{MAX_VALIDATION_ATTEMPTS}"
                if outcome == OUTCOME_VALIDATION_FAILED:
                    violation_types = ",".join(v["violation_type"] for v in row.violations)
                    detail = f"violation_types={violation_types}"
                else:
                    detail = ""

            logger.info(
                "transaction=%s fingerprint=%s outcome=%s attempt=%s %s",
                p.transaction.id,
                p.fingerprint[:12],
                outcome,
                attempt_display,
                detail,
            )

        summary = " ".join(f"{outcome}={outcome_counts.get(outcome, 0)}" for outcome in _OUTCOME_ORDER)
        logger.info("run complete: %s", summary)

        if outcome_counts.get(OUTCOME_COMPOSITION_ERROR, 0) > 0:
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
