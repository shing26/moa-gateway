"""D5 判据的真栈验证：webhook 投递 -> 任务行 + 队列消息（需要真实 Redis + PostgreSQL）。

为什么必须是真栈：D5 把 webhook 从"同步跑完 5 个 agent"改成"落任务行 + XADD +
返回 202"。这条改动的**全部失败模式都是静默的**——

- 任务行没落库：worker 收到消息后 ``TaskNotFound``，ack 掉，表现为"跑了但没结果"，
  队列日志里一行错误都没有。
- 入队的 repo/pr/sha 与任务行的身份三元组对不上：同上。
- 重投真的多建了一行任务：两次审查各自认领自己那行，PR 上会收到两条评论。

单测里那套 RecordingQueue 只验了路由自己的判断，XADD 字段与 PG 写入都不是真的。
所以这里两个服务都接真的；服务没起就 skip，不伪造通过：

    docker compose -f docker-compose.dev.yml up -d redis postgres
    uv run pytest tests/integration/test_webhook_dispatch_e2e.py -q
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

import pytest

psycopg = pytest.importorskip("psycopg")

from app.config import settings
from app.main import app
from apps.code_review_pipeline.routing import github_review_route as route
from apps.code_review_pipeline.routing.github_signature import sign_payload
from apps.code_review_pipeline.storage.review_store import PostgresReviewStore, build_review_store
from tests.support import app_client

DSN = "postgresql://gateway:gateway@localhost:5433/gateway"
REDIS_URL = "redis://localhost:6380/0"
SECRET = "integration-webhook-secret"


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """真 Redis + 真 PG，并让路由的单例指向它们。"""
    monkeypatch.setattr(settings, "vector_db_dsn", DSN)
    monkeypatch.setattr(settings, "redis_url", REDIS_URL)
    monkeypatch.setattr(settings, "github_token", "integration-token")
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "code_review_auto_migrate", True)

    store = build_review_store()
    if not isinstance(store, PostgresReviewStore):
        monkeypatch.setattr(settings, "vector_db_dsn", "")
        pytest.skip("拿到的是内存 store，说明 PostgreSQL DSN 没生效")

    redis_mod = pytest.importorskip("redis")
    # 断言侧一律用**同步**客户端。
    #
    # 异步 redis 客户端会把连接绑在第一次使用它的那个 event loop 上，而这里有
    # 两个 loop：TestClient 自带的（跑 webhook）与 pytest-asyncio 的（跑断言）。
    # 用 asyncio.run() 去 ping 会让连接落在随即被关掉的临时 loop 上，之后
    # TestClient 里第一次 enqueue 就报 "Event loop is closed"——而报错点在
    # teardown，离真正的原因十万八千里。
    try:
        sync_redis = redis_mod.Redis.from_url(REDIS_URL, decode_responses=False)
        sync_redis.ping()
    except Exception as exc:  # noqa: BLE001 - Redis 不在不是测试失败
        pytest.skip(f"Redis 不可用（{type(exc).__name__}），跳过集成用例")

    tag = uuid.uuid4().hex[:8]
    stream = f"moa:test:{tag}"
    group = f"grp:{tag}"
    # 身份三元组也必须每轮唯一。身份固定的话，第一轮跑完留下的行（可能已是
    # running/done）会让第二轮的"首次投递"直接命中已存在状态，于是第一条
    # assert acquire_task 就失败——而且失败原因离真正的代码问题很远。
    repo = f"acme-{tag}/widgets"
    from apps.code_review_pipeline.task_queue import TaskQueue

    # 路由用异步客户端（它只在 TestClient 的 loop 里被用到），断言用同步客户端。
    route_queue = TaskQueue(
        redis_mod.asyncio.from_url(REDIS_URL, decode_responses=False),
        stream=stream,
        group=group,
    )
    monkeypatch.setattr(route, "_review_store", store)
    monkeypatch.setattr(route, "_task_queue", route_queue)
    try:
        yield {"store": store, "redis": sync_redis, "stream": stream, "repo": repo}
    finally:
        try:
            sync_redis.delete(stream)
            sync_redis.close()
        except Exception:  # noqa: BLE001 - 清理失败不该让用例变成失败
            pass
        with psycopg.connect(DSN, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM code_review_prs WHERE repo=%s", (repo,))
            conn.commit()


def _payload(repo: str, pr: int, sha: str, action: str = "opened") -> bytes:
    return json.dumps(
        {
            "action": action,
            "pull_request": {
                "number": pr,
                "title": f"integration #{pr}",
                "user": {"login": "tester"},
                "head": {"sha": sha},
                "base": {"sha": "0" * 40},
                "html_url": f"https://github.com/{repo}/pull/{pr}",
                "diff_url": f"https://github.com/{repo}/pull/{pr}.diff",
                "labels": [{"name": "integration"}],
                "requested_reviewers": [{"login": "moa-bot"}],
            },
            "repository": {"full_name": repo},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _post(client: Any, raw: bytes) -> Any:
    return client.post(
        "/webhook/github/review",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign_payload(raw, SECRET)},
    )


def _row_count(repo: str, pr: int, sha: str) -> int:
    """**另开一条连接**数行数，而不是复用 store 的连接。

    两层理由：一是 store 的连接是惰性建立的，直接摸 ``store._conn`` 在它还没建
    出来时就拿到 None；二是集成用例的意义正是"数据真的提交了、别的会话看得见"，
    复用同一条连接会把这个性质一起验掉。
    """
    with psycopg.connect(DSN, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM code_review_prs "
                "WHERE repo=%s AND pr_number=%s AND head_sha=%s",
                (repo, pr, sha),
            )
            return int(cur.fetchone()[0])


def _stream_rows(stack: dict[str, Any]) -> list[dict[str, str]]:
    """读流内容（同步客户端）。键与值都要解码——真 Redis 两种都是 bytes。"""
    rows = []
    for _mid, fields in stack["redis"].xrange(stack["stream"]):
        rows.append(
            {
                (k.decode() if isinstance(k, bytes) else str(k)): (
                    v.decode() if isinstance(v, bytes) else str(v)
                )
                for k, v in fields.items()
            }
        )
    return rows


@pytest.mark.postgres
@pytest.mark.redis
def test_first_delivery_creates_task_row_and_streams_one_message(
    stack: dict[str, Any],
) -> None:
    repo = stack["repo"]
    sha = "1111" + "0" * 36
    raw = _payload(repo, 501, sha)
    with app_client(app) as client:
        response = _post(client, raw)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["task_key"] == f"{repo}#501@{sha}"

    # 任务行真的落库了，且身份三元组可被 worker 反查。worker 认领就是按这三列查的，
    # 对不上时它会 TaskNotFound 然后**静默 ack**。
    assert _row_count(repo, 501, sha) == 1
    assert route._get_review_store().get_task_state((repo, 501, sha)) == "queued"

    entries = _stream_rows(stack)
    assert len(entries) == 1, entries
    assert entries[0]["repo"] == repo
    assert entries[0]["pr_number"] == "501"
    assert entries[0]["head_sha"] == sha
    assert entries[0]["task_key"] == f"{repo}#501@{sha}"


@pytest.mark.postgres
@pytest.mark.redis
def test_redelivery_keeps_one_task_row_and_still_streams(stack: dict[str, Any]) -> None:
    """重投：任务行不增，但仍会再投一条队列消息。

    仍然入队是刻意的（"入队无脑重复、幂等在消费端"）：查-写之间有窗口，靠入队前
    先查去重反而更容易漏。消费端的条件 UPDATE 认领会让第二条消息认领失败并 ack。
    """
    repo = stack["repo"]
    sha = "2222" + "0" * 36
    raw = _payload(repo, 502, sha)
    with app_client(app) as client:
        assert _post(client, raw).status_code == 202
        second = _post(client, raw)
    assert second.status_code == 200
    assert second.json()["status"] == "idempotent"
    assert _row_count(repo, 502, sha) == 1
    entries = _stream_rows(stack)
    assert len(entries) == 2, entries


@pytest.mark.postgres
@pytest.mark.redis
def test_consumer_side_dedup_makes_the_second_message_a_noop(
    stack: dict[str, Any],
) -> None:
    """两条消息 -> 只有一个能认领成功。这就是"无脑重投"成立的原因。"""
    repo = stack["repo"]
    sha = "3333" + "0" * 36
    raw = _payload(repo, 503, sha)
    with app_client(app) as client:
        _post(client, raw)
        _post(client, raw)

    store = stack["store"]
    identity = (repo, 503, sha)
    assert store.acquire_task(identity) is True, "第一条消息认领失败"
    assert store.acquire_task(identity) is False, "第二条消息不该认领成功"
    assert store.get_task_state(identity) == "running"

    # 两条消息携带的身份三元组完全相同——所以它们指向的是同一行任务，
    # 而那行任务只会被认领一次。
    entries = _stream_rows(stack)
    assert len(entries) == 2
    assert all(e["repo"] == repo and e["pr_number"] == "503" for e in entries)


@pytest.mark.postgres
@pytest.mark.redis
def test_non_reviewable_action_creates_no_task_row_and_no_message(
    stack: dict[str, Any],
) -> None:
    repo = stack["repo"]
    sha = "4444" + "0" * 36
    with app_client(app) as client:
        response = _post(client, _payload(repo, 504, sha, action="closed"))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert _row_count(repo, 504, sha) == 0
    assert _stream_rows(stack) == []
