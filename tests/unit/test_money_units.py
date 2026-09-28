"""Money units: proposals stay in account currency while providers read and write their own unit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from paid_media_agent.config import AccountBinding, AccountRegistry
from paid_media_agent.domain.common import JsonValue, Platform, RiskLevel
from paid_media_agent.domain.money import minor_exponent, to_currency, to_provider
from paid_media_agent.store import Store
from paid_media_agent.tools.catalog import (
    DEFAULT_LOCAL_POLICY,
    CatalogEntry,
    RawTool,
    StaticCatalogProvider,
    build_authorized_catalog,
)
from paid_media_agent.tools.providers import ProviderResult
from paid_media_agent.tools.write_policy import WriteOperation, WritePolicy
from paid_media_agent.tools.writes import (
    ApprovalPolicy,
    ApprovalSigner,
    ProposalService,
    WriteExecutor,
    WriteGate,
)


@pytest.mark.parametrize(
    ("value", "unit", "currency", "provider", "back"),
    [
        (57.5, "minor", "USD", 5750, Decimal("57.5")),
        (5000, "minor", "JPY", 5000, Decimal(5000)),
        (1.234, "minor", "KWD", 1234, Decimal("1.234")),
        (12.345, "micros", "USD", 12_350_000, Decimal("12.35")),
        ("40", "currency", "USD", 40.0, Decimal(40)),
    ],
)
def test_amounts_round_to_the_currency_then_scale(
    value: Any, unit: Any, currency: str, provider: int | float, back: Decimal
) -> None:
    sent = to_provider(value, unit, currency)
    assert sent == provider and type(sent) is type(provider)
    assert to_currency(sent, unit, currency) == back


def test_minor_exponents_follow_iso_4217() -> None:
    assert (minor_exponent("USD"), minor_exponent("jpy"), minor_exponent("BHD")) == (2, 0, 3)


def test_currency_operations_keep_their_policy_digest() -> None:
    plain = WriteOperation(
        tool_name="t",
        readback_tool="r",
        target_arg="campaign_id",
        editable_fields=("daily_budget",),
        readback_fields={"daily_budget": "daily_budget"},
        risk=RiskLevel.MEDIUM,
    )
    # The digest before provider units existed; proposals awaiting approval keep verifying.
    assert plain.digest() == "57b95f67bdedbf46"
    minor = plain.model_copy(update={"provider_units": {"daily_budget": "minor"}})
    assert minor.digest() != plain.digest()


class MinorUnitProvider:
    """A campaign whose provider holds its budget in cents, like Meta's Graph API."""

    def __init__(self) -> None:
        self.budget = 5000
        self.mutations: list[dict[str, JsonValue]] = []

    async def call_read(
        self, entry: CatalogEntry, arguments: dict[str, JsonValue]
    ) -> ProviderResult:
        return ProviderResult(payload={"campaign": {"id": "c-1", "daily_budget": str(self.budget)}})

    async def call_mutation(
        self, entry: CatalogEntry, arguments: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        self.mutations.append(dict(arguments))
        value = arguments["daily_budget"]
        assert isinstance(value, int)
        self.budget = value
        return {"id": "c-1", "success": True}


async def test_a_proposal_in_currency_writes_minor_units_and_verifies_them() -> None:
    tools = [
        RawTool(
            platform="meta_ads",
            name="update_campaign_budget",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "campaign_id": {"type": "string"},
                    "daily_budget": {"type": "integer"},
                },
            },
            annotations={"readOnlyHint": False},
        ),
        RawTool(
            platform="meta_ads",
            name="get_campaign_state",
            input_schema={
                "type": "object",
                "properties": {"account_id": {"type": "string"}, "campaign_id": {"type": "string"}},
            },
            annotations={"readOnlyHint": True},
        ),
    ]
    policy = DEFAULT_LOCAL_POLICY.model_copy(
        update={"admitted_mutations": ("meta_ads__update_campaign_budget",)}
    )
    catalog = StaticCatalogProvider(build_authorized_catalog(tools, policy=policy, source="test"))
    operation = WriteOperation(
        tool_name="meta_ads__update_campaign_budget",
        readback_tool="meta_ads__get_campaign_state",
        target_arg="campaign_id",
        editable_fields=("daily_budget",),
        readback_fields={"daily_budget": "daily_budget"},
        risk=RiskLevel.MEDIUM,
        units={"daily_budget": "account currency per day"},
        provider_units={"daily_budget": "minor"},
    )
    accounts = AccountRegistry(
        bindings=(
            AccountBinding(
                alias="shop",
                platform=Platform.META_ADS,
                provider_account_id="act_1",
                currency="USD",
                timezone="UTC",
            ),
        )
    )
    store = Store()
    repos = store.repositories
    signer = ApprovalSigner.ephemeral()
    provider = MinorUnitProvider()
    service = ProposalService(
        catalog_provider=catalog,
        accounts=accounts,
        write_policy=WritePolicy(operations=(operation,)),
        approval_policy=ApprovalPolicy(approver_refs=frozenset({"boss"})),
        signer=signer,
        proposals=repos.proposals,
        approvals=repos.approvals,
        read_provider=provider,
    )
    record = await service.propose(
        thread_id="t-1",
        requester_ref="analyst",
        account_alias="shop",
        tool_name="meta_ads__update_campaign_budget",
        target_ref="c-1",
        changes={"daily_budget": 57.555},
        reason="test",
    )
    cs = record.changeset
    assert [(v.field, v.value) for v in cs.before] == [("daily_budget", 50.0)]
    assert [(v.field, v.value) for v in cs.after] == [("daily_budget", 57.56)], "what will be held"
    assert cs.canonical_args["daily_budget"] == 5756, "the approved digest covers provider units"

    service.approve(cs.proposal_id, approver_ref="boss")
    executor = WriteExecutor(
        service=service,
        catalog_provider=catalog,
        write_policy=WritePolicy(operations=(operation,)),
        accounts=accounts,
        signer=signer,
        approvals=repos.approvals,
        receipts=repos.receipts,
        provider=provider,
        read_provider=provider,
        gate=WriteGate(writes_enabled=False, provider_is_fake=True),
    )
    receipt = await executor.execute(cs.proposal_id)
    assert receipt.status == "verified", receipt.reason
    assert provider.mutations == [
        {"account_id": "act_1", "campaign_id": "c-1", "daily_budget": 5756}
    ]
    assert [(v.field, v.value) for v in receipt.verified_state] == [("daily_budget", 57.56)]
