from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable


async def await_cleanup(
    awaitable: Awaitable[object],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except BaseException:  # noqa: BLE001 - caller reports cleanup result by type
            break

    operation_error: BaseException | None = None
    try:
        task.result()
    except asyncio.CancelledError as error:
        if cancellation is None:
            cancellation = error
    except BaseException as error:  # noqa: BLE001 - caller reports cleanup result by type
        operation_error = error
    return operation_error, cancellation
