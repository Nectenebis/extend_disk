"""Real tools on temporary regular files only; no loop/mount/root needed."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_extend_free_space import h, MIB


@unittest.skipUnless(shutil.which("growpart") and shutil.which("sfdisk"), "requires growpart and sfdisk")
class PartitionImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "disk.img"
        self.path.touch()
        self.extend(64 * MIB)
        # A regular image cannot be a PV. Isolate growpart's optional LVM hook
        # from host LVM discovery, while running real partition tools.
        tools = Path(self.tmp.name) / "bin"
        tools.mkdir()
        lvm = tools / "lvm"
        lvm.write_text("#!/bin/sh\nexit 5\n")
        lvm.chmod(0o755)
        self.env = patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"]})
        self.env.start()
        self.addCleanup(self.env.stop)

    def extend(self, size):
        with self.path.open("r+b") as stream:
            stream.truncate(size)

    def cli(self, *argv, input=None, expected=0):
        result = subprocess.run(argv, input=input, text=True, capture_output=True,
                                env=dict(os.environ, LC_ALL="C"))
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def table(self):
        return json.loads(self.cli("sfdisk", "--json", str(self.path)))["partitiontable"]

    def digest(self):
        value = hashlib.sha256()
        with self.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(MIB), b""):
                value.update(chunk)
        return value.hexdigest()

    def test_gpt_expansion_repairs_backup_header_and_preserves_identity(self):
        self.cli("sfdisk", str(self.path), input="label: gpt\nsize=8M,type=L\ntype=L\n")
        before = self.table()
        old = before["partitions"][1]
        self.extend(96 * MIB)  # Same stale backup-GPT condition as after vCenter expansion.
        digest = self.digest()
        self.assertTrue(h.probe_partition(str(self.path), old["node"]))
        with patch.object(h, "device_size", return_value=old["size"] * before["sectorsize"]):
            plan = h.plan_partition(str(self.path), old["node"])
        self.assertGreater(plan["expected"], old["size"] * before["sectorsize"])
        self.assertEqual(self.digest(), digest, "dry-run modified the image")
        self.cli("growpart", "--update=off", str(self.path), "2")
        after = self.table()
        new = after["partitions"][1]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["partitions"][0], before["partitions"][0])
        for key in ("start", "uuid", "type"):
            self.assertEqual(new[key], old[key])
        self.assertGreater(new["size"], old["size"])
        self.assertGreater(after["lastlba"], before["lastlba"])
        self.cli("sfdisk", "--verify", str(self.path))
        self.assertFalse(h.probe_partition(str(self.path), new["node"]))

    def test_full_disk_with_boot_partition_has_no_false_free_space(self):
        self.cli("sfdisk", str(self.path), input="label: gpt\nsize=8M,type=L\ntype=L\n")
        root = self.table()["partitions"][1]
        digest = self.digest()
        self.assertFalse(h.probe_partition(str(self.path), root["node"]))
        self.assertEqual(self.digest(), digest)

    def test_mbr_expansion_is_repeatable(self):
        self.cli("sfdisk", str(self.path), input="label: dos\nsize=8M,type=83\ntype=83\n")
        old = self.table()["partitions"][1]
        self.extend(96 * MIB)
        self.cli("growpart", "--update=off", str(self.path), "2")
        new = self.table()["partitions"][1]
        self.assertEqual(new["start"], old["start"])
        self.assertGreater(new["size"], old["size"])
        self.assertFalse(h.probe_partition(str(self.path), new["node"]))

    def test_root_before_swap_cannot_consume_disk_tail(self):
        self.cli("sfdisk", str(self.path), input="label: gpt\nsize=32M,type=L\ntype=S\n")
        old = self.table()["partitions"]
        self.extend(96 * MIB)
        self.assertFalse(h.probe_partition(str(self.path), old[0]["node"]))
        self.assertEqual(self.table()["partitions"], old)

    def test_partition_number_does_not_override_physical_order(self):
        self.cli("sfdisk", str(self.path), input=(
            "label: gpt\n" + str(self.path) + "1 : start=32768, size=96000, type=L\n"
            + str(self.path) + "2 : start=2048, size=16384, type=L\n"))
        old = self.table()["partitions"]
        self.extend(96 * MIB)
        self.cli("growpart", "--update=off", str(self.path), "2")
        new = self.table()["partitions"]
        self.assertEqual(new[0], old[0])
        self.assertEqual(new[1]["start"], old[1]["start"])
        self.assertLessEqual(new[1]["start"] + new[1]["size"], new[0]["start"])
        self.cli("sfdisk", "--verify", str(self.path))


@unittest.skipUnless(shutil.which("mkfs.ext4") and shutil.which("dumpe2fs") and shutil.which("resize2fs"),
                     "requires e2fsprogs")
class FilesystemImages(unittest.TestCase):
    def test_real_ext4_growth_and_second_run_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "ext4.img"
            with image.open("wb") as stream:
                stream.truncate(64 * MIB)
            result = subprocess.run(["mkfs.ext4", "-F", "-q", str(image)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = dict(fs="ext4", device=str(image), mountpoint=None)
            old_blocks, block_size = h.filesystem_geometry(target)
            with image.open("r+b") as stream:
                stream.truncate(96 * MIB)
            with patch.object(h, "device_size", side_effect=lambda path: Path(path).stat().st_size), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                h.grow_filesystem(target)
            self.assertIn("CHANGED: filesystem", output.getvalue())
            self.assertGreater(h.filesystem_geometry(target)[0], old_blocks)
            with patch.object(h, "device_size", side_effect=lambda path: Path(path).stat().st_size), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                h.grow_filesystem(target)
            self.assertNotIn("CHANGED:", output.getvalue())


@unittest.skipUnless(all(shutil.which(tool) for tool in ("sfdisk", "fatresize", "mkfs.fat", "fsck.fat")),
                     "requires sfdisk, fatresize and dosfstools")
class FatImages(unittest.TestCase):
    def test_native_fat32_growth_preserves_efi_metadata_and_volume_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk, filesystem = Path(tmp) / "disk.img", Path(tmp) / "fat.img"
            with disk.open("wb") as stream:
                stream.truncate(1200 * MIB)
            with filesystem.open("wb") as stream:
                stream.truncate(600 * MIB)
            subprocess.run(["mkfs.fat", "-F", "32", str(filesystem)], check=True, capture_output=True)
            old_geometry = h.fat_geometry(str(filesystem))
            subprocess.run(["sfdisk", str(disk)], input="label:gpt\nsize=600M,type=U,name=EFI\n",
                           text=True, capture_output=True, check=True)
            with disk.open("r+b") as output, filesystem.open("rb") as source:
                output.seek(MIB)
                shutil.copyfileobj(source, output, length=MIB)
            before = json.loads(subprocess.check_output(["sfdisk", "--json", str(disk)], text=True))["partitiontable"]
            result = subprocess.run(["fatresize", str(disk), "--partition", "1", "--size", str(900 * MIB)],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            after = json.loads(subprocess.check_output(["sfdisk", "--json", str(disk)], text=True))["partitiontable"]
            for key in ("start", "uuid", "type", "name"):
                self.assertEqual(after["partitions"][0][key], before["partitions"][0][key])
            self.assertEqual(after["id"], before["id"])
            self.assertGreater(after["partitions"][0]["size"], before["partitions"][0]["size"])
            with disk.open("rb") as source:
                source.seek(after["partitions"][0]["start"] * after["sectorsize"])
                geometry = h.parse_fat_boot_sector(source.read(512))
                self.assertEqual(geometry["bits"], 32)
                self.assertEqual(geometry["volume_id"], old_geometry["volume_id"])
                self.assertGreater(geometry["sectors"], old_geometry["sectors"])
                source.seek(after["partitions"][0]["start"] * after["sectorsize"])
                with filesystem.open("wb") as output:
                    remaining = after["partitions"][0]["size"] * after["sectorsize"]
                    while remaining:
                        chunk = source.read(min(MIB, remaining))
                        self.assertTrue(chunk)
                        output.write(chunk)
                        remaining -= len(chunk)
            checked = subprocess.run(["fsck.fat", "-n", str(filesystem)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)


if __name__ == "__main__":
    unittest.main()
