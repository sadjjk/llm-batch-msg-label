"""Prompt 构造模块：读模板 + 解析标签 + 填数据 + 响应解析"""

from typing import Optional
import json
import os
import re
from pathlib import Path

# 对话标记常量
DIALOGUE_MARKER = "对话"


def dialogue_tag(i: int) -> str:
    return f"---{DIALOGUE_MARKER}{i}---"


# LLM 输出格式约定（key 与 ResponseParser 绑定，请勿修改 key）
OUTPUT_FORMAT = {"conv": "对话序号数字（如 1、2、3，不要填\"对话1\"）", 
                 "label_value": "命中的标签名（必须从上方标签名清单中原样复制，不可增删字符、去掉前缀或修改格式）", 
                 "line": "关键依据的消息序号数组（如[3]或[1,3,5]）⚠️必须在该对话标注的line范围内取值，严禁超出或跨对话引用 例如：对话3只有[1][2]两条消息，则 line 只能填 [1] 或 [2] 或 [1,2]，绝不能填 [3] 或更大的数"}


class LabelParser:
    """标签定义解析器"""

    def __init__(self, labels: list[dict], label_match: str = "multi"):
        """
        Args:
            labels: [{"name": "标签名", "definition": "定义"}, ...]
            label_match: "multi" 或 "single"
        """
        self.labels = labels
        self.label_match = label_match
        self.label_file_name: str = ""
        self._names = [label["name"] for label in labels]

    @classmethod
    def from_file(cls, path: str, label_match: str = "multi") -> "LabelParser":
        """从 md 文件解析标签"""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        labels = []
        current_name = None
        current_lines = []

        for line in content.split("\n"):
            if line.startswith("# "):
                if current_name:
                    labels.append({
                        "name": current_name,
                        "definition": "\n".join(current_lines).strip()
                    })
                current_name = line[2:].strip()
                current_lines = []
            else:
                current_lines.append(line)

        if current_name:
            labels.append({
                "name": current_name,
                "definition": "\n".join(current_lines).strip()
            })

        parser = cls(labels, label_match)
        parser.label_file_name = Path(path).stem
        return parser

    @property
    def names(self) -> list[str]:
        return self._names

    @property
    def match_rule(self) -> str:
        """根据 label_match 返回匹配规则文本"""
        if self.label_match == "single":
            return "- 每条对话最多命中一个标签，选最匹配的\n- 不命中任何标签 → 不输出该对话"
        else:
            return "- 命中多个标签 → 输出多个对象\n- 不命中任何标签 → 不输出该对话"

    def format_for_prompt(self) -> str:
        """格式化标签定义为 prompt 文本"""
        parts = []
        for i, label in enumerate(self.labels, 1):
            parts.append(f"{i}. {label['name']}\n{label['definition']}")
        # 追加合法标签名清单
        name_list = "\n".join(f"- {n}" for n in self._names)
        parts.append(f"⚠️ label_value 只能填以下值之一（原样复制，不可修改）：\n{name_list}")
        return "\n\n".join(parts)


class PromptBuilder:
    """Prompt 构造器"""

    def __init__(self, template: str):
        self.template = template
        self.output_format = json.dumps(OUTPUT_FORMAT, ensure_ascii=False)

    @classmethod
    def from_file(cls, path: str) -> "PromptBuilder":
        """从模板文件加载"""
        if not os.path.exists(path):
            raise FileNotFoundError(f"Template not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            template = f.read()
        return cls(template)

    @staticmethod
    def format_dialogues(batch: list[dict]) -> str:
        """格式化对话列表为 prompt 文本

        Args:
            batch: [{"pk": ..., "sentences": [{"time": ..., "text": ...}, ...]}, ...]

        Returns:
            ---对话1---
            [1] 注销账户
            [2] 注销失败

            ---对话2---
            [1] 你好
            [2] 我刚看到信息
        """
        blocks = []
        for i, item in enumerate(batch, 1):
            lines = [dialogue_tag(i)]
            for j, s in enumerate(item["sentences"], 1):
                lines.append(f"[{j}] {s['text']}")
            total = len(item["sentences"])
            lines.append(f"（共{total}条消息，line范围:1-{total}）")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @staticmethod
    def build_dialogue_map(batch: list[dict]) -> dict:
        """构建对话序号到 pk + sentences 的映射

        Returns:
            {1: {"pk": "d4e01310", "sentences": [{"time": "15:33:18", "text": "注销账户"}, ...]}, ...}
        """
        dialogue_map = {}
        for i, item in enumerate(batch, 1):
            dialogue_map[i] = {
                "pk": item["pk"],
                "sentences": item["sentences"],
            }
        return dialogue_map

    def build(self, labels_text: str, dialogues_text: str, match_rule: str) -> str:
        """填充模板，返回完整 prompt"""
        prompt = self.template.replace("{{labels_text}}", labels_text)
        prompt = prompt.replace("{{dialogues_text}}", dialogues_text)
        prompt = prompt.replace("{{match_rule}}", match_rule)
        prompt = prompt.replace("{{output_format}}", self.output_format)
        return prompt


class ResponseParser:
    """LLM 响应解析器：解析 conv/line，还原 pk + evidence"""

    @staticmethod
    def parse(response_text: str, dialogue_map: dict, primary_key_name: str, batch_count: int, sub_index: int = 0, label_file_name: str = "", label_names: Optional[list[str]] = None) -> tuple[list[dict], list[dict]]:
        """解析 LLM 返回的 JSON 数组，用 dialogue_map 还原 pk 和 evidence

        Args:
            response_text: LLM 原始响应
            dialogue_map: {conv序号: {"pk": ..., "sentences": [...]}, ...}
            primary_key_name: 主键字段名（如 token_id、conv_id）

        Returns:
            (valid_results, warnings)
            valid_results: [{主键字段名: ..., "label": ..., "label_value": ..., "evidence": ...}, ...]
            warnings: [{"token_id": ..., "conv": ..., "error": ..., "dialogue": ...}, ...]
        """
        if not response_text:
            return [], [{"token_id": None, "conv": None, "error": "Empty response", "dialogue": None}]

        text = response_text.strip()
        # 去 markdown 代码块包裹
        if text.startswith("```"):
            text = re.sub(r'^```\w*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            text = text.strip()

        try:
            results = json.loads(text)
            if isinstance(results, dict):
                results = [results]
        except json.JSONDecodeError as e:
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if match:
                try:
                    results = json.loads(match.group())
                except json.JSONDecodeError:
                    # 尝试按行解析 JSONL 格式
                    results = []
                    for line in text.strip().split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            results.append(obj)
                        except json.JSONDecodeError:
                            pass
                    if not results:
                        return [], [{"token_id": None, "conv": None, "error": f"JSON parse error: {e}", "dialogue": None}]
            else:
                # 尝试按行解析 JSONL 格式
                results = []
                for line in text.strip().split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        results.append(obj)
                    except json.JSONDecodeError:
                        pass
                if not results:
                    return [], [{"token_id": None, "conv": None, "error": f"JSON parse error: {e}", "dialogue": None}]

        if not isinstance(results, list):
            return [], [{"token_id": None, "conv": None, "error": f"Expected list, got {type(results).__name__}", "dialogue": None}]

        valid_results = []
        warnings = []

        for item in results:
            if not isinstance(item, dict):
                warnings.append({"token_id": None, "conv": None, "error": f"Non-dict item: {item}", "dialogue": None})
                continue

            conv_key, label_key, line_key = list(OUTPUT_FORMAT.keys())
            conv = item.get(conv_key)
            label_value = item.get(label_key, "")
            # 归一化：LLM 可能返回 "标签名：定义"，截断只保留标签名
            if label_names and label_value:
                for name in label_names:
                    if label_value.startswith(name) and len(label_value) > len(name) and label_value[len(name)] in ("：", ":"):
                        label_value = name
                        break
            line = item.get(line_key)

            # 校验 conv
            if conv is None:
                warnings.append({"token_id": None, "conv": None, "error": f"Missing {conv_key} in: {item}", "dialogue": None})
                continue
            try:
                conv = int(conv)
            except (ValueError, TypeError):
                m = re.search(r'\d+', str(conv))
                if m:
                    conv = int(m.group())
                else:
                    warnings.append({"token_id": None, "conv": None, "error": f"Invalid {conv_key}: {conv}", "dialogue": None})
                    continue
            if conv not in dialogue_map:
                warnings.append({"token_id": None, "conv": conv, "error": f"{conv_key} out of range: {conv}", "dialogue": None})
                continue

            entry = dialogue_map[conv]

            # 校验 label_value
            if not label_value:
                warnings.append({"token_id": entry["pk"], "conv": conv, "error": f"Empty {label_key} for {conv_key} {conv}", "dialogue": entry["sentences"]})
                continue

            # 校验 line（必填，统一转 list[int]）
            if line is None:
                warnings.append({"token_id": entry["pk"], "conv": conv, "label_value": label_value, "error": f"Missing {line_key} for {conv_key} {conv}, {label_key} {label_value}", "dialogue": entry["sentences"]})
                continue
            # 兼容 LLM 返回 int 或 list[int]
            if isinstance(line, int):
                line = [line]
            elif isinstance(line, list):
                try:
                    line = [int(x) for x in line]
                except (ValueError, TypeError):
                    warnings.append({"token_id": entry["pk"], "conv": conv, "label_value": label_value, "error": f"Invalid {line_key}: {line} for {conv_key} {conv}", "dialogue": entry["sentences"]})
                    continue
            else:
                m_list = re.findall(r'\d+', str(line))
                if not m_list:
                    warnings.append({"token_id": entry["pk"], "conv": conv, "label_value": label_value, "error": f"Invalid {line_key}: {line} for {conv_key} {conv}", "dialogue": entry["sentences"]})
                    continue
                line = [int(x) for x in m_list]

            # 去重排序
            line = sorted(set(line))

            # 校验越界
            invalid_lines = [n for n in line if n < 1 or n > len(entry["sentences"])]
            if invalid_lines:
                if len(entry["sentences"]) == 1:
                    # 只有1条消息，越界序号全部修复为1
                    line = [1]
                else:
                    # 多条消息时越界，无法判断实际指向，报错
                    warnings.append({"token_id": entry["pk"], "conv": conv, "label_value": label_value, "error": f"{line_key} out of range: {conv_key} {conv} {line_key} {invalid_lines} (max {len(entry['sentences'])})", "dialogue": entry["sentences"]})
                    continue

            # 还原 pk
            token_id = entry["pk"]

            # 还原 evidence（多条换行拼接）
            evidence_parts = []
            for n in line:
                sentence = entry["sentences"][n - 1]
                time_str = sentence.get("time", "")
                text_content = sentence.get("text", "")
                if time_str:
                    evidence_parts.append(f"[{time_str}] {text_content}")
                else:
                    evidence_parts.append(f"[No.{n}] {text_content}")
            evidence = "\n---\n".join(evidence_parts)

            valid_results.append({
                "batch": batch_count,
                "sub": sub_index,
                "conv": conv,
                primary_key_name: token_id,
                "label": label_file_name,
                "label_value": label_value,
                "evidence": evidence,
            })

        return valid_results, warnings

    @staticmethod
    def extract_dialogue_prompt(full_prompt: str, conv: int) -> Optional[str]:
        """从完整 prompt 中提取指定对话，生成可重跑的单条 prompt"""
        pattern = re.compile(f"---{DIALOGUE_MARKER}\\d+---")
        positions = [(m.start(), m.end()) for m in pattern.finditer(full_prompt)]

        for i, (start, end) in enumerate(positions):
            if full_prompt[start:end] == dialogue_tag(conv):
                header = full_prompt[:positions[0][0]]
                content_end = positions[i + 1][0] if i + 1 < len(positions) else len(full_prompt)
                dialogue_content = full_prompt[end:content_end].strip()
                footer_match = re.search(r'\n输出[:：]', full_prompt)
                footer = full_prompt[footer_match.start():] if footer_match else ""
                return f"{header}{dialogue_tag(conv)}\n{dialogue_content}\n\n{footer}"

        return None

    @staticmethod
    def extract_conv_response(response_text: str, conv) -> Optional[str]:
        """从 LLM 响应中提取指定 conv 的条目"""
        try:
            items = json.loads(response_text.strip())
            if isinstance(items, list):
                matched = [item for item in items if isinstance(item, dict) and item.get("conv") == conv]
                if matched:
                    return json.dumps(matched, ensure_ascii=False)
        except (json.JSONDecodeError, AttributeError):
            pass
        return response_text[:500] if response_text else None
