"""Deterministic recipe YAML to DOT flowchart conversion.

Converts Amplifier recipe YAML files into Graphviz DOT diagrams that match
the visual conventions of hand-crafted reference flow diagrams.

Usage::

    from amplifier_module_tool_recipes.recipe_to_dot import recipe_to_dot
    dot_str = recipe_to_dot("path/to/recipe.yaml")

No LLM calls — pure deterministic function. Only requires PyYAML (already a
package dependency) and the Python standard library.
"""

import hashlib
import re
from pathlib import Path

import yaml

# ── Visual convention constants ───────────────────────────────────────────────

_COLOR_START_END = "#e0e0e0"
_COLOR_BASH = "#bbdefb"
_COLOR_AGENT = "#c8e6c9"
_COLOR_RECIPE = "#e0e0e0"
_COLOR_APPROVAL = "#ffe0b2"
_COLOR_CONDITION = "#fff9c4"
_COLOR_CLUSTER_FILL = "#f9f9f9"
_COLOR_CLUSTER_BORDER = "#999999"
_COLOR_LEGEND_FILL = "white"
_COLOR_LEGEND_BORDER = "#cccccc"


# ── Public API ────────────────────────────────────────────────────────────────


def recipe_to_dot(yaml_path: str | Path) -> str:
    """Convert a recipe YAML file to a DOT flowchart string.

    Reads the YAML, detects flat (``steps``) or staged (``stages``) mode, and
    emits a complete Graphviz DOT graph that matches the visual conventions
    used by hand-crafted reference diagrams in this repository.

    Visual conventions:

    * **Agent steps** — green box (``#c8e6c9``)
    * **Bash steps** — blue box (``#bbdefb``)
    * **Sub-recipe calls** — gray dashed box (``#e0e0e0``)
    * **Approval gates** — orange diamond (``#ffe0b2``)
    * **Condition diamonds** — yellow diamond (``#fff9c4``)
    * **Start / End** — gray oval (``#e0e0e0``)
    * **Stages** — filled rounded cluster with gray border
    * **Legend** — always appended, shows only types actually used

    Args:
        yaml_path: Path to a valid recipe YAML file.

    Returns:
        Complete, valid DOT string suitable for passing to ``dot -Tsvg``.

    Raises:
        FileNotFoundError: If *yaml_path* does not exist.
        ValueError: If the YAML is not a valid recipe (missing ``name`` key or
            neither ``steps`` nor ``stages`` top-level key).

    Example::

        dot = recipe_to_dot("examples/simple-analysis-recipe.yaml")
        with open("diagram.dot", "w") as fh:
            fh.write(dot)
    """
    yaml_path = Path(yaml_path)
    recipe = _parse_recipe(yaml_path)

    name = recipe.get("name", "recipe")
    description = recipe.get("description", "")
    graph_id = _sanitize_id(name)
    title = _make_title(name, description)

    step_types_used: set[str] = {"start_end"}
    body_parts: list[str] = []

    if "stages" in recipe:
        stage_body, stage_exit, used = _render_staged(recipe["stages"])
        step_types_used.update(used)
        body_parts.append(stage_body)
        body_parts.append(f"    {stage_exit} -> done")
    else:
        steps = recipe.get("steps", [])
        steps_body, last_node, used = _render_steps_block(steps, "start", "    ")
        step_types_used.update(used)
        body_parts.append(steps_body)
        body_parts.append(f"    {last_node} -> done")

    body = "\n".join(body_parts)

    # Compute structural hash and embed as graph attribute for freshness tracking.
    # LLM-enhanced DOT files can extract this to know when the YAML structure
    # changed, even though labels have been rewritten.
    structural_hash = hashlib.sha256(body.encode()).hexdigest()

    out: list[str] = [
        f"digraph {graph_id} {{",
        "    rankdir=TB",
        '    fontname="Helvetica"',
        "    fontsize=12",
        f"    label={_q(title)}",
        "    labelloc=t",
        "    labeljust=c",
        "    compound=true",
        "    nodesep=0.6",
        "    ranksep=0.7",
        '    bgcolor="white"',
        f'    source_hash="{structural_hash}"',
        "",
        '    node [fontname="Helvetica", fontsize=11, style="filled,rounded"]',
        '    edge [fontname="Helvetica", fontsize=9]',
        "",
        f'    start [label="Start", shape=oval, fillcolor="{_COLOR_START_END}"]',
        f'    done  [label="Done!", shape=oval, fillcolor="{_COLOR_START_END}"]',
        "",
        body,
        "",
        _build_legend(step_types_used),
        "}",
    ]
    return "\n".join(out)


def recipe_dot_hash(yaml_path: str | Path) -> str:
    """Return the SHA-256 hex digest of the DOT that would be generated.

    Useful for change detection — if the hash of a recipe's DOT output is
    the same as a stored value, the diagram is up-to-date.

    Args:
        yaml_path: Path to a valid recipe YAML file.

    Returns:
        64-character lowercase hex string (SHA-256 of the DOT output encoded
        as UTF-8).

    Example::

        h1 = recipe_dot_hash("recipe.yaml")
        # ... later, after potential edits ...
        h2 = recipe_dot_hash("recipe.yaml")
        if h1 != h2:
            regenerate_diagram()
    """
    dot = recipe_to_dot(yaml_path)
    return hashlib.sha256(dot.encode()).hexdigest()


# ── Internal helpers ──────────────────────────────────────────────────────────


def _parse_recipe(yaml_path: Path) -> dict:
    """Parse and basic-validate a recipe YAML file.

    Args:
        yaml_path: Resolved path to the YAML file.

    Returns:
        Parsed dictionary from the YAML file.

    Raises:
        FileNotFoundError: If *yaml_path* does not exist.
        ValueError: If the YAML lacks a ``name`` key, or has neither
            ``steps`` nor ``stages``.
    """
    if not yaml_path.exists():
        raise FileNotFoundError(f"Recipe file not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {yaml_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Recipe YAML must be a mapping, got {type(data).__name__}")

    if "name" not in data:
        raise ValueError(f"Recipe YAML missing required 'name' key: {yaml_path}")

    has_steps = "steps" in data
    has_stages = "stages" in data
    if not has_steps and not has_stages:
        raise ValueError(
            f"Recipe YAML must have either 'steps' or 'stages': {yaml_path}"
        )

    return data


def _sanitize_id(raw: str) -> str:
    """Make an arbitrary string safe for use as a DOT node identifier.

    * Replaces hyphens, spaces, and dots with underscores.
    * Strips any remaining non-alphanumeric/underscore characters.
    * Prefixes with ``n_`` if the result starts with a digit or is empty.

    Args:
        raw: Any string (step ID, stage name, recipe name, …).

    Returns:
        A non-empty DOT-safe identifier string.

    Examples::

        _sanitize_id("audit-dependencies")  # "audit_dependencies"
        _sanitize_id("phase-1-security")    # "phase_1_security"
        _sanitize_id("123start")            # "n_123start"
    """
    s = re.sub(r"[\-\s\.]", "_", raw)
    s = re.sub(r"[^\w]", "", s)
    s = s.strip("_")
    if not s:
        s = "node"
    if s[0].isdigit():
        s = "n_" + s
    return s


def _auto_label(step_id: str) -> str:
    """Convert a hyphen-separated step ID to a readable multi-line label.

    Rules:

    * Split on hyphens and underscores.
    * Title-case each word.
    * If more than 3 words, wrap to at most 2 lines (split roughly in half).

    Args:
        step_id: A step identifier such as ``"audit-dependencies"`` or
            ``"generate-phase1-commands"``.

    Returns:
        A label string with ``\\n`` line breaks where needed.

    Examples::

        _auto_label("audit-dependencies")        # "Audit\\nDependencies"
        _auto_label("generate-report")           # "Generate\\nReport"
        _auto_label("a-b-c-d-e")                 # "A B C\\nD E"
    """
    words = [w.title() for w in re.split(r"[\-_]", step_id) if w]
    if not words:
        return step_id.title()
    if len(words) <= 2:
        return "\n".join(words)
    if len(words) == 3:
        # 2 on first line, 1 on second
        return " ".join(words[:2]) + "\n" + words[2]
    # 4+ words: split roughly in half
    mid = (len(words) + 1) // 2
    return " ".join(words[:mid]) + "\n" + " ".join(words[mid:])


def _step_type_key(step: dict) -> str:
    """Return a canonical type key for a step dict.

    Recognised keys (in priority order): ``type`` field, presence of
    ``command`` key, presence of ``recipe`` key.  Defaults to ``"agent"``.

    Args:
        step: A step dictionary as parsed from the recipe YAML.

    Returns:
        One of: ``"bash"``, ``"recipe"``, ``"agent"``.
    """
    t = (step.get("type") or "").lower()
    if t == "bash" or (not t and "command" in step):
        return "bash"
    if t == "recipe" or (not t and "recipe" in step):
        return "recipe"
    return "agent"


def _step_attrs(step: dict) -> tuple[str, str, str]:
    """Return ``(shape, fillcolor, style)`` for a step node.

    Args:
        step: A step dictionary.

    Returns:
        A 3-tuple of DOT attribute string values.
    """
    key = _step_type_key(step)
    if key == "bash":
        return "box", _COLOR_BASH, "filled,rounded"
    if key == "recipe":
        return "box", _COLOR_RECIPE, "filled,rounded,dashed"
    return "box", _COLOR_AGENT, "filled,rounded"


def _make_title(name: str, description: str) -> str:
    """Build the graph label from recipe name and description.

    Format: ``"Pretty Name — First sentence of description (max 80 chars)"``.
    If *description* is empty, the title is just the prettified name.

    Args:
        name: The recipe ``name`` field (e.g. ``"dependency-upgrade-staged"``).
        description: The recipe ``description`` field (may be empty).

    Returns:
        A single- or two-line graph title string.

    Examples::

        _make_title("simple-analysis", "A simple analysis recipe.")
        # "Simple Analysis — A simple analysis recipe."
    """
    pretty = " ".join(w.title() for w in re.split(r"[\-_]", name) if w)
    if not description:
        return pretty
    # Take first sentence, truncate to 80 chars
    first_sentence = re.split(r"[.\n]", description.strip())[0].strip()
    if len(first_sentence) > 80:
        first_sentence = first_sentence[:77] + "..."
    if first_sentence:
        return f"{pretty} \u2014 {first_sentence}"
    return pretty


def _simplify_condition(condition: str) -> str:
    """Produce a short human-readable label from a condition expression.

    Strips ``{{`` / ``}}`` Jinja-style delimiters and truncates to 30 chars.

    Args:
        condition: A condition string such as ``"{{classification}} == 'simple'"``.

    Returns:
        A short, readable string (≤ 30 chars).

    Examples::

        _simplify_condition("{{classification}} == 'simple'")
        # "classification == 'simple'"
    """
    cleaned = re.sub(r"\{\{|\}\}", "", condition).strip()
    if len(cleaned) > 30:
        cleaned = cleaned[:27] + "..."
    return cleaned


def _tooltip(step: dict) -> str:
    """Extract a short tooltip string from a step dict.

    Preference order: agent name, prompt excerpt, command excerpt, step id.

    Args:
        step: A step dictionary.

    Returns:
        A tooltip string safe for DOT attribute values (double-quotes removed,
        newlines replaced with spaces, max 100 chars).
    """
    raw = (
        step.get("agent")
        or step.get("prompt", "")[:100]
        or step.get("command", "")[:100]
        or step.get("id", "")
    )
    return raw.replace('"', "'").replace("\n", " ")[:100]


def _q(s: str) -> str:
    """Wrap *s* in DOT double-quotes, escaping internal double-quotes.

    Args:
        s: Any string value to quote.

    Returns:
        A DOT-safe double-quoted string.
    """
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _title_case(s: str) -> str:
    """Title-case a hyphen/underscore-separated identifier.

    Args:
        s: An identifier such as ``"phase-1-security"``.

    Returns:
        A title-cased string such as ``"Phase 1 Security"``.
    """
    return " ".join(w.title() for w in re.split(r"[\-_]", s) if w)


def _next_unconditional_nid(node_defs: list[dict], from_idx: int) -> str:
    """Return the node ID of the next unconditional step after *from_idx*.

    Args:
        node_defs: List of node descriptor dicts (see :func:`_render_steps_block`).
        from_idx: Index to search from (exclusive).

    Returns:
        Node ID string, or ``"done"`` if no unconditional step follows.
    """
    for j in range(from_idx + 1, len(node_defs)):
        if not node_defs[j]["condition"]:
            return node_defs[j]["nid"]
    return "done"


def _render_steps_block(
    steps: list[dict],
    prev_node: str,
    indent: str = "    ",
) -> tuple[str, str, set[str]]:
    """Render a flat list of steps to DOT node/edge declarations.

    Handles sequential flow, conditional branching (decision diamonds),
    foreach/parallel annotations, and while-loop back-edges.

    The *prev_node* → first-step edge IS emitted by this function, making it
    easy to chain stages and approval gates.

    Args:
        steps: List of step dictionaries from the recipe YAML.
        prev_node: DOT node ID that should connect to the first rendered node
            (e.g. ``"start"``, an approval gate ID, or the last node of a
            previous stage).
        indent: Indentation prefix for all emitted lines.

    Returns:
        A 3-tuple of:

        * *dot_str* — DOT fragment (nodes + edges, no trailing newline).
        * *last_node* — Node ID of the final unconditional step, suitable for
          connecting to the next element (end node, next stage, etc.).
          Falls back to *prev_node* if *steps* is empty.
        * *step_types_used* — Set of type keys present (subset of
          ``{"agent", "bash", "recipe", "condition"}``).
    """
    if not steps:
        return "", prev_node, set()

    lines: list[str] = []
    step_types: set[str] = set()

    # ── Build node descriptor list ────────────────────────────────────────────
    node_defs: list[dict] = []
    for i, step in enumerate(steps):
        sid = step.get("id") or f"step_{i}"
        nid = "step_" + _sanitize_id(sid)
        shape, fillcolor, style = _step_attrs(step)
        step_types.add(_step_type_key(step))

        label = _auto_label(sid)
        if _step_type_key(step) == "recipe":
            label += "\n(sub-recipe)"
        if step.get("foreach"):
            label += "\n(for each)"
        elif step.get("parallel"):
            label += "\n(parallel)"

        tt = _tooltip(step)
        node_defs.append(
            {
                "sid": sid,
                "nid": nid,
                "shape": shape,
                "fillcolor": fillcolor,
                "style": style,
                "label": label,
                "tooltip": tt,
                "condition": step.get("condition"),
                "depends_on": step.get("depends_on") or [],
                "while_condition": step.get("while_condition"),
            }
        )

    # ── Emit node declarations ────────────────────────────────────────────────
    for nd in node_defs:
        if nd["condition"]:
            cid = "cond_" + nd["nid"]
            cond_label = _simplify_condition(nd["condition"])
            lines.append(
                f"{indent}{cid} [label={_q(cond_label)}, shape=diamond,"
                f' fillcolor="{_COLOR_CONDITION}"]'
            )
            step_types.add("condition")

        tt_attr = f", tooltip={_q(nd['tooltip'])}" if nd["tooltip"] else ""
        lines.append(
            f"{indent}{nd['nid']} [label={_q(nd['label'])},"
            f" shape={nd['shape']},"
            f' fillcolor="{nd["fillcolor"]}",'
            f' style="{nd["style"]}"{tt_attr}]'
        )

    lines.append("")

    # ── Emit edges ────────────────────────────────────────────────────────────
    # last_uncond tracks the last node that sequential (non-conditional) steps
    # connect from — conditional steps branch off from it without advancing it.
    last_uncond = prev_node
    # Deferred skip edges: (diamond_id, skip_target) to emit after main loop
    skip_edges: list[tuple[str, str]] = []
    # Track which (src, diamond) pairs we've already emitted to avoid duplicates
    emitted_to_diamond: set[str] = set()

    for i, nd in enumerate(node_defs):
        if nd["depends_on"]:
            sources = ["step_" + _sanitize_id(d) for d in nd["depends_on"]]
        else:
            sources = [last_uncond]

        if nd["condition"]:
            cid = "cond_" + nd["nid"]
            cond_label = _simplify_condition(nd["condition"])
            # Connect each source → diamond (deduplicated)
            for src in sources:
                key = f"{src}->{cid}"
                if key not in emitted_to_diamond:
                    lines.append(f"{indent}{src} -> {cid}")
                    emitted_to_diamond.add(key)
            # Diamond → conditional step
            lines.append(f"{indent}{cid} -> {nd['nid']} [label={_q(cond_label[:25])}]")
            # Schedule skip edge to next unconditional step
            skip_target = _next_unconditional_nid(node_defs, i)
            skip_key = (cid, skip_target)
            if skip_key not in skip_edges:
                skip_edges.append(skip_key)
        else:
            for src in sources:
                lines.append(f"{indent}{src} -> {nd['nid']}")
            last_uncond = nd["nid"]

        # While-loop self-edge
        if nd.get("while_condition"):
            lines.append(
                f"{indent}{nd['nid']} -> {nd['nid']}"
                f' [label="loop", style=dashed, color="{_COLOR_CLUSTER_BORDER}"]'
            )

    # Emit skip (else/bypass) edges
    for cid, tgt in skip_edges:
        lines.append(f'{indent}{cid} -> {tgt} [label="skip", style=dashed]')

    return "\n".join(lines), last_uncond, step_types


def _render_staged(stages: list[dict]) -> tuple[str, str, set[str]]:
    """Render a staged recipe to DOT cluster subgraphs with approval gates.

    Each stage is wrapped in a ``subgraph cluster_<name>`` block.  When a
    stage carries ``approval.required: true``, an orange approval-gate diamond
    is inserted **before** that stage (between the previous stage's exit node
    and this stage's first step).

    Args:
        stages: List of stage dictionaries from the recipe YAML.

    Returns:
        A 3-tuple of:

        * *dot_str* — DOT fragment for all stages + gates (no leading/trailing
          ``digraph`` wrapper).
        * *last_exit* — Node ID of the final node after all stages, for
          connecting to ``done``.
        * *step_types_used* — Set of all step type keys seen.
    """
    if not stages:
        return "    start -> done", "start", set()

    lines: list[str] = []
    step_types: set[str] = set()
    prev_exit = "start"

    for i, stage in enumerate(stages):
        sname = stage.get("name") or f"stage_{i}"
        cluster_id = "cluster_" + _sanitize_id(sname)
        stage_steps: list[dict] = stage.get("steps") or []
        approval = stage.get("approval") or {}
        has_approval = bool(approval.get("required"))

        # ── Approval gate before this stage ───────────────────────────────────
        if has_approval:
            gate_id = "gate_" + _sanitize_id(sname)
            gate_label = "Human\nApproves?"
            ap_prompt = approval.get("prompt", "")
            tt_attr = ""
            if ap_prompt:
                tt_attr = f", tooltip={_q(ap_prompt[:100].replace(chr(10), ' '))}"
            lines.append(
                f"    {gate_id} [label={_q(gate_label)}, shape=diamond,"
                f' fillcolor="{_COLOR_APPROVAL}"{tt_attr}]'
            )
            lines.append(f"    {prev_exit} -> {gate_id}")
            prev_exit = gate_id
            step_types.add("approval")

        # ── Stage cluster ──────────────────────────────────────────────────────
        stage_label = _title_case(sname)
        lines.append(f"    subgraph {cluster_id} {{")
        lines.append(f"        label={_q(stage_label)}")
        lines.append('        style="filled,rounded"')
        lines.append(f'        fillcolor="{_COLOR_CLUSTER_FILL}"')
        lines.append(f'        color="{_COLOR_CLUSTER_BORDER}"')
        lines.append("")

        if stage_steps:
            block, last_node, used = _render_steps_block(
                stage_steps, prev_exit, "        "
            )
            step_types.update(used)
            lines.append(block)
            lines.append("    }")
            prev_exit = last_node
        else:
            # Empty stage: invisible placeholder keeps the cluster visible
            ph_id = "ph_" + _sanitize_id(sname)
            lines.append(f"        {ph_id} [label={_q(stage_label)}, style=invis]")
            lines.append("    }")
            lines.append(f"    {prev_exit} -> {ph_id} [style=invis]")
            prev_exit = ph_id

        lines.append("")

    return "\n".join(lines), prev_exit, step_types


def _build_legend(step_types_used: set[str]) -> str:
    """Build a DOT legend cluster for the step types actually present.

    Only entry types that appear in the diagram are included.  Entries are
    always emitted in a stable order, connected by invisible edges to force
    horizontal layout.

    Args:
        step_types_used: Set of type keys from the rendered body.  Known
            values: ``"bash"``, ``"agent"``, ``"recipe"``, ``"condition"``,
            ``"approval"``, ``"start_end"``.

    Returns:
        A DOT ``subgraph cluster_legend { ... }`` string.
    """
    # Ordered entries: (node_id, label, shape, fillcolor, extra_style)
    all_entries = [
        ("leg_bash", "Script Step", "box", _COLOR_BASH, ""),
        ("leg_agent", "AI Agent Step", "box", _COLOR_AGENT, ""),
        (
            "leg_sub",
            "Sub-Recipe Call",
            "box",
            _COLOR_RECIPE,
            ', style="filled,rounded,dashed"',
        ),
        ("leg_cond", "Condition", "diamond", _COLOR_CONDITION, ""),
        ("leg_gate", "Approval Gate", "diamond", _COLOR_APPROVAL, ""),
        ("leg_start", "Start / End", "oval", _COLOR_START_END, ""),
    ]

    type_check = {
        "leg_bash": "bash",
        "leg_agent": "agent",
        "leg_sub": "recipe",
        "leg_cond": "condition",
        "leg_gate": "approval",
        "leg_start": "start_end",
    }

    entries = [e for e in all_entries if type_check[e[0]] in step_types_used]
    if not entries:
        entries = [all_entries[-1]]  # always show at least start/end

    lines: list[str] = [
        "    subgraph cluster_legend {",
        '        label="Legend"',
        '        style="filled,rounded"',
        f'        fillcolor="{_COLOR_LEGEND_FILL}"',
        f'        color="{_COLOR_LEGEND_BORDER}"',
        "        fontsize=10",
        "        node [shape=box, fontsize=9, width=1.6]",
    ]

    node_ids: list[str] = []
    for nid, label, shape, fillcolor, extra in entries:
        lines.append(
            f"        {nid} [label={_q(label)}, shape={shape},"
            f' fillcolor="{fillcolor}"{extra}]'
        )
        node_ids.append(nid)

    # Invisible edges for horizontal layout
    if len(node_ids) > 1:
        chain = " -> ".join(node_ids)
        lines.append(f"        {chain} [style=invis]")

    lines.append("    }")
    return "\n".join(lines)
