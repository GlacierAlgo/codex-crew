"""Click command-line interface for codex-crew."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shlex
from typing import Any, Sequence

import click

from codex_crew.hook import run_stop_hook
from codex_crew.launcher import CrewLaunch, LaunchError, launch_crew
from codex_crew.loop_package import (
    DEFAULT_LOOP_ID,
    LoopPackageError,
    check_loop_installation,
    discover_loop_packages,
    install_loop_package,
    load_loop_package,
)
from codex_crew.storage import (
    aggregate_latest,
    default_database_path,
    initialize_database,
    latest_final,
    latest_snapshots,
)


@dataclass(frozen=True)
class CliContext:
    database_path: Path


@click.group(help="Capture, query, and launch Codex crew sessions.")
@click.option(
    "--db",
    type=click.Path(path_type=Path, dir_okay=False),
    help="SQLite path (default: CODEX_CREW_DB or ~/.local/state/codex-crew/snapshots.sqlite3).",
)
@click.pass_context
def cli(context: click.Context, db: Path | None) -> None:
    context.obj = CliContext(database_path=db or default_database_path())


@cli.command("init-db", help="Create the SQLite schema.")
@click.pass_obj
def init_db(context: CliContext) -> None:
    click.echo(initialize_database(context.database_path))


@cli.group("hook", help="Lifecycle hook entry points.")
def hook_group() -> None:
    pass


@hook_group.command("stop", help="Read a Stop payload from stdin.")
@click.pass_obj
def hook_stop(context: CliContext) -> None:
    code = run_stop_hook(database_path=context.database_path)
    if code:
        raise click.exceptions.Exit(code)


@cli.command("latest", help="Show recent Stop snapshots.")
@click.option("--session-id")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--json", "json_output", is_flag=True)
@click.pass_obj
def latest(
    context: CliContext,
    session_id: str | None,
    limit: int,
    json_output: bool,
) -> None:
    rows = latest_snapshots(
        context.database_path,
        session_id=session_id,
        limit=limit,
    )
    if json_output:
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_latest(rows)


@cli.command("final", help="Print the latest captured final message.")
@click.option("--session-id")
@click.option("--turn-id")
@click.pass_obj
def final(context: CliContext, session_id: str | None, turn_id: str | None) -> None:
    final_text = latest_final(
        context.database_path,
        session_id=session_id,
        turn_id=turn_id,
    )
    if final_text is None:
        raise click.exceptions.Exit(1)
    click.echo(final_text)


@cli.command("summary", help="Aggregate the latest cumulative snapshot per session.")
@click.option("--json", "json_output", is_flag=True)
@click.pass_obj
def summary(context: CliContext, json_output: bool) -> None:
    values = aggregate_latest(context.database_path)
    if json_output:
        click.echo(json.dumps(values, ensure_ascii=False, indent=2))
    else:
        for key, value in values.items():
            click.echo(f"{key}: {value}")


@cli.command("hook-config", help="Print a hooks.json fragment for this checkout.")
@click.pass_obj
def hook_config(context: CliContext) -> None:
    click.echo(
        json.dumps(_hook_config(context.database_path), ensure_ascii=False, indent=2)
    )


@cli.group("loop", help="Discover and manage repository-owned loop packages.")
def loop_group() -> None:
    pass


@loop_group.command("list", help="List validated loop packages.")
@click.option("--json", "json_output", is_flag=True)
def loop_list(json_output: bool) -> None:
    try:
        packages = discover_loop_packages()
    except LoopPackageError as error:
        raise click.ClickException(str(error)) from error
    if json_output:
        click.echo(
            json.dumps(
                [
                    {
                        "id": package.id,
                        "manual": str(package.manual_path),
                        "roles": [role.id for role in package.roles],
                        "runtime_profiles": [
                            role.runtime_profile for role in package.roles
                        ],
                        "layout": package.layout.name,
                    }
                    for package in packages
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for package in packages:
        roles = ",".join(role.id for role in package.roles)
        click.echo(f"{package.id}\troles={roles}\tlayout={package.layout.name}")


@loop_group.command("install", help="Install managed runtime profile adapters.")
@click.argument("loop_id", default=DEFAULT_LOOP_ID)
@click.option(
    "--codex-home",
    type=click.Path(path_type=Path, file_okay=False),
    help="Codex home (default: CODEX_HOME or ~/.codex).",
)
def loop_install(loop_id: str, codex_home: Path | None) -> None:
    try:
        package = load_loop_package(loop_id)
        profiles = install_loop_package(package, codex_home=codex_home)
    except LoopPackageError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"installed loop {package.id}: {len(profiles)} managed profiles")
    for profile in profiles:
        click.echo(
            f"{profile.role}\t{profile.runtime_profile}\t{profile.symlink_path}"
        )


@loop_group.command("check", help="Validate sources and installed profile adapters.")
@click.argument("loop_id", default=DEFAULT_LOOP_ID)
@click.option(
    "--codex-home",
    type=click.Path(path_type=Path, file_okay=False),
    help="Codex home (default: CODEX_HOME or ~/.codex).",
)
def loop_check(loop_id: str, codex_home: Path | None) -> None:
    try:
        package = load_loop_package(loop_id)
        profiles = check_loop_installation(package, codex_home=codex_home)
    except LoopPackageError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"checked loop {package.id}: {len(profiles)} managed profiles")
    for profile in profiles:
        click.echo(
            f"{profile.role}\t{profile.runtime_profile}\t{profile.symlink_path}"
        )


@cli.command("launch", help="Launch a manifest-defined Codex loop in one tmux window.")
@click.argument(
    "project_dir",
    type=click.Path(path_type=Path, file_okay=False),
)
@click.option("--loop", "loop_id", default=DEFAULT_LOOP_ID, show_default=True)
@click.option("--session", default="default", show_default=True, help="Existing tmux session.")
@click.option("--window-name", help="tmux window name (default: crew-<project>).")
@click.option("--json", "json_output", is_flag=True)
def launch(
    project_dir: Path,
    loop_id: str,
    session: str,
    window_name: str | None,
    json_output: bool,
) -> None:
    try:
        result = launch_crew(
            project_dir,
            loop_id=loop_id,
            session=session,
            window_name=window_name,
        )
    except LaunchError as error:
        raise click.ClickException(str(error)) from error
    if json_output:
        click.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_launch(result)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = cli.main(
            args=None if argv is None else list(argv),
            prog_name="codex-crew",
            standalone_mode=False,
        )
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except click.exceptions.Exit as error:
        return error.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    return int(result or 0)


def _print_latest(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    click.echo("ASOF\tSESSION\tTURN\tMODEL\tTOTAL\tGOAL\tOBJECTIVE")
    for row in rows:
        asof = datetime.fromtimestamp(row["asof_at"]).astimezone().isoformat(timespec="seconds")
        total = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
        click.echo(
            "\t".join(
                (
                    asof,
                    row["session_id"],
                    row["turn_id"],
                    row["model"] or "-",
                    str(total),
                    row["goal_status"] or "-",
                    (row["goal_objective_excerpt"] or "-").replace("\t", " ").replace("\n", " "),
                )
            )
        )


def _print_launch(result: CrewLaunch) -> None:
    click.echo(f"loop: {result.loop_id} ({result.layout})")
    click.echo(f"window: {result.session}:{result.window_index} ({result.window_name})")
    click.echo(f"project: {result.project_dir}")
    for pane in result.panes:
        click.echo(
            f"{pane.role}: {pane.pane_id} "
            f"(profile={pane.runtime_profile}, model={pane.model}, "
            f"reasoning_effort={pane.reasoning_effort})"
        )
    click.echo(f"open: tmux select-window -t {result.session}:{result.window_index}")


def _hook_config(database_path: Path) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    executable = project_root / "bin" / "codex-crew"
    command_parts = [str(executable)]
    if database_path != default_database_path():
        command_parts.extend(["--db", str(database_path)])
    command_parts.extend(["hook", "stop"])
    command = " ".join(shlex.quote(part) for part in command_parts)
    return {
        "description": "Persist Codex Stop snapshots with codex-crew.",
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 10,
                            "statusMessage": "Recording Codex turn",
                        }
                    ]
                }
            ]
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
