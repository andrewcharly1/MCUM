from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from MCUM.db.embedder import EMBEDDING_DIM
from MCUM.db import experience_store
from MCUM.policy import normalize_task_brief
from MCUM.sisl.autonomous_loop import _normalize_loop_timestamp


def test_normalize_task_brief_defaults_to_unconfirmed() -> None:
    brief = normalize_task_brief("C:/workspace", "validar hardening")
    assert brief["confirmed"] is False


def test_embedding_to_sql_validates_dimension_and_nan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experience_store, "_is_pgvector_enabled", lambda force_refresh=False: True)

    vector = [0.0] * EMBEDDING_DIM
    sql_value = experience_store._embedding_to_sql(vector)
    assert sql_value.startswith("[")
    assert sql_value.endswith("]")

    with pytest.raises(ValueError, match="exactly"):
        experience_store._embedding_to_sql(vector[:-1])

    bad = list(vector)
    bad[0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        experience_store._embedding_to_sql(bad)


def test_normalize_loop_timestamp_uses_local_timezone_for_naive(monkeypatch: pytest.MonkeyPatch) -> None:
    local_tz = timezone(timedelta(hours=-3))
    monkeypatch.setattr("MCUM.sisl.autonomous_loop._local_timezone", lambda: local_tz)

    naive_value = datetime(2026, 3, 14, 10, 0, 0)
    normalized = _normalize_loop_timestamp(naive_value)

    assert normalized == datetime(2026, 3, 14, 13, 0, 0, tzinfo=timezone.utc)


def test_normalize_loop_timestamp_returns_none_for_invalid_value() -> None:
    assert _normalize_loop_timestamp("not-a-timestamp") is None
