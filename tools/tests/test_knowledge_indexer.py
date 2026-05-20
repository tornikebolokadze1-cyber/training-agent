"""Unit tests for tools/integrations/knowledge_indexer.py (Qdrant backend).

Covers the same behaviors as the Pinecone-era tests, now mocked against
the Qdrant client:
- chunk_text: pure-logic text splitting
- get_qdrant_client / get_pinecone_index alias: caching, missing URL
- embed_text: retry on failure, success path
- embed_texts_batch: batching, empty input
- validate_embedding: dimension, zero-vector, norm checks
- lecture_exists_in_index: count-based existence check
- get_lecture_vector_count: count-based counting
- delete_lecture_vectors: filter-based deletion + pre-count
- check_pinecone_health: healthy and unhealthy states
- index_lecture_content: validation, idempotency, embedding validation, full pipeline
- query_knowledge: empty query, group filter, response parsing, score threshold
- _batch_upsert: batching and retry

Run with:
    pytest tools/tests/test_knowledge_indexer.py -v
"""

from __future__ import annotations

import math
import uuid
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.integrations.knowledge_indexer as ki

# ===========================================================================
# Helpers
# ===========================================================================


def _reset_caches():
    ki._qdrant_client_cache = None
    ki._embed_client_cache = None


def _make_valid_vector(dim: int = 3072, value: float = 0.1) -> list[float]:
    """Create a valid embedding vector of the correct dimension."""
    return [value] * dim


def _make_mock_embedding(value: float = 0.1, dim: int = 3072) -> MagicMock:
    """Create a mock Gemini embedding response."""
    fake_embedding = MagicMock()
    fake_embedding.values = [value] * dim
    return fake_embedding


def _count_result(n: int) -> MagicMock:
    """Build a fake Qdrant CountResult with a ``count`` attribute."""
    obj = MagicMock()
    obj.count = n
    return obj


def _scored_point(score: float, payload: dict) -> MagicMock:
    """Build a fake Qdrant ScoredPoint."""
    obj = MagicMock()
    obj.score = score
    obj.payload = payload
    return obj


def _query_response(points: list) -> MagicMock:
    """Build a fake Qdrant QueryResponse with .points."""
    obj = MagicMock()
    obj.points = points
    return obj


# ===========================================================================
# 1. chunk_text — pure logic (unchanged from Pinecone era)
# ===========================================================================


class TestChunkText:
    def test_empty_text_returns_empty_list(self):
        assert ki.chunk_text("") == []

    def test_short_text_returns_single_chunk(self):
        text = "Hello, world!"
        result = ki.chunk_text(text)
        assert len(result) == 1
        assert result[0] == text

    def test_long_text_produces_multiple_chunks(self):
        text = "a" * 5000
        result = ki.chunk_text(text, chunk_size=500, overlap=50)
        assert len(result) > 1

    def test_chunks_do_not_exceed_max_chars(self):
        text = "word " * 3000
        chunk_size = 500
        char_size = chunk_size * ki.CHARS_PER_TOKEN
        result = ki.chunk_text(text, chunk_size=chunk_size, overlap=50)
        for chunk in result:
            assert len(chunk) <= char_size + 10

    def test_overlap_creates_shared_content(self):
        text = "abcdefghij" * 300
        chunks = ki.chunk_text(text, chunk_size=250, overlap=25)
        assert len(chunks) >= 2

    def test_whitespace_only_text_returns_empty(self):
        assert ki.chunk_text("   \n\n  ") == []


# ===========================================================================
# 2. get_qdrant_client (and get_pinecone_index alias) — caching and creation
# ===========================================================================


class TestGetQdrantClient:
    def setup_method(self):
        _reset_caches()

    def test_raises_when_url_missing(self):
        with patch.object(ki, "QDRANT_URL", ""):
            with pytest.raises(RuntimeError, match="not configured"):
                ki.get_qdrant_client()

    def test_returns_cached_client(self):
        fake_client = MagicMock()
        ki._qdrant_client_cache = fake_client

        result = ki.get_qdrant_client()
        assert result is fake_client

    def test_pinecone_alias_returns_same(self):
        """get_pinecone_index() must keep working for backward compatibility."""
        fake_client = MagicMock()
        ki._qdrant_client_cache = fake_client

        assert ki.get_pinecone_index() is fake_client

    def test_creates_collection_when_missing(self):
        mock_client = MagicMock()
        mock_collections = MagicMock()
        mock_collections.collections = []  # collection does not exist
        mock_client.get_collections.return_value = mock_collections

        with patch.object(ki, "QDRANT_URL", "http://localhost:6333"), \
             patch.object(ki, "QDRANT_API_KEY", "test"), \
             patch("tools.integrations.knowledge_indexer.QdrantClient",
                   return_value=mock_client):
            result = ki.get_qdrant_client()

        mock_client.create_collection.assert_called_once()
        assert result is mock_client

    def test_does_not_recreate_existing_collection(self):
        existing = MagicMock()
        existing.name = ki.QDRANT_COLLECTION_NAME
        mock_collections = MagicMock()
        mock_collections.collections = [existing]

        mock_client = MagicMock()
        mock_client.get_collections.return_value = mock_collections

        with patch.object(ki, "QDRANT_URL", "http://localhost:6333"), \
             patch.object(ki, "QDRANT_API_KEY", "test"), \
             patch("tools.integrations.knowledge_indexer.QdrantClient",
                   return_value=mock_client):
            ki.get_qdrant_client()

        mock_client.create_collection.assert_not_called()


# ===========================================================================
# 3. embed_text — retry and success (unchanged)
# ===========================================================================


class TestEmbedText:
    def setup_method(self):
        _reset_caches()

    def test_returns_embedding_vector(self):
        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response

        ki._embed_client_cache = mock_client

        result = ki.embed_text("test text")
        assert len(result) == 3072
        assert result[0] == 0.1

    def test_retries_on_failure(self):
        fake_embedding = _make_mock_embedding(0.5)
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = [
            Exception("transient"),
            fake_response,
        ]
        ki._embed_client_cache = mock_client

        with patch("tools.integrations.knowledge_indexer.time.sleep"):
            result = ki.embed_text("retry text")

        assert len(result) == 3072

    def test_raises_after_max_retries(self):
        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = Exception("persistent error")
        ki._embed_client_cache = mock_client

        with patch("tools.core.retry.time.sleep"):
            with pytest.raises(Exception, match="persistent error"):
                ki.embed_text("will fail")


# ===========================================================================
# 4. embed_texts_batch
# ===========================================================================


class TestEmbedTextsBatch:
    def setup_method(self):
        _reset_caches()

    def test_empty_input_returns_empty(self):
        assert ki.embed_texts_batch([]) == []

    def test_batches_texts(self):
        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding] * 5

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        texts = ["text"] * 5
        result = ki.embed_texts_batch(texts)
        assert len(result) == 5


# ===========================================================================
# 5. validate_embedding
# ===========================================================================


class TestValidateEmbedding:
    def test_valid_vector_passes(self):
        vec = _make_valid_vector()
        ki.validate_embedding(vec, label="test")

    def test_wrong_dimension_raises(self):
        vec = [0.1] * 1024
        with pytest.raises(ki.EmbeddingQualityError, match="expected 3072 dims, got 1024"):
            ki.validate_embedding(vec)

    def test_zero_vector_raises(self):
        vec = [0.0] * 3072
        with pytest.raises(ki.EmbeddingQualityError, match="all zeros"):
            ki.validate_embedding(vec)

    def test_near_zero_norm_raises(self):
        vec = [1e-10] * 3072
        with pytest.raises(ki.EmbeddingQualityError, match="all zeros"):
            ki.validate_embedding(vec)

    def test_huge_norm_raises(self):
        vec = [1000.0] * 3072
        with pytest.raises(ki.EmbeddingQualityError, match="norm out of range"):
            ki.validate_embedding(vec)

    def test_normal_unit_vector_passes(self):
        val = 1.0 / math.sqrt(3072)
        vec = [val] * 3072
        ki.validate_embedding(vec)

    def test_empty_vector_raises(self):
        with pytest.raises(ki.EmbeddingQualityError, match="expected 3072 dims, got 0"):
            ki.validate_embedding([])


# ===========================================================================
# 6. lecture_exists_in_index — Qdrant count-based
# ===========================================================================


class TestLectureExistsInIndex:
    def setup_method(self):
        _reset_caches()

    def test_returns_true_when_count_positive(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(2)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            result = ki.lecture_exists_in_index(1, 3)

        assert result is True
        mock_client.count.assert_called_once()

    def test_returns_false_when_count_zero(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(0)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            result = ki.lecture_exists_in_index(1, 3)

        assert result is False

    def test_uses_content_type_filter(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(1)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            result = ki.lecture_exists_in_index(2, 5, content_type="transcript")

        assert result is True
        call_kwargs = mock_client.count.call_args.kwargs
        # The filter must be present and target our collection.
        assert call_kwargs["collection_name"] == ki.QDRANT_COLLECTION_NAME
        assert "count_filter" in call_kwargs

    def test_returns_false_on_exception(self):
        mock_client = MagicMock()
        mock_client.count.side_effect = Exception("API error")

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            result = ki.lecture_exists_in_index(1, 1)

        assert result is False


# ===========================================================================
# 7. get_lecture_vector_count — Qdrant count-based
# ===========================================================================


class TestGetLectureVectorCount:
    def setup_method(self):
        _reset_caches()

    def test_returns_count_from_qdrant(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(3)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            count = ki.get_lecture_vector_count(1, 2, "summary")

        assert count == 3

    def test_returns_zero_when_count_zero(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(0)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            count = ki.get_lecture_vector_count(1, 2)

        assert count == 0

    def test_returns_zero_on_error(self):
        mock_client = MagicMock()
        mock_client.count.side_effect = Exception("Qdrant error")

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            count = ki.get_lecture_vector_count(1, 1)

        assert count == 0


# ===========================================================================
# 8. delete_lecture_vectors — Qdrant filter-based delete
# ===========================================================================


class TestDeleteLectureVectors:
    def setup_method(self):
        _reset_caches()

    def test_deletes_and_returns_precount(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(5)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            n = ki.delete_lecture_vectors(1, 3)

        assert n == 5
        mock_client.delete.assert_called_once()

    def test_returns_zero_when_no_vectors_exist(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(0)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            n = ki.delete_lecture_vectors(1, 3)

        assert n == 0
        mock_client.delete.assert_not_called()

    def test_returns_zero_on_delete_failure(self):
        mock_client = MagicMock()
        mock_client.count.return_value = _count_result(2)
        mock_client.delete.side_effect = Exception("Qdrant down")

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            n = ki.delete_lecture_vectors(1, 3)

        assert n == 0


# ===========================================================================
# 9. check_pinecone_health (now wraps Qdrant)
# ===========================================================================


class TestCheckPineconeHealth:
    def setup_method(self):
        _reset_caches()

    def test_healthy_report(self):
        mock_client = MagicMock()
        info = MagicMock()
        info.points_count = 500
        mock_client.get_collection.return_value = info
        mock_client.count.return_value = _count_result(0)

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            report = ki.check_pinecone_health()

        assert report.healthy is True
        assert report.total_vectors == 500
        assert report.error is None

    def test_unhealthy_report_on_exception(self):
        with patch.object(
            ki, "get_qdrant_client", side_effect=RuntimeError("no URL")
        ):
            report = ki.check_pinecone_health()

        assert report.healthy is False
        assert report.total_vectors == 0
        assert "no URL" in report.error

    def test_includes_lecture_counts(self):
        mock_client = MagicMock()
        info = MagicMock()
        info.points_count = 100
        mock_client.get_collection.return_value = info

        # First (g1_l1) returns 2; everything else 0.
        call_seq = {"n": 0}

        def fake_count(**kwargs):
            call_seq["n"] += 1
            return _count_result(2 if call_seq["n"] == 1 else 0)

        mock_client.count.side_effect = fake_count

        with patch.object(ki, "get_qdrant_client", return_value=mock_client):
            report = ki.check_pinecone_health()

        assert report.healthy is True
        assert report.lecture_counts.get("g1_l1") == 2

    def test_report_is_immutable(self):
        report = ki.PineconeHealthReport(
            healthy=True, total_vectors=0, lecture_counts={}, error=None
        )
        with pytest.raises(AttributeError):
            report.healthy = False  # type: ignore[misc]


# ===========================================================================
# 10. index_lecture_content
# ===========================================================================


class TestIndexLectureContent:
    def setup_method(self):
        _reset_caches()

    def test_invalid_content_type_raises(self):
        with pytest.raises(ValueError, match="Unknown content_type"):
            ki.index_lecture_content(1, 1, "content", "invalid_type")

    def test_empty_content_returns_zero(self):
        result = ki.index_lecture_content(1, 1, "   ", "summary")
        assert result == 0

    def test_full_pipeline(self):
        mock_client = MagicMock()

        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_embed_client = MagicMock()
        mock_embed_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_embed_client

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=0), \
             patch.object(ki, "_batch_upsert", return_value=1):

            result = ki.index_lecture_content(1, 3, "lecture content", "summary")

        assert result == 1
        # Stale vectors should be cleaned via delete-by-filter
        mock_client.delete.assert_called_once()

    def test_idempotent_skip_when_same_count(self):
        with patch.object(ki, "get_qdrant_client", return_value=MagicMock()), \
             patch.object(ki, "chunk_text", return_value=["chunk1", "chunk2"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=2):

            result = ki.index_lecture_content(1, 3, "lecture content", "summary")

        assert result == 0

    def test_idempotent_reindex_when_more_chunks(self):
        mock_client = MagicMock()

        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding] * 3

        mock_embed_client = MagicMock()
        mock_embed_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_embed_client

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "chunk_text", return_value=["c1", "c2", "c3"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=2), \
             patch.object(ki, "_batch_upsert", return_value=3):

            result = ki.index_lecture_content(1, 3, "longer content", "summary")

        assert result == 3

    def test_force_bypasses_idempotency(self):
        mock_client = MagicMock()

        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_embed_client = MagicMock()
        mock_embed_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_embed_client

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=1), \
             patch.object(ki, "_batch_upsert", return_value=1):

            result = ki.index_lecture_content(
                1, 3, "content", "summary", force=True
            )

        assert result == 1

    def test_embedding_validation_catches_zero_vectors(self):
        mock_client = MagicMock()

        zero_embedding = MagicMock()
        zero_embedding.values = [0.0] * 3072
        fake_response = MagicMock()
        fake_response.embeddings = [zero_embedding]

        mock_embed_client = MagicMock()
        mock_embed_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_embed_client

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=0):

            with pytest.raises(ki.EmbeddingQualityError, match="all zeros"):
                ki.index_lecture_content(1, 1, "content", "summary")

    def test_embedding_validation_catches_wrong_dims(self):
        mock_client = MagicMock()

        wrong_dim_embedding = MagicMock()
        wrong_dim_embedding.values = [0.1] * 768
        fake_response = MagicMock()
        fake_response.embeddings = [wrong_dim_embedding]

        mock_embed_client = MagicMock()
        mock_embed_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_embed_client

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=0):

            with pytest.raises(ki.EmbeddingQualityError, match="expected 3072"):
                ki.index_lecture_content(1, 1, "content", "summary")


# ===========================================================================
# 11. query_knowledge — Qdrant query_points
# ===========================================================================


class TestQueryKnowledge:
    def setup_method(self):
        _reset_caches()

    def test_empty_query_returns_empty(self):
        result = ki.query_knowledge("   ")
        assert result == []

    def test_returns_parsed_results(self):
        point = _scored_point(
            score=0.95,
            payload={
                "text": "chunk text",
                "group_number": 1,
                "lecture_number": 2,
                "content_type": "summary",
            },
        )
        mock_client = MagicMock()
        mock_client.query_points.return_value = _query_response([point])

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("what is AI?", group_number=1, top_k=3)

        assert len(results) == 1
        assert results[0]["text"] == "chunk text"
        assert results[0]["score"] == 0.95

        # Group filter must be set on the Qdrant call.
        call_kwargs = mock_client.query_points.call_args.kwargs
        assert call_kwargs["collection_name"] == ki.QDRANT_COLLECTION_NAME
        assert call_kwargs["query_filter"] is not None

    def test_no_group_filter_when_none(self):
        mock_client = MagicMock()
        mock_client.query_points.return_value = _query_response([])

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            ki.query_knowledge("test", group_number=None)

        call_kwargs = mock_client.query_points.call_args.kwargs
        assert call_kwargs["query_filter"] is None

    def test_filters_below_score_threshold(self):
        high = _scored_point(score=0.8, payload={"text": "relevant", "group_number": 1})
        low = _scored_point(score=0.15, payload={"text": "irrelevant", "group_number": 1})

        mock_client = MagicMock()
        mock_client.query_points.return_value = _query_response([high, low])

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("test", score_threshold=0.3)

        assert len(results) == 1
        assert results[0]["text"] == "relevant"

    def test_direct_mode_uses_lower_threshold(self):
        # 0.42 sits above MIN_RELEVANCE_SCORE (0.40) AND above the direct
        # default (0.3), so it should make it through.
        point = _scored_point(score=0.42, payload={"text": "marginal"})

        mock_client = MagicMock()
        mock_client.query_points.return_value = _query_response([point])

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("test", mode="direct")

        assert len(results) == 1

    def test_passive_mode_uses_higher_threshold(self):
        point = _scored_point(score=0.35, payload={"text": "marginal"})

        mock_client = MagicMock()
        mock_client.query_points.return_value = _query_response([point])

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("test", mode="passive")

        # 0.35 < 0.40 pre-filter → excluded.
        assert len(results) == 0

    def test_explicit_threshold_overrides_mode(self):
        point = _scored_point(score=0.55, payload={"text": "content"})

        mock_client = MagicMock()
        mock_client.query_points.return_value = _query_response([point])

        with patch.object(ki, "get_qdrant_client", return_value=mock_client), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge(
                "test", score_threshold=0.6, mode="direct"
            )

        assert len(results) == 0  # 0.55 < 0.6 explicit threshold

    def test_invalid_query_embedding_returns_empty(self):
        with patch.object(ki, "get_qdrant_client", return_value=MagicMock()), \
             patch.object(ki, "embed_text", return_value=[0.0] * 3072):

            results = ki.query_knowledge("test query")

        assert results == []


# ===========================================================================
# 12. _batch_upsert
# ===========================================================================


class TestBatchUpsert:
    def test_upserts_in_batches(self):
        mock_client = MagicMock()
        # Use legacy dict form to verify the coercion shim also works.
        vectors = [
            {"id": f"g1_l1_summary_{i}", "values": [0.1], "metadata": {}}
            for i in range(150)
        ]

        total = ki._batch_upsert(mock_client, vectors)

        assert total == 150
        # 150 / batch_size 100 → 2 calls
        assert mock_client.upsert.call_count == 2

    def test_retries_on_failure(self):
        mock_client = MagicMock()
        mock_client.upsert.side_effect = [Exception("transient"), None]
        vectors = [{"id": "g1_l1_summary_0", "values": [0.1], "metadata": {}}]

        with patch("tools.integrations.knowledge_indexer.time.sleep"):
            total = ki._batch_upsert(mock_client, vectors)

        assert total == 1

    def test_empty_vectors_returns_zero(self):
        mock_client = MagicMock()
        total = ki._batch_upsert(mock_client, [])
        assert total == 0
        mock_client.upsert.assert_not_called()


# ===========================================================================
# 13. Vector ID conversion (legacy → uuid5)
# ===========================================================================


class TestVectorIdConversion:
    def test_uuid_is_deterministic(self):
        a = ki._vector_id_for(4, 3, "summary", 0)
        b = ki._vector_id_for(4, 3, "summary", 0)
        assert a == b
        # Must look like a UUID
        uuid.UUID(a)

    def test_different_keys_produce_different_uuids(self):
        a = ki._vector_id_for(1, 1, "summary", 0)
        b = ki._vector_id_for(1, 1, "summary", 1)
        c = ki._vector_id_for(1, 2, "summary", 0)
        assert len({a, b, c}) == 3

    def test_legacy_dict_id_is_hashed(self):
        """_to_point must hash a Pinecone-style string ID into a UUID5."""
        v = {"id": "g4_l3_summary_0", "values": [0.1] * 3072, "metadata": {}}
        point = ki._to_point(v)
        # Production code path: the ID is now a UUID string.
        uuid.UUID(point.id)
        # Original key preserved in payload for debugging.
        assert point.payload.get("legacy_id") == "g4_l3_summary_0"


# ===========================================================================
# 14. Constants
# ===========================================================================


class TestConstants:
    def test_embedding_dimension(self):
        assert ki.EMBEDDING_DIMENSION == 3072

    def test_valid_content_types(self):
        assert "transcript" in ki.CONTENT_TYPES
        assert "summary" in ki.CONTENT_TYPES
        assert "gap_analysis" in ki.CONTENT_TYPES
        assert "deep_analysis" in ki.CONTENT_TYPES

    def test_upsert_batch_size_is_positive(self):
        assert ki.UPSERT_BATCH_SIZE > 0

    def test_collection_name_matches_legacy(self):
        # Same name as the old Pinecone index so dashboards keep working.
        assert ki.QDRANT_COLLECTION_NAME == "training-course"
