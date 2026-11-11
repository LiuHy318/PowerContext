"""阅读清单服务（Reading List Service）。

一个刻意保持小巧、可运行、带测试的业务工程，用于 SDD（Spec-Driven Development）
上下文管理系统的端到端验证。规格见 ``minimax/specs/``。
"""

from reading_list.models import Page, ReadingItem, Status
from reading_list.service import ReadingListService, list_all_items

__all__ = ["Page", "ReadingItem", "Status", "ReadingListService", "list_all_items"]
