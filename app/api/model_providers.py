"""Model-provider management API."""

import sqlite3

from fastapi import APIRouter, HTTPException, Response

from app.enrichment import enrichment_manager
from app.model_provider import (
    CreateModelProviderRequest,
    ModelProviderError,
    UpdateModelProviderRequest,
    get_model_provider_service,
)


router = APIRouter(prefix="/api/model-providers", tags=["Model Providers"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _raise_provider_error(error: ModelProviderError) -> None:
    status_code = {
        "provider_not_found": 404,
        "provider_exists": 409,
        "provider_disabled": 409,
        "managed_provider": 409,
        "default_provider_required": 409,
        "provider_timeout": 504,
        "provider_unavailable": 503,
    }.get(error.code, 422)
    raise HTTPException(status_code=status_code, detail=str(error)) from error


def _raise_database_error(error: sqlite3.OperationalError) -> None:
    if "locked" in str(error).casefold() or "busy" in str(error).casefold():
        raise HTTPException(
            status_code=503,
            detail="模型配置正在被另一个请求更新，请稍后重试。",
        ) from error
    raise error


@router.get("")
def list_model_providers(response: Response):
    _no_store(response)
    try:
        service = get_model_provider_service()
        items = [item.model_dump(mode="json") for item in service.list()]
        return {"items": items, "total": len(items)}
    except sqlite3.OperationalError as error:
        _raise_database_error(error)


@router.post("", status_code=201)
def create_model_provider(payload: CreateModelProviderRequest, response: Response):
    _no_store(response)
    try:
        result = get_model_provider_service().create(payload)
        enrichment_manager.invalidate_provider_status()
        return result.model_dump(mode="json")
    except ModelProviderError as error:
        _raise_provider_error(error)
    except sqlite3.OperationalError as error:
        _raise_database_error(error)


@router.get("/codex/catalog")
def get_codex_model_catalog(response: Response):
    _no_store(response)
    try:
        return get_model_provider_service().codex_catalog()
    except ModelProviderError as error:
        _raise_provider_error(error)
    except sqlite3.OperationalError as error:
        _raise_database_error(error)


@router.patch("/{provider_id}")
def update_model_provider(
    provider_id: str,
    payload: UpdateModelProviderRequest,
    response: Response,
):
    _no_store(response)
    try:
        result = get_model_provider_service().update(provider_id, payload)
        enrichment_manager.invalidate_provider_status()
        enrichment_manager.wake()
        return result.model_dump(mode="json")
    except ModelProviderError as error:
        _raise_provider_error(error)
    except sqlite3.OperationalError as error:
        _raise_database_error(error)


@router.post("/{provider_id}/test")
def test_model_provider(provider_id: str, response: Response):
    _no_store(response)
    try:
        result = get_model_provider_service().status(provider_id)
        enrichment_manager.invalidate_provider_status()
        return result
    except ModelProviderError as error:
        _raise_provider_error(error)
    except sqlite3.OperationalError as error:
        _raise_database_error(error)


@router.delete("/{provider_id}", status_code=204)
def delete_model_provider(provider_id: str, response: Response):
    _no_store(response)
    try:
        get_model_provider_service().delete(provider_id)
        enrichment_manager.invalidate_provider_status()
    except ModelProviderError as error:
        _raise_provider_error(error)
    except sqlite3.OperationalError as error:
        _raise_database_error(error)
