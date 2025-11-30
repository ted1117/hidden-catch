from datetime import datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError
from celery import chain
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.session import get_db
from app.models import Difference, Game, GameStage, GameStageHit, GameUploadSlot, Puzzle
from app.schemas.game import (
    CreateGameRequest,
    CreateGameResponse,
    FinishGameRequest,
    FinishGameResponse,
    GameDetailResponse,
    StageCompleteRequest,
    StageResultResponse,
    UploadCompleteRequest,
    UploadSlot,
    UploadSlotsStatusResponse,
    UploadSlotStatus,
)
from app.schemas.puzzle import (
    CheckAnswerRequest,
    CheckAnswerResponse,
    FoundDifference,
    HitAttempt,
    PuzzleForGameResponse,
)
from app.worker.tasks import generate_puzzle_task
from app.worker.tasks_legacy import run_imagen_pipeline


def _build_s3_client() -> Any:
    client_kwargs: dict[str, str] = {}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        client_kwargs["aws_access_key_id"] = settings.aws_access_key_id
        client_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("s3", region_name=settings.aws_region, **client_kwargs)


_S3_CLIENT = _build_s3_client()


class GameService:
    def __init__(self, session: Session, s3_client: Any | None = None):
        self.session = session
        self.s3_client = s3_client or _S3_CLIENT

    def _build_slot_key(self, game_id: int, slot_number: int) -> str:
        prefix = settings.aws_s3_upload_prefix.strip("/")
        key_suffix = f"game-{game_id}/slot-{slot_number}.png"
        return f"{prefix}/{key_suffix}" if prefix else key_suffix

    def _generate_presigned_upload_url(self, object_key: str) -> str:
        if not settings.aws_s3_bucket_name:
            raise HTTPException(status_code=500, detail="S3 bucket is not configured")
        ttl = (
            settings.aws_s3_presign_ttl_seconds
            if settings.aws_s3_presign_ttl_seconds > 0
            else 900
        )
        return self.s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": settings.aws_s3_bucket_name,
                "Key": object_key,
            },
            ExpiresIn=ttl,
        )

    def _validate_upload_content_type(self, object_key: str) -> str:
        if not settings.aws_s3_bucket_name:
            raise HTTPException(status_code=500, detail="S3 bucket is not configured")
        try:
            metadata = self.s3_client.head_object(
                Bucket=settings.aws_s3_bucket_name,
                Key=object_key,
            )
        except ClientError as exc:
            raise HTTPException(
                status_code=400,
                detail="Uploaded object not found in storage.",
            ) from exc
        content_type = metadata.get("ContentType") or ""
        allowed = settings.allowed_upload_content_types or []
        if allowed and content_type not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content type: {content_type}",
            )
        return content_type

    def create_game(self, payload: CreateGameRequest) -> CreateGameResponse:
        game = Game(
            mode=payload.mode,
            difficulty=payload.difficulty,
            status="waiting_upload",
            time_limit_seconds=payload.time_limit_seconds,
        )
        self.session.add(game)
        self.session.flush()

        now = datetime.now()
        slots: list[UploadSlot] = []
        ttl_seconds = (
            settings.aws_s3_presign_ttl_seconds
            if settings.aws_s3_presign_ttl_seconds > 0
            else 900
        )
        for index in range(payload.requested_slot_count):
            slot_number = index + 1
            expires_at = now + timedelta(seconds=ttl_seconds)
            object_key = self._build_slot_key(game.id, slot_number)
            presigned_url = self._generate_presigned_upload_url(object_key)

            upload_slot = GameUploadSlot(
                game_id=game.id,
                slot_number=slot_number,
                presigned_url=presigned_url,
                expires_at=expires_at,
                s3_object_key=object_key,
            )

            stage = GameStage(
                game_id=game.id,
                stage_number=slot_number,
                status="waiting_upload",
            )
            self.session.add(upload_slot)
            self.session.add(stage)
            self.session.flush()
            upload_slot.stage_id = stage.id

            slots.append(
                UploadSlot(
                    slot=slot_number,
                    presigned_url=presigned_url,
                    expires_at=expires_at,
                    accepted_content_types=list(
                        settings.allowed_upload_content_types or []
                    ),
                )
            )
        self.session.commit()
        return CreateGameResponse(
            game_id=game.id,
            mode=payload.mode,
            difficulty=payload.difficulty,
            status="waiting_upload",
            upload_slots=slots,
            time_limit_seconds=payload.time_limit_seconds or 300,
        )

    def mark_upload_complete(
        self, game_id: int, data: UploadCompleteRequest
    ) -> UploadSlotsStatusResponse:
        slot = (
            self.session.query(GameUploadSlot)
            .filter(
                GameUploadSlot.game_id == game_id,
                GameUploadSlot.slot_number == data.slot,
            )
            .one_or_none()
        )
        if slot is None:
            raise HTTPException(status_code=404, detail="Upload slot not found")

        slot.uploaded = True
        slot.s3_object_key = slot.s3_object_key or self._build_slot_key(
            game_id, slot.slot_number
        )
        slot.analysis_status = "pending"
        slot.analysis_error = None
        slot.detected_objects = None
        slot.last_analyzed_at = None
        self._validate_upload_content_type(slot.s3_object_key)
        self.session.commit()
        # run_imagen_pipeline.delay(slot.id)
        generate_puzzle_task.delay(slot.id)

        slots = (
            self.session.query(GameUploadSlot)
            .filter(GameUploadSlot.game_id == game_id)
            .order_by(GameUploadSlot.slot_number)
            .all()
        )
        all_uploaded = all(s.uploaded for s in slots)
        game = self.session.query(Game).filter(Game.id == game_id).one_or_none()
        if game is None:
            raise HTTPException(status_code=404, detail="Game not found")
        # if all_uploaded and game.status == "waiting_upload":
        #     game.status = "playing"

        status = [
            UploadSlotStatus(
                slot=s.slot_number,
                s3_object_key=s.s3_object_key,
                uploaded=s.uploaded,
                analysis_status=s.analysis_status,
                analysis_error=s.analysis_error,
                detected_objects=s.detected_objects,
                last_analyzed_at=s.last_analyzed_at,
            )
            for s in slots
        ]
        self.session.commit()

        return UploadSlotsStatusResponse(
            game_id=game_id,
            status=game.status,
            slot_statuses=status,
        )

    def get_upload_status(self, game_id: int) -> UploadSlotsStatusResponse:
        slots = (
            self.session.query(GameUploadSlot)
            .filter(GameUploadSlot.game_id == game_id)
            .order_by(GameUploadSlot.slot_number)
            .all()
        )
        status = [
            UploadSlotStatus(
                slot=slot.slot_number,
                s3_object_key=slot.s3_object_key,
                uploaded=slot.uploaded,
                analysis_status=slot.analysis_status,
                analysis_error=slot.analysis_error,
                detected_objects=slot.detected_objects,
                last_analyzed_at=slot.last_analyzed_at,
            )
            for slot in slots
        ]
        game = self.session.query(Game).filter(Game.id == game_id).one_or_none()
        if game is None:
            raise HTTPException(status_code=404, detail="Game not found")

        return UploadSlotsStatusResponse(
            game_id=game_id,
            status=game.status,
            slot_statuses=status,
        )

    def get_game_detail(self, game_id: int) -> GameDetailResponse:
        game = (
            self.session.query(Game)
            .options(
                selectinload(Game.stages).selectinload(GameStage.puzzle),
                selectinload(Game.stages)
                .selectinload(GameStage.hits)
                .selectinload(GameStageHit.difference),
            )
            .filter(Game.id == game_id)
            .one_or_none()
        )
        if game is None:
            raise HTTPException(status_code=404, detail="Game not found")

        current_stage = next(
            (
                stage
                for stage in game.stages
                if stage.status in ("waiting_puzzle", "playing")
            ),
            game.stages[-1] if game.stages else None,
        )
        current_stage_number = current_stage.stage_number if current_stage else 0
        puzzle = (
            current_stage.puzzle
            if current_stage
            and current_stage.status == "playing"
            and current_stage.puzzle
            and current_stage.puzzle.is_completed
            else None
        )
        puzzle_schema = (
            PuzzleForGameResponse(
                puzzle_id=puzzle.id,
                original_image_url=self._build_view_url(puzzle.original_image_url),
                modified_image_url=self._build_view_url(puzzle.modified_image_url),
                width=puzzle.width,
                height=puzzle.height,
                total_difference_count=len(puzzle.differences),
            )
            if puzzle
            else None
        )

        return GameDetailResponse(
            game_id=game.id,
            mode=game.mode,
            difficulty=game.difficulty,
            status=game.status,
            created_at=game.created_at,
            updated_at=game.updated_at,
            puzzle=puzzle_schema,
            current_score=game.current_score,
            current_stage=current_stage_number,
            total_stages=len(game.stages),
        )

    def check_answer(
        self, game_id: int, stage_number: int, payload: CheckAnswerRequest
    ) -> CheckAnswerResponse:
        stage = (
            self.session.query(GameStage)
            .options(
                selectinload(GameStage.puzzle),
                selectinload(GameStage.hits).selectinload(GameStageHit.difference),
                selectinload(GameStage.game),
            )
            .filter(
                GameStage.game_id == game_id,
                GameStage.stage_number == stage_number,
            )
            .one_or_none()
        )
        if stage is None:
            raise HTTPException(status_code=404, detail="Stage not found")
        if stage.puzzle is None:
            raise HTTPException(status_code=400, detail="Puzzle not ready")

        attempt = HitAttempt(x=payload.x, y=payload.y)
        print(attempt)
        matched = self._match_difference(stage.puzzle.differences, payload.x, payload.y)
        total_diffs = stage.total_difference_count or len(stage.puzzle.differences)

        if matched is None:
            return self._build_check_answer_response(
                stage,
                attempt=attempt,
                is_correct=False,
                total_difference_count=total_diffs,
            )

        already_hit_ids = {hit.difference_id for hit in stage.hits if hit.difference_id}
        if matched.id in already_hit_ids:
            return self._build_check_answer_response(
                stage,
                attempt=attempt,
                is_correct=False,
                is_already_found=True,
                total_difference_count=total_diffs,
            )

        hit = GameStageHit(stage_id=stage.id, difference_id=matched.id)
        stage.hits.append(hit)
        stage.found_difference_count += 1
        stage.game.current_score += 100
        if stage.total_difference_count is None:
            stage.total_difference_count = total_diffs

        self.session.add(hit)
        self.session.commit()
        self.session.refresh(hit)

        return self._build_check_answer_response(
            stage,
            attempt=attempt,
            is_correct=True,
            total_difference_count=total_diffs,
        )

    def complete_stage(
        self,
        game_id: int,
        stage_number: int,
        payload: StageCompleteRequest,
    ) -> StageResultResponse:
        stage = (
            self.session.query(GameStage)
            .options(
                selectinload(GameStage.puzzle),
                selectinload(GameStage.game).selectinload(Game.upload_slots),
            )
            .filter(
                GameStage.game_id == game_id,
                GameStage.stage_number == stage_number,
            )
            .one_or_none()
        )
        if stage is None:
            raise HTTPException(status_code=404, detail="Stage not found")

        stage.status = "finished"
        stage.completed_at = datetime.now()
        if stage.total_difference_count is None and stage.puzzle is not None:
            stage.total_difference_count = len(stage.puzzle.differences)

        total_stages = len(stage.game.stages)
        is_last_stage = stage_number >= total_stages

        next_stage = (
            self.session.query(GameStage)
            .options(selectinload(GameStage.puzzle))
            .filter(
                GameStage.game_id == game_id,
                GameStage.stage_number == stage_number + 1,
            )
            .one_or_none()
        )
        if next_stage and next_stage.status == "finished":
            raise HTTPException(status_code=404, detail="Stage not found")
        next_puzzle_schema = None
        next_stage_number = next_stage.stage_number if next_stage else None

        if is_last_stage:
            # 마지막 stage를 완료한 경우
            stage.game.status = "finished"
        elif (
            next_stage
            and next_stage.puzzle
            and next_stage.status == "playing"
            and next_stage.puzzle.is_completed
        ):
            # 다음 puzzle이 완료된 경우
            next_puzzle_schema = PuzzleForGameResponse(
                puzzle_id=next_stage.puzzle.id,
                original_image_url=self._build_view_url(
                    next_stage.puzzle.original_image_url
                ),
                modified_image_url=self._build_view_url(
                    next_stage.puzzle.modified_image_url
                ),
                width=next_stage.puzzle.width,
                height=next_stage.puzzle.height,
                total_difference_count=len(next_stage.puzzle.differences),
            )
            stage.game.status = "playing"
        elif next_stage:
            # 다음 stage가 있지만 puzzle이 아직 완료되지 않은 경우
            stage.game.status = "waiting_next_stage"
        else:
            # 다음 stage가 없는 경우 (데이터 불일치)
            stage.game.status = "finished"

        self.session.commit()

        return StageResultResponse(
            game_id=game_id,
            stage_number=stage_number,
            total_stages=total_stages,
            status=stage.game.status,
            current_score=stage.game.current_score,
            found_difference_count=stage.found_difference_count,
            total_difference_count=stage.total_difference_count or 0,
            next_stage_number=next_stage_number,
            next_puzzle=next_puzzle_schema,
        )

    def finish_game(
        self,
        game_id: int,
        payload: FinishGameRequest,
    ) -> FinishGameResponse:
        game = self.session.query(Game).filter(Game.id == game_id).one_or_none()
        if game is None:
            raise HTTPException(status_code=404, detail="Game not found.")

        game.status = "finished"

        final_score = game.current_score

        self.session.commit()

        return FinishGameResponse(
            game_id=game_id,
            status=game.status,
            difficulty=game.difficulty,
            final_score=final_score,
        )

    def _match_difference(self, differences, x: float, y: float):
        for diff in differences:
            if (
                diff.x <= x <= diff.x + diff.width
                and diff.y <= y <= diff.y + diff.height
            ):
                return diff
        return None

    def _build_check_answer_response(
        self,
        stage: GameStage,
        *,
        attempt: HitAttempt | None = None,
        is_correct: bool,
        is_already_found: bool = False,
        total_difference_count: int,
    ) -> CheckAnswerResponse:
        found_infos: list[FoundDifference] = []
        for hit in stage.hits:
            if hit.difference is None:
                continue
            found_infos.append(
                FoundDifference(
                    difference_id=hit.difference.id,
                    x=hit.difference.x,
                    y=hit.difference.y,
                    width=hit.difference.width,
                    height=hit.difference.height,
                    label=hit.difference.label,
                    hit_at=hit.hit_at,
                )
            )

        return CheckAnswerResponse(
            is_correct=is_correct,
            is_already_found=is_already_found,
            current_score=stage.game.current_score,
            found_difference_count=stage.found_difference_count,
            total_difference_count=total_difference_count,
            game_status=stage.game.status,
            newly_hit_difference=attempt,
            found_differences=found_infos,
        )

    def _assign_dummy_puzzle_if_needed(
        self, game: Game, slots: list[GameUploadSlot]
    ) -> None:
        """Celery/AI 없이 더미 데이터 생성"""
        if game.stages:
            game.status = "playing"
            return

        source_slot = next((slot for slot in slots if slot.uploaded), None)
        if source_slot is None:
            return

        image_key = source_slot.s3_object_key or ""
        width, height = 1024.0, 768.0
        puzzle = Puzzle(
            difficulty=game.difficulty or "normal",
            original_image_url=image_key,
            modified_image_url=image_key,
            width=width,
            height=height,
        )
        self.session.add(puzzle)
        self.session.flush()

        dummy_differences = self._build_dummy_differences(width, height)
        for index, diff in enumerate(dummy_differences):
            self.session.add(
                Difference(
                    puzzle_id=puzzle.id,
                    index=index,
                    x=diff["x"],
                    y=diff["y"],
                    width=diff["width"],
                    height=diff["height"],
                    label=diff["label"],
                )
            )

        stage = GameStage(
            game_id=game.id,
            puzzle_id=puzzle.id,
            stage_number=1,
            status="playing",
            started_at=datetime.now(),
            total_difference_count=len(dummy_differences),
        )
        self.session.add(stage)
        game.status = "playing"

    def _build_view_url(self, stored_value: str | None) -> str:
        if not stored_value:
            raise HTTPException(status_code=500, detail="Puzzle image is not available")
        if stored_value.startswith("http://") or stored_value.startswith("https://"):
            return stored_value
        if not settings.aws_s3_bucket_name:
            raise HTTPException(status_code=500, detail="S3 bucket is not configured")
        ttl = (
            settings.aws_s3_presign_ttl_seconds
            if settings.aws_s3_presign_ttl_seconds > 0
            else 900
        )
        return self.s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": settings.aws_s3_bucket_name,
                "Key": stored_value,
            },
            ExpiresIn=ttl,
        )

    def _build_dummy_differences(
        self, width: float, height: float
    ) -> list[dict[str, float | str | None]]:
        base = min(width, height) * 0.1
        return [
            {
                "x": width * 0.15,
                "y": height * 0.2,
                "width": base,
                "height": base,
                "label": "clock",
            },
            {
                "x": width * 0.55,
                "y": height * 0.4,
                "width": base,
                "height": base,
                "label": "tree",
            },
            {
                "x": width * 0.35,
                "y": height * 0.65,
                "width": base,
                "height": base,
                "label": "window",
            },
        ]


def get_game_service(session: Session = Depends(get_db)) -> GameService:
    return GameService(session)
