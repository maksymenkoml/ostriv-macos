import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.diagnostics import PatchError
from ostriv_macos.payload import load_manifest, validate_payload


RECOVERY = "The download is incomplete. Download the release ZIP again."


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, payload: bytes, **overrides) -> Path:
        target = self.root / "prebuilt" / "opengl32.dll"
        target.parent.mkdir()
        target.write_bytes(payload)
        entry = {
            "path": "prebuilt/opengl32.dll",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "pe": True,
        }
        entry.update(overrides)
        return self.write_manifest_data({"schema": 1, "files": [entry]})

    def write_manifest_data(self, data) -> Path:
        path = self.root / "payload-manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def assert_payload_error(self, callback, code):
        with self.assertRaises(PatchError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(RECOVERY, caught.exception.player_message)

    def test_valid_pe_payload_passes(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        validate_payload(self.root, load_manifest(path))

    def test_lfs_pointer_is_rejected_before_header_or_hash(self):
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n"
        path = self.write_manifest(pointer)
        self.assert_payload_error(
            lambda: validate_payload(self.root, load_manifest(path)),
            "payload.lfs_pointer",
        )

    def test_missing_payload_is_rejected(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        (self.root / "prebuilt" / "opengl32.dll").unlink()
        self.assert_payload_error(
            lambda: validate_payload(self.root, load_manifest(path)), "payload.missing"
        )

    def test_non_pe_payload_is_rejected_before_size_or_hash(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        (self.root / "prebuilt" / "opengl32.dll").write_bytes(b"not a DLL")
        self.assert_payload_error(
            lambda: validate_payload(self.root, load_manifest(path)), "payload.not_pe"
        )

    def test_size_mismatch_is_rejected_before_hash(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        (self.root / "prebuilt" / "opengl32.dll").write_bytes(b"MZshort")
        self.assert_payload_error(
            lambda: validate_payload(self.root, load_manifest(path)),
            "payload.size_mismatch",
        )

    def test_hash_mismatch_is_rejected(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        (self.root / "prebuilt" / "opengl32.dll").write_bytes(
            b"MZ" + b"\0" * 63 + b"\1"
        )
        self.assert_payload_error(
            lambda: validate_payload(self.root, load_manifest(path)),
            "payload.hash_mismatch",
        )

    def test_malformed_json_is_rejected(self):
        path = self.root / "payload-manifest.json"
        path.write_text("{", encoding="utf-8")
        self.assert_payload_error(lambda: load_manifest(path), "payload.manifest")

    def test_unsupported_schema_is_rejected(self):
        path = self.write_manifest_data({"schema": 2, "files": []})
        self.assert_payload_error(lambda: load_manifest(path), "payload.manifest")

    def test_duplicate_paths_are_rejected(self):
        entry = {
            "path": "prebuilt/opengl32.dll",
            "size": 1,
            "sha256": "0" * 64,
            "pe": True,
        }
        path = self.write_manifest_data({"schema": 1, "files": [entry, entry]})
        self.assert_payload_error(lambda: load_manifest(path), "payload.manifest")

    def test_absolute_path_is_rejected(self):
        path = self.write_manifest(b"MZ", path="/tmp/opengl32.dll")
        self.assert_payload_error(lambda: load_manifest(path), "payload.manifest")

    def test_parent_traversal_path_is_rejected(self):
        path = self.write_manifest(b"MZ", path="prebuilt/../opengl32.dll")
        self.assert_payload_error(lambda: load_manifest(path), "payload.manifest")
