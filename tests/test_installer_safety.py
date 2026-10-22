import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cppcheck-installer-safety-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.tool = self.folder / "install/cpp_check"
        self.tool.mkdir(parents=True)
        shutil.copy2(ROOT / "install/cpp_check/install.sh", self.tool / "install.sh")

        self.bin_dir = self.folder / "bin"
        self.bin_dir.mkdir()
        self.log = self.folder / "calls.jsonl"
        self._write_stub("fixture-cxx", "raise SystemExit(0)\n")
        self._write_stub("cmake", self._cmake_stub())
        self._write_stub("make", self._make_stub())

        self.prefix = self.folder / "prefix"
        self.source = self.folder / "external-source"
        self.source.mkdir()
        (self.source / "Makefile").write_text("fixture makefile\n")
        self.environment = dict(os.environ)
        self.environment.update({
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(self.folder / "home"),
            "FIXTURE_LOG": str(self.log),
            "CPPCHECK_BUILD_JOBS": "1",
            "CPPCHECK_INSTALL_PREFIX": str(self.prefix),
            "CPPCHECK_SOURCE_DIR": str(self.source),
            "CXX": "fixture-cxx",
            "PAGER": "cat",
            "NO_COLOR": "1",
            "CI": "true",
            "TERM": "dumb",
        })

    def _write_stub(self, name, body):
        path = self.bin_dir / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(0o755)

    @staticmethod
    def _make_stub():
        return '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FIXTURE_LOG"], "a") as stream:
    stream.write(json.dumps(["make", *args]) + "\\n")
source = Path(args[args.index("-C") + 1])
if "clean" in args:
    for artifact in source.rglob("*.o"):
        artifact.unlink()
elif "install" in args:
    prefix = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("PREFIX=")))
    binary = prefix / "bin/cppcheck"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\\necho Cppcheck-fixture\\n")
    binary.chmod(0o755)
else:
    (source / "build-output.o").write_text("fixture build output")
'''

    @staticmethod
    def _cmake_stub():
        return '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FIXTURE_LOG"], "a") as stream:
    stream.write(json.dumps(["cmake", *args]) + "\\n")
if args[0] == "-S":
    build = Path(args[args.index("-B") + 1])
    prefix = next(arg.split("=", 1)[1] for arg in args if arg.startswith("-DCMAKE_INSTALL_PREFIX="))
    build.mkdir(parents=True, exist_ok=True)
    (build / "fixture-prefix").write_text(prefix)
    (build / "cmake-output.o").write_text("fixture build output")
elif args[0] == "--build":
    (Path(args[1]) / "cmake-built.o").write_text("fixture build output")
elif args[0] == "--install":
    build = Path(args[1])
    prefix = Path((build / "fixture-prefix").read_text())
    binary = prefix / "bin/cppcheck"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\\necho Cppcheck-fixture\\n")
    binary.chmod(0o755)
'''

    def run_installer(self, **extra_environment):
        environment = dict(self.environment, **extra_environment)
        completed = subprocess.run(
            ["bash", str(self.tool / "install.sh")],
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue((self.prefix / "bin/cppcheck").is_file())
        return completed

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def write_source_archive(self):
        source_root = self.tool / "source"
        source_root.mkdir(exist_ok=True)
        archive = source_root / "cppcheck-source.tar.xz"
        with tarfile.open(archive, "w:xz") as bundle:
            payload = b"fixture source\n"
            info = tarfile.TarInfo("cppcheck/Makefile")
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (source_root / "cppcheck-source.tar.xz.sha256").write_text(
            f"{digest}  cppcheck-source.tar.xz\n"
        )

    def test_custom_make_source_is_never_cleaned(self):
        sentinel = self.source / "keep.o"
        sentinel.write_text("user data")

        self.run_installer(CPPCHECK_BUILD_SYSTEM="make")

        self.assertEqual(sentinel.read_text(), "user data")
        self.assertTrue((self.source / "build-output.o").exists())
        self.assertFalse(any("clean" in call for call in self.calls() if call[0] == "make"))

    def test_fresh_default_source_still_cleans_build_artifacts(self):
        self.write_source_archive()
        self.environment.pop("CPPCHECK_SOURCE_DIR")

        self.run_installer(CPPCHECK_BUILD_SYSTEM="make")

        extracted = self.tool / "tmp/cppcheck-source/cppcheck"
        self.assertTrue((extracted / "Makefile").is_file())
        self.assertFalse(list(extracted.rglob("*.o")))
        self.assertTrue(any("clean" in call for call in self.calls() if call[0] == "make"))

    def test_incomplete_preexisting_default_source_is_not_cleaned(self):
        self.write_source_archive()
        existing_source = self.tool / "tmp/cppcheck-source/cppcheck"
        existing_source.mkdir(parents=True)
        sentinel = existing_source / "pre-existing.o"
        sentinel.write_text("user data")
        self.environment.pop("CPPCHECK_SOURCE_DIR")

        self.run_installer(CPPCHECK_BUILD_SYSTEM="make")

        self.assertEqual(sentinel.read_text(), "user data")
        self.assertTrue((existing_source / "build-output.o").exists())
        self.assertFalse(any("clean" in call for call in self.calls() if call[0] == "make"))

    def test_custom_cmake_build_directory_is_retained(self):
        build = self.folder / "shared-build-cache"
        build.mkdir()
        sentinel = build / "keep.txt"
        sentinel.write_text("user data")

        self.run_installer(CPPCHECK_BUILD_SYSTEM="cmake", CPPCHECK_CMAKE_BUILD_DIR=str(build))

        self.assertEqual(sentinel.read_text(), "user data")
        self.assertTrue((build / "cmake-output.o").exists())

    def test_fresh_default_cmake_build_directory_is_cleaned(self):
        build = self.tool / "tmp/cppcheck-cmake-build"

        self.run_installer(CPPCHECK_BUILD_SYSTEM="cmake")

        self.assertFalse(build.exists())

    def test_existing_default_cmake_build_directory_is_retained(self):
        build = self.tool / "tmp/cppcheck-cmake-build"
        build.mkdir(parents=True)
        sentinel = build / "keep.txt"
        sentinel.write_text("pre-existing data")

        self.run_installer(CPPCHECK_BUILD_SYSTEM="cmake")

        self.assertEqual(sentinel.read_text(), "pre-existing data")
        self.assertTrue((build / "cmake-output.o").exists())

    def test_symlinked_default_cmake_build_directory_is_retained(self):
        target = self.folder / "home-analogue/build-cache"
        target.mkdir(parents=True)
        sentinel = target / "keep.txt"
        sentinel.write_text("user data")
        build = self.tool / "tmp/cppcheck-cmake-build"
        build.parent.mkdir(parents=True)
        build.symlink_to(target, target_is_directory=True)

        self.run_installer(CPPCHECK_BUILD_SYSTEM="cmake")

        self.assertTrue(build.is_symlink())
        self.assertEqual(sentinel.read_text(), "user data")
        self.assertTrue((target / "cmake-output.o").exists())


if __name__ == "__main__":
    unittest.main()
