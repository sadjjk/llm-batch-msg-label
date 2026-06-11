"""日志模块 — 按天滚动，保留 30 天，错误单独记录，LLM 详细 IO 按日期目录存储"""

from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timezone, timedelta
import functools
import logging
import shutil
import os

class Logger:
    """应用日志类，单例模式，按天滚动，保留 30 天"""

    CST = timezone(timedelta(hours=8))

    _instances = {}

    def __new__(cls, name: str = "logging", log_dir: str = ""):
        if name in cls._instances:
            return cls._instances[name]
        instance = super().__new__(cls)
        instance._init(name, log_dir)
        cls._instances[name] = instance
        return instance

    def _init(self, name: str, log_dir: str):
        if not log_dir:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
        self.log_dir = os.path.abspath(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )

            # 全量日志 app.log
            app_handler = TimedRotatingFileHandler(
                os.path.join(self.log_dir, f"{name}.log"),
                when="midnight", interval=1, backupCount=30, encoding="utf-8"
            )
            app_handler.suffix = "%Y-%m-%d"
            app_handler.setLevel(logging.INFO)
            app_handler.setFormatter(formatter)
            self.logger.addHandler(app_handler)

            # 错误日志 app.err.log
            err_handler = TimedRotatingFileHandler(
                os.path.join(self.log_dir, f"{name}.err.log"),
                when="midnight", interval=1, backupCount=30, encoding="utf-8"
            )
            err_handler.suffix = "%Y-%m-%d"
            err_handler.setLevel(logging.ERROR)
            err_handler.setFormatter(formatter)
            self.logger.addHandler(err_handler)

            # 控制台
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            console.setFormatter(formatter)
            self.logger.addHandler(console)

    def info(self, msg: str):
        self.logger.info(msg)

    def warning(self, msg: str):
        self.logger.warning(msg)

    def error(self, msg: str):
        self.logger.error(msg)

    # ─── LLM 详细 IO ───

    @staticmethod
    def fmt_duration(seconds: float) -> str:
        """格式化耗时：<60s 用 XX.Xs，≥60s 用 XXmXXs"""
        if seconds >= 60:
            return f"{int(seconds // 60)}m{seconds % 60:.0f}s"
        return f"{seconds:.1f}s"

    def save_llm_detail(self, file_key: str, data_file: str, label_file_name: str,
                        batch_count: int, offset: int, sub_results: list, config):
        """保存 LLM 完整 IO 到 llm_detail/{日期}/ 目录（txt 格式，支持多分片）"""
        model_id = config.model_id
        base_url = config.base_url
        batch_size_split = config.batch_size_split
        started_at = sub_results[0][3] if sub_results else 0
        finished_at = sub_results[-1][4] if sub_results else 0
        duration_s = finished_at - started_at
        today = datetime.now(self.CST).strftime("%Y-%m-%d")
        detail_dir = os.path.join(self.log_dir, "llm_detail", today)
        os.makedirs(detail_dir, exist_ok=True)

        filename = f"{datetime.now(self.CST).strftime('%Y%m%d_%H%M%S')}_{file_key}_{label_file_name}_batch_{batch_count:03d}.txt"
        filepath = os.path.join(detail_dir, filename)

        tz = timezone(timedelta(hours=8))

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("========== META ==========\n")
            f.write(f"file_key: {file_key}\n")
            f.write(f"label: {label_file_name}\n")
            f.write(f"model_id: {model_id}\n")
            f.write(f"base_url: {base_url}\n")
            f.write(f"batch_count: {batch_count}\n")
            f.write(f"offset: {offset}\n")
            f.write(f"split: {batch_size_split}\n")
            f.write(f"data_file: {data_file}\n")
            f.write(f"started_at: {datetime.fromtimestamp(started_at, tz=tz).isoformat()}\n")
            f.write(f"finished_at: {datetime.fromtimestamp(finished_at, tz=tz).isoformat()}\n")
            f.write(f"duration: {self.fmt_duration(duration_s)}\n")

            # 多分片格式
            f.write("\n")
            for i, (resp, dm, prompt, t0, t1) in enumerate(sub_results, 1):
                sub_duration = t1 - t0
                f.write(f"========== SUB {i} ==========\n")
                f.write(f"started_at: {datetime.fromtimestamp(t0, tz=tz).isoformat()}\n")
                f.write(f"finished_at: {datetime.fromtimestamp(t1, tz=tz).isoformat()}\n")
                f.write(f"duration: {self.fmt_duration(sub_duration)}\n")
                f.write(f"prompt_tokens: ~{len(prompt) // 4}\n")
                f.write(f"response_length: {len(resp) if resp else 0}\n")
                f.write(f"\n========== PROMPT {i} ==========\n")
                f.write(prompt)
                f.write(f"\n\n========== RESPONSE {i} ==========\n")
                f.write(resp or "")
                f.write("\n\n")

    def cleanup_llm_detail(self, keep_days: int = 30):
        """清理超过 keep_days 天的 llm_detail 子目录"""
        detail_dir = os.path.join(self.log_dir, "llm_detail")
        if not os.path.exists(detail_dir):
            return

        now = datetime.now(self.CST)
        for entry in os.listdir(detail_dir):
            entry_path = os.path.join(detail_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            try:
                entry_date = datetime.strptime(entry, "%Y-%m-%d").replace(tzinfo=self.CST)
                if (now - entry_date).days > keep_days:
                    shutil.rmtree(entry_path)
                    self.info(f"Cleaned up old llm_detail: {entry}")
            except ValueError:
                continue


def log_errors(func):
    """装饰器：异常时记录日志再抛出"""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            if hasattr(self, 'log') and self.log:
                self.log.error(f"❌ {func.__name__} 失败: {e}")
            raise
    return wrapper
