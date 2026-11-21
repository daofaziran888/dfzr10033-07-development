"""分段时效校验领域服务：按业务交付阶段判定数据延迟，替代单一SLA阈值。

一段数据从来源同步、湖仓扫描到缓存刷新属于不同交付阶段，各阶段的时限和
证据来源都不同。本模块把“检查起点 + 规则版本 + 各阶段阈值”一次性登记到
同一条校验记录上，阶段结论彼此独立：缓存刷新成功不能抵掉来源同步缺失，
历史批次也不会被新规则追溯改判。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 交付阶段：来源同步 → 湖仓扫描 → 缓存刷新
STAGE_SOURCE = "来源同步"
STAGE_LAKE = "湖仓扫描"
STAGE_CACHE = "缓存刷新"
STAGES: tuple[str, ...] = (STAGE_SOURCE, STAGE_LAKE, STAGE_CACHE)

VERDICT_OK = "达标"
VERDICT_TIMEOUT = "超时"
VERDICT_MISSING = "证据缺失"

CONCLUSION_OK = "达标"
CONCLUSION_BREACH = "违约"
CONCLUSION_INSUFFICIENT = "证据不足"

STATUS_BY_CONCLUSION = {
    CONCLUSION_OK: "已达标",
    CONCLUSION_BREACH: "已违约",
    CONCLUSION_INSUFFICIENT: "证据不足",
}

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
_EMPTY_MARKERS = {"", "-", "--", "n/a", "na", "none", "null", "未知", "无"}

# 规则版本按生效时间排序；生效时刻本身按新版本处理（左闭右开）。
RULE_VERSIONS: list[dict[str, Any]] = [
    {
        "版本": "v1",
        "生效时间": "2026-11-01 00:00:00",
        "说明": "首版交付阶段时限，兼容历史批",
        "阶段时限": {STAGE_SOURCE: 900, STAGE_LAKE: 1800, STAGE_CACHE: 3600},
        "缺证据判定": {
            STAGE_SOURCE: "source_registry 缺少最近同步时间，视为来源未完成同步",
            STAGE_LAKE: "data_lake_read 缺少最后扫描时间，视为湖仓扫描未发生",
            STAGE_CACHE: "result_cache 缺少创建/刷新时间，视为缓存未刷新",
        },
    },
    {
        "版本": "v2",
        "生效时间": "2026-11-20 00:00:00",
        "说明": "收紧同步与扫描时限",
        "阶段时限": {STAGE_SOURCE: 600, STAGE_LAKE: 1200, STAGE_CACHE: 1800},
        "缺证据判定": {
            STAGE_SOURCE: "source_registry 缺少最近同步时间，视为来源未完成同步",
            STAGE_LAKE: "data_lake_read 缺少最后扫描时间，视为湖仓扫描未发生",
            STAGE_CACHE: "result_cache 缺少刷新时间，视为缓存未刷新",
        },
    },
]

RULE_BY_VERSION = {rule["版本"]: rule for rule in RULE_VERSIONS}


class StageFreshnessService(BaseModuleService):
    MODULE = "stage_freshness"
    ENTITY = "分段时效校验记录"
    OPERATION_TERM = "核验"
    LIST_FIELDS = [
        "校验编号", "批次编号", "目标数据集", "规则版本",
        "检查起点", "时效结论", "违约环节", "触发次数",
    ]
    REQUIRED_FIELDS = ["批次编号", "目标数据集"]
    STATUS_ORDER = ["待核验", "已达标", "已违约", "证据不足"]
    ACTION_RULES = {"确认达标": "已达标", "标记违约": "已违约"}
    NEGATIVE_ACTIONS = ["标记违约"]

    # ---------------------------------------------------------------- 规则

    def list_entries(self, *, keyword: str | None = None, **kwargs: Any):
        """关键词跨字段匹配：批次号和数据集都能检索到同一条记录。"""
        if not keyword:
            return super().list_entries(keyword=keyword, **kwargs)
        filters = dict(kwargs.get("filters") or {})
        kwargs["filters"] = filters
        items, total = super().list_entries(keyword=None, **{**kwargs, "page": 1, "size": 1_000_000})
        needle = keyword.casefold()
        matched = [
            row for row in items
            if any(needle in str(row.get(field, "")).casefold() for field in self.LIST_FIELDS)
        ]
        page = int(kwargs.get("page", 1))
        size = int(kwargs.get("size", 20))
        start = (page - 1) * size
        return matched[start:start + size], len(matched)

    def list_rule_versions(self) -> list[dict[str, Any]]:
        """输出可读的规则版本清单，供前端和接口核对各阶段阈值。"""
        return [_public_rule(rule) for rule in RULE_VERSIONS]

    # ------------------------------------------------------------ 登记入口

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """界面上登记一条校验记录：按批次+数据集去重后执行一次分段核验。"""
        batch_id = str(values.get("批次编号") or "").strip()
        dataset = str(values.get("目标数据集") or "").strip()
        missing = [field for field, value in (("批次编号", batch_id), ("目标数据集", dataset)) if not value]
        if missing:
            return None, missing

        evidence: dict[str, Any] = {}
        for stage, field in (
            (STAGE_SOURCE, "来源同步时间"),
            (STAGE_LAKE, "湖仓扫描时间"),
            (STAGE_CACHE, "缓存刷新时间"),
        ):
            if field in values:
                evidence[stage] = values.get(field) or ""

        result = self.verify_batch(
            batch_id,
            dataset,
            check_start=values.get("检查起点") or None,
            rule_version=values.get("规则版本") or None,
            evidence=evidence,
        )
        if not result["ok"]:
            return None, [result.get("message", "分段时效核验失败")]
        return result["record"], []

    # ---------------------------------------------------------- 核验主流程

    def verify_batch(
        self,
        batch_id: str,
        dataset: str,
        *,
        check_start: str | None = None,
        rule_version: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """核验一个批次的交付时效，并把最终口径登记到校验记录。

        - 检查起点优先取批次开始同步的时间；首次核验后即定格。
        - 规则版本按检查起点解析，生效时刻归新版本。
        - 同一批次再次提交时沿用已登记版本和阈值，只对新增阶段告警计数。
        """
        batch_id = str(batch_id or "").strip()
        dataset = str(dataset or "").strip()
        if not batch_id or not dataset:
            return {"ok": False, "message": "批次编号和目标数据集不能为空"}

        provided = {
            stage: value for stage, value in (evidence or {}).items()
            if value is not None
        }
        existing = self._find_batch(batch_id, dataset)

        pinned = existing is not None
        version_ignored = False
        version_replayed = False

        if existing is not None:
            # 历史批：沿用首次登记时的口径，版本、检查起点、阈值都不再漂移。
            version = str(existing.get("规则版本", ""))
            rule = RULE_BY_VERSION.get(version)
            limits = _limits_from_record(existing)
            missing_conditions = (
                dict(rule["缺证据判定"]) if rule else {
                    stage: "缺少阶段证据" for stage in STAGES
                }
            )
            effective_from = str(existing.get("规则生效时间", ""))
            start = parse_timestamp(existing.get("检查起点", "")) or _now()
            version_ignored = bool(rule_version) and str(rule_version) != version
            version_replayed = bool(rule_version) and str(rule_version) == version
            resolved_evidence = {
                stage: (
                    provided[stage]
                    if stage in provided
                    else existing.get(f"{stage}时间", "") or ""
                )
                for stage in STAGES
            }
        else:
            resolved_evidence = {
                stage: (
                    provided[stage]
                    if stage in provided
                    else _lookup_evidence(dataset, stage)
                )
                for stage in STAGES
            }
            if check_start:
                start = parse_timestamp(check_start)
                if start is None:
                    return {"ok": False, "message": f"检查起点格式无法解析：{check_start}"}
            else:
                start = _earliest_evidence(resolved_evidence) or _now()

            rule = _resolve_rule(start, rule_version)
            if rule is None:
                available = "、".join(sorted(RULE_BY_VERSION))
                return {"ok": False, "message": f"规则版本不存在：{rule_version}（可选：{available}）"}
            version = rule["版本"]
            limits = dict(rule["阶段时限"])
            missing_conditions = dict(rule["缺证据判定"])
            effective_from = rule["生效时间"]

        evaluation = self._evaluate(start, limits, missing_conditions, resolved_evidence)
        now_text = _format(_now())

        rows = store.rows(self.MODULE)
        if existing is None:
            entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            existing = {
                "id": entry_id,
                "校验编号": f"SEGM-{entry_id:04d}",
                "批次编号": batch_id,
                "目标数据集": dataset,
                "阶段告警": "",
                "触发次数": "0",
            }
            rows.append(existing)

        alert_counts = _parse_alert_counts(str(existing.get("阶段告警", "")))
        new_alerts = [stage for stage in evaluation["breached_stages"] if alert_counts.get(stage, 0) == 0]
        for stage in new_alerts:
            alert_counts[stage] = 1
        # 已发生过的阶段告警保留历史次数，重复提交不会再次累加。
        trigger_total = sum(alert_counts.values())

        existing.update({
            "目标数据集": dataset,
            "规则版本": version,
            "规则生效时间": effective_from,
            "检查起点": _format(start),
            "时效结论": evaluation["conclusion"],
            "违约环节": "、".join(evaluation["breached_stages"]),
            "缺证据环节": "、".join(evaluation["missing_stages"]),
            "超时原因": evaluation["reason_summary"],
            "阶段告警": "、".join(f"{stage}:{count}" for stage, count in sorted(alert_counts.items()) if count),
            "触发次数": str(trigger_total),
            "最后核验": now_text,
            "status": STATUS_BY_CONCLUSION[evaluation["conclusion"]],
            "pending": evaluation["conclusion"] != CONCLUSION_OK,
            "abnormal": evaluation["conclusion"] != CONCLUSION_OK,
        })

        for stage in STAGES:
            stage_result = evaluation["stage_results"][stage]
            existing[f"{stage}判定"] = stage_result["判定"]
            existing[f"{stage}耗时"] = (
                "" if stage_result["耗时"] is None else f"{stage_result['耗时']:.0f}s"
            )
            existing[f"{stage}原因"] = stage_result["原因"]
            existing[f"{stage}时间"] = stage_result["时间"]
            existing[f"{stage}时限"] = f"{limits[stage]}s"

        return {
            "ok": True,
            "record": existing,
            "dataset": dataset,
            "version": version,
            "check_start": _format(start),
            "thresholds": {stage: f"{limits[stage]}s" for stage in STAGES},
            "conclusion": evaluation["conclusion"],
            "breached_stages": evaluation["breached_stages"],
            "missing_stages": evaluation["missing_stages"],
            "timeout_reasons": evaluation["timeout_reasons"],
            "breakdown": evaluation["breakdown"],
            "duplicate": pinned,
            "version_pinned": pinned,
            "requested_version_ignored": version_ignored,
            "requested_version_replayed": version_replayed,
            "new_alerts": new_alerts,
            "alert_counted": bool(new_alerts),
            "counted_alerts": trigger_total,
        }

    # ------------------------------------------------------------ 模拟演练

    def simulate_delivery(
        self,
        batch_id: str,
        dataset: str,
        *,
        check_start: str | None = None,
        rule_version: str | None = None,
        source_offset: int = 300,
        lake_offset: int = 900,
        cache_offset: int = 2400,
        boundary_version: str | None = None,
    ) -> dict[str, Any]:
        """模拟来源与缓存的两种到达顺序，并在规则生效临界点复核同一批次。

        两种顺序各自独立按阶段判定，因此“缓存已刷新”不会掩盖“来源未到”。
        演练结果只用于核对口径，不写入校验记录。
        """
        start = parse_timestamp(check_start) if check_start else _now()
        if start is None:
            return {"ok": False, "message": f"检查起点格式无法解析：{check_start}"}

        rule = _resolve_rule(start, rule_version)
        if rule is None:
            return {"ok": False, "message": f"规则版本不存在：{rule_version}"}

        orders = [
            (
                "来源先到、缓存后到",
                {STAGE_SOURCE: source_offset, STAGE_LAKE: lake_offset, STAGE_CACHE: cache_offset},
            ),
            (
                "缓存先到、来源后到",
                {STAGE_SOURCE: cache_offset, STAGE_LAKE: lake_offset, STAGE_CACHE: source_offset},
            ),
        ]
        order_results = []
        for label, offsets in orders:
            evidence = {stage: _format(start + timedelta(seconds=offsets[stage])) for stage in STAGES}
            preview = self._preview(start, rule, evidence)
            order_results.append({
                "顺序": label,
                "批次编号": batch_id,
                "偏移秒": offsets,
                "来源判定": preview["stage_results"][STAGE_SOURCE]["判定"],
                "缓存判定": preview["stage_results"][STAGE_CACHE]["判定"],
                "超时原因": preview["timeout_reasons"],
                "缺证据原因": preview["missing_reasons"],
                "结论": preview["conclusion"],
                "breakdown": preview["breakdown"],
            })

        boundary = self._boundary_check(start, boundary_version)
        return {
            "ok": True,
            "dataset": dataset,
            "check_start": _format(start),
            "version": rule["版本"],
            "arrival_orders": order_results,
            "rule_boundary": boundary,
            "sla_change": self._cross_version_case(
                start, source_offset=source_offset, lake_offset=lake_offset,
                cache_offset=cache_offset,
            ),
        }

    def _cross_version_case(
        self,
        start: datetime,
        *,
        source_offset: int,
        lake_offset: int,
        cache_offset: int,
    ) -> dict[str, Any]:
        """同一批已开始同步、尚未刷新缓存时SLA变更：两个起点对应两套口径。

        这里只做对照，不落记录；真正核验时以登记的检查起点为准，一份结论
        只采用一个版本。
        """
        latest = RULE_VERSIONS[-1]
        boundary = parse_timestamp(latest["生效时间"])
        if boundary is None:
            return {"适用": False, "原因": "规则缺少生效时间"}
        # 让批次在规则生效前开始同步、缓存刷新落在生效之后。
        started = boundary - timedelta(seconds=max(60, cache_offset - 600))
        evidence = {
            STAGE_SOURCE: _format(started + timedelta(seconds=source_offset)),
            STAGE_LAKE: _format(started + timedelta(seconds=lake_offset)),
            STAGE_CACHE: _format(boundary + timedelta(seconds=120)),
        }
        old_start = _resolve_rule(started)
        new_start = _resolve_rule(boundary)
        views = []
        for label, point, rule in (
            ("按开始同步时间（变更前）", started, old_start),
            ("按缓存刷新时间（变更后）", boundary, new_start),
        ):
            preview = self._preview(point, rule, evidence)
            views.append({
                "口径": label,
                "检查起点": preview["check_start"],
                "采用版本": preview["version"],
                "阶段时限": preview["thresholds"],
                "结论": preview["conclusion"],
                "违约环节": preview["breached_stages"],
                "超时原因": preview["timeout_reasons"],
            })
        return {
            "适用": True,
            "规则变更时间": latest["生效时间"],
            "证据": evidence,
            "对照口径": views,
            "登记建议": (
                "以校验记录中登记的检查起点为准；同一批次只采用一个版本，"
                "不得按结论好坏在两个版本间切换。"
            ),
        }

    def check_rule_boundary(
        self,
        *,
        boundary_version: str | None = None,
        source_offset: int = 700,
        lake_offset: int = 1300,
        cache_offset: int = 1900,
    ) -> dict[str, Any]:
        """单独核对规则生效临界点：生效前1秒归旧版本，生效时刻归新版本。"""
        return self._boundary_check(
            _now(),
            boundary_version,
            offsets={
                STAGE_SOURCE: source_offset,
                STAGE_LAKE: lake_offset,
                STAGE_CACHE: cache_offset,
            },
        )

    # ---------------------------------------------------------------- 内部

    def _find_batch(self, batch_id: str, dataset: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (
                str(row.get("批次编号", "")).casefold() == batch_id.casefold()
                and str(row.get("目标数据集", "")).casefold() == dataset.casefold()
            ):
                return row
        return None

    def _evaluate(
        self,
        start: datetime,
        limits: dict[str, int],
        missing_conditions: dict[str, str],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        stage_results: dict[str, dict[str, Any]] = {}
        breach_reasons: dict[str, str] = {}
        for stage in STAGES:
            raw_time = evidence.get(stage, "")
            stage_results[stage] = _judge_stage(
                stage,
                raw_time,
                start,
                int(limits.get(stage, 0)),
                missing_conditions.get(stage, "缺少阶段证据"),
            )
            if stage_results[stage]["判定"] != VERDICT_OK:
                breach_reasons[stage] = stage_results[stage]["原因"]

        breached = [stage for stage in STAGES if stage_results[stage]["判定"] != VERDICT_OK]
        missing = [stage for stage in STAGES if stage_results[stage]["判定"] == VERDICT_MISSING]
        timeouts = [stage for stage in STAGES if stage_results[stage]["判定"] == VERDICT_TIMEOUT]

        # 有超时即违约；只有缺证据时单列为“证据不足”，但都不算达标。
        if timeouts:
            conclusion = CONCLUSION_BREACH
        elif missing:
            conclusion = CONCLUSION_INSUFFICIENT
        else:
            conclusion = CONCLUSION_OK

        return {
            "stage_results": stage_results,
            "breached_stages": breached,
            "timeout_stages": timeouts,
            "missing_stages": missing,
            "reasons": breach_reasons,
            "missing_reasons": {
                stage: breach_reasons[stage] for stage in missing
            },
            "timeout_reasons": {
                stage: breach_reasons[stage] for stage in timeouts
            },
            "reason_summary": "；".join(breach_reasons[stage] for stage in timeouts),
            "conclusion": conclusion,
            "breakdown": [
                {
                    "环节": stage,
                    "判定": stage_results[stage]["判定"],
                    "时间": stage_results[stage]["时间"],
                    "耗时": stage_results[stage]["耗时"],
                    "时限秒数": int(limits.get(stage, 0)),
                    "原因": stage_results[stage]["原因"],
                }
                for stage in STAGES
            ],
        }

    def _preview(
        self,
        start: datetime,
        rule: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        evaluation = self._evaluate(
            start,
            dict(rule["阶段时限"]),
            dict(rule["缺证据判定"]),
            evidence,
        )
        return {
            "check_start": _format(start),
            "version": rule["版本"],
            "thresholds": {stage: f"{rule['阶段时限'][stage]}s" for stage in STAGES},
            **evaluation,
        }

    def _boundary_check(
        self,
        reference: datetime,
        boundary_version: str | None = None,
        *,
        offsets: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        rule = RULE_BY_VERSION.get(boundary_version) if boundary_version else RULE_VERSIONS[-1]
        if rule is None:
            return {"ok": False, "message": f"规则版本不存在：{boundary_version}"}
        boundary = parse_timestamp(rule["生效时间"])
        offsets = offsets or {STAGE_SOURCE: 700, STAGE_LAKE: 1300, STAGE_CACHE: 1900}
        points = []
        for label, point in (
            ("生效前1秒", boundary - timedelta(seconds=1)),
            ("生效时刻", boundary),
        ):
            # 每个核对点按自己的检查起点解析版本，才能看出临界点两侧口径不同。
            point_rule = _resolve_rule(point)
            evidence = {stage: _format(point + timedelta(seconds=offsets[stage])) for stage in STAGES}
            preview = self._preview(point, point_rule, evidence)
            points.append({
                "核对点": label,
                "检查起点": preview["check_start"],
                "采用版本": preview["version"],
                "阶段时限": preview["thresholds"],
                "结论": preview["conclusion"],
                "违约环节": preview["breached_stages"],
                "超时原因": preview["timeout_reasons"],
                "缺证据原因": preview["missing_reasons"],
                "breakdown": preview["breakdown"],
            })
        return {
            "边界版本": rule["版本"],
            "生效时间": rule["生效时间"],
            "批次参照时间": _format(reference),
            "points": points,
        }

# ------------------------------------------------------------------ 工具函数


def parse_timestamp(value: Any) -> datetime | None:
    """解析常见时间字符串；空值和占位符一律视为无证据。"""
    if value is None:
        return None
    text = str(value).strip()
    if text.casefold() in _EMPTY_MARKERS:
        return None
    text = text.replace("T", " ").split(".")[0].strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _format(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _judge_stage(
    stage: str,
    raw_time: Any,
    start: datetime,
    limit_seconds: int,
    missing_condition: str,
) -> dict[str, Any]:
    moment = parse_timestamp(raw_time)
    if moment is None:
        return {
            "环节": stage,
            "时间": str(raw_time or ""),
            "耗时": None,
            "判定": VERDICT_MISSING,
            "原因": f"{stage}缺证据：{missing_condition}",
        }

    elapsed = (moment - start).total_seconds()
    if elapsed < 0:
        return {
            "环节": stage,
            "时间": _format(moment),
            "耗时": elapsed,
            "判定": VERDICT_MISSING,
            "原因": f"{stage}证据时间早于检查起点，不能作为本批次交付证据",
        }

    if elapsed > limit_seconds:
        return {
            "环节": stage,
            "时间": _format(moment),
            "耗时": elapsed,
            "判定": VERDICT_TIMEOUT,
            "原因": (
                f"{stage}耗时{elapsed:.0f}s，超过时限{limit_seconds}s"
                f"（超出{elapsed - limit_seconds:.0f}s）"
            ),
        }

    return {
        "环节": stage,
        "时间": _format(moment),
        "耗时": elapsed,
        "判定": VERDICT_OK,
        "原因": "",
    }


def _resolve_rule(moment: datetime, version: str | None = None) -> dict[str, Any] | None:
    if version:
        return RULE_BY_VERSION.get(version)
    selected = RULE_VERSIONS[0]
    for rule in RULE_VERSIONS:
        effective = parse_timestamp(rule["生效时间"])
        if effective is not None and effective <= moment:
            selected = rule
        else:
            break
    return selected


def _limits_from_record(row: dict[str, Any]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for stage in STAGES:
        text = str(row.get(f"{stage}时限", "")).strip()
        match = re.match(r"(\d+)", text)
        limits[stage] = int(match.group(1)) if match else 0
    return limits


def _parse_alert_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for part in re.split(r"[、,;；]", text):
        part = part.strip()
        if not part or ":" not in part:
            continue
        stage, _, number = part.rpartition(":")
        try:
            counts[stage.strip()] = int(number.strip())
        except ValueError:
            continue
    return counts


def _earliest_evidence(evidence: dict[str, Any]) -> datetime | None:
    moments = [parse_timestamp(value) for value in evidence.values()]
    moments = [moment for moment in moments if moment is not None]
    return min(moments) if moments else None


def _lookup_evidence(dataset: str, stage: str) -> str:
    """从各模块已有数据中取目标数据集在该阶段的最近完成时间。"""
    if stage == STAGE_SOURCE:
        return _latest_time(
            "source_registry",
            dataset,
            time_field="最近同步",
            match_fields=("源编号", "连接地址", "数据库名"),
        )
    if stage == STAGE_LAKE:
        return _latest_time(
            "data_lake_read",
            dataset,
            time_field="最后扫描",
            match_fields=("路径编号", "存储位置"),
        )
    return _latest_time(
        "result_cache",
        dataset,
        time_field="创建时间",
        match_fields=("条目编号", "查询摘要"),
    )


def _latest_time(
    module: str,
    dataset: str,
    *,
    time_field: str,
    match_fields: tuple[str, ...],
) -> str:
    matched = [
        row for row in store.rows(module)
        if _row_matches(dataset, row, match_fields)
    ]
    best: tuple[datetime, str] = (datetime.min, "")
    for row in matched:
        raw = row.get(time_field, "")
        moment = parse_timestamp(raw)
        if moment is not None and moment >= best[0]:
            best = (moment, _format(moment))
    return best[1]


def _row_matches(dataset: str, row: dict[str, Any], fields: tuple[str, ...]) -> bool:
    needle = str(dataset or "").strip().casefold()
    if not needle:
        return False
    for field in fields:
        value = str(row.get(field, "")).strip().casefold()
        if value and (needle in value or value in needle):
            return True
    return False


def _public_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "版本": rule["版本"],
        "生效时间": rule["生效时间"],
        "说明": rule.get("说明", ""),
        "阶段时限": {
            stage: f"{rule['阶段时限'][stage]}s" for stage in STAGES
        },
        "缺证据判定": dict(rule["缺证据判定"]),
    }
