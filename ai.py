"""AI integration: Google Gemini for intelligent classification, summaries, and tagging."""

import json
import logging
import re
from typing import TypedDict, Optional, List, Dict, Any

import google.generativeai as genai

logger = logging.getLogger("archivist.ai")

class ClassificationResult(TypedDict):
    genre: str
    tags: List[str]
    new_tags: List[str]
    metadata: Dict[str, str]
    error: Optional[str]

class SummaryResult(TypedDict):
    gloss: Optional[str]
    summary: Optional[str]
    error: Optional[str]

class ImageResult(TypedDict):
    alt_text: Optional[str]
    transcription: Optional[str]
    error: Optional[str]


async def classify_and_tag(
    text: str,
    tag_pool: List[str],
    api_key: str,
    model: str = "gemini-1.5-flash",
    system_prompt: str = "",
) -> ClassificationResult:
    """
    Pass 1: Determine genre, metadata, and tags.
    """
    genai.configure(api_key=api_key)
    
    prompt = (
        f"{system_prompt}\n\n"
        f"Existing tag pool: {json.dumps(tag_pool)}\n\n"
        f"Content to analyze (first 30k chars):\n{text[:30000]}"
    )

    try:
        model_instance = genai.GenerativeModel(model)
        response = await model_instance.generate_content_async(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                response_mime_type="application/json",
            )
        )

        data = json.loads(response.text)
        return {
            "genre": data.get("genre", "Unknown"),
            "tags": [t.lower().strip() for t in data.get("tags", [])],
            "new_tags": [t.lower().strip() for t in data.get("new_tags", [])],
            "metadata": data.get("metadata", {}),
            "error": None
        }
    except Exception as e:
        logger.exception("Classification failed")
        return {"genre": "Unknown", "tags": [], "new_tags": [], "metadata": {}, "error": str(e)}


async def generate_summary(
    text: str,
    title: Optional[str],
    url: str,
    genre: str,
    metadata: Dict[str, str],
    api_key: str,
    model: str = "gemini-1.5-flash",
    system_prompt: str = "",
) -> SummaryResult:
    """
    Pass 2: Generate genre-aware summary.
    """
    genai.configure(api_key=api_key)
    
    prompt = f"{system_prompt}\n\n"
    prompt += f"GENRE: {genre}\n"
    if metadata:
        prompt += f"METADATA: {json.dumps(metadata)}\n"
    prompt += f"URL: {url}\n"
    if title:
        prompt += f"TITLE: {title}\n"
    prompt += f"\nCONTENT:\n{text[:30000]}"

    try:
        model_instance = genai.GenerativeModel(model)
        response = await model_instance.generate_content_async(
            prompt,
            generation_config=genai.types.GenerationConfig(temperature=0.3)
        )

        response_text = response.text
        gloss = None
        summary = None
        for line in response_text.split("\n"):
            line = line.strip()
            if line.lower().startswith("**gloss:**"):
                gloss = line.split(":**", 1)[1].strip()
            elif line.lower().startswith("**summary:**"):
                summary = line.split(":**", 1)[1].strip()

        if not gloss and not summary:
            summary = response_text.strip()

        return {"gloss": gloss, "summary": summary, "error": None}
    except Exception as e:
        logger.exception("Summary failed")
        return {"gloss": None, "summary": None, "error": str(e)}


async def describe_image(
    image_bytes: bytes,
    mime_type: str,
    api_key: str,
    model: str = "gemini-1.5-flash",
    system_prompt: str = "",
) -> ImageResult:
# ... (existing describe_image logic)

async def generate_embedding(
    text: str,
    api_key: str,
    model: str = "text-embedding-004",
) -> Optional[List[float]]:
    """
    Generate a vector embedding for a piece of text.
    Used for semantic search.
    """
    genai.configure(api_key=api_key)
    try:
        # text_embedding_004 is the latest stable text-only model
        result = genai.embed_content(
            model=model,
            content=text,
            task_type="retrieval_document",
        )
        return result['embedding']
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        return None
