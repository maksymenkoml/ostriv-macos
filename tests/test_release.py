import hashlib
import io
import os
import stat
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from scripts.build_release import ASSET_NAME, build_release, safe_extract


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_FILES = {
    "LICENSE",
    "README.md",
    "assets/settings.data",
    "ostriv_macos/__init__.py",
    "ostriv_macos/cli.py",
    "ostriv_macos/diagnostics.py",
    "ostriv_macos/discovery.py",
    "ostriv_macos/installer.py",
    "ostriv_macos/launcher.py",
    "ostriv_macos/launcher_runtime.py",
    "ostriv_macos/payload.py",
    "patch.py",
    "payload-manifest.json",
    "prebuilt/README.md",
    "prebuilt/dxil.dll",
    "prebuilt/libgallium_wgl.dll",
    "prebuilt/libwinpthread-1.dll",
    "prebuilt/opengl32.dll",
}
EXPECTED_PAYLOAD = {
    "assets/settings.data": (
        1332,
        "08bdbb1bb0aacdbc2d4aff6d0a22d653af90516ab151cd4e3ec60847e2efd8e1",
    ),
    "prebuilt/dxil.dll": (
        1503072,
        "cbcfe883a09fd0ca1f98abdf3a9553b560895e3283a136da82a8381253a169df",
    ),
    "prebuilt/libgallium_wgl.dll": (
        45716642,
        "816817bc20216fa07840dbba86ef25ec1715f6899df635f1f1aec9af209cf8b3",
    ),
    "prebuilt/libwinpthread-1.dll": (
        343646,
        "1bb16e85f19c34629364de7407b3531201e787d803df0db6e46d01d2e8a277ac",
    ),
    "prebuilt/opengl32.dll": (
        539467,
        "3b5c1e578c8b41dab765fcc90e5981917a8ea22105dee67d410633b3f5af2c3f",
    ),
}


class ReleaseArtifactTests(unittest.TestCase):
    def test_built_zip_has_exact_hydrated_inventory_and_preflights_gitless(self):
        # Catches an omitted/extra player file, an LFS pointer, corrupt payload,
        # unsafe extraction, or a release that only works beside the Git checkout.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / ASSET_NAME
            build_release(archive)
            unpacked = root / "unpacked"

            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(sorted(EXPECTED_FILES), bundle.namelist())
                for name, (expected_size, expected_hash) in EXPECTED_PAYLOAD.items():
                    content = bundle.read(name)
                    self.assertEqual(expected_size, len(content), name)
                    self.assertEqual(
                        expected_hash,
                        hashlib.sha256(content).hexdigest(),
                        name,
                    )
                patch_info = bundle.getinfo("patch.py")
                self.assertNotEqual(0, (patch_info.external_attr >> 16) & 0o111)
                patch_source = bundle.read("patch.py").decode("utf-8")
                self.assertLessEqual(len(patch_source.splitlines()), 15)
                self.assertIn("from ostriv_macos.cli import main", patch_source)
                safe_extract(bundle, unpacked)

            self.assertFalse((unpacked / ".git").exists())
            self.assertTrue(os.access(unpacked / "patch.py", os.X_OK))
            environment = {
                "HOME": str(root / "empty-home"),
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            result = subprocess.run(
                ["python3", "patch.py", "--preflight"],
                cwd=unpacked,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stdout)
            self.assertEqual("", result.stderr)

    def test_builder_rejects_any_asset_name_other_than_the_public_contract(self):
        # Catches accidentally publishing a differently named artifact that the
        # stable README download URL cannot resolve.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "player.zip"
            with self.assertRaises(ValueError):
                build_release(output)
            self.assertFalse(output.exists())

    def test_failed_build_does_not_replace_an_existing_asset(self):
        # Catches writing directly to the final path before staging validation.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / ASSET_NAME
            output.write_bytes(b"known-good-existing-asset")
            with mock.patch(
                "scripts.build_release.validate_payload",
                side_effect=RuntimeError("staged validation failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "staged validation failed"):
                    build_release(output)
            self.assertEqual(b"known-good-existing-asset", output.read_bytes())

    def test_hyphenated_command_is_a_thin_executable_wrapper(self):
        # Catches the documented CLI drifting into a second builder implementation.
        wrapper = REPOSITORY_ROOT / "scripts/build-release.py"
        self.assertTrue(os.access(wrapper, os.X_OK))
        source = wrapper.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 10)
        self.assertIn("from scripts.build_release import main", source)
        self.assertNotIn("def build_release", source)
        result = subprocess.run(
            ["python3", str(wrapper), "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--output", result.stdout)


class SafeExtractTests(unittest.TestCase):
    def _write_archive(self, archive: Path, members):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive, "w") as bundle:
                for info, content in members:
                    bundle.writestr(info, content)

    @staticmethod
    def _member(
        name,
        *,
        size=0,
        compressed_size=0,
        compression=zipfile.ZIP_STORED,
        flags=0,
    ):
        info = zipfile.ZipInfo("placeholder")
        info.filename = name
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.file_size = size
        info.compress_size = compressed_size
        info.compress_type = compression
        info.flag_bits = flags
        return info

    def test_rejects_hostile_members_before_extracting_anything(self):
        # Each mutation would otherwise escape, alias, or materialize a non-file.
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        fifo = zipfile.ZipInfo("pipe")
        fifo.create_system = 3
        fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
        hostile = {
            "absolute POSIX path": [("/tmp/escape", b"bad")],
            "absolute Windows path": [("C:/escape", b"bad")],
            "parent traversal": [("../escape", b"bad")],
            "backslash traversal": [("..\\escape", b"bad")],
            "symlink": [(symlink, b"elsewhere")],
            "special file": [(fifo, b"")],
            "duplicate": [("same", b"first"), ("same", b"second")],
        }
        for label, members in hostile.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "hostile.zip"
                self._write_archive(archive, members)
                destination = root / "destination"
                with zipfile.ZipFile(archive) as bundle:
                    with self.assertRaises(ValueError):
                        safe_extract(bundle, destination)
                self.assertFalse(destination.exists())

    def test_rejects_resource_abusive_inventory_without_touching_destination(self):
        # Removing any central-directory resource guard would let hostile metadata
        # reach extraction or alter a pre-existing player directory.
        class InventoryBundle:
            def __init__(self, members):
                self.members = members

            def infolist(self):
                return self.members

            def open(self, *_args, **_kwargs):
                raise AssertionError("invalid inventory reached streaming")

        cases = {
            "encrypted": [self._member("file", size=1, compressed_size=1, flags=1)],
            "unsupported compression": [
                self._member(
                    "file",
                    size=1,
                    compressed_size=1,
                    compression=zipfile.ZIP_BZIP2,
                )
            ],
            "too many entries": [
                self._member("file-{:02d}".format(index)) for index in range(65)
            ],
            "path too long": [self._member("a/" * 256 + "b")],
            "component too long": [self._member("a" * 256)],
            "NUL path": [self._member("bad\x00name")],
            "surrogate path": [self._member("bad\ud800name")],
            "member too large": [
                self._member(
                    "large",
                    size=64 * 1024 * 1024 + 1,
                    compressed_size=64 * 1024 * 1024 + 1,
                )
            ],
            "total too large": [
                self._member(
                    "large-a",
                    size=50 * 1024 * 1024,
                    compressed_size=50 * 1024 * 1024,
                ),
                self._member(
                    "large-b",
                    size=50 * 1024 * 1024,
                    compressed_size=50 * 1024 * 1024,
                ),
            ],
            "compression ratio": [
                self._member(
                    "ratio",
                    size=1_000_000,
                    compressed_size=1,
                    compression=zipfile.ZIP_DEFLATED,
                )
            ],
            "zero compressed size": [
                self._member(
                    "impossible",
                    size=1,
                    compressed_size=0,
                    compression=zipfile.ZIP_DEFLATED,
                )
            ],
        }
        for label, members in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "destination"
                destination.mkdir()
                sentinel = destination / "sentinel"
                sentinel.write_bytes(b"keep")
                with self.assertRaises(ValueError):
                    safe_extract(InventoryBundle(members), destination)
                self.assertEqual(b"keep", sentinel.read_bytes())
                self.assertEqual(["sentinel"], [path.name for path in destination.iterdir()])

    def test_declared_actual_size_breach_leaves_no_partial_destination(self):
        # A stream that exceeds its declared size must not publish bytes already read.
        member = self._member("file", size=1, compressed_size=1)

        class MismatchedBundle:
            def infolist(self):
                return [member]

            def open(self, *_args, **_kwargs):
                return io.BytesIO(b"two bytes")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination"
            with self.assertRaises(ValueError):
                safe_extract(MismatchedBundle(), destination)
            self.assertFalse(destination.exists())

    def test_extracts_only_validated_directories_and_regular_files(self):
        # Catches validation that rejects legitimate directory entries or loses
        # the executable permission required by the player entrypoint.
        directory = zipfile.ZipInfo("folder/")
        directory.create_system = 3
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        executable = zipfile.ZipInfo("folder/tool")
        executable.create_system = 3
        executable.external_attr = (stat.S_IFREG | 0o755) << 16
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "valid.zip"
            self._write_archive(archive, [(directory, b""), (executable, b"ok")])
            destination = root / "destination"
            with zipfile.ZipFile(archive) as bundle:
                safe_extract(bundle, destination)
            self.assertEqual(b"ok", (destination / "folder/tool").read_bytes())
            self.assertTrue(os.access(destination / "folder/tool", os.X_OK))


class WorkflowTests(unittest.TestCase):
    def test_workflows_parse_and_use_the_supported_verification_matrices(self):
        # Catches malformed YAML and loss of any supported Python/OS lane.
        paths = [
            REPOSITORY_ROOT / ".github/workflows/ci.yml",
            REPOSITORY_ROOT / ".github/workflows/release.yml",
        ]
        result = subprocess.run(
            [
                "ruby",
                "-e",
                'require "yaml"; ARGV.each { |path| YAML.parse_file(path) }',
                *(str(path) for path in paths),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

        ci = paths[0].read_text(encoding="utf-8")
        self.assertIn('os: [ubuntu-latest, macos-latest]', ci)
        self.assertIn('python: ["3.9", "3.13"]', ci)
        self.assertIn("actions/checkout@v7", ci)
        self.assertIn("actions/setup-python@v7", ci)
        self.assertIn("lfs: true", ci)
        self.assertIn("python -m unittest discover -s tests -v", ci)
        self.assertIn(
            "python scripts/build-release.py --output dist/ostriv-macos-player.zip",
            ci,
        )

        release = paths[1].read_text(encoding="utf-8")
        self.assertIn('tags: ["v*"]', release)
        self.assertIn('os: [ubuntu-latest, macos-latest]', release)
        self.assertIn('python-version: "3.9"', release)
        self.assertEqual(2, release.count("actions/checkout@v7"))
        self.assertEqual(2, release.count("actions/setup-python@v7"))
        self.assertEqual(2, release.count("lfs: true"))
        self.assertIn("needs: verify", release)
        self.assertIn("--verify-tag", release)
        self.assertIn(
            '"dist/ostriv-macos-player.zip#Ostriv for macOS (Apple Silicon)"',
            release,
        )
        self.assertNotIn("actions/create-release", release)
        self.assertNotIn("actions/upload-release-asset", release)


class PlayerDocumentationTests(unittest.TestCase):
    def test_readme_leads_with_one_ordered_player_path(self):
        # Catches player instructions being duplicated or hidden below contributor setup.
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        development = readme.index("## Development")
        player = readme[:development]
        link = (
            "https://github.com/maksymenkoml/ostriv-macos/releases/latest/download/"
            "ostriv-macos-player.zip"
        )
        steps = [
            player.index(link),
            player.index("Extract"),
            player.index("python3 patch.py"),
            player.index("Ostriv (patched)"),
        ]
        self.assertEqual(steps, sorted(steps))
        self.assertLess(steps[-1], 1500)
        self.assertEqual(1, readme.count(link))
        self.assertEqual(1, player.count("`python3 patch.py`"))
        self.assertEqual(1, readme.count("Steam's **Play** button"))
        self.assertNotIn("git clone", player)
        self.assertNotIn("git lfs", player.lower())
        self.assertGreater(readme.index("git clone"), development)
        self.assertGreater(readme.lower().index("git lfs"), development)
        self.assertGreater(readme.index("scripts/build-driver.sh"), development)

    def test_readme_has_one_troubleshooting_action_per_required_case_and_log(self):
        # Catches the concise failure map losing a known player-facing diagnosis.
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Troubleshooting", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("| **")]
        cases = [
            "Package: FAILED",
            "CrossOver/game not found",
            "Steam timeout",
            "Graphics-context failure",
            "Unexpected failure",
        ]
        self.assertEqual(len(cases), len(rows))
        for case in cases:
            matching = [row for row in rows if case in row]
            self.assertEqual(1, len(matching), case)
            self.assertEqual(3, len(matching[0].split("|")) - 1, matching[0])
        self.assertIn("~/Library/Logs/ostriv-macos/install.log", section)

    def test_prebuilt_readme_distinguishes_hydrated_player_asset_from_git_lfs(self):
        # Catches contributors' LFS setup leaking into the player's install path.
        readme = (REPOSITORY_ROOT / "prebuilt/README.md").read_text(encoding="utf-8")
        self.assertIn("hydrated DLLs", readme)
        self.assertIn("release asset", readme)
        self.assertIn("repository contributors only", readme)


if __name__ == "__main__":
    unittest.main()
