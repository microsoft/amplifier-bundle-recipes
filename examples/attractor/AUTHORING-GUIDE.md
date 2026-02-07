# Attractor Authoring Guide

How to write specs and scenarios that converge fast.

This guide is for AI agents and humans preparing inputs for the Attractor
recipe. The quality of the spec and scenarios is the single biggest lever
on whether the factory converges in 1 iteration or 5.

---

## The Model

The Attractor treats code as **opaque weights**. Correctness is inferred
exclusively from externally observable behavior — never from inspecting
source. This reframes what a specification is: not an implementation guide,
but a declaration of observable behaviors against which the output will be
validated.

There are two inputs:

- **Spec** (`spec.md`) — declares WHAT to build. The agent reads this.
- **Scenarios** (`scenarios.md`) — declare HOW TO KNOW it works. The agent
  never sees these. They are a holdout set, like evaluation data in ML
  training.

The factory loop is: generate code from the spec → run scenarios against the
code → if scenarios fail, analyze failures and generate targeted feedback →
repeat. The agent only sees the spec and the feedback. It never sees the
scenarios themselves.

---

## Writing a Spec

### What the Recipe Extracts

The seed stage parses the spec into:

| Field | Required | What it's used for |
|-------|----------|-------------------|
| Summary | Yes | One-paragraph description of what's being built |
| Language | Yes | Determines project scaffolding, entry point, dependency management |
| Framework | No | If applicable (Flask, Express, etc.) |
| Components | Yes | Major modules/files to create |
| Requirements | Yes | Every functional requirement, explicit and implicit |
| Entry point | Yes | How the software is started (`python server.py`, `npm start`) |
| Dependencies | Yes | External libraries or services needed |
| Complexity | No | Helps the factory calibrate expectations |

### Structure

Use markdown with clear sections. The agent scans for structure, not prose.

```markdown
# [Project Name] Specification

## Overview
One paragraph. What is this and what does it do.

## Technical Requirements
- Language: Python 3.10+
- Framework: None (stdlib only)
- Storage: JSON file on disk
- Port: 8080
- No external dependencies

## API / Interface
### [Endpoint or Function 1]
- Input: what it accepts
- Output: what it returns
- Error cases: what happens when input is wrong

### [Endpoint or Function 2]
...

## Data Model
Concrete schema with types and defaults.

## Behavior
- How the system starts
- How state persists
- Edge cases and error handling
```

### Rules for Good Specs

**Be concrete about interfaces.** The harness validates external behavior.
If the spec says "return an error" but doesn't say what status code or error
format, the harness can't check it and the factory can't converge.

```
Bad:  "Return an error if the input is invalid"
Good: "Return 400 Bad Request with {"error": "title is required"}"
```

**Declare the entry point explicitly.** The harness needs to start the software.

```
Bad:  "The server should be easy to start"
Good: "The server starts with: python server.py"
```

**Specify data formats exactly.** JSON field names, types, defaults, and
required vs optional.

```
Bad:  "Each item has a name and status"
Good: "Each item: {"id": "unique-string", "title": "non-empty string", "done": false}"
```

**State what NOT to do.** Scope boundaries prevent the agent from wandering.

```
"No external dependencies — standard library only"
"Do NOT implement authentication"
"The storage layer uses a JSON file, not a database"
```

**Include error cases.** Every endpoint/function should specify what happens
with bad input, missing resources, and edge cases. These become scenarios.

**Omit implementation details.** The spec declares WHAT, not HOW. Don't
prescribe class hierarchies, function names, or internal architecture. The
code is opaque — only the observable interface matters.

```
Bad:  "Create a TodoHandler class with a handle_post method"
Good: "POST /todos creates a new todo and returns 201 with the created item"
```

### What Correlates with Fast Convergence

From real factory runs:

| Spec quality | Convergence |
|-------------|-------------|
| Concrete interfaces, exact formats, explicit error cases | 1 iteration |
| Good structure but some ambiguous assertions | 2 iterations |
| Source-inspection requirements (check code structure, not behavior) | 3+ iterations |

The pattern: **the more your spec describes observable behavior (not internal
structure), the faster the factory converges.**

---

## Writing Scenarios

### What the Recipe Extracts

The seed stage parses each scenario into:

| Field | Required | What it's used for |
|-------|----------|-------------------|
| id | Yes | Unique identifier (e.g., "scenario-01") |
| name | Yes | Short descriptive name |
| description | Yes | What user-visible behavior this validates |
| preconditions | Yes | Setup steps before the scenario runs |
| steps | Yes | Ordered sequence of user/system actions |
| assertions | Yes | Observable outcomes that must hold |
| type | Yes | "deterministic" or "semantic" |

### Scenario Types

**Deterministic** — Exact output matching. The harness runs the steps
programmatically and checks assertions with code. Use for:
- API responses (status codes, JSON fields)
- CLI output (exit codes, stdout content)
- File operations (file exists, content matches)
- State changes (data persists, data deleted)

**Semantic** — Intent matching. The harness captures output and an LLM
judge evaluates whether the behavior satisfies the scenario. Use for:
- Error message quality ("is the message user-friendly?")
- Output formatting ("is the report well-structured?")
- Behavioral quality ("does the search return relevant results?")

**Prefer deterministic.** Every deterministic scenario is one the harness
can check without ambiguity. Semantic scenarios require LLM judgment, which
is slower and less reliable. Use semantic only when the assertion genuinely
requires interpretation.

### Structure

```markdown
# [Project] Scenarios

## Scenario 1: [Short name]

**Description:** What user-visible behavior this validates.

**Preconditions:**
- Server is running
- Database is empty

**Steps:**
1. Send POST /items with body {"title": "Test"}
2. Extract id from the response
3. Send GET /items/{id}

**Assertions:**
- POST response status is 201
- POST response body has an id field
- GET response status is 200
- GET response body title is "Test"

**Type:** deterministic
```

### Rules for Good Scenarios

**Each assertion must be machine-checkable.** The harness translates your
assertions into executable checks. If the assertion requires human judgment,
mark the scenario as "semantic."

```
Bad:  "The response looks correct"
Good: "Response status is 200 and body contains an id field"
```

**Scenarios are independent.** Each scenario sets up its own preconditions
and tears down afterward. One scenario's failure must not affect another.
Don't write scenario chains where scenario 3 depends on scenario 2's output.

**Cover the happy path AND error cases.** A spec with 5 endpoints needs
scenarios for successful use of each endpoint AND for each error condition
(404 not found, 400 bad request, etc.).

**Preconditions must be actionable.** The harness needs to set up the state.
"Server is running" means the harness starts the server. "Database is empty"
means the harness deletes the data file.

```
Bad:  "The system is in a good state"
Good: "Remove todos.json if it exists, then start the server"
```

**Steps must be concrete operations.** The harness translates steps into
actual commands (HTTP requests, CLI invocations, file operations).

```
Bad:  "Create an item"
Good: "Send POST /todos with body {"title": "Buy groceries"}"
```

**Test one behavior per scenario.** A scenario that tests create, update,
AND delete is hard to debug when it fails. Split into three scenarios.

**Include a "fresh start" scenario.** Test that the system works correctly
with no prior state. This catches initialization bugs.

**Include a persistence scenario.** Test that state survives across
operations (not just within a single request).

### The Holdout Principle

Scenarios are a holdout set. The generating agent never sees them.
This prevents reward hacking — the agent can't write code that trivially
passes the tests because it doesn't know what the tests check.

The factory loop feeds the agent only:
- The spec (what to build)
- Validation results (which scenarios passed/failed, with evidence)
- Feedback analysis (root cause + fix recommendations)

The agent sees "Scenario 3 failed: expected status 201, got 500" but never
sees the scenario definition itself. This keeps the agent focused on
genuinely implementing the spec rather than gaming the checks.

### What Correlates with Fast Convergence

| Scenario quality | Effect |
|-----------------|--------|
| All deterministic, testing external behavior | Fastest convergence |
| Mix of deterministic + semantic | Slightly slower (LLM judge adds uncertainty) |
| Source inspection ("check that function X exists") | Slowest — the harness greps code instead of running it |

The pattern: **scenarios that test what the software DOES (externally
observable behavior) converge faster than scenarios that test what the
software IS (internal structure).**

---

## Checklist

Before running the factory:

### Spec
- [ ] Language and framework are explicit
- [ ] Entry point is specified (`python server.py`, `npm start`, etc.)
- [ ] Every interface has concrete input/output formats
- [ ] Error cases specify exact responses (status codes, error messages)
- [ ] Data model has explicit field names, types, and defaults
- [ ] Dependencies are listed (or "no external dependencies" is stated)
- [ ] Scope boundaries are declared (what NOT to build)
- [ ] No implementation prescriptions (class names, function names, architecture)

### Scenarios
- [ ] Every requirement in the spec has at least one scenario
- [ ] Every error case has a scenario
- [ ] Each scenario is independent (own preconditions, no dependencies on other scenarios)
- [ ] Preconditions are actionable (harness can set them up programmatically)
- [ ] Steps are concrete operations (HTTP requests, CLI commands, file operations)
- [ ] Assertions are machine-checkable (exact values, not "looks correct")
- [ ] A "fresh start" scenario exists (empty state → first operation)
- [ ] A persistence scenario exists (state survives across operations)
- [ ] Majority of scenarios are deterministic (use semantic only when necessary)
- [ ] Each scenario tests one behavior

### Coverage Ratio

Aim for **2-3 scenarios per spec requirement**:
- One happy-path scenario
- One error-case scenario
- One edge-case scenario (optional but improves convergence reliability)

A 10-scenario set covering a simple API is typically sufficient to converge
in 1-2 iterations. More complex projects may need 20-30 scenarios.

---

## Anti-Patterns

**The vague spec:** "Build a todo app." No language, no interface, no data
model. The agent will make assumptions that conflict with the scenarios.

**The implementation-prescriptive spec:** "Create a class called TodoService
with methods add(), remove(), and list()." This constrains the agent to one
architecture when the scenarios only care about external behavior.

**The source-inspection scenario:** "The file server.py exists and contains
a class called Handler." The harness greps source code instead of running it.
This is slow to converge because the agent may use different names or
structures that work correctly but don't match the pattern.

**The dependent scenario chain:** Scenario 3 says "Using the item created in
Scenario 2, update its title." If Scenario 2 fails, Scenario 3 also fails
even if the update logic is correct. Make each scenario self-contained.

**The unmeasurable assertion:** "The API is fast." Fast compared to what?
Either specify a concrete threshold ("response time under 500ms") or mark as
semantic and describe what "fast" means for the LLM judge.

**The everything scenario:** "Create an item, list all items, update the
item, delete the item, verify it's gone." This is 5 behaviors in one
scenario. When it fails, the feedback can only say "the scenario failed" —
not which behavior is broken. Split it.
