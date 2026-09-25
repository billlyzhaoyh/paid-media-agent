"""The DuckDB repositories honour the persistence protocols, in memory and on disk."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from paid_media_agent.domain.proposals import (
    ApprovalClaim,
    ProposalRecord,
    ProposalState,
    WriteReceipt,
)
from paid_media_agent.store import Store, StoreBusy
from tests.unit.test_proposals_and_security import _changeset


@pytest.fixture(params=["memory", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    opened = Store() if request.param == "memory" else Store(tmp_path / "state" / "pma.duckdb")
    yield opened
    opened.close()


def _record(
    thread_id: str = "t1", state: ProposalState = ProposalState.AWAITING_APPROVAL
) -> ProposalRecord:
    return ProposalRecord(
        changeset=_changeset(proposal_id=uuid4(), thread_id=thread_id),
        state=state,
        routing_id=f"rt-{uuid4()}",
    )


def _claim(proposal_id: UUID, *, approved_at: datetime) -> ApprovalClaim:
    return ApprovalClaim(
        claim_id=uuid4(),
        proposal_id=proposal_id,
        revision=1,
        payload_digest="digest",
        account_ref="demo-google",
        tool_name="google_ads__update_campaign_budget",
        requester_ref="local-user",
        approver_ref="reviewer-1",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(minutes=15),
        nonce="n",
        signature="s",
    )


def test_proposals_insert_once_and_replace_only_the_expected_record(store: Store) -> None:
    proposals = store.repositories.proposals
    record = _record()
    assert proposals.save(record) and not proposals.save(record)
    assert proposals.get(record.changeset.proposal_id) == record
    assert proposals.get_by_routing_id(record.routing_id) == record
    revised = record.model_copy(update={"state": ProposalState.EXECUTING, "routing_id": "rt-new"})
    assert proposals.save(revised, expected=record)
    assert not proposals.save(record, expected=record), "a stale expected record loses"
    assert proposals.get_by_routing_id("rt-new") == revised
    assert proposals.get_by_routing_id(record.routing_id) is None
    other = _record()
    assert not proposals.save(revised, expected=other), "expected must be the same proposal"


def test_racing_replacements_have_exactly_one_winner(store: Store) -> None:
    proposals = store.repositories.proposals
    record = _record()
    assert proposals.save(record)
    updates = [
        record.model_copy(update={"state": ProposalState.EXECUTING}),
        record.model_copy(update={"state": ProposalState.REJECTED}),
    ]
    ready = Barrier(2, timeout=5)

    def compete(updated: ProposalRecord) -> bool:
        ready.wait()
        return proposals.save(updated, expected=record)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(compete, updates))
    assert sorted(results) == [False, True]
    assert proposals.get(record.changeset.proposal_id) == updates[results.index(True)]


def test_proposals_list_per_thread_in_creation_order(store: Store) -> None:
    proposals = store.repositories.proposals
    first, second, elsewhere = _record("t1"), _record("t1"), _record("t2")
    for record in (first, second, elsewhere):
        assert proposals.save(record)
    assert proposals.list_for_thread("t1") == [first, second]
    assert proposals.list_for_thread("missing") == []


def test_claims_are_single_use_and_the_latest_unused_wins(store: Store) -> None:
    approvals = store.repositories.approvals
    proposal_id = uuid4()
    now = datetime.now(UTC)
    older, newer = (
        _claim(proposal_id, approved_at=now),
        _claim(proposal_id, approved_at=now + timedelta(seconds=5)),
    )
    approvals.save(older)
    approvals.save(newer)
    assert approvals.latest_unused(proposal_id, 1) == newer
    assert approvals.latest_unused(proposal_id, 2) is None
    assert approvals.mark_used(newer.claim_id) is True
    assert approvals.mark_used(newer.claim_id) is False, "replay"
    assert approvals.mark_used(uuid4()) is False, "unknown claim"
    assert approvals.latest_unused(proposal_id, 1) == older


def test_receipts_upsert_and_round_trip(store: Store) -> None:
    receipts = store.repositories.receipts
    proposal_id = uuid4()
    receipt = WriteReceipt(
        proposal_id=proposal_id,
        revision=1,
        status="unknown",
        mutation_attempted=True,
        provider_operation_ref=None,
        verified_state=(),
        checked_at=datetime.now(UTC),
        catalog_revision="rev1",
    )
    receipts.save(receipt)
    verified = receipt.model_copy(update={"status": "verified"})
    receipts.save(verified)
    assert receipts.get(proposal_id) == verified and receipts.get(uuid4()) is None


def test_dedupe_and_thread_ownership(store: Store) -> None:
    repos = store.repositories
    assert repos.dedupe.seen("event-1") is False and repos.dedupe.seen("event-1") is True
    assert repos.threads.claim("thread-1", "alice") and not repos.threads.claim("thread-1", "bob")
    assert repos.threads.owner("thread-1") == "alice" and repos.threads.owner("other") is None


def test_state_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "pma.duckdb"
    first = Store(path)
    record = _record()
    assert first.repositories.proposals.save(record)
    first.repositories.threads.claim("thread-1", "alice")
    first.close()
    reopened = Store(path)
    assert reopened.repositories.proposals.get(record.changeset.proposal_id) == record
    assert reopened.repositories.threads.owner("thread-1") == "alice"
    reopened.close()


def test_a_second_process_is_told_the_file_is_busy(tmp_path: Path) -> None:
    path = tmp_path / "pma.duckdb"
    held = Store(path)
    probe = (
        "from paid_media_agent.store import Store, StoreBusy\n"
        "try:\n"
        f"    Store({str(path)!r})\n"
        "    print('opened')\n"
        "except StoreBusy:\n"
        "    print('busy')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)  # noqa: S603
    held.close()
    assert result.stdout.strip() == "busy", result.stderr
    assert StoreBusy.__doc__
