# *************************************************************
# Eclipse Tractus-X - Test Orchestrator Service
# *************************************************************

"""
Shared helpers to build step-based test results.

This mirrors the step-based output structure used in `api/traceability_test.py`,
so other test modules can return a machine-readable list of steps that is easy
to call from other services (e.g., a Java backend).

When verbose=True, steps include full request/response metadata in the same
structure as make_request_verbose:
    - request: {method, url, headers, content}
    - response: {status_code, headers, text}
    - response_json: parsed JSON body
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from test_orchestrator.errors import HTTPError


class StepContext(dict):
    """
    Extended dict for step entries that provides helper methods for common operations.
    
    This allows cleaner code in step blocks by providing methods like `attach_io()`
    instead of manually copying fields from verbose responses.
    """
    
    def __init__(self, step_name: str, verbose: bool = False):
        super().__init__()
        self["step"] = step_name
        self["status"] = "running"
        self._verbose = verbose
    
    def attach_io(self, response: Optional[Dict[str, Any]]) -> None:
        """
        Attach request/response IO metadata from a verbose response dict.
        
        Mirrors the structure of make_request_verbose. When verbose is enabled,
        the step will include:
            - request: {method, url, headers (redacted), content}
            - response: {status_code, headers (redacted), text}
            - response_json: parsed JSON body
        
        Args:
            response: A dict with 'request', 'response', and 'response_json' keys
                      (from make_request_status_only, make_request_verbose, or 
                      get_catalog with verbose=True). If None or not verbose, does nothing.
        """
        if not self._verbose or not response:
            return
        
        # Copy the full request/response structure like make_request_verbose
        if response.get("request"):
            self["request"] = response["request"]
        if response.get("response"):
            self["response"] = response["response"]
        if response.get("response_json") is not None:
            self["response_json"] = response["response_json"]
    
    def set_warning(self, message: str, details: Optional[str] = None) -> None:
        """
        Mark this step as a warning (non-fatal issue).
        
        Args:
            message: Short warning message/code.
            details: Optional longer explanation.
        """
        self["status"] = "warning"
        self["message"] = message
        if details:
            self["details"] = details
    
    def set_skipped(self, message: str) -> None:
        """Mark this step as skipped."""
        self["status"] = "skipped"
        self["message"] = message


@asynccontextmanager
async def step(result: Dict[str, Any], step_name: str, *, verbose: bool = False):
    """
    Context manager for a single step in a step-based test result.
    
    This context manager:
    - Appends a step entry to result["steps"]
    - Yields a StepContext (dict subclass) with helper methods
    - On success, marks the step as "success" (unless manually changed)
    - On exception, marks it as "failed" and re-raises
    
    Args:
        result: The overall result dict to append steps to. Must be mutable.
        step_name: Name identifier for this step.
        verbose: If True, step will include detailed IO data when attach_io() is called.
    
    Yields:
        StepContext: A dict-like object for the step with helper methods:
            - attach_io(response): Attach full request/response metadata
            - set_warning(message, details): Mark step as warning
            - set_skipped(message): Mark step as skipped
    
    Example:
        async with step(result, "get_catalog", verbose=True) as s:
            response = await get_catalog(..., verbose=True)
            s.attach_io(response)
            catalog_json = response.get("response_json", {})
    """
    step_entry = StepContext(step_name, verbose=verbose)
    result.setdefault("steps", []).append(step_entry)

    try:
        yield step_entry
        # Only mark success if the step didn't explicitly change its own status
        # (e.g., warning/skipped).
        if step_entry.get("status") == "running":
            step_entry["status"] = "success"
    except HTTPError as e:
        step_entry["status"] = "failed"
        step_entry["message"] = str(e)
        if verbose and getattr(e, "details", None) is not None:
            step_entry["details"] = e.details
        result["status"] = "failed"
        raise
    except Exception as e:
        step_entry["status"] = "failed"
        step_entry["message"] = f"Unexpected error: {e}"
        if verbose:
            step_entry["details"] = {"exception": repr(e)}
        result["status"] = "failed"
        raise


def overall_status(results: list[dict]) -> str:
    """
    Determine overall status from a list of individual test results.
    
    Args:
        results: List of result dicts, each with a 'status' key.
    
    Returns:
        - "success" if all results are successful
        - "partial_success" if at least one succeeded but others failed
        - "failed" if none succeeded
    """
    statuses = {r.get("status") for r in results}
    if statuses == {"success"}:
        return "success"
    if "success" in statuses:
        return "partial_success"
    return "failed"


def derive_result_status(result: Dict[str, Any]) -> None:
    """
    Update result['status'] based on step outcomes.
    
    This is a helper to derive the overall status from the steps after all
    steps have been executed. Modifies result in-place.
    
    Args:
        result: A result dict with 'steps' list. Will update 'status' key.
    """
    steps = result.get("steps", [])
    if any(st.get("status") == "failed" for st in steps):
        result["status"] = "failed"
    elif any(st.get("status") == "warning" for st in steps):
        result["status"] = "partial_success"
    else:
        result["status"] = "success"
