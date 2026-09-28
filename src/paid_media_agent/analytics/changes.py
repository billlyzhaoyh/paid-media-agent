"""The change log: every proposal decision and execution outcome, one row per changed field.

`ProposalService` and `WriteExecutor` report here after each saved decision. The receipt stays the
authority on what happened; this table is the history analysis joins against, so an outcome can be
attributed to the change that caused it and an override (rejection or edit) is visible as such.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation

from paid_media_agent.config import AccountRegistry
from paid_media_agent.domain.common import JsonValue
from paid_media_agent.domain.proposals import ProposalRecord, WriteReceipt
from paid_media_agent.store.db import Store, json_rows, naive_utc, utc_now

log = logging.getLogger(__name__)

_EVENT_SHAPE = {
    "event_id": "UUID",
    "field": "VARCHAR",
    "before_value": "JSON",
    "after_value": "JSON",
}


def _value(value: JsonValue) -> JsonValue:
    """Proposals keep amounts as decimal strings; the log stores them as numbers."""
    if isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation:
            return value
        return float(number) if number.is_finite() else value
    return value


class ChangeRecorder:
    """Implements `tools.writes.ChangeLog` on the state store. Failures are logged, not raised."""

    def __init__(
        self,
        store: Store,
        accounts: AccountRegistry,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._accounts = accounts
        self._clock = clock

    def proposal_event(self, record: ProposalRecord, status: str) -> None:
        self._record(record, status, self._clock())

    def receipt_event(self, record: ProposalRecord, receipt: WriteReceipt) -> None:
        self._record(record, receipt.status, naive_utc(receipt.checked_at))

    def _record(self, record: ProposalRecord, status: str, at: datetime) -> None:
        cs = record.changeset
        binding = self._accounts.resolve(cs.account_ref)
        before = {fv.field: fv.value for fv in cs.before}
        events = [
            {
                "event_id": str(uuid.uuid4()),
                "field": fv.field,
                # Raw values: the JSON-typed columns keep them as JSON, not as quoted text.
                "before_value": _value(before.get(fv.field)),
                "after_value": _value(fv.value),
            }
            for fv in cs.after
        ]
        if not events:
            return
        try:
            self._store.write(
                "INSERT INTO change_events (event_id, source, proposal_id, revision, platform, "  # noqa: S608
                "provider_account_id, account_alias, entity_type, entity_ref, tool_name, field, "
                "before_value, after_value, status, risk_flags, occurred_at) "
                "SELECT event_id, 'agent', ?, ?, ?, ?, ?, 'campaign', ?, ?, field, before_value, "
                f"after_value, ?, ?, ? FROM ({json_rows(_EVENT_SHAPE)})",
                [
                    cs.proposal_id,
                    cs.revision,
                    cs.platform.value,
                    binding.provider_account_id if binding else "",
                    cs.account_ref,
                    cs.target_ref,
                    cs.tool_name,
                    status,
                    list(cs.risk_flags),
                    at,
                    json.dumps(events),
                ],
            )
        except Exception:
            log.warning("change log write failed for proposal %s", cs.proposal_id, exc_info=True)
