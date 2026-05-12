"""
app/ai/generator.py  –  Gemini / Gemma 4 AI layer

Classification: uses response_mime_type="application/json" for Gemini models only.
Gemma models (gemma-*) do NOT support this parameter and return 500 INTERNAL if
it is set — for those, JSON output is enforced via prompt instruction instead.

Draft generation: uses a hard delimiter (###EMAIL_START###) in the prompt so
that even if the model writes preamble, we slice it off exactly. Keyword
fallback always runs in parallel for classification resilience.

Model fallback hierarchy (per-call):
  1. Primary   — gemma-4-31b-it  (or GEMINI_MODEL setting)
  2. Secondary — gemma-4-26b-a4b-it  (Gemma 4 MoE variant, lighter)
  2b. Tertiary — gemini-2.0-flash     (Gemini Flash, widely available)
  3. Tertiary  — keyword-only    (no API call, classification only)

Draft generation falls back through the same model chain; the tertiary
fallback produces a generic canned reply.
"""

import os
import re
import json
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemma-4-31b-it")

# Ordered fallback chain: primary → secondary → tertiary → (keyword only for classification)
# All strings are verified Gemini API model identifiers (v1beta).
#   gemma-4-31b-it      — Gemma 4 31B Dense (primary default)
#   gemma-4-26b-a4b-it  — Gemma 4 26B MoE, lighter; same generation
#   gemini-2.0-flash    — Gemini Flash; reliable, widely available last resort
_MODEL_FALLBACK_CHAIN = [
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
    "gemini-2.0-flash",
]

_DRAFT_DELIMITER = "###EMAIL_START###"


# ── SDK helpers ────────────────────────────────────────────────────────────────

def _get_client():
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


def _get_primary_model() -> str:
    """Return the configured primary model name (DB setting → env → default)."""
    try:
        from app.db.models import get_setting
        val = get_setting("GEMINI_MODEL")
        if val:
            return val
    except Exception:
        pass
    return _DEFAULT_MODEL


def _build_fallback_chain(primary: str) -> list[str]:
    """
    Build the ordered model list for a call attempt.

    The configured primary is always first.  The rest of _MODEL_FALLBACK_CHAIN
    follows (deduped, primary excluded so it doesn't appear twice).
    """
    chain = [primary]
    for m in _MODEL_FALLBACK_CHAIN:
        if m != primary:
            chain.append(m)
    return chain


# ── Query type catalogue ────────────────────────────────────────────────────────

# Built-in query types — always present.
_BUILTIN_QUERY_TYPES = ["billing", "technical", "general", "complaint", "refund", "onboarding"]


def get_all_query_types() -> list[str]:
    """
    Return the combined list of built-in + custom query types.
    Custom types come from the custom_query_types table (added by migration).
    Falls back gracefully if the table does not exist yet.
    """
    types = list(_BUILTIN_QUERY_TYPES)
    try:
        from app.db.models import get_custom_query_types
        custom = get_custom_query_types()
        for qt in custom:
            name = qt.get("name", "").strip().lower()
            if name and name not in types:
                types.append(name)
    except Exception as exc:
        logger.warning(f"Could not load custom query types: {exc}")
    return types


# Mutable keyword rules — starts with built-ins, custom rules appended at runtime.
_BUILTIN_KEYWORD_RULES: list[tuple[str, list[str]]] = [
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


def _get_keyword_rules() -> list[tuple[str, list[str]]]:
    """
    Return built-in keyword rules merged with any custom rules stored in DB.
    Custom keywords are stored as comma-separated plain words; we convert each
    word to a word-boundary regex pattern automatically.
    """
    rules = list(_BUILTIN_KEYWORD_RULES)
    try:
        from app.db.models import get_custom_query_types
        custom = get_custom_query_types()
        for qt in custom:
            name = qt.get("name", "").strip().lower()
            keywords_raw = qt.get("keywords", "") or ""
            if not name:
                continue
            # Parse comma/newline-separated keyword list
            words = [w.strip().lower() for w in re.split(r"[,\n]+", keywords_raw) if w.strip()]
            if not words:
                continue
            patterns = [r"\b" + re.escape(w) + r"\b" for w in words]
            # Merge with existing entry for this type if it exists
            for i, (qt_name, existing_patterns) in enumerate(rules):
                if qt_name == name:
                    rules[i] = (name, existing_patterns + patterns)
                    break
            else:
                rules.append((name, patterns))
    except Exception as exc:
        logger.warning(f"Could not load custom keyword rules: {exc}")
    return rules


def _keyword_classify(subject: str, body: str) -> str | None:
    text = (subject + " " + body).lower()
    scores: dict[str, int] = {}
    for qtype, patterns in _get_keyword_rules():
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


# ── Internal API call helpers with per-model error handling ───────────────────

def _model_supports_json_mime(model: str) -> bool:
    """
    Return True only for Gemini (non-Gemma) models that support
    response_mime_type="application/json" without raising a 500.

    Gemma models (gemma-*) do NOT support this config parameter — passing it
    causes a consistent 500 INTERNAL error from the API server regardless of
    prompt content or quota.  The fix is to omit the parameter entirely for
    Gemma models and rely on prompt-level JSON instructions instead.
    """
    return not model.lower().startswith("gemma")


def _call_model_json(model: str, prompt: str) -> dict:
    """
    Call the Gemini API and return a parsed JSON dict.

    For Gemini models: uses response_mime_type="application/json" (guaranteed
    valid JSON output, no preamble).

    For Gemma models: omits response_mime_type (avoids 500 INTERNAL), appends
    an explicit JSON-only instruction to the prompt, then strips any markdown
    fences before parsing.

    Raises an exception on any failure (caller handles fallback).
    """
    from google import genai
    from google.genai import types

    client = _get_client()

    if _model_supports_json_mime(model):
        # Gemini family: native JSON mode
        response = client.models.generate_content(
            model    = model,
            contents = prompt,
            config   = types.GenerateContentConfig(
                response_mime_type = "application/json",
                temperature        = 0.0,
            )
        )
    else:
        # Gemma family: plain text mode, JSON enforced via prompt
        gemma_prompt = (
            prompt
            + "\n\nIMPORTANT: Your entire response must be a single valid JSON object."
              " Do NOT include any text, explanation, or markdown before or after the JSON."
        )
        response = client.models.generate_content(
            model    = model,
            contents = gemma_prompt,
            config   = types.GenerateContentConfig(
                temperature = 0.0,
            )
        )

    text = (response.text or "").strip()
    # Strip markdown fences in case the model wraps the JSON anyway
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text).rstrip("`").strip()
    return json.loads(text)


def _call_model_text(model: str, prompt: str) -> str:
    """
    Call the Gemini API for plain text generation on a specific model.
    Raises an exception on any failure (caller handles fallback).
    """
    from google import genai
    from google.genai import types

    client = _get_client()
    response = client.models.generate_content(
        model    = model,
        contents = prompt,
        config   = types.GenerateContentConfig(
            temperature = 0.7,
        )
    )
    return (response.text or "").strip()


# ── Classification ─────────────────────────────────────────────────────────────

def classify_email(subject: str, body: str) -> dict:
    """
    Returns {"query_type": str, "confidence": float, "summary": str}

    Tries each model in the fallback chain in order.  If all API calls fail,
    falls back to keyword classification (tertiary).
    """
    query_types = get_all_query_types()
    keyword_type = _keyword_classify(subject, body)

    if not GEMINI_API_KEY:
        qtype = keyword_type or "general"
        return {"query_type": qtype, "confidence": 0.6, "summary": body[:120]}

    prompt = f"""You are an email classifier for a customer support team.

Classify this email into EXACTLY ONE category: {', '.join(query_types)}

{_FEW_SHOT}

Rules:
- Pick the MOST SPECIFIC category. Use "general" only when nothing else fits.
- Emails about money/charges → billing (unless explicitly a refund request → refund).
- Emails expressing anger/frustration → complaint.
- confidence: float 0.0–1.0 reflecting your certainty.

Return a JSON object with these exact keys: query_type, confidence, summary.

Subject: {subject}
Body: {body[:1500]}"""

    primary = _get_primary_model()
    chain   = _build_fallback_chain(primary)

    for model in chain:
        try:
            result = _call_model_json(model, prompt)

            if model != primary:
                logger.info(f"Classification: used fallback model '{model}'")

            result["confidence"] = float(result.get("confidence", 0.5))

            if result.get("query_type") not in query_types:
                raise ValueError(f"Invalid query_type returned: {result.get('query_type')!r}")

            # Keyword override when AI is uncertain
            if result["confidence"] < 0.55 and keyword_type and keyword_type != result["query_type"]:
                logger.info(
                    f"Low AI confidence ({result['confidence']:.0%}) → "
                    f"keyword override: {result['query_type']} → {keyword_type}"
                )
                result["query_type"] = keyword_type
                result["confidence"] = 0.65

            logger.info(f"Classified as '{result['query_type']}' ({result['confidence']:.0%}) via '{model}'")
            return result

        except json.JSONDecodeError as exc:
            logger.warning(f"[{model}] Classification JSON parse error: {exc} — trying next model")
        except Exception as exc:
            logger.warning(f"[{model}] Classification error: {type(exc).__name__}: {exc} — trying next model")

    # Tertiary fallback: keyword only
    logger.error("All models failed for classification — using keyword fallback (tertiary).")
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
    """
    Generate the *body-only* content of the reply — no greeting line, no sign-off.

    The template wrapper (Dear {{customer_name}} … Best regards, Support Team)
    is applied separately by the orchestrator, so the AI must produce only the
    substantive middle paragraphs.  This avoids double greetings/endings when a
    template is in use, and keeps the output clean when no template is present.
    """
    if not GEMINI_API_KEY:
        return (
            "Thank you for reaching out regarding your query.\n\n"
            "[AI generation unavailable — please configure GEMINI_API_KEY]"
        )

    template_instruction = (
        f"\nThe following template structure will wrap your response — "
        f"do NOT replicate its greeting or sign-off:\n{template_body}"
        if template_body else ""
    )

    # The AI produces ONLY the substantive body paragraphs.
    # The template (or the orchestrator) supplies the greeting and sign-off.
    prompt = f"""You are a professional customer support representative composing an email reply.
Query type: {query_type}{template_instruction}

CRITICAL RULES — read carefully:
- Write ONLY the substantive body paragraphs of the reply.
- Do NOT include any greeting line (e.g. "Dear ...", "Hi ...", "Hello ...").
- Do NOT include any sign-off, closing, or signature (e.g. "Best regards", "Kind regards", "Sincerely", "Support Team", "Thanks").
- A greeting and sign-off are added automatically — duplicating them will break the email.
- 2–4 short paragraphs maximum.
- Be helpful, empathetic, and professional.
- Do NOT add placeholder text like [Your Name] or [reference number].
- Output the marker {_DRAFT_DELIMITER} on its own line, then immediately write the body paragraphs. Nothing after the paragraphs.

Client email address: {sender}
Subject: {subject}
Client message:
{body[:2000]}"""

    primary = _get_primary_model()
    chain   = _build_fallback_chain(primary)

    for model in chain:
        try:
            raw = _call_model_text(model, prompt)

            if model != primary:
                logger.info(f"Draft generation: used fallback model '{model}'")

            # Extract everything after the delimiter
            if _DRAFT_DELIMITER in raw:
                draft = raw.split(_DRAFT_DELIMITER, 1)[1].strip()
            else:
                # Delimiter absent — try to strip any preamble heuristically.
                # Remove a leading greeting line if the model ignored instructions.
                draft = re.sub(
                    r"^(Dear\s+\S[^\n]*\n+|Hi\s+\S[^\n]*\n+|Hello\s+\S[^\n]*\n+)",
                    "", raw, flags=re.IGNORECASE
                ).strip()
                # Remove a trailing sign-off block if present.
                draft = re.sub(
                    r"\n+(Best regards|Kind regards|Sincerely|Warm regards|Regards|Thank you)[,\s]*\n.*$",
                    "", draft, flags=re.IGNORECASE | re.DOTALL
                ).strip()

            logger.success(f"Draft generated via '{model}'")
            return draft

        except Exception as exc:
            logger.warning(f"[{model}] Draft generation error: {type(exc).__name__}: {exc} — trying next model")

    # Tertiary fallback: canned reply
    logger.error("All models failed for draft generation — returning canned response.")
    return (
        "Thank you for contacting us. We have received your message and a member of "
        "our team will review it shortly.\n\n"
        "We apologise for any inconvenience and will get back to you as soon as possible."
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
