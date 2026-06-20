import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "control_plane.api:app",
        host=os.getenv("FORGE_CONTROL_HOST", "127.0.0.1"),
        port=int(os.getenv("FORGE_CONTROL_PORT", "8787")),
        reload=False,
    )
