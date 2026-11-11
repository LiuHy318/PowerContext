"""列表查询服务：排序、分页，以及（待实现的）筛选。

本文件是 TASK-001 的**局部修改目标**。基线状态见 ``minimax/docs/snapshots/before/``。
"""

from __future__ import annotations

from collections.abc import Iterable

from reading_list.models import Page, ReadingItem, Status

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100
DEFAULT_SORT = "added_at_desc"
SORT_MODES = (DEFAULT_SORT, "added_at_asc", "title_asc")


def _validate_page(page: int, page_size: int, sort: str) -> None:
    """分页与排序参数的入参校验（SPEC-002 固定行为，不得放宽）。"""
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("page must be an integer >= 1")
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise ValueError("page_size must be an integer")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be within 1..{MAX_PAGE_SIZE}")
    if sort not in SORT_MODES:
        raise ValueError(f"sort must be one of {SORT_MODES}")


def _sort_key(sort: str):
    """排序键：主键相同时以 ``id`` 升序作为稳定次序（SPEC-002）。"""
    if sort == "added_at_asc":
        return lambda item: (item.added_at, item.id)
    if sort == "title_asc":
        return lambda item: (item.title, item.id)
    return lambda item: (-item.added_at.timestamp(), item.id)


class ReadingListService:
    """只读的阅读清单查询服务。"""

    def __init__(self, items: Iterable[ReadingItem]) -> None:
        self._items: tuple[ReadingItem, ...] = tuple(sorted(items, key=lambda item: item.id))

    @property
    def items(self) -> tuple[ReadingItem, ...]:
        """全量条目（未排序、未分页），供调试与测试使用。"""
        return self._items

    def get_item(self, item_id: str) -> ReadingItem | None:
        """按 id 取单条；不存在返回 ``None``（SPEC-003）。"""
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    def list_items(
        self,
        *,
        status: Status | str | None = None,
        min_rating: int | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        sort: str = DEFAULT_SORT,
    ) -> Page:
        """返回一页阅读条目。

        ``status`` 与 ``min_rating`` 的筛选语义见 SPEC-001；``sort`` / ``page`` /
        ``page_size`` 的语义见 SPEC-002。
        """
        _validate_page(page, page_size, sort)

        if status is not None:
            try:
                status = Status(status)
            except (TypeError, ValueError) as exc:
                valid_statuses = tuple(item.value for item in Status)
                raise ValueError(f"status must be one of {valid_statuses}") from exc
        if min_rating is not None:
            if not isinstance(min_rating, int) or isinstance(min_rating, bool):
                raise ValueError("min_rating must be an integer")
            if min_rating < 1 or min_rating > 5:
                raise ValueError("min_rating must be within 1..5")

        selected: list[ReadingItem] = []
        for item in self._items:
            if status is not None and item.status != status:
                continue
            if min_rating is not None and (
                item.rating is None or item.rating < min_rating
            ):
                continue
            selected.append(item)

        ordered = sorted(selected, key=_sort_key(sort))
        total = len(ordered)
        total_pages = (total + page_size - 1) // page_size
        start = (page - 1) * page_size
        window = ordered[start : start + page_size]
        return Page(
            items=window,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        )


def list_all_items(
    items: Iterable[ReadingItem],
    *,
    status: Status | str | None = None,
    min_rating: int | None = None,
) -> list[ReadingItem]:
    """便捷包装：返回全量（不分页）结果，语义与 ``list_items`` 一致。"""
    service = ReadingListService(items)
    page = service.list_items(status=status, min_rating=min_rating, page=1, page_size=MAX_PAGE_SIZE)
    return list(page.items)
