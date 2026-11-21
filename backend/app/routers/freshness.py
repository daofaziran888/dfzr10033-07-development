"""分段时效与契约遵约接口：按交付阶段核验、规则边界复核、违约清单查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.data_contract import DataContractService
from app.services.freshness_breach import FreshnessBreachService
from app.services.stage_freshness import StageFreshnessService, STAGES

router = APIRouter(prefix="/api/freshness", tags=["分段时效"])

stage_svc = StageFreshnessService()
contract_svc = DataContractService()
breach_svc = FreshnessBreachService()


@router.get("/rules")
def list_rules() -> dict[str, Any]:
    """分段时效规则版本：每阶段时限和缺证据判定条件。"""
    return {"ok": True, "stages": list(STAGES), "versions": stage_svc.list_rule_versions()}


@router.post("/verify")
def verify_batch(
    batch_id: str = Query(...), dataset: str = Query(...),
    check_start: str | None = Query(default=None),
    rule_version: str | None = Query(default=None),
    source_sync: str | None = Query(default=None),
    lake_scan: str | None = Query(default=None),
    cache_refresh: str | None = Query(default=None),
) -> dict[str, Any]:
    """核验批次交付时效：分阶段判定、登记最终口径、按阶段计一次告警。"""
    result = stage_svc.verify_batch(
        batch_id, dataset,
        check_start=check_start,
        rule_version=rule_version,
        evidence={STAGES[0]: source_sync, STAGES[1]: lake_scan, STAGES[2]: cache_refresh},
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/simulate")
def simulate(
    batch_id: str = Query(...), dataset: str = Query(...),
    check_start: str | None = Query(default=None),
    rule_version: str | None = Query(default=None),
    source_offset: int = Query(default=300),
    lake_offset: int = Query(default=900),
    cache_offset: int = Query(default=2400),
    boundary_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """模拟来源与缓存的先后到达，并复核规则生效临界点。"""
    result = stage_svc.simulate_delivery(
        batch_id, dataset,
        check_start=check_start,
        rule_version=rule_version,
        source_offset=source_offset,
        lake_offset=lake_offset,
        cache_offset=cache_offset,
        boundary_version=boundary_version,
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/rule-boundary")
def rule_boundary(boundary_version: str | None = Query(default=None)) -> dict[str, Any]:
    """核对规则生效临界点：生效前1秒归旧版本、生效时刻归新版本。"""
    return stage_svc.check_rule_boundary(boundary_version=boundary_version)


@router.post("/contracts/{contract_id}/verify")
def verify_contract(contract_id: int) -> dict[str, Any]:
    """契约遵约核验：重算分段时效明细，并把违约环节回写违约清单。"""
    result = contract_svc.verify_contract_compliance(contract_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.get("/contracts/{contract_no}/breaches")
def contract_breaches(contract_no: str) -> dict[str, Any]:
    """按契约查看违约清单，每条结论都带规则版本和检查起点。"""
    items = breach_svc.list_breaches(contract_no)
    return {"ok": True, "contract_no": contract_no, "items": items, "total": len(items)}
