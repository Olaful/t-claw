"""Cron 解析器单元测试

覆盖：表达式解析、匹配、next_fire、日/周 OR 语义、非法表达式拒绝。
运行：在 backend 目录下执行 `python -m pytest tests/test_cron_parser.py -v`
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from app.core.scheduler.cron import CronParseError, CronSchedule, parse  # noqa: E402


# ===== 解析 =====

def test_parse_star_all():
    s = parse("* * * * *")
    assert s.minutes == set(range(60))
    assert s.hours == set(range(24))
    assert s.days == set(range(1, 32))
    assert s.months == set(range(1, 13))
    assert s.weekdays == set(range(7))


def test_parse_single_values():
    s = parse("0 9 * * *")
    assert s.minutes == {0}
    assert s.hours == {9}


def test_parse_step():
    s = parse("*/15 * * * *")
    assert s.minutes == {0, 15, 30, 45}


def test_parse_range():
    s = parse("0 9-17 * * *")
    assert s.hours == set(range(9, 18))


def test_parse_range_with_step():
    s = parse("0 0 * * 1-5/2")
    assert s.weekdays == {1, 3, 5}


def test_parse_list():
    s = parse("5,20,40 * * * *")
    assert s.minutes == {5, 20, 40}


def test_parse_combined_list():
    s = parse("0-5,10 */2 * * *")
    assert s.minutes == {0, 1, 2, 3, 4, 5, 10}
    assert s.hours == set(range(0, 24, 2))


def test_weekday_7_normalized_to_0():
    s = parse("0 0 * * 7")
    assert s.weekdays == {0}


# ===== 非法表达式 =====

@pytest.mark.parametrize("expr", [
    "* * * *",          # 4 字段
    "* * * * * *",      # 6 字段
    "60 * * * *",       # 分钟越界
    "* 24 * * *",       # 小时越界
    "* * 0 * *",        # 日最小为 1
    "* * 32 * *",       # 日越界
    "* * * 13 *",       # 月越界
    "* * * * 8",        # 星期越界
    "0-5-10 * * * *",   # 非法范围
    "*/0 * * * *",      # 步长为 0
    "10-5 * * * *",     # 范围倒置
    "a * * * *",        # 非数字
    "* * * * L",        # 不支持 L
    "* * W * *",        # 不支持 W
    "* * ? * *",        # 不支持 ?
    "5, * * * *",       # 空列表项
    "",                 # 空表达式
])
def test_invalid_expression(expr):
    with pytest.raises(CronParseError):
        parse(expr)


# ===== 匹配 =====

def test_matches_every_minute():
    s = parse("* * * * *")
    assert s.matches(datetime(2025, 6, 15, 12, 34))


def test_matches_daily_at_9():
    s = parse("0 9 * * *")
    assert s.matches(datetime(2025, 6, 15, 9, 0))
    assert not s.matches(datetime(2025, 6, 15, 9, 1))
    assert not s.matches(datetime(2025, 6, 15, 10, 0))


def test_matches_weekday():
    # 2025-06-16 是周一（cron 星期=1）
    s = parse("0 9 * * 1")
    assert s.matches(datetime(2025, 6, 16, 9, 0))
    # 2025-06-15 是周日（cron 星期=0）
    assert not s.matches(datetime(2025, 6, 15, 9, 0))


def test_matches_sunday_as_0_and_7():
    sunday = datetime(2025, 6, 15, 9, 0)  # 周日
    assert parse("0 9 * * 0").matches(sunday)
    assert parse("0 9 * * 7").matches(sunday)


# ===== OR 语义（日与星期同时约束） =====

def test_or_semantics_dom_or_dow():
    # 每月 13 号 或 每周五
    s = parse("0 0 13 * 5")
    assert s.matches(datetime(2025, 6, 13, 0, 0))      # 13号(周五) ✔
    assert s.matches(datetime(2025, 6, 20, 0, 0))      # 周五 ✔
    assert s.matches(datetime(2025, 7, 13, 0, 0))      # 13号(周日) ✔
    assert not s.matches(datetime(2025, 6, 14, 0, 0))  # 非13号且非周五 ✘


def test_star_dow_is_and_semantics():
    # 只有星期字段约束 → 仅周五（2025-06-13 与 2025-06-20 均为周五，2025-06-14 为周六）
    s = parse("0 0 * * 5")
    assert s.matches(datetime(2025, 6, 20, 0, 0))      # 周五
    assert s.matches(datetime(2025, 6, 13, 0, 0))      # 13号也是周五 → 匹配星期约束
    assert not s.matches(datetime(2025, 6, 14, 0, 0))  # 周六


def test_star_dom_is_and_semantics():
    # 只有日字段约束 → 每月 13 号
    s = parse("0 0 13 * *")
    assert s.matches(datetime(2025, 6, 13, 0, 0))
    assert not s.matches(datetime(2025, 6, 20, 0, 0))


# ===== next_fire =====

def test_next_fire_next_minute():
    s = parse("* * * * *")
    after = datetime(2025, 6, 15, 12, 34, 20)
    assert s.next_fire(after) == datetime(2025, 6, 15, 12, 35)


def test_next_fire_daily():
    s = parse("0 9 * * *")
    # 9 点前 → 当天 9 点
    assert s.next_fire(datetime(2025, 6, 15, 8, 0)) == datetime(2025, 6, 15, 9, 0)
    # 9 点后 → 次日 9 点
    assert s.next_fire(datetime(2025, 6, 15, 9, 0)) == datetime(2025, 6, 16, 9, 0)


def test_next_fire_specific_weekday():
    # 每周一 9 点；2025-06-15 是周日 → 下一个周一 2025-06-16
    s = parse("0 9 * * 1")
    assert s.next_fire(datetime(2025, 6, 15, 10, 0)) == datetime(2025, 6, 16, 9, 0)


def test_next_fire_specific_month():
    # 每年 2 月 29 日 0 点（闰年）：2025 非闰年 → 2028-02-29
    s = parse("0 0 29 2 *")
    assert s.next_fire(datetime(2025, 6, 15, 0, 0)) == datetime(2028, 2, 29, 0, 0)


def test_next_fire_or_semantics():
    # 每月 13 号或每周五 0 点；2025-06-14(周六) 之后 → 06-20(周五) 早于 07-13
    s = parse("0 0 13 * 5")
    assert s.next_fire(datetime(2025, 6, 14, 0, 0)) == datetime(2025, 6, 20, 0, 0)


def test_next_fire_returns_none_for_impossible():
    # 2 月 30 日不存在
    s = parse("0 0 30 2 *")
    assert s.next_fire(datetime(2025, 1, 1, 0, 0)) is None


# ===== period_seconds =====

def test_period_every_minute():
    assert parse("* * * * *").period_seconds(datetime(2025, 6, 15, 12, 0)) == 60


def test_period_every_15_minutes():
    assert parse("*/15 * * * *").period_seconds(datetime(2025, 6, 15, 12, 7)) == 900


def test_period_daily():
    assert parse("0 9 * * *").period_seconds(datetime(2025, 6, 15, 10, 0)) == 86400


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
