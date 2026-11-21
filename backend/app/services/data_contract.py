"""数据契约领域服务：数据消费者与生产者之间的契约管理、Schema约束与SLA跟踪。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.freshness_breach import FreshnessBreachService
from app.services.stage_freshness import (
    CONCLUSION_BREACH,
    CONCLUSION_OK,
    STAGES,
    VERDICT_MISSING,
    VERDICT_TIMEOUT,
)
from app.store import store

breach_service = FreshnessBreachService()


class DataContractService(BaseModuleService):
    MODULE = "data_contract"
    ENTITY = "契约定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["契约编号", "数据集", "Schema约束", "SLA定义", "更新频率", "负责人", "消费者", "生效时间"]
    REQUIRED_FIELDS = ["契约编号", "数据集"]
    STATUS_ORDER = ["草稿", "已承诺", "已违约", "已终止"]
    ACTION_RULES = {"签署契约": "已承诺", "标记违约": "已违约", "终止契约": "已终止"}
    NEGATIVE_ACTIONS = ["标记违约", "终止契约"]

    def verify_contract_compliance(self, contract_id: int) -> dict[str, Any]:
        """验证数据契约的合规性，跨模块检查Schema一致性和数据新鲜度。

        跨模块数据流：
        - 读取 source_registry 校验数据源状态
        - 读取 semantic_model 校验Schema一致性
        - 读取 stage_freshness 重新计算分段时效、核对实际违约环节
        - 读取 freshness_mon 检查历史SLA告警
        - 读取 query_audit 检查消费记录
        - 把分段时效核验出的违约明细回写 freshness_breach 违约清单
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        dataset = contract.get("数据集", "")
        violations: list[str] = []

        # 检查数据源是否存在且可用
        sources = store.rows("source_registry")
        source_ok = False
        for s in sources:
            if s.get("源编号", "") in dataset or dataset in str(s.get("连接地址", "")):
                if s.get("status") == "已停用":
                    violations.append(f"数据源{s['源编号']}已停用")
                else:
                    source_ok = True

        if not source_ok:
            violations.append("未找到对应的可用数据源")

        # 检查Schema一致性
        models = store.rows("semantic_model")
        schema_ok = False
        for m in models:
            if m.get("status") == "已投产" and dataset in str(m.get("源表映射", "")):
                if m.get("字段别名", "") == contract.get("Schema约束", ""):
                    schema_ok = True
                else:
                    violations.append("Schema定义与语义模型不一致")
                break

        # 检查历史SLA新鲜度告警
        freshness_rows = store.rows("freshness_mon")
        freshness_ok = True
        for f in freshness_rows:
            if dataset in str(f.get("目标数据集", "")):
                if f.get("status") == "已告警":
                    freshness_ok = False
                    violations.append(f"新鲜度告警: {f.get('延迟阈值', 'unknown')}")

        # 重新计算分段时效：直接读校验记录，核对实际违约环节
        stage_detail = self._rebuild_stage_freshness(contract, dataset)
        if stage_detail["status"] in ("已违约", "证据不足"):
            for stage in stage_detail["breached_stages"]:
                violations.append(f"分段时效违约[{stage}]: {stage_detail['reasons'].get(stage, '')}")
        if stage_detail["missing_stages"]:
            violations.append(
                f"分段时效证据不足: {'、'.join(stage_detail['missing_stages'])}"
            )

        # 把分段时效的违约明细回写违约清单
        breach_writeback = self._write_breach_list(contract, dataset, stage_detail)

        # 检查消费记录
        audits = store.rows("query_audit")
        consumer = contract.get("消费者", "")
        consumption_records = [a for a in audits if consumer in str(a.get("操作用户", ""))]
        if not consumption_records:
            violations.append("消费者无实际查询记录")

        # 更新契约状态
        if violations:
            contract["status"] = "已违约"
            contract["pending"] = True
            contract["abnormal"] = True
        else:
            contract["status"] = "已承诺"
            contract["pending"] = False
            contract["abnormal"] = False

        return {
            "ok": True,
            "contract_id": contract_id,
            "contract_no": contract.get("契约编号", ""),
            "status": contract["status"],
            "violations": violations,
            "has_source": source_ok,
            "has_schema_match": schema_ok,
            "is_fresh": freshness_ok and stage_detail["status"] == "已达标",
            "consumption_records": len(consumption_records),
            "stage_freshness": stage_detail,
            "breach_list": breach_writeback,
        }

    def _rebuild_stage_freshness(self, contract: dict[str, Any], dataset: str) -> dict[str, Any]:
        """读取分段时效校验记录，重新判定本契约涉及批次的遵约明细。

        契约只认“已登记过最终口径”的批次：规则版本和检查起点都取自校验记录，
        不在契约层重新解析，避免一份违约结论混用两个版本。
        """
        records = [
            row for row in store.rows("stage_freshness")
            if str(row.get("目标数据集", "")).casefold() == dataset.casefold()
            and row.get("时效结论")
        ]
        batches: list[dict[str, Any]] = []
        breached_stages: list[str] = []
        missing_stages: list[str] = []
        reasons: dict[str, str] = {}
        settled_batches: set[str] = set()

        for row in records:
            conclusion = str(row.get("时效结论", ""))
            stage_rows = []
            for stage in STAGES:
                verdict = str(row.get(f"{stage}判定", "") or "未核验")
                stage_rows.append({
                    "环节": stage,
                    "判定": verdict,
                    "时间": row.get(f"{stage}时间", ""),
                    "耗时": row.get(f"{stage}耗时", ""),
                    "时限": row.get(f"{stage}时限", ""),
                    "原因": row.get(f"{stage}原因", ""),
                })
                if verdict == VERDICT_MISSING and stage not in missing_stages:
                    missing_stages.append(stage)
                if conclusion != CONCLUSION_BREACH:
                    continue
                if verdict == VERDICT_TIMEOUT:
                    if stage not in breached_stages:
                        breached_stages.append(stage)
                    reasons.setdefault(stage, str(row.get(f"{stage}原因", "")))

            if conclusion == CONCLUSION_OK:
                settled_batches.add(str(row.get("批次编号", "")))

            batch = {
                "批次编号": row.get("批次编号", ""),
                "校验编号": row.get("校验编号", ""),
                "规则版本": row.get("规则版本", ""),
                "检查起点": row.get("检查起点", ""),
                "时效结论": conclusion,
                "违约环节": row.get("违约环节", ""),
                "缺证据环节": row.get("缺证据环节", ""),
                "阶段": stage_rows,
            }
            batches.append(batch)

        if any(batch["时效结论"] == CONCLUSION_BREACH for batch in batches):
            status = "已违约"
        elif batches:
            status = "证据不足"
        else:
            status = "未登记"

        return {
            "status": status,
            "records": len(batches),
            "breached_stages": breached_stages,
            "missing_stages": missing_stages,
            "reasons": reasons,
            "settled_batches": settled_batches,
            "batches": batches,
        }

    def _write_breach_list(
        self,
        contract: dict[str, Any],
        dataset: str,
        stage_detail: dict[str, Any],
    ) -> dict[str, Any]:
        """把当前违约明细回写到违约清单。

        只回写“超时”环节；只有已给出达标结论的批次才消除旧条目，
        证据不足的批次保持原状，避免把未取证当成已恢复。
        """
        contract_no = str(contract.get("契约编号", ""))
        breaches = []
        for batch in stage_detail["batches"]:
            if batch["时效结论"] != CONCLUSION_BREACH:
                continue
            for stage_row in batch["阶段"]:
                if stage_row["判定"] != VERDICT_TIMEOUT:
                    continue
                breaches.append({
                    "批次编号": batch["批次编号"],
                    "目标数据集": dataset,
                    "违约环节": stage_row["环节"],
                    "规则版本": batch["规则版本"],
                    "检查起点": batch["检查起点"],
                    "结论": CONCLUSION_BREACH,
                    "原因": stage_row["原因"],
                })
        return breach_service.upsert_breaches(
            contract_no,
            breaches,
            settled_batches=stage_detail["settled_batches"],
        )

    def propagate_schema_change(self, contract_id: int, new_schema: str) -> dict[str, Any]:
        """当Schema变化时，通过契约将变更通知所有消费者。

        跨模块数据流：
        - 更新 data_contract Schema约束
        - 通知 related semantic_model 更新
        - 更新 stage_freshness 重新评估分段时效
        - 写入 query_audit
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        old_schema = contract.get("Schema约束", "")
        contract["Schema约束"] = new_schema
        contract["pending"] = True

        # 通知语义模型
        models = store.rows("semantic_model")
        for m in models:
            if contract.get("数据集", "") in str(m.get("源表映射", "")):
                m["字段别名"] = new_schema
                m["pending"] = True

        # 写入审计
        rows = store.rows("query_audit")
        audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        audit = {
            "id": audit_id,
            "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
            "查询语句": f"CONTRACT_SCHEMA_CHANGE contract={contract_id}",
            "执行耗时": "150ms",
            "扫描行数": "1",
            "返回行数": "1",
            "数据来源": "data_contract",
            "操作用户": "contract_manager",
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "正常",
            "pending": False,
            "abnormal": False,
        }
        rows.append(audit)

        return {
            "ok": True,
            "contract_id": contract_id,
            "old_schema": old_schema,
            "new_schema": new_schema,
        }