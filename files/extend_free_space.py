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

PROTECTED_PARTTYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",  # EFI System
    "21686148-6449-6e6f-744e-656564454649",  # BIOS boot
    "bc13c2ff-59e6-4262-a352-b275fd6f7172",  # Extended Boot Loader
    "0xef", "ef",  # MBR EFI System
}
SUPPORTED_DISK_RE = r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)$"


class AmbiguousTarget(RuntimeError):
    """Automatic selection needs an explicit mounted filesystem."""


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
    columns = "NAME,PATH,TYPE,PKNAME,FSTYPE,MOUNTPOINTS,RM,SIZE,PARTTYPE"
    argv = ["lsblk", "-b", "-J", "-o", columns]
    result = command(argv)
    if result.returncode and "unknown column" in result.stderr.lower():
        argv[-1] = columns.replace("MOUNTPOINTS", "MOUNTPOINT")
        result = command(argv)
    return json.loads(checked(result, argv))["blockdevices"]


def mountpoints(node):
    points = node.get("mountpoints", node.get("mountpoint")) or []
    return [p for p in ([points] if isinstance(points, str) else points) if p]


def protected_mountpoint(path):
    path = os.path.normpath(path)
    return any(path == base or path.startswith(base + "/") for base in ("/boot", "/efi"))


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


def parse_mountpoints(value):
    requests = {}
    units = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
             "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}
    for entry in re.split(r"[,\s]+", value.strip()):
        if not entry:
            continue
        path, separator, amount = entry.partition("=")
        if not path.startswith("/"):
            raise ValueError("Use an absolute mountpoint: " + entry)
        path = os.path.normpath(path)
        if protected_mountpoint(path):
            raise ValueError("Boot filesystems are excluded; manage them manually: " + path)
        allocation = dict(type="all")
        if separator:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(%|B|[KMGT]i?B)", amount, re.I)
            if not match or Decimal(match.group(1)) <= 0:
                raise ValueError("Invalid allocation {0}; use e.g. /data=20GiB, /logs=40%, /archive".format(entry))
            number, unit = Decimal(match.group(1)), match.group(2).upper()
            if unit == "%":
                if number > 100:
                    raise ValueError("Percentage exceeds 100: " + entry)
                allocation = dict(type="percent", value=number)
            else:
                size = number * units[unit]
                if size != int(size):
                    raise ValueError("Size must be a whole number of bytes: " + entry)
                allocation = dict(type="bytes", value=int(size))
        if path in requests and requests[path]["allocation"] != allocation:
            raise ValueError("Conflicting allocations for mountpoint " + path)
        requests[path] = dict(mountpoint=path, allocation=allocation)
    return list(requests.values())


def discover_targets(blockdevices, selected=None, requests=None):
    nodes = list(walk(blockdevices))
    protected = {os.path.realpath(n["path"]) for n in nodes
                 if any(protected_mountpoint(p) for p in mountpoints(n))
                 or (n.get("parttype") or "").lower() in PROTECTED_PARTTYPES}
    root = find_root_node(nodes)
    if root is None and not selected:
        raise RuntimeError("Cannot identify the root block device; use --disk for explicitly selected data disks")
    root_disk = disk_for(root)
    targets, skipped = [], []

    def add(device, kind, disk, pv=None, request=None):
        def reject(message):
            if request:
                raise RuntimeError(message)
            skipped.append("SKIP: " + message)

        ancestor = device
        while ancestor and ancestor.get("type") != "disk":
            if os.path.realpath(ancestor["path"]) in protected:
                reject("boot filesystem/partition is excluded; manage it manually: " + device["path"])
                return
            ancestor = ancestor.get("parent")
        if not supported_disk(disk):
            reject("unsupported physical disk for " + device["path"])
            return
        if not can_grow_filesystem(device):
            reject("unsupported or unmounted filesystem {0}: {1}".format(node_fstype(device) or "unknown", device["path"]))
            return
        parent = device.get("parent") or {}
        if pv is None and (device.get("type") != "part" or parent.get("type") != "disk"
                           or device.get("children")):
            reject("unsupported storage layers: " + device["path"])
            return
        if pv is not None and not (pv.get("type") == "disk" or
                                    (pv.get("type") == "part" and pv.get("parent", {}).get("type") == "disk")):
            reject("unsupported PV storage layers: " + device["path"])
            return
        if request and pv is None and request["allocation"]["type"] != "all":
            raise RuntimeError("Sizes and percentages require an LVM logical volume: " + request["mountpoint"])
        targets.append(dict(kind=kind, device=device["path"], disk=disk, pv=pv,
                            part=(pv if pv and pv["type"] == "part" else device if pv is None else None),
                            fs=node_fstype(device), mountpoint=(request["mountpoint"] if request else
                                                              (mountpoints(device) or [None])[0]),
                            explicit=bool(request), allocation=(request["allocation"] if request else dict(type="all"))))

    overridden = set()
    explicit_devices = {}
    for request in requests or []:
        matches = [n for n in nodes if request["mountpoint"] in mountpoints(n)]
        if not matches:
            raise RuntimeError("Not an exact mounted filesystem: " + request["mountpoint"])
        disks = {disk_for(n)["path"] for n in matches if disk_for(n)}
        if len(disks) != 1:
            raise RuntimeError("Mountpoint does not resolve to one physical disk: " + request["mountpoint"])
        device, disk = matches[0], disk_for(matches[0])
        if selected and disk["path"] not in selected:
            raise RuntimeError("Mountpoint is outside the --disk selection: " + request["mountpoint"])
        key = os.path.realpath(device["path"])
        if key in explicit_devices:
            if explicit_devices[key] != request["allocation"]:
                raise RuntimeError("Conflicting mountpoint aliases for " + device["path"])
            continue
        if device["type"] not in ("part", "lvm"):
            raise RuntimeError("Unsupported selected storage layout: " + request["mountpoint"])
        add(device, "system" if root_disk and disk["path"] == root_disk["path"] else "data", disk,
            device.get("parent") if device["type"] == "lvm" else None, request)
        overridden.add(disk["path"])
        explicit_devices[key] = request["allocation"]

    if root and root_disk and root_disk["path"] not in overridden and (not selected or root_disk["path"] in selected):
        if root["type"] in ("part", "lvm"):
            add(root, "system", root_disk, root.get("parent") if root["type"] == "lvm" else None)
        else:
            skipped.append("SKIP: unsupported root storage layout")

    for disk in (n for n in nodes if supported_disk(n)):
        if (disk["path"] in overridden or (root_disk and disk["path"] == root_disk["path"])
                or (selected and disk["path"] not in selected)):
            continue
        children = [n for n in nodes if n.get("parent") is disk]
        parts = [n for n in children if n["type"] == "part"]
        if not parts:
            lvs = [n for n in children if n["type"] == "lvm"]
            if len(lvs) == 1 and len(children) == 1:
                add(lvs[0], "data", disk, disk)
            else:
                skipped.append(ambiguity_message(disk, nodes, "unsupported or ambiguous disk layout"))
        elif len(parts) == 1 and len(children) == 1:
            part = parts[0]
            descendants = [n for n in nodes if n.get("parent") is part]
            if not descendants:
                add(part, "data", disk)
            elif len(descendants) == 1 and descendants[0]["type"] == "lvm":
                add(descendants[0], "data", disk, part)
            else:
                skipped.append(ambiguity_message(disk, nodes, "multiple LVs or unsupported storage layers"))
        else:
            skipped.append(ambiguity_message(disk, nodes, "data disk must have exactly one partition for automatic selection"))
    unique = {}
    for target in targets:
        unique.setdefault(target["device"], target)
    return list(unique.values()), skipped


def ambiguity_message(disk, nodes, reason):
    candidates = sorted({point for node in nodes if disk_for(node) is disk for point in mountpoints(node)})
    return "SKIP: {0}: {1}. Select the filesystem in extend_disk_mountpoints / --mountpoints{2}".format(
        disk["path"], reason, ": " + ", ".join(candidates) if candidates else "")


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


def partition_table(disk_path):
    table = json.loads(run(["sfdisk", "--json", disk_path]))["partitiontable"]
    if table["label"] not in ("gpt", "dos"):
        raise RuntimeError("Unsupported partition table on " + disk_path)
    return table


def partition_size(disk_path, part_path):
    table = partition_table(disk_path)
    for part in table["partitions"]:
        if os.path.realpath(part["node"]) == os.path.realpath(part_path):
            return integer(part["size"]) * integer(table["sectorsize"])
    raise RuntimeError("Partition not found in on-disk table: " + part_path)


def growpart_probe(disk_path, part_path):
    argv = ["growpart", "--dry-run", "--update=off", disk_path, part_number(part_path)]
    result = command(argv)
    if result.returncode == 1 and result.stdout.startswith("NOCHANGE:"):
        return result
    if result.returncode == 0 and result.stdout.startswith("CHANGE:"):
        return result
    raise RuntimeError("Cannot plan partition growth: {0}\n{1}\n{2}".format(
        " ".join(argv), result.stdout.strip(), result.stderr.strip()))


def probe_partition(disk_path, part_path):
    return growpart_probe(disk_path, part_path).returncode == 0


def plan_partition(disk_path, part_path):
    before = partition_size(disk_path, part_path)
    current = device_size(part_path)
    if current > before:
        raise RuntimeError("Kernel size exceeds the on-disk partition size: " + part_path)
    result = growpart_probe(disk_path, part_path)
    grow = result.returncode == 0
    expected = before
    if grow:
        match = re.search(r"\bnew:\s+size=(\d+)\b", result.stdout)
        if not match:
            raise RuntimeError("Cannot read growpart projected size: " + result.stdout)
        expected = int(match.group(1)) * integer(partition_table(disk_path)["sectorsize"])
        if expected <= before:
            raise RuntimeError("Invalid projected partition size: " + part_path)
    return dict(disk=disk_path, path=part_path, grow=grow, expected=expected, current=current,
                reason=result.stdout.strip())


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
    if target["kind"] == "data" and not target.get("explicit") and len({lv["lv_path"] for lv in in_vg}) != 1:
        raise AmbiguousTarget("Refusing to choose between multiple data LVs in VG {0}; "
                              "select a mountpoint in extend_disk_mountpoints / --mountpoints".format(vg))
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
        return False
    if target["fs"] == "ext4":
        run(["resize2fs", target["device"]])
    else:
        run(["xfs_growfs", "-d", target["mountpoint"]])
    after, _ = filesystem_geometry(target)
    if after > before:
        print("CHANGED: filesystem " + target["device"])
    return after > before


def allocate_extents(targets, free, extent_size):
    remainders = [t for t in targets if t["allocation"]["type"] == "all"]
    if len(remainders) > 1:
        raise ValueError("Several paths would consume the same VG remainder; specify sizes/percentages for all but one")
    percentages = sum((t["allocation"]["value"] for t in targets
                       if t["allocation"]["type"] == "percent"), Decimal(0))
    if percentages > 100:
        raise ValueError("Combined percentages in one VG exceed 100%")
    fixed = any(t["allocation"]["type"] == "bytes" for t in targets)
    if percentages == 100 and fixed:
        raise ValueError("100% of VG free space leaves no room for fixed-size requests")
    allocations = {t["device"]: 0 for t in targets}
    if free == 0:
        return allocations
    fractions = {}
    for target in targets:
        spec = target["allocation"]
        if spec["type"] == "bytes":
            allocations[target["device"]] = (spec["value"] + extent_size - 1) // extent_size
        elif spec["type"] == "percent":
            value = Decimal(free) * spec["value"] / 100
            allocations[target["device"]] = int(value)
            fractions[target["device"]] = value - int(value)
    requested = sum(allocations.values())
    if requested > free:
        raise ValueError("Insufficient VG space: requested {0} extents ({1} bytes), available {2} ({3} bytes)".format(
            requested, requested * extent_size, free, free * extent_size))
    if remainders:
        allocations[remainders[0]["device"]] = free - requested
    elif percentages == 100:
        # Largest remainder rounding consumes all extents, independent of input order.
        for device in sorted(fractions, key=lambda item: (-fractions[item], item))[:free - requested]:
            allocations[device] += 1
    return allocations


def prepare_explicit_targets(targets):
    groups, partitions = {}, {}
    for target in targets:
        if not target.get("explicit"):
            continue
        blocks, block_size = filesystem_geometry(target)
        if blocks * block_size > device_size(target["device"]):
            raise RuntimeError("Filesystem exceeds its block device size: " + target["device"])
        part = target["part"]
        partition = None
        if part:
            key = (target["disk"]["path"], part["path"])
            if key not in partitions:
                partitions[key] = plan_partition(*key)
            partition = partitions[key]
        state = lvm_state(target) if target["pv"] else None
        key = ("vg", state["pv"]["vg_name"]) if state else ("partition", target["device"])
        if key not in groups:
            groups[key] = dict(targets=[], state=state, partitions=[], allocations={})
        group = groups[key]
        group["targets"].append(target)
        if partition and partition not in group["partitions"]:
            group["partitions"].append(partition)
    for group in groups.values():
        state = group["state"]
        if state is None:
            continue
        target = group["targets"][0]
        if len(group["partitions"]) > 1:
            raise RuntimeError("Several PV partitions in one VG are not supported")
        expected = (group["partitions"][0]["expected"] if group["partitions"] else
                    device_size(target["pv"]["path"]))
        count = (expected - integer(state["pv"]["pe_start"])) // state["extent_size"]
        if count < integer(state["pv"]["pv_pe_count"]):
            raise RuntimeError("PV metadata exceeds its projected block device size: " + target["pv"]["path"])
        group["forecast_free"] = count - integer(state["pv"]["pv_pe_alloc_count"])
        try:
            group["allocations"] = allocate_extents(group["targets"], group["forecast_free"], state["extent_size"])
        except ValueError as error:
            raise ValueError("VG {0} ({1}): {2}".format(state["pv"]["vg_name"],
                             ", ".join(t["mountpoint"] for t in group["targets"]), error))
    return list(groups.values())


def process_explicit_group(group, dry_run):
    state = group["state"]
    for partition in group["partitions"]:
        if dry_run:
            print("DRY-RUN: {0}: partition growth={1}, projected bytes={2}".format(
                partition["path"], partition["grow"], partition["expected"]))
        else:
            resize_partition(partition["disk"], partition["path"], partition["grow"])
            if not partition["grow"] and partition["current"] == partition["expected"]:
                print("SKIP: {0}: {1}; checking filesystem capacity".format(
                    partition["path"], partition.get("reason", "no contiguous free space after partition")))
    allocations = group["allocations"]
    if state and not dry_run:
        target = group["targets"][0]
        pv_path = target["pv"]["path"]
        current = lvm_state(target)
        capacity = (device_size(pv_path) - integer(current["pv"]["pe_start"])) // current["extent_size"]
        if capacity < integer(current["pv"]["pv_pe_count"]):
            raise RuntimeError("PV metadata exceeds its block device size: " + pv_path)
        if capacity > integer(current["pv"]["pv_pe_count"]):
            run(["pvresize", pv_path])
            current = lvm_state(target)
        if integer(current["pv"]["pv_pe_count"]) > integer(state["pv"]["pv_pe_count"]):
            print("CHANGED: PV " + pv_path)
        # Freeze one actual post-pvresize snapshot for all LV allocations in the VG.
        allocations = allocate_extents(group["targets"], current["free"], current["extent_size"])
    for target in group["targets"]:
        if state:
            extents = allocations[target["device"]]
            print("{0}: {1}: add {2} extents ({3} bytes)".format(
                "DRY-RUN" if dry_run else "PLAN", target["mountpoint"], extents, extents * state["extent_size"]))
            if not dry_run and extents:
                run(["lvextend", "-l", "+{0}".format(extents), target["device"], target["pv"]["path"]])
                print("CHANGED: LV " + target["device"])
            if not extents:
                print("SKIP: {0}: no available extents for this allocation".format(target["mountpoint"]))
        if dry_run:
            print("DRY-RUN: check/grow {0} filesystem {1} ({2})".format(
                target["fs"], target["mountpoint"], target["device"]))
    errors = []
    if not dry_run:
        for target in group["targets"]:
            try:
                if not grow_filesystem(target):
                    print("SKIP: {0}: filesystem already uses the available block device capacity".format(target["mountpoint"]))
            except Exception as error:
                errors.append("{0}: {1}".format(target["mountpoint"], error))
    if errors:
        raise RuntimeError("; ".join(errors))


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
    changed = bool(grow)
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
                changed = True
            state = refreshed
        if state["free"] > 0:
            run(["lvextend", "-l", "+{0}".format(state["free"]), target["device"], pv_path])
            print("CHANGED: LV " + target["device"])
            changed = True
    # Reruns must finish an FS resize even if partition/PV/LV already grew.
    if not grow_filesystem(target) and not changed:
        print("SKIP: {0}: no additional usable space; filesystem already uses the available capacity".format(
            target["mountpoint"] or target["device"]))


@contextlib.contextmanager
def mutation_lock():
    with open("/run/lock/extend-disk.lock", "a", encoding="ascii") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another extend-disk process is running")
        yield


def execute(args):
    requests = parse_mountpoints(getattr(args, "mountpoints", ""))
    devices = load_lsblk()
    selected = {os.path.realpath(path) for path in args.disk}
    known = {n["path"] for n in walk(devices) if supported_disk(n)}
    if selected - known:
        raise RuntimeError("Unknown or unsupported selected disks: " + ", ".join(sorted(selected - known)))
    if requests:
        # Catch typos, unsupported targets and filter conflicts before rescanning.
        discover_targets(devices, selected, requests)
    if not args.dry_run:
        rescan_block_devices([n for n in walk(devices) if supported_disk(n) and (not selected or n["path"] in selected)])
        devices = load_lsblk()
    else:
        print("DRY-RUN: no device rescan; planning uses sizes currently visible to the kernel")
    targets, skipped = discover_targets(devices, selected, requests)
    for line in skipped:
        print(line)
    # All explicit budgets and conflicts are checked before ANY partition/PV/LV write.
    groups = prepare_explicit_targets(targets)
    errors = []
    for group in groups:
        label = ", ".join(t["mountpoint"] for t in group["targets"])
        print("FOUND: selected " + label)
        try:
            process_explicit_group(group, args.dry_run)
        except Exception as error:
            errors.append("ERROR: {0}: {1}".format(label, error))
    for target in targets:
        if target.get("explicit"):
            continue
        print("FOUND: {0} {1}".format(target["kind"], target["device"]))
        try:
            process_target(target, args.dry_run)
        except AmbiguousTarget as error:
            print("SKIP: {0}: {1}".format(target["device"], error))
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
    parser.add_argument("--mountpoints", default="", help="per-disk overrides: '/data=20GiB, /logs' or '/data=40%%, /logs=60%%'")
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
