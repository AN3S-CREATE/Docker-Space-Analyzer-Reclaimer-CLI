"""Playbook: AI/ML image bloat (Ollama, CUDA, large base images)."""

from __future__ import annotations

import re

from ..models import Finding, HealthStatus, ImageInfo
from ..utils import humanize_size
from .base import BasePlaybook, GeneratedScript, PlaybookContext

AI_PATTERN = re.compile(
    r"(?i)(ollama|cuda|nvidia|pytorch|tensorflow|/tf|vllm|huggingface|tensorrt|"
    r"jupyter|nvcr\.io|deepstream)"
)
LARGE_BASE_BYTES = 5 * 1000**3


def _is_ai_image(image: ImageInfo) -> bool:
    if any(AI_PATTERN.search(tag) for tag in image.repo_tags):
        return True
    return image.size_bytes >= LARGE_BASE_BYTES


class AiMlBloatPlaybook(BasePlaybook):
    """Detect large AI/ML images and offer selective, safe removal."""

    id = "ai-ml-bloat"
    title = "AI/ML image bloat (Ollama, CUDA, large bases)"

    def _candidates(self, ctx: PlaybookContext) -> list[ImageInfo]:
        return sorted(
            (img for img in ctx.usage.image_list if _is_ai_image(img)),
            key=lambda i: i.size_bytes,
            reverse=True,
        )

    def detect(self, ctx: PlaybookContext) -> Finding | None:
        candidates = self._candidates(ctx)
        if not candidates:
            return None
        total = sum(img.size_bytes for img in candidates)
        removable = sum(img.reclaim_bytes for img in candidates if not img.in_use)
        return Finding(
            severity=HealthStatus.WARNING,
            code=self.id,
            message=(
                f"{len(candidates)} AI/ML or large base image(s) use {humanize_size(total)}; "
                f"~{humanize_size(removable)} in unused images can be reclaimed."
            ),
            detail={
                "images": [
                    {"name": img.display_name, "size_bytes": img.size_bytes, "in_use": img.in_use}
                    for img in candidates
                ],
                "removable_bytes": removable,
            },
            playbook=self.id,
            est_reclaimable_bytes=removable,
        )

    def render_script(self, ctx: PlaybookContext) -> GeneratedScript:
        candidates = self._candidates(ctx)
        unused = [img for img in candidates if not img.in_use]
        rm_lines = (
            "\n".join(
                f"# {img.display_name}  ({humanize_size(img.size_bytes)})\n"
                f"docker image rm {img.repo_tags[0] if img.repo_tags else img.id}"
                for img in unused
            )
            or "# (no unused AI/ML images to remove)"
        )
        steps = [
            "Review the AI/ML images below and their sizes.",
            "Remove only images you no longer need (running models are excluded).",
            "For Ollama, remove unused models with: ollama rm <model>",
            "Consider a slimmer base (e.g. cuda runtime instead of devel).",
        ]
        content = f"""#!/usr/bin/env bash
# AI/ML image cleanup — review each line before running. NOTHING runs automatically.
set -euo pipefail

# --- Unused AI/ML images (safe to remove) ---
{rm_lines}

# --- Ollama models (list, then remove unused ones) ---
# ollama list
# ollama rm <model-name>

echo "Done. Re-run: docker-disk analyze"
"""
        return GeneratedScript(
            playbook_id=self.id,
            title=self.title,
            shell="bash",
            filename="recover_ai_ml_images.sh",
            steps=steps,
            content=content,
            est_reclaim_bytes=sum(img.reclaim_bytes for img in unused),
            danger="Do not remove images backing a running model/container.",
        )
