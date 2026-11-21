"""数据时效违约清单：承接分段时效校验结论，按契约+批次+环节去重。

清单只记录“最终口径”下确实违约的环节：规则版本和检查起点随结论一起落库，
后续重新核验只更新或消除同一键的记录，不会产生重复告警。证据不足的批次
不动清单，避免把“没取证”误记成“已违约”或把历史违约草率消除。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FreshnessBreachService(BaseModuleService):
    MODULE = "freshness_breach"
    ENTITY = "违约清单条目"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "违约编号", "契约编号", "批次编号", "目标数据集",
        "违约环节", "规则版本", "检查起点", "结论",
    ]
    REQUIRED_FIELDS = ["契约编号", "批次编号", "违约环节"]
    STATUS_ORDER = ["待处理", "已确认", "已消除"]
    ACTION_RULES = {"确认违约": "已确认", "标记消除": "已消除"}
    NEGATIVE_ACTIONS = ["确认违约"]

    def list_entries(self, *, keyword: str | None = None, **kwargs: Any):
        """关键词跨字段匹配：违约编号、契约编号、批次号都能检索到同一条目。"""
        if not keyword:
            return super().list_entries(keyword=keyword, **kwargs)
        kwargs["filters"] = dict(kwargs.get("filters") or {})
        items, _ = super().list_entries(keyword=None, **{**kwargs, "page": 1, "size": 1_000_000})
        needle = keyword.casefold()
        matched = [
            row for row in items
            if any(needle in str(row.get(field, "")).casefold() for field in self.LIST_FIELDS)
        ]
        page = int(kwargs.get("page", 1))
        size = int(kwargs.get("size", 20))
        start = (page - 1) * size
        return matched[start:start + size], len(matched)

    def upsert_breaches(
        self,
        contract_no: str,
        breaches: list[dict[str, Any]],
        *,
        settled_batches: set[str] | None = None,
    ) -> dict[str, Any]:
        """把一次契约核验的违约明细写回清单，按契约+批次+环节保持唯一。

        settled_batches 是本次已能给出确定结论（达标）的批次，只有这些批次
        的旧条目会被消除；证据不足的批次维持原状。
        """
        rows = store.rows(self.MODULE)
        existing = [row for row in rows if str(row.get("契约编号", "")) == str(contract_no)]
        current_keys = {
            (str(item.get("批次编号", "")), str(item.get("违约环节", "")))
            for item in breaches
        }
        settled = settled_batches or set()

        cleared: list[str] = []
        for row in existing:
            key = (str(row.get("批次编号", "")), str(row.get("违约环节", "")))
            if key in current_keys or row.get("status") == "已消除":
                continue
            if str(row.get("批次编号", "")) not in settled:
                continue
            row["status"] = "已消除"
            row["pending"] = False
            row["abnormal"] = False
            cleared.append(str(row.get("违约编号", "")))

        created: list[str] = []
        updated: list[str] = []
        for item in breaches:
            batch_id = str(item.get("批次编号", ""))
            stage = str(item.get("违约环节", ""))
            record = next(
                (
                    row for row in existing
                    if str(row.get("批次编号", "")) == batch_id
                    and str(row.get("违约环节", "")) == stage
                ),
                None,
            )
            values = {
                "契约编号": str(contract_no),
                "批次编号": batch_id,
                "目标数据集": str(item.get("目标数据集", "")),
                "违约环节": stage,
                "规则版本": str(item.get("规则版本", "")),
                "检查起点": str(item.get("检查起点", "")),
                "结论": str(item.get("结论", "违约")),
                "原因": str(item.get("原因", "")),
            }
            if record is None:
                row_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
                record = {
                    "id": row_id,
                    "违约编号": f"BREA-{row_id:04d}",
                    "status": "待处理",
                    "pending": True,
                    "abnormal": True,
                    **values,
                }
                rows.append(record)
                created.append(str(record["违约编号"]))
            else:
                if record.get("status") == "已消除":
                    record["status"] = "待处理"
                    record["pending"] = True
                    record["abnormal"] = True
                record.update(values)
                updated.append(str(record.get("违约编号", "")))

        open_rows = [
            row for row in store.rows(self.MODULE)
            if str(row.get("契约编号", "")) == str(contract_no) and row.get("status") != "已消除"
        ]
        return {
            "created": created,
            "updated": updated,
            "cleared": cleared,
            "open_total": len(open_rows),
            "items": [
                {
                    "违约编号": row.get("违约编号"),
                    "批次编号": row.get("批次编号"),
                    "违约环节": row.get("违约环节"),
                    "规则版本": row.get("规则版本"),
                    "检查起点": row.get("检查起点"),
                    "结论": row.get("结论"),
                    "状态": row.get("status"),
                }
                for row in open_rows
            ],
        }

    def list_breaches(self, contract_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(self.MODULE)
        if contract_no:
            rows = [row for row in rows if str(row.get("契约编号", "")) == contract_no]
        return [dict(row) for row in rows]
