"""The independent DONE/BLOCKED verifier, including the read-only-goal rule.

The verifier normally asks the text model (DeepSeek) for a second opinion. For a goal that
only reads or expands, the model can only produce false negatives ("group not expanded")
because nothing was changed, so a read-only goal must never be flipped to `unverified`.
DeepSeek is stubbed here (and is out of balance in production as of 2026-11-05), so these
tests never hit the network.
"""
from jev_runner import verify

# Capture the real function before conftest's autouse `_verify_ok` stub replaces the module attr.
_REAL_CHECK = verify.check


def test_is_read_only_goal_true_for_read_and_expand_goals():
    assert verify._is_read_only_goal(
        "Open the AI Agents group and read the switch states. Do not click Save.")
    assert verify._is_read_only_goal(
        "Open this page and report what sections and email domains are visible. "
        "Do not change or save anything.")
    assert verify._is_read_only_goal("Read only. List the workflows shown.")


def test_is_read_only_goal_false_for_action_goals():
    assert not verify._is_read_only_goal("Click the switch named 'Dashboard' once to turn it off.")
    assert not verify._is_read_only_goal("In the From Name field type 'Naults Windham' and click Save.")
    assert not verify._is_read_only_goal("Delete the step, then confirm.")
    # a goal that reads AND saves still needs verifying — the save is the part that can fail
    assert not verify._is_read_only_goal(
        "Expand the group, read the switches, then click Save to apply.")


def test_check_skips_model_and_lets_verdict_stand_for_read_only_goal(monkeypatch):
    # model is configured and would say "not done"...
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    called = []

    def fake_post(*a, **k):
        called.append(1)
        return {"choices": [{"message": {"content": '{"done": false, "reason": "group not expanded"}'}}]}

    monkeypatch.setattr(verify.model, "post_json", fake_post)
    ok, why = _REAL_CHECK(
        "Open the AI Agents group and read its switch states. Do not click Save.",
        {"url": "x", "text": "States: AI Agents=on | View logs=off", "actions": []}, [])
    assert ok is None       # ...but a read-only goal is never flipped to unverified (Jev's verdict stands)
    assert called == []      # the unreliable second opinion is not consulted at all


def test_check_consults_model_for_an_action_goal(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    monkeypatch.setattr(
        verify.model, "post_json",
        lambda *a, **k: {"choices": [{"message": {"content": '{"done": false, "reason": "not saved"}'}}]})
    ok, why = _REAL_CHECK("Change the From Name to X and click Save.",
                          {"url": "x", "text": "y", "actions": []}, [])
    assert ok is False
    assert "not saved" in why
