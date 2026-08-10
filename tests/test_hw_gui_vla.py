from pathlib import Path
import sys
import tempfile
import unittest

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "androidworld"))

from eval.benchmarks.hw_gui_vla import load_tasks  # noqa: E402


class HwGuiVlaLoaderTest(unittest.TestCase):
    def test_exclusions_primary_flag_and_step_budget(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "cases"
        sheet.append(["用例编号", "任务", "涉及APP", "预期步数", "是否统计"])
        sheet.append(["A-1", "播放视频", "腾讯视频", 5, None])
        sheet.append(["A-2", "诊断任务", "QQ音乐", 4, "N"])
        sheet.append(["A-3", "排除任务", "中国联通", 3, None])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.xlsx"
            workbook.save(path)
            tasks = load_tasks(path)

        self.assertEqual([task.case_id for task in tasks], ["A-1", "A-2"])
        self.assertEqual(tasks[0].max_steps, 7)
        self.assertTrue(tasks[0].count_in_primary_metric)
        self.assertFalse(tasks[1].count_in_primary_metric)

    def test_unknown_app_is_rejected(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["用例编号", "任务", "涉及APP", "预期步数", "是否统计"])
        sheet.append(["A-1", "任务", "未知应用", 2, None])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.xlsx"
            workbook.save(path)
            with self.assertRaisesRegex(ValueError, "No package mapping"):
                load_tasks(path)


if __name__ == "__main__":
    unittest.main()
