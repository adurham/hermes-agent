"""Adaptive sampling tuner driven by reasoning_content repetition.

Watches the reasoning emitted by the model on each turn, scores how
repetitive it is, and proposes ``frequency_penalty`` /
``presence_penalty`` overrides for the *next* request.

Designed for thinking-heavy local models (DSv4-Flash, Qwen3.5-MoE) where
the visible token stream contains a long ``<think>`` block that often
restates the same facts and lead phrases ("Let me check ...") across
many turns of a tool-calling loop.

Stateless w.r.t. config; per-session state lives on the instance.
``reset()`` clears history at session boundaries.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


_LEAD_PHRASE_RE = re.compile(
    r"\b(?:Let me|Let's|Now let me|I'll|I should|I need to|I will|Now I)\b",
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b")  # "Crown Point", "Little Calumet River"


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


@dataclass
class TunerConfig:
    enabled: bool = True
    fp_max: float = 0.5
    pp_max: float = 0.2
    cold_start_turns: int = 2
    history_window: int = 3  # how many prior turns to compare against
    ema_alpha: float = 0.5
    log_decisions: bool = True


@dataclass
class TurnSnapshot:
    turn_index: int
    tokens: list[str] = field(default_factory=list)
    ngrams_4: set[tuple[str, ...]] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)


class RepetitionTuner:
    """Score reasoning_content per turn and propose sampling penalties."""

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict(config or {})
        self.config = TunerConfig(
            enabled=bool(cfg.get("enabled", True)),
            fp_max=float(cfg.get("fp_max", 0.5)),
            pp_max=float(cfg.get("pp_max", 0.2)),
            cold_start_turns=int(cfg.get("cold_start_turns", 2)),
            history_window=int(cfg.get("history_window", 3)),
            ema_alpha=float(cfg.get("ema_alpha", 0.5)),
            log_decisions=bool(cfg.get("log_decisions", True)),
        )
        self._history: deque[TurnSnapshot] = deque(maxlen=self.config.history_window)
        self._entity_counts: Counter[str] = Counter()
        self._turns_seen = 0
        self._ema_score: float = 0.0
        self._last_decision: dict[str, float] = {}

    def reset(self) -> None:
        self._history.clear()
        self._entity_counts.clear()
        self._turns_seen = 0
        self._ema_score = 0.0
        self._last_decision = {}

    def observe(self, reasoning_content: str | None, *, turn_index: int) -> None:
        if not self.config.enabled or not reasoning_content:
            return
        text = reasoning_content
        tokens = _tokenize(text)
        if len(tokens) < 8:
            return  # not enough signal

        ngrams_4 = _ngrams(tokens, 4)
        entities = set(_ENTITY_RE.findall(text))

        ngram_overlap = self._ngram_overlap(ngrams_4)
        lead_density = self._lead_phrase_density(text, len(tokens))
        intra_rep = self._intra_turn_repetition(tokens)
        fact_score = self._fact_restatement_score(entities)

        score = (
            0.4 * ngram_overlap
            + 0.2 * min(1.0, lead_density / 0.05)  # 5% lead-phrases ≈ saturated
            + 0.2 * intra_rep
            + 0.2 * fact_score
        )
        score = max(0.0, min(1.0, score))

        a = self.config.ema_alpha
        self._ema_score = (a * score) + ((1 - a) * self._ema_score) if self._turns_seen > 0 else score

        self._history.append(
            TurnSnapshot(turn_index=turn_index, tokens=tokens, ngrams_4=ngrams_4, entities=entities)
        )
        for e in entities:
            self._entity_counts[e] += 1
        self._turns_seen += 1

        decision = self._score_to_params(self._ema_score)
        self._last_decision = decision

        if self.config.log_decisions:
            logger.info(
                "[repetition_tuner] turn=%d score=%.2f ema=%.2f "
                "(ngram=%.2f lead=%.2f intra=%.2f fact=%.2f) → fp=%.2f pp=%.2f",
                turn_index,
                score,
                self._ema_score,
                ngram_overlap,
                min(1.0, lead_density / 0.05),
                intra_rep,
                fact_score,
                decision.get("frequency_penalty", 0.0),
                decision.get("presence_penalty", 0.0),
            )

    def suggest(self) -> dict[str, float]:
        if not self.config.enabled:
            return {}
        if self._turns_seen < self.config.cold_start_turns:
            return {}
        return dict(self._last_decision)

    def _ngram_overlap(self, ngrams_now: set) -> float:
        if not self._history or not ngrams_now:
            return 0.0
        prior = set()
        for snap in self._history:
            prior |= snap.ngrams_4
        if not prior:
            return 0.0
        inter = len(ngrams_now & prior)
        union = len(ngrams_now | prior)
        return inter / union if union else 0.0

    def _lead_phrase_density(self, text: str, n_tokens: int) -> float:
        if n_tokens == 0:
            return 0.0
        return len(_LEAD_PHRASE_RE.findall(text)) / n_tokens

    def _intra_turn_repetition(self, tokens: list[str]) -> float:
        if len(tokens) < 4:
            return 0.0
        grams = [tuple(tokens[i : i + 4]) for i in range(len(tokens) - 3)]
        if not grams:
            return 0.0
        counts = Counter(grams)
        repeats = sum(c - 1 for c in counts.values() if c > 1)
        return min(1.0, repeats / len(grams))

    def _fact_restatement_score(self, entities_now: set[str]) -> float:
        if not entities_now or self._turns_seen == 0:
            return 0.0
        prior_emissions = sum(self._entity_counts[e] for e in entities_now)
        if not prior_emissions:
            return 0.0
        return min(1.0, prior_emissions / (4 * len(entities_now)))

    def _score_to_params(self, score: float) -> dict[str, float]:
        fp_max = self.config.fp_max
        pp_max = self.config.pp_max

        if score < 0.2:
            fp = 0.0
            pp = 0.0
        elif score < 0.5:
            t = (score - 0.2) / 0.3
            fp = t * (0.6 * fp_max)
            pp = t * (0.5 * pp_max)
        elif score < 0.8:
            t = (score - 0.5) / 0.3
            fp = (0.6 * fp_max) + t * (0.4 * fp_max)
            pp = (0.5 * pp_max) + t * (0.5 * pp_max)
        else:
            fp = fp_max
            pp = pp_max

        out: dict[str, float] = {}
        if fp > 0.001:
            out["frequency_penalty"] = round(fp, 3)
        if pp > 0.001:
            out["presence_penalty"] = round(pp, 3)
        return out
