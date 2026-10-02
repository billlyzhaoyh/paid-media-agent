"""Anomaly backtest: the ±50% day-over-day rule versus predicted bands, on simulated ground truth.

Each seed simulates campaigns with labelled shocks (tracking outage, overspend, conversion surge)
and operator budget changes, which are not anomalies. Weekly checks then run over the final weeks
exactly as a live check would, reading the history as it stood on each check date. A campaign-day
counts as caught when any metric is flagged on it.

    uv run python tests/eval/anomaly_backtest.py                    # local band vs the rule
    uv run python tests/eval/anomaly_backtest.py --tabpfn --seeds 1 # also TabPFN (bills tokens)

`--tabpfn` sends simulated rows to Prior Labs and uses TABPFN_TOKEN; each check costs about
20,000 tokens (two calls). Not part of `pytest`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from paid_media_agent.analytics.anomalies import check_anomalies
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.sim.scenario import run_scenario
from paid_media_agent.sim.simulator import ScenarioParams
from paid_media_agent.store import Store

START = date(2026, 1, 5)


def scenario(seed: int, days: int) -> tuple[Store, ScenarioParams]:
    params = ScenarioParams(
        scenario_id=f"backtest-{seed}",
        seed=seed,
        n_campaigns=8,
        days=days,
        start=START,
        shock_rate=0.04,
        cold_starts=1,
    )
    store = Store()
    run_scenario(store, params)
    return store, params


async def evaluate(
    store: Store,
    params: ScenarioParams,
    predictor: Predictor | None,
    weeks: int,
    band: float = 0.95,
) -> dict[str, Any]:
    truth = {
        (ref, day): label
        for ref, day, label in store.fetch(
            "SELECT entity_ref, day, injected_anomaly FROM sim_truth"
        )
    }
    changes = {
        (ref, observed.date())
        for ref, observed in store.fetch(
            "SELECT entity_ref, occurred_at FROM change_events WHERE field = 'daily_budget'"
        )
    }
    flagged: set[tuple[str, date]] = set()
    checked: set[tuple[str, date]] = set()
    methods: Counter[str] = Counter()
    end = params.start + timedelta(days=params.days)
    for week in range(weeks, 0, -1):
        as_of = end - timedelta(days=7 * (week - 1))
        report = await check_anomalies(store, predictor, as_of=as_of, record=False, band=band)
        methods.update(report.methods.values())
        assert report.window_start is not None and report.window_end is not None
        day = report.window_start
        while day <= report.window_end:
            checked.update((ref, day) for ref, d in truth if d == day)
            day += timedelta(days=1)
        flagged.update((f.entity_ref, f.day) for f in report.flags)
    positives = {k for k in checked if truth.get(k)}
    caught = flagged & positives
    false_alarms = flagged - positives
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for key in positives:
        by_kind[str(truth[key])][1] += 1
        by_kind[str(truth[key])][0] += key in caught
    return {
        "methods": dict(methods),
        "checked": len(checked),
        "shocks": len(positives),
        "caught": len(caught),
        "false_alarms": len(false_alarms),
        "false_alarms_on_budget_change": sum(
            1
            for k in false_alarms
            if (k[0], k[1]) in changes or (k[0], k[1] - timedelta(1)) in changes
        ),
        "precision": len(caught) / max(len(flagged), 1),
        "recall": len(caught) / max(len(positives), 1),
        "by_kind": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_kind.items())},
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--days", type=int, default=150)
    parser.add_argument("--tabpfn", action="store_true")
    parser.add_argument("--band", type=float, default=0.95, help="Expected-range coverage.")
    parser.add_argument("--only-tabpfn", action="store_true", help="Skip the local methods.")
    args = parser.parse_args()
    methods: dict[str, Predictor | None] = {
        "rule (±50% day over day)": None,
        "local band": LocalPredictor(),
    }
    if args.tabpfn:
        from paid_media_agent.predict.budget import GuardedPredictor
        from paid_media_agent.predict.tabpfn import TabPFNPredictor

        token = os.environ.get("TABPFN_TOKEN")
        if not token:
            raise SystemExit("set TABPFN_TOKEN")
        methods["tabpfn band"] = TabPFNPredictor(token)
        if args.only_tabpfn:
            methods = {"tabpfn band": methods["tabpfn band"]}
    totals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for seed in range(1, args.seeds + 1):
        store, params = scenario(seed, args.days)
        for name, predictor in methods.items():
            if name == "tabpfn band":
                assert predictor is not None
                predictor = GuardedPredictor(
                    predictor, store, daily_tokens=2_000_000, monthly_tokens=5_000_000
                )
            result = await evaluate(store, params, predictor, args.weeks, args.band)
            totals[name].append(result)
            print(
                f"seed {seed}  {name:<26} P={result['precision']:.2f} R={result['recall']:.2f} "
                f"caught {result['caught']}/{result['shocks']}  false alarms "
                f"{result['false_alarms']} ({result['false_alarms_on_budget_change']} on budget "
                f"changes) of {result['checked']} checked  {result['by_kind']}  {result['methods']}"
            )
    print()
    for name, results in totals.items():
        n = len(results)
        precision = sum(r["precision"] for r in results) / n
        recall = sum(r["recall"] for r in results) / n
        false_alarms = sum(r["false_alarms"] for r in results) / n
        print(f"{name:<26} mean P={precision:.2f} R={recall:.2f} false alarms {false_alarms:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
