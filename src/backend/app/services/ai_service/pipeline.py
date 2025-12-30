from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import io

from botocore.exceptions import ClientError
from PIL import Image, ImageOps
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.game import Game, GameStage
from app.models.puzzle import Difference, Puzzle
from app.models.upload_slot import GameUploadSlot
from app.services.ai_service.helpers import (
    MAX_SIZE_BYTES,
    _build_s3_client,
    _modify_image_with_imagen,
    _process_rects_with_iou,
    _reduce_image_size,
    detect_objects_logic,
    filter_rects_by_overlap,
    filter_rects_by_size,
)


@dataclass(frozen=True)
class EditPayload:
    slot_id: int
    s3_object_key: str
    rects: list[dict]


def validate_upload_slot(session: Session, slot_id: int) -> bool:
    """
    Validate that the slot exists and the uploaded object is acceptable.
    """
    slot = session.get(GameUploadSlot, slot_id)
    if slot is None:
        return False
    if not slot.s3_object_key:
        slot.analysis_status = "failed"
        slot.analysis_error = "Missing s3_object_key."
        slot.last_analyzed_at = datetime.now()
        return False
    if not settings.aws_s3_bucket_name:
        slot.analysis_status = "failed"
        slot.analysis_error = "S3 bucket is not configured."
        slot.last_analyzed_at = datetime.now()
        return False

    s3_client = _build_s3_client()
    try:
        metadata = s3_client.head_object(
            Bucket=settings.aws_s3_bucket_name,
            Key=slot.s3_object_key,
        )
    except ClientError:
        slot.analysis_status = "failed"
        slot.analysis_error = "Uploaded object not found in storage."
        slot.last_analyzed_at = datetime.now()
        return False

    content_type = (metadata.get("ContentType") or "").strip()
    allowed = settings.allowed_upload_content_types or []
    if allowed and content_type not in allowed:
        slot.analysis_status = "failed"
        slot.analysis_error = f"Unsupported content type: {content_type}"
        slot.last_analyzed_at = datetime.now()
        return False

    return True


def detect_objects_for_slot(session: Session, slot_id: int) -> bool:
    """
    Run detection and store filtered rects on the slot.
    """
    slot = session.get(GameUploadSlot, slot_id)
    if slot is None or not slot.s3_object_key:
        return False
    if slot.analysis_status == "failed":
        return False

    detection_result = detect_objects_logic(slot.s3_object_key)
    if detection_result is None:
        slot.analysis_status = "failed"
        slot.analysis_error = "No objects detected."
        slot.last_analyzed_at = datetime.now()
        return False

    original_rects, image_width, image_height, _normalized_image_bytes = (
        detection_result
    )

    size_filtered_rects = filter_rects_by_size(
        original_rects, image_width, image_height, size_threshold=0.4
    )
    overlap_filtered_rects = filter_rects_by_overlap(
        size_filtered_rects, overlap_threshold=0.9
    )
    processed_rects = _process_rects_with_iou(overlap_filtered_rects)
    final_rects: list[dict[str, float | str]] = [
        rect for rect in processed_rects if rect is not None
    ]

    if not final_rects:
        slot.analysis_status = "failed"
        slot.analysis_error = "No valid rects after filtering."
        slot.last_analyzed_at = datetime.now()
        return False

    slot.detected_objects = [
        {
            "x": float(rect["x"]),
            "y": float(rect["y"]),
            "width": float(rect["width"]),
            "height": float(rect["height"]),
            "label": str(rect["label"]),
        }
        for rect in final_rects
    ]

    return True


def prepare_edit_payload(session: Session, slot_id: int) -> EditPayload | None:
    """
    Load slot data needed for Imagen edit without doing external calls.
    """
    slot = session.get(GameUploadSlot, slot_id)
    if slot is None or not slot.s3_object_key:
        return None
    if slot.analysis_status == "failed":
        return None

    if slot.stage_id:
        stage = session.get(GameStage, slot.stage_id)
        if stage and stage.puzzle and stage.puzzle.is_completed:
            return None

    rects = slot.detected_objects or []
    if not rects:
        slot.analysis_status = "failed"
        slot.analysis_error = "No detected objects to edit."
        slot.last_analyzed_at = datetime.now()
        return None

    return EditPayload(slot_id=slot.id, s3_object_key=slot.s3_object_key, rects=rects)


def run_imagen_edit(payload: EditPayload) -> tuple[str, int, int] | None:
    """
    Run Imagen edit and store the result in S3.
    """
    normalized_image_bytes, image_width, image_height = _load_normalized_image_from_s3(
        payload.s3_object_key
    )
    detection_results = _build_detection_results(payload.rects)

    imagen_bytes = _modify_image_with_imagen(
        normalized_image_bytes,
        detection_results,
    )
    if not imagen_bytes:
        return None

    s3_client = _build_s3_client()
    output_key = _build_imagen_output_key(payload.s3_object_key)
    s3_client.put_object(
        Bucket=settings.aws_s3_bucket_name,
        Key=output_key,
        Body=imagen_bytes,
        ContentType="image/png",
    )

    return output_key, image_width, image_height


def edit_image_for_slot(session: Session, slot_id: int) -> tuple[str, int, int] | None:
    """
    Edit the image with Imagen and store the result in S3.
    """
    payload = prepare_edit_payload(session, slot_id)
    if payload is None:
        return None

    result = run_imagen_edit(payload)
    if result is None:
        slot = session.get(GameUploadSlot, slot_id)
        if slot:
            slot.analysis_status = "failed"
            slot.analysis_error = "Imagen edit returned no result."
            slot.last_analyzed_at = datetime.now()
        return None

    return result


def assign_stage_for_slot(
    session: Session,
    slot_id: int,
    *,
    output_key: str,
    image_width: int,
    image_height: int,
) -> bool:
    """
    Assign the edited puzzle to the next empty stage and store differences.
    """
    slot = session.get(GameUploadSlot, slot_id)
    if slot is None or not slot.s3_object_key:
        return False
    if slot.analysis_status == "failed":
        return False

    rects = slot.detected_objects or []
    if not rects:
        slot.analysis_status = "failed"
        slot.analysis_error = "No detected objects to assign."
        slot.last_analyzed_at = datetime.now()
        return False

    game = session.get(Game, slot.game_id)
    if game is None:
        slot.analysis_status = "failed"
        slot.analysis_error = "Game not found."
        slot.last_analyzed_at = datetime.now()
        return False

    stage = None
    if slot.stage_id:
        stage = session.get(GameStage, slot.stage_id)
        if stage and stage.puzzle and stage.puzzle.is_completed:
            # Avoid duplicate stage assignment on retries or duplicate calls.
            return True

    if stage is None:
        stage = _select_next_empty_stage(session, game.id)
    if stage is None:
        slot.analysis_status = "failed"
        slot.analysis_error = "No empty stage available."
        slot.last_analyzed_at = datetime.now()
        return False

    if stage.puzzle is None:
        puzzle = Puzzle(
            difficulty=game.difficulty,
            original_image_url=slot.s3_object_key,
            modified_image_url=output_key,
            width=image_width,
            height=image_height,
            is_completed=True,
        )
        session.add(puzzle)
        session.flush()
        stage.puzzle_id = puzzle.id
        slot.stage_id = stage.id
    else:
        puzzle = stage.puzzle
        puzzle.modified_image_url = output_key
        puzzle.is_completed = True
        if not puzzle.original_image_url:
            puzzle.original_image_url = slot.s3_object_key
        if not puzzle.width or not puzzle.height:
            puzzle.width = image_width
            puzzle.height = image_height

    session.execute(delete(Difference).where(Difference.puzzle_id == puzzle.id))
    differences = _build_differences(puzzle.id, rects)
    session.add_all(differences)

    stage.total_difference_count = len(rects)
    stage.status = "playing"
    if stage.started_at is None:
        stage.started_at = datetime.now()

    game.status = "playing"

    slot.analysis_status = "completed"
    slot.analysis_error = None
    slot.last_analyzed_at = datetime.now()
    return True


def edit_and_assign_stage(session: Session, slot_id: int) -> bool:
    """
    Backwards-compatible wrapper to run edit and stage assignment in one call.
    """
    edit_result = edit_image_for_slot(session, slot_id)
    if edit_result is None:
        slot = session.get(GameUploadSlot, slot_id)
        stage = (
            session.get(GameStage, slot.stage_id) if slot and slot.stage_id else None
        )
        if stage and stage.puzzle and stage.puzzle.is_completed:
            return True
        return False

    output_key, image_width, image_height = edit_result
    return assign_stage_for_slot(
        session,
        slot_id,
        output_key=output_key,
        image_width=image_width,
        image_height=image_height,
    )


def _load_normalized_image_from_s3(s3_object_key: str) -> tuple[bytes, int, int]:
    s3_client = _build_s3_client()
    s3_response = s3_client.get_object(
        Bucket=settings.aws_s3_bucket_name,
        Key=s3_object_key,
    )
    image_bytes = s3_response["Body"].read()

    if len(image_bytes) > MAX_SIZE_BYTES:
        image_bytes = _reduce_image_size(image_bytes, limit=MAX_SIZE_BYTES)
        s3_client.put_object(
            Bucket=settings.aws_s3_bucket_name,
            Key=s3_object_key,
            Body=image_bytes,
            ContentType="image/jpeg",
        )

    with Image.open(io.BytesIO(image_bytes)) as img:
        img_with_exif = ImageOps.exif_transpose(img)
        img_with_exif = img_with_exif.convert("RGB")
        image_width, image_height = img_with_exif.size

        normalized_output = io.BytesIO()
        img_with_exif.save(
            normalized_output,
            format="JPEG",
            quality=95,
            optimize=True,
        )
        normalized_image_bytes = normalized_output.getvalue()

    return normalized_image_bytes, image_width, image_height


def _build_detection_results(rects: list[dict]) -> list[dict]:
    detection_results: list[dict] = []
    for rect in rects:
        x = float(rect["x"])
        y = float(rect["y"])
        width = float(rect["width"])
        height = float(rect["height"])
        label = str(rect.get("label") or "object")

        detection_results.append(
            {
                "name": label,
                "pixel_box": {
                    "xmin": int(x),
                    "ymin": int(y),
                    "xmax": int(x + width),
                    "ymax": int(y + height),
                },
                "prompt": f"Modify {label} to create a difference.",
            }
        )

    return detection_results


def _build_differences(puzzle_id: int, rects: list[dict]) -> list[Difference]:
    differences: list[Difference] = []
    for index, rect in enumerate(rects, start=1):
        differences.append(
            Difference(
                puzzle_id=puzzle_id,
                index=index,
                x=float(rect["x"]),
                y=float(rect["y"]),
                width=float(rect["width"]),
                height=float(rect["height"]),
                label=str(rect.get("label") or ""),
            )
        )
    return differences


def _build_imagen_output_key(s3_object_key: str) -> str:
    if s3_object_key.endswith(".png"):
        return s3_object_key[: -len(".png")] + "-imagen.png"
    return s3_object_key + "-imagen.png"


def _select_next_empty_stage(session: Session, game_id: int) -> GameStage | None:
    return session.execute(
        select(GameStage)
        .where(GameStage.game_id == game_id, GameStage.puzzle_id.is_(None))
        .order_by(GameStage.stage_number.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
