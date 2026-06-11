"""LLM API 调用模块：请求、重试、限流"""

from scripts.logger import Logger, log_errors
from typing import Optional
import requests
import time
import random


class LLMClient:
    """LLM API 客户端"""

    def __init__(self, base_url: str, api_key: str, model_id: str,
                 model_timeout: int = 120, max_retries: int = 3, max_tokens: int = 4096):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.model_timeout = model_timeout
        self.log = Logger()
        self.max_retries = max_retries
        self.max_tokens = max_tokens

    @classmethod
    def from_config(cls, config) -> "LLMClient":
        """从 Config 对象创建"""
        return cls(
            base_url=config.base_url,
            api_key=config.api_key,
            model_id=config.model_id,
            model_timeout=config.model_timeout,
            max_retries=config.max_retries,
            max_tokens=config.max_tokens,
        )

    @log_errors
    def call(self, prompt: str) -> Optional[str]:
        """调 LLM API，指数退避重试，返回响应文本或 None"""
        url = f"{self.base_url}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "disabled"},
        }

        for attempt in range(self.max_retries):
            try:
                time.sleep(random.uniform(0, 3))
                self.log.info(f"LLM请求: POST {url} (model={self.model_id}, attempt={attempt + 1}/{self.max_retries})")
                resp = requests.post(url, headers=headers, json=payload, timeout=self.model_timeout)
                self.log.info(f"LLM响应: {resp.status_code} ({len(resp.content)} bytes, {resp.elapsed.total_seconds():.1f}s)")

                if resp.status_code == 429:
                    wait = 2 ** attempt * 5
                    self.log.warning(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    self.log.error(f"API error: {resp.status_code} {resp.text[:200]}")
                    if attempt < self.max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None

                data = resp.json()
                return data["choices"][0]["message"]["content"]

            except requests.exceptions.Timeout:
                self.log.warning(f"Timeout (attempt {attempt + 1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except Exception as e:
                self.log.error(f"Request error: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None

        return None
