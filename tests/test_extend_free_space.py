import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("helper", Path(__file__).resolve().parents[1] / "files/extend_free_space.py")
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)
MIB = 1024 ** 2


def node(name, kind, fs=None, mount=None, children=()):
    return dict(name=name, path="/dev/" + name, type=kind, fstype=fs,
                mountpoints=[mount], rm=0, size=128 * MIB, children=list(children))


def root_disk():
    return node("sda", "disk", children=[node("sda1", "part", "ext4", "/")])


def lvm_target(kind="system"):
    return dict(kind=kind, device="/dev/vg/root", disk={"path": "/dev/sda"},
                pv={"path": "/dev/sda2", "type": "part"}, part={"path": "/dev/sda2"},
                fs="ext4", mountpoint="/")


def state(free=0, count=100):
    return dict(pv=dict(pv_name="/dev/sda2", vg_name="vg", pe_start=str(MIB),
                        pv_pe_count=str(count), pv_pe_alloc_count=str(count - free)),
                free=free, extent_size=4 * MIB)


class Discovery(unittest.TestCase):
    def test_root_and_three_data_layouts(self):
        plain = node("sdb", "disk", children=[node("sdb1", "part", "xfs", "/data")])
        partition_lvm = node("sdc", "disk", children=[node("sdc1", "part", "LVM2_member", children=[
            node("vg-data", "lvm", "ext4", "/data2")])])
        disk_lvm = node("nvme0n1", "disk", children=[node("vg-other", "lvm", "xfs", "/data3")])
        targets, skipped = h.discover_targets([root_disk(), plain, partition_lvm, disk_lvm])
        self.assertEqual(len(targets), 4)
        self.assertFalse(skipped)
        self.assertEqual(targets[2]["pv"]["path"], "/dev/sdc1")
        self.assertIsNone(targets[3]["part"])

    def test_root_lvm_with_swap_is_selected_but_data_multilv_is_not(self):
        def disk(name, mount):
            return node(name, "disk", children=[node(name + "1", "part", "LVM2_member", children=[
                node(name + "-root", "lvm", "ext4", mount), node(name + "-swap", "lvm", "swap")])])
        targets, skipped = h.discover_targets([disk("sda", "/"), disk("sdb", "/data")])
        self.assertEqual([t["device"] for t in targets], ["/dev/sda-root"])
        self.assertTrue(any("multiple LVs" in message for message in skipped))

    def test_data_multiple_partitions_are_not_guessed(self):
        data = node("sdb", "disk", children=[node("sdb1", "part", "ext4", "/data"), node("sdb2", "part", "swap")])
        targets, skipped = h.discover_targets([root_disk(), data])
        self.assertEqual(len(targets), 1)
        self.assertIn("exactly one partition", skipped[0])

    def test_unmounted_xfs_and_encrypted_device_are_skipped(self):
        data = node("sdb", "disk", children=[node("sdb1", "part", "xfs")])
        encrypted = node("sdc", "disk", children=[node("sdc1", "part", "crypto_LUKS", children=[
            node("crypt-data", "crypt", "ext4", "/data")])])
        targets, skipped = h.discover_targets([root_disk(), data, encrypted])
        self.assertEqual(len(targets), 1)
        self.assertEqual(len(skipped), 2)

    def test_disk_allowlist_excludes_root(self):
        data = node("sdb", "disk", children=[node("sdb1", "part", "ext4", "/data")])
        targets, _ = h.discover_targets([root_disk(), data], {"/dev/sdb"})
        self.assertEqual([t["device"] for t in targets], ["/dev/sdb1"])

    def test_unknown_root_requires_explicit_selection(self):
        with patch.object(h, "run", return_value="overlay"):
            with self.assertRaisesRegex(RuntimeError, "root block device"):
                h.discover_targets([])

    def test_legacy_lsblk(self):
        failed = subprocess.CompletedProcess([], 1, "", "lsblk: unknown column: MOUNTPOINTS")
        ok = subprocess.CompletedProcess([], 0, json.dumps({"blockdevices": [dict(mountpoint="/")]}), "")
        with patch.object(h, "command", side_effect=[failed, ok]) as cmd:
            nodes = h.load_lsblk()
        self.assertIn("MOUNTPOINT,", cmd.call_args.args[0][-1])
        self.assertEqual(h.mountpoints(nodes[0]), ["/"])


class Partition(unittest.TestCase):
    def test_geometry_uses_sector_size_including_4k_disks(self):
        table = dict(partitiontable=dict(label="gpt", sectorsize=4096,
                                        partitions=[dict(node="/dev/nvme0n1p2", start=2048, size=4000)]))
        with patch.object(h, "run", return_value=json.dumps(table)):
            self.assertEqual(h.partition_size("/dev/nvme0n1", "/dev/nvme0n1p2"), 4000 * 4096)

    def test_probe_distinguishes_nochange_from_error(self):
        with patch.object(h, "command", return_value=subprocess.CompletedProcess([], 1, "NOCHANGE: partition 2", "")):
            self.assertFalse(h.probe_partition("/dev/sda", "/dev/sda2"))
        with patch.object(h, "command", return_value=subprocess.CompletedProcess([], 2, "", "bad GPT")):
            with self.assertRaisesRegex(RuntimeError, "bad GPT"):
                h.probe_partition("/dev/sda", "/dev/sda2")

    def test_stale_kernel_size_is_a_hard_failure(self):
        with patch.object(h, "partition_size", return_value=200), \
             patch.object(h, "device_size", return_value=100), \
             patch.object(h, "command", return_value=subprocess.CompletedProcess([], 1, "", "busy")), \
             patch.object(h, "run"), patch.object(h.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Kernel still sees the old size"):
                h.resize_partition("/dev/sda", "/dev/sda2", False)

    def test_retry_syncs_already_written_partition(self):
        with patch.object(h, "partition_size", return_value=200), \
             patch.object(h, "device_size", side_effect=[100, 200]), \
             patch.object(h, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as cmd, \
             patch.object(h, "run"):
            h.resize_partition("/dev/sda", "/dev/sda2", False)
        self.assertEqual(cmd.call_args.args[0], ["partx", "--update", "--nr", "2", "/dev/sda"])

    def test_kernel_size_larger_than_table_is_rejected(self):
        with patch.object(h, "partition_size", return_value=100), patch.object(h, "device_size", return_value=200):
            with self.assertRaisesRegex(RuntimeError, "exceeds the on-disk"):
                h.resize_partition("/dev/sda", "/dev/sda2", False)

    def test_partition_change_is_reported_even_if_kernel_update_fails(self):
        with patch.object(h, "partition_size", side_effect=[100, 200]), \
             patch.object(h, "command", return_value=subprocess.CompletedProcess([], 2, "", "kernel update failed")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "kernel update failed"):
                h.resize_partition("/dev/sda", "/dev/sda2", True)
        self.assertIn("CHANGED: partition", output.getvalue())


class Lvm(unittest.TestCase):
    def reports(self, tool, fields, section):
        return [state()["pv"]] if tool == "pvs" else [dict(vg_name="vg", vg_extent_size=str(4 * MIB))]

    def segment(self, name="root", segtype="linear"):
        return dict(lv_path="/dev/vg/" + name, vg_name="vg", lv_attr="-wi-ao----", segtype=segtype)

    def test_lvm_segments_report_and_multiple_root_vg_lvs(self):
        segments = {"report": [{"seg": [self.segment(), self.segment("swap")]}]}
        with patch.object(h, "report", side_effect=self.reports), patch.object(h, "run", return_value=json.dumps(segments)):
            self.assertEqual(h.lvm_state(lvm_target())["free"], 0)
            with self.assertRaisesRegex(RuntimeError, "multiple data LVs"):
                h.lvm_state(lvm_target("data"))

    def test_multi_pv_vg_rejected_before_mutation(self):
        pvs = [state()["pv"], dict(state()["pv"], pv_name="/dev/sdb1")]
        with patch.object(h, "report", return_value=pvs), patch.object(h, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "multi-PV"):
                h.lvm_state(lvm_target())
            run.assert_not_called()

    def test_mixed_segments_and_thin_lvs_are_rejected(self):
        for segments in [[self.segment(segtype="thin")], [self.segment(), self.segment(segtype="striped")]]:
            with self.subTest(segments=segments), patch.object(h, "report", side_effect=self.reports), \
                 patch.object(h, "run", return_value=json.dumps({"report": [{"seg": segments}]})):
                with self.assertRaisesRegex(RuntimeError, "ordinary linear"):
                    h.lvm_state(lvm_target())

    def test_existing_free_extents_used_with_explicit_pv(self):
        with patch.object(h, "lvm_state", return_value=state(free=3)), \
             patch.object(h, "filesystem_geometry", return_value=(10, 4096)), \
             patch.object(h, "probe_partition", return_value=False), patch.object(h, "resize_partition"), \
             patch.object(h, "device_size", return_value=401 * MIB), patch.object(h, "grow_filesystem") as fs, \
             patch.object(h, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            h.process_target(lvm_target(), False)
        run.assert_called_once_with(["lvextend", "-l", "+3", "/dev/vg/root", "/dev/sda2"])
        fs.assert_called_once()

    def test_pv_resize_precedes_lv_extension(self):
        with patch.object(h, "lvm_state", side_effect=[state(), state(free=50, count=150)]), \
             patch.object(h, "filesystem_geometry", return_value=(10, 4096)), \
             patch.object(h, "probe_partition", return_value=True), patch.object(h, "resize_partition"), \
             patch.object(h, "device_size", return_value=601 * MIB), patch.object(h, "grow_filesystem"), \
             patch.object(h, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            h.process_target(lvm_target(), False)
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ["pvresize", "/dev/sda2"], ["lvextend", "-l", "+50", "/dev/vg/root", "/dev/sda2"]])

    def test_retry_after_lv_growth_finishes_filesystem(self):
        with patch.object(h, "lvm_state", return_value=state()), \
             patch.object(h, "filesystem_geometry", return_value=(10, 4096)), \
             patch.object(h, "probe_partition", return_value=False), patch.object(h, "resize_partition"), \
             patch.object(h, "device_size", return_value=401 * MIB), patch.object(h, "grow_filesystem") as fs, \
             patch.object(h, "run") as run:
            h.process_target(lvm_target(), False)
        run.assert_not_called()
        fs.assert_called_once()

    def test_unsafe_lvm_does_not_resize_partition(self):
        with patch.object(h, "lvm_state", side_effect=RuntimeError("unsafe VG")), \
             patch.object(h, "probe_partition") as probe, patch.object(h, "resize_partition") as resize:
            with self.assertRaisesRegex(RuntimeError, "unsafe VG"):
                h.process_target(lvm_target(), False)
        probe.assert_not_called()
        resize.assert_not_called()

    def test_pv_on_shrunken_device_is_not_allocated(self):
        with patch.object(h, "lvm_state", return_value=state(free=3)), \
             patch.object(h, "filesystem_geometry", return_value=(10, 4096)), \
             patch.object(h, "probe_partition", return_value=False), patch.object(h, "resize_partition"), \
             patch.object(h, "device_size", return_value=201 * MIB), patch.object(h, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "PV metadata exceeds"):
                h.process_target(lvm_target(), False)
        run.assert_not_called()


class Filesystem(unittest.TestCase):
    def test_xfs_geometry_and_data_growth(self):
        target = dict(fs="xfs", device="/dev/vg/data", mountpoint="/data")
        with patch.object(h, "run", return_value="data = bsize=4096 blocks=1000, imaxpct=25"):
            self.assertEqual(h.filesystem_geometry(target), (1000, 4096))
        with patch.object(h, "filesystem_geometry", side_effect=[(1000, 4096), (2000, 4096)]), \
             patch.object(h, "device_size", return_value=2000 * 4096), patch.object(h, "run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            h.grow_filesystem(target)
        run.assert_called_once_with(["xfs_growfs", "-d", "/data"])
        self.assertIn("CHANGED: filesystem", output.getvalue())

    def test_no_change_is_idempotent(self):
        with patch.object(h, "filesystem_geometry", return_value=(1000, 4096)), \
             patch.object(h, "device_size", return_value=1000 * 4096), patch.object(h, "run") as run:
            h.grow_filesystem(dict(fs="ext4", device="/dev/sda1"))
        run.assert_not_called()

    def test_decimal_sizes_remain_exact_above_float_precision(self):
        self.assertEqual(h.integer("9007199254740993.00"), 9007199254740993)

    def test_filesystem_larger_than_device_is_rejected(self):
        with patch.object(h, "filesystem_geometry", return_value=(1000, 4096)), \
             patch.object(h, "device_size", return_value=500 * 4096), patch.object(h, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "Filesystem exceeds"):
                h.grow_filesystem(dict(fs="ext4", device="/dev/sda1"))
        run.assert_not_called()


class Execution(unittest.TestCase):
    def test_dry_run_never_rescans(self):
        with patch.object(h, "load_lsblk", return_value=[root_disk()]), \
             patch.object(h, "rescan_block_devices") as rescan, patch.object(h, "process_target") as process, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(h.execute(argparse.Namespace(disk=[], dry_run=True)), 0)
        rescan.assert_not_called()
        self.assertTrue(process.call_args.args[1])

    def test_bad_allowlist_is_not_silently_ignored(self):
        with patch.object(h, "load_lsblk", return_value=[root_disk()]), patch.object(h, "rescan_block_devices") as rescan:
            with self.assertRaisesRegex(RuntimeError, "Unknown or unsupported"):
                h.execute(argparse.Namespace(disk=["/dev/sdz"], dry_run=False))
        rescan.assert_not_called()

    def test_error_on_one_disk_does_not_hide_result_on_next(self):
        data = node("sdb", "disk", children=[node("sdb1", "part", "ext4", "/data")])
        with patch.object(h, "load_lsblk", return_value=[root_disk(), data]), \
             patch.object(h, "process_target", side_effect=[RuntimeError("bad GPT"), None]) as process, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(h.execute(argparse.Namespace(disk=[], dry_run=True)), 1)
        self.assertEqual(process.call_count, 2)
        self.assertIn("bad GPT", errors.getvalue())

    def test_dry_run_target_issues_only_read_commands(self):
        target = lvm_target()
        with patch.object(h, "lvm_state", return_value=state()), \
             patch.object(h, "filesystem_geometry", return_value=(1000, 4096)), \
             patch.object(h, "probe_partition", return_value=True), \
             patch.object(h, "partition_size", return_value=500 * MIB), \
             patch.object(h, "device_size", return_value=400 * MIB), \
             patch.object(h, "resize_partition") as resize, patch.object(h, "grow_filesystem") as fs, \
             patch.object(h, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            h.process_target(target, True)
        resize.assert_not_called()
        fs.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
