from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "deploy/create-wechat-direct-backup.sh"
READER_SCRIPT = ROOT / "deploy/rollback-personal-wechat-reader.sh"
RELEASE_SCRIPT = ROOT / "deploy/rollback-workbench-release.sh"


def run_script(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["zsh", str(script), *args],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_manifest(backup_dir: Path, *relative_paths: str) -> None:
    lines = []
    for relative_path in relative_paths:
        digest = hashlib.sha256((backup_dir / relative_path).read_bytes()).hexdigest()
        lines.append(f"{digest}  {relative_path}")
    (backup_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_release_backup(tmp_path: Path) -> Path:
    backup_dir = tmp_path / "release-backup"
    backup_dir.mkdir()
    launchagents = backup_dir / "launchagents"
    launchagents.mkdir()
    (launchagents / "com.finance-workbench.worker.plist").write_text(
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<plist version="1.0"><dict>\n'
            "<key>Label</key><string>com.finance-workbench.worker</string>\n"
            "<key>ProgramArguments</key><array><string>python</string></array>\n"
            "</dict></plist>\n"
        ),
        encoding="utf-8",
    )
    (backup_dir / "compose.yaml").write_text(
        "services:\n  workbench:\n    image: finance-workbench:local\n",
        encoding="utf-8",
    )
    (backup_dir / "workbench.sqlite3").write_bytes(b"SQLite format 3\x00test")
    (backup_dir / "objects.tgz").write_bytes(b"objects tar")
    (backup_dir / "docker-image-id.txt").write_text(
        f"sha256:{'a' * 64}\n", encoding="utf-8"
    )
    (backup_dir / "docker-image-tag.txt").write_text(
        "finance-workbench:local\n", encoding="utf-8"
    )
    write_manifest(
        backup_dir,
        "compose.yaml",
        "workbench.sqlite3",
        "objects.tgz",
        "docker-image-id.txt",
        "docker-image-tag.txt",
        "launchagents/com.finance-workbench.worker.plist",
    )
    return backup_dir


def test_rollback_scripts_keep_the_safe_surface_static() -> None:
    scripts = {
        "backup": BACKUP_SCRIPT.read_text(encoding="utf-8"),
        "reader": READER_SCRIPT.read_text(encoding="utf-8"),
        "release": RELEASE_SCRIPT.read_text(encoding="utf-8"),
    }

    assert all("--dry-run" in content for content in scripts.values())
    for content in scripts.values():
        assert "git reset --hard" not in content
        assert "docker compose down -v" not in content
        assert "rm -rf" not in content

    backup = scripts["backup"]
    for required in (
        "rev-parse HEAD",
        "status --short --branch",
        "diff --binary HEAD",
        "workbench.sqlite3",
        "objects.tgz",
        "launchagents",
        "docker-image-id.txt",
        "docker-image-tag.txt",
        "SHA256SUMS",
        "mkstemp",
        "os.unlink",
    ):
        assert required in backup

    reader = scripts["reader"]
    assert "PERSONAL_WECHAT_READER" in reader
    assert "direct|--mode" not in reader
    assert "direct|ciphertalk" in reader
    assert "com.finance-workbench.worker" in reader
    assert "kickstart" in reader and "-k" in reader

    release = scripts["release"]
    assert "--backup-dir" in release
    assert "SHA256SUMS" in release
    assert "image inspect" in release
    assert "image tag" in release
    assert "--no-build" in release
    assert "launchctl" in release
    assert "com.finance-workbench.worker" in release
    assert 'cp -p "$runtime_config" "$staged_runtime_config"' in release
    assert 'mv -f "$staged_runtime_config" "$compose_file"' in release
    assert "workbench.sqlite3" in release
    assert "不会恢复或覆盖 SQLite" in release


def test_backup_dry_run_does_not_create_or_call_live_tools(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    backup_dir = tmp_path / "backup"
    result = run_script(
        BACKUP_SCRIPT,
        "--project-root",
        str(project_root),
        "--backup-dir",
        str(backup_dir),
        "--dry-run",
        env={"DOCKER_BIN": str(tmp_path / "missing-docker")},
    )

    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert not backup_dir.exists()


@pytest.mark.parametrize("reader", ["direct", "ciphertalk"])
def test_personal_reader_dry_run_uses_backup_and_never_touches_launchctl(
    tmp_path: Path, reader: str
) -> None:
    backup_dir = tmp_path / "backup"
    launchagents = backup_dir / "launchagents"
    launchagents.mkdir(parents=True)
    (launchagents / "com.finance-workbench.worker.plist").write_text(
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<plist version="1.0"><dict>\n'
            "<key>Label</key><string>com.finance-workbench.worker</string>\n"
            "<key>EnvironmentVariables</key><dict>\n"
            "<key>PERSONAL_WECHAT_READER</key><string>ciphertalk</string>\n"
            "</dict></dict></plist>\n"
        ),
        encoding="utf-8",
    )
    target_dir = tmp_path / "live-launchagents"

    result = run_script(
        READER_SCRIPT,
        "--backup-dir",
        str(backup_dir),
        "--reader",
        reader,
        "--launchagent-dir",
        str(target_dir),
        "--dry-run",
        env={"LAUNCHCTL_BIN": str(tmp_path / "missing-launchctl")},
    )

    assert result.returncode == 0, result.stderr
    assert f"PERSONAL_WECHAT_READER={reader}" in result.stdout
    assert "com.finance-workbench.worker" in result.stdout
    assert "不会调用 launchctl" in result.stdout
    assert not (target_dir / "com.finance-workbench.worker.plist").exists()


def test_release_dry_run_validates_backup_and_leaves_sqlite_and_runtime_untouched(
    tmp_path: Path,
) -> None:
    backup_dir = make_release_backup(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    launchagent_dir = tmp_path / "live-launchagents"
    current_compose = project_root / "compose.yaml"
    current_compose.write_text("current-runtime\n", encoding="utf-8")

    result = run_script(
        RELEASE_SCRIPT,
        "--backup-dir",
        str(backup_dir),
        "--project-root",
        str(project_root),
        "--launchagent-dir",
        str(launchagent_dir),
        "--dry-run",
        env={"DOCKER_BIN": str(tmp_path / "missing-docker")},
    )

    assert result.returncode == 0, result.stderr
    assert "SHA256 manifest 已验证" in result.stdout
    assert "docker image tag" in result.stdout
    assert "saved worker LaunchAgent" in result.stdout
    assert "不会恢复或覆盖 SQLite" in result.stdout
    assert current_compose.read_text(encoding="utf-8") == "current-runtime\n"
    assert (backup_dir / "workbench.sqlite3").read_bytes() == b"SQLite format 3\x00test"
    assert not (launchagent_dir / "com.finance-workbench.worker.plist").exists()


def test_release_dry_run_rejects_bad_checksum_before_service_change(
    tmp_path: Path,
) -> None:
    backup_dir = make_release_backup(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    shutil.copytree(backup_dir, project_root, dirs_exist_ok=True)
    (backup_dir / "objects.tgz").write_bytes(b"changed after manifest")

    result = run_script(
        RELEASE_SCRIPT,
        "--backup-dir",
        str(backup_dir),
        "--project-root",
        str(project_root),
        "--dry-run",
    )

    assert result.returncode != 0
    assert "SHA256 manifest 校验失败" in result.stderr
    assert "docker image tag" not in result.stdout


def test_personal_reader_switch_rehearsal_preserves_current_worker_config(
    tmp_path: Path,
) -> None:
    backup_dir = tmp_path / "backup"
    backup_agents = backup_dir / "launchagents"
    live_agents = tmp_path / "live-launchagents"
    backup_agents.mkdir(parents=True)
    live_agents.mkdir()
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>'
        "<key>Label</key><string>com.finance-workbench.worker</string>"
        "<key>EnvironmentVariables</key><dict>"
        "<key>PERSONAL_WECHAT_READER</key><string>ciphertalk</string>"
        "<key>KEEP_ME</key><string>preserved</string>"
        "</dict></dict></plist>\n"
    )
    backup = backup_agents / "com.finance-workbench.worker.plist"
    target = live_agents / "com.finance-workbench.worker.plist"
    backup.write_text(plist, encoding="utf-8")
    target.write_text(plist, encoding="utf-8")
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        '#!/bin/zsh\n[[ "$1" == "print" ]] && print "state = running\\npid = 123"\nexit 0\n',
        encoding="utf-8",
    )
    fake_launchctl.chmod(0o700)
    common = (
        "--backup-dir",
        str(backup_dir),
        "--launchagent-dir",
        str(live_agents),
    )
    direct = run_script(
        READER_SCRIPT,
        *common,
        "--reader",
        "direct",
        "--enabled-at",
        "2026-09-01T00:00:00Z",
        env={"LAUNCHCTL_BIN": str(fake_launchctl)},
    )
    assert direct.returncode == 0, direct.stderr
    assert "direct" in target.read_text(encoding="utf-8")
    assert "2026-09-01T00:00:00Z" in target.read_text(encoding="utf-8")
    assert "KEEP_ME" in target.read_text(encoding="utf-8")
    fallback = run_script(
        READER_SCRIPT,
        *common,
        "--reader",
        "ciphertalk",
        env={"LAUNCHCTL_BIN": str(fake_launchctl)},
    )
    assert fallback.returncode == 0, fallback.stderr
    assert "ciphertalk" in target.read_text(encoding="utf-8")
    assert "2026-09-01T00:00:00Z" in target.read_text(encoding="utf-8")


def test_full_release_rollback_rehearsal_changes_runtime_only(tmp_path: Path) -> None:
    backup_dir = make_release_backup(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    launchagent_dir = tmp_path / "live-launchagents"
    compose = project_root / "compose.yaml"
    compose.write_text(
        "services:\n  workbench:\n    image: current:broken\n", encoding="utf-8"
    )
    database_before = (backup_dir / "workbench.sqlite3").read_bytes()
    objects_before = (backup_dir / "objects.tgz").read_bytes()
    fake_docker = tmp_path / "docker"
    docker_log = tmp_path / "docker.log"
    fake_docker.write_text(
        '#!/bin/zsh\nprint -r -- "$*" >> "$FAKE_DOCKER_LOG"\n'
        '[[ " $* " == *" ps "* ]] && print "workbench"\nexit 0\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    fake_plist_buddy = tmp_path / "PlistBuddy"
    fake_plist_buddy.write_text(
        '#!/bin/zsh\nprint -r -- "com.finance-workbench.worker"\n',
        encoding="utf-8",
    )
    fake_plist_buddy.chmod(0o700)
    fake_plutil = tmp_path / "plutil"
    fake_plutil.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
    fake_plutil.chmod(0o700)
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/zsh\n"
        'if [[ "$1" == "print" ]]; then\n'
        '  print -r -- "state = running"\n'
        '  print -r -- "pid = 123"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(0o700)
    result = run_script(
        RELEASE_SCRIPT,
        "--backup-dir",
        str(backup_dir),
        "--project-root",
        str(project_root),
        "--launchagent-dir",
        str(launchagent_dir),
        env={
            "DOCKER_BIN": str(fake_docker),
            "FAKE_DOCKER_LOG": str(docker_log),
            "PLIST_BUDDY": str(fake_plist_buddy),
            "PLUTIL_BIN": str(fake_plutil),
            "LAUNCHCTL_BIN": str(fake_launchctl),
        },
    )
    assert result.returncode == 0, result.stderr
    assert compose.read_text(encoding="utf-8") == (
        backup_dir / "compose.yaml"
    ).read_text(encoding="utf-8")
    calls = docker_log.read_text(encoding="utf-8")
    assert "image inspect" in calls and "image tag" in calls
    assert "--no-build" in calls and "--force-recreate workbench" in calls
    assert (backup_dir / "workbench.sqlite3").read_bytes() == database_before
    assert (backup_dir / "objects.tgz").read_bytes() == objects_before
    assert (launchagent_dir / "com.finance-workbench.worker.plist").read_bytes() == (
        backup_dir / "launchagents/com.finance-workbench.worker.plist"
    ).read_bytes()
