"""Unit tests for :mod:`docker_disk_toolkit.utils`.

These exercise the highest-risk pure functions: size parsing/humanizing,
reclaimable/count parsing, protect-list matching, time/duration parsing, and
the CommandRunner fixture seam.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from docker_disk_toolkit import utils
from docker_disk_toolkit.utils import (
    FixtureCommandRunner,
    RegexCompileError,
    SizeParseError,
    compile_protect_matchers,
    result,
)


class TestParseSize:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0B", 0),
            ("0", 0),
            ("", 0),
            ("N/A", 0),
            ("<unknown>", 0),
            ("512", 512),
            ("512B", 512),
            ("1kB", 1_000),
            ("1.05kB", 1_050),
            ("123.4MB", 123_400_000),
            ("2.3GB", 2_300_000_000),
            ("4.1GB", 4_100_000_000),
            ("1KiB", 1_024),
            ("955.5MiB", 1_001_914_368),
            ("1.1TiB", 1_209_462_790_554),
            ("1,5GB", 1_500_000_000),  # comma decimal separator
        ],
    )
    def test_known_values(self, text: str, expected: int) -> None:
        assert utils.parse_size(text) == expected

    def test_int_and_float_passthrough(self) -> None:
        assert utils.parse_size(1234) == 1234
        assert utils.parse_size(1234.9) == 1234
        assert utils.parse_size(None) == 0

    def test_default_binary_flag(self) -> None:
        assert utils.parse_size("1GB", default_binary=True) == 1024**3

    @pytest.mark.parametrize("bad", ["abc", "12 qux", "GB", "1.2.3GB", "12 quux"])
    def test_malformed_raises(self, bad: str) -> None:
        with pytest.raises(SizeParseError):
            utils.parse_size(bad)


class TestHumanizeSize:
    @pytest.mark.parametrize(
        ("num", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (4_100_000_000, "4.1 GB"),
            (1_500_000, "1.5 MB"),
        ],
    )
    def test_decimal(self, num: int, expected: str) -> None:
        assert utils.humanize_size(num) == expected

    def test_binary(self) -> None:
        assert utils.humanize_size(1024, binary=True) == "1.0 KiB"

    def test_none_and_negative(self) -> None:
        assert utils.humanize_size(None) == "n/a"
        assert utils.humanize_size(-2_000_000).startswith("-2.0 MB")

    def test_roundtrip_is_close(self) -> None:
        original = 3_500_000_000
        human = utils.humanize_size(original)
        assert utils.parse_size(human.replace(" ", "")) == pytest.approx(original, rel=0.01)


class TestReclaimableAndCount:
    def test_reclaimable_with_percent(self) -> None:
        assert utils.parse_reclaimable("2.3GB (56%)") == (2_300_000_000, 56.0)

    def test_reclaimable_without_percent(self) -> None:
        assert utils.parse_reclaimable("0B") == (0, None)

    def test_reclaimable_none(self) -> None:
        assert utils.parse_reclaimable(None) == (0, None)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("12", 12), (5, 5), ("-1", 0), ("", 0), (None, 0), ("garbage", 0)],
    )
    def test_parse_count(self, value: object, expected: int) -> None:
        assert utils.parse_count(value) == expected  # type: ignore[arg-type]


class TestTimeParsing:
    def test_rfc3339_nanoseconds(self) -> None:
        parsed = utils.parse_docker_time("2026-07-20T14:05:31.123456789Z")
        assert parsed is not None
        assert parsed.year == 2026 and parsed.tzinfo is not None

    def test_ls_created_at_form(self) -> None:
        parsed = utils.parse_docker_time("2026-07-20 14:05:31 +0200 SAST")
        assert parsed is not None
        assert parsed.utcoffset() == timedelta(hours=2)

    def test_zero_and_empty(self) -> None:
        assert utils.parse_docker_time("0001-01-01T00:00:00Z") is None
        assert utils.parse_docker_time("") is None
        assert utils.parse_docker_time(None) is None
        assert utils.parse_docker_time("not a date") is None

    def test_age(self) -> None:
        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        when = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
        assert utils.age(when, now=now) == timedelta(days=1)
        assert utils.age(None) is None

    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("7d", 604800), ("12h", 43200), ("30m", 1800), ("45s", 45), ("2w", 1209600)],
    )
    def test_parse_duration(self, text: str, seconds: int) -> None:
        delta = utils.parse_duration(text)
        assert delta is not None and delta.total_seconds() == seconds

    def test_parse_duration_empty_and_bad(self) -> None:
        assert utils.parse_duration(None) is None
        assert utils.parse_duration("  ") is None
        with pytest.raises(ValueError):
            utils.parse_duration("soon")


class TestProtectMatching:
    def test_exact_glob_regex(self) -> None:
        matcher = compile_protect_matchers(["postgres_data", "*_cache", "regex:^db_"])
        assert matcher.matches("postgres_data")
        assert matcher.matches("build_cache")
        assert matcher.matches("db_main")
        assert not matcher.matches("random_volume")

    def test_comma_separated_tokens(self) -> None:
        matcher = compile_protect_matchers(["a,b,c"])
        assert matcher.matches("a") and matcher.matches("b") and matcher.matches("c")

    def test_regex_list_param(self) -> None:
        matcher = compile_protect_matchers(regexes=["nextcloud", "^vault"])
        assert matcher.matches("nextcloud_data")
        assert matcher.matches("vault123")

    def test_empty_matcher_is_falsy(self) -> None:
        matcher = compile_protect_matchers()
        assert not matcher
        assert not matcher.matches("anything")

    def test_bad_regex_raises(self) -> None:
        with pytest.raises(RegexCompileError):
            compile_protect_matchers(["regex:["])


class TestPathAndEnvHelpers:
    def test_safe_output_path_rejects_traversal(self) -> None:
        assert not utils.is_safe_output_path(Path("reports/../.."))

    def test_safe_output_path_rejects_system_root(self, tmp_path: Path) -> None:
        assert not utils.is_safe_output_path(Path("/"))
        assert utils.is_safe_output_path(tmp_path / "reports")

    def test_system_dirs_are_absolute_and_resolved(self) -> None:
        """Regression guard for the membership test silently never matching.

        ``is_safe_output_path`` compares a *resolved* candidate against this
        set, so an unresolved entry (e.g. ``Path("/etc")`` on Windows, which is
        drive-less) can never match and the guard becomes a no-op.
        """
        assert utils._SYSTEM_DIRS, "system-dir set must never be empty"
        for system_dir in utils._SYSTEM_DIRS:
            assert system_dir.is_absolute()
            assert system_dir == system_dir.resolve()

    def test_safe_output_path_rejects_any_filesystem_root(self, tmp_path: Path) -> None:
        """Roots are detected structurally, so every drive letter is covered.

        The previous implementation enumerated only ``C:\\``, leaving ``D:\\``
        and every other volume accepted.
        """
        assert not utils.is_safe_output_path(Path(tmp_path.anchor))

    def test_safe_output_path_rejects_system_dirs_and_their_children(self) -> None:
        for system_dir in utils._SYSTEM_DIRS:
            assert not utils.is_safe_output_path(system_dir)
            assert not utils.is_safe_output_path(system_dir / "docker-disk-reports")

    def test_safe_output_path_allows_ordinary_locations(self, tmp_path: Path) -> None:
        assert utils.is_safe_output_path(tmp_path / "reports")
        assert utils.is_safe_output_path(Path("~/docker-disk-reports"))

    def test_safe_output_path_rejects_unresolvable(self) -> None:
        assert not utils.is_safe_output_path(Path("reports") / ".." / ".." / "..")

    def test_posix_candidates_cover_the_system_tree(self) -> None:
        """Branch selection is asserted purely; the POSIX filesystem behaviour
        itself is exercised by the Linux CI job."""
        candidates = utils._system_dir_candidates(windows=False)
        assert {"/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev"} <= set(
            candidates
        )
        # /var and %ProgramData% stay writable for service deployments.
        assert "/var" not in candidates

    def test_windows_candidates_follow_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYSTEMROOT", r"E:\CustomWindows")
        candidates = utils._system_dir_candidates(windows=True)
        assert r"E:\CustomWindows" in candidates

    def test_windows_candidates_fall_back_without_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            monkeypatch.delenv(var, raising=False)
        candidates = utils._system_dir_candidates(windows=True)
        assert candidates == ["C:/Windows", "C:/Program Files", "C:/Program Files (x86)"]

    def test_atomic_write(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "out.txt"
        utils.atomic_write_text(target, "hello")
        assert target.read_text() == "hello"
        utils.atomic_write_text(target, "world")
        assert target.read_text() == "world"
        # no leftover temp files
        assert list(target.parent.glob(".*tmp")) == []

    def test_new_run_id_unique(self) -> None:
        assert utils.new_run_id() != utils.new_run_id()
        assert len(utils.new_run_id()) == 12

    def test_safe_percent(self) -> None:
        assert utils.safe_percent(1, 4) == 25.0
        assert utils.safe_percent(1, 0) == 0.0


class TestNdjson:
    def test_iter_ndjson_skips_noise(self) -> None:
        text = 'WARNING: deprecated\n{"a": 1}\n\n{"b": 2}\nnot json\n[1,2]\n'
        objects = list(utils.iter_ndjson(text))
        assert objects == [{"a": 1}, {"b": 2}]

    def test_iter_ndjson_empty(self) -> None:
        assert list(utils.iter_ndjson("")) == []


class TestCommandRunner:
    def test_fixture_prefix_match(self) -> None:
        runner = FixtureCommandRunner(
            [
                (["docker", "image", "ls"], result('{"ID":"abc"}')),
                (["docker", "version"], result('{"Client":{}}')),
            ]
        )
        res = runner.run(["docker", "image", "ls", "-a", "--format", "{{json .}}"])
        assert res.stdout == '{"ID":"abc"}'
        assert res.ok
        # argv is re-stamped to what the caller asked for
        assert res.argv[-1] == "{{json .}}"
        assert runner.calls[-1][1] == "image"

    def test_fixture_default_and_miss(self) -> None:
        runner = FixtureCommandRunner([], default=result("x", returncode=1))
        assert runner.run(["anything"]).returncode == 1
        runner2 = FixtureCommandRunner([])
        miss = runner2.run(["nope"])
        assert miss.returncode == 127 and "no fixture" in miss.stderr

    def test_subprocess_runner_missing_binary(self) -> None:
        res = utils.SubprocessRunner().run(["definitely-not-a-real-binary-xyz"])
        assert res.returncode == 127
        assert not res.ok

    def test_default_runner_is_singleton(self) -> None:
        assert utils.default_runner() is utils.default_runner()
