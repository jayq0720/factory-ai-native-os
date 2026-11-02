"""从模型输出里取 JSON 对象。模型偶尔会包一层 ```json 或前后带话，这里宽松地取第一个完整对象。"""

from __future__ import annotations

import json
from typing import Any


def extract_json(text: str) -> dict[str, Any] | None:
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("\n") + 1 :] if "\n" in s else s
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return v if isinstance(v, dict) else None
    return None
