"""分段时效校验、规则版本锁定与违约清单回写测试。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.data_contract import DataContractService
from app.services.freshness_breach import FreshnessBreachService
from app.services.stage_freshness import (
    CONCLUSION_BREACH,
    CONCLUSION_INSUFFICIENT,
    CONCLUSION_OK,
    STAGE_CACHE,
    STAGE_LAKE,
    STAGE_SOURCE,
    VERDICT_MISSING,
    VERDICT_OK,
    VERDICT_TIMEOUT,
    StageFreshnessService,
)

client = TestClient(app)


@pytest.fixture()
def svc():
    return StageFreshnessService()


def evidence(start: str, source: int, lake: int, cache: int) -> dict[str, str]:
    from datetime import datetime, timedelta

    base = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    return {
        STAGE_SOURCE: (base + timedelta(seconds=source)).strftime("%Y-%m-%d %H:%M:%S"),
        STAGE_LAKE: (base + timedelta(seconds=lake)).strftime("%Y-%m-%d %H:%M:%S"),
        STAGE_CACHE: (base + timedelta(seconds=cache)).strftime("%Y-%m-%d %H:%M:%S"),
    }


class TestStageJudgement:
    def test_each_stage_has_own_limit(self, svc):
        """同一耗时在不同阶段结论不同，说明不是共用同一阈值。"""
        start = "2026-11-21 00:00:00"
        result = svc.verify_batch(
            "T-LIMIT-1", "ds_limit", check_start=start,
            evidence=evidence(start, source=700, lake=700, cache=700),
            rule_version="v2",
        )
        assert result["ok"]
        assert result["breakdown"][0]["判定"] == VERDICT_TIMEOUT  # 来源同步 600s
        assert result["breakdown"][1]["判定"] == VERDICT_OK  # 湖仓扫描 1200s
        assert result["breakdown"][2]["判定"] == VERDICT_OK  # 缓存刷新 1800s

    def test_cache_success_cannot_offset_missing_source(self, svc):
        """缓存刷新达标不能抵掉来源同步缺证据。"""
        start = "2026-11-21 00:00:00"
        ev = evidence(start, source=0, lake=100, cache=200)
        ev[STAGE_SOURCE] = ""
        result = svc.verify_batch(
            "T-MISS-1", "ds_missing", check_start=start, evidence=ev, rule_version="v2",
        )
        assert result["conclusion"] == CONCLUSION_INSUFFICIENT
        assert result["breakdown"][0]["判定"] == VERDICT_MISSING
        assert result["breakdown"][2]["判定"] == VERDICT_OK
        assert STAGE_SOURCE in result["missing_stages"]
        assert STAGE_CACHE not in result["breached_stages"]

    def test_timeout_outranks_missing(self, svc):
        start = "2026-11-21 00:00:00"
        ev = evidence(start, source=9999, lake=100, cache=200)
        ev[STAGE_LAKE] = ""
        result = svc.verify_batch(
            "T-MIX-1", "ds_mix", check_start=start, evidence=ev, rule_version="v2",
        )
        assert result["conclusion"] == CONCLUSION_BREACH
        assert set(result["breached_stages"]) == {STAGE_SOURCE, STAGE_LAKE}

    def test_evidence_before_check_start_is_not_accepted(self, svc):
        start = "2026-11-21 00:00:00"
        result = svc.verify_batch(
            "T-EARLY-1", "ds_early", check_start=start,
            evidence={
                STAGE_SOURCE: "2026-11-20 23:50:00",
                STAGE_LAKE: "2026-11-21 00:10:00",
                STAGE_CACHE: "2026-11-21 00:20:00",
            },
            rule_version="v2",
        )
        assert result["breakdown"][0]["判定"] == VERDICT_MISSING


class TestVersionPinning:
    def test_historical_batch_keeps_old_thresholds(self, svc):
        """先按 v1 登记的批次，SLA 变更后重新提交仍按 v1 判。"""
        start = "2026-11-19 23:00:00"
        ev = evidence(start, source=700, lake=700, cache=700)
        first = svc.verify_batch("T-VER-1", "ds_ver", check_start=start, evidence=ev)
        assert first["version"] == "v1"
        assert first["conclusion"] == CONCLUSION_OK

        again = svc.verify_batch(
            "T-VER-1", "ds_ver", rule_version="v2", evidence=ev,
        )
        assert again["version"] == "v1"
        assert again["version_pinned"] is True
        assert again["requested_version_ignored"] is True
        assert again["thresholds"][STAGE_SOURCE] == "900s"
        assert again["conclusion"] == CONCLUSION_OK

    def test_boundary_switches_version_exactly_at_effective_time(self, svc):
        boundary = svc.check_rule_boundary(boundary_version="v2")
        before, at = boundary["points"]
        assert before["采用版本"] == "v1"
        assert at["采用版本"] == "v2"
        assert before["阶段时限"] != at["阶段时限"]

    def test_sla_change_mid_batch_has_two_calibers(self, svc):
        """同一批已开始同步、尚未刷新缓存时规则变更，两个起点各按自己版本。"""
        sim = svc.simulate_delivery(
            "T-SLA-1", "ds_sla", check_start="2026-11-21 00:00:00",
        )
        change = sim["sla_change"]
        assert change["适用"] is True
        calibers = {item["采用版本"] for item in change["对照口径"]}
        assert calibers == {"v1", "v2"}
        assert "只采用一个版本" in change["登记建议"]


class TestAlertCounting:
    def test_repeated_submission_counts_once(self, svc):
        start = "2026-11-21 00:00:00"
        ev = evidence(start, source=9999, lake=100, cache=200)
        first = svc.verify_batch("T-CNT-1", "ds_cnt", check_start=start, evidence=ev)
        assert first["new_alerts"] == [STAGE_SOURCE]
        assert first["counted_alerts"] == 1

        second = svc.verify_batch("T-CNT-1", "ds_cnt", evidence=ev)
        assert second["duplicate"] is True
        assert second["new_alerts"] == []
        assert second["counted_alerts"] == 1

    def test_distinct_stage_alert_is_counted_separately(self, svc):
        start = "2026-11-21 00:00:00"
        first = svc.verify_batch(
            "T-CNT-2", "ds_cnt2", check_start=start,
            evidence=evidence(start, source=9999, lake=100, cache=200),
        )
        assert first["counted_alerts"] == 1
        second = svc.verify_batch(
            "T-CNT-2", "ds_cnt2",
            evidence=evidence(start, source=9999, lake=100, cache=9999),
        )
        assert second["new_alerts"] == [STAGE_CACHE]
        assert second["counted_alerts"] == 2


class TestArrivalSimulation:
    def test_source_and_cache_order_reasons(self, svc):
        sim = svc.simulate_delivery(
            "T-ORDER-1", "ds_order",
            check_start="2026-11-21 00:00:00", source_offset=300,
            cache_offset=2400, lake_offset=900,
        )
        source_first, cache_first = sim["arrival_orders"]
        assert source_first["来源判定"] == VERDICT_OK
        assert source_first["缓存判定"] == VERDICT_TIMEOUT
        assert STAGE_CACHE in source_first["超时原因"]

        assert cache_first["来源判定"] == VERDICT_TIMEOUT
        assert cache_first["缓存判定"] == VERDICT_OK
        assert STAGE_SOURCE in cache_first["超时原因"]
        assert STAGE_CACHE not in cache_first["超时原因"]


class TestContractWriteback:
    def test_contract_recomputes_and_writes_breach_list(self):
        svc = StageFreshnessService()
        contract_svc = DataContractService()
        breach_svc = FreshnessBreachService()
        no = "DATA-T-WB"

        start = "2026-11-21 00:00:00"
        svc.verify_batch(
            "T-WB-1", "ds_writeback", check_start=start,
            evidence=evidence(start, source=9999, lake=100, cache=9999),
        )
        contract = {
            "id": 9901, "契约编号": no, "数据集": "ds_writeback",
            "Schema约束": "id,name", "消费者": "nobody", "status": "已承诺",
        }

        # 直接走内部重建与回写，避免依赖种子契约。
        detail = contract_svc._rebuild_stage_freshness(contract, "ds_writeback")
        assert detail["status"] == "已违约"
        assert set(detail["breached_stages"]) == {STAGE_SOURCE, STAGE_CACHE}
        writeback = contract_svc._write_breach_list(contract, "ds_writeback", detail)
        assert writeback["open_total"] == 2
        items = breach_svc.list_breaches(no)
        assert {item["违约环节"] for item in items} == {STAGE_SOURCE, STAGE_CACHE}
        assert all(item["规则版本"] == "v2" for item in items)
        assert all(item["检查起点"] == start for item in items)

        # 重复核验同一批次不新增条目
        again = contract_svc._write_breach_list(contract, "ds_writeback", detail)
        assert again["created"] == []
        assert len(breach_svc.list_breaches(no)) == 2

    def test_insufficient_evidence_does_not_clear_existing_breach(self):
        contract_svc = DataContractService()
        breach_svc = FreshnessBreachService()
        no = "DATA-T-INSUF"
        start = "2026-11-21 00:00:00"
        svc = StageFreshnessService()
        svc.verify_batch(
            "T-INSUF-1", "ds_insuf", check_start=start,
            evidence=evidence(start, source=9999, lake=100, cache=200),
        )
        contract = {"契约编号": no, "数据集": "ds_insuf"}
        detail = contract_svc._rebuild_stage_freshness(contract, "ds_insuf")
        contract_svc._write_breach_list(contract, "ds_insuf", detail)
        assert len(breach_svc.list_breaches(no)) == 1

        # 补交来源缺失的提交：只有缺证据，不能把历史超时违约草率消除
        svc.verify_batch("T-INSUF-1", "ds_insuf", evidence={STAGE_SOURCE: ""})
        detail2 = contract_svc._rebuild_stage_freshness(contract, "ds_insuf")
        writeback = contract_svc._write_breach_list(contract, "ds_insuf", detail2)
        assert writeback["cleared"] == []
        assert len(breach_svc.list_breaches(no)) == 1


class TestFreshnessApi:
    def test_rules_endpoint_lists_stage_limits(self):
        resp = client.get("/api/freshness/rules")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["stages"] == [STAGE_SOURCE, STAGE_LAKE, STAGE_CACHE]
        assert len(payload["versions"]) >= 2
        assert all("阶段时限" in version for version in payload["versions"])

    def test_verify_endpoint(self):
        resp = client.post("/api/freshness/verify", params={
            "batch_id": "T-API-1", "dataset": "ds_api",
            "check_start": "2026-11-21 00:00:00",
            "source_sync": "2026-11-21 00:05:00",
            "lake_scan": "2026-11-21 00:15:00",
            "cache_refresh": "2026-11-21 00:40:00",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["conclusion"] == CONCLUSION_BREACH
        assert data["version"] == "v2"

    def test_simulate_endpoint_covers_boundary(self):
        resp = client.post("/api/freshness/simulate", params={
            "batch_id": "T-API-2", "dataset": "ds_api2",
            "check_start": "2026-11-21 00:00:00",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["arrival_orders"]) == 2
        versions = [point["采用版本"] for point in data["rule_boundary"]["points"]]
        assert versions == ["v1", "v2"]

    def test_unknown_version_rejected(self):
        resp = client.post("/api/freshness/verify", params={
            "batch_id": "T-API-3", "dataset": "ds_api3", "rule_version": "v9",
        })
        assert resp.status_code == 400
