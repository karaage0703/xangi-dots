"""Check the distributable ZIP layout and exact source contents."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


class SkillArchiveTests(unittest.TestCase):
    def test_archives_contain_only_the_expected_skill_files(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [
                sys.executable,
                str(ROOT / "scripts/package-skills.py"),
                "--output",
                directory,
            ]
            subprocess.run(command, check=True, capture_output=True)
            expected = {
                "dots-xangi": ["SKILL.md"],
                "xangi-dots": ["SKILL.md", "README.md"],
            }
            originals = {}
            for name, files in expected.items():
                path = Path(directory) / f"{name}.zip"
                originals[path] = path.read_bytes()
                with ZipFile(path) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(set(archive.namelist()), {f"{name}/{file}" for file in files})
                    for file in files:
                        self.assertEqual(
                            archive.read(f"{name}/{file}"),
                            (ROOT / "skills" / name / file).read_bytes(),
                        )
            # A mistaken reuse of the output directory must not replace a published ZIP.
            retry = subprocess.run(command, capture_output=True)
            self.assertNotEqual(retry.returncode, 0)
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
