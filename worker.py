
from js import Response

async def on_fetch(request, env):
    return Response.new(
        "Yuta Bot Worker is running!",
        {"status": 200}
    )

async def on_fetch_default(request, env):
    return await on_fetch(request, env)
