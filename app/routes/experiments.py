"""Read-only dashboard routes for persisted experiment results."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app.rendering.experiment_html import (
    batch_page,
    batches_page,
    experiments_shell,
    run_page,
)
from app.services.experiment_results import ExperimentResultService

router = APIRouter()


def _service() -> ExperimentResultService:
    return ExperimentResultService()


@router.get("/dashboard/api/experiments")
async def experiments_api() -> JSONResponse:
    return JSONResponse({"batches": _service().list_batches()})


@router.get("/dashboard/api/experiments/{batch_id}")
async def experiment_batch_api(batch_id: str) -> JSONResponse:
    try:
        return JSONResponse(_service().batch(batch_id))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/dashboard/api/experiments/{batch_id}/runs/{experiment_id}/{trial_index}"
)
async def experiment_run_api(
    batch_id: str, experiment_id: str, trial_index: int
) -> JSONResponse:
    try:
        return JSONResponse(
            _service().run(batch_id, experiment_id, trial_index)
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/dashboard/experiments", response_class=HTMLResponse)
async def experiments_page() -> HTMLResponse:
    return HTMLResponse(
        experiments_shell(
            "experiments",
            "实验台",
            "多智能体批次、Run Record 与可复核证据",
            batches_page(_service().list_batches()),
        )
    )


@router.get(
    "/dashboard/experiments/{batch_id}", response_class=HTMLResponse
)
async def experiment_batch_page(batch_id: str) -> HTMLResponse:
    try:
        payload = _service().batch(batch_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HTMLResponse(
        experiments_shell(
            "experiments",
            "实验批次",
            str(payload.get("manifest", {}).get("batch_id", batch_id)),
            batch_page(payload),
        )
    )


@router.get(
    "/dashboard/experiments/{batch_id}/runs/{experiment_id}/{trial_index}",
    response_class=HTMLResponse,
)
async def experiment_run_page(
    batch_id: str, experiment_id: str, trial_index: int
) -> HTMLResponse:
    try:
        record = _service().run(batch_id, experiment_id, trial_index)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HTMLResponse(
        experiments_shell(
            "experiments",
            "Run Record",
            f"{experiment_id} / trial-{trial_index:03d}",
            run_page(record, batch_id),
        )
    )


__all__ = ["router"]
