# -*- coding: utf-8 -*-
"""U16 配置模块化：统一配置层行为测试。

验证点（详见 docs/system_design_u16.md）：
  * DEFAULT_LITELLM_MODEL 全系统唯一主模型常量（决策#7）。
  * 三级优先级：环境变量 ▶ config.yaml ▶ 代码默认。
  * 新增字段默认值与类型/范围校验（validate_structured 拒绝非法值；
    数值字段在解析期已被 parse_env_* 钳制到合法区间，time_slot_default 为原始
    字符串不参与钳制，因此其校验是可触发的）。
  * 代理去重：use_proxy / HTTP_PROXY 两条路径共用国内域名 NO_PROXY 排除集合，
    且 apply_proxy_settings 幂等、CI（GITHUB_ACTIONS=true）跳过。
  * config.yaml 解析：仅非敏感默认值，不得包含任何密钥类字段（红线）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import config as cfg_mod
from src.config import (
    DEFAULT_LITELLM_MODEL,
    apply_proxy_settings,
    get_config,
    load_config_yaml,
)

# 受 U16 影响的 env 名（测试间隔离用）。
_U16_ENV_VARS = (
    "LITELLM_MODEL", "REPAIR_MODEL", "REPAIR_TEMPERATURE",
    "LANGCHAIN_MODEL_NAME", "DEEPSEEK_BASE_URL", "STOCK_LIST",
    "REPAIR_MAX_ROUNDS", "JUDGE_ENABLED", "JUDGE_USE_LLM",
    "LLM_TEMPERATURE", "MAX_TOKENS", "MAX_OUTPUT_TOKENS",
    "USE_PROXY", "PROXY_HOST", "PROXY_PORT", "TIME_SLOT",
    "REPORTS_DIR", "DIST_DIR", "CONFIG_PROFILE",
    "GENERATION_BACKEND", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "GITHUB_ACTIONS", "http_proxy", "https_proxy", "no_proxy",
)


@pytest.fixture(autouse=True)
def _reset_config_state(monkeypatch):
    """每个测试前重置 config 单例 / yaml 缓存，禁用 .env 加载以保证确定性，
    并清理相关 env，避免测试相互污染。"""
    # 禁用 .env 加载：U16 新增字段均直接读 os.getenv，禁用 setup_env 后只受
    # 进程 env + config.yaml + 代码默认三层影响，测试完全确定性。
    monkeypatch.setattr(cfg_mod, "setup_env", lambda: None)
    # 单例 _instance 是 Config 的类属性，不是模块属性，用类方法重置。
    cfg_mod.Config.reset_instance()
    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_CACHE", None)
    for env_name in _U16_ENV_VARS:
        monkeypatch.delenv(env_name, raising=False)
    yield
    # 还原单例（monkeypatch 自动恢复 env 与 setup_env）。
    cfg_mod.Config.reset_instance()
    monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_CACHE", None)


def _rebuild():
    cfg_mod.Config.reset_instance()
    cfg_mod._CONFIG_YAML_CACHE = None
    return get_config()


# --------------------------------------------------------------------------- #
# 决策#7：唯一主模型常量
# --------------------------------------------------------------------------- #
def test_default_litellm_model_constant_unique():
    assert DEFAULT_LITELLM_MODEL == "deepseek/deepseek-v4-flash"
    # 全系统唯一来源：Config 字段默认值应等于该常量。
    c = _rebuild()
    assert c.default_litellm_model == DEFAULT_LITELLM_MODEL


# --------------------------------------------------------------------------- #
# 三级优先级：env ▶ yaml ▶ code
# --------------------------------------------------------------------------- #
def test_code_default_when_no_yaml_no_env(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load_config_yaml", lambda: {})
    c = _rebuild()
    assert c.generation_temperature == 0.7
    assert c.repair_max_rounds == 1
    assert c.max_output_tokens == 8192


def test_yaml_tier_merges_when_no_env(monkeypatch):
    fake_yaml = {
        "model": {"default_litellm_model": "gemini/x", "repair_temperature": 0.9},
        "thresholds": {"generation_temperature": 1.1, "repair_max_rounds": 2},
    }
    monkeypatch.setattr(cfg_mod, "load_config_yaml", lambda: fake_yaml)
    c = _rebuild()
    assert c.default_litellm_model == "gemini/x"            # yaml
    assert c.repair_temperature == 0.9                       # yaml
    assert c.generation_temperature == 1.1                   # yaml
    assert c.repair_max_rounds == 2                          # yaml


def test_env_overrides_yaml(monkeypatch):
    fake_yaml = {"thresholds": {"generation_temperature": 1.1}}
    monkeypatch.setattr(cfg_mod, "load_config_yaml", lambda: fake_yaml)
    monkeypatch.setenv("LLM_TEMPERATURE", "0.3")
    c = _rebuild()
    assert c.generation_temperature == 0.3                   # env 优先于 yaml(1.1)


def test_env_overrides_code_when_no_yaml(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load_config_yaml", lambda: {})
    monkeypatch.setenv("REPAIR_MAX_ROUNDS", "2")
    c = _rebuild()
    assert c.repair_max_rounds == 2


# --------------------------------------------------------------------------- #
# 新增字段默认值（与 config.yaml / 代码默认一致）
# --------------------------------------------------------------------------- #
def test_new_field_defaults_match_contract():
    c = _rebuild()
    assert c.repair_model == ""
    assert c.repair_temperature == 0.1
    assert c.langchain_model_name == "deepseek-chat"
    assert c.deepseek_base_url == "https://api.deepseek.com"
    assert c.default_stock_list == "600036,159915,603823,512400"
    assert c.repair_max_rounds == 1
    assert c.judge_enabled is True
    assert c.judge_use_llm is False
    assert c.generation_temperature == 0.7
    assert c.max_tokens == 2048
    assert c.max_output_tokens == 8192
    assert c.use_proxy is False
    assert c.proxy_host == "127.0.0.1"
    assert c.proxy_port == 10809
    assert c.time_slot_default == "1800"
    assert c.reports_dir == "reports"
    assert c.dist_dir == "web/dist"
    assert c.config_profile == ""
    assert c.profiles == {}


# --------------------------------------------------------------------------- #
# 数值字段在解析期被钳制到合法区间（parse_env_* 行为）
# --------------------------------------------------------------------------- #
def test_numeric_fields_clamped_into_range(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load_config_yaml", lambda: {})
    monkeypatch.setenv("LLM_TEMPERATURE", "5")          # > 2.0
    monkeypatch.setenv("REPAIR_TEMPERATURE", "-1")      # < 0.0
    monkeypatch.setenv("REPAIR_MAX_ROUNDS", "9")        # > 3
    monkeypatch.setenv("PROXY_PORT", "99999")          # > 65535
    c = _rebuild()
    assert c.generation_temperature == 2.0
    assert c.repair_temperature == 0.0
    assert c.repair_max_rounds == 3
    assert c.proxy_port == 65535
    # 钳制后不应产生任何 u16 错误。
    codes = {i.code for i in c.validate_structured() if i.severity == "error"}
    assert "u16_generation_temperature" not in codes
    assert "u16_repair_temperature" not in codes
    assert "u16_repair_max_rounds" not in codes
    assert "u16_proxy_port" not in codes


# --------------------------------------------------------------------------- #
# validate_structured：范围/格式校验（直接注入越界值以触发校验逻辑）
# --------------------------------------------------------------------------- #
def test_validate_rejects_out_of_range_fields(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load_config_yaml", lambda: {})
    c = _rebuild()
    c.generation_temperature = 5.0
    c.repair_temperature = 9.0
    c.repair_max_rounds = 5
    c.proxy_port = 99999
    codes = {i.code for i in c.validate_structured() if i.severity == "error"}
    assert "u16_generation_temperature" in codes
    assert "u16_repair_temperature" in codes
    assert "u16_repair_max_rounds" in codes
    assert "u16_proxy_port" in codes


def test_validate_rejects_bad_time_slot(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load_config_yaml", lambda: {})
    c = _rebuild()
    c.time_slot_default = "999"           # 非 4 位 HHMM
    codes = {i.code for i in c.validate_structured() if i.severity == "error"}
    assert "u16_time_slot_default" in codes
    # 合法值不产生该错误。
    c.time_slot_default = "1800"
    codes2 = {i.code for i in c.validate_structured() if i.severity == "error"}
    assert "u16_time_slot_default" not in codes2


# --------------------------------------------------------------------------- #
# 代理去重
# --------------------------------------------------------------------------- #
def test_apply_proxy_settings_use_proxy_sets_domestic_exclusion(monkeypatch):
    monkeypatch.setenv("USE_PROXY", "true")
    c = _rebuild()
    apply_proxy_settings(c)
    assert os.environ.get("http_proxy") == "http://127.0.0.1:10809"
    assert os.environ.get("https_proxy") == "http://127.0.0.1:10809"
    no_proxy = os.environ.get("NO_PROXY", "")
    assert "eastmoney.com" in no_proxy
    assert "127.0.0.1" in no_proxy
    # 幂等：重复调用结果一致，不重复追加。
    apply_proxy_settings(c)
    assert os.environ.get("http_proxy") == "http://127.0.0.1:10809"
    assert os.environ.get("NO_PROXY", "").count("eastmoney.com") == 1


def test_apply_proxy_settings_skips_in_ci(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("USE_PROXY", "true")
    monkeypatch.delenv("http_proxy", raising=False)
    c = _rebuild()
    apply_proxy_settings(c)
    assert os.environ.get("http_proxy") is None


def test_legacy_http_proxy_exclusion_applied_on_load(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://corp.proxy:8080")
    c = _rebuild()  # _load_from_env 内会按 HTTP_PROXY 写代理 + 国内域名排除
    assert os.environ.get("http_proxy") == "http://corp.proxy:8080"
    assert "eastmoney.com" in os.environ.get("NO_PROXY", "")


# --------------------------------------------------------------------------- #
# config.yaml 解析（红线：仅非敏感默认值，无密钥）
# --------------------------------------------------------------------------- #
def test_load_config_yaml_groups_present():
    data = load_config_yaml()
    assert isinstance(data, dict)
    for group in ("model", "holdings", "analysis_style", "thresholds", "profiles"):
        assert group in data


def test_config_yaml_has_no_secrets():
    data = load_config_yaml()

    def _walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                upper = str(key).upper()
                # 红线：仅拦截真实密钥字段。MAX_TOKENS / MAX_OUTPUT_TOKENS 等
                # 含 "TOKEN" 子串但属额度配置，非密钥，故只拦截以 _TOKEN 结尾或
                # 含显式密钥词的字段。
                assert "API_KEY" not in upper, f"疑似密钥字段: {path}/{key}"
                assert "SECRET" not in upper, f"疑似密钥字段: {path}/{key}"
                assert "PASSWORD" not in upper, f"疑似密钥字段: {path}/{key}"
                assert not upper.endswith("_TOKEN"), f"疑似密钥字段: {path}/{key}"
                _walk(value, f"{path}/{key}")

    _walk(data)
