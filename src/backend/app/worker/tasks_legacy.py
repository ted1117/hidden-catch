from datetime import datetime
import io
import os
import tempfile

import boto3
from celery import chain
from google.cloud import vision
from PIL import Image, ImageDraw, ImageOps
from sqlalchemy import delete

from app.core.config import settings
from app.db.utils import get_session
from app.models.game import Game, GameStage
from app.models.puzzle import Difference, Puzzle
from app.models.upload_slot import GameUploadSlot
from app.worker.celery_app import celery_app

MAX_SIZE_BYTES = 27_000_000


def _calculate_overlap_ratio(
    child_box: dict[str, float], parent_box: dict[str, float]
) -> float:
    """
    Rect 사이의 포함 비율을 계산합니다.
    """
    x1, y1 = child_box["x"], child_box["y"]
    x2 = x1 + child_box["width"]
    y2 = y1 + child_box["height"]

    px1, py1 = parent_box["x"], parent_box["y"]
    px2 = px1 + parent_box["width"]
    py2 = py1 + parent_box["height"]

    # 교집합 영역 계산
    inter_x1 = max(x1, px1)
    inter_y1 = max(y1, py1)
    inter_x2 = min(x2, px2)
    inter_y2 = min(y2, py2)

    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0

    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    child_area = child_box["width"] * child_box["height"]

    if child_area == 0:
        return 0.0

    return inter_area / child_area


def _build_rect_tree_legacy(
    rects: list[dict[str, float]], labels: list[str], overlap_threshold: float = 0.9
) -> list[dict]:
    """
    [구 버전] rect들 간의 포함 관계를 기반으로 트리 구조를 생성합니다.
    90% 이상 겹치면 포함 관계로 간주합니다.
    """
    # 면적 계산 및 인덱스와 함께 저장
    rect_data: list[tuple[dict[str, float], str, float, int]] = []
    for i, (rect, label) in enumerate(zip(rects, labels)):
        area = rect["width"] * rect["height"]
        rect_data.append((rect, label, area, i))

    # 면적 내림차순으로 정렬 (큰 것부터)
    rect_data.sort(key=lambda x: x[2], reverse=True)

    # 트리 노드 생성
    nodes: list[dict] = []
    node_map: dict[int, dict] = {}  # index -> node

    for rect, label, area, original_index in rect_data:
        node = {
            "rect": rect,
            "label": label,
            "index": original_index,
            "children": [],
            "parent": None,
        }
        nodes.append(node)
        node_map[original_index] = node

    # 각 rect에 대해 부모 찾기
    for i, (rect, label, area, original_index) in enumerate(rect_data):
        current_node = node_map[original_index]

        # 자신보다 큰 rect들 중에서 90% 이상 포함되는 가장 작은 rect 찾기
        best_parent = None
        best_parent_area = float("inf")

        for j in range(i):  # 자신보다 큰 rect들만 확인
            parent_rect, parent_label, parent_area, parent_index = rect_data[j]
            parent_node = node_map[parent_index]

            # 이미 부모가 있으면 건너뛰기
            if parent_node["parent"] is not None:
                continue

            overlap_ratio = _calculate_overlap_ratio(rect, parent_rect)
            if overlap_ratio >= overlap_threshold:
                # 더 작은 부모를 선택 (더 가까운 부모)
                if parent_area < best_parent_area:
                    best_parent = parent_node
                    best_parent_area = parent_area

        if best_parent:
            best_parent["children"].append(current_node)
            current_node["parent"] = best_parent

    # 루트 노드들만 반환 (부모가 없는 노드들)
    root_nodes = [node for node in nodes if node["parent"] is None]
    return root_nodes


def _shrink_box_centered(
    box: dict[str, float], shrink_ratio: float = 0.1
) -> dict[str, float]:
    """
    박스를 중앙을 고정한 상태에서 크기를 축소
    """
    center_x = box["x"] + box["width"] / 2
    center_y = box["y"] + box["height"] / 2

    new_width = box["width"] * (1 - shrink_ratio)
    new_height = box["height"] * (1 - shrink_ratio)

    return {
        "x": center_x - new_width / 2,
        "y": center_y - new_height / 2,
        "width": new_width,
        "height": new_height,
    }


def _process_rects_with_iou_legacy(
    rects: list[dict[str, float]], labels: list[str]
) -> tuple[list[dict[str, float] | None], list[str]]:
    """
    [구 버전] 모든 rect에 대해 IoU를 계산하고 처리합니다.
    """
    processed_rects: list[dict[str, float] | None] = [rect.copy() for rect in rects]
    processed_labels = labels.copy()

    # 각 rect에 대해 다른 모든 rect와의 겹침 비율 계산 (해당 rect 면적 대비)
    for i, current_rect in enumerate(processed_rects):
        if current_rect is None:
            continue

        current_area = current_rect["width"] * current_rect["height"]
        max_overlap_ratio = 0.0

        for j, other_rect in enumerate(processed_rects):
            if i == j or other_rect is None:
                continue

            # 교집합 영역 계산
            x1_1, y1_1 = current_rect["x"], current_rect["y"]
            x2_1 = x1_1 + current_rect["width"]
            y2_1 = y1_1 + current_rect["height"]

            x1_2, y1_2 = other_rect["x"], other_rect["y"]
            x2_2 = x1_2 + other_rect["width"]
            y2_2 = y1_2 + other_rect["height"]

            inter_x1 = max(x1_1, x1_2)
            inter_y1 = max(y1_1, y1_2)
            inter_x2 = min(x2_1, x2_2)
            inter_y2 = min(y2_1, y2_2)

            if inter_x2 > inter_x1 and inter_y2 > inter_y1:
                inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
                # 현재 rect 면적 대비 겹침 비율
                overlap_ratio = inter_area / current_area if current_area > 0 else 0.0
                max_overlap_ratio = max(max_overlap_ratio, overlap_ratio)

        # 겹침 비율에 따라 처리 (해당 rect 면적 대비)
        if max_overlap_ratio >= 0.5:  # 겹침 >= 50% → 삭제
            processed_rects[i] = None
        elif max_overlap_ratio >= 0.1:  # 10% <= 겹침 < 50% → 중앙 고정하고 10% 축소
            processed_rects[i] = _shrink_box_centered(current_rect, shrink_ratio=0.1)
        # 겹침 < 10% → 그대로 유지

    return processed_rects, processed_labels


def _reduce_image_size(image_bytes: bytes, limit: int = MAX_SIZE_BYTES) -> bytes:
    """
    이미지 크기를 제한 크기 이하로 축소합니다.
    """
    current_bytes = image_bytes

    while len(current_bytes) > limit:
        with Image.open(io.BytesIO(current_bytes)) as img:
            img = img.convert("RGB")

            new_width = max(1, int(img.width * 0.7))
            new_height = max(1, int(img.height * 0.7))

            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            with io.BytesIO() as output:
                img.save(output, format="PNG")
                current_bytes = output.getvalue()

    return current_bytes


def _build_mask_from_detections_legacy(
    detection_results: list[dict],
    canvas_size: tuple[int, int],
) -> tuple[Image.Image, str]:
    """
    [구 버전] 탐지 결과로부터 마스크 이미지와 프롬프트를 생성합니다.
    """
    from PIL import ImageFilter

    mask_image = Image.new("L", canvas_size, 0)
    draw = ImageDraw.Draw(mask_image)
    combined_prompt_list: list[str] = []

    for item in detection_results:
        box = item.get("pixel_box")
        if not box:
            continue
        prompt_idea = item.get("prompt") or f"Modify {item.get('name', 'object')}."
        draw.rectangle([box["xmin"], box["ymin"], box["xmax"], box["ymax"]], fill=255)
        combined_prompt_list.append(prompt_idea)

    if not combined_prompt_list:
        raise ValueError("No valid detection prompts to build Imagen request.")

    mask_image = mask_image.filter(ImageFilter.GaussianBlur(radius=5))
    final_prompt = " ".join(combined_prompt_list)
    return mask_image, final_prompt


def modify_image_with_imagen_legacy(
    original_image_path: str, detection_results: list[dict]
) -> bytes | None:
    """
    [구 버전] Imagen을 사용하여 이미지를 수정합니다.
    """
    from google import genai
    from google.genai import types

    if not detection_results:
        raise ValueError("detection_results must not be empty.")

    with Image.open(original_image_path) as opened:
        pil_original = opened.convert("RGB")
        width, height = pil_original.size

    # 마스크 생성 함수 호출
    mask_image, final_prompt = _build_mask_from_detections_legacy(
        detection_results,
        (width, height),
    )

    # 마스크 강제 이진화 처리
    mask_image = mask_image.convert("L").point(lambda x: 255 if x > 100 else 0)

    original_bytes_io = io.BytesIO()
    mask_bytes_io = io.BytesIO()

    pil_original.save(original_bytes_io, format="PNG")
    mask_image.save(mask_bytes_io, format="PNG")

    original_bytes = original_bytes_io.getvalue()
    mask_bytes = mask_bytes_io.getvalue()

    # Reference 설정
    raw_ref = types.RawReferenceImage(
        reference_id=1,
        reference_image=types.Image(image_bytes=original_bytes, mime_type="image/png"),
    )

    mask_ref = types.MaskReferenceImage(
        reference_id=2,
        reference_image=types.Image(image_bytes=mask_bytes, mime_type="image/png"),
        config=types.MaskReferenceConfig(
            mask_mode=types.MaskReferenceMode.MASK_MODE_USER_PROVIDED,
            mask_dilation=0,
        ),
    )

    client = genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location="us-central1",
    )

    try:
        response = client.models.edit_image(
            model="imagen-3.0-capability-001",
            prompt=final_prompt,
            reference_images=[raw_ref, mask_ref],
            config=types.EditImageConfig(
                edit_mode=types.EditMode.EDIT_MODE_INPAINT_INSERTION,
                number_of_images=1,
                output_mime_type="image/png",
            ),
        )
    except Exception as e:
        print(f"Imagen API Error Detail: {e}")
        return None

    if response.generated_images:
        return response.generated_images[0].image.image_bytes

    return None


@celery_app.task(name="app.worker.detect.detect_objects_for_slot")
def detect_objects_for_slot(slot_id: int):
    """
    [구 버전] GameUploadSlot에서 슬롯을 가져와 Vision API로 오브젝트를 탐지하고 DB에 저장합니다.
    """
    with get_session() as session:
        slot = session.get(GameUploadSlot, slot_id)
        if slot is None:
            return

        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )

        s3_response = s3_client.get_object(
            Bucket=settings.aws_s3_bucket_name, Key=slot.s3_object_key
        )

        image_bytes = s3_response["Body"].read()

        if len(image_bytes) > MAX_SIZE_BYTES:
            image_bytes = _reduce_image_size(image_bytes, limit=MAX_SIZE_BYTES)
            s3_client.put_object(
                Bucket=settings.aws_s3_bucket_name,
                Key=slot.s3_object_key,
                Body=image_bytes,
                ContentType="image/png",
            )

        # EXIF orientation으로 사진 방향 고정
        with Image.open(io.BytesIO(image_bytes)) as img:
            img_with_exif = ImageOps.exif_transpose(img)
            img_with_exif = img_with_exif.convert("RGB")
            image_width, image_height = img_with_exif.size

            normalized_output = io.BytesIO()
            img_with_exif.save(normalized_output, format="PNG")
            normalized_image_bytes = normalized_output.getvalue()

        client = vision.ImageAnnotatorClient()
        image = vision.Image(content=normalized_image_bytes)

        objects = client.object_localization(image=image).localized_object_annotations  # type: ignore
        if not objects:
            slot.analysis_error = "No objects detected."
            slot.analysis_status = "failed"
            slot.last_analyzed_at = datetime.now()
            return

        game = session.get(Game, slot.game_id)
        if game is None:
            slot.analysis_status = "failed"
            slot.analysis_error = "Game not found."
            slot.last_analyzed_at = datetime.now()
            return

        existing_stage = (
            session.get(GameStage, slot.stage_id) if slot.stage_id else None
        )
        puzzle = (
            existing_stage.puzzle if existing_stage and existing_stage.puzzle else None
        )

        if puzzle is None:
            puzzle = Puzzle(
                difficulty=game.difficulty,
                original_image_url=slot.s3_object_key,
                modified_image_url=slot.s3_object_key,
                width=image_width,
                height=image_height,
            )
            session.add(puzzle)
            session.flush()

            if existing_stage is not None:
                existing_stage.puzzle_id = puzzle.id
            else:
                existing_stage = GameStage(
                    game_id=game.id,
                    puzzle_id=puzzle.id,
                    stage_number=slot.slot_number,
                    status="waiting_puzzle",
                    started_at=datetime.now(),
                )
                session.add(existing_stage)
                session.flush()
                slot.stage_id = existing_stage.id
                game.status = "waiting_next_stage"

        # 먼저 모든 원본 rect 수집
        original_rects: list[dict[str, float]] = []
        original_labels: list[str] = []
        for object_ in objects:
            label = object_.name
            vertices = object_.bounding_poly.normalized_vertices

            v_min, v_max = vertices[0], vertices[2]

            x = v_min.x * image_width
            y = v_min.y * image_height
            width = (v_max.x - v_min.x) * image_width
            height = (v_max.y - v_min.y) * image_height

            original_rects.append({"x": x, "y": y, "width": width, "height": height})
            original_labels.append(label)

        # 전체 이미지 면적 계산
        total_image_area = image_width * image_height
        size_threshold = 0.4  # 40% 이상인 rect 제외

        # 너무 큰 rect 제외 (전체 이미지 면적의 40% 이상)
        size_filtered_rects: list[dict[str, float]] = []
        size_filtered_labels: list[str] = []
        for rect, label in zip(original_rects, original_labels):
            rect_area = rect["width"] * rect["height"]
            area_ratio = rect_area / total_image_area
            if area_ratio < size_threshold:
                size_filtered_rects.append(rect)
                size_filtered_labels.append(label)

        # 트리 구조 생성 (90% 이상 겹치면 포함 관계)
        tree = _build_rect_tree_legacy(
            size_filtered_rects, size_filtered_labels, overlap_threshold=0.9
        )

        # 트리 구조에서 부모-자식 관계가 있는 경우 더 큰 rect(부모) 제외
        excluded_indices: set[int] = set()

        def mark_parents_for_exclusion(node: dict) -> None:
            """부모-자식 관계에서 부모(더 큰 rect)를 제외 목록에 추가"""
            if node["children"]:
                # 자식이 있으면 부모(자신) 제외
                excluded_indices.add(node["index"])
                # 자식들도 재귀적으로 확인
                for child in node["children"]:
                    mark_parents_for_exclusion(child)

        # 모든 루트 노드에서 시작하여 부모 제외
        for root_node in tree:
            mark_parents_for_exclusion(root_node)

        # 제외되지 않은 rect만 필터링
        filtered_rects: list[dict[str, float]] = []
        filtered_labels: list[str] = []
        for i, (rect, label) in enumerate(
            zip(size_filtered_rects, size_filtered_labels)
        ):
            if i not in excluded_indices:
                filtered_rects.append(rect)
                filtered_labels.append(label)

        # IoU에 따라 처리 (50% 이상 삭제, 10-50% 축소, 10% 미만 유지)
        processed_rects, processed_labels = _process_rects_with_iou_legacy(
            filtered_rects, filtered_labels
        )

        # 처리된 rect로 Difference 생성
        detected: list[dict] = []
        stored_differences: list[Difference] = []
        index = 0

        stmt = delete(Difference).where(Difference.puzzle_id == puzzle.id)
        session.execute(stmt)
        for processed_rect, label in zip(processed_rects, processed_labels):
            # 삭제된 rect는 건너뛰기
            if processed_rect is None:
                continue

            index += 1
            x = processed_rect["x"]
            y = processed_rect["y"]
            width = processed_rect["width"]
            height = processed_rect["height"]

            # normalized rect 계산 (0-1000 스케일)
            normalized_rect = [
                int((y / image_height) * 1000),
                int((x / image_width) * 1000),
                int(((y + height) / image_height) * 1000),
                int(((x + width) / image_width) * 1000),
            ]

            detected.append(
                {
                    "label": label,
                    "rect": normalized_rect,
                }
            )

            difference = Difference(
                puzzle_id=puzzle.id,
                index=index,
                x=x,
                y=y,
                width=width,
                height=height,
                label=label,
            )

            session.add(difference)
            session.flush()
            stored_differences.append(difference)

        if settings.debug:
            debug_image = Image.open(io.BytesIO(normalized_image_bytes)).convert("RGB")
            draw = ImageDraw.Draw(debug_image)
            for diff in stored_differences:
                x1, y1 = diff.x, diff.y
                x2 = x1 + diff.width
                y2 = y1 + diff.height
                draw.rectangle(
                    [(x1, y1), (x2, y2)],
                    outline="red",
                    width=3,
                )
                if diff.label:
                    draw.text(
                        (x1, max(0, y1 - 12)),
                        diff.label,
                        fill="red",
                    )
            debug_image.show(title=f"slot-{slot_id}-detections")

        slot.detected_objects = detected
        slot.analysis_status = "completed"
        slot.analysis_error = None
        slot.last_analyzed_at = datetime.now()
        if existing_stage:
            existing_stage.total_difference_count = len(detected)
            existing_stage.status = "waiting_puzzle"

    return {
        "slot_id": slot.id,
        "detected": detected,
        "image_bytes": normalized_image_bytes,
    }


@celery_app.task(name="app.worker.detect.edit_image_with_imagen3")
def edit_image_with_imagen3(payload: dict):
    """
    [구 버전] 탐지된 오브젝트를 기반으로 Imagen을 사용하여 이미지를 수정합니다.
    """
    slot_id = payload["slot_id"]
    detected = payload["detected"]
    image_bytes = payload["image_bytes"]
    with get_session() as session:
        slot = session.get(GameUploadSlot, slot_id)
        if slot is None or not slot.s3_object_key:
            return

        s3_object_key = slot.s3_object_key
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )

        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                image_width, image_height = img.size
        except Exception as exc:
            slot.analysis_status = "failed"
            slot.analysis_error = f"Invalid image: {exc}"
            slot.last_analyzed_at = datetime.now()
            return

        detection_results: list[dict] = []
        for item in detected:
            rect = item.get("rect")
            if not rect or len(rect) != 4:
                continue
            ymin, xmin, ymax, xmax = rect
            pixel_box = {
                "xmin": int(xmin / 1000 * image_width),
                "ymin": int(ymin / 1000 * image_height),
                "xmax": int(xmax / 1000 * image_width),
                "ymax": int(ymax / 1000 * image_height),
            }
            detection_results.append(
                {
                    "name": item.get("label") or "object",
                    "pixel_box": pixel_box,
                    "prompt": item.get("prompt")
                    or f"Modify {item.get('label') or 'object'} to create a difference.",
                }
            )

        if not detection_results:
            slot.analysis_status = "failed"
            slot.analysis_error = "No detection results for Imagen."
            slot.last_analyzed_at = datetime.now()
            return

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        try:
            temp_file.write(image_bytes)
            temp_file.flush()
            temp_file_path = temp_file.name
        finally:
            temp_file.close()

        try:
            imagen_bytes = modify_image_with_imagen_legacy(
                temp_file_path,
                detection_results,
            )
        except Exception as exc:
            imagen_bytes = None
            slot.analysis_status = "failed"
            slot.analysis_error = f"Imagen edit failed: {exc}"
            slot.last_analyzed_at = datetime.now()
        finally:
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

        if not imagen_bytes:
            return

        output_key = s3_object_key.replace(".png", "-imagen.png")
        s3_client.put_object(
            Bucket=settings.aws_s3_bucket_name,
            Key=output_key,
            Body=imagen_bytes,
            ContentType="image/png",
        )

        game = session.get(Game, slot.game_id)
        if game is None:
            slot.analysis_status = "failed"
            slot.analysis_error = "Game not found."
            slot.last_analyzed_at = datetime.now()
            return

        stage = session.get(GameStage, slot.stage_id) if slot.stage_id else None
        if stage is None:
            stage = GameStage(
                game_id=game.id,
                stage_number=slot.slot_number,
                status="waiting_puzzle",
                started_at=datetime.now(),
            )
            session.add(stage)
            session.flush()
            slot.stage_id = stage.id
        if not stage or not stage.puzzle:
            slot.analysis_status = "failed"
            slot.analysis_error = "Puzzle not found."
            slot.last_analyzed_at = datetime.now()
            return

        stage.puzzle.modified_image_url = output_key
        stage.puzzle.is_completed = True
        stage.status = "playing"
        game.status = "playing"

        slot.analysis_status = "completed"
        slot.analysis_error = None
        slot.last_analyzed_at = datetime.now()


@celery_app.task(name="app.worker.detect.run_imagen_pipeline")
def run_imagen_pipeline(slot_id: int) -> None:
    """
    [구 버전] 구 버전 Imagen 파이프라인을 실행합니다.
    """
    chain(
        detect_objects_for_slot.s(slot_id),
        edit_image_with_imagen3.s(),
    ).delay()
