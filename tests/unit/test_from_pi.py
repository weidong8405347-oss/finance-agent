"""LLMRouter.from_pi：直接复用 pi 的 provider 配置（~/.pi/agent/）作为单一真相源。"""

import json

import pytest

from finance_agent.llm.router import LLMRouter, ProviderConfigError


def make_pi_dir(tmp_path):
    pi = tmp_path / ".pi" / "agent"
    pi.mkdir(parents=True)
    (pi / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "novita-gpt": {
                        "api": "openai-completions",
                        "baseUrl": "https://api.novita.ai/openai/v1",
                        "models": [{"id": "pa/gpt-5.6-sol"}],
                    },
                    "dashscope": {
                        "api": "openai-completions",
                        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "models": [
                            {"id": "kimi-k3"},
                            {"id": "ZHIPU/GLM-5.3"},
                            {"id": "deepseek-v4-pro-0813"},
                        ],
                    },
                    "novita-claude": {
                        "api": "anthropic-messages",  # 非 OpenAI 协议 → 跳过
                        "baseUrl": "https://api.novita.ai/anthropic",
                        "models": [{"id": "anthropic/claude-opus-5"}],
                    },
                }
            }
        )
    )
    (pi / "auth.json").write_text(
        json.dumps(
            {
                "novita-gpt": {"type": "api_key", "key": "sk-novita"},
                "dashscope": {"type": "api_key", "key": "sk-dash"},
                "novita-claude": {"type": "api_key", "key": "sk-claude"},
            }
        )
    )
    return pi


def test_from_pi_loads_openai_compatible_providers(tmp_path):
    router = LLMRouter.from_pi(pi_dir=make_pi_dir(tmp_path), default_provider="novita-gpt")

    gpt = router.get("research")
    assert gpt.spec.base_url == "https://api.novita.ai/openai/v1"
    assert gpt.spec.model == "pa/gpt-5.6-sol"
    assert gpt.spec.api_key == "sk-novita"


def test_from_pi_multi_model_provider_exposes_aliases(tmp_path):
    router = LLMRouter.from_pi(pi_dir=make_pi_dir(tmp_path), default_provider="novita-gpt")
    # dashscope 多模型 → 每个模型一个别名键
    assert router.get("fast", )  # fast 角色存在（role_map 默认含 fast→dashscope:kimi-k3）
    kimi = router.get("fast")
    assert kimi.spec.model == "kimi-k3"
    assert kimi.spec.api_key == "sk-dash"
    glm = router.get("dashscope:ZHIPU/GLM-5.3")
    assert glm.spec.model == "ZHIPU/GLM-5.3"


def test_from_pi_skips_non_openai_protocol(tmp_path):
    router = LLMRouter.from_pi(pi_dir=make_pi_dir(tmp_path), default_provider="novita-gpt")
    # anthropic-messages 协议的 provider 不被收录（fail-closed，不会误用）
    assert "novita-claude" not in router.providers()
    assert "novita-gpt" in router.providers()


def test_from_pi_missing_dir_fail_closed(tmp_path):
    router = LLMRouter.from_pi(pi_dir=tmp_path / "nonexistent", default_provider="novita-gpt")
    assert router.providers() == []
    with pytest.raises(ProviderConfigError):
        router.get("research")
