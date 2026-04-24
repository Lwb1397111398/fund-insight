"""
配置管理
"""
import os
import json
from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, List, Optional

load_dotenv()

BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

PERSIST_CONFIG_FILE = DATA_DIR / "llm_config.json"

PERSIST_FIELDS = [
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_LIGHT_MODEL",
    "LLM_STRATEGY",
    "VOLCENGINE_API_KEY",
    "VOLCENGINE_BASE_URL",
    "VOLCENGINE_MODEL",
    "VOLCENGINE_LIGHT_MODEL",
]

DEFAULT_ANALYSIS_RULES = {
    "focus_sectors": [],
    "ignore_sectors": [],
    "ignore_keywords": ["广告", "推广", "合作", "赞助"],
    "min_confidence": 20,
    "max_confidence": 100,
    "default_prediction_period": "1个月"
}

PLATFORM_RULES = {
    "xiaohongshu": {
        "name": "小红书",
        "content_max_length": 2000,
        "ignore_patterns": [
            r"#.*?#",
            r"@[\w]+",
            r"点击链接",
            r"关注我",
            r"点赞收藏"
        ],
        "emoji_handling": "remove",
        "hashtag_prefix": "#"
    },
    "weibo": {
        "name": "微博",
        "content_max_length": 5000,
        "ignore_patterns": [
            r"#.*?#",
            r"@[\w]+",
            r"转发微博",
            r"微博视频"
        ],
        "emoji_handling": "keep",
        "hashtag_prefix": "#"
    },
    "wechat": {
        "name": "微信公众号",
        "content_max_length": 10000,
        "ignore_patterns": [
            r"扫码关注",
            r"点击阅读原文",
            r"长按识别"
        ],
        "emoji_handling": "keep",
        "hashtag_prefix": ""
    },
    "zhihu": {
        "name": "知乎",
        "content_max_length": 8000,
        "ignore_patterns": [
            r"知乎专栏",
            r"赞同.*?评论",
            r"分享知乎"
        ],
        "emoji_handling": "keep",
        "hashtag_prefix": ""
    }
}


class Config:
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-ai/DeepSeek-V3.2")

    LLM_LIGHT_MODEL = os.getenv("LLM_LIGHT_MODEL", "Qwen/Qwen2.5-7B-Instruct")

    LLM_STRATEGY = os.getenv("LLM_STRATEGY", "auto")

    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "siliconflow")

    VOLCENGINE_API_KEY = os.getenv("VOLCENGINE_API_KEY", "")
    VOLCENGINE_BASE_URL = os.getenv("VOLCENGINE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    VOLCENGINE_MODEL = os.getenv("VOLCENGINE_MODEL", "doubao-seed-2-0-pro-260215")
    VOLCENGINE_LIGHT_MODEL = os.getenv("VOLCENGINE_LIGHT_MODEL", "doubao-seed-2-0-lite-260215")

    DATABASE_URL = os.getenv("DATABASE_URL", "")
    DB_PATH = DATA_DIR / "fund_insight.db"

    SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
    SERVER_PORT = int(os.getenv("SERVER_PORT", "8002"))

    CRAWLER_ENABLED = os.getenv("CRAWLER_ENABLED", "false").lower() == "true"
    CRAWLER_REQUEST_DELAY = float(os.getenv("CRAWLER_REQUEST_DELAY", "2.0"))
    MAX_POSTS_PER_FUND = int(os.getenv("MAX_POSTS_PER_FUND", "10"))
    CRAWLER_TIMEOUT = int(os.getenv("CRAWLER_TIMEOUT", "10"))

    FUND_API_TIMEOUT = int(os.getenv("FUND_API_TIMEOUT", "10"))
    FUND_API_MAX_RETRIES = int(os.getenv("FUND_API_MAX_RETRIES", "3"))

    CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "5"))
    CIRCUIT_BREAKER_RECOVERY = int(os.getenv("CIRCUIT_BREAKER_RECOVERY", "60"))

    CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))
    CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))

    RETRY_MAX_COUNT = int(os.getenv("RETRY_MAX_COUNT", "3"))
    RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

    VERIFY_FLAT_THRESHOLD_SHORT = float(os.getenv("VERIFY_FLAT_THRESHOLD_SHORT", "0.5"))
    VERIFY_FLAT_THRESHOLD_MEDIUM = float(os.getenv("VERIFY_FLAT_THRESHOLD_MEDIUM", "1.0"))
    VERIFY_FLAT_THRESHOLD_LONG = float(os.getenv("VERIFY_FLAT_THRESHOLD_LONG", "2.0"))

    VERIFY_MIN_DATA_POINTS = int(os.getenv("VERIFY_MIN_DATA_POINTS", "2"))

    VERIFY_PROCESS_THRESHOLD = float(os.getenv("VERIFY_PROCESS_THRESHOLD", "0.5"))

    _analysis_rules: Dict = None
    _platform_rules: Dict = None

    @classmethod
    def load_persisted_config(cls):
        db_loaded = cls._load_from_database()
        if db_loaded:
            return

        if PERSIST_CONFIG_FILE.exists():
            try:
                with open(PERSIST_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                for field in PERSIST_FIELDS:
                    if field in saved and saved[field]:
                        setattr(cls, field, saved[field])
                print(f"[Config] 已从持久化文件加载配置: {list(saved.keys())}")
            except (json.JSONDecodeError, IOError, OSError) as e:
                print(f"[Config] 加载持久化配置失败: {e}")

    @classmethod
    def _load_from_database(cls) -> bool:
        try:
            from src.models.database import engine, DB_TYPE
            if DB_TYPE != "postgresql":
                return False
            from sqlalchemy import text
            with engine.connect() as conn:
                result = conn.execute(text("SELECT config_key, config_value FROM system_config"))
                rows = result.fetchall()
                if not rows:
                    return False
                for row in rows:
                    key, value = row[0], row[1]
                    if key in PERSIST_FIELDS and value:
                        setattr(cls, key, value)
                print(f"[Config] 已从数据库加载配置: {len(rows)} 项")
                return True
        except Exception as e:
            print(f"[Config] 从数据库加载配置失败: {e}")
            return False

    @classmethod
    def save_persisted_config(cls):
        data = {}
        for field in PERSIST_FIELDS:
            val = getattr(cls, field, None)
            if val is not None:
                data[field] = val

        cls._save_to_database(data)

        try:
            with open(PERSIST_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[Config] 配置已持久化保存")
        except (IOError, OSError) as e:
            print(f"[Config] 保存持久化配置失败: {e}")

    @classmethod
    def _save_to_database(cls, data: dict):
        try:
            from src.models.database import engine, DB_TYPE
            if DB_TYPE != "postgresql":
                return
            from sqlalchemy import text
            with engine.connect() as conn:
                for key, value in data.items():
                    if value is None:
                        continue
                    conn.execute(
                        text("""
                            INSERT INTO system_config (config_key, config_value, updated_at)
                            VALUES (:key, :value, NOW())
                            ON CONFLICT (config_key)
                            DO UPDATE SET config_value = :value, updated_at = NOW()
                        """),
                        {"key": key, "value": str(value)}
                    )
                conn.commit()
            print(f"[Config] 配置已保存到数据库: {len(data)} 项")
        except Exception as e:
            print(f"[Config] 保存配置到数据库失败: {e}")

    @property
    def analysis_rules(self) -> Dict:
        if self._analysis_rules is None:
            rules_file = DATA_DIR / "analysis_rules.json"
            if rules_file.exists():
                try:
                    with open(rules_file, 'r', encoding='utf-8') as f:
                        custom_rules = json.load(f)
                        self._analysis_rules = {**DEFAULT_ANALYSIS_RULES, **custom_rules}
                except (json.JSONDecodeError, IOError, OSError) as e:
                    print(f"[Config] 加载分析规则失败: {e}, 使用默认规则")
                    self._analysis_rules = DEFAULT_ANALYSIS_RULES.copy()
            else:
                self._analysis_rules = DEFAULT_ANALYSIS_RULES.copy()
        return self._analysis_rules

    @property
    def platform_rules(self) -> Dict:
        if self._platform_rules is None:
            rules_file = DATA_DIR / "platform_rules.json"
            if rules_file.exists():
                try:
                    with open(rules_file, 'r', encoding='utf-8') as f:
                        custom_rules = json.load(f)
                        self._platform_rules = {**PLATFORM_RULES, **custom_rules}
                except (json.JSONDecodeError, IOError, OSError) as e:
                    print(f"[Config] 加载平台规则失败: {e}, 使用默认规则")
                    self._platform_rules = PLATFORM_RULES.copy()
            else:
                self._platform_rules = PLATFORM_RULES.copy()
        return self._platform_rules

    def get_platform_rule(self, platform: str) -> Dict:
        return self.platform_rules.get(platform, self.platform_rules.get("xiaohongshu", {}))

    def update_analysis_rules(self, rules: Dict):
        self._analysis_rules = {**DEFAULT_ANALYSIS_RULES, **rules}
        rules_file = DATA_DIR / "analysis_rules.json"
        with open(rules_file, 'w', encoding='utf-8') as f:
            json.dump(self._analysis_rules, f, ensure_ascii=False, indent=2)

    def reload_rules(self):
        self._analysis_rules = None
        self._platform_rules = None


config = Config()
config.load_persisted_config()
