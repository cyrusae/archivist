"""AI integration: Google Gemini for classification, summaries, tagging, and OCR."""

import json
import logging
from typing import TypedDict, Optional, List, Dict

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
    model: str = "gemini-2.0-flash",
    system_prompt: str = "",
) -> ClassificationResult:
    """Pass 1: determine genre, metadata, and tags."""
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
            ),
        )

        data = json.loads(response.text)
        return {
            "genre": data.get("genre", "Unknown"),
            "tags": [t.lower().strip() for t in data.get("tags", []) if t],
            "new_tags": [t.lower().strip() for t in data.get("new_tags", []) if t],
            "metadata": data.get("metadata", {}) or {},
            "error": None,
        }
    except Exception as e:
        logger.exception("Classification failed")
        return {
            "genre": "Unknown",
            "tags": [],
            "new_tags": [],
            "metadata": {},
            "error": str(e),
        }


async def generate_summary(
    text: str,
    title: Optional[str],
    url: str,
    genre: str,
    metadata: Dict[str, str],
    api_key: str,
    model: str = "gemini-2.0-flash",
    system_prompt: str = "",
) -> SummaryResult:
    """Pass 2: generate a genre-aware summary."""
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
            generation_config=genai.types.GenerationConfig(temperature=0.3),
        )

        response_text = response.text or ""
        gloss = None
        summary = None
        for line in response_text.splitlines():
            line = line.strip()
            if line.lower().startswith("**gloss:**"):
                gloss = line.split(":**", 1)[1].strip()
            elif line.lower().startswith("**summary:**"):
                summary = line.split(":**", 1)[1].strip()

        if not gloss and not summary:
            # Fallback: don't dump unbounded LLM output into Discord — trim.
            fallback = response_text.strip()
            summary = (fallback[:500] + "…") if len(fallback) > 500 else fallback

        return {"gloss": gloss, "summary": summary, "error": None}
    except Exception as e:
        logger.exception("Summary failed")
        return {"gloss": None, "summary": None, "error": str(e)}


async def describe_image(
    image_bytes: bytes,
    mime_type: str,
    api_key: str,
    model: str = "gemini-2.0-flash",
    system_prompt: str = "",
) -> ImageResult:
    """
    Describe an image with Gemini's multimodal input: produce alt-text plus
    a verbatim transcription if any text is visible. The expected response
    shape is configured in `config/default.yaml` under `gemini.image_system_prompt`.
    """
    genai.configure(api_key=api_key)

    prompt = system_prompt or (
        "Describe this image for a blind or low-vision reader, then "
        "transcribe any visible text.\n"
        "Respond in this exact format:\n"
        "**Alt Text:** [one-to-two sentence description]\n"
        "**Transcription:** [verbatim text, or omit the line entirely]"
    )

    try:
        model_instance = genai.GenerativeModel(model)
        response = await model_instance.generate_content_async(
            [prompt, {"mime_type": mime_type, "data": image_bytes}],
            generation_config=genai.types.GenerationConfig(temperature=0.2),
        )

        response_text = response.text or ""

        alt_text: Optional[str] = None
        transcription_lines: List[str] = []
        in_transcription = False

        for line in response_text.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("**alt text:**"):
                alt_text = stripped.split(":**", 1)[1].strip() or None
                in_transcription = False
            elif lower.startswith("**transcription:**"):
                rest = stripped.split(":**", 1)[1].strip()
                if rest:
                    transcription_lines.append(rest)
                in_transcription = True
            elif in_transcription:
                transcription_lines.append(line)

        transcription = (
            "\n".join(transcription_lines).strip() or None
            if transcription_lines
            else None
        )

        # Fallback: no structured shape at all — treat the whole response as alt-text,
        # capped so a runaway model can't blow the Discord 2000-char limit.
        if alt_text is None and transcription is None:
            fallback = response_text.strip()
            alt_text = (
                (fallback[:500] + "…") if len(fallback) > 500 else (fallback or None)
            )

        return {
            "alt_text": alt_text,
            "transcription": transcription,
            "error": None,
        }
    except Exception as e:
        logger.exception("Image description failed")
        return {"alt_text": None, "transcription": None, "error": str(e)}


async def generate_embedding(
    text: str,
    api_key: str,
    model: str = "text-embedding-004",
) -> Optional[List[float]]:
    """
    Generate a 768-dim embedding for a piece of text, used for semantic search.
    Return shape must match the `vector(768)` column in `archived_links`.
    """
    genai.configure(api_key=api_key)
    try:
        result = genai.embed_content(
            model=model,
            content=text,
            task_type="retrieval_document",
        )
        embedding = result.get("embedding") if isinstance(result, dict) else None
        if not embedding:
            logger.warning("Embedding response had no 'embedding' field")
            return None
        return embedding
    except Exception as e:
        logger.error(f"Embedding failed: {type(e).__name__}: {e}")
        return None
