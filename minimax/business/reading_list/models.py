"""领域模型：阅读条目与分页结果。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Status(str, Enum):
    """阅读状态。取值是稳定的对外契约，见 SPEC-002。"""

    TO_READ = "to_read"
    READING = "reading"
    FINISHED = "finished"


class ReadingItem(BaseModel):
    """一条阅读记录。``rating`` 为 1..5，未评分为 ``None``。"""

    id: str
    title: str
    author: str
    status: Status
    rating: int | None = Field(default=None, ge=1, le=5)
    added_at: datetime
    tags: list[str] = Field(default_factory=list)


class Page(BaseModel):
    """一页结果。``total`` / ``total_pages`` 描述筛选后的全集，见 SPEC-002。"""

    items: list[ReadingItem]
    page: int
    page_size: int
    total: int
    total_pages: int
