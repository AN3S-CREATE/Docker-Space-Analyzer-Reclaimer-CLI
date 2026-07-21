"""Recorded Docker CLI output fixtures, keyed by scenario.

Each scenario returns a :class:`~docker_disk_toolkit.utils.FixtureCommandRunner`
mapping argv prefixes to canned stdout, so the whole suite runs without a live
Docker daemon. Payloads mirror real ``docker ... --format '{{json .}}'`` output.
"""

from __future__ import annotations

from docker_disk_toolkit.utils import CommandResult, FixtureCommandRunner, result

# --- reusable JSON payloads (NDJSON where Docker emits per-line objects) ------

VERSION_OK = (
    '{"Client":{"Version":"27.1.1","ApiVersion":"1.46"},'
    '"Server":{"Version":"27.1.1","ApiVersion":"1.46"}}'
)

INFO_ENGINE = (
    '{"DockerRootDir":"/var/lib/docker","Driver":"overlay2","OSType":"linux",'
    '"OperatingSystem":"Ubuntu 24.04 LTS","ServerVersion":"27.1.1",'
    '"SecurityOptions":["name=seccomp,profile=builtin"],"Name":"homelab"}'
)

INFO_DESKTOP_WINDOWS = (
    '{"DockerRootDir":"/var/lib/docker","Driver":"overlay2","OSType":"linux",'
    '"OperatingSystem":"Docker Desktop","ServerVersion":"27.1.1",'
    '"SecurityOptions":["name=seccomp,profile=unconfined"],"Name":"docker-desktop"}'
)

INFO_ROOTLESS = (
    '{"DockerRootDir":"/home/user/.local/share/docker","Driver":"overlay2",'
    '"OSType":"linux","OperatingSystem":"Ubuntu 24.04 LTS","ServerVersion":"27.1.1",'
    '"SecurityOptions":["name=seccomp","name=rootless"],"Name":"rootless-host"}'
)

DF_SUMMARY_TYPICAL = "\n".join(
    [
        '{"Type":"Images","TotalCount":"3","Active":"1","Size":"6.58GB","Reclaimable":"6.4GB (97%)"}',
        '{"Type":"Containers","TotalCount":"2","Active":"1","Size":"12MB","Reclaimable":"10MB (83%)"}',
        '{"Type":"Local Volumes","TotalCount":"3","Active":"1","Size":"1.5GB","Reclaimable":"1.45GB (96%)"}',
        '{"Type":"Build Cache","TotalCount":"5","Active":"0","Size":"800MB","Reclaimable":"800MB (100%)"}',
    ]
)

IMAGES_TYPICAL = "\n".join(
    [
        '{"Containers":"1","CreatedAt":"2026-06-01 10:00:00 +0000 UTC","Digest":"<none>",'
        '"ID":"sha256:aaa0000000000000000000000000000000000000000000000000000000000000",'
        '"Repository":"nginx","SharedSize":"5MB","Size":"180MB","Tag":"latest",'
        '"UniqueSize":"175MB","VirtualSize":"180MB","Labels":"maintainer=nginx"}',
        '{"Containers":"0","CreatedAt":"2026-05-01 10:00:00 +0000 UTC","Digest":"<none>",'
        '"ID":"sha256:bbb0000000000000000000000000000000000000000000000000000000000000",'
        '"Repository":"<none>","SharedSize":"0B","Size":"400MB","Tag":"<none>",'
        '"UniqueSize":"400MB","VirtualSize":"400MB","Labels":""}',
        '{"Containers":"0","CreatedAt":"2026-04-01 10:00:00 +0000 UTC","Digest":"<none>",'
        '"ID":"sha256:ccc0000000000000000000000000000000000000000000000000000000000000",'
        '"Repository":"ollama/ollama","SharedSize":"0B","Size":"6GB","Tag":"latest",'
        '"UniqueSize":"6GB","VirtualSize":"6GB","Labels":""}',
    ]
)

CONTAINERS_TYPICAL = "\n".join(
    [
        '{"ID":"c1000000000000000000000000000000000000000000000000000000000000000",'
        '"Names":"web","Image":"nginx:latest","State":"running","Status":"Up 2 hours",'
        '"CreatedAt":"2026-07-20 08:00:00 +0000 UTC","Size":"2MB (virtual 182MB)",'
        '"Mounts":"webdata","Labels":"com.example.role=web"}',
        '{"ID":"c2000000000000000000000000000000000000000000000000000000000000000",'
        '"Names":"old_job","Image":"busybox:latest","State":"exited","Status":"Exited (0) 3 days ago",'
        '"CreatedAt":"2026-07-10 08:00:00 +0000 UTC","Size":"10MB (virtual 20MB)",'
        '"Mounts":"","Labels":""}',
    ]
)

VOLUMES_TYPICAL = "\n".join(
    [
        '{"Driver":"local","Name":"webdata","Scope":"local",'
        '"Mountpoint":"/var/lib/docker/volumes/webdata/_data","Labels":""}',
        '{"Driver":"local","Name":"postgres_data","Scope":"local",'
        '"Mountpoint":"/var/lib/docker/volumes/postgres_data/_data","Labels":"app=db"}',
        '{"Driver":"local","Name":"scratch_tmp","Scope":"local",'
        '"Mountpoint":"/var/lib/docker/volumes/scratch_tmp/_data","Labels":""}',
    ]
)

NETWORKS_TYPICAL = "\n".join(
    [
        '{"ID":"n1","Name":"bridge","Driver":"bridge","Scope":"local","CreatedAt":"2026-01-01 00:00:00 +0000 UTC"}',
        '{"ID":"n2","Name":"host","Driver":"host","Scope":"local","CreatedAt":"2026-01-01 00:00:00 +0000 UTC"}',
        '{"ID":"n3","Name":"myapp_default","Driver":"bridge","Scope":"local","CreatedAt":"2026-07-01 00:00:00 +0000 UTC"}',
    ]
)

DF_VERBOSE_TYPICAL = (
    '{"Images":[],"Containers":[],'
    '"Volumes":['
    '{"Name":"webdata","Links":"1","Size":"50MB"},'
    '{"Name":"postgres_data","Links":"0","Size":"1.2GB"},'
    '{"Name":"scratch_tmp","Links":"0","Size":"250MB"}'
    "],"
    '"BuildCache":['
    '{"ID":"bc1","Type":"regular","Size":"800MB","InUse":false,"Shared":false,'
    '"LastUsedAt":"2026-07-01T00:00:00Z","UsageCount":2,"Description":"RUN pip install"}'
    "]}"
)

DF_SUMMARY_EMPTY = "\n".join(
    [
        '{"Type":"Images","TotalCount":"0","Active":"0","Size":"0B","Reclaimable":"0B"}',
        '{"Type":"Containers","TotalCount":"0","Active":"0","Size":"0B","Reclaimable":"0B"}',
        '{"Type":"Local Volumes","TotalCount":"0","Active":"0","Size":"0B","Reclaimable":"0B"}',
        '{"Type":"Build Cache","TotalCount":"0","Active":"0","Size":"0B","Reclaimable":"0B"}',
    ]
)


def _typical_rules() -> list[tuple[list[str], CommandResult]]:
    return [
        (["docker", "version"], result(VERSION_OK)),
        (["docker", "info"], result(INFO_ENGINE)),
        (["docker", "system", "df", "-v"], result(DF_VERBOSE_TYPICAL)),
        (["docker", "system", "df", "--format"], result(DF_SUMMARY_TYPICAL)),
        (["docker", "image", "ls"], result(IMAGES_TYPICAL)),
        (["docker", "container", "ls"], result(CONTAINERS_TYPICAL)),
        (["docker", "volume", "ls"], result(VOLUMES_TYPICAL)),
        (["docker", "network", "ls"], result(NETWORKS_TYPICAL)),
        (["docker", "image", "rm"], result("Deleted: sha256:bbb")),
        (["docker", "container", "rm"], result("old_job")),
        (["docker", "volume", "rm"], result("scratch_tmp")),
        (["docker", "network", "rm"], result("myapp_default")),
        (["docker", "builder", "prune"], result("Total reclaimed space: 800MB")),
    ]


def _empty_rules() -> list[tuple[list[str], CommandResult]]:
    return [
        (["docker", "version"], result(VERSION_OK)),
        (["docker", "info"], result(INFO_ENGINE)),
        (["docker", "system", "df", "-v"], result('{"Volumes":[],"BuildCache":[]}')),
        (["docker", "system", "df", "--format"], result(DF_SUMMARY_EMPTY)),
        (["docker", "image", "ls"], result("")),
        (["docker", "container", "ls"], result("")),
        (["docker", "volume", "ls"], result("")),
        (["docker", "network", "ls"], result(NETWORKS_TYPICAL)),
    ]


def _desktop_windows_rules() -> list[tuple[list[str], CommandResult]]:
    rules = _typical_rules()
    rules[1] = (["docker", "info"], result(INFO_DESKTOP_WINDOWS))
    return rules


def _rootless_rules() -> list[tuple[list[str], CommandResult]]:
    rules = _typical_rules()
    rules[1] = (["docker", "info"], result(INFO_ROOTLESS))
    return rules


# --- probe failure scenarios -------------------------------------------------

DAEMON_DOWN_STDERR = (
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)
PERMISSION_STDERR = (
    "permission denied while trying to connect to the Docker daemon socket at "
    "unix:///var/run/docker.sock"
)

SCENARIOS = {
    "typical": _typical_rules,
    "typical-engine": _typical_rules,
    "empty": _empty_rules,
    "desktop-windows": _desktop_windows_rules,
    "rootless": _rootless_rules,
    "daemon-down": lambda: [
        (
            ["docker", "version"],
            result('{"Client":{"Version":"27.1.1"}}', returncode=1, stderr=DAEMON_DOWN_STDERR),
        ),
    ],
    "permission-denied": lambda: [
        (
            ["docker", "version"],
            result('{"Client":{"Version":"27.1.1"}}', returncode=1, stderr=PERMISSION_STDERR),
        ),
    ],
    "not-installed": lambda: [
        (["docker", "version"], result("", returncode=127, stderr="docker: command not found")),
    ],
    "timeout": lambda: [
        (["docker", "version"], result("", returncode=124, stderr="command timed out after 8s")),
    ],
}


def make_runner(scenario: str) -> FixtureCommandRunner:
    """Build a fixture runner for a named scenario."""
    if scenario not in SCENARIOS:
        raise KeyError(f"unknown scenario {scenario!r}; known: {sorted(SCENARIOS)}")
    return FixtureCommandRunner(SCENARIOS[scenario]())
