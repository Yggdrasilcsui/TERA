"""
TERA Meta Logic
----------------
A domain-agnostic stability gate for time-evolving informational systems.

This file implements only the meta-logic / feedback loop.
It deliberately avoids any domain-specific example.
Domain users must provide the required hooks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Protocol, Sequence, Tuple


class Gate(IntEnum):
    REJECT = -1
    WAIT = 0
    ACCEPT = 1


@dataclass(frozen=True)
class TERAConfig:
    # Acceptance / rejection thresholds for the tension metric.
    tau_accept: float
    tau_reject: float

    # Bounded-drift coefficients.
    kappa: float
    c: float

    # Informational energy weights.
    alpha: float
    beta: float
    gamma: float

    # Tension weights.
    alpha_s: float
    beta_s: float

    # Base coupling strength.
    eta0: float

    # Context forensics.
    max_depth: int = 2
    probe_budget: int = 8
    epsilon_stability: float = 1e-9

    # User-provided context set. Each item should be a context descriptor.
    context_set: Sequence[Any] = field(default_factory=tuple)

    # Decision aggregation thresholds.
    accept_ratio: float = 0.70
    reject_ratio: float = 0.50

    # WAIT damping factor used after an undecided step.
    wait_damping: float = 0.25


@dataclass
class TERAResult:
    gate: Gate
    drift_ok: bool
    info_energy_now: float
    info_energy_next: float
    delta_info_energy: float
    tension: float
    effective_coupling: float
    context_votes: Mapping[Gate, int]
    diagnostics: Mapping[str, Any]


class TERADomain(ABC):
    """
    Domain interface for TERA.
    Implementations connect the meta-logic to a real model, sensor system,
    simulator, or inference engine.
    """

    @abstractmethod
    def default_context(self) -> Any:
        """Return the baseline context."""
        raise NotImplementedError

    @abstractmethod
    def reference_belief(self, context: Any) -> Any:
        """Return the reference belief state b* for a context."""
        raise NotImplementedError

    @abstractmethod
    def reference_state(self, context: Any) -> Any:
        """Return the reference informational state rho* for a context."""
        raise NotImplementedError

    @abstractmethod
    def belief_update(self, belief: Any, control: Any, observation_next: Any, context: Any) -> Any:
        """Return the next belief state."""
        raise NotImplementedError

    @abstractmethod
    def kl_divergence(self, belief: Any, reference_belief: Any) -> float:
        """Return D_KL(b || b*)."""
        raise NotImplementedError

    @abstractmethod
    def entropy(self, belief: Any) -> float:
        """Return the uncertainty / entropy term H(b)."""
        raise NotImplementedError

    @abstractmethod
    def constraint_penalty(self, belief: Any, context: Any) -> float:
        """Return the structural constraint penalty R(b, context)."""
        raise NotImplementedError

    @abstractmethod
    def intrinsic_operator(self, rho: Any, context: Any) -> Any:
        """Return the intrinsic drift operator L(rho)."""
        raise NotImplementedError

    @abstractmethod
    def projector(self, rho: Any, context: Any) -> Any:
        """Return Pi(rho), the structural projection."""
        raise NotImplementedError

    @abstractmethod
    def frobenius_norm(self, value: Any) -> float:
        """Return a norm compatible with the domain representation."""
        raise NotImplementedError

    @abstractmethod
    def subtract(self, left: Any, right: Any) -> Any:
        """Return left - right for domain values."""
        raise NotImplementedError

    @abstractmethod
    def add(self, left: Any, right: Any) -> Any:
        """Return left + right for domain values."""
        raise NotImplementedError

    @abstractmethod
    def scale(self, value: Any, scalar: float) -> Any:
        """Return scalar * value for domain values."""
        raise NotImplementedError

    @abstractmethod
    def modulation(self, rho: Any, context: Any) -> Any:
        """Return the modulation term m(rho, context)."""
        raise NotImplementedError

    @abstractmethod
    def time_step(self, context: Any) -> float:
        """Return the effective time scale dt."""
        raise NotImplementedError

    def catastrophic_constraint(self, penalty_value: float, context: Any) -> bool:
        """
        Optional policy hook.
        Override if the domain needs a context-dependent catastrophe threshold.
        """
        return False

    def deepen_contexts(self, config: TERAConfig, depth: int) -> Sequence[Any]:
        """
        Optional hook for recursive forensics.
        Default behavior reuses the same context set.
        """
        return config.context_set


class TERAEngine:
    """
    Domain-agnostic implementation of TERA meta-logic.
    """

    def __init__(self, domain: TERADomain, config: TERAConfig) -> None:
        self.domain = domain
        self.config = config

    # ------------------------------------------------------------------
    # Core metrics
    # ------------------------------------------------------------------

    def info_energy(self, belief: Any, context: Any) -> float:
        b_star = self.domain.reference_belief(context)
        return (
            self.config.alpha * self.domain.kl_divergence(belief, b_star)
            + self.config.beta * self.domain.entropy(belief)
            + self.config.gamma * self.domain.constraint_penalty(belief, context)
        )

    def tension(self, rho: Any, context: Any) -> float:
        l_rho = self.domain.intrinsic_operator(rho, context)
        projected = self.domain.projector(rho, context)
        return (
            self.config.alpha_s * self.domain.frobenius_norm(l_rho)
            + self.config.beta_s * self.domain.frobenius_norm(
                self.domain.subtract(rho, projected)
            )
        )

    def gate_from_tension(self, tension_value: float) -> Gate:
        if tension_value <= self.config.tau_accept:
            return Gate.ACCEPT
        if tension_value >= self.config.tau_reject:
            return Gate.REJECT
        return Gate.WAIT

    def effective_coupling(self, tension_value: float, gate: Gate) -> float:
        if gate == Gate.REJECT:
            return 0.0
        return self.config.eta0 / (1.0 + tension_value)

    def bounded_drift(self, belief_now: Any, belief_next: Any, context: Any) -> Tuple[bool, float, float, float]:
        i_now = self.info_energy(belief_now, context)
        i_next = self.info_energy(belief_next, context)
        delta_i = i_next - i_now
        ok = delta_i <= (self.config.kappa * i_now + self.config.c)
        return ok, i_now, i_next, delta_i

    # ------------------------------------------------------------------
    # Context / forensic layer
    # ------------------------------------------------------------------

    def _sample_contexts(self, context_set: Sequence[Any]) -> Sequence[Any]:
        if len(context_set) <= self.config.probe_budget:
            return context_set
        return context_set[: self.config.probe_budget]

    def context_forensics(
        self,
        belief: Any,
        rho: Any,
        control: Any,
        observation_next: Any,
        depth: int = 0,
    ) -> Tuple[Gate, Dict[Gate, int]]:
        if depth >= self.config.max_depth:
            return Gate.WAIT, {Gate.ACCEPT: 0, Gate.WAIT: 1, Gate.REJECT: 0}

        contexts = self.domain.deepen_contexts(self.config, depth)
        sampled_contexts = self._sample_contexts(contexts)

        votes: Dict[Gate, int] = {Gate.ACCEPT: 0, Gate.WAIT: 0, Gate.REJECT: 0}

        for ctx in sampled_contexts:
            belief_next = self.domain.belief_update(belief, control, observation_next, ctx)

            penalty = self.domain.constraint_penalty(belief_next, ctx)
            if self.domain.catastrophic_constraint(penalty, ctx):
                votes[Gate.REJECT] += 1
                continue

            drift_ok, _, _, _ = self.bounded_drift(belief, belief_next, ctx)
            s_value = self.tension(rho, ctx)
            local_gate = self.gate_from_tension(s_value)

            if not drift_ok:
                votes[Gate.REJECT if local_gate == Gate.REJECT else Gate.WAIT] += 1
            else:
                votes[local_gate] += 1

        total_votes = sum(votes.values())
        if total_votes == 0:
            return Gate.WAIT, votes

        accept_ratio = votes[Gate.ACCEPT] / total_votes
        reject_ratio = votes[Gate.REJECT] / total_votes

        if reject_ratio >= self.config.reject_ratio:
            return Gate.REJECT, votes
        if accept_ratio >= self.config.accept_ratio:
            return Gate.ACCEPT, votes

        # If the context verdict is not invariant enough, recurse.
        return self.context_forensics(
            belief=belief,
            rho=rho,
            control=control,
            observation_next=observation_next,
            depth=depth + 1,
        )

    # ------------------------------------------------------------------
    # Master step
    # ------------------------------------------------------------------

    def step(self, belief: Any, rho: Any, control: Any, observation_next: Any) -> Tuple[Any, Any, TERAResult]:
        ctx0 = self.domain.default_context()

        # 1) Informational update.
        belief_next = self.domain.belief_update(belief, control, observation_next, ctx0)

        # 2) Drift check.
        drift_ok, i_now, i_next, delta_i = self.bounded_drift(belief, belief_next, ctx0)

        # 3) Tension gate.
        s_value = self.tension(rho, ctx0)
        gate0 = self.gate_from_tension(s_value)

        # 4) Context forensics across admissible contexts.
        gate, votes = self.context_forensics(
            belief=belief,
            rho=rho,
            control=control,
            observation_next=observation_next,
            depth=0,
        )

        # 5) Effective coupling.
        eta = self.effective_coupling(s_value, gate)

        # 6) Informational state update.
        dt = self.domain.time_step(ctx0)
        rho_star = self.domain.reference_state(ctx0)
        projected = self.domain.projector(rho, ctx0)

        correction = self.domain.scale(
            self.domain.add(
                self.domain.subtract(rho_star, rho),
                self.domain.subtract(rho, projected),
            ),
            eta,
        )

        rho_next = self.domain.add(
            rho,
            self.domain.scale(
                self.domain.add(
                    self.domain.add(
                        self.domain.intrinsic_operator(rho, ctx0),
                        self.domain.modulation(rho, ctx0),
                    ),
                    correction,
                ),
                dt,
            ),
        )

        # 7) Final clamp behavior.
        if gate == Gate.REJECT:
            rho_next = projected
        elif gate == Gate.WAIT:
            rho_delta = self.domain.subtract(rho_next, rho)
            rho_next = self.domain.add(rho, self.domain.scale(rho_delta, self.config.wait_damping))

        result = TERAResult(
            gate=gate,
            drift_ok=drift_ok,
            info_energy_now=i_now,
            info_energy_next=i_next,
            delta_info_energy=delta_i,
            tension=s_value,
            effective_coupling=eta,
            context_votes=votes,
            diagnostics={
                "baseline_gate": gate0,
                "baseline_context": ctx0,
            },
        )
        return belief_next, rho_next, result

    # ------------------------------------------------------------------
    # Feedback loop
    # ------------------------------------------------------------------

    def run(
        self,
        belief0: Any,
        rho0: Any,
        data_stream: Iterable[Tuple[Any, Any]],
    ) -> Iterable[Tuple[Any, Any, TERAResult]]:
        belief = belief0
        rho = rho0

        for control, observation_next in data_stream:
            belief, rho, result = self.step(belief, rho, control, observation_next)
            yield belief, rho, result
