"""PluginRegistry：重名拒绝、依赖/版本检查、配置校验、按场景编译能力集合。

方案 §6.2 的宿主验收义务：
- 缺凭证显示 unavailable 与原因（不静默消失）；
- 一个非关键源失败不阻断全部研究（partial_with_reason）；fail_closed 的关键插件
  不可用时编译报错（阻断是显式决定，不是意外）；
- 编译结果（CompiledSet）是能力页与 manifest 冻结的唯一真相源——
  「能力页与运行不一致」由构造消除。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .contracts import (
    API_VERSION,
    HOST_CAPABILITIES,
    Plugin,
    PluginManifest,
    ToolDefinition,
    auth_env_names,
)


class PluginError(Exception):
    """注册/编译失败（fail-loud：重名、版本不兼容、关键插件不可用）。"""


@dataclass(frozen=True)
class PluginStatus:
    plugin_id: str
    version: str
    kind: str
    status: str  # enabled / missing_config / unavailable / degraded
    reason: str = ""
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledSet:
    """一个场景（stage × market × role）的编译产物。"""

    stage: str
    market: str
    role: str
    plugins: tuple[PluginManifest, ...]
    tools: dict[str, ToolDefinition]
    #: 全部声明工具的 schema（静态绑定 + 仅声明）：能力页/冻结/路由绑定的真相源
    declared_schemas: dict[str, dict]
    #: 工具名 → 声明插件 id（运行期绑定的 trace 归属；重名已在注册期拒绝）
    tool_owners: dict[str, str]
    adapters: tuple[Any, ...]
    statuses: tuple[PluginStatus, ...]
    config_hash: str
    compiled_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def enabled(self) -> tuple[PluginStatus, ...]:
        return tuple(s for s in self.statuses if s.status == "enabled")

    @property
    def blocked(self) -> tuple[PluginStatus, ...]:
        return tuple(s for s in self.statuses if s.status != "enabled")

    def as_payload(self) -> dict[str, Any]:
        """能力页视图（从实际编译结果生成，含版本与不可用原因）。"""
        return {
            "stage": self.stage, "market": self.market, "role": self.role,
            "config_hash": self.config_hash,
            "compiled_at": self.compiled_at.isoformat(),
            "plugins": [
                {"id": s.plugin_id, "version": s.version, "kind": s.kind,
                 "status": s.status, "reason": s.reason,
                 "capabilities": list(s.capabilities), "tools": list(s.tools)}
                for s in self.statuses
            ],
            "tools": sorted(self.declared_schemas),
            "adapters": [getattr(a, "capability", lambda: None)().source_id
                         if callable(getattr(a, "capability", None)) else str(a)
                         for a in self.adapters],
        }


class PluginRegistry:
    """版本化 Python 注册表（进程内；代码即配置，无热加载）。"""

    def __init__(self, *, host_capabilities: frozenset[str] = HOST_CAPABILITIES):
        self._plugins: dict[str, Plugin] = {}
        self._tool_owners: dict[str, str] = {}  # tool name → plugin id（重名拒绝）
        self._host_capabilities = set(host_capabilities)

    # ---------------- 注册 ----------------

    def register(self, plugin: Plugin) -> None:
        m = plugin.manifest
        if m.api_version != API_VERSION:
            raise PluginError(
                f"插件 {m.id} 的 api_version={m.api_version!r} 与宿主 {API_VERSION!r} 不兼容"
            )
        if m.id in self._plugins:
            raise PluginError(f"插件重名拒绝：{m.id} 已注册（版本 "
                              f"{self._plugins[m.id].manifest.version}）")
        bound = {t.name for t in plugin.tools}
        declared = set(plugin.tool_schemas)
        provided = bound | declared
        for tool in plugin.tools:
            owner = self._tool_owners.get(tool.name)
            if owner is not None:
                raise PluginError(
                    f"工具重名拒绝：{tool.name} 已由插件 {owner} 提供，{m.id} 不得重复声明"
                )
        for name in plugin.tool_schemas:
            owner = self._tool_owners.get(name)
            if owner is not None:
                raise PluginError(
                    f"工具重名拒绝：{name} 已由插件 {owner} 提供，{m.id} 不得重复声明"
                )
        for name in m.tools:
            if name not in provided:
                raise PluginError(
                    f"插件 {m.id} 的 manifest.tools 声明了 {name} 但既无 ToolDefinition "
                    "也无 tool_schemas 声明（schema 有而 handler 无 = 能力页与运行"
                    "不一致，拒绝注册）"
                )
        for name in provided - set(m.tools):
            raise PluginError(
                f"插件 {m.id} 提供了未在 manifest.tools 声明的工具 {name}"
            )
        self._plugins[m.id] = plugin
        for name in provided:
            self._tool_owners[name] = m.id

    @property
    def plugin_ids(self) -> list[str]:
        return sorted(self._plugins)

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    # ---------------- 可用性（配置校验） ----------------

    def availability(
        self, plugin: Plugin, env: Mapping[str, str],
    ) -> tuple[str, str]:
        """(status, reason)：配置层可用性（运行期 degraded 由 status_probe 补充）。"""
        need = auth_env_names(plugin.manifest.auth)
        if need and not any(str(env.get(name) or "").strip() for name in need):
            return "missing_config", f"缺少凭证环境变量（任一即可）：{'|'.join(need)}"
        return "enabled", ""

    # ---------------- 编译 ----------------

    def compile(
        self, *, stage: str, market: str = "", role: str = "",
        env: Mapping[str, str] | None = None,
    ) -> CompiledSet:
        """按场景编译能力集合：适用性过滤 → 配置校验 → 依赖检查 → 工具/adapter 汇总。

        依赖检查（requires）迭代到不动点：插件可以依赖其他插件贡献的能力；
        依赖不满足 → unavailable（非关键）或 PluginError（fail_closed 关键插件）。
        """
        environ: Mapping[str, str] = env if env is not None else os.environ
        candidates = [
            p for p in self._plugins.values()
            if p.manifest.applies_to.matches(stage=stage, market=market)
        ]
        statuses: dict[str, PluginStatus] = {}
        enabled: dict[str, Plugin] = {}
        for p in candidates:
            status, reason = self.availability(p, environ)
            # 运行期降级探测（可选；失败不阻断，如实标注）
            if status == "enabled" and p.status_probe is not None:
                try:
                    probe = p.status_probe()
                    if str(probe.get("status") or "") == "degraded":
                        status, reason = "degraded", str(probe.get("detail") or "")
                except Exception as e:  # noqa: BLE001 - 探测失败 = degraded，不装死
                    status, reason = "degraded", f"status_probe 失败: {type(e).__name__}: {e}"
            if status == "enabled" or status == "degraded":
                enabled[p.id] = p
            statuses[p.id] = PluginStatus(
                plugin_id=p.id, version=p.manifest.version, kind=p.manifest.kind,
                status=status, reason=reason,
                capabilities=tuple(p.manifest.capabilities), tools=tuple(p.manifest.tools),
            )
        # 依赖迭代（requires ⊆ 宿主能力 ∪ 已启用插件能力）
        changed = True
        while changed:
            changed = False
            provided = set(self._host_capabilities)
            for pid, p in enabled.items():
                if statuses[pid].status in ("enabled", "degraded"):
                    provided.update(p.manifest.capabilities)
            for pid, p in list(enabled.items()):
                missing = [r for r in p.manifest.requires if r not in provided]
                if missing:
                    reason = f"依赖能力缺失：{missing}"
                    if p.manifest.critical:
                        raise PluginError(f"关键插件 {pid} 不可用：{reason}（fail_closed）")
                    enabled.pop(pid)
                    statuses[pid] = PluginStatus(
                        plugin_id=pid, version=p.manifest.version, kind=p.manifest.kind,
                        status="unavailable", reason=reason,
                        capabilities=tuple(p.manifest.capabilities),
                        tools=tuple(p.manifest.tools),
                    )
                    changed = True
        # 工具与 adapter 汇总（按 role/stage 过滤；重名已在注册期拒绝）
        tools: dict[str, ToolDefinition] = {}
        declared: dict[str, dict] = {}
        owners: dict[str, str] = {}
        adapters: list[Any] = []
        manifests: list[PluginManifest] = []
        for p in enabled.values():
            manifests.append(p.manifest)
            for tool in p.tools:
                if tool.applies(stage=stage, role=role):
                    tools[tool.name] = tool
            for name, schema in p.declared_schemas().items():
                declared[name] = schema
            for name in p.manifest.tools:
                owners[name] = p.id
            # enabled 与 degraded 都供数（degraded 是可见性状态：限速/回退通道等，
            # 能力仍在）；unavailable/missing_config 已在 enabled 集合之外
            adapters.extend(p.adapters)
        config_hash = self._config_hash(manifests, declared, environ)
        return CompiledSet(
            stage=stage, market=market, role=role,
            plugins=tuple(sorted(manifests, key=lambda m: m.id)),
            tools=tools, declared_schemas=declared, tool_owners=owners,
            adapters=tuple(adapters),
            statuses=tuple(statuses[p.id] for p in candidates),
            config_hash=config_hash,
        )

    # ---------------- 冻结（freezing 见 freezing.py，哈希口径在此统一） ----------------

    def _config_hash(
        self, manifests: list[PluginManifest], declared: dict[str, dict],
        env: Mapping[str, str],
    ) -> str:
        """配置哈希：插件/工具/schema 版本 + 凭证**存在性**（密钥值绝不进哈希原文）。"""
        material = {
            "api_version": API_VERSION,
            "plugins": {m.id: {"version": m.version, "fp": m.schema_fingerprint()}
                        for m in sorted(manifests, key=lambda x: x.id)},
            "tools": {
                name: hashlib.sha256(json.dumps(
                    schema, ensure_ascii=False, sort_keys=True, default=str
                ).encode("utf-8")).hexdigest()[:12]
                for name, schema in sorted(declared.items())
            },
            "auth_present": sorted(
                name for m in manifests
                for name in auth_env_names(m.auth) if str(env.get(name) or "").strip()
            ),
        }
        canon = json.dumps(material, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


__all__ = ["PluginError", "PluginRegistry", "PluginStatus", "CompiledSet"]
