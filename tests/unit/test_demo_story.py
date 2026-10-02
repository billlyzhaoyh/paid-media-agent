"""The demo's recording keys, its recorder, and the page's script."""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources

import numpy as np
import pytest

from paid_media_agent.predict.protocol import Prediction, PredictionRequest, PredictorUnavailable
from paid_media_agent.testing.demo_story import _against
from paid_media_agent.testing.demo_visual import RecordedTabPFN, Recorder, target_key


def _request(purpose: str, targets: list[float], rows: int = 3) -> PredictionRequest:
    return PredictionRequest(
        purpose=purpose,
        columns=("a", "naive"),
        x_train=np.ones((len(targets), 2)),
        y_train=np.asarray(targets, dtype=np.float64),
        x_test=np.ones((rows, 2)),
        quantiles=(0.025, 0.5, 0.975),
    )


def _call(purpose: str, level: float, **extra: object) -> dict[str, object]:
    return {
        "model_version": "v-test",
        "purpose": purpose,
        "n_train": 2,
        "n_test": 3,
        "n_features": 2,
        "values": [[level] * 3, [level + 1] * 3, [level + 2] * 3],
        **extra,
    }


class _Live:
    name = "tabpfn"

    def __init__(self) -> None:
        self.asked = 0

    async def estimate_tokens(self, request: PredictionRequest) -> int:
        return 10_000

    async def predict(self, request: PredictionRequest) -> Prediction:
        self.asked += 1
        return Prediction(request.quantiles, np.full((3, request.n_test), 9.0), self.name, "live/1")


def test_the_target_key_is_whole_hundredths_and_only_for_the_checks() -> None:
    assert target_key(_request("anomaly:spend", [10.10, 20.25])) == 3035
    assert target_key(_request("anomaly:conversions", [3, 4])) == 700
    assert target_key(_request("bandit:global", [0.5, 0.7])) is None


async def test_recorded_answers_of_one_shape_are_told_apart_by_their_targets() -> None:
    week_one, week_two = (
        _request("anomaly:spend", [10.0, 20.0]),
        _request("anomaly:spend", [11.0, 21.0]),
    )
    recorded = RecordedTabPFN(
        [_call("anomaly:spend", 1.0, targets=3000), _call("anomaly:spend", 5.0, targets=3200)]
    )
    assert (await recorded.predict(week_one)).values[0][0] == 1.0
    assert (await recorded.predict(week_two)).values[0][0] == 5.0
    assert (await recorded.predict(week_one)).model_version.endswith("(recorded)")
    with pytest.raises(PredictorUnavailable):
        await recorded.predict(_request("anomaly:spend", [1.0, 2.0]))
    # An older entry with no key still answers its shape.
    assert (await RecordedTabPFN([_call("anomaly:spend", 7.0)]).predict(week_two)).values[0][
        0
    ] == 7.0
    assert await recorded.estimate_tokens(week_one) == 0


async def test_the_recorder_asks_live_only_for_what_the_recording_lacks() -> None:
    known, new = _request("anomaly:spend", [10.0, 20.0]), _request("anomaly:spend", [11.0, 21.0])
    live = _Live()
    recorder = Recorder(RecordedTabPFN([_call("anomaly:spend", 1.0)]), live)
    assert await recorder.estimate_tokens(known) == 0
    first = await recorder.predict(known)
    assert live.asked == 0 and recorder.asked_live == 0 and first.values[0][0] == 1.0

    alone = Recorder(None, live)
    assert await alone.estimate_tokens(new) == 10_000
    answer = await alone.predict(new)
    await alone.predict(new)  # the same answer is kept once
    assert alone.asked_live == 2 and answer.values[0][0] == 9.0
    assert alone.calls == [
        {
            "model_version": "live/1",
            "purpose": "anomaly:spend",
            "n_train": 2,
            "n_test": 3,
            "n_features": 2,
            "values": [[9.0] * 3] * 3,
            "targets": 3200,
        }
    ]
    assert recorder.calls[0]["targets"] == 3000, "a replayed entry gains its key"
    # What the recorder kept answers the same question on replay.
    assert (await RecordedTabPFN(alone.calls).predict(new)).values[0][0] == 9.0


def test_a_value_on_the_edge_of_its_range_is_written_with_a_decimal() -> None:
    assert _against(323.0, 261.2, 315.4, "spend") == "323 against 261 to 315"
    assert _against(293.6, 294.2, 354.0, "spend") == "293.6 against 294.2 to 354.0"
    assert _against(6.0, 12.96, 35.2, "conversions") == "6 against 13 to 35.2"


def test_the_page_script_parses() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = resources.files("paid_media_agent.testing").joinpath("templates", "demo.js")
    checked = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)  # noqa: S603
    assert checked.returncode == 0, checked.stderr
