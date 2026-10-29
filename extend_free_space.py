#!/usr/bin/env python3
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time


SUPPORTED_FS = {"xfs", "ext4"}
SUPPORTED_DISK_RE = r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)$"


def run(command, check=True, input_text=None):
    result = subprocess.run(command, text=True, input=input_text, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            "Command failed: {0}\nstdout: {1}\nstderr: {2}".format(
                " ".join(command), result.stdout.strip(), result.stderr.strip()
            )
        )
    return result.stdout.strip()


def write_sysfs(path, value):
    try:
        with open(path, "w", encoding="ascii") as sysfs_file:
            sysfs_file.write(value)
        return True
    except OSError:
        return False


def is_supported_disk_name(name):
    return re.match(SUPPORTED_DISK_RE, name) is not None


def rescan_block_devices():
    rescanned = []

    for device_path in glob.glob("/sys/class/block/*"):
        name = os.path.basename(device_path)
        if not is_supported_disk_name(name):
            continue
        if write_sysfs(os.path.join(device_path, "device/rescan"), "1\n"):
            rescanned.append(name)

    for host_scan in glob.glob("/sys/class/scsi_host/host*/scan"):
        write_sysfs(host_scan, "- - -\n")

    run(["udevadm", "settle"], check=False)
    if rescanned:
        print("OK: rescanned disks: {0}".format(", ".join(sorted(rescanned))))


def load_lsblk():
    data = run(
        [
            "lsblk",
            "-b",
            "-J",
            "-o",
            "NAME,PATH,TYPE,PKNAME,FSTYPE,MOUNTPOINTS,RM,SIZE",
        ]
    )
    return json.loads(data)["blockdevices"]


def walk(nodes, parent=None):
    for node in nodes:
        item = dict(node)
        item["parent"] = parent
        yield item
        yield from walk(node.get("children", []) or [], item)


def mountpoints(node):
    points = node.get("mountpoints") or []
    if isinstance(points, str):
        points = [points]
    return [point for point in points if point]


def node_fstype(node):
    if node.get("fstype"):
        return node["fstype"]
    points = mountpoints(node)
    if not points:
        return None
    fstype = run(["findmnt", "-n", "-o", "FSTYPE", points[0]], check=False)
    return fstype or None


def can_grow_filesystem(node):
    fstype = node_fstype(node)
    return fstype == "ext4" or (fstype == "xfs" and len(mountpoints(node)) > 0)


def part_number(path):
    match = re.search(r"(?:p)?([0-9]+)$", path)
    if not match:
        raise RuntimeError("Cannot detect partition number for {0}".format(path))
    return match.group(1)


def bytes_from_lvm(value):
    return int(float(str(value).strip().lstrip("<")))


def node_size(node):
    return int(node.get("size") or 0)


def is_non_removable(node):
    return node.get("rm") in (0, "0", False, None)


def is_supported_disk(node):
    name = node.get("name") or os.path.basename(node.get("path") or "")
    return is_supported_disk_name(name)


def partition_has_tail_free_space(disk, part):
    parts = [child for child in disk.get("children", []) or [] if child.get("type") == "part"]
    if not parts:
        return False
    last_part = sorted(parts, key=lambda item: int(part_number(item["path"])))[-1]
    if last_part.get("path") != part.get("path"):
        return False
    return node_size(disk) - node_size(part) > 16 * 1024 * 1024


def bytes_available_for_whole_pv(pv_path):
    pv_data = run(["pvs", "--noheadings", "--nosuffix", "--units", "b", "-o", "pv_size,pv_free", pv_path])
    values = [bytes_from_lvm(value) for value in pv_data.split()]
    disk_size = int(run(["blockdev", "--getsize64", pv_path]))
    pv_size = values[0]
    pv_free = values[1]
    return max(disk_size - pv_size, 0) + pv_free


def find_root_node(nodes):
    for node in nodes:
        if "/" in mountpoints(node):
            return node
    root_source = os.path.realpath(run(["findmnt", "-n", "-o", "SOURCE", "/"]))
    for node in nodes:
        if os.path.realpath(node.get("path") or "") == root_source:
            return node
    return None


def disk_for(node):
    current = node
    while current and current.get("type") != "disk":
        current = current.get("parent")
    return current


def grow_fs(device, fstype, mountpoint, dry_run):
    if dry_run:
        return
    if fstype == "xfs":
        if not mountpoint:
            raise RuntimeError("XFS resize requires mounted filesystem on {0}".format(device))
        run(["xfs_growfs", mountpoint])
    elif fstype == "ext4":
        run(["resize2fs", device])


def resize_partition(disk_path, part_path, dry_run):
    if dry_run:
        return
    old_part_size = int(run(["blockdev", "--getsize64", part_path]))
    number = part_number(part_path)
    result = subprocess.run(
        ["parted", "-s", "-f", disk_path, "resizepart", number, "100%"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        run(
            ["parted", "---pretend-input-tty", disk_path, "resizepart", number, "100%"],
            input_text="Yes\n",
        )
    run(["partprobe", disk_path], check=False)
    run(["partx", "-u", disk_path], check=False)
    run(["blockdev", "--rereadpt", disk_path], check=False)
    run(["udevadm", "settle"], check=False)
    for _ in range(10):
        if int(run(["blockdev", "--getsize64", part_path])) > old_part_size:
            return
        time.sleep(1)


def extend_plain_partition(target, dry_run):
    resize_partition(target["disk"]["path"], target["part"]["path"], dry_run)
    grow_fs(
        target["part"]["path"],
        target["fs"],
        target["mountpoint"],
        dry_run,
    )


def extend_lvm(target, dry_run):
    pv = target["pv"]["path"]
    if target["pv"]["type"] == "part":
        resize_partition(target["disk"]["path"], pv, dry_run)
    if not dry_run:
        run(["pvresize", pv])
        pv_free = bytes_from_lvm(run(["pvs", "--noheadings", "--nosuffix", "--units", "b", "-o", "pv_free", pv]))
        if pv_free <= 16 * 1024 * 1024:
            return False
        run(["lvextend", "-r", "-l", "+100%FREE", target["lv"]["path"]])
    return True


def target_key(target):
    return "{0}:{1}".format(target["kind"], target["device"])


def discover_targets(blockdevices):
    nodes = list(walk(blockdevices))
    root_node = find_root_node(nodes)
    root_disk = disk_for(root_node) if root_node else None
    targets = []
    skipped = []

    root_fs = node_fstype(root_node) if root_node else None
    if root_node and root_fs in SUPPORTED_FS:
        if root_node.get("type") == "part":
            disk = disk_for(root_node)
            if disk and partition_has_tail_free_space(disk, root_node):
                targets.append(
                    {
                        "kind": "system plain partition",
                        "device": root_node["path"],
                        "disk": disk,
                        "part": root_node,
                        "fs": root_fs,
                        "mountpoint": "/",
                    }
                )
        elif root_node.get("type") == "lvm":
            pv = root_node.get("parent")
            disk = disk_for(root_node)
            if pv and disk:
                has_space = partition_has_tail_free_space(disk, pv) if pv.get("type") == "part" else bytes_available_for_whole_pv(pv["path"]) > 16 * 1024 * 1024
                if has_space:
                    targets.append(
                        {
                            "kind": "system lvm",
                            "device": root_node["path"],
                            "disk": disk,
                            "pv": pv,
                            "lv": root_node,
                            "fs": root_fs,
                            "mountpoint": "/",
                        }
                    )
    elif root_node:
        skipped.append("SKIP: system filesystem is not xfs/ext4: {0}".format(root_fs or "unknown"))

    for disk in [
        node
        for node in nodes
        if node.get("type") == "disk" and is_non_removable(node) and is_supported_disk(node)
    ]:
        if root_disk and disk.get("path") == root_disk.get("path"):
            continue

        children = disk.get("children", []) or []
        parts = [child for child in children if child.get("type") == "part"]
        disk_lvs = [
            child
            for child in children
            if child.get("type") == "lvm" and node_fstype(child) in SUPPORTED_FS and can_grow_filesystem(child)
        ]

        if len(parts) == 0 and len(disk_lvs) == 1:
            if bytes_available_for_whole_pv(disk["path"]) > 16 * 1024 * 1024:
                targets.append(
                    {
                        "kind": "data lvm on disk",
                        "device": disk_lvs[0]["path"],
                        "disk": disk,
                        "pv": disk,
                        "lv": disk_lvs[0],
                        "fs": node_fstype(disk_lvs[0]),
                        "mountpoint": (mountpoints(disk_lvs[0]) or [None])[0],
                    }
                )
            continue

        if len(parts) != 1:
            skipped.append("SKIP: {0} has {1} partitions".format(disk["path"], len(parts)))
            continue

        part = parts[0]
        if not partition_has_tail_free_space(disk, part):
            skipped.append(
                "SKIP: {0} has no tail free space by lsblk sizes: disk={1} part={2}".format(
                    disk["path"], node_size(disk), node_size(part)
                )
            )
            continue

        part_fs = node_fstype(part)
        if part_fs in SUPPORTED_FS and can_grow_filesystem(part):
            targets.append(
                {
                    "kind": "data plain partition",
                    "device": part["path"],
                    "disk": disk,
                    "part": part,
                    "fs": part_fs,
                    "mountpoint": (mountpoints(part) or [None])[0],
                }
            )
            continue

        children = part.get("children", []) or []
        lvs = [
            child
            for child in children
            if child.get("type") == "lvm" and node_fstype(child) in SUPPORTED_FS and can_grow_filesystem(child)
        ]
        if len(lvs) == 1:
            lv_fs = node_fstype(lvs[0])
            targets.append(
                {
                    "kind": "data lvm on partition",
                    "device": lvs[0]["path"],
                    "disk": disk,
                    "pv": part,
                    "lv": lvs[0],
                    "fs": lv_fs,
                    "mountpoint": (mountpoints(lvs[0]) or [None])[0],
                }
            )
        else:
            skipped.append(
                "SKIP: {0} has free space but no single mounted xfs/ext4 partition or LV".format(disk["path"])
            )

    seen = set()
    unique_targets = []
    for target in targets:
        key = target_key(target)
        if key not in seen:
            seen.add(key)
            unique_targets.append(target)
    return unique_targets, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rescan_block_devices()
    targets, skipped = discover_targets(load_lsblk())

    for line in skipped:
        print(line)

    if not targets:
        print("OK: no disks with supported free space found")
        return 0

    errors = []
    for target in targets:
        try:
            print("FOUND: {0} {1}".format(target["kind"], target["device"]))
            if args.dry_run:
                print("DRY-RUN: would extend {0}".format(target["device"]))
                continue
            if target["kind"].endswith("plain partition"):
                extend_plain_partition(target, args.dry_run)
                print("CHANGED: extended {0}".format(target["device"]))
            else:
                if extend_lvm(target, args.dry_run):
                    print("CHANGED: extended {0}".format(target["device"]))
                else:
                    print("OK: no VG free space after pvresize for {0}".format(target["device"]))
        except Exception as error:
            errors.append("ERROR: {0}: {1}".format(target["device"], error))

    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("ERROR: {0}".format(error), file=sys.stderr)
        sys.exit(1)
