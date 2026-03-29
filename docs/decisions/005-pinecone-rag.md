# 005: Pinecone for Course Knowledge RAG

## Date
2026-03-18

## Status
accepted

## Context
The WhatsApp assistant ("მრჩეველი") needs to answer course-related questions from training participants. To provide accurate, course-specific answers, the assistant needs access to lecture content. Options:
1. Pass full lecture transcriptions in each prompt (context stuffing)
2. Use a vector database for RAG (Retrieval-Augmented Generation)
3. Fine-tune a model on course content

## Decision
Use **Pinecone** as the vector database for RAG.

Configuration:
- Index name: `training-course`
- Embedding model: `gemini-embedding-001` (3072 dimensions)
- Implementation: `tools/integrations/knowledge_indexer.py`
- API key: `PINECONE_API_KEY` env var

## Reasoning
1. **Context stuffing rejected**: 15 lectures × 2 groups = 30 full transcriptions. Far too much to fit in any prompt, even with 1M token models. Also extremely expensive per query.
2. **Fine-tuning rejected**: Requires significant effort, model hosting costs, and must be re-done after each lecture. Not practical for continuously growing course content.
3. **Pinecone chosen**: Managed vector DB, simple SDK, supports upsert/query patterns, integrates well with Gemini embeddings. Free tier sufficient for this use case (~30 lectures × chunks).

RAG flow:
1. After lecture analysis, `knowledge_indexer.py` chunks content and upserts with gemini-embedding-001
2. On incoming question, embed the question and query Pinecone for top-k relevant chunks
3. Feed retrieved context to Claude (reasoning) → Gemini (Georgian response)

## Consequences
- **Positive**: Accurate, course-specific answers. Knowledge grows automatically with each lecture. Low query cost.
- **Negative**: Additional service dependency. Embedding quality depends on gemini-embedding-001.
- **Scaling**: Free tier handles current load. May need paid tier if course grows significantly.
