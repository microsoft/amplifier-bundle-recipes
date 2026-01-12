# Examples Catalog

**Browse working recipe examples**

This catalog describes all example recipes included in the collection. Each example demonstrates different patterns and use cases.

## How to Run Recipes

**In a session (recommended):** Just ask naturally - "run the code-review recipe on src/auth.py"

**From CLI:** Use `amplifier tool invoke recipes`:
```bash
amplifier tool invoke recipes operation=execute recipe_path=<recipe.yaml> context='{"key": "value"}'
```

> **Note**: There is no `amplifier recipes` CLI command. Recipes are invoked via the `recipes` tool.

---

## Quick Reference

| Recipe | Domain | Steps | Key Pattern | Agents Used |
|--------|--------|-------|-------------|-------------|
| [simple-analysis](#simple-analysis) | Tutorial | 3 | Sequential basics | zen-architect |
| [bash-step-example](#bash-step-example) | Tutorial | 7 | Bash steps | (none - bash only) |
| [conditional-workflow](#conditional-workflow) | Tutorial | 4 | Conditional routing | zen-architect |
| [code-review](#code-review) | Code Quality | 4 | Multi-mode agents | zen-architect |
| [comprehensive-review](#comprehensive-review) | Code Quality | 3 | Recipe composition | zen-architect, security-guardian |
| [security-audit](#security-audit) | Security | 4 | Domain specialist | security-guardian, zen-architect |
| [test-generation](#test-generation) | Testing | 3 | Artifact generation | zen-architect, test-coverage |
| [dependency-upgrade](#dependency-upgrade) | DevOps | 4 | Planning workflow | zen-architect, integration-specialist |
| [dependency-upgrade-staged](#dependency-upgrade-staged) | DevOps | 5 stages | Approval gates | zen-architect, integration-specialist |
| [multi-file-analysis](#multi-file-analysis) | Analysis | 2 | Parallel foreach | zen-architect |
| [parallel-analysis](#parallel-analysis) | Analysis | 2 | Multi-perspective | zen-architect |
| [repo-activity-analysis](#repo-activity-analysis) | GitHub | 6 | Bash + agent hybrid | zen-architect |
| [multi-repo-activity-report](#multi-repo-activity-report) | GitHub | 3 | Recipe invocation | zen-architect |
| [feature-announcement](#feature-announcement) | Communication | 3 | Human-first output | zen-architect |
| [context-intelligence](#context-intelligence) | Advanced | varies | Tiered complexity | various |

---

## Tutorial Examples

### Simple Analysis

**File:** `examples/simple-analysis-recipe.yaml`

Basic analysis workflow demonstrating core recipe concepts: context variables, sequential steps, and output accumulation.

**Use Cases:**
- Learning recipe basics
- Template for new recipes
- Simple file analysis

**Workflow:**
```
1. Extract Summary → 3-sentence summary
2. Identify Key Points → 3-5 key points
3. Generate Report → Markdown report
```

**Example Usage:**
```
In session: "run simple-analysis on README.md"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=simple-analysis-recipe.yaml \
       context='{"file_path": "README.md"}'
```

**Key Learnings:**
- Context variables with `{{variable}}` syntax
- Sequential step execution
- Output chaining between steps

---

### Bash Step Example

**File:** `examples/bash-step-example.yaml`

Demonstrates bash step capabilities for direct shell execution without LLM overhead.

**Use Cases:**
- System commands in workflows
- Environment variable handling
- Exit code-based conditionals
- Working directory control

**Workflow:**
```
1. Simple Echo → Basic command
2. Environment Variables → Variable passing
3. Multi-line Command → System info
4. Check Command → Exit code capture
5. Report Success → Conditional step
6. List Tmp → Working directory
7. Summary → Results compilation
```

**Example Usage:**
```
In session: "run bash-step-example with project_name MyProject"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=bash-step-example.yaml \
       context='{"project_name": "MyProject"}'
```

**Key Learnings:**
- `type: "bash"` for shell commands (no LLM cost)
- Environment variables via `env:` section
- Exit code capture with `output_exit_code`
- Conditional steps based on exit codes
- Working directory control with `cwd:`

---

### Conditional Workflow

**File:** `examples/conditional-workflow.yaml`

Demonstrates conditional step execution based on classification results.

**Use Cases:**
- Dynamic workflow routing
- Complexity-based processing
- Skip unnecessary steps

**Workflow:**
```
1. Classify → Determine simple vs complex
2a. Simple Process → (if simple) Direct processing
2b. Complex Analyze → (if complex) Detailed analysis
2c. Complex Synthesize → (if complex) Synthesis
3. Report → Final summary
```

**Example Usage:**
```
In session: "run conditional-workflow on this complex dataset"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=conditional-workflow.yaml \
       context='{"input_data": "complex dataset with multiple dimensions"}'
```

**Key Learnings:**
- `condition:` field for conditional execution
- Classification-based routing
- Multiple paths through workflow

---

## Code Quality Examples

### Code Review

**File:** `examples/code-review-recipe.yaml`

Multi-stage code review workflow with conditional execution based on issue severity.

**Use Cases:**
- Pre-merge code review
- Architecture assessment
- Technical debt identification

**Workflow:**
```
1. Analyze Structure → Extract patterns, complexity
2. Identify Issues → Find problems, assess severity
3. Suggest Improvements → (if issues found) Design solutions
4. Validate Suggestions → (if critical/high) Deep validation
```

**Example Usage:**
```
In session: "run code-review on src/auth.py"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=code-review-recipe.yaml \
       context='{"file_path": "src/auth.py"}'
```

**Key Learnings:**
- Multi-mode agent usage (ANALYZE, ARCHITECT, REVIEW)
- Severity-based conditional execution
- Progressive refinement pattern

---

### Comprehensive Review

**File:** `examples/comprehensive-review.yaml`

Combines code review and security audit using recipe composition.

**Use Cases:**
- Combined quality and security analysis
- Modular workflow design
- DRY principle (reuse existing recipes)

**Workflow:**
```
1. Code Review (sub-recipe) → Invoke code-review-recipe.yaml
2. Security Audit (sub-recipe) → Invoke security-audit-recipe.yaml
3. Synthesize → Unified prioritized action plan
```

**Example Usage:**
```
In session: "run comprehensive-review on src/api.py"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=comprehensive-review.yaml \
       context='{"file_path": "src/api.py"}'
```

**Key Learnings:**
- Recipe composition with `type: "recipe"`
- Context isolation for sub-recipes
- Recursion protection with `max_depth`

---

## Security Examples

### Security Audit

**File:** `examples/security-audit-recipe.yaml`

Comprehensive security analysis covering vulnerabilities, configurations, and best practices.

**Use Cases:**
- Pre-production security review
- Vulnerability assessment
- Compliance checks

**Workflow:**
```
1. Vulnerability Scan → Static analysis
2. Configuration Review → Security settings
3. Dependency Audit → Vulnerable dependencies
4. Synthesize Findings → Prioritized remediation
```

**Example Usage:**
```
In session: "run security-audit on src/api.py"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=security-audit-recipe.yaml \
       context='{"file_path": "src/api.py", "severity_threshold": "medium"}'
```

**Key Learnings:**
- Domain specialist agent (security-guardian)
- Multi-perspective security analysis
- Actionable remediation output

---

## Testing Examples

### Test Generation

**File:** `examples/test-generation-recipe.yaml`

Analyze code and generate comprehensive test suite.

**Use Cases:**
- Adding tests to legacy code
- Improving test coverage
- TDD verification

**Workflow:**
```
1. Analyze Code Structure → Understand code
2. Design Test Strategy → Determine test approach
3. Generate Test Code → Create actual tests
```

**Example Usage:**
```
In session: "generate tests for src/utils.py using pytest"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=test-generation-recipe.yaml \
       context='{"file_path": "src/utils.py", "test_framework": "pytest"}'
```

**Key Learnings:**
- Specialized agent (test-coverage)
- Artifact generation (ready-to-use test code)
- Configurable output (framework, coverage target)

---

## DevOps Examples

### Dependency Upgrade

**File:** `examples/dependency-upgrade-recipe.yaml`

Systematic dependency upgrade workflow (flat, non-staged).

**Use Cases:**
- Monthly dependency updates
- Security vulnerability remediation
- Quick upgrade planning

**Workflow:**
```
1. Audit Current Dependencies → List versions, check updates
2. Plan Upgrade Strategy → Determine order, identify risks
3. Validate Compatibility → Check breaking changes
4. Generate Upgrade Commands → Ready-to-run commands
```

**Example Usage:**
```
In session: "run dependency-upgrade for this project using pip"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=dependency-upgrade-recipe.yaml \
       context='{"project_path": ".", "package_manager": "pip"}'
```

**Key Learnings:**
- Domain-specific agent (integration-specialist)
- Risk assessment in planning
- Actionable output (executable commands)

---

### Dependency Upgrade Staged

**File:** `examples/dependency-upgrade-staged-recipe.yaml`

Dependency upgrade with approval gates between phases.

**Use Cases:**
- High-stakes production upgrades
- Phased rollout (security → minor → major)
- Human-in-loop verification

**Workflow (Staged):**
```
Stage 1: Assessment (no approval)
  → Audit dependencies, plan strategy

Stage 2: Validation (approval required)
  → Check compatibility

Stage 3: Phase 1 - Critical (approval required)
  → Security fixes only

Stage 4: Phase 2 - Minor (approval required)
  → Minor version updates

Stage 5: Phase 3 - Major (approval required)
  → Major version updates (highest risk)
```

**Example Usage:**
```
In session: "run dependency-upgrade-staged for this project"

CLI:
# Start the recipe
amplifier tool invoke recipes operation=execute \
  recipe_path=dependency-upgrade-staged-recipe.yaml \
  context='{"project_path": ".", "package_manager": "uv"}'

# Check pending approvals
amplifier tool invoke recipes operation=approvals

# Approve a stage
amplifier tool invoke recipes operation=approve \
  session_id=<session_id> stage_name=validation
```

**Key Learnings:**
- `stages:` for approval gates
- Human-in-loop checkpoints
- Risk-ordered execution phases
- Compare with flat version for when to use each

---

## Analysis Examples

### Multi-File Analysis

**File:** `examples/multi-file-analysis.yaml`

Analyze multiple files in parallel with per-file insights and consolidated summary.

**Use Cases:**
- Bulk file analysis
- Codebase surveys
- Parallel processing patterns

**Workflow:**
```
1. Analyze Each File (parallel foreach)
   → All files analyzed concurrently
   → Results collected into array

2. Create Summary
   → Synthesize all analyses
   → Executive summary with priorities
```

**Example Usage:**
```
In session: "analyze src/auth.py, src/models.py, and src/utils.py together"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=multi-file-analysis.yaml \
       context='{"files": ["src/auth.py", "src/models.py", "src/utils.py"]}'
```

**Key Learnings:**
- `foreach:` with `parallel: true` for concurrent execution
- `collect:` to gather results into array
- `on_error: "continue"` for fault tolerance
- 3x+ speedup vs sequential

---

### Parallel Analysis

**File:** `examples/parallel-analysis-recipe.yaml`

Analyze code from multiple perspectives simultaneously.

**Use Cases:**
- Multi-perspective code review
- Trade-off analysis
- Comprehensive assessment

**Workflow:**
```
1. Multi-Perspective Analysis (parallel foreach)
   → Security, performance, maintainability, testability
   → All run concurrently

2. Synthesize Perspectives
   → Cross-cutting concerns
   → Prioritized recommendations
```

**Example Usage:**
```
In session: "analyze src/core.py from multiple perspectives"

CLI: amplifier tool invoke recipes operation=execute \
       recipe_path=parallel-analysis-recipe.yaml \
       context='{"file_path": "src/core.py"}'
```

**Key Learnings:**
- Same file, multiple perspectives
- Parallel foreach over perspectives list
- Synthesis of competing concerns

---

## GitHub Examples

### Repo Activity Analysis

**File:** `examples/repo-activity-analysis.yaml`

Analyze a GitHub repository for commits and PRs in a date range.

**Use Cases:**
- Daily/weekly activity reports
- Contribution tracking
- Release notes preparation

**Workflow:**
```
1. Parse Date Range → Convert natural language to dates
2. Detect/Parse Repo → Get owner/name from URL or cwd
3. Fetch Commits (bash) → gh CLI for commit data
4. Fetch PRs (bash) → gh CLI for PR data
5. Synthesize Report → Human-readable summary
6. Write Files (bash) → Save report to disk
```

**Example Usage:**
```
In session: "analyze this repo's activity for the last week"

CLI (defaults - current repo since yesterday):
amplifier tool invoke recipes operation=execute \
  recipe_path=repo-activity-analysis.yaml

CLI (specific repo and date range):
amplifier tool invoke recipes operation=execute \
  recipe_path=repo-activity-analysis.yaml \
  context='{"repo_url": "https://github.com/microsoft/amplifier-core", "date_range": "last 7 days"}'
```

**Key Learnings:**
- Hybrid bash + agent steps
- `type: "bash"` for deterministic commands (no LLM cost)
- Natural language date parsing
- `_precomputed` pattern for orchestration optimization

---

### Multi-Repo Activity Report

**File:** `examples/multi-repo-activity-report.yaml`

Generate activity reports across multiple GitHub repositories.

**Use Cases:**
- Ecosystem-wide activity tracking
- Multi-repo release coordination
- Team contribution reports

**Workflow:**
```
1. Load Repos → From JSON list or manifest file
2. Analyze Each Repo (foreach)
   → Invokes repo-activity-analysis.yaml for each
   → Parallel execution option

3. Synthesize Cross-Repo Report
   → Combined activity summary
   → Cross-repo patterns
```

**Example Usage:**
```
In session: "generate activity report for amplifier-core and amplifier-foundation"

CLI (with repo list):
amplifier tool invoke recipes operation=execute \
  recipe_path=multi-repo-activity-report.yaml \
  context='{"repos": [{"owner": "microsoft", "name": "amplifier-core", "url": "https://github.com/microsoft/amplifier-core"}]}'

CLI (with manifest file):
amplifier tool invoke recipes operation=execute \
  recipe_path=multi-repo-activity-report.yaml \
  context='{"repos_manifest": "./repos.json"}'
```

**Key Learnings:**
- Recipe invocation within foreach
- Manifest file support
- Cross-repo synthesis

---

## Communication Examples

### Feature Announcement

**File:** `examples/feature-announcement.yaml`

Generate human-friendly feature announcements from Amplifier session work.

**Original Author:** Salil Das (https://github.com/sadlilas)

**Use Cases:**
- Communicate technical changes to mixed audiences
- Generate plain-text announcements for Teams/Slack
- Consistent announcement format

**Workflow:**
```
1. Analyze Changes
   → Session analysis, git history, or user description
   → Structured change summary

2. Generate Announcement
   → Human-first, plain text
   → Hook + bullets + try it + links format

3. Save Announcement
   → Write to file for easy copy-paste
```

**Input Modes:**
- Current session (default)
- Specific session ID
- Git repository history

**Example Usage:**
```
In session: "run the feature-announcement recipe"

CLI (analyze current session):
amplifier tool invoke recipes operation=execute \
  recipe_path=feature-announcement.yaml

CLI (with git history):
amplifier tool invoke recipes operation=execute \
  recipe_path=feature-announcement.yaml \
  context='{"repo_path": ".", "git_range": "HEAD~5..HEAD"}'

CLI (with user description):
amplifier tool invoke recipes operation=execute \
  recipe_path=feature-announcement.yaml \
  context='{"user_description": "Added new caching layer for API responses"}'
```

**Key Learnings:**
- Human-first output (plain text, no markdown)
- Multiple input mode detection
- Structured format for scanning (hook, bullets, action, links)

---

## Advanced Examples

### Context Intelligence

**Directory:** `examples/context-intelligence/`

A collection of advanced recipes for context-aware AI workflows, organized in tiers of complexity.

**Structure:**
```
context-intelligence/
├── tier1/          # Foundation patterns
├── tier2/          # Intermediate patterns
├── tier3/          # Advanced patterns
├── compression/    # Context compression techniques
├── synthesis/      # Information synthesis
├── verification/   # Output verification
├── orchestrators/  # Custom orchestration
├── workflows/      # Complete workflow examples
├── foundation/     # Core utilities
├── shared/         # Shared resources
└── test/           # Test recipes
```

See `examples/context-intelligence/README.md` for detailed documentation.

---

## Test Recipes

These recipes are primarily for testing recipe engine functionality:

| Recipe | Purpose |
|--------|---------|
| `test-parse-json.yaml` | Verify `parse_json` functionality |
| `ultra-minimal-test.yaml` | Minimal recipe for quick testing |

---

## Using Examples

### Copy and Customize

```bash
# Copy example to your recipes directory
cp examples/code-review-recipe.yaml my-recipes/custom-review.yaml

# Edit as needed, then run in a session:
# "run my-recipes/custom-review.yaml on src/mycode.py"
```

### Study Patterns

Each example demonstrates specific patterns:

| Pattern | Examples |
|---------|----------|
| Sequential steps | simple-analysis |
| Bash steps | bash-step-example, repo-activity-analysis |
| Conditional execution | conditional-workflow, code-review |
| Parallel foreach | multi-file-analysis, parallel-analysis |
| Approval gates (staged) | dependency-upgrade-staged |
| Recipe composition | comprehensive-review, multi-repo-activity-report |
| Multi-mode agents | code-review |
| Domain specialists | security-audit, test-generation |

---

## Example Categories

### By Domain

| Domain | Examples |
|--------|----------|
| **Tutorial** | simple-analysis, bash-step-example, conditional-workflow |
| **Code Quality** | code-review, comprehensive-review |
| **Security** | security-audit |
| **Testing** | test-generation |
| **DevOps** | dependency-upgrade, dependency-upgrade-staged |
| **Analysis** | multi-file-analysis, parallel-analysis |
| **GitHub** | repo-activity-analysis, multi-repo-activity-report |
| **Communication** | feature-announcement |
| **Advanced** | context-intelligence |

### By Pattern

| Pattern | Examples |
|---------|----------|
| **Sequential** | simple-analysis, code-review |
| **Conditional** | conditional-workflow, code-review |
| **Parallel** | multi-file-analysis, parallel-analysis |
| **Staged (Approval Gates)** | dependency-upgrade-staged |
| **Recipe Composition** | comprehensive-review, multi-repo-activity-report |
| **Bash Hybrid** | bash-step-example, repo-activity-analysis |
| **Artifact Generation** | test-generation, feature-announcement |

---

## Contributing Examples

Have a useful recipe? Share it!

1. **Test thoroughly** with multiple inputs
2. **Document well** (context vars, usage, expected output)
3. **Follow best practices** (see [Best Practices](BEST_PRACTICES.md))
4. **Submit PR** with:
   - Recipe YAML file
   - Description for this catalog
   - Example usage

See [Contributing Guidelines](../CONTRIBUTING.md) for details.

---

## Next Steps

1. **Browse examples** in `examples/` directory
2. **Try an example** with your data
3. **Customize** for your needs
4. **Create your own** using patterns learned
5. **Share** useful recipes with community

---

**Questions about examples?** Ask in [GitHub Discussions](https://github.com/microsoft/amplifier-bundle-recipes/discussions)!
