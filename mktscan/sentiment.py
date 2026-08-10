"""
mktscan/sentiment.py
Sentiment scoring engine.
Supports three backends:
  - finbert  : ProsusAI/finbert (best for financial text, runs locally)
  - vader    : VADER (fast, no GPU needed, less accurate)
  - openai   : GPT-4o-mini (highest quality, costs ~$0.001/article)
"""
from __future__ import annotations
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

# Symmetric bull/bear band.
#
# These used to be +0.3 / -0.1, which is a systematic bearish labelling bias: a
# headline set averaging -0.15 was called BEARISH while its mirror image at +0.15
# was called NEUTRAL. There was no stated rationale for the asymmetry, and it
# propagated into the dashboard's colour coding and into every downstream count
# of "how many tickers are bearish today".
BULL_THRESHOLD = 0.20
BEAR_THRESHOLD = -0.20


def classify_score(score: float) -> str:
    if score >= BULL_THRESHOLD:
        return "BULLISH"
    if score <= BEAR_THRESHOLD:
        return "BEARISH"
    return "NEUTRAL"


# ── FinBERT backend ────────────────────────────────────────────────────────────

class FinBERTScorer:
    """
    Uses ProsusAI/finbert, a BERT model fine-tuned on financial text.
    Outputs positive/negative/neutral probabilities.
    First run downloads ~440MB model weights (cached after that).
    """

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        batch_size: int = 16,
        max_length: int = 512,
    ):
        self.model_name = model_name
        self.batch_size = batch_size
        # config.sentiment.max_text_length was defined but never read.
        self.max_length = int(max_length or 512)
        self._pipe = None

    def _load(self):
        if self._pipe is not None:
            return
        try:
            from transformers import pipeline
            log.info(f"[FinBERT] Loading model {self.model_name} ...")
            self._pipe = pipeline(
                "text-classification",
                model=self.model_name,
                top_k=None,
                truncation=True,
                max_length=self.max_length,
            )
            log.info("[FinBERT] Model ready")
        except ImportError:
            raise RuntimeError(
                "transformers/torch not installed. "
                "Run: pip install transformers torch"
            )

    def score_text(self, text: str) -> float:
        """Score a single text. Returns -1.0 to +1.0."""
        self._load()
        clean = _clean_text(text)
        if not clean:
            return 0.0
        try:
            # The pipeline already truncates to max_length *tokens*; slicing by
            # characters here as well only threw away text the model could have
            # read (512 tokens is roughly 2,000 characters).
            results = self._pipe(clean)[0]
            probs = {r["label"].lower(): r["score"] for r in results}
            return probs.get("positive", 0) - probs.get("negative", 0)
        except Exception as e:
            log.warning(f"[FinBERT] Score failed: {e}")
            return 0.0

    def score_batch(self, texts: list[str]) -> list[float]:
        """
        Score a batch of texts.

        On batch failure this falls back to per-item scoring rather than
        emitting a run of 0.0 — a genuine neutral and a crashed batch were
        previously indistinguishable in the stored data.
        """
        self._load()
        cleaned = [_clean_text(t) for t in texts]
        scores: list[float] = []

        for i in range(0, len(cleaned), self.batch_size):
            batch = cleaned[i:i + self.batch_size]
            try:
                results = self._pipe(batch)
                if len(results) != len(batch):
                    raise ValueError(
                        f"pipeline returned {len(results)} results for {len(batch)} inputs"
                    )
                for item in results:
                    probs = {r["label"].lower(): r["score"] for r in item}
                    scores.append(probs.get("positive", 0) - probs.get("negative", 0))
            except Exception as e:
                log.warning(f"[FinBERT] Batch score failed ({e}); scoring individually")
                scores.extend(self.score_text(t) for t in batch)

        return scores


# ── VADER backend ──────────────────────────────────────────────────────────────

class VADERScorer:
    """
    VADER: fast rule-based sentiment. Good baseline, weaker on financial jargon.
    No download required (just vaderSentiment package).
    """

    def __init__(self):
        self._analyzer = None

    def _load(self):
        if self._analyzer is not None:
            return
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._analyzer = SentimentIntensityAnalyzer()
        except ImportError:
            raise RuntimeError("vaderSentiment not installed. Run: pip install vaderSentiment")

    def score_text(self, text: str) -> float:
        self._load()
        compound = self._analyzer.polarity_scores(_clean_text(text))["compound"]
        # VADER compound is -1 to +1, matches our scale
        return round(compound, 4)

    def score_batch(self, texts: list[str]) -> list[float]:
        return [self.score_text(t) for t in texts]


# ── OpenAI backend ─────────────────────────────────────────────────────────────

class OpenAIScorer:
    """
    Uses GPT-4o-mini for financial sentiment scoring.
    Most accurate but costs money (~$0.001 per article batch).
    """

    SYSTEM_PROMPT = """You are a financial sentiment analyst. Given a list of news headlines,
return a JSON array of sentiment scores, one per headline, in the same order.
Each score is a float from -1.0 (very bearish) to +1.0 (very bullish).
0.0 = neutral. Consider: earnings beats/misses, guidance changes, analyst actions,
product launches, regulatory news, and macro impacts. Return ONLY the JSON array, no explanation."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model   = model
        self._client = None

    def _load(self):
        if self._client is not None:
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key)
        except ImportError:
            raise RuntimeError("openai not installed. Run: pip install openai")

    BATCH_SIZE = 20

    def score_batch(self, texts: list[str]) -> list[float]:
        """
        Score headlines in batches, verifying that the model returned exactly one
        score per input.

        The previous version did ``all_scores.extend(parsed)`` with no length
        check. If the model returned 18 scores for 20 headlines — which happens
        when it merges near-duplicates or the response is truncated — every
        subsequent article in the run was assigned the wrong score, silently and
        permanently. The final ``[:len(texts)]`` slice hid the symptom by making
        the list *look* the right length.

        Now a length mismatch triggers one retry, then falls back to per-item
        scoring for that batch so misalignment cannot propagate.
        """
        self._load()
        if not texts:
            return []

        all_scores: list[float] = []
        for i in range(0, len(texts), self.BATCH_SIZE):
            batch = texts[i:i + self.BATCH_SIZE]
            all_scores.extend(self._score_one_batch(batch))

        assert len(all_scores) == len(texts), "score/text length invariant violated"
        return all_scores

    def _score_one_batch(self, batch: list[str], _retry: bool = True) -> list[float]:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user",
                     "content": json.dumps(batch, ensure_ascii=False)},
                ],
                temperature=0,
                # ~8 tokens per score plus JSON punctuation. The old cap of 200
                # was marginal for 20 items; overflow truncated the JSON, the
                # parse threw, and the entire batch silently became 0.0.
                max_tokens=16 * len(batch) + 64,
                response_format={"type": "json_object"},
            )
            raw    = resp.choices[0].message.content.strip()
            parsed = json.loads(raw)

            # Accept either a bare array or {"scores": [...]}.
            if isinstance(parsed, dict):
                for key in ("scores", "sentiment", "results", "data"):
                    if isinstance(parsed.get(key), list):
                        parsed = parsed[key]
                        break

            if isinstance(parsed, list) and len(parsed) == len(batch):
                return [_clamp(_to_float(s)) for s in parsed]

            got = len(parsed) if isinstance(parsed, list) else "non-list"
            log.warning(
                f"[OpenAI] Expected {len(batch)} scores, got {got}. "
                f"{'Retrying.' if _retry else 'Falling back to per-item scoring.'}"
            )
            if _retry:
                return self._score_one_batch(batch, _retry=False)

        except Exception as e:
            log.error(f"[OpenAI] Batch scoring failed: {e}")
            if _retry:
                return self._score_one_batch(batch, _retry=False)

        # Per-item fallback: slower, but alignment is guaranteed.
        scores: list[float] = []
        for text in batch:
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps([text], ensure_ascii=False)},
                    ],
                    temperature=0,
                    max_tokens=32,
                )
                parsed = json.loads(resp.choices[0].message.content.strip())
                value  = parsed[0] if isinstance(parsed, list) and parsed else 0.0
                scores.append(_clamp(_to_float(value)))
            except Exception:
                scores.append(0.0)
        return scores

    def score_text(self, text: str) -> float:
        return self.score_batch([text])[0]


# ── Factory ────────────────────────────────────────────────────────────────────

def build_scorer(cfg: dict[str, Any]):
    """
    Returns the appropriate scorer based on config.
    If finbert is requested but torch/transformers are unavailable,
    automatically falls back to VADER with a warning.
    """
    model = cfg.get("model", "finbert")

    if model == "finbert":
        try:
            import torch          # noqa: F401
            import transformers   # noqa: F401
            scorer = FinBERTScorer(
                model_name=cfg.get("finbert_model", "ProsusAI/finbert"),
                batch_size=cfg.get("batch_size", 16),
                max_length=cfg.get("max_text_length", 512),
            )
            # Load eagerly so a failure surfaces here, where we can still fall
            # back, rather than on the first scoring call inside the run loop.
            scorer._load()
            log.info("[Sentiment] Using FinBERT scorer (ProsusAI/finbert)")
            return scorer
        except Exception as e:
            # Previously this caught only ImportError, but FinBERTScorer._load
            # raises RuntimeError, and a failed model download raises OSError.
            # Neither was caught, so the "automatic fallback to VADER" advertised
            # in the docstring never actually happened.
            log.warning(
                f"[Sentiment] FinBERT unavailable ({type(e).__name__}: {e}) — "
                "falling back to VADER. Install with: pip install transformers torch"
            )
            return VADERScorer()
    elif model == "vader":
        log.info("[Sentiment] Using VADER scorer")
        return VADERScorer()
    elif model == "openai":
        api_key = cfg.get("openai_api_key", "")
        if not api_key or api_key.startswith("YOUR_"):
            raise ValueError("OpenAI API key not set in config.")
        return OpenAIScorer(api_key=api_key, model=cfg.get("openai_model", "gpt-4o-mini"))
    else:
        raise ValueError(f"Unknown sentiment model: {model}. Use: finbert | vader | openai")


# ── Aggregation ────────────────────────────────────────────────────────────────

def aggregate_scores(
    articles: list[dict],
    scorer,
    source_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Score all articles for a ticker and return an aggregated result.

    source_weights: optional per-source multipliers, e.g.
        {"wsj": 1.5, "benzinga": 1.2, "yahoo": 1.0, "finviz": 0.9}
    """
    if not articles:
        return {
            "score": 0.0,
            "label": "NEUTRAL",
            "article_count": 0,
            "source_breakdown": {},
            "unique_stories": 0,
            "duplicates_collapsed": 0,
        }

    from .database import headline_key

    weights = source_weights or {}

    # ── Collapse duplicate stories before scoring ─────────────────────────────
    # A single wire story republished by six outlets was previously counted six
    # times: it moved the weighted mean six times over, and it made the ticker
    # look like it had six independent sources confirming the same view. We keep
    # one representative per story — preferring the highest-weighted source, so
    # the WSJ copy wins over the aggregator copy — and record how many distinct
    # outlets carried it, which is the honest input to a diversity bonus.
    best_by_story: dict[str, dict] = {}
    outlets_by_story: dict[str, set[str]] = {}
    ordered_keys: list[str] = []

    for article in articles:
        key = headline_key(article.get("headline", "")) or f"__unique__{id(article)}"
        src = article.get("source", "unknown")
        outlets_by_story.setdefault(key, set()).add(src)

        if key not in best_by_story:
            best_by_story[key] = article
            ordered_keys.append(key)
        else:
            incumbent = best_by_story[key]
            if weights.get(src, 1.0) > weights.get(incumbent.get("source", "unknown"), 1.0):
                best_by_story[key] = article

    unique_articles = [best_by_story[k] for k in ordered_keys]
    duplicates      = len(articles) - len(unique_articles)
    if duplicates:
        log.debug(f"[Sentiment] collapsed {duplicates} duplicate stories")

    texts = [
        (a.get("headline", "") + " " + (a.get("body_snippet") or "")).strip()
        for a in unique_articles
    ]

    raw_scores = scorer.score_batch(texts)
    if len(raw_scores) != len(unique_articles):
        log.error(
            f"[Sentiment] scorer returned {len(raw_scores)} scores for "
            f"{len(unique_articles)} articles — padding with neutral"
        )
        raw_scores = (list(raw_scores) + [0.0] * len(unique_articles))[:len(unique_articles)]

    weighted_sum = 0.0
    weight_total = 0.0
    source_breakdown: dict[str, int] = {}

    for article, score in zip(unique_articles, raw_scores):
        article["sentiment"] = round(score, 4)
        key = headline_key(article.get("headline", "")) or ""
        src = article.get("source", "unknown")
        w   = weights.get(src, 1.0)
        weighted_sum += score * w
        weight_total += w
        source_breakdown[src] = source_breakdown.get(src, 0) + 1

        # Propagate the score onto the duplicates too, so they persist with a
        # sentiment value rather than NULL.
        for dup in articles:
            if dup is not article and headline_key(dup.get("headline", "")) == key:
                dup["sentiment"] = round(score, 4)

    final_score = round(weighted_sum / weight_total, 4) if weight_total else 0.0

    # Distinct outlets carrying at least one story — the real diversity measure.
    distinct_outlets = len({s for outlets in outlets_by_story.values() for s in outlets})

    return {
        "score":                final_score,
        "label":                classify_score(final_score),
        "article_count":        len(unique_articles),
        "source_breakdown":     source_breakdown,
        "unique_stories":       len(unique_articles),
        "duplicates_collapsed": duplicates,
        "distinct_outlets":     distinct_outlets,
    }


# ── Utils ──────────────────────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    """Model output is meant to be in [-1, 1]; enforce it rather than trusting it."""
    return max(lo, min(hi, value))
