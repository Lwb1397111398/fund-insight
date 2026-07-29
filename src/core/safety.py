"""运行时安全开关。"""
import os


def destructive_cleanup_enabled() -> bool:
    """批量硬删除开关。

    默认开启：保留策略已保护待验证/长期预测净值与博主归档准确率。
    仅当显式设置 ENABLE_DATA_CLEANUP=false 时关闭。
    执行时仍需 X-Danger-Confirm 确认头。
    """
    return os.getenv("ENABLE_DATA_CLEANUP", "true").lower() == "true"
