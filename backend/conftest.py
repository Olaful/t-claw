"""pytest 配置：确保 app 模块可被正确导入"""
import sys
from pathlib import Path

# 将 backend 目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))
