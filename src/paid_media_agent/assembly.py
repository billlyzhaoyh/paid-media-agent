"""One shared agent assembly. Runtime adapters compose these components; they never fork them."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from paid_media_agent.analytics.changes import ChangeRecorder
from paid_media_agent.analytics.goals import GoalStore, account_today
from paid_media_agent.analytics.ingest import AnalyticsRecorder
from paid_media_agent.bandit.live import live_config
from paid_media_agent.config import Settings
from paid_media_agent.harness.files import build_file_tools
from paid_media_agent.harness.loop import Agent, ApprovalGate
from paid_media_agent.harness.models import ChatModel, resolve_model
from paid_media_agent.harness.skills import discover_skills, skills_prompt
from paid_media_agent.harness.tools import ToolDispatcher, ToolSpec
from paid_media_agent.predict.factory import build_predictor
from paid_media_agent.runtime.profiles import RuntimeProfile
from paid_media_agent.store import Store
from paid_media_agent.store.conversations import ConversationStore
from paid_media_agent.tools.anomalies import CHECK_ANOMALIES_TOOL, build_check_anomalies_tool
from paid_media_agent.tools.bandit import RECOMMEND_BUDGETS_TOOL, build_recommend_budgets_tool
from paid_media_agent.tools.catalog import AuthorizedToolCatalog
from paid_media_agent.tools.compare_periods import COMPARE_PERIODS_TOOL, build_compare_periods_tool
from paid_media_agent.tools.discovery import (
    DISCOVER_TOOLS_TOOL,
    LIST_ACCOUNTS_TOOL,
    build_discover_tools_tool,
    build_list_accounts_tool,
)
from paid_media_agent.tools.history import QUERY_HISTORY_TOOL, build_query_history_tool
from paid_media_agent.tools.host_writes import HostOperation, goals_operation
from paid_media_agent.tools.pacing import CHECK_PACING_TOOL, build_check_pacing_tool
from paid_media_agent.tools.reads import ReadDispatcher, build_platform_read_tools
from paid_media_agent.tools.reports import RENDER_REPORT_TOOL, build_render_report_tool
from paid_media_agent.tools.summary import SUMMARIZE_WINDOW_TOOL, build_summarize_window_tool
from paid_media_agent.tools.write_tools import build_execute_gate, build_write_tools
from paid_media_agent.tools.writes import (
    DISCOVER_WRITE_OPERATIONS_TOOL,
    EXECUTE_CHANGE_TOOL,
    GET_PROPOSAL_TOOL,
    PROPOSE_CHANGE_TOOL,
    ProposalService,
    WriteExecutor,
)

FILESYSTEM_TOOLS: tuple[str, ...] = ("ls", "read_file", "write_file", "edit_file", "glob", "grep")
CORE_TOOLS: tuple[str, ...] = (
    LIST_ACCOUNTS_TOOL,
    DISCOVER_TOOLS_TOOL,
    COMPARE_PERIODS_TOOL,
    SUMMARIZE_WINDOW_TOOL,
    RENDER_REPORT_TOOL,
    QUERY_HISTORY_TOOL,
    CHECK_ANOMALIES_TOOL,
    RECOMMEND_BUDGETS_TOOL,
    CHECK_PACING_TOOL,
)
WRITE_TOOLS: tuple[str, ...] = (
    DISCOVER_WRITE_OPERATIONS_TOOL,
    PROPOSE_CHANGE_TOOL,
    EXECUTE_CHANGE_TOOL,
    GET_PROPOSAL_TOOL,
)
TOOL_SELECTION = "discover_tools"
"""Platform read tools are bound once discover_tools activates them for the thread."""


@dataclass(frozen=True)
class AssemblyMetadata:
    model_spec: str
    selection: str
    max_active_reads: int
    catalog_revision: str
    catalog_source: str
    read_tool_count: int
    mutation_entry_count: int
    denied_entry_count: int
    tool_names: tuple[str, ...]
    write_gate: str
    write_policy_issues: tuple[str, ...]


@dataclass(frozen=True)
class AgentComponents:
    model: ChatModel
    tools: tuple[ToolSpec, ...]
    dispatcher: ToolDispatcher
    gate: ApprovalGate
    system_prompt: str
    metadata: AssemblyMetadata
    proposal_service: ProposalService
    write_executor: WriteExecutor
    read_dispatcher: ReadDispatcher
    max_model_calls: int
    model_timeout_seconds: int


def host_operations(runtime: RuntimeProfile) -> dict[str, HostOperation]:
    """Changes to this deployment's own data that need approval: today, account goals."""
    goals = goals_operation(
        GoalStore(runtime.store), lambda alias: account_today(runtime.accounts, alias)
    )
    return {goals.tool_name: goals}


def _services(
    settings: Settings, runtime: RuntimeProfile
) -> tuple[ReadDispatcher, ProposalService, WriteExecutor]:
    dispatcher = ReadDispatcher(
        catalog_provider=runtime.catalog_provider,
        accounts=runtime.accounts,
        provider=runtime.read_provider,
        artifacts=runtime.artifacts,
        recorder=AnalyticsRecorder(runtime.store),
    )
    service = ProposalService(
        catalog_provider=runtime.catalog_provider,
        accounts=runtime.accounts,
        write_policy=runtime.write_policy,
        approval_policy=runtime.approval_policy,
        signer=runtime.signer,
        proposals=runtime.proposals,
        approvals=runtime.approvals,
        read_provider=runtime.read_provider,
        change_log=ChangeRecorder(runtime.store, runtime.accounts),
        host_operations=host_operations(runtime),
    )
    executor = WriteExecutor(
        service=service,
        catalog_provider=runtime.catalog_provider,
        write_policy=runtime.write_policy,
        accounts=runtime.accounts,
        signer=runtime.signer,
        approvals=runtime.approvals,
        receipts=runtime.receipts,
        provider=runtime.write_provider,
        read_provider=runtime.read_provider,
        gate=runtime.write_gate(settings),
    )
    return dispatcher, service, executor


def system_prompt_for(project_root: Path) -> str:
    """`instructions.md` plus an index of the runtime skills the model can read on demand."""
    instructions = project_root / "instructions.md"
    base = instructions.read_text(encoding="utf-8") if instructions.exists() else ""
    skills_dir = project_root / "skills"
    if not skills_dir.is_dir():
        skills_dir = project_root / "workspace" / "skills"
    index = skills_prompt(discover_skills(skills_dir))
    return "\n\n".join(part for part in (base.strip(), index) if part)


def build_agent_components(
    *,
    settings: Settings,
    runtime: RuntimeProfile,
    catalog: AuthorizedToolCatalog,
    model: ChatModel | None = None,
    system_prompt: str | None = None,
) -> AgentComponents:
    """Compose model, tools, dispatcher, and approval gate. No network, no global state."""
    model_config = settings.model_settings()
    resolved: ChatModel = model or resolve_model(
        model_config,
        api_key_env=settings.paid_media_model_api_key_env,
        timeout_seconds=settings.paid_media_model_timeout_seconds,
        zero_data_retention=settings.paid_media_model_zero_data_retention,
    )
    read_dispatcher, service, executor = _services(settings, runtime)
    project_root = runtime.skills_root or Path.cwd()
    goals = GoalStore(runtime.store)
    core = [
        build_list_accounts_tool(runtime.accounts, goals),
        build_discover_tools_tool(runtime.catalog_provider),
        build_compare_periods_tool(runtime.artifacts, goals.current),
        build_summarize_window_tool(runtime.artifacts, goals.current),
        build_render_report_tool(runtime.artifacts),
        build_query_history_tool(runtime.store, runtime.accounts),
        build_check_pacing_tool(runtime.store, runtime.accounts),
        build_check_anomalies_tool(
            runtime.store,
            runtime.accounts,
            build_predictor(settings, runtime.store),
            band=settings.paid_media_anomaly_band,
        ),
        build_recommend_budgets_tool(
            runtime.store,
            runtime.accounts,
            build_predictor(settings, runtime.store),
            config=live_config(settings.paid_media_bandit_policy),
        ),
    ]
    tools = (
        *core,
        *build_write_tools(service, executor),
        *build_file_tools(project_root),
        *build_platform_read_tools(catalog, read_dispatcher),
    )
    dispatcher = ToolDispatcher(
        tools={t.name: t for t in tools},
        catalog_provider=runtime.catalog_provider,
        artifacts=runtime.artifacts,
        secrets=tuple(s for s in (*_secret_values(settings), *runtime.extra_secrets) if s),
        offload_chars=settings.paid_media_result_offload_chars,
    )
    metadata = AssemblyMetadata(
        model_spec=model_config.spec,
        selection=TOOL_SELECTION,
        max_active_reads=settings.paid_media_max_selected_tools,
        catalog_revision=catalog.revision,
        catalog_source=catalog.source,
        read_tool_count=sum(1 for t in tools if t.kind == "read"),
        mutation_entry_count=len(catalog.mutation_entries()),
        denied_entry_count=len(catalog.denied_entries()),
        tool_names=tuple(t.name for t in tools),
        write_gate=executor.gate.describe(),
        write_policy_issues=tuple(
            f"{i.tool_name}: {i.reason}" for i in runtime.write_policy_issues
        ),
    )
    prompt = system_prompt if system_prompt is not None else system_prompt_for(project_root)
    return AgentComponents(
        model=resolved,
        tools=tools,
        dispatcher=dispatcher,
        gate=build_execute_gate(service),
        system_prompt=prompt,
        metadata=metadata,
        proposal_service=service,
        write_executor=executor,
        read_dispatcher=read_dispatcher,
        max_model_calls=settings.paid_media_max_model_calls,
        model_timeout_seconds=settings.paid_media_model_timeout_seconds,
    )


def build_agent(components: AgentComponents, store: Store) -> Agent:
    """The loop over these components, with conversations in the profile's state store."""
    return Agent(
        model=components.model,
        system_prompt=components.system_prompt,
        dispatcher=components.dispatcher,
        conversations=ConversationStore(store),
        gate=components.gate,
        max_model_calls=components.max_model_calls,
        model_timeout_seconds=components.model_timeout_seconds,
        max_active_reads=components.metadata.max_active_reads,
    )


def _secret_values(settings: Settings) -> tuple[str, ...]:
    values: list[str] = []
    for secret in (
        settings.pipeboard_api_token,
        settings.paid_media_approval_signing_key,
        settings.slack_bot_token,
        settings.slack_app_token,
        settings.slack_signing_secret,
        settings.paid_media_api_tokens,
        settings.tabpfn_token,
    ):
        if secret is not None:
            values.append(secret.get_secret_value())
    return tuple(values)
