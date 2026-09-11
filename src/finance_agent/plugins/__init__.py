"""薄插件层（tools-plugins 方案 §6）：统一装配、质量、预算和评测的第一阶段。

范围（方案 §6.2「第一版不必把所有阶段都改成通用 hook」）：
- 数据（source）、文档解析（documents.reader）、知识/核验（knowledge.context、
  research.verifier）、档案整合（profile.core、profile.consolidator）四个接缝统一；
- 内建强约束留在宿主：DataGateway 时间准入、证据原文绑定、MetricSpec、
  ProfileWriter/TypedMetricWriter、namespace 与快照隔离——插件无法声明关闭。

不做（方案的「不宜照搬」）：任意代码热加载、插件商城、复杂依赖图调度器；
MCP 是后续传输适配（§6.3），不在本包第一版。
"""

from .contracts import (
    API_VERSION,
    HOST_CAPABILITIES,
    AppliesTo,
    Plugin,
    PluginManifest,
    ToolDefinition,
)
from .executor import ToolExecutor
from .freezing import freeze_manifest, freeze_payload
from .registry import CompiledSet, PluginError, PluginRegistry, PluginStatus

__all__ = [
    "API_VERSION", "HOST_CAPABILITIES", "AppliesTo", "Plugin", "PluginManifest",
    "ToolDefinition", "ToolExecutor", "freeze_manifest", "freeze_payload",
    "CompiledSet", "PluginError", "PluginRegistry", "PluginStatus",
]
