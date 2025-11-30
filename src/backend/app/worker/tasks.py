from datetime import datetime
import time

from app.db.utils import get_session
from app.models.upload_slot import GameUploadSlot
from app.services.ai_service import process_puzzle_generation
from app.worker.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3)
def generate_puzzle_task(self, slot_id: int) -> None:
    """
    퍼즐 생성 Task를 실행합니다.
    """
    try:
        with get_session() as session:
            process_puzzle_generation(session, slot_id)
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
