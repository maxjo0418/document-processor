"""ODL 점선 table 재구성 중 text tree를 잘라내고 분배하는 helper."""

from __future__ import annotations

import unicodedata
from typing import Any

from ..meta import PdfBoundingBox, coerce_bbox
from .table_reconstruct_geometry import _INTERIOR_PAD_PT

_LEAF_TEXT_TYPES = frozenset({"span", "text chunk", "run"})
_CHILD_KEYS = ("kids", "spans", "runs", "list items")
# Cells carry their content in parallel ``kids``/``paragraphs`` lists; both
# must be rewritten in lockstep so downstream distribution sees the same
# split structure on either key.
_PRESPLIT_CHILD_KEYS = _CHILD_KEYS + ("paragraphs",)


def _learn_unit_separators(nodes: list[dict[str, Any]]) -> set[str]:
    """node들에서 paragraph 앞에 쓰인 bullet 성격의 구분 문자를 찾는다.

    어떤 문자가 leaf ``content``의 첫 non-whitespace 문자로 나오고, 그 뒤에
    공백이 있으며, Unicode punctuation / symbol / other-number 범주에 속하면
    unit separator로 본다. 이 기준은 ``§`` ``▪`` ``•`` ``※`` ``①②③`` 등을
    잡아내되 문자와 plain ASCII digit은 제외한다. 문서에서 직접 학습하기
    때문에 고정 bullet set을 하드코딩하지 않아도 된다.
    """
    seps: set[str] = set()

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        content = node.get("content")
        if isinstance(content, str):
            stripped = content.lstrip()
            if len(stripped) >= 2 and stripped[1].isspace():
                ch = stripped[0]
                if _is_unit_marker(ch):
                    seps.add(ch)
        for key in _CHILD_KEYS:
            items = node.get(key)
            if isinstance(items, list):
                for item in items:
                    visit(item)

    for n in nodes:
        visit(n)
    return seps


def _is_unit_marker(ch: str) -> bool:
    if ch.isalpha():
        return False
    if ch.isascii() and ch.isdigit():
        return False
    cat = unicodedata.category(ch)
    # S* Symbol (math, other, modifier), P* Punctuation, No "Other Number"
    # (covers circled digits like ①). Nd (decimal digit) is handled by the
    # isascii-isdigit check above, but non-ASCII decimal digits are also
    # excluded via that check.
    return bool(cat) and (cat[0] == "S" or cat[0] == "P" or cat == "No")


def _split_content_into_units(
    content: str,
    separators: set[str],
    *,
    target_count: int,
) -> list[str] | None:
    """정확히 ``target_count``개의 논리 unit을 반환하거나 ``None``을 반환한다.

    기본 전략은 문서에서 학습한 bullet separator를 사용해
    ``<whitespace><separator>`` 경계에서 나누는 것이다. 이 방식은
    ``"④ ..." / "§ ..."`` 같은 의미 단위를 보존한다.

    fallback 전략은 bullet split 결과 개수가 맞지 않을 때 whitespace로 나누는
    것이다. 단, 결과 token이 모두 짧은 code-like identifier처럼 보일 때만
    인정한다. 예를 들어 table의 "표준산업분류" column에서 ``"66 68 69390"``
    처럼 ODL이 한 leaf로 합친 code list를 복구하면서, 한국어/영어 문장은
    잘못 나누지 않는다.
    """
    if not content:
        return None
    if separators:
        bullet_units = _split_by_bullet_separators(content, separators)
        if len(bullet_units) == target_count:
            return bullet_units
    ws_units = content.split()
    if (
        len(ws_units) == target_count
        and all(_is_simple_token(u) for u in ws_units)
    ):
        return ws_units
    return None


def _split_by_bullet_separators(content: str, separators: set[str]) -> list[str]:
    units: list[str] = []
    start = 0
    i = 1
    while i < len(content):
        if content[i] in separators and content[i - 1].isspace():
            units.append(content[start:i].strip())
            start = i
        i += 1
    units.append(content[start:].strip())
    return [u for u in units if u]


def _is_simple_token(token: str) -> bool:
    """짧은 code-like token인지 판단한다.

    digit, symbol, mixed token은 허용하지만 순수 alphabetic prose는 제외한다.
    이 기준은 whitespace fallback이 우연히 word count가 맞는 한국어/영어
    문장을 잘못 나누는 일을 막는다.
    """
    if not token or len(token) > 15:
        return False
    has_digit = any(ch.isdigit() for ch in token)
    has_alpha = any(ch.isalpha() for ch in token)
    # Accept: any token containing a digit, or a token with no letters at all
    # (pure punctuation / symbol markers).
    return has_digit or not has_alpha


def _presplit_merged_leaves(
    cell: dict[str, Any],
    y_splits: list[float],
    separators: set[str],
) -> None:
    """``cell`` tree를 순회하며 ``y_splits``를 가로지르는 leaf를 미리 나눈다.

    leaf content가 가로지른 sub-band 수와 같은 개수의 unit으로 분해될 때만
    split한다. 새로 만든 synthetic sub-leaf에는 비례 bbox를 부여해서 이후
    분배 단계가 각 unit을 올바른 sub-band에 넣을 수 있게 한다.
    """

    def process(parent: dict[str, Any]) -> None:
        for key in _PRESPLIT_CHILD_KEYS:
            items = parent.get(key)
            if not isinstance(items, list):
                continue
            new_items: list[Any] = []
            for item in items:
                if not isinstance(item, dict):
                    new_items.append(item)
                    continue
                process(item)
                has_children = any(
                    isinstance(item.get(k), list) and item.get(k) for k in _CHILD_KEYS
                )
                if has_children:
                    new_items.append(item)
                    continue
                split = _try_split_merged_leaf(item, y_splits, separators)
                if split:
                    new_items.extend(split)
                else:
                    new_items.append(item)
            parent[key] = new_items

    process(cell)


def _try_split_merged_leaf(
    leaf: dict[str, Any],
    y_splits: list[float],
    separators: set[str],
) -> list[dict[str, Any]] | None:
    bbox = coerce_bbox(leaf.get("bounding box"))
    if bbox is None:
        return None
    content = leaf.get("content")
    if not isinstance(content, str) or not content:
        return None
    interior_ys = sorted(
        y
        for y in y_splits
        if bbox.bottom_pt + _INTERIOR_PAD_PT < y < bbox.top_pt - _INTERIOR_PAD_PT
    )
    if not interior_ys:
        return None
    units = _split_content_into_units(
        content, separators, target_count=len(interior_ys) + 1
    )
    if units is None:
        return None

    y_cuts = [bbox.bottom_pt, *interior_ys, bbox.top_pt]
    # Content order: first unit is visually at the TOP (highest y), so it
    # maps to the topmost y-band (y_cuts[-2..-1]). Reverse the list so
    # index i aligns with y_cuts[i..i+1] (bottom-up traversal).
    units_bottom_to_top = list(reversed(units))
    out: list[dict[str, Any]] = []
    for i, unit in enumerate(units_bottom_to_top):
        new_leaf = dict(leaf)
        new_leaf["bounding box"] = [
            bbox.left_pt,
            y_cuts[i],
            bbox.right_pt,
            y_cuts[i + 1],
        ]
        new_leaf["content"] = unit
        out.append(new_leaf)
    return out


def _distribute_children(
    children: Any,
    sub_bbox: PdfBoundingBox,
) -> list[Any]:
    """``sub_bbox`` 안에 속하는 child subset을 반환한다.

    dict child는 모두 ``_node_restricted_to``를 거친다. node가 leaf span을
    들고 있으면 span level로 pruning하고, 그렇지 않으면 bbox center test로
    판단한다. 이 방식은 ``paragraph``/``heading`` 같은 type과 무관하게,
    ``선정규모`` + ``협력기업``처럼 시각적으로 다른 label이 한 node의 여러
    stacked span으로 들어온 ODL 출력도 처리한다.
    """
    if not isinstance(children, list):
        return []
    kept: list[Any] = []
    for child in children:
        if not isinstance(child, dict):
            kept.append(child)
            continue
        restricted = _node_restricted_to(child, sub_bbox)
        if restricted is not None:
            kept.append(restricted)
    return kept


def _node_restricted_to(
    node: dict[str, Any],
    sub_bbox: PdfBoundingBox,
) -> dict[str, Any] | None:
    """``sub_bbox`` 안의 descendant만 남긴 ``node`` shallow copy를 반환한다.

    일반적인 ODL tree pruning이다. 알려진 container key
    (``kids``/``spans``/``runs``/``list items``)를 재귀적으로 따라가서,
    stacked span을 가진 paragraph, list item을 가진 list, label을 묶은
    heading 같은 hierarchy를 node shape가 유지되는 가장 세밀한 level에서
    좁힌다. leaf-like node는 bbox-center match로 판단한다.
    """
    if node.get("type") in _LEAF_TEXT_TYPES:
        bbox = coerce_bbox(node.get("bounding box")) or coerce_bbox(node.get("bbox"))
        if bbox is None:
            return None
        return dict(node) if _bbox_center_in(bbox, sub_bbox) else None

    child_collections: list[tuple[str, list[Any]]] = []
    for key in _CHILD_KEYS:
        items = node.get(key)
        if isinstance(items, list) and items:
            child_collections.append((key, items))

    if not child_collections:
        node_bbox = coerce_bbox(node.get("bounding box"))
        if node_bbox is None:
            return dict(node)
        return dict(node) if _bbox_center_in(node_bbox, sub_bbox) else None

    new_node = dict(node)
    kept_any = False
    for key, items in child_collections:
        new_items: list[Any] = []
        for item in items:
            if isinstance(item, dict):
                restricted = _node_restricted_to(item, sub_bbox)
                if restricted is not None:
                    new_items.append(restricted)
                    kept_any = True
            else:
                new_items.append(item)
        new_node[key] = new_items

    if not kept_any:
        return None

    # Rebuild bbox from surviving leaf descendants so parent cells can rely
    # on consistent bbox information post-pruning.
    leaf_bboxes = _collect_leaf_bboxes(new_node)
    if leaf_bboxes:
        new_node["bounding box"] = [
            min(b.left_pt for b in leaf_bboxes),
            min(b.bottom_pt for b in leaf_bboxes),
            max(b.right_pt for b in leaf_bboxes),
            max(b.top_pt for b in leaf_bboxes),
        ]

    # For nodes that carry text as a concatenated ``content`` string (paragraph,
    # heading, …), rebuild it from the surviving direct leaf spans so the
    # downstream adapter's text extraction stays in sync with the retained
    # spans. Nodes without direct leaf spans (list, list item, …) keep their
    # original content field.
    direct_spans = [
        item
        for key in ("spans", "runs")
        for item in new_node.get(key) or []
        if isinstance(item, dict) and item.get("type") in _LEAF_TEXT_TYPES
    ]
    if direct_spans and "content" in new_node:
        new_node["content"] = "".join(
            s.get("content", "")
            for s in direct_spans
            if isinstance(s.get("content"), str)
        )

    # Keep list metadata consistent.
    if isinstance(new_node.get("list items"), list):
        new_node["number of list items"] = len(new_node["list items"])

    return new_node


def _collect_leaf_bboxes(node: dict[str, Any]) -> list[PdfBoundingBox]:
    bboxes: list[PdfBoundingBox] = []

    def visit(current: Any) -> None:
        if not isinstance(current, dict):
            return
        has_children = any(
            isinstance(current.get(k), list) and current.get(k) for k in _CHILD_KEYS
        )
        if current.get("type") in _LEAF_TEXT_TYPES or not has_children:
            bbox = coerce_bbox(current.get("bounding box")) or coerce_bbox(current.get("bbox"))
            if bbox is not None:
                bboxes.append(bbox)
            return
        for key in _CHILD_KEYS:
            items = current.get(key)
            if isinstance(items, list):
                for item in items:
                    visit(item)

    for key in _CHILD_KEYS:
        items = node.get(key)
        if isinstance(items, list):
            for item in items:
                visit(item)
    return bboxes


def _bbox_center_in(bbox: PdfBoundingBox, sub_bbox: PdfBoundingBox) -> bool:
    cx = (bbox.left_pt + bbox.right_pt) / 2.0
    cy = (bbox.bottom_pt + bbox.top_pt) / 2.0
    # Strict half-open partitioning: a center lying on any sub-cell boundary
    # is assigned to exactly one neighbor (the lower / left one). No padding
    # on either side — when adjacent sub-cells share a boundary, padding on
    # one side's boundary would let a boundary-hugging center match both
    # and end up duplicated.
    return (
        sub_bbox.left_pt <= cx < sub_bbox.right_pt
        and sub_bbox.bottom_pt <= cy < sub_bbox.top_pt
    )
