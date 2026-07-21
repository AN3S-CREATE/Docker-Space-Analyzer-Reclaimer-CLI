"""Playbook: Docker Desktop / WSL2 VHDX bloat.

WSL2 virtual disks grow but never auto-shrink, so the ``.vhdx`` on the Windows
host can be far larger than the data Docker actually uses. Compacting requires
shutting Docker down and running ``Optimize-VHD`` (Hyper-V) or ``diskpart``.
"""

from __future__ import annotations

from ..models import DockerBackend, Finding, HealthStatus
from ..utils import humanize_size
from .base import BasePlaybook, GeneratedScript, PlaybookContext

_DESKTOP_OR_WSL = {
    DockerBackend.DESKTOP_WINDOWS,
    DockerBackend.DESKTOP_MAC,
    DockerBackend.DESKTOP_LINUX,
    DockerBackend.WSL,
}

SLACK_WARN_BYTES = 10 * 1000**3
SLACK_CRITICAL_BYTES = 40 * 1000**3


class VhdxBloatPlaybook(BasePlaybook):
    """Detect and remediate an oversized Docker Desktop WSL2 VHDX."""

    id = "vhdx-bloat"
    title = "Docker Desktop / WSL2 VHDX bloat"

    def detect(self, ctx: PlaybookContext) -> Finding | None:
        if ctx.backend not in _DESKTOP_OR_WSL or not ctx.vhdx_files:
            return None
        largest = ctx.vhdx_files[0]
        slack = max(largest.size_bytes - ctx.usage.total_bytes, 0)
        if slack < SLACK_WARN_BYTES:
            return None
        severity = HealthStatus.CRITICAL if slack >= SLACK_CRITICAL_BYTES else HealthStatus.WARNING
        return Finding(
            severity=severity,
            code=self.id,
            message=(
                f"VHDX {largest.path} is {humanize_size(largest.size_bytes)} on disk but Docker "
                f"only uses ~{humanize_size(ctx.usage.total_bytes)} inside it — about "
                f"{humanize_size(slack)} is reclaimable by compacting the virtual disk."
            ),
            detail={
                "vhdx_path": largest.path,
                "vhdx_size_bytes": largest.size_bytes,
                "docker_used_bytes": ctx.usage.total_bytes,
                "all_vhdx": [{"path": v.path, "size_bytes": v.size_bytes} for v in ctx.vhdx_files],
            },
            playbook=self.id,
            est_reclaimable_bytes=slack,
        )

    def render_script(self, ctx: PlaybookContext) -> GeneratedScript:
        paths = [v.path for v in ctx.vhdx_files] or [
            r"%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx"
        ]
        optimize_lines = "\n".join(f'Optimize-VHD -Path "{p}" -Mode Full' for p in paths)
        diskpart_blocks = "\n".join(
            '@"\n'
            f'select vdisk file="{p}"\n'
            "attach vdisk readonly\n"
            "compact vdisk\n"
            "detach vdisk\n"
            "exit\n"
            '"@ | diskpart'
            for p in paths
        )
        steps = [
            "Quit Docker Desktop completely (tray icon -> Quit Docker Desktop).",
            "Shut down all WSL2 distros: wsl --shutdown",
            "Compact the VHDX with Optimize-VHD (Hyper-V) OR diskpart (Windows Home).",
            "Start Docker Desktop again and re-run: docker-disk analyze",
        ]
        content = f"""# Docker Desktop VHDX compaction — review before running (PowerShell, as Administrator)
# Reclaimable estimate: {humanize_size(ctx.vhdx_files[0].size_bytes - ctx.usage.total_bytes) if ctx.vhdx_files else 'n/a'}
# ------------------------------------------------------------------------------

Write-Host "1. Quit Docker Desktop from the system tray before continuing." -ForegroundColor Yellow
Read-Host "Press Enter once Docker Desktop is fully stopped"

# 2. Shut down WSL2 so the VHDX is not in use
wsl --shutdown
Start-Sleep -Seconds 5

# 3a. Preferred: Optimize-VHD (requires the Hyper-V feature; Windows Pro/Enterprise)
if (Get-Command Optimize-VHD -ErrorAction SilentlyContinue) {{
{optimize_lines}
}} else {{
    Write-Host "Optimize-VHD not available (Windows Home). Falling back to diskpart..." -ForegroundColor Yellow
    # 3b. Fallback: diskpart compact (read-only attach keeps data safe)
{diskpart_blocks}
}}

Write-Host "Done. Start Docker Desktop and run: docker-disk analyze" -ForegroundColor Green
"""
        return GeneratedScript(
            playbook_id=self.id,
            title=self.title,
            shell="powershell",
            filename="recover_vhdx_bloat.ps1",
            steps=steps,
            content=content,
            requires_elevation=True,
            est_reclaim_bytes=(
                ctx.vhdx_files[0].size_bytes - ctx.usage.total_bytes if ctx.vhdx_files else None
            ),
            danger="Compaction requires Docker fully stopped; never run against an in-use VHDX.",
        )
