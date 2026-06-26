# astrbot_plugin_qq_group_stats

> 内置「群成员统计中台」WebUI 看板的 AstrBot 插件 —— 多 QQ 群成员的去重、重复率、活跃度与人数增长可视化

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.25-blue)](https://github.com/AstrBotDevs/AstrBot)
![platform](https://img.shields.io/badge/platform-aiocqhttp-green)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

一个面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的插件：在 AstrBot 仪表盘里内置一个插件页面，把所有已纳入统计的群成员数据汇总成可视化看板——各群规模、去重总人数、**跨群重复率/重叠分析**、单群成员明细、成员活跃度，并支持在网页里发现 / 纳入 / 移出统计群。

---

## 功能

### 📊 群成员统计中台（WebUI 页面）

在「仪表盘 → 插件 → QQ 群成员统计中台 → 详情页 → 群成员统计中台」打开。

- **核心指标卡**：统计群数（已纳入 / 未纳入）、去重总人数、总人次、成员重复率、人次重复率、人均加群数
- **群组概览**：各群人数、规模占比、状态，可点开查看成员；未纳入群有「未纳入」标记
- **成员明细**：QQ 头像、QQ 号 / 昵称、群名片 / 头衔、角色（群主 / 管理员 / 成员）、加入时间、最后发言，支持搜索；可**导出 CSV**（含 BOM，Excel 直接打开不乱码），也可直接**踢出成员**（二次确认，不可逆）。会先识别机器人在该群的身份（拉成员时顺带读出，无额外开销）：非管理员的群隐藏「踢出」并提示，管理员也只能踢权限更低的成员（群主 > 管理员 > 成员），群概览里对应标「非管理 / 管理员 / 群主」
- **跨群重复分析**：规模占比环形图、在群数分布柱状图、群两两重叠 Top（共享成员最多的群对，可点「查看成员」下钻看两群的**共同成员明细**——含各群名片 / 角色，支持搜索与导出 CSV）、跨群最多成员 Top
- **群人数增长**：曲线叠加——「按加入时间累计」反映留存（立即有历史，但已退群者不计入）；「实际人数」由后台采样循环从启用起按天落盘记录净人数（随时间积累准确曲线）。「全部统计群」默认展示去重总线 + 各群分线；可按「年 / 月 / 日」切换粒度与默认可视窗口（年=全程、月=近 12 个月、日=近 1 个月），也支持自定义起止日期筛选
- **群成员活跃度**：基于成员 `last_sent_time`（无需额外接口）把成员分成 活跃 / 较活跃 / 沉默 / 无记录（阈值可配置），给出活跃率、潜水成员 Top（最久未发言，便于清理）与各群活跃率；选中单个群时再叠加 QQ 原生活跃榜（`get_group_honor_info`：当前龙王、群聊之火、历史龙王 等）
- **统计范围切换**：默认「仅统计群」（轻量）；切到「全部群」按需拉取未纳入群成员（单独缓存、有上限保护），未纳入群也计入去重 / 重复率 / 重叠分析
- **按 QQ 检索**：输入任意 QQ 号，实时逐群核查机器人所有群，列出该号在哪些群、每个群的角色 / 名片 / 头衔 / 加入时间
- **发现 / 管理群**：列出机器人加入的所有群，网页里直接纳入 / 移出统计、或让机器人**退群**（二次确认，不可逆），并标出「已退群的孤儿统计项」；机器人新进 / 退群后默认群列表可能滞后，弹窗内「刷新群列表」可忽略缓存重新拉取

> 页面在沙箱 iframe 内，经仪表盘 JWT 鉴权，仅登录管理员可访问。
>
> **退群 / 踢人为不可逆的写操作**，点击后均需在弹窗里二次确认；踢人 / 退群需机器人在该群具备相应权限（踢人需管理员 / 群主），否则平台会返回错误并提示失败。

#### 重复率口径

| 指标 | 定义 |
|------|------|
| 去重总人数 `unique_members` | 所有群成员并集去重后的人数 |
| 总人次 `total_occurrences` | 各群人数之和（同一人在 N 个群计 N 次） |
| 成员重复率 `member_repeat_rate` | 在 ≥2 个群的人数 ÷ 去重总人数 |
| 人次重复率 `seat_repeat_rate` | (总人次 − 去重人数) ÷ 总人次 |
| 人均加群数 `avg_groups_per_member` | 总人次 ÷ 去重总人数 |

### 🗂 人数历史落盘

后台采样循环按 `history_sample_interval_seconds`（默认一天一次）拉取统计群成员、去重计数，把真实净人数落盘到 `data/plugin_data/astrbot_plugin_qq_group_stats/member_history.json`，供「群人数增长」的「实际人数」曲线随时间积累。

---

## 安装

将本仓库克隆到 AstrBot 的插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Dracowyn/astrbot_plugin_qq_group_stats.git
```

或在 AstrBot 插件市场搜索安装后重启。本插件无第三方依赖（仅用 AstrBot 运行时已提供的 quart 与 astrbot.api）。

---

## 配置

在 AstrBot 仪表盘的插件配置页填写（字段定义见 [_conf_schema.json](_conf_schema.json)）：

| 配置项 | 说明 | 默认 |
|--------|------|------|
| `request_timeout_seconds` | OneBot 请求超时 | `15` |
| `max_member_per_group` | 单群拉取上限，超出截断告警 | `5000` |
| `groups` | 已纳入统计的群号列表（建议用指令纳入，勿手改） | `[]` |
| `platform_id` | 指定 aiocqhttp 平台 ID，多账号时区分 | `""` |
| `enabled` | 启用后台定时采样（人数历史曲线） | `true` |
| `startup_delay_seconds` | 启动后延迟首次采样，等待平台 ready | `15` |
| `dashboard_cache_ttl_seconds` | 仪表盘数据缓存有效期；TTL 内复用，点「刷新数据」强制重拉 | `120` |
| `unbound_collect_max_groups` | 「全部群」范围单次最多采集多少未纳入群（超出截断，防压 Napcat） | `30` |
| `member_search_max_groups` | 按 QQ 检索时最多逐群核查多少个群（另有约 60 秒总预算，超时返回部分结果） | `50` |
| `member_search_concurrency` | 按 QQ 检索的并发请求数（越大越快越压 Napcat，建议 4–10） | `6` |
| `history_sample_interval_seconds` | 「群人数增长」真实曲线的落盘采样间隔；后台采样循环每轮检查，距上次 ≥ 此值才记一个点（改后免重启生效） | `86400` |
| `sample_retry_interval_seconds` | 采集失败后的短重试 / 暂停期间的重查间隔；不必等满采样间隔，`resume` 也按此响应（被采样间隔封顶，最小 10s） | `300` |
| `activity_active_days` | 「群成员活跃度」里最后发言距今 ≤ 此天数记为「活跃」 | `7` |
| `activity_idle_days` | 最后发言距今 ≤ 此天数（且超过「活跃」阈值）记为「较活跃」，更久记为「沉默」；应 ≥ `activity_active_days` | `30` |

---

## 指令

均需管理员权限，在群内 / 私聊使用：

| 指令 | 说明 |
|------|------|
| `/qq_stats bind` | 把当前群纳入统计（群内使用） |
| `/qq_stats unbind` | 把当前群移出统计（群内使用） |
| `/qq_stats list` | 查看已纳入统计的群 |
| `/qq_stats sample` | 立即采集一次并记录人数历史 |
| `/qq_stats status` | 查看插件运行状态 |
| `/qq_stats pause` / `resume` | 暂停 / 恢复后台定时采样 |

> 纳入 / 移出统计、刷新群列表也都可以直接在「群成员统计中台」页面上操作。

---

## 开发

```bash
# 纯逻辑 + 采集层测试（pytest）
python -m pytest tests/ -q
```

- `collector.py` —— 带缓存（check-lock-recheck）的采集层，采样循环与仪表盘共用同一份缓存
- `analytics.py` —— 统计分析纯函数（重复率 / 重叠分布 / 两两重叠 / 加入时间增长）
- `history.py` —— 人数历史落盘（按天采样真实净人数，供「群人数增长」真实曲线）
- `webapi.py` —— 仪表盘 Web API（`register_web_api` 注册，前端 Bridge 调用）
- `pages/dashboard/index.html` —— 沙箱仪表盘前端（零外部依赖）

---

## License

见 [LICENSE](LICENSE)。

## 作者

[dracowyn](https://github.com/Dracowyn)
