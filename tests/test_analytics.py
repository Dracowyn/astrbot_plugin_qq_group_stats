"""analytics.py 纯逻辑测试：去重 / 重复率口径 / 在群分布 / 跨群 Top / 两两重叠。"""

from dataclasses import dataclass, field

import pytest

import analytics


# --------------------------- 测试替身（鸭子类型 _Snapshot） ---------------------------
@dataclass
class FakeMember:
    user_id: int
    nickname: str = ""
    card: str = ""
    role: str = "member"
    join_time: int = 0
    last_sent_time: int = 0


@dataclass
class FakeSnap:
    group_id: str
    members: list = field(default_factory=list)
    error: str | None = None
    group_name: str = ""
    fetched_at: int = 0
    bound: bool = True

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def user_ids(self) -> set[int]:
        return {m.user_id for m in self.members}


def snap(gid: str, uids, name="", error=None, bound=True) -> FakeSnap:
    return FakeSnap(
        group_id=gid,
        members=[FakeMember(user_id=u, nickname=f"n{u}", card=f"c{u}") for u in uids],
        group_name=name,
        error=error,
        bound=bound,
    )


@pytest.fixture
def overlap_snaps():
    # g1: {1,2,3}  g2: {2,3,4}  g3: {3,4,5}
    return [snap("g1", [1, 2, 3]), snap("g2", [2, 3, 4]), snap("g3", [3, 4, 5])]


# --------------------------- compute_overview ---------------------------
class TestOverview:
    def test_core_counts(self, overlap_snaps):
        ov = analytics.compute_overview(overlap_snaps)
        assert ov["unique_members"] == 5  # {1,2,3,4,5}
        assert ov["total_occurrences"] == 9  # 3+3+3
        assert ov["duplicated_members"] == 3  # 用户 2,3,4 在 >=2 个群

    def test_repeat_rates(self, overlap_snaps):
        ov = analytics.compute_overview(overlap_snaps)
        assert ov["member_repeat_rate"] == pytest.approx(0.6)  # 3/5
        assert ov["seat_repeat_rate"] == pytest.approx(0.4444, abs=1e-4)  # (9-5)/9
        assert ov["avg_groups_per_member"] == pytest.approx(1.8)  # 9/5

    def test_distribution(self, overlap_snaps):
        ov = analytics.compute_overview(overlap_snaps)
        # 在 1 群: {1,5}=2; 在 2 群: {2,4}=2; 在 3 群: {3}=1
        assert ov["distribution"] == [
            {"groups": 1, "members": 2},
            {"groups": 2, "members": 2},
            {"groups": 3, "members": 1},
        ]

    def test_groups_sorted_desc_and_share(self, overlap_snaps):
        ov = analytics.compute_overview(overlap_snaps)
        assert [g["group_id"] for g in ov["groups"]] == ["g1", "g2", "g3"]
        # 每群 3 人 / 总 9 人次 = 0.3333
        assert ov["groups"][0]["share"] == pytest.approx(0.3333, abs=1e-4)
        assert ov["groups"][0]["rel_bar"] == pytest.approx(1.0)

    def test_failed_group_excluded_from_math_but_listed(self):
        snaps = [snap("g1", [1, 2, 3]), snap("g2", [], error="timeout")]
        ov = analytics.compute_overview(snaps)
        assert ov["unique_members"] == 3
        assert ov["total_occurrences"] == 3
        assert ov["successful_group_count"] == 1
        assert ov["failed_group_count"] == 1
        # 失败群仍出现在列表，排在最后，并带 error
        last = ov["groups"][-1]
        assert last["group_id"] == "g2" and last["error"] == "timeout"

    def test_bound_unbound_counts_and_flags(self):
        snaps = [snap("g1", [1, 2], bound=True), snap("g2", [2, 3], bound=False)]
        ov = analytics.compute_overview(snaps)
        assert ov["total_group_count"] == 2
        assert ov["bound_group_count"] == 1
        assert ov["unbound_group_count"] == 1
        by = {g["group_id"]: g for g in ov["groups"]}
        assert by["g1"]["bound"] is True and by["g2"]["bound"] is False
        # 去重/重复率跨绑定+未绑定一起算：unique {1,2,3}=3
        assert ov["unique_members"] == 3

    def test_empty_no_zero_division(self):
        ov = analytics.compute_overview([])
        assert ov["unique_members"] == 0
        assert ov["member_repeat_rate"] == 0.0
        assert ov["seat_repeat_rate"] == 0.0
        assert ov["avg_groups_per_member"] == 0.0
        assert ov["distribution"] == []

    def test_no_overlap_rates_zero(self):
        snaps = [snap("g1", [1, 2]), snap("g2", [3, 4])]
        ov = analytics.compute_overview(snaps)
        assert ov["unique_members"] == 4
        assert ov["duplicated_members"] == 0
        assert ov["member_repeat_rate"] == 0.0
        assert ov["seat_repeat_rate"] == 0.0


# --------------------------- compute_top_overlap ---------------------------
class TestTopOverlap:
    def test_only_multi_group_users_sorted(self, overlap_snaps):
        rows = analytics.compute_top_overlap(overlap_snaps)
        # 仅 >=2 群：user3(3), user2(2), user4(2)；按群数降序、uid 升序
        assert [(r["user_id"], r["group_count"]) for r in rows] == [
            (3, 3),
            (2, 2),
            (4, 2),
        ]

    def test_display_prefers_card(self, overlap_snaps):
        rows = analytics.compute_top_overlap(overlap_snaps)
        top = rows[0]
        assert top["user_id"] == 3 and top["display"] == "c3"
        assert sorted(top["groups"]) == ["g1", "g2", "g3"]

    def test_limit(self, overlap_snaps):
        rows = analytics.compute_top_overlap(overlap_snaps, limit=1)
        assert len(rows) == 1 and rows[0]["user_id"] == 3

    def test_no_overlap_empty(self):
        rows = analytics.compute_top_overlap([snap("g1", [1]), snap("g2", [2])])
        assert rows == []


# --------------------------- compute_pairwise_overlap ---------------------------
class TestPairwise:
    def test_pairs_and_ratio(self, overlap_snaps):
        pw = analytics.compute_pairwise_overlap(overlap_snaps)
        assert pw["available"] is True
        pairs = {(p["group_a"], p["group_b"]): p for p in pw["pairs"]}
        assert pairs[("g1", "g2")]["shared"] == 2  # {2,3}
        assert pairs[("g1", "g3")]["shared"] == 1  # {3}
        assert pairs[("g2", "g3")]["shared"] == 2  # {3,4}
        assert pairs[("g1", "g2")]["ratio"] == pytest.approx(0.6667, abs=1e-4)

    def test_sorted_by_shared_desc(self, overlap_snaps):
        pw = analytics.compute_pairwise_overlap(overlap_snaps)
        shared = [p["shared"] for p in pw["pairs"]]
        assert shared == sorted(shared, reverse=True)

    def test_skips_zero_overlap_pairs(self):
        pw = analytics.compute_pairwise_overlap([snap("g1", [1]), snap("g2", [2])])
        assert pw["available"] is True and pw["pairs"] == []

    def test_too_many_groups_skipped(self, overlap_snaps):
        pw = analytics.compute_pairwise_overlap(overlap_snaps, max_groups=2)
        assert pw["available"] is False and pw["reason"] == "too_many_groups"


# --------------------------- compute_growth_series ---------------------------
from datetime import datetime, timezone  # noqa: E402


def _ts(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def _snap_jt(gid, members, error=None, bound=True):
    """members: [(user_id, join_time), ...]"""
    return FakeSnap(
        group_id=gid,
        members=[FakeMember(user_id=u, join_time=jt) for u, jt in members],
        error=error,
        bound=bound,
    )


class TestGrowth:
    def test_single_group_monthly_cumulative(self):
        s = _snap_jt("g1", [(1, _ts(2025, 1, 5)), (2, _ts(2025, 1, 20)), (3, _ts(2025, 3, 2))])
        r = analytics.compute_growth_series([s], group_id="g1", bucket="month")
        assert [p["count"] for p in r["points"]] == [2, 3]  # 2025-01 累计2，2025-03 累计3
        assert r["unknown_join"] == 0 and r["total"] == 3 and r["bucket"] == "month"

    def test_unknown_join_excluded(self):
        s = _snap_jt("g1", [(1, _ts(2025, 1, 5)), (2, 0)])
        r = analytics.compute_growth_series([s], group_id="g1")
        assert r["unknown_join"] == 1 and r["total"] == 2
        assert [p["count"] for p in r["points"]] == [1]

    def test_aggregate_dedup_uses_earliest(self):
        # 用户 1 在 g1/g2 各有记录，取最早（2024-06）；用户 2 仅 g1（2025-01）
        s1 = _snap_jt("g1", [(1, _ts(2024, 6, 1)), (2, _ts(2025, 1, 1))])
        s2 = _snap_jt("g2", [(1, _ts(2025, 2, 1))], bound=False)
        r = analytics.compute_growth_series([s1, s2], group_id=None, bucket="month")
        assert r["total"] == 2  # 去重 {1,2}
        assert [p["count"] for p in r["points"]] == [1, 2]

    def test_failed_group_excluded(self):
        s = _snap_jt("g1", [(1, _ts(2025, 1, 1))], error="boom")
        r = analytics.compute_growth_series([s])
        assert r["points"] == [] and r["total"] == 0

    def test_day_bucket(self):
        s = _snap_jt("g1", [(1, _ts(2025, 1, 1)), (2, _ts(2025, 1, 2))])
        r = analytics.compute_growth_series([s], group_id="g1", bucket="day")
        assert [p["count"] for p in r["points"]] == [1, 2]  # 两天两个桶

    def test_unknown_bucket_falls_back_to_month(self):
        s = _snap_jt("g1", [(1, _ts(2025, 1, 1))])
        r = analytics.compute_growth_series([s], group_id="g1", bucket="bogus")
        assert r["bucket"] == "month"

    def test_year_bucket(self):
        s = _snap_jt("g1", [(1, _ts(2023, 3, 1)), (2, _ts(2023, 11, 1)), (3, _ts(2025, 2, 1))])
        r = analytics.compute_growth_series([s], group_id="g1", bucket="year")
        # 2023 两人累计 2，2025 累计 3（中间无 2024 桶）
        assert [p["count"] for p in r["points"]] == [2, 3]
        assert r["bucket"] == "year"
        assert r["points"][0]["ts"] == _ts(2023, 1, 1)

    def test_missing_group_returns_empty(self):
        s = _snap_jt("g1", [(1, _ts(2025, 1, 1))])
        r = analytics.compute_growth_series([s], group_id="nope")
        assert r["points"] == [] and r["total"] == 0


class TestGrowthMulti:
    def test_total_dedup_plus_per_group_lines(self):
        # 用户 1 在两群（最早 2024-06），用户 2 仅 g1（2025-01），用户 3 仅 g2（2025-02）
        s1 = _snap_jt("g1", [(1, _ts(2024, 6, 1)), (2, _ts(2025, 1, 1))], )
        s1.group_name = "群一"
        s2 = _snap_jt("g2", [(1, _ts(2025, 2, 1)), (3, _ts(2025, 3, 5))])
        s2.group_name = "群二"
        r = analytics.compute_growth_multi([s1, s2], bucket="month")
        assert r["total"]["total"] == 3  # 去重 {1,2,3}
        assert [p["count"] for p in r["total"]["points"]] == [1, 2, 3]
        # 各群一条线，按当前人数降序（两群都 2 人，稳定保序）
        names = {g["group_id"]: g for g in r["groups"]}
        assert names["g1"]["group_name"] == "群一"
        assert [p["count"] for p in names["g1"]["points"]] == [1, 2]
        assert [p["count"] for p in names["g2"]["points"]] == [1, 2]
        assert r["omitted"] == 0 and r["bucket"] == "month"

    def test_groups_sorted_by_member_count_desc(self):
        big = _snap_jt("big", [(i, _ts(2025, 1, 1)) for i in range(1, 6)])  # 5 人
        small = _snap_jt("small", [(9, _ts(2025, 1, 1))])  # 1 人
        r = analytics.compute_growth_multi([small, big])
        assert [g["group_id"] for g in r["groups"]] == ["big", "small"]

    def test_max_groups_caps_lines_and_counts_omitted(self):
        snaps = [_snap_jt(f"g{i}", [(i, _ts(2025, 1, 1))]) for i in range(5)]
        r = analytics.compute_growth_multi(snaps, max_groups=3)
        assert len(r["groups"]) == 3 and r["omitted"] == 2
        # total 仍覆盖全部群去重，不受 max_groups 影响
        assert r["total"]["total"] == 5

    def test_failed_group_excluded(self):
        ok = _snap_jt("g1", [(1, _ts(2025, 1, 1))])
        bad = _snap_jt("g2", [(2, _ts(2025, 1, 1))], error="boom")
        r = analytics.compute_growth_multi([ok, bad])
        assert [g["group_id"] for g in r["groups"]] == ["g1"]
        assert r["total"]["total"] == 1


# --------------------------- compute_activity ---------------------------
def _snap_act(gid, members, error=None, bound=True, name=""):
    """members: [(user_id, last_sent_time), ...]"""
    return FakeSnap(
        group_id=gid,
        members=[FakeMember(user_id=u, last_sent_time=ls) for u, ls in members],
        error=error,
        bound=bound,
        group_name=name,
    )


_NOW = _ts(2025, 6, 1)  # 基准“现在”，各用例据此判活跃


class TestActivity:
    def test_buckets_active_semi_idle_unknown(self):
        s = _snap_act("g1", [
            (1, _ts(2025, 5, 28)),  # 4 天前 → active
            (2, _ts(2025, 5, 10)),  # 22 天前 → semi
            (3, _ts(2025, 1, 1)),   # 很久 → idle
            (4, 0),                 # 无记录 → unknown
        ])
        r = analytics.compute_activity([s], group_id="g1", now=_NOW)
        assert r["distribution"] == {"active": 1, "semi": 1, "idle": 1, "unknown": 1}
        assert r["total"] == 4 and r["known"] == 3
        assert r["active_rate"] == 0.25  # 1 active / 4 total

    def test_all_dedup_uses_most_recent_speak(self):
        # 用户 1 在 g1 很久没发言，但在 g2 最近发言 → 全部群口径应算 active
        s1 = _snap_act("g1", [(1, _ts(2025, 1, 1))])
        s2 = _snap_act("g2", [(1, _ts(2025, 5, 30)), (2, _ts(2025, 5, 30))], bound=False)
        r = analytics.compute_activity([s1, s2], group_id=None, now=_NOW)
        assert r["total"] == 2  # 去重 {1,2}
        assert r["distribution"]["active"] == 2 and r["distribution"]["idle"] == 0

    def test_idle_top_sorted_excludes_unknown(self):
        s = _snap_act("g1", [
            (1, _ts(2025, 5, 1)),
            (2, _ts(2024, 12, 1)),
            (3, 0),  # unknown 不进 idle_top
        ])
        r = analytics.compute_activity([s], group_id="g1", now=_NOW)
        ids = [m["user_id"] for m in r["idle_top"]]
        assert ids == [2, 1]  # 最久未发言在前
        assert all(m["last_sent_time"] > 0 for m in r["idle_top"])
        assert r["idle_top"][0]["silent_days"] >= r["idle_top"][1]["silent_days"]

    def test_idle_limit(self):
        s = _snap_act("g1", [(i, _ts(2025, 1, i)) for i in range(1, 10)])
        r = analytics.compute_activity([s], group_id="g1", now=_NOW, idle_limit=3)
        assert len(r["idle_top"]) == 3

    def test_group_rates_scoped_to_selected_group(self):
        # 选单群时，各群活跃率只应给该群一行（与分布/活跃率同口径）
        a = _snap_act("g1", [(1, _ts(2025, 5, 30))])
        b = _snap_act("g2", [(2, _ts(2025, 5, 30))])
        r = analytics.compute_activity([a, b], group_id="g1", now=_NOW)
        assert [g["group_id"] for g in r["group_rates"]] == ["g1"]

    def test_group_rates_sorted_desc(self):
        active = _snap_act("hi", [(1, _ts(2025, 5, 30)), (2, _ts(2025, 5, 30))], name="活跃群")
        quiet = _snap_act("lo", [(3, _ts(2024, 1, 1)), (4, _ts(2024, 1, 1))], name="潜水群")
        r = analytics.compute_activity([active, quiet], group_id=None, now=_NOW)
        assert [g["group_id"] for g in r["group_rates"]] == ["hi", "lo"]
        assert r["group_rates"][0]["active_rate"] == 1.0
        assert r["group_rates"][1]["active_rate"] == 0.0

    def test_idle_days_clamped_to_active_days(self):
        # idle_days < active_days 时夹紧为 active_days，不至于让 semi 区间为负
        s = _snap_act("g1", [(1, _ts(2025, 5, 28))])
        r = analytics.compute_activity([s], group_id="g1", active_days=7, idle_days=3, now=_NOW)
        assert r["idle_days"] == 7

    def test_failed_group_excluded(self):
        ok = _snap_act("g1", [(1, _ts(2025, 5, 30))])
        bad = _snap_act("g2", [(2, _ts(2025, 5, 30))], error="boom")
        r = analytics.compute_activity([ok, bad], group_id=None, now=_NOW)
        assert r["total"] == 1
        assert [g["group_id"] for g in r["group_rates"]] == ["g1"]

    def test_all_unknown_no_zero_division(self):
        s = _snap_act("g1", [(1, 0), (2, 0)])
        r = analytics.compute_activity([s], group_id="g1", now=_NOW)
        assert r["known"] == 0 and r["active_rate"] == 0.0
        assert r["distribution"]["unknown"] == 2


# --------------------------- compute_overlap_members ---------------------------
def _snap_ov(gid, members, error=None, name=""):
    """members: [(user_id, card, role), ...]"""
    return FakeSnap(
        group_id=gid,
        group_name=name,
        members=[FakeMember(user_id=u, nickname=f"n{u}", card=c, role=r) for u, c, r in members],
        error=error,
    )


class TestOverlapMembers:
    def test_intersection_with_per_group_cards_roles(self):
        a = _snap_ov("g1", [(1, "甲A", "admin"), (2, "乙A", "member"), (3, "丙", "member")], name="群一")
        b = _snap_ov("g2", [(1, "甲B", "member"), (2, "乙B", "owner"), (4, "丁", "member")], name="群二")
        r = analytics.compute_overlap_members([a, b], "g1", "g2")
        assert r["available"] is True and r["shared"] == 2 and r["truncated"] is False
        assert [m["user_id"] for m in r["members"]] == [1, 2]  # 按 user_id 升序
        m1 = r["members"][0]
        assert m1["card_a"] == "甲A" and m1["role_a"] == "admin"
        assert m1["card_b"] == "甲B" and m1["role_b"] == "member"
        assert m1["display"] == "甲A"  # 展示名优先取群 A 名片
        assert r["group_a_name"] == "群一" and r["group_b_name"] == "群二"

    def test_display_falls_back_to_nickname(self):
        a = _snap_ov("g1", [(5, "", "member")])
        b = _snap_ov("g2", [(5, "", "member")])
        r = analytics.compute_overlap_members([a, b], "g1", "g2")
        assert r["members"][0]["display"] == "n5"  # 两边都无名片 → 退回昵称

    def test_no_overlap(self):
        a = _snap_ov("g1", [(1, "", "member")])
        b = _snap_ov("g2", [(2, "", "member")])
        r = analytics.compute_overlap_members([a, b], "g1", "g2")
        assert r["available"] is True and r["shared"] == 0 and r["members"] == []

    def test_missing_group_unavailable(self):
        a = _snap_ov("g1", [(1, "", "member")])
        r = analytics.compute_overlap_members([a], "g1", "nope")
        assert r["available"] is False and r["members"] == []

    def test_failed_group_excluded(self):
        a = _snap_ov("g1", [(1, "", "member")])
        b = _snap_ov("g2", [(1, "", "member")], error="boom")
        r = analytics.compute_overlap_members([a, b], "g1", "g2")
        assert r["available"] is False  # 失败群被 _successful 剔除

    def test_limit_truncates(self):
        a = _snap_ov("g1", [(i, "", "member") for i in range(1, 11)])
        b = _snap_ov("g2", [(i, "", "member") for i in range(1, 11)])
        r = analytics.compute_overlap_members([a, b], "g1", "g2", limit=3)
        assert r["shared"] == 10 and len(r["members"]) == 3 and r["truncated"] is True
