"""仪表盘后端 Web API（经 ``register_web_api`` 注册，前端 Bridge 调用）。

统一信封 ``{status, message, data}``：前端 Bridge 对非 error 解出 ``data``。
全部走仪表盘 JWT 鉴权（``/api/plug/*``），即仅登录管理员可访问，可安全下发成员明细。

handler 以 ``plugin`` 实例为参数；main.py 用薄方法委托到此处，保持 main.py 精简。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from quart import jsonify, request

from . import analytics
from .collector import CollectionResult

if TYPE_CHECKING:
    from .main import QQGroupStatsPlugin


def _ok(data: Any = None, message: str | None = None):
    return jsonify(
        {"status": "ok", "data": data if data is not None else {}, "message": message}
    )


def _err(message: str):
    return jsonify({"status": "error", "message": message, "data": None})


def _request_timeout(plugin: "QQGroupStatsPlugin") -> int:
    return int(plugin.config.get("request_timeout_seconds", 15) or 15)


def _status_block(
    plugin: "QQGroupStatsPlugin", result: CollectionResult | None, scope: str
) -> dict[str, Any]:
    cfg = plugin.config
    last_sampled_at = plugin._last_sampled_at
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "scope": scope,
        "sample_interval_seconds": int(
            cfg.get("history_sample_interval_seconds", 86400) or 86400
        ),
        "dashboard_cache_ttl": int(cfg.get("dashboard_cache_ttl_seconds", 120) or 120),
        "last_count": plugin._last_count,
        "last_sampled_at": int(last_sampled_at) if last_sampled_at else 0,
        "last_error": plugin._last_error or "",
        "fetched_at": result.fetched_at if result else 0,
        "cache_age": int(plugin._collector.cache_age() or 0),
    }


# ----------------------------------------------------------------------
# GET /overview —— 仪表盘主数据（一个请求拿全）
# ----------------------------------------------------------------------
async def overview(plugin: "QQGroupStatsPlugin"):
    """核心指标 + 各群概览 + 重复分析 + 运行状态。

    ``?scope=all`` 含未绑定群（按需拉取、单独缓存）；默认 ``bound`` 只统计绑定群。
    ``?refresh=1`` 强制重新拉取；否则缓存新鲜（年龄 < TTL）时直接复用。
    """
    force = str(request.args.get("refresh", "")).lower() in {"1", "true", "yes"}
    scope = "all" if str(request.args.get("scope", "")).lower() == "all" else "bound"
    ttl = int(plugin.config.get("dashboard_cache_ttl_seconds", 120) or 120)

    try:
        result = await plugin._collector.get(
            ttl=ttl, force=force, include_unbound=(scope == "all")
        )
    except Exception as e:
        return _err(f"采集失败：{type(e).__name__}: {e}"[:200])

    snapshots = result.snapshots
    data = {
        "overview": analytics.compute_overview(snapshots),
        "top_overlap": analytics.compute_top_overlap(snapshots, limit=30),
        "pairwise": analytics.compute_pairwise_overlap(snapshots),
        "status": _status_block(plugin, result, scope),
        "errors": list(result.errors),
    }
    return _ok(data)


# ----------------------------------------------------------------------
# GET /group/<group_id>/members —— 单群成员明细（读缓存，不额外拉群）
# ----------------------------------------------------------------------
async def members(plugin: "QQGroupStatsPlugin", group_id: str):
    gid = str(group_id or "").strip()
    if not gid.isdigit():
        return _err("群号非法")

    # 在绑定 + 未绑定两份缓存里找。
    snap = plugin._collector.find_snapshot(gid)
    if snap is None:
        return _err(f"群 {gid} 不在缓存（请先在概览页刷新，或切到“全部群”范围）")
    if snap.error:
        return _ok(
            {
                "group_id": gid,
                "group_name": snap.group_name,
                "member_count": 0,
                "members": [],
                "bound": snap.bound,
                "error": snap.error,
                "fetched_at": snap.fetched_at,
                "bot_role": snap.bot_role,
            }
        )

    return _ok(
        {
            "group_id": gid,
            "group_name": snap.group_name,
            "member_count": snap.member_count,
            "members": [m.to_dict() for m in snap.members],
            "bound": snap.bound,
            "error": None,
            "fetched_at": snap.fetched_at,
            "bot_role": snap.bot_role,  # 机器人在本群身份，前端据此决定是否显示「踢出」
        }
    )


# ----------------------------------------------------------------------
# GET /member/<user_id>/groups —— 按 QQ 号实时逐群核查在哪些群
# ----------------------------------------------------------------------
async def member_groups(plugin: "QQGroupStatsPlugin", user_id: str):
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return _err("QQ 号非法")
    try:
        res = await plugin._collector.find_member_live(int(uid))
    except Exception as e:
        return _err(f"检索失败：{type(e).__name__}: {e}"[:200])
    return _ok(res)


# ----------------------------------------------------------------------
# GET /growth —— 群人数增长曲线（留存=按加入时间累计 + 真实=落盘记录）
# ----------------------------------------------------------------------
async def growth(plugin: "QQGroupStatsPlugin"):
    """``?group=<gid|all>`` 选群，``?bucket=day|week|month`` 选粒度。

    口径与历史采样一致，只看「绑定群」。返回统一的 ``series`` 列表，前端按 ``kind`` 上色叠加：
    - all：去重总曲线（``total``）+ 各群曲线（``group``）+ 实际采样（``actual``）。
    - 单群：该群按加入时间累计（``total``）+ 实际采样（``actual``）。
    曲线随当前成员 ``join_time`` 即时可见，``actual`` 随落盘采样逐步积累。
    """
    group = str(request.args.get("group", "")).strip()
    bucket = str(request.args.get("bucket", "month")).lower()
    gid = group if group and group.lower() != "all" else None
    if gid is not None and not gid.isdigit():
        return _err("群号非法")
    ttl = int(plugin.config.get("dashboard_cache_ttl_seconds", 120) or 120)
    try:
        result = await plugin._collector.get(ttl=ttl)  # 绑定群范围
        snapshots = result.snapshots
        selector = [
            {"group_id": s.group_id, "group_name": s.group_name}
            for s in snapshots
            if s.bound and not s.error
        ]
        if gid is None:
            multi = analytics.compute_growth_multi(snapshots, bucket=bucket)
            series = [
                {"key": "total", "name": "全部统计群（去重）", "kind": "total",
                 "points": multi["total"]["points"]},
                {"key": "actual", "name": "实际人数（采样）", "kind": "actual",
                 "points": plugin._history.aggregate_series()},
            ]
            series += [
                {"key": g["group_id"], "kind": "group",
                 "name": g["group_name"] or ("群 " + g["group_id"]), "points": g["points"]}
                for g in multi["groups"]
            ]
            unknown = int(multi["total"].get("unknown_join", 0))
            omitted = int(multi.get("omitted", 0))
        else:
            retained = analytics.compute_growth_series(snapshots, group_id=gid, bucket=bucket)
            series = [
                {"key": "total", "name": "按加入时间累计", "kind": "total",
                 "points": retained["points"]},
                {"key": "actual", "name": "实际人数（采样）", "kind": "actual",
                 "points": plugin._history.group_series(gid)},
            ]
            unknown = int(retained.get("unknown_join", 0))
            omitted = 0
    except Exception as e:
        return _err(f"增长曲线计算失败：{type(e).__name__}: {e}"[:200])
    return _ok(
        {
            "group": gid or "all",
            "bucket": bucket if bucket in {"day", "week", "month", "year"} else "month",
            "series": series,
            "unknown_join": unknown,
            "omitted": omitted,
            "groups": selector,
        }
    )


# ----------------------------------------------------------------------
# GET /overlap —— 两个群的共同成员明细（读缓存，给「群两两重叠 Top」下钻用）
# ----------------------------------------------------------------------
async def overlap_members(plugin: "QQGroupStatsPlugin"):
    """``?a=<gid>&b=<gid>``：返回两群交集成员。两群须已在缓存（同一统计范围加载过）。"""
    a = str(request.args.get("a", "")).strip()
    b = str(request.args.get("b", "")).strip()
    if not a.isdigit() or not b.isdigit():
        return _err("群号非法")
    if a == b:
        return _err("两个群相同")
    # 在绑定 + 未绑定两份缓存里找；两群可能分属不同范围。
    snap_a = plugin._collector.find_snapshot(a)
    snap_b = plugin._collector.find_snapshot(b)
    # 已在缓存但本轮拉取失败的群，单独提示，避免误导用户「未加载」反复刷新。
    for gid_c, sc in ((a, snap_a), (b, snap_b)):
        if sc is not None and sc.error:
            return _err(f"群 {gid_c} 数据拉取失败：{sc.error}"[:200])
    snaps = [s for s in (snap_a, snap_b) if s is not None]
    data = analytics.compute_overlap_members(snaps, a, b)
    if not data.get("available"):
        return _err("两个群需先在概览页同一统计范围加载，请刷新后重试")
    return _ok(data)


# ----------------------------------------------------------------------
# GET /activity —— 群成员活跃度（按 last_sent_time 分布，复用已采集数据）
# ----------------------------------------------------------------------
async def activity(plugin: "QQGroupStatsPlugin"):
    """``?group=<gid|all>`` 选群。口径只看「绑定群」，与历史采样一致。

    活跃度来自成员 ``last_sent_time``，无需额外接口；阈值由配置 ``activity_active_days /
    activity_idle_days`` 决定。QQ 原生活跃榜走单独的 ``/honor``（按需逐群拉）。
    """
    group = str(request.args.get("group", "")).strip()
    gid = group if group and group.lower() != "all" else None
    if gid is not None and not gid.isdigit():
        return _err("群号非法")
    ttl = int(plugin.config.get("dashboard_cache_ttl_seconds", 120) or 120)
    active_days = int(plugin.config.get("activity_active_days", 7) or 7)
    idle_days = int(plugin.config.get("activity_idle_days", 30) or 30)
    try:
        result = await plugin._collector.get(ttl=ttl)  # 绑定群范围
        data = analytics.compute_activity(
            result.snapshots, group_id=gid, active_days=active_days, idle_days=idle_days
        )
        groups = [
            {"group_id": s.group_id, "group_name": s.group_name}
            for s in result.snapshots
            if s.bound and not s.error
        ]
    except Exception as e:
        return _err(f"活跃度计算失败：{type(e).__name__}: {e}"[:200])
    data["group"] = gid or "all"
    data["groups"] = groups
    return _ok(data)


# ----------------------------------------------------------------------
# GET /honor —— 某群的 QQ 原生活跃榜（龙王 / 群聊之火等），按需逐群拉
# ----------------------------------------------------------------------
async def honor(plugin: "QQGroupStatsPlugin"):
    """``?group=<gid>`` 必填单个群：荣誉榜是 QQ 按群维护的，没有"全部群"聚合。"""
    gid = str(request.args.get("group", "")).strip()
    if not gid.isdigit():
        return _err("请选择单个群查看活跃榜")
    try:
        data = await plugin._collector.fetch_group_honor(gid)
    except Exception as e:
        return _err(f"获取活跃榜失败：{type(e).__name__}: {e}"[:200])
    return _ok({"group_id": gid, **data})


# ----------------------------------------------------------------------
# GET /discover —— 机器人加入的所有群（含是否已绑定）
# ----------------------------------------------------------------------
async def discover(plugin: "QQGroupStatsPlugin"):
    """``?refresh=1`` 让 OneBot 忽略缓存重新拉群列表（机器人新进 / 退群后刷新用）。"""
    no_cache = str(request.args.get("refresh", "")).lower() in {"1", "true", "yes"}
    try:
        groups = await plugin._collector.discover_groups(
            _request_timeout(plugin), no_cache=no_cache
        )
    except Exception as e:
        return _err(f"获取群列表失败：{type(e).__name__}: {e}"[:200])

    bound = {str(g) for g in (plugin.config.get("groups", []) or [])}
    for g in groups:
        g["bound"] = g["group_id"] in bound
    groups.sort(key=lambda g: (not g["bound"], -g["member_count"]))

    # 已绑定但机器人已退群/get_group_list 未返回的"孤儿"群号，单列出来便于清理。
    discovered_ids = {g["group_id"] for g in groups}
    orphan = sorted(bound - discovered_ids)
    return _ok({"groups": groups, "bound_count": len(bound), "orphan_bound": orphan})


# ----------------------------------------------------------------------
# POST /bind & /unbind —— 从仪表盘管理统计列表
# ----------------------------------------------------------------------
async def bind(plugin: "QQGroupStatsPlugin"):
    gid = await _read_group_id()
    if gid is None:
        return _err("缺少有效的 group_id")
    groups = plugin.add_group(gid)
    return _ok({"groups": groups}, message=f"已将 QQ 群 {gid} 纳入统计")


async def unbind(plugin: "QQGroupStatsPlugin"):
    gid = await _read_group_id()
    if gid is None:
        return _err("缺少有效的 group_id")
    groups = plugin.remove_group(gid)
    return _ok({"groups": groups}, message=f"已将 QQ 群 {gid} 移出统计")


async def _read_json() -> dict[str, Any]:
    try:
        return await request.get_json(force=True, silent=True) or {}
    except Exception:
        return {}


async def _read_group_id() -> str | None:
    body = await _read_json()
    gid = str(body.get("group_id", "")).strip()
    return gid if gid.isdigit() else None


# ----------------------------------------------------------------------
# 群管理动作（写操作，不可逆）：退群 / 踢人
# ----------------------------------------------------------------------
async def leave_group(plugin: "QQGroupStatsPlugin"):
    gid = await _read_group_id()
    if gid is None:
        return _err("缺少有效的 group_id")
    try:
        await plugin._collector.leave_group(gid)
    except Exception as e:
        return _err(f"退群失败：{type(e).__name__}: {e}"[:200])
    # 退群后该群已不可访问：移出统计列表并清缓存，让后续统计反映现状。
    groups = plugin.remove_group(gid)
    plugin._collector.invalidate_cache()
    return _ok({"groups": groups}, message=f"已退出 QQ 群 {gid}")


_ROLE_RANK = {"owner": 3, "admin": 2, "member": 1}


def _can_kick(bot_role: str, target_role: str) -> bool:
    """机器人能否踢目标：自身须是管理员+（rank≥2）且严格高于目标（群主>管理员>成员）。"""
    return _ROLE_RANK.get(bot_role, 0) >= 2 and _ROLE_RANK.get(bot_role, 0) > _ROLE_RANK.get(
        target_role, 0
    )


def _kick_block_reason(bot_role: str, target_role: str) -> str:
    if _ROLE_RANK.get(bot_role, 0) < 2:
        return "机器人在本群不是管理员，无法踢人"
    return "目标是群主 / 管理员，机器人权限不足，无法踢出"


async def kick_member(plugin: "QQGroupStatsPlugin", group_id: str):
    gid = str(group_id or "").strip()
    if not gid.isdigit():
        return _err("群号非法")
    body = await _read_json()
    uid = str(body.get("user_id", "")).strip()
    reject = bool(body.get("reject_add_request", False))
    if not uid.isdigit():
        return _err("QQ 号非法")

    # 先用缓存里的身份做前置校验：避免在没权限的群盲发 set_group_kick 拿到含糊错误。
    # 只有当机器人身份「确定」（owner/admin/member）且判定不可踢时才拦截；身份未知则放行，让动作自行尝试。
    snap = plugin._collector.find_snapshot(gid)
    if snap is not None and not snap.error:
        bot_role = snap.bot_role
        target_role = next(
            (m.role for m in snap.members if m.user_id == int(uid)), "unknown"
        )
        if bot_role in _ROLE_RANK and not _can_kick(bot_role, target_role):
            return _err(_kick_block_reason(bot_role, target_role))

    try:
        await plugin._collector.kick_member(gid, int(uid), reject)
    except Exception as e:
        return _err(f"踢出失败：{type(e).__name__}: {e}"[:200])
    # 缓存里仍含被踢成员，清掉以便下次刷新拿到最新名单。
    plugin._collector.invalidate_cache()
    return _ok({"group_id": gid, "user_id": uid}, message=f"已将 {uid} 踢出群 {gid}")
