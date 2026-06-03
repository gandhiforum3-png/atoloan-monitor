# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

This project is in its initial setup phase — no application code exists yet. The repository contains only the GSD (Get Shit Done) Claude Code workflow tooling under `.claude/`.

## Project Context

**atoloan-monitor** is a monitoring application for Atoloan (Mena Investment Group). The tech stack, architecture, and build commands will be documented here once the project is initialized.

## GSD Workflow

This project uses the GSD skill system. The typical workflow for starting a new project:

```
/gsd:new-project     # initialize PROJECT.md and roadmap
/gsd:plan-phase      # plan a phase before implementing
/gsd:execute-phase   # execute the planned phase
/gsd:verify-work     # validate what was built
```

Use `/gsd:help` for a full command reference.

<!-- GSD:project-start source:PROJECT.md -->
## Project

**Atoloan Monitor**

Atoloan Monitor is an AI-powered SRE (Site Reliability Engineering) agent that continuously watches Atoloan's AWS infrastructure — Kubernetes pods, EC2 instances, FastAPI backend, and PostgreSQL database — and automatically remediates incidents without human intervention, stopping hard at any destructive action. It is built for Atoloan's production stack first, with an architecture designed to generalize into a configurable product for other teams.

**Core Value:** The agent detects, diagnoses, and resolves infrastructure incidents automatically — 24/7 — so engineers are only involved when human judgment is genuinely required.

### Constraints

- **Safety**: No delete, drop, destroy, or terminate operations — ever. Enforced at skill boundary, not orchestrator logic.
- **Auth**: IAM role on dedicated EC2 — no credentials in environment variables or code.
- **Remediation gate**: Auto-remediate only when RCA confidence ≥ 0.8. Below threshold → human escalation with full diagnosis context.
- **Learning period**: 7-day observation-only mode before enforcement activates. Cannot be skipped on first deployment.
- **Scope**: Atoloan's stack (AWS, K8s, FastAPI, Postgres, Docker) — no multi-tenant or generic-infra support in v1.
- **Stack**: Python for agent backend, React for dashboard, Claude API (claude-sonnet-4-6 or claude-opus-4-8 for RCA).
- **K8s scaler pre-flight**: Must check namespace resource quotas and node headroom before any scale-up action.
- **Postgres query killer pre-flight**: Must classify query type (application vs. maintenance) before killing.
<!-- GSD:project-end -->

<!-- GSD:stack-start source:STACK.md -->
## Technology Stack

Technology stack not yet documented. Will populate after codebase mapping or first phase.
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd:quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd:debug` for investigation and bug fixing
- `/gsd:execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd:profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
