"""Local multi-user web app wrapping the demoReview pipeline.

FastAPI + SQLite + Jinja2 + Steam OpenID. Imports the existing
`demoreview` package (parsing + rendering) as modules -- no logic is
duplicated here; this layer only handles auth, storage, and serving.

Run from the repo root:  python -m web   (or: uvicorn web.app:app)
"""
