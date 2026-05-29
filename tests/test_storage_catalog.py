"""Catalog storage — CRUD across three namespaces + uniqueness, isolation,
sync watermark roundtrip (ADR 041).

Runs across all three backends via the shared ``storage`` fixture in
conftest.py — ``InMemoryStorage``, ``SqliteProvider``, and
``PostgresProvider`` (skipped when ``MOVATE_PG_TEST_URL`` is unset).

Asserts the additive tables default-off (no rows until written), the
namespace ↔ tenant_id invariant holds at the storage layer, the read
join unions movate + caller's private + community, ratings recompute the
cached summary, and the sync watermark round-trips per source.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from movate.core.models import (
    CatalogEntry,
    CatalogSource,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_entry(
    *,
    slug: str = "demo",
    source: CatalogSource = CatalogSource.MOVATE,
    tenant_id: str | None = None,
    latest_version: str = "1.0.0",
    name: str | None = None,
    title: str | None = None,
    description: str = "An agent.",
    tags: list[str] | None = None,
    shape: str | None = "faq",
    recommended_for: str | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        slug=slug,
        source=source,
        tenant_id=tenant_id,
        latest_version=latest_version,
        name=name or slug,
        title=title or slug.replace("_", " ").title(),
        description=description,
        tags=tags or [],
        shape=shape,
        recommended_for=recommended_for,
    )


def _bytes(payload: str) -> tuple[bytes, str]:
    raw = payload.encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Default-off
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_default_off_no_rows(storage) -> None:
    assert await storage.list_catalog_entries("tenant-a") == []
    assert await storage.get_catalog_entry("anything", source=CatalogSource.MOVATE) is None
    assert await storage.get_catalog_sync_watermark(CatalogSource.MOVATE) is None


# ---------------------------------------------------------------------------
# Upsert / get round-trip — movate namespace
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_upsert_movate_entry_round_trip(storage) -> None:
    entry = _make_entry(slug="faq-bot", tags=["support", "faq"], shape="faq")
    await storage.upsert_catalog_entry(entry)
    got = await storage.get_catalog_entry("faq-bot", source=CatalogSource.MOVATE)
    assert got is not None
    assert got.slug == "faq-bot"
    assert got.source is CatalogSource.MOVATE
    assert got.tenant_id is None
    assert got.title == "Faq-Bot"
    assert got.tags == ["support", "faq"]
    assert got.shape == "faq"


@pytest.mark.unit
async def test_upsert_movate_overwrites_in_place(storage) -> None:
    await storage.upsert_catalog_entry(_make_entry(slug="x", description="v1"))
    await storage.upsert_catalog_entry(
        _make_entry(slug="x", description="v2", latest_version="2.0.0")
    )
    got = await storage.get_catalog_entry("x", source=CatalogSource.MOVATE)
    assert got is not None
    assert got.description == "v2"
    assert got.latest_version == "2.0.0"


# ---------------------------------------------------------------------------
# Private namespace + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_private_requires_tenant(storage) -> None:
    with pytest.raises(ValueError):
        await storage.upsert_catalog_entry(
            _make_entry(slug="needs-tenant", source=CatalogSource.PRIVATE)
        )


@pytest.mark.unit
async def test_movate_must_not_have_tenant(storage) -> None:
    with pytest.raises(ValueError):
        await storage.upsert_catalog_entry(
            _make_entry(
                slug="movate-with-tenant",
                source=CatalogSource.MOVATE,
                tenant_id="tenant-a",
            )
        )


@pytest.mark.unit
async def test_private_round_trip_and_isolation(storage) -> None:
    a = _make_entry(
        slug="ops-helper",
        source=CatalogSource.PRIVATE,
        tenant_id="tenant-a",
    )
    b = _make_entry(
        slug="ops-helper",
        source=CatalogSource.PRIVATE,
        tenant_id="tenant-b",
    )
    await storage.upsert_catalog_entry(a)
    await storage.upsert_catalog_entry(b)

    got_a = await storage.get_catalog_entry(
        "ops-helper", source=CatalogSource.PRIVATE, tenant_id="tenant-a"
    )
    got_b = await storage.get_catalog_entry(
        "ops-helper", source=CatalogSource.PRIVATE, tenant_id="tenant-b"
    )
    assert got_a is not None and got_a.tenant_id == "tenant-a"
    assert got_b is not None and got_b.tenant_id == "tenant-b"

    # cross-tenant lookup returns None (no leak)
    assert (
        await storage.get_catalog_entry(
            "ops-helper", source=CatalogSource.PRIVATE, tenant_id="tenant-c"
        )
        is None
    )
    # null tenant on private lookup → None (no implicit cross-tenant read)
    assert await storage.get_catalog_entry("ops-helper", source=CatalogSource.PRIVATE) is None


# ---------------------------------------------------------------------------
# Read API visibility — movate + caller's private + community
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_unions_namespaces(storage) -> None:
    await storage.upsert_catalog_entry(_make_entry(slug="public-1"))
    await storage.upsert_catalog_entry(_make_entry(slug="public-2"))
    await storage.upsert_catalog_entry(
        _make_entry(slug="a-private", source=CatalogSource.PRIVATE, tenant_id="tenant-a")
    )
    await storage.upsert_catalog_entry(
        _make_entry(slug="b-private", source=CatalogSource.PRIVATE, tenant_id="tenant-b")
    )

    visible_a = {e.slug for e in await storage.list_catalog_entries("tenant-a")}
    visible_b = {e.slug for e in await storage.list_catalog_entries("tenant-b")}
    assert visible_a == {"public-1", "public-2", "a-private"}
    assert visible_b == {"public-1", "public-2", "b-private"}


@pytest.mark.unit
async def test_list_filters(storage) -> None:
    await storage.upsert_catalog_entry(
        _make_entry(slug="ticket-1", tags=["support"], shape="ticket_triager")
    )
    await storage.upsert_catalog_entry(
        _make_entry(slug="rag-doc", tags=["rag", "docs"], shape="rag_qa")
    )
    await storage.upsert_catalog_entry(_make_entry(slug="faq-help", tags=["faq"], shape="faq"))

    by_tag = await storage.list_catalog_entries("tenant-a", tag_filter="rag")
    assert [e.slug for e in by_tag] == ["rag-doc"]

    by_shape = await storage.list_catalog_entries("tenant-a", shape_filter="ticket_triager")
    assert [e.slug for e in by_shape] == ["ticket-1"]

    by_q = await storage.list_catalog_entries("tenant-a", q="rag")
    assert [e.slug for e in by_q] == ["rag-doc"]


@pytest.mark.unit
async def test_list_pagination_by_after_slug(storage) -> None:
    for slug in ["a", "b", "c", "d"]:
        await storage.upsert_catalog_entry(_make_entry(slug=slug))
    page = await storage.list_catalog_entries("tenant-a", after_slug="b", limit=10)
    assert [e.slug for e in page] == ["c", "d"]


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_version_upsert_and_get(storage) -> None:
    await storage.upsert_catalog_entry(_make_entry(slug="ver"))
    payload, digest = _bytes("hello-bundle")
    record = await storage.upsert_catalog_entry_version(
        "ver",
        source=CatalogSource.MOVATE,
        version="1.0.0",
        bundle_tar=payload,
        digest=digest,
    )
    assert record.bundle_tar == payload
    assert record.digest == digest

    got = await storage.get_catalog_entry_version(
        "ver", source=CatalogSource.MOVATE, version="1.0.0"
    )
    assert got is not None and got.bundle_tar == payload

    versions = await storage.get_catalog_entry_versions("ver", source=CatalogSource.MOVATE)
    assert len(versions) == 1


@pytest.mark.unit
async def test_version_upsert_overwrites_payload(storage) -> None:
    await storage.upsert_catalog_entry(_make_entry(slug="ver"))
    first, first_digest = _bytes("first")
    await storage.upsert_catalog_entry_version(
        "ver",
        source=CatalogSource.MOVATE,
        version="1.0.0",
        bundle_tar=first,
        digest=first_digest,
    )
    second, second_digest = _bytes("second")
    await storage.upsert_catalog_entry_version(
        "ver",
        source=CatalogSource.MOVATE,
        version="1.0.0",
        bundle_tar=second,
        digest=second_digest,
    )
    got = await storage.get_catalog_entry_version(
        "ver", source=CatalogSource.MOVATE, version="1.0.0"
    )
    assert got is not None and got.bundle_tar == second


@pytest.mark.unit
async def test_version_isolation_for_private(storage) -> None:
    payload_a, digest_a = _bytes("tenant-a")
    payload_b, digest_b = _bytes("tenant-b")
    await storage.upsert_catalog_entry(
        _make_entry(slug="ver", source=CatalogSource.PRIVATE, tenant_id="tenant-a")
    )
    await storage.upsert_catalog_entry(
        _make_entry(slug="ver", source=CatalogSource.PRIVATE, tenant_id="tenant-b")
    )
    await storage.upsert_catalog_entry_version(
        "ver",
        source=CatalogSource.PRIVATE,
        version="1.0.0",
        bundle_tar=payload_a,
        digest=digest_a,
        tenant_id="tenant-a",
    )
    await storage.upsert_catalog_entry_version(
        "ver",
        source=CatalogSource.PRIVATE,
        version="1.0.0",
        bundle_tar=payload_b,
        digest=digest_b,
        tenant_id="tenant-b",
    )
    got = await storage.get_catalog_entry_version(
        "ver",
        source=CatalogSource.PRIVATE,
        version="1.0.0",
        tenant_id="tenant-a",
    )
    assert got is not None and got.bundle_tar == payload_a


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_record_rating_recomputes_summary(storage) -> None:
    await storage.upsert_catalog_entry(_make_entry(slug="rated"))
    s1 = await storage.record_catalog_rating("rated", tenant_id="tenant-a", rating=5)
    s2 = await storage.record_catalog_rating("rated", tenant_id="tenant-b", rating=3)
    assert s1.count == 1 and s1.avg == 5.0
    assert s2.count == 2 and abs(s2.avg - 4.0) < 1e-9

    # The cached summary on the entry row reflects the latest aggregate.
    refreshed = await storage.get_catalog_entry("rated", source=CatalogSource.MOVATE)
    assert refreshed is not None
    assert refreshed.ratings_summary.count == 2
    assert abs(refreshed.ratings_summary.avg - 4.0) < 1e-9


@pytest.mark.unit
async def test_record_rating_overwrites_prior(storage) -> None:
    await storage.upsert_catalog_entry(_make_entry(slug="rated"))
    await storage.record_catalog_rating("rated", tenant_id="tenant-a", rating=1)
    final = await storage.record_catalog_rating("rated", tenant_id="tenant-a", rating=5)
    assert final.count == 1 and final.avg == 5.0


@pytest.mark.unit
async def test_rating_rejects_out_of_range(storage) -> None:
    await storage.upsert_catalog_entry(_make_entry(slug="rated"))
    with pytest.raises(ValueError):
        await storage.record_catalog_rating("rated", tenant_id="tenant-a", rating=0)
    with pytest.raises(ValueError):
        await storage.record_catalog_rating("rated", tenant_id="tenant-a", rating=6)


# ---------------------------------------------------------------------------
# Sync watermark
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_sync_watermark_round_trip(storage) -> None:
    when = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    await storage.set_catalog_sync_watermark(CatalogSource.MOVATE, when)
    got = await storage.get_catalog_sync_watermark(CatalogSource.MOVATE)
    assert got is not None
    assert got.replace(microsecond=0) == when


@pytest.mark.unit
async def test_sync_watermark_per_source(storage) -> None:
    movate_ts = datetime(2026, 5, 1, tzinfo=UTC)
    community_ts = datetime(2026, 5, 28, tzinfo=UTC)
    await storage.set_catalog_sync_watermark(CatalogSource.MOVATE, movate_ts)
    await storage.set_catalog_sync_watermark(CatalogSource.COMMUNITY, community_ts)
    assert (await storage.get_catalog_sync_watermark(CatalogSource.MOVATE)).replace(
        microsecond=0
    ) == movate_ts
    assert (await storage.get_catalog_sync_watermark(CatalogSource.COMMUNITY)).replace(
        microsecond=0
    ) == community_ts


@pytest.mark.unit
async def test_sync_watermark_upserts_in_place(storage) -> None:
    first = datetime(2026, 1, 1, tzinfo=UTC)
    second = datetime(2026, 6, 1, tzinfo=UTC)
    await storage.set_catalog_sync_watermark(CatalogSource.MOVATE, first)
    await storage.set_catalog_sync_watermark(CatalogSource.MOVATE, second)
    assert (await storage.get_catalog_sync_watermark(CatalogSource.MOVATE)).replace(
        microsecond=0
    ) == second
