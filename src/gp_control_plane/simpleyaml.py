from __future__ import annotations

from pathlib import Path
from typing import Any


class YAMLError(ValueError):
    pass


def load_file(path: Path) -> Any:
    return loads(path.read_text(encoding="utf-8"))


def dump_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(data), encoding="utf-8")


def loads(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise YAMLError("tabs are not supported for indentation")
        lines.append((len(line) - len(line.lstrip(" ")), line.lstrip(" ")))
    if not lines:
        return {}
    data, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise YAMLError(f"unexpected trailing YAML at line {index + 1}")
    return data


def dumps(data: Any) -> str:
    return _dump_value(data, 0).rstrip() + "\n"


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    current_indent, content = lines[index]
    if current_indent < indent:
        return {}, index
    if current_indent != indent:
        raise YAMLError(f"unexpected indentation at line {index + 1}")
    if content.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise YAMLError(f"unexpected nested mapping at line {index + 1}")
        if content.startswith("- "):
            break
        key, value = _split_key_value(content, index)
        if value == "":
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                parsed, index = _parse_block(lines, index + 1, lines[index + 1][0])
                result[key] = parsed
            else:
                result[key] = None
                index += 1
        else:
            result[key] = _parse_scalar(value)
            index += 1
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise YAMLError(f"unexpected nested list item at line {index + 1}")
        if not content.startswith("- "):
            break
        item = content[2:].strip()
        if item == "":
            if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                result.append(None)
                index += 1
            else:
                parsed, index = _parse_block(lines, index + 1, lines[index + 1][0])
                result.append(parsed)
            continue
        if ":" in item and not item.startswith(("'", '"')):
            key, value = _split_key_value(item, index)
            entry: dict[str, Any] = {key: _parse_scalar(value) if value else None}
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                nested, index = _parse_block(lines, index, lines[index][0])
                if isinstance(nested, dict):
                    entry.update(nested)
                else:
                    raise YAMLError(f"list item mapping cannot merge non-mapping at line {index}")
            result.append(entry)
        else:
            result.append(_parse_scalar(item))
            index += 1
    return result, index


def _split_key_value(content: str, index: int) -> tuple[str, str]:
    if ":" not in content:
        raise YAMLError(f"expected key/value at line {index + 1}")
    key, value = content.split(":", 1)
    key = key.strip()
    if not key:
        raise YAMLError(f"empty key at line {index + 1}")
    return key, value.strip()


def _parse_scalar(value: str) -> Any:
    if value in ("", "null", "Null", "NULL", "~"):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _dump_value(data: Any, indent: int) -> str:
    pad = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_dump_value(value, indent + 2).rstrip())
            else:
                lines.append(f"{pad}{key}: {_dump_scalar(value)}")
        return "\n".join(lines) + "\n"
    if isinstance(data, list):
        if not data:
            return f"{pad}[]\n"
        lines = []
        for item in data:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{pad}- {{}}")
                    continue
                first = True
                for key, value in item.items():
                    if first:
                        if isinstance(value, (dict, list)):
                            lines.append(f"{pad}- {key}:")
                            lines.append(_dump_value(value, indent + 4).rstrip())
                        else:
                            lines.append(f"{pad}- {key}: {_dump_scalar(value)}")
                        first = False
                    else:
                        if isinstance(value, (dict, list)):
                            lines.append(f"{pad}  {key}:")
                            lines.append(_dump_value(value, indent + 4).rstrip())
                        else:
                            lines.append(f"{pad}  {key}: {_dump_scalar(value)}")
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.append(_dump_value(item, indent + 2).rstrip())
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")
        return "\n".join(lines) + "\n"
    return f"{pad}{_dump_scalar(data)}\n"


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or text.startswith(("{", "[", "-", "#")) or ": " in text:
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for i, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1].isspace():
                return line[:i]
    return line
