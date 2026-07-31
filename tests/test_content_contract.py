# -*- coding: utf-8 -*-
"""内容契约 schema 一致性测试 —— U2 方案C 数据边界。

断言 web/mock/manifest.json（提交进仓库的假数据契约）与 web/src/dsa.ts 的
DsaManifest 接口字段结构一致：
  * manifest 的顶层 key 都应在 DsaManifest 接口中声明
  * DsaManifest 必须声明契约关键字段 schemaVersion / fragmentTypes / slots 等
  * slots 类型为 Record<string, SlotEntry>、fragmentTypes 为 string[]
  * mock manifest 的 fragmentTypes 与 slots[*].fragments 的 key 自洽
（emit 脚本产出的真实 manifest 字段结构由 test_emit_artifacts.py 覆盖。）
"""

from __future__ import annotations

import os
import re
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.join(_HERE, "..", "web")
_DSA_TS = os.path.join(_WEB_DIR, "src", "dsa.ts")
_MOCK_MANIFEST = os.path.join(_WEB_DIR, "mock", "manifest.json")


def _load_dsa_ts() -> str:
    with open(_DSA_TS, "r", encoding="utf-8") as fh:
        return fh.read()


def _interface_fields(ts_text: str) -> set:
    m = re.search(r"export interface DsaManifest\s*\{([\s\S]*?)\n\}", ts_text)
    assert m, "DsaManifest 接口未在 web/src/dsa.ts 中找到"
    block = m.group(1)
    return set(re.findall(r"^\s*([A-Za-z_][\w]*)\s*\??\s*:", block, flags=re.M))


def test_manifest_top_level_keys_declared_in_interface():
    """mock manifest 的每个顶层 key 都必须能在 DsaManifest 接口中找到对应声明。"""
    ts = _load_dsa_ts()
    fields = _interface_fields(ts)
    manifest = json.load(open(_MOCK_MANIFEST, "r", encoding="utf-8"))
    for key in manifest.keys():
        assert key in fields, f"manifest 顶层 key {key!r} 未在 DsaManifest 接口声明"


def test_dsa_interface_declares_contract_fields():
    """DsaManifest 接口必须声明契约关键字段。"""
    ts = _load_dsa_ts()
    fields = _interface_fields(ts)
    for required in ("schemaVersion", "fragmentTypes", "slots", "generatedAt", "site", "stats", "model"):
        assert required in fields, f"DsaManifest 缺少契约字段 {required!r}"


def test_interface_typing_for_slots_and_fragmenttypes():
    """slots 应为 Record<string, SlotEntry>，fragmentTypes 应为 string[]。"""
    ts = _load_dsa_ts()
    assert re.search(r"slots\?\s*:\s*Record<string,\s*SlotEntry>", ts), \
        "slots 字段应声明为 Record<string, SlotEntry>"
    assert re.search(r"fragmentTypes\?\s*:\s*string\[\]", ts), \
        "fragmentTypes 字段应声明为 string[]"


def test_fragment_types_consistency_in_mock():
    """mock manifest 的 fragmentTypes 与 slots[*].fragments 的 key 自洽。"""
    manifest = json.load(open(_MOCK_MANIFEST, "r", encoding="utf-8"))
    assert isinstance(manifest["fragmentTypes"], list)
    assert all(isinstance(t, str) for t in manifest["fragmentTypes"])
    ft = set(manifest["fragmentTypes"])

    for slot_code, slot in manifest["slots"].items():
        assert isinstance(slot, dict)
        assert isinstance(slot.get("fragments"), dict)
        for ftype, fname in slot["fragments"].items():
            assert ftype in ft, f"slot {slot_code} 含未登记的片段类型 {ftype!r}"
            if fname is not None:
                assert isinstance(fname, str) and fname.endswith(".html"), fname


def test_mock_manifest_field_shapes():
    """mock manifest 关键字段的数据形态（版本号/日期/站点 base）合法。"""
    manifest = json.load(open(_MOCK_MANIFEST, "r", encoding="utf-8"))
    assert manifest["schemaVersion"] == "1.0"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", manifest["generatedAt"])
    assert manifest["site"]["base"] == "/daily_stock_analysis_test/"
    assert set(manifest["stats"].keys()) == {
        "slotsPerDay", "holdings", "reportTypes", "monthlyCost",
    }
