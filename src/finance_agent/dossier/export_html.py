"""自包含交互 HTML 导出（设计 §11.4 第二期：组件契约已稳定，复用在线读模型）。

与 JSON/Markdown 导出的差别：HTML 导出不是「另一种文本渲染」，而是
**冻结数据 + 在线页面同一代码** 的单文件应用：

- 内嵌 viewer 包（frontend/dist-export/export-viewer.js/.css，
  由 `npm run build:export` 产出）——与在线档案页共享 StockDossierPage、
  模块组件、图表与来源抽屉的同一源码，展示与交互逐像素一致；
- 内嵌该快照的全部冻结读模型（snapshot + 全部模块 payload + 快照内证据
  摘录 + 研究产物），viewer 的 API 层短路到内嵌数据——file:// 双击即可
  离线打开，章节导航/来源抽屉/图表/研究报告深链/前进后退全部可用；
- 依赖服务器的操作（as_of 时间旅行、补研、估值试算、情景保存、再导出）
  在离线 UI 中禁用并注明——冻结文件不伪装成能写回或重算。

安全纪律：
- 内嵌 JSON 转义 `<` 为 \\u003c（防 `</script>` 提前闭合与 `<!--` 注释劫持）；
- viewer JS/CSS 内联前校验不含 `</script` / `</style`（构建产物意外包含时
  拒绝导出，不静默产出损坏文件）；
- 证据收集走 service.evidence_detail（与在线路由同源）：只嵌快照引用的
  可见摘录，知道 id 也不能让导出携带越快照数据。
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 避免循环导入（service 在本模块函数内按需引用）
    from .service import DossierError, DossierService

logger = logging.getLogger("finance_agent.dossier.export_html")

BUNDLE_JS = "export-viewer.js"
BUNDLE_CSS = "export-viewer.css"


def default_bundle_dir() -> Path:
    """viewer 包目录：FA_EXPORT_BUNDLE_DIR 覆盖；默认仓库根 frontend/dist-export
    （相对本文件定位，与服务启动 cwd 无关）；非仓库布局回落 cwd。"""
    override = os.environ.get("FA_EXPORT_BUNDLE_DIR")
    if override:
        return Path(override)
    # src/finance_agent/dossier/export_html.py → parents[3] = 仓库根
    repo_relative = Path(__file__).resolve().parents[3] / "frontend" / "dist-export"
    if repo_relative.is_dir():
        return repo_relative
    return Path.cwd() / "frontend" / "dist-export"


def collect_export_data(snap: dict[str, Any], service: DossierService) -> dict[str, Any]:
    """收集离线 viewer 需要的全部冻结读模型（与在线 API 同源，见各 service 方法）。"""
    snapshot_id = snap["context"]["snapshot_id"]

    modules: dict[str, Any] = {}
    for mod in snap.get("modules", {}):
        try:
            modules[mod] = service.module_payload_for_export(snapshot_id, mod)
        except Exception as e:  # 单个模块失败不阻断导出（离线页显示模块缺失态）
            logger.warning("HTML 导出跳过模块 %s: %s", mod, e)

    evidence: dict[str, Any] = {}
    for eid in snap.get("evidence_refs", []):
        try:
            evidence[eid] = service.evidence_detail(snapshot_id, eid)
        except Exception as e:
            logger.warning("HTML 导出跳过证据 %s: %s", eid, e)

    # 研究产物：快照引用 + 模块 payload 里列出的（产物/情景）——
    # 离线研究报告页（#/research/<id>）与在线一致可读
    artifact_ids = set(snap.get("research", {}).get("artifact_refs", []) or [])
    for mp in modules.values():
        body = (mp or {}).get("payload") or {}
        for key in ("artifacts", "scenarios"):
            for item in body.get(key) or []:
                aid = (item or {}).get("artifact_id")
                if aid:
                    artifact_ids.add(aid)
    artifacts: dict[str, Any] = {}
    for aid in sorted(artifact_ids):
        art = service.artifact(aid)
        if art is not None:
            artifacts[aid] = art

    return {
        "kind": "dossier-html-export",
        "format_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "snapshot": snap,
        "modules": modules,
        "evidence": evidence,
        "artifacts": artifacts,
    }


def render_snapshot_html(
    snap: dict[str, Any], service: DossierService, bundle_dir: Path
) -> str:
    from .service import DossierError

    js_path, css_path = bundle_dir / BUNDLE_JS, bundle_dir / BUNDLE_CSS
    missing = [p.name for p in (js_path, css_path) if not p.is_file()]
    if missing:
        raise DossierError(
            f"HTML 导出包未构建（{bundle_dir} 缺 {', '.join(missing)}）："
            "先运行 cd frontend && npm run build:export，"
            "或用 FA_EXPORT_BUNDLE_DIR 指向已有构建产物",
            status=503,
        )
    js = js_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")
    if "</script" in js.lower():
        raise DossierError(
            f"导出包 {BUNDLE_JS} 含 '</script'，无法安全内联（拒绝导出，不产出损坏文件）",
            status=500,
        )
    if "</style" in css.lower():
        raise DossierError(
            f"导出包 {BUNDLE_CSS} 含 '</style'，无法安全内联", status=500
        )

    data = collect_export_data(snap, service)
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # `</script>` 与 `<!--` 双重防护：JSON 字符串内容里不得出现裸 `<`
    data_json = data_json.replace("<", "\\u003c")

    entity = snap["entity"]
    ctx = snap["context"]
    name = entity.get("name") or entity["id"]
    title = html_mod.escape(f"{name} 研究档案（冻结导出 {ctx['as_of'][:10]}）")
    provenance = html_mod.escape(json.dumps({
        "format": "dossier-html-export",
        "entity": f"{entity['kind']}:{entity['id']}",
        "snapshot_id": ctx["snapshot_id"],
        "as_of": ctx["as_of"],
        "generated_at": ctx["generated_at"],
        "data_hash": snap.get("data_hash", ""),
        "exported_at": data["exported_at"],
    }, ensure_ascii=False))

    # 注意：不用 .format/f-string 包裹整段（css/js 内含大量花括号）
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="UTF-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>\n'
        f"<title>{title}</title>\n"
        f"<style>\n{css}\n</style>\n"
        "</head>\n<body>\n"
        f"<!-- finance-agent 冻结导出（自包含交互 HTML） {provenance} -->\n"
        '<div id="root"></div>\n'
        "<noscript>本导出是交互式 HTML（与在线档案页同一代码渲染），需要启用 JavaScript；"
        "数据与在线页面同源（DossierSnapshot 冻结读模型已内嵌）。</noscript>\n"
        "<script>\nwindow.__DOSSIER_EXPORT__ = "
        + data_json
        + ";\n</script>\n<script>\n"
        + js
        + "\n</script>\n</body>\n</html>\n"
    )
