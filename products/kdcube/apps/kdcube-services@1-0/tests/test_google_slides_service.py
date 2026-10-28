"""The Slides app composes SDK execution with account-bound consent."""
from pathlib import Path

import pytest

from kdcube_ai_app.apps.chat.sdk.integrations.connected_accounts import ConnectedAccountCredential
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

BUNDLE_ROOT=Path(__file__).resolve().parents[1]


def module():
    return load_dynamic_module_for_path(BUNDLE_ROOT/"services/productivity/google_slides.py")[1]


@pytest.mark.asyncio
async def test_credentials_remain_inside_adapter_and_claims_bind_one_account(monkeypatch):
    m=module()
    seen=[]
    async def resolve(*args,**kwargs):
        seen.append(kwargs)
        return ConnectedAccountCredential(ok=True,access_token="fixture-provider-credential",account_id="account",provider_id="google",connector_app_id="gmail",claim=kwargs["claim"],tool_name=kwargs["tool_name"])
    async def execute(**kwargs):
        assert kwargs["access_token"] == "fixture-provider-credential"
        return {"ok":True,"ret":{"presentation_id":"deck"}}
    monkeypatch.setattr(m,"resolve_connected_account_claim",resolve)
    monkeypatch.setattr(m,"resolve_connector_app_id",lambda provider:"gmail")
    monkeypatch.setattr(m,"execute_google_slides_operation",execute)
    result=await m.GoogleSlidesService()._execute_once(operation="copy",claim=["slides:read","slides:write"],tool_name="named_services.slides.object.action.copy",payload={"title":"Copy"},account_id="")
    assert [r["claim"] for r in seen] == ["slides:read","slides:write"]
    assert seen[1]["account_id"] == "account"
    assert result["ret"]["account_id"] == "account"
    assert "fixture-provider-credential" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial,unknown",[(True,False),(False,True)])
async def test_auth_failure_after_partial_or_uncertain_write_is_not_refreshed(monkeypatch,partial,unknown):
    m=module()
    async def resolve(*args,**kwargs):
        return ConnectedAccountCredential(ok=True,access_token="fixture",account_id="account",provider_id="google",connector_app_id="gmail",claim=kwargs["claim"],tool_name=kwargs["tool_name"])
    async def execute(**kwargs):
        return {"ok":False,"error":{"code":"google_slides_authorization_failed","message":"Auth failed"},"ret":{"category":"authorization_failed","partial_result":partial,"outcome_unknown":unknown}}
    monkeypatch.setattr(m,"resolve_connected_account_claim",resolve)
    monkeypatch.setattr(m,"resolve_connector_app_id",lambda provider:"gmail")
    monkeypatch.setattr(m,"execute_google_slides_operation",execute)
    result=await m.GoogleSlidesService()._execute_once(operation="fill_template",claim="slides:write",tool_name="test",payload={},account_id="account")
    assert "__connected_account_auth_failure__" not in result


def test_both_descriptor_sources_declare_the_same_slides_authority_contract():
    import yaml
    import kdcube_ai_app
    platform=Path(kdcube_ai_app.__file__).resolve().parents[3]/"deployment/bundles.yaml"
    ecosystem=BUNDLE_ROOT.parents[3]/"products/connection-hub/apps/connection-hub@1-0/config/bundles.template.yaml"
    def contract(path):
        config=yaml.safe_load(path.read_text())["bundles"]["items"][0]["config"]["connections"]
        google=config["delegated_to_kdcube"]["providers"]["google"]
        oauth=config["delegated_credentials"]["oauth"]
        resource=next(r for r in oauth["resources"] if "/mcp/named_services" in r["resource"])
        namespace=resource["named_services"]["namespaces"]["slides"]
        claims={key:value for key,value in google["claims"].items() if key.startswith("slides:")}
        allowed=[value for value in google["connector_apps"]["gmail"]["allowed_claims"] if value.startswith("slides:")]
        return claims,allowed,namespace
    left,right=contract(platform),contract(ecosystem)
    assert left == right
    assert set(left[0]) == {"slides:read","slides:write"}
    assert "https://www.googleapis.com/auth/presentations.readonly" in left[0]["slides:read"]["provider_scopes"]
    assert "https://www.googleapis.com/auth/drive.file" in left[0]["slides:write"]["provider_scopes"]
    actions=left[2]["tools"]["action"]["operations"]
    assert set(actions) == {f"object.action.{name}" for name in ("copy","export","thumbnail","fill_template","replace_text","replace_shapes_with_image")}
