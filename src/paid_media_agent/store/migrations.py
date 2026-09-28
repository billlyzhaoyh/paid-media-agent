"""Numbered schema migrations. Append new ones; never edit a migration that has shipped."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

OPERATIONAL = """
CREATE TABLE proposals (
    proposal_id UUID PRIMARY KEY,
    routing_id VARCHAR NOT NULL UNIQUE,
    thread_id VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    record JSON NOT NULL,
    record_sha VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE approvals (
    claim_id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    approved_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    claim JSON NOT NULL
);
CREATE TABLE receipts (
    proposal_id UUID PRIMARY KEY,
    receipt JSON NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE dedupe (
    dedupe_key VARCHAR PRIMARY KEY,
    seen_at TIMESTAMP NOT NULL
);
CREATE TABLE threads (
    thread_id VARCHAR PRIMARY KEY,
    owner_ref VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL
);
"""

CONVERSATIONS = """
CREATE TABLE messages (
    thread_id VARCHAR NOT NULL,
    seq INTEGER NOT NULL,
    role VARCHAR NOT NULL,
    content VARCHAR NOT NULL,
    tool_calls JSON,
    tool_call_id VARCHAR,
    tool_name VARCHAR,
    status VARCHAR,
    provider_state JSON,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (thread_id, seq)
);
CREATE TABLE pending_tool_calls (
    thread_id VARCHAR NOT NULL,
    tool_call_id VARCHAR NOT NULL,
    tool_name VARCHAR NOT NULL,
    args JSON NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (thread_id, tool_call_id)
);
CREATE TABLE thread_tools (
    thread_id VARCHAR PRIMARY KEY,
    activated JSON NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
"""

ENTITY_KEY = "platform, provider_account_id, entity_type, entity_ref"

ANALYTICS = f"""
CREATE TABLE pulls (
    pull_id UUID PRIMARY KEY,
    source VARCHAR NOT NULL,
    tool_name VARCHAR NOT NULL,
    catalog_revision VARCHAR,
    platform VARCHAR NOT NULL,
    provider_account_id VARCHAR NOT NULL,
    account_alias VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    requested_start DATE,
    requested_end DATE,
    actual_start DATE,
    actual_end DATE,
    data_complete_through DATE,
    artifact_id VARCHAR,
    row_count INTEGER NOT NULL,
    quality_flags VARCHAR[] NOT NULL,
    pulled_at TIMESTAMP NOT NULL,
    pulled_on DATE NOT NULL
);
CREATE TABLE entity_daily_snapshots (
    platform VARCHAR NOT NULL,
    provider_account_id VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_ref VARCHAR NOT NULL,
    day DATE NOT NULL,
    pull_id UUID NOT NULL,
    pulled_at TIMESTAMP NOT NULL,
    pulled_on DATE NOT NULL,
    account_alias VARCHAR NOT NULL,
    entity_name VARCHAR NOT NULL,
    currency VARCHAR NOT NULL,
    spend DECIMAL(18, 4) NOT NULL,
    impressions BIGINT,
    clicks BIGINT,
    conversions DECIMAL(18, 4),
    conversion_value DECIMAL(18, 4),
    is_complete BOOLEAN NOT NULL,
    quality_flags VARCHAR[] NOT NULL,
    PRIMARY KEY (platform, provider_account_id, entity_type, entity_ref, day, pull_id)
);
CREATE TABLE maturity_days (
    platform VARCHAR PRIMARY KEY,
    days INTEGER NOT NULL
);
INSERT INTO maturity_days VALUES
    ('google_ads', 7), ('meta_ads', 7), ('reddit_ads', 7), ('linkedin_ads', 14),
    ('x_ads', 7), ('openai_ads', 7);
CREATE TABLE entity_settings_snapshots (
    platform VARCHAR NOT NULL,
    provider_account_id VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_ref VARCHAR NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    pull_id UUID NOT NULL,
    account_alias VARCHAR NOT NULL,
    entity_name VARCHAR,
    status VARCHAR,
    daily_budget DECIMAL(18, 4),
    budget_type VARCHAR,
    bid_strategy VARCHAR,
    target_cpa DECIMAL(18, 4),
    target_roas DECIMAL(18, 6),
    currency VARCHAR,
    raw JSON NOT NULL,
    PRIMARY KEY (platform, provider_account_id, entity_type, entity_ref, observed_at)
);
CREATE TABLE change_events (
    event_id UUID PRIMARY KEY,
    source VARCHAR NOT NULL,
    proposal_id UUID,
    revision INTEGER,
    platform VARCHAR NOT NULL,
    provider_account_id VARCHAR NOT NULL,
    account_alias VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_ref VARCHAR NOT NULL,
    tool_name VARCHAR,
    field VARCHAR NOT NULL,
    before_value JSON,
    after_value JSON,
    status VARCHAR NOT NULL,
    risk_flags VARCHAR[] NOT NULL,
    occurred_at TIMESTAMP NOT NULL
);

-- The newest snapshot of every entity-day, whatever its age.
CREATE VIEW entity_daily_latest AS
SELECT * FROM entity_daily_snapshots
QUALIFY row_number() OVER (
    PARTITION BY {ENTITY_KEY}, day ORDER BY pulled_at DESC, pull_id DESC
) = 1;

-- The newest snapshot pulled at least the platform's maturity window after the day, so late
-- conversions have arrived. Days that have not matured yet are absent.
CREATE VIEW entity_daily_matured AS
SELECT s.*, s.pulled_on - s.day AS age_days
FROM entity_daily_snapshots s
LEFT JOIN maturity_days m ON m.platform = s.platform
WHERE s.is_complete AND s.pulled_on - s.day >= coalesce(m.days, 7)
QUALIFY row_number() OVER (
    PARTITION BY s.platform, s.provider_account_id, s.entity_type, s.entity_ref, s.day
    ORDER BY s.pulled_at DESC, s.pull_id DESC
) = 1;

-- Share of matured conversions already reported N days after the day: the empirical lag curve.
CREATE VIEW conversion_lag AS
WITH by_age AS (
    SELECT {ENTITY_KEY}, account_alias, day, pulled_on - day AS age_days, conversions
    FROM entity_daily_snapshots
    WHERE conversions IS NOT NULL
    QUALIFY row_number() OVER (
        PARTITION BY {ENTITY_KEY}, day, pulled_on - day ORDER BY pulled_at DESC, pull_id DESC
    ) = 1
)
SELECT
    b.platform, b.provider_account_id, any_value(b.account_alias) AS account_alias,
    b.entity_type, b.age_days,
    count(*) AS entity_days,
    sum(b.conversions) AS conversions_at_age,
    sum(m.conversions) AS conversions_matured,
    sum(b.conversions) / nullif(sum(m.conversions), 0) AS completeness
FROM by_age b
JOIN entity_daily_matured m USING ({ENTITY_KEY}, day)
WHERE b.age_days <= m.age_days
GROUP BY b.platform, b.provider_account_id, b.entity_type, b.age_days;

-- One row per settings version: consecutive identical observations collapse into one.
CREATE VIEW entity_settings_history AS
WITH marked AS (
    SELECT *,
        lag(struct_pack(status, daily_budget, budget_type, bid_strategy, target_cpa, target_roas))
            OVER (PARTITION BY {ENTITY_KEY} ORDER BY observed_at) AS previous
    FROM entity_settings_snapshots
), versions AS (
    SELECT * FROM marked
    WHERE previous IS NULL OR previous IS DISTINCT FROM
        struct_pack(status, daily_budget, budget_type, bid_strategy, target_cpa, target_roas)
)
SELECT
    {ENTITY_KEY}, account_alias, entity_name,
    observed_at AS valid_from,
    lead(observed_at) OVER (PARTITION BY {ENTITY_KEY} ORDER BY observed_at) AS valid_to,
    status, daily_budget, budget_type, bid_strategy, target_cpa, target_roas, currency
FROM versions;

-- Latest daily rows with the settings in force by the end of each day, pacing, and the matured
-- conversions when the day has matured.
CREATE VIEW entity_daily_panel AS
SELECT
    l.platform, l.provider_account_id, l.account_alias, l.entity_type, l.entity_ref,
    l.entity_name, l.day, l.currency, l.spend, l.impressions, l.clicks, l.conversions,
    l.conversion_value, l.is_complete, l.pulled_on - l.day AS age_days,
    m.conversions AS conversions_matured,
    m.conversion_value AS conversion_value_matured,
    m.day IS NOT NULL AS is_matured,
    h.status, h.daily_budget, h.bid_strategy, h.target_cpa, h.target_roas,
    l.spend / nullif(h.daily_budget, 0) AS pacing_ratio
FROM entity_daily_latest l
LEFT JOIN entity_daily_matured m USING ({ENTITY_KEY}, day)
ASOF LEFT JOIN entity_settings_history h
    ON h.platform = l.platform
    AND h.provider_account_id = l.provider_account_id
    AND h.entity_type = l.entity_type
    AND h.entity_ref = l.entity_ref
    AND CAST(l.day + 1 AS TIMESTAMP) > h.valid_from;
"""  # noqa: S608 - schema text built from constants

SIMULATION = """
CREATE TABLE sim_scenarios (
    scenario_id VARCHAR PRIMARY KEY,
    seed BIGINT NOT NULL,
    n_campaigns INTEGER NOT NULL,
    days INTEGER NOT NULL,
    start_day DATE NOT NULL,
    params JSON NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE sim_truth (
    scenario_id VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,
    entity_ref VARCHAR NOT NULL,
    day DATE NOT NULL,
    budget DOUBLE NOT NULL,
    spend DOUBLE NOT NULL,
    expected_conversions DOUBLE NOT NULL,
    conversions INTEGER NOT NULL,
    kappa1 DOUBLE NOT NULL,
    kappa2 DOUBLE NOT NULL,
    weekday_factor DOUBLE NOT NULL,
    injected_anomaly VARCHAR,
    PRIMARY KEY (scenario_id, entity_ref, day)
);
"""

JOBS = """
CREATE TABLE job_runs (
    run_id UUID PRIMARY KEY,
    job VARCHAR NOT NULL,
    trigger VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    detail JSON
);
"""

PREDICTIONS = """
CREATE TABLE predictor_calls (
    call_id UUID PRIMARY KEY,
    provider VARCHAR NOT NULL,
    model_version VARCHAR,
    purpose VARCHAR NOT NULL,
    request_sha VARCHAR NOT NULL,
    n_train INTEGER NOT NULL,
    n_test INTEGER NOT NULL,
    n_features INTEGER NOT NULL,
    tokens_estimated BIGINT NOT NULL,
    latency_ms INTEGER,
    status VARCHAR NOT NULL,
    error VARCHAR,
    result JSON,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE anomaly_checks (
    check_id UUID PRIMARY KEY,
    account_alias VARCHAR,
    window_start DATE,
    window_end DATE,
    as_of DATE NOT NULL,
    predictor VARCHAR NOT NULL,
    methods JSON NOT NULL,
    rows_checked INTEGER NOT NULL,
    flag_count INTEGER NOT NULL,
    notes JSON NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE anomaly_flags (
    flag_id UUID PRIMARY KEY,
    check_id UUID NOT NULL,
    platform VARCHAR NOT NULL,
    provider_account_id VARCHAR NOT NULL,
    account_alias VARCHAR NOT NULL,
    entity_ref VARCHAR NOT NULL,
    entity_name VARCHAR,
    day DATE NOT NULL,
    metric VARCHAR NOT NULL,
    observed DOUBLE NOT NULL,
    expected DOUBLE,
    lo DOUBLE,
    hi DOUBLE,
    direction VARCHAR NOT NULL,
    score DOUBLE NOT NULL,
    method VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL
);
"""

BANDIT = """
CREATE TABLE bandit_runs (
    run_id UUID PRIMARY KEY,
    mode VARCHAR NOT NULL,
    scenario_id VARCHAR,
    account_alias VARCHAR,
    decision_day DATE NOT NULL,
    policy VARCHAR NOT NULL,
    policy_version VARCHAR NOT NULL,
    objective VARCHAR NOT NULL,
    total_budget DOUBLE NOT NULL,
    currency VARCHAR,
    prior_source VARCHAR NOT NULL,
    config JSON NOT NULL,
    seed BIGINT,
    data_checks JSON NOT NULL,
    fallback_used BOOLEAN NOT NULL,
    notes JSON NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE bandit_decisions (
    run_id UUID NOT NULL,
    arm_key VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,
    provider_account_id VARCHAR NOT NULL,
    account_alias VARCHAR NOT NULL,
    entity_ref VARCHAR NOT NULL,
    entity_name VARCHAR,
    eligible BOOLEAN NOT NULL,
    ineligible_reason VARCHAR,
    current_budget DOUBLE,
    pacing_ratio DOUBLE,
    spend_unit DOUBLE,
    n_history INTEGER NOT NULL,
    n_pseudo INTEGER NOT NULL,
    prior_precision_kappa2 DOUBLE,
    post_mean JSON,
    post_cov JSON,
    sampled_kappa1 DOUBLE,
    sampled_kappa2 DOUBLE,
    rejected_draws INTEGER,
    budget_thompson DOUBLE,
    budget_greedy DOUBLE,
    final_budget DOUBLE,
    lower_bound DOUBLE,
    upper_bound DOUBLE,
    constrained_by VARCHAR[] NOT NULL,
    expected_conversions DOUBLE,
    propensity DOUBLE,
    proposal_id UUID,
    PRIMARY KEY (run_id, arm_key)
);
"""

BANDIT_OUTCOMES = """
-- What happened after each budget decision, over its hold window [decision_day, +hold_days):
-- the budget actually in force at the end of the window, spend, and matured conversions only.
-- outcome: overridden (proposal rejected, or a different budget in force), superseded (a newer
-- decision for the campaign within the window), pending (not every day has matured), followed.
CREATE VIEW bandit_outcomes AS
WITH decided AS (
    SELECT r.run_id, r.mode, r.decision_day, r.policy,
        CAST(json_extract(r.config, '$.hold_days') AS INTEGER) AS hold_days,
        d.arm_key, d.platform, d.provider_account_id, d.account_alias, d.entity_ref,
        d.entity_name, d.current_budget, d.final_budget, d.expected_conversions,
        d.propensity, d.proposal_id,
        lead(r.decision_day) OVER (PARTITION BY d.arm_key ORDER BY r.decision_day, r.created_at)
            AS next_decision_day
    FROM bandit_decisions d JOIN bandit_runs r USING (run_id)
    WHERE d.eligible AND d.final_budget IS NOT NULL
), windowed AS (
    SELECT x.run_id, x.arm_key,
        count(p.day) AS days_reported,
        count(p.day) FILTER (WHERE p.is_matured) AS days_matured,
        arg_max(p.daily_budget::DOUBLE, p.day) AS budget_in_force,
        sum(p.spend)::DOUBLE AS spend,
        sum(p.conversions_matured) FILTER (WHERE p.is_matured)::DOUBLE AS conversions_matured
    FROM decided x
    LEFT JOIN entity_daily_panel p
        ON p.platform = x.platform
        AND p.provider_account_id = x.provider_account_id
        AND p.entity_type = 'campaign'
        AND p.entity_ref = x.entity_ref
        AND p.day >= x.decision_day
        AND p.day < x.decision_day + x.hold_days
    GROUP BY x.run_id, x.arm_key
), proposal AS (
    SELECT proposal_id, arg_max(status, occurred_at) AS proposal_status
    FROM change_events
    WHERE source = 'agent' AND proposal_id IS NOT NULL
    GROUP BY proposal_id
)
SELECT x.run_id, x.mode, x.decision_day, x.policy, x.hold_days, x.arm_key, x.platform,
    x.provider_account_id, x.account_alias, x.entity_ref, x.entity_name, x.current_budget,
    x.final_budget, x.expected_conversions * x.hold_days AS expected_conversions_window,
    x.propensity, x.proposal_id, pr.proposal_status,
    w.budget_in_force, w.days_reported, w.days_matured, w.spend, w.conversions_matured,
    CASE
        WHEN pr.proposal_status = 'rejected' THEN 'overridden'
        WHEN x.next_decision_day < x.decision_day + x.hold_days THEN 'superseded'
        WHEN w.days_matured < x.hold_days THEN 'pending'
        WHEN w.budget_in_force IS NULL
            OR abs(w.budget_in_force / x.final_budget - 1) > 0.02 THEN 'overridden'
        ELSE 'followed'
    END AS outcome
FROM decided x
JOIN windowed w USING (run_id, arm_key)
LEFT JOIN proposal pr ON pr.proposal_id = x.proposal_id;
"""

MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("0001_operational", OPERATIONAL),
    ("0002_conversations", CONVERSATIONS),
    ("0003_analytics", ANALYTICS),
    ("0004_simulation", SIMULATION),
    ("0005_jobs", JOBS),
    ("0006_predictions", PREDICTIONS),
    ("0007_bandit", BANDIT),
    ("0008_bandit_outcomes", BANDIT_OUTCOMES),
)


def apply_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version VARCHAR PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                [version, datetime.now(UTC).replace(tzinfo=None)],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
