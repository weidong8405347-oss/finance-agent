"""RunManifest 契约：模式冻结 + eval 必须带 as_of + 截止日分区。"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from finance_agent.harness.manifest import CutoffZone, RunManifest, RunMode


def test_eval_mode_requires_as_of():
    with pytest.raises(ValidationError):
        RunManifest(run_id="r1", mode=RunMode.EVAL)


def test_live_mode_rejects_eval_as_of():
    with pytest.raises(ValidationError):
        RunManifest(run_id="r1", mode=RunMode.LIVE, eval_as_of=datetime(2024, 1, 1, tzinfo=UTC))


def test_manifest_is_frozen_after_creation():
    m = RunManifest(run_id="r1", mode=RunMode.LIVE)
    with pytest.raises(ValidationError):
        m.mode = RunMode.EVAL  # type: ignore[misc]


def test_eval_manifest_ok():
    m = RunManifest(
        run_id="r1",
        mode=RunMode.EVAL,
        eval_as_of=datetime(2023, 6, 30, tzinfo=UTC),
        backbone_model="gpt-x",
        backbone_cutoff=date(2023, 10, 1),
    )
    assert m.mode is RunMode.EVAL


def test_cutoff_zone_classification():
    m = RunManifest(run_id="r1", mode=RunMode.LIVE, backbone_cutoff=date(2023, 10, 1))
    # cutoff 及之前 = 污染区；之后 = 诚实区
    assert m.cutoff_zone(datetime(2023, 9, 30, tzinfo=UTC)) is CutoffZone.CONTAMINATED
    assert m.cutoff_zone(datetime(2023, 10, 1, tzinfo=UTC)) is CutoffZone.CONTAMINATED
    assert m.cutoff_zone(datetime(2024, 1, 1, tzinfo=UTC)) is CutoffZone.HONEST


def test_cutoff_zone_unknown_without_backbone_cutoff():
    m = RunManifest(run_id="r1", mode=RunMode.LIVE)
    assert m.cutoff_zone(datetime(2024, 1, 1, tzinfo=UTC)) is CutoffZone.UNKNOWN
