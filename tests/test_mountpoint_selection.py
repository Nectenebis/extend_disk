import argparse
import contextlib
from decimal import Decimal
import io
import unittest
from unittest.mock import patch

from test_extend_free_space import h, node, root_disk, lvm_target, state, MIB


def selected_target(path, spec="", device=None):
    target = lvm_target("data")
    request = h.parse_mountpoints(path + ("=" + spec if spec else ""))[0]
    target.update(explicit=True, mountpoint=path, allocation=request["allocation"], device=device or "/dev/vg" + path)
    return target


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


class Input(unittest.TestCase):
    def test_mixed_separators_sizes_percent_and_remainder(self):
        values = h.parse_mountpoints(" /data=20GiB, /logs\n/archive=12.5% ")
        self.assertEqual(values[0]["allocation"], dict(type="bytes", value=20 * 1024 ** 3))
        self.assertEqual(values[1]["allocation"], dict(type="all"))
        self.assertEqual(values[2]["allocation"], dict(type="percent", value=Decimal("12.5")))

    def test_empty_input_and_identical_duplicates(self):
        self.assertEqual(h.parse_mountpoints(" , \n "), [])
        self.assertEqual(len(h.parse_mountpoints("/data/, /data")), 1)

    def test_invalid_input(self):
        for value in ["data", "/data=+20GiB", "/data=rest", "/data=0GiB", "/data=-1GiB",
                      "/data=101%", "/data=0%", "/data=", "/data=20", "/data=NaN%", "/data=0.1B"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                h.parse_mountpoints(value)

    def test_conflicting_duplicates(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            h.parse_mountpoints("/data=20GiB, /data=30GiB")


class Selection(unittest.TestCase):
    def data_disk(self):
        return node("sdb", "disk", children=[node("sdb1", "part", "ext4", "/data"),
                                              node("sdb2", "part", "ext4", "/second_data")])

    def test_explicit_second_partition_keeps_automatic_root(self):
        targets, skipped = h.discover_targets([root_disk(), self.data_disk()], requests=h.parse_mountpoints("/second_data"))
        self.assertEqual({t["device"] for t in targets}, {"/dev/sda1", "/dev/sdb2"})
        self.assertFalse(skipped)

    def test_explicit_system_target_replaces_root_only_on_its_disk(self):
        system = node("sda", "disk", children=[node("sda1", "part", "ext4", "/"),
                                               node("sda2", "part", "ext4", "/var")])
        data = node("sdb", "disk", children=[node("sdb1", "part", "ext4", "/data")])
        targets, _ = h.discover_targets([system, data], requests=h.parse_mountpoints("/var"))
        self.assertEqual({t["device"] for t in targets}, {"/dev/sda2", "/dev/sdb1"})

    def test_explicit_multilv_data_is_resolved(self):
        data = node("sdb", "disk", children=[node("sdb1", "part", "LVM2_member", children=[
            node("vg-data", "lvm", "ext4", "/data"), node("vg-logs", "lvm", "ext4", "/logs")])])
        targets, skipped = h.discover_targets([root_disk(), data], requests=h.parse_mountpoints("/data=20GiB /logs"))
        self.assertEqual(len(targets), 3)
        self.assertFalse(skipped)
        self.assertEqual(sum(t["explicit"] for t in targets), 2)

    def test_ambiguous_disk_lists_mountpoints_and_is_skipped(self):
        targets, skipped = h.discover_targets([root_disk(), self.data_disk()])
        self.assertEqual(len(targets), 1)
        self.assertIn("extend_disk_mountpoints", skipped[0])
        self.assertIn("/second_data", skipped[0])

    def test_mountpoint_must_match_exactly(self):
        for path in ["/missing", "/data/subdirectory"]:
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "exact mounted filesystem"):
                h.discover_targets([root_disk(), self.data_disk()], requests=h.parse_mountpoints(path))

    def test_plain_partition_rejects_size_or_percent(self):
        for path in ["/data=20GiB", "/data=50%"]:
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "require an LVM"):
                h.discover_targets([root_disk(), self.data_disk()], requests=h.parse_mountpoints(path))

    def test_mountpoint_outside_disk_filter_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "outside the --disk"):
            h.discover_targets([root_disk(), self.data_disk()], {"/dev/sda"}, h.parse_mountpoints("/data"))

    def test_bind_aliases_of_same_device_are_deduplicated(self):
        data = self.data_disk()
        data["children"][0]["mountpoints"] = ["/data", "/bind"]
        targets, _ = h.discover_targets([root_disk(), data], requests=h.parse_mountpoints("/data /bind"))
        self.assertEqual(len(targets), 2)
        with self.assertRaisesRegex(RuntimeError, "Conflicting mountpoint aliases"):
            h.discover_targets([root_disk(), data], requests=h.parse_mountpoints("/data /bind=20GiB"))


class Allocation(unittest.TestCase):
    def allocate(self, specs, free=100, extent=4 * MIB):
        targets = [selected_target(path, spec) for path, spec in specs]
        return h.allocate_extents(targets, free, extent)

    def test_fixed_size_and_unmarked_remainder(self):
        self.assertEqual(self.allocate([("/data", "20MiB"), ("/logs", "")]),
                         {"/dev/vg/data": 5, "/dev/vg/logs": 95})

    def test_percentages_share_one_initial_pool(self):
        self.assertEqual(self.allocate([("/data", "40%"), ("/logs", "60%")]),
                         {"/dev/vg/data": 40, "/dev/vg/logs": 60})

    def test_percentage_and_fixed_size_and_remainder(self):
        self.assertEqual(self.allocate([("/data", "25%"), ("/logs", "20MiB"), ("/archive", "")]),
                         {"/dev/vg/data": 25, "/dev/vg/logs": 5, "/dev/vg/archive": 70})

    def test_fixed_only_leaves_unused_space(self):
        self.assertEqual(self.allocate([("/data", "20MiB"), ("/logs", "40MiB")]),
                         {"/dev/vg/data": 5, "/dev/vg/logs": 10})

    def test_fractional_sizes_round_up_to_an_extent(self):
        self.assertEqual(self.allocate([("/data", "5MiB")])["/dev/vg/data"], 2)

    def test_hundred_percent_rounding_consumes_all_and_is_order_independent(self):
        specs = [("/data", "50%"), ("/logs", "50%")]
        forward = self.allocate(specs, free=5)
        self.assertEqual(sum(forward.values()), 5)
        self.assertEqual(forward, self.allocate(list(reversed(specs)), free=5))

    def test_percentage_below_one_extent_is_zero_not_all(self):
        self.assertEqual(self.allocate([("/data", "1%"), ("/logs", "")], free=5),
                         {"/dev/vg/data": 0, "/dev/vg/logs": 5})

    def test_no_free_space_skips_even_fixed_size(self):
        self.assertEqual(self.allocate([("/data", "20GiB"), ("/logs", "")], free=0),
                         {"/dev/vg/data": 0, "/dev/vg/logs": 0})

    def test_multiple_unmarked_paths_are_a_conflict_even_when_full(self):
        for free in (0, 100):
            with self.subTest(free=free), self.assertRaisesRegex(ValueError, "same VG remainder"):
                self.allocate([("/data", ""), ("/logs", "")], free=free)

    def test_oversubscribed_fixed_percent_and_mixed_budgets(self):
        for specs in [[("/data", "401MiB")], [("/data", "60%"), ("/logs", "60%")],
                      [("/data", "80%"), ("/logs", "100MiB")], [("/data", "100%"), ("/logs", "1MiB")]]:
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                self.allocate(specs)


class Planning(unittest.TestCase):
    def test_latent_automatic_lvm_ambiguity_is_a_successful_skip(self):
        with patch.object(h, "load_lsblk", return_value=[root_disk()]), \
             patch.object(h, "process_target", side_effect=h.AmbiguousTarget("select a mountpoint")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(h.execute(argparse.Namespace(disk=[], dry_run=True, mountpoints="")), 0)
        self.assertIn("SKIP:", output.getvalue())

    def test_projected_partition_size_uses_growpart_sectors_and_logical_sector_size(self):
        import subprocess
        output = "CHANGE: partition=2 start=256 old: size=100 end=355 new: size=200 end=455"
        with patch.object(h, "partition_size", return_value=409600), \
             patch.object(h, "device_size", return_value=409600), \
             patch.object(h, "growpart_probe", return_value=subprocess.CompletedProcess([], 0, output, "")), \
             patch.object(h, "partition_table", return_value=dict(sectorsize=4096)):
            self.assertEqual(h.plan_partition("/dev/sda", "/dev/sda2")["expected"], 819200)

    def test_unreadable_growth_projection_is_not_guessed(self):
        import subprocess
        with patch.object(h, "partition_size", return_value=100), patch.object(h, "device_size", return_value=100), \
             patch.object(h, "growpart_probe", return_value=subprocess.CompletedProcess([], 0, "CHANGE: unknown", "")):
            with self.assertRaisesRegex(RuntimeError, "projected size"):
                h.plan_partition("/dev/sda", "/dev/sda2")

    def test_group_forecasts_new_pv_capacity_and_grows_shared_partition_once(self):
        targets = [selected_target("/data", "20MiB"), selected_target("/logs")]
        partition = dict(disk="/dev/sda", path="/dev/sda2", grow=True, current=401 * MIB, expected=801 * MIB)
        with patch.object(h, "filesystem_geometry", return_value=(1, 4096)), \
             patch.object(h, "device_size", return_value=401 * MIB), \
             patch.object(h, "plan_partition", return_value=partition) as plan, \
             patch.object(h, "lvm_state", return_value=state()):
            groups = h.prepare_explicit_targets(targets)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["forecast_free"], 100)
        self.assertEqual(groups[0]["allocations"], {"/dev/vg/data": 5, "/dev/vg/logs": 95})
        plan.assert_called_once()

    def test_insufficient_future_capacity_fails_before_partition_write(self):
        target = selected_target("/data", "500MiB")
        partition = dict(disk="/dev/sda", path="/dev/sda2", grow=True, current=401 * MIB, expected=801 * MIB)
        with patch.object(h, "filesystem_geometry", return_value=(1, 4096)), \
             patch.object(h, "device_size", return_value=401 * MIB), \
             patch.object(h, "plan_partition", return_value=partition), \
             patch.object(h, "lvm_state", return_value=state()), patch.object(h, "resize_partition") as resize:
            with self.assertRaisesRegex(ValueError, "Insufficient VG"):
                h.prepare_explicit_targets([target])
        resize.assert_not_called()

    def test_explicit_lvm_metadata_allows_other_lvs(self):
        segments = {"report": [{"seg": [dict(lv_path="/dev/vg/root", vg_name="vg", lv_attr="-wi-ao----", segtype="linear"),
                                        dict(lv_path="/dev/vg/logs", vg_name="vg", lv_attr="-wi-ao----", segtype="linear")]}]}
        import json
        target = lvm_target("data")
        target["explicit"] = True
        def reports(tool, fields, section):
            return [state()["pv"]] if tool == "pvs" else [dict(vg_name="vg", vg_extent_size=str(4 * MIB))]
        with patch.object(h, "report", side_effect=reports), patch.object(h, "run", return_value=json.dumps(segments)):
            self.assertEqual(h.lvm_state(target)["free"], 0)

    def test_apply_freezes_actual_pool_before_first_lv_extension(self):
        targets = [selected_target("/logs"), selected_target("/data", "20MiB")]
        actual = state(free=100, count=200)
        group = dict(targets=targets, state=state(), partitions=[], allocations={})
        with patch.object(h, "lvm_state", return_value=actual) as metadata, \
             patch.object(h, "device_size", return_value=801 * MIB), \
             patch.object(h, "grow_filesystem", return_value=True), patch.object(h, "run") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            h.process_explicit_group(group, False)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["lvextend", "-l", "+95", "/dev/vg/logs", "/dev/sda2"],
            ["lvextend", "-l", "+5", "/dev/vg/data", "/dev/sda2"]])
        metadata.assert_called_once()

    def test_dry_run_has_no_write_commands(self):
        target = selected_target("/data")
        group = dict(targets=[target], state=state(), partitions=[], allocations={target["device"]: 0})
        with patch.object(h, "resize_partition") as resize, patch.object(h, "run") as run, \
             patch.object(h, "grow_filesystem") as fs, contextlib.redirect_stdout(io.StringIO()):
            h.process_explicit_group(group, True)
        resize.assert_not_called()
        run.assert_not_called()
        fs.assert_not_called()

    def test_runtime_space_change_is_checked_before_lv_writes(self):
        target = selected_target("/data", "20MiB")
        group = dict(targets=[target], state=state(), partitions=[], allocations={target["device"]: 5})
        with patch.object(h, "lvm_state", return_value=state(free=3, count=200)), \
             patch.object(h, "device_size", return_value=801 * MIB), patch.object(h, "run") as run:
            with self.assertRaisesRegex(ValueError, "Insufficient VG"):
                h.process_explicit_group(group, False)
        run.assert_not_called()

    def test_no_space_is_success_and_can_still_finish_filesystem_growth(self):
        target = selected_target("/data", "20GiB")
        group = dict(targets=[target], state=state(), partitions=[], allocations={target["device"]: 0})
        with patch.object(h, "lvm_state", return_value=state()), patch.object(h, "device_size", return_value=401 * MIB), \
             patch.object(h, "run") as run, patch.object(h, "grow_filesystem", return_value=True) as fs, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            h.process_explicit_group(group, False)
        run.assert_not_called()
        fs.assert_called_once_with(target)
        self.assertIn("SKIP:", output.getvalue())

    def test_filesystem_error_does_not_skip_other_requested_filesystems(self):
        targets = [selected_target("/data", "50%"), selected_target("/logs", "50%")]
        group = dict(targets=targets, state=state(), partitions=[], allocations={})
        with patch.object(h, "lvm_state", return_value=state()), patch.object(h, "device_size", return_value=401 * MIB), \
             patch.object(h, "grow_filesystem", side_effect=[RuntimeError("resize failed"), True]) as fs, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "resize failed"):
                h.process_explicit_group(group, False)
        self.assertEqual(fs.call_count, 2)

    def test_preflight_error_prevents_automatic_root_growth(self):
        args = argparse.Namespace(disk=[], dry_run=False, mountpoints="/data=20GiB")
        data = node("sdb", "disk", children=[node("sdb1", "part", "LVM2_member", children=[
            node("vg-data", "lvm", "ext4", "/data")])])
        with patch.object(h, "load_lsblk", return_value=[root_disk(), data]), \
             patch.object(h, "rescan_block_devices"), \
             patch.object(h, "prepare_explicit_targets", side_effect=ValueError("budget conflict")), \
             patch.object(h, "process_target") as automatic:
            with self.assertRaisesRegex(ValueError, "budget conflict"):
                h.execute(args)
        automatic.assert_not_called()

    def test_unknown_mountpoint_prevents_rescan_and_all_writes(self):
        with patch.object(h, "load_lsblk", return_value=[root_disk()]), \
             patch.object(h, "rescan_block_devices") as rescan, patch.object(h, "process_target") as automatic:
            with self.assertRaisesRegex(RuntimeError, "exact mounted filesystem"):
                h.execute(argparse.Namespace(disk=[], dry_run=False, mountpoints="/typo"))
        rescan.assert_not_called()
        automatic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
