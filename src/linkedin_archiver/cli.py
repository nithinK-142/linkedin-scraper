"""Command-line interface for the LinkedIn Saved Posts Archiver."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import logging

import typer

from linkedin_archiver import stages
from linkedin_archiver.settings import (
    archive_dir,
    default_saved_posts_file,
    setup_logging,
    load_config_file,
    config_path_value,
)

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
    runtime_config,
):
    try:
        return stages.resolve_target(
            browser=browser if browser is not None else runtime_config.browser,
            browser_path=browser_path if browser_path is not None else runtime_config.browser_path,
            user_data_dir=user_data_dir if user_data_dir is not None else runtime_config.user_data_dir,
            profile=profile if profile is not None else runtime_config.profile,
            cdp_port=cdp_port if cdp_port is not None else runtime_config.cdp_port,
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


def limit_option():
    return typer.Option(
        None,
        "--limit",
        "--number",
        "-n",
        min=1,
        help="Maximum number of posts to scrape/download. Default: all.",
    )


def sleep_option():
    return typer.Option(
        None,
        "--sleep",
        help="Seconds to wait between LinkedIn page/post operations. Use N or MIN-MAX.",
    )


def verbose_option():
    return typer.Option(False, "--verbose", "-v", help="Show debug logs on the console and in the log file.")


def _configure_logger(command: str, verbose: bool, config) -> object:
    level = logging.DEBUG if (verbose or config.verbose) else logging.INFO
    return setup_logging(command, level=level)


def _runtime_config():
    try:
        return load_config_file()
    except Exception as exc:
        typer.echo(f"Error loading config.toml: {exc}", err=True)
        raise typer.Exit(1)


def _path(value: Optional[str]):
    return config_path_value(value)


@app.command("profiles")
def profiles(
    browser: Optional[str] = typer.Option(None, "--browser", help="brave, chrome, or chromium."),
    user_data_dir: Optional[str] = typer.Option(None, "--user-data-dir", help="Browser user-data directory."),
    verbose: bool = verbose_option(),
):
    config = _runtime_config()
    browser = browser if browser is not None else config.browser
    user_data_dir = user_data_dir if user_data_dir is not None else config.user_data_dir
    user_data_dir = str(Path(user_data_dir).expanduser()) if user_data_dir is not None else None
    raise typer.Exit(stages.list_profiles(browser=browser, user_data_dir=user_data_dir, logger=_configure_logger("profiles", verbose, config)))


@app.command("collect")
def collect(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help=f"Output JSON. Default: {default_saved_posts_file()}"),
    limit: Optional[int] = limit_option(),
    sleep: Optional[str] = sleep_option(),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
    verbose: bool = verbose_option(),
):
    config = _runtime_config()
    browser_path = str(Path(browser_path).expanduser()) if browser_path is not None else None
    user_data_dir = str(Path(user_data_dir).expanduser()) if user_data_dir is not None else None
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port, config)
    output = output or _path(config.collect_output)
    limit = limit if limit is not None else config.limit
    sleep = sleep if sleep is not None else config.sleep
    raise typer.Exit(stages.collect_saved_posts(target, output=output, limit=limit, sleep=sleep, logger=_configure_logger("collect", verbose, config)))


@app.command("archive")
def archive(
    input_file: Optional[Path] = typer.Option(None, "--input", "-i", help=f"Input JSON. Default: {default_saved_posts_file()}"),
    limit: Optional[int] = limit_option(),
    sleep: Optional[str] = sleep_option(),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help=f"Archive directory. Default: {archive_dir()}"),
    resume: bool = typer.Option(False, "--resume", help="Accepted for compatibility; resume is always enabled."),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
    verbose: bool = verbose_option(),
):
    del resume
    config = _runtime_config()
    browser_path = str(Path(browser_path).expanduser()) if browser_path is not None else None
    user_data_dir = str(Path(user_data_dir).expanduser()) if user_data_dir is not None else None
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port, config)
    input_file = input_file or _path(config.archive_input)
    output_dir = output_dir or _path(config.archive_output)
    limit = limit if limit is not None else config.limit
    sleep = sleep if sleep is not None else config.sleep
    raise typer.Exit(stages.archive_posts(target, input_file=input_file, output_dir=output_dir, limit=limit, sleep=sleep, logger=_configure_logger("archive", verbose, config)))


@app.command("recover")
def recover(
    url: list[str] = typer.Option([], "--url", help="Post URL. Repeat for multiple posts."),
    limit: Optional[int] = limit_option(),
    sleep: Optional[str] = sleep_option(),
    input_file: Optional[Path] = typer.Option(None, "--input", "-i", help="JSON list of post URLs."),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Recovery output root."),
    playback_timeout: Optional[int] = typer.Option(None, "--playback-timeout", min=1, help="Max video playback wait in seconds."),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
    verbose: bool = verbose_option(),
):
    config = _runtime_config()
    browser_path = str(Path(browser_path).expanduser()) if browser_path is not None else None
    user_data_dir = str(Path(user_data_dir).expanduser()) if user_data_dir is not None else None
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port, config)
    urls = tuple(url) if url else config.recover_urls
    input_file = input_file or _path(config.recover_input)
    output_dir = output_dir or _path(config.recover_output)
    playback_timeout = playback_timeout if playback_timeout is not None else config.playback_timeout
    limit = limit if limit is not None else config.limit
    sleep = sleep if sleep is not None else config.sleep
    raise typer.Exit(
        stages.recover_media(
            target,
            urls=urls,
            input_file=input_file,
            output_dir=output_dir,
            playback_timeout=playback_timeout,
            limit=limit,
            sleep=sleep,
            logger=_configure_logger("recover", verbose, config),
        )
    )


@app.command("run")
def run(
    skip_recover: Optional[bool] = typer.Option(None, "--skip-recover/--no-skip-recover", help="Stop after archive."),
    limit: Optional[int] = limit_option(),
    sleep: Optional[str] = sleep_option(),
    browser: Optional[str] = browser_options()[0],
    browser_path: Optional[str] = browser_options()[1],
    user_data_dir: Optional[str] = browser_options()[2],
    profile: Optional[str] = browser_options()[3],
    cdp_port: Optional[int] = browser_options()[4],
    verbose: bool = verbose_option(),
):
    config = _runtime_config()
    browser_path = str(Path(browser_path).expanduser()) if browser_path is not None else None
    user_data_dir = str(Path(user_data_dir).expanduser()) if user_data_dir is not None else None
    target = _target(browser, browser_path, user_data_dir, profile, cdp_port, config)
    limit = limit if limit is not None else config.limit
    sleep = sleep if sleep is not None else config.sleep
    skip_recover = skip_recover if skip_recover is not None else config.skip_recover
    raise typer.Exit(stages.run_all(target, limit=limit, sleep=sleep, skip_recover=skip_recover, logger=_configure_logger("run", verbose, config)))


@app.command("status")
def status():
    raise typer.Exit(stages.show_status())


def main(argv: list[str] | None = None) -> None:
    app(args=argv, prog_name="linkedin-scraper")


if __name__ == "__main__":
    main()
