"""让测试直接 import 插件根目录下的纯逻辑模块（analytics）。

AstrBot 运行时以 ``data.plugins.astrbot_plugin_qq_group_stats.xxx`` 形式导入；测试则把
插件根目录加入 sys.path，直接 ``import analytics``。analytics 不含任何插件内交叉导入或
astrbot 依赖（纯函数 + 鸭子类型），两种方式都成立。
"""

import os
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)
