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
Provides FastAPI endpoints for verifying Digital Twin availability and
submodel schema compliance.

This module defines API endpoints used to validate Digital Twin presence in a
partner's Digital Twin Registry (DTR), and perform schema validation of
referenced submodels. These endpoints support the test orchestration workflows
required to ensure interoperability within the Catena-X ecosystem.

The primary goal is to confirm that participants correctly implement DTR
integration, DT retrieval, and submodel provisioning according to Catena-X
specifications.

Endpoints:
- POST /data-transfer/: Verifies Digital Twin availability in the partner DTR.
- POST /schema-validation/: Checks partner submodels against semantic schemas.
"""

import logging
from typing import Dict

from fastapi import APIRouter, Depends

from test_orchestrator.auth import verify_auth
from test_orchestrator.base_utils import submodel_validation
from test_orchestrator.utils.submodel_schema_validation import (
    process_and_retrieve_dtr,
)

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post('/check',
             response_model=Dict,
             dependencies=[Depends(verify_auth)])
async def submodel_schema_validation(
    counter_party_address: str,
    counter_party_id: str,
    asset_id: str,
    submodel_semantic_id: str,
    timeout: int = 80,
):
    """
    Endpoint to validate partner submodels against semantic schemas.

    Steps performed:
    1. Retrieve Digital Twin Registry (DTR) shell descriptors for the provided events
       using the partner's address and ID.
    2. For each shell descriptor, perform submodel schema validation to ensure
       compliance with Catena-X standards.
    3. Return a success message if all validations pass.

    - :param counter_party_address: Address of the dsp endpoint of a connector
                                    (ends on api/v1/dsp for DSP version 2024-01).
    - :param counter_party_id: The identifier of the test subject that operates the connector.
    - :param list_of_events: List of event dicts containing catenaXId and submodelSemanticId.
    - :param timeout: Timeout for external requests. Defaults to 80.
    - :param max_events: Maximum allowed number of events. Defaults to 2.

    return: a json with a success message if validation succeeds.
    """

    shell_descriptor, policy_validation = await process_and_retrieve_dtr(
        asset_id=asset_id,
        # submodel_semantic_id=submodel_semantic_id,
        counter_party_address=counter_party_address,
        counter_party_id=counter_party_id,
        timeout=timeout,
    )

    assert isinstance(shell_descriptor, dict), "Shell descriptor is not an dictionary!"

    validation_result = await submodel_validation(
        counter_party_id=counter_party_id,
        shell_descriptor_spec=shell_descriptor,
        semantic_id=submodel_semantic_id
    )

    return {'message': 'Special Characteristics validation is completed.',
            'submodel_validation_message': validation_result,
            'policy_validation_message': policy_validation}
