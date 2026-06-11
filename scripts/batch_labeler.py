"""批量打标器：串联配置、数据加载、LLM 调用、结果写入"""

from scripts.prompt_builder import PromptBuilder, LabelParser, ResponseParser
from scripts.progress_writer import ProgressWriter
from scripts.file_loader import FileLoader
from scripts.llm_client import LLMClient
from scripts.logger import Logger
from scripts.config import Config
from scripts.notifier import Notifier
from typing import Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import time
import pandas as pd


class BatchLabeler:
    """批量打标器"""

    def __init__(self, config: Config, data_file: str, force_run: bool, db_meta: dict = {}):
        self.config = config
        self.data_file = data_file
        self.force_run = force_run
        self.db_meta = db_meta or {}
        self.log = Logger(log_dir=config.log_dir)
        self.prompt_builder = PromptBuilder.from_file(config.prompt_template_path)
        self.llm_client = LLMClient.from_config(config)
        self.writer = ProgressWriter(config.output_dir, data_file, config.result_dir)

    @staticmethod
    def _count_label_hits(result_path: str) -> dict:
        """统计 result.jsonl 中各标签值的命中数"""
        from collections import Counter
        hits = Counter()
        if os.path.exists(result_path):
            with open(result_path, "r", encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    hits[r["label_value"]] += 1
        return dict(hits)

    # 检查进度 + 过滤待处理 label
    def resolve_pending_labels(self):
        """检查进度 + 过滤待处理 label

        返回:
            list[dict] — 待处理 label 列表
            [] — 全部完成，无需处理
            None — 配置变更，需调用方决定退出
        """
        file_prog = self.writer.get_file_progress()

        if self.force_run:
            if file_prog:
                self.log.info(f"Force run: clearing existing results for {Path(self.data_file).name}")
                self.writer.clean_file()
            return self.config.label_files

        if not file_prog:
            return self.config.label_files

        # dry_run 模式跳过配置变更检查
        if self.config.dry_run:
            return self.config.label_files

        # 配置变更
        saved_snapshot = file_prog.get("config_snapshot", {})
        current_snapshot = self.config.config_snapshot
        if saved_snapshot != current_snapshot:
            self.log.warning("配置已变更，想要重新生成 建议 --force_run")
            self.log.warning(f"  旧: {json.dumps(saved_snapshot, ensure_ascii=False)}")
            self.log.warning(f"  新: {json.dumps(current_snapshot, ensure_ascii=False)}")
            return None

        # 全部完成
        if file_prog.get("status") == "completed":
            self.log.info(f"已有上次完成的打标结果: {file_prog['result_dir']}，如需重跑建议 --force_run")
            return []

        # 过滤待处理 label
        labels_prog = file_prog.get("labels", {})
        return [
            t for t in self.config.label_files
            if not (labels_prog.get(Path(t["path"]).stem, {}).get("status") == "completed")
        ]

    # 分片并发调用 LLM 并合并解析
    def _batch_call_and_parse(self, batch, labels_text, match_rule,
                              primary_key_name, batch_count,
                              current_offset, label_file_name="", label_names=None) -> tuple[list[dict], list[tuple], list[dict], Optional[str], Optional[int]]:
        """分片并发调用 LLM 并合并解析结果

        返回: (all_results, sub_results, sub_warnings, error_msg, error_sub)
            sub_warnings: [enriched_warning_dict, ...]
            error_msg 非 None 表示有 sub_batch 调用失败
            error_sub: LLM 调用失败的 sub 序号
        """
        batch_size_split = self.config.batch_size_split
        sub_size = len(batch) // batch_size_split
        sub_batches = [batch[i*sub_size:(i+1)*sub_size] for i in range(batch_size_split - 1)]
        sub_batches.append(batch[(batch_size_split-1)*sub_size:])

        def call_one(sub_batch):
            dt = PromptBuilder.format_dialogues(sub_batch)
            dm = PromptBuilder.build_dialogue_map(sub_batch)
            p = self.prompt_builder.build(labels_text, dt, match_rule)
            t0 = time.time()
            r = self.llm_client.call(p)
            t1 = time.time()
            return r, dm, p, t0, t1

        with ThreadPoolExecutor(max_workers=batch_size_split) as pool:
            futures = [pool.submit(call_one, sb) for sb in sub_batches]
            sub_results = [f.result() for f in futures]

        all_results = []
        sub_warnings = []
        error_msg = None
        error_sub = None
        for i, (resp, dm, p, t0, t1) in enumerate(sub_results):
            if resp is None:
                self.log.error(f"[{current_offset}] batch {batch_count} sub {i+1} LLM call failed")
                error_msg = f"batch {batch_count} sub {i+1} LLM call failed at offset {current_offset}"
                error_sub = i + 1
                break
            parsed, warnings = ResponseParser.parse(resp, dm, primary_key_name, batch_count, sub_index=i+1, label_file_name=label_file_name, label_names=label_names)
            all_results.extend(parsed)
            if warnings:
                for w in warnings:
                    raw_resp = ResponseParser.extract_conv_response(resp, w["conv"]) if w["conv"] else (resp[:500] if resp else None)
                    w["sub"] = i + 1
                    w["raw_response"] = raw_resp
                    if w["token_id"]:
                        w["row"] = current_offset + i * sub_size + (w["conv"] - 1)
                        w["prompt"] = ResponseParser.extract_dialogue_prompt(p, w["conv"])
                    sub_warnings.append(w)

        return all_results, sub_results, sub_warnings, error_msg, error_sub

    # 处理单个 label
    def process_label(self, label_parser: LabelParser) -> bool:
        """处理单个 label 的打标任务，返回 True=成功 False=失败"""
        batch_size = self.config.batch_size
        lw = self.writer.for_label(label_parser.label_file_name)
        labels_text = label_parser.format_for_prompt()
        match_rule = label_parser.match_rule
        loader = self.loader

        self.log.info(f"Label: {label_parser.names} (match={label_parser.label_match})")
        self.log.info(f"Total rows: {loader.total_rows}")

        start_offset = lw.init_progress()
        batch_count = lw.get_batch_count()
        lw.update_progress(status="running")

        error_count = 0
        llm_total = 0
        start_time = time.time()
        current_offset = 0

        # 流式分批处理
        for batch in loader.iter_batches(batch_size):
            # 跳过已处理的批次
            if current_offset + len(batch) <= start_offset:
                current_offset += len(batch)
                continue

            # 部分跳过
            if current_offset < start_offset:
                batch = batch[start_offset - current_offset:]
                current_offset = start_offset

            # 调 LLM
            batch_count += 1
            self.log.info(f"[{current_offset}/{loader.total_rows}] {lw._label_file_name} batch {batch_count} ({len(batch)} dialogues, split={self.config.batch_size_split})")

            llm_start = time.time()
            results, sub_results, sub_warnings, error_msg, error_sub = self._batch_call_and_parse(
                batch, labels_text, match_rule, loader.primary_key, batch_count,
                current_offset, label_file_name=label_parser.label_file_name, label_names=label_parser._names)

            if error_msg:
                lw.abort(current_offset, error_msg, batch_count=batch_count, sub=error_sub)
                raise AssertionError(error_msg)

            llm_duration = time.time() - llm_start
            llm_total += llm_duration
            self.log.info(f"LLM耗时: {Logger.fmt_duration(llm_duration)}")

            # 保存 LLM 详细 IO
            self.log.save_llm_detail(
                file_key=self.writer._file_key,
                data_file=self.data_file,
                label_file_name=lw._label_file_name,
                batch_count=batch_count,
                offset=current_offset,
                sub_results=sub_results,
                config=self.config,
            )

            if results:
                lw.append_result(results)
                self.log.info(f"[{current_offset}] OK ({len(results)} hits)")
            else:
                self.log.info(f"[{current_offset}] OK (0 hits)")

            # 处理解析 warning
            if sub_warnings:
                lw.warning(sub_warnings, batch_count)

            current_offset += len(batch)
            lw.update_progress(offset=current_offset, batch_count=batch_count)

        duration = time.time() - start_time
        lw.update_progress(status="completed")
        self.log.info(f"Label '{lw._label_file_name}' done: {loader.total_rows} rows, {error_count} errors, LLM总耗时: {Logger.fmt_duration(llm_total)}, 总耗时: {Logger.fmt_duration(duration)}")

        # 统计各标签值命中数
        label_hits = self._count_label_hits(lw.result_path())
        lw.save_label_hits(label_hits)

        # 写 label 耗时
        lw.save_duration(Logger.fmt_duration(duration))

        return True

    # 预览 prompt，不写 progress 不调 LLM
    def _dry_run(self, pending_labels: list[dict]):
        
        self.log.info(f"{'='*30} DRY RUN {'='*30}")
        for label_task in pending_labels:
            label_parser = LabelParser.from_file(label_task["path"], label_task["label_match"])
            label_file_name = Path(label_task["path"]).stem
            batch = next(self.loader.iter_batches(10))
            dialogues_text = PromptBuilder.format_dialogues(batch)
            prompt = self.prompt_builder.build(label_parser.format_for_prompt(), dialogues_text, label_parser.match_rule)
            self.log.info(f"[DRY RUN] {label_file_name}: {len(batch)} dialogues, prompt ~{len(prompt) // 4} tokens")
            self.log.info(f"[DRY RUN] Prompt:\n{prompt}")

    # 主流程
    def merge_results(self, output_name: str = '') -> str:
        """合并各 label 的 result.jsonl → 单个 parquet"""
        if not output_name:
            if self.config.result_file:
                output_name = self.config.result_file
            elif self.config.db_table and self.config.db.get("date_field_value"):
                output_name = f"{self.config.db_table}_{self.config.db.get('date_field_value')}_llm_total_label_result"
            else:
                output_name = f"{self.writer._file_key}_llm_total_label_result"
        # 读中间结果用 output_dir
        intermediate_dir = os.path.join(self.writer.output_dir, self.writer._file_key)
        all_rows = []
        for label_dir in sorted(Path(intermediate_dir).iterdir()):
            result_file = label_dir / "result.jsonl"
            if result_file.is_file():
                with open(result_file, "r", encoding="utf-8") as f:
                    for line in f:
                        all_rows.append(json.loads(line))
        if not all_rows:
            self.log.info("无结果可合并")
            return ''
        df = pd.DataFrame(all_rows)
        # 写合并结果用 result_dir
        result_dir = self.writer.result_dir
        os.makedirs(result_dir, exist_ok=True)
        output_path = os.path.join(result_dir, f"{output_name}.parquet")
        df.to_parquet(output_path, index=False)
        self.writer.update_file_progress(result_file=output_path)
        self.log.info(f"合并完成: {output_path} ({len(df)} rows)")
        return output_path

    def upload_result(self, result_file: str):
        # 表名优先级：result_table > {库名}.tmp_{文件名}
        if self.config.result_table:
            table_name = self.config.result_table
        else:
            file_name = os.path.splitext(os.path.basename(self.data_file))[0].replace('-', '')
            table_name = f"{self.config.db_database}.tmp_{file_name}"
        from scripts.db_manager import StarRocksDB
        db = StarRocksDB.from_config(self.config)
        db.load_from_file(result_file, table_name=table_name, is_overwrite=True)
        self.writer.update_file_progress(result_table=table_name)
        self.log.info(f"结果已上传到表: {table_name}")

    def run(self):
        
        self.log.info(f"{'='*30} START {'='*30}")
        run_start = time.time()
        
        try:
            # 检查进度 + 过滤待处理 label
            pending_labels = self.resolve_pending_labels()
            if pending_labels is None:
                # 配置变更，需用户决定是否 force_run
                return
            if pending_labels:
                self.log.info(f"待处理 label: {[Path(t['path']).stem for t in pending_labels]}")
        
                # 加载数据文件
                self.loader = FileLoader(
                    data_file=self.data_file,
                    primary_key=self.config.primary_key,
                    message_column=self.config.message_column,
                    message_time_format=self.config.message_time_format,
                    message_time_sep=self.config.message_time_sep,
                    message_multi_sep=self.config.message_multi_sep,
                )
                self.log.info(f"Loaded {self.data_file}: {self.loader.total_rows} rows")
        
                if self.config.dry_run:
                    self._dry_run(pending_labels)
                    
                else:
                    # 初始化文件级进度
                    self.writer.init_progress(self.loader.total_rows, self.config.config_snapshot, self.db_meta)
            
                    # label 分组并发处理
                    batch_label = self.config.batch_label
                    label_groups = [pending_labels[i:i+batch_label] for i in range(0, len(pending_labels), batch_label)]
                    failed_labels = []

                    for group in label_groups:
                        with ThreadPoolExecutor(max_workers=len(group)) as pool:
                            futures = {}
                            for label_task in group:
                                label_path = label_task["path"]
                                label_match = label_task["label_match"]
                                label_file_name = Path(label_path).stem
                                label_parser = LabelParser.from_file(label_path, label_match)
                                self.log.info(f"Label: {label_file_name} (match={label_match}) | File: {Path(self.data_file).name}")
                                future = pool.submit(self.process_label, label_parser)
                                futures[future] = label_file_name

                            for future in as_completed(futures):
                                label_file_name = futures[future]
                                try:
                                    success = future.result()
                                    if not success:
                                        failed_labels.append(label_file_name)
                                except Exception as e:
                                    import traceback
                                    self.log.error(f"Label '{label_file_name}' unexpected error: {e}\n{traceback.format_exc()}")
                                    failed_labels.append(label_file_name)

                    if failed_labels:
                        raise AssertionError(f"Labels failed: {', '.join(failed_labels)}")

        
            # 更新文件级状态
            self.writer.update_file_progress(status="completed", error_msg="")
            result_file = self.merge_results()
            if result_file and self.config.db_table:
                self.upload_result(result_file)
        
            # 写文件级耗时
            run_duration = Logger.fmt_duration(time.time() - run_start)
            self.writer.save_duration(run_duration)

        except KeyboardInterrupt:
            self.writer.abort_running_labels()
            self.writer.update_file_progress(status="aborted", error_msg="用户手动中断 (Ctrl+C)")
            run_duration = Logger.fmt_duration(time.time() - run_start)
            self.writer.save_duration(run_duration)
        except Exception as e:
            self.writer.update_file_progress(status="failed", error_msg=str(e))
            run_duration = Logger.fmt_duration(time.time() - run_start)
            self.writer.save_duration(run_duration)
            raise
        finally:
            # 推送通知
            notifier = Notifier.from_config(self.config)
            file_prog = self.writer.get_file_progress()
            if file_prog:
                notifier.send(title=self.config.notify_title, file_prog=file_prog)
        
        self.log.info(f"{'='*30} END {'='*30}")
