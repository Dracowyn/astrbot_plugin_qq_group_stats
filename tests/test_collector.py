"""collector.py 测试：脏数据容错 + 历史采样载荷契约 + 采集层并发/缓存行为。

collector 依赖 ``astrbot.api``；若环境不可用则整体跳过（纯逻辑测试见 test_analytics.py）。
"""

import asyncio

import pytest

collector = pytest.importorskip(
    "collector", reason="collector 依赖 astrbot.api，运行时环境不可用时跳过"
)

MemberInfo = collector.MemberInfo
GroupSnapshot = collector.GroupSnapshot
CollectionResult = collector.CollectionResult
StatsCollector = collector.StatsCollector
result_to_member_counts = collector.result_to_member_counts


# ======================= 同步纯逻辑 =======================
class TestSafeInt:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (5, 5), ("7", 7), ("123456789", 123456789), (0, 0), ("", 0), (None, 0),
            ("N/A", 0),  # 旧实现会 ValueError 中断整轮采集
            ("--", 0), ("12.9", 12), (12.9, 12),
            (True, 0), (False, 0),  # bool 不当作 1
        ],
    )
    def test_safe_int(self, value, expected):
        assert collector._safe_int(value) == expected

    def test_custom_default(self):
        assert collector._safe_int("bad", default=-1) == -1


class TestFromRaw:
    def test_int_and_str_user_id(self):
        assert MemberInfo.from_raw({"user_id": 10001}).user_id == 10001
        assert MemberInfo.from_raw({"user_id": "10002"}).user_id == 10002

    def test_invalid_user_id_returns_none(self):
        assert MemberInfo.from_raw({"user_id": "abc"}) is None
        assert MemberInfo.from_raw({"nickname": "no id"}) is None

    def test_dirty_time_fields_do_not_raise(self):
        # 关键回归：脏 join_time/last_sent_time 不得抛异常
        m = MemberInfo.from_raw({"user_id": 1, "join_time": "N/A", "last_sent_time": None})
        assert m.join_time == 0 and m.last_sent_time == 0

    def test_fields_mapped(self):
        m = MemberInfo.from_raw(
            {"user_id": 1, "card": "c", "role": "admin", "sex": "male", "title": "t",
             "join_time": 1700000000}
        )
        assert (m.card, m.role, m.sex, m.title, m.join_time) == ("c", "admin", "male", "t", 1700000000)


def _snap(gid, uids, err=None, name=""):
    members = tuple(MemberInfo.from_raw({"user_id": u}) for u in uids)
    return GroupSnapshot(group_id=gid, group_name=name, members=members, error=err, fetched_at=100)


class TestMemberCounts:
    def test_unique_and_shape(self):
        res = CollectionResult(
            snapshots=(_snap("g1", [1, 2, 3], name="甲群"), _snap("g2", [2, 3, 4], name="乙群")),
            bound_group_count=2, fetched_at=100,
        )
        count, per_group = result_to_member_counts(res)
        assert count == 4  # 跨群去重 {1,2,3,4}
        assert per_group == {"g1": 3, "g2": 3}

    def test_failed_group_excluded(self):
        res = CollectionResult(
            snapshots=(_snap("g1", [1, 2]), _snap("g2", [], "timeout", name="乙群")),
            bound_group_count=2, fetched_at=100, errors=("group g2: timeout",),
        )
        count, per_group = result_to_member_counts(res)
        assert count == 2  # 失败群不计入去重
        assert per_group == {"g1": 2}  # 失败群被剔除，不落进历史

    def test_empty(self):
        res = CollectionResult(snapshots=(), bound_group_count=0, fetched_at=123)
        count, per_group = result_to_member_counts(res)
        assert count == 0
        assert per_group == {}


# ======================= 采集层并发/缓存（async） =======================
class FakeClient:
    """伪 aiocqhttp client，记录调用次数；可注入延迟以构造竞态。"""

    def __init__(self, members_by_group, names=None, delay=0.0, self_id=None):
        self.members_by_group = members_by_group
        self.names = names or {}
        self.delay = delay
        self.self_id = self_id
        self.member_list_calls = 0
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs.get("group_id")))
        if action == "get_login_info":
            return {"user_id": self.self_id} if self.self_id is not None else {}
        if action == "get_group_info":
            return {"group_name": self.names.get(kwargs["group_id"], ""), "group_id": kwargs["group_id"]}
        if action == "get_group_member_list":
            self.member_list_calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.members_by_group.get(kwargs["group_id"], [])
        if action == "get_group_list":
            return [
                {"group_id": g, "group_name": self.names.get(g, ""), "member_count": len(m)}
                for g, m in self.members_by_group.items()
            ]
        if action == "get_group_member_info":
            members = self.members_by_group.get(kwargs["group_id"], [])
            for mm in members:
                if int(mm.get("user_id", -1)) == int(kwargs["user_id"]):
                    return dict(mm)
            raise RuntimeError("member not found")  # 不在群 → 多数实现返回 action 错误
        if action in ("set_group_leave", "set_group_kick"):
            return None
        raise RuntimeError(f"unknown action {action}")


@pytest.mark.asyncio
async def test_collect_dedups_within_group():
    fake = FakeClient({101: [{"user_id": 1}, {"user_id": 1}, {"user_id": 2}, {"user_id": "3"}]})
    c = StatsCollector(lambda: {"groups": [101]}, lambda: fake)
    res = await c.collect()
    snap = res.snapshot_for("101")
    assert snap.member_count == 3  # {1,2,3}，群内重复只计一次

@pytest.mark.asyncio
async def test_collect_survives_dirty_member_fields():
    # 关键回归：单条脏成员不得中断整群采集
    fake = FakeClient({101: [{"user_id": 1, "join_time": "N/A"}, {"user_id": 2}]})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    res = await c.collect()
    assert res.successful_count == 1
    assert res.snapshot_for("101").member_count == 2

@pytest.mark.asyncio
async def test_collect_no_groups_returns_empty():
    c = StatsCollector(lambda: {"groups": []}, lambda: None)
    res = await c.collect()
    assert res.bound_group_count == 0 and res.snapshots == ()

@pytest.mark.asyncio
async def test_collect_raises_when_client_none():
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: None)
    with pytest.raises(RuntimeError):
        await c.collect()

@pytest.mark.asyncio
async def test_get_uses_cache_within_ttl():
    fake = FakeClient({101: [{"user_id": 1}]})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    await c.get(ttl=1000)
    await c.get(ttl=1000)
    assert fake.member_list_calls == 1  # 第二次走缓存

@pytest.mark.asyncio
async def test_get_force_refetches():
    fake = FakeClient({101: [{"user_id": 1}]})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    await c.get(ttl=1000)
    await c.get(ttl=1000, force=True)
    assert fake.member_list_calls == 2  # force 强制重拉

@pytest.mark.asyncio
async def test_get_rechecks_cache_after_lock():
    # 双拉回归：collect 持锁拉群期间，排队的 get 取锁后应复查命中缓存，不再二次全量拉群。
    fake = FakeClient({101: [{"user_id": 1}]}, delay=0.05)
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    task = asyncio.create_task(c.collect())  # 上报循环：取锁后拉群 50ms
    await asyncio.sleep(0.01)  # 确保 collect 先取到锁并进入拉取
    res = await c.get(ttl=1000)  # 仪表盘：排队 -> 取锁复查 -> 命中
    await task
    assert fake.member_list_calls == 1
    assert res.snapshot_for("101") is not None

@pytest.mark.asyncio
async def test_discover_groups():
    fake = FakeClient({101: [{"user_id": 1}], 202: [{"user_id": 2}, {"user_id": 3}]},
                      names={101: "甲群", 202: "乙群"})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    groups = await c.discover_groups(timeout=5)
    by_id = {g["group_id"]: g for g in groups}
    assert by_id["202"]["member_count"] == 2 and by_id["202"]["group_name"] == "乙群"


class _CapClient:
    """仅记录 get_group_list 的 kwargs，用于校验 no_cache 透传。"""

    def __init__(self):
        self.group_list_kwargs = None

    async def call_action(self, action, **kwargs):
        if action == "get_group_list":
            self.group_list_kwargs = kwargs
            return []
        raise RuntimeError(f"unexpected action {action}")


@pytest.mark.asyncio
async def test_discover_groups_defaults_to_cache():
    cap = _CapClient()
    c = StatsCollector(lambda: {"groups": []}, lambda: cap)
    await c.discover_groups(timeout=5)
    assert cap.group_list_kwargs.get("no_cache") is False

@pytest.mark.asyncio
async def test_discover_groups_no_cache_passthrough():
    cap = _CapClient()
    c = StatsCollector(lambda: {"groups": []}, lambda: cap)
    await c.discover_groups(timeout=5, no_cache=True)
    assert cap.group_list_kwargs.get("no_cache") is True

@pytest.mark.asyncio
async def test_force_refresh_forces_group_list_no_cache():
    # 关键回归：force 刷新「全部群」时，群列表也应 no_cache 重拉（捕捉新进/退群）。
    cap = _CapClient()
    c = StatsCollector(lambda: {"groups": []}, lambda: cap)
    await c.get(ttl=1000, force=True, include_unbound=True)
    assert cap.group_list_kwargs.get("no_cache") is True


# ======================= 全部群范围（include_unbound） =======================
@pytest.mark.asyncio
async def test_get_include_unbound_combines_and_flags():
    fake = FakeClient(
        {101: [{"user_id": 1}, {"user_id": 2}], 202: [{"user_id": 2}, {"user_id": 3}], 303: [{"user_id": 9}]},
        names={101: "甲", 202: "乙", 303: "丙"},
    )
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)  # 仅 101 绑定
    res = await c.get(ttl=1000, include_unbound=True)
    by = {s.group_id: s for s in res.snapshots}
    assert by["101"].bound is True
    assert by["202"].bound is False and by["303"].bound is False
    assert res.bound_group_count == 1  # 仍是绑定群数

@pytest.mark.asyncio
async def test_member_counts_only_bound_even_with_unbound():
    fake = FakeClient(
        {101: [{"user_id": 1}, {"user_id": 2}], 202: [{"user_id": 3}, {"user_id": 4}]},
        names={101: "甲", 202: "乙"},
    )
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    res = await c.get(ttl=1000, include_unbound=True)
    count, per_group = result_to_member_counts(res)
    assert count == 2  # 仅绑定群 101 的 {1,2}
    assert list(per_group) == ["101"]  # 未绑定群 202 不计入历史

@pytest.mark.asyncio
async def test_unbound_collect_cap():
    members = {1000 + i: [{"user_id": i}] for i in range(5)}  # 5 个群，全未绑定
    fake = FakeClient(members)
    c = StatsCollector(
        lambda: {"groups": [], "unbound_collect_max_groups": 2}, lambda: fake
    )
    res = await c.get(ttl=1000, include_unbound=True)
    unbound = [s for s in res.snapshots if not s.bound]
    assert len(unbound) == 2  # 被上限截断
    assert any("截断" in e or "上限" in e for e in res.errors)

@pytest.mark.asyncio
async def test_find_snapshot_across_caches():
    fake = FakeClient({101: [{"user_id": 1}], 202: [{"user_id": 2}]}, names={101: "甲", 202: "乙"})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    await c.get(ttl=1000, include_unbound=True)
    assert c.find_snapshot("101").bound is True
    assert c.find_snapshot("202").bound is False
    assert c.find_snapshot("999") is None


# ======================= 按 QQ 实时检索（find_member_live） =======================
@pytest.mark.asyncio
async def test_find_member_live_hits_multiple_groups():
    fake = FakeClient(
        {101: [{"user_id": 1}, {"user_id": 2}], 202: [{"user_id": 2}, {"user_id": 3}]},
        names={101: "甲", 202: "乙"},
    )
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    res = await c.find_member_live(2)  # 用户 2 同时在 101、202
    assert res["found_count"] == 2
    by = {g["group_id"]: g for g in res["groups"]}
    assert by["101"]["bound"] is True and by["202"]["bound"] is False
    assert res["checked_groups"] == 2 and res["uncertain"] == 0

@pytest.mark.asyncio
async def test_find_member_live_single_and_none():
    fake = FakeClient({101: [{"user_id": 1}], 202: [{"user_id": 2}]})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    one = await c.find_member_live(2)
    assert one["found_count"] == 1 and one["groups"][0]["group_id"] == "202"
    none = await c.find_member_live(999)
    assert none["found_count"] == 0 and none["groups"] == []

@pytest.mark.asyncio
async def test_find_member_live_truncated_and_bound_first():
    fake = FakeClient(
        {101: [{"user_id": 5}], 202: [{"user_id": 5}], 303: [{"user_id": 5}]},
        names={101: "甲", 202: "乙", 303: "丙"},
    )
    # 仅 202 绑定；上限 2 → 截断到前 2 个群（101、202）。
    c = StatsCollector(
        lambda: {"groups": ["202"], "member_search_max_groups": 2}, lambda: fake
    )
    res = await c.find_member_live(5)
    assert res["truncated"] is True and res["checked_groups"] == 2
    assert res["found_count"] == 2
    # 绑定群排前
    assert res["groups"][0]["group_id"] == "202" and res["groups"][0]["bound"] is True

@pytest.mark.asyncio
async def test_find_member_live_no_groups():
    fake = FakeClient({})  # 机器人不在任何群
    c = StatsCollector(lambda: {"groups": []}, lambda: fake)
    res = await c.find_member_live(123456)
    assert res["found_count"] == 0 and res["checked_groups"] == 0


# ======================= 群管理动作（退群 / 踢人 / 清缓存） =======================
@pytest.mark.asyncio
async def test_leave_group_calls_action():
    fake = FakeClient({101: [{"user_id": 1}]})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    await c.leave_group("101")
    assert ("set_group_leave", 101) in fake.calls

@pytest.mark.asyncio
async def test_kick_member_passes_user_and_reject():
    seen = {}
    class Cap:
        async def call_action(self, action, **kwargs):
            seen[action] = kwargs
            return None
    c = StatsCollector(lambda: {"groups": []}, lambda: Cap())
    await c.kick_member("101", 2, reject_add_request=True)
    kw = seen["set_group_kick"]
    assert kw["group_id"] == 101 and kw["user_id"] == 2 and kw["reject_add_request"] is True

@pytest.mark.asyncio
async def test_leave_group_requires_client():
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: None)
    with pytest.raises(RuntimeError):
        await c.leave_group("101")

@pytest.mark.asyncio
async def test_invalidate_cache_forces_refetch():
    fake = FakeClient({101: [{"user_id": 1}]})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    await c.get(ttl=1000)
    assert fake.member_list_calls == 1
    c.invalidate_cache()
    await c.get(ttl=1000)
    assert fake.member_list_calls == 2  # 清缓存后强制重拉


# ======================= QQ 原生活跃榜（get_group_honor_info） =======================
@pytest.mark.asyncio
async def test_fetch_group_honor_normalizes():
    seen = {}

    class HonorClient:
        async def call_action(self, action, **kwargs):
            seen["action"] = action
            seen["kwargs"] = kwargs
            return {
                "group_id": 101,
                "current_talkative": {"user_id": "1", "nickname": "龙王", "day_count": 5},
                "performer_list": [
                    {"user_id": 2, "nickname": "火", "description": "群聊之火"},
                    {"user_id": "bad"},  # 非法 user_id → 跳过
                ],
                "legend_list": "not-a-list",  # 类型错误 → 空列表
            }

    c = StatsCollector(lambda: {"groups": []}, lambda: HonorClient())
    data = await c.fetch_group_honor("101")
    assert seen["action"] == "get_group_honor_info"
    assert seen["kwargs"]["group_id"] == 101 and seen["kwargs"]["type"] == "all"
    assert data["current_talkative"] == {"user_id": 1, "nickname": "龙王", "day_count": 5}
    assert data["lists"]["performer"] == [
        {"user_id": 2, "nickname": "火", "description": "群聊之火"}
    ]
    assert data["lists"]["legend"] == []


@pytest.mark.asyncio
async def test_fetch_group_honor_bad_response():
    class BadClient:
        async def call_action(self, action, **kwargs):
            return "garbage"

    c = StatsCollector(lambda: {"groups": []}, lambda: BadClient())
    data = await c.fetch_group_honor("101")
    assert data["current_talkative"] is None
    assert data["lists"]["performer"] == []


# ======================= 机器人在群身份识别（能否踢人的前提） =======================
@pytest.mark.asyncio
async def test_collect_detects_bot_role_per_group():
    # 机器人 QQ 999：群 101 是管理员、群 102 只是普通成员
    fake = FakeClient(
        {
            101: [{"user_id": 1}, {"user_id": 999, "role": "admin"}],
            102: [{"user_id": 2}, {"user_id": 999, "role": "member"}],
        },
        self_id=999,
    )
    c = StatsCollector(lambda: {"groups": ["101", "102"]}, lambda: fake)
    res = await c.collect()
    by = {s.group_id: s for s in res.snapshots}
    assert by["101"].bot_role == "admin" and by["101"].bot_can_kick is True
    assert by["102"].bot_role == "member" and by["102"].bot_can_kick is False
    assert ("get_login_info", None) in fake.calls  # 每轮拉一次自身身份


@pytest.mark.asyncio
async def test_collect_bot_role_unknown_without_login_info():
    # get_login_info 不可用（返回空）→ 无法定位机器人 → bot_role 保持 unknown，不阻断采集
    fake = FakeClient({101: [{"user_id": 1}, {"user_id": 999, "role": "owner"}]})
    c = StatsCollector(lambda: {"groups": ["101"]}, lambda: fake)
    res = await c.collect()
    assert res.snapshots[0].bot_role == "unknown"
    assert res.snapshots[0].bot_can_kick is False
