"""PDF preview용 DocIR 보정 단계.

공통 HTML 렌더러는 PDF 세부 사정을 몰라야 하므로, PDF에서만 얻는 힌트는
여기서 DocIR에 반영한다. 현재 담당하는 일은 다음과 같다.

- ODL/pdfium 시각 박스를 도식 보존용 layout `TableIR`로 승격
- ODL `left-page/right-page` 모아찍기를 synthetic page로 분리
- ODL `left-column/right-column`을 `ColumnLayoutInfo.column_index`로 반영
- 같은 줄에 놓인 image/table block을 1행 N열 `TableIR`로 묶기
- ODL table grid boundary를 기존 `TableIR` geometry에 보강
"""

from __future__ import annotations

from typing import Any

from ...models import DocIR, ImageIR, PageInfo, ParagraphIR, RunIR, TableCellIR, TableIR
from ...style_types import CellStyleInfo, ColumnLayoutInfo, ParaStyleInfo, TableStyleInfo
from ..enhancement import enrich_pdf_table_backgrounds
from ..meta import PdfBoundingBox
from ..odl.adapter import _pdf_node_kwargs
from .models import (
    PdfLayoutRegion,
    PdfPreviewContext,
    PdfPreviewTableContext,
    PdfPreviewVisualBlockCandidate,
    _AssignedCandidate,
    _AssignedCandidateGroup,
    _CANDIDATE_ASSIGN_TOLERANCE_PT,
    _LAYOUT_TABLE_ALIGNMENT_OVERLAP_RATIO,
    _LAYOUT_TABLE_BOUNDARY_TOLERANCE_PT,
    _LAYOUT_TABLE_GROUP_GAP_TOLERANCE_PT,
    _PreviewRenderNode,
)
from .shared import (
    _bbox_area,
    _bbox_center,
    _bbox_contains,
    _bbox_intersection,
    _bbox_touches_or_near,
    _shared_bbox_distance,
)


def _paragraph_column_layout(paragraph: ParagraphIR) -> ColumnLayoutInfo | None:
    return paragraph.para_style.column_layout if paragraph.para_style is not None else None


def _set_paragraph_column_layout(paragraph: ParagraphIR, layout: ColumnLayoutInfo | None) -> None:
    if layout is None:
        if paragraph.para_style is not None:
            paragraph.para_style.column_layout = None
        return
    if paragraph.para_style is None:
        paragraph.para_style = ParaStyleInfo()
    paragraph.para_style.column_layout = layout


# ---------- bbox / region helpers ----------
# ODL region과 DocIR bbox를 매칭하기 위한 작은 좌표 유틸들이다.
# `left-page/right-page`와 `left-column/right-column`은 여기서는 둘 다
# left/right role로 정규화하고, 실제 의미 차이는 아래 normalize 단계에서 나눈다.

def _paragraph_bbox(paragraph: ParagraphIR) -> PdfBoundingBox | None:
    return getattr(paragraph, "bbox", None)


def _region_role(region_type: str | None) -> str | None:
    if region_type in {"left-page", "left-column"}:
        return "left"
    if region_type in {"right-page", "right-column"}:
        return "right"
    if region_type == "main":
        return "main"
    return None


def _bbox_region_type(
    bbox: PdfBoundingBox | None,
    *,
    page: PageInfo,
    page_regions: list[PdfLayoutRegion],
    explicit_region_type: str | None = None,
) -> str:
    """bbox가 page region 중 main/left/right 어디에 가장 잘 들어맞는지 고른다."""
    normalized_explicit = _region_role(explicit_region_type)
    if normalized_explicit is not None:
        return normalized_explicit
    if bbox is None or not page_regions:
        return "main"

    best_region_type = "main"
    best_score: tuple[int, float, float] | None = None
    for region in page_regions:
        region_bbox = region.bounding_box
        if region_bbox is None:
            continue
        overlap = _bbox_intersection(region_bbox, bbox)
        overlap_area = _bbox_area(overlap) if overlap is not None else 0.0
        contains_center = 0
        center_x, center_y = _bbox_center(bbox)
        if (
            region_bbox.left_pt <= center_x <= region_bbox.right_pt
            and region_bbox.bottom_pt <= center_y <= region_bbox.top_pt
        ):
            contains_center = 1
        if not contains_center and overlap_area <= 0.0:
            continue
        score = (
            contains_center,
            overlap_area,
            -_shared_bbox_distance(region_bbox, bbox),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_region_type = _region_role(region.region_type) or "main"
    return best_region_type


def _bbox_order_key(
    bbox: PdfBoundingBox | None,
    *,
    fallback_index: int,
    subindex: int = 0,
) -> tuple[float, float, int, int]:
    if bbox is None:
        return (1_000_000.0, 1_000_000.0, fallback_index, subindex)
    return (-bbox.top_pt, bbox.left_pt, fallback_index, subindex)


def _bbox_offsets_from_page(
    bbox: PdfBoundingBox | None,
    *,
    page: PageInfo,
) -> tuple[float | None, float | None]:
    if bbox is None or page.height_pt is None:
        return None, None
    top_offset = max(page.height_pt - bbox.top_pt, 0.0)
    bottom_offset = max(page.height_pt - bbox.bottom_pt, top_offset)
    return top_offset, bottom_offset


# ---------- candidate scoring ----------
# pdfium에서 찾은 visual block 후보에 실제 DocIR node를 배정하기 위한 점수 계산.

def _bbox_assignment_score(candidate_bbox: PdfBoundingBox, node_bbox: PdfBoundingBox) -> tuple[int, float, float]:
    """candidate bbox가 node bbox를 얼마나 잘 설명하는지 점수화한다."""
    if _bbox_contains(
        candidate_bbox,
        node_bbox,
        tolerance_pt=_CANDIDATE_ASSIGN_TOLERANCE_PT,
    ):
        candidate_area = _bbox_area(candidate_bbox)
        node_area = _bbox_area(node_bbox)
        area_ratio = 1.0 if candidate_area <= 0.0 else min(node_area / candidate_area, 1.0)
        return (3, area_ratio, -candidate_area)

    center_x, center_y = _bbox_center(node_bbox)
    if (
        candidate_bbox.left_pt - _CANDIDATE_ASSIGN_TOLERANCE_PT <= center_x <= candidate_bbox.right_pt + _CANDIDATE_ASSIGN_TOLERANCE_PT
        and candidate_bbox.bottom_pt - _CANDIDATE_ASSIGN_TOLERANCE_PT <= center_y <= candidate_bbox.top_pt + _CANDIDATE_ASSIGN_TOLERANCE_PT
    ):
        overlap = _bbox_intersection(candidate_bbox, node_bbox)
        overlap_area = _bbox_area(overlap) if overlap is not None else 0.0
        node_area = _bbox_area(node_bbox)
        overlap_ratio = 0.0 if node_area <= 0.0 else overlap_area / node_area
        return (2, overlap_ratio, -_bbox_area(candidate_bbox))

    overlap = _bbox_intersection(candidate_bbox, node_bbox)
    if overlap is None:
        return (0, 0.0, 0.0)
    node_area = _bbox_area(node_bbox)
    overlap_ratio = 0.0 if node_area <= 0.0 else _bbox_area(overlap) / node_area
    return (1, overlap_ratio, -_bbox_area(candidate_bbox))


def _best_candidate_for_node(
    node_bbox: PdfBoundingBox,
    candidates: list[PdfPreviewVisualBlockCandidate],
) -> PdfPreviewVisualBlockCandidate | None:
    best_candidate: PdfPreviewVisualBlockCandidate | None = None
    best_score: tuple[int, float, float] | None = None
    for candidate in candidates:
        score = _bbox_assignment_score(candidate.bounding_box, node_bbox)
        if score[0] <= 0:
            continue
        if best_score is None or score > best_score:
            best_candidate = candidate
            best_score = score
    return best_candidate


# ---------- candidate grouping ----------
# 가까운 visual block 후보 여러 개를 하나의 layout table로 묶기 위한 로직.

def _span_overlap_ratio(
    left_start: float,
    left_end: float,
    right_start: float,
    right_end: float,
) -> float:
    overlap = max(min(left_end, right_end) - max(left_start, right_start), 0.0)
    shorter_span = min(max(left_end - left_start, 0.0), max(right_end - right_start, 0.0))
    if overlap <= 0.0 or shorter_span <= 0.0:
        return 0.0
    return overlap / shorter_span


def _candidate_boxes_belong_to_same_group(
    left: _AssignedCandidate,
    right: _AssignedCandidate,
) -> bool:
    if left.region_type != right.region_type:
        return False

    left_bbox = left.candidate.bounding_box
    right_bbox = right.candidate.bounding_box
    if _bbox_intersection(left_bbox, right_bbox) is not None:
        return True
    if _bbox_contains(left_bbox, right_bbox, tolerance_pt=_CANDIDATE_ASSIGN_TOLERANCE_PT):
        return True
    if _bbox_contains(right_bbox, left_bbox, tolerance_pt=_CANDIDATE_ASSIGN_TOLERANCE_PT):
        return True
    if _bbox_touches_or_near(left_bbox, right_bbox, tolerance_pt=_CANDIDATE_ASSIGN_TOLERANCE_PT):
        return True

    horizontal_alignment = _span_overlap_ratio(
        left_bbox.left_pt,
        left_bbox.right_pt,
        right_bbox.left_pt,
        right_bbox.right_pt,
    )
    vertical_alignment = _span_overlap_ratio(
        left_bbox.bottom_pt,
        left_bbox.top_pt,
        right_bbox.bottom_pt,
        right_bbox.top_pt,
    )
    horizontal_gap = max(left_bbox.left_pt - right_bbox.right_pt, right_bbox.left_pt - left_bbox.right_pt, 0.0)
    vertical_gap = max(left_bbox.bottom_pt - right_bbox.top_pt, right_bbox.bottom_pt - left_bbox.top_pt, 0.0)
    return (
        horizontal_alignment >= _LAYOUT_TABLE_ALIGNMENT_OVERLAP_RATIO
        and vertical_gap <= _LAYOUT_TABLE_GROUP_GAP_TOLERANCE_PT
    ) or (
        vertical_alignment >= _LAYOUT_TABLE_ALIGNMENT_OVERLAP_RATIO
        and horizontal_gap <= _LAYOUT_TABLE_GROUP_GAP_TOLERANCE_PT
    )


def _build_candidate_groups(
    assigned_candidates: list[_AssignedCandidate],
    *,
    page: PageInfo,
) -> list[_AssignedCandidateGroup]:
    if not assigned_candidates:
        return []

    ordered = sorted(assigned_candidates, key=lambda item: item.order_key)
    index_by_id = {id(candidate): index for index, candidate in enumerate(ordered)}
    groups: list[_AssignedCandidateGroup] = []
    seen: set[int] = set()

    for root in ordered:
        root_id = id(root)
        if root_id in seen:
            continue
        stack = [root]
        members: list[_AssignedCandidate] = []
        while stack:
            current = stack.pop()
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)
            members.append(current)
            for other in ordered:
                other_id = id(other)
                if other_id in seen:
                    continue
                if _candidate_boxes_belong_to_same_group(current, other):
                    stack.append(other)

        member_bboxes = [member.candidate.bounding_box for member in members]
        group_bbox = PdfBoundingBox(
            left_pt=min(bbox.left_pt for bbox in member_bboxes),
            bottom_pt=min(bbox.bottom_pt for bbox in member_bboxes),
            right_pt=max(bbox.right_pt for bbox in member_bboxes),
            top_pt=max(bbox.top_pt for bbox in member_bboxes),
        )
        top_offset, bottom_offset = _bbox_offsets_from_page(group_bbox, page=page)
        groups.append(
            _AssignedCandidateGroup(
                candidates=sorted(
                    members,
                    key=lambda item: _bbox_order_key(
                        item.candidate.bounding_box,
                        fallback_index=index_by_id[id(item)],
                    ),
                ),
                region_type=members[0].region_type,
                top_offset_pt=top_offset,
                bottom_offset_pt=bottom_offset,
                order_key=_bbox_order_key(group_bbox, fallback_index=len(groups)),
                bounding_box=group_bbox,
            )
        )

    groups.sort(key=lambda group: group.order_key)
    return groups


# ---------- node collection ----------
# page 안의 paragraph/table/image/run을 bbox가 있는 임시 노드로 펼친다.

def _content_node_bbox(node: Any) -> PdfBoundingBox | None:
    return getattr(node, "bbox", None)


def _collect_page_render_nodes(
    page_paragraphs: list[ParagraphIR],
) -> tuple[list[_PreviewRenderNode], list[_PreviewRenderNode], list[_PreviewRenderNode], list[_PreviewRenderNode]]:
    """한 페이지의 DocIR 요소들을 visual block 후보에 배정할 수 있는 단위로 수집한다."""
    paragraph_nodes: list[_PreviewRenderNode] = []
    table_nodes: list[_PreviewRenderNode] = []
    image_nodes: list[_PreviewRenderNode] = []
    run_nodes: list[_PreviewRenderNode] = []

    for fallback_index, paragraph in enumerate(page_paragraphs):
        paragraph_bbox = _paragraph_bbox(paragraph)
        paragraph_key = _bbox_order_key(paragraph_bbox, fallback_index=fallback_index)
        if paragraph_bbox is not None:
            paragraph_nodes.append(
                _PreviewRenderNode(
                    kind="paragraph",
                    node_id=paragraph.node_id,
                    bbox=paragraph_bbox,
                    order_key=paragraph_key,
                    parent_paragraph_id=paragraph.node_id,
                    parent_para_style=paragraph.para_style,
                    paragraph=paragraph,
                )
            )

        for content_index, node in enumerate(paragraph.content, start=1):
            node_bbox = _content_node_bbox(node)
            if node_bbox is None:
                continue
            node_key = _bbox_order_key(node_bbox, fallback_index=fallback_index, subindex=content_index)
            common_kwargs = {
                "node_id": getattr(node, "node_id", None) or f"{paragraph.node_id}.c{content_index}",
                "bbox": node_bbox,
                "order_key": node_key,
                "parent_paragraph_id": paragraph.node_id,
                "parent_para_style": paragraph.para_style,
            }
            if isinstance(node, TableIR):
                table_nodes.append(_PreviewRenderNode(kind="table", table=node, **common_kwargs))
            elif isinstance(node, ImageIR):
                image_nodes.append(_PreviewRenderNode(kind="image", image=node, **common_kwargs))
            elif isinstance(node, RunIR):
                run_nodes.append(_PreviewRenderNode(kind="run", run=node, **common_kwargs))

    return paragraph_nodes, table_nodes, image_nodes, run_nodes


def _page_box_candidates(
    preview_context: PdfPreviewContext,
    *,
    page_number: int,
) -> list[PdfPreviewVisualBlockCandidate]:
    return [
        candidate
        for candidate in preview_context.visual_block_candidates
        if candidate.page_number == page_number and candidate.candidate_type in {"axis_box", "open_frame"}
    ]


def _candidate_conflicts_with_table_bbox(
    candidate_bbox: PdfBoundingBox,
    table_bbox: PdfBoundingBox,
) -> bool:
    """실제 TableIR와 겹치는 장식 후보를 layout-table 승격에서 제외한다."""
    intersection = _bbox_intersection(candidate_bbox, table_bbox)
    if intersection is None:
        return _shared_bbox_distance(candidate_bbox, table_bbox) <= _CANDIDATE_ASSIGN_TOLERANCE_PT

    candidate_area = _bbox_area(candidate_bbox)
    table_area = _bbox_area(table_bbox)
    intersection_area = _bbox_area(intersection)
    if candidate_area <= 0.0 or table_area <= 0.0 or intersection_area <= 0.0:
        return False

    candidate_overlap = intersection_area / candidate_area
    table_overlap = intersection_area / table_area
    close_full_match = (
        candidate_overlap >= 0.82
        and table_overlap >= 0.82
        and _shared_bbox_distance(candidate_bbox, table_bbox) <= 12.0
    )
    return (
        close_full_match
        or candidate_overlap >= 0.20
        or table_overlap >= 0.20
        or _bbox_contains(table_bbox, candidate_bbox, tolerance_pt=_CANDIDATE_ASSIGN_TOLERANCE_PT)
        or _bbox_contains(candidate_bbox, table_bbox, tolerance_pt=_CANDIDATE_ASSIGN_TOLERANCE_PT)
    )


def _assign_page_nodes_to_candidates(
    page: PageInfo,
    page_paragraphs: list[ParagraphIR],
    *,
    page_regions: list[PdfLayoutRegion],
    preview_context: PdfPreviewContext,
) -> tuple[list[_AssignedCandidate], set[str], set[str]]:
    """visual block 후보에 page node들을 배정하고, flow에서 제거할 id도 함께 반환한다."""
    paragraph_nodes, table_nodes, image_nodes, run_nodes = _collect_page_render_nodes(page_paragraphs)
    table_bboxes = [table_node.bbox for table_node in table_nodes]
    candidates = [
        candidate
        for candidate in _page_box_candidates(preview_context, page_number=page.page_number)
        if not any(_candidate_conflicts_with_table_bbox(candidate.bounding_box, table_bbox) for table_bbox in table_bboxes)
    ]
    if not candidates:
        return [], set(), set()

    assigned_candidates: list[_AssignedCandidate] = []
    for candidate_index, candidate in enumerate(
        sorted(candidates, key=lambda item: (item.bounding_box.top_pt, item.bounding_box.left_pt))
    ):
        bbox = candidate.bounding_box
        top_offset, bottom_offset = _bbox_offsets_from_page(bbox, page=page)
        assigned_candidates.append(
            _AssignedCandidate(
                candidate=candidate,
                region_type=_bbox_region_type(
                    bbox,
                    page=page,
                    page_regions=page_regions,
                    explicit_region_type=None,
                ),
                top_offset_pt=top_offset,
                bottom_offset_pt=bottom_offset,
                order_key=_bbox_order_key(bbox, fallback_index=candidate_index),
                paragraph_nodes=[],
                table_nodes=[],
                image_nodes=[],
                run_nodes=[],
            )
        )

    candidate_lookup = {id(candidate.candidate): candidate for candidate in assigned_candidates}
    assigned_paragraph_ids: set[str] = set()
    assigned_child_ids: set[str] = set()

    for node in sorted(paragraph_nodes, key=lambda item: item.order_key):
        candidate = _best_candidate_for_node(node.bbox, candidates)
        if candidate is None or node.paragraph is None:
            continue
        assigned = candidate_lookup[id(candidate)]
        assigned.paragraph_nodes.append(node)
        assigned.order_key = min(assigned.order_key, node.order_key)
        assigned_paragraph_ids.add(node.node_id)

    for nodes, target_attr in (
        (table_nodes, "table_nodes"),
        (image_nodes, "image_nodes"),
        (run_nodes, "run_nodes"),
    ):
        for node in sorted(nodes, key=lambda item: item.order_key):
            if node.parent_paragraph_id in assigned_paragraph_ids:
                continue
            candidate = _best_candidate_for_node(node.bbox, candidates)
            if candidate is None:
                continue
            assigned = candidate_lookup[id(candidate)]
            getattr(assigned, target_attr).append(node)
            assigned.order_key = min(assigned.order_key, node.order_key)
            assigned_child_ids.add(node.node_id)

    assigned_candidates = [
        assigned_candidate
        for assigned_candidate in assigned_candidates
        if _assigned_candidate_has_content(assigned_candidate)
    ]
    return assigned_candidates, assigned_paragraph_ids, assigned_child_ids


def _assigned_candidate_has_content(assigned_candidate: _AssignedCandidate) -> bool:
    return bool(
        assigned_candidate.paragraph_nodes
        or assigned_candidate.table_nodes
        or assigned_candidate.image_nodes
        or assigned_candidate.run_nodes
    )


def _filter_page_flow_paragraphs(
    page_paragraphs: list[ParagraphIR],
    *,
    assigned_paragraph_ids: set[str],
    assigned_child_ids: set[str],
) -> list[ParagraphIR]:
    filtered: list[ParagraphIR] = []
    for paragraph in page_paragraphs:
        if paragraph.node_id in assigned_paragraph_ids:
            continue
        if not assigned_child_ids:
            filtered.append(paragraph)
            continue
        remaining_content = [
            node
            for node in paragraph.content
            if getattr(node, "node_id", "") not in assigned_child_ids
        ]
        if len(remaining_content) == len(paragraph.content):
            filtered.append(paragraph)
            continue
        clone = paragraph.model_copy(deep=True)
        clone.content = remaining_content
        clone.recompute_text()
        if clone.content or clone.text.strip():
            filtered.append(clone)
    return filtered


def _promoted_candidate_node_ids(
    assigned_candidates: list[_AssignedCandidate],
    *,
    promoted_candidate_ids: set[int],
) -> tuple[set[str], set[str]]:
    """실제로 layout-table로 승격된 candidate의 원본 node id만 제거 대상으로 삼는다."""
    paragraph_ids: set[str] = set()
    child_ids: set[str] = set()
    for assigned_candidate in assigned_candidates:
        if id(assigned_candidate.candidate) not in promoted_candidate_ids:
            continue
        paragraph_ids.update(node.node_id for node in assigned_candidate.paragraph_nodes)
        child_ids.update(node.node_id for node in assigned_candidate.table_nodes)
        child_ids.update(node.node_id for node in assigned_candidate.image_nodes)
        child_ids.update(node.node_id for node in assigned_candidate.run_nodes)
    return paragraph_ids, child_ids


# ---------- layout table promotion ----------
# pdfium/ODL visual block 후보를 실제 DocIR의 layout `TableIR`로 승격한다.
# 표처럼 보이는 박스/선 영역을 flow 안에서 안정적으로 보존하기 위한 preview 보정이다.

def _cluster_boundary_values(
    values: list[float],
    *,
    descending: bool,
) -> list[float]:
    if not values:
        return []

    clustered: list[list[float]] = []
    for value in sorted(values, reverse=descending):
        if not clustered or abs(value - clustered[-1][-1]) > _LAYOUT_TABLE_BOUNDARY_TOLERANCE_PT:
            clustered.append([value])
            continue
        clustered[-1].append(value)

    representatives = [sum(cluster) / len(cluster) for cluster in clustered]
    return sorted(representatives, reverse=descending)


def _nearest_boundary_index(boundaries: list[float], value: float) -> int:
    return min(range(len(boundaries)), key=lambda index: abs(boundaries[index] - value))


def _auxiliary_nodes_to_paragraphs(
    nodes: list[_PreviewRenderNode],
) -> list[tuple[tuple[float, float, int, int], ParagraphIR]]:
    if not nodes:
        return []

    grouped: dict[str, list[_PreviewRenderNode]] = {}
    group_order: dict[str, tuple[float, float, int, int]] = {}
    group_para_style: dict[str, Any] = {}
    group_bbox: dict[str, PdfBoundingBox] = {}
    for node in nodes:
        group_key = node.parent_paragraph_id or node.node_id
        grouped.setdefault(group_key, []).append(node)
        group_order[group_key] = min(group_order.get(group_key, node.order_key), node.order_key)
        if group_key not in group_para_style:
            group_para_style[group_key] = node.parent_para_style
        group_bbox[group_key] = (
            node.bbox
            if group_key not in group_bbox
            else PdfBoundingBox(
                left_pt=min(group_bbox[group_key].left_pt, node.bbox.left_pt),
                bottom_pt=min(group_bbox[group_key].bottom_pt, node.bbox.bottom_pt),
                right_pt=max(group_bbox[group_key].right_pt, node.bbox.right_pt),
                top_pt=max(group_bbox[group_key].top_pt, node.bbox.top_pt),
            )
        )

    paragraphs: list[tuple[tuple[float, float, int, int], ParagraphIR]] = []
    for group_key, group_nodes in grouped.items():
        content_nodes: list[Any] = []
        for node in sorted(group_nodes, key=lambda item: item.order_key):
            if node.table is not None:
                content_nodes.append(node.table.model_copy(deep=True))
            elif node.image is not None:
                content_nodes.append(node.image.model_copy(deep=True))
            elif node.run is not None:
                content_nodes.append(node.run.model_copy(deep=True))
        if not content_nodes:
            continue
        paragraph = ParagraphIR(
            **_pdf_node_kwargs("paragraph", f"{group_key}.layout-table"),
            text="",
            bbox=group_bbox.get(group_key),
            para_style=group_para_style.get(group_key),
            content=content_nodes,
        )
        paragraph.recompute_text()
        paragraphs.append((group_order[group_key], paragraph))
    return paragraphs


def _assigned_candidate_cell_paragraphs(assigned_candidate: _AssignedCandidate) -> list[ParagraphIR]:
    content_blocks: list[tuple[tuple[float, float, int, int], ParagraphIR]] = []
    for paragraph_node in sorted(assigned_candidate.paragraph_nodes, key=lambda item: item.order_key):
        if paragraph_node.paragraph is None:
            continue
        content_blocks.append((paragraph_node.order_key, paragraph_node.paragraph.model_copy(deep=True)))

    auxiliary_nodes = sorted(
        assigned_candidate.table_nodes + assigned_candidate.image_nodes + assigned_candidate.run_nodes,
        key=lambda node: node.order_key,
    )
    content_blocks.extend(_auxiliary_nodes_to_paragraphs(auxiliary_nodes))
    content_blocks.sort(key=lambda item: item[0])
    return [paragraph for _, paragraph in content_blocks]


def _assigned_candidate_real_table_unit_ids(
    assigned_candidate: _AssignedCandidate,
) -> set[str]:
    table_unit_ids: set[str] = set()

    for paragraph_node in assigned_candidate.paragraph_nodes:
        if paragraph_node.paragraph is None:
            continue
        for content_node in paragraph_node.paragraph.content:
            if isinstance(content_node, TableIR):
                table_unit_ids.add(content_node.node_id)

    for table_node in assigned_candidate.table_nodes:
        if table_node.table is not None:
            table_unit_ids.add(table_node.table.node_id)

    return table_unit_ids


def _group_has_real_table(
    assigned_candidate_group: _AssignedCandidateGroup,
) -> bool:
    for assigned_candidate in assigned_candidate_group.candidates:
        if _assigned_candidate_real_table_unit_ids(assigned_candidate):
            return True
    return False


def _layout_table_cell_style(
    bbox: PdfBoundingBox,
    *,
    colspan: int,
    rowspan: int,
) -> CellStyleInfo:
    return CellStyleInfo(
        width_pt=max(bbox.right_pt - bbox.left_pt, 0.0),
        border_top="1px solid #4a4f57",
        border_bottom="1px solid #4a4f57",
        border_left="1px solid #4a4f57",
        border_right="1px solid #4a4f57",
        colspan=max(colspan, 1),
        rowspan=max(rowspan, 1),
    )


def _build_layout_table_paragraph_for_group(
    assigned_candidate_group: _AssignedCandidateGroup,
    *,
    page_number: int,
    group_index: int,
) -> ParagraphIR | None:
    assigned_candidates = assigned_candidate_group.candidates
    if not assigned_candidates:
        return None

    group_bbox = assigned_candidate_group.bounding_box
    x_boundaries = _cluster_boundary_values(
        [group_bbox.left_pt, group_bbox.right_pt]
        + [candidate.candidate.bounding_box.left_pt for candidate in assigned_candidates]
        + [candidate.candidate.bounding_box.right_pt for candidate in assigned_candidates],
        descending=False,
    )
    y_boundaries = _cluster_boundary_values(
        [group_bbox.top_pt, group_bbox.bottom_pt]
        + [candidate.candidate.bounding_box.top_pt for candidate in assigned_candidates]
        + [candidate.candidate.bounding_box.bottom_pt for candidate in assigned_candidates],
        descending=True,
    )
    if len(x_boundaries) < 2 or len(y_boundaries) < 2:
        return None

    cells: list[TableCellIR] = []
    for candidate_index, assigned_candidate in enumerate(assigned_candidates, start=1):
        bbox = assigned_candidate.candidate.bounding_box
        left_index = _nearest_boundary_index(x_boundaries, bbox.left_pt)
        right_index = _nearest_boundary_index(x_boundaries, bbox.right_pt)
        top_index = _nearest_boundary_index(y_boundaries, bbox.top_pt)
        bottom_index = _nearest_boundary_index(y_boundaries, bbox.bottom_pt)

        colspan = max(right_index - left_index, 1)
        rowspan = max(bottom_index - top_index, 1)
        cell = TableCellIR(
            **_pdf_node_kwargs("cell", f"pdf-preview.p{page_number}.layout-table.{group_index}.cell.{candidate_index}"),
            row_index=top_index + 1,
            col_index=left_index + 1,
            bbox=bbox,
            cell_style=_layout_table_cell_style(
                bbox,
                colspan=colspan,
                rowspan=rowspan,
            ),
            paragraphs=_assigned_candidate_cell_paragraphs(assigned_candidate),
        )
        cell.recompute_text()
        cells.append(cell)

    table_path = f"pdf-preview.p{page_number}.layout-table.{group_index}"
    table = TableIR(
        **_pdf_node_kwargs("table", table_path),
        row_count=max(len(y_boundaries) - 1, 1),
        col_count=max(len(x_boundaries) - 1, 1),
        bbox=group_bbox,
        table_style=TableStyleInfo(
            row_count=max(len(y_boundaries) - 1, 1),
            col_count=max(len(x_boundaries) - 1, 1),
            width_pt=max(group_bbox.right_pt - group_bbox.left_pt, 0.0),
            # 도식용 layout table은 원본에 있던 박스 cell만 선을 그린다.
            # 전체 grid를 켜면 빈 filler cell에도 선이 생겨 PDF보다 지저분해진다.
            render_grid=False,
        ),
        cells=cells,
    )
    paragraph = ParagraphIR(
        **_pdf_node_kwargs("paragraph", f"{table_path}.paragraph"),
        text="",
        page_number=page_number,
        bbox=group_bbox,
        content=[table],
    )
    paragraph.recompute_text()
    return paragraph


def _promote_assigned_candidates_to_layout_tables(
    assigned_candidates: list[_AssignedCandidate],
    *,
    page: PageInfo,
) -> tuple[list[ParagraphIR], set[int]]:
    if not assigned_candidates:
        return [], set()

    paragraphs: list[ParagraphIR] = []
    promoted_candidate_ids: set[int] = set()
    for group_index, assigned_candidate_group in enumerate(_build_candidate_groups(assigned_candidates, page=page), start=1):
        if _group_has_real_table(assigned_candidate_group):
            continue
        paragraph = _build_layout_table_paragraph_for_group(
            assigned_candidate_group,
            page_number=page.page_number,
            group_index=group_index,
        )
        if paragraph is None:
            continue
        paragraphs.append(paragraph)
        promoted_candidate_ids.update(id(assigned_candidate.candidate) for assigned_candidate in assigned_candidate_group.candidates)

    return paragraphs, promoted_candidate_ids


def _bbox_union(bboxes: list[PdfBoundingBox | None]) -> PdfBoundingBox | None:
    filtered = [bbox for bbox in bboxes if bbox is not None]
    if not filtered:
        return None
    return PdfBoundingBox(
        left_pt=min(bbox.left_pt for bbox in filtered),
        bottom_pt=min(bbox.bottom_pt for bbox in filtered),
        right_pt=max(bbox.right_pt for bbox in filtered),
        top_pt=max(bbox.top_pt for bbox in filtered),
    )


def _page_regions_for_page(preview_context: PdfPreviewContext, page_number: int) -> list[PdfLayoutRegion]:
    return [region for region in preview_context.layout_regions if region.page_number == page_number]


def _paragraph_bbox_or_content(paragraph: ParagraphIR) -> PdfBoundingBox | None:
    return _paragraph_bbox(paragraph) or _bbox_union([_content_node_bbox(node) for node in paragraph.content])


_COLUMN_GRID_MATCH_TOLERANCE_PT = 12.0


def _column_layout_from_bboxes(
    left_column_bbox: PdfBoundingBox,
    right_column_bbox: PdfBoundingBox,
) -> ColumnLayoutInfo | None:
    gap_pt = max(right_column_bbox.left_pt - left_column_bbox.right_pt, 0.0)
    left_width = max(left_column_bbox.right_pt - left_column_bbox.left_pt, 0.0)
    right_width = max(right_column_bbox.right_pt - right_column_bbox.left_pt, 0.0)
    if gap_pt <= 0.0 or left_width <= 0.0 or right_width <= 0.0:
        return None
    return ColumnLayoutInfo(
        count=2,
        gap_pt=gap_pt,
        widths_pt=[left_width, right_width],
        equal_width=abs(left_width - right_width) <= 1.0,
    )


def _collect_column_grid_layouts(preview_context: PdfPreviewContext) -> list[tuple[PdfBoundingBox, PdfBoundingBox, ColumnLayoutInfo]]:
    grids: list[tuple[PdfBoundingBox, PdfBoundingBox, ColumnLayoutInfo]] = []
    page_numbers = sorted({region.page_number for region in preview_context.layout_regions})
    for page_number in page_numbers:
        page_regions = _page_regions_for_page(preview_context, page_number)
        left_column_bbox = _bbox_union(
            [region.bounding_box for region in page_regions if region.region_type == "left-column"]
        )
        right_column_bbox = _bbox_union(
            [region.bounding_box for region in page_regions if region.region_type == "right-column"]
        )
        if left_column_bbox is None or right_column_bbox is None:
            continue
        layout = _column_layout_from_bboxes(left_column_bbox, right_column_bbox)
        if layout is not None:
            grids.append((left_column_bbox, right_column_bbox, layout))
    return grids


def _matching_column_grid_layout(
    column_bbox: PdfBoundingBox,
    *,
    role: str,
    grids: list[tuple[PdfBoundingBox, PdfBoundingBox, ColumnLayoutInfo]],
) -> ColumnLayoutInfo | None:
    column_width = _bbox_width(column_bbox) or 0.0
    best: tuple[float, ColumnLayoutInfo] | None = None
    for left_bbox, right_bbox, layout in grids:
        reference_bbox = left_bbox if role == "left" else right_bbox
        reference_width = _bbox_width(reference_bbox) or 0.0
        left_delta = abs(column_bbox.left_pt - reference_bbox.left_pt)
        width_delta = abs(column_width - reference_width)
        if left_delta > _COLUMN_GRID_MATCH_TOLERANCE_PT or width_delta > _COLUMN_GRID_MATCH_TOLERANCE_PT:
            continue
        score = left_delta + width_delta
        if best is None or score < best[0]:
            best = (score, layout)
    return best[1].model_copy(deep=True) if best is not None else None


def _apply_region_driven_layout(
    doc_ir: DocIR,
    *,
    preview_context: PdfPreviewContext,
) -> None:
    """ODL left-column/right-column을 paragraph.para_style.column_layout에 반영한다.

    페이지를 나누지 않고, 같은 페이지 안의 2단 편집을 HTML 렌더러가
    좌/우 flow로 렌더할 수 있도록 `column_index`만 붙인다.
    """
    if not doc_ir.pages:
        return

    column_grid_layouts = _collect_column_grid_layouts(preview_context)

    for page in doc_ir.pages:
        page_regions = _page_regions_for_page(preview_context, page.page_number)
        left_column_bbox = _bbox_union(
            [
                region.bounding_box
                for region in page_regions
                if region.region_type == "left-column"
            ]
        )
        right_column_bbox = _bbox_union(
            [
                region.bounding_box
                for region in page_regions
                if region.region_type == "right-column"
            ]
        )
        if left_column_bbox is not None and right_column_bbox is not None:
            layout = _column_layout_from_bboxes(left_column_bbox, right_column_bbox)
        elif left_column_bbox is not None:
            layout = _matching_column_grid_layout(left_column_bbox, role="left", grids=column_grid_layouts)
        elif right_column_bbox is not None:
            layout = _matching_column_grid_layout(right_column_bbox, role="right", grids=column_grid_layouts)
        else:
            layout = None
        if layout is None:
            continue

        for paragraph in [p for p in doc_ir.paragraphs if p.page_number == page.page_number]:
            bbox = _paragraph_bbox_or_content(paragraph)
            if bbox is None:
                continue
            role = _bbox_region_type(
                bbox,
                page=page,
                page_regions=page_regions,
                explicit_region_type=None,
            )
            if role == "left":
                _set_paragraph_column_layout(paragraph, layout.model_copy(update={"column_index": 0}, deep=True))
            elif role == "right":
                _set_paragraph_column_layout(paragraph, layout.model_copy(update={"column_index": 1}, deep=True))


def _shift_bbox(bbox: PdfBoundingBox | None, *, origin: PdfBoundingBox) -> PdfBoundingBox | None:
    if bbox is None:
        return None
    return PdfBoundingBox(
        left_pt=bbox.left_pt - origin.left_pt,
        bottom_pt=bbox.bottom_pt - origin.bottom_pt,
        right_pt=bbox.right_pt - origin.left_pt,
        top_pt=bbox.top_pt - origin.bottom_pt,
    )


def _rebase_bboxes(node: Any, *, origin: PdfBoundingBox) -> None:
    """left/right-page 분리 후 bbox 좌표계를 새 논리 페이지 원점으로 옮긴다."""
    if hasattr(node, "bbox"):
        node.bbox = _shift_bbox(getattr(node, "bbox", None), origin=origin)

    if isinstance(node, ParagraphIR):
        for child in node.content:
            _rebase_bboxes(child, origin=origin)
    elif isinstance(node, TableIR):
        for cell in node.cells:
            _rebase_bboxes(cell, origin=origin)
    elif isinstance(node, TableCellIR):
        for paragraph in node.paragraphs:
            _rebase_bboxes(paragraph, origin=origin)


def _logical_page_info(source: PageInfo, *, page_number: int, bbox: PdfBoundingBox) -> PageInfo:
    """모아찍기 분리 후 사용할 논리 PageInfo를 만든다.

    region bbox 크기를 그대로 쓰면 페이지가 너무 작아지므로, 일반 A4처럼
    portrait 방향으로 정규화한다. source 크기가 없을 때만 bbox 크기로 fallback한다.
    """
    page = source.model_copy(deep=True)
    page.page_number = page_number
    if source.width_pt is not None and source.height_pt is not None:
        page.width_pt = min(source.width_pt, source.height_pt)
        page.height_pt = max(source.width_pt, source.height_pt)
    else:
        page.width_pt = _bbox_width(bbox)
        page.height_pt = _bbox_height(bbox)
    return page


def _split_spread_pages_for_doc(
    doc_ir: DocIR,
    *,
    preview_context: PdfPreviewContext,
) -> None:
    """ODL left-page/right-page 모아찍기를 synthetic page 두 장으로 분리한다.

    이 단계는 ODL이 명시적으로 `left-page/right-page`를 준 경우에만 동작한다.
    `left-column/right-column`은 페이지 분리가 아니라 2단 layout으로 처리한다.
    """
    paragraphs_by_page: dict[int, list[ParagraphIR]] = {}
    unpaged: list[ParagraphIR] = []
    for paragraph in doc_ir.paragraphs:
        if paragraph.page_number is None:
            unpaged.append(paragraph)
        else:
            paragraphs_by_page.setdefault(paragraph.page_number, []).append(paragraph)

    next_page_number = 1
    new_pages: list[PageInfo] = []
    new_paragraphs: list[ParagraphIR] = []

    for page in doc_ir.pages:
        page_regions = _page_regions_for_page(preview_context, page.page_number)
        left_bbox = _bbox_union([region.bounding_box for region in page_regions if region.region_type == "left-page"])
        right_bbox = _bbox_union([region.bounding_box for region in page_regions if region.region_type == "right-page"])
        page_paragraphs = paragraphs_by_page.pop(page.page_number, [])

        if left_bbox is None or right_bbox is None:
            copied_page = page.model_copy(deep=True)
            copied_page.page_number = next_page_number
            new_pages.append(copied_page)
            for paragraph in page_paragraphs:
                paragraph.page_number = next_page_number
                new_paragraphs.append(paragraph)
            next_page_number += 1
            continue

        split_regions = [region for region in page_regions if region.region_type in {"left-page", "right-page"}]
        for role_name, region_bbox in (("left", left_bbox), ("right", right_bbox)):
            new_pages.append(_logical_page_info(page, page_number=next_page_number, bbox=region_bbox))
            for paragraph in page_paragraphs:
                role = _bbox_region_type(
                    _paragraph_bbox_or_content(paragraph),
                    page=page,
                    page_regions=split_regions,
                    explicit_region_type=None,
                )
                if role != role_name:
                    continue
                cloned = paragraph.model_copy(deep=True)
                cloned.page_number = next_page_number
                _rebase_bboxes(cloned, origin=region_bbox)
                new_paragraphs.append(cloned)
            next_page_number += 1

    for paragraphs in paragraphs_by_page.values():
        new_paragraphs.extend(paragraphs)

    doc_ir.pages = new_pages
    doc_ir.paragraphs = new_paragraphs + unpaged


def _bbox_width(bbox: PdfBoundingBox | None) -> float | None:
    if bbox is None:
        return None
    return max(bbox.right_pt - bbox.left_pt, 0.0)


def _bbox_height(bbox: PdfBoundingBox | None) -> float | None:
    if bbox is None:
        return None
    return max(bbox.top_pt - bbox.bottom_pt, 0.0)


def _bbox_horizontal_slack(container: PdfBoundingBox, content: PdfBoundingBox) -> tuple[float, float]:
    return (
        max(content.left_pt - container.left_pt, 0.0),
        max(container.right_pt - content.right_pt, 0.0),
    )


def _bbox_vertical_slack(container: PdfBoundingBox, content: PdfBoundingBox) -> tuple[float, float]:
    return (
        max(container.top_pt - content.top_pt, 0.0),
        max(content.bottom_pt - container.bottom_pt, 0.0),
    )


def _infer_bbox_horizontal_align(container: PdfBoundingBox, content: PdfBoundingBox) -> str | None:
    container_width = _bbox_width(container) or 0.0
    content_width = _bbox_width(content) or 0.0
    if container_width <= 0.0 or content_width <= 0.0 or content_width >= container_width * 0.92:
        return None

    left_gap, right_gap = _bbox_horizontal_slack(container, content)
    slack = left_gap + right_gap
    if slack < max(container_width * 0.08, 6.0):
        return None
    if abs(left_gap - right_gap) <= max(container_width * 0.08, 6.0):
        return "center"
    if left_gap >= max(container_width * 0.16, 10.0) and right_gap <= max(slack * 0.18, 4.0):
        return "right"
    return None


def _infer_bbox_vertical_align(container: PdfBoundingBox, content: PdfBoundingBox) -> str | None:
    container_height = _bbox_height(container) or 0.0
    content_height = _bbox_height(content) or 0.0
    if container_height <= 0.0 or content_height <= 0.0 or content_height >= container_height * 0.86:
        return None

    top_gap, bottom_gap = _bbox_vertical_slack(container, content)
    if abs(top_gap - bottom_gap) <= max(container_height * 0.14, 5.0):
        return "middle"
    return None


def _vertical_overlap_ratio(left: PdfBoundingBox, right: PdfBoundingBox) -> float:
    overlap = max(0.0, min(left.top_pt, right.top_pt) - max(left.bottom_pt, right.bottom_pt))
    smaller_height = min(_bbox_height(left) or 0.0, _bbox_height(right) or 0.0)
    if smaller_height <= 0.0:
        return 0.0
    return overlap / smaller_height


_LAYOUT_ROW_ARROW_CONNECTORS = frozenset(
    {
        "->",
        "<-",
        "→",
        "←",
        "↑",
        "↓",
        "↔",
        "↕",
        "➡",
        "⬅",
        "⬆",
        "⬇",
        "➜",
        "➝",
        "➔",
        "⇒",
        "⇐",
        "⇧",
        "⇩",
        "⇔",
        "ð",
        "ï",
        "",
    }
)


def _is_layout_row_block_candidate(paragraph: ParagraphIR) -> bool:
    """가로 row의 기준이 될 수 있는 block paragraph인지 본다.

    일반 텍스트 오탐을 줄이기 위해 현재는 ImageIR/TableIR 포함 paragraph만 허용한다.
    """
    return any(isinstance(node, (ImageIR, TableIR)) for node in paragraph.content)


def _is_arrow_connector_paragraph(paragraph: ParagraphIR) -> bool:
    return (
        paragraph.text.strip() in _LAYOUT_ROW_ARROW_CONNECTORS
        and bool(paragraph.content)
        and all(isinstance(node, RunIR) for node in paragraph.content)
    )


def _is_layout_row_candidate(paragraph: ParagraphIR) -> bool:
    return _is_layout_row_block_candidate(paragraph) or _is_arrow_connector_paragraph(paragraph)


def _same_layout_row(left: ParagraphIR, right: ParagraphIR) -> bool:
    """두 block paragraph가 같은 가로 줄에 놓인 것으로 볼 수 있는지 판단한다."""
    left_bbox = _paragraph_bbox_or_content(left)
    right_bbox = _paragraph_bbox_or_content(right)
    if left_bbox is None or right_bbox is None:
        return False

    left_height = _bbox_height(left_bbox) or 0.0
    right_height = _bbox_height(right_bbox) or 0.0
    smaller_height = min(left_height, right_height)
    if smaller_height <= 0.0:
        return False

    _left_center_x, left_center_y = _bbox_center(left_bbox)
    _right_center_x, right_center_y = _bbox_center(right_bbox)
    center_delta = abs(left_center_y - right_center_y)
    return (
        _vertical_overlap_ratio(left_bbox, right_bbox) >= 0.70
        and center_delta <= max(smaller_height * 0.75, 12.0)
        and (left_bbox.right_pt <= right_bbox.left_pt or right_bbox.right_pt <= left_bbox.left_pt)
    )


def _column_layout_identity(paragraph: ParagraphIR) -> tuple[object, ...] | None:
    layout = _paragraph_column_layout(paragraph)
    if layout is None:
        return None
    return (
        layout.count,
        layout.column_index,
        round(layout.gap_pt, 3) if layout.gap_pt is not None else None,
        tuple(round(width, 3) for width in layout.widths_pt),
        tuple(round(gap, 3) for gap in layout.gaps_pt),
        layout.equal_width,
    )


def _layout_row_paragraph(
    *,
    page_number: int,
    row_index: int,
    paragraphs: list[ParagraphIR],
) -> ParagraphIR:
    """같은 줄에 놓인 block paragraph들을 1행 N열 borderless TableIR로 감싼다."""
    ordered = sorted(
        paragraphs,
        key=lambda paragraph: (_paragraph_bbox_or_content(paragraph).left_pt if _paragraph_bbox_or_content(paragraph) else 0.0),
    )
    row_bbox = _bbox_union([_paragraph_bbox_or_content(paragraph) for paragraph in ordered])
    cells: list[TableCellIR] = []

    for index, paragraph in enumerate(ordered, start=1):
        bbox = _paragraph_bbox_or_content(paragraph)
        next_bbox = _paragraph_bbox_or_content(ordered[index]) if index < len(ordered) else None
        gap_pt = None
        if bbox is not None and next_bbox is not None:
            gap_pt = max(next_bbox.left_pt - bbox.right_pt, 0.0)

        cell_paragraph = paragraph.model_copy(deep=True)
        _set_paragraph_column_layout(cell_paragraph, None)

        cell = TableCellIR(
            **_pdf_node_kwargs("cell", f"pdf-preview.p{page_number}.layout-row.{row_index}.cell.{index}"),
            row_index=1,
            col_index=index,
            bbox=bbox,
            cell_style=CellStyleInfo(
                width_pt=_bbox_width(bbox),
                height_pt=_bbox_height(bbox),
                vertical_align="top",
                padding_right_pt=gap_pt,
            ),
            paragraphs=[cell_paragraph],
        )
        cell.recompute_text()
        cells.append(cell)

    table_path = f"pdf-preview.p{page_number}.layout-row.{row_index}"
    table = TableIR(
        **_pdf_node_kwargs("table", table_path),
        row_count=1,
        col_count=len(cells),
        bbox=row_bbox,
        table_style=TableStyleInfo(
            row_count=1,
            col_count=len(cells),
            width_pt=_bbox_width(row_bbox),
            height_pt=_bbox_height(row_bbox),
            render_grid=False,
        ),
        cells=cells,
    )
    seed_layout = _paragraph_column_layout(ordered[0])
    paragraph = ParagraphIR(
        **_pdf_node_kwargs("paragraph", f"{table_path}.paragraph"),
        page_number=page_number,
        bbox=row_bbox,
        para_style=ParaStyleInfo(column_layout=seed_layout.model_copy(deep=True)) if seed_layout is not None else None,
        content=[table],
    )
    paragraph.recompute_text()
    return paragraph


def _promote_layout_rows_for_doc(doc_ir: DocIR) -> None:
    """image/table block이 가로로 나란히 놓인 경우 flow 안에서 한 줄로 보존한다."""
    if not doc_ir.paragraphs:
        return

    paragraphs_by_page: dict[int, list[ParagraphIR]] = {}
    unpaged: list[ParagraphIR] = []
    for paragraph in doc_ir.paragraphs:
        if paragraph.page_number is None:
            unpaged.append(paragraph)
            continue
        paragraphs_by_page.setdefault(paragraph.page_number, []).append(paragraph)

    new_paragraphs: list[ParagraphIR] = []
    row_index = 0
    for page in doc_ir.pages:
        page_paragraphs = paragraphs_by_page.pop(page.page_number, [])
        grouped_ids: set[int] = set()
        replacements: dict[int, ParagraphIR] = {}

        for seed in page_paragraphs:
            if id(seed) in grouped_ids or not _is_layout_row_block_candidate(seed):
                continue
            seed_layout = _column_layout_identity(seed)
            row = [
                candidate
                for candidate in page_paragraphs
                if id(candidate) not in grouped_ids
                and candidate is not seed
                and _is_layout_row_candidate(candidate)
                and _column_layout_identity(candidate) == seed_layout
                and _same_layout_row(seed, candidate)
            ]
            if not row:
                continue
            if not any(_is_layout_row_block_candidate(candidate) for candidate in row):
                continue

            row_index += 1
            row.insert(0, seed)
            replacement = _layout_row_paragraph(
                page_number=page.page_number,
                row_index=row_index,
                paragraphs=row,
            )
            replacements[id(seed)] = replacement
            grouped_ids.update(id(paragraph) for paragraph in row)

        for paragraph in page_paragraphs:
            if id(paragraph) in replacements:
                new_paragraphs.append(replacements[id(paragraph)])
            elif id(paragraph) not in grouped_ids:
                new_paragraphs.append(paragraph)

    for paragraphs in paragraphs_by_page.values():
        new_paragraphs.extend(paragraphs)
    doc_ir.paragraphs = new_paragraphs + unpaged


def _paragraph_text_bbox(paragraph: ParagraphIR) -> PdfBoundingBox | None:
    return _bbox_union([run.bbox for run in paragraph.runs if run.bbox is not None])


def _cell_content_bbox(cell: TableCellIR) -> PdfBoundingBox | None:
    bboxes: list[PdfBoundingBox | None] = []
    for paragraph in cell.paragraphs:
        bboxes.append(_paragraph_text_bbox(paragraph) or _paragraph_bbox_or_content(paragraph))
    return _bbox_union(bboxes)


def _apply_table_cell_bbox_style_hints(table: TableIR) -> None:
    for cell in table.cells:
        if cell.bbox is None:
            continue
        content_bbox = _cell_content_bbox(cell)
        if content_bbox is None:
            continue

        style = cell.cell_style
        if style is None:
            style = CellStyleInfo()
            cell.cell_style = style

        if style.horizontal_align is None:
            style.horizontal_align = _infer_bbox_horizontal_align(cell.bbox, content_bbox)
        if style.vertical_align is None:
            style.vertical_align = _infer_bbox_vertical_align(cell.bbox, content_bbox)


def _page_content_bbox(page: PageInfo) -> PdfBoundingBox | None:
    if page.width_pt is None or page.height_pt is None:
        return None
    margin_left = _non_negative_value(page.margin_left_pt, default=42.0)
    margin_right = _non_negative_value(page.margin_right_pt, default=42.0)
    margin_top = _non_negative_value(page.margin_top_pt, default=48.0)
    margin_bottom = _non_negative_value(page.margin_bottom_pt, default=48.0)
    return PdfBoundingBox(
        left_pt=margin_left,
        bottom_pt=margin_bottom,
        right_pt=max(page.width_pt - margin_right, margin_left),
        top_pt=max(page.height_pt - margin_top, margin_bottom),
    )


def _non_negative_value(value: float | None, *, default: float) -> float:
    if value is None:
        return default
    return max(value, 0.0)


def _apply_paragraph_bbox_style_hints(
    paragraph: ParagraphIR,
    *,
    container_bbox: PdfBoundingBox,
) -> None:
    if _paragraph_column_layout(paragraph) is not None:
        return

    bbox = _paragraph_text_bbox(paragraph) or _paragraph_bbox(paragraph)
    if bbox is None:
        return

    style = paragraph.para_style
    if style is None:
        style = ParaStyleInfo()
        paragraph.para_style = style

    if style.align is None:
        style.align = _infer_bbox_horizontal_align(container_bbox, bbox)

    if style.align is not None:
        return
    if style.left_indent_pt is not None:
        return

    left_indent = bbox.left_pt - container_bbox.left_pt
    content_width = _bbox_width(container_bbox) or 0.0
    if 4.0 <= left_indent <= content_width * 0.35:
        style.left_indent_pt = left_indent


def _apply_table_tree_bbox_style_hints(table: TableIR) -> None:
    for cell in table.cells:
        container_bbox = cell.bbox or table.bbox
        if container_bbox is None:
            continue
        for paragraph in cell.paragraphs:
            _apply_paragraph_tree_bbox_style_hints(paragraph, container_bbox=container_bbox)
    _apply_table_cell_bbox_style_hints(table)


def _apply_paragraph_tree_bbox_style_hints(
    paragraph: ParagraphIR,
    *,
    container_bbox: PdfBoundingBox,
) -> None:
    for node in paragraph.content:
        if isinstance(node, TableIR):
            _apply_table_tree_bbox_style_hints(node)
    _apply_paragraph_bbox_style_hints(paragraph, container_bbox=container_bbox)


def _apply_bbox_style_hints(doc_ir: DocIR) -> None:
    """PDF bbox를 공통 style hint로 바꿔 HTML/HWP 렌더러가 재사용하게 한다."""
    pages = {page.page_number: page for page in doc_ir.pages}
    for paragraph in doc_ir.paragraphs:
        if paragraph.page_number is None:
            continue
        page = pages.get(paragraph.page_number)
        if page is None:
            continue
        page_bbox = _page_content_bbox(page)
        if page_bbox is None:
            continue
        _apply_paragraph_tree_bbox_style_hints(paragraph, container_bbox=page_bbox)


# ---------- public surface ----------

def enrich_pdf_doc_ir(
    doc_ir: DocIR,
    *,
    preview_context: PdfPreviewContext | None = None,
) -> DocIR:
    """PDF 원천 정보에서 얻은 layout/style hint를 공통 DocIR에 반영한다.

    호출 순서가 중요하다. 먼저 visual block을 TableIR로 승격하고, spread page를
    나눈 뒤, 남은 page 안에서 column/layout-row 보정을 적용한다.
    """
    if (doc_ir.source_doc_type or "").lower() != "pdf":
        return doc_ir

    _apply_preview_table_geometry(doc_ir, preview_context=preview_context)

    # Raster-based refinement stays here so the shared HTML renderer remains
    # unaware of PDF-specific extraction quirks.
    enrich_pdf_table_backgrounds(doc_ir)
    if preview_context is not None:
        _promote_visual_boxes_for_doc(doc_ir, preview_context=preview_context)
        _split_spread_pages_for_doc(doc_ir, preview_context=preview_context)
        _apply_region_driven_layout(doc_ir, preview_context=preview_context)
        _promote_layout_rows_for_doc(doc_ir)
    _apply_bbox_style_hints(doc_ir)
    return doc_ir


def _promote_visual_boxes_for_doc(
    doc_ir: DocIR,
    *,
    preview_context: PdfPreviewContext,
) -> None:
    """각 물리 페이지에서 visual block 후보를 layout TableIR로 승격한다."""
    if not doc_ir.pages or not preview_context.visual_block_candidates:
        return

    paragraphs_by_page: dict[int, list[ParagraphIR]] = {}
    unpaged: list[ParagraphIR] = []
    for paragraph in doc_ir.paragraphs:
        if paragraph.page_number is None:
            unpaged.append(paragraph)
            continue
        paragraphs_by_page.setdefault(paragraph.page_number, []).append(paragraph)

    handled_page_numbers: set[int] = set()
    new_paragraphs: list[ParagraphIR] = []
    residual_candidate_ids: set[int] = set()

    for page in doc_ir.pages:
        handled_page_numbers.add(page.page_number)
        page_paragraphs = paragraphs_by_page.get(page.page_number, [])
        page_regions = [
            region
            for region in preview_context.layout_regions
            if region.page_number == page.page_number
        ]
        assigned_candidates, _assigned_paragraph_ids, _assigned_child_ids = _assign_page_nodes_to_candidates(
            page,
            page_paragraphs,
            page_regions=page_regions,
            preview_context=preview_context,
        )
        promoted_paragraphs, promoted_candidate_ids = _promote_assigned_candidates_to_layout_tables(
            assigned_candidates,
            page=page,
        )
        promoted_paragraph_ids, promoted_child_ids = _promoted_candidate_node_ids(
            assigned_candidates,
            promoted_candidate_ids=promoted_candidate_ids,
        )
        flow_paragraphs = _filter_page_flow_paragraphs(
            page_paragraphs,
            assigned_paragraph_ids=promoted_paragraph_ids,
            assigned_child_ids=promoted_child_ids,
        )
        flow_paragraphs.extend(promoted_paragraphs)

        # Preserve visual reading order across original paragraphs and promoted tables.
        indexed = list(enumerate(flow_paragraphs))
        indexed.sort(key=lambda item: _bbox_order_key(_paragraph_bbox(item[1]), fallback_index=item[0]))
        new_paragraphs.extend(paragraph for _, paragraph in indexed)

        for assigned_candidate in assigned_candidates:
            if id(assigned_candidate.candidate) not in promoted_candidate_ids:
                residual_candidate_ids.add(id(assigned_candidate.candidate))

    # Carry along paragraphs whose page number doesn't match any DocIR page.
    for page_number, paragraphs in paragraphs_by_page.items():
        if page_number not in handled_page_numbers:
            new_paragraphs.extend(paragraphs)

    doc_ir.paragraphs = new_paragraphs + unpaged
    preview_context.visual_block_candidates = [
        candidate
        for candidate in preview_context.visual_block_candidates
        if id(candidate) in residual_candidate_ids
    ]


def _apply_preview_table_geometry(
    doc_ir: DocIR,
    *,
    preview_context: PdfPreviewContext | None,
) -> DocIR:
    """ODL table grid boundary를 기존 TableIR의 width/height/cell size에 반영한다."""
    if preview_context is None or not preview_context.tables:
        return doc_ir

    table_contexts = [
        table_context
        for table_context in preview_context.tables
        if table_context.grid_row_boundaries or table_context.grid_column_boundaries
    ]
    if not table_contexts:
        return doc_ir

    for paragraph in doc_ir.paragraphs:
        for table in paragraph.tables:
            table_context = _match_preview_table_context(table_contexts, paragraph.page_number, table)
            if table_context is None:
                continue
            _apply_table_context(table, table_context)

    return doc_ir


def _match_preview_table_context(
    candidates: list[PdfPreviewTableContext],
    paragraph_page_number: int | None,
    table,
) -> PdfPreviewTableContext | None:
    page_number = paragraph_page_number
    bounding_box = getattr(table, "bbox", None)

    if page_number is None:
        return None

    if bounding_box is None:
        return None

    for candidate in candidates:
        if candidate.page_number != page_number or candidate.bounding_box is None:
            continue
        if _shared_bbox_distance(candidate.bounding_box, bounding_box) <= 4.0:
            return candidate
    return None


def _apply_table_context(table, table_context: PdfPreviewTableContext) -> None:
    if table.table_style is not None:
        if table.table_style.width_pt is None and table_context.grid_column_boundaries:
            table.table_style.width_pt = _span_extent(table_context.grid_column_boundaries, 1, table.col_count)
        if table.table_style.height_pt is None and table_context.grid_row_boundaries:
            table.table_style.height_pt = _span_extent(table_context.grid_row_boundaries, 1, table.row_count)

    for cell in table.cells:
        if cell.cell_style is None:
            cell.cell_style = CellStyleInfo()

        colspan = max(cell.cell_style.colspan, 1)
        rowspan = max(cell.cell_style.rowspan, 1)

        if cell.cell_style.width_pt is None and table_context.grid_column_boundaries:
            width_pt = _span_extent(table_context.grid_column_boundaries, cell.col_index, colspan)
            if width_pt is not None:
                cell.cell_style.width_pt = width_pt
        if cell.cell_style.height_pt is None and table_context.grid_row_boundaries:
            height_pt = _span_extent(table_context.grid_row_boundaries, cell.row_index, rowspan)
            if height_pt is not None:
                cell.cell_style.height_pt = height_pt


def _span_extent(boundaries: list[float], start_index_1based: int, span: int) -> float | None:
    start_index = start_index_1based - 1
    end_index = start_index + span
    if start_index < 0 or end_index >= len(boundaries):
        return None
    return abs(boundaries[end_index] - boundaries[start_index])
