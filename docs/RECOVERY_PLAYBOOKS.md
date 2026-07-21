# Recovery Playbooks

`docker-disk` detects common disk-bloat situations during `analyze` and can
generate tailored, **review-before-run** recovery scripts:

```bash
docker-disk analyze                 # findings include any triggered playbooks
docker-disk report --playbooks      # render the matching recovery scripts
```

Nothing destructive is ever executed automatically. Scripts are printed (and can
be written to `~/docker-disk-reports/recovery/`) for you to review and run.

---

## 1. Docker Desktop / WSL2 VHDX bloat (Windows & macOS)

**Symptom:** Your `C:` drive keeps filling even after `docker system prune`.
Docker Desktop stores everything inside a WSL2 `ext4.vhdx` (Windows) or a disk
image (macOS) that **grows but never auto-shrinks**. So host free space is *not*
the same as space available inside Docker — `analyze` flags this caveat.

**Detector:** backend is Docker Desktop/WSL and the largest VHDX on disk is much
larger than the data Docker actually uses (>10 GB slack = warning, >40 GB =
critical).

**Fix (Windows, reviewed, as Administrator):**

```powershell
# 1. Quit Docker Desktop from the tray, then:
wsl --shutdown
# 2a. Hyper-V present (Windows Pro/Enterprise):
Optimize-VHD -Path "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx" -Mode Full
# 2b. Windows Home (no Optimize-VHD): compact via diskpart
@"
select vdisk file="C:\Users\<you>\AppData\Local\Docker\wsl\disk\docker_data.vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@ | diskpart
```

**macOS:** Docker Desktop → Settings → Resources → "Disk image size" / Clean up,
or delete `~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw`
after quitting Docker (it is recreated).

---

## 2. BuildKit / build-cache explosion

**Symptom:** Build cache dominates `docker system df` (common on CI builders or
frequent `docker build` loops).

**Detector:** build cache > 5 GB, or > 40 % of total Docker usage.

**Fix:**

```bash
docker builder du                          # inspect
docker builder prune --filter until=168h -f   # safe: keep < 7 days & in-use
docker builder prune -a -f                 # aggressive: all not-in-use
docker builder prune --keep-storage 10GB -f   # cap future growth
```

`docker-disk cleanup --level 1` already prunes reclaimable build cache safely.

---

## 3. AI/ML image bloat (Ollama, CUDA, large bases)

**Symptom:** A handful of images (CUDA/PyTorch/TensorFlow/vLLM/Ollama) consume
tens of GB.

**Detector:** images matching AI/ML name patterns, or any single image > 5 GB.

**Fix (review each line; running models are excluded):**

```bash
docker image ls --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | sort -k2 -h
docker image rm <unused-image>             # only unreferenced images
ollama list && ollama rm <model>           # Ollama models live in a volume
```

Tips: prefer `…:runtime` over `…:devel` CUDA bases; a single Ollama volume can
hold many GB of models — list and remove what you no longer use.

---

## 4. Named-volume creep (databases, Nextcloud, TrueNAS)

**Symptom:** Named volumes holding real data (Postgres/MySQL/Mongo, Nextcloud,
TrueNAS-adjacent mounts) grow steadily.

**Detector:** stateful-named volumes > 2 GB, or any local volume > 5 GB. These
are flagged **protected** — the toolkit never proposes deleting them.

**Fix — back up first, reclaim inside the app, never blind-delete:**

```bash
# Back up a volume to a tarball before touching it
docker run --rm -v <vol>:/data -v "$PWD":/backup alpine \
  tar czf /backup/<vol>.tar.gz -C /data .

# Reclaim inside the application instead of deleting the volume:
#   Postgres:  docker exec <pg> psql -c 'VACUUM FULL;'
#   Nextcloud: docker exec <nc> php occ trashbin:cleanup --all-users
#   MySQL:     OPTIMIZE TABLE ...;
```

Only `docker volume rm <vol>` once you have a **verified** backup and are sure it
is unused. Add app-specific names to your protect-list to be safe:
`docker-disk cleanup --level 2 --protect 'nextcloud|pgdata'`.

---

## 5. Linux overlay2 bloat

**Symptom:** `/var/lib/docker/overlay2` is huge on a native Linux engine.

**Detector:** overlay2 driver with > 10 GB (or > 50 %) reclaimable.

**Fix:**

```bash
docker system df
docker-disk cleanup --level 1              # safe prune first
docker system prune -a                     # remove all unused images
sudo du -sh /var/lib/docker/overlay2       # confirm on disk
```

---

## Emergency mode & "what just ate my disk?"

- `docker-disk emergency --no-dry-run` — runs the safest high-impact tier
  (build cache → dangling images → stopped containers → unused networks),
  stops there, and prints higher-impact next steps (including the guarded
  `docker system prune -af --volumes` one-liner, which it never runs for you).
- `docker-disk analyze --forensic --since 24h` — correlates recently created
  images/build-cache/containers with the growth to answer *what just ate my disk*.

For hosts without Python, `scripts/emergency-cleanup.sh` performs the same safe
tier using only the `docker` CLI.
