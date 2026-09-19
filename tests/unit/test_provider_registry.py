"""供应商注册表（M5）：单一事实源 + 与 _qualify_model/ops 表单的一致性。"""

from app.agents.provider import _qualify_model
from app.agents.provider_registry import PROVIDERS, ProviderSpec, get_provider, visible_options


def test_registry_values_unique():
    values = [spec.value for spec in PROVIDERS]
    assert len(values) == len(set(values))


def test_previously_drifted_provider_sets_are_unioned():
    """provider.py 与 dashboard 表单曾各硬编码一份且已漂移，此处钉住并集。"""
    values = {spec.value for spec in PROVIDERS}
    # 原 provider.py 的 LiteLLM 前缀表
    assert {"openai", "deepseek", "anthropic", "gemini", "mistral", "cohere", "openrouter"} <= values
    # 原 provider.py 的 OpenAI-compatible 集合
    assert {"omniroute", "vllm", "hosted_vllm", "lmstudio", "openai_compatible", "nvidia", "nvidia_nim"} <= values
    # 原 provider.py 的 local 硬编码 + 原表单的 direct
    assert {"local", "ollama", "direct"} <= values


def test_every_visible_option_is_qualifiable():
    for spec in visible_options():
        qualified = _qualify_model("bare-model", spec.value)
        if spec.kind == "direct":
            assert qualified == "bare-model"
        else:
            assert "/" in qualified, f"{spec.value} 无法映射到任何 LiteLLM 前缀"


def test_qualify_model_by_kind():
    assert _qualify_model("gpt-4o", "openai") == "openai/gpt-4o"
    assert _qualify_model("deepseek-chat", "deepseek") == "deepseek/deepseek-chat"
    assert _qualify_model("mistral-large", "mistral") == "mistral/mistral-large"
    # openai_compatible 与 local 都走 openai/ 直连前缀
    assert _qualify_model("m", "local") == "openai/m"
    assert _qualify_model("m", "nvidia_nim") == "openai/m"
    # 已带限定符的模型名透传；未知 provider 透传
    assert _qualify_model("ollama/qwen2.5", "local") == "ollama/qwen2.5"
    assert _qualify_model("m", "unknown-provider") == "m"


def test_hidden_specs_are_not_in_form_but_still_resolve():
    hidden = [spec for spec in PROVIDERS if spec.hidden]
    assert hidden, "mistral/cohere/vllm 等内部别名应保留 hidden 条目"
    visible_values = {spec.value for spec in visible_options()}
    for spec in hidden:
        assert spec.value not in visible_values
        assert get_provider(spec.value) is spec


def test_spec_frozen_and_defaults():
    spec = ProviderSpec("x", "X", "litellm", "x")
    assert spec.hidden is False
    assert spec.litellm_prefix == "x"
