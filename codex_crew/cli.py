"""Click command-line interface for codex-crew."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shlex
from typing import Any, Sequence

import click

from codex_crew.app_server import (
    DEFAULT_APP_SERVER_ENDPOINT,
    AppServerError,
    check_app_server,
)
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
from codex_crew.crew_runtime import (
    CrewCommandResult,
    CrewRuntimeError,
    crew_final,
    crew_goal_clear,
    crew_goal_get,
    crew_goal_set,
    crew_send,
    crew_status,
    crew_steer,
    crew_wait,
)
from codex_crew.storage import (
    aggregate_latest,
    default_database_path,
    initialize_database,
    latest_final,
    latest_snapshots,
)
from codex_crew.startup import StartupError, up_crew


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
    code = run_stop_hook(
        database_path=context.database_path,
    )
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


@cli.group("app-server", help="Check Codex App Server connectivity.")
def app_server_group() -> None:
    pass


@app_server_group.command("check", help="Check a Unix app-server WebSocket endpoint.")
@click.argument("endpoint", default=DEFAULT_APP_SERVER_ENDPOINT)
def app_server_check(endpoint: str) -> None:
    try:
        result = check_app_server(endpoint)
    except AppServerError as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        f"ready\tendpoint={result.endpoint}\tsocket={result.socket_path}"
    )


@cli.group("crew", help="Control one exact App Server native thread.")
def crew_group() -> None:
    pass


def _native_thread_options(function):
    function = click.option(
        "--thread-id", required=True, help="Exact App Server native thread id."
    )(function)
    return click.option(
        "--endpoint",
        default=DEFAULT_APP_SERVER_ENDPOINT,
        show_default=True,
        help="App Server Unix WebSocket endpoint.",
    )(function)


@crew_group.command("status", help="Read authoritative native thread status.")
@_native_thread_options
@click.option("--json", "json_output", is_flag=True)
def crew_status_command(
    endpoint: str,
    thread_id: str,
    json_output: bool,
) -> None:
    try:
        result = crew_status(endpoint, thread_id=thread_id)
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


def _message_options(function):
    function = click.option(
        "--message-file",
        type=click.Path(path_type=Path, dir_okay=False),
        help="Read the complete UTF-8 message from one file.",
    )(function)
    return click.option(
        "--message",
        help="Complete message text, or - to read the complete message from stdin.",
    )(function)


@crew_group.command("send", help="Start one user turn on a native thread.")
@_native_thread_options
@_message_options
@click.option("--json", "json_output", is_flag=True)
def crew_send_command(
    endpoint: str,
    thread_id: str,
    message: str | None,
    message_file: Path | None,
    json_output: bool,
) -> None:
    text = _read_message(message, message_file)
    try:
        result = crew_send(
            endpoint,
            thread_id=thread_id,
            message=text,
        )
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


@crew_group.command("steer", help="Append input to one exact active native turn.")
@_native_thread_options
@click.option("--expected-turn-id", required=True)
@_message_options
@click.option("--json", "json_output", is_flag=True)
def crew_steer_command(
    endpoint: str,
    thread_id: str,
    expected_turn_id: str,
    message: str | None,
    message_file: Path | None,
    json_output: bool,
) -> None:
    text = _read_message(message, message_file)
    try:
        result = crew_steer(
            endpoint,
            thread_id=thread_id,
            expected_turn_id=expected_turn_id,
            message=text,
        )
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


@crew_group.command("wait", help="Wait for one exact native turn to finish.")
@_native_thread_options
@click.option("--turn-id", required=True)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=click.FloatRange(min=0.001),
    required=True,
)
@click.option("--json", "json_output", is_flag=True)
def crew_wait_command(
    endpoint: str,
    thread_id: str,
    turn_id: str,
    timeout_seconds: float,
    json_output: bool,
) -> None:
    try:
        result = crew_wait(
            endpoint,
            thread_id=thread_id,
            turn_id=turn_id,
            timeout_seconds=timeout_seconds,
        )
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


@crew_group.command("final", help="Read one authoritative native final item.")
@_native_thread_options
@click.option("--turn-id")
@click.option("--json", "json_output", is_flag=True)
def crew_final_command(
    endpoint: str,
    thread_id: str,
    turn_id: str | None,
    json_output: bool,
) -> None:
    try:
        result = crew_final(
            endpoint,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


@crew_group.group("goal", help="Manage the app-server-native thread goal.")
def crew_goal_group() -> None:
    pass


@crew_goal_group.command("get", help="Read the native thread goal.")
@_native_thread_options
@click.option("--json", "json_output", is_flag=True)
def crew_goal_get_command(
    endpoint: str, thread_id: str, json_output: bool
) -> None:
    try:
        result = crew_goal_get(endpoint, thread_id=thread_id)
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


@crew_goal_group.command("set", help="Set an active native thread goal.")
@_native_thread_options
@click.option("--objective", required=True)
@click.option("--token-budget", type=click.IntRange(min=1))
@click.option("--json", "json_output", is_flag=True)
def crew_goal_set_command(
    endpoint: str,
    thread_id: str,
    objective: str,
    token_budget: int | None,
    json_output: bool,
) -> None:
    try:
        result = crew_goal_set(
            endpoint,
            thread_id=thread_id,
            objective=objective,
            token_budget=token_budget,
        )
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


@crew_goal_group.command("clear", help="Clear the native thread goal.")
@_native_thread_options
@click.option("--json", "json_output", is_flag=True)
def crew_goal_clear_command(
    endpoint: str, thread_id: str, json_output: bool
) -> None:
    try:
        result = crew_goal_clear(endpoint, thread_id=thread_id)
    except CrewRuntimeError as error:
        raise click.ClickException(str(error)) from error
    _emit_crew_result(result, json_output=json_output)


@cli.command("up", help="Prepare and launch a repository-owned Codex crew.")
@click.argument(
    "project_dir",
    required=False,
    default=".",
    type=click.Path(path_type=Path, file_okay=False),
)
@click.option("--loop", "loop_id", default=DEFAULT_LOOP_ID, show_default=True)
@click.option("--session", default="default", show_default=True)
@click.option("--window-name", help="tmux window name (default: crew-<project>).")
@click.option("--json", "json_output", is_flag=True)
def up(
    project_dir: Path,
    loop_id: str,
    session: str,
    window_name: str | None,
    json_output: bool,
) -> None:
    """Run profile, tmux, App Server, and native launch gates in order."""

    try:
        result = up_crew(
            project_dir,
            loop_id=loop_id,
            session=session,
            window_name=window_name,
        )
    except StartupError as error:
        raise click.ClickException(str(error)) from error
    if json_output:
        click.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_launch(result)


@cli.command("launch", help="Launch a manifest-defined Codex loop in one tmux window.")
@click.argument(
    "project_dir",
    type=click.Path(path_type=Path, file_okay=False),
)
@click.option("--loop", "loop_id", default=DEFAULT_LOOP_ID, show_default=True)
@click.option(
    "--app-server",
    "app_server_endpoint",
    metavar="ENDPOINT",
    default=DEFAULT_APP_SERVER_ENDPOINT,
    show_default=True,
    help="App Server endpoint used by every launched TUI (unix:// or unix://PATH).",
)
@click.option(
    "--session", default="default", show_default=True, help="Existing tmux session."
)
@click.option("--window-name", help="tmux window name (default: crew-<project>).")
@click.option("--json", "json_output", is_flag=True)
def launch(
    project_dir: Path,
    loop_id: str,
    app_server_endpoint: str,
    session: str,
    window_name: str | None,
    json_output: bool,
) -> None:
    try:
        result = launch_crew(
            project_dir,
            loop_id=loop_id,
            app_server_endpoint=app_server_endpoint,
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
    click.echo(f"app-server: {result.app_server_endpoint}")
    click.echo(f"window: {result.session}:{result.window_index} ({result.window_name})")
    click.echo(f"project: {result.project_dir}")
    for pane in result.panes:
        detail = (
            f"{pane.role}: {pane.pane_id} "
            f"(profile={pane.runtime_profile}, model={pane.model}, "
            f"reasoning_effort={pane.reasoning_effort}, "
            f"service_tier={pane.service_tier})"
        )
        detail += (
            f" thread={pane.thread_id} "
            f"bootstrap_turn={pane.bootstrap_turn_id}"
        )
        click.echo(detail)
    click.echo(f"open: tmux select-window -t {result.session}:{result.window_index}")


def _read_message(message: str | None, message_file: Path | None) -> str:
    if (message is None) == (message_file is None):
        raise click.UsageError("provide exactly one of --message-file or --message")
    if message_file is not None:
        try:
            return message_file.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as error:
            raise click.ClickException(
                f"could not read UTF-8 message file {message_file}: {error}"
            ) from error
    if message == "-":
        return click.get_text_stream("stdin").read()
    return message or ""


def _emit_crew_result(result: CrewCommandResult, *, json_output: bool) -> None:
    if json_output:
        click.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return
    turn = result.turn_id or "null"
    click.echo(
        f"{result.command}\tendpoint={result.endpoint}\t"
        f"thread_id={result.thread_id}\tturn_id={turn}\tstatus={result.status}"
    )
    if result.command == "final" and isinstance(result.data.get("final_text"), str):
        click.echo(result.data["final_text"])
    if result.command.startswith("goal."):
        click.echo(json.dumps(result.data.get("goal"), ensure_ascii=False))


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
