"""HTTP error normalization."""

from aiohttp import web

from .context import DashboardContext


def create_json_error_middleware(context: DashboardContext):
    @web.middleware
    async def json_error_middleware(request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            context.node.get_logger().error(f"Unhandled web error for {request.path}: {exc}")
            return web.json_response({"error": str(exc)}, status=500)

    return json_error_middleware


def create_static_cache_middleware():
    @web.middleware
    async def static_cache_middleware(request: web.Request, handler):
        response = await handler(request)
        if request.path.startswith("/static/") and response.status < 400:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if request.query.get("v")
                else "public, max-age=3600"
            )
        return response

    return static_cache_middleware
