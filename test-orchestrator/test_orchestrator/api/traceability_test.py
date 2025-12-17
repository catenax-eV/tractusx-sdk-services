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
Provide FastAPI endpoints for data asset checks.

"""
from fastapi import APIRouter, Depends
from test_orchestrator import config
from test_orchestrator.auth import verify_auth
from test_orchestrator.checks.create_notification import (
    qualitynotification_receive,
    qualitynotification_update,
)
from test_orchestrator.checks.policy_validation import validate_policy
from test_orchestrator.checks.catalog_version_validation import validate_catalog_version
from test_orchestrator.checks.request_catalog import get_catalog
from test_orchestrator.errors import HTTPError
from test_orchestrator.logging.log_manager import LoggingManager
from test_orchestrator.utils import init_negotiation, obtain_negotiation_state, get_data_address

router = APIRouter()
logger = LoggingManager.get_logger(__name__)


async def run_step(step_name: str, func, asset_result, **kwargs):
    """Run a step with standardized logging, error handling, and step recording."""
    try:
        result = await func(**kwargs) if callable(func) else func
        asset_result['steps'].append({
            'step': step_name,
            'status': 'success',
            'details': result if isinstance(result, dict) else None
        })
        return result, True
    except HTTPError as e:
        asset_result['steps'].append({
            'step': step_name,
            'status': 'failed',
            'message': str(e),
            'details': getattr(e, 'details', None)
        })
    except Exception as e:
        asset_result['steps'].append({
            'step': step_name,
            'status': 'failed',
            'message': f'Unexpected error: {e}'
        })
    asset_result['status'] = 'failed'
    return None, False


async def invoke_notification(asset, endpoint, authorization, job_id, asset_id, counter_party_id, asset_result):
    """Invoke the correct notification operation based on asset type."""
    operations = {
        'receive': (qualitynotification_receive, 'Receive invoked successfully'),
        'update': (qualitynotification_update, 'Update invoked successfully')
    }

    op_type = next((k for k in operations if k in asset['dct_type_id'].lower()), None)
    if op_type:
        func, msg = operations[op_type]
        await run_step(
            f'invoke_{op_type}',
            func,
            asset_result,
            endpoint=endpoint,
            authorization=authorization,
            notification_type=asset['notificationType'],
            job_id=job_id,
            sender_bpn=config.SENDER_BPN,
            receiver_bpn=counter_party_id,
            **({'asset_id': asset_id} if op_type == 'receive' else {})
        )
        asset_result['message'] = msg
    else:
        asset_result['steps'].append({
            'step': 'invoke_operation',
            'status': 'skipped',
            'message': 'No matching operation for asset type'
        })


async def process_asset(asset, counter_party_address, counter_party_id, job_id, asset_id):
    """Process all steps for a single asset and return its result."""
    asset_result = {
        'asset_id': asset['asset_id'],
        'dct_type_id': asset['dct_type_id'],
        'status': 'success',
        'message': None,
        'steps': []
    }

    # Step 1: Get catalog
    catalog_response, proceed = await run_step(
        'get_catalog',
        get_catalog,
        asset_result,
        counter_party_address=counter_party_address,
        counter_party_id=counter_party_id,
        operand_left="'http://purl.org/dc/terms/type'.'@id'",
        operand_right=f'https://w3id.org/catenax/taxonomy#{asset["dct_type_id"]}'
    )

    # Step 2: Validate policy
    if proceed:
        def policy_func():
            return validate_policy(catalog_response['response_json'], asset['dct_type_id'], "traceability:1.0")

        _, proceed = await run_step('validate_policy', policy_func, asset_result)
    else:
        asset_result['steps'].append(
            {'step': 'validate_policy', 'status': 'skipped', 'message': 'Previous step failed'})

    # Step 3: Validate catalog version
    if proceed:
        def catalog_version_func():
            return validate_catalog_version(catalog_response.get('response_json', {}), asset['dct_type_id'], '2.0')

        _, proceed = await run_step('validate_catalog_version', catalog_version_func, asset_result)
    else:
        asset_result['steps'].append(
            {'step': 'validate_catalog_version', 'status': 'skipped', 'message': 'Previous step failed'})

    # Step 4: Initiate negotiation
    if proceed:
        negotiation, proceed = await run_step(
            'init_negotiation',
            init_negotiation,
            asset_result,
            counter_party_address=counter_party_address,
            counter_party_id=counter_party_id,
            catalog_json=catalog_response.get('response_json', {}),
            operand_right=asset['dct_type_id']
        )
    else:
        negotiation = None

    # Step 5: Obtain negotiation state
    edr_state_id = negotiation.get('@id') if negotiation else None
    if proceed:
        _, proceed = await run_step(
            'obtain_negotiation_state',
            obtain_negotiation_state,
            asset_result,
            counter_party_address=counter_party_address,
            counter_party_id=counter_party_id,
            edr_state_id=edr_state_id,
            operand_right=asset['dct_type_id']
        )

    # Step 6: Get EDR data address
    if proceed:
        edr_data_address, proceed = await run_step(
            'get_data_address',
            get_data_address,
            asset_result,
            counter_party_address=counter_party_address,
            counter_party_id=counter_party_id,
            edr_state_id=edr_state_id
        )
    else:
        edr_data_address = None

    endpoint = edr_data_address.get('endpoint') if edr_data_address else None
    authorization = edr_data_address.get('authorization') if edr_data_address else None

    # Step 7: Invoke notification operation
    await invoke_notification(asset, endpoint, authorization, job_id, asset_id, counter_party_id, asset_result)

    return asset_result


def get_overall_status(results):
    """Determine the overall status from individual asset results."""
    statuses = {r['status'] for r in results}
    if statuses == {'success'}:
        return 'success'
    if 'success' in statuses:
        return 'partial_success'
    return 'failed'


@router.post('/check', dependencies=[Depends(verify_auth)])
async def traceability_test(counter_party_address: str, counter_party_id: str, job_id: str, asset_id: str):
    """Endpoint to run traceability tests for multiple assets."""
    data_assets = [{
        'dct_type_id': 'ReceiveQualityInvestigationNotification',
        'asset_id': 'qualityinvestigationnotification-receive',
        'notificationType': 'Traceability-QualityNotification-Investigation:2.0.0'
    }, {
        'dct_type_id': 'ReceiveQualityAlertNotification',
        'asset_id': 'qualityalertnotification-receipt',
        'notificationType': 'Traceability-QualityNotification-Alert:2.0.0'
    }, {
        'dct_type_id': 'UpdateQualityInvestigationNotification',
        'asset_id': 'qualityinvestigationnotification-update',
        'notificationType': 'Traceability-QualityNotification-Investigation:2.0.0'
    }, {
        'dct_type_id': 'UpdateQualityAlertNotification',
        'asset_id': 'qualityalertnotification-update',
        'notificationType': 'Traceability-QualityNotification-Alert:2.0.0'
    }]

    results = [await process_asset(asset, counter_party_address, counter_party_id, job_id, asset_id)
               for asset in data_assets]

    return {
        'status': get_overall_status(results),
        'message': 'CX-0125 Traceability checks completed',
        'results': results
    }
