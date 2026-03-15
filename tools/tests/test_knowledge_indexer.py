"""Unit tests for tools/knowledge_indexer.py.

Covers:
- chunk_text: pure-logic text splitting
- get_pinecone_index: caching, creation, missing API key
- embed_text: retry on failure, success path
- embed_texts_batch: batching, empty input
- index_lecture_content: validation, stale vector cleanup, full pipeline
- query_knowledge: empty query, group filter, response parsing
- _batch_upsert: batching and retry

Run with:
    pytest tools/tests/test_knowledge_indexer.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.knowledge_indexer as ki


# ===========================================================================
# Helpers
# ===========================================================================

def _reset_caches():
    ki._pinecone_index_cache = None
    ki._embed_client_cache = None


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
        # This is a rough structural test
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
             patch("tools.knowledge_indexer.Pinecone", return_value=mock_pc), \
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
             patch("tools.knowledge_indexer.Pinecone", return_value=mock_pc):

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
        fake_embedding = MagicMock()
        fake_embedding.values = [0.1] * 3072

        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response

        ki._embed_client_cache = mock_client

        result = ki.embed_text("test text")
        assert len(result) == 3072
        assert result[0] == 0.1

    def test_retries_on_failure(self):
        fake_embedding = MagicMock()
        fake_embedding.values = [0.5] * 3072
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = [
            Exception("transient"),
            fake_response,
        ]
        ki._embed_client_cache = mock_client

        with patch("tools.knowledge_indexer.time.sleep"):
            result = ki.embed_text("retry text")

        assert len(result) == 3072

    def test_raises_after_max_retries(self):
        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = Exception("persistent error")
        ki._embed_client_cache = mock_client

        with patch("tools.knowledge_indexer.time.sleep"):
            with pytest.raises(RuntimeError, match="failed after"):
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
        fake_embedding = MagicMock()
        fake_embedding.values = [0.1] * 3072
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding] * 5

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        texts = ["text"] * 5
        result = ki.embed_texts_batch(texts)
        assert len(result) == 5


# ===========================================================================
# 5. index_lecture_content — validation and pipeline
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

        fake_embedding = MagicMock()
        fake_embedding.values = [0.1] * 3072
        fake_response = MagicMock()
        fake_response.embeddings = [fake_embedding]

        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_response
        ki._embed_client_cache = mock_client

        with patch.object(ki, "get_pinecone_index", return_value=mock_index), \
             patch.object(ki, "chunk_text", return_value=["chunk1"]), \
             patch.object(ki, "_batch_upsert", return_value=1):

            result = ki.index_lecture_content(1, 3, "lecture content", "summary")

        assert result == 1
        # Stale vectors should be cleaned
        mock_index.delete.assert_called_once()


# ===========================================================================
# 6. query_knowledge
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
             patch.object(ki, "embed_text", return_value=[0.1] * 3072):

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
             patch.object(ki, "embed_text", return_value=[0.1] * 3072):

            ki.query_knowledge("test", group_number=None)

        query_call = mock_index.query.call_args
        assert query_call[1]["filter"] is None


# ===========================================================================
# 7. _batch_upsert
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

        with patch("tools.knowledge_indexer.time.sleep"):
            total = ki._batch_upsert(mock_index, vectors)

        assert total == 1


# ===========================================================================
# 8. Constants
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
