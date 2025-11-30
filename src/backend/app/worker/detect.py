import io
import json
from typing import Sequence, cast

from google import genai
from google.genai import types
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFilter
from pydantic import BaseModel
from vertexai.preview.vision_models import Image, ImageGenerationModel

from app.core.config import settings


class DetectedObjects(BaseModel):
    object_name: str
    box_2d: list[float]
    type: str
    modification_idea: str


def find_game_objects_normalized(image_bytes: bytes):
    with PILImage.open(io.BytesIO(image_bytes)) as img:
        img_width, img_height = img.size

    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/png")
    client = genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location="us-central1",
    )

    prompt = """
    You are an expert Game Level Designer for a "Spot the Difference" puzzle game.
    Your task is to analyze the image and identify 5 distinct objects to modify.
     **Selection Criteria:**
     1. Select objects that are clearly visible and distinct from the background.
     2. EXCLUDE objects that are too small, blurry, or have complex/ambiguous boundaries.
     3. Focus on objects where a change (e.g., color change, removal, replacement) would be noticeable.
     4. Ensure that the `box_2d` regions of the selected objects overlap as little as possible.
     5. If an overlap is unavoidable, prioritize modifying the object with the larger area.
    **Output Requirements:**
    1. Provide the output STRICTLY in valid JSON format.
    2. Do NOT use Markdown formatting.
    3. The `box_2d` coordinates must be in the format `[ymin, xmin, ymax, xmax]`.
    4. **IMPORTANT:** Values must be **normalized floats between 0.0 and 1.0** (relative to the image size).
    **JSON Example (Follow this pattern):**
    [
        {
            "object_name": "Man's Shirt",
            "box_2d": [0.45, 0.30, 0.60, 0.50],
            "type": "Color Change",
            "modification_idea": "Change the shirt color to bright yellow"
        }
    ]
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[image_part, prompt],
        config=types.GenerateContentConfigDict(
            response_mime_type="application/json",
            response_json_schema=DetectedObjects.model_json_schema(),
        ),
    )

    if response is None:
        return

    if response.text is None:
        return
    try:
        raw_data = json.loads(response.text)
        if isinstance(raw_data, dict):
            result_items = [raw_data]
        elif isinstance(raw_data, list):
            result_items = raw_data
        else:
            raise ValueError("Gemini returned unsupported JSON structure.")

        final_result = []
        for item in result_items:
            norm_box = item["box_2d"]

            ymin, xmin, ymax, xmax = norm_box

            pixel_box = {
                "ymin": int(ymin * img_height),
                "xmin": int(xmin * img_width),
                "ymax": int(ymax * img_height),
                "xmax": int(xmax * img_width),
            }

            processed_item = {
                "name": item["object_name"],
                "prompt": item["modification_idea"],
                "normalized": norm_box,
                "pixel_box": pixel_box,
            }
            final_result.append(processed_item)

        return final_result
    except Exception as e:
        print(e)
        return []


def _build_mask_from_detections(
    detection_results: Sequence[dict],
    canvas_size: tuple[int, int],
):
    mask_image = PILImage.new("L", canvas_size, 0)
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


def _initialize_imagen_client():
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location="us-central1",
    )


def modify_image_with_imagen_legacy(original_image_path, detection_results):
    """
    [구 버전] Imagen을 사용하여 이미지를 수정합니다.
    """
    if not detection_results:
        raise ValueError("detection_results must not be empty.")

    with PILImage.open(original_image_path) as opened:
        pil_original = opened.convert("RGB")
        width, height = pil_original.size

    # 마스크 생성 함수 호출
    mask_image, final_prompt = _build_mask_from_detections(
        detection_results,
        (width, height),
    )

    # ============================================================
    # [핵심 수정] 마스크 강제 이진화 처리 (오류 해결 파트)
    # 1. 흑백(L) 모드로 변환
    # 2. 128 기준으로 완전한 검은색(0)과 흰색(255)으로 나눔
    # 3. 1-bit 픽셀(mode='1')로 변환하지 말고 'L'이나 'RGB' 유지 권장 (호환성 위해)
    # ============================================================
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
            mask_dilation=0,  # 영역을 살짝(5%) 넓혀 경계선 어색함 방지
        ),
    )

    client = _initialize_imagen_client()

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


def modify_image_with_imagen(original_image_bytes, detection_results):
    """
    Imagen을 사용하여 이미지를 수정합니다.
    """
    if not detection_results:
        raise ValueError("detection_results must not be empty.")

    # 바이트를 BytesIO로 변환
    image_source = (
        original_image_bytes
        if isinstance(original_image_bytes, io.BytesIO)
        else io.BytesIO(original_image_bytes)
    )

    with PILImage.open(image_source) as opened:
        pil_original = opened.convert("RGB")
        width, height = pil_original.size

    # 마스크 생성 함수 호출
    mask_image, final_prompt = _build_mask_from_detections(
        detection_results,
        (width, height),
    )

    # ============================================================
    # [핵심 수정] 마스크 강제 이진화 처리 (오류 해결 파트)
    # 1. 흑백(L) 모드로 변환
    # 2. 128 기준으로 완전한 검은색(0)과 흰색(255)으로 나눔
    # 3. 1-bit 픽셀(mode='1')로 변환하지 말고 'L'이나 'RGB' 유지 권장 (호환성 위해)
    # ============================================================
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
            mask_dilation=0,  # 영역을 살짝(5%) 넓혀 경계선 어색함 방지
        ),
    )

    client = _initialize_imagen_client()

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


def modify_image_with_imagen2(original_image_path, detection_results):
    pil_original = PILImage.open(original_image_path)
    width, height = pil_original.size

    mask_image = PILImage.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask_image)

    combined_prompt_list = []

    for item in detection_results:
        box = item["pixel_box"]
        prompt_idea = item["prompt"]

        draw.rectangle([box["xmin"], box["ymin"], box["xmax"], box["ymax"]], fill=255)

        combined_prompt_list.append(prompt_idea)

    mask_image = mask_image.filter(ImageFilter.GaussianBlur(radius=5))

    final_prompt = " ".join(combined_prompt_list)

    original_bytes = io.BytesIO()
    pil_original.save(original_bytes, format="PNG")
    vertex_original = Image(original_bytes.getvalue())

    mask_bytes = io.BytesIO()
    mask_image.save(mask_bytes, format="PNG")
    vertex_mask = Image(mask_bytes.getvalue())

    generation_model = ImageGenerationModel.from_pretrained("imagegeneration@006")
    response = generation_model.edit_image(
        base_image=vertex_original,
        mask=vertex_mask,
        prompt=final_prompt,
        guidance_scale=60,  # 프롬프트를 얼마나 따를지 (높을수록 프롬프트 충실)
        mask_mode="semantic",  # 마스크 안쪽을 수정
    )

    if response.images:
        return response.images[0]._image_bytes
