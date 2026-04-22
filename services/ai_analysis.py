"""AI-powered stock news analysis using Groq."""

import json
import logging
from groq import Groq
from config import GROQ_API_KEY, GROQ_MODEL

logger = logging.getLogger(__name__)

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


def analyze_stock_news(symbol: str, articles: list, signals: list) -> dict:
    """
    Send articles + signal data to Groq for analysis.
    Returns a structured analysis dict.
    Raises RuntimeError if API key is not set.
    """
    if not client:
        raise RuntimeError("GROQ_API_KEY is not set")

    articles_text = ""
    for i, a in enumerate(articles[:80]):
        articles_text += f"{i+1}. [{a['date']}] {a['title']} - Source: {a['source']}\n"

    if signals:
        signals_text = "REGULATORY FILINGS FOUND:\n"
        for s in signals:
            signals_text += f"- {s['type']}: {s['detail']} (Date: {s.get('date', 'N/A')})\n"
    else:
        signals_text = "No regulatory signals found for this stock."

    prompt = f"""You are an expert Indian stock market analyst. Analyze the following news articles and regulatory filing signals for {symbol}.

NEWS ARTICLES (last 6 months):
{articles_text}

{signals_text}

Provide your analysis as a JSON object with EXACTLY this structure (no markdown, no backticks, pure JSON only):
{{
    "summary": "2-3 sentence overall summary of what is happening with this stock",
    "sentiment": "BULLISH" or "BEARISH" or "NEUTRAL",
    "key_patterns": [
        {{"theme": "string describing the pattern", "count": number of articles related, "months": ["Apr 2026", "Mar 2026"], "significance": "HIGH or MEDIUM or LOW"}}
    ],
    "connections": [
        {{"observation": "string connecting a news event to a regulatory filing or another news event", "significance": "HIGH or MEDIUM or LOW"}}
    ],
    "bullet_summary": [
        {{"month": "Apr 2026", "points": ["bullet point 1", "bullet point 2"]}}
    ],
    "risk_factors": ["risk 1", "risk 2"],
    "catalysts": ["catalyst 1", "catalyst 2"]
}}"""

    logger.info("Sending %d articles + %d signals to Groq for %s", len(articles), len(signals), symbol)

    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=1,
        max_completion_tokens=4096,
        top_p=1,
        reasoning_effort="medium",
        stream=False,
        stop=None,
    )

    response_text = completion.choices[0].message.content.strip()

    # Strip markdown fencing if present
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
    if response_text.endswith("```"):
        response_text = response_text.rsplit("```", 1)[0]
    response_text = response_text.strip()

    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("JSON decode failed: %s — raw: %s", e, response_text[:300])
        return {
            "summary": response_text[:500],
            "sentiment": "NEUTRAL",
            "key_patterns": [],
            "connections": [],
            "bullet_summary": [],
            "risk_factors": [],
            "catalysts": [],
            "raw_response": response_text,
        }
