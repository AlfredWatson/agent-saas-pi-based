from fastapi import APIRouter, Depends, HTTPException

from ...api.dependencies import current_user
from ...db.models import User
from ...services.runtime_locator import RuntimeBusyError, RuntimeUnavailableError, get_runtime_locator

router = APIRouter(prefix="/runtime", tags=["runtime"])


def render(status):
    return {
        "state": status.state,
        "image": status.image,
        "container_id": status.container_id,
        "last_error": status.last_error,
        "last_seen_at": status.last_seen_at,
    }


@router.get("")
async def status(user: User = Depends(current_user)):
    return render(await get_runtime_locator().status(user.id))


@router.post(":start")
async def start(user: User = Depends(current_user)):
    try:
        endpoint = await get_runtime_locator().ensure(user.id)
    except RuntimeUnavailableError as exc:
        raise HTTPException(503, "runtime_unavailable") from exc
    return {"state": endpoint.state}


@router.post(":stop")
async def stop(user: User = Depends(current_user)):
    try:
        return render(await get_runtime_locator().stop(user.id))
    except RuntimeBusyError as exc:
        raise HTTPException(409, "runtime_busy") from exc
    except RuntimeUnavailableError as exc:
        raise HTTPException(409, "runtime_not_stoppable") from exc


@router.post(":recreate")
async def recreate(user: User = Depends(current_user)):
    try:
        endpoint = await get_runtime_locator().recreate(user.id)
    except RuntimeBusyError as exc:
        raise HTTPException(409, "runtime_busy") from exc
    except RuntimeUnavailableError as exc:
        raise HTTPException(503, "runtime_unavailable") from exc
    return {"state": endpoint.state}
