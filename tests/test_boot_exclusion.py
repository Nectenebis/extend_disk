import argparse
import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from test_extend_free_space import h, node, root_disk


class BootExclusion(unittest.TestCase):
    def test_explicit_boot_paths_fail_before_discovery_or_rescan(self):
        for path in ("/boot", "/boot/efi=1gb", "/boot/other", "/efi", "/efi/loader",
                     "/data/../boot/"):
            for dry_run in (False, True):
                with self.subTest(path=path, dry_run=dry_run), \
                     patch.object(h, "load_lsblk") as load, patch.object(h, "rescan_block_devices") as rescan:
                    with self.assertRaisesRegex(ValueError, "manage them manually"):
                        h.execute(argparse.Namespace(disk=[], dry_run=dry_run, mountpoints=path))
                    load.assert_not_called()
                    rescan.assert_not_called()

    def test_automatic_boot_partition_or_lv_is_skipped(self):
        for mountpoint in ("/boot", "/boot/efi", "/boot/extra", "/efi"):
            for lvm in (False, True):
                part = (node("sdb1", "part", "LVM2_member", children=[
                    node("vg-boot", "lvm", "ext4", mountpoint)]) if lvm else
                    node("sdb1", "part", "ext4", mountpoint))
                disk = node("sdb", "disk", children=[part])
                with self.subTest(mountpoint=mountpoint, lvm=lvm):
                    targets, skipped = h.discover_targets([root_disk(), disk])
                    self.assertEqual([t["device"] for t in targets], ["/dev/sda1"])
                    self.assertTrue(any("boot filesystem/partition is excluded" in line for line in skipped))

    def test_boot_mount_alias_cannot_bypass_protection(self):
        boot = node("sdb1", "part", "ext4", "/boot")
        boot["mountpoints"].append("/alias")
        with self.assertRaisesRegex(RuntimeError, "boot filesystem/partition is excluded"):
            h.discover_targets([root_disk(), node("sdb", "disk", children=[boot])],
                               requests=h.parse_mountpoints("/alias"))

    def test_boot_partition_types_excluded_without_mount_or_under_another_path(self):
        for parttype in h.PROTECTED_PARTTYPES:
            for mountpoint in (None, "/data"):
                part = node("sdb1", "part", "ext4", mountpoint)
                part["parttype"] = parttype.upper()
                disks = [root_disk(), node("sdb", "disk", children=[part])]
                with self.subTest(parttype=parttype, mountpoint=mountpoint):
                    targets, skipped = h.discover_targets(disks)
                    self.assertEqual([t["device"] for t in targets], ["/dev/sda1"])
                    self.assertIn("boot filesystem/partition is excluded", skipped[0])
                    if mountpoint:
                        with self.assertRaisesRegex(RuntimeError, "manage it manually"):
                            h.discover_targets(disks, requests=h.parse_mountpoints(mountpoint))

    def test_root_and_explicit_data_keep_working_with_system_boot_partitions(self):
        system = node("sda", "disk", children=[node("sda1", "part", "vfat", "/boot/efi"),
            node("sda2", "part", "ext4", "/boot"), node("sda3", "part", "ext4", "/")])
        data = node("sdb", "disk", children=[node("sdb1", "part", "ext4", "/data")])
        targets, skipped = h.discover_targets([system, data], requests=h.parse_mountpoints("/data"))
        self.assertEqual({t["device"] for t in targets}, {"/dev/sda3", "/dev/sdb1"})
        self.assertFalse(skipped)

    def test_root_lv_still_selected_when_boot_lv_shares_pv(self):
        system = node("sda", "disk", children=[node("sda1", "part", "LVM2_member", children=[
            node("vg-boot", "lvm", "ext4", "/boot"), node("vg-root", "lvm", "ext4", "/")])])
        targets, skipped = h.discover_targets([system])
        self.assertEqual([t["device"] for t in targets], ["/dev/vg-root"])
        self.assertFalse(skipped)

    def test_ordinary_paths_with_boot_in_name_are_allowed(self):
        for path in ("/boot-data", "/efi-backup", "/data/boot"):
            disk = node("sdb", "disk", children=[node("sdb1", "part", "ext4", path)])
            targets, _ = h.discover_targets([root_disk(), disk], requests=h.parse_mountpoints(path))
            self.assertEqual(len(targets), 2)

    def test_fat_on_data_partition_is_unsupported(self):
        disk = node("sdb", "disk", children=[node("sdb1", "part", "vfat", "/data")])
        targets, skipped = h.discover_targets([root_disk(), disk])
        self.assertEqual(len(targets), 1)
        self.assertIn("unsupported", skipped[0])
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            h.discover_targets([root_disk(), disk], requests=h.parse_mountpoints("/data"))

    def test_cli_help_retains_percentages_without_offline_mode(self):
        with patch.object(sys, "argv", ["helper", "--help"]), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_status:
                h.main()
        self.assertEqual(exit_status.exception.code, 0)
        self.assertIn("40%", output.getvalue())
        self.assertNotIn("--allow-unmount", output.getvalue())
