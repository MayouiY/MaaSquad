"""ProjectInterface（interface.json）解析。

负责把「任务名 + 选项取值」转换为 MaaFramework 可执行的 entry 与 pipeline_override，
规则参考 ProjectInterface V2 协议：
https://github.com/MaaXYZ/MaaFramework/blob/main/docs/zh_cn/3.3-ProjectInterfaceV2协议.md
"""

import copy
import json
import re
from pathlib import Path
from typing import Any, Optional

SWITCH_YES = {"Yes", "yes", "Y", "y"}
SWITCH_NO = {"No", "no", "N", "n"}


class InterfaceError(ValueError):
    pass


def strip_jsonc(text: str) -> str:
    """去除 JSONC 中的注释与尾随逗号。"""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_jsonc(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(strip_jsonc(text))
    except json.JSONDecodeError as e:
        raise InterfaceError(f"{path} 解析失败: {e}") from e


def merge_override(base: dict[str, Any], override: dict[str, Any]) -> None:
    """将 override 合并进 base（原地修改），后合并的同名字段覆盖先合并的。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_override(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _available(item: dict[str, Any], controller: Optional[str], resource: Optional[str]) -> bool:
    ctrls = item.get("controller")
    ress = item.get("resource")
    if ctrls is not None and controller is not None and controller not in ctrls:
        return False
    if ress is not None and resource is not None and resource not in ress:
        return False
    return True


# Win32 虚拟键码，用于 hotkey 类型选项的字符串取值（如 "Ctrl+A"）
_VK_NAMES = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "SHIFT": 0x10, "CTRL": 0x11,
    "CONTROL": 0x11, "ALT": 0x12, "PAUSE": 0x13, "CAPSLOCK": 0x14, "ESC": 0x1B,
    "ESCAPE": 0x1B, "SPACE": 0x20, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23,
    "HOME": 0x24, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "DELETE": 0x2E, "WIN": 0x5B,
}


def _vk_code(name: str) -> int:
    key = name.strip().upper()
    if len(key) == 1 and (key.isdigit() or "A" <= key <= "Z"):
        return ord(key)
    if re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", key):
        return 0x70 + int(key[1:]) - 1
    if key in _VK_NAMES:
        return _VK_NAMES[key]
    raise InterfaceError(f"无法识别的按键: {name}")


def _parse_hotkey(value: Any) -> dict[str, int]:
    """hotkey 取值可以是整数键码、{"primary": 69, "modifier1": 17} 或 "Ctrl+E"。"""
    if isinstance(value, bool):
        raise InterfaceError(f"无效的快捷键: {value}")
    if isinstance(value, int):
        return {"primary": value}
    if isinstance(value, dict):
        if not isinstance(value.get("primary"), int):
            raise InterfaceError(f"快捷键缺少整数 primary: {value}")
        return {k: v for k, v in value.items() if isinstance(v, int)}
    if isinstance(value, str) and value.strip():
        parts = [p for p in value.split("+") if p.strip()]
        codes = [_vk_code(p) for p in parts]
        result = {"primary": codes[-1]}
        for idx, code in enumerate(codes[:-1], start=1):
            result[f"modifier{idx}"] = code
        return result
    raise InterfaceError(f"无效的快捷键: {value}")


def _substitute(template: Any, values: dict[str, Any]) -> Any:
    """替换模板中的 {名称} 占位符；整个字符串恰为占位符时保留值的类型。"""
    if isinstance(template, dict):
        return {k: _substitute(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute(v, values) for v in template]
    if not isinstance(template, str):
        return template

    exact = re.fullmatch(r"\{([^{}]+)\}", template)
    if exact and exact.group(1) in values:
        return values[exact.group(1)]

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            return m.group(0)
        v = values[key]
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    return re.sub(r"\{([^{}]+)\}", repl, template)


def _convert_input(raw: str, pipeline_type: Optional[str], name: str) -> Any:
    if pipeline_type == "int":
        try:
            return int(raw)
        except ValueError as e:
            raise InterfaceError(f"输入 {name} 需要整数，实际为 {raw!r}") from e
    if pipeline_type == "bool":
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n", ""):
            return False
        raise InterfaceError(f"输入 {name} 需要布尔值，实际为 {raw!r}")
    return raw


class ProjectInterface:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.base_dir = self.path.parent
        data = load_jsonc(self.path)
        if not isinstance(data, dict):
            raise InterfaceError(f"{self.path} 顶层必须是对象")
        self._merge_imports(data)
        self.data = data

        self.controllers: list[dict[str, Any]] = data.get("controller", [])
        self.resources: list[dict[str, Any]] = data.get("resource", [])
        self.tasks: list[dict[str, Any]] = data.get("task", [])
        self.options: dict[str, dict[str, Any]] = data.get("option", {})
        self.presets: list[dict[str, Any]] = data.get("preset", [])
        self.global_option: list[str] = data.get("global_option", [])

    def _merge_imports(self, data: dict[str, Any]) -> None:
        for rel in data.get("import", []):
            sub = load_jsonc(self.base_dir / rel)
            for key in ("task", "preset", "setting", "pretask"):
                if key in sub:
                    data.setdefault(key, []).extend(sub[key])
            if "option" in sub:
                data.setdefault("option", {}).update(sub["option"])
            for key in ("global_option", "group"):
                if key not in sub:
                    continue
                target = data.setdefault(key, [])

                def ident(x):
                    return x.get("name") if isinstance(x, dict) else x

                seen = {ident(x) for x in target}
                for item in sub[key]:
                    if ident(item) not in seen:
                        target.append(item)
                        seen.add(ident(item))

    # ---- 基本信息 ----

    @property
    def name(self) -> str:
        return self.data.get("name", "")

    @property
    def version(self) -> str:
        return self.data.get("version", "")

    @property
    def agents(self) -> list[dict[str, Any]]:
        agent = self.data.get("agent")
        if agent is None:
            return []
        return agent if isinstance(agent, list) else [agent]

    def get_controller(self, name: Optional[str] = None) -> dict[str, Any]:
        if not self.controllers:
            raise InterfaceError("interface.json 未定义任何 controller")
        if not name:
            return self.controllers[0]
        for c in self.controllers:
            if c["name"] == name:
                return c
        raise InterfaceError(f"未找到控制器: {name}")

    def get_resource(self, name: Optional[str] = None, controller: Optional[str] = None) -> dict[str, Any]:
        candidates = [r for r in self.resources if controller is None or _available(r, controller, None)]
        if not candidates:
            raise InterfaceError("没有可用的 resource")
        if not name:
            return candidates[0]
        for r in candidates:
            if r["name"] == name:
                return r
        raise InterfaceError(f"未找到可用于当前控制器的资源: {name}")

    def resource_paths(self, resource: dict[str, Any], controller: dict[str, Any]) -> list[Path]:
        paths = list(resource.get("path", [])) + list(controller.get("attach_resource_path", []))
        return [(self.base_dir / p).resolve() for p in paths]

    def get_task(self, name: str) -> dict[str, Any]:
        for t in self.tasks:
            if t["name"] == name:
                return t
        raise InterfaceError(f"未找到任务: {name}")

    def get_preset(self, name: str) -> dict[str, Any]:
        for p in self.presets:
            if p["name"] == name:
                return p
        raise InterfaceError(f"未找到预设: {name}")

    # ---- 对外描述 ----

    def describe(self) -> dict[str, Any]:
        """返回可公开的接口描述（不含密码字段默认值等敏感信息）。"""

        def opt(name: str, o: dict[str, Any]) -> dict[str, Any]:
            d = {
                "name": name,
                "type": o.get("type", "select"),
                "label": o.get("label", name),
                "description": o.get("description", ""),
            }
            for key in ("controller", "resource", "default_case", "min_count", "max_count"):
                if key in o:
                    d[key] = o[key]
            if "cases" in o:
                d["cases"] = [{"name": c["name"], "label": c.get("label", c["name"]), "option": c.get("option", [])} for c in o["cases"]]
            if "inputs" in o:
                d["inputs"] = [
                    {
                        k: v
                        for k, v in i.items()
                        if k in ("name", "label", "description", "default", "input_type", "pipeline_type", "verify", "pattern_msg", "password")
                    }
                    for i in o["inputs"]
                ]
            if "hotkeys" in o:
                d["hotkeys"] = [{k: v for k, v in h.items() if k in ("name", "label", "description", "default")} for h in o["hotkeys"]]
            return d

        return {
            "name": self.name,
            "version": self.version,
            "description": self.data.get("description", ""),
            "controller": [{"name": c["name"], "label": c.get("label", c["name"]), "type": c.get("type")} for c in self.controllers],
            "resource": [{"name": r["name"], "label": r.get("label", r["name"]), "controller": r.get("controller")} for r in self.resources],
            "task": [
                {
                    "name": t["name"],
                    "label": t.get("label", t["name"]),
                    "entry": t["entry"],
                    "default_check": t.get("default_check", False),
                    "option": t.get("option", []),
                    "controller": t.get("controller"),
                    "resource": t.get("resource"),
                }
                for t in self.tasks
            ],
            "option": {k: opt(k, v) for k, v in self.options.items()},
            "global_option": self.global_option,
            "preset": [
                {
                    "name": p["name"],
                    "label": p.get("label", p["name"]),
                    "description": p.get("description", ""),
                    "task": [{"name": t["name"], "enabled": t.get("enabled", True)} for t in p.get("task", [])],
                }
                for p in self.presets
            ],
            "agent": bool(self.agents),
        }

    # ---- 运行参数 ----

    def expand_preset(self, name: str) -> list[dict[str, Any]]:
        preset = self.get_preset(name)
        return [
            {"name": t["name"], "option": copy.deepcopy(t.get("option", {}))}
            for t in preset.get("task", [])
            if t.get("enabled", True)
        ]

    def build_task(
        self,
        task_name: str,
        option_values: Optional[dict[str, Any]] = None,
        controller: Optional[str] = None,
        resource: Optional[str] = None,
    ) -> tuple[str, dict[str, Any]]:
        """返回 (entry, pipeline_override)。

        合并顺序：task.pipeline_override → global_option → resource.option
        → controller.option → task.option，后者覆盖前者。
        """
        task = self.get_task(task_name)
        if not _available(task, controller, resource):
            raise InterfaceError(f"任务 {task_name} 不适用于控制器 {controller} / 资源 {resource}")

        values = option_values or {}
        if not isinstance(values, dict):
            raise InterfaceError("option 必须是对象：{选项名: 取值}")

        override: dict[str, Any] = {}
        merge_override(override, task.get("pipeline_override", {}))

        groups: list[list[str]] = [list(self.global_option)]
        if resource is not None:
            groups.append(list(self.get_resource(resource).get("option", [])))
        if controller is not None:
            groups.append(list(self.get_controller(controller).get("option", [])))
        groups.append(list(task.get("option", [])))

        known = self._reachable_options([n for g in groups for n in g])
        unknown = set(values) - known
        if unknown:
            raise InterfaceError(f"任务 {task_name} 不支持选项: {', '.join(sorted(unknown))}")

        for group in groups:
            for opt_name in group:
                self._apply_option(opt_name, values, controller, resource, override, depth=0)

        return task["entry"], override

    def _reachable_options(self, roots: list[str]) -> set[str]:
        """返回从 roots 出发可达的全部选项名（含所有 case 的子选项）。"""
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            for case in self.options.get(name, {}).get("cases", []):
                stack.extend(case.get("option", []))
        return seen

    def _apply_option(
        self,
        name: str,
        values: dict[str, Any],
        controller: Optional[str],
        resource: Optional[str],
        override: dict[str, Any],
        depth: int,
    ) -> None:
        if depth > 32:
            raise InterfaceError(f"选项嵌套过深: {name}")
        opt = self.options.get(name)
        if opt is None:
            raise InterfaceError(f"interface.json 中未定义选项: {name}")
        if not _available(opt, controller, resource):
            return

        opt_type = opt.get("type", "select")
        value = values.get(name)

        if opt_type in ("select", "switch", "checkbox"):
            cases = opt.get("cases", [])
            by_name = {c["name"]: c for c in cases}
            selected = self._select_cases(name, opt_type, opt, cases, value)
            for case_name in [c["name"] for c in cases if c["name"] in selected]:
                case = by_name[case_name]
                merge_override(override, case.get("pipeline_override", {}))
                for sub in case.get("option", []):
                    self._apply_option(sub, values, controller, resource, override, depth + 1)

        elif opt_type == "input":
            given = value or {}
            if not isinstance(given, dict):
                raise InterfaceError(f"选项 {name} 为 input 类型，取值应为 {{字段名: 值}}")
            subst = {}
            for item in opt.get("inputs", []):
                field = item["name"]
                raw = given.get(field, item.get("default"))
                if raw is None:
                    raise InterfaceError(f"选项 {name} 缺少输入字段: {field}")
                raw = str(raw)
                verify = item.get("verify")
                if verify and not re.search(verify, raw):
                    msg = item.get("pattern_msg") or f"不匹配 {verify}"
                    raise InterfaceError(f"选项 {name} 的字段 {field} 校验失败: {msg}")
                subst[field] = _convert_input(raw, item.get("pipeline_type"), field)
            merge_override(override, _substitute(opt.get("pipeline_override", {}), subst))

        elif opt_type == "hotkey":
            given = value or {}
            if not isinstance(given, dict):
                raise InterfaceError(f"选项 {name} 为 hotkey 类型，取值应为 {{字段名: 快捷键}}")
            subst = {}
            for item in opt.get("hotkeys", []):
                field = item["name"]
                raw = given.get(field, item.get("default"))
                if raw is None:
                    raise InterfaceError(f"选项 {name} 缺少快捷键字段: {field}")
                keys = _parse_hotkey(raw)
                subst[field] = keys["primary"]
                for k, v in keys.items():
                    subst[f"{field}.{k}"] = v
            merge_override(override, _substitute(opt.get("pipeline_override", {}), subst))

        else:
            raise InterfaceError(f"选项 {name} 的类型 {opt_type} 不受支持")

    @staticmethod
    def _select_cases(name: str, opt_type: str, opt: dict[str, Any], cases: list[dict[str, Any]], value: Any) -> set[str]:
        names = [c["name"] for c in cases]
        if opt_type == "checkbox":
            if value is None:
                value = opt.get("default_case", [])
            if not isinstance(value, list):
                raise InterfaceError(f"选项 {name} 为 checkbox 类型，取值应为 case 名数组")
            bad = [v for v in value if v not in names]
            if bad:
                raise InterfaceError(f"选项 {name} 没有这些 case: {bad}")
            if len(value) < opt.get("min_count", 0):
                raise InterfaceError(f"选项 {name} 至少选择 {opt['min_count']} 项")
            if "max_count" in opt and len(value) > opt["max_count"]:
                raise InterfaceError(f"选项 {name} 最多选择 {opt['max_count']} 项")
            return set(value)

        if value is None:
            value = opt.get("default_case") or (names[0] if names else None)
        if opt_type == "switch" and isinstance(value, bool):
            group = SWITCH_YES if value else SWITCH_NO
            value = next((n for n in names if n in group), value)
        if value not in names:
            raise InterfaceError(f"选项 {name} 没有 case: {value}，可选: {names}")
        return {value}
