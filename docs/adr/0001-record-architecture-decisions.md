# 1. Record architecture decisions

**Status:** accepted · 2026-09

## Context

The interesting part of a small tool is rarely the code; it is the handful of
choices that constrain it. Those choices are invisible six months later, and
the person who inherits the repository re-litigates them, or worse, "fixes"
one without knowing what it was protecting against.

## Decision

Every decision that closes off an alternative gets a short record here:
context, the decision, and what it costs. Numbered, immutable — a decision that
gets reversed is superseded by a new record, not edited.

## Consequences

Four more records exist because of this one. Each is short on purpose; an ADR
nobody reads is as useless as no ADR.
