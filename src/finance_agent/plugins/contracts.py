"""最小插件契约（tools-plugins 方案 §6.2）：类型化 manifest + 工具定义。

原则（方案的「不宜照搬」清单同样生效）：
- 代码即配置：受版本控制的 Python 注册表，不做任意代码热加载/插件商城/
  复杂依赖图调度器；
- ToolDefinition 同一对象声明 schema、handler、适用阶段/角色、读写性质、超时——
  不再出现「schema 有而 handler 无、能力页与运行不一致」；
- 内建强约束永远在宿主：DataGateway 时间准入、证据原文绑定、MetricSpec、
  ProfileWriter/TypedMetricWriter 单写者、namespace 与快照隔离。插件只能贡献
  数据/候选/核验意见，不能通过声明关闭这些约束（manifest 里没有这种开关）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

#: 契约版本：registry 拒绝不兼容 api_version 的插件（升级走显式迁移）
API_VERSION = "finance-plugin-v1"

PluginKind = Literal["source", "processing", "transport"]

#: 宿主内建能力（requires 可满足的公共部分；不由任何插件提供）
HOST_CAPABILITIES: frozenset[str] = frozenset({
    "gateway.timelock",      # DataGateway PIT 时间准入（fail-closed）
    "evidence.registry",     # ChunkStore/EvidenceDesk 逐字证据绑定
    "kb.bitemporal",         # 双时态知识库（as_of 投影）
    "metrics.typed",         # MetricStore typed 观测/论断/计算/计划
    "writers.single",        # ProfileWriter/TypedMetricWriter 单写者门禁
    "documents.store",       # DocumentStore（Document Read v2）
    "events.append_only",    # 事件溯源台账
    "snapshot.isolation",    # 快照/namespace 隔离
})


class AppliesTo(BaseModel):
    """适用场景（编译期过滤）：市场与阶段；空 = 不限。"""

    model_config = ConfigDict(extra="forbid")

    markets: list[str] = []
    stages: list[str] = []

    def matches(self, *, stage: str, market: str) -> bool:
        if self.stages and stage not in self.stages:
            return False
        return not (self.markets and market and market not in self.markets)


class PluginManifest(BaseModel):
    """插件的类型化 manifest（方案 §6.2 契约示意的可执行形态）。"""

    model_config = ConfigDict(extra="forbid")

    id: str                                   # source.sec / documents.reader / ...
    version: str
    api_version: str = API_VERSION
    kind: PluginKind
    #: 贡献的能力（filings.search / documents.fetch / financials.xbrl / ...）
    capabilities: list[str] = []
    applies_to: AppliesTo = AppliesTo()
    #: 依赖的宿主/他插件能力（编译期检查；缺失 → unavailable 并给原因）
    requires: list[str] = []
    #: 贡献的工具名（与 ToolDefinition 同源；能力页从编译结果生成）
    tools: list[str] = []
    #: 时间语义：per_record_verified（逐条验收）/ source_declared / none
    temporal_policy: str = "unknown"
    #: 认证形态：none / configured_user_agent / api_key:ENV_A|ENV_B（任一存在即可用）
    auth: str = "none"
    #: 网络策略标注（sec_endpoints / exa_api / local...）——审计与限流分组的依据
    network_policy: str = "local"
    #: 失败策略：fail_closed（缺它必须阻断）/ partial_with_reason（降级可见不阻断）
    failure_policy: Literal["fail_closed", "partial_with_reason"] = "partial_with_reason"
    #: 契约测试标识（离线夹具 + 授权冒烟的测试文件/用例名）
    contract_tests: str = ""

    @property
    def critical(self) -> bool:
        """fail_closed 即关键插件：编译不可用时阻断（默认非关键，失败不拖死研究）。"""
        return self.failure_policy == "fail_closed"

    def schema_fingerprint(self) -> str:
        canon = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ToolDefinition:
    """同一对象声明 schema + handler + 适用范围（方案 §6.2 ToolDefinition 行）。

    rw=write 的工具其 handler 仍必须走宿主受控 writer——读写性质是审计与
    权限分组的声明，不是绕过门禁的通道。
    """

    name: str
    schema_: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    plugin_id: str
    rw: Literal["read", "write"] = "read"
    #: 空 = 全阶段/全角色可用
    stages: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    timeout_s: float = 120.0

    def schema_hash(self) -> str:
        canon = json.dumps(self.schema_, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]

    def applies(self, *, stage: str, role: str) -> bool:
        if self.stages and stage not in self.stages:
            return False
        return not (self.roles and role not in self.roles)


@dataclass(frozen=True)
class Plugin:
    """一个已注册插件：manifest + 工具声明/绑定 + 数据源 adapter（source 类）。

    工具双轨（与本库「handler 按 run 装配闭包」的架构对齐）：
    - tools：静态绑定的 ToolDefinition（schema+handler 同对象，可经 ToolExecutor 执行）；
    - tool_schemas：仅声明 schema 的工具（handler 由 run 装配层绑定，如网关/
      文档/上下文工具）——注册表保证声明完整性与重名拒绝，能力页/冻结从
      编译结果生成，运行装配是否覆盖声明由 parity 测试把关。
    """

    manifest: PluginManifest
    tools: tuple[ToolDefinition, ...] = ()
    tool_schemas: dict[str, dict] = field(default_factory=dict)
    #: SourceAdapter 实例（source 类插件的数据贡献；宿主 DataGateway 负责时间准入）
    adapters: tuple[Any, ...] = ()
    #: 运行期状态探测（可选）：返回 {"status": "degraded", "detail": ...} 之类
    status_probe: Callable[[], dict[str, Any]] | None = None

    @property
    def id(self) -> str:
        return self.manifest.id

    def declared_schemas(self) -> dict[str, dict]:
        """全部声明工具的 schema（静态绑定 + 仅声明；重名已在注册期拒绝）。"""
        out = dict(self.tool_schemas)
        for t in self.tools:
            out[t.name] = t.schema_
        return out


def auth_env_names(auth: str) -> list[str]:
    """auth 声明 → 需要的环境变量名（api_key:ENV_A|ENV_B = 任一存在即可用）。"""
    if not auth.startswith("api_key:"):
        return []
    return [n.strip() for n in auth[len("api_key:"):].split("|") if n.strip()]


__all__ = [
    "API_VERSION", "HOST_CAPABILITIES", "AppliesTo", "PluginManifest",
    "ToolDefinition", "Plugin", "auth_env_names",
]
