"""Safe, multi-level Docker cleanup with a protect-list and full audit trail.

Levels (each a declarative target set, so the same list drives "what would be
freed" and "what gets removed"):

* **0 REPORT**    — plan only, removes nothing.
* **1 SAFE**      — dangling images, stopped containers, unused networks, cache.
* **2 AGGRESSIVE**— level 1 + unused volumes **except** protect-list matches.
* **3 NUCLEAR**   — level 2 + unused (tagged) images; needs type-to-confirm.

Safety invariants: dry-run removes nothing; the protect-list always wins;
confirmation is required for real destructive runs (level 3 needs an explicit
force / typed phrase even with ``--yes``); every attempt is audited.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import analyzer, system_info
from .config import ToolkitConfig
from .context import RunContext
from .docker_client import DockerClient
from .errors import DockerUnavailableError
from .models import (
    AuditEvent,
    CleanupPlan,
    CleanupResult,
    ConfirmationRecord,
    ContainerInfo,
    DiskUsage,
    DockerBackend,
    DockerUsage,
    HealthStatus,
    ImageInfo,
    ObjectKind,
    PruneLevel,
    Removable,
    SpaceDelta,
    VolumeInfo,
)
from .utils import ProtectMatcher, age, compile_protect_matchers, humanize_size, is_tty

# Whether each level touches a given target.
_LEVEL_TARGETS: dict[PruneLevel, set[str]] = {
    PruneLevel.REPORT: set(),
    PruneLevel.SAFE: {"dangling-images", "stopped-containers", "networks", "build-cache"},
    PruneLevel.AGGRESSIVE: {
        "dangling-images",
        "stopped-containers",
        "networks",
        "build-cache",
        "unused-volumes",
    },
    PruneLevel.NUCLEAR: {
        "dangling-images",
        "stopped-containers",
        "networks",
        "build-cache",
        "unused-volumes",
        "unused-images",
    },
}


@dataclass
class SelectionCriteria:
    """Selective-cleanup filters and the protect/exclude matchers."""

    labels: dict[str, str] = field(default_factory=dict)
    name_globs: list[str] = field(default_factory=list)
    min_age: timedelta | None = None
    max_age: timedelta | None = None
    min_size_bytes: int | None = None
    protect_matcher: ProtectMatcher = field(default_factory=compile_protect_matchers)
    exclude_images_matcher: ProtectMatcher = field(default_factory=compile_protect_matchers)

    @classmethod
    def from_config(
        cls,
        config: ToolkitConfig,
        *,
        extra_protect: list[str] | None = None,
        exclude_images: list[str] | None = None,
        labels: dict[str, str] | None = None,
        name_globs: list[str] | None = None,
        min_age: timedelta | None = None,
        max_age: timedelta | None = None,
        min_size_bytes: int | None = None,
    ) -> SelectionCriteria:
        """Build criteria, merging config protect-lists with CLI overrides."""
        protect = compile_protect_matchers(
            globs=[*config.protect_volumes, *(extra_protect or [])],
            regexes=config.effective_protect_regexes(),
        )
        exclude = compile_protect_matchers(globs=[*config.exclude_images, *(exclude_images or [])])
        return cls(
            labels=labels or {},
            name_globs=name_globs or [],
            min_age=min_age,
            max_age=max_age,
            min_size_bytes=min_size_bytes,
            protect_matcher=protect,
            exclude_images_matcher=exclude,
        )


def _passes_filters(
    name: str,
    size_bytes: int,
    created_at: datetime | None,
    labels: dict[str, str],
    criteria: SelectionCriteria,
    now: datetime,
) -> bool:
    """Return ``True`` if an object passes the (AND-combined) selective filters."""
    from fnmatch import fnmatch

    if criteria.name_globs and not any(fnmatch(name, g) for g in criteria.name_globs):
        return False
    if criteria.labels and not all(labels.get(k) == v for k, v in criteria.labels.items()):
        return False
    if criteria.min_size_bytes is not None and size_bytes < criteria.min_size_bytes:
        return False
    if criteria.min_age is not None or criteria.max_age is not None:
        obj_age = age(created_at, now=now)
        if obj_age is None:
            return False
        if criteria.min_age is not None and obj_age < criteria.min_age:
            return False
        if criteria.max_age is not None and obj_age > criteria.max_age:
            return False
    return True


def _image_removable(image: ImageInfo, reasons: list[str]) -> Removable:
    ref = image.repo_tags[0] if image.repo_tags else image.id
    return Removable(
        kind=ObjectKind.IMAGE,
        id=ref,
        name=image.display_name,
        reclaim_bytes=image.reclaim_bytes,
        in_use=image.in_use,
        reasons=reasons,
    )


def _container_removable(container: ContainerInfo) -> Removable:
    return Removable(
        kind=ObjectKind.CONTAINER,
        id=container.id,
        name=container.name,
        reclaim_bytes=container.reclaim_bytes,
        in_use=container.running,
        reasons=["stopped container"],
    )


def _volume_removable(volume: VolumeInfo) -> Removable:
    return Removable(
        kind=ObjectKind.VOLUME,
        id=volume.name,
        name=volume.name,
        reclaim_bytes=volume.reclaim_bytes,
        in_use=volume.in_use,
        reasons=["unused volume"],
    )


def build_plan(
    usage: DockerUsage,
    level: PruneLevel,
    criteria: SelectionCriteria,
    *,
    now: datetime,
) -> CleanupPlan:
    """Compute a reviewable cleanup plan for ``level`` (pure; no side effects).

    Assumes ``usage`` has been correlated (in-use / protected flags set). The
    protect-list and exclude filters always subtract from the candidate set.
    """
    targets = _LEVEL_TARGETS[level]
    items: list[Removable] = []
    protected: list[Removable] = []

    if "dangling-images" in targets:
        for img in usage.image_list:
            if not (img.dangling and not img.in_use):
                continue
            if criteria.exclude_images_matcher.matches(img.display_name):
                rem = _image_removable(img, ["excluded image"])
                rem.spared_reason = "matches --exclude-images"
                protected.append(rem)
                continue
            if _passes_filters(
                img.display_name, img.reclaim_bytes, img.created_at, img.labels, criteria, now
            ):
                items.append(_image_removable(img, ["dangling image"]))

    if "unused-images" in targets:
        for img in usage.image_list:
            if img.dangling or img.in_use:
                continue
            if criteria.exclude_images_matcher.matches(img.display_name):
                rem = _image_removable(img, ["excluded image"])
                rem.spared_reason = "matches --exclude-images"
                protected.append(rem)
                continue
            if _passes_filters(
                img.display_name, img.reclaim_bytes, img.created_at, img.labels, criteria, now
            ):
                items.append(_image_removable(img, ["unused (unreferenced) image"]))

    if "stopped-containers" in targets:
        for cont in usage.container_list:
            if cont.running:
                continue
            if _passes_filters(
                cont.name, cont.reclaim_bytes, cont.created_at, cont.labels, criteria, now
            ):
                items.append(_container_removable(cont))

    if "unused-volumes" in targets:
        for vol in usage.volume_list:
            if vol.in_use:
                continue
            if vol.protected:
                rem = _volume_removable(vol)
                rem.spared_reason = vol.protect_reason or "protected volume"
                protected.append(rem)
                continue
            if _passes_filters(
                vol.name, vol.reclaim_bytes, vol.created_at, vol.labels, criteria, now
            ):
                items.append(_volume_removable(vol))

    if "build-cache" in targets and usage.build_cache.reclaimable_bytes > 0:
        items.append(
            Removable(
                kind=ObjectKind.BUILD_CACHE,
                id="build-cache",
                name="build cache",
                reclaim_bytes=usage.build_cache.reclaimable_bytes,
                reasons=["reclaimable build cache"],
            )
        )

    if "networks" in targets:
        unused_networks = [n for n in usage.network_list if not n.builtin]
        if unused_networks:
            items.append(
                Removable(
                    kind=ObjectKind.NETWORK,
                    id="unused-networks",
                    name=f"{len(unused_networks)} unused network(s)",
                    reclaim_bytes=0,
                    reasons=["unused networks"],
                )
            )

    total = sum(item.reclaim_bytes for item in items)
    confirmation_mode = (
        "none"
        if level == PruneLevel.REPORT
        else "type-to-confirm" if level == PruneLevel.NUCLEAR else "single"
    )
    commands = [_command_for(item) for item in items]
    return CleanupPlan(
        level=level,
        items=items,
        protected=protected,
        total_reclaim_bytes=total,
        docker_reported_reclaim_bytes=usage.reclaimable_bytes,
        commands=commands,
        requires_confirmation=level != PruneLevel.REPORT and bool(items),
        confirmation_mode=confirmation_mode,
    )


def _command_for(item: Removable) -> list[str]:
    if item.kind is ObjectKind.IMAGE:
        return ["docker", "image", "rm", item.id]
    if item.kind is ObjectKind.CONTAINER:
        return ["docker", "container", "rm", item.id]
    if item.kind is ObjectKind.VOLUME:
        return ["docker", "volume", "rm", item.id]
    if item.kind is ObjectKind.BUILD_CACHE:
        return ["docker", "builder", "prune", "-f"]
    return ["docker", "network", "prune", "-f"]


def execute_plan(
    client: DockerClient,
    plan: CleanupPlan,
    *,
    dry_run: bool,
    run_id: str,
    backend: DockerBackend,
    confirmation: ConfirmationRecord,
    actor: str,
    now: datetime,
    force: bool = False,
    stop_on_error: bool = False,
) -> tuple[list[AuditEvent], list[str]]:
    """Execute (or simulate) a plan item-by-item, returning audit events + errors.

    Each removal is individually guarded: a single failure is recorded and the
    run continues (unless ``stop_on_error``). Protected/spared items are recorded
    too, for a complete audit trail.
    """
    events: list[AuditEvent] = []
    errors: list[str] = []

    def make_event(
        item: Removable, outcome: str, *, command: list[str], error: str | None, actual: int | None
    ) -> AuditEvent:
        return AuditEvent(
            ts=now,
            run_id=run_id,
            level=plan.level,
            dry_run=dry_run,
            object_kind=item.kind,
            object_id=item.id,
            object_name=item.name,
            reclaim_predicted_bytes=item.reclaim_bytes,
            reclaim_actual_bytes=actual,
            command=command,
            confirmation=confirmation,
            outcome=outcome,
            error=error,
            actor=actor,
            docker_backend=backend,
        )

    for spared in plan.protected:
        events.append(
            make_event(
                spared,
                "skipped-protected",
                command=[],
                error=None,
                actual=None,
            )
        )

    for item in plan.items:
        outcome, error, actual, command = _run_item(client, item, dry_run=dry_run, force=force)
        events.append(make_event(item, outcome, command=command, error=error, actual=actual))
        if outcome == "error":
            errors.append(f"{item.kind.value} {item.name}: {error}")
            if stop_on_error:
                break

    return events, errors


def _run_item(
    client: DockerClient, item: Removable, *, dry_run: bool, force: bool
) -> tuple[str, str | None, int | None, list[str]]:
    """Execute one plan item, returning ``(outcome, error, reclaimed, command)``."""
    kind = item.kind
    if kind is ObjectKind.IMAGE:
        res = client.remove_image(item.id, force=force, dry_run=dry_run)
    elif kind is ObjectKind.CONTAINER:
        res = client.remove_container(item.id, force=force, dry_run=dry_run)
    elif kind is ObjectKind.VOLUME:
        res = client.remove_volume(item.id, force=force, dry_run=dry_run)
    elif kind is ObjectKind.BUILD_CACHE:
        outcome = client.prune_build_cache(dry_run=dry_run)
        actual = None if dry_run else outcome.reclaimed_bytes
        return (
            "dry-run" if dry_run else ("removed" if outcome.ok else "error"),
            outcome.error,
            actual,
            outcome.command,
        )
    else:  # network
        outcome = client.prune_networks(dry_run=dry_run)
        return (
            "dry-run" if dry_run else ("removed" if outcome.ok else "error"),
            outcome.error,
            0,
            outcome.command,
        )

    if dry_run:
        return ("dry-run", None, None, res.command)
    if res.ok:
        return ("removed", None, item.reclaim_bytes, res.command)
    return ("error", res.error, None, res.command)


# Confirmation callback: (plan, mode) -> granted. Returns False to abort.
ConfirmFn = Callable[[CleanupPlan, str], bool]


def _auto_deny(plan: CleanupPlan, mode: str) -> bool:
    return False


def run_cleanup(
    ctx: RunContext,
    *,
    level: PruneLevel,
    criteria: SelectionCriteria | None = None,
    dry_run: bool | None = None,
    assume_yes: bool | None = None,
    force: bool = False,
    stop_on_error: bool = False,
    confirm_fn: ConfirmFn | None = None,
) -> CleanupResult:
    """Run a full cleanup: analyze → plan → confirm → execute → delta.

    Args:
        ctx: The run context.
        level: Cleanup level (0-3).
        criteria: Selective filters; defaults to config-derived protect-lists.
        dry_run: Override config's dry-run (``None`` uses config).
        assume_yes: Override config's assume-yes (``None`` uses config).
        force: Required for level-3 nuclear removal even with ``assume_yes``.
        stop_on_error: Abort the batch on the first failed removal.
        confirm_fn: Interactive confirmation callback. ``None`` auto-denies when
            confirmation is required and ``assume_yes`` is not set.

    Returns:
        A :class:`CleanupResult` (does not write reports/audit — the caller does).

    Raises:
        DockerUnavailableError: If the daemon is not usable (cleanup needs it).
    """
    config = ctx.config
    client = ctx.docker
    now = ctx.now
    criteria = criteria or SelectionCriteria.from_config(config)
    dry_run = config.dry_run if dry_run is None else dry_run
    assume_yes = config.assume_yes if assume_yes is None else assume_yes
    confirm_fn = confirm_fn or _auto_deny

    probe = client.probe()
    if not probe.ok:
        raise DockerUnavailableError(
            f"Cleanup requires Docker, but it is {probe.availability.value}.",
            remediation=probe.remediation,
            fatal=True,
        )

    info = client.info()
    usage = client.collect_usage()
    analyzer.correlate_usage(usage, config.protect_matcher())
    pre_disks = system_info.collect_disks(
        docker_root_dir=info.docker_root_dir, extra_paths=[str(config.report_dir)]
    )
    pre_health = analyzer.worst(
        *(analyzer.assess_disk_health(d, config.thresholds) for d in pre_disks)
    )

    plan = build_plan(usage, level, criteria, now=now)

    # Decide whether we actually mutate anything.
    effective_dry = dry_run or level == PruneLevel.REPORT
    granted = True
    if not effective_dry and plan.requires_confirmation:
        mode = plan.confirmation_mode
        if mode == "type-to-confirm":
            granted = force or confirm_fn(plan, mode)
        else:
            granted = assume_yes or confirm_fn(plan, mode)
    if not effective_dry and not granted:
        effective_dry = True  # confirmation refused -> simulate, remove nothing

    confirmation = ConfirmationRecord(
        mode=plan.confirmation_mode,
        tty=is_tty(),
        assume_yes=bool(assume_yes),
        forced=force,
        response="granted" if granted and not effective_dry else "aborted",
    )

    events, errors = execute_plan(
        client,
        plan,
        dry_run=effective_dry,
        run_id=ctx.correlation_id,
        backend=probe.backend,
        confirmation=confirmation,
        actor=system_info.hostname(),
        now=now,
        force=force,
        stop_on_error=stop_on_error,
    )

    # Post snapshot for the before/after delta.
    post_usage = client.collect_usage(force_refresh=True) if not effective_dry else usage
    post_disks = (
        system_info.collect_disks(docker_root_dir=info.docker_root_dir)
        if not effective_dry
        else pre_disks
    )
    post_health = analyzer.worst(
        *(analyzer.assess_disk_health(d, config.thresholds) for d in post_disks)
    )
    delta = _compute_delta(pre_disks, post_disks, usage, post_usage, events)

    return CleanupResult(
        correlation_id=ctx.correlation_id,
        level=level,
        dry_run=effective_dry,
        plan=plan,
        audit_events=events,
        delta=delta,
        errors=errors,
        pre_health=pre_health,
        post_health=post_health,
    )


def _primary_free(disks: list[DiskUsage]) -> int:
    for disk in disks:
        if disk.is_docker_root:
            return disk.free_bytes
    return disks[0].free_bytes if disks else 0


def _compute_delta(
    pre_disks: list[DiskUsage],
    post_disks: list[DiskUsage],
    pre_usage: DockerUsage,
    post_usage: DockerUsage,
    events: list[AuditEvent],
) -> SpaceDelta:
    reclaimed = sum(
        e.reclaim_actual_bytes or e.reclaim_predicted_bytes
        for e in events
        if e.outcome == "removed"
    )
    return SpaceDelta(
        host_free_before_bytes=_primary_free(pre_disks),
        host_free_after_bytes=_primary_free(post_disks),
        docker_total_before_bytes=pre_usage.total_bytes,
        docker_total_after_bytes=post_usage.total_bytes,
        reclaimed_bytes=reclaimed,
    )


def overall_health(result: CleanupResult) -> HealthStatus:
    """Grade the post-cleanup health for exit-code mapping."""
    return result.post_health


# ---------------------------------------------------------------------------
# Emergency mode — "free space now", safest high-impact first
# ---------------------------------------------------------------------------

NUCLEAR_ONE_LINER = "docker system prune -af --volumes"


def emergency_next_steps(usage: DockerUsage) -> list[str]:
    """Higher-impact follow-ups to surface after the safe emergency prune.

    These are *suggestions* only — never executed. The final entry is the
    nuclear one-liner, shown but guarded behind the normal confirmation flow.
    """
    steps: list[str] = []
    big_volumes = sorted(
        (v for v in usage.volume_list if not v.in_use and not v.protected),
        key=lambda v: v.size_bytes or 0,
        reverse=True,
    )[:3]
    for vol in big_volumes:
        steps.append(
            f"Unused volume {vol.name} ({humanize_size(vol.size_bytes)}) — back up, then "
            f"`docker-disk cleanup --level 2` to remove unprotected volumes."
        )
    big_images = sorted(
        (i for i in usage.image_list if not i.in_use),
        key=lambda i: i.size_bytes,
        reverse=True,
    )[:3]
    for img in big_images:
        steps.append(
            f"Unused image {img.display_name} ({humanize_size(img.size_bytes)}) — "
            f"`docker-disk cleanup --level 3 --force` removes unused tagged images."
        )
    steps.append(f"Last resort (removes ALL unused incl. volumes): {NUCLEAR_ONE_LINER}")
    return steps


def run_emergency(
    ctx: RunContext,
    *,
    dry_run: bool | None = None,
    confirm_fn: ConfirmFn | None = None,
) -> CleanupResult:
    """Run the safest high-impact cleanup (level 1) immediately.

    Emergency mode auto-grants the single confirmation for the *safe* tier
    (dangling images, stopped containers, unused networks, build cache); it
    never removes volumes or in-use objects. ``dry_run`` still honours config
    unless overridden, so ``emergency`` previews and ``emergency --yes`` acts.
    """
    grant = confirm_fn or (lambda plan, mode: True)
    return run_cleanup(
        ctx,
        level=PruneLevel.SAFE,
        dry_run=dry_run,
        assume_yes=True,
        confirm_fn=grant,
    )
