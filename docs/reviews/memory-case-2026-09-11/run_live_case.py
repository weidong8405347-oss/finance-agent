"""Real research/profile case through the application's API, without mock boundaries.

An isolated data directory preserves production dossiers. Automatic follow-up wake
is disabled as in run_sentinel.py, so each frozen request runs exactly once.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient
from finance_agent.api.app import create_app
from finance_agent.cli import _apply_dotenv_proxy, build_orchestrator

DATA = ROOT / "data/reviews/memory-case-2026-09-11"
OUTPUT = Path(__file__).resolve().parent
COMMANDS = {
    "industry": "/research industry:memory-semiconductors 存储行业12至24个月投资研究，截止2026-09-11，区分HBM和通用DRAM和NAND及HDD，比较美光MU、海力士SKHY及000660、闪迪SNDK、铠侠285A、三星005930的供需周期和相对投资价值，给出来源、估值前提、主要反证和美股方案 --depth=standard",
    "micron": "/profile MU 未来12至24个月中等风险投资，截止2026-09-11，核验最新已公布业绩和股价；深究HBM与DRAM/NAND结构、价格与bit出货、正常化利润、资本开支和自由现金流、客户预付款、与海力士及闪迪的相对估值、情景回报和论点失效条件 --depth=deep",
    "hynix": "/profile SKHY 海力士12至24个月投资研究，截止2026-09-11，核验SKHY与韩国000660的ADS比率及汇率和交易主体，最新已公布业绩、剔除非经营收益后的盈利、HBM优势、资本开支和FCF、相对MU估值及风险 --depth=standard",
    "sandisk": "/profile SNDK 闪迪12至24个月投资研究，截止2026-09-11，核验最新财务和拆分后业务边界，NAND与企业SSD收入和利润驱动，JV现金流及客户预付款调整后的FCF、债务和正常化估值，与MU及铠侠比较投资价值和风险 --depth=standard",
}


def main():
    _apply_dotenv_proxy()
    orch = build_orchestrator(DATA)
    orch["command_runner"].set_wake(None)
    app = create_app(
        kb=orch["kb"], events=orch["events"], decisions=orch["decisions"].decisions,
        evals_dir=orch["evals_dir"], chat_service=orch["chat_service"],
        command_runner=orch["command_runner"], approvals=orch["approvals"],
        knowledge_dir=orch["knowledge_dir"], reports_dir=orch["reports_dir"],
        capabilities_info=orch["capabilities_info"], data_dir=DATA,
        metrics=orch["metrics"], dossier_service=orch["dossier_service"],
        calculation_service=orch["calculations"],
    )
    client = TestClient(app)
    manifest = {"started_at": datetime.now(UTC).isoformat(), "data_dir": str(DATA),
                "api_transport": "in-process TestClient; real orchestrator, LLM and gateway",
                "wake_disabled": True, "commands": {},
                "capabilities": client.get("/api/capabilities").json()}
    suffix = datetime.now(UTC).strftime("%H%M%S")
    for name, command in COMMANDS.items():
        session = f"memory-case-{name}-{suffix}"
        response = client.post("/api/chat", json={"session_id": session, "message": command})
        response.raise_for_status()
        manifest["commands"][name] = {"command": command, **response.json()}
        print(json.dumps({"started": name, **response.json()}, ensure_ascii=False), flush=True)
    path = OUTPUT / "live-case-results.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    t0 = time.monotonic()
    finished = set()
    last_seq = 0
    while len(finished) < len(COMMANDS):
        time.sleep(5)
        for name, request in manifest["commands"].items():
            events = client.get(f"/api/sessions/{request['run_id']}/events").json()
            dones = [e for e in events if e.get("type") == "command/done"
                     and e.get("payload", {}).get("command_id") == request["command_id"]]
            if dones and name not in finished:
                finished.add(name)
                request["done"] = dones[-1]
                request["elapsed_seconds"] = round(time.monotonic()-t0, 1)
                print(json.dumps({"finished": name, "result": dones[-1]}, ensure_ascii=False), flush=True)
        rows = orch["events"]._conn.execute(
            "SELECT seq, run_id, type, payload, ts FROM events WHERE seq > ? ORDER BY seq", (last_seq,)
        ).fetchall()
        if rows:
            last_seq = rows[-1][0]
            wanted = {"research/assessment", "research/budget", "step_agent/end",
                      "research/artifact_created", "dossier/published", "research/error"}
            manifest["signals"] = manifest.get("signals", []) + [
                {"seq": r[0], "run_id": r[1], "type": r[2], "payload": json.loads(r[3]), "ts": r[4]}
                for r in rows if r[2] in wanted]
        manifest["elapsed_seconds"] = round(time.monotonic()-t0, 1)
        manifest["finished"] = sorted(finished)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["entities"] = client.get("/api/knowledge/entities").json()
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Saved {path}", flush=True)


if __name__ == "__main__":
    main()
