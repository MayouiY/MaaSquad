import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pi import InterfaceError, ProjectInterface, strip_jsonc  # noqa: E402

INTERFACE = """
{
    // 注释
    "interface_version": 2,
    "name": "Demo",
    "version": "1.2.3",
    "controller": [
        {"name": "安卓", "type": "Adb", "option": ["控制器选项"]},
        {"name": "桌面", "type": "Win32"}
    ],
    "resource": [
        {"name": "官服", "path": ["./resource"]},
        {"name": "桌面资源", "path": ["./resource"], "controller": ["桌面"]}
    ],
    "global_option": ["全局"],
    "import": ["extra.json"],
    "task": [
        {"name": "A", "entry": "EntryA", "pipeline_override": {"EntryA": {"timeout": 1, "next": ["X"]}}, "option": ["难度", "开关", "多选", "输入"]},
        {"name": "仅桌面", "entry": "EntryB", "controller": ["桌面"]},
    ],
    "option": {
        "全局": {"cases": [{"name": "开", "pipeline_override": {"EntryA": {"timeout": 2, "g": true}}}]},
        "控制器选项": {"cases": [{"name": "c", "pipeline_override": {"EntryA": {"c": 1}}}]},
        "难度": {
            "type": "select",
            "default_case": "普通",
            "cases": [
                {"name": "普通", "pipeline_override": {"EntryA": {"timeout": 3}}},
                {"name": "困难", "option": ["关卡"], "pipeline_override": {"EntryA": {"timeout": 4}}}
            ]
        },
        "关卡": {"type": "select", "cases": [{"name": "1-1", "pipeline_override": {"Stage": {"text": "1-1"}}}, {"name": "1-2"}]},
        "开关": {"type": "switch", "cases": [{"name": "Yes", "pipeline_override": {"S": {"enabled": true}}}, {"name": "No", "pipeline_override": {"S": {"enabled": false}}}]},
        "多选": {"type": "checkbox", "max_count": 2, "cases": [{"name": "甲", "pipeline_override": {"M": {"v": "甲", "a": 1}}}, {"name": "乙", "pipeline_override": {"M": {"v": "乙", "b": 1}}}, {"name": "丙"}]},
        "输入": {
            "type": "input",
            "inputs": [
                {"name": "章节", "default": "4", "pipeline_type": "string", "verify": "^\\\\d+$"},
                {"name": "超时", "default": "20000", "pipeline_type": "int"},
                {"name": "启用", "default": "true", "pipeline_type": "bool"}
            ],
            "pipeline_override": {"Input": {"next": "Chapter_{章节}", "timeout": "{超时}", "enabled": "{启用}", "list": ["{章节}"]}}
        },
    },
    "preset": [
        {"name": "日常", "task": [{"name": "A", "option": {"难度": "困难"}}, {"name": "仅桌面", "enabled": false}]}
    ],
}
"""

EXTRA = {
    "task": [{"name": "导入任务", "entry": "Imported", "option": ["快捷键"]}],
    "option": {
        "快捷键": {
            "type": "hotkey",
            "hotkeys": [{"name": "技能", "default": "E"}],
            "pipeline_override": {"Key": {"key": "{技能}", "mod": "{技能.modifier1}"}},
        }
    },
}


class ProjectInterfaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "interface.json").write_text(INTERFACE, encoding="utf-8")
        (base / "extra.json").write_text(json.dumps(EXTRA, ensure_ascii=False), encoding="utf-8")
        self.pi = ProjectInterface(base / "interface.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_strip_jsonc_keeps_strings(self):
        text = '{"url": "http://a//b", /* c */ "s": "x/*y*/", "l": [1, 2,],}'
        self.assertEqual(json.loads(strip_jsonc(text)), {"url": "http://a//b", "s": "x/*y*/", "l": [1, 2]})

    def test_defaults_and_merge_order(self):
        entry, override = self.pi.build_task("A", {}, "安卓", "官服")
        self.assertEqual(entry, "EntryA")
        # task.pipeline_override < global_option < controller.option < task.option
        self.assertEqual(override["EntryA"], {"timeout": 3, "next": ["X"], "g": True, "c": 1})
        self.assertEqual(override["S"], {"enabled": True})  # switch 默认取第一个 case
        self.assertNotIn("M", override)
        self.assertEqual(
            override["Input"],
            {"next": "Chapter_4", "timeout": 20000, "enabled": True, "list": ["4"]},
        )

    def test_nested_option_and_switch_bool(self):
        _, override = self.pi.build_task("A", {"难度": "困难", "关卡": "1-1", "开关": False})
        self.assertEqual(override["EntryA"]["timeout"], 4)
        self.assertEqual(override["Stage"], {"text": "1-1"})
        self.assertEqual(override["S"], {"enabled": False})

    def test_unselected_sub_option_value_is_ignored(self):
        _, override = self.pi.build_task("A", {"难度": "普通", "关卡": "1-1"})
        self.assertNotIn("Stage", override)

    def test_checkbox_merges_in_definition_order(self):
        _, override = self.pi.build_task("A", {"多选": ["乙", "甲"]})
        self.assertEqual(override["M"], {"v": "乙", "a": 1, "b": 1})
        with self.assertRaises(InterfaceError):
            self.pi.build_task("A", {"多选": ["甲", "乙", "丙"]})

    def test_input_validation(self):
        with self.assertRaises(InterfaceError):
            self.pi.build_task("A", {"输入": {"章节": "abc"}})
        with self.assertRaises(InterfaceError):
            self.pi.build_task("A", {"输入": {"超时": "x"}})

    def test_errors(self):
        with self.assertRaises(InterfaceError):
            self.pi.build_task("不存在")
        with self.assertRaises(InterfaceError):
            self.pi.build_task("A", {"不存在的选项": 1})
        with self.assertRaises(InterfaceError):
            self.pi.build_task("A", {"难度": "地狱"})
        with self.assertRaises(InterfaceError):
            self.pi.build_task("仅桌面", {}, "安卓", "官服")
        self.assertEqual(self.pi.build_task("仅桌面", {}, "桌面", "桌面资源")[0], "EntryB")

    def test_resource_filter(self):
        self.assertEqual(self.pi.get_resource(None, "安卓")["name"], "官服")
        with self.assertRaises(InterfaceError):
            self.pi.get_resource("桌面资源", "安卓")

    def test_import_and_hotkey(self):
        self.assertIn("导入任务", [t["name"] for t in self.pi.tasks])
        _, override = self.pi.build_task("导入任务", {})
        self.assertEqual(override["Key"]["key"], ord("E"))
        _, override = self.pi.build_task("导入任务", {"快捷键": {"技能": "Ctrl+F1"}})
        self.assertEqual(override["Key"], {"key": 0x70, "mod": 0x11})
        _, override = self.pi.build_task("导入任务", {"快捷键": {"技能": 65}})
        self.assertEqual(override["Key"]["key"], 65)

    def test_preset(self):
        items = self.pi.expand_preset("日常")
        self.assertEqual(items, [{"name": "A", "option": {"难度": "困难"}}])

    def test_describe(self):
        desc = self.pi.describe()
        self.assertEqual(desc["name"], "Demo")
        self.assertEqual([t["name"] for t in desc["task"]], ["A", "仅桌面", "导入任务"])
        self.assertEqual(desc["option"]["输入"]["type"], "input")


if __name__ == "__main__":
    unittest.main()
