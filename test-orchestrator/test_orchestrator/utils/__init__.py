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

# Load utils.py directly (folder takes precedence over file, so we use importlib)
import importlib.util
import os

_utils_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'baseutils.py')
_spec = importlib.util.spec_from_file_location("_baseutils_module", _utils_path)
_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_utils)

init_negotiation = _utils.init_negotiation
obtain_negotiation_state = _utils.obtain_negotiation_state
get_data_address = _utils.get_data_address
