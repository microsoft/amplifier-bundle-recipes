# Context Intelligence Recipes

A comprehensive code analysis system built with Amplifier recipes that analyzes codebases through progressively broader lenses, from single files to architectural patterns.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        FULL ANALYSIS WORKFLOW                               │
│                                                                             │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │DISCOVERY │ → │ TIER 1   │ → │ TIER 2   │ → │ TIER 3   │ → │COMPRESS  │  │
│  │Find files│   │Single-   │   │Pairwise  │   │Cross-    │   │Aggregate │  │
│  │& classify│   │file      │   │compare   │   │cutting   │   │findings  │  │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘  │
│                                                                             │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐                 │
│  │SYNTHESTIC│ → │CATEGORIZE│ → │ EXECUTE  │ → │ REPORT   │                 │
│  │Root cause│   │Route to  │   │Auto-fix  │   │Final     │                 │
│  │analysis  │   │buckets   │   │(optional)│   │summary   │                 │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Full repository analysis (standard depth)
amplifier run "execute @recipes:examples/context-intelligence/workflows/full-analysis-workflow.yaml \
  with repo_path=./my-project analysis_depth=standard dry_run=true"

# Quick single-file analysis
amplifier run "execute @recipes:examples/context-intelligence/tier1/single-file-orchestrator.yaml \
  with file_path=./my-project/src/main.py"

# Incremental analysis (for CI/CD)
amplifier run "execute @recipes:examples/context-intelligence/workflows/incremental-analysis.yaml \
  with repo_path=./my-project base_ref=HEAD~1"
```

## Directory Structure

```
context-intelligence/
├── foundation/           # Core utilities
│   ├── file-discovery.yaml      # Discover and classify files
│   ├── context-loader.yaml      # Smart file loading with token limits
│   └── schemas/                 # Standardized output formats
│       └── finding-schema.yaml
├── tier1/                # Single-file analyses
│   ├── comment-code-conflict.yaml    # Docstrings vs actual behavior
│   ├── dead-code-analysis.yaml       # Unreachable/unused code
│   ├── internal-consistency.yaml     # Logical contradictions
│   ├── naming-semantic-mismatch.yaml # Names vs behavior
│   └── single-file-orchestrator.yaml # Runs all Tier 1 analyses
├── tier2/                # Pairwise comparisons
│   ├── doc-code-accuracy.yaml       # Documentation vs implementation
│   ├── semantic-duplicate.yaml      # Code duplication detection
│   └── cross-doc-contradiction.yaml # Conflicting documentation
├── tier3/                # Cross-cutting analysis
│   ├── dry-violation-analysis.yaml     # Repeated code patterns
│   └── architectural-consistency.yaml  # Layer violations, structure
├── verification/         # Adversarial verification
│   ├── claim-verification.yaml      # Single-round verification
│   ├── verification-retry.yaml      # Retry with feedback
│   └── progressive-iteration.yaml   # Multi-round debate
├── compression/          # Finding aggregation
│   ├── compress-file-findings.yaml  # Per-file compression
│   └── aggregate-findings.yaml      # Cross-file aggregation
├── synthesis/            # Decision routing
│   ├── synthesize-findings.yaml     # Root cause analysis
│   ├── categorize-findings.yaml     # Route to teams/processes
│   └── action-executor.yaml         # Apply safe fixes
├── orchestrators/        # Multi-file orchestration
│   └── repo-analysis-orchestrator.yaml
└── workflows/            # Master workflows
    ├── full-analysis-workflow.yaml  # Complete 10-stage pipeline
    └── incremental-analysis.yaml    # CI/CD integration
```

## Analysis Depth Modes

| Mode | Tiers | Duration | Use Case |
|------|-------|----------|----------|
| `quick` | Tier 1 only | ~2 min | Fast PR check |
| `standard` | Tier 1 + 2 | ~10 min | Regular analysis |
| `deep` | Tier 1 + 2 + 3 | ~40 min | Full architectural review |

## What Each Tier Detects

### Tier 1: Single-File Issues
- Docstrings that contradict actual behavior
- Unreachable code, unused functions, stale imports
- Logical contradictions within a file
- Names that don't match behavior (e.g., `get_user()` returns user_id)

### Tier 2: Cross-File Issues
- Documentation that doesn't match implementation
- Code duplication across files
- Conflicting claims in different docs

### Tier 3: Architectural Issues
- Same logic repeated 3+ times across codebase (DRY violations)
- Layer violations, circular dependencies
- Structural drift from intended architecture

## Output

The pipeline produces:
- **Repository health grade** (A-F with numeric score)
- **Prioritized findings** by severity and actionability
- **Root cause analysis** grouping related symptoms
- **Categorized actions** (auto-fixable, quick wins, tech debt, needs discussion)
- **Execution report** (if auto-fix enabled)

## Configuration

Key parameters for `full-analysis-workflow.yaml`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `repo_path` | (required) | Path to repository |
| `analysis_depth` | `standard` | `quick`, `standard`, or `deep` |
| `max_files` | `20` | Maximum files to analyze |
| `dry_run` | `true` | Preview changes without applying |
| `skip_tiers` | `[]` | Tiers to skip (e.g., `["tier2", "tier3"]`) |

## Recipe Count: 24

| Category | Count |
|----------|-------|
| Foundation | 3 |
| Tier 1 | 5 |
| Tier 2 | 3 |
| Tier 3 | 2 |
| Verification | 3 |
| Compression | 2 |
| Synthesis | 3 |
| Orchestrators | 1 |
| Workflows | 2 |
