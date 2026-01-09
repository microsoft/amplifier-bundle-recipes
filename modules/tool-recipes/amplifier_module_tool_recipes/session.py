"""Session management and persistence."""

import datetime
import json
import shutil
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from .models import Recipe


class ApprovalStatus(str, Enum):
    """Approval status for a stage."""

    PENDING = "pending"  # Waiting for approval
    APPROVED = "approved"  # Approved, proceed to next stage
    DENIED = "denied"  # Denied, stop execution
    NOT_REQUIRED = "not_required"  # No approval needed for this stage
    TIMEOUT = "timeout"  # Timed out waiting for approval


class CancellationStatus(str, Enum):
    """Cancellation status for a recipe session.

    State machine:
        NONE -> REQUESTED (first cancel request, graceful)
        REQUESTED -> IMMEDIATE (second cancel request, stop now)
        REQUESTED/IMMEDIATE -> CANCELLED (execution stopped)
    """

    NONE = "none"  # Running normally
    REQUESTED = "requested"  # Graceful cancellation requested (complete current step)
    IMMEDIATE = "immediate"  # Immediate cancellation requested (stop ASAP)
    CANCELLED = "cancelled"  # Execution stopped due to cancellation


def generate_session_id() -> str:
    """Generate unique session ID following W3C Trace Context pattern.

    Format: {span}-{timestamp}_{identifier}
    Example: 7cc787dd22d54f6c-20251118-114317_recipe

    This follows the same W3C Trace Context principles as sub-sessions:
    - Span ID (16 hex chars) for distributed tracing compatibility
    - Timestamp for human readability and chronological sorting
    - Identifier suffix ("recipe") for session type clarity
    - Underscore separator before identifier (consistent with sub-session agent names)

    Returns:
        Session ID string in W3C Trace Context compatible format
    """
    # Generate 16-char hex span ID (W3C Trace Context standard)
    span_id = uuid.uuid4().hex[:16]

    # Human-readable timestamp (YYYYMMDD-HHMMSS)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    # Format: {span}-{timestamp}_{identifier}
    # Underscore before identifier (consistent with sub-session agent names)
    return f"{span_id}-{timestamp}_recipe"


def get_project_slug(project_path: Path) -> str:
    """Convert project path to slug for session directory."""
    # Convert absolute path to slug
    abs_path = project_path.resolve()
    slug = str(abs_path).replace("/", "-").replace("\\", "-")

    # Remove leading dash
    if slug.startswith("-"):
        slug = slug[1:]

    return slug


class SessionManager:
    """Manages recipe session persistence and cleanup."""

    def __init__(self, base_dir: Path, auto_cleanup_days: int = 7):
        """
        Initialize session manager.

        Args:
            base_dir: Base directory for all sessions (~/.amplifier/projects)
            auto_cleanup_days: Auto-delete sessions older than N days
        """
        self.base_dir = Path(base_dir).expanduser()
        self.auto_cleanup_days = auto_cleanup_days

    def get_sessions_dir(self, project_path: Path) -> Path:
        """Get sessions directory for project."""
        slug = get_project_slug(project_path)
        return self.base_dir / slug / "recipe-sessions"

    def create_session(self, recipe: Recipe, project_path: Path, recipe_path: Path | None = None) -> str:
        """
        Create new session.

        Args:
            recipe: Recipe being executed
            project_path: Current project directory
            recipe_path: Optional path to recipe file (will be copied to session)

        Returns:
            session_id: Unique session identifier
        """
        session_id = generate_session_id()
        session_dir = self.get_session_dir(session_id, project_path)

        # Create session directory
        session_dir.mkdir(parents=True, exist_ok=True)

        # Save copy of recipe to session directory
        if recipe_path and recipe_path.exists():
            import shutil

            shutil.copy2(recipe_path, session_dir / "recipe.yaml")

        # Initialize state
        state = {
            "session_id": session_id,
            "recipe_name": recipe.name,
            "recipe_version": recipe.version,
            "started": datetime.datetime.now().isoformat(),
            "current_step_index": 0,
            "context": recipe.context.copy(),
            "completed_steps": [],
            "project_path": str(project_path.resolve()),
        }

        # Save initial state
        self.save_state(session_id, project_path, state)

        return session_id

    def get_session_dir(self, session_id: str, project_path: Path) -> Path:
        """Get session directory path."""
        sessions_dir = self.get_sessions_dir(project_path)
        return sessions_dir / session_id

    def save_state(self, session_id: str, project_path: Path, state: dict[str, Any]) -> None:
        """Save session state to disk."""
        session_dir = self.get_session_dir(session_id, project_path)
        state_file = session_dir / "state.json"

        # Write with retry (cloud sync issues)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def load_state(self, session_id: str, project_path: Path) -> dict[str, Any]:
        """Load session state from disk."""
        session_dir = self.get_session_dir(session_id, project_path)
        state_file = session_dir / "state.json"

        if not state_file.exists():
            raise FileNotFoundError(f"Session state not found: {session_id}")

        with open(state_file, encoding="utf-8") as f:
            return json.load(f)

    def session_exists(self, session_id: str, project_path: Path) -> bool:
        """Check if session exists."""
        session_dir = self.get_session_dir(session_id, project_path)
        state_file = session_dir / "state.json"
        return state_file.exists()

    def list_sessions(self, project_path: Path) -> list[dict[str, Any]]:
        """
        List all sessions for project.

        Returns list of session info dicts with:
        - session_id
        - recipe_name
        - started
        - current_step_index
        - completed_steps
        """
        sessions_dir = self.get_sessions_dir(project_path)

        if not sessions_dir.exists():
            return []

        sessions = []
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue

            state_file = session_dir / "state.json"
            if not state_file.exists():
                continue

            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)

                sessions.append(
                    {
                        "session_id": state.get("session_id", session_dir.name),
                        "recipe_name": state.get("recipe_name", "unknown"),
                        "started": state.get("started"),
                        "current_step_index": state.get("current_step_index", 0),
                        "completed_steps": state.get("completed_steps", []),
                    }
                )
            except Exception:
                # Skip corrupted sessions
                continue

        # Sort by started time (newest first)
        sessions.sort(key=lambda s: s.get("started", ""), reverse=True)

        return sessions

    def cleanup_old_sessions(self, project_path: Path) -> int:
        """
        Delete sessions older than auto_cleanup_days.

        Returns number of sessions deleted.
        """
        sessions_dir = self.get_sessions_dir(project_path)

        if not sessions_dir.exists():
            return 0

        cutoff = datetime.datetime.now() - datetime.timedelta(days=self.auto_cleanup_days)
        deleted_count = 0

        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue

            state_file = session_dir / "state.json"
            if not state_file.exists():
                continue

            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)

                started_str = state.get("started")
                if not started_str:
                    continue

                started = datetime.datetime.fromisoformat(started_str.replace("Z", "+00:00"))

                if started < cutoff:
                    # Delete old session
                    shutil.rmtree(session_dir)
                    deleted_count += 1

            except Exception:
                # Skip problematic sessions
                continue

        return deleted_count

    # === Approval Gate Methods (Phase 3) ===

    def get_stage_approval_status(self, session_id: str, project_path: Path, stage_name: str) -> ApprovalStatus:
        """Get approval status for a specific stage.

        Args:
            session_id: Session identifier
            project_path: Project directory
            stage_name: Name of the stage

        Returns:
            ApprovalStatus for the stage
        """
        state = self.load_state(session_id, project_path)
        stage_approvals = state.get("stage_approvals", {})
        status_str = stage_approvals.get(stage_name, ApprovalStatus.NOT_REQUIRED.value)
        return ApprovalStatus(status_str)

    def set_stage_approval_status(
        self,
        session_id: str,
        project_path: Path,
        stage_name: str,
        status: ApprovalStatus,
        reason: str | None = None,
    ) -> None:
        """Set approval status for a specific stage.

        Args:
            session_id: Session identifier
            project_path: Project directory
            stage_name: Name of the stage
            status: New approval status
            reason: Optional reason (e.g., denial reason)
        """
        state = self.load_state(session_id, project_path)

        if "stage_approvals" not in state:
            state["stage_approvals"] = {}
        if "approval_history" not in state:
            state["approval_history"] = []

        state["stage_approvals"][stage_name] = status.value

        # Record in history
        state["approval_history"].append(
            {
                "stage": stage_name,
                "status": status.value,
                "timestamp": datetime.datetime.now().isoformat(),
                "reason": reason,
            }
        )

        self.save_state(session_id, project_path, state)

    def get_pending_approval(self, session_id: str, project_path: Path) -> dict[str, Any] | None:
        """Get information about the current pending approval, if any.

        Args:
            session_id: Session identifier
            project_path: Project directory

        Returns:
            Dict with stage info and approval prompt, or None if no pending approval
        """
        state = self.load_state(session_id, project_path)

        pending_stage = state.get("pending_approval_stage")
        if not pending_stage:
            return None

        return {
            "session_id": session_id,
            "recipe_name": state.get("recipe_name", "unknown"),
            "stage_name": pending_stage,
            "approval_prompt": state.get("pending_approval_prompt", ""),
            "approval_timeout": state.get("pending_approval_timeout", 0),
            "approval_requested_at": state.get("pending_approval_requested_at"),
            "approval_default": state.get("pending_approval_default", "deny"),
        }

    def set_pending_approval(
        self,
        session_id: str,
        project_path: Path,
        stage_name: str,
        prompt: str,
        timeout: int,
        default: str,
    ) -> None:
        """Set a pending approval for the session.

        Args:
            session_id: Session identifier
            project_path: Project directory
            stage_name: Stage awaiting approval
            prompt: Approval prompt to show user
            timeout: Seconds before timeout
            default: What happens on timeout ('approve' or 'deny')
        """
        state = self.load_state(session_id, project_path)

        state["pending_approval_stage"] = stage_name
        state["pending_approval_prompt"] = prompt
        state["pending_approval_timeout"] = timeout
        state["pending_approval_default"] = default
        state["pending_approval_requested_at"] = datetime.datetime.now().isoformat()

        # Set stage status to pending
        if "stage_approvals" not in state:
            state["stage_approvals"] = {}
        state["stage_approvals"][stage_name] = ApprovalStatus.PENDING.value

        self.save_state(session_id, project_path, state)

    def clear_pending_approval(self, session_id: str, project_path: Path) -> None:
        """Clear pending approval after it has been processed."""
        state = self.load_state(session_id, project_path)

        state.pop("pending_approval_stage", None)
        state.pop("pending_approval_prompt", None)
        state.pop("pending_approval_timeout", None)
        state.pop("pending_approval_default", None)
        state.pop("pending_approval_requested_at", None)

        self.save_state(session_id, project_path, state)

    def list_pending_approvals(self, project_path: Path) -> list[dict[str, Any]]:
        """List all sessions with pending approvals.

        Args:
            project_path: Project directory

        Returns:
            List of pending approval info dicts
        """
        sessions = self.list_sessions(project_path)
        pending = []

        for session_info in sessions:
            session_id = session_info["session_id"]
            approval_info = self.get_pending_approval(session_id, project_path)
            if approval_info:
                pending.append(approval_info)

        return pending

    def check_approval_timeout(self, session_id: str, project_path: Path) -> ApprovalStatus | None:
        """Check if pending approval has timed out.

        Returns:
            ApprovalStatus if timeout occurred and was applied, None otherwise
        """
        approval_info = self.get_pending_approval(session_id, project_path)
        if not approval_info:
            return None

        requested_at = approval_info.get("approval_requested_at")
        timeout = approval_info.get("approval_timeout", 0)

        if not requested_at or timeout == 0:
            return None  # No timeout configured

        requested_time = datetime.datetime.fromisoformat(requested_at)
        elapsed = (datetime.datetime.now() - requested_time).total_seconds()

        if elapsed >= timeout:
            stage_name = approval_info["stage_name"]
            default = approval_info.get("approval_default", "deny")

            # Apply default action
            if default == "approve":
                self.set_stage_approval_status(
                    session_id, project_path, stage_name, ApprovalStatus.APPROVED, reason="Timeout - auto-approved"
                )
                self.clear_pending_approval(session_id, project_path)
                return ApprovalStatus.APPROVED

            self.set_stage_approval_status(
                session_id, project_path, stage_name, ApprovalStatus.TIMEOUT, reason="Timeout - auto-denied"
            )
            self.clear_pending_approval(session_id, project_path)
            return ApprovalStatus.TIMEOUT

        return None

    # === Cancellation Methods ===

    def get_cancellation_status(self, session_id: str, project_path: Path) -> CancellationStatus:
        """Get current cancellation status for a session.

        Args:
            session_id: Session identifier
            project_path: Project directory

        Returns:
            CancellationStatus for the session (NONE if session doesn't exist)
        """
        if not self.session_exists(session_id, project_path):
            return CancellationStatus.NONE

        try:
            state = self.load_state(session_id, project_path)
            status_str = state.get("cancellation_status", CancellationStatus.NONE.value)
            return CancellationStatus(status_str)
        except (FileNotFoundError, json.JSONDecodeError):
            return CancellationStatus.NONE

    def is_cancellation_requested(self, session_id: str, project_path: Path) -> bool:
        """Check if cancellation has been requested (graceful or immediate).

        Args:
            session_id: Session identifier
            project_path: Project directory

        Returns:
            True if cancellation requested (REQUESTED or IMMEDIATE status)
        """
        status = self.get_cancellation_status(session_id, project_path)
        return status in (CancellationStatus.REQUESTED, CancellationStatus.IMMEDIATE)

    def is_immediate_cancellation(self, session_id: str, project_path: Path) -> bool:
        """Check if immediate cancellation has been requested.

        Args:
            session_id: Session identifier
            project_path: Project directory

        Returns:
            True if immediate cancellation requested
        """
        return self.get_cancellation_status(session_id, project_path) == CancellationStatus.IMMEDIATE

    def request_cancellation(
        self,
        session_id: str,
        project_path: Path,
        immediate: bool = False,
    ) -> tuple[bool, str]:
        """Request cancellation of a running recipe session.

        First request triggers graceful cancellation (complete current step).
        Second request (or immediate=True) triggers immediate cancellation.

        Args:
            session_id: Session identifier
            project_path: Project directory
            immediate: If True, request immediate cancellation

        Returns:
            Tuple of (success, message) - success is True if request was processed
        """
        state = self.load_state(session_id, project_path)
        current_status = CancellationStatus(state.get("cancellation_status", CancellationStatus.NONE.value))

        if current_status == CancellationStatus.CANCELLED:
            return False, "Session already cancelled"

        now = datetime.datetime.now().isoformat()

        if immediate or current_status == CancellationStatus.REQUESTED:
            # Escalate to immediate
            state["cancellation_status"] = CancellationStatus.IMMEDIATE.value
            state["cancellation_requested_at"] = state.get("cancellation_requested_at", now)
            state["cancellation_escalated_at"] = now
            self.save_state(session_id, project_path, state)

            if current_status == CancellationStatus.REQUESTED:
                return True, "Escalated to immediate cancellation"
            return True, "Immediate cancellation requested"

        # First request - graceful
        state["cancellation_status"] = CancellationStatus.REQUESTED.value
        state["cancellation_requested_at"] = now
        self.save_state(session_id, project_path, state)
        return True, "Graceful cancellation requested (will complete current step)"

    def mark_cancelled(
        self,
        session_id: str,
        project_path: Path,
        cancelled_at_step: str | None = None,
        error: str | None = None,
    ) -> None:
        """Mark a session as cancelled (execution stopped).

        Args:
            session_id: Session identifier
            project_path: Project directory
            cancelled_at_step: Step ID where execution stopped
            error: Optional error message
        """
        state = self.load_state(session_id, project_path)

        was_immediate = state.get("cancellation_status") == CancellationStatus.IMMEDIATE.value
        state["cancellation_status"] = CancellationStatus.CANCELLED.value
        state["cancelled_at"] = datetime.datetime.now().isoformat()
        state["cancelled_at_step"] = cancelled_at_step
        state["cancellation_was_immediate"] = was_immediate
        if error:
            state["cancellation_error"] = error

        self.save_state(session_id, project_path, state)

    def clear_cancellation(self, session_id: str, project_path: Path) -> None:
        """Clear cancellation status (for resuming a cancelled session).

        Args:
            session_id: Session identifier
            project_path: Project directory
        """
        state = self.load_state(session_id, project_path)

        # Only clear if actually cancelled (not if in progress)
        if state.get("cancellation_status") == CancellationStatus.CANCELLED.value:
            state["cancellation_status"] = CancellationStatus.NONE.value
            # Keep history fields for debugging
            self.save_state(session_id, project_path, state)
