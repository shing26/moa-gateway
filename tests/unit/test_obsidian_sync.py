from __future__ import annotations

import pathlib

import pytest

from app.knowledge import KnowledgeBase
from app.obsidian_sync import ObsidianVaultSync
from app.vectordb import VectorDBClient


def _make_vault(tmp_path: pathlib.Path) -> pathlib.Path:
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "a.md").write_text("alpha beta", encoding="utf-8")
    (vault / "index.md").write_text("hello world", encoding="utf-8")
    return vault


@pytest.mark.asyncio
async def test_obsidian_sync_adds_with_stable_ids(tmp_path: pathlib.Path) -> None:
    vault = _make_vault(tmp_path)
    kb = KnowledgeBase(VectorDBClient())
    sync = ObsidianVaultSync(vault_path=str(vault), knowledge_base=kb)

    assert await sync.sync_once() == 2
    docs = await kb.list_docs()
    assert len(docs) == 2
    assert all(d["id"].startswith("obs:") for d in docs)
    assert await sync.sync_once() == 0


@pytest.mark.asyncio
async def test_obsidian_sync_overwrites_and_deletes(tmp_path: pathlib.Path) -> None:
    vault = _make_vault(tmp_path)
    kb = KnowledgeBase(VectorDBClient())
    sync = ObsidianVaultSync(vault_path=str(vault), knowledge_base=kb)
    await sync.sync_once()

    target = vault / "notes" / "a.md"
    doc_id = sync._doc_id("notes/a.md")
    target.write_text("alpha beta gamma delta epsilon", encoding="utf-8")
    await sync.sync_once()

    docs = await kb.list_docs()
    assert len(docs) == 2
    doc = await kb.get_doc(doc_id)
    assert doc is not None
    assert len(doc["chunks"]) == 1

    target.unlink()
    await sync.sync_once()
    docs = await kb.list_docs()
    assert len(docs) == 1
    assert await kb.get_doc(doc_id) is None


@pytest.mark.asyncio
async def test_obsidian_sync_subfolder(tmp_path: pathlib.Path) -> None:
    vault = _make_vault(tmp_path)
    kb = KnowledgeBase(VectorDBClient())
    sync = ObsidianVaultSync(vault_path=str(vault), subfolder="notes", knowledge_base=kb)

    await sync.sync_once()
    docs = await kb.list_docs()
    assert len(docs) == 1
    assert docs[0]["title"] == "a.md"


@pytest.mark.asyncio
async def test_obsidian_sync_disabled_when_vault_missing(tmp_path: pathlib.Path) -> None:
    kb = KnowledgeBase(VectorDBClient())
    sync = ObsidianVaultSync(vault_path=str(tmp_path / "missing"), knowledge_base=kb)

    assert sync.enabled is False
    assert await sync.sync_once() == 0
