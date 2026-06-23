"""统计分析（纯函数，便于单测）。

输入为 ``collector.GroupSnapshot`` 序列，输出 JSON 可序列化 dict。失败的群（``error`` 非空）
一律从统计中剔除，只对成功拉到成员的群计算。

口径定义（仪表盘据此展示，避免歧义）：
- 去重人数 unique_members：所有群成员并集的人数。
- 总人次 total_occurrences：各群人数之和（同一人在 N 个群计 N 次）。
- 重复成员数 duplicated_members：出现在 >= 2 个群的人数。
- 成员重复率 member_repeat_rate = duplicated_members / unique_members
  （多少比例的人不止在一个群里——最直观的"重复率"）。
- 人次重复率 seat_repeat_rate = (total_occurrences - unique_members) / total_occurrences
  （有多少"席位"是被重复的人占的）。
- 人均加群数 avg_groups_per_member = total_occurrences / unique_members。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Iterable, Protocol, Sequence


class _Member(Protocol):
    """analytics 用到的成员字段（鸭子类型，避免与 collector 硬耦合，便于独立单测）。"""

    user_id: int
    nickname: str
    card: str
    role: str
    join_time: int
    last_sent_time: int


class _Snapshot(Protocol):
    """analytics 用到的群快照字段。``error`` 非空表示该群本轮拉取失败，应剔除。"""

    group_id: str
    group_name: str
    error: str | None
    fetched_at: int
    bound: bool

    @property
    def member_count(self) -> int: ...

    @property
    def user_ids(self) -> set[int]: ...

    @property
    def members(self) -> Sequence[_Member]: ...


def _round(value: float, ndigits: int = 4) -> float:
    return round(float(value), ndigits)


def _successful(snapshots: Iterable[_Snapshot]) -> list[_Snapshot]:
    return [s for s in snapshots if not s.error]


def compute_membership(snapshots: Iterable[_Snapshot]) -> Counter:
    """user_id -> 其所在（成功拉取的）群数量。"""
    membership: Counter = Counter()
    for snap in _successful(snapshots):
        for uid in snap.user_ids:
            membership[uid] += 1
    return membership


def compute_overview(snapshots: Sequence[_Snapshot]) -> dict[str, Any]:
    """汇总核心指标 + 各群占比 + 在群数分布。"""
    successful = _successful(snapshots)
    failed = [s for s in snapshots if s.error]

    membership = compute_membership(successful)
    unique_members = len(membership)
    total_occurrences = sum(s.member_count for s in successful)
    duplicated_members = sum(1 for c in membership.values() if c >= 2)

    member_repeat_rate = (
        duplicated_members / unique_members if unique_members else 0.0
    )
    seat_repeat_rate = (
        (total_occurrences - unique_members) / total_occurrences
        if total_occurrences
        else 0.0
    )
    avg_groups_per_member = (
        total_occurrences / unique_members if unique_members else 0.0
    )

    # 在群数分布：在恰好 k 个群里的人数。key 升序。
    dist_counter: Counter = Counter(membership.values())
    distribution = [
        {"groups": k, "members": dist_counter[k]} for k in sorted(dist_counter)
    ]

    # 各群占比按"该群人数 / 总人次"，便于看哪个群规模最大。
    largest = max((s.member_count for s in successful), default=0)
    groups = [
        {
            "group_id": s.group_id,
            "group_name": s.group_name,
            "member_count": s.member_count,
            "share": _round(s.member_count / total_occurrences)
            if total_occurrences
            else 0.0,
            "rel_bar": _round(s.member_count / largest) if largest else 0.0,
            "error": s.error,
            "fetched_at": s.fetched_at,
            "bound": bool(getattr(s, "bound", True)),
            "bot_role": getattr(s, "bot_role", "unknown"),
        }
        for s in snapshots
    ]
    # 成功的群按人数降序在前，失败的群排在最后。
    groups.sort(key=lambda g: (g["error"] is not None, -g["member_count"]))

    return {
        "total_group_count": len(snapshots),
        "bound_group_count": sum(1 for s in snapshots if getattr(s, "bound", True)),
        "unbound_group_count": sum(1 for s in snapshots if not getattr(s, "bound", True)),
        "successful_group_count": len(successful),
        "failed_group_count": len(failed),
        "unique_members": unique_members,
        "total_occurrences": total_occurrences,
        "duplicated_members": duplicated_members,
        "member_repeat_rate": _round(member_repeat_rate),
        "seat_repeat_rate": _round(seat_repeat_rate),
        "avg_groups_per_member": _round(avg_groups_per_member, 2),
        "distribution": distribution,
        "groups": groups,
    }


def compute_top_overlap(
    snapshots: Sequence[_Snapshot], limit: int = 30
) -> list[dict[str, Any]]:
    """加群数最多的成员 Top N（跨群"活跃/重复"用户）。

    昵称取该用户在任一群里的非空名片/昵称（优先名片）。仅返回在 >= 2 个群的用户。
    """
    membership = compute_membership(snapshots)
    # 为每个 user 选一个展示名 + 记录其所在群号。
    display: dict[int, str] = {}
    groups_of: dict[int, list[str]] = {}
    for snap in _successful(snapshots):
        for m in snap.members:
            groups_of.setdefault(m.user_id, []).append(snap.group_id)
            if m.user_id not in display:
                display[m.user_id] = m.card or m.nickname or str(m.user_id)

    rows = [
        {
            "user_id": uid,
            "display": display.get(uid, str(uid)),
            "group_count": count,
            "groups": groups_of.get(uid, []),
        }
        for uid, count in membership.items()
        if count >= 2
    ]
    rows.sort(key=lambda r: (-r["group_count"], r["user_id"]))
    return rows[:limit]


_BUCKETS = {"day", "week", "month", "year"}


def _bucket_start(ts: int, bucket: str) -> int:
    """把时间戳归到所在桶（天/周/月/年）的起点（UTC），返回该起点的时间戳。"""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    if bucket == "day":
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    elif bucket == "week":  # 周一为一周起点
        monday = d - timedelta(days=d.weekday())
        start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    elif bucket == "year":
        start = datetime(d.year, 1, 1, tzinfo=timezone.utc)
    else:  # month
        start = datetime(d.year, d.month, 1, tzinfo=timezone.utc)
    return int(start.timestamp())


def _cumulative_series(join_times: Sequence[int], bucket: str) -> dict[str, Any]:
    """把一组 join_time 归桶并累计成单调递增曲线。

    返回 ``{"points": [{"ts", "count"}], "unknown_join": int, "total": int}``。
    join_time ≤ 0（未知）的不进曲线，仅计入 ``unknown_join``。
    """
    total = len(join_times)
    valid = sorted(t for t in join_times if t and t > 0)
    unknown = total - len(valid)

    counts: dict[int, int] = {}
    for t in valid:
        b = _bucket_start(t, bucket)
        counts[b] = counts.get(b, 0) + 1

    points: list[dict[str, int]] = []
    cum = 0
    for b in sorted(counts):
        cum += counts[b]
        points.append({"ts": b, "count": cum})
    return {"points": points, "unknown_join": unknown, "total": total}


def _earliest_join_times(snapshots: Sequence[_Snapshot]) -> list[int]:
    """全部群去重：每个 user 取其在各群里"最早的有效 join_time"（无有效值则保留 0）。"""
    earliest: dict[int, int] = {}
    for s in snapshots:
        for m in s.members:
            jt = int(m.join_time or 0)
            prev = earliest.get(m.user_id)
            if prev is None:
                earliest[m.user_id] = jt
            elif jt > 0 and (prev <= 0 or jt < prev):
                earliest[m.user_id] = jt
    return list(earliest.values())


def compute_growth_series(
    snapshots: Sequence[_Snapshot],
    group_id: str | None = None,
    bucket: str = "month",
) -> dict[str, Any]:
    """按当前成员的 ``join_time`` 累计出"留存增长曲线"。

    - ``group_id=None``：全部（成功拉取的）群去重，每个 user 取其最早 join_time。
    - 否则只看该群的成员。
    - ``bucket``：day / week / month，按桶累计成单调递增的人数曲线。
    - join_time ≤ 0（未知）的成员无法定位时间，计入 ``unknown_join`` 但不进曲线。

    返回 ``{"points": [{"ts", "count"}], "unknown_join": int, "total": int, "bucket": str}``。
    它只反映"当前仍在群的人"何时加入，已退群的人不计入（故称留存）。
    """
    bucket = bucket if bucket in _BUCKETS else "month"
    successful = _successful(snapshots)

    if group_id is not None:
        gid = str(group_id)
        snap = next((s for s in successful if s.group_id == gid), None)
        join_times = [int(m.join_time or 0) for m in snap.members] if snap is not None else []
    else:
        join_times = _earliest_join_times(successful)

    out = _cumulative_series(join_times, bucket)
    out["bucket"] = bucket
    return out


def compute_growth_multi(
    snapshots: Sequence[_Snapshot],
    bucket: str = "month",
    max_groups: int = 20,
) -> dict[str, Any]:
    """"全部绑定"视图：去重总曲线 + 各群单独曲线（前端叠加展示）。

    群按当前人数降序取前 ``max_groups`` 条线（线太多会糊成一团），其余只在
    ``omitted`` 里计数、不画线。每条曲线与 ``compute_growth_series`` 同口径。

    返回 ``{"bucket", "total": {...}, "groups": [{"group_id","group_name",...}], "omitted": int}``。
    """
    bucket = bucket if bucket in _BUCKETS else "month"
    successful = _successful(snapshots)

    total = _cumulative_series(_earliest_join_times(successful), bucket)
    ordered = sorted(successful, key=lambda s: -s.member_count)
    groups = [
        {
            "group_id": s.group_id,
            "group_name": s.group_name,
            **_cumulative_series([int(m.join_time or 0) for m in s.members], bucket),
        }
        for s in ordered[:max_groups]
    ]
    return {
        "bucket": bucket,
        "total": total,
        "groups": groups,
        "omitted": max(0, len(ordered) - max_groups),
    }


_ACTIVITY_KEYS = ("active", "semi", "idle", "unknown")


def _activity_bucket(last_sent: int, now: int, active_s: int, idle_s: int) -> str:
    """按"距今多久没发言"归桶。last_sent ≤ 0 视为无记录（从未发言或后端未提供）。"""
    ls = int(last_sent or 0)
    if ls <= 0:
        return "unknown"
    age = now - ls
    if age <= active_s:
        return "active"
    if age <= idle_s:
        return "semi"
    return "idle"


def compute_activity(
    snapshots: Sequence[_Snapshot],
    group_id: str | None = None,
    active_days: int = 7,
    idle_days: int = 30,
    now: int | None = None,
    idle_limit: int = 30,
) -> dict[str, Any]:
    """基于成员 ``last_sent_time`` 的活跃度分布（无需额外接口，复用已采集数据）。

    - ``group_id=None``：全部（成功拉取的）群去重，每个 user 取其在各群里"最近一次发言"
      （last_sent_time 取最大）——只要在任一群活跃即算活跃。否则只看该群。
    - 桶：active（``active_days`` 天内）/ semi（``idle_days`` 天内）/ idle（更久）/ unknown（无记录）。
    - ``idle_top``：有发言记录但最久未发言的成员（便于清理潜水）。
    - ``group_rates``：各群活跃率（active / 群人数），按活跃率降序。

    返回含 ``distribution / total / known / active_rate / idle_top / group_rates`` 等字段。
    """
    now = int(now) if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
    active_days = max(0, int(active_days or 0))
    idle_days = max(active_days, int(idle_days or 0))  # 保证 idle_days ≥ active_days
    active_s = active_days * 86400
    idle_s = idle_days * 86400
    successful = _successful(snapshots)

    if group_id is not None:
        gid = str(group_id)
        snap = next((s for s in successful if s.group_id == gid), None)
        sources = [snap] if snap is not None else []
    else:
        sources = successful

    # 每个 user 的"代表 last_sent_time"：取其各群里最近的一次发言 + 记录该次所在群。
    latest: dict[int, dict[str, Any]] = {}
    for s in sources:
        for m in s.members:
            ls = int(m.last_sent_time or 0)
            cur = latest.get(m.user_id)
            if cur is None or ls > cur["last_sent_time"]:
                latest[m.user_id] = {
                    "user_id": m.user_id,
                    "display": m.card or m.nickname or str(m.user_id),
                    "last_sent_time": ls,
                    "group_id": s.group_id,
                    "group_name": s.group_name,
                }

    dist = dict.fromkeys(_ACTIVITY_KEYS, 0)
    for info in latest.values():
        dist[_activity_bucket(info["last_sent_time"], now, active_s, idle_s)] += 1
    total = len(latest)
    known = total - dist["unknown"]
    active_rate = dist["active"] / total if total else 0.0  # 全员口径：active_days 天内发言占比

    idle_top = sorted(
        (info for info in latest.values() if info["last_sent_time"] > 0),
        key=lambda r: r["last_sent_time"],
    )[: max(0, int(idle_limit))]
    idle_top = [
        {**info, "silent_days": max(0, (now - info["last_sent_time"]) // 86400)}
        for info in idle_top
    ]

    # 各群活跃率与上面的口径一致：选单群时只给该群一行，全部群时给全部。
    group_rates: list[dict[str, Any]] = []
    for s in sources:
        gd = dict.fromkeys(_ACTIVITY_KEYS, 0)
        for m in s.members:
            gd[_activity_bucket(int(m.last_sent_time or 0), now, active_s, idle_s)] += 1
        mc = s.member_count
        group_rates.append(
            {
                "group_id": s.group_id,
                "group_name": s.group_name,
                "member_count": mc,
                **gd,
                "active_rate": _round(gd["active"] / mc) if mc else 0.0,
            }
        )
    group_rates.sort(key=lambda r: (-r["active_rate"], -r["member_count"]))

    return {
        "distribution": dist,
        "total": total,
        "known": known,
        "active_rate": _round(active_rate),
        "active_days": active_days,
        "idle_days": idle_days,
        "now": now,
        "idle_top": idle_top,
        "group_rates": group_rates,
    }


def compute_pairwise_overlap(
    snapshots: Sequence[_Snapshot], max_groups: int = 40
) -> dict[str, Any]:
    """两两群之间的共同成员数。群数超过 ``max_groups`` 时跳过（组合数爆炸）。

    上限放宽到 40，让"全部群"范围（绑定 + 未绑定，通常 ≤35 群）也能算出关系，供群关系图连线。
    """
    successful = _successful(snapshots)
    if len(successful) > max_groups:
        return {"available": False, "reason": "too_many_groups", "pairs": []}

    id_sets = {s.group_id: s.user_ids for s in successful}
    pairs: list[dict[str, Any]] = []
    for a, b in combinations(successful, 2):
        shared = len(id_sets[a.group_id] & id_sets[b.group_id])
        if shared <= 0:
            continue
        smaller = min(a.member_count, b.member_count) or 1
        pairs.append(
            {
                "group_a": a.group_id,
                "group_a_name": a.group_name,
                "group_b": b.group_id,
                "group_b_name": b.group_name,
                "shared": shared,
                "ratio": _round(shared / smaller),
            }
        )
    pairs.sort(key=lambda p: -p["shared"])
    return {"available": True, "pairs": pairs}


def compute_overlap_members(
    snapshots: Sequence[_Snapshot],
    group_a: str,
    group_b: str,
    limit: int = 500,
) -> dict[str, Any]:
    """两个群的共同成员明细（user_id 交集）。两群都须在快照里且成功拉取，否则 available=False。

    每个共同成员给出其在两群里的名片 / 角色（同一人各群名片可能不同），按 user_id 升序，
    超过 ``limit`` 截断并置 ``truncated``。
    """
    successful = _successful(snapshots)
    a = next((s for s in successful if s.group_id == str(group_a)), None)
    b = next((s for s in successful if s.group_id == str(group_b)), None)
    if a is None or b is None:
        return {
            "available": False,
            "shared": 0,
            "members": [],
            "truncated": False,
            "group_a": str(group_a),
            "group_a_name": a.group_name if a is not None else "",
            "group_b": str(group_b),
            "group_b_name": b.group_name if b is not None else "",
        }

    a_by = {m.user_id: m for m in a.members}
    b_by = {m.user_id: m for m in b.members}
    rows: list[dict[str, Any]] = []
    for uid in a_by.keys() & b_by.keys():
        ma, mb = a_by[uid], b_by[uid]
        rows.append(
            {
                "user_id": uid,
                "display": ma.card or mb.card or ma.nickname or mb.nickname or str(uid),
                "nickname": ma.nickname or mb.nickname or "",
                "card_a": ma.card,
                "role_a": ma.role,
                "card_b": mb.card,
                "role_b": mb.role,
            }
        )
    rows.sort(key=lambda r: r["user_id"])
    total = len(rows)
    limit = max(0, int(limit))
    return {
        "available": True,
        "shared": total,
        "truncated": total > limit,
        "members": rows[:limit],
        "group_a": a.group_id,
        "group_a_name": a.group_name,
        "group_b": b.group_id,
        "group_b_name": b.group_name,
    }
