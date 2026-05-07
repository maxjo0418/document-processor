"""pdfium 시각 primitive 분석 코드.

ODL이 텍스트/표 구조를 잘 못 잡는 경우를 보완하기 위해 PDF 페이지의 선,
박스, 배경 도형을 pdfium으로 직접 읽는다. 여기서는 그런 저수준 primitive를
정리해서 dotted/segmented rule, axis-aligned box, visual block 후보로
바꾸는 일만 한다. DocIR 수정은 `normalize.py`에서 한다.
"""

from __future__ import annotations

from ...models import PageInfo
from ..meta import PdfBoundingBox
from .models import (
    PdfPreviewVisualBlockCandidate,
    PdfPreviewVisualPrimitive,
    _VISUAL_BOX_SEED_MIN_SIZE_PT,
    _VISUAL_FRAME_MIN_SIZE_PT,
    _VISUAL_LINE_JOIN_TOLERANCE_PT,
    _VISUAL_MIN_LINE_SEGMENT_PT,
    _VISUAL_OPEN_FRAME_PRIMITIVE_LIMIT,
    _VISUAL_SEGMENTED_AXIS_TOLERANCE_PT,
    _VISUAL_SEGMENTED_GAP_TOLERANCE_PT,
    _VISUAL_SEGMENTED_MAX_FRAGMENT_PT,
    _VISUAL_SEGMENTED_MIN_PARTS,
    _VISUAL_SEGMENTED_MIN_SPAN_PT,
    _VISUAL_TOUCH_TOLERANCE_PT,
)
from .shared import (
    _bbox_area,
    _bbox_center,
    _bbox_contains,
    _bbox_from_bounds,
    _bbox_intersection,
    _bbox_touches_or_near,
    _shared_bbox_distance,
    _shared_page_content_margins,
    _union_box_bounds,
)


# ---------- primitive 판별/기본 geometry ----------

def _has_visible_stroke(primitive: PdfPreviewVisualPrimitive) -> bool:
    if not primitive.has_stroke:
        return False
    if primitive.stroke_color is None:
        return False
    rgba = primitive.stroke_color.removeprefix("#")
    if len(rgba) != 8:
        return True
    try:
        red = int(rgba[0:2], 16)
        green = int(rgba[2:4], 16)
        blue = int(rgba[4:6], 16)
        alpha = int(rgba[6:8], 16)
    except ValueError:
        return True
    if alpha < 16:
        return False
    if red >= 245 and green >= 245 and blue >= 245:
        return False
    return True


def _has_visible_fill(primitive: PdfPreviewVisualPrimitive) -> bool:
    if not primitive.has_fill:
        return False
    if primitive.fill_color is None:
        return False
    rgba = primitive.fill_color.removeprefix("#")
    if len(rgba) != 8:
        return True
    try:
        red = int(rgba[0:2], 16)
        green = int(rgba[2:4], 16)
        blue = int(rgba[4:6], 16)
        alpha = int(rgba[6:8], 16)
    except ValueError:
        return True
    if alpha < 16:
        return False
    if red >= 245 and green >= 245 and blue >= 245:
        return False
    return True


def _primitive_rule_color(primitive: PdfPreviewVisualPrimitive) -> str | None:
    if _has_visible_stroke(primitive):
        return primitive.stroke_color
    if _has_visible_fill(primitive):
        return primitive.fill_color
    return None


def _primitive_size(primitive: PdfPreviewVisualPrimitive) -> tuple[float, float]:
    bbox = primitive.bounding_box
    return (
        max(bbox.right_pt - bbox.left_pt, 0.0),
        max(bbox.top_pt - bbox.bottom_pt, 0.0),
    )


def _primitive_bbox_line_orientation(
    primitive: PdfPreviewVisualPrimitive,
    *,
    page_width: float,
    page_height: float,
    min_length_pt: float,
) -> str | None:
    width, height = _primitive_size(primitive)
    if width <= 0.0 or height <= 0.0:
        return None
    narrow_width = max(page_width * 0.03, 10.0)
    narrow_height = max(page_height * 0.03, 10.0)
    if width <= narrow_width and height > width and height > min_length_pt:
        return "vertical"
    if height <= narrow_height and width > height and width > min_length_pt:
        return "horizontal"
    return None


def _filled_thin_rect_line_orientation(
    primitive: PdfPreviewVisualPrimitive,
) -> str | None:
    if primitive.object_type != "path" or not primitive.is_axis_aligned_box:
        return None
    if not _has_visible_fill(primitive):
        return None
    width, height = _primitive_size(primitive)
    if width <= 0.0 or height <= 0.0:
        return None
    max_thickness = 3.0
    if width <= max_thickness and height > _VISUAL_MIN_LINE_SEGMENT_PT and height > width * 3:
        return "vertical"
    if height <= max_thickness and width > _VISUAL_MIN_LINE_SEGMENT_PT and width > height * 3:
        return "horizontal"
    return None


def _primitive_line_span(primitive: PdfPreviewVisualPrimitive, orientation: str) -> float:
    start, end = _primitive_line_span_range(primitive, orientation)
    return max(end - start, 0.0)


def _primitive_line_span_range(
    primitive: PdfPreviewVisualPrimitive,
    orientation: str,
) -> tuple[float, float]:
    bbox = primitive.bounding_box
    if orientation == "horizontal":
        return bbox.left_pt, bbox.right_pt
    return bbox.bottom_pt, bbox.top_pt


def _primitive_line_axis_center(primitive: PdfPreviewVisualPrimitive, orientation: str) -> float:
    bbox = primitive.bounding_box
    if orientation == "horizontal":
        return (bbox.top_pt + bbox.bottom_pt) / 2.0
    return (bbox.left_pt + bbox.right_pt) / 2.0


def _line_primitives_belong_to_same_frame(
    left: PdfPreviewVisualPrimitive,
    right: PdfPreviewVisualPrimitive,
) -> bool:
    left_orientation = _primitive_line_orientation(left)
    right_orientation = _primitive_line_orientation(right)
    if left_orientation is None or right_orientation is None:
        return False

    left_endpoints = _primitive_line_endpoints(left)
    right_endpoints = _primitive_line_endpoints(right)
    if not left_endpoints or not right_endpoints:
        return False

    if left_orientation != right_orientation:
        return any(
            _point_distance(left_point, right_point) <= _VISUAL_LINE_JOIN_TOLERANCE_PT
            for left_point in left_endpoints
            for right_point in right_endpoints
        )

    if left_orientation == "horizontal":
        same_axis = abs(left_endpoints[0][1] - right_endpoints[0][1]) <= _VISUAL_LINE_JOIN_TOLERANCE_PT
    else:
        same_axis = abs(left_endpoints[0][0] - right_endpoints[0][0]) <= _VISUAL_LINE_JOIN_TOLERANCE_PT
    if not same_axis:
        return False

    return any(
        _point_distance(left_point, right_point) <= _VISUAL_LINE_JOIN_TOLERANCE_PT
        for left_point in left_endpoints
        for right_point in right_endpoints
    )


def _is_open_frame_component(component: list[PdfPreviewVisualPrimitive]) -> bool:
    if len(component) < 3:
        return False

    orientations = {_primitive_line_orientation(primitive) for primitive in component}
    if "horizontal" not in orientations or "vertical" not in orientations:
        return False

    bbox = _union_visual_primitive_bboxes(component)
    if bbox is None:
        return False
    width = bbox.right_pt - bbox.left_pt
    height = bbox.top_pt - bbox.bottom_pt
    return width >= _VISUAL_FRAME_MIN_SIZE_PT and height >= _VISUAL_FRAME_MIN_SIZE_PT


def _horizontal_line_matches_box_boundary(
    primitive: PdfPreviewVisualPrimitive,
    *,
    left_x: float,
    right_x: float,
) -> bool:
    if _primitive_line_orientation(primitive) != "horizontal":
        return False
    if right_x - left_x < _VISUAL_BOX_SEED_MIN_SIZE_PT:
        return False
    line_left, line_right = _primitive_line_span_range(primitive, "horizontal")
    if line_left > left_x + _VISUAL_TOUCH_TOLERANCE_PT:
        return False
    if line_right < right_x - _VISUAL_TOUCH_TOLERANCE_PT:
        return False

    span = line_right - line_left
    if abs(line_left - left_x) <= _VISUAL_TOUCH_TOLERANCE_PT and abs(line_right - right_x) <= _VISUAL_TOUCH_TOLERANCE_PT:
        return True
    return span <= (right_x - left_x) * 1.35


def _vertical_line_matches_box_boundary(
    primitive: PdfPreviewVisualPrimitive,
    *,
    x: float,
    bottom_y: float,
    top_y: float,
) -> bool:
    if _primitive_line_orientation(primitive) != "vertical":
        return False
    x_center = _primitive_line_axis_center(primitive, "vertical")
    if abs(x_center - x) > _VISUAL_TOUCH_TOLERANCE_PT:
        return False
    line_bottom, line_top = _primitive_line_span_range(primitive, "vertical")
    return line_bottom <= bottom_y + _VISUAL_TOUCH_TOLERANCE_PT and line_top >= top_y - _VISUAL_TOUCH_TOLERANCE_PT


def _dedupe_seed_bboxes(seed_bboxes: list[PdfBoundingBox]) -> list[PdfBoundingBox]:
    if not seed_bboxes:
        return []

    kept: list[PdfBoundingBox] = []
    for candidate in sorted(
        seed_bboxes,
        key=lambda item: (
            item.top_pt,
            item.left_pt,
            (item.right_pt - item.left_pt) * (item.top_pt - item.bottom_pt),
        ),
    ):
        if any(
            _bbox_overlap_ratio(existing, candidate) >= 0.95
            or _bbox_contains(existing, candidate, tolerance_pt=_VISUAL_TOUCH_TOLERANCE_PT)
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _primitive_belongs_to_axis_box(
    primitive: PdfPreviewVisualPrimitive,
    axis_box_bbox: PdfBoundingBox,
) -> bool:
    if _bbox_contains(axis_box_bbox, primitive.bounding_box, tolerance_pt=_VISUAL_TOUCH_TOLERANCE_PT):
        return True

    orientation = _primitive_line_orientation(primitive)
    bbox = primitive.bounding_box
    if orientation == "vertical":
        x_center = (bbox.left_pt + bbox.right_pt) / 2.0
        y_overlap = not (
            bbox.top_pt < axis_box_bbox.bottom_pt - _VISUAL_TOUCH_TOLERANCE_PT
            or bbox.bottom_pt > axis_box_bbox.top_pt + _VISUAL_TOUCH_TOLERANCE_PT
        )
        return y_overlap and (
            abs(x_center - axis_box_bbox.left_pt) <= _VISUAL_TOUCH_TOLERANCE_PT
            or abs(x_center - axis_box_bbox.right_pt) <= _VISUAL_TOUCH_TOLERANCE_PT
        )
    if orientation == "horizontal":
        y_center = (bbox.top_pt + bbox.bottom_pt) / 2.0
        overlap_width = min(axis_box_bbox.right_pt, bbox.right_pt) - max(axis_box_bbox.left_pt, bbox.left_pt)
        box_width = max(axis_box_bbox.right_pt - axis_box_bbox.left_pt, 0.0)
        if box_width <= 0.0:
            return False
        return (
            overlap_width >= box_width * 0.70
            and axis_box_bbox.bottom_pt - _VISUAL_TOUCH_TOLERANCE_PT <= y_center <= axis_box_bbox.top_pt + _VISUAL_TOUCH_TOLERANCE_PT
        )
    return False


def _union_visual_primitive_bboxes(
    primitives: list[PdfPreviewVisualPrimitive],
) -> PdfBoundingBox | None:
    if not primitives:
        return None
    return PdfBoundingBox(
        left_pt=min(primitive.bounding_box.left_pt for primitive in primitives),
        bottom_pt=min(primitive.bounding_box.bottom_pt for primitive in primitives),
        right_pt=max(primitive.bounding_box.right_pt for primitive in primitives),
        top_pt=max(primitive.bounding_box.top_pt for primitive in primitives),
    )


def _primitive_is_long_rule(primitive: PdfPreviewVisualPrimitive) -> bool:
    roles = set(primitive.candidate_roles)
    return "long_vertical_rule" in roles or "long_horizontal_rule" in roles


def _bbox_overlap_ratio(left: PdfBoundingBox, right: PdfBoundingBox) -> float:
    intersection = _bbox_intersection(left, right)
    if intersection is None:
        return 0.0
    intersection_area = _bbox_area(intersection)
    if intersection_area <= 0.0:
        return 0.0
    left_area = _bbox_area(left)
    right_area = _bbox_area(right)
    if left_area <= 0.0 or right_area <= 0.0:
        return 0.0
    return max(intersection_area / left_area, intersection_area / right_area)


def _primitive_line_orientation(primitive: PdfPreviewVisualPrimitive) -> str | None:
    roles = set(primitive.candidate_roles)
    if "horizontal_line_segment" in roles:
        return "horizontal"
    if "vertical_line_segment" in roles:
        return "vertical"
    return None


def _primitive_line_endpoints(
    primitive: PdfPreviewVisualPrimitive,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    bbox = primitive.bounding_box
    orientation = _primitive_line_orientation(primitive)
    if orientation == "horizontal":
        y = (bbox.top_pt + bbox.bottom_pt) / 2.0
        return (bbox.left_pt, y), (bbox.right_pt, y)
    if orientation == "vertical":
        x = (bbox.left_pt + bbox.right_pt) / 2.0
        return (x, bbox.bottom_pt), (x, bbox.top_pt)
    return None


def _point_distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    return ((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2) ** 0.5


def _point_bucket_keys(point: tuple[float, float], *, tolerance_pt: float) -> list[tuple[int, int]]:
    bucket_size = max(tolerance_pt, 1.0)
    base_x = int(point[0] // bucket_size)
    base_y = int(point[1] // bucket_size)
    return [
        (base_x + dx, base_y + dy)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
    ]


# ---------- pdfium 객체 추출 ----------

def _extract_pdfium_visual_primitives(
    page,  # noqa: ANN001
    *,
    page_number: int,
    include_fill_only_rules: bool = False,
    raw_module=None,  # noqa: ANN001
) -> list[PdfPreviewVisualPrimitive]:
    raw = raw_module
    if raw is None:
        try:
            import pypdfium2.raw as raw
        except Exception:
            return []

    page_width = page.get_width() or 0.0
    page_height = page.get_height() or 0.0
    primitives: list[PdfPreviewVisualPrimitive] = []
    for draw_order, obj in enumerate(page.get_objects()):
        bounds = obj.get_bounds()
        if bounds is None:
            continue

        bbox = _bbox_from_bounds(bounds)
        if bbox is None:
            continue

        object_type = _pdfium_object_type_name(raw, obj.raw)
        if object_type != "path":
            continue

        primitives.append(
            PdfPreviewVisualPrimitive(
                page_number=page_number,
                draw_order=draw_order,
                object_type=object_type,
                bounding_box=bbox,
                fill_color=_pdfium_color(raw, obj.raw, getter=raw.FPDFPageObj_GetFillColor),
                stroke_color=_pdfium_color(raw, obj.raw, getter=raw.FPDFPageObj_GetStrokeColor),
                stroke_width_pt=_pdfium_stroke_width(raw, obj.raw),
                has_fill=_pdfium_has_fill(raw, obj.raw),
                has_stroke=_pdfium_has_stroke(raw, obj.raw),
                is_axis_aligned_box=object_type == "path" and _pdfium_is_axis_aligned_box(raw, obj.raw),
            )
        )

    segmented_primitives = _build_segmented_rule_primitives(
        primitives,
        page_width=page_width,
        page_height=page_height,
        include_fill_only_rules=include_fill_only_rules,
    )
    primitives.extend(segmented_primitives)
    primitives.extend(_build_axis_box_edge_primitives(primitives))
    for primitive in primitives:
        primitive.candidate_roles = _candidate_roles_for_visual_primitive(
            primitive,
            page_width=page_width,
            page_height=page_height,
            include_fill_only_rules=include_fill_only_rules,
        )

    return [
        primitive
        for primitive in primitives
        if primitive.candidate_roles
        and (
            not (primitive.object_type == "path" and primitive.is_axis_aligned_box)
            or (
                include_fill_only_rules
                and _filled_thin_rect_line_orientation(primitive) is not None
            )
        )
    ]


def extract_pdfium_table_rule_primitives(
    page,  # noqa: ANN001
    *,
    page_number: int,
    raw_module=None,  # noqa: ANN001
) -> list[PdfPreviewVisualPrimitive]:
    """canonical table split 보강용 선 primitive만 외부에 공개한다."""

    primitives = _extract_pdfium_visual_primitives(
        page,
        page_number=page_number,
        include_fill_only_rules=True,
        raw_module=raw_module,
    )
    return [
        primitive
        for primitive in primitives
        if {"horizontal_line_segment", "vertical_line_segment"} & set(primitive.candidate_roles)
    ]


# ---------- dotted/segmented rule과 box edge 합성 ----------

def _build_segmented_rule_primitives(
    primitives: list[PdfPreviewVisualPrimitive],
    *,
    page_width: float,
    page_height: float,
    include_fill_only_rules: bool = False,
) -> list[PdfPreviewVisualPrimitive]:
    buckets: dict[tuple[int, str, str, int], list[PdfPreviewVisualPrimitive]] = {}
    for primitive in primitives:
        rule_color = _primitive_rule_color(primitive)
        if rule_color is None:
            continue
        orientation = _primitive_bbox_line_orientation(
            primitive,
            page_width=page_width,
            page_height=page_height,
            min_length_pt=0.0,
        )
        if orientation is None and include_fill_only_rules:
            orientation = _filled_thin_rect_line_orientation(primitive)
        if orientation is None:
            continue
        line_span = _primitive_line_span(primitive, orientation)
        if line_span <= 0.0 or line_span > _VISUAL_SEGMENTED_MAX_FRAGMENT_PT:
            continue
        axis_value = _primitive_line_axis_center(primitive, orientation)
        bucket_key = (
            primitive.page_number,
            orientation,
            rule_color,
            round(axis_value / _VISUAL_SEGMENTED_AXIS_TOLERANCE_PT),
        )
        buckets.setdefault(bucket_key, []).append(primitive)

    synthetic_primitives: list[PdfPreviewVisualPrimitive] = []
    next_draw_order = max((primitive.draw_order for primitive in primitives), default=-1) + 1
    for (page_number, orientation, stroke_color, _axis_bucket), group in buckets.items():
        group.sort(key=lambda item: _primitive_line_span_range(item, orientation)[0])
        run: list[PdfPreviewVisualPrimitive] = []
        for primitive in group:
            if not run:
                run = [primitive]
                continue
            if _segmented_rule_can_extend(run[-1], primitive, orientation):
                run.append(primitive)
                continue
            synthetic = _build_segmented_rule_primitive(
                run,
                page_number=page_number,
                orientation=orientation,
                stroke_color=stroke_color,
                draw_order=next_draw_order,
            )
            if synthetic is not None:
                synthetic_primitives.append(synthetic)
                next_draw_order += 1
            run = [primitive]

        synthetic = _build_segmented_rule_primitive(
            run,
            page_number=page_number,
            orientation=orientation,
            stroke_color=stroke_color,
            draw_order=next_draw_order,
        )
        if synthetic is not None:
            synthetic_primitives.append(synthetic)
            next_draw_order += 1

    return synthetic_primitives


def _build_axis_box_edge_primitives(
    primitives: list[PdfPreviewVisualPrimitive],
) -> list[PdfPreviewVisualPrimitive]:
    synthetic_primitives: list[PdfPreviewVisualPrimitive] = []
    next_draw_order = max((primitive.draw_order for primitive in primitives), default=-1) + 1
    for primitive in primitives:
        if not primitive.is_axis_aligned_box or not _has_visible_stroke(primitive):
            continue

        bbox = primitive.bounding_box
        stroke_width = max(primitive.stroke_width_pt or 1.0, 1.0)
        half_stroke = stroke_width / 2.0
        synthetic_primitives.extend(
            [
                PdfPreviewVisualPrimitive(
                    page_number=primitive.page_number,
                    draw_order=next_draw_order,
                    object_type="axis_box_edge_horizontal",
                    bounding_box=PdfBoundingBox(
                        left_pt=bbox.left_pt,
                        bottom_pt=bbox.top_pt - half_stroke,
                        right_pt=bbox.right_pt,
                        top_pt=bbox.top_pt + half_stroke,
                    ),
                    fill_color=None,
                    stroke_color=primitive.stroke_color,
                    stroke_width_pt=stroke_width,
                    has_fill=False,
                    has_stroke=True,
                    is_axis_aligned_box=False,
                ),
                PdfPreviewVisualPrimitive(
                    page_number=primitive.page_number,
                    draw_order=next_draw_order + 1,
                    object_type="axis_box_edge_horizontal",
                    bounding_box=PdfBoundingBox(
                        left_pt=bbox.left_pt,
                        bottom_pt=bbox.bottom_pt - half_stroke,
                        right_pt=bbox.right_pt,
                        top_pt=bbox.bottom_pt + half_stroke,
                    ),
                    fill_color=None,
                    stroke_color=primitive.stroke_color,
                    stroke_width_pt=stroke_width,
                    has_fill=False,
                    has_stroke=True,
                    is_axis_aligned_box=False,
                ),
                PdfPreviewVisualPrimitive(
                    page_number=primitive.page_number,
                    draw_order=next_draw_order + 2,
                    object_type="axis_box_edge_vertical",
                    bounding_box=PdfBoundingBox(
                        left_pt=bbox.left_pt - half_stroke,
                        bottom_pt=bbox.bottom_pt,
                        right_pt=bbox.left_pt + half_stroke,
                        top_pt=bbox.top_pt,
                    ),
                    fill_color=None,
                    stroke_color=primitive.stroke_color,
                    stroke_width_pt=stroke_width,
                    has_fill=False,
                    has_stroke=True,
                    is_axis_aligned_box=False,
                ),
                PdfPreviewVisualPrimitive(
                    page_number=primitive.page_number,
                    draw_order=next_draw_order + 3,
                    object_type="axis_box_edge_vertical",
                    bounding_box=PdfBoundingBox(
                        left_pt=bbox.right_pt - half_stroke,
                        bottom_pt=bbox.bottom_pt,
                        right_pt=bbox.right_pt + half_stroke,
                        top_pt=bbox.top_pt,
                    ),
                    fill_color=None,
                    stroke_color=primitive.stroke_color,
                    stroke_width_pt=stroke_width,
                    has_fill=False,
                    has_stroke=True,
                    is_axis_aligned_box=False,
                ),
            ]
        )
        next_draw_order += 4
    return synthetic_primitives


def _segmented_rule_can_extend(
    left: PdfPreviewVisualPrimitive,
    right: PdfPreviewVisualPrimitive,
    orientation: str,
) -> bool:
    if left.page_number != right.page_number:
        return False
    if _primitive_rule_color(left) != _primitive_rule_color(right):
        return False
    if orientation == "horizontal":
        left_axis = (left.bounding_box.top_pt + left.bounding_box.bottom_pt) / 2.0
        right_axis = (right.bounding_box.top_pt + right.bounding_box.bottom_pt) / 2.0
    else:
        left_axis = (left.bounding_box.left_pt + left.bounding_box.right_pt) / 2.0
        right_axis = (right.bounding_box.left_pt + right.bounding_box.right_pt) / 2.0
    if abs(left_axis - right_axis) > _VISUAL_SEGMENTED_AXIS_TOLERANCE_PT:
        return False

    _, left_end = _primitive_line_span_range(left, orientation)
    right_start, _ = _primitive_line_span_range(right, orientation)
    gap = right_start - left_end
    return gap <= _VISUAL_SEGMENTED_GAP_TOLERANCE_PT


def _build_segmented_rule_primitive(
    run: list[PdfPreviewVisualPrimitive],
    *,
    page_number: int,
    orientation: str,
    stroke_color: str,
    draw_order: int,
) -> PdfPreviewVisualPrimitive | None:
    if len(run) < _VISUAL_SEGMENTED_MIN_PARTS:
        return None

    starts_ends = [_primitive_line_span_range(item, orientation) for item in run]
    start = min(item[0] for item in starts_ends)
    end = max(item[1] for item in starts_ends)
    span = end - start
    if span < _VISUAL_SEGMENTED_MIN_SPAN_PT:
        return None

    gaps = [
        max(current_start - previous_end, 0.0)
        for (_, previous_end), (current_start, _) in zip(starts_ends, starts_ends[1:])
    ]
    has_visible_gap = any(gap >= _VISUAL_TOUCH_TOLERANCE_PT for gap in gaps)
    if not has_visible_gap and len(run) < 5:
        return None

    bbox = _union_visual_primitive_bboxes(run)
    if bbox is None:
        return None

    return PdfPreviewVisualPrimitive(
        page_number=page_number,
        draw_order=draw_order,
        object_type=f"segmented_{orientation}_rule",
        bounding_box=bbox,
        fill_color=None,
        stroke_color=stroke_color,
        stroke_width_pt=max((item.stroke_width_pt or 0.0) for item in run) or None,
        has_fill=False,
        has_stroke=True,
        is_axis_aligned_box=False,
    )


def _pdfium_object_type_name(raw, obj_raw) -> str:  # noqa: ANN001
    object_type = raw.FPDFPageObj_GetType(obj_raw)
    if object_type == raw.FPDF_PAGEOBJ_PATH:
        return "path"
    if object_type == raw.FPDF_PAGEOBJ_SHADING:
        return "shading"
    if object_type == raw.FPDF_PAGEOBJ_IMAGE:
        return "image"
    if object_type == raw.FPDF_PAGEOBJ_TEXT:
        return "text"
    return "unknown"


def _pdfium_color(raw, obj_raw, *, getter) -> str | None:  # noqa: ANN001
    from ctypes import c_uint

    red = c_uint()
    green = c_uint()
    blue = c_uint()
    alpha = c_uint()
    if not getter(obj_raw, red, green, blue, alpha):
        return None
    return f"#{red.value:02x}{green.value:02x}{blue.value:02x}{alpha.value:02x}"


def _pdfium_stroke_width(raw, obj_raw) -> float | None:  # noqa: ANN001
    from ctypes import c_float

    width = c_float()
    if not raw.FPDFPageObj_GetStrokeWidth(obj_raw, width):
        return None
    return float(width.value)


def _pdfium_has_fill(raw, obj_raw) -> bool:  # noqa: ANN001
    from ctypes import c_int

    if not hasattr(raw, "FPDFPath_GetDrawMode"):
        return False
    fill_mode = c_int()
    stroke = c_int()
    if not raw.FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke):
        return False
    return fill_mode.value != getattr(raw, "FPDF_FILLMODE_NONE", 0)


def _pdfium_has_stroke(raw, obj_raw) -> bool:  # noqa: ANN001
    from ctypes import c_int

    if not hasattr(raw, "FPDFPath_GetDrawMode"):
        return False
    fill_mode = c_int()
    stroke = c_int()
    if not raw.FPDFPath_GetDrawMode(obj_raw, fill_mode, stroke):
        return False
    return bool(stroke.value)


def _pdfium_is_axis_aligned_box(raw, obj_raw) -> bool:  # noqa: ANN001
    points = _pdfium_path_points(raw, obj_raw)
    if len(points) < 4:
        return False

    unique_points = {(round(x, 3), round(y, 3)) for x, y in points}
    if len(unique_points) != 4:
        return False

    xs = {point[0] for point in unique_points}
    ys = {point[1] for point in unique_points}
    return len(xs) == 2 and len(ys) == 2


def _pdfium_path_points(raw, obj_raw) -> list[tuple[float, float]]:  # noqa: ANN001
    from ctypes import c_float

    segment_count = raw.FPDFPath_CountSegments(obj_raw)
    if segment_count <= 0:
        return []

    points: list[tuple[float, float]] = []
    for segment_index in range(segment_count):
        segment = raw.FPDFPath_GetPathSegment(obj_raw, segment_index)
        if not segment:
            continue
        segment_type = raw.FPDFPathSegment_GetType(segment)
        if segment_type not in (raw.FPDF_SEGMENT_MOVETO, raw.FPDF_SEGMENT_LINETO):
            return []
        x = c_float()
        y = c_float()
        if not raw.FPDFPathSegment_GetPoint(segment, x, y):
            continue
        points.append((float(x.value), float(y.value)))
    return points


def _candidate_roles_for_visual_primitive(
    primitive: PdfPreviewVisualPrimitive,
    *,
    page_width: float,
    page_height: float,
    include_fill_only_rules: bool = False,
) -> list[str]:
    roles: list[str] = []
    width, height = _primitive_size(primitive)
    if width <= 0.0 or height <= 0.0:
        return roles
    has_visible_stroke = _has_visible_stroke(primitive)
    filled_rect_orientation = (
        _filled_thin_rect_line_orientation(primitive)
        if include_fill_only_rules
        else None
    )
    is_segmented_horizontal = primitive.object_type == "segmented_horizontal_rule"
    is_segmented_vertical = primitive.object_type == "segmented_vertical_rule"

    narrow_width = max(page_width * 0.03, 10.0)
    narrow_height = max(page_height * 0.03, 10.0)

    is_vertical_segment = is_segmented_vertical or (
        (has_visible_stroke or filled_rect_orientation == "vertical")
        and width <= narrow_width
        and height > width
        and height > _VISUAL_MIN_LINE_SEGMENT_PT
    )
    is_horizontal_segment = is_segmented_horizontal or (
        (has_visible_stroke or filled_rect_orientation == "horizontal")
        and height <= narrow_height
        and width > height
        and width > _VISUAL_MIN_LINE_SEGMENT_PT
    )
    if is_vertical_segment:
        roles.append("vertical_line_segment")
    if is_horizontal_segment:
        roles.append("horizontal_line_segment")
    if is_segmented_vertical:
        roles.append("segmented_vertical_rule")
    if is_segmented_horizontal:
        roles.append("segmented_horizontal_rule")

    is_long_vertical_rule = (
        not is_segmented_vertical
        and (has_visible_stroke or filled_rect_orientation == "vertical")
        and height >= page_height * 0.70
        and width <= narrow_width
    )
    is_long_horizontal_rule = (
        not is_segmented_horizontal
        and (has_visible_stroke or filled_rect_orientation == "horizontal")
        and width >= page_width * 0.70
        and height <= narrow_height
    )
    if is_long_vertical_rule:
        roles.append("long_vertical_rule")
    if is_long_horizontal_rule:
        roles.append("long_horizontal_rule")

    return roles


def _build_visual_block_candidates(
    primitives: list[PdfPreviewVisualPrimitive],
) -> list[PdfPreviewVisualBlockCandidate]:
    """선/박스 primitive들을 묶어 normalize 단계에서 TableIR로 승격할 후보를 만든다."""
    if not primitives:
        return []

    candidates: list[PdfPreviewVisualBlockCandidate] = []

    line_primitives = [
        primitive
        for primitive in primitives
        if {"horizontal_line_segment", "vertical_line_segment"} & set(primitive.candidate_roles)
    ]
    line_primitives = _dedupe_line_primitives_for_graph(line_primitives)
    if not line_primitives:
        return _dedupe_visual_block_candidates(candidates)

    # Long rules should participate in the same line graph as ordinary line
    # segments. Only when the line set itself is too large do we fall back to
    # a cheap pass, and even then we still let long rules form frames/boxes
    # among themselves before falling back to ordinary line candidates.
    if len(line_primitives) > _VISUAL_OPEN_FRAME_PRIMITIVE_LIMIT:
        long_line_primitives = [
            primitive for primitive in line_primitives if _primitive_is_long_rule(primitive)
        ]
        return _dedupe_visual_block_candidates(_build_open_frame_candidates(long_line_primitives))

    components = _connected_line_components(line_primitives)
    axis_box_candidates: list[PdfPreviewVisualBlockCandidate] = []
    for component in components:
        if not _is_open_frame_component(component):
            continue
        axis_box_candidates.extend(_build_axis_box_candidates_from_component(component))
    candidates.extend(axis_box_candidates)

    assigned_draw_orders = {
        draw_order
        for candidate in axis_box_candidates
        for draw_order in candidate.primitive_draw_orders
    }
    leftover_lines = [
        primitive
        for primitive in line_primitives
        if primitive.draw_order not in assigned_draw_orders
    ]
    candidates.extend(_build_open_frame_candidates(leftover_lines))

    return _dedupe_visual_block_candidates(candidates)


def _connected_line_components(
    line_primitives: list[PdfPreviewVisualPrimitive],
) -> list[list[PdfPreviewVisualPrimitive]]:
    if not line_primitives:
        return []

    adjacency: dict[int, set[int]] = {index: set() for index in range(len(line_primitives))}
    endpoint_buckets: dict[tuple[int, int], set[int]] = {}
    for index, primitive in enumerate(line_primitives):
        endpoints = _primitive_line_endpoints(primitive)
        if endpoints is None:
            continue
        for endpoint in endpoints:
            for bucket_key in _point_bucket_keys(endpoint, tolerance_pt=_VISUAL_LINE_JOIN_TOLERANCE_PT):
                endpoint_buckets.setdefault(bucket_key, set()).add(index)

    for left_index, left in enumerate(line_primitives):
        endpoints = _primitive_line_endpoints(left)
        if endpoints is None:
            continue
        candidate_indices: set[int] = set()
        for endpoint in endpoints:
            for bucket_key in _point_bucket_keys(endpoint, tolerance_pt=_VISUAL_LINE_JOIN_TOLERANCE_PT):
                candidate_indices.update(endpoint_buckets.get(bucket_key, set()))
        for right_index in candidate_indices:
            if right_index <= left_index:
                continue
            right = line_primitives[right_index]
            if _line_primitives_belong_to_same_frame(left, right):
                adjacency[left_index].add(right_index)
                adjacency[right_index].add(left_index)

    components: list[list[PdfPreviewVisualPrimitive]] = []
    visited: set[int] = set()
    for start_index in range(len(line_primitives)):
        if start_index in visited:
            continue
        stack = [start_index]
        component_indices: list[int] = []
        while stack:
            current_index = stack.pop()
            if current_index in visited:
                continue
            visited.add(current_index)
            component_indices.append(current_index)
            stack.extend(neighbor for neighbor in adjacency[current_index] if neighbor not in visited)
        components.append([line_primitives[index] for index in component_indices])
    return components


def _dedupe_line_primitives_for_graph(
    line_primitives: list[PdfPreviewVisualPrimitive],
) -> list[PdfPreviewVisualPrimitive]:
    if not line_primitives:
        return []

    kept: list[PdfPreviewVisualPrimitive] = []
    for primitive in sorted(
        line_primitives,
        key=lambda item: (
            item.page_number,
            item.draw_order,
            item.bounding_box.left_pt,
            item.bounding_box.bottom_pt,
            item.bounding_box.right_pt,
            item.bounding_box.top_pt,
        ),
    ):
        duplicate_index: int | None = None
        for index, existing in enumerate(kept):
            if _line_primitives_are_graph_duplicates(existing, primitive):
                duplicate_index = index
                break
        if duplicate_index is None:
            kept.append(primitive)
            continue

        merged_roles = sorted(set(kept[duplicate_index].candidate_roles) | set(primitive.candidate_roles))
        kept[duplicate_index] = kept[duplicate_index].model_copy(update={"candidate_roles": merged_roles})
    return kept


def _line_primitives_are_graph_duplicates(
    left: PdfPreviewVisualPrimitive,
    right: PdfPreviewVisualPrimitive,
) -> bool:
    if left.page_number != right.page_number:
        return False
    left_orientation = _primitive_line_orientation(left)
    right_orientation = _primitive_line_orientation(right)
    if left_orientation is None or left_orientation != right_orientation:
        return False
    if _bbox_overlap_ratio(left.bounding_box, right.bounding_box) >= 0.98:
        return True

    if left_orientation == "horizontal":
        left_axis = (left.bounding_box.top_pt + left.bounding_box.bottom_pt) / 2.0
        right_axis = (right.bounding_box.top_pt + right.bounding_box.bottom_pt) / 2.0
    else:
        left_axis = (left.bounding_box.left_pt + left.bounding_box.right_pt) / 2.0
        right_axis = (right.bounding_box.left_pt + right.bounding_box.right_pt) / 2.0
    if abs(left_axis - right_axis) > _VISUAL_TOUCH_TOLERANCE_PT:
        return False

    left_start, left_end = _primitive_line_span_range(left, left_orientation)
    right_start, right_end = _primitive_line_span_range(right, right_orientation)
    left_span = max(left_end - left_start, 0.0)
    right_span = max(right_end - right_start, 0.0)
    overlap = min(left_end, right_end) - max(left_start, right_start)
    if left_span <= 0.0 or right_span <= 0.0 or overlap <= 0.0:
        return False
    span_ratio = min(left_span, right_span) / max(left_span, right_span)
    if span_ratio < 0.98:
        return False
    overlap_ratio = overlap / min(left_span, right_span)
    return overlap_ratio >= 0.98


def _build_axis_box_candidates_from_component(
    component: list[PdfPreviewVisualPrimitive],
) -> list[PdfPreviewVisualBlockCandidate]:
    candidates: list[PdfPreviewVisualBlockCandidate] = []
    for seed_bbox in _find_axis_box_seed_bboxes_from_component(component):
        local_members = [
            member
            for member in component
            if _primitive_belongs_to_axis_box(member, seed_bbox)
        ]
        if not local_members:
            continue
        draw_orders = sorted({member.draw_order for member in local_members})
        source_roles = sorted({role for member in local_members for role in member.candidate_roles})
        candidates.append(
            PdfPreviewVisualBlockCandidate(
                page_number=component[0].page_number,
                candidate_type="axis_box",
                bounding_box=seed_bbox,
                primitive_draw_orders=draw_orders,
                source_roles=source_roles,
                child_cells=[],
            )
        )
    return _dedupe_visual_block_candidates(candidates)


def _find_axis_box_seed_bboxes_from_component(
    component: list[PdfPreviewVisualPrimitive],
) -> list[PdfBoundingBox]:
    vertical_lines = sorted(
        (
            primitive
            for primitive in component
            if _primitive_line_orientation(primitive) == "vertical"
        ),
        key=lambda item: (
            _primitive_line_axis_center(item, "vertical"),
            item.bounding_box.bottom_pt,
            item.bounding_box.top_pt,
            item.draw_order,
        ),
    )
    horizontal_lines = sorted(
        (
            primitive
            for primitive in component
            if _primitive_line_orientation(primitive) == "horizontal"
        ),
        key=lambda item: (
            _primitive_line_axis_center(item, "horizontal"),
            item.bounding_box.left_pt,
            item.bounding_box.right_pt,
            item.draw_order,
        ),
    )
    if len(vertical_lines) < 2 or len(horizontal_lines) < 2:
        return []

    seed_bboxes: list[PdfBoundingBox] = []

    for left_index, left in enumerate(vertical_lines):
        left_x = _primitive_line_axis_center(left, "vertical")
        for right in vertical_lines[left_index + 1 :]:
            right_x = _primitive_line_axis_center(right, "vertical")
            if right_x - left_x < _VISUAL_BOX_SEED_MIN_SIZE_PT:
                continue

            supporting_horizontals = [
                horizontal
                for horizontal in horizontal_lines
                if _horizontal_line_matches_box_boundary(horizontal, left_x=left_x, right_x=right_x)
            ]
            if len(supporting_horizontals) < 2:
                continue

            for bottom_index, bottom in enumerate(supporting_horizontals):
                bottom_y = _primitive_line_axis_center(bottom, "horizontal")
                for top in supporting_horizontals[bottom_index + 1 :]:
                    top_y = _primitive_line_axis_center(top, "horizontal")
                    if top_y - bottom_y < _VISUAL_BOX_SEED_MIN_SIZE_PT:
                        continue
                    if not _vertical_line_matches_box_boundary(left, x=left_x, bottom_y=bottom_y, top_y=top_y):
                        continue
                    if not _vertical_line_matches_box_boundary(right, x=right_x, bottom_y=bottom_y, top_y=top_y):
                        continue
                    seed_bboxes.append(
                        PdfBoundingBox(
                            left_pt=left_x,
                            bottom_pt=bottom_y,
                            right_pt=right_x,
                            top_pt=top_y,
                        )
                    )

    return _dedupe_seed_bboxes(seed_bboxes)


def _build_open_frame_candidates(
    line_primitives: list[PdfPreviewVisualPrimitive],
) -> list[PdfPreviewVisualBlockCandidate]:
    candidates: list[PdfPreviewVisualBlockCandidate] = []
    for component in _connected_line_components(line_primitives):
        candidate_bbox = _union_visual_primitive_bboxes(component)
        if candidate_bbox is None:
            continue
        if not _is_open_frame_component(component):
            continue

        candidates.append(
            PdfPreviewVisualBlockCandidate(
                page_number=component[0].page_number,
                candidate_type="open_frame",
                bounding_box=candidate_bbox,
                primitive_draw_orders=sorted({primitive.draw_order for primitive in component}),
                source_roles=sorted({role for primitive in component for role in primitive.candidate_roles}),
                child_cells=[],
            )
        )
    return _dedupe_visual_block_candidates(candidates)


def _dedupe_visual_block_candidates(
    candidates: list[PdfPreviewVisualBlockCandidate],
) -> list[PdfPreviewVisualBlockCandidate]:
    if not candidates:
        return []

    kept: list[PdfPreviewVisualBlockCandidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            item.page_number,
            -((item.bounding_box.right_pt - item.bounding_box.left_pt) * (item.bounding_box.top_pt - item.bounding_box.bottom_pt)),
            item.candidate_type,
        ),
    ):
        duplicate = False
        for existing in kept:
            if existing.page_number != candidate.page_number:
                continue
            if _bbox_overlap_ratio(existing.bounding_box, candidate.bounding_box) >= 0.95:
                duplicate = True
                break
            if _bbox_contains(
                existing.bounding_box,
                candidate.bounding_box,
                tolerance_pt=_VISUAL_TOUCH_TOLERANCE_PT,
            ) and existing.candidate_type == candidate.candidate_type:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)

    kept.sort(key=lambda item: (item.page_number, item.bounding_box.top_pt, item.bounding_box.left_pt))
    return kept
