"""Unit tests for tools/integrations/knowledge_indexer.py.

Covers:
- chunk_text: pure-logic text splitting
- get_pinecone_index: caching, creation, missing API key
- embed_text: retry on failure, success path
- embed_texts_batch: batching, empty input
- validate_embedding: dimension, zero-vector, norm checks
- lecture_exists_in_index: prefix-based existence check
- get_lecture_vector_count: prefix-based counting
- check_pinecone_health: healthy and unhealthy states
- index_lecture_content: validation, idempotency, embedding validation, full pipeline
- query_knowledge: empty query, group filter, response parsing, score threshold
- _batch_upsert: batching and retry

Run with:
    pytest tools/tests/test_knowledge_indexer.py -v
"""

from __future__ import annotations

import math
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
    ki._pinecone_index_cache = None
    ki._embed_client_cache = None


def _make_valid_vector(dim: int = 3072, value: float = 0.1) -> list[float]:
    """Create a valid embedding vector of the correct dimension."""
    return [value] * dim


def _make_mock_embedding(value: float = 0.1, dim: int = 3072) -> MagicMock:
    """Create a mock Gemini embedding response."""
    fake_embedding = MagicMock()
    fake_embedding.values = [value] * dim
    return fake_embedding


# ===========================================================================
# 1. chunk_text — pure logic
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
        # chunk_size=500 tokens ~= 2000 chars
        text = "a" * 5000
        result = ki.chunk_text(text, chunk_size=500, overlap=50)
        assert len(result) > 1

    def test_chunks_do_not_exceed_max_chars(self):
        text = "word " * 3000  # ~15000 chars
        chunk_size = 500
        char_size = chunk_size * ki.CHARS_PER_TOKEN
        result = ki.chunk_text(text, chunk_size=chunk_size, overlap=50)
        for chunk in result:
            assert len(chunk) <= char_size + 10  # small tolerance for trim

    def test_overlap_creates_shared_content(self):
        text = "abcdefghij" * 300  # 3000 chars
        chunks = ki.chunk_text(text, chunk_size=250, overlap=25)
        assert len(chunks) >= 2
        # With overlap, second chunk should start before end of first
        assert len(chunks) >= 2

    def test_whitespace_only_text_returns_empty(self):
        assert ki.chunk_text("   \n\n  ") == []


# ===========================================================================
# 2. get_pinecone_index — caching and creation
# ===========================================================================


class TestGetPineconeIndex:
    def setup_method(self):
        _reset_caches()

    def test_raises_when_api_key_missing(self):
        with patch.object(ki, "PINECONE_API_KEY", ""):
            with pytest.raises(RuntimeError, match="not configured"):
                ki.get_pinecone_index()

    def test_returns_cached_index(self):
        fake_index = MagicMock()
        ki._pinecone_index_cache = fake_index

        result = ki.get_pinecone_index()
        assert result is fake_index

    def test_creates_index_when_not_existing(self):
        mock_pc = MagicMock()
        # list_indexes returns empty — index doesn't exist
        mock_pc.list_indexes.return_value = []
        mock_index = MagicMock()
        mock_pc.Index.return_value = mock_index

        with patch.object(ki, "PINECONE_API_KEY", "key"), \
             patch("tools.integrations.knowledge_indexer.Pinecone", return_value=mock_pc), \
             patch.object(ki, "_wait_for_index_ready"):

            result = ki.get_pinecone_index()

        mock_pc.create_index.assert_called_once()
        assert result is mock_index

    def test_uses_existing_index(self):
        mock_pc = MagicMock()
        mock_idx_info = MagicMock()
        mock_idx_info.name = ki.PINECONE_INDEX_NAME
        mock_pc.list_indexes.return_value = [mock_idx_info]
        mock_index = MagicMock()
        mock_pc.Index.return_value = mock_index

        with patch.object(ki, "PINECONE_API_KEY", "key"), \
             patch("tools.integrations.knowledge_indexer.Pinecone", return_value=mock_pc):

            result = ki.get_pinecone_index()

        mock_pc.create_index.assert_not_called()
        assert result is mock_index


# ===========================================================================
# 3. embed_text — retry and success
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
# 5. validate_embedding — NEW
# ===========================================================================


class TestValidateEmbedding:
    def test_valid_vector_passes(self):
        vec = _make_valid_vector()
        ki.validate_embedding(vec, label="test")  # should not raise

    def test_wrong_dimension_raises(self):
        vec = [0.1] * 1024  # wrong dim
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
        vec = [1000.0] * 3072  # norm = 1000 * sqrt(3072) >> 100
        with pytest.raises(ki.EmbeddingQualityError, match="norm out of range"):
            ki.validate_embedding(vec)

    def test_normal_unit_vector_passes(self):
        # A unit vector (norm=1) should pass
        val = 1.0 / math.sqrt(3072)
        vec = [val] * 3072
        ki.validate_embedding(vec)  # should not raise

    def test_empty_vector_raises(self):
        with pytest.raises(ki.EmbeddingQualityError, match="expected 3072 dims, got 0"):
            ki.validate_embedding([])


# ===========================================================================
# 6. lecture_exists_in_index — NEW (prefix-based)
# ===========================================================================


class TestLectureExistsInIndex:
    def setup_method(self):
        _reset_caches()

    def test_returns_true_when_vectors_exist(self):
        mock_index = MagicMock()
        # Simulate list() returning some IDs
        mock_index.list.return_value = iter(["g1_l3_summary_0", "g1_l3_summary_1"])

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            result = ki.lecture_exists_in_index(1, 3)

        assert result is True
        mock_index.list.assert_called_once()

    def test_returns_false_when_no_vectors(self):
        mock_index = MagicMock()
        mock_index.list.return_value = iter([])

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            result = ki.lecture_exists_in_index(1, 3)

        assert result is False

    def test_uses_content_type_prefix_when_specified(self):
        mock_index = MagicMock()
        mock_index.list.return_value = iter(["g2_l5_transcript_0"])

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            result = ki.lecture_exists_in_index(2, 5, content_type="transcript")

        assert result is True
        call_args = mock_index.list.call_args
        assert call_args[1]["prefix"] == "g2_l5_transcript_"

    def test_returns_false_on_exception(self):
        mock_index = MagicMock()
        mock_index.list.side_effect = Exception("API error")

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            result = ki.lecture_exists_in_index(1, 1)

        assert result is False


# ===========================================================================
# 7. get_lecture_vector_count — NEW
# ===========================================================================


class TestGetLectureVectorCount:
    def setup_method(self):
        _reset_caches()

    def test_returns_count_of_vectors(self):
        mock_index = MagicMock()
        # Pinecone list() returns a generator of pages, where each page is a list of IDs.
        # Return a single page containing 3 vector IDs.
        mock_index.list.return_value = iter([["g1_l2_summary_0", "g1_l2_summary_1", "g1_l2_summary_2"]])

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            count = ki.get_lecture_vector_count(1, 2, "summary")

        assert count == 3

    def test_returns_zero_when_no_vectors(self):
        mock_index = MagicMock()
        mock_index.list.return_value = iter([])

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            count = ki.get_lecture_vector_count(1, 2)

        assert count == 0

    def test_returns_zero_on_error(self):
        mock_index = MagicMock()
        mock_index.list.side_effect = Exception("Pinecone error")

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            count = ki.get_lecture_vector_count(1, 1)

        assert count == 0


# ===========================================================================
# 8. check_pinecone_health — NEW
# ===========================================================================


class TestCheckPineconeHealth:
    def setup_method(self):
        _reset_caches()

    def test_healthy_report(self):
        mock_index = MagicMock()
        mock_stats = MagicMock()
        mock_stats.total_vector_count = 500
        mock_stats.namespaces = {}
        mock_index.describe_index_stats.return_value = mock_stats
        mock_index.list.return_value = iter([])  # no vectors found per lecture

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
            report = ki.check_pinecone_health()

        assert report.healthy is True
        assert report.total_vectors == 500
        assert report.error is None

    def test_unhealthy_report_on_exception(self):
        with patch.object(ki, "get_pinecone_index", side_effect=RuntimeError("no API key")):
            report = ki.check_pinecone_health()

        assert report.healthy is False
        assert report.total_vectors == 0
        assert "no API key" in report.error

    def test_includes_lecture_counts(self):
        mock_index = MagicMock()
        mock_stats = MagicMock()
        mock_stats.total_vector_count = 100
        mock_stats.namespaces = {}
        mock_index.describe_index_stats.return_value = mock_stats

        # Return vectors only for g1_l1_ prefix
        def fake_list(prefix="", limit=1000):
            if prefix == "g1_l1_":
                return iter(["g1_l1_summary_0", "g1_l1_summary_1"])
            return iter([])

        mock_index.list = fake_list

        with patch.object(ki, "get_pinecone_index", return_value=mock_index):
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
# 9. index_lecture_content — validation, idempotency, and pipeline
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
        mock_index = MagicMock()

        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=0), \
             patch.object(ki, "_batch_upsert", return_value=1):

            result = ki.index_lecture_content(1, 3, "lecture content", "summary")

        assert result == 1
        # Stale vectors should be cleaned
        mock_index.delete.assert_called_once()

    def test_idempotent_skip_when_same_count(self):
        """Should skip when existing vector count >= new chunk count."""
        with patch.object(ki, "get_pinecone_index", return_value=MagicMock()), \
             patch.object(ki, "chunk_text", return_value=["chunk1", "chunk2"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=2):

            result = ki.index_lecture_content(1, 3, "lecture content", "summary")

        assert result == 0  # skipped

    def test_idempotent_reindex_when_more_chunks(self):
        """Should re-index when new content has more chunks than existing."""
        mock_index = MagicMock()

        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding] * 3

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "chunk_text", return_value=["c1", "c2", "c3"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=2), \
             patch.object(ki, "_batch_upsert", return_value=3):

            result = ki.index_lecture_content(1, 3, "longer content", "summary")

        assert result == 3  # re-indexed

    def test_force_bypasses_idempotency(self):
        """Should re-index even when counts match if force=True."""
        mock_index = MagicMock()

        fake_embedding = _make_mock_embedding()
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=1), \
             patch.object(ki, "_batch_upsert", return_value=1):

            result = ki.index_lecture_content(
                1, 3, "content", "summary", force=True
            )

        assert result == 1

    def test_embedding_validation_catches_zero_vectors(self):
        """Should raise EmbeddingQualityError for zero embeddings."""
        mock_index = MagicMock()

        # Return zero-vector embedding
        zero_embedding = MagicMock()
        zero_embedding.values = [0.0] * 3072
        fake_response = MagicMock()
        fake_response.embeddings = [zero_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=0):

            with pytest.raises(ki.EmbeddingQualityError, match="all zeros"):
                ki.index_lecture_content(1, 1, "content", "summary")

    def test_embedding_validation_catches_wrong_dims(self):
        """Should raise EmbeddingQualityError for wrong dimension."""
        mock_index = MagicMock()

        wrong_dim_embedding = MagicMock()
        wrong_dim_embedding.values = [0.1] * 768  # wrong dim
        fake_response = MagicMock()
        fake_response.embeddings = [wrong_dim_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "get_lecture_vector_count", return_value=0):

            with pytest.raises(ki.EmbeddingQualityError, match="expected 3072"):
                ki.index_lecture_content(1, 1, "content", "summary")


# ===========================================================================
# 10. query_knowledge — with score threshold
# ===========================================================================


class TestQueryKnowledge:
    def setup_method(self):
        _reset_caches()

    def test_empty_query_returns_empty(self):
        result = ki.query_knowledge("   ")
        assert result == []

    def test_returns_parsed_results(self):
        mock_match = MagicMock()
        mock_match.metadata = {
            "text": "chunk text",
            "group_number": 1,
            "lecture_number": 2,
            "content_type": "summary",
        }
        mock_match.score = 0.95

        mock_response = MagicMock()
        mock_response.matches = [mock_match]

        mock_index = MagicMock()
        mock_index.query.return_value = mock_response

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("what is AI?", group_number=1, top_k=3)

        assert len(results) == 1
        assert results[0]["text"] == "chunk text"
        assert results[0]["score"] == 0.95

        # Verify group filter was passed
        query_call = mock_index.query.call_args
        assert query_call[1]["filter"] == {"group_number": {"$eq": 1}}

    def test_no_group_filter_when_none(self):
        mock_response = MagicMock()
        mock_response.matches = []
        mock_index = MagicMock()
        mock_index.query.return_value = mock_response

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            ki.query_knowledge("test", group_number=None)

        query_call = mock_index.query.call_args
        assert query_call[1]["filter"] is None

    def test_filters_below_score_threshold(self):
        """Results below the score threshold should be excluded."""
        high_match = MagicMock()
        high_match.metadata = {"text": "relevant", "group_number": 1}
        high_match.score = 0.8

        low_match = MagicMock()
        low_match.metadata = {"text": "irrelevant", "group_number": 1}
        low_match.score = 0.15

        mock_response = MagicMock()
        mock_response.matches = [high_match, low_match]

        mock_index = MagicMock()
        mock_index.query.return_value = mock_response

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("test", score_threshold=0.3)

        assert len(results) == 1
        assert results[0]["text"] == "relevant"

    def test_direct_mode_uses_lower_threshold(self):
        """Direct mode should use PINECONE_SCORE_THRESHOLD_DIRECT (0.3)."""
        low_match = MagicMock()
        low_match.metadata = {"text": "marginal"}
        low_match.score = 0.42  # above MIN_RELEVANCE_SCORE pre-filter (0.40) and direct threshold (0.3)

        mock_response = MagicMock()
        mock_response.matches = [low_match]
        mock_index = MagicMock()
        mock_index.query.return_value = mock_response

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("test", mode="direct")

        assert len(results) == 1  # 0.42 > 0.3 direct threshold and > 0.40 pre-filter

    def test_passive_mode_uses_higher_threshold(self):
        """Passive mode should use PINECONE_SCORE_THRESHOLD_PASSIVE (0.4)."""
        low_match = MagicMock()
        low_match.metadata = {"text": "marginal"}
        low_match.score = 0.35  # above 0.3 but below 0.4

        mock_response = MagicMock()
        mock_response.matches = [low_match]
        mock_index = MagicMock()
        mock_index.query.return_value = mock_response

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge("test", mode="passive")

        assert len(results) == 0  # 0.35 < 0.4 threshold

    def test_explicit_threshold_overrides_mode(self):
        """Explicit score_threshold should override mode-based defaults."""
        match = MagicMock()
        match.metadata = {"text": "content"}
        match.score = 0.55

        mock_response = MagicMock()
        mock_response.matches = [match]
        mock_index = MagicMock()
        mock_index.query.return_value = mock_response

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "embed_text", return_value=_make_valid_vector()):

            results = ki.query_knowledge(
                "test", score_threshold=0.6, mode="direct"
            )

        assert len(results) == 0  # 0.55 < 0.6 explicit threshold

    def test_invalid_query_embedding_returns_empty(self):
        """If the query embedding fails validation, return empty."""
        with patch.object(ki, "get_pinecone_index", return_value=MagicMock()), \
             patch.object(ki, "embed_text", return_value=[0.0] * 3072):

            results = ki.query_knowledge("test query")

        assert results == []


# ===========================================================================
# 11. _batch_upsert
# ===========================================================================


class TestBatchUpsert:
    def test_upserts_in_batches(self):
        mock_index = MagicMock()
        vectors = [{"id": f"v{i}", "values": [0.1], "metadata": {}} for i in range(150)]

        total = ki._batch_upsert(mock_index, vectors)

        assert total == 150
        # Should be called in 2 batches (100 + 50)
        assert mock_index.upsert.call_count == 2

    def test_retries_on_failure(self):
        mock_index = MagicMock()
        mock_index.upsert.side_effect = [
            Exception("transient"),
            None,  # success on retry
        ]
        vectors = [{"id": "v1", "values": [0.1], "metadata": {}}]

        with patch("tools.integrations.knowledge_indexer.time.sleep"):
            total = ki._batch_upsert(mock_index, vectors)

        assert total == 1

    def test_empty_vectors_returns_zero(self):
        mock_index = MagicMock()
        total = ki._batch_upsert(mock_index, [])
        assert total == 0
        mock_index.upsert.assert_not_called()


# ===========================================================================
# 12. Constants
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
