"""群人数历史落盘：记录"真实净人数"的时间序列。

与 analytics 的"按加入时间累计"留存曲线互补——留存曲线立刻有历史但不含已退群的人，
本模块从现在起按天采样真实人数（含进退群净值），随时间积累出准确曲线，前端两条线叠加。

- 后台采样循环每轮调用 ``maybe_record``，内部按 ``min_interval`` 节流（默认一天一个点）。
- 存为 JSON：``{"points": [{"ts": int, "unique": int, "groups": {gid: count}}, ...]}``。
- 只保留最近 ``max_points`` 个点；纯文件、无外部依赖；读多写少。
- 写用临时文件 + 原子替换，避免写一半损坏。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_qq_group_stats"


class HistoryStore:
    def __init__(self, path: str, min_interval: int = 86400, max_points: int = 400) -> None:
        self._path = path
        self._min_interval = max(1, int(min_interval or 86400))
        self._max_points = max(2, int(max_points or 400))
        self._points: list[dict[str, Any]] = []
        self._loaded = False

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._points = []
            return
        except Exception as e:  # 损坏 / 非法 JSON：丢弃重来，不影响主流程
            logger.warning(f"[{PLUGIN_NAME}] history load failed: {e}")
            self._points = []
            return
        pts = data.get("points") if isinstance(data, dict) else None
        self._points = (
            [p for p in pts if isinstance(p, dict) and "ts" in p]
            if isinstance(pts, list)
            else []
        )

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"points": self._points}, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] history save failed: {e}")

    # ------------------------------------------------------------------
    def maybe_record(
        self,
        unique: int,
        groups: dict[str, int],
        now: int | None = None,
        min_interval: int | None = None,
    ) -> bool:
        """距上一个点 ≥ 节流间隔 才追加一个新点；返回是否真的记录了。

        ``min_interval`` 可按调用覆盖实例默认值——调用方（插件）传入实时配置，
        这样用户在运行期调整采样间隔后无需重启即可生效。
        """
        self._load()
        interval = self._min_interval if min_interval is None else max(1, int(min_interval))
        ts = int(now if now is not None else time.time())
        if self._points and (ts - int(self._points[-1].get("ts", 0))) < interval:
            return False
        self._points.append(
            {
                "ts": ts,
                "unique": int(unique),
                "groups": {str(k): int(v) for k, v in (groups or {}).items()},
            }
        )
        if len(self._points) > self._max_points:
            self._points = self._points[-self._max_points :]
        self._save()
        return True

    def aggregate_series(self) -> list[dict[str, int]]:
        """去重总人数随时间的真实序列。"""
        self._load()
        return [
            {"ts": int(p["ts"]), "count": int(p.get("unique", 0) or 0)}
            for p in self._points
        ]

    def group_series(self, group_id: str) -> list[dict[str, int]]:
        """单群人数随时间的真实序列（仅含记录过该群的点）。"""
        self._load()
        gid = str(group_id)
        out: list[dict[str, int]] = []
        for p in self._points:
            g = p.get("groups") or {}
            if gid in g:
                out.append({"ts": int(p["ts"]), "count": int(g.get(gid, 0) or 0)})
        return out
