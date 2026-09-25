"""DuckDB implementations of the repository protocols in `persistence/interfaces.py`."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import duckdb

from paid_media_agent.domain.proposals import ApprovalClaim, ProposalRecord, WriteReceipt
from paid_media_agent.store.db import Store, StoreConflict, naive_utc, utc_now


def _canonical(record: ProposalRecord) -> tuple[str, str]:
    """Stored JSON and its digest. The digest is the compare-and-swap token for the record."""
    text = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()


class DuckDBProposalRepository:
    def __init__(self, store: Store) -> None:
        self._store = store

    def save(self, record: ProposalRecord, *, expected: ProposalRecord | None = None) -> bool:
        proposal_id = record.changeset.proposal_id
        if expected is not None and expected.changeset.proposal_id != proposal_id:
            return False
        text, sha = _canonical(record)
        now = utc_now()
        try:
            if expected is None:
                rows = self._store.write(
                    "INSERT INTO proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING RETURNING proposal_id",
                    [
                        proposal_id,
                        record.routing_id,
                        record.changeset.thread_id,
                        record.state.value,
                        text,
                        sha,
                        now,
                        now,
                    ],
                )
            else:
                rows = self._store.write(
                    "UPDATE proposals SET routing_id = ?, thread_id = ?, state = ?, record = ?, "
                    "record_sha = ?, updated_at = ? "
                    "WHERE proposal_id = ? AND record_sha = ? RETURNING proposal_id",
                    [
                        record.routing_id,
                        record.changeset.thread_id,
                        record.state.value,
                        text,
                        sha,
                        now,
                        proposal_id,
                        _canonical(expected)[1],
                    ],
                )
        except (StoreConflict, duckdb.ConstraintException):
            return False
        return len(rows) == 1

    def get(self, proposal_id: UUID) -> ProposalRecord | None:
        rows = self._store.fetch(
            "SELECT record FROM proposals WHERE proposal_id = ?", [proposal_id]
        )
        return ProposalRecord.model_validate_json(rows[0][0]) if rows else None

    def get_by_routing_id(self, routing_id: str) -> ProposalRecord | None:
        rows = self._store.fetch("SELECT record FROM proposals WHERE routing_id = ?", [routing_id])
        return ProposalRecord.model_validate_json(rows[0][0]) if rows else None

    def list_for_thread(self, thread_id: str) -> list[ProposalRecord]:
        rows = self._store.fetch(
            "SELECT record FROM proposals WHERE thread_id = ? ORDER BY created_at, proposal_id",
            [thread_id],
        )
        return [ProposalRecord.model_validate_json(row[0]) for row in rows]


class DuckDBApprovalRepository:
    def __init__(self, store: Store) -> None:
        self._store = store

    def save(self, claim: ApprovalClaim) -> None:
        self._store.write(
            "INSERT INTO approvals (claim_id, proposal_id, revision, approved_at, claim) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (claim_id) DO UPDATE SET "
            "proposal_id = excluded.proposal_id, revision = excluded.revision, "
            "approved_at = excluded.approved_at, claim = excluded.claim",
            [
                claim.claim_id,
                claim.proposal_id,
                claim.revision,
                naive_utc(claim.approved_at),
                claim.model_dump_json(),
            ],
        )

    def latest_unused(self, proposal_id: UUID, revision: int) -> ApprovalClaim | None:
        rows = self._store.fetch(
            "SELECT claim FROM approvals WHERE proposal_id = ? AND revision = ? "
            "AND used_at IS NULL ORDER BY approved_at DESC LIMIT 1",
            [proposal_id, revision],
        )
        return ApprovalClaim.model_validate_json(rows[0][0]) if rows else None

    def mark_used(self, claim_id: UUID) -> bool:
        try:
            rows = self._store.write(
                "UPDATE approvals SET used_at = ? WHERE claim_id = ? AND used_at IS NULL "
                "RETURNING claim_id",
                [utc_now(), claim_id],
            )
        except StoreConflict:
            return False
        return len(rows) == 1


class DuckDBReceiptRepository:
    def __init__(self, store: Store) -> None:
        self._store = store

    def save(self, receipt: WriteReceipt) -> None:
        self._store.write(
            "INSERT INTO receipts VALUES (?, ?, ?) ON CONFLICT (proposal_id) DO UPDATE SET "
            "receipt = excluded.receipt, created_at = excluded.created_at",
            [receipt.proposal_id, receipt.model_dump_json(), utc_now()],
        )

    def get(self, proposal_id: UUID) -> WriteReceipt | None:
        rows = self._store.fetch(
            "SELECT receipt FROM receipts WHERE proposal_id = ?", [proposal_id]
        )
        return WriteReceipt.model_validate_json(rows[0][0]) if rows else None


class DuckDBDedupeStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    def seen(self, key: str) -> bool:
        try:
            rows = self._store.write(
                "INSERT INTO dedupe VALUES (?, ?) ON CONFLICT DO NOTHING RETURNING dedupe_key",
                [key, utc_now()],
            )
        except StoreConflict:
            return True
        return not rows


class DuckDBThreadOwnershipStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    def owner(self, thread_id: str) -> str | None:
        rows = self._store.fetch("SELECT owner_ref FROM threads WHERE thread_id = ?", [thread_id])
        return str(rows[0][0]) if rows else None

    def claim(self, thread_id: str, caller_ref: str) -> bool:
        try:
            self._store.write(
                "INSERT INTO threads VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [thread_id, caller_ref, utc_now()],
            )
        except StoreConflict:
            pass
        return self.owner(thread_id) == caller_ref


class OperationalRepositories:
    def __init__(self, store: Store) -> None:
        self.proposals = DuckDBProposalRepository(store)
        self.approvals = DuckDBApprovalRepository(store)
        self.receipts = DuckDBReceiptRepository(store)
        self.dedupe = DuckDBDedupeStore(store)
        self.threads = DuckDBThreadOwnershipStore(store)
