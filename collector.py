"""群成员数据采集层（带缓存），供后台采样循环与仪表盘页面共用。

设计要点：
- 采集是昂贵操作（每群一次 ``get_group_member_list``，Napcat 会限速），因此结果缓存在内存。
- 采样循环只拉「绑定群」并按自己的节奏 ``collect()`` 拉新；仪表盘读缓存（TTL 过期 / 用户点刷新再拉）。
- 「未绑定群」是仪表盘按需、单独缓存的统计范围（``include_unbound``），采样循环不受影响。
- 「按 QQ 搜在哪些群」是实时逐群 ``get_group_member_info`` 核查（``find_member_live``），不依赖缓存。
- 全部用不可变 dataclass 承载快照，避免隐式 in-place 修改。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_qq_group_stats"


def _coerce_user_id(value: Any) -> int | None:
    """OneBot v11 规范 user_id 为 int64，但部分 Napcat 版本序列化为字符串，两种都接受。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _safe_int(value: Any, default: int = 0) -> int:
    """容错转 int：脏数据（如 "N/A"、None、float 字符串）一律回退到 default。

    必须容错——某些 OneBot 适配器对 join_time/last_sent_time 返回非数字字符串，
    若直接 int() 抛 ValueError 会中断整轮采集（旧实现根本不解析这些字段，故不受影响）。
    """
    if isinstance(value, bool):  # bool 是 int 子类，单独排除避免 True->1
        return default
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# get_group_honor_info 的榜单字段 -> 前端用的短名（统一规整，缺字段则空列表）。
_HONOR_LISTS = (
    ("talkative_list", "talkative"),   # 历史龙王
    ("performer_list", "performer"),   # 群聊之火（活跃）
    ("legend_list", "legend"),         # 群聊炽焰（传说）
    ("emotion_list", "emotion"),       # 快乐源泉
    ("strong_newbie_list", "strong_newbie"),  # 冒尖小春笋（新人）
)


def _normalize_honor(raw: Any) -> dict[str, Any]:
    """把 ``get_group_honor_info`` 原始返回规整成 ``{current_talkative, lists}``，并发友好、字段容错。"""
    if not isinstance(raw, dict):
        return {"current_talkative": None, "lists": {short: [] for _, short in _HONOR_LISTS}}

    current = None
    ct = raw.get("current_talkative")
    if isinstance(ct, dict):
        uid = _coerce_user_id(ct.get("user_id"))
        if uid is not None:
            current = {
                "user_id": uid,
                "nickname": str(ct.get("nickname") or ""),
                "day_count": _safe_int(ct.get("day_count")),
            }

    lists: dict[str, list[dict[str, Any]]] = {}
    for key, short in _HONOR_LISTS:
        items = raw.get(key)
        out: list[dict[str, Any]] = []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                uid = _coerce_user_id(it.get("user_id"))
                if uid is None:
                    continue
                out.append(
                    {
                        "user_id": uid,
                        "nickname": str(it.get("nickname") or ""),
                        "description": str(it.get("description") or ""),
                    }
                )
        lists[short] = out
    return {"current_talkative": current, "lists": lists}


@dataclass(frozen=True)
class MemberInfo:
    """单个群成员（仅保留仪表盘需要的字段）。"""

    user_id: int
    nickname: str
    card: str
    role: str  # owner / admin / member
    sex: str  # male / female / unknown
    title: str  # 群头衔
    join_time: int
    last_sent_time: int
    level: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "MemberInfo | None":
        uid = _coerce_user_id(raw.get("user_id"))
        if uid is None:
            return None
        return cls(
            user_id=uid,
            nickname=str(raw.get("nickname") or ""),
            card=str(raw.get("card") or ""),
            role=str(raw.get("role") or "member"),
            sex=str(raw.get("sex") or "unknown"),
            title=str(raw.get("title") or ""),
            join_time=_safe_int(raw.get("join_time")),
            last_sent_time=_safe_int(raw.get("last_sent_time")),
            level=str(raw.get("level") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "card": self.card,
            "role": self.role,
            "sex": self.sex,
            "title": self.title,
            "join_time": self.join_time,
            "last_sent_time": self.last_sent_time,
            "level": self.level,
        }


@dataclass(frozen=True)
class GroupSnapshot:
    """单个群在某一时刻的成员快照。``error`` 非空表示该群本轮拉取失败。

    ``bound`` 表示该群是否在统计列表（config.groups）；未绑定群仅仪表盘"全部群"范围才采集。
    """

    group_id: str
    group_name: str
    members: tuple[MemberInfo, ...]
    error: str | None
    fetched_at: int
    bound: bool = True
    # 机器人自己在本群的身份：owner / admin / member / unknown（拉成员时顺带识别，用于判断能否踢人）。
    bot_role: str = "unknown"

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def user_ids(self) -> set[int]:
        return {m.user_id for m in self.members}

    @property
    def bot_can_kick(self) -> bool:
        """机器人是否具备踢人前提（群主或管理员）。具体能否踢某人还要看目标身份。"""
        return self.bot_role in ("owner", "admin")

    def summary_dict(self) -> dict[str, Any]:
        """不含成员明细的轻量摘要（用于群列表/概览）。"""
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "member_count": self.member_count,
            "error": self.error,
            "fetched_at": self.fetched_at,
            "bound": self.bound,
            "bot_role": self.bot_role,
        }


@dataclass(frozen=True)
class CollectionResult:
    """一次采集的结果。``bound_group_count`` 始终是「绑定群」数量（统计口径），与是否含未绑定群无关。"""

    snapshots: tuple[GroupSnapshot, ...]
    bound_group_count: int
    fetched_at: int
    errors: tuple[str, ...] = field(default_factory=tuple)

    def snapshot_for(self, group_id: str) -> GroupSnapshot | None:
        for snap in self.snapshots:
            if snap.group_id == str(group_id):
                return snap
        return None

    @property
    def successful_count(self) -> int:
        return sum(1 for s in self.snapshots if not s.error)


class StatsCollector:
    """负责拉取群成员数据并缓存。线程/协程安全由内部锁保证。

    依赖以回调注入，避免与具体插件耦合：
    - ``get_config``：返回当前配置 dict。
    - ``resolve_client``：返回 aiocqhttp client，或 None（平台未就绪）。

    两份缓存：``_cached``（绑定群，供采样 + 仪表盘）/ ``_cached_unbound``（未绑定群，仅仪表盘"全部群"）。
    """

    def __init__(
        self,
        get_config: Callable[[], Any],
        resolve_client: Callable[[], Any],
    ) -> None:
        self._get_config = get_config
        self._resolve_client = resolve_client
        self._lock = asyncio.Lock()
        self._cached: CollectionResult | None = None
        self._cached_unbound: CollectionResult | None = None
        self._self_id: int | None = None  # 机器人 QQ 号，会话内不变，取到一次后复用

    # ------------------------------------------------------------------
    # 读缓存
    # ------------------------------------------------------------------
    @property
    def cached(self) -> CollectionResult | None:
        return self._cached

    def cache_age(self) -> float | None:
        """绑定群缓存年龄（秒）；无缓存返回 None。"""
        if self._cached is None:
            return None
        return max(0.0, time.time() - self._cached.fetched_at)

    @staticmethod
    def _fresh(cache: CollectionResult | None, ttl: int) -> CollectionResult | None:
        """缓存存在且年龄 < ttl 时返回缓存，否则 None。无 await，调用安全。"""
        if cache is not None and (time.time() - cache.fetched_at) < ttl:
            return cache
        return None

    def find_snapshot(self, group_id: str) -> GroupSnapshot | None:
        """在两份缓存里找指定群的快照（成员明细页用）。"""
        gid = str(group_id)
        for cache in (self._cached, self._cached_unbound):
            if cache is not None:
                snap = cache.snapshot_for(gid)
                if snap is not None:
                    return snap
        return None

    async def get(
        self, ttl: int, force: bool = False, include_unbound: bool = False
    ) -> CollectionResult:
        """返回采集结果。``include_unbound`` 为真时合并「全部群」范围（按需拉取未绑定群）。

        check-lock-recheck：先无锁快速命中缓存；未命中再取锁、取锁后复查，避免惊群重复拉群。
        """
        if not force:
            bound = self._fresh(self._cached, ttl)
            if bound is not None:
                if not include_unbound:
                    return bound
                unbound = self._fresh(self._cached_unbound, ttl)
                if unbound is not None:
                    return self._combine(bound, unbound)
        async with self._lock:
            bound = None if force else self._fresh(self._cached, ttl)
            if bound is None:
                bound = await self._collect_bound_locked()
            if not include_unbound:
                return bound
            unbound = None if force else self._fresh(self._cached_unbound, ttl)
            if unbound is None:
                # force 刷新时连群列表一起刷新（no_cache），捕捉新进 / 退群。
                unbound = await self._collect_unbound_locked(no_cache=force)
            return self._combine(bound, unbound)

    @staticmethod
    def _combine(
        bound: CollectionResult, unbound: CollectionResult | None
    ) -> CollectionResult:
        if unbound is None:
            return bound
        return CollectionResult(
            snapshots=bound.snapshots + unbound.snapshots,
            bound_group_count=bound.bound_group_count,
            # 取较旧的时间，让"采集于"如实反映两份数据里更陈旧的一份。
            fetched_at=min(bound.fetched_at, unbound.fetched_at),
            errors=bound.errors + unbound.errors,
        )

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    async def collect(self) -> CollectionResult:
        """强制拉取所有「绑定群」的成员列表（采样循环用，总是拉新），刷新并返回缓存。"""
        async with self._lock:
            return await self._collect_bound_locked()

    async def _collect_bound_locked(self) -> CollectionResult:
        """采集绑定群（config.groups），调用方必须已持有 ``self._lock``。"""
        cfg = self._get_config() or {}
        groups = [str(g) for g in (cfg.get("groups", []) or [])]
        now = int(time.time())

        if not groups:
            result = CollectionResult(snapshots=(), bound_group_count=0, fetched_at=now)
            self._cached = result
            return result

        client = self._require_client()
        request_timeout = int(cfg.get("request_timeout_seconds", 15) or 15)
        max_per_group = int(cfg.get("max_member_per_group", 5000) or 5000)
        self_id = await self._login_self_id(client, request_timeout)

        snapshots: list[GroupSnapshot] = []
        errors: list[str] = []
        for gid in groups:
            snap = await self._fetch_group(
                client, gid, request_timeout, max_per_group, now,
                bound=True, self_id=self_id,
            )
            if snap.error:
                errors.append(f"group {gid}: {snap.error}")
            snapshots.append(snap)

        result = CollectionResult(
            snapshots=tuple(snapshots),
            bound_group_count=len(groups),
            fetched_at=now,
            errors=tuple(errors),
        )
        self._cached = result
        return result

    async def _collect_unbound_locked(self, no_cache: bool = False) -> CollectionResult:
        """采集「机器人所在但未绑定」的群（受 unbound_collect_max_groups 上限保护）。

        ``no_cache`` 透传给群列表发现：强制刷新（force）时一并刷新群列表，捕捉新进 / 退群。
        """
        cfg = self._get_config() or {}
        bound_ids = {str(g) for g in (cfg.get("groups", []) or [])}
        now = int(time.time())
        request_timeout = int(cfg.get("request_timeout_seconds", 15) or 15)
        max_per_group = int(cfg.get("max_member_per_group", 5000) or 5000)
        max_groups = max(1, int(cfg.get("unbound_collect_max_groups", 30) or 30))

        client = self._require_client()  # 顶部获取一次（未就绪则抛错，与绑定采集一致）
        try:
            all_groups = await self.discover_groups(request_timeout, no_cache=no_cache)
        except Exception as e:
            # 不缓存「发现失败」（多为瞬时网络问题）：让下次请求即时重试，而非等到 TTL 过期。
            return CollectionResult(
                snapshots=(), bound_group_count=0, fetched_at=now,
                errors=(f"discover: {type(e).__name__}: {e}"[:300],),
            )

        unbound = [
            g for g in all_groups if g.get("group_id") and g["group_id"] not in bound_ids
        ]
        errors: list[str] = []
        if len(unbound) > max_groups:
            logger.warning(
                f"[{PLUGIN_NAME}] unbound groups {len(unbound)} > {max_groups}, truncated"
            )
            errors.append(f"未绑定群 {len(unbound)} 个超过上限 {max_groups}，已截断")
            unbound = unbound[:max_groups]

        self_id = await self._login_self_id(client, request_timeout)
        snapshots: list[GroupSnapshot] = []
        for g in unbound:
            # 群名已由 discover_groups 提供，复用以省一次 get_group_info 调用。
            snap = await self._fetch_group(
                client, g["group_id"], request_timeout, max_per_group, now,
                bound=False, group_name=g.get("group_name") or "", self_id=self_id,
            )
            if snap.error:
                errors.append(f"group {g['group_id']}: {snap.error}")
            snapshots.append(snap)

        result = CollectionResult(
            snapshots=tuple(snapshots), bound_group_count=0, fetched_at=now,
            errors=tuple(errors),
        )
        self._cached_unbound = result
        return result

    def _require_client(self) -> Any:
        client = self._resolve_client()
        if client is None:
            raise RuntimeError("aiocqhttp 平台尚未就绪")
        return client

    async def _login_self_id(self, client: Any, timeout: int) -> int | None:
        """取机器人自己的 QQ 号（``get_login_info``）。失败返回 None（不影响主采集）。

        机器人 QQ 号会话内不变：取到一次便缓存复用，省掉每轮采集 / 每次强制刷新的重复调用；
        失败不缓存，下次再试。用于在成员列表里定位机器人自身、识别其群身份。
        """
        if self._self_id is not None:
            return self._self_id
        try:
            info = await asyncio.wait_for(
                client.call_action("get_login_info"), timeout=timeout
            )
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] get_login_info failed: {e!r}")
            return None
        uid = _coerce_user_id(info.get("user_id")) if isinstance(info, dict) else None
        if uid is not None:
            self._self_id = uid
        return uid

    async def _fetch_group(
        self,
        client: Any,
        gid: str,
        timeout: int,
        max_per_group: int,
        now: int,
        bound: bool = True,
        group_name: str | None = None,
        self_id: int | None = None,
    ) -> GroupSnapshot:
        # 群名：未提供时尽力而为获取，失败不影响成员拉取。
        name = group_name if group_name is not None else await self._fetch_group_name(
            client, gid, timeout
        )

        try:
            raw_members = await asyncio.wait_for(
                client.call_action("get_group_member_list", group_id=int(gid)),
                timeout=timeout,
            )
        except Exception as e:
            # 截断错误串：避免异常 message 过长撑大缓存/响应体，也收敛前端 tooltip 展示。
            err = f"{type(e).__name__}: {e}"[:300]
            logger.warning(f"[{PLUGIN_NAME}] fetch members failed for {gid}: {err}")
            return GroupSnapshot(gid, name, (), err, now, bound)

        if not isinstance(raw_members, list):
            err = f"unexpected response type {type(raw_members).__name__}"
            return GroupSnapshot(gid, name, (), err, now, bound)

        if len(raw_members) > max_per_group:
            logger.warning(
                f"[{PLUGIN_NAME}] group {gid} returned {len(raw_members)} "
                f"> {max_per_group}, truncated"
            )
            raw_members = raw_members[:max_per_group]

        members: list[MemberInfo] = []
        seen: set[int] = set()
        bot_role = "unknown"  # 在成员里定位机器人自身，读出其群身份
        for raw in raw_members:
            if not isinstance(raw, dict):
                continue
            member = MemberInfo.from_raw(raw)
            if member is None or member.user_id in seen:
                continue
            seen.add(member.user_id)
            members.append(member)
            if self_id is not None and member.user_id == self_id:
                bot_role = member.role

        return GroupSnapshot(gid, name, tuple(members), None, now, bound, bot_role=bot_role)

    async def _fetch_group_name(self, client: Any, gid: str, timeout: int) -> str:
        try:
            info = await asyncio.wait_for(
                client.call_action("get_group_info", group_id=int(gid)),
                timeout=timeout,
            )
        except Exception:
            return ""
        if isinstance(info, dict):
            return str(info.get("group_name") or "")
        return ""

    async def discover_groups(
        self, timeout: int, no_cache: bool = False
    ) -> list[dict[str, Any]]:
        """返回机器人加入的所有群（用于"群发现"，不拉成员，开销小）。

        ``no_cache=True`` 让 OneBot 实现忽略本地缓存、重新向服务器拉取群列表：
        机器人新进 / 退群后默认缓存会滞后，需据此强制刷新。
        """
        client = self._require_client()
        try:
            groups = await asyncio.wait_for(
                client.call_action("get_group_list", no_cache=no_cache),
                timeout=timeout,
            )
        except Exception as e:
            raise RuntimeError(f"{type(e).__name__}: {e}") from e
        if not isinstance(groups, list):
            return []
        out: list[dict[str, Any]] = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            out.append(
                {
                    "group_id": str(g.get("group_id") or ""),
                    "group_name": str(g.get("group_name") or ""),
                    "member_count": int(g.get("member_count") or 0),
                    "max_member_count": int(g.get("max_member_count") or 0),
                }
            )
        return out

    # ------------------------------------------------------------------
    # 按 QQ 搜在哪些群（实时逐群核查）
    # ------------------------------------------------------------------
    async def find_member_live(self, user_id: int) -> dict[str, Any]:
        """逐群调用 ``get_group_member_info`` 核查指定 QQ 在机器人哪些群里。

        - 覆盖机器人所在全部群（受 member_search_max_groups 上限），不依赖缓存。
        - 并发受 member_search_concurrency 限制，保护 Napcat。
        - 超时计入 ``uncertain``（无法确定）；其它异常视为"不在该群"（多数实现对非成员返回 action 错误）。
        """
        cfg = self._get_config() or {}
        timeout = int(cfg.get("request_timeout_seconds", 15) or 15)
        max_groups = max(1, int(cfg.get("member_search_max_groups", 50) or 50))
        concurrency = max(1, int(cfg.get("member_search_concurrency", 6) or 6))
        bound_ids = {str(g) for g in (cfg.get("groups", []) or [])}

        client = self._require_client()
        all_groups = await self.discover_groups(timeout)
        truncated = len(all_groups) > max_groups
        groups = all_groups[:max_groups]
        if not groups:
            return {
                "user_id": user_id, "checked_groups": 0, "found_count": 0,
                "uncertain": 0, "unfinished": 0, "truncated": truncated, "groups": [],
            }

        sem = asyncio.Semaphore(concurrency)
        stats = {"uncertain": 0}  # 可变容器：避免 nonlocal 标量在并发协程里增量的隐晦写法

        async def check(g: dict[str, Any]) -> dict[str, Any] | None:
            gid = g.get("group_id") or ""
            async with sem:
                try:
                    info = await asyncio.wait_for(
                        client.call_action(
                            "get_group_member_info",
                            group_id=int(gid),
                            user_id=user_id,
                            no_cache=False,
                        ),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    stats["uncertain"] += 1
                    return None
                except Exception as exc:
                    # 多数实现对"不在群"返回 action 错误 → 视为不在该群；限速/协议错误也落这里，
                    # 记 DEBUG 便于 Napcat 过载时排查，避免被静默当成"不在群"。
                    logger.debug(f"[{PLUGIN_NAME}] member_info {gid} miss/err: {exc!r}")
                    return None
            if not isinstance(info, dict):
                return None
            member = MemberInfo.from_raw(info)
            if member is None:
                return None
            return {
                "group_id": gid,
                "group_name": g.get("group_name") or "",
                "bound": gid in bound_ids,
                **member.to_dict(),
            }

        # 总预算：机器人群很多 / Napcat 慢时，避免拖到 HTTP 请求超时；超预算返回已完成的部分结果。
        budget = max(10, min(60, (len(groups) // concurrency + 1) * timeout))
        tasks = [asyncio.create_task(check(g)) for g in groups]
        done, pending = await asyncio.wait(tasks, timeout=budget)
        for t in pending:
            t.cancel()

        found: list[dict[str, Any]] = []
        for t in done:
            try:
                r = t.result()
            except Exception:  # pragma: no cover - check 内部已兜住异常
                r = None
            if r:
                found.append(r)
        found.sort(key=lambda r: (not r["bound"], r["group_id"]))
        return {
            "user_id": user_id,
            "checked_groups": len(groups) - len(pending),
            "found_count": len(found),
            "uncertain": stats["uncertain"],
            "unfinished": len(pending),  # 因预算超时未完成核查的群数
            "truncated": truncated,
            "groups": found,
        }


    # ------------------------------------------------------------------
    # 群管理动作（写操作，会真实改动 QQ 群；仅供管理员在仪表盘里点按触发）
    # ------------------------------------------------------------------
    def invalidate_cache(self) -> None:
        """丢弃两份缓存，迫使下次读取重新拉取。群管理动作（退群/踢人）后调用，让统计反映最新成员。"""
        self._cached = None
        self._cached_unbound = None

    async def leave_group(self, group_id: str, is_dismiss: bool = False) -> None:
        """机器人退出指定群（``is_dismiss=True`` 且机器人为群主时解散群）。不可逆。"""
        client = self._require_client()
        cfg = self._get_config() or {}
        timeout = int(cfg.get("request_timeout_seconds", 15) or 15)
        await asyncio.wait_for(
            client.call_action(
                "set_group_leave", group_id=int(group_id), is_dismiss=bool(is_dismiss)
            ),
            timeout=timeout,
        )

    async def kick_member(
        self, group_id: str, user_id: int, reject_add_request: bool = False
    ) -> None:
        """把指定成员踢出群（``reject_add_request=True`` 时同时拒绝其再次加群）。不可逆。"""
        client = self._require_client()
        cfg = self._get_config() or {}
        timeout = int(cfg.get("request_timeout_seconds", 15) or 15)
        await asyncio.wait_for(
            client.call_action(
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=bool(reject_add_request),
            ),
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # 群荣誉（QQ 原生活跃榜：龙王 / 群聊之火 / 活跃榜），按需逐群拉取
    # ------------------------------------------------------------------
    async def fetch_group_honor(self, group_id: str, htype: str = "all") -> dict[str, Any]:
        """调用 ``get_group_honor_info`` 取某群的 QQ 原生荣誉数据并规整。

        只读、按需（仪表盘选中单群时才调用），不缓存——榜单变动快、调用也不频繁。
        """
        client = self._require_client()
        cfg = self._get_config() or {}
        timeout = int(cfg.get("request_timeout_seconds", 15) or 15)
        raw = await asyncio.wait_for(
            client.call_action(
                "get_group_honor_info", group_id=int(group_id), type=str(htype or "all")
            ),
            timeout=timeout,
        )
        return _normalize_honor(raw)


def result_to_member_counts(result: CollectionResult) -> tuple[int, dict[str, int]]:
    """从采集结果提取 ``(去重人数, {group_id: 人数})``，只统计成功拉取的「绑定群」。

    供后台采样循环落盘人数历史（``history.HistoryStore``）：``count`` = 各绑定群成员并集去重，
    映射里每个绑定群给出其成员数。拉取失败或未绑定的群一律剔除，避免把脏数据写进历史曲线。
    """
    unique: set[int] = set()
    per_group: dict[str, int] = {}
    for snap in result.snapshots:
        if not snap.bound or snap.error:
            continue  # 只算成功拉取的绑定群
        unique |= snap.user_ids
        per_group[snap.group_id] = snap.member_count
    return len(unique), per_group
