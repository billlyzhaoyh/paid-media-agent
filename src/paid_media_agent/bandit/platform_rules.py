"""How each ad platform, and campaign type, handles budgets: rules as data, with their sources.

A rule is enforced only when it is verified on the platform's own documentation (`verified`);
anything else is carried as a note. Seeded from the research of 2026-09-29
(https://claude.ai/artifact/CLEaGDJVVydXFUXDoMBEhB).

- `max_step`: the largest budget change the platform advises in one move; the bandit takes the
  smaller of this and its own step limit.
- `min_days_between_changes`: the platform's spacing between budget changes.
- `min_daily_budget`: the smallest daily budget the platform accepts (account currency, USD-like).
- `min_budget_cpa_multiple`: the platform's advice that a budget should cover N conversions.
- `pacing_window`: the period over which the platform keeps average spend to the budget; a single
  day can overshoot (`overdelivery_day`) as long as the window holds.
- `spend_everything`: bid strategies built to spend the whole budget, so spending it is not by
  itself evidence that the budget is what binds.
- `target_types`: bid strategies that stop at a target or cap, where under-spending points to the
  target rather than to demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from paid_media_agent.domain.common import Platform

PacingWindow = Literal["day", "week", "month", "flight"]


@dataclass(frozen=True)
class PlatformRules:
    platform: Platform
    channel_type: str | None = None
    max_step: float | None = None
    min_days_between_changes: int | None = None
    min_daily_budget: float | None = None
    min_budget_cpa_multiple: float | None = None
    pacing_window: PacingWindow = "day"
    overdelivery_day: float | None = None
    """How far one day may exceed the daily budget, as a multiple (2.0 = up to 2x)."""
    window_cap: float | None = None
    """The window's cap as a multiple of the daily budget (30.4 for a Google month)."""
    spend_everything: frozenset[str] = field(default_factory=frozenset)
    target_types: frozenset[str] = field(default_factory=frozenset)
    source: str = ""
    verified: bool = False
    notes: tuple[str, ...] = ()

    def pacing_note(self) -> str | None:
        """One sentence on how this platform paces, for pacing readings."""
        if self.overdelivery_day is None:
            return None
        name = _NAMES.get(self.platform, self.platform.value)
        cap = f" and {self.window_cap:g}x in a {self.pacing_window}" if self.window_cap else ""
        sentence = (
            f"{name} can spend up to {self.overdelivery_day:g}x a daily budget on one day{cap}"
        )
        if self.pacing_window in ("week", "month"):
            sentence += f"; judge pacing over the {self.pacing_window}, not a single day"
        return sentence + "."


_NAMES = {
    Platform.GOOGLE_ADS: "Google Ads",
    Platform.META_ADS: "Meta",
    Platform.TIKTOK_ADS: "TikTok",
    Platform.LINKEDIN_ADS: "LinkedIn",
    Platform.REDDIT_ADS: "Reddit",
    Platform.PINTEREST_ADS: "Pinterest",
    Platform.SNAP_ADS: "Snapchat",
    Platform.X_ADS: "X",
}


_GOOGLE_SPEND_ALL = frozenset(
    {"MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "MAXIMIZE_CLICKS", "TARGET_SPEND"}
)
_GOOGLE_TARGETS = frozenset(
    {
        "TARGET_CPA",
        "TARGET_ROAS",
        "TARGET_IMPRESSION_SHARE",
        "MANUAL_CPC",
        "MANUAL_CPM",
        "MANUAL_CPV",
        "TARGET_CPM",
    }
)
_GOOGLE = PlatformRules(
    Platform.GOOGLE_ADS,
    pacing_window="month",
    overdelivery_day=2.0,
    window_cap=30.4,
    spend_everything=_GOOGLE_SPEND_ALL,
    target_types=_GOOGLE_TARGETS,
    source="https://support.google.com/google-ads/answer/2375423",
    verified=True,
)

_RULES: dict[tuple[Platform, str | None], PlatformRules] = {
    (Platform.GOOGLE_ADS, None): _GOOGLE,
    (Platform.GOOGLE_ADS, "DEMAND_GEN"): replace(
        _GOOGLE,
        channel_type="DEMAND_GEN",
        max_step=0.15,
        min_budget_cpa_multiple=10.0,
        source="https://support.google.com/google-ads/answer/16797388",
        notes=("Demand Gen: raise by at most 15% at a time and wait for about 50 conversions",),
    ),
    (Platform.GOOGLE_ADS, "MULTI_CHANNEL"): replace(
        _GOOGLE,
        channel_type="MULTI_CHANNEL",
        min_budget_cpa_multiple=10.0,
        source="https://support.google.com/google-ads/answer/6167162",
        notes=("App campaigns: budget at least 10x the target CPA; allow 7-14 days to settle",),
    ),
    (Platform.META_ADS, None): PlatformRules(
        Platform.META_ADS,
        pacing_window="week",
        overdelivery_day=1.75,
        window_cap=7.0,
        spend_everything=frozenset({"LOWEST_COST_WITHOUT_CAP"}),
        target_types=frozenset({"COST_CAP", "LOWEST_COST_BID_CAP", "LOWEST_COST_WITH_MIN_ROAS"}),
        source="https://www.facebook.com/business/help/190490051321426",
        verified=False,
        notes=(
            "Meta gives no size for a budget edit that restarts learning; the 20% rule is "
            "practitioner lore",
        ),
    ),
    (Platform.TIKTOK_ADS, None): PlatformRules(
        Platform.TIKTOK_ADS,
        max_step=0.30,
        min_days_between_changes=2,
        min_daily_budget=20.0,
        pacing_window="week",
        overdelivery_day=1.25,
        window_cap=7.0,
        spend_everything=frozenset({"MAX_DELIVERY", "BID_TYPE_NO_BID"}),
        target_types=frozenset({"COST_CAP", "BID_TYPE_CUSTOM", "BID_CAP", "MIN_ROAS"}),
        source="https://ads.tiktok.com/help/article/budget?lang=en",
        verified=True,
    ),
    (Platform.LINKEDIN_ADS, None): PlatformRules(
        Platform.LINKEDIN_ADS,
        pacing_window="week",
        overdelivery_day=2.0,
        window_cap=7.0,
        spend_everything=frozenset({"MAXIMUM_DELIVERY", "AUTOMATED"}),
        target_types=frozenset({"COST_CAP", "MANUAL", "TARGET_COST"}),
        source="https://www.linkedin.com/help/lms/answer/a422101",
        verified=True,
    ),
    (Platform.REDDIT_ADS, None): PlatformRules(
        Platform.REDDIT_ADS,
        overdelivery_day=1.2,
        spend_everything=frozenset({"LOWEST_COST", "AUTOMATIC"}),
        target_types=frozenset({"COST_CAP", "MANUAL_BIDDING", "MAXIMUM_BID"}),
        source="https://business.reddithelp.com/s/article/How-much-do-Reddit-Ads-cost",
        verified=False,
    ),
    (Platform.PINTEREST_ADS, None): PlatformRules(
        Platform.PINTEREST_ADS,
        pacing_window="week",
        overdelivery_day=1.25,
        spend_everything=frozenset({"AUTOMATIC_BID", "PERFORMANCE_PLUS"}),
        target_types=frozenset({"CUSTOM_BID", "MAX_BID", "TARGET_ROAS", "ROAS"}),
        source="https://help.pinterest.com/en/business/article/set-up-campaign-budgets",
        verified=True,
    ),
    (Platform.SNAP_ADS, None): PlatformRules(
        Platform.SNAP_ADS,
        min_daily_budget=5.0,
        spend_everything=frozenset({"AUTO_BID"}),
        target_types=frozenset({"LOWEST_COST_WITH_MAX_BID", "TARGET_COST"}),
        source="https://developers.snap.com/marketing-api/Ads-API/ad-squads",
        verified=True,
    ),
    (Platform.X_ADS, None): PlatformRules(
        Platform.X_ADS,
        pacing_window="flight",
        spend_everything=frozenset({"AUTO"}),
        target_types=frozenset({"MAX", "TARGET"}),
        source="https://docs.x.com/x-ads-api/campaign-management",
        verified=True,
    ),
}


def rules_for(platform: Platform | str, channel_type: str | None = None) -> PlatformRules:
    """The rules for a platform and campaign type, falling back to the platform's defaults."""
    key = Platform(platform)
    if channel_type is not None and (key, channel_type.upper()) in _RULES:
        return _RULES[(key, channel_type.upper())]
    return _RULES.get((key, None), PlatformRules(key))


def strategy_family(rules: PlatformRules, bid_strategy: str | None) -> str | None:
    """`spend_everything`, `target`, or None when the strategy is unknown."""
    if not bid_strategy:
        return None
    name = bid_strategy.upper()
    if name in rules.spend_everything:
        return "spend_everything"
    if name in rules.target_types:
        return "target"
    return None
