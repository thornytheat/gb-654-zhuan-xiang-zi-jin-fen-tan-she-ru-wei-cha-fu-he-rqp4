"""M7 跨批次尾差结算服务编排。"""
from __future__ import annotations

from fastapi import Request

from app.errors import DomainError
from app.modules.m7_settlement import core
from app.settlement_store import SettlementStore


def _store(request: Request) -> SettlementStore:
    if not hasattr(request.app.state, "settlement_store"):
        request.app.state.settlement_store = SettlementStore(request.app.state.settings.db_path)
    return request.app.state.settlement_store


def sample_preview() -> dict:
    # 页面演示：纯函数样例，不触碰台账。
    return core.build(core.SAMPLE, {}, 0, committed=False)


def preview(request: Request, payload: dict) -> dict:
    return _store(request).preview(payload)


def commit(request: Request, payload: dict) -> dict:
    expected = payload.get("expected_version")
    if expected is None:
        raise DomainError("MISSING_VERSION", "结算须携带 expected_version 乐观版本号")
    return _store(request).commit(payload, int(expected))


def readback(request: Request, fund_year: str, batch_id: str) -> dict:
    return _store(request).readback(fund_year, batch_id)


def list_batches(request: Request, fund_year: str) -> dict:
    return {"fund_year": fund_year, "batches": _store(request).list_batches(fund_year)}


def ledger(request: Request, fund_year: str) -> dict:
    store = _store(request)
    with store.connect() as db:
        return {
            "fund_year": fund_year,
            "version": store.get_version(db, fund_year),
            "deficits": store.get_deficits(db, fund_year),
        }
