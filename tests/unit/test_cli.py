"""L2 CLI 测试：真实装配路径必须有覆盖（RCA 规矩 2）。

- research --mock：真实装配 + 脚本化 LLM 边界，全跑通
- research 无 provider 且非 mock：快速失败 + 可操作报错（不静默）
- serve：子进程真实启动 → /api/sessions 200 → 终止
"""

import json
import subprocess
import sys
import time
import urllib.request

from finance_agent.cli import main


def test_cli_research_mock_runs(tmp_path, capsys):
    rc = main(["research", "--ticker", "AAA", "--mock", "--data-dir", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stop_reason"] in {"converged", "stalled", "budget"}
    assert out["data_dir"] == str(tmp_path)


def test_cli_research_real_without_provider_fails_actionably(tmp_path, capsys, monkeypatch):
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})
    # 钉住 provider 解析接缝：只看 env（隔离本机真实 pi 配置，防误打付费 API）
    from finance_agent.llm.router import LLMRouter

    monkeypatch.setattr("finance_agent.cli._router", lambda: LLMRouter.from_env())
    rc = main(["research", "--ticker", "AAA", "--data-dir", str(tmp_path)])
    assert rc == 2  # 非零退出码（SystemExit 由 __main__ 层转换）
    err = capsys.readouterr().err
    assert ".env" in err and "OPENAI_API_KEY" in err  # 可操作指引，不是 traceback


def test_cli_serve_subprocess_smoke(tmp_path):
    """真实启动：子进程起服务 → 探活 → 终止。"""
    port = 18931
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "finance_agent", "serve",
            "--no-open", "--no-build",
            "--port", str(port),
            "--data-dir", str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        ok = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=1) as r:
                    ok = r.status == 200
                    break
            except Exception:
                time.sleep(0.3)
        assert ok, "serve 子进程 15s 内未就绪"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert proc.returncode is not None
