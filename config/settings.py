# config/settings.py
import os

from dotenv import load_dotenv

load_dotenv()


class LLMSettings:
    """从 .env / 环境变量读取 LLM 配置"""

    model: str = os.getenv("LLM_MODEL", "deepseek-v4-pro")
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    api_key: str = os.getenv("LLM_API_KEY", "")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    db_path: str = os.getenv("DB_PATH", "data/agent.db")

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "db_path": self.db_path,
        }
