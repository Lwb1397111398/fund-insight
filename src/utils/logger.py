"""
日志配置模块
提供统一的日志记录方式
"""
import logging
import sys
from pathlib import Path

from src.core.config import config


def setup_logger(name: str = None, level: str = None) -> logging.Logger:
    """
    创建或获取日志记录器
    
    Args:
        name: 日志记录器名称，通常使用 __name__
        level: 日志级别，默认从配置读取
        
    Returns:
        配置好的日志记录器
    """
    logger = logging.getLogger(name)
    
    if logger.handlers:
        return logger
    
    log_level = level or getattr(config, 'LOG_LEVEL', 'INFO')
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    
    return logger


def get_logger(name: str = None) -> logging.Logger:
    """
    获取日志记录器
    
    Args:
        name: 模块名称
        
    Returns:
        日志记录器
    """
    return setup_logger(name)


root_logger = setup_logger()
