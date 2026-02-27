# *************************************************************
# Eclipse Tractus-X - Test Orchestrator Service
# *************************************************************

from fastapi import APIRouter, Depends
from test_orchestrator.test_steps import step, overall_status
from test_orchestrator import config
from test_orchestrator.auth import verify_auth
from test_orchestrator.checks.catalog_version_validation import validate_catalog_version
from test_orchestrator.checks.create_notification import (
    qualitynotification_receive,
    qualitynotification_update,
)
from test_orchestrator.checks.policy_validation import validate_policy
from test_orchestrator.checks.request_catalog import get_catalog
from test_orchestrator.errors import HTTPError
from test_orchestrator.logging.log_manager import LoggingManager
from test_orchestrator.utils import (
    get_data_address,
    init_negotiation,
    obtain_negotiation_state,
)

router = APIRouter()
logger = LoggingManager.get_logger(__name__)

async def invoke_notification(asset, endpoint, authorization, job_id, asset_id, counter_party_id, asset_result):
    """Invoke the correct notification operation based on asset type."""
    operations = {
        "receive": (qualitynotification_receive, "Receive invoked successfully"),
        "update": (qualitynotification_update, "Update invoked successfully")
    }
    op_type = next((k for k in operations if k in asset["dct_type_id"].lower()), None)

    if not op_type:
        asset_result["steps"].append({
            "step": "invoke_operation",
            "status": "skipped",
            "message": "No matching operation for asset type"
        })
        return

    func, msg = operations[op_type]

    async with step(asset_result, f"invoke_{op_type}"):
        kwargs = dict(
            endpoint=endpoint,
            authorization=authorization,
            notification_type=asset["notificationType"],
            job_id=job_id,
            sender_bpn=config.SENDER_BPN,
            receiver_bpn=counter_party_id
        )
        if op_type == "receive":
            kwargs["asset_id"] = asset_id

        await func(**kwargs)
        asset_result["message"] = msg


async def process_asset(asset, counter_party_address, counter_party_id, job_id, asset_id):
    """Process all steps for a single asset and return its result."""
    asset_result = {
        "asset_id": asset["asset_id"],
        "dct_type_id": asset["dct_type_id"],
        "status": "success",
        "message": None,
        "steps": []
    }

    try:
        # Step 1: Get catalog
        async with step(asset_result, "get_catalog"):
            catalog_response = await get_catalog(
                counter_party_address=counter_party_address,
                counter_party_id=counter_party_id,
                operand_left="'http://purl.org/dc/terms/type'.'@id'",
                operand_right=f"https://w3id.org/catenax/taxonomy#{asset['dct_type_id']}"
            )

        # Step 2: Validate policy
        async with step(asset_result, "validate_policy"):
            validate_policy(catalog_response["response_json"], asset["dct_type_id"], "traceability:1.0")

        # Step 3: Validate catalog version
        async with step(asset_result, "validate_catalog_version"):
            validate_catalog_version(catalog_response.get("response_json", {}), asset["dct_type_id"], "2.0")

        # Step 4: Initiate negotiation
        async with step(asset_result, "init_negotiation"):
            negotiation = await init_negotiation(
                counter_party_address=counter_party_address,
                counter_party_id=counter_party_id,
                catalog_json=catalog_response.get("response_json", {}),
                operand_right=asset["dct_type_id"]
            )

        edr_state_id = negotiation.get("@id") if negotiation else None

        # Step 5: Obtain negotiation state
        async with step(asset_result, "obtain_negotiation_state"):
            await obtain_negotiation_state(
                counter_party_address=counter_party_address,
                counter_party_id=counter_party_id,
                edr_state_id=edr_state_id,
                operand_right=asset["dct_type_id"]
            )

        # Step 6: Get EDR data address
        async with step(asset_result, "get_data_address"):
            edr_data_address = await get_data_address(
                counter_party_address=counter_party_address,
                counter_party_id=counter_party_id,
                edr_state_id=edr_state_id
            )

        endpoint = edr_data_address.get("endpoint")
        authorization = edr_data_address.get("authorization")

        # Step 7: Invoke notification
        await invoke_notification(asset, endpoint, authorization, job_id, asset_id, counter_party_id, asset_result)

    except Exception:
        # Fail-fast: stop processing this asset if any critical step failed
        pass

    return asset_result


@router.post("/check", dependencies=[Depends(verify_auth)])
async def traceability_test(counter_party_address: str, counter_party_id: str, job_id: str, asset_id: str):
    """Endpoint to run traceability tests for multiple assets."""
    data_assets = [
        {
            "dct_type_id": "ReceiveQualityInvestigationNotification",
            "asset_id": "qualityinvestigationnotification-receive",
            "notificationType": "Traceability-QualityNotification-Investigation:2.0.0"
        },
        {
            "dct_type_id": "ReceiveQualityAlertNotification",
            "asset_id": "qualityalertnotification-receipt",
            "notificationType": "Traceability-QualityNotification-Alert:2.0.0"
        },
        {
            "dct_type_id": "UpdateQualityInvestigationNotification",
            "asset_id": "qualityinvestigationnotification-update",
            "notificationType": "Traceability-QualityNotification-Investigation:2.0.0"
        },
        {
            "dct_type_id": "UpdateQualityAlertNotification",
            "asset_id": "qualityalertnotification-update",
            "notificationType": "Traceability-QualityNotification-Alert:2.0.0"
        }
    ]

    results = [await process_asset(asset, counter_party_address, counter_party_id, job_id, asset_id)
               for asset in data_assets]

    return {
        "status": overall_status(results),
        "message": "CX-0125 Traceability checks completed",
        "results": results
    }
