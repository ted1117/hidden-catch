from datetime import datetime
import io
from typing import Any

import boto3
from google.cloud import vision
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.game import Game, GameStage
from app.models.puzzle import Difference, Puzzle
from app.models.upload_slot import GameUploadSlot

MAX_SIZE_BYTES = 27_000_000


def _build_s3_client() -> Any:
    """
    S3 클라이언트를 생성합니다.

    Returns:
        boto3 S3 클라이언트
    """
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    return s3_client


def _reduce_image_size(image_bytes: bytes, limit: int = MAX_SIZE_BYTES) -> bytes:
    """
    이미지 크기를 제한 크기 이하로 축소합니다.

    Args:
        image_bytes: 원본 이미지 바이트
        limit: 최대 크기 (바이트)

    Returns:
        축소된 이미지 바이트
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


def _calculate_overlap_ratio(
    child_box: dict[str, float], parent_box: dict[str, float]
) -> float:
    """
    Rect 사이의 포함 비율을 계산합니다.

    Args:
        child_box: 자식 박스 {'x': float, 'y': float, 'width': float, 'height': float}
        parent_box: 부모 박스 {'x': float, 'y': float, 'width': float, 'height': float}

    Returns:
        child_box 면적 대비 겹침 비율 (0.0 ~ 1.0)
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


def _shrink_box_centered(
    box: dict[str, float], shrink_ratio: float = 0.1
) -> dict[str, float]:
    """
    박스를 중앙을 고정한 상태에서 크기를 축소합니다.

    Args:
        box: 축소할 박스 {'x': float, 'y': float, 'width': float, 'height': float}
        shrink_ratio: 축소 비율 (기본값: 0.1 = 10%)

    Returns:
        축소된 박스 (중앙 고정)
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


def _build_rect_tree(
    rects: list[dict[str, float | str]], overlap_threshold: float = 0.9
) -> list[dict]:
    """
    rect들 간의 포함 관계를 기반으로 트리 구조를 생성합니다.
    90% 이상 겹치면 포함 관계로 간주합니다.

    Args:
        rects: rect 리스트 [{'x': float, 'y': float, 'width': float, 'height': float, 'label': str}, ...]
        overlap_threshold: 포함 관계로 간주할 겹침 비율 (기본값: 0.9 = 90%)

    Returns:
        트리 구조 리스트 [{'rect': dict, 'label': str, 'children': list, 'index': int}, ...]
        루트 노드들만 반환되며, 각 노드는 children 리스트를 가집니다.
    """
    # 면적 계산 및 인덱스와 함께 저장
    rect_data: list[tuple[dict[str, float | str], float, int]] = []
    for i, rect in enumerate(rects):
        area = float(rect["width"]) * float(rect["height"])
        rect_data.append((rect, area, i))

    # 면적 내림차순으로 정렬 (큰 것부터)
    rect_data.sort(key=lambda x: x[1], reverse=True)

    # 트리 노드 생성
    nodes: list[dict] = []
    node_map: dict[int, dict] = {}  # index -> node

    for rect, area, original_index in rect_data:
        node = {
            "rect": rect,
            "label": str(rect["label"]),
            "index": original_index,
            "children": [],
            "parent": None,
        }
        nodes.append(node)
        node_map[original_index] = node

    # 각 rect에 대해 부모 찾기
    for i, (rect, area, original_index) in enumerate(rect_data):
        current_node = node_map[original_index]

        # 자신보다 큰 rect들 중에서 90% 이상 포함되는 가장 작은 rect 찾기
        best_parent = None
        best_parent_area = float("inf")

        for j in range(i):  # 자신보다 큰 rect들만 확인
            parent_rect, parent_area, parent_index = rect_data[j]
            parent_node = node_map[parent_index]

            # 이미 부모가 있으면 건너뛰기
            if parent_node["parent"] is not None:
                continue

            # rect만 추출하여 overlap 계산
            rect_only = {
                "x": float(rect["x"]),
                "y": float(rect["y"]),
                "width": float(rect["width"]),
                "height": float(rect["height"]),
            }
            parent_rect_only = {
                "x": float(parent_rect["x"]),
                "y": float(parent_rect["y"]),
                "width": float(parent_rect["width"]),
                "height": float(parent_rect["height"]),
            }
            overlap_ratio = _calculate_overlap_ratio(rect_only, parent_rect_only)
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


def _process_rects_with_iou(
    rects: list[dict[str, float | str]],
) -> list[dict[str, float | str] | None]:
    """
    모든 rect에 대해 IoU를 계산하고 처리합니다.

    Args:
        rects: rect 리스트 [{'x': float, 'y': float, 'width': float, 'height': float, 'label': str}, ...]

    Returns:
        처리된 rect 리스트 (삭제된 것은 None)
    """
    processed_rects: list[dict[str, float | str] | None] = [
        rect.copy() for rect in rects
    ]

    # 각 rect에 대해 다른 모든 rect와의 겹침 비율 계산 (해당 rect 면적 대비)
    for i, current_rect in enumerate(processed_rects):
        if current_rect is None:
            continue

        current_area = float(current_rect["width"]) * float(current_rect["height"])
        max_overlap_ratio = 0.0

        for j, other_rect in enumerate(processed_rects):
            if i == j or other_rect is None:
                continue

            # 교집합 영역 계산
            x1_1, y1_1 = float(current_rect["x"]), float(current_rect["y"])
            x2_1 = x1_1 + float(current_rect["width"])
            y2_1 = y1_1 + float(current_rect["height"])

            x1_2, y1_2 = float(other_rect["x"]), float(other_rect["y"])
            x2_2 = x1_2 + float(other_rect["width"])
            y2_2 = y1_2 + float(other_rect["height"])

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
            # rect만 추출하여 축소
            rect_only = {
                "x": float(current_rect["x"]),
                "y": float(current_rect["y"]),
                "width": float(current_rect["width"]),
                "height": float(current_rect["height"]),
            }
            shrunk_rect = _shrink_box_centered(rect_only, shrink_ratio=0.1)
            # 라벨 유지하면서 축소된 좌표 업데이트
            processed_rects[i] = {
                "x": shrunk_rect["x"],
                "y": shrunk_rect["y"],
                "width": shrunk_rect["width"],
                "height": shrunk_rect["height"],
                "label": current_rect["label"],
            }
        # 겹침 < 10% → 그대로 유지

    return processed_rects


def _build_mask_from_detections(
    detection_results: list[dict],
    canvas_size: tuple[int, int],
) -> tuple[Image.Image, str]:
    """
    탐지 결과로부터 마스크 이미지와 프롬프트를 생성합니다.

    Args:
        detection_results: 탐지 결과 리스트
        canvas_size: 캔버스 크기 (width, height)

    Returns:
        (마스크 이미지, 최종 프롬프트)
    """
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


def _modify_image_with_imagen(
    original_image_bytes: bytes | io.BytesIO, detection_results: list[dict]
) -> bytes | None:
    """
    Imagen을 사용하여 이미지를 수정합니다.

    Args:
        original_image_bytes: 이미지 바이트(bytes 또는 BytesIO)
        detection_results: 탐지 결과 리스트

    Returns:
        수정된 이미지 바이트 또는 None
    """
    from google import genai
    from google.genai import types

    if not detection_results:
        raise ValueError("detection_results must not be empty.")

    # 바이트를 BytesIO로 변환
    image_source = (
        original_image_bytes
        if isinstance(original_image_bytes, io.BytesIO)
        else io.BytesIO(original_image_bytes)
    )

    with Image.open(image_source) as opened:
        pil_original = opened.convert("RGB")
        width, height = pil_original.size

    # 마스크 생성 함수 호출
    mask_image, final_prompt = _build_mask_from_detections(
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


def detect_objects_logic(
    s3_object_key: str,
) -> tuple[list[dict[str, float | str]], int, int, bytes] | None:
    """
    S3에서 이미지를 가져와 Vision API로 오브젝트를 탐지합니다.

    Args:
        s3_object_key: S3 객체 키

    Returns:
        tuple[list[dict[str, float | str]], int, int, bytes] | None:
            (rect 리스트, 이미지 너비, 이미지 높이, 이미지 바이트) 또는 None
            rect는 {'x': float, 'y': float, 'width': float, 'height': float, 'label': str} 형태
    """
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
            ContentType="image/png",
        )

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
        return None

    original_rects: list[dict[str, float | str]] = []

    for object in objects:
        label = object.name
        vertices = object.bounding_poly.normalized_vertices
        if not label or not vertices:
            continue

        v_min, v_max = vertices[0], vertices[2]

        x = v_min.x * image_width
        y = v_min.y * image_height
        puzzle_width = (v_max.x - v_min.x) * image_width
        puzzle_height = (v_max.y - v_min.y) * image_height

        original_rects.append(
            {
                "x": x,
                "y": y,
                "width": puzzle_width,
                "height": puzzle_height,
                "label": label,
            }
        )

    return original_rects, image_width, image_height, normalized_image_bytes


def filter_rects_by_size(
    original_rects: list[dict[str, float | str]],
    image_width: int,
    image_height: int,
    size_threshold: float = 0.4,
) -> list[dict[str, float | str]]:
    """
    이미지 면적의 size_threshold 이상인 rect를 제외합니다.

    Args:
        original_rects: rect 리스트 [{'x': float, 'y': float, 'width': float, 'height': float, 'label': str}, ...]
        image_width: 이미지 너비
        image_height: 이미지 높이
        size_threshold: 제외할 면적 비율 (기본값: 0.4 = 40%)

    Returns:
        필터링된 rect 리스트
    """
    total_image_area = image_width * image_height

    # 이미지 면적의 size_threshold 이상인 rect 제외
    size_filtered_rects: list[dict[str, float | str]] = []
    for rect in original_rects:
        rect_area = float(rect["width"]) * float(rect["height"])
        area_ratio = rect_area / total_image_area
        if area_ratio < size_threshold:
            size_filtered_rects.append(rect)

    return size_filtered_rects


def filter_rects_by_overlap(
    rects: list[dict[str, float | str]], overlap_threshold: float = 0.9
) -> list[dict[str, float | str]]:
    """
    부모-자식 관계가 있는 경우 더 큰 rect(부모)를 제외합니다.

    Args:
        rects: rect 리스트 [{'x': float, 'y': float, 'width': float, 'height': float, 'label': str}, ...]
        overlap_threshold: 포함 관계로 간주할 겹침 비율 (기본값: 0.9 = 90%)

    Returns:
        필터링된 rect 리스트
    """
    tree = _build_rect_tree(rects, overlap_threshold=overlap_threshold)

    excluded_indices: set[int] = set()

    # 스택으로 DFS
    stack = tree[:]
    while stack:
        node = stack.pop()
        if node["children"]:
            excluded_indices.add(node["index"])
        stack.extend(node["children"])

    filtered_rects: list[dict[str, float | str]] = []
    for i, rect in enumerate(rects):
        if i not in excluded_indices:
            filtered_rects.append(rect)

    return filtered_rects


def process_puzzle_generation(session: Session, slot_id: int) -> None:
    """
    퍼즐 생성 파이프라인을 실행합니다.
    detect -> filter -> puzzle DB creation -> imagen edit -> save 과정을 순차적으로 수행합니다.

    Args:
        session: DB 세션
        slot_id: GameUploadSlot ID

    Returns:
        None

    Raises:
        ValueError: slot을 찾을 수 없거나 필수 데이터가 없는 경우
    """
    slot = session.get(GameUploadSlot, slot_id)
    if slot is None or not slot.s3_object_key:
        raise ValueError(f"Slot {slot_id} not found or missing s3_object_key")

    # 1. Vision API로 오브젝트 탐지
    detection_result = detect_objects_logic(slot.s3_object_key)
    if detection_result is None:
        slot.analysis_status = "failed"
        slot.analysis_error = "No objects detected."
        slot.last_analyzed_at = datetime.now()
        return

    original_rects, image_width, image_height, normalized_image_bytes = detection_result

    # 2. 크기 필터링
    size_filtered_rects = filter_rects_by_size(
        original_rects, image_width, image_height, size_threshold=0.4
    )

    # 3. 겹침 필터링 (부모-자식 관계 제거)
    overlap_filtered_rects = filter_rects_by_overlap(
        size_filtered_rects, overlap_threshold=0.9
    )

    # 4. IoU 처리 (50% 이상 삭제, 10-50% 축소, 10% 미만 유지)
    processed_rects = _process_rects_with_iou(overlap_filtered_rects)

    # None 제거
    final_rects: list[dict[str, float | str]] = [
        rect for rect in processed_rects if rect is not None
    ]

    if not final_rects:
        slot.analysis_status = "failed"
        slot.analysis_error = "No valid rects after filtering."
        slot.last_analyzed_at = datetime.now()
        return

    # 5. Game과 GameStage 조회/생성
    game = session.get(Game, slot.game_id)
    if game is None:
        slot.analysis_status = "failed"
        slot.analysis_error = "Game not found."
        slot.last_analyzed_at = datetime.now()
        return

    existing_stage = session.get(GameStage, slot.stage_id) if slot.stage_id else None
    puzzle = existing_stage.puzzle if existing_stage and existing_stage.puzzle else None

    if puzzle is None:
        puzzle = Puzzle(
            difficulty=game.difficulty,
            original_image_url=slot.s3_object_key,
            modified_image_url=slot.s3_object_key,
            width=image_width,
            height=image_height,
            is_completed=False,
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
    else:
        # puzzle이 이미 존재하는 경우, 기존 Difference 삭제 (재실행 시 중복 방지)
        stmt = delete(Difference).where(Difference.puzzle_id == puzzle.id)
        session.execute(stmt)

    # 6. 새 Difference 생성

    differences: list[Difference] = []
    for index, rect in enumerate(final_rects, start=1):
        difference = Difference(
            puzzle_id=puzzle.id,
            index=index,
            x=float(rect["x"]),
            y=float(rect["y"]),
            width=float(rect["width"]),
            height=float(rect["height"]),
            label=str(rect["label"]),
        )
        differences.append(difference)

    session.add_all(differences)
    session.flush()

    # 7. Imagen으로 이미지 수정
    detection_results: list[dict] = []
    for rect in final_rects:
        x = float(rect["x"])
        y = float(rect["y"])
        width = float(rect["width"])
        height = float(rect["height"])
        label = str(rect["label"])

        pixel_box = {
            "xmin": int(x),
            "ymin": int(y),
            "xmax": int(x + width),
            "ymax": int(y + height),
        }
        detection_results.append(
            {
                "name": label or "object",
                "pixel_box": pixel_box,
                "prompt": f"Modify {label or 'object'} to create a difference.",
            }
        )

    try:
        imagen_bytes = _modify_image_with_imagen(
            normalized_image_bytes,
            detection_results,
        )
    except Exception as exc:
        imagen_bytes = None
        slot.analysis_status = "failed"
        slot.analysis_error = f"Imagen edit failed: {exc}"
        slot.last_analyzed_at = datetime.now()
        return

    if not imagen_bytes:
        slot.analysis_status = "failed"
        slot.analysis_error = "Imagen edit returned no result."
        slot.last_analyzed_at = datetime.now()
        return

    # 8. 수정된 이미지를 S3에 저장
    s3_client = _build_s3_client()
    output_key = slot.s3_object_key.replace(".png", "-imagen.png")
    s3_client.put_object(
        Bucket=settings.aws_s3_bucket_name,
        Key=output_key,
        Body=imagen_bytes,
        ContentType="image/png",
    )

    # 9. Puzzle과 GameStage 업데이트
    puzzle.modified_image_url = output_key
    puzzle.is_completed = True

    if existing_stage:
        existing_stage.total_difference_count = len(final_rects)
        existing_stage.status = "playing"
    else:
        # 이미 생성했지만 혹시 모를 경우를 대비
        stage = session.get(GameStage, slot.stage_id)
        if stage:
            stage.total_difference_count = len(final_rects)
            stage.status = "playing"

    game.status = "playing"

    # 10. Slot 상태 업데이트
    slot.analysis_status = "completed"
    slot.analysis_error = None
    slot.last_analyzed_at = datetime.now()
