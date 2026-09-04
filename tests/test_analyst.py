"""The eval analyst's detectors, tested against synthetic tool output.

These run on hand-built numbers rather than a real eval, so each rule can be
checked in isolation and, crucially, checked for *silence* on healthy input. A
detector that fires on everything is as useless as one that never fires -- and
the analyst has already shipped one wrong finding (a calibration check that
compared a zero-contact base rate against an all-contacts average), so the
negative cases matter as much as the positive ones.

The two positive cases below are the two real bugs this agent found:
`cart.py` / `receivables.py` never proposing `retry`, and the arbiter running
~1.7x high on opt-out risk.
"""

from __future__ import annotations

import json

from eval.analyst import Finding, deterministic_analysis
from rcp.llm.client import Tool


def fake_tools(*, breakdown, mix, calibration, reasons=None):
    """Wrap literal dicts as tools so a rule can be exercised on its own."""
    stub = {"type": "object", "properties": {}}
    return {
        "segment_breakdown": Tool("segment_breakdown", "", stub, lambda: breakdown),
        "channel_mix": Tool("channel_mix", "", stub,
                            lambda segment: mix.get(segment, {"baseline": {},
                                                              "control_plane": {}})),
        "suppression_reasons": Tool("suppression_reasons", "", stub,
                                    lambda segment: reasons or {"reasons": []}),
        "calibration_check": Tool("calibration_check", "", stub, lambda: calibration),
        "sample_decision": Tool("sample_decision", "", stub,
                                lambda segment, outcome="suppressed": {"found": False}),
    }


def healthy_segment(**over):
    row = {
        "baseline_net_paise": 100_000, "control_net_paise": 150_000,
        "delta_paise": 50_000, "delta_pct": 50.0, "events_per_seed": 200.0,
        "baseline_sent": 100.0, "control_sent": 70.0,
        "control_recovered_paise": 200_000, "baseline_recovered_paise": 180_000,
        "control_spend_paise": 1_000, "baseline_spend_paise": 1_200,
        "control_false_suppression_paise": 1_000,
    }
    row.update(over)
    return row


def healthy_calibration(predicted=0.02, actual=0.019):
    return {
        "arbiter_belief": {"opt_out_base": 0.01, "opt_out_per_extra_contact": 0.018},
        "seeds": 20,
        "baseline": {"messaged_sends": 3000, "opt_outs": int(actual * 3000),
                     "predicted_rate": predicted, "actual_rate": actual},
        "control_plane": {"messaged_sends": 2000, "opt_outs": 30,
                          "predicted_rate": predicted, "actual_rate": actual},
        "ratio_predicted_over_actual": round(predicted / actual, 2),
    }


def run(**kw) -> list[Finding]:
    result = deterministic_analysis(fake_tools(**kw))
    return json.loads(result.text)


ALL_HEALTHY = {
    "seeds": 20,
    "subscription": healthy_segment(),
    "cart": healthy_segment(),
    "receivables": healthy_segment(),
}


def test_healthy_system_produces_no_findings():
    """The most important test here. A detector that always fires is noise."""
    assert run(breakdown=ALL_HEALTHY, mix={},
               calibration=healthy_calibration()) == []


def test_detects_an_unused_channel_that_is_cheaper_and_better():
    """The cart bug, and later the receivables bug: the baseline recovers more
    per send on a channel the proposer never even considers."""
    mix = {"cart": {
        "baseline": {"retry": {"sent": 2000, "recovered": 460,
                               "recovery_rate": 0.23, "cost_per_send_paise": 200}},
        "control_plane": {"whatsapp": {"sent": 1400, "recovered": 250,
                                       "recovery_rate": 0.179,
                                       "cost_per_send_paise": 3500}},
    }}
    findings = run(breakdown=ALL_HEALTHY, mix=mix, calibration=healthy_calibration())

    assert len(findings) == 1
    finding = findings[0]
    assert finding["severity"] == "high"
    assert "retry" in finding["title"]
    assert "23.0%" in finding["evidence"] and "17.9%" in finding["evidence"]
    assert finding["where"] == "rcp/proposers/cart.py"


def test_ignores_an_unused_channel_that_is_merely_cheaper():
    """Cheaper-but-worse is a real trade-off, not a bug."""
    mix = {"cart": {
        "baseline": {"email": {"sent": 2000, "recovered": 100,
                               "recovery_rate": 0.05, "cost_per_send_paise": 300}},
        "control_plane": {"whatsapp": {"sent": 1400, "recovered": 250,
                                       "recovery_rate": 0.179,
                                       "cost_per_send_paise": 3500}},
    }}
    assert run(breakdown=ALL_HEALTHY, mix=mix,
               calibration=healthy_calibration()) == []


def test_ignores_a_channel_with_too_few_sends_to_judge():
    mix = {"cart": {
        "baseline": {"retry": {"sent": 5, "recovered": 4, "recovery_rate": 0.8,
                               "cost_per_send_paise": 200}},
        "control_plane": {"whatsapp": {"sent": 1400, "recovered": 250,
                                       "recovery_rate": 0.179,
                                       "cost_per_send_paise": 3500}},
    }}
    assert run(breakdown=ALL_HEALTHY, mix=mix,
               calibration=healthy_calibration()) == []


def test_detects_opt_out_risk_running_high():
    """The measured bug: predicted 0.0316 against an observed 0.0187."""
    findings = run(breakdown=ALL_HEALTHY, mix={},
                   calibration=healthy_calibration(predicted=0.0316, actual=0.0187))

    assert len(findings) == 1
    assert "overestimates opt-out risk by 1.7x" in findings[0]["title"]
    assert "3000 messaged sends" in findings[0]["evidence"]
    assert findings[0]["where"] == "rcp/arbiter/score.py"


def test_detects_opt_out_risk_running_low():
    findings = run(breakdown=ALL_HEALTHY, mix={},
                   calibration=healthy_calibration(predicted=0.01, actual=0.04))
    assert "underestimates" in findings[0]["title"]


def test_calibration_within_tolerance_is_silent():
    """A 20% gap on a noisy rate is not worth anyone's afternoon."""
    assert run(breakdown=ALL_HEALTHY, mix={},
               calibration=healthy_calibration(predicted=0.022, actual=0.019)) == []


def test_detects_a_losing_segment():
    breakdown = dict(ALL_HEALTHY, cart=healthy_segment(
        control_net_paise=80_000, delta_paise=-20_000, delta_pct=-20.0))
    findings = run(breakdown=breakdown, mix={}, calibration=healthy_calibration())

    losing = [f for f in findings if "loses" in f["title"]]
    assert len(losing) == 1
    assert losing[0]["severity"] == "high"          # >10% loss
    assert "20 seeds" in losing[0]["evidence"], "sample size must be stated"


def test_small_loss_is_medium_not_high():
    breakdown = dict(ALL_HEALTHY, cart=healthy_segment(
        control_net_paise=96_000, delta_paise=-4_000, delta_pct=-4.0))
    findings = run(breakdown=breakdown, mix={}, calibration=healthy_calibration())
    assert [f["severity"] for f in findings if "loses" in f["title"]] == ["medium"]


def test_detects_spending_more_to_recover_less():
    breakdown = dict(ALL_HEALTHY, cart=healthy_segment(
        control_spend_paise=3_000, baseline_spend_paise=900,
        control_recovered_paise=150_000, baseline_recovered_paise=200_000))
    findings = run(breakdown=breakdown, mix={}, calibration=healthy_calibration())
    assert any("spends more to recover less" in f["title"] for f in findings)


def test_detects_suppression_abandoning_recoverable_money():
    breakdown = dict(ALL_HEALTHY, receivables=healthy_segment(
        control_recovered_paise=30_000, control_false_suppression_paise=47_000))
    findings = run(breakdown=breakdown, mix={}, calibration=healthy_calibration())
    assert any("abandoning recoverable money" in f["title"] for f in findings)


def test_findings_are_ordered_by_severity():
    breakdown = dict(ALL_HEALTHY, cart=healthy_segment(
        control_net_paise=80_000, delta_paise=-20_000, delta_pct=-20.0,
        control_recovered_paise=30_000, control_false_suppression_paise=47_000))
    findings = run(breakdown=breakdown, mix={},
                   calibration=healthy_calibration(predicted=0.0316, actual=0.0187))
    severities = [f["severity"] for f in findings]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1,
                                                           "low": 2}[s])


def test_every_finding_carries_a_fix_and_a_location():
    breakdown = dict(ALL_HEALTHY, cart=healthy_segment(
        control_net_paise=80_000, delta_paise=-20_000, delta_pct=-20.0))
    for finding in run(breakdown=breakdown, mix={},
                       calibration=healthy_calibration(predicted=0.0316,
                                                       actual=0.0187)):
        assert finding["suggested_fix"], finding["title"]
        assert finding["where"], finding["title"]
        assert any(ch.isdigit() for ch in finding["evidence"]), \
            f"unquantified finding: {finding['title']}"


# --------------------------------------------------------------------------
# guarding against a model's confident inventions
# --------------------------------------------------------------------------

from eval.analyst import _known_paths, _parse


def test_real_paths_pass_through():
    parsed = _parse(json.dumps([{
        "severity": "high", "title": "t", "evidence": "1 of 2",
        "suggested_fix": "f", "where": "rcp/proposers/cart.py"}]))
    assert parsed[0].where == "rcp/proposers/cart.py"


def test_invented_paths_are_flagged_not_presented_as_fact():
    """A live Groq run returned four findings citing `cart_scoring.go`,
    `channel_caps.go` and `optout_arbiter.go` -- in a repo with no Go in it.
    The evidence may still be sound, so the finding survives; the path does
    not get to look real."""
    parsed = _parse(json.dumps([{
        "severity": "high", "title": "t", "evidence": "1 of 2",
        "suggested_fix": "f", "where": "cart_scoring.go"}]))
    assert parsed[0].where == "(unverified: cart_scoring.go)"
    assert parsed[0].evidence == "1 of 2", "the finding itself is kept"


def test_missing_path_stays_empty():
    parsed = _parse(json.dumps([{"severity": "low", "title": "t",
                                 "evidence": "1", "suggested_fix": "f"}]))
    assert parsed[0].where == ""


def test_known_paths_cover_what_the_prompt_advertises():
    """Every path named in the system prompt must be one _parse accepts, or the
    agent gets punished for following instructions."""
    from eval.analyst import SYSTEM
    known = _known_paths()
    advertised = [
        line.strip().split()[0]
        for line in SYSTEM.splitlines()
        if line.strip().startswith(("rcp/", "config/"))
    ]
    assert advertised, "the prompt should list the layout"
    assert set(advertised) <= known, set(advertised) - known


def test_prose_around_the_json_is_tolerated():
    """Models wrap arrays in explanation or code fences."""
    parsed = _parse('Here is what I found:\n```json\n'
                    '[{"severity":"low","title":"t","evidence":"1",'
                    '"suggested_fix":"f","where":"config/scoring.yaml"}]\n```\n')
    assert len(parsed) == 1 and parsed[0].where == "config/scoring.yaml"


def test_unparseable_output_yields_nothing_rather_than_crashing():
    assert _parse("the model rambled and produced no array") == []
    assert _parse("[not, valid, json") == []
