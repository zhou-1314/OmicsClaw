from omicsclaw.runtime.context_layers import get_skill_contract


def test_skill_contract_mentions_sc_batch_auto_prepare_workflow():
    contract = get_skill_contract(capability_context_present=False)

    assert "auto_prepare=true" in contract
    assert "confirm_workflow_skip=true" in contract
    assert "sc-batch-integration" in contract
