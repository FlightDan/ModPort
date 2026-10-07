from pathlib import Path
import tempfile
import unittest

from modport.contracts import OperationInput
from modport.handlers import _snapshot_stage_output


class OutputSnapshotTests(unittest.TestCase):
    def test_same_basename_outputs_keep_distinct_task_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            command = OperationInput("run", "gap_research", "gap_research", "run:gap_research:1", str(root))
            files = {
                ".modport/gap-research/report.json": '{"gap_findings": []}',
                ".modport/gap-research/sources/report.json": '{"source": "upstream documentation"}',
                ".modport/evidence/a/result.json": '{"test_id": "a"}',
                ".modport/evidence/b/result.json": '{"test_id": "b"}',
            }
            refs = {}
            for relative, content in files.items():
                path = baseline / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                _, refs[relative] = _snapshot_stage_output(root, baseline, command, relative)
            self.assertEqual(len({ref["path"] for ref in refs.values()}), len(files))
            for relative, ref in refs.items():
                self.assertEqual((root / ref["path"]).read_text(), files[relative])
