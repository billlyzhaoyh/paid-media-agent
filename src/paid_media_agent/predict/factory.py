"""The configured predictor, always behind the cache and token guard."""

from __future__ import annotations

from paid_media_agent.config import Settings
from paid_media_agent.predict.budget import GuardedPredictor
from paid_media_agent.predict.local import LocalPredictor
from paid_media_agent.predict.protocol import Predictor
from paid_media_agent.predict.tabpfn import TabPFNPredictor
from paid_media_agent.store.db import Store


def build_predictor(settings: Settings, store: Store) -> Predictor | None:
    """None means the labelled day-over-day rule. A missing TabPFN token fails at call time."""
    if settings.paid_media_predictor == "none":
        return None
    inner: Predictor
    if settings.paid_media_predictor == "tabpfn":
        token = settings.tabpfn_token.get_secret_value() if settings.tabpfn_token else ""
        inner = TabPFNPredictor(token, base_url=settings.tabpfn_base_url)
    else:
        inner = LocalPredictor()
    return GuardedPredictor(
        inner,
        store,
        daily_tokens=settings.paid_media_tabpfn_daily_tokens,
        monthly_tokens=settings.paid_media_tabpfn_monthly_tokens,
    )
