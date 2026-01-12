# *************************************************************
# Eclipse Tractus-X - Test Orchestrator Service
#
# Copyright (c) 2025 BMW AG
# Copyright (c) 2025 Contributors to the Eclipse Foundation
#
# See the NOTICE file(s) distributed with this work for additional
# information regarding copyright ownership.
#
# This program and the accompanying materials are made available under the
# terms of the Apache License, Version 2.0 which is available at
# https://www.apache.org/licenses/LICENSE-2.0.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations
# under the License.
#
# SPDX-License-Identifier: Apache-2.0
# *************************************************************

"""
Step-based Certificate Validation Endpoints (CX-0135).

This module provides step-based validation endpoints for Business Partner Certificates
and the Company Certificate Management API (CCMAPI). Each endpoint returns a structured
response with individual steps, making it easy to track progress and identify failures.

Endpoints:
    POST /cert-validation-test/     - Full certificate validation with feedback
    POST /feedbackmessage-validation/ - Validate feedback message schema
    POST /check                      - Run all certificate checks (aggregated)

The `verbose=true` query parameter enables detailed request/response metadata
in the response (with sensitive headers redacted).

Standards Reference:
    CX-0135 Company Certificate Management
    https://catenax-ev.github.io/docs/next/standards/CX-0135-CompanyCertificateManagement

Integration Notes:
    The step-based response format is designed for easy consumption by Java backends:
    
    {
        "status": "success" | "partial_success" | "failed",
        "message": "Human-readable summary",
        "steps": [
            {"step": "step_name", "status": "success" | "failed" | "warning", ...},
            ...
        ]
    }
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from test_orchestrator.auth import verify_auth
from test_orchestrator.certificate_utils import (
    send_feedback,
    run_certificate_checks,
    run_feedback_check,
    get_ccmapi_access,
    SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
    SEMANTIC_ID_FEEDBACK_MESSAGE_CONTENT,
    SEMANTIC_ID_BUSINESS_PARTNER_CERTIFICATE,
)
from test_orchestrator.api.ccm_test import validate_ccmapi_offer_setup
from test_orchestrator.errors import HTTPError, Error
from test_orchestrator.test_steps import step, overall_status, derive_result_status

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


async def validate_feedback_message_steps(
    payload: Dict,
    *,
    semantic_id_header: str = SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
    semantic_id_content: str = SEMANTIC_ID_FEEDBACK_MESSAGE_CONTENT,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Validate a feedback message against the semantic models.
    
    This test validates:
    1. The message header conforms to the MessageHeaderAspect schema
    2. The message content conforms to the MessageContentAspect schema
    
    Args:
        payload: The feedback message JSON to validate.
        semantic_id_header: Semantic model ID for header validation.
        semantic_id_content: Semantic model ID for content validation.
        verbose: If True, include detailed validation info in response.
    
    Returns:
        Step-based result dict with status, message, and steps list.
    """
    result: Dict[str, Any] = {
        "status": "success",
        "message": "Feedback message validation completed",
        "steps": [],
    }

    header_errors: Optional[Dict] = None
    content_errors: Optional[Dict] = None

    async with step(result, "validate_message_header", verbose=verbose) as s:
        header_errors, content_errors = run_feedback_check(
            semantic_id_header=semantic_id_header,
            semantic_id_content=semantic_id_content,
            validation_schema=payload,
        )
        if header_errors and header_errors.get("status") == "nok":
            s.set_warning(
                "HEADER_VALIDATION_ISSUES",
                f"Header validation found issues: {header_errors.get('errors', [])}"
            )
        if verbose:
            s["validation_result"] = header_errors

    async with step(result, "validate_message_content", verbose=verbose) as s:
        if content_errors and content_errors.get("status") == "nok":
            s.set_warning(
                "CONTENT_VALIDATION_ISSUES",
                f"Content validation found issues: {content_errors.get('errors', [])}"
            )
        if verbose:
            s["validation_result"] = content_errors

    derive_result_status(result)

    messages = {
        "success": "Feedback message is valid",
        "partial_success": "Feedback message has minor validation issues",
        "failed": "Feedback message validation failed",
    }
    result["message"] = messages.get(result["status"], "Feedback message validation completed")

    return result


async def validate_certificate_steps(
    payload: Dict,
    *,
    semantic_id_header: str = SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
    semantic_id_content: str = SEMANTIC_ID_BUSINESS_PARTNER_CERTIFICATE,
    contract_reference: bool = False,
    timeout: int = 80,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Validate a Business Partner Certificate and test the feedback mechanism.
    
    This test performs:
    1. Negotiate CCMAPI access from the sender's connector
    2. Send initial RECEIVED feedback
    3. Validate the certificate header against the MessageHeaderAspect schema
    4. Validate the certificate content against the BusinessPartnerCertificate schema
    5. Validate the CCMAPI offer setup (asset + policy)
    6. Send final ACCEPTED or REJECTED feedback based on validation
    
    Args:
        payload: Certificate JSON containing header and content.
        semantic_id_header: Semantic model ID for header validation.
        semantic_id_content: Semantic model ID for certificate validation.
        contract_reference: If True, validates policy includes ContractReference.
        timeout: Request timeout in seconds.
        verbose: If True, include request/response IO metadata in response.
    
    Returns:
        Step-based result dict with status, message, and steps list.
    """
    result: Dict[str, Any] = {
        "status": "success",
        "message": "Certificate validation completed",
        "steps": [],
    }

    # Extract header info for CCMAPI access
    header = payload.get("header", {})
    sender_feedback_url = header.get("senderFeedbackUrl")
    sender_bpn = header.get("senderBpn")

    if not sender_feedback_url or not sender_bpn:
        raise HTTPError(
            Error.BAD_REQUEST,
            message="Missing required header fields",
            details="Certificate must contain header.senderFeedbackUrl and header.senderBpn",
        )

    dataplane_url: Optional[str] = None
    dataplane_access_key: Optional[str] = None
    header_errors: Optional[Dict] = None
    cert_errors: Optional[Dict] = None
    validation_failed = False

    # Step 1: Negotiate CCMAPI access
    async with step(result, "negotiate_ccmapi_access", verbose=verbose) as s:
        dataplane_url, dataplane_access_key = await get_ccmapi_access(
            counter_party_address=sender_feedback_url,
            counter_party_id=sender_bpn,
            operand_left="http://purl.org/dc/terms/type",
            operand_right="%https://w3id.org/catenax/taxonomy#CCMAPI%",
            asset_validation=True,
            timeout=timeout,
        )
        if verbose:
            s["dataplane_url"] = dataplane_url

    # Step 2: Send initial RECEIVED feedback
    async with step(result, "send_received_feedback", verbose=verbose) as s:
        await send_feedback(
            payload, "RECEIVED", dataplane_url, dataplane_access_key, errors=[], timeout=timeout
        )
        if verbose:
            s["feedback_type"] = "RECEIVED"

    # Step 3: Validate certificate header
    async with step(result, "validate_certificate_header", verbose=verbose) as s:
        header_errors, cert_errors = run_certificate_checks(
            semantic_id_header=semantic_id_header,
            semantic_id_content=semantic_id_content,
            validation_schema=payload,
        )
        if header_errors and header_errors.get("status") == "nok":
            validation_failed = True
            s.set_warning(
                "HEADER_VALIDATION_FAILED",
                f"Header validation errors: {header_errors.get('errors', [])}"
            )
        if verbose:
            s["validation_result"] = header_errors

    # Step 4: Validate certificate content
    async with step(result, "validate_certificate_content", verbose=verbose) as s:
        if cert_errors and cert_errors.get("status") == "nok":
            validation_failed = True
            s.set_warning(
                "CERTIFICATE_VALIDATION_FAILED",
                f"Certificate validation errors: {cert_errors.get('errors', [])}"
            )
        if verbose:
            s["validation_result"] = cert_errors

    # Step 5: Validate CCMAPI offer setup (policy check)
    policy_warning = False
    async with step(result, "validate_ccmapi_offer", verbose=verbose) as s:
        offer_result = await validate_ccmapi_offer_setup(
            counter_party_address=sender_feedback_url,
            counter_party_id=sender_bpn,
            contract_reference=contract_reference,
            timeout=timeout,
            verbose=verbose,
        )
        if offer_result.get("status") != "success":
            policy_warning = True
            s.set_warning(
                "POLICY_VALIDATION_WARNING",
                "CCMAPI offer policy validation had issues. "
                "See CX-0135 CompanyCertificateManagement#216 Usage Policy."
            )
        if verbose:
            s["offer_validation"] = offer_result

    # Step 6: Send final feedback (REJECTED or ACCEPTED)
    async with step(result, "send_final_feedback", verbose=verbose) as s:
        if validation_failed:
            await send_feedback(
                payload, "REJECTED", dataplane_url, dataplane_access_key,
                errors=[cert_errors] if cert_errors else [], timeout=timeout
            )
            s["feedback_type"] = "REJECTED"
            s["status"] = "warning"
            s["message"] = "Sent REJECTED feedback due to validation failures"
        else:
            await send_feedback(
                payload, "ACCEPTED", dataplane_url, dataplane_access_key, errors=[], timeout=timeout
            )
            s["feedback_type"] = "ACCEPTED"

    derive_result_status(result)

    # Build final message
    if result["status"] == "success":
        result["message"] = "Certificate is valid and all feedback sent successfully"
    elif result["status"] == "partial_success":
        if policy_warning:
            result["message"] = (
                "Certificate validation completed with policy warnings. "
                "See https://catenax-ev.github.io/docs/next/standards/"
                "CX-0135-CompanyCertificateManagement#216-usage-policy"
            )
        else:
            result["message"] = "Certificate validation completed with warnings"
    else:
        result["message"] = "Certificate validation failed"

    return result


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/feedbackmessage-validation/",
    response_model=Dict,
    dependencies=[Depends(verify_auth)],
    summary="Validate feedback message schema",
    description="Validates a feedback message against the MessageHeaderAspect and MessageContentAspect schemas.",
)
async def feedback_message_validation_endpoint(
    payload: Dict,
    semantic_id_header: Optional[str] = SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
    semantic_id_content: Optional[str] = SEMANTIC_ID_FEEDBACK_MESSAGE_CONTENT,
    verbose: bool = False,
    timeout: int = 80,
) -> Dict:
    """
    Validate a feedback message against the semantic models.
    
    Args:
        payload: The feedback message JSON to validate.
        semantic_id_header: Semantic model ID for header validation.
        semantic_id_content: Semantic model ID for content validation.
        verbose: If True, includes detailed validation info in the response.
        timeout: Request timeout in seconds (unused, kept for API consistency).
    
    Returns:
        Step-based result with status and validation outcomes.
    """
    return await validate_feedback_message_steps(
        payload=payload,
        semantic_id_header=semantic_id_header or SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
        semantic_id_content=semantic_id_content or SEMANTIC_ID_FEEDBACK_MESSAGE_CONTENT,
        verbose=verbose,
    )


@router.post(
    "/cert-validation-test/",
    response_model=Dict,
    dependencies=[Depends(verify_auth)],
    summary="Validate certificate and test feedback",
    description="Full certificate validation against semantic model with CCMAPI feedback delivery.",
)
async def validate_certificate_endpoint(
    payload: Dict,
    semantic_id_header: Optional[str] = SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
    semantic_id_content: Optional[str] = SEMANTIC_ID_BUSINESS_PARTNER_CERTIFICATE,
    contract_reference: bool = False,
    verbose: bool = False,
    timeout: int = 80,
) -> Dict:
    """
    Validate a Business Partner Certificate and test the feedback mechanism.
    
    This is the main certificate validation endpoint that:
    1. Validates the certificate against the BusinessPartnerCertificate schema
    2. Negotiates access to the sender's CCMAPI asset
    3. Sends appropriate feedback (RECEIVED → ACCEPTED/REJECTED)
    4. Validates the CCMAPI offer policy
    
    Args:
        payload: Certificate JSON containing header and content.
        semantic_id_header: Semantic model ID for header validation.
        semantic_id_content: Semantic model ID for certificate validation.
        contract_reference: If True, validates policy includes ContractReference.
        verbose: If True, includes request/response metadata in the response.
        timeout: Request timeout in seconds.
    
    Returns:
        Step-based result with status and individual step outcomes.
    """
    return await validate_certificate_steps(
        payload=payload,
        semantic_id_header=semantic_id_header or SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
        semantic_id_content=semantic_id_content or SEMANTIC_ID_BUSINESS_PARTNER_CERTIFICATE,
        contract_reference=contract_reference,
        timeout=timeout,
        verbose=verbose,
    )


@router.post(
    "/check",
    response_model=Dict,
    dependencies=[Depends(verify_auth)],
    summary="Run all certificate checks",
    description="Aggregated endpoint that runs certificate validation + CCMAPI checks. "
                "Designed for easy integration with Java backends.",
)
async def cert_check(
    payload: Dict,
    semantic_id_header: Optional[str] = SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
    semantic_id_content: Optional[str] = SEMANTIC_ID_BUSINESS_PARTNER_CERTIFICATE,
    contract_reference: bool = False,
    verbose: bool = False,
    timeout: int = 80,
) -> Dict:
    """
    Run all CX-0135 certificate compliance checks.
    
    This aggregated endpoint runs:
    1. Certificate Validation - validates certificate schema and sends feedback
    2. CCMAPI Offer Test - validates asset and policy configuration
    
    The response includes individual results for each test, making it easy
    to identify which specific checks passed or failed.
    
    Args:
        payload: Certificate JSON containing header and content.
        semantic_id_header: Semantic model ID for header validation.
        semantic_id_content: Semantic model ID for certificate validation.
        contract_reference: If True, validates that policy includes ContractReference.
        verbose: If True, includes request/response metadata in the response.
        timeout: Request timeout in seconds.
    
    Returns:
        Aggregated result with overall status and individual test results.
    
    Example Response:
        {
            "status": "success",
            "message": "CX-0135 Certificate checks completed",
            "results": [
                {"status": "success", "message": "...", "steps": [...]},
                {"status": "success", "message": "...", "steps": [...]}
            ]
        }
    """
    header = payload.get("header", {})
    sender_feedback_url = header.get("senderFeedbackUrl")
    sender_bpn = header.get("senderBpn")

    # Run certificate validation
    cert_result = await validate_certificate_steps(
        payload=payload,
        semantic_id_header=semantic_id_header or SEMANTIC_ID_FEEDBACK_MESSAGE_HEADER,
        semantic_id_content=semantic_id_content or SEMANTIC_ID_BUSINESS_PARTNER_CERTIFICATE,
        contract_reference=contract_reference,
        timeout=timeout,
        verbose=verbose,
    )

    # Run CCMAPI offer validation (if we have the sender info)
    offer_result: Dict[str, Any] = {"status": "skipped", "message": "No sender info", "steps": []}
    if sender_feedback_url and sender_bpn:
        offer_result = await validate_ccmapi_offer_setup(
            counter_party_address=sender_feedback_url,
            counter_party_id=sender_bpn,
            contract_reference=contract_reference,
            timeout=timeout,
            verbose=verbose,
        )

    return {
        "status": overall_status([cert_result, offer_result]),
        "message": "CX-0135 Certificate checks completed",
        "results": [cert_result, offer_result],
    }
