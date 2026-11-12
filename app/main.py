"""FastAPI 入口：Swagger 可发起采集、查询目录与进度。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query

from . import binding, db
from .harvester import HarvestConflictError, harvest
from .oai_client import OAIClient, OAIError
from .schemas import (
    BibRecordOut,
    HarvestRequest,
    HarvestResponse,
    ProgressResponse,
    RunOut,
    SourceBindingResponse,
)

OAI_ENDPOINT = os.environ.get(
    "OAI_ENDPOINT", "http://oai-source:8080/oai"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="联合目录补采采集服务",
    description=(
        "通过 OAI-PMH ListRecords(oai_dc) 按日期补采单馆书目。\n\n"
        "- 首次采集另填起始日；之后从上次成功采集的截止日（含当天）续采\n"
        "- 续页只用 resumptionToken；令牌失效则丢弃已取页、按原区间重抓一次\n"
        "- 整批采完才在一个事务内提交源地址绑定、书目变更和截止日，无匹配也推进截止日\n"
        "- 数据卷与源地址绑定：旧库来源未确认、或当前 OAI_ENDPOINT 与已绑定地址"
        "不符时返回 409，不访问源、不写运行历史，避免演练源覆盖正式库书目"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def _validate_iso_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} 必须是 YYYY-MM-DD 格式"
        ) from exc
    return value


@app.post(
    "/api/v1/harvest",
    response_model=HarvestResponse,
    summary="发起一次补采",
    tags=["采集"],
)
def trigger_harvest(req: HarvestRequest) -> HarvestResponse:
    _validate_iso_date(req.until_date, "截止日")
    if req.from_date is not None:
        _validate_iso_date(req.from_date, "起始日")
        if req.from_date > req.until_date:
            raise HTTPException(
                status_code=422, detail="起始日不能晚于截止日（起止日都包含）"
            )

    # 源地址准入检查：未通过直接 409，此前不创建 OAI 客户端、不访问源，
    # 也不写 harvest_runs 运行历史
    preflight_conn = db.get_connection()
    try:
        try:
            binding.preflight(preflight_conn, OAI_ENDPOINT)
        except binding.SourceBindingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        preflight_conn.close()

    with OAIClient(OAI_ENDPOINT) as client:
        try:
            summary = harvest(
                client,
                until_date=req.until_date,
                from_date=req.from_date,
                endpoint=OAI_ENDPOINT,
            )
        except HarvestConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OAIError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "采集失败，本批书目、截止日与源地址绑定均未提交（已回滚）",
                    "oai_error": exc.code,
                    "detail": exc.message,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"采集失败，本批变更已回滚：{exc}",
            ) from exc

    return HarvestResponse(**summary.__dict__)


@app.get(
    "/api/v1/catalog",
    response_model=list[BibRecordOut],
    summary="查询本地联合目录",
    tags=["目录"],
)
def list_catalog(
    status: str = Query(
        "active",
        description="active=在架（默认），withdrawn=已撤下，all=全部",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[BibRecordOut]:
    allowed = {"active", "withdrawn", "all"}
    if status not in allowed:
        raise HTTPException(
            status_code=422, detail=f"status 只支持 {sorted(allowed)}"
        )
    conn = db.get_connection()
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM bib_records ORDER BY identifier LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bib_records WHERE status = ? "
                "ORDER BY identifier LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return [BibRecordOut(**dict(row)) for row in rows]
    finally:
        conn.close()


@app.get(
    "/api/v1/catalog/{identifier}",
    response_model=BibRecordOut,
    summary="按 OAI 标识查询单条书目",
    tags=["目录"],
)
def get_record(identifier: str) -> BibRecordOut:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bib_records WHERE identifier = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return BibRecordOut(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/progress",
    response_model=ProgressResponse,
    summary="查询采集进度（上次成功截止日）",
    tags=["进度"],
)
def get_progress() -> ProgressResponse:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT last_until, last_harvest_at FROM harvest_progress WHERE id = 1"
        ).fetchone()
        if row is None:
            return ProgressResponse(last_until=None, last_harvest_at=None)
        return ProgressResponse(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/source-binding",
    response_model=SourceBindingResponse,
    summary="查询数据卷与 OAI 源地址的绑定状态",
    tags=["进度"],
)
def get_source_binding() -> SourceBindingResponse:
    conn = db.get_connection()
    try:
        row = db.get_binding(conn)
        return SourceBindingResponse(
            bound_endpoint=row["endpoint"] if row else None,
            bound_at=row["bound_at"] if row else None,
            current_endpoint=db.normalize_endpoint(OAI_ENDPOINT),
            has_local_data=db.has_local_data(conn),
            confirmed_endpoint=binding.get_confirmed_endpoint(),
        )
    finally:
        conn.close()


@app.get(
    "/api/v1/runs",
    response_model=list[RunOut],
    summary="查询采集运行历史",
    tags=["进度"],
)
def list_runs(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM harvest_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunOut(**dict(row)) for row in rows]
    finally:
        conn.close()


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}
