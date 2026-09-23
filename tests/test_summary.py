from summary import summarize


def _fake_state(status="done", history=None):
    return {
        "status": status,
        "elapsed_ms": 7073,
        "page": {"url": "https://www.google.com/travel/flights?hl=en&results"},
        "history": history if history is not None else [],
    }


def test_summarize_trims_history_to_documented_fields():
    state = _fake_state(
        history=[
            {
                "action": "Where from? · San Francisco",
                "kind": "fill",
                "text": "Zurich",
                "page_changed": True,
                "elapsed_ms": 1200,
                "probability": 0.94,
                "confidence": 0.91,
                "raw_answers": {"operation": {"choice": "TYPE_TEXT"}},
                "usage": {"input_tokens": 512},
            }
        ]
    )

    result = summarize(state, hit_step_cap=False)

    assert result == {
        "status": "done",
        "final_url": "https://www.google.com/travel/flights?hl=en&results",
        "elapsed_ms": 7073,
        "steps": [
            {
                "action": "Where from? · San Francisco",
                "kind": "fill",
                "text": "Zurich",
                "page_changed": True,
                "elapsed_ms": 1200,
            }
        ],
    }


def test_summarize_reports_max_steps_when_cap_hit_before_done_or_blocked():
    state = _fake_state(status="ready", history=[])

    result = summarize(state, hit_step_cap=True)

    assert result["status"] == "max_steps"


def test_summarize_keeps_done_status_even_if_cap_flag_is_true():
    # The loop can hit done/blocked on the exact same tick the cap would have
    # triggered; a real status always wins over the cap label.
    state = _fake_state(status="done", history=[])

    result = summarize(state, hit_step_cap=True)

    assert result["status"] == "done"
