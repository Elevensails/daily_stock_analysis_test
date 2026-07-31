"""U3 事后校验 gate — LLM-as-judge / 结构化校验（防幻觉）。

对每份个股 / 大盘 / vibe 报告做内容级校验，拦截幻觉与红线违规：
  - 红线合规：禁止具体买卖建议、保收益 / 必涨必跌等绝对化承诺
  - 内部数字一致性：报告自相矛盾（如称「涨停」但同篇出现负收益）
  - 不可能数值：主板非 ST 单日涨跌幅超过 ±10% 且未标注例外
  - 未举证断言：无条件的绝对化断言（「一定 / 必然 / 100%」）
  - 关键数字与行情源核对（可选）：若提供 ``source_facts``，抽取报告关键数字比对
  - LLM-as-judge（可选）：提供 ``llm_judge`` 可调用对象时用模型做忠实度评分

输出 :class:`ValidationResult`（passed / score / reasons / checks）；
:func:`gate_report` 在判定不通过时把原因追加写入 ``logs/judge_rejects.jsonl``。

本模块**仅依赖标准库**，既能被 ``tests`` 直接导入，也能被 ``scripts/`` 下的
``emit_frontend_artifacts.py`` 在部署前统一调用。LLM judge 通过注入的
callable 解耦，默认不启用（离线、零成本），生产环境可接入 DeepSeek。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "CheckResult",
    "ValidationResult",
    "JudgeConfig",
    "Paragraph",
    "ViolationSegment",
    "split_paragraphs",
    "locate_violations",
    "validate",
    "gate_report",
    "append_reject_record",
]


@dataclass
class CheckResult:
    """单项校验结果。"""

    name: str
    passed: bool
    detail: str
    severity: str = "warn"  # "critical" | "warn"


@dataclass
class Paragraph:
    """段落切分结果（U3 修改策略：违规段定位的最小单元）。

    行号为 1-based 闭区间 ``[line_start, line_end]``，与编辑器行号一致。
    """

    index: int
    line_start: int
    line_end: int
    text: str


@dataclass
class ViolationSegment:
    """结构化违规段指针（U3 修改策略 §3.1 跨文件契约）。

    ``granularity`` ∈ ``paragraph | document``（``sentence`` 预留 P2-1）；
    ``document`` 级（``paragraph_index=None``）表示无法定位到具体段落
    （如 LLM judge 整体低分），此类段**不可被 safe_degrade 剥离**。
    """

    check: str
    severity: str
    reason: str
    quote: "str | None" = None
    granularity: str = "paragraph"
    paragraph_index: "int | None" = None
    line_start: "int | None" = None
    line_end: "int | None" = None

    def to_dict(self) -> dict:
        """输出 §3.1 契约 schema（location 嵌套结构）。"""
        return {
            "check": self.check,
            "severity": self.severity,
            "reason": self.reason,
            "quote": self.quote,
            "location": {
                "granularity": self.granularity,
                "paragraph_index": self.paragraph_index,
                "line_start": self.line_start,
                "line_end": self.line_end,
            },
        }


@dataclass
class ValidationResult:
    """一次校验的总体结果。"""

    passed: bool
    score: float  # 0..1，越高越可信
    reasons: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    # U3 修改策略：结构化违规段（fail 时由 locate_violations 填充；加法字段）
    violation_segments: list[ViolationSegment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "checks": [asdict(c) for c in self.checks],
            "violation_segments": [s.to_dict() for s in self.violation_segments],
        }


@dataclass
class JudgeConfig:
    """校验 gate 配置（阈值可配置）。"""

    enabled: bool = True
    min_score: float = 0.5  # score 低于此值判定 fail
    use_llm: bool = False  # 是否启用 LLM-as-judge（需注入 llm_judge callable）


# ---------------------------------------------------------------------------
# 启发式规则（无需联网，始终生效）
# ---------------------------------------------------------------------------

# 红线：具体买卖建议 + 价位 / 目标价，或收益 / 涨跌承诺。
_RED_LINE_PATTERNS: list[tuple[str, str]] = [
    (r"买入[\s\S]{0,14}?(目标价|止盈|挂单|\d+\.\d+\s*元)", "含具体买入建议与价位"),
    (r"卖出[\s\S]{0,14}?(目标价|止损|挂单|\d+\.\d+\s*元)", "含具体卖出建议与价位"),
    (
        r"(必然涨停|必定涨停|肯定涨停|100%\s*涨停|稳涨|必涨|包涨|稳赚|保收益| guaranteed|必跌|必定跌停|铁定)",
        "含保收益 / 必涨必跌等绝对化承诺",
    ),
    (r"(历史规律.*保证|保证.*收益|稳赚不赔|零风险|无风险)", "含收益保证类违规表述"),
]

# 涨停 / 跌停 标记
_LIMIT_UP_RE = re.compile(r"(涨停|封板|一字板|顶板)")
_LIMIT_DOWN_RE = re.compile(r"(跌停|打到跌停)")

# 涨跌幅数值（含正负号）
_PCT_RE = re.compile(r"([+\-]?\d{1,2}(?:\.\d+)?)\s*%")

# 例外板块（涨跌幅限制不同，不触发「不可能数值」）
_EXCEPTION_BOARD_RE = re.compile(r"(科创|创业|北交|ST|融|转债|新股|\bN[一-龥])")

# 无条件绝对化断言（软红线，降分不致命）
_UNGROUNDED_RE = re.compile(r"(一定涨|必然|100%\s*(会|获利|盈利)| guaranteed|绝对会涨|必定)")


def _check_red_lines(text: str) -> Optional[CheckResult]:
    for pat, msg in _RED_LINE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return CheckResult("red_line", False, msg, "critical")
    return CheckResult("red_line", True, "未检出红线违规", "critical")


def _check_internal_consistency(text: str) -> CheckResult:
    """称涨停 / 跌停但同篇出现相反方向的涨跌幅 → 自相矛盾。"""
    if _LIMIT_UP_RE.search(text):
        neg = re.search(r"-\d{1,2}(?:\.\d+)?\s*%", text)
        if neg:
            return CheckResult(
                "self_consistency",
                False,
                f"称涨停但出现负收益 {neg.group(0)}",
                "critical",
            )
    if _LIMIT_DOWN_RE.search(text):
        pos = re.search(r"([+\-]?\d{1,2}(?:\.\d+)?)\s*%", text)
        # 跌停股同篇出现明显正收益，视为矛盾
        if pos and float(pos.group(1)) > 0:
            return CheckResult(
                "self_consistency",
                False,
                f"称跌停但出现正收益 {pos.group(0)}",
                "critical",
            )
    return CheckResult("self_consistency", True, "无涨停 / 跌停方向自相矛盾", "warn")


def _check_impossible_value(text: str) -> CheckResult:
    """单日涨跌幅超过主板限制（±10%）且未标注例外板块 → 可疑。"""
    if _EXCEPTION_BOARD_RE.search(text):
        return CheckResult("impossible_move", True, "已标注例外板块，跳过不可能数值检查", "warn")
    for m in _PCT_RE.finditer(text):
        val = float(m.group(1))
        if abs(val) > 10.5:
            return CheckResult(
                "impossible_move",
                False,
                f"单日涨跌幅 {val}% 超主板上限且未标注例外板块",
                "warn",
            )
    return CheckResult("impossible_move", True, "无超范围涨跌幅", "warn")


def _check_ungrounded(text: str) -> CheckResult:
    if _UNGROUNDED_RE.search(text):
        return CheckResult("ungrounded", False, "含无条件绝对化断言（未举证）", "warn")
    return CheckResult("ungrounded", True, "无未举证绝对化断言", "warn")


def _check_numeric_source(text: str, source_facts: dict) -> Optional[CheckResult]:
    """关键数字与行情源核对（可选）。

    ``source_facts`` 由分析阶段提供，至少包含 ``is_limit_up`` / ``is_limit_down``
    之一。若报告声称涨停 / 跌停与行情源状态矛盾，判 fail。
    """
    is_up = source_facts.get("is_limit_up")
    is_down = source_facts.get("is_limit_down")
    if is_up is False and _LIMIT_UP_RE.search(text):
        return CheckResult(
            "numeric_source", False, "行情源显示未涨停，报告却称涨停", "critical"
        )
    if is_down is False and _LIMIT_DOWN_RE.search(text):
        return CheckResult(
            "numeric_source", False, "行情源显示未跌停，报告却称跌停", "critical"
        )
    # 价格核对：若提供 price，检查报告中「现价 / 收盘价」附近数字是否接近
    price = source_facts.get("price")
    if price is not None:
        m = re.search(r"(?:现价|收盘价|最新价)[^\d]{0,6}?([\d]+\.[\d]{2})", text)
        if m:
            claimed = float(m.group(1))
            if abs(claimed - float(price)) / float(price) > 0.02:  # 偏差 > 2%
                return CheckResult(
                    "numeric_source",
                    False,
                    f"报告价 {claimed} 与行情源 {price} 偏差超 2%",
                    "critical",
                )
    return CheckResult("numeric_source", True, "关键数字与行情源一致", "warn")


# ---------------------------------------------------------------------------
# U3 修改策略：段落切分 + 违规段定位（只加结构化定位层，判定逻辑零改动）
# ---------------------------------------------------------------------------

_QUOTE_MAX_LEN = 80  # quote 摘录上限（§3.1：≤80 字）


def split_paragraphs(text: str) -> "list[Paragraph]":
    """按空行把全文切分为段落，记录 1-based 行号区间。

    连续非空行归入同一段；纯空白行是段落分隔符。空文本返回空列表。
    """
    paragraphs: list[Paragraph] = []
    lines = text.split("\n")
    buf: list[str] = []
    start_line = 0
    for lineno, line in enumerate(lines, start=1):
        if line.strip():
            if not buf:
                start_line = lineno
            buf.append(line)
        else:
            if buf:
                paragraphs.append(Paragraph(
                    index=len(paragraphs),
                    line_start=start_line,
                    line_end=lineno - 1,
                    text="\n".join(buf),
                ))
                buf = []
    if buf:
        paragraphs.append(Paragraph(
            index=len(paragraphs),
            line_start=start_line,
            line_end=len(lines),
            text="\n".join(buf),
        ))
    return paragraphs


def _clip_quote(snippet: str) -> str:
    """把违规命中文本裁剪到 ``_QUOTE_MAX_LEN`` 字以内（单行化）。"""
    one_line = " ".join(snippet.split())
    return one_line[:_QUOTE_MAX_LEN]


def _locate_red_line(para: Paragraph) -> "ViolationSegment | None":
    for pat, msg in _RED_LINE_PATTERNS:
        m = re.search(pat, para.text, re.IGNORECASE)
        if m:
            return ViolationSegment(
                check="red_line", severity="critical", reason=msg,
                quote=_clip_quote(m.group(0)), granularity="paragraph",
                paragraph_index=para.index,
                line_start=para.line_start, line_end=para.line_end,
            )
    return None


def _locate_by_recheck(
    para: Paragraph,
    check_fn: "Callable[[str], Optional[CheckResult]]",
) -> "ViolationSegment | None":
    """对单段复跑同一检查函数；段内复现 fail 则产出 paragraph 级 segment。"""
    cr = check_fn(para.text)
    if cr is not None and not cr.passed:
        return ViolationSegment(
            check=cr.name, severity=cr.severity, reason=cr.detail,
            quote=_clip_quote(para.text), granularity="paragraph",
            paragraph_index=para.index,
            line_start=para.line_start, line_end=para.line_end,
        )
    return None


def locate_violations(
    text: str,
    checks: "list[CheckResult]",
    *,
    source_facts: Optional[dict] = None,
) -> "list[ViolationSegment]":
    """对已 fail 的检查项做段落级复扫定位，产出结构化违规段列表。

    双层策略（判定逻辑零改动）：全文级 pass/fail 结论由 :func:`validate`
    按现有逻辑得出；本函数仅对 **fail 的检查项** 逐段复跑同一规则，
    命中段 → ``paragraph`` 级 segment；段内均无法复现（如跨段矛盾、
    LLM judge 整体低分）→ 回退一条 ``document`` 级 segment，保证
    每个失败检查项至少产出 1 条 segment。
    """
    segments: list[ViolationSegment] = []
    paragraphs = split_paragraphs(text)
    for check in checks:
        if check.passed:
            continue
        located: list[ViolationSegment] = []
        for para in paragraphs:
            seg: "ViolationSegment | None" = None
            if check.name == "red_line":
                seg = _locate_red_line(para)
            elif check.name == "self_consistency":
                seg = _locate_by_recheck(para, _check_internal_consistency)
            elif check.name == "impossible_move":
                seg = _locate_by_recheck(para, _check_impossible_value)
            elif check.name == "ungrounded":
                seg = _locate_by_recheck(para, _check_ungrounded)
            elif check.name == "numeric_source" and source_facts:
                seg = _locate_by_recheck(
                    para, lambda t: _check_numeric_source(t, source_facts)
                )
            if seg is not None:
                located.append(seg)
        if located:
            segments.extend(located)
        else:
            # 定位不到（llm_judge / 跨段矛盾等）→ document 级 segment
            segments.append(ViolationSegment(
                check=check.name, severity=check.severity, reason=check.detail,
                quote=None, granularity="document",
                paragraph_index=None, line_start=None, line_end=None,
            ))
    return segments


def validate(
    text: str,
    *,
    source_facts: Optional[dict] = None,
    report_kind: str = "stock",
    config: Optional[JudgeConfig] = None,
    llm_judge: Optional[Callable[..., dict]] = None,
) -> ValidationResult:
    """对一份报告文本做结构化校验，返回 :class:`ValidationResult`。

    参数
    ----
    text: 报告全文（markdown / 纯文本均可）
    source_facts: 分析阶段可用的行情源关键数字（可选，用于数字核对）
    report_kind: ``stock`` / ``market`` / ``vibe``，仅用于日志与可读性
    config: :class:`JudgeConfig`，默认启用、阈值 0.5
    llm_judge: 可选的 LLM-as-judge callable，签名 ``(text, *, report_kind,
        source_facts) -> {"score": float, "reasons": [str]}``；调用失败自动降级
    """
    config = config or JudgeConfig()
    if not config.enabled:
        return ValidationResult(
            passed=True, score=1.0, reasons=["judge disabled"], checks=[]
        )

    checks: list[CheckResult] = []
    reasons: list[str] = []
    score = 1.0
    critical_failed = False

    # 1) 红线合规（致命）
    rl = _check_red_lines(text)
    checks.append(rl)
    if not rl.passed:
        reasons.append(f"[红线] {rl.detail}")
        critical_failed = True

    # 2) 内部数字一致性（致命）
    sc = _check_internal_consistency(text)
    checks.append(sc)
    if not sc.passed:
        reasons.append(f"[自相矛盾] {sc.detail}")
        critical_failed = True

    # 3) 不可能数值（软）
    iv = _check_impossible_value(text)
    checks.append(iv)
    if not iv.passed:
        reasons.append(f"[可疑数值] {iv.detail}")
        score -= 0.3

    # 4) 未举证断言（软）
    ug = _check_ungrounded(text)
    checks.append(ug)
    if not ug.passed:
        reasons.append(f"[未举证] {ug.detail}")
        score -= 0.2

    # 5) 关键数字与行情源核对（可选，致命）
    if source_facts:
        ns = _check_numeric_source(text, source_facts)
        checks.append(ns)
        if ns is not None and not ns.passed:
            reasons.append(f"[数字核对] {ns.detail}")
            critical_failed = True

    # 6) LLM-as-judge（可选）
    if config.use_llm and llm_judge is not None:
        try:
            jr = llm_judge(text, report_kind=report_kind, source_facts=source_facts) or {}
            jscore = float(jr.get("score", 1.0))
            score = min(score, jscore)
            for r in jr.get("reasons", []):
                reasons.append(f"[LLM] {r}")
            checks.append(
                CheckResult(
                    "llm_judge", jscore >= config.min_score, f"LLM 评分 {jscore:.2f}", "warn"
                )
            )
            if jscore < config.min_score:
                critical_failed = True
        except Exception as exc:  # 降级：judge 失败不应阻断发布流程
            checks.append(
                CheckResult("llm_judge", True, f"LLM judge 调用失败，降级启发式: {exc}", "warn")
            )

    # 致命项失败直接把分数压到 0，确保 fail 时 score 也低（便于 CI 阈值判断）
    if critical_failed:
        score = 0.0
    score = max(0.0, min(1.0, score))
    passed = (not critical_failed) and (score >= config.min_score)
    result = ValidationResult(
        passed=passed, score=score, reasons=reasons, checks=checks
    )
    # U3 修改策略：fail 时补充结构化违规段定位（加法层，判定结论不受影响）
    if not passed:
        try:
            result.violation_segments = locate_violations(
                text, checks, source_facts=source_facts
            )
        except Exception:
            # 定位层绝不反噬判定主流程（保底空列表）
            result.violation_segments = []
    return result


def gate_report(
    text: str,
    *,
    source_facts: Optional[dict] = None,
    report_kind: str = "stock",
    config: Optional[JudgeConfig] = None,
    log_path: str = "logs/judge_rejects.jsonl",
    context: Optional[dict] = None,
    llm_judge: Optional[Callable[..., dict]] = None,
    log: bool = True,
) -> ValidationResult:
    """校验一份报告；若不通过，把拒绝原因追加写入 ``log_path`` 并返回结果。

    参数
    ----
    log: 为 ``True``（默认）时保持原行为——判定不通过后写 jsonl；
        为 ``False`` 时仅做校验、不写日志（由调用方自行控制终态记录，
        例如 emit 接入 repair loop 后写带 repair 字段的终态记录）。

    返回 :class:`ValidationResult`，调用方据此决定是否发布（如 emit 阶段跳过）。
    """
    config = config or JudgeConfig()
    result = validate(
        text,
        source_facts=source_facts,
        report_kind=report_kind,
        config=config,
        llm_judge=llm_judge,
    )
    if not result.passed and log:
        _append_reject(log_path, report_kind, result, context)
    return result


def append_reject_record(
    log_path: str,
    report_kind: str,
    result: ValidationResult,
    context: Optional[dict] = None,
    **extra: object,
) -> None:
    """把一条拒绝/终态记录追加写入 jsonl（失败静默，不阻断主流程）。

    在原有 ``ts/kind/passed/score/reasons/checks/context`` 字段基础上，
    合并任意 ``**extra`` 字段（如 repair loop 的 ``repair_rounds`` /
    ``rewritten`` / ``final_action`` / ``repair_reasons``），供 emit 写
    带 repair 语义的终态记录。旧字段结构保持兼容。
    """
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": report_kind,
            "passed": result.passed,
            "score": round(result.score, 3),
            "reasons": result.reasons,
            "checks": [asdict(c) for c in result.checks],
            "context": context or {},
        }
        # 合并 repair 相关扩展字段（不覆盖上述核心字段）
        for k, v in extra.items():
            rec[k] = v
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # gate 日志失败绝不应影响主流程发布
        pass


def _append_reject(
    log_path: str,
    report_kind: str,
    result: ValidationResult,
    context: Optional[dict],
) -> None:
    """（内部兼容包装）把拒绝记录追加写入 jsonl；委托 :func:`append_reject_record`。"""
    append_reject_record(log_path, report_kind, result, context)
