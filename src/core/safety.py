"""运行时安全开关。"""
import os


def destructive_cleanup_enabled() -> bool:
    """只有显式开启的隔离维护环境才允许批量硬删除数据。"""
    return os.getenv("ENABLE_DATA_CLEANUP", "false").lower() == "true"
