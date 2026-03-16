import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import fastar
import httpx
import typer
from dotenv import load_dotenv

load_dotenv()

FAST_COV_API_URL = "https://coverage-0cc8740f.fastapicloud.dev/coverage/upload-tar/"


def main(
    *,
    path: Path = typer.Argument(..., help="Path to the htmlcov directory to upload"),
    api_key: Annotated[
        str, typer.Option(envvar="FAST_COV_API_KEY", help="API key for authentication")
    ],
    repo_owner: Annotated[
        str, typer.Option(envvar="FAST_COV_REPO_OWNER", help="GitHub repository owner")
    ],
    repo_name: Annotated[
        str, typer.Option(envvar="FAST_COV_REPO_NAME", help="GitHub repository name")
    ],
    commit_sha: Annotated[
        str, typer.Option(envvar="FAST_COV_COMMIT_SHA", help="Git commit SHA")
    ],
    invalidate_cache: Annotated[
        bool,
        typer.Option(
            envvar="FAST_COV_INVALIDATE_CACHE",
            help="Whether to invalidate cache for the uploaded report",
        ),
    ] = False,
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
    timeout: int = typer.Option(120, help="HTTP request timeout in seconds"),
) -> None:
    """Upload an HTML coverage report directory as a tar.gz archive."""
    if not path.is_dir():
        typer.echo(f"Error: {path} is not a directory", err=True)
        raise typer.Exit(1)

    with TemporaryDirectory() as tmp_path:
        tar_file = Path(tmp_path) / "htmlcov.tar.gz"
        with fastar.open(tar_file, "w:gz") as archive:
            for file_path in path.rglob("*"):
                archive.append(file_path)

        client = httpx.Client(timeout=timeout)

        resp = client.post(
            FAST_COV_API_URL,
            params={
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "invalidate_cache": str(invalidate_cache).lower(),
            },
            files={
                "file_archive": (
                    "htmlcov.tar.gz",
                    open(tar_file, "rb"),
                    "application/gzip",
                )
            },
            headers={"token": api_key},
        )

    if resp.status_code != 200:
        typer.echo(f"Upload failed with status code: {resp.status_code}", err=True)
        typer.echo(f"Response content: {resp.text}", err=True)
        raise typer.Exit(1)

    resp_json = resp.json()
    typer.echo(f"Upload response: {resp_json}")

    coverage_val = resp_json.get("coverage")
    if coverage_val and float(coverage_val) >= coverage_threshold:
        status_state = "success"
    else:
        status_state = "failure"
    status_url = (
        f"https://api.github.com/repos/{repo_owner}/{repo_name}/statuses/{commit_sha}"
    )
    status_resp = httpx.post(
        status_url,
        json={
            "state": status_state,
            "description": f"Coverage {resp_json['coverage'] or '??'}%",
            "target_url": resp_json["url"],
            "context": "fast-coverage",
        },
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=timeout,
    )

    if status_resp.status_code >= 300:
        typer.echo(f"Failed to set commit status: {status_resp.status_code}", err=True)
        typer.echo(f"Response: {status_resp.text}", err=True)
        raise typer.Exit(1)

    typer.echo("Commit status set successfully")


if __name__ == "__main__":
    typer.run(main)
