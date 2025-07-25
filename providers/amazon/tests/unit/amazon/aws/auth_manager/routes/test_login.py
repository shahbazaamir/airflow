# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from tests_common.test_utils.version_compat import AIRFLOW_V_3_0_PLUS

if not AIRFLOW_V_3_0_PLUS:
    pytest.skip("AWS auth manager is only compatible with Airflow >= 3.0.0", allow_module_level=True)

from fastapi.testclient import TestClient

from airflow.api_fastapi.app import AUTH_MANAGER_FASTAPI_APP_PREFIX, create_app

from tests_common.test_utils.config import conf_vars
from tests_common.test_utils.mock_plugins import mock_plugin_manager

# onelogin is optional dependency from apache-airflow-providers-amazon[python3-saml]
# we want to skip it for the lowest dependency checks as it does not install extra dependencies
# https://github.com/apache/airflow/pull/50449#issuecomment-2897572327
OneLogin_Saml2_IdPMetadataParser = pytest.importorskip(
    "onelogin.saml2.idp_metadata_parser"
).OneLogin_Saml2_IdPMetadataParser

SAML_METADATA_URL = "/saml/metadata"
SAML_METADATA_PARSED = {
    "idp": {
        "entityId": "https://portal.sso.us-east-1.amazonaws.com/saml/assertion/<assertion>",
        "singleSignOnService": {
            "url": "https://portal.sso.us-east-1.amazonaws.com/saml/assertion/<assertion>",
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
        },
        "singleLogoutService": {
            "url": "https://portal.sso.us-east-1.amazonaws.com/saml/logout/<assertion>",
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
        },
        "x509cert": "<cert>",
    },
    "security": {"authnRequestsSigned": False},
    "sp": {"NameIDFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"},
}


@pytest.fixture
def test_client():
    with conf_vars(
        {
            (
                "core",
                "auth_manager",
            ): "airflow.providers.amazon.aws.auth_manager.aws_auth_manager.AwsAuthManager",
            ("aws_auth_manager", "saml_metadata_url"): SAML_METADATA_URL,
        }
    ):
        with (
            patch.object(OneLogin_Saml2_IdPMetadataParser, "parse_remote") as mock_parse_remote,
            patch(
                "airflow.providers.amazon.aws.auth_manager.avp.facade.AwsAuthManagerAmazonVerifiedPermissionsFacade.is_policy_store_schema_up_to_date"
            ) as mock_is_policy_store_schema_up_to_date,
        ):
            mock_is_policy_store_schema_up_to_date.return_value = True
            mock_parse_remote.return_value = SAML_METADATA_PARSED
            yield TestClient(create_app())


def get_login_callback_response(relay_state: str):
    with conf_vars(
        {
            (
                "core",
                "auth_manager",
            ): "airflow.providers.amazon.aws.auth_manager.aws_auth_manager.AwsAuthManager",
            ("aws_auth_manager", "saml_metadata_url"): SAML_METADATA_URL,
            ("api", "ssl_cert"): "false",
        }
    ):
        with (
            patch.object(OneLogin_Saml2_IdPMetadataParser, "parse_remote") as mock_parse_remote,
            patch(
                "airflow.providers.amazon.aws.auth_manager.routes.login._init_saml_auth"
            ) as mock_init_saml_auth,
            patch(
                "airflow.providers.amazon.aws.auth_manager.avp.facade.AwsAuthManagerAmazonVerifiedPermissionsFacade.is_policy_store_schema_up_to_date"
            ) as mock_is_policy_store_schema_up_to_date,
        ):
            mock_is_policy_store_schema_up_to_date.return_value = True
            mock_parse_remote.return_value = SAML_METADATA_PARSED

            auth = Mock()
            auth.is_authenticated.return_value = True
            auth.get_nameid.return_value = "user_id"
            auth.get_attributes.return_value = {
                "id": ["1"],
                "groups": ["group_1", "group_2"],
                "email": ["email"],
            }
            mock_init_saml_auth.return_value = auth
            client = TestClient(create_app())
            return client.post(
                AUTH_MANAGER_FASTAPI_APP_PREFIX + "/login_callback",
                follow_redirects=False,
                data={"RelayState": relay_state},
            )


@mock_plugin_manager(plugins=[])
class TestLoginRouter:
    @pytest.mark.parametrize(
        "url",
        ["/login", "/login/token"],
    )
    def test_login(self, test_client, url):
        response = test_client.get(AUTH_MANAGER_FASTAPI_APP_PREFIX + url, follow_redirects=False)
        assert response.status_code == 307
        assert "location" in response.headers
        assert response.headers["location"].startswith(
            "https://portal.sso.us-east-1.amazonaws.com/saml/assertion/"
        )

    def test_login_callback_successful_with_relay_state_redirect(self):
        response = get_login_callback_response("login-redirect")
        assert response.status_code == 303
        assert "location" in response.headers
        assert "_token" in response.cookies
        assert response.headers["location"].startswith("http://localhost:8080/")

    def test_login_callback_successful_with_relay_state_token(self):
        response = get_login_callback_response("login-token")
        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_login_callback_with_invalid_relay_state(self):
        response = get_login_callback_response("dummy")
        assert response.status_code == 500

    def test_login_callback_unsuccessful(self):
        with conf_vars(
            {
                (
                    "core",
                    "auth_manager",
                ): "airflow.providers.amazon.aws.auth_manager.aws_auth_manager.AwsAuthManager",
                ("aws_auth_manager", "saml_metadata_url"): SAML_METADATA_URL,
            }
        ):
            with (
                patch.object(OneLogin_Saml2_IdPMetadataParser, "parse_remote") as mock_parse_remote,
                patch(
                    "airflow.providers.amazon.aws.auth_manager.routes.login._init_saml_auth"
                ) as mock_init_saml_auth,
                patch(
                    "airflow.providers.amazon.aws.auth_manager.avp.facade.AwsAuthManagerAmazonVerifiedPermissionsFacade.is_policy_store_schema_up_to_date"
                ) as mock_is_policy_store_schema_up_to_date,
            ):
                mock_is_policy_store_schema_up_to_date.return_value = True
                mock_parse_remote.return_value = SAML_METADATA_PARSED

                auth = Mock()
                auth.is_authenticated.return_value = False
                mock_init_saml_auth.return_value = auth
                client = TestClient(create_app())
                response = client.post(AUTH_MANAGER_FASTAPI_APP_PREFIX + "/login_callback")
                assert response.status_code == 500
