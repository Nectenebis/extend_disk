#!/usr/bin/env python3
"""Grow supported Linux root/data filesystems without guessing disk geometry."""
import argparse
import contextlib
from decimal import Decimal
import fcntl
import json
import os
import re
import subprocess
import sys
import time

SUPPORTED_FS = {"xfs", "ext4"}
SUPPORTED_DISK_RE = r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)$"


def command(argv):
    return subprocess.run(argv, text=True, capture_output=True, env=dict(os.environ, LC_ALL="C"))


def checked(result, argv):
    if result.returncode:
        raise RuntimeError("Command failed ({0}): {1}\n{2}\n{3}".format(
            result.returncode, " ".join(argv), result.stdout.strip(), result.stderr.strip()))
    return result.stdout.strip()


def run(argv):
    return checked(command(argv), argv)


def walk(nodes, parent=None):
    for node in nodes:
        item = dict(node, parent=parent)
        yield item
        yield from walk(node.get("children") or [], item)


def load_lsblk():
    columns = "NAME,PATH,TYPE,PKNAME,FSTYPE,MOUNTPOINTS,RM,SIZE"
    argv = ["lsblk", "-b", "-J", "-o", columns]
    result = command(argv)
    if result.returncode and "unknown column" in result.stderr.lower():
        argv[-1] = columns.replace("MOUNTPOINTS", "MOUNTPOINT")
        result = command(argv)
    return json.loads(checked(result, argv))["blockdevices"]


def mountpoints(node):
    points = node.get("mountpoints", node.get("mountpoint")) or []
    return [p for p in ([points] if isinstance(points, str) else points) if p]


def node_fstype(node):
    if node.get("fstype"):
        return node["fstype"]
    points = mountpoints(node)
    return run(["findmnt", "-n", "-o", "FSTYPE", points[0]]) if points else None


def can_grow_filesystem(node):
    fs = node_fstype(node)
    return fs == "ext4" or (fs == "xfs" and bool(mountpoints(node)))


def disk_for(node):
    while node and node.get("type") != "disk":
        node = node.get("parent")
    return node


def part_number(path):
    match = re.search(r"([0-9]+)$", path)
    if not match:
        raise RuntimeError("Cannot detect partition number: " + path)
    return match.group(1)


def integer(value):
    return int(Decimal(str(value).strip()))


def device_size(path):
    return integer(run(["blockdev", "--getsize64", path]))


def supported_disk(node):
    return (node.get("type") == "disk" and node.get("rm") in (0, "0", False, None)
            and re.fullmatch(SUPPORTED_DISK_RE, node.get("name", "")) is not None)


def find_root_node(nodes):
    for node in nodes:
        if "/" in mountpoints(node):
            return node
    source = os.path.realpath(run(["findmnt", "-n", "-o", "SOURCE", "/"]))
    return next((n for n in nodes if os.path.realpath(n["path"]) == source), None)


def discover_targets(blockdevices, selected=None):
    nodes = list(walk(blockdevices))
    root = find_root_node(nodes)
    if root is None and not selected:
        raise RuntimeError("Cannot identify the root block device; use --disk for explicitly selected data disks")
    root_disk = disk_for(root)
    targets, skipped = [], []

    def add(device, kind, disk, pv=None):
        if not supported_disk(disk):
            skipped.append("SKIP: unsupported physical disk for " + device["path"])
            return
        if not can_grow_filesystem(device):
            skipped.append("SKIP: unsupported or unmounted filesystem: " + device["path"])
            return
        parent = device.get("parent") or {}
        if pv is None and (device.get("type") != "part" or parent.get("type") != "disk"
                           or device.get("children")):
            skipped.append("SKIP: unsupported storage layers: " + device["path"])
            return
        if pv is not None and not (pv.get("type") == "disk" or
                                    (pv.get("type") == "part" and pv.get("parent", {}).get("type") == "disk")):
            skipped.append("SKIP: unsupported PV storage layers: " + device["path"])
            return
        targets.append(dict(kind=kind, device=device["path"], disk=disk, pv=pv,
                            part=(pv if pv and pv["type"] == "part" else device if pv is None else None),
                            fs=node_fstype(device), mountpoint=(mountpoints(device) or [None])[0]))

    if root and root_disk and (not selected or root_disk["path"] in selected):
        if root["type"] in ("part", "lvm"):
            add(root, "system", root_disk, root.get("parent") if root["type"] == "lvm" else None)
        else:
            skipped.append("SKIP: unsupported root storage layout")

    for disk in (n for n in nodes if supported_disk(n)):
        if (root_disk and disk["path"] == root_disk["path"]) or (selected and disk["path"] not in selected):
            continue
        children = [n for n in nodes if n.get("parent") is disk]
        parts = [n for n in children if n["type"] == "part"]
        if not parts:
            lvs = [n for n in children if n["type"] == "lvm"]
            if len(lvs) == 1 and len(children) == 1:
                add(lvs[0], "data", disk, disk)
            else:
                skipped.append("SKIP: unsupported or ambiguous disk layout: " + disk["path"])
        elif len(parts) == 1 and len(children) == 1:
            part = parts[0]
            descendants = [n for n in nodes if n.get("parent") is part]
            if not descendants:
                add(part, "data", disk)
            elif len(descendants) == 1 and descendants[0]["type"] == "lvm":
                add(descendants[0], "data", disk, part)
            else:
                skipped.append("SKIP: multiple LVs or unsupported storage layers: " + part["path"])
        else:
            skipped.append("SKIP: data disk must have exactly one partition: " + disk["path"])
    unique = {}
    for target in targets:
        unique.setdefault(target["device"], target)
    return list(unique.values()), skipped


def rescan_block_devices(disks):
    for disk in disks:
        name = disk["name"]
        paths = ["/sys/class/block/{0}/device/rescan".format(name)]
        if name.startswith("nvme"):
            controller = re.match(r"nvme[0-9]+", name).group()
            paths.append("/sys/class/nvme/{0}/rescan_controller".format(controller))
        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "w", encoding="ascii") as stream:
                    stream.write("1\n")
                print("OK: rescanned " + disk["path"])
                break
            except OSError as error:
                print("WARN: cannot rescan {0}: {1}".format(disk["path"], error), file=sys.stderr)
    run(["udevadm", "settle"])


def partition_size(disk_path, part_path):
    table = json.loads(run(["sfdisk", "--json", disk_path]))["partitiontable"]
    if table["label"] not in ("gpt", "dos"):
        raise RuntimeError("Unsupported partition table on " + disk_path)
    for part in table["partitions"]:
        if os.path.realpath(part["node"]) == os.path.realpath(part_path):
            return integer(part["size"]) * integer(table["sectorsize"])
    raise RuntimeError("Partition not found in on-disk table: " + part_path)


def probe_partition(disk_path, part_path):
    argv = ["growpart", "--dry-run", "--update=off", disk_path, part_number(part_path)]
    result = command(argv)
    if result.returncode == 1 and result.stdout.startswith("NOCHANGE:"):
        return False
    if result.returncode == 0 and result.stdout.startswith("CHANGE:"):
        return True
    raise RuntimeError("Cannot plan partition growth: {0}\n{1}\n{2}".format(
        " ".join(argv), result.stdout.strip(), result.stderr.strip()))


def resize_partition(disk_path, part_path, grow):
    before = partition_size(disk_path, part_path)
    if grow:
        argv = ["growpart", "--update=on", disk_path, part_number(part_path)]
        result = command(argv)
        after = partition_size(disk_path, part_path)
        if after < before:
            raise RuntimeError("Partition unexpectedly shrank: " + part_path)
        if after > before:
            print("CHANGED: partition " + part_path)
        if result.returncode and not (result.returncode == 1 and result.stdout.startswith("NOCHANGE:")):
            checked(result, argv)
    expected = partition_size(disk_path, part_path)
    current = device_size(part_path)
    if current > expected:
        raise RuntimeError("Kernel size exceeds the on-disk partition size; refusing filesystem resize: " + part_path)
    if current < expected:
        argv = ["partx", "--update", "--nr", part_number(part_path), disk_path]
        result = command(argv)
        if result.returncode:
            print("WARN: " + result.stderr.strip(), file=sys.stderr)
        run(["udevadm", "settle"])
        for _ in range(10):
            if device_size(part_path) >= expected:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Kernel still sees the old size of {0}; stop before PV/filesystem resize. "
                               "A reboot may be required; rerun afterwards.".format(part_path))


def report(tool, fields, section):
    data = json.loads(run([tool, "--readonly", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", fields]))
    return [{key: str(value).strip() for key, value in row.items()}
            for group in data["report"] for row in group.get(section, [])]


def lvm_state(target):
    pvs = report("pvs", "pv_name,vg_name,pe_start,pv_pe_count,pv_pe_alloc_count", "pv")
    matching = [pv for pv in pvs if os.path.realpath(pv["pv_name"]) == os.path.realpath(target["pv"]["path"])]
    if len(matching) != 1 or not matching[0]["vg_name"]:
        raise RuntimeError("Cannot identify PV/VG for " + target["device"])
    pv = matching[0]
    vg = pv["vg_name"]
    if len([item for item in pvs if item["vg_name"] == vg]) != 1:
        raise RuntimeError("Refusing multi-PV VG {0}: allocation policy must be explicit".format(vg))
    argv = ["lvs", "--readonly", "--segments", "--reportformat", "json", "-o", "lv_path,vg_name,lv_attr,segtype"]
    lvs = [row for group in json.loads(run(argv))["report"] for row in group["seg"]]
    lvs = [{key: str(value).strip() for key, value in lv.items()} for lv in lvs]
    in_vg = [lv for lv in lvs if lv["vg_name"] == vg]
    matching = [lv for lv in in_vg if os.path.realpath(lv["lv_path"]) == os.path.realpath(target["device"])]
    if not matching or any(lv["segtype"] != "linear" or not lv["lv_attr"].startswith("-") for lv in matching):
        raise RuntimeError("Only ordinary linear LVs are supported: " + target["device"])
    if target["kind"] == "data" and len({lv["lv_path"] for lv in in_vg}) != 1:
        raise RuntimeError("Refusing to choose between multiple data LVs in VG " + vg)
    vgs = report("vgs", "vg_name,vg_extent_size", "vg")
    extent_size = integer(next(row["vg_extent_size"] for row in vgs if row["vg_name"] == vg))
    return dict(pv=pv, extent_size=extent_size,
                free=integer(pv["pv_pe_count"]) - integer(pv["pv_pe_alloc_count"]))


def filesystem_geometry(target):
    if target["fs"] == "ext4":
        output = run(["dumpe2fs", "-h", target["device"]])
        count = re.search(r"^Block count:\s+(\d+)\s*$", output, re.M)
        size = re.search(r"^Block size:\s+(\d+)\s*$", output, re.M)
        if count and size:
            return int(count.group(1)), int(size.group(1))
    else:
        output = run(["xfs_info", target["mountpoint"]])
        match = re.search(r"data\s*=\s*bsize=(\d+)\s+blocks=(\d+)", output)
        if match:
            return int(match.group(2)), int(match.group(1))
    raise RuntimeError("Cannot read filesystem geometry for " + target["device"])


def grow_filesystem(target):
    before, block_size = filesystem_geometry(target)
    capacity = device_size(target["device"]) // block_size
    if capacity < before:
        raise RuntimeError("Filesystem exceeds its block device size: " + target["device"])
    if capacity == before:
        return
    if target["fs"] == "ext4":
        run(["resize2fs", target["device"]])
    else:
        run(["xfs_growfs", "-d", target["mountpoint"]])
    after, _ = filesystem_geometry(target)
    if after > before:
        print("CHANGED: filesystem " + target["device"])


def process_target(target, dry_run):
    disk_path = target["disk"]["path"]
    part = target["part"]
    state = lvm_state(target) if target["pv"] else None
    filesystem_geometry(target)  # Validate all layers before the first write.
    grow = probe_partition(disk_path, part["path"]) if part else False
    if dry_run:
        if part:
            current = device_size(part["path"])
            on_disk = partition_size(disk_path, part["path"])
            print("DRY-RUN: {0}: partition growth={1}, kernel-size-sync={2}".format(
                part["path"], grow, current < on_disk))
        if state:
            print("DRY-RUN: {0}: PV free extents={1}; check PV capacity, then grow only this LV on this PV".format(
                target["device"], state["free"]))
        print("DRY-RUN: check/grow {0} filesystem {1}".format(target["fs"], target["device"]))
        return
    if part:
        resize_partition(disk_path, part["path"], grow)
    if state:
        pv_path = target["pv"]["path"]
        pv = state["pv"]
        capacity = (device_size(pv_path) - integer(pv["pe_start"])) // state["extent_size"]
        if capacity < integer(pv["pv_pe_count"]):
            raise RuntimeError("PV metadata exceeds its block device size: " + pv_path)
        if capacity > integer(pv["pv_pe_count"]):
            run(["pvresize", pv_path])
            refreshed = lvm_state(target)
            if integer(refreshed["pv"]["pv_pe_count"]) > integer(pv["pv_pe_count"]):
                print("CHANGED: PV " + pv_path)
            state = refreshed
        if state["free"] > 0:
            run(["lvextend", "-l", "+{0}".format(state["free"]), target["device"], pv_path])
            print("CHANGED: LV " + target["device"])
    # Reruns must finish an FS resize even if partition/PV/LV already grew.
    grow_filesystem(target)


@contextlib.contextmanager
def mutation_lock():
    with open("/run/lock/extend-disk.lock", "a", encoding="ascii") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another extend-disk process is running")
        yield


def execute(args):
    devices = load_lsblk()
    selected = {os.path.realpath(path) for path in args.disk}
    known = {n["path"] for n in walk(devices) if supported_disk(n)}
    if selected - known:
        raise RuntimeError("Unknown or unsupported selected disks: " + ", ".join(sorted(selected - known)))
    if not args.dry_run:
        rescan_block_devices([n for n in walk(devices) if supported_disk(n) and (not selected or n["path"] in selected)])
        devices = load_lsblk()
    else:
        print("DRY-RUN: no device rescan; planning uses sizes currently visible to the kernel")
    targets, skipped = discover_targets(devices, selected)
    for line in skipped:
        print(line)
    errors = []
    for target in targets:
        print("FOUND: {0} {1}".format(target["kind"], target["device"]))
        try:
            process_target(target, args.dry_run)
        except Exception as error:
            errors.append("ERROR: {0}: {1}".format(target["device"], error))
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print("OK: supported targets processed" if targets else "OK: no supported targets found")
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect only; do not rescan or resize devices")
    parser.add_argument("--disk", action="append", default=[], help="limit processing to this disk; repeat as needed")
    args = parser.parse_args()
    if args.dry_run:
        return execute(args)
    if os.geteuid() != 0:
        raise RuntimeError("Resizing requires root; use --dry-run for inspection")
    with mutation_lock():
        return execute(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
