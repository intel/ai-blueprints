# Copyright (C) 2024-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import List, Dict, Any, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException
from celery.result import AsyncResult

from core.handlers.auth_handler import get_current_user_id
from core.celery.celery_config import celery_app
from core.schemas.celery_schemas import (
    JobListResponse,
    JobWithStatus,
    JobStatusResponse,
    PrepareDataResponse,
)
from core.utils.job_metadata import (
    get_job_metadata,
    get_jobs_by_user,
    mark_job_cancelled,
    store_job_metadata,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/dataprep", tags=["dataprep"])

CANCELLED = "CANCELLED"
# Celery states after which a job has nothing left to stop.
_FINISHED = {"SUCCESS", "FAILURE", "REVOKED", CANCELLED}

# Maximum number of file IDs accepted per job submission
MAX_FILE_IDS_PER_JOB: int = 50


def _resolve_chord_status(
    task_result: AsyncResult,
) -> Tuple[str, Optional[Dict], Optional[str]]:
    """
    Resolve the status, result payload, and error string for a parent Celery
    task that may wrap a chord workflow.

    Returns a (status, result, error) tuple compatible with the existing
    JobStatusResponse / JobListResponse schemas.
    """
    status = task_result.status
    result: Optional[Dict] = None
    error: Optional[str] = None

    if not task_result.ready():
        return status, result, error

    if not task_result.successful():
        error = str(task_result.info)
        return status, result, error

    result = task_result.result

    # If the parent task delegated work to a chord, follow the chord
    if not (isinstance(result, dict) and "chord_id" in result):
        return status, result, error

    chord_id = result["chord_id"]
    chord_result = AsyncResult(chord_id, app=celery_app)
    status = chord_result.status

    if chord_result.ready():
        if chord_result.successful():
            result = chord_result.result
            # Business-logic failures are encoded in the result payload
            if isinstance(result, dict) and result.get("status") == "failed":
                status = "FAILURE"
                error = result.get("message", "Processing failed")
        else:
            result = None
            error = str(chord_result.info)
        return status, result, error

    # Chord still in progress — calculate completion percentage
    # NOTE: This performs N individual Redis lookups (one per child task).
    # For very large batches a future optimisation can batch-fetch via
    # celery.result.GroupResult or a raw Redis MGET.
    status = "PROCESSING"
    try:
        total_files: int = result.get("file_count", len(result.get("file_ids", [])))
        child_task_ids: List[str] = result.get("child_task_ids", [])

        completed = sum(
            1
            for task_id in child_task_ids
            if AsyncResult(task_id, app=celery_app).ready()
        )

        progress = int((completed / total_files * 100)) if total_files > 0 else 0

        result = {
            "aggregated_file_id": "",
            "total_qa_pairs": 0,
            "successful_files": completed,
            "failed_files": 0,
            "status": "processing",
            "message": f"Processing {completed}/{total_files} files",
            "progress": progress,
            "completed_files": completed,
            "total_files": total_files,
        }
    except Exception:
        logger.exception("Failed to calculate chord progress for chord_id=%s", chord_id)
        result = {
            "aggregated_file_id": "",
            "total_qa_pairs": 0,
            "successful_files": 0,
            "failed_files": 0,
            "status": "processing",
            "message": "Processing...",
            "progress": 0,
            "completed_files": 0,
            "total_files": 0,
        }

    return status, result, error


@router.post("", response_model=PrepareDataResponse)
async def prepare_data(
    file_ids: List[str] = Body(..., embed=True, description="File IDs to process"),
    user_id: str = Depends(get_current_user_id),
):
    """
    Submit files for data preparation.
    Processes your files and generates training data in JSONL format.
    Returns a job ID to track the processing status.
    """
    if not file_ids:
        raise HTTPException(status_code=400, detail="At least one file ID is required")

    if len(file_ids) > MAX_FILE_IDS_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"Too many file IDs. Maximum allowed per job is {MAX_FILE_IDS_PER_JOB}.",
        )

    try:
        task = celery_app.send_task(
            "core.celery.tasks.dataset_generation_using_data_kits",
            args=[file_ids, user_id],
            queue="dataprep_queue",
        )

        # Store job metadata in Redis so user can retrieve it later
        metadata_stored = store_job_metadata(
            job_id=task.id,
            user_id=user_id,
            file_id=",".join(file_ids),
            metadata={
                "task_name": "dataset_generation_using_data_kits",
                "original_file_ids": file_ids,
                "file_count": len(file_ids),
                "processing_type": "parallel_chord_aggregation",
            },
        )

        if not metadata_stored:
            raise HTTPException(
                status_code=500,
                detail="Failed to store job metadata. Please retry.",
            )

        return {
            "submitted_job_ids": [task.id],
            "job_status": "Submitted",
            "result": None,
            "error": None,
            "file_count": len(file_ids),
            "processing_mode": "parallel_with_aggregation",
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to submit batch job for user %s (%d files)", user_id, len(file_ids)
        )
        raise HTTPException(
            status_code=500,
            detail="An error occurred while submitting the job.",
        )


@router.get("/status/{job_id}", response_model=JobStatusResponse)
async def get_prepare_data_status(job_id: str) -> Dict[str, Any]:
    """
    Get the status and result of your data preparation job.
    Returns the processed files when completed.
    """
    task_result = AsyncResult(job_id, app=celery_app)
    status, result, error = _resolve_chord_status(task_result)
    if (get_job_metadata(job_id) or {}).get("cancelled_at"):
        status, error = CANCELLED, "Cancelled by the user"

    # If the parent task delegated to a chord, expose the chord's ID to the caller
    if (
        task_result.ready()
        and task_result.successful()
        and isinstance(task_result.result, dict)
        and "chord_id" in task_result.result
    ):
        job_id = task_result.result["chord_id"]

    return {
        "job_id": job_id,
        "status": status,
        "result": result,
        "error": error,
    }


@router.get("/jobs", response_model=JobListResponse)
async def get_all_jobs_by_user(
    user_id: str = Depends(get_current_user_id),
):
    """
    Get all your data preparation jobs.
    Returns a list of jobs with their current status.
    """
    jobs_metadata = get_jobs_by_user(user_id)

    if not jobs_metadata:
        return JobListResponse(user_id=user_id, total_jobs=0, jobs=[])

    jobs_with_status = []
    for job_meta in jobs_metadata:
        job_id = job_meta["job_id"]
        task_result = AsyncResult(job_id, app=celery_app)

        status, result, error = _resolve_chord_status(task_result)
        if job_meta.get("cancelled_at"):
            status, error = CANCELLED, "Cancelled by the user"

        job_with_status = JobWithStatus(
            job_id=job_meta["job_id"],
            user_id=job_meta["user_id"],
            file_id=job_meta["file_id"],
            submitted_at=job_meta["submitted_at"],
            status=status,
            result=result,
            error=error,
            metadata=job_meta.get("metadata", {}),
        )
        jobs_with_status.append(job_with_status)

    return JobListResponse(
        user_id=user_id,
        total_jobs=len(jobs_with_status),
        jobs=jobs_with_status,
    )


def _job_task_ids(job_id: str) -> List[str]:
    """Every Celery task of a job: the submit task, each file's chain, the aggregation."""
    ids = [job_id]
    parent = AsyncResult(job_id, app=celery_app)
    if parent.ready() and parent.successful() and isinstance(parent.result, dict):
        # all_task_ids covers each chain's every step; child_task_ids only the
        # last one (jobs submitted before all_task_ids was recorded).
        ids += parent.result.get("all_task_ids") or parent.result.get("child_task_ids", [])
        if parent.result.get("chord_id"):
            ids.append(parent.result["chord_id"])
    return list(dict.fromkeys(ids))


@router.post("/jobs/{job_id}/cancel", response_model=JobStatusResponse)
async def cancel_job(
    job_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Cancel one of your unfinished data preparation jobs.

    Tasks not started yet are dropped; running ones are stopped with SIGUSR1,
    which raises inside the task so it removes its temporary files before it
    ends. The job then reads CANCELLED. Files a finished step already produced
    stay in your files.
    """
    meta = get_job_metadata(job_id)
    if not meta or meta.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if meta.get("cancelled_at"):
        return {"job_id": job_id, "status": CANCELLED, "result": None, "error": "Cancelled by the user"}

    status, _, _ = _resolve_chord_status(AsyncResult(job_id, app=celery_app))
    if status in _FINISHED:
        raise HTTPException(status_code=409, detail=f"The job has already finished ({status})")

    task_ids = _job_task_ids(job_id)
    try:
        celery_app.control.revoke(task_ids, terminate=True, signal="SIGUSR1")
    except Exception:
        # The worker may be gone already (a restart): the job is still marked
        # cancelled, which is what its status reports from now on.
        logger.exception("Could not revoke the tasks of job %s", job_id)
    mark_job_cancelled(job_id)
    logger.info("Cancelled data preparation job %s (%d tasks)", job_id, len(task_ids))
    return {"job_id": job_id, "status": CANCELLED, "result": None, "error": "Cancelled by the user"}

