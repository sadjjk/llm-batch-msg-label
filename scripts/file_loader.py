"""对话文件加载模块：统一加载 + 预处理 + 时间转换 + 去 HTML"""

from datetime import datetime, timezone, timedelta
from scripts.logger import Logger, log_errors
from html.parser import HTMLParser
from html import unescape
from pathlib import Path
import pandas as pd
import json


class HTMLStripper(HTMLParser):
    """精细去 HTML：保留文字、br/p/div 转换行、处理 entity、跳过 script/style/img"""

    SKIP_TAGS = {"script", "style", "img", "input", "meta", "link"}
    BLOCK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.result = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.result.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.result.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.result.append(data)

    def handle_entityref(self, name):
        if self._skip_depth == 0:
            self.result.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        if self._skip_depth == 0:
            self.result.append(unescape(f"&#{name};"))

    def get_text(self) -> str:
        return "".join(self.result).strip()


class FileLoader:
    """对话文件加载器，支持 parquet/csv/excel/json"""

    CST = timezone(timedelta(hours=8))

    def __init__(self, data_file: str, primary_key: str, message_column: str,
                 message_time_format: str, message_time_sep: str, message_multi_sep: str):
        self.data_file = data_file
        self.primary_key = primary_key
        self.message_column = message_column
        self.message_time_format = message_time_format
        self.message_time_sep = message_time_sep
        self.message_multi_sep = message_multi_sep

        self.log = Logger()
        self._df = self._load_file()

    @log_errors
    def _load_file(self) -> pd.DataFrame:
        """根据扩展名加载文件"""
        ext = Path(self.data_file).suffix.lower()
        if ext == ".parquet":
            return pd.read_parquet(self.data_file)
        elif ext == ".csv":
            return pd.read_csv(self.data_file)
        elif ext in (".xlsx", ".xls"):
            return pd.read_excel(self.data_file)
        elif ext == ".json":
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return pd.DataFrame(data)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

    @property
    def total_rows(self) -> int:
        return len(self._df)

    @property
    def columns(self) -> list[str]:
        return self._df.columns.tolist()

    def _convert_time(self, time_str: str) -> str:
        """根据 message_time_format 转换时间为 HH:MM:SS"""
        if self.message_time_format == "raw":
            return time_str
        try:
            if self.message_time_format == "timestamp_ms":
                dt = datetime.fromtimestamp(int(time_str) / 1000, tz=FileLoader.CST)
            elif self.message_time_format == "timestamp_s":
                dt = datetime.fromtimestamp(int(time_str), tz=FileLoader.CST)
            elif self.message_time_format == "yyyymmddhhmmss":
                dt = datetime.strptime(time_str, "%Y%m%d%H%M%S").replace(tzinfo=FileLoader.CST)
            elif self.message_time_format == "iso8601":
                dt = datetime.fromisoformat(time_str)
            else:
                return time_str
            return dt.strftime("%H:%M:%S")
        except (ValueError, OSError):
            return time_str

    @staticmethod
    def _strip_html(text: str) -> str:
        """精细去 HTML 标签"""
        stripper = HTMLStripper()
        stripper.feed(text)
        return stripper.get_text()

    def _process_message(self, raw: str) -> list[dict]:
        """预处理单条对话：时间转换 + 去 HTML，返回句子列表

        Returns:
            [{"time": "HH:MM:SS", "text": "内容"}, ...]
            message_time_format=none 时 time=""
        """
        if not raw or not isinstance(raw, str):
            return []

        messages = raw.split(self.message_multi_sep)
        sentences = []

        for msg in messages:
            msg = msg.strip()
            if not msg:
                continue

            # 无时间信息模式：整条消息当文本，不拆时间
            if self.message_time_format == "none":
                text = self._strip_html(msg)
                if text.strip():
                    sentences.append({"time": "", "text": text})
                continue

            sep_idx = msg.find(self.message_time_sep)
            if sep_idx == -1:
                # 无时间戳，去 HTML 保留原文
                text = self._strip_html(msg)
                if text.strip():
                    sentences.append({"time": "", "text": text})
                continue

            ts_part = msg[:sep_idx]
            text_part = msg[sep_idx + len(self.message_time_sep):]

            time_str = self._convert_time(ts_part)
            text = self._strip_html(text_part)

            if text.strip():
                sentences.append({"time": time_str, "text": text})

        return sentences

    def iter_batches(self, batch_size: int):
        """分批读取，返回统一格式

        Yields:
            list[dict]: [{"primary_key": ..., "sentences": [...]}, ...]
        """
        for start in range(0, len(self._df), batch_size):
            batch_df = self._df.iloc[start:start + batch_size]
            results = []

            for _, row in batch_df.iterrows():
                pk = str(row.get(self.primary_key, ""))
                raw_msg = str(row.get(self.message_column, ""))
                sentences = self._process_message(raw_msg)

                results.append({
                    "pk": pk,
                    "sentences": sentences,
                })

            yield results
