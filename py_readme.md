# Python helper in extend_disk role

Этот документ объясняет файл `files/extend_free_space.py` как небольшой урок Python.
Скрипт написан для Ansible-роли `extend_disk`: Ansible ставит пакеты, копирует скрипт на сервер и запускает его от root.

## Что делает роль целиком

Роль состоит из двух частей:

1. `tasks/main.yml` - Ansible-обвязка.
2. `files/extend_free_space.py` - Python-логика поиска и расширения дисков.

Ansible делает простые вещи:

```yaml
- name: Install packages for disk resize
  ansible.builtin.package:
    name: "{{ extend_disk_packages }}"
    state: present
```

Он ставит нужные пакеты: `lvm2`, `parted`, `xfsprogs`, `e2fsprogs`.

Потом копирует Python-скрипт:

```yaml
- name: Copy disk resize helper
  ansible.builtin.copy:
    src: extend_free_space.py
    dest: /usr/local/sbin/extend-free-space
```

И запускает его:

```yaml
- name: Find and extend disks with free space
  ansible.builtin.command:
    argv: "{{ ['/usr/local/sbin/extend-free-space'] + (['--dry-run'] if extend_disk_dry_run | bool else []) }}"
```

То есть Ansible не пытается сам понять всю структуру дисков. Этим занимается Python.

## Почему здесь Python

Задача звучит просто: "найти свободное место и расширить диск".
Но на практике нужно проверить много условий:

- где находится `/`;
- какой диск системный;
- какие диски дополнительные;
- сколько разделов на дополнительном диске;
- есть ли LVM;
- LVM находится на разделе или прямо на диске;
- какая файловая система: `xfs`, `ext4` или что-то другое;
- есть ли свободное место после последнего раздела;
- можно ли безопасно расширять.

В Ansible это превратилось бы в много `set_fact`, циклов и условий.
Python здесь используется как маленькая программа, где такая логика читается проще.

## Общая схема работы скрипта

Скрипт делает примерно такой путь:

1. Делает rescan блочных устройств, чтобы Linux увидел новый размер дисков после расширения в гипервизоре.
2. Получает информацию о дисках через `lsblk`.
3. Превращает JSON от `lsblk` в Python-объекты.
4. Ищет системный раздел или системный LV, где смонтирован `/`.
5. Ищет дополнительные диски.
6. Отбирает только поддерживаемые варианты.
7. Для каждого найденного кандидата выполняет расширение.
8. Печатает результат для Ansible.

Главная функция внизу файла называется `main()`.
С неё удобно начинать чтение.

## Shebang

Первая строка:

```python
#!/usr/bin/env python3
```

Она говорит Linux: "запускай этот файл через `python3`".

Благодаря этому файл можно выполнить как обычную команду:

```bash
/usr/local/sbin/extend-free-space
```

А не так:

```bash
python3 /usr/local/sbin/extend-free-space
```

## Imports

В начале файла:

```python
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
```

Это подключение стандартных библиотек Python.

Что за что отвечает:

- `argparse` - разбор аргументов командной строки, например `--dry-run`;
- `glob` - поиск файлов по маске, например `/sys/class/block/*`;
- `json` - чтение JSON от команды `lsblk -J`;
- `os` - работа с путями, например `os.path.basename`;
- `re` - регулярные выражения;
- `subprocess` - запуск Linux-команд из Python;
- `sys` - работа с кодом выхода и `stderr`;
- `time` - паузы через `sleep`.

Все эти библиотеки встроены в Python, отдельно ставить их не нужно.

## Rescan дисков перед lsblk

Когда диск увеличили в гипервизоре, Linux внутри ВМ не всегда сразу видит новый размер.
Например в VMware, KVM или другой виртуализации снаружи диск уже стал больше, а внутри ВМ `lsblk` всё ещё показывает старый размер.

Для этого в скрипте есть rescan перед поиском дисков.

```python
def write_sysfs(path, value):
    try:
        with open(path, "w", encoding="ascii") as sysfs_file:
            sysfs_file.write(value)
        return True
    except OSError:
        return False
```

Эта функция пишет значение в файл Linux sysfs.
Sysfs - это специальные файлы в `/sys`, через которые можно управлять устройствами ядра.

Например ручной rescan часто делают так:

```bash
echo 1 > /sys/class/block/sdb/device/rescan
```

В Python то же самое делается через `open(..., "w")`.

Конструкция:

```python
with open(path, "w", encoding="ascii") as sysfs_file:
```

открывает файл на запись.
`with` гарантирует, что файл будет закрыт автоматически.

Если файла нет или ядро не поддерживает такой rescan, будет `OSError`.
Скрипт ловит эту ошибку и возвращает `False`, потому что на разных ВМ разные устройства.

```python
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
```

Что здесь происходит:

1. `glob.glob("/sys/class/block/*")` находит все блочные устройства.
2. `os.path.basename(device_path)` берёт имя, например `sdb`.
3. `is_supported_disk_name(name)` оставляет только обычные диски: `sd*`, `vd*`, `xvd*`, `nvme*`.
4. В файл `device/rescan` пишется `1`.
5. Потом дополнительно сканируются SCSI host через `/sys/class/scsi_host/host*/scan`.
6. `udevadm settle` ждёт, пока udev обработает изменения.

Это не расширяет разделы само по себе.
Это только заставляет Linux увидеть новый размер диска.
После этого уже работает `lsblk`, `parted`, `pvresize` и остальная логика.

## Константа SUPPORTED_FS

```python
SUPPORTED_FS = {"xfs", "ext4"}
```

Это множество, по-английски `set`.

Множество удобно, когда нужно проверить: "значение входит в список разрешённых?".

Пример:

```python
if root_fs in SUPPORTED_FS:
    ...
```

Это читается так: если файловая система root входит в `{"xfs", "ext4"}`, то продолжаем.

## Функция run()

```python
def run(command, check=True, input_text=None):
    result = subprocess.run(command, text=True, input=input_text, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            "Command failed: {0}\nstdout: {1}\nstderr: {2}".format(
                " ".join(command), result.stdout.strip(), result.stderr.strip()
            )
        )
    return result.stdout.strip()
```

Это вспомогательная функция для запуска Linux-команд.

Например вместо того, чтобы каждый раз писать:

```python
subprocess.run(["lsblk", "-J"], text=True, capture_output=True)
```

мы пишем:

```python
run(["lsblk", "-J"])
```

Разбор параметров:

- `command` - список аргументов команды;
- `check=True` - если команда завершилась ошибкой, падать с исключением;
- `input_text=None` - текст, который можно передать команде в stdin.

Почему `command` это список, а не строка:

```python
["lsblk", "-b", "-J"]
```

Так безопаснее. Python сам передаёт аргументы программе.
Не нужно собирать строку руками.

Внутри:

```python
result.returncode
```

Это код завершения команды.
В Linux `0` означает успех, не `0` означает ошибку.

Если команда упала, скрипт выбрасывает исключение:

```python
raise RuntimeError(...)
```

Исключение - это ошибка Python, которую можно поймать или дать программе завершиться.

## load_lsblk()

```python
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
```

Эта функция запускает:

```bash
lsblk -b -J -o NAME,PATH,TYPE,PKNAME,FSTYPE,MOUNTPOINTS,RM,SIZE
```

Важные ключи:

- `-b` - размеры в байтах;
- `-J` - вывод в JSON;
- `-o ...` - какие поля вывести.

JSON удобен тем, что Python может превратить его в обычные структуры:

```python
json.loads(data)
```

После этого получается словарь Python.

Примерно такой:

```python
{
    "blockdevices": [
        {
            "name": "sda",
            "path": "/dev/sda",
            "type": "disk",
            "size": 25769803776,
            "children": [...]
        }
    ]
}
```

Функция возвращает только список устройств:

```python
return json.loads(data)["blockdevices"]
```

## Словари и списки

В этом скрипте часто используются две структуры Python.

Список:

```python
targets = []
```

Список хранит несколько элементов по порядку.

Словарь:

```python
{
    "kind": "data lvm on partition",
    "device": "/dev/mapper/vg_data-lv_data",
    "disk": disk,
    "pv": part,
    "lv": lvs[0],
    "fs": lv_fs,
    "mountpoint": "/data",
}
```

Словарь хранит пары `ключ: значение`.
К значению обращаются по ключу:

```python
target["device"]
```

## walk()

```python
def walk(nodes, parent=None):
    for node in nodes:
        item = dict(node)
        item["parent"] = parent
        yield item
        yield from walk(node.get("children", []) or [], item)
```

Это одна из самых "питоновских" функций в скрипте.

`lsblk` возвращает дерево:

```text
sdb
└─sdb1
  └─vg_data-lv_data
```

В JSON это вложенные `children`.

Но иногда удобнее работать с плоским списком:

```text
sdb
sdb1
vg_data-lv_data
```

Функция `walk()` обходит дерево и выдаёт все устройства по одному.

### Что такое yield

Обычно функция возвращает значение через `return`.

```python
def add(a, b):
    return a + b
```

А `yield` делает функцию генератором.
Генератор отдаёт значения по одному.

```python
yield item
```

Здесь это значит: "отдай текущий элемент, но не заканчивай функцию навсегда".

Строка:

```python
yield from walk(node.get("children", []) or [], item)
```

говорит: "если у текущего устройства есть дочерние устройства, обойди и их".

### Зачем добавляется parent

```python
item["parent"] = parent
```

Так каждый дочерний элемент знает своего родителя.

Например LV знает, что его родитель `sdb1`, а `sdb1` знает, что его родитель `sdb`.
Это потом помогает найти физический диск для root-раздела или LV.

## mountpoints()

```python
def mountpoints(node):
    points = node.get("mountpoints") or []
    if isinstance(points, str):
        points = [points]
    return [point for point in points if point]
```

Эта функция возвращает список точек монтирования устройства.

Почему нужна отдельная функция:

- иногда `mountpoints` уже список;
- иногда может прийти строка;
- иногда там `None`;
- пустые значения нужно убрать.

`node.get("mountpoints")` безопаснее, чем `node["mountpoints"]`.

Если ключа нет, `node["mountpoints"]` вызовет ошибку.
А `node.get("mountpoints")` вернёт `None`.

Строка:

```python
return [point for point in points if point]
```

Это list comprehension.
Она создаёт новый список из всех непустых `point`.

Обычным циклом это выглядело бы так:

```python
result = []
for point in points:
    if point:
        result.append(point)
return result
```

## node_fstype()

```python
def node_fstype(node):
    if node.get("fstype"):
        return node["fstype"]
    points = mountpoints(node)
    if not points:
        return None
    fstype = run(["findmnt", "-n", "-o", "FSTYPE", points[0]], check=False)
    return fstype or None
```

Эта функция определяет файловую систему устройства.

Сначала пробует взять её из `lsblk`:

```python
node.get("fstype")
```

Если `lsblk` не показал тип ФС, но устройство смонтировано, используется `findmnt`.

Например:

```bash
findmnt -n -o FSTYPE /data
```

Это нужно для случаев, когда LV смонтирован, но `lsblk` не отдал `FSTYPE` в ожидаемом месте.

## can_grow_filesystem()

```python
def can_grow_filesystem(node):
    fstype = node_fstype(node)
    return fstype == "ext4" or (fstype == "xfs" and len(mountpoints(node)) > 0)
```

Функция отвечает на вопрос: "можно ли расширять эту файловую систему?".

Правила:

- `ext4` можно расширять через `resize2fs`;
- `xfs` можно расширять только когда она смонтирована;
- всё остальное не поддерживаем.

Выражение:

```python
fstype == "xfs" and len(mountpoints(node)) > 0
```

читается так:

если файловая система `xfs` и есть хотя бы одна точка монтирования.

## part_number()

```python
def part_number(path):
    match = re.search(r"(?:p)?([0-9]+)$", path)
    if not match:
        raise RuntimeError("Cannot detect partition number for {0}".format(path))
    return match.group(1)
```

Эта функция вытаскивает номер раздела из пути.

Примеры:

- `/dev/sda4` -> `4`;
- `/dev/sdb1` -> `1`;
- `/dev/nvme0n1p3` -> `3`.

Здесь используется регулярное выражение:

```python
r"(?:p)?([0-9]+)$"
```

Очень коротко:

- `(?:p)?` - буква `p` может быть, а может не быть;
- `([0-9]+)` - одна или больше цифр, их мы хотим забрать;
- `$` - конец строки.

Если номер не найден, скрипт падает с понятной ошибкой.

## bytes_from_lvm()

```python
def bytes_from_lvm(value):
    return int(float(str(value).strip().lstrip("<")))
```

LVM иногда выводит размер так:

```text
<1073741824.00
```

Здесь:

- `str(value)` - превращаем значение в строку;
- `.strip()` - убираем пробелы по краям;
- `.lstrip("<")` - убираем символ `<` слева;
- `float(...)` - превращаем в число с дробной частью;
- `int(...)` - превращаем в целое число.

## node_size()

```python
def node_size(node):
    return int(node.get("size") or 0)
```

Берёт размер устройства из `lsblk`.

Если размера нет, возвращает `0`.

Это защита от ошибки:

```python
int(None)
```

Так писать нельзя, Python упадёт.
Поэтому используется:

```python
node.get("size") or 0
```

Если `size` пустой, берём `0`.

## is_non_removable()

```python
def is_non_removable(node):
    return node.get("rm") in (0, "0", False, None)
```

Проверяет, что диск не съёмный.

Поле `RM` от `lsblk` может приходить по-разному:

- `0`;
- `"0"`;
- `False`;
- иногда вообще отсутствовать.

Поэтому проверка принимает несколько вариантов.

## is_supported_disk()

```python
def is_supported_disk(node):
    name = node.get("name") or os.path.basename(node.get("path") or "")
    return re.match(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)$", name) is not None
```

Эта функция отсекает устройства, которые не хотим трогать.

Поддерживаем:

- `sda`, `sdb`;
- `vda`, `vdb`;
- `xvda`, `xvdb`;
- `nvme0n1`, `nvme1n1`.

Не хотим трогать:

- `sr0`;
- `zram0`;
- странные виртуальные устройства.

`re.match(...) is not None` означает: регулярное выражение совпало.

## partition_has_tail_free_space()

```python
def partition_has_tail_free_space(disk, part):
    parts = [child for child in disk.get("children", []) or [] if child.get("type") == "part"]
    if not parts:
        return False
    last_part = sorted(parts, key=lambda item: int(part_number(item["path"])))[-1]
    if last_part.get("path") != part.get("path"):
        return False
    return node_size(disk) - node_size(part) > 16 * 1024 * 1024
```

Функция проверяет, можно ли расширить конкретный раздел.

Условия:

1. На диске есть разделы.
2. Нужный раздел является последним.
3. После него есть больше 16 MiB свободного места.

Почему раздел должен быть последним:

Если раздел не последний, за ним уже есть другой раздел.
Расширять его нельзя, потому что он налезет на следующий раздел.

Строка:

```python
last_part = sorted(parts, key=lambda item: int(part_number(item["path"])))[-1]
```

делает вот что:

1. сортирует разделы по номеру;
2. берёт последний элемент через `[-1]`.

`lambda` - это маленькая функция без имени.

```python
lambda item: int(part_number(item["path"]))
```

Для каждого раздела она возвращает его номер.
По этому номеру и происходит сортировка.

Порог:

```python
16 * 1024 * 1024
```

Это 16 MiB в байтах.
Маленькие отличия размера игнорируются.

## bytes_available_for_whole_pv()

```python
def bytes_available_for_whole_pv(pv_path):
    pv_data = run(["pvs", "--noheadings", "--nosuffix", "--units", "b", "-o", "pv_size,pv_free", pv_path])
    values = [bytes_from_lvm(value) for value in pv_data.split()]
    disk_size = int(run(["blockdev", "--getsize64", pv_path]))
    pv_size = values[0]
    pv_free = values[1]
    return max(disk_size - pv_size, 0) + pv_free
```

Эта функция нужна для случая LVM прямо на диске без раздела.

Например:

```text
sdb
└─vg_data-lv_data
```

Там нет `/dev/sdb1`.
PV находится прямо на `/dev/sdb`.

Функция считает:

- размер PV по LVM;
- свободное место внутри PV;
- реальный размер блочного устройства;
- разницу между новым размером диска и старым размером PV.

Команда:

```bash
pvs --noheadings --nosuffix --units b -o pv_size,pv_free /dev/sdb
```

даёт размер PV и свободное место в нём.

Команда:

```bash
blockdev --getsize64 /dev/sdb
```

даёт реальный размер устройства в байтах.

## find_root_node()

```python
def find_root_node(nodes):
    for node in nodes:
        if "/" in mountpoints(node):
            return node
    root_source = os.path.realpath(run(["findmnt", "-n", "-o", "SOURCE", "/"]))
    for node in nodes:
        if os.path.realpath(node.get("path") or "") == root_source:
            return node
    return None
```

Эта функция ищет устройство, на котором смонтирован `/`.

Сначала простой способ:

```python
if "/" in mountpoints(node):
```

Если `lsblk` прямо показал `/`, отлично.

Если нет, используем `findmnt`:

```bash
findmnt -n -o SOURCE /
```

`os.path.realpath(...)` нужен, потому что устройство может быть symlink.
Например `/dev/mapper/...` может ссылаться на `/dev/dm-0`.

Если root не найден, возвращается `None`.

`None` в Python означает "ничего", "нет значения".

## disk_for()

```python
def disk_for(node):
    current = node
    while current and current.get("type") != "disk":
        current = current.get("parent")
    return current
```

Эта функция поднимается от устройства вверх к физическому диску.

Например:

```text
vg_data-lv_data -> sdb1 -> sdb
```

Цикл `while` работает, пока:

- `current` не пустой;
- тип текущего устройства не `disk`.

Когда дошли до диска, возвращаем его.

## grow_fs()

```python
def grow_fs(device, fstype, mountpoint, dry_run):
    if dry_run:
        return
    if fstype == "xfs":
        if not mountpoint:
            raise RuntimeError("XFS resize requires mounted filesystem on {0}".format(device))
        run(["xfs_growfs", mountpoint])
    elif fstype == "ext4":
        run(["resize2fs", device])
```

Эта функция расширяет файловую систему.

Если `dry_run=True`, ничего не делаем:

```python
if dry_run:
    return
```

Для `xfs`:

```bash
xfs_growfs /data
```

`xfs` расширяется по точке монтирования.

Для `ext4`:

```bash
resize2fs /dev/sdb1
```

`ext4` расширяется по устройству.

## resize_partition()

```python
def resize_partition(disk_path, part_path, dry_run):
    if dry_run:
        return
    old_part_size = int(run(["blockdev", "--getsize64", part_path]))
    number = part_number(part_path)
    ...
```

Эта функция расширяет раздел до конца диска.

Например:

```bash
parted -s -f /dev/sdb resizepart 1 100%
```

Что происходит:

1. Запоминаем старый размер раздела.
2. Узнаём номер раздела.
3. Запускаем `parted resizepart`.
4. Если `parted` требует подтверждение, отвечаем `Yes`.
5. Просим ядро перечитать таблицу разделов.
6. Ждём, пока размер раздела реально изменится.

Почему есть fallback:

```python
if result.returncode != 0:
    run(
        ["parted", "---pretend-input-tty", disk_path, "resizepart", number, "100%"],
        input_text="Yes\n",
    )
```

На занятом root-разделе `parted` может спросить:

```text
Partition is being used. Are you sure you want to continue?
```

Скрипт отвечает `Yes`.

После изменения раздела выполняются команды:

```python
run(["partprobe", disk_path], check=False)
run(["partx", "-u", disk_path], check=False)
run(["blockdev", "--rereadpt", disk_path], check=False)
run(["udevadm", "settle"], check=False)
```

Они помогают ядру Linux увидеть новый размер раздела.

`check=False` значит: если команда не сработала, не падать.
Это сделано потому, что разные системы поддерживают разные команды перечитывания.

## extend_plain_partition()

```python
def extend_plain_partition(target, dry_run):
    resize_partition(target["disk"]["path"], target["part"]["path"], dry_run)
    grow_fs(
        target["part"]["path"],
        target["fs"],
        target["mountpoint"],
        dry_run,
    )
```

Эта функция обрабатывает обычный раздел без LVM.

Пример:

```text
sdb
└─sdb1 /data ext4
```

Шаги:

1. Расширить раздел `sdb1`.
2. Расширить файловую систему на `sdb1`.

`target` - это словарь с информацией о найденной цели.

## extend_lvm()

```python
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
```

Эта функция обрабатывает LVM.

Если PV находится на разделе:

```text
sdb
└─sdb1
  └─vg_data-lv_data /data
```

сначала расширяется `/dev/sdb1`.

Потом:

```bash
pvresize /dev/sdb1
```

Эта команда говорит LVM: "PV стал больше, используй новый размер".

Затем проверяется свободное место в VG:

```python
pv_free = bytes_from_lvm(...)
```

Если места больше 16 MiB:

```bash
lvextend -r -l +100%FREE /dev/mapper/vg_data-lv_data
```

Ключи:

- `-l +100%FREE` - отдать LV всё свободное место VG;
- `-r` - сразу расширить файловую систему.

Поэтому для LVM отдельно `resize2fs` или `xfs_growfs` здесь не вызывается.
Это делает `lvextend -r`.

## target_key()

```python
def target_key(target):
    return "{0}:{1}".format(target["kind"], target["device"])
```

Эта функция создаёт строковый ключ для найденной цели.

Например:

```text
data lvm on partition:/dev/mapper/vg_data-lv_data
```

Он нужен, чтобы убрать дубликаты.

## discover_targets()

Это самая большая и главная функция поиска.

```python
def discover_targets(blockdevices):
    nodes = list(walk(blockdevices))
    root_node = find_root_node(nodes)
    root_disk = disk_for(root_node) if root_node else None
    targets = []
    skipped = []
```

Что создаётся:

- `nodes` - плоский список всех устройств;
- `root_node` - устройство, где смонтирован `/`;
- `root_disk` - физический диск, на котором находится `/`;
- `targets` - что будем расширять;
- `skipped` - что пропустили и почему.

### Тернарное выражение

```python
root_disk = disk_for(root_node) if root_node else None
```

Это короткая форма `if`.

Обычным кодом:

```python
if root_node:
    root_disk = disk_for(root_node)
else:
    root_disk = None
```

## Поиск системного диска

```python
root_fs = node_fstype(root_node) if root_node else None
if root_node and root_fs in SUPPORTED_FS:
    ...
```

Скрипт расширяет системный диск только для `/`.

Если `/` находится прямо на разделе:

```python
if root_node.get("type") == "part":
```

создаётся target вида:

```python
{
    "kind": "system plain partition",
    "device": root_node["path"],
    "disk": disk,
    "part": root_node,
    "fs": root_fs,
    "mountpoint": "/",
}
```

Если `/` находится на LVM:

```python
elif root_node.get("type") == "lvm":
```

создаётся target вида:

```python
{
    "kind": "system lvm",
    "device": root_node["path"],
    "disk": disk,
    "pv": pv,
    "lv": root_node,
    "fs": root_fs,
    "mountpoint": "/",
}
```

Важная идея:

Системный диск может иметь несколько разделов: `/boot`, swap, `/`.
Но расширять нужно только тот раздел или LV, где находится `/`.

## Поиск дополнительных дисков

```python
for disk in [
    node
    for node in nodes
    if node.get("type") == "disk" and is_non_removable(node) and is_supported_disk(node)
]:
```

Это цикл по дискам.

Берём только:

- устройства типа `disk`;
- несъёмные;
- с поддерживаемым именем.

Если диск системный, пропускаем:

```python
if root_disk and disk.get("path") == root_disk.get("path"):
    continue
```

`continue` означает: перейти к следующему элементу цикла.

### Доп-диск без разделов, но с LVM

```python
if len(parts) == 0 and len(disk_lvs) == 1:
```

Это случай:

```text
sdb
└─vg_data-lv_data /data
```

Если на таком PV есть свободное место, добавляем target:

```python
"kind": "data lvm on disk"
```

### Доп-диск с несколькими разделами

```python
if len(parts) != 1:
    skipped.append("SKIP: {0} has {1} partitions".format(disk["path"], len(parts)))
    continue
```

По условию задачи дополнительные диски расширяем только если там один раздел.
Если разделов 0, 2, 3 и больше, такой диск пропускается.

Исключение: LVM прямо на диске без разделов обработан выше.

### Доп-диск с одним разделом

```python
part = parts[0]
if not partition_has_tail_free_space(disk, part):
    skipped.append(...)
    continue
```

Если один раздел есть, проверяем, есть ли после него свободное место.

Если файловая система прямо на разделе:

```python
if part_fs in SUPPORTED_FS and can_grow_filesystem(part):
```

создаётся:

```python
"kind": "data plain partition"
```

Если внутри раздела один LV:

```python
if len(lvs) == 1:
```

создаётся:

```python
"kind": "data lvm on partition"
```

Это как раз твой тестовый случай:

```text
sdb
└─sdb1
  └─vg_data-lv_data /data
```

## Удаление дубликатов

```python
seen = set()
unique_targets = []
for target in targets:
    key = target_key(target)
    if key not in seen:
        seen.add(key)
        unique_targets.append(target)
return unique_targets, skipped
```

Здесь используется `set`, чтобы не добавить одну и ту же цель два раза.

Логика:

1. Создаём пустое множество `seen`.
2. Идём по найденным target.
3. Строим ключ.
4. Если ключа ещё не было, добавляем target в итоговый список.

## main()

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
```

`main()` - главная функция программы.

Она разбирает аргумент:

```bash
/usr/local/sbin/extend-free-space --dry-run
```

Если `--dry-run` передан, то:

```python
args.dry_run
```

будет `True`.

Дальше:

```python
rescan_block_devices()
targets, skipped = discover_targets(load_lsblk())
```

Эта строка делает сразу несколько вещей:

1. `rescan_block_devices()` просит Linux перечитать размеры дисков.
2. `load_lsblk()` получает информацию о дисках.
3. `discover_targets(...)` ищет, что можно расширить.
4. Результат раскладывается в две переменные: `targets` и `skipped`.

## Печать skip-сообщений

```python
for line in skipped:
    print(line)
```

Все причины пропуска печатаются в stdout.
Ansible потом показывает их в debug-задаче.

Если целей нет:

```python
if not targets:
    print("OK: no disks with supported free space found")
    return 0
```

`return 0` означает успешное завершение.

## Обработка всех целей

```python
errors = []
for target in targets:
    try:
        ...
    except Exception as error:
        errors.append("ERROR: {0}: {1}".format(target["device"], error))
```

Скрипт пытается обработать все найденные цели.

Если одна цель упала, ошибка сохраняется в список `errors`, но цикл идёт дальше.

Это важно: если root-диск дал проблему, дополнительный диск всё равно будет попробован.

## try / except

```python
try:
    ...
except Exception as error:
    ...
```

Это обработка ошибок.

Всё внутри `try` может упасть.
Если ошибка случилась, Python переходит в `except`.

Здесь ошибка не скрывается, а добавляется в список:

```python
errors.append(...)
```

В конце:

```python
if errors:
    return 1
```

Код выхода `1` говорит Ansible: задача завершилась ошибкой.

## Как выбирается способ расширения

```python
if target["kind"].endswith("plain partition"):
    extend_plain_partition(target, args.dry_run)
else:
    extend_lvm(target, args.dry_run)
```

Если тип цели заканчивается на `plain partition`, значит это обычный раздел без LVM.

Иначе считаем, что это LVM.

Примеры:

- `system plain partition` -> обычный раздел;
- `data plain partition` -> обычный раздел;
- `system lvm` -> LVM;
- `data lvm on partition` -> LVM;
- `data lvm on disk` -> LVM.

## Блок запуска программы

```python
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("ERROR: {0}".format(error), file=sys.stderr)
        sys.exit(1)
```

Это стандартный Python-паттерн.

Когда файл запускают как программу:

```bash
python3 extend_free_space.py
```

переменная `__name__` равна `"__main__"`.

Тогда вызывается:

```python
main()
```

`sys.exit(main())` завершает программу с тем кодом, который вернула `main()`.

Если произошла непойманная ошибка, она печатается в stderr:

```python
print(..., file=sys.stderr)
```

И программа завершается с кодом `1`.

## Что такое dry-run

В роли есть переменная:

```yaml
extend_disk_dry_run: false
```

Если поставить:

```yaml
extend_disk_dry_run: true
```

Ansible запустит:

```bash
/usr/local/sbin/extend-free-space --dry-run
```

В этом режиме скрипт ищет цели, печатает, что бы он расширил, но не меняет диски.

Это полезно для проверки логики.

## Как скрипт сообщает Ansible, что были изменения

В Ansible-задаче:

```yaml
changed_when: "'CHANGED:' in extend_disk_result.stdout"
```

Скрипт печатает:

```python
print("CHANGED: extended {0}".format(target["device"]))
```

Если Ansible видит строку `CHANGED:`, задача считается изменившей систему.

Если строка не появилась, задача будет `ok`.

## Основные Python-конструкции из этого файла

В этом скрипте используются:

- функции `def`;
- списки `[]`;
- словари `{}`;
- множества `set`;
- циклы `for`;
- цикл `while`;
- условия `if / elif / else`;
- исключения `try / except`;
- генератор `yield`;
- list comprehension;
- регулярные выражения через `re`;
- запуск внешних команд через `subprocess`;
- работа с JSON через `json.loads`.

## Как читать этот скрипт новичку

Лучший порядок чтения:

1. Сначала прочитать `main()`.
2. Потом `discover_targets()`.
3. Потом `extend_plain_partition()` и `extend_lvm()`.
4. Потом вспомогательные функции сверху.

Не обязательно понимать каждую строку сразу.
Важнее сначала понять поток:

```text
main()
  -> rescan_block_devices()
  -> load_lsblk()
  -> discover_targets()
  -> extend_plain_partition() или extend_lvm()
  -> run(...)
```

## Мини-словарь терминов Python

`def` - объявление функции.

`return` - вернуть значение из функции.

`yield` - отдавать значения по одному, не завершая функцию полностью.

`list` - список, например `[1, 2, 3]`.

`dict` - словарь, например `{"path": "/dev/sda"}`.

`set` - множество уникальных значений.

`None` - отсутствие значения.

`True` / `False` - булевы значения.

`if` - условие.

`for` - цикл по элементам.

`while` - цикл, пока условие истинно.

`try / except` - обработка ошибок.

`raise` - вызвать ошибку.

`import` - подключить библиотеку.

## Мини-пример target

Для такого диска:

```text
sdb                   8:16   0    4G  0 disk
└─sdb1                8:17   0 1022M  0 part
  └─vg_data-lv_data 253:0    0 1020M  0 lvm  /data
```

скрипт создаёт примерно такой target:

```python
{
    "kind": "data lvm on partition",
    "device": "/dev/mapper/vg_data-lv_data",
    "disk": {
        "name": "sdb",
        "path": "/dev/sdb",
        "type": "disk",
        "size": 4294967296,
    },
    "pv": {
        "name": "sdb1",
        "path": "/dev/sdb1",
        "type": "part",
        "size": 1071644672,
    },
    "lv": {
        "name": "vg_data-lv_data",
        "path": "/dev/mapper/vg_data-lv_data",
        "type": "lvm",
    },
    "fs": "xfs",
    "mountpoint": "/data",
}
```

Потом `extend_lvm()` делает:

```text
resizepart /dev/sdb 1 100%
pvresize /dev/sdb1
lvextend -r -l +100%FREE /dev/mapper/vg_data-lv_data
```

## Что можно улучшить в будущем

Для учебного и практического развития можно добавить:

- больше debug-вывода по найденным дискам;
- режим `--list`, который только показывает кандидатов;
- unit-тесты на функции `part_number()`, `partition_has_tail_free_space()`;
- отдельную функцию для создания target-словарей, чтобы уменьшить повторение;
- поддержку других файловых систем, если она понадобится.

Но сейчас скрипт специально оставлен простым: он решает конкретную задачу и читается сверху вниз.
