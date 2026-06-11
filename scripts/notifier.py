"""消息推送模块：配了就推，没配静默跳过"""

import json
import urllib.request
from pathlib import Path

from scripts.logger import Logger


class Notifier:
    """调度器：遍历已配置渠道推送，失败只 log 不中断主流程"""

    def __init__(self, channels: list):
        self.channels = channels
        self.log = Logger()

    @classmethod
    def from_config(cls, config) -> "Notifier":
        channels = []
        notify = config.notify or {}
        wecom_key = notify.get("wecom_webhook_key")
        if wecom_key:
            channels.append(WeComNotifier(wecom_key))
        return cls(channels)

    def send(self, title: str, file_prog: dict):
        """向所有已配置渠道推送"""
        if not self.channels:
            return
        summary = self._build_summary(file_prog)
        content = self._build_markdown(title, summary)
        for ch in self.channels:
            try:
                ch.send(content)
            except Exception as e:
                self.log.warning(f"推送失败 [{ch.name}]: {e}")

    def _build_summary(self, file_prog: dict) -> dict:
        """从 progress.json 的 file_prog 构建推送摘要"""
        total_rows = file_prog.get("total_rows", 0)
        batch_size = file_prog.get("config_snapshot", {}).get("batch_size", 1)
        total_batches = (total_rows + batch_size - 1) // batch_size if batch_size > 0 else 0
        labels_summary = []
        for name, prog in file_prog.get("labels", {}).items():
            labels_summary.append({
                "name": name,
                "status": prog.get("status", "unknown"),
                "labeled_count": prog.get("labeled_count", 0),
                "label_hits": prog.get("label_hits", {}),
                "duration": prog.get("duration", ""),
                "parse_error_count": prog.get("parse_error_count", 0),
                "batch_count": prog.get("batch_count", 0),
                "total_batches": total_batches,
            "result_file": prog.get("result_file", ""),
            })
        return {
            "data_file": Path(file_prog.get("data_file", "")).name,
            "model_id": file_prog.get("config_snapshot", {}).get("model_id", ""),
            "total_rows": file_prog.get("total_rows", 0),
            "status": file_prog.get("status", "unknown"),
            "duration": file_prog.get("duration", ""),
            "error_msg": file_prog.get("error_msg", ""),
            "db_source": file_prog.get("db_source", {}),
            "result_file": file_prog.get("result_file", ""),
            "result_table": file_prog.get("result_table", ""),
            "labels": labels_summary,
        }

    def _build_markdown(self, title: str, summary: dict) -> str:
        """公共 markdown 生成，多渠道共用"""
        status_icons = {"completed": "✅", "failed": "❌", "aborted": "⚠️"}
        icon = status_icons.get(summary["status"], "❓")
        lines = [f"## {title} {icon}"]

        db_source = summary.get('db_source', {})
        if db_source:
            db_name = db_source.get('database', '')
            tbl_name = db_source.get('table', '')
            full_table = f"{db_name}.{tbl_name}" if db_name and not tbl_name.startswith(db_name + '.') else tbl_name
            lines.append(f"> 数据表: `{full_table}`")
            lines.append(f"> 筛选条件: {db_source.get('date_field', '')} = {db_source.get('date_field_value', '')}")

        lines.append(f"> 数据文件: `{summary.get('data_file', '')}`")
        lines.append(f"> 模型: `{summary.get('model_id', '')}`")
        lines.append(f"> 总行数: {summary.get('total_rows', 0)}")
        duration = summary.get('duration', '')
        if duration:
            lines.append(f"> 总耗时: {duration}")

        if summary.get('error_msg'):
            err_icon = "⚠️" if summary["status"] == "aborted" else "❌"
            err_label = "中断原因" if summary["status"] == "aborted" else "失败原因"
            lines.append(f"> {err_icon} {err_label}: {summary['error_msg']}")

        merged = summary.get('result_file', '')
        if merged:
            lines.append(f"> 结果文件: `{Path(merged).name}`")
        result_table = summary.get('result_table', '')
        if result_table:
            lines.append(f"> 结果表: `{result_table}`")

        lines.append('\n')
        total = summary.get('total_rows', 0)
        for label in summary.get('labels', []):
            name = label['name']
            ls = label['status']
            count = label.get('labeled_count', 0)
            rate = f"{count/total*100:.1f}%" if total > 0 else "0%"
            status_icon = status_icons.get(ls, "❓")
            err = label.get('parse_error_count', 0)
            err_tag = f" ⚠️{err}解析异常" if err > 0 else ""

            lines.append('\n')
            batch_count = label.get('batch_count', 0)
            total_batches = label.get('total_batches', 0)
            batch_info = f" [{batch_count}/{total_batches}]" if total_batches > 0 else ""
            lines.append(f"### {name} {status_icon}{batch_info} ({count}命中/{rate}){err_tag}")
            result_file = label.get('result_file', '')
            if result_file:
                # 取 output/{file_key}/ 之后的相对路径
                parts = Path(result_file).parts
                rel = '/'.join(parts[-2:]) if len(parts) >= 2 else Path(result_file).name
                lines.append(f"> 结果: `{rel}`")

            ld = label.get('duration', '')
            if ld:
                lines.append(f"> 耗时: {ld}")

            for idx, (hit_name, hit_count) in enumerate(sorted(label.get('label_hits', {}).items(), key=lambda x: x[1], reverse=True), 1):
                hit_rate = f"{hit_count/total*100:.1f}%" if total > 0 else "0%"
                lines.append(f"> {idx}. {hit_name}: {hit_count} ({hit_rate})")

        return "\n".join(lines)


class WeComNotifier:
    """企业微信 webhook 推送"""

    BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
    MAX_BYTES = 4096

    def __init__(self, webhook_key: str):
        self.key = webhook_key
        self.url = f"{self.BASE_URL}{webhook_key}"
        self.name = "wecom"

    def send(self, content: str):
        """发送 markdown 消息，超长自动按 label 边界分段"""
        if self._utf8_len(content) <= self.MAX_BYTES:
            self._post(content)
            return

        # 超长：按 ### 分割，分段发送
        segments = content.split("\n### ")
        header = segments[0]
        current = header

        for seg in segments[1:]:
            label_block = "### " + seg
            if self._utf8_len(current) + self._utf8_len(label_block) > self.MAX_BYTES:
                self._post(current)
                current = f"> [续] 上一条\n\n{label_block}"
            else:
                current += "\n\n" + label_block

        if current.strip():
            self._post(current)

    @staticmethod
    def _utf8_len(text: str) -> int:
        return len(text.encode("utf-8"))

    def _post(self, content: str):
        """POST markdown 消息到企业微信 webhook"""
        data = json.dumps({
            "msgtype": "markdown",
            "markdown": {"content": content}
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("errcode", 0) != 0:
                raise RuntimeError(f"wecom errcode={result.get('errcode')}, errmsg={result.get('errmsg')}")
