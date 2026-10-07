"""CLI language selection, display, and machine-output boundaries."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import cli, cli_i18n


class CliI18nTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.preference = Path(temporary.name) / "cli-language.json"
        self.preference_patch = patch("modport.cli_i18n.preference_path", return_value=self.preference)
        self.preference_patch.start()
        self.addCleanup(self.preference_patch.stop)

    def help_output(self, arguments):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(arguments)
        self.assertEqual(raised.exception.code, 0)
        return stdout.getvalue()

    def test_language_flag_localizes_root_nested_and_subcommand_help(self):
        root = self.help_output(["--lang", "zh-CN", "--help"])
        run = self.help_output(["run", "--lang", "zh-CN", "--help"])
        nested = self.help_output(["dependency-cache", "fetch", "--lang", "zh", "--help"])

        for output in (root, run, nested):
            self.assertIn("用法：", output)
            self.assertIn("显示此帮助信息并退出", output)
            self.assertNotIn("usage:", output)
        self.assertIn("使用 Dispatcher SDK 将 Forge 模组迁移到 NeoForge", root)
        self.assertIn("为本次 Run 固定使用的模型 JSON 文件", run)
        self.assertIn("来源证明 URL", nested)

    def test_parser_errors_are_translated_and_keep_argparse_exit_code(self):
        stderr = io.StringIO()
        arguments = [
            "run", "--lang", "zh-CN", "--mod-id", "sample",
            "--source-repository", "https://example.invalid/sample",
            "--source-revision", "v1", "--source-minecraft", "1.20.1",
            "--target-minecraft", "1.21.1", "--validation-scope", "unknown",
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli.main(arguments)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("错误：", stderr.getvalue())
        self.assertIn("无效选项：", stderr.getvalue())
        self.assertIn("用法：", stderr.getvalue())

    def test_explicit_choice_is_remembered_and_json_output_is_unchanged(self):
        machine_value = {"schema": 1, "model": "example", "enabled": True}
        stdout = io.StringIO()
        with patch("modport.cli.load_model_config", return_value=machine_value), redirect_stdout(stdout):
            self.assertEqual(cli.main(["models", "show", "--lang", "zh"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), machine_value)
        self.assertEqual(cli_i18n.load_preference(), "zh-CN")

        subsequent_help = self.help_output(["--help"])
        self.assertIn("用法：", subsequent_help)

    def test_system_locale_selection_falls_back_for_unsupported_locale(self):
        with patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}, clear=True):
            self.assertEqual(cli_i18n.system_language(), "zh-CN")
        with patch.dict(os.environ, {"LC_ALL": "C", "LANG": "zh_CN.UTF-8"}, clear=True):
            self.assertEqual(cli_i18n.system_language(), "en")
        with patch.dict(os.environ, {"LANG": "fr_FR.UTF-8"}, clear=True):
            self.assertEqual(cli_i18n.system_language(), "en")
        for windows_name in ("Chinese_China", "Chinese (Simplified)_China"):
            with patch.dict(os.environ, {}, clear=True), patch(
                    "modport.cli_i18n.locale.getlocale", return_value=(windows_name, "936")):
                self.assertEqual(cli_i18n.system_language(), "zh-CN")
        with patch.dict(os.environ, {}, clear=True), patch(
                "modport.cli_i18n.locale.getlocale", return_value=("Chinese (Traditional)_Taiwan", "950")):
            self.assertEqual(cli_i18n.system_language(), "zh-CN")

    def test_program_validation_is_translated_but_raw_exception_text_is_preserved(self):
        base_arguments = [
            "run", "--lang", "zh-CN", "--mod-id", "sample",
            "--source-repository", "https://example.invalid/sample",
            "--source-revision", "v1", "--source-minecraft", "1.20.1",
            "--target-minecraft", "1.21.1",
        ]
        stderr = io.StringIO()
        with patch("modport.cli.MigrationOperations"), redirect_stderr(stderr):
            self.assertEqual(cli.main(base_arguments + ["--verify-artifact"]), 1)
        self.assertIn("run --verify-artifact 需要 --handoff", stderr.getvalue())

        stderr = io.StringIO()
        with patch("modport.cli.MigrationOperations") as operations, redirect_stderr(stderr):
            operations.return_value.run.side_effect = ValueError("SDK error: invalid execution identity")
            self.assertEqual(cli.main(base_arguments), 1)
        self.assertIn("SDK error: invalid execution identity", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
