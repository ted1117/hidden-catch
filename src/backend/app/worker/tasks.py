from datetime import datetime
import time
from warnings import deprecated

from celery import chain

from app.db.utils import get_session
from app.models.upload_slot import GameUploadSlot
from app.services import ai_service
from app.worker.celery_app import celery_app


@deprecated("This function is deprecated. Use generate_puzzle_pipeline_task instead.")
@celery_app.task(bind=True, max_retries=3)
def generate_puzzle_task(self, slot_id: int) -> None:
    """
    퍼즐 생성 Task를 실행합니다.
    """
    try:
        with get_session() as session:
            ai_service.process_puzzle_generation(session, slot_id)
    except Exception as exc:
        # 재시도 가능한 오류인 경우 재시도
        try:
            raise self.retry(exc=exc, countdown=60)
        except self.MaxRetriesExceededError:
            # 최대 재시도 횟수 초과 시 실패 처리
            with get_session() as session:
                slot = session.get(GameUploadSlot, slot_id)
                if slot:
                    slot.analysis_status = "failed"
                    slot.analysis_error = f"Max retries exceeded: {exc}"
                    slot.last_analyzed_at = datetime.now()


@celery_app.task
def long_running_task(param: int) -> str:
    time.sleep(10)
    return f"Proceed {param} successfully!"


@celery_app.task
def generate_puzzle_pipeline_task(slot_id: int) -> None:
    chain(
        validate_upload_slot_task.si(slot_id),
        detect_objects_task.s(),
        edit_image_task.s(),
        assign_stage_task.s(),
    ).delay()


@celery_app.task(bind=True, max_retries=0)
def validate_upload_slot_task(self, slot_id: int) -> int:
    with get_session() as session:
        ok = ai_service.validate_upload_slot(session, slot_id)
        if not ok:
            return slot_id
    return slot_id


@celery_app.task(bind=True, max_retries=2)
def detect_objects_task(self, slot_id: int) -> int:
    try:
        with get_session() as session:
            ai_service.detect_objects_for_slot(session, slot_id)
        return slot_id
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(bind=True, max_retries=5)
def edit_image_task(self, slot_id: int) -> dict[str, int | str] | None:
    with get_session() as session:
        payload = ai_service.prepare_edit_payload(session, slot_id)
    if payload is None:
        return None

    result = ai_service.run_imagen_edit(payload)
    if result is None:
        raise self.retry(countdown=30)

    output_key, image_width, image_height = result
    return {
        "slot_id": slot_id,
        "output_key": output_key,
        "image_width": image_width,
        "image_height": image_height,
    }


@celery_app.task(bind=True, max_retries=5)
def assign_stage_task(self, payload: dict[str, int | str] | None) -> None:
    if not payload:
        return
    slot_id = int(payload["slot_id"])
    output_key = str(payload["output_key"])
    image_width = int(payload["image_width"])
    image_height = int(payload["image_height"])

    try:
        with get_session() as session:
            ai_service.assign_stage_for_slot(
                session,
                slot_id,
                output_key=output_key,
                image_width=image_width,
                image_height=image_height,
            )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
