"""
main.py — 批量打标主入口
参数解析 → 配置读取 → 启动 BatchLabeler
"""

from scripts.batch_labeler import BatchLabeler
from scripts.config import Config
from pathlib import Path
import argparse
import sys


def resolve_data_file(config: Config, args: argparse.Namespace) -> tuple[str, dict]:
    """根据配置解析数据文件路径（DB 导出 或 本地文件），返回 (data_file, db_meta)"""
    if config.db_table:
        if not args.db_date_field_value:
            print("必须指定表日期 --db_date_field_value ")
            sys.exit(1)
        from scripts.db_manager import StarRocksDB
        db = StarRocksDB.from_config(config)
        date_field = config.db_date_field
        date_field_value = args.db_date_field_value
        where = f"WHERE DATE_FORMAT({date_field},'{config.db_date_format}') = '{date_field_value}'" if date_field and date_field_value else ""
        cols = f"{config.primary_key}, {config.message_column}"
        sql = f"SELECT {cols} FROM {config.db_table} {where}"
        db_meta = {
            "database": config.db_database,
            "table": config.db_table,
            "date_field": date_field,
            "date_field_value": date_field_value,
            "date_format": config.db_date_format,
            "query_sql": sql,
        }
        
        file_name = f'{config.db_table}_{date_field_value}'
        data_file = db.query_to_file(sql, file_name=file_name, output_dir=config.db_table_output_dir)

        return data_file, db_meta
    elif args.data_file:
        data_file = str(Path(args.data_file).resolve())
        if not Path(data_file).is_file():
            print(f"Data file not found: {data_file}")
            sys.exit(1)
        return data_file, {}
    else:
        print("必须指定 --data_file 或 --db_table ")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="流式分批打标")

    parser.add_argument("--config", default="config.json", help="config.json 路径（默认同目录下）")
    parser.add_argument("--data_file", default=None, help="数据文件路径（parquet/csv/excel/json）")
        
    # 数据配置 (config.file)
    parser.add_argument("--output_dir", default=None, help="输出目录（覆盖 file.output_dir）")
    parser.add_argument("--result_dir", default=None, help="结果输出目录（覆盖 file.result_dir，默认用 output_dir）")
    parser.add_argument("--result_file", default=None, help="结果文件名（覆盖 file.result_file，默认自动生成）")
    parser.add_argument("--primary_key", default=None, help="主键字段名（覆盖 file.primary_key）")
    parser.add_argument("--message_column", default=None, help="对话内容字段名（覆盖 file.message_column）")
    parser.add_argument("--message_time_format", default=None, help="消息时间格式（覆盖 file.message_time_format）")
    parser.add_argument("--message_time_sep", default=None, help="消息时间分隔符（覆盖 file.message_time_sep）")
    parser.add_argument("--message_multi_sep", default=None, help="多条消息分隔符（覆盖 file.message_multi_sep）")

    # 模型配置 (config.model)
    parser.add_argument("--model_id", default=None, help="模型标识（覆盖 model.model_id）")

    # prompt 配置 (config.prompt)
    parser.add_argument("--prompt_template_path", default=None, help="prompt 模板路径（覆盖 prompt.prompt_template_path）")

    # label 配置 (config.label)
    parser.add_argument("--labels_file", default=None, help="标签定义文件，逗号分割多文件（覆盖 label.files）")
    parser.add_argument("--labels_match", default=None, help="标签匹配方式 multi/single，逗号分割对应多文件（默认 multi）")

    # 处理配置 (config.processing)
    parser.add_argument("--batch_size", type=int, default=None, help="每批条数（覆盖 processing.batch_size）")
    parser.add_argument("--batch_size_split", type=int, default=None, help="每批拆成几份并发（覆盖 processing.batch_size_split）")
    parser.add_argument("--batch_label", type=int, default=None, help="label级并发数（覆盖 processing.batch_label）")
    parser.add_argument("--force_run", action="store_true", default=False, help="强制重跑，清空已有结果")
    parser.add_argument("--dry_run", action='store_true', default=None, help="只构造 prompt 不调 LLM（覆盖 processing.dry_run）")
    
    # 消息配置 (config.notify)   
    parser.add_argument("--wecom_webhook_key", default=None, help="企业微信 webhook key（覆盖 notify.wecom_webhook_key）")
    parser.add_argument("--notify_title", default=None, help="推送标题（覆盖 notify.title）")
    
    # DB配置 (config.db)   
    parser.add_argument("--db_table", default=None, help="数据库表名（传入则走 DB 导出流程，覆盖 db.table）")
    parser.add_argument("--db_date_field", default=None, help="日期字段名（覆盖 db.date_field）")
    parser.add_argument("--db_date_field_value", default=None, help="日期值（如 20260501，配合 date_field 筛选）")
    parser.add_argument("--db_date_format", default=None, help="日期格式（覆盖 db.date_format，如 yyyyMMdd）")
    parser.add_argument("--db_table_output_dir", default=None, help="DB导出文件保存目录（覆盖 db.table_output_dir）")
    parser.add_argument("--db_result_table", default=None, help="结果上传表名（覆盖 db.result_table，默认自动生成）")

    args = parser.parse_args()

    # 加载配置 & CLI 覆盖
    config = Config.load(args.config).merge_cli(args)

    # 数据文件
    data_file, db_meta = resolve_data_file(config, args)

    # 初始化打标器
    labeler = BatchLabeler(config, data_file, args.force_run, db_meta=db_meta)
    labeler.run()


if __name__ == "__main__":
    main()
