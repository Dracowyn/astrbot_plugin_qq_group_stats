from __future__ import annotations

import asyncio
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import webapi
from .collector import StatsCollector, result_to_member_counts
from .history import HistoryStore


PLUGIN_NAME = "astrbot_plugin_qq_group_stats"

# 采集失败 / 暂停期间的重试与重查间隔（秒）默认值；可被配置 sample_retry_interval_seconds 覆盖。
# 用于避免等满一个采样间隔（可能长达一天）才重试或响应 resume；实际取值会被采样间隔封顶。
DEFAULT_RETRY_INTERVAL_SECONDS = 300


def _extract_group_id(event: AstrMessageEvent) -> str | None:
    """从事件提取 QQ 群号；非 aiocqhttp 群消息返回 None。

    用 event 官方接口判断，不再依赖解析 unified_msg_origin —— 该字符串第一段
    是用户在 WebUI 设置的 platform_id（实例 ID），不一定等于 "aiocqhttp"。
    """
    try:
        if event.get_platform_name() != "aiocqhttp":
            return None
        # MessageType.GROUP_MESSAGE.value == "GroupMessage"
        if event.get_message_type().value != "GroupMessage":
            return None
        gid = str(event.get_group_id() or "")
        return gid if gid else None
    except Exception:
        return None


class QQGroupStatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config

        self._start_task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        # 采集层：采样循环与仪表盘共用同一份缓存，内部自带锁避免重复拉群。
        self._collector = StatsCollector(
            lambda: self.config, self._resolve_aiocqhttp_client
        )
        # 人数历史落盘（真实净人数的时间序列），后台采样循环按天采样。
        self._history = HistoryStore(
            self._history_path(),
            min_interval=int(self.config.get("history_sample_interval_seconds", 86400) or 86400),
        )

        self._last_count: int | None = None
        self._last_sampled_at: float = 0.0
        self._last_error: str = ""

        self._start_task = asyncio.create_task(self._start())

    def _history_path(self) -> str:
        """历史文件落在 data/plugin_data/<插件名>/（随插件更新保留）；取不到则退回插件目录。"""
        try:
            return os.path.join(str(StarTools.get_data_dir(PLUGIN_NAME)), "member_history.json")
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] StarTools data dir unavailable ({e}); "
                "history falls back to plugin dir (gitignored)"
            )
            return os.path.join(os.path.dirname(__file__), "member_history.json")

    # ------------------------------------------------------------------
    # 仪表盘 Web API（沙箱页面经 Bridge 调用，走仪表盘 JWT 鉴权）
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        prefix = f"/{PLUGIN_NAME}"
        # 路由前缀必须为插件名：前端 Bridge 会把调用拼成 /api/plug/<插件名>/<endpoint>。
        self.context.register_web_api(
            f"{prefix}/overview", self._api_overview, ["GET"], "QQ 群统计仪表盘概览"
        )
        self.context.register_web_api(
            f"{prefix}/group/<group_id>/members",
            self._api_members,
            ["GET"],
            "查看单群成员明细",
        )
        self.context.register_web_api(
            f"{prefix}/member/<user_id>/groups",
            self._api_member_groups,
            ["GET"],
            "按 QQ 号实时检索在哪些群",
        )
        self.context.register_web_api(
            f"{prefix}/growth", self._api_growth, ["GET"], "群人数增长曲线"
        )
        self.context.register_web_api(
            f"{prefix}/overlap", self._api_overlap, ["GET"], "两个群的共同成员明细"
        )
        self.context.register_web_api(
            f"{prefix}/activity", self._api_activity, ["GET"], "群成员活跃度分布"
        )
        self.context.register_web_api(
            f"{prefix}/honor", self._api_honor, ["GET"], "群 QQ 原生活跃榜"
        )
        self.context.register_web_api(
            f"{prefix}/discover", self._api_discover, ["GET"], "发现机器人加入的所有群"
        )
        self.context.register_web_api(
            f"{prefix}/bind", self._api_bind, ["POST"], "把群纳入统计列表"
        )
        self.context.register_web_api(
            f"{prefix}/unbind", self._api_unbind, ["POST"], "从统计列表移除群"
        )
        self.context.register_web_api(
            f"{prefix}/leave", self._api_leave, ["POST"], "机器人退出指定群"
        )
        self.context.register_web_api(
            f"{prefix}/group/<group_id>/kick", self._api_kick, ["POST"], "踢出群成员"
        )
        logger.info(f"[{PLUGIN_NAME}] dashboard web api registered")

    async def _api_overview(self):
        return await webapi.overview(self)

    async def _api_members(self, group_id):
        return await webapi.members(self, group_id)

    async def _api_member_groups(self, user_id):
        return await webapi.member_groups(self, user_id)

    async def _api_growth(self):
        return await webapi.growth(self)

    async def _api_overlap(self):
        return await webapi.overlap_members(self)

    async def _api_activity(self):
        return await webapi.activity(self)

    async def _api_honor(self):
        return await webapi.honor(self)

    async def _api_discover(self):
        return await webapi.discover(self)

    async def _api_bind(self):
        return await webapi.bind(self)

    async def _api_unbind(self):
        return await webapi.unbind(self)

    async def _api_leave(self):
        return await webapi.leave_group(self)

    async def _api_kick(self, group_id):
        return await webapi.kick_member(self, group_id)

    # ------------------------------------------------------------------
    # 统计列表增删（命令与仪表盘共用，单一写盘入口）
    # ------------------------------------------------------------------
    def add_group(self, gid: str) -> list[str]:
        groups = [str(g) for g in (self.config.get("groups", []) or [])]
        if gid not in groups:
            groups = groups + [gid]
            self.config["groups"] = groups
            self.config.save_config()
        return groups

    def remove_group(self, gid: str) -> list[str]:
        groups = [str(g) for g in (self.config.get("groups", []) or [])]
        if gid in groups:
            groups = [g for g in groups if g != gid]
            self.config["groups"] = groups
            self.config.save_config()
        return groups

    # ------------------------------------------------------------------
    # 命令组
    # ------------------------------------------------------------------
    @filter.command_group("qq_stats")
    def qq_stats(self):
        """QQ 群成员统计"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @qq_stats.command("bind")
    async def bind(self, event: AstrMessageEvent):
        """把当前群纳入统计"""
        gid = _extract_group_id(event)
        if gid is None:
            yield event.plain_result("仅支持在 QQ 群内使用 /qq_stats bind 命令")
            return
        groups = [str(g) for g in (self.config.get("groups", []) or [])]
        if gid in groups:
            yield event.plain_result(f"当前 QQ 群 {gid} 已经在统计列表里")
            return
        self.add_group(gid)
        yield event.plain_result(f"已将 QQ 群 {gid} 纳入统计。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @qq_stats.command("unbind")
    async def unbind(self, event: AstrMessageEvent):
        """把当前群移出统计"""
        gid = _extract_group_id(event)
        if gid is None:
            yield event.plain_result("仅支持在 QQ 群内使用 /qq_stats unbind 命令")
            return
        groups = [str(g) for g in (self.config.get("groups", []) or [])]
        if gid not in groups:
            yield event.plain_result(f"当前 QQ 群 {gid} 不在统计列表里")
            return
        self.remove_group(gid)
        yield event.plain_result(f"已将 QQ 群 {gid} 移出统计")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @qq_stats.command("list")
    async def list_groups(self, event: AstrMessageEvent):
        """查看已纳入统计的群"""
        groups = [str(g) for g in (self.config.get("groups", []) or [])]
        if not groups:
            yield event.plain_result(
                "当前还没有纳入任何 QQ 群。\n在想统计的群里发 /qq_stats bind 即可纳入。"
            )
            return
        lines = [f"已纳入统计 {len(groups)} 个 QQ 群："]
        lines.extend(f"  {i}. QQ 群 {g}" for i, g in enumerate(groups, 1))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @qq_stats.command("sample")
    async def force_sample(self, event: AstrMessageEvent):
        """立即采集一次并记录人数历史"""
        try:
            count, per_group = await self.run_sample_now()
        except Exception as e:
            yield event.plain_result(f"采集失败：{e}")
            return
        yield event.plain_result(
            f"已采集：去重人数 {count}，覆盖 {len(per_group)} 个群"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @qq_stats.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看插件运行状态"""
        enabled = bool(self.config.get("enabled", True))
        interval = int(self.config.get("history_sample_interval_seconds", 86400) or 86400)
        groups = [str(g) for g in (self.config.get("groups", []) or [])]
        last_sampled_at = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_sampled_at))
            if self._last_sampled_at
            else "尚未采集"
        )
        last_count = "未知" if self._last_count is None else str(self._last_count)

        lines = [
            "QQ 群成员统计 · 当前状态",
            f"  后台采样: {'已启用' if enabled else '已暂停'}",
            f"  采样间隔: {interval}s",
            f"  统计群数: {len(groups)}",
            f"  上次人数: {last_count}",
            f"  上次采集: {last_sampled_at}",
            f"  上次错误: {self._last_error or '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @qq_stats.command("pause")
    async def pause(self, event: AstrMessageEvent):
        """暂停后台定时采样"""
        self.config["enabled"] = False
        self.config.save_config()
        yield event.plain_result("已暂停后台定时采样")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @qq_stats.command("resume")
    async def resume(self, event: AstrMessageEvent):
        """恢复后台定时采样"""
        self.config["enabled"] = True
        self.config.save_config()
        yield event.plain_result("已恢复后台定时采样")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def terminate(self) -> None:
        # 先停 _start_task，确保它不再在结尾处创建 _loop_task；再停 _loop_task。
        # 顺序反过来会有毫秒级竞态：_start_task 在被取消前可能刚好赋值 _loop_task。
        for task in (self._start_task, self._loop_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning(f"[{PLUGIN_NAME}] task cleanup error: {e}")

    # ------------------------------------------------------------------
    # 启动 & 采样循环
    # ------------------------------------------------------------------
    async def _start(self) -> None:
        try:
            delay = int(self.config.get("startup_delay_seconds", 15) or 0)
            if delay > 0:
                await asyncio.sleep(delay)
            logger.info(f"[{PLUGIN_NAME}] started, sampling loop begins")
            self._loop_task = asyncio.create_task(self._sample_loop())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            logger.error(f"[{PLUGIN_NAME}] _start failed: {e}")

    async def _sample_loop(self) -> None:
        """后台按采样间隔拉一次绑定群成员，落盘真实净人数（供“群人数增长”实际曲线）。

        采样是这个循环唯一的职责。HistoryStore 内部还会按 ``min_interval`` 节流，
        循环按同一间隔醒来即可。
        """
        try:
            while True:
                interval = max(
                    60, int(self.config.get("history_sample_interval_seconds", 86400) or 86400)
                )
                retry = min(
                    interval,
                    max(
                        10,
                        int(
                            self.config.get(
                                "sample_retry_interval_seconds", DEFAULT_RETRY_INTERVAL_SECONDS
                            )
                            or DEFAULT_RETRY_INTERVAL_SECONDS
                        ),
                    ),
                )
                if not self.config.get("enabled", True):
                    # 暂停期间短间隔轮询，让 resume 后尽快恢复采样，而非等满一个采样间隔。
                    await asyncio.sleep(retry)
                    continue
                try:
                    await self._sample_history_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._last_error = f"{type(e).__name__}: {e}"
                    logger.warning(f"[{PLUGIN_NAME}] sample cycle failed: {e}")
                    # 失败（如平台未就绪、Napcat 抖动）后短间隔重试，不等满一个采样间隔。
                    await asyncio.sleep(retry)
                    continue
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # 采集 & 历史落盘
    # ------------------------------------------------------------------
    async def _sample_history_once(self) -> tuple[int, dict[str, int]]:
        """强制拉新绑定群成员、去重计数并按节流落盘历史；顺带刷新仪表盘缓存。

        失败抛异常（并记入 ``_last_error`` 供状态展示），调用方负责面向用户措辞。
        """
        result = await self._collector.collect()
        # "全部失败"按拉取成功的群数判定：有绑定群但一个都没拉成功才抛错。
        if result.bound_group_count > 0 and result.successful_count == 0:
            raise RuntimeError("; ".join(result.errors) or "所有群成员拉取失败")
        count, per_group = result_to_member_counts(result)
        if result.bound_group_count > 0:
            # 仅在有绑定群时落盘并更新人数：无绑定群时跳过，免得历史曲线积累一串 0。
            # min_interval 取实时配置，用户运行期调小采样间隔后无需重启即可生效。
            try:
                self._history.maybe_record(
                    count,
                    per_group,
                    min_interval=int(
                        self.config.get("history_sample_interval_seconds", 86400) or 86400
                    ),
                )
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] history record skipped: {e}")
            self._last_count = count
        self._last_sampled_at = time.time()
        self._last_error = ""
        return count, per_group

    async def run_sample_now(self) -> tuple[int, dict[str, int]]:
        """立即采集并落盘一次（``/qq_stats sample`` 命令用）。失败抛异常并记入 ``_last_error``。"""
        try:
            return await self._sample_history_once()
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            raise

    def _resolve_aiocqhttp_client(self):
        platform_id = str(self.config.get("platform_id", "") or "")
        if platform_id:
            try:
                platform = self.context.get_platform_inst(platform_id)
            except Exception:
                platform = None
            if platform is not None:
                return platform.get_client()

        try:
            instances = self.context.platform_manager.get_insts()
        except Exception:
            instances = []
        for platform in instances:
            try:
                if platform.meta().name == "aiocqhttp":
                    return platform.get_client()
            except Exception:
                continue
        return None
