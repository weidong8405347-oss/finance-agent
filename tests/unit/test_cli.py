"""L2 CLI 测试：真实装配路径必须有覆盖（RCA 规矩 2）。

- research --mock：真实装配 + 脚本化 LLM 边界，全跑通
- research 无 provider 且非 mock：快速失败 + 可操作报错（不静默）
- serve：子进程真实启动 → /api/sessions 200 → 终止
"""

import json
import os
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


def test_dotenv_proxy_injected_when_env_absent(tmp_path, monkeypatch):
    from finance_agent.cli import _apply_dotenv_proxy

    (tmp_path / ".env").write_text("HTTPS_PROXY=http://127.0.0.1:7897\nHTTP_PROXY=http://127.0.0.1:7897\n")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    injected = _apply_dotenv_proxy(tmp_path / ".env")
    assert injected == ["HTTPS_PROXY", "HTTP_PROXY"]
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7897"


def test_dotenv_proxy_does_not_override_explicit_env(tmp_path, monkeypatch):
    from finance_agent.cli import _apply_dotenv_proxy

    (tmp_path / ".env").write_text("HTTPS_PROXY=http://127.0.0.1:7897\n")
    monkeypatch.setenv("HTTPS_PROXY", "http://explicit:1")  # 显式环境变量优先
    injected = _apply_dotenv_proxy(tmp_path / ".env")
    assert injected == []
    assert os.environ["HTTPS_PROXY"] == "http://explicit:1"


def test_dotenv_proxy_no_file_no_injection(tmp_path, monkeypatch):
    from finance_agent.cli import _apply_dotenv_proxy

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    injected = _apply_dotenv_proxy(tmp_path / ".env")  # 文件不存在
    assert injected == []
    assert "HTTPS_PROXY" not in os.environ


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


def test_router_prefers_data_dir_config(tmp_path, monkeypatch):
    """provider 配置优先级（§4.4）：data_dir/llm-providers.json 存在即优先于 pi/.env。"""
    import json as _json

    from finance_agent.cli import _router

    (tmp_path / "llm-providers.json").write_text(_json.dumps({
        "providers": {
            "local": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-x", "models": ["m1"]}
        },
        "default_provider": "local",
        "role_map": {"research": "local:m1"},
    }))
    router = _router(tmp_path)
    assert router.get("research").spec.base_url == "http://127.0.0.1:9/v1"

    # 热生效：改文件后下一次 _router() 即见（无需重启）
    (tmp_path / "llm-providers.json").write_text(_json.dumps({
        "providers": {
            "local": {"base_url": "http://127.0.0.1:8/v1", "api_key": "sk-x", "models": ["m2"]}
        },
        "default_provider": "local",
        "role_map": {"research": "local:m2"},
    }))
    router2 = _router(tmp_path)
    assert router2.get("research").spec.model == "m2"


def test_router_config_fail_closed_actionable(tmp_path):
    """自有配置损坏 → fail-closed 且报错可读（不静默回落 pi——那会让人以为新配置生效了）。"""
    from finance_agent.cli import _router
    from finance_agent.llm.router import ProviderConfigError

    (tmp_path / "llm-providers.json").write_text("{broken")
    try:
        _router(tmp_path)
        raise AssertionError("应抛 ProviderConfigError")
    except ProviderConfigError as e:
        assert "llm-providers.json" in str(e)
