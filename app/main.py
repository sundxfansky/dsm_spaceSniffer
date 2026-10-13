from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .jobs import JobStore
from .models import AnalysisRequest, Credentials, DiscoveryResponse, JobCreated, JobResponse
from .synology import SynologyError, discover


BASE_DIR = Path(__file__).resolve().parent
jobs = JobStore()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    jobs.shutdown()


app = FastAPI(title="DSM Storage Inspector", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/discover", response_model=DiscoveryResponse)
def discover_volumes(credentials: Credentials) -> DiscoveryResponse:
    try:
        session, volumes = discover(credentials)
        try:
            return DiscoveryResponse(
                host=session.host,
                server_fingerprint=session.fingerprint,
                volumes=volumes,
                identity=session.identity,
            )
        finally:
            session.close()
    except (SynologyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs", response_model=JobCreated, status_code=202)
def create_job(request: AnalysisRequest) -> JobCreated:
    return JobCreated(job_id=jobs.create(request))


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JobResponse.model_validate(job)


@app.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
def cancel_job(job_id: str) -> JobResponse:
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JobResponse.model_validate(job)


@app.get("/api/jobs/{job_id}/tree")
def browse_tree(job_id: str, path: str = Query(max_length=4096), offset: int = Query(default=0, ge=0),
                limit: int = Query(default=30, ge=1, le=100), q: str = Query(default="", max_length=512)) -> dict:
    try:
        result = jobs.browse(job_id, path, offset, limit, q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result
