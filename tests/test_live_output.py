import unittest

import importlib.util
from pathlib import Path
import sys

module_path = Path(__file__).resolve().parents[1] / "core/client/output/live_output.py"
spec = importlib.util.spec_from_file_location("live_output", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
LiveOutputSession, edit_delta = module.LiveOutputSession, module.edit_delta
bounded_edit = module.bounded_edit


class LiveOutputTests(unittest.TestCase):
    def test_append_and_revision(self):
        self.assertEqual(edit_delta("hello", "hello world"), (0, " world"))
        self.assertEqual(edit_delta("我喜欢你", "我喜欢迎"), (1, "迎"))
        self.assertEqual(edit_delta("same", "same"), (0, ""))

    def test_updates_only_same_window(self):
        events = []
        active = [10]
        session = LiveOutputSession("task", lambda value: events.append(("write", value)),
                                    lambda: events.append(("backspace", None)),
                                    lambda: active[0])
        self.assertTrue(session.update("早上号"))
        self.assertTrue(session.update("早上好"))
        active[0] = 11
        self.assertFalse(session.update("早上好。"))
        active[0] = 10
        self.assertFalse(session.update("早上好。"))
        self.assertEqual(events, [("write", "早上号"), ("backspace", None), ("write", "好")])

    def test_final_formatting_does_not_erase_whole_utterance(self):
        old = "请放到C盘下载目录，继续使用客户端，而且后面还有很多已经输入的文字。"
        new = "请放到 C 盘下载目录，继续使用客户端，而且后面还有很多已经输入的文字。后面还有一句。"
        remove, append, limited = bounded_edit(old, new)
        self.assertTrue(limited)
        self.assertLessEqual(remove, 16)
        self.assertEqual(append, "后面还有一句。")

    def test_final_omission_does_not_delete_spoken_tail(self):
        old = "已经输入的内容很多，最后还有我说出的这句话。"
        new = "已经输入的内容很多。"
        remove, append, limited = bounded_edit(old, new, final=True)
        self.assertEqual((remove, append, limited), (0, "", True))

    def test_large_rewrite_preserves_existing_text(self):
        events = []
        session = LiveOutputSession("task", events.append, lambda: events.append("delete"), lambda: 7)
        self.assertTrue(session.update("x" * 201))
        self.assertTrue(session.update("y"))
        self.assertTrue(session.revision_skipped)
        self.assertEqual(events, ["x" * 201])


if __name__ == "__main__":
    unittest.main()
