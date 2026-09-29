"""SCRUM-53 Phase B. Runs the Investigation Agent graph for flagged
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

Run with: python -m scripts.compose_rationales
Scope to one user: python -m scripts.compose_rationales --user-id 2
Preview without calling the model: python -m scripts.compose_rationales --dry-run
Compose more than the default 25 per run: python -m scripts.compose_rationales --limit 100
"""

import argparse
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.investigation_agent import graph as graph_module
from app.investigation_agent.fingerprint import compute_fact_fingerprint
from app.investigation_agent.llm import current_model_id
from app.investigation_agent.payload import build_payload
from app.investigation_agent.prompts import PROMPT_VERSION
from app.investigation_agent.state import InvestigationState, transaction_data_from_orm
from app.investigation_agent.validation import VALIDATOR_VERSION
from app.models import AgentRationale, Transaction
from app.rules.engine import (
    evaluate_all_rules,
    rule_names_by_transaction,
    rule_values_by_transaction,
)

DEFAULT_LIMIT = 25


@dataclass
class PendingComposition:
    transaction: Transaction
    rule_names: list[str]
    rule_values: dict[str, dict]
    fingerprint: str


def _collect_pending(db: Session, user_id: int | None) -> tuple[int, list[PendingComposition]]:
    """Every flagged transaction in scope (optionally narrowed to one user),
    grouped by user since each rule needs that user's full transaction
    history to evaluate correctly (the same requirement
    app.routers.transactions._refresh_flags has), minus whichever already
    have a passing agent_rationales row for their current fact fingerprint +
    PROMPT_VERSION. Returns (total flagged count in scope, still-pending
    transactions oldest-id-first) so the caller can report how many were
    skipped as already-composed, independent of what --limit then truncates.

    One query per user for the already-passing check (not one per
    transaction) -- a user's flagged-transaction count is small enough at
    this project's scale that this isn't the N+1 the SCRUM-53 ticket's "one
    query" requirement is aimed at (that requirement targets
    _refresh_flags's per-request path, which this offline script isn't).
    """
    query = db.query(Transaction)
    if user_id is not None:
        query = query.filter(Transaction.user_id == user_id)

    transactions_by_user: dict[int, list[Transaction]] = defaultdict(list)
    for transaction in query.all():
        transactions_by_user[transaction.user_id].append(transaction)

    total_flagged = 0
    pending: list[PendingComposition] = []
    for user_transactions in transactions_by_user.values():
        hits = evaluate_all_rules(user_transactions)
        rule_names_by_id = rule_names_by_transaction(hits)
        if not rule_names_by_id:
            continue
        rule_values_by_id = rule_values_by_transaction(hits)
        total_flagged += len(rule_names_by_id)

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

        already_passing = set(
            db.query(AgentRationale.transaction_id, AgentRationale.fact_fingerprint).filter(
                tuple_(AgentRationale.transaction_id, AgentRationale.fact_fingerprint).in_(
                    list(fingerprint_by_id.items())
                ),
                AgentRationale.prompt_version == PROMPT_VERSION,
                AgentRationale.validation_passed.is_(True),
            )
        )

        for transaction_id, rule_names in rule_names_by_id.items():
            fingerprint = fingerprint_by_id[transaction_id]
            if (transaction_id, fingerprint) in already_passing:
                continue
            pending.append(
                PendingComposition(
                    transaction=transactions_by_id[transaction_id],
                    rule_names=rule_names,
                    rule_values=rule_values_by_id.get(transaction_id, {}),
                    fingerprint=fingerprint,
                )
            )

    pending.sort(key=lambda p: p.transaction.id)
    return total_flagged, pending


def _compose_one(pending: PendingComposition) -> AgentRationale:
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
    audit record app.models.AgentRationale.composed_text exists for.
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

    return AgentRationale(
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", type=int, default=None, help="scope composition to this user only")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"max transactions to compose (default {DEFAULT_LIMIT})")
    parser.add_argument("--dry-run", action="store_true", help="list what would be composed, without calling the model")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        total_flagged, full_pending = _collect_pending(db, args.user_id)
        pending = full_pending[: args.limit]
        skipped = total_flagged - len(full_pending)

        if args.dry_run:
            print(f"{len(pending)} transaction(s) would be composed (dry run, no model calls):")
            for p in pending:
                print(f"  transaction {p.transaction.id}: rules={p.rule_names} fingerprint={p.fingerprint[:12]}...")
            print(f"composed=0 passed=0 failed=0 skipped={skipped} (dry run)")
            return

        composed = passed = failed = 0
        for p in pending:
            row = _compose_one(p)
            db.add(row)
            db.commit()
            composed += 1
            if row.validation_passed:
                passed += 1
            else:
                failed += 1
            print(
                f"transaction {p.transaction.id}: "
                f"{'PASSED' if row.validation_passed else 'FAILED'} "
                f"(rules={p.rule_names})"
            )

        print(f"composed={composed} passed={passed} failed={failed} skipped={skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
