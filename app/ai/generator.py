"""
app/ai/generator.py  –  Gemini / Gemma 4 AI layer

Classification: uses response_mime_type="application/json" — the API forces the
model to output valid JSON only, regardless of any preamble tendency.
No ThinkingConfig (not supported on gemma-4-31b-it).

Draft generation: uses a hard delimiter (###EMAIL_START###) in the prompt so
that even if the model writes preamble, we slice it off exactly. Keyword
fallback always runs in parallel for classification resilience.
"""

import os
import re
import json
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemma-4-31b-it")

_DRAFT_DELIMITER = "###EMAIL_START###"


# ── SDK helpers ────────────────────────────────────────────────────────────────

def _get_client():
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


def _get_model_name() -> str:
    try:
        from app.db.models import get_setting
        return get_setting("GEMINI_MODEL") or _DEFAULT_MODEL
    except Exception:
        return _DEFAULT_MODEL


# ── Query type catalogue ────────────────────────────────────────────────────────

QUERY_TYPES = ["billing", "technical", "general", "complaint", "refund", "onboarding"]

_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("billing",    [r"\binvoice\b", r"\bcharge\b", r"\bpayment\b", r"\bbill\b",
                    r"\bsubscription\b", r"\bprice\b", r"\bpricing\b", r"\bfee\b",
                    r"\bovercharged\b", r"\btransaction\b"]),
    ("refund",     [r"\brefund\b", r"\bmoney back\b", r"\breimburse\b",
                    r"\bchargeback\b", r"\bcancel.*charge\b", r"\bget.*money\b"]),
    ("complaint",  [r"\bcomplaint\b", r"\bunacceptable\b", r"\bdisappointed\b",
                    r"\bfrustrat\b", r"\bangry\b", r"\bterrible\b", r"\bworst\b",
                    r"\bescalate\b", r"\bmanager\b", r"\bunhappy\b"]),
    ("technical",  [r"\bbug\b", r"\berror\b", r"\bcrash\b", r"\bnot working\b",
                    r"\bbroke\b", r"\bissue with\b", r"\bcan.t log\b",
                    r"\bcannot log\b", r"\bfailed\b", r"\blatency\b",
                    r"\btimeout\b", r"\bintegration\b", r"\bapi\b"]),
    ("onboarding", [r"\bnew account\b", r"\bjust signed up\b", r"\bget started\b",
                    r"\bhow do i\b", r"\bsetup\b", r"\bset up\b",
                    r"\bonboard\b", r"\bwelcome\b", r"\btutorial\b",
                    r"\bfirst time\b", r"\bregistered\b"]),
    ("general",    [r"\bquestion\b", r"\binquiry\b", r"\bwould like to know\b",
                    r"\bmore information\b", r"\bmore info\b"]),
]


def _keyword_classify(subject: str, body: str) -> str | None:
    text = (subject + " " + body).lower()
    scores: dict[str, int] = {}
    for qtype, patterns in _KEYWORD_RULES:
        hits = sum(1 for p in patterns if re.search(p, text))
        if hits:
            scores[qtype] = hits
    if not scores:
        return None
    return max(scores, key=lambda k: scores[k])


# ── Few-shot examples ──────────────────────────────────────────────────────────

_FEW_SHOT = """
Examples:
- Subject "Invoice #1234 incorrect amount" → billing
- Subject "I was charged twice this month" → billing
- Subject "Please refund my payment" → refund
- Subject "App crashes when I click Save" → technical
- Subject "Cannot log in to my account" → technical
- Subject "Your service is absolutely terrible" → complaint
- Subject "How do I get started?" → onboarding
- Subject "What are your opening hours?" → general
""".strip()


# ── Classification ─────────────────────────────────────────────────────────────

def classify_email(subject: str, body: str) -> dict:
    """
    Returns {"query_type": str, "confidence": float, "summary": str}

    response_mime_type="application/json" forces the API to return only a valid
    JSON object — no preamble, no markdown fences, no prose. This works on all
    models including gemma-4-31b-it and requires no ThinkingConfig.
    """
    keyword_type = _keyword_classify(subject, body)

    if not GEMINI_API_KEY:
        qtype = keyword_type or "general"
        return {"query_type": qtype, "confidence": 0.6, "summary": body[:120]}

    # The schema description goes in the prompt; the MIME type enforces the format
    prompt = f"""You are an email classifier for a customer support team.

Classify this email into EXACTLY ONE category: {', '.join(QUERY_TYPES)}

{_FEW_SHOT}

Rules:
- Pick the MOST SPECIFIC category. Use "general" only when nothing else fits.
- Emails about money/charges → billing (unless explicitly a refund request → refund).
- Emails expressing anger/frustration → complaint.
- confidence: float 0.0–1.0 reflecting your certainty.

Return a JSON object with these exact keys: query_type, confidence, summary.

Subject: {subject}
Body: {body[:1500]}"""

    try:
        from google import genai
        from google.genai import types

        client = _get_client()

        response = client.models.generate_content(
            model    = _get_model_name(),
            contents = prompt,
            config   = types.GenerateContentConfig(
                response_mime_type = "application/json",
                temperature        = 0.0,
            )
        )

        text = (response.text or "").strip()

        # Safety: strip any accidental markdown fences
        text = text.replace("```json", "").replace("```", "").strip()

        result = json.loads(text)
        result["confidence"] = float(result.get("confidence", 0.5))

        if result.get("query_type") not in QUERY_TYPES:
            raise ValueError(f"Invalid query_type: {result.get('query_type')!r}")

        # Keyword override when AI is uncertain
        if result["confidence"] < 0.55 and keyword_type and keyword_type != result["query_type"]:
            logger.info(
                f"Low AI confidence ({result['confidence']:.0%}) → "
                f"keyword override: {result['query_type']} → {keyword_type}"
            )
            result["query_type"] = keyword_type
            result["confidence"] = 0.65

        logger.info(f"Classified as '{result['query_type']}' ({result['confidence']:.0%})")
        return result

    except json.JSONDecodeError as exc:
        logger.warning(f"Classification JSON parse error: {exc} → keyword fallback")
    except Exception as exc:
        logger.error(f"Classification error: {type(exc).__name__}: {exc} → keyword fallback")

    qtype = keyword_type or "general"
    confidence = 0.60 if keyword_type else 0.30
    return {"query_type": qtype, "confidence": confidence, "summary": subject}


# ── Draft generation ───────────────────────────────────────────────────────────

def generate_draft(
    sender: str,
    subject: str,
    body: str,
    query_type: str,
    template_body: str | None = None,
) -> str:
    if not GEMINI_API_KEY:
        return (
            f"Dear Customer,\n\n"
            f"Thank you for reaching out regarding: {subject}.\n\n"
            f"[AI generation unavailable — please configure GEMINI_API_KEY]\n\n"
            f"Best regards,\nSupport Team"
        )

    template_instruction = (
        f"\nUse the following template as a structural guide:\n{template_body}"
        if template_body else ""
    )

    # The delimiter is the extraction mechanism: whatever the model writes before
    # ###EMAIL_START### is discarded. Only the content after it is used.
    prompt = f"""You are a professional customer support representative.
Query type: {query_type}{template_instruction}

Rules:
- Write a helpful, empathetic, professional email reply to the client below.
- 3-5 short paragraphs max.
- Do NOT include a subject line.
- End with "Best regards,\nSupport Team".
- Do NOT add placeholder text like [Your Name].
- Output the marker {_DRAFT_DELIMITER} on its own line, then immediately write the email. Nothing after the email.

Client: {sender}
Subject: {subject}
Body:
{body[:2000]}"""

    try:
        from google import genai
        from google.genai import types

        client = _get_client()

        response = client.models.generate_content(
            model    = _get_model_name(),
            contents = prompt,
            config   = types.GenerateContentConfig(
                temperature = 0.7,
            )
        )

        raw = (response.text or "").strip()

        # Extract everything after the delimiter
        if _DRAFT_DELIMITER in raw:
            draft = raw.split(_DRAFT_DELIMITER, 1)[1].strip()
        else:
            # Delimiter not present — model ignored the instruction.
            # Try to find the email by looking for "Dear" as the start of the body.
            m = re.search(r"(Dear\s+\S)", raw)
            draft = raw[m.start():].strip() if m else raw

        return draft

    except Exception as exc:
        logger.error(f"Draft generation error: {type(exc).__name__}: {exc}")
        return (
            f"Dear Customer,\n\n"
            f"Thank you for contacting us. We received your message and will respond shortly.\n\n"
            f"Best regards,\nSupport Team"
        )


# ── Convenience wrapper ────────────────────────────────────────────────────────

def process_email(sender: str, subject: str, body: str, template_body: str | None = None) -> dict:
    """
    Returns {"query_type": str, "confidence": float, "summary": str, "draft": str}
    """
    classification = classify_email(subject, body)
    draft = generate_draft(
        sender        = sender,
        subject       = subject,
        body          = body,
        query_type    = classification["query_type"],
        template_body = template_body,
    )
    return {**classification, "draft": draft}