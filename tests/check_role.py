"""Validate the actual Ansible argv expression; run after installing ansible-core."""
from pathlib import Path
import yaml
from jinja2 import Environment
from ansible.plugins.filter.core import FilterModule

tasks = yaml.safe_load((Path(__file__).resolve().parents[1] / "tasks/main.yml").read_text())
expression = next(task["ansible.builtin.command"]["argv"] for task in tasks if "ansible.builtin.command" in task)
environment = Environment()
environment.filters.update(FilterModule().filters())
for dry_run in (False, True):
    for disks in ([], ["/dev/sda", "/dev/disk/by-id/scsi-example"]):
        actual = environment.compile_expression(expression.strip()[2:-2])(
            extend_disk_dry_run=dry_run, extend_disk_devices=disks)
        expected = ["/usr/local/sbin/extend-free-space"]
        expected += ["--dry-run"] if dry_run else []
        expected += ["--disk=" + path for path in disks]
        assert actual == expected, (actual, expected)
print("Ansible argv: 4 cases passed")
