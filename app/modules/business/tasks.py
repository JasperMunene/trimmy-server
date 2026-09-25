from app.core.storage import get_storage
from app.workers.registry import task


# inline_fallback: cheap, and skipping it would leave orphaned files in the bucket.
@task("storage.delete_object", queue="default", max_retries=5, inline_fallback=True)
async def delete_stored_object(key: str) -> None:
    await get_storage().delete(key)
