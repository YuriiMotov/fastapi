import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import stamina
import typer
from aiobotocore.session import get_session
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer()


COV_PATTERNS = [
    re.compile(r'<span\s+class="pc_cov">\s*([\d.]+)%\s*</span>'),
    re.compile(r"<li><b>Coverage</b>:\s*([\d.]+)%</li>"),
]


def _get_coverage_info(cov_report_path: Path) -> float | None:
    cov_index = cov_report_path / "index.html"

    if not cov_index.exists():
        return None
    with open(cov_index) as f:
        html = f.read()
    for pattern in COV_PATTERNS:
        m = pattern.search(html)
        if m:
            return float(m.group(1))
    return None


async def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: dict | None = None,
    timeout: int = 60,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        async for attempt in stamina.retry_context(
            on=(httpx.TransportError, httpx.HTTPStatusError),
            attempts=3,
            wait_jitter=2.0,
        ):
            with attempt:
                resp = await client.request(method, url, headers=headers, json=json)
                if resp.status_code >= 500:
                    resp.raise_for_status()
    return resp


async def _upload_files(
    directory: Path,
    session: dict,
    concurrency: int,
) -> int:
    semaphore = asyncio.Semaphore(concurrency)
    files = [f for f in directory.rglob("*") if f.is_file()]

    async with get_session().create_client(
        "s3",
        region_name=session["region"],
        aws_access_key_id=session["access_key_id"],
        aws_secret_access_key=session["secret_access_key"],
        aws_session_token=session["session_token"],
    ) as s3:

        async def upload_one(file_path: Path):
            key = f"sites/{session['site_id']}/{file_path.relative_to(directory)}"
            async with semaphore:
                await s3.put_object(
                    Bucket=session["bucket"],
                    Key=key,
                    Body=file_path.read_bytes(),
                )

        async with asyncio.TaskGroup() as tg:
            for f in files:
                tg.create_task(upload_one(f))

    return len(files)


async def _main(
    directory: Path,
    api_url: str,
    api_key: str,
    concurrency: int,
    repo_owner: str,
    repo_name: str,
    commit_sha: str,
    coverage_threshold: float,
    gh_token: str,
    invalidate_cache: bool,
) -> None:
    start_time = datetime.now()
    typer.echo("Creating upload session...")
    resp = await _request(
        "POST",
        f"{api_url}/coverage/create-site/",
        headers={"token": api_key},
        timeout=120,
    )
    session = resp.json()
    typer.echo(f"Session created: site_id={session['site_id']}")

    typer.echo(f"Uploading files from {directory}...")
    count = await _upload_files(directory, session, concurrency)

    typer.echo(f"Uploaded {count} files in {datetime.now() - start_time}.")

    coverage_val = _get_coverage_info(directory)
    if coverage_val is None:
        typer.echo("Coverage info not found in the report. Skipping status update.")
        return

    typer.echo(f"Coverage value is {coverage_val}")
    typer.echo("Updating commit status...")

    status_state = "success" if coverage_val >= coverage_threshold else "failure"

    status_url = (
        f"https://api.github.com/repos/{repo_owner}/{repo_name}/statuses/{commit_sha}"
    )
    status_data = {
        "state": status_state,
        "description": f"Coverage {coverage_val}%",
        "target_url": f"{api_url}/coverage/{session['site_id']}/",
        "context": "fast-coverage",
    }
    status_resp = await _request(
        "POST",
        status_url,
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        },
        json=status_data,
    )

    if status_resp.status_code != 201:
        typer.echo(f"Failed to set commit status: {status_resp.status_code}", err=True)
        typer.echo(f"Response: {status_resp.text}", err=True)
        raise typer.Exit(1)

    typer.echo("Commit status set successfully")

    if not invalidate_cache:
        typer.echo("Skipping cache invalidation (invalidate_cache=False)")
        return

    # Clear badge cache
    resp = await _request(
        "POST",
        f"{api_url}/coverage/invalidate-cache/{repo_owner}/{repo_name}/",
        headers={"token": api_key},
    )
    if resp.status_code == 200:
        typer.echo("Cache invalidated successfully")
    else:
        typer.echo(f"Failed to invalidate cache: {resp.status_code}", err=True)
        typer.echo(f"Response: {resp.text}", err=True)

    # Purge github Camo cache for the badge
    badge_url_re = re.compile(
        r"<img\s[^>]*"
        + r'src="(https://camo\.githubusercontent\.com/[a-f0-9]+/[a-zA-Z0-9]+)"[^>]*'
        + rf'data-canonical-src="{re.escape(api_url)}[^"]*"'
    )

    readme_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/readme"
    readme_resp = await _request(
        "GET",
        readme_url,
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github.html+json",
        },
        timeout=30,
    )
    if readme_resp.status_code != 200:
        typer.echo(f"Failed to fetch README: {readme_resp.status_code}", err=True)
        return

    match = badge_url_re.search(readme_resp.text)
    if not match:
        typer.echo("Badge URL not found in README")
        return

    badge_url = match.group(1)
    typer.echo(f"Purging Camo cache for: {badge_url}")
    purge_resp = await _request("PURGE", badge_url, timeout=30)
    if purge_resp.status_code == 200:
        typer.echo("Camo cache purged successfully")
    else:
        typer.echo(f"Failed to purge Camo cache: {purge_resp.status_code}", err=True)


@app.command()
def upload(
    *,
    directory: Annotated[Path, typer.Argument(help="Directory to upload")],
    api_url: Annotated[
        str, typer.Option(envvar="FAST_COV_API_URL", help="Backend API base URL")
    ],
    api_key: Annotated[
        str, typer.Option(envvar="FAST_COV_API_KEY", help="API key for authentication")
    ],
    concurrency: Annotated[int, typer.Option(help="Max concurrent uploads")] = 50,
    repo_owner: Annotated[
        str, typer.Option(envvar="FAST_COV_REPO_OWNER", help="GitHub repository owner")
    ],
    repo_name: Annotated[
        str, typer.Option(envvar="FAST_COV_REPO_NAME", help="GitHub repository name")
    ],
    commit_sha: Annotated[
        str, typer.Option(envvar="FAST_COV_COMMIT_SHA", help="Git commit SHA")
    ],
    coverage_threshold: Annotated[
        float,
        typer.Option(
            envvar="FAST_COV_COVERAGE_THRESHOLD",
            help="Minimum coverage percentage to set success status",
        ),
    ] = 100.0,
    gh_token: Annotated[
        str,
        typer.Option(
            envvar="FAST_COV_GH_TOKEN", help="GitHub token for setting commit status"
        ),
    ],
    invalidate_cache: Annotated[
        bool,
        typer.Option(
            envvar="FAST_COV_INVALIDATE_CACHE",
            help="Whether to invalidate the cache (enabled for default branch)",
        ),
    ] = False,
) -> None:
    """Upload a directory to a temporary site."""
    if not directory.is_dir():
        typer.echo(f"Error: {directory} is not a directory", err=True)
        raise typer.Exit(1)

    asyncio.run(
        _main(
            directory=directory,
            api_url=api_url,
            api_key=api_key,
            concurrency=concurrency,
            repo_owner=repo_owner,
            repo_name=repo_name,
            commit_sha=commit_sha,
            coverage_threshold=coverage_threshold,
            gh_token=gh_token,
            invalidate_cache=invalidate_cache,
        )
    )


if __name__ == "__main__":
    app()
