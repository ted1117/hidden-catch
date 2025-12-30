from __future__ import annotations

import io
from typing import Any

import boto3
from google.cloud import vision
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.core.config import settings

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


def _reduce_image_size(
    image_bytes: bytes,
    limit: int = MAX_SIZE_BYTES,
    output_format: str = "JPEG",
    quality: int = 90,
) -> bytes:
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
                img.save(
                    output,
                    format=output_format,
                    quality=quality,
                    optimize=True,
                )
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
    rect_data: list[tuple[dict[str, float | str], float, int]] = []
    for i, rect in enumerate(rects):
        area = float(rect["width"]) * float(rect["height"])
        rect_data.append((rect, area, i))

    rect_data.sort(key=lambda x: x[1], reverse=True)

    nodes: list[dict] = []
    node_map: dict[int, dict] = {}

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

    for i, (rect, area, original_index) in enumerate(rect_data):
        current_node = node_map[original_index]

        best_parent = None
        best_parent_area = float("inf")

        for j in range(i):
            parent_rect, parent_area, parent_index = rect_data[j]
            parent_node = node_map[parent_index]

            if parent_node["parent"] is not None:
                continue

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
                if parent_area < best_parent_area:
                    best_parent = parent_node
                    best_parent_area = parent_area

        if best_parent:
            best_parent["children"].append(current_node)
            current_node["parent"] = best_parent

    return [node for node in nodes if node["parent"] is None]


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

    for i, current_rect in enumerate(processed_rects):
        if current_rect is None:
            continue

        current_area = float(current_rect["width"]) * float(current_rect["height"])
        max_overlap_ratio = 0.0

        for j, other_rect in enumerate(processed_rects):
            if i == j or other_rect is None:
                continue

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
                overlap_ratio = inter_area / current_area if current_area > 0 else 0.0
                max_overlap_ratio = max(max_overlap_ratio, overlap_ratio)

        if max_overlap_ratio >= 0.5:
            processed_rects[i] = None
        elif max_overlap_ratio >= 0.1:
            rect_only = {
                "x": float(current_rect["x"]),
                "y": float(current_rect["y"]),
                "width": float(current_rect["width"]),
                "height": float(current_rect["height"]),
            }
            shrunk_rect = _shrink_box_centered(rect_only, shrink_ratio=0.1)
            processed_rects[i] = {
                "x": shrunk_rect["x"],
                "y": shrunk_rect["y"],
                "width": shrunk_rect["width"],
                "height": shrunk_rect["height"],
                "label": current_rect["label"],
            }

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

    image_source = (
        original_image_bytes
        if isinstance(original_image_bytes, io.BytesIO)
        else io.BytesIO(original_image_bytes)
    )

    with Image.open(image_source) as opened:
        pil_original = opened.convert("RGB")
        width, height = pil_original.size

    mask_image, final_prompt = _build_mask_from_detections(
        detection_results,
        (width, height),
    )

    mask_image = mask_image.convert("L").point(lambda x: 255 if x > 100 else 0)

    original_bytes_io = io.BytesIO()
    mask_bytes_io = io.BytesIO()

    pil_original.save(
        original_bytes_io,
        format="JPEG",
        quality=95,
        optimize=True,
    )
    mask_image.save(mask_bytes_io, format="PNG")

    original_bytes = original_bytes_io.getvalue()
    mask_bytes = mask_bytes_io.getvalue()

    raw_ref = types.RawReferenceImage(
        reference_id=1,
        reference_image=types.Image(
            image_bytes=original_bytes,
            mime_type="image/jpeg",
        ),
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


__all__ = [
    "MAX_SIZE_BYTES",
    "_build_s3_client",
    "_modify_image_with_imagen",
    "_process_rects_with_iou",
    "_reduce_image_size",
    "detect_objects_logic",
    "filter_rects_by_overlap",
    "filter_rects_by_size",
]
