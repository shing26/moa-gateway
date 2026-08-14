from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from apps.code_review_pipeline.rag.embeddings import generate_embeddings
from apps.code_review_pipeline.rag.knowledge_base import KnowledgeBase, KnowledgeDoc, build_knowledge_base
from apps.code_review_pipeline.routing.github_client import GitHubClient, GitHubRepo

logger = logging.getLogger("moa.code_review.rag.ingestion")

DEFAULT_MODULES = ("auth", "payment", "accounts", "billing")
MAX_DIFF_CHARS = 4000
MAX_DESCRIPTION_CHARS = 2000


@dataclass(frozen=True)
class HistoricalPR:
    repo: GitHubRepo
    pr_number: int
    title: str
    author: str
    merged_at: str
    html_url: str
    diff: str
    description: str
    modules: tuple[str, ...]
    approvers: tuple[str, ...]


def _parse_repo(repo: str) -> GitHubRepo:
    match = re.match(r"^(?P<owner>[^/]+)/(?P<name>[^/]+)$", repo.strip())
    if not match:
        raise ValueError(f"invalid repo format: {repo}")
    return GitHubRepo(owner=match.group("owner"), name=match.group("name"))


def _module_matches(filename: str, modules: tuple[str, ...]) -> bool:
    normalized = filename.lower()
    return any(module.lower() in normalized for module in modules)


def _filter_modules(files: list[dict[str, Any]], modules: tuple[str, ...]) -> tuple[str, ...]:
    matched: set[str] = set()
    for file in files:
        filename = str(file.get("filename", ""))
        for module in modules:
            if _module_matches(filename, (module,)):
                matched.add(module)
                break
    return tuple(sorted(matched))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


async def _fetch_merged_prs(
    client: GitHubClient,
    repo: GitHubRepo,
    since: dt.datetime,
    until: dt.datetime,
) -> list[dict[str, Any]]:
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f"repo:{repo.owner}/{repo.name} is:pr is:merged merged:{since_str}..{until_str}"
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = await client._client.get(
            "/search/issues",
            params={"q": query, "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        if not items:
            break
        results.extend(items)
        if len(items) < 100:
            break
        page += 1
    return results


async def _fetch_reviews(client: GitHubClient, repo: GitHubRepo, pr_number: int) -> list[dict[str, Any]]:
    resp = await client._client.get(
        f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}/reviews",
        params={"per_page": 100},
    )
    resp.raise_for_status()
    return list(resp.json())


async def _fetch_pr_files(client: GitHubClient, repo: GitHubRepo, pr_number: int) -> list[dict[str, Any]]:
    return await client.get_pr_files(repo, pr_number)


async def _fetch_pr(client: GitHubClient, repo: GitHubRepo, pr_number: int) -> dict[str, Any]:
    resp = await client._client.get(f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}")
    resp.raise_for_status()
    return dict(resp.json())


async def _load_historical_pr(
    client: GitHubClient,
    repo: GitHubRepo,
    pr_number: int,
    modules: tuple[str, ...],
    min_approvers: int = 2,
) -> HistoricalPR | None:
    pr = await _fetch_pr(client, repo, pr_number)
    files = await _fetch_pr_files(client, repo, pr_number)
    matched_modules = _filter_modules(files, modules)
    if not matched_modules:
        return None

    reviews = await _fetch_reviews(client, repo, pr_number)
    approvers = tuple(
        review.get("user", {}).get("login", "")
        for review in reviews
        if review.get("state", "").upper() == "APPROVED"
    )
    if len(set(approvers)) < min_approvers:
        return None

    diff_parts = []
    for file in files:
        patch = file.get("patch")
        if patch:
            diff_parts.append(f"### {file.get('filename', '')}\n{patch}")
    diff = "\n".join(diff_parts)

    description = str(pr.get("body") or "")
    if not description:
        description = pr.get("title", "")

    return HistoricalPR(
        repo=repo,
        pr_number=pr_number,
        title=str(pr.get("title", "")),
        author=str(pr.get("user", {}).get("login", "")),
        merged_at=str(pr.get("merged_at", "")),
        html_url=str(pr.get("html_url", "")),
        diff=diff,
        description=description,
        modules=matched_modules,
        approvers=approvers,
    )


def _chunk_pr(pr: HistoricalPR) -> list[KnowledgeDoc]:
    docs: list[KnowledgeDoc] = []

    title = pr.title.strip()
    desc = _truncate(pr.description.strip(), MAX_DESCRIPTION_CHARS)
    if title or desc:
        docs.append(
            KnowledgeDoc(
                source_type="historical_pr",
                source_id=f"pr-{pr.repo.owner}-{pr.repo.name}-{pr.pr_number}",
                title=title or f"PR #{pr.pr_number}",
                content=f"PR #{pr.pr_number}: {title}\n\n{desc}".strip(),
                metadata={
                    "repo": f"{pr.repo.owner}/{pr.repo.name}",
                    "pr_number": pr.pr_number,
                    "author": pr.author,
                    "merged_at": pr.merged_at,
                    "modules": pr.modules,
                    "approvers": pr.approvers,
                    "html_url": pr.html_url,
                    "chunk_type": "summary",
                },
            )
        )

    diff = pr.diff.strip()
    if diff:
        if len(diff) <= MAX_DIFF_CHARS:
            docs.append(
                KnowledgeDoc(
                    source_type="historical_pr",
                    source_id=f"pr-{pr.repo.owner}-{pr.repo.name}-{pr.pr_number}-diff",
                    title=f"{title} (diff)",
                    content=diff,
                    metadata={
                        "repo": f"{pr.repo.owner}/{pr.repo.name}",
                        "pr_number": pr.pr_number,
                        "modules": pr.modules,
                        "chunk_type": "diff",
                    },
                )
            )
        else:
            chunks = [
                diff[i : i + MAX_DIFF_CHARS]
                for i in range(0, len(diff), MAX_DIFF_CHARS)
            ]
            for idx, chunk in enumerate(chunks, start=1):
                docs.append(
                    KnowledgeDoc(
                        source_type="historical_pr",
                        source_id=f"pr-{pr.repo.owner}-{pr.repo.name}-{pr.pr_number}-diff-{idx}",
                        title=f"{title} (diff {idx}/{len(chunks)})",
                        content=chunk,
                        metadata={
                            "repo": f"{pr.repo.owner}/{pr.repo.name}",
                            "pr_number": pr.pr_number,
                            "modules": pr.modules,
                            "chunk_type": "diff",
                            "chunk_index": idx,
                            "chunk_total": len(chunks),
                        },
                    )
                )
    return docs


async def ingest_repo(
    *,
    repo: str,
    months: int = 3,
    min_approvers: int = 2,
    modules: tuple[str, ...] = DEFAULT_MODULES,
    limit: int | None = None,
    github_token: str | None = None,
) -> int:
    repo_obj = _parse_repo(repo)
    token = github_token or os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for historical PR ingestion")

    client = GitHubClient(token=token)
    try:
        await client._client.get("/rate_limit")
    except Exception as exc:
        raise RuntimeError(f"github auth failed: {exc}") from exc

    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=30 * months)

    logger.info("fetch merged PRs repo=%s since=%s", repo, since.date())
    raw_prs = await _fetch_merged_prs(client, repo_obj, since, now)
    logger.info("found %d merged PR candidates", len(raw_prs))

    if limit is not None:
        raw_prs = raw_prs[:limit]

    knowledge_base = build_knowledge_base()
    total_ingested = 0
    for idx, raw in enumerate(raw_prs, start=1):
        pr_number = int(raw.get("number", 0))
        logger.info("[%d/%d] processing PR #%d", idx, len(raw_prs), pr_number)
        try:
            pr = await _load_historical_pr(client, repo_obj, pr_number, modules, min_approvers=min_approvers)
        except Exception as exc:
            logger.warning("failed to load PR #%d: %s", pr_number, exc)
            continue
        if pr is None:
            logger.info("skip PR #%d: not in target modules or too few approvers", pr_number)
            continue

        docs = _chunk_pr(pr)
        if not docs:
            continue
        try:
            count = await knowledge_base.ingest_documents(docs)
        except Exception as exc:
            logger.error("failed to ingest PR #%d: %s", pr_number, exc)
            continue
        logger.info("ingested %d chunks from PR #%d", count, pr_number)
        total_ingested += count

    return total_ingested


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Ingest historical GitHub PRs into the RAG knowledge base")
    parser.add_argument("--repo", required=True, help="owner/repo, e.g. octocat/Hello-World")
    parser.add_argument("--months", type=int, default=3, help="lookback window in months")
    parser.add_argument("--min-approvers", type=int, default=2, help="minimum distinct approvers")
    parser.add_argument("--modules", nargs="*", default=list(DEFAULT_MODULES), help="module path keywords to match")
    parser.add_argument("--limit", type=int, help="max PRs to process")
    parser.add_argument("--github-token", help="GitHub token (default: GITHUB_TOKEN env)")
    args = parser.parse_args()

    total = asyncio.run(
        ingest_repo(
            repo=args.repo,
            months=args.months,
            min_approvers=args.min_approvers,
            modules=tuple(args.modules),
            limit=args.limit,
            github_token=args.github_token,
        )
    )
    print(f"ingested total chunks: {total}")


if __name__ == "__main__":
    main()
