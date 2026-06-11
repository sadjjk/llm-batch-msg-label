"""进度管理模块：文件级进度 + Label 级进度 + 结果写入"""

from datetime import datetime, timezone, timedelta
from scripts.logger import Logger, log_errors
from typing import Optional
from pathlib import Path
import json
import os
import shutil
import threading



class ProgressWriter:
    """文件级进度管理（绑定单个 data_file）"""

    def __init__(self, output_dir: str, data_file: str, result_dir: str = ""):
        self.output_dir = os.path.abspath(output_dir)
        self.result_dir = os.path.abspath(result_dir) if result_dir else self.output_dir
        self._data_file = data_file
        self._file_key = self.file_key(data_file)
        self.log = Logger()
        self._lock = threading.Lock()
        os.makedirs(self.output_dir, exist_ok=True)

    @property
    def progress_path(self) -> str:
        return os.path.join(self.output_dir, "progress.json")

    @staticmethod
    def file_key(data_file: str) -> str:
        """生成文件标识：{stem}_{size}"""
        size = os.path.getsize(data_file)
        stem = Path(data_file).stem
        return f"{stem}_{size}"

    # 进度读写

    def load_progress(self) -> dict:
        """读取全局 progress"""
        if os.path.exists(self.progress_path):
            with open(self.progress_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_progress(self, progress: dict):
        """保存全局 progress（调用方需持有 _lock）"""
        with open(self.progress_path, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)

    # 文件级进度

    def get_file_progress(self) -> Optional[dict]:
        """查询文件级进度"""
        progress = self.load_progress()
        return progress.get(self._file_key)

    def init_progress(self, total_rows: int, config_snapshot: dict, db_meta: dict = {}):
        """初始化文件级进度（仅新文件时创建记录）"""
        with self._lock:
            progress = self.load_progress()

            if self._file_key in progress:
                return

            now = datetime.now(timezone(timedelta(hours=8))).isoformat()
            record = {
                "data_file": self._data_file,
                "result_dir": os.path.join(self.result_dir, self._file_key),
                "file_size": os.path.getsize(self._data_file),
                "total_rows": total_rows,
                "status": "running",
                "config_snapshot": config_snapshot,
                "started_at": now,
                "updated_at": now,
                "labels": {},
            }
            if db_meta:
                record["db_source"] = db_meta
            progress[self._file_key] = record
            self.save_progress(progress)

    def abort_running_labels(self):
        """将所有 running 状态的 label 标记为 aborted"""
        with self._lock:
            progress = self.load_progress()
            if self._file_key in progress:
                now = datetime.now(timezone(timedelta(hours=8))).isoformat()
                for name, lp in progress[self._file_key].get("labels", {}).items():
                    if lp.get("status") == "running":
                        lp["status"] = "aborted"
                        lp["updated_at"] = now
                self.save_progress(progress)

    def update_file_progress(self, **kwargs):
        """更新文件级字段"""
        with self._lock:
            progress = self.load_progress()
            if self._file_key in progress:
                progress[self._file_key].update(kwargs)
                now = datetime.now(timezone(timedelta(hours=8))).isoformat()
                progress[self._file_key]["updated_at"] = now
                if kwargs.get("status") == "completed":
                    progress[self._file_key]["completed_at"] = now
                self.save_progress(progress)

    def save_duration(self, duration: str):
        """写入文件级耗时"""
        with self._lock:
            progress = self.load_progress()
            if self._file_key in progress:
                progress[self._file_key]["duration"] = duration
                self.save_progress(progress)

    # 清理

    @log_errors
    def clean_file(self):
        """清空该文件所有结果和进度"""
        with self._lock:
            progress = self.load_progress()

            if self._file_key in progress:
                file_d = os.path.join(self.result_dir, self._file_key)
                if os.path.isdir(file_d):
                    deleted = []
                    for f in Path(file_d).rglob("*"):
                        if f.is_file():
                            deleted.append(str(f.relative_to(file_d)))
                    if deleted:
                        self.log.info(f"Deleting {self._file_key}:")
                        for f in sorted(deleted):
                            self.log.info(f"  - {f}")
                    shutil.rmtree(file_d)
                # 删除合并结果文件
                merged = progress[self._file_key].get("result_file")
                if merged and os.path.isfile(merged):
                    self.log.info(f"  - {os.path.basename(merged)}  (合并结果)")
                    os.remove(merged)
                del progress[self._file_key]
                self.save_progress(progress)

    # 工厂方法

    def for_label(self, label_file_name: str) -> "LabelProgressWriter":
        """创建 Label 级进度管理器"""
        return LabelProgressWriter(self, label_file_name)


class LabelProgressWriter:
    """Label 级进度 + 结果写入（持有 ProgressWriter 引用，共享 _lock）"""

    def __init__(self, pw: ProgressWriter, label_file_name: str):
        self._pw = pw
        self._label_file_name = label_file_name

    # 路径

    def label_dir(self) -> str:
        """结果目录：output/{file_key}/{label_file_name}/"""
        d = os.path.join(self._pw.output_dir, self._pw._file_key, self._label_file_name)
        os.makedirs(d, exist_ok=True)
        return d

    def result_path(self) -> str:
        return os.path.join(self.label_dir(), "result.jsonl")

    def parse_errors_path(self) -> str:
        return os.path.join(self.label_dir(), "parse_errors.jsonl")

    def parse_warnings_path(self) -> str:
        return os.path.join(self.label_dir(), "parse_warnings.jsonl")

    # 进度查询

    def get_status(self) -> Optional[str]:
        """查询 label 状态"""
        file_prog = self._pw.get_file_progress()
        if not file_prog:
            return None
        label_prog = file_prog.get("labels", {}).get(self._label_file_name)
        return label_prog.get("status") if label_prog else None

    def get_batch_count(self) -> int:
        """查询 label 已完成的 batch 数"""
        file_prog = self._pw.get_file_progress()
        if not file_prog:
            return 0
        return file_prog.get("labels", {}).get(self._label_file_name, {}).get("batch_count", 0)

    # 进度更新

    def init_progress(self) -> int:
        """初始化 label 进度，返回 start_offset"""
        with self._pw._lock:
            progress = self._pw.load_progress()
            now = datetime.now(timezone(timedelta(hours=8))).isoformat()

            if self._pw._file_key not in progress:
                return 0

            labels = progress[self._pw._file_key].setdefault("labels", {})
            if self._label_file_name not in labels:
                labels[self._label_file_name] = {
                    "result_file": os.path.join(self._pw.output_dir, self._pw._file_key, self._label_file_name, "result.jsonl"),
                    "offset": 0,
                    "batch_count": 0,
                    "status": "pending",
                    "labeled_count": 0,
                    "started_at": now,
                    "updated_at": now,
                }
                self._pw.save_progress(progress)
                return 0

            label_prog = labels[self._label_file_name]
            if label_prog.get("status") == "completed":
                return -1

            offset = label_prog.get("offset", 0)
            if offset > 0:
                self._pw.log.info(f"Resuming {self._label_file_name} from offset {offset}")
            return offset

    def update_progress(self, **kwargs):
        """更新 label 级进度"""
        with self._pw._lock:
            progress = self._pw.load_progress()
            if self._pw._file_key in progress:
                labels = progress[self._pw._file_key].setdefault("labels", {})
                if self._label_file_name not in labels:
                    labels[self._label_file_name] = {}
                labels[self._label_file_name].update(kwargs)
                now = datetime.now(timezone(timedelta(hours=8))).isoformat()
                labels[self._label_file_name]["updated_at"] = now
                if kwargs.get("status") == "completed":
                    labels[self._label_file_name]["completed_at"] = now
                self._pw.save_progress(progress)

    def save_label_hits(self, label_hits: dict):
        """写入 label_hits（各标签值命中数），label 跑完时调用一次"""
        with self._pw._lock:
            progress = self._pw.load_progress()
            if self._pw._file_key in progress:
                label_prog = progress[self._pw._file_key].get("labels", {}).get(self._label_file_name, {})
                label_prog["label_hits"] = label_hits
                self._pw.save_progress(progress)

    def save_duration(self, duration: str):
        """写入 label 耗时"""
        with self._pw._lock:
            progress = self._pw.load_progress()
            if self._pw._file_key in progress:
                label_prog = progress[self._pw._file_key].get("labels", {}).get(self._label_file_name, {})
                label_prog["duration"] = duration
                self._pw.save_progress(progress)

    # 结果写入

    @log_errors
    def append_result(self, results: list[dict]):
        """追加写入 result.jsonl"""
        path = self.result_path()
        with open(path, "a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with self._pw._lock:
            progress = self._pw.load_progress()
            if self._pw._file_key in progress:
                label_prog = progress[self._pw._file_key].get("labels", {}).get(self._label_file_name, {})
                label_prog["labeled_count"] = label_prog.get("labeled_count", 0) + len(results)
                self._pw.save_progress(progress)

    @log_errors
    def append_parse_error(self, row: int, batch_count: int, sub: int,
                           conv: int, token_id: str, error_msg: str,
                           dialogue: list, prompt: str,
                           raw_response: Optional[str] = None):
        """追加写入 parse_errors.jsonl（有 token_id，可定位可重跑）"""
        path = self.parse_errors_path()
        record = {
            "row": row,
            "batch_count": batch_count,
            "sub": sub,
            "conv": conv,
            "token_id": token_id,
            "error": error_msg,
            "dialogue": dialogue,
            "prompt": prompt,
        }
        if raw_response:
            record["raw_response"] = raw_response
        record["timestamp"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self._pw._lock:
            progress = self._pw.load_progress()
            if self._pw._file_key in progress:
                label_prog = progress[self._pw._file_key].get("labels", {}).get(self._label_file_name, {})
                label_prog["parse_error_count"] = label_prog.get("parse_error_count", 0) + 1
                if batch_count is not None:
                    label_prog.setdefault("parse_error_batches", []).append(batch_count)
                label_prog.setdefault("parse_error_file", self.parse_errors_path())
                self._pw.save_progress(progress)

    @log_errors
    def append_parse_warning(self, batch_count: int, sub: int,
                             conv, warning_msg: str,
                             raw_response: Optional[str] = None):
        """追加写入 parse_warnings.jsonl（无 token_id，仅告警）"""
        path = self.parse_warnings_path()
        record = {
            "batch_count": batch_count,
            "sub": sub,
            "conv": conv,
            "warning": warning_msg,
        }
        if raw_response:
            record["raw_response"] = raw_response
        record["timestamp"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 复合操作

    def warning(self, warnings: list[dict], batch_count: int):
        """批量写入 parse warning/error，按 token_id 有无分流"""
        for w in warnings:
            if w.get("token_id"):
                self.append_parse_error(
                    row=w["row"], batch_count=batch_count, sub=w["sub"],
                    conv=w["conv"], token_id=w["token_id"],
                    error_msg=w["error"], dialogue=w["dialogue"],
                    prompt=w["prompt"], raw_response=w.get("raw_response"))
            else:
                self.append_parse_warning(
                    batch_count=batch_count, sub=w["sub"],
                    conv=w.get("conv"), warning_msg=w["error"],
                    raw_response=w.get("raw_response"))

    def abort(self, offset: int, error_msg: str,
              batch_count: Optional[int] = None, sub: Optional[int] = None):
        """标签级失败：记录错误 + 标记 label 和文件均为 aborted"""
        self.append_parse_error(
            row=offset, batch_count=batch_count or 0, sub=sub or 0,
            conv=0, token_id="", error_msg=error_msg,
            dialogue=[], prompt="")
        self.update_progress(status="aborted")
        self._pw.update_file_progress(status="aborted")
