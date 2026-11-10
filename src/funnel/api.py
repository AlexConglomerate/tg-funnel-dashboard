"""FastAPI: дашборд, статистика, health-check."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from funnel.config import FunnelConfig
from funnel.db import Database, to_db, utcnow
from funnel.stats import Stats, compute_stats
from funnel.webapp_auth import InitDataError, validate_init_data

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
TICK_STALE_AFTER = timedelta(seconds=10)


class SchedulerState(Protocol):
    @property
    def last_tick(self) -> datetime | None: ...

    @property
    def paused(self) -> bool: ...


def create_app(
    *,
    db: Database,
    config: FunnelConfig,
    bot_token: str,
    admin_ids: frozenset[int],
    scheduler: SchedulerState,
    clock: Callable[[], datetime] = utcnow,
    static_dir: Path = STATIC_DIR,
) -> FastAPI:
    app = FastAPI(title="tg-funnel-dashboard", docs_url=None, redoc_url=None)

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> int:
        if not authorization or not authorization.startswith("tma "):
            raise HTTPException(status_code=401, detail="нужен заголовок Authorization: tma …")
        try:
            init = validate_init_data(
                init_data=authorization.removeprefix("tma "),
                bot_token=bot_token,
                now=clock(),
            )
        except InitDataError as exc:
            log.warning("webapp_auth_failed reason=%s", exc)
            raise HTTPException(status_code=401, detail="initData не прошли проверку") from exc
        if init.user_id not in admin_ids:
            log.warning("webapp_forbidden user_id=%s", init.user_id)
            raise HTTPException(status_code=403, detail="нет доступа")
        return init.user_id

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        # no-store: Telegram и браузер не кэшируют страницу между перезапусками туннеля.
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/stats")
    async def stats(_admin: Annotated[int, Depends(require_admin)]) -> Stats:
        return await compute_stats(db=db, config=config, now=clock())

    @app.get("/health")
    async def health() -> JSONResponse:
        now = clock()
        last_tick = scheduler.last_tick
        body: dict[str, object] = {
            "last_tick": to_db(last_tick) if last_tick else None,
            "scheduler_paused": scheduler.paused,
        }
        try:
            await asyncio.wait_for(db.ping(), timeout=2)
        except Exception as exc:
            return JSONResponse(
                {"status": "fail", "reason": f"db: {type(exc).__name__}", **body}, status_code=503
            )
        stale = last_tick is None or now - last_tick > TICK_STALE_AFTER
        if stale and not scheduler.paused:
            return JSONResponse(
                {"status": "fail", "reason": "scheduler: нет прохода цикла дольше 10 с", **body},
                status_code=503,
            )
        return JSONResponse({"status": "ok", **body})

    return app
