# *************************************************************
# Eclipse Tractus-X - Test Orchestrator Service
# *************************************************************

"""
Step-based CCM/Certificate GET checks (CX-0135).

This module provides step-based validation endpoints for Company Certificate Management
(CCM) that can be called from external backends (e.g., Java services). Each endpoint
returns a structured response with individual steps, making it easy to track progress
and identify failures.

Endpoints:
    GET /ccmapi-offer-test/     - Validate CCMAPI offer setup (asset, policy)
    GET /feedbackmechanism-validation/ - Test feedback mechanism (negotiate + send)
    POST /check                 - Run all CCM checks (aggregated)

The `verbose=true` query parameter enables detailed request/response metadata
in the response (with sensitive headers redacted).

Standards Reference:
    CX-0135 Company Certificate Management
    https://catenax-ev.github.io/docs/next/standards/CX-0135-CompanyCertificateManagement
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Tuple

from fastapi import APIRouter, Depends

from test_orchestrator import config
from test_orchestrator.auth import get_dt_pull_service_headers, verify_auth
from test_orchestrator.certificate_utils import (
    build_feedback_message,
    check_for_single_ccmapi_asset,
    validate_policy as validate_ccm_policy,
)
from test_orchestrator.checks.request_catalog import get_catalog
from test_orchestrator.errors import Error, HTTPError
from test_orchestrator.request_handler import make_request_status_only
from test_orchestrator.test_steps import overall_status, step, derive_result_status

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _extract_single_ccmapi_asset(catalog_json: dict) -> Tuple[str, Optional[dict]]:
    """
    Validate catalog contains exactly one CCMAPI asset and return (asset_id, policies).
    
    Args:
        catalog_json: The catalog response from the EDC.
    
    Returns:
        Tuple of (asset_id, policies dict or None).
    
    Raises:
        HTTPError: If asset extraction fails.
    """
    check_for_single_ccmapi_asset(catalog_json)
    dataset = catalog_json.get("dcat:dataset")
    if isinstance(dataset, dict):
        asset_id = dataset.get("@id")
        policies = dataset.get("odrl:hasPolicy")
        return asset_id, policies
    # Fallback (should not happen due to check_for_single_ccmapi_asset)
    raise HTTPError(
        Error.ASSET_NOT_FOUND,
        message="Please check asset/policy/contractdefinition configuration",
        details="The CCMAPI asset could not be extracted from the catalog response.",
    )


async def validate_ccmapi_offer_setup(
    counter_party_address: str,
    counter_party_id: str,
    *,
    contract_reference: bool = True,
    timeout: int = 80,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Validate the CCMAPI offer setup for a given connector.
    
    This test validates:
    1. The catalog can be fetched and contains a CCMAPI asset
    2. Exactly one CCMAPI asset exists (per CX-0135 standard)
    3. The usage policy matches the expected configuration
    
    Args:
        counter_party_address: DSP endpoint URL of the target connector.
        counter_party_id: BPN of the target partner.
        contract_reference: If True, validates policy includes ContractReference.
        timeout: Request timeout in seconds.
        verbose: If True, include request/response additional metadata in response.
    
    Returns:
        Step-based result dict with status, message, and steps list.
    """
    result: Dict[str, Any] = {
        "status": "success",
        "message": "CCMAPI offer test completed",
        "steps": [],
    }

    catalog_json: dict = {}
    policies: Optional[dict] = None

    async with step(result, "get_catalog", verbose=verbose) as s:
        catalog_response = await get_catalog(
            counter_party_address=counter_party_address,
            counter_party_id=counter_party_id,
            operand_left="http://purl.org/dc/terms/type",
            operand_right="%https://w3id.org/catenax/taxonomy#CCMAPI%",
            timeout=timeout,
            verbose=verbose,
        )
        catalog_json = catalog_response.get("response_json", {})
        s.attach_io(catalog_response)

    async with step(result, "extract_asset_and_policy", verbose=verbose):
        _, policies = _extract_single_ccmapi_asset(catalog_json)

    async with step(result, "validate_policy", verbose=verbose) as s:
        exists = validate_ccm_policy(policies=policies, contract_reference=contract_reference)
        if not exists:
            s.set_warning(
                "POLICY_VALIDATION_FAILED",
                "The usage policy that is used for the asset is not accurate. "
                "See CX-0135 CompanyCertificateManagement#216 Usage Policy."
            )

    # Derive overall status from steps
    derive_result_status(result)

    # Set appropriate message based on status
    messages = {
        "success": "CCMAPI Offer is set up correctly",
        "partial_success": "CCMAPI Offer is reachable, but policy validation emitted warnings",
        "failed": "CCMAPI offer test failed",
    }
    result["message"] = messages.get(result["status"], "CCMAPI offer test completed")

    return result


async def feedback_mechanism_validation_steps(
    counter_party_address: str,
    counter_party_id: str,
    *,
    message_type: Literal["RECEIVED", "ACCEPTED", "REJECTED"] = "RECEIVED",
    timeout: int = 80,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Validate the feedback mechanism of a CCMAPI implementation.
    
    This test performs a full EDR negotiation flow and sends a feedback message:
    1. Fetch catalog and validate single CCMAPI asset exists
    2. Initiate contract negotiation
    3. Wait for negotiation to finalize
    4. Query transfer process
    5. Get data address (endpoint + authorization)
    6. Send feedback message of specified type
    
    Args:
        counter_party_address: DSP endpoint URL of the target connector.
        counter_party_id: BPN of the target partner.
        message_type: Type of feedback to send ("RECEIVED", "ACCEPTED", "REJECTED").
        timeout: Request timeout in seconds.
        verbose: If True, include request/response IO metadata in response.
    
    Returns:
        Step-based result dict with status, message, and steps list.
    """
    result: Dict[str, Any] = {
        "status": "success",
        "message": "Feedback mechanism validation completed",
        "steps": [],
    }

    # Step 1: Catalog lookup (CCMAPI)
    catalog_json: dict = {}
    async with step(result, "get_catalog", verbose=verbose) as s:
        catalog_response = await get_catalog(
            counter_party_address=counter_party_address,
            counter_party_id=counter_party_id,
            operand_left="http://purl.org/dc/terms/type",
            operand_right="%https://w3id.org/catenax/taxonomy#CCMAPI%",
            timeout=timeout,
            verbose=verbose,
        )
        catalog_json = catalog_response.get("response_json", {})
        s.attach_io(catalog_response)

    async with step(result, "validate_single_asset", verbose=verbose):
        check_for_single_ccmapi_asset(catalog_json)

    # Step 2: Negotiate access (init-negotiation)
    edr_state_id: Optional[str] = None
    async with step(result, "init_negotiation", verbose=verbose) as s:
        init_response = await make_request_status_only(
            "POST",
            f"{config.DT_PULL_SERVICE_ADDRESS}/edr/init-negotiation/",
            timeout=timeout,
            params={"counter_party_address": counter_party_address, "counter_party_id": counter_party_id},
            json=catalog_json,
            headers=get_dt_pull_service_headers(),
        )
        s.attach_io(init_response)
        init_json = init_response.get("response_json")
        if not isinstance(init_json, dict) or "@id" not in init_json:
            raise HTTPError(
                Error.CONTRACT_NEGOTIATION_FAILED,
                message="Access to the CCMAPI asset could not be negotiated.",
                details="Unexpected response from init-negotiation.",
            )
        edr_state_id = init_json.get("@id")

    # Step 3: Wait for negotiation finalized
    async with step(result, "obtain_negotiation_state", verbose=verbose) as s:
        state_response = await make_request_status_only(
            "GET",
            f"{config.DT_PULL_SERVICE_ADDRESS}/edr/negotiation-state/",
            timeout=timeout,
            params={"counter_party_address": counter_party_address, "counter_party_id": counter_party_id, "state_id": edr_state_id},
            headers=get_dt_pull_service_headers(),
        )
        s.attach_io(state_response)
        state_json = state_response.get("response_json")
        if isinstance(state_json, dict) and state_json.get("state") == "TERMINATED":
            raise HTTPError(
                Error.CONTRACT_NEGOTIATION_FAILED,
                message="Contract negotiation terminated.",
                details=state_json,
            )
        if not isinstance(state_json, dict) or state_json.get("state") != "FINALIZED":
            current_state = state_json.get("state", "UNKNOWN") if isinstance(state_json, dict) else "UNKNOWN"
            raise HTTPError(
                Error.CONTRACT_NEGOTIATION_FAILED,
                message=f"Contract negotiation stuck in state {current_state}",
                details=state_json,
            )

    # Step 4: Transfer-process query
    transfer_process_id: Optional[str] = None
    async with step(result, "fetch_transfer_process", verbose=verbose) as s:
        query_spec = {
            "@context": {"@vocab": "https://w3id.org/edc/v0.0.1/ns/"},
            "@type": "QuerySpec",
            "filterExpression": [
                {"operandLeft": "contractNegotiationId", "operator": "=", "operandRight": edr_state_id}
            ],
        }
        tp_response = await make_request_status_only(
            "POST",
            f"{config.DT_PULL_SERVICE_ADDRESS}/edr/transfer-process/",
            timeout=timeout,
            params={"counter_party_address": counter_party_address, "counter_party_id": counter_party_id},
            json=query_spec,
            headers=get_dt_pull_service_headers(),
        )
        s.attach_io(tp_response)
        tp_json = tp_response.get("response_json")
        if not isinstance(tp_json, list) or not tp_json:
            raise HTTPError(
                Error.DATA_TRANSFER_FAILED,
                message="Could not retrieve transfer process.",
                details=tp_json,
            )
        transfer_process_id = tp_json[0].get("transferProcessId")
        if not transfer_process_id:
            raise HTTPError(
                Error.DATA_TRANSFER_FAILED,
                message="Transfer process response did not contain transferProcessId.",
                details=tp_json[0],
            )

    # Step 5: Data address
    dataplane_url: Optional[str] = None
    dataplane_access_key: Optional[str] = None
    async with step(result, "get_data_address", verbose=verbose) as s:
        da_response = await make_request_status_only(
            "GET",
            f"{config.DT_PULL_SERVICE_ADDRESS}/edr/data-address/",
            timeout=timeout,
            params={
                "counter_party_address": counter_party_address,
                "counter_party_id": counter_party_id,
                "transfer_process_id": transfer_process_id,
            },
            headers=get_dt_pull_service_headers(),
        )
        s.attach_io(da_response)
        da_json = da_response.get("response_json")
        if not isinstance(da_json, dict):
            raise HTTPError(Error.BAD_GATEWAY, message="Unexpected data-address response", details=da_json)
        dataplane_url = da_json.get("endpoint")
        dataplane_access_key = da_json.get("authorization")
        if not dataplane_url or not dataplane_access_key:
            raise HTTPError(
                Error.BAD_GATEWAY,
                message="Data-address response did not contain endpoint/authorization.",
                details=da_json,
            )

    # Step 6: Send feedback (selected type)
    async with step(result, "send_feedback", verbose=verbose) as s:
        payload = {
            "header": {
                "senderFeedbackUrl": counter_party_address,
                "receiverBpn": counter_party_id,
                "senderBpn": counter_party_id,
            },
            "content": {},
        }
        errors = _build_rejection_errors() if message_type == "REJECTED" else []
        message_body = build_feedback_message(payload=payload, status=message_type, errors=errors)
        
        feedback_response = await make_request_status_only(
            "POST",
            f"{config.DT_PULL_SERVICE_ADDRESS}/dtr/send-feedback/",
            timeout=timeout,
            params={"dataplane_url": dataplane_url},
            json=message_body,
            headers=get_dt_pull_service_headers(headers={"Authorization": dataplane_access_key}),
        )
        s.attach_io(feedback_response)
        if verbose:
            s["result"] = feedback_response.get("response_json")

    return result


def _build_rejection_errors() -> list:
    """Build sample rejection errors for testing REJECTED feedback messages."""
    return [
        {
            "certificateErrors": [
                {"message": "We do not process certificates on Sunday"},
                {"message": "Certificate has expired in 2024"},
                {"message": "Certificate was revoked"},
                {"message": "Unexpected data format"},
                {"message": "Unexpected language expected English, received Mandarin"},
                {"message": "Expected PDF, received JPG"},
                {"message": "Unknown BPNL000000000000"},
            ],
            "locationErrors": [
                {"bpn": "BPNS000000000002", "locationErrors": [{"message": "Site BPNS000000000002 has been Rejected"}]},
                {"bpn": "BPNS000000000003", "locationErrors": [{"message": "Site BPNS000000000003 is missing"}]},
            ],
        }
    ]


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/ccmapi-offer-test/",
    response_model=Dict,
    dependencies=[Depends(verify_auth)],
    summary="Validate CCMAPI offer setup",
    description="Tests if the CCMAPI asset and policy are correctly configured per CX-0135.",
)
async def ccmapi_offer_test_endpoint(
    counter_party_address: str,
    counter_party_id: str,
    contract_reference: bool = True,
    verbose: bool = False,
    timeout: int = 80,
) -> Dict:
    """
    Validate the CCMAPI offer setup for a connector.
    
    Args:
        counter_party_address: DSP endpoint URL (e.g., https://connector.example.com/api/v1/dsp)
        counter_party_id: Business Partner Number (BPN) of the target
        contract_reference: If True, validates that policy includes ContractReference
        verbose: If True, includes request/response metadata in the response
        timeout: Request timeout in seconds
    
    Returns:
        Step-based result with status and individual step outcomes.
    """
    return await validate_ccmapi_offer_setup(
        counter_party_address=counter_party_address,
        counter_party_id=counter_party_id,
        contract_reference=contract_reference,
        verbose=verbose,
        timeout=timeout,
    )


@router.get(
    "/feedbackmechanism-validation/",
    response_model=Dict,
    dependencies=[Depends(verify_auth)],
    summary="Validate feedback mechanism",
    description="Tests the full EDR negotiation flow and feedback delivery per CX-0135.",
)
async def feedback_mechanism_validation_endpoint(
    counter_party_address: str,
    counter_party_id: str,
    message_type: Optional[Literal["RECEIVED", "ACCEPTED", "REJECTED"]] = "RECEIVED",
    verbose: bool = False,
    timeout: int = 80,
) -> Dict:
    """
    Validate the feedback mechanism of a CCMAPI implementation.
    
    Args:
        counter_party_address: DSP endpoint URL
        counter_party_id: Business Partner Number (BPN) of the target
        message_type: Type of feedback message to send
        verbose: If True, includes request/response metadata in the response
        timeout: Request timeout in seconds
    
    Returns:
        Step-based result with status and individual step outcomes.
    """
    return await feedback_mechanism_validation_steps(
        counter_party_address=counter_party_address,
        counter_party_id=counter_party_id,
        message_type=message_type or "RECEIVED",
        verbose=verbose,
        timeout=timeout,
    )


@router.post(
    "/check",
    response_model=Dict,
    dependencies=[Depends(verify_auth)],
    summary="Run all CCM checks",
    description="Aggregated endpoint that runs CCMAPI offer test + feedback validation. "
                "Designed for easy integration with Java backends.",
)
async def ccm_check(
    counter_party_address: str,
    counter_party_id: str,
    contract_reference: bool = True,
    message_type: Optional[Literal["RECEIVED", "ACCEPTED", "REJECTED"]] = "RECEIVED",
    verbose: bool = False,
    timeout: int = 80,
) -> Dict:
    """
    Run all CX-0135 CCM compliance checks.
    
    This aggregated endpoint runs:
    1. CCMAPI Offer Test - validates asset and policy configuration
    2. Feedback Mechanism Validation - tests EDR flow and feedback delivery
    
    The response includes individual results for each test, making it easy
    to identify which specific checks passed or failed.
    
    Args:
        counter_party_address: DSP endpoint URL
        counter_party_id: Business Partner Number (BPN) of the target
        contract_reference: If True, validates that policy includes ContractReference
        message_type: Type of feedback message to send in feedback test
        verbose: If True, includes request/response metadata in the response
        timeout: Request timeout in seconds
    
    Returns:
        Aggregated result with overall status and individual test results.
    
    Example Response:
        {
            "status": "success",
            "message": "CX-0135 CCM checks completed",
            "results": [
                {"status": "success", "message": "...", "steps": [...]},
                {"status": "success", "message": "...", "steps": [...]}
            ]
        }
    """
    offer = await validate_ccmapi_offer_setup(
        counter_party_address=counter_party_address,
        counter_party_id=counter_party_id,
        contract_reference=contract_reference,
        timeout=timeout,
        verbose=verbose,
    )
    feedback = await feedback_mechanism_validation_steps(
        counter_party_address=counter_party_address,
        counter_party_id=counter_party_id,
        message_type=message_type or "RECEIVED",
        timeout=timeout,
        verbose=verbose,
    )

    return {
        "status": overall_status([offer, feedback]),
        "message": "CX-0135 CCM checks completed",
        "results": [offer, feedback],
    }


