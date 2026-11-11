"""Production FastAPI Application for GroundCheck Claim Verifier.
Provides Kubernetes health probes, Prometheus metrics, OpenAPI specs,
and asynchronous batch and single-claim verification."""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from .. import __version__
from ..aiml.semantic_attributor import SemanticClaimAttributor
from ..db import connect, now_iso
from ..evals import default_cases, grounded_narrator, inflating_baseline, run_eval, swapped_window_baseline
from ..golden import case_variants
from ..metrics import Fact, build_facts
from ..narrator import annotate, fmt, ignore_list, verify
from .metrics_collector import METRICS
from .schemas import (
    BatchItemResponse,
    BatchVerifyRequest,
    BatchVerifyResponse,
    FactModel,
    HealthResponse,
    ReadinessResponse,
    SegmentModel,
    TitleModel,
    VerifyRequest,
    VerifyResponse,
)


def create_app(db_target: str | None = None) -> FastAPI:
    app = FastAPI(
        title="GroundCheck Claim Verifier API",
        description="Production API for claim-level verification of AI-written analytics. "
                    "Detects hallucinated numbers, misattributed windows, and causal overclaims.",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize shared database and semantic model
    target = db_target or os.environ.get("DATABASE_URL", ":memory:")
    # If using in-memory demo database by default
    conn = connect(target, check_same_thread=False)
    if target == ":memory:":
        from ..sample_data import seed_all
        seed_all(conn)

    app.state.conn = conn
    app.state.start_time = time.time()
    app.state.attributor = SemanticClaimAttributor()

    @app.middleware("http")
    async def request_tracing_and_metrics(request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        response.headers["X-Request-ID"] = req_id
        path = request.url.path
        METRICS.record_request(request.method, path, response.status_code, duration)
        return response

    @app.get("/healthz", response_model=HealthResponse, tags=["Observability"])
    async def health_check():
        """Kubernetes Liveness Probe."""
        return HealthResponse(
            status="healthy",
            version=__version__,
            database="connected",
            uptime_seconds=round(time.time() - app.state.start_time, 2),
            timestamp=now_iso(),
        )

    @app.get("/ready", response_model=ReadinessResponse, tags=["Observability"])
    async def readiness_check():
        """Kubernetes Readiness Probe."""
        db_ok = False
        try:
            row = app.state.conn.execute("SELECT 1 as alive").fetchone()
            db_ok = row is not None and row["alive"] == 1
        except Exception:
            db_ok = False

        is_ready = db_ok
        status_code = status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
        res = ReadinessResponse(ready=is_ready, checks={"database": db_ok, "attributor": True})
        if not is_ready:
            raise HTTPException(status_code=status_code, detail=res.model_dump())
        return res

    @app.get("/metrics", response_class=PlainTextResponse, tags=["Observability"])
    async def prometheus_metrics():
        """Prometheus Scrape Endpoint."""
        return METRICS.render()

    @app.post("/api/v1/verify", response_model=VerifyResponse, tags=["Verification"])
    async def verify_claim(req: VerifyRequest):
        """Verify an AI-generated analytics summary against ground truth facts."""
        start = time.perf_counter()
        domain_facts = [
            Fact(
                id=f.id,
                label=f.label,
                value=f.value,
                unit=f.unit,
                query=f.query,
            )
            for f in req.facts
        ]

        # Register any custom fact cues
        for f in req.facts:
            if f.cue:
                from ..narrator import CUES
                CUES[f.id] = f.cue

        rep = verify(req.text, domain_facts, ignore=req.ignore)
        segs = annotate(req.text, domain_facts, ignore=req.ignore)

        # Record metrics
        METRICS.record_verification(
            n_numbers=rep.n_numbers,
            n_misattributed=len(rep.misattributed),
            n_unsupported=len(rep.unsupported),
            n_style=len(rep.style_flags),
        )

        latency = (time.perf_counter() - start) * 1000.0
        return VerifyResponse(
            ok=rep.ok,
            n_numbers=rep.n_numbers,
            claim_accuracy=round(rep.claim_accuracy, 4),
            groundedness=round(rep.groundedness, 4),
            problems=rep.reasons(),
            segments=[SegmentModel(text=s["text"], status=s["status"], detail=s["detail"]) for s in segs],
            latency_ms=round(latency, 2),
        )

    @app.post("/api/v1/verify/batch", response_model=BatchVerifyResponse, tags=["Verification"])
    async def verify_batch(req: BatchVerifyRequest):
        """Batch process multiple analytical claims in a single API call."""
        start = time.perf_counter()
        results: list[BatchItemResponse] = []
        passed_count = 0

        for item in req.items:
            domain_facts = [
                Fact(id=f.id, label=f.label, value=f.value, unit=f.unit, query=f.query)
                for f in item.facts
            ]
            rep = verify(item.text, domain_facts, ignore=item.ignore)
            segs = annotate(item.text, domain_facts, ignore=item.ignore)

            METRICS.record_verification(
                n_numbers=rep.n_numbers,
                n_misattributed=len(rep.misattributed),
                n_unsupported=len(rep.unsupported),
                n_style=len(rep.style_flags),
            )

            if rep.ok:
                passed_count += 1

            results.append(
                BatchItemResponse(
                    item_id=item.item_id,
                    ok=rep.ok,
                    n_numbers=rep.n_numbers,
                    claim_accuracy=round(rep.claim_accuracy, 4),
                    problems=rep.reasons(),
                    segments=[SegmentModel(text=s["text"], status=s["status"], detail=s["detail"]) for s in segs],
                )
            )

        total = len(results)
        latency = (time.perf_counter() - start) * 1000.0
        return BatchVerifyResponse(
            total_processed=total,
            passed=passed_count,
            failed=total - passed_count,
            pass_rate=round(passed_count / total, 4) if total else 0.0,
            items=results,
            latency_ms=round(latency, 2),
        )

    @app.get("/api/v1/titles", response_model=list[TitleModel], tags=["Dataset"])
    async def list_titles():
        """Retrieve all tracked titles in the database."""
        rows = app.state.conn.execute("SELECT id, name, genre FROM titles ORDER BY id").fetchall()
        return [TitleModel(id=r["id"], name=r["name"], genre=r["genre"]) for r in rows]

    @app.get("/api/v1/facts/{title_id}", response_model=list[FactModel], tags=["Dataset"])
    async def get_facts_for_title(title_id: int):
        """Retrieve computed ground truth facts for a title."""
        cases = default_cases(app.state.conn)
        case = next((c for c in cases if c.title_id == title_id), None)
        if not case:
            raise HTTPException(status_code=404, detail="Title not found")

        facts = build_facts(app.state.conn, title_id, case.peer_ids)
        return [
            FactModel(
                id=f.id,
                label=f.label,
                value=f.value,
                unit=f.unit,
                query=f.query,
            )
            for f in facts
        ]

    return app
