"""配置读取模块"""

import json
import os
from pathlib import Path


class Config:
    """项目配置，从 config.json 读取"""

    def __init__(self, file: dict, label: dict, model: dict, prompt: dict, processing: dict, notify: dict = {}, db: dict = {}):
        self.file = file
        self.label = label
        self.model = model
        self.prompt = prompt
        self.processing = processing
        self.notify = notify or {}
        self.db = db or {}

    @classmethod
    def load(cls, path: str) -> "Config":
        """从 config.json 加载配置"""
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        file_cfg = data.get("file", {})
        label = data.get("label", {})
        model = data.get("model", {})
        prompt = data.get("prompt", {})
        processing = data.get("processing", {})

        # 校验 file 必填字段
        file_required = ["primary_key", "message_column", "message_time_format",
                         "message_time_sep", "message_multi_sep"]
        for field in file_required:
            if field not in file_cfg:
                raise ValueError(f"Missing required file config: {field}")

        # 校验 label.files
        label_files = label.get("files", [])
        if not label_files:
            # 未配置 label.files → 自动扫描 labels/ 目录，默认 multi 模式（多匹配）
            md_files = sorted(Path("labels").glob("*.md"))
            if not md_files:
                raise ValueError("未配置 label.files 且 labels/ 下无 .md 文件")
            label["files"] = [{"path": str(f), "label_match": "multi"} for f in md_files]
        else:
            # 已配置 → path 必填，label_match 默认 multi 模式（多匹配）
            for item in label_files:
                if "path" not in item:
                    raise ValueError("Each label file must have 'path'")
                item.setdefault("label_match", "multi")

        # 校验 model 必填字段
        model_required = ["base_url", "api_key", "model_id"]
        for field in model_required:
            if not model.get(field):
                raise ValueError(f"Missing required model config: {field}")

        # 校验 prompt 必填字段
        prompt_required = ["prompt_template_path"]
        for field in prompt_required:
            if field not in prompt:
                raise ValueError(f"Missing required prompt config: {field}")

        notify = data.get("notify", {})
        db = data.get("db", {})

        return cls(file=file_cfg, label=label, model=model, prompt=prompt, processing=processing, notify=notify, db=db)

    # ─── file 属性 ───

    @property
    def output_dir(self) -> str:
        """输出目录（默认 output）"""
        return self.file.get("output_dir", "output")

    @property
    def result_dir(self) -> str:
        """结果输出目录（默认用 output_dir）"""
        return self.file.get("result_dir", "") or self.output_dir

    @property
    def result_file(self) -> str:
        """结果文件名（空则自动生成）"""
        return self.file.get("result_file", "")

    @property
    def log_dir(self) -> str:
        """日志目录（默认 logs）"""
        return self.file.get("log_dir", "logs")

    @property
    def notify_title(self) -> str:
        """推送标题（默认 批量打标）"""
        return self.notify.get("title", "批量打标")

    # ─── db 属性 ───

    @property
    def db_host(self) -> str:
        """数据库主机"""
        return self.db.get("host", "")

    @property
    def db_port(self) -> int:
        """数据库端口（默认 10009）"""
        return self.db.get("port", 10009)

    @property
    def db_username(self) -> str:
        """数据库用户名"""
        return self.db.get("username", "")

    @property
    def db_password(self) -> str:
        """数据库密码"""
        return self.db.get("password", "")

    @property
    def db_database(self) -> str:
        """数据库名"""
        return self.db.get("database", "")

    @property
    def db_table(self) -> str:
        """默认表名"""
        return self.db.get("table", "")

    @property
    def db_date_field(self) -> str:
        """日期字段名"""
        return self.db.get("date_field", "")

    @property
    def db_date_format(self) -> str:
        """日期格式（默认 %Y-%m-%d）"""
        return self.db.get("date_format", "yyyyMMdd")


    @property
    def db_table_output_dir(self) -> str:
        """表保存路径"""
        return self.db.get("table_output_dir", "data")

    @property
    def result_table(self) -> str:
        """结果上传表名（空则用 {库名}.tmp_{文件名}）"""
        return self.db.get("result_table", "")

    @property
    def oss_mount_path(self) -> str:
        """OSS 本地挂载路径"""
        return self.db.get("s3", {}).get("oss_mount_path", "")

    @property
    def s3_bucket_path(self) -> str:
        """S3 bucket 路径"""
        return self.db.get("s3", {}).get("bucket_path", "")

    @property
    def s3_access_key(self) -> str:
        """S3 access key"""
        return self.db.get("s3", {}).get("access_key", "")

    @property
    def s3_secret_key(self) -> str:
        """S3 secret key"""
        return self.db.get("s3", {}).get("secret_key", "")

    @property
    def s3_region(self) -> str:
        """S3 region"""
        return self.db.get("s3", {}).get("region", "")

    @property
    def s3_endpoint(self) -> str:
        """S3 endpoint"""
        return self.db.get("s3", {}).get("endpoint", "")

    @property
    def primary_key(self) -> str:
        """数据主键字段名，用于标识每条对话的唯一 ID"""
        return self.file["primary_key"]

    @property
    def message_column(self) -> str:
        """对话内容字段名，该列包含待标注的对话文本"""
        return self.file["message_column"]

    @property
    def message_time_format(self) -> str:
        """消息时间格式
        timestamp_ms  - 毫秒时间戳 (1704067200000)
        timestamp_s   - 秒时间戳 (1704067200)
        yyyymmddhhmmss - 数字拼接 (20240101120000)
        iso8601       - ISO 格式字符串 (2024-01-01T12:00:00)
        raw           - 原样输出不转换
        none          - 消息无时间信息，整条当纯文本处理"""
        return self.file["message_time_format"]

    @property
    def message_time_sep(self) -> str:
        """时间与消息内容的分隔符，如 ":" 表示 "15:30:00:你好"
        message_time_format=none 时无效"""
        return self.file["message_time_sep"]

    @property
    def message_multi_sep(self) -> str:
        """同一字段内多条消息的分隔符，如 $$$ 表示 消息1$$$消息2"""
        return self.file["message_multi_sep"]

    # ─── label 属性 ───

    @property
    def label_files(self) -> list[dict]:
        """标签定义文件列表
        每项: {"path": "labels/xxx.md", "label_match": "multi|single"}
        path        - 标签定义文件路径
        label_match - multi: 一条对话可命中多标签; single: 只取最匹配"""
        return self.label["files"]

    # ─── model 属性 ───

    @property
    def base_url(self) -> str:
        """LLM API 地址，OpenAI 兼容格式 (如 http://host:port/v1)"""
        return self.model["base_url"]

    @property
    def api_key(self) -> str:
        """LLM API 密钥"""
        return self.model["api_key"]

    @property
    def model_id(self) -> str:
        """模型标识 (如 deepseek-v4-pro)"""
        return self.model["model_id"]

    @property
    def model_timeout(self) -> int:
        """单次 API 请求超时秒数 (默认 120)"""
        return self.model.get("model_timeout", 120)

    @property
    def max_retries(self) -> int:
        """API 请求失败最大重试次数 (默认 3)"""
        return self.model.get("max_retries", 3)

    @property
    def max_tokens(self) -> int:
        """LLM 响应最大 token 数 (默认 4096)"""
        return self.model.get("max_tokens", 4096)

    # ─── prompt 属性 ───

    @property
    def prompt_template_path(self) -> str:
        """Prompt 模板文件路径"""
        return self.prompt["prompt_template_path"]

    @property
    def config_snapshot(self) -> dict:
        """影响打标结果的配置快照，用于续跑时比对"""
        return {
            "model_id": self.model_id,
            "max_tokens": self.max_tokens,
            "batch_size_split": self.batch_size_split,
            "batch_size": self.batch_size,
            "batch_label": self.batch_label,
            "prompt_template_path": self.prompt_template_path,
            "label_files": self.label_files,
        }

    # ─── processing 属性 ───

    @property
    def batch_size(self) -> int:
        """每次 LLM 调用处理的对话条数 (默认 20)"""
        return self.processing.get("batch_size", 20)

    @property
    def batch_size_split(self) -> int:
        """每批拆成几份并发（默认1=不拆）"""
        return self.processing.get("batch_size_split", 1)

    @property
    def batch_label(self) -> int:
        """label 级并发数（默认 1=串行）"""
        return self.processing.get("batch_label", 1)

    @property
    def dry_run(self) -> bool:
        """试运行模式，只构建 prompt 不调用 LLM (默认 False)"""
        return self.processing.get("dry_run", False)

    def merge_cli(self, args) -> "Config":
        """用 CLI 参数覆盖配置，非 None 的 CLI 参数优先"""
        # 数据配置覆盖 (config.file)
        if args.output_dir:
            self.file["output_dir"] = args.output_dir
        if args.result_dir:
            self.file["result_dir"] = args.result_dir
        if args.result_file:
            self.file["result_file"] = args.result_file
        if args.primary_key:
            self.file["primary_key"] = args.primary_key
        if args.message_column:
            self.file["message_column"] = args.message_column
        if args.message_time_format:
            self.file["message_time_format"] = args.message_time_format
        if args.message_time_sep:
            self.file["message_time_sep"] = args.message_time_sep
        if args.message_multi_sep:
            self.file["message_multi_sep"] = args.message_multi_sep

        # 模型配置覆盖 (config.model)
        if args.model_id:
            self.model["model_id"] = args.model_id

        # prompt 配置覆盖 (config.prompt)
        if args.prompt_template_path:
            self.prompt["prompt_template_path"] = args.prompt_template_path

        # label 配置覆盖 (config.label)
        if args.labels_file:
            paths = [p.strip() for p in args.labels_file.split(",")]
            if args.labels_match:
                modes = [m.strip() for m in args.labels_match.split(",")]
            else:
                modes = ["multi"] * len(paths)
            self.label["files"] = [
                {"path": paths[i], "label_match": modes[i] if i < len(modes) else "multi"}
                for i in range(len(paths))
            ]

        # 处理配置覆盖 (config.processing)
        if args.batch_size:
            self.processing["batch_size"] = args.batch_size
        if args.batch_size_split:
            self.processing["batch_size_split"] = args.batch_size_split
        if args.batch_label:
            self.processing["batch_label"] = args.batch_label
        if args.dry_run:
            self.processing["dry_run"] = True

        # 推送配置覆盖 (config.notify)
        if args.wecom_webhook_key:
            self.notify["wecom_webhook_key"] = args.wecom_webhook_key
        if args.notify_title:
            self.notify["title"] = args.notify_title

        # DB 配置覆盖 (config.db)
        if args.db_table:
            self.db["table"] = args.db_table
        if args.db_date_field:
            self.db["date_field"] = args.db_date_field
        if args.db_date_field_value:
            self.db["date_field_value"] = args.db_date_field_value
        if args.db_date_format:
            self.db["date_format"] = args.db_date_format
        if args.db_table_output_dir:
            self.db["table_output_dir"] = args.db_table_output_dir
        if args.db_result_table:
            self.db["result_table"] = args.db_result_table

        return self
