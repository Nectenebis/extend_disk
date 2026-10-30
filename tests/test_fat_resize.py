import copy
import contextlib
import io
import json
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_extend_free_space import h, MIB, node, root_disk


def boot_sector():
    data = bytearray(512)
    data[510:512] = b"\x55\xaa"
    struct.pack_into("<H", data, 11, 512)
    data[13] = 8
    struct.pack_into("<H", data, 14, 32)
    data[16] = 2
    struct.pack_into("<I", data, 32, 600 * MIB // 512)
    struct.pack_into("<I", data, 36, 2048)
    data[67:71] = b"\x12\x34\x56\x78"
    return data


def target():
    return dict(device="/dev/sda1", mountpoint="/boot/efi", fs="vfat", pv=None, explicit=True,
                disk=dict(path="/dev/sda"), part=dict(path="/dev/sda1"), allocation=dict(type="all"))


def table():
    # Actual reported layout: EFI and /boot are exactly adjacent.
    return dict(label="gpt", sectorsize=512, lastlba=83886046, partitions=[
        dict(node="/dev/sda1", start=2048, size=1228800, uuid="EFI-UUID", type="C12A7328-F81F-11D2-BA4B-00A0C93EC93B"),
        dict(node="/dev/sda2", start=1230848, size=2097152, uuid="BOOT-UUID", type="LINUX")])


def mount():
    return dict(source="/dev/sda1", target="/boot/efi", fstype="vfat", fsroot="/", options="rw,nosuid,nodev,relatime,fmask=0077,dmask=0077")


def plan(action=True):
    return dict(action=action, desired=728 * MIB, maximum=1200 * MIB,
                geometry=h.parse_fat_boot_sector(boot_sector()), mount=mount(), table=table(), partition=table()["partitions"][0])


class Units(unittest.TestCase):
    def test_decimal_and_binary_units_all_cases(self):
        for unit, multiplier in {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
                                 "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}.items():
            for spelling in {unit, unit.lower(), unit.upper(), unit.title()}:
                with self.subTest(unit=spelling):
                    spec = h.parse_mountpoints("/data=2" + spelling)[0]["allocation"]
                    self.assertEqual(spec, dict(type="bytes", value=2 * multiplier))

    def test_decimal_fraction_and_duplicate_equivalence(self):
        self.assertEqual(h.parse_mountpoints("/data=1.5gb")[0]["allocation"]["value"], 1500000000)
        self.assertEqual(len(h.parse_mountpoints("/data=1GB /data=1000mb")), 1)


class FatGeometry(unittest.TestCase):
    def test_bpb_geometry_and_volume_id(self):
        geometry = h.parse_fat_boot_sector(boot_sector())
        self.assertEqual(geometry["bits"], 32)
        self.assertEqual(geometry["sectors"] * geometry["sector_size"], 600 * MIB)
        self.assertEqual(geometry["volume_id"], "12345678")

    def test_invalid_sector_and_cluster_size_are_rejected(self):
        for offset, value in [(12, 0), (13, 0), (13, 3), (510, 0)]:
            data = boot_sector()
            data[offset] = value
            with self.subTest(offset=offset, value=value), self.assertRaises(RuntimeError):
                h.parse_fat_boot_sector(data)

    def test_nonfat_is_not_sent_to_xfs_info(self):
        with patch.object(h, "fat_geometry", return_value=h.parse_fat_boot_sector(boot_sector())), patch.object(h, "run") as run:
            self.assertEqual(h.filesystem_geometry(target()), (600 * MIB // 512, 512))
        run.assert_not_called()


class FatPlanning(unittest.TestCase):
    def test_fat_is_explicit_only_and_allows_a_fixed_size(self):
        disk = node("sdb", "disk", children=[node("sdb1", "part", "vfat", "/boot/efi")])
        targets, _ = h.discover_targets([root_disk(), disk])
        self.assertEqual(len(targets), 1)
        targets, _ = h.discover_targets([root_disk(), disk], requests=h.parse_mountpoints("/boot/efi=1gb"))
        self.assertEqual(targets[0]["allocation"]["value"], 1000000000)

    def test_fat_percent_is_not_a_vg_allocation(self):
        disk = node("sdb", "disk", children=[node("sdb1", "part", "vfat", "/boot/efi")])
        with self.assertRaisesRegex(RuntimeError, "require an LVM"):
            h.discover_targets([root_disk(), disk], requests=h.parse_mountpoints("/boot/efi=50%"))

    def test_actual_adjacent_boot_layout_is_successful_skip_without_unmount(self):
        selected = target()
        selected["allocation"] = dict(type="bytes", value=1024 ** 3)
        partition = dict(disk="/dev/sda", path="/dev/sda1", expected=600 * MIB)
        with patch.object(h, "fat_geometry", return_value=h.parse_fat_boot_sector(boot_sector())), \
             patch.object(h, "partition_table", return_value=table()), patch.object(h, "fat_mount") as mounts:
            result = h.plan_fat_growth(selected, partition, False, False)
        self.assertFalse(result["action"])
        mounts.assert_not_called()
        with patch.object(h, "run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            h.grow_fat(selected, result)
        run.assert_not_called()
        self.assertIn("adjacent partitions are not moved", output.getvalue())

    def test_online_fat_growth_is_rejected_before_writes(self):
        with patch.object(h, "fat_geometry", return_value=h.parse_fat_boot_sector(boot_sector())), \
             patch.object(h, "partition_table", return_value=table()):
            with self.assertRaisesRegex(RuntimeError, "allow_unmount"):
                h.plan_fat_growth(target(), dict(disk="/dev/sda", path="/dev/sda1", expected=1200 * MIB), False, False)

    def test_dry_run_can_plan_without_unmount_permission_or_installed_tools(self):
        with patch.object(h, "fat_geometry", return_value=h.parse_fat_boot_sector(boot_sector())), \
             patch.object(h, "partition_table", return_value=table()), patch.object(h, "fat_mount", return_value=mount()), \
             patch.object(h.shutil, "which", return_value=None):
            result = h.plan_fat_growth(target(), dict(disk="/dev/sda", path="/dev/sda1", expected=1200 * MIB), False, True)
        self.assertTrue(result["action"])

    def test_wrong_partition_types_are_rejected_before_unmount(self):
        for layout in [dict(table(), label="dos"), table()]:
            layout = copy.deepcopy(layout)
            if layout["label"] == "gpt": layout["partitions"][0]["type"] = "LINUX-DATA"
            with self.subTest(label=layout["label"]), \
                 patch.object(h, "fat_geometry", return_value=h.parse_fat_boot_sector(boot_sector())), \
                 patch.object(h, "partition_table", return_value=layout), patch.object(h, "fat_mount") as mounts:
                with self.assertRaisesRegex(RuntimeError, "GPT EFI System"):
                    h.plan_fat_growth(target(), dict(disk="/dev/sda", path="/dev/sda1", expected=1200 * MIB), True, False)
            mounts.assert_not_called()

    def test_fixed_growth_larger_than_gap_fails(self):
        selected = target()
        selected["allocation"] = dict(type="bytes", value=1024 ** 3)
        with patch.object(h, "fat_geometry", return_value=h.parse_fat_boot_sector(boot_sector())), \
             patch.object(h, "partition_table", return_value=table()):
            with self.assertRaisesRegex(RuntimeError, "Insufficient contiguous"):
                h.plan_fat_growth(selected, dict(disk="/dev/sda", path="/dev/sda1", expected=900 * MIB), True, False)

    def test_bind_aliases_and_nested_mounts_are_rejected(self):
        for mounts in [[mount(), dict(mount(), target="/alias")], [mount(), dict(mount(), target="/boot/efi/nested", source="other")]]:
            with self.subTest(mounts=mounts), patch.object(h, "run", return_value=json.dumps(dict(filesystems=mounts))):
                with self.assertRaises(RuntimeError):
                    h.fat_mount(target())


class FatExecution(unittest.TestCase):
    def test_cli_help_with_percentage_examples(self):
        with patch.object(sys, "argv", ["helper", "--help"]), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_status:
                h.main()
        self.assertEqual(exit_status.exception.code, 0)
        self.assertIn("40%", output.getvalue())
        self.assertIn("--allow-unmount", output.getvalue())

    def test_busy_mount_prevents_all_writes(self):
        with patch.object(h, "run", side_effect=RuntimeError("busy")) as run, patch.object(h.subprocess, "run") as resize:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                h.grow_fat(target(), plan())
        run.assert_called_once_with(["umount", "--", "/boot/efi"])
        resize.assert_not_called()

    def test_failed_fsck_always_attempts_remount_and_never_resizes(self):
        def run(argv):
            if argv[0] == "fsck.fat":
                raise RuntimeError("FAT errors")
        with patch.object(h, "run", side_effect=run) as commands, patch.object(h.subprocess, "run") as resize:
            with self.assertRaisesRegex(RuntimeError, "FAT errors"):
                h.grow_fat(target(), plan())
        resize.assert_not_called()
        self.assertEqual(commands.call_args.args[0], ["mount", "-t", "vfat", "-o", mount()["options"], "/dev/sda1", "/boot/efi"])

    def test_success_preserves_mount_flags_and_partition_metadata(self):
        after = copy.deepcopy(table())
        after["partitions"][0]["size"] = 728 * MIB // 512
        geometry = dict(plan()["geometry"], sectors=728 * MIB // 512)
        with patch.object(h, "run") as commands, patch.object(h, "partition_table", return_value=after), \
             patch.object(h.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as resize, \
             patch.object(h, "device_size", side_effect=lambda path: 40 * 1024 ** 3 if path == "/dev/sda" else 728 * MIB), \
             patch.object(h, "resize_partition"), patch.object(h, "fat_geometry", return_value=geometry), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            h.grow_fat(target(), plan())
        self.assertIn("CHANGED: FAT32", output.getvalue())
        self.assertEqual(resize.call_args.args[0], ["fatresize", "/dev/sda", "--partition", "1", "--size", str(728 * MIB)])
        self.assertEqual(commands.call_args.args[0][-3:], [mount()["options"], "/dev/sda1", "/boot/efi"])

    def test_remount_failure_is_not_reported_as_success(self):
        def run(argv):
            if argv[0] == "fsck.fat": raise RuntimeError("bad FAT")
            if argv[0] == "mount": raise RuntimeError("restore failed")
        with patch.object(h, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "Failed to restore FAT mount"):
                h.grow_fat(target(), plan())


if __name__ == "__main__":
    unittest.main()
