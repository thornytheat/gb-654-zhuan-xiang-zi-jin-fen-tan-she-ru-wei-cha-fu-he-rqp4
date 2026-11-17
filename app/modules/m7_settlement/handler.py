"""M7 跨批次尾差结算接口。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

from .core import SAMPLE
from . import service

router = APIRouter(tags=["M7-跨批次尾差结算"])


@router.get("/settlement")
def page(request: Request):
    data = service.sample_preview()
    return request.app.state.templates.TemplateResponse(
        request, "m7_settlement.html", {"title": "跨批次尾差结算", "data": data}
    )


@router.post("/api/settlement/preview")
def preview_api(request: Request, payload: dict = Body(default={})):
    # 预览只读取当前欠额出具试算，不占欠额、不写任何台账。
    return service.preview(request, payload or dict(SAMPLE))


@router.post("/api/settlement/commit")
def commit_api(request: Request, payload: dict = Body(...)):
    return service.commit(request, payload)


@router.get("/api/settlement/batches/{fund_year}")
def batches_api(request: Request, fund_year: str):
    return service.list_batches(request, fund_year)


@router.get("/api/settlement/batch/{fund_year}/{batch_id}")
def readback_api(request: Request, fund_year: str, batch_id: str):
    return service.readback(request, fund_year, batch_id)


@router.get("/api/settlement/ledger/{fund_year}")
def ledger_api(request: Request, fund_year: str):
    return service.ledger(request, fund_year)
