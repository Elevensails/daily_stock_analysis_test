"""U3 防幻觉闭环 — repair loop（事后自动修复 + 安全降级发布）。

拦截（gate fail）的报告不直接丢弃，而是进入修复循环：
  1. 用 **最新一轮** ``reasons`` + ``violation_segments``（结构化违规段指针）
     构造**定向修订** prompt（未标红段逐字保留）；
  2. 注入的 ``llm_call`` 定向修订报告（兼容 ``(system, user)`` 两参 mock 与
     ``(system, user, *, model, temperature)`` 真实签名，``inspect`` 探测透传）；
  3. 对修订文本跑**完整** ``validate()``（同首轮 config / llm_judge，不绕过）；
  4. 通过 → ``emit``；仍 fail → 用本轮 validate 的**最新** reasons/segments
     进入下一轮（最多 ``max_rounds``，硬上限 3）；
  5. 轮数耗尽仍 fail：若 ``safe_degrade_enabled`` → 尝试
     :func:`src.core.degrade.assemble_degraded`（剥标红段 + 占位标记 + 复检），
     成功 → ``emit_degraded``；失败或开关关 → ``reject``（与旧行为一致）；
  6. ``llm_call`` 抛错 → 先尽力 degrade（有可用 segments 且开关开），
     否则安全降级 ``reject``（绝不抛到调用方）。

``final_action`` 三终态枚举的**唯一定义点**（共享约定 #1）：
:data:`FINAL_EMIT` / :data:`FINAL_EMIT_DEGRADED` / :data:`FINAL_REJECT`。

本模块**仅依赖标准库 + src.core.validator / src.core.degrade**，不 import
analyzer，便于单测用 mock 注入 ``llm_call``，零网络、零重型依赖。
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.core.degrade import assemble_degraded
from src.core.prompts_repair import REPAIR_SYSTEM, REPAIR_USER_TEMPLATE
from src.core.validator import JudgeConfig, ViolationSegment, validate

__all__ = [
    "FINAL_EMIT",
    "FINAL_EMIT_DEGRADED",
    "FINAL_REJECT",
    "RepairResult",
    "repair_report",
]

# final_action 三终态枚举（唯一定义点；持久化为字符串字面量）
FINAL_EMIT: str = "emit"
FINAL_EMIT_DEGRADED: str = "emit_degraded"
FINAL_REJECT: str = "reject"


@dataclass
class RepairResult:
    """一次 repair loop 的结果。"""

    passed: bool            # 最终文本是否通过完整 gate（emit/emit_degraded=True）
    final_text: str         # emit=修正稿；emit_degraded=降级稿；reject=原始文本
    rounds: int             # 实际 rewrite 次数（首轮即通过=0）
    rewritten: bool         # 是否真正发起过模型重写（rounds>0 或曾尝试调用）
    final_action: str       # FINAL_EMIT | FINAL_EMIT_DEGRADED | FINAL_REJECT
    repair_reasons: list    # 喂给模型的首轮违规原因（便于回查）
    last_reasons: list = field(default_factory=list)       # 末轮 validate 的最新原因
    degraded_segments: list = field(default_factory=list)  # 降级剥离段（dict 子集）


def _segments_block(segments: "list | None") -> str:
    """把结构化违规段渲染为 prompt 可读块（段号 + 原文引用 + 原因）。"""
    if not segments:
        return "-（无段级定位，请依据校验反馈整体复核并修正违规处）"
    lines: list[str] = []
    for seg in segments:
        if isinstance(seg, ViolationSegment):
            sd = seg.to_dict()
        elif isinstance(seg, dict):
            sd = seg
        else:
            continue
        loc = sd.get("location") or {}
        para_idx = loc.get("paragraph_index", sd.get("paragraph_index"))
        granularity = loc.get("granularity", sd.get("granularity", "document"))
        check = sd.get("check", "")
        reason = sd.get("reason", "")
        quote = sd.get("quote")
        if granularity == "paragraph" and para_idx is not None:
            head = f"- 第 {para_idx} 段（0 起始段号，check={check}）：{reason}"
        else:
            head = f"- 全文级（check={check}）：{reason}"
        if quote:
            head += f"\n  原文引用：「{quote}」"
        lines.append(head)
    return "\n".join(lines) if lines else "-（无段级定位，请整体复核合规）"


def _build_rewrite_prompt(
    text: str,
    reasons: list,
    segments: "list | None",
    source_facts: Optional[dict],
    report_kind: str = "stock",
) -> "tuple[str, str]":
    """构造 (system, user) 定向修订 prompt（段指针 + grounding 约束）。

    使用字符串替换而非 ``str.format``，避免正文/反馈中的 ``{`` ``}`` 触发格式化错误。
    """
    reasons_text = "\n".join(f"- {r}" for r in (reasons or [])) or "- （无具体反馈，请整体复核合规）"
    if source_facts:
        facts_text = json.dumps(source_facts, ensure_ascii=False, indent=2)
    else:
        facts_text = "（无，请仅依据校验反馈修正违规处，禁止编造数字）"

    user = (
        REPAIR_USER_TEMPLATE
        .replace("{report_kind}", report_kind)
        .replace("{reasons}", reasons_text)
        .replace("{violation_segments_block}", _segments_block(segments))
        .replace("{source_facts_block}", facts_text)
        .replace("{original_text}", text)
    )
    return REPAIR_SYSTEM, user


def _call_llm_compat(
    llm_call: Callable,
    system: str,
    user: str,
    *,
    model: Optional[str] = None,
    temperature: float = 0.1,
) -> str:
    """兼容调用注入的 llm_call（P1-1：REPAIR_TEMPERATURE 真正透传）。

    用 ``inspect.signature`` 探测 ``llm_call`` 是否接受 ``temperature`` /
    ``model`` 关键字（含 ``**kwargs``），接受则透传；否则退回
    ``(system, user)`` 两参调用——兼容既有测试的 ``fake_llm(system, user)``
    mock，19 个既有用例不改仍过（共享约定 #6）。
    """
    kwargs: dict = {}
    try:
        params = inspect.signature(llm_call).parameters
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if "temperature" in params or has_var_kw:
            kwargs["temperature"] = temperature
        if model is not None and ("model" in params or has_var_kw):
            kwargs["model"] = model
    except (TypeError, ValueError):
        # 签名不可探测（C 扩展 / 特殊 callable）→ 保守两参调用
        kwargs = {}
    if kwargs:
        return llm_call(system, user, **kwargs)
    return llm_call(system, user)


def _try_degrade(
    current: str,
    latest_segments: list,
    *,
    source_facts: Optional[dict],
    report_kind: str,
    cfg: JudgeConfig,
    rounds: int,
    rewritten: bool,
    repair_reasons: list,
    latest_reasons: list,
    original_text: str,
) -> RepairResult:
    """轮耗尽 / llm 异常后的安全降级尝试；不可降级则回退 reject。"""
    try:
        dres = assemble_degraded(
            current,
            latest_segments,
            source_facts=source_facts,
            report_kind=report_kind,
            config=cfg,
        )
    except Exception:
        dres = None
    if dres is not None and dres.ok:
        return RepairResult(
            passed=True,  # 降级稿已通过完整 validate 复检
            final_text=dres.degraded_text,
            rounds=rounds,
            rewritten=rewritten,
            final_action=FINAL_EMIT_DEGRADED,
            repair_reasons=repair_reasons,
            last_reasons=latest_reasons,
            degraded_segments=list(dres.removed_segments),
        )
    # 不可降级 → 现行 reject（原文回传，永不 emit 不合格版）
    return RepairResult(
        passed=False,
        final_text=original_text,
        rounds=rounds,
        rewritten=rewritten,
        final_action=FINAL_REJECT,
        repair_reasons=repair_reasons,
        last_reasons=latest_reasons,
    )


def repair_report(
    text: str,
    *,
    reasons: list,
    violation_segments: "list | None" = None,
    source_facts: Optional[dict] = None,
    report_kind: str = "stock",
    model: Optional[str] = None,
    max_rounds: int = 2,
    llm_call: Callable,
    config: Optional[JudgeConfig] = None,
    llm_judge: Optional[Callable[..., dict]] = None,
    temperature: float = 0.1,
    safe_degrade_enabled: bool = True,
) -> RepairResult:
    """fail → 定向修订 → 再校验循环；耗尽后可选安全降级发布。

    任意 ``llm_call`` 抛错即安全降级（尽力 degrade 后 reject），绝不抛到调用方。

    参数
    ----
    text: 首轮 gate 判定 fail 的报告全文
    reasons: 首轮 ``ValidationResult.reasons``（首轮 prompt 用；后续每轮用最新）
    violation_segments: 首轮 gate 的结构化违规段（P0-1；缺省用内部预检结果）
    source_facts: 行情源侧车（可选，缺失即退化，仅靠 reasons+segments 自纠）
    report_kind: stock | market | vibe（用于日志与可读）
    model: 重写模型（默认由 llm_call 内部决定，一般 = REPAIR_MODEL/LITELLM_MODEL）
    max_rounds: 重写轮数（硬上限 3；默认由调用方传 cfg.repair_max_rounds）
    llm_call: 注入的重写 callable；兼容 (s,u) 与 (s,u,*,model,temperature)
    config: 与首轮相同，保证完整 gate 不绕过
    llm_judge: 可选 LLM judge（P1-2 预留）
    temperature: 重写温度（P1-1：本次真正透传给 llm_call；默认 0.1）
    safe_degrade_enabled: P0-7 开关；False 时轮耗尽行为与旧版逐字段一致（reject）
    """
    cfg = config or JudgeConfig()
    reasons_list = list(reasons) if reasons else []

    # 防御：若原文已通过完整 gate（极端情况），直接 emit，不进 loop（AC3）
    pre = validate(
        text,
        source_facts=source_facts,
        report_kind=report_kind,
        config=cfg,
        llm_judge=llm_judge,
    )
    if pre.passed:
        return RepairResult(
            passed=True,
            final_text=text,
            rounds=0,
            rewritten=False,
            final_action=FINAL_EMIT,
            repair_reasons=reasons_list,
            last_reasons=list(pre.reasons),
        )

    # 最新反馈通道（P0-3）：首轮用入参（缺省回退内部预检结果），此后每轮刷新
    latest_reasons: list = reasons_list or list(pre.reasons)
    latest_segments: list = (
        list(violation_segments) if violation_segments else list(pre.violation_segments)
    )

    rounds = 0
    current = text
    for i in range(min(int(max_rounds), 3)):
        try:
            system, user = _build_rewrite_prompt(
                current, latest_reasons, latest_segments, source_facts, report_kind
            )
            rewritten_text = _call_llm_compat(
                llm_call, system, user, model=model, temperature=temperature
            )
        except Exception:
            # 模型不可用：尽力保底交付——有可用 segments 且开关开 → 先试 degrade
            if safe_degrade_enabled and latest_segments:
                return _try_degrade(
                    current, latest_segments,
                    source_facts=source_facts, report_kind=report_kind, cfg=cfg,
                    rounds=i, rewritten=True,
                    repair_reasons=reasons_list, latest_reasons=latest_reasons,
                    original_text=text,
                )
            # 安全降级：立即 reject，绝不抛到调用方（AC5）
            return RepairResult(
                passed=False,
                final_text=text,
                rounds=i,
                rewritten=True,
                final_action=FINAL_REJECT,
                repair_reasons=reasons_list,
                last_reasons=latest_reasons,
            )

        # 对修订文本跑完整 gate（同首轮 config / llm_judge，不绕过任何 check）
        res = validate(
            rewritten_text,
            source_facts=source_facts,
            report_kind=report_kind,
            config=cfg,
            llm_judge=llm_judge,
        )
        if res.passed:
            return RepairResult(
                passed=True,
                final_text=rewritten_text,
                rounds=i + 1,
                rewritten=True,
                final_action=FINAL_EMIT,
                repair_reasons=reasons_list,
                last_reasons=list(res.reasons),
            )
        # 仍 fail：刷新最新反馈（P0-3），本轮修订稿作为下一轮输入（渐进修复）
        latest_reasons = list(res.reasons)
        latest_segments = list(res.violation_segments)
        current = rewritten_text
        rounds = i + 1

    # 轮数耗尽仍 fail：开关开 → 安全降级尝试；否则/失败 → 现行 reject
    if safe_degrade_enabled:
        return _try_degrade(
            current, latest_segments,
            source_facts=source_facts, report_kind=report_kind, cfg=cfg,
            rounds=rounds, rewritten=True,
            repair_reasons=reasons_list, latest_reasons=latest_reasons,
            original_text=text,
        )
    return RepairResult(
        passed=False,
        final_text=text,
        rounds=rounds,
        rewritten=True,
        final_action=FINAL_REJECT,
        repair_reasons=reasons_list,
        last_reasons=latest_reasons,
    )
