"""Cron 表达式解析器 - 标准 5 字段格式

字段顺序: 分钟 小时 日 月 星期
支持语法: *、*/N、N、N-M、N-M/S、N,M,...
不支持: L、W、? 等扩展语法（解析时报错）
所有时间按本地时区解释。
Day-of-month 和 day-of-week 同时约束（均非 *）时使用 OR 语义（标准 cron 行为）。
"""
from datetime import datetime, timedelta
from typing import Optional, Set


class CronParseError(ValueError):
    """Cron 表达式解析错误"""


# 各字段的取值范围: (最小值, 最大值)
_FIELD_RANGES = [
    (0, 59),   # 分钟
    (0, 23),   # 小时
    (1, 31),   # 日
    (1, 12),   # 月
    (0, 7),    # 星期（0 和 7 均表示周日）
]

_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

# 禁止出现的扩展语法字符
_FORBIDDEN_CHARS = set("LlWw?")

# next_fire 最大搜索天数（约 4 年，覆盖闰年 2/29 场景）
_MAX_SEARCH_DAYS = 1500


def _parse_field(field: str, min_val: int, max_val: int, field_name: str) -> Set[int]:
    """解析单个字段，返回允许的值集合"""
    values: Set[int] = set()

    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronParseError(f"Empty item in {field_name} field: '{field}'")

        step = 1
        if "/" in part:
            base, _, step_str = part.partition("/")
            if not step_str.isdigit() or int(step_str) < 1:
                raise CronParseError(f"Invalid step '{step_str}' in {field_name} field")
            step = int(step_str)
            part = base

        if part == "*":
            lo, hi = min_val, max_val
        elif "-" in part:
            lo_str, _, hi_str = part.partition("-")
            if not lo_str.isdigit() or not hi_str.isdigit():
                raise CronParseError(f"Invalid range '{part}' in {field_name} field")
            lo, hi = int(lo_str), int(hi_str)
        elif part.isdigit():
            lo = hi = int(part)
        else:
            raise CronParseError(f"Invalid value '{part}' in {field_name} field")

        if lo < min_val or hi > max_val or lo > hi:
            raise CronParseError(
                f"Value out of range in {field_name} field: '{part}' "
                f"(allowed {min_val}-{max_val})"
            )

        values.update(range(lo, hi + 1, step))

    return values


class CronSchedule:
    """解析后的 Cron 计划"""

    def __init__(self, expression: str):
        self.expression = expression.strip()
        self._parse()

    def _parse(self):
        expr = self.expression

        # 拒绝不支持的扩展语法
        for ch in _FORBIDDEN_CHARS:
            if ch in expr:
                raise CronParseError(f"Unsupported cron syntax '{ch}' in '{expr}' (L/W/? are not supported)")

        fields = expr.split()
        if len(fields) != 5:
            raise CronParseError(
                f"Cron expression must have exactly 5 fields (minute hour day month weekday), got {len(fields)}: '{expr}'"
            )

        self.minutes = _parse_field(fields[0], 0, 59, _FIELD_NAMES[0])
        self.hours = _parse_field(fields[1], 0, 23, _FIELD_NAMES[1])
        self.days = _parse_field(fields[2], 1, 31, _FIELD_NAMES[2])
        self.months = _parse_field(fields[3], 1, 12, _FIELD_NAMES[3])
        weekdays = _parse_field(fields[4], 0, 7, _FIELD_NAMES[4])
        # 7 -> 0（均表示周日）
        self.weekdays = {0 if v == 7 else v for v in weekdays}

        # 记录日/星期字段是否为通配（用于 OR 语义判定）
        self._dom_star = fields[2].strip() == "*"
        self._dow_star = fields[4].strip() == "*"

    # ===== 匹配 =====

    def _day_matches(self, dt: datetime) -> bool:
        """日匹配：日与星期同时约束时用 OR 语义，否则用 AND（其中一个为通配）"""
        dom_ok = dt.day in self.days
        # Python weekday(): 周一=0；cron: 周日=0
        dow_ok = ((dt.weekday() + 1) % 7) in self.weekdays

        if not self._dom_star and not self._dow_star:
            return dom_ok or dow_ok  # OR 语义
        return dom_ok and dow_ok

    def matches(self, dt: datetime) -> bool:
        """判断给定时间（本地时区）是否匹配"""
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._day_matches(dt)
        )

    # ===== 下一次触发 =====

    def next_fire(self, after: datetime) -> Optional[datetime]:
        """计算 after 之后的下一次触发时间（本地时区，精确到分钟）"""
        # 从下一分钟开始
        candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)

        for _ in range(_MAX_SEARCH_DAYS):
            if candidate.month not in self.months or not self._day_matches(candidate):
                # 跳到下一天 00:00
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue

            if candidate.hour not in self.hours:
                # 跳到下一小时
                candidate = (candidate + timedelta(hours=1)).replace(minute=0)
                continue

            if candidate.minute not in self.minutes:
                candidate += timedelta(minutes=1)
                continue

            return candidate

        return None

    def period_seconds(self, reference: Optional[datetime] = None) -> int:
        """连续两次触发的间隔（秒），用于重复任务的抖动计算"""
        ref = reference or datetime.now()
        first = self.next_fire(ref)
        if first is None:
            return 0
        second = self.next_fire(first)
        if second is None:
            return 0
        return int((second - first).total_seconds())


def parse(expression: str) -> CronSchedule:
    """解析 Cron 表达式，失败抛出 CronParseError"""
    return CronSchedule(expression)
