"""find 搜索语言：在视频中按条件定位时刻与区间。详见 query 模块说明。"""

from pixelprobe.find.engine import DEFINITIONS, FindResult, find
from pixelprobe.find.query import QUERY_HINT, Term, needs_decode, parse_query

__all__ = ["DEFINITIONS", "FindResult", "QUERY_HINT", "Term", "find", "needs_decode", "parse_query"]
