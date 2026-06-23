"""history.py 测试：落盘 / 读取 / 节流 / 裁剪 / 损坏恢复。

history 依赖 ``astrbot.api``；环境不可用时整体跳过。
"""

import pytest

history = pytest.importorskip(
    "history", reason="history 依赖 astrbot.api，运行时环境不可用时跳过"
)

HistoryStore = history.HistoryStore


def test_records_and_reads(tmp_path):
    h = HistoryStore(str(tmp_path / "h.json"), min_interval=100)
    assert h.maybe_record(10, {"g1": 6, "g2": 5}, now=1000) is True
    assert h.aggregate_series() == [{"ts": 1000, "count": 10}]
    assert h.group_series("g1") == [{"ts": 1000, "count": 6}]


def test_throttle(tmp_path):
    h = HistoryStore(str(tmp_path / "h.json"), min_interval=100)
    assert h.maybe_record(10, {"g1": 6}, now=1000) is True
    assert h.maybe_record(12, {"g1": 7}, now=1050) is False  # 距上次 < 100s
    assert h.maybe_record(12, {"g1": 7}, now=1100) is True  # 刚好 100s
    assert [pt["count"] for pt in h.aggregate_series()] == [10, 12]


def test_min_interval_override(tmp_path):
    h = HistoryStore(str(tmp_path / "h.json"), min_interval=100)
    assert h.maybe_record(1, {"g1": 1}, now=1000) is True
    # 实例默认 100s 本会节流；显式传入更小的 min_interval 即放行（运行期调小间隔无需重启）
    assert h.maybe_record(2, {"g1": 2}, now=1050, min_interval=10) is True
    assert [pt["count"] for pt in h.aggregate_series()] == [1, 2]


def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "h.json")
    HistoryStore(p, min_interval=100).maybe_record(5, {"g1": 5}, now=1000)
    h2 = HistoryStore(p, min_interval=100)
    assert h2.aggregate_series() == [{"ts": 1000, "count": 5}]


def test_max_points_pruned(tmp_path):
    h = HistoryStore(str(tmp_path / "h.json"), min_interval=1, max_points=3)
    for i in range(5):
        h.maybe_record(i, {"g1": i}, now=1000 + i * 10)
    series = h.aggregate_series()
    assert len(series) == 3 and [pt["count"] for pt in series] == [2, 3, 4]


def test_corrupt_file_recovers(tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{ not json", encoding="utf-8")
    h = HistoryStore(str(p), min_interval=100)
    assert h.aggregate_series() == []  # 损坏 → 当空，不抛
    assert h.maybe_record(7, {"g1": 7}, now=2000) is True
    assert h.aggregate_series() == [{"ts": 2000, "count": 7}]


def test_group_series_only_includes_recorded(tmp_path):
    h = HistoryStore(str(tmp_path / "h.json"), min_interval=1)
    h.maybe_record(5, {"g1": 5}, now=1000)
    h.maybe_record(8, {"g1": 5, "g2": 3}, now=1010)
    assert h.group_series("g2") == [{"ts": 1010, "count": 3}]  # 仅第二个点含 g2
