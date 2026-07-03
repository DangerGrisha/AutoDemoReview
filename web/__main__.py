"""Run the web app: `python -m web` (from the repo root).

Single worker on purpose -- the parse semaphore in web/pipeline.py is per
process, and each parse of a 300-500 MB demo is memory-heavy.
"""

if __name__ == "__main__":
    import uvicorn

    from . import config

    uvicorn.run("web.app:app", host=config.HOST, port=config.PORT, workers=1)
