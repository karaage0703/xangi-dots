#!/usr/bin/env python3
"""Build portable skill archives without credentials or runtime state."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    for name in ("xangi-dots", "dots-xangi"):
        source = root / "skills" / name / "SKILL.md"
        target = args.output / (name + ".zip")
        with ZipFile(target, "x", compression=ZIP_DEFLATED) as archive:
            archive.write(source, name + "/SKILL.md")
            if name == "xangi-dots":
                archive.write(source.parent / "README.md", name + "/README.md")
        print(target)


if __name__ == "__main__":
    main()
