import json
import logging
from functools import wraps

from src.common.exceptions import CustomToolException

log = logging.getLogger(__name__)


def with_context(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        session = None
        token = None

        try:
            __request__ = kwargs.get("__request__", None)
            __user__ = kwargs.get("__user__", None)
            __metadata__ = kwargs.get("__metadata__", None)
            __event_emitter__ = kwargs.get("__event_emitter__", None)
            __event_call__ = kwargs.get("__event_call__", None)

            if __request__ is None:
                raise ValueError("Request context not available")
            if __user__ is None:
                raise ValueError("User context not available")
            if __metadata__ is None:
                raise ValueError("Metadata context not available")
            else:
                if __metadata__.get("files", None) is None:
                    __metadata__["files"] = []

            connect_handler, *other_handlers = self._get_handlers()

            await self._emit_status(
                __event_emitter__,
                "Connecting to server...",
                done=False,
            )

            # Connect to server
            session = await connect_handler(__user__, __event_call__)

            # Set context for this call
            token = self.context.set((session, *other_handlers))

            return await func(self, *args, **kwargs)

        except CustomToolException as e:
            log.error(f"{e} ({e.error})" if e.error else str(e))
            return json.dumps({"error": str(e)})

        except Exception as e:
            log.exception(e)
            return json.dumps({"error": str(e)})

        finally:
            # Reset context for this call
            if token is not None:
                self.context.reset(token)

            # Disconnect from server
            if session is not None:
                self._disconnect(session)

    return wrapper
