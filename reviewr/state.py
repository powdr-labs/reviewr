"""Shared, anonymized review state: findings, votes, and convergence logic.

This is the single source of truth the reviewers debate over. Crucially, the
*view* handed back to reviewers is anonymized — a finding carries an id and its
content but never the name of the AI that raised it or the names behind the
votes. This blind-review setup is deliberate: it stops a reviewer from
deferring to (or reflexively attacking) another model's findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .findings import Finding, SEVERITY_ORDER, Stance, Vote


@dataclass
class TrackedFinding:
    id: str
    finding: Finding
    origin: str           # reviewer that raised it (internal only, never shown)
    round_added: int
    # latest vote per reviewer name (internal); the origin auto-agrees.
    votes: dict[str, Vote] = field(default_factory=dict)

    def tally(self) -> tuple[int, int, int]:
        agree = dispute = unsure = 0
        for v in self.votes.values():
            if v.stance == Stance.agree:
                agree += 1
            elif v.stance == Stance.dispute:
                dispute += 1
            else:
                unsure += 1
        return agree, dispute, unsure

    def status(self) -> str:
        """confirmed | rejected | contested, from the current tally."""
        agree, dispute, _ = self.tally()
        if dispute == 0:
            return "confirmed"
        if dispute >= agree:
            return "rejected"
        return "contested"


class ReviewState:
    def __init__(self) -> None:
        self.findings: list[TrackedFinding] = []
        self._counter = 0

    # -- mutation ---------------------------------------------------------

    def add_findings(
        self, reviewer: str, new: list[Finding], round_idx: int
    ) -> list[str]:
        """Append findings from a reviewer; the author auto-agrees. Returns ids."""
        ids = []
        for f in new:
            self._counter += 1
            fid = f"F{self._counter}"
            tf = TrackedFinding(
                id=fid, finding=f, origin=reviewer, round_added=round_idx
            )
            tf.votes[reviewer] = Vote(
                finding_id=fid, stance=Stance.agree, reason="raised this finding"
            )
            self.findings.append(tf)
            ids.append(fid)
        return ids

    def apply_votes(self, reviewer: str, votes: list[Vote]) -> None:
        by_id = {tf.id: tf for tf in self.findings}
        for v in votes:
            tf = by_id.get(v.finding_id)
            if tf is not None:
                tf.votes[reviewer] = v

    # -- views ------------------------------------------------------------

    def active(self, drop_rejected: bool) -> list[TrackedFinding]:
        if not drop_rejected:
            return list(self.findings)
        return [tf for tf in self.findings if tf.status() != "rejected"]

    def anon_view(self, drop_rejected: bool) -> list[dict]:
        """The anonymized findings list handed to reviewers each round."""
        view = []
        for tf in self.active(drop_rejected):
            agree, dispute, unsure = tf.tally()
            f = tf.finding
            view.append(
                {
                    "id": tf.id,
                    "title": f.title,
                    "severity": f.severity.value,
                    "category": f.category.value,
                    "file": f.file,
                    "line": f.line,
                    "description": f.description,
                    "suggested_fix": f.suggested_fix,
                    "tally": {"agree": agree, "dispute": dispute, "unsure": unsure},
                    # anonymized dissent so reviewers can weigh objections
                    "objections": [
                        v.reason
                        for v in tf.votes.values()
                        if v.stance == Stance.dispute and v.reason
                    ],
                }
            )
        return view

    # -- convergence ------------------------------------------------------

    def status_snapshot(self, drop_rejected: bool) -> tuple[tuple[str, str], ...]:
        return tuple(
            (tf.id, tf.status()) for tf in self.active(drop_rejected)
        )

    def has_contested(self, drop_rejected: bool) -> bool:
        return any(tf.status() == "contested" for tf in self.active(drop_rejected))

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "findings": [
                {
                    "id": tf.id,
                    "origin": tf.origin,
                    "round_added": tf.round_added,
                    "status": tf.status(),
                    "tally": dict(zip(("agree", "dispute", "unsure"), tf.tally())),
                    "finding": tf.finding.model_dump(),
                    "votes": {k: v.model_dump() for k, v in tf.votes.items()},
                }
                for tf in self.findings
            ]
        }

    def confirmed_sorted(self) -> list[TrackedFinding]:
        out = [tf for tf in self.findings if tf.status() == "confirmed"]
        out.sort(key=lambda tf: SEVERITY_ORDER[tf.finding.severity])
        return out
