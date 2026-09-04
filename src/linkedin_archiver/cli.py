"""Command-line interface for the LinkedIn Saved Posts Archiver."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from linkedin_archiver import stages
from linkedin_archiver.settings import archive_dir, default_saved_posts_file, setup_logging, load_config_file

app = typer.Typer(
    no_args_is_help=True,
    add_completion=True,
    help="Archive LinkedIn saved posts and recover media through an authenticated Chromium session.",
)


def _target(
    browser: Optional[str],
    browser_path: Optional[str],
    user_data_dir: Optional[str],
    profile: Optional[str],
    cdp_port: Optional[int],
):
    runtime_config = load_config_file()
    try:
        return stages.resolve_target(
            browser=browser,
            browser_path=browser_path,
            user_data_dir=user_data_dir,
            profile=profile,
            cdp_port=cdp_port,
            runtime_config=runtime_config,
        )
    except SystemExit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


def browser_options():
    return (
        typer.Option(None, "--browser", help="brave, chrome, chromium, or detected list number."),
        typer.Option(None, "--browser-path", help="Browser executable path."),
        typer.Option(None, "--user-data-dir", help="Browser user-data directory."),
        typer.Option(None, "--profile", help="Profile directory, display name, or detected list number."),
        typer.Option(None, "--cdp-port", min=1, max=65535, help="CDP port. Default: 9222."),
    )


@app.command("profiles")
def profiles(
    browser: Optional[str] = typer.Option(None, "--browser", help="brave, chrome, or chromium."),
    user_data_dir: Optional[str] = typer.Option(None, "--user-data-dir", help="Browser user-data directory."),
):
    raise typer.Exit(stages.list_profiles(browser=browser, user_data_dir=user_data_dir, logger=setup_logging("profiles")))


@app.command("collect")
def collect(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help=f"Output JSON. Default: {default_saved_posts_file()}"),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
):
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port)
    raise typer.Exit(stages.collect_saved_posts(target, output=output, logger=setup_logging("collect")))


@app.command("archive")
def archive(
    input_file: Optional[Path] = typer.Option(None, "--input", "-i", help=f"Input JSON. Default: {default_saved_posts_file()}"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help=f"Archive directory. Default: {archive_dir()}"),
    resume: bool = typer.Option(False, "--resume", help="Accepted for compatibility; resume is always enabled."),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
):
    del resume
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port)
    raise typer.Exit(stages.archive_posts(target, input_file=input_file, output_dir=output_dir, logger=setup_logging("archive")))


@app.command("recover")
def recover(
    url: list[str] = typer.Option([], "--url", help="Post URL. Repeat for multiple posts."),
    input_file: Optional[Path] = typer.Option(None, "--input", "-i", help="JSON list of post URLs."),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Recovery output root."),
    playback_timeout: Optional[int] = typer.Option(None, "--playback-timeout", min=1, help="Max video playback wait in seconds."),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
):
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port)
    raise typer.Exit(
        stages.recover_media(
            target,
            urls=tuple(url),
            input_file=input_file,
            output_dir=output_dir,
            playback_timeout=playback_timeout,
            logger=setup_logging("recover"),
        )
    )


@app.command("run")
def run(
    skip_recover: bool = typer.Option(False, "--skip-recover", "--skip-video-capture", help="Stop after archive."),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
):
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port)
    raise typer.Exit(stages.run_all(target, skip_recover=skip_recover, logger=setup_logging("run")))


@app.command("status")
def status():
    raise typer.Exit(stages.show_status())


def main(argv: list[str] | None = None) -> None:
    app(args=argv, prog_name="linkedin-archiver")


if __name__ == "__main__":
    main()
