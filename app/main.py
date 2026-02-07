import asyncio
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI

from .admin import router as admin_router
from .keys import router as keys_router
from .proxy import router as proxy_router, close_http_client
from .openai_compat import router as openai_router
from .config import settings
from .db import Base, engine
from .usage_buffer import flush_loop, flush_once


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s: %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Start usage flush loop
    flush_task = asyncio.create_task(flush_loop(settings.usage_flush_interval))
    logging.info("CoinCoin Proxy started")
    
    try:
        yield
    finally:
        flush_task.cancel()
        await flush_once()
        await close_http_client()
        logging.info("CoinCoin Proxy stopped")


app = FastAPI(
    title="CoinCoin Proxy",
    description="OpenAI Compatible API Proxy for Azure OpenAI",
    version="1.0.0",
    lifespan=lifespan,
)

# 直接代理（Azure Responses API 原生格式）
app.include_router(proxy_router)

# OpenAI 兼容层（Chat Completions 格式）
app.include_router(openai_router)

# Key 管理
app.include_router(keys_router)

# Admin 管理后台
app.include_router(admin_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "coincoin-proxy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
