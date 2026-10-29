"""Validate the actual Ansible argv expression; run after installing ansible-core."""
from pathlib import Path
import yaml
from jinja2 import Environment
from ansible.plugins.filter.core import FilterModule

tasks = yaml.safe_load((Path(__file__).resolve().parents[1] / "tasks/main.yml").read_text())
expression = next(task["ansible.builtin.command"]["argv"] for task in tasks if "ansible.builtin.command" in task)
environment = Environment()
environment.filters.update(FilterModule().filters())
cases = 0
for dry_run in (False, True):
    for disks in ([], ["/dev/sda", "/dev/disk/by-id/scsi-example"]):
        for mountpoints in ("", "  ", "/data=20GiB, /logs", "/data=40% /logs=60%"):
            actual = environment.compile_expression(expression.strip()[2:-2])(
                extend_disk_dry_run=dry_run, extend_disk_devices=disks, extend_disk_mountpoints=mountpoints)
            expected = ["/usr/local/sbin/extend-free-space"]
            expected += ["--dry-run"] if dry_run else []
            expected += ["--mountpoints=" + mountpoints] if mountpoints.strip() else []
            expected += ["--disk=" + path for path in disks]
            assert actual == expected, (actual, expected)
            cases += 1
print("Ansible argv: {0} cases passed".format(cases))
