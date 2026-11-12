"""Validation and simulated-execution contract tests for the n8n workflow export."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from outreach_system.api import create_app
from outreach_system.config import Settings
from outreach_system.models import FilterStatus, InfluencerProfile, PersonalizedMessage
from outreach_system.sending import InstagramDMWorkflow
from outreach_system.storage import OutreachDatabase

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / "workflows" / "n8n_micro_influencer_outreach.json"
TEST_API_KEY = "n8n-contract-test-key"


def _run_n8n_code_node(js_code: str, input_items: list[dict]) -> list[dict]:
    """Execute the workflow's JavaScript Code node directly in Node.js with mocked $input."""
    harness = (
        "const raw = JSON.parse(process.argv[1]);\n"
        "const $input = { all: () => raw.map(j => ({ json: j })) };\n"
        "const fn = new Function('$input', " + json.dumps(js_code) + ");\n"
        "const out = fn($input);\n"
        "console.log(JSON.stringify(out));\n"
    )
    proc = subprocess.run(
        ["node", "-e", harness, json.dumps(input_items)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(proc.stdout)


def test_n8n_workflow_json_structure_and_security() -> None:
    wf = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}

    assert "Split & Validate Message Items" in nodes
    assert nodes["Split & Validate Message Items"]["type"] == "n8n-nodes-base.code"

    # Must connect Fetch Messages -> Split & Validate -> Has Verified Public Email?
    conns = wf["connections"]
    assert conns["Fetch Shortlisted Personalized Messages"]["main"][0][0]["node"] == "Split & Validate Message Items"
    assert conns["Split & Validate Message Items"]["main"][0][0]["node"] == "Has Verified Public Email?"

    # Mutating HTTP nodes must use env variable auth and hard-pin simulate mode
    send_node = nodes["Send / Simulate Email (Duplicate Guarded)"]
    assert "mode: 'simulate'" in send_node["parameters"]["jsonBody"]
    assert "$env.OUTREACH_API_KEY" in json.dumps(send_node["parameters"]["headerParameters"])

    run_node = nodes["Run Discovery, Filter, Enrich & AI Personalize"]
    assert "auto_send: false" in run_node["parameters"]["jsonBody"]
    assert "$env.OUTREACH_API_KEY" in json.dumps(run_node["parameters"]["headerParameters"])

    # Read GET nodes must also pass X-API-Key header
    msg_node = nodes["Fetch Shortlisted Personalized Messages"]
    summary_node = nodes["Fetch Final Outreach Tracker Summary"]
    assert "$env.OUTREACH_API_KEY" in json.dumps(msg_node["parameters"]["headerParameters"])
    assert "$env.OUTREACH_API_KEY" in json.dumps(summary_node["parameters"]["headerParameters"])


def test_n8n_code_node_and_api_end_to_end_contract(tmp_path: Path) -> None:
    wf = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    split_node = next(n for n in wf["nodes"] if n["name"] == "Split & Validate Message Items")
    js_code = split_node["parameters"]["jsCode"]

    # 1. Empty /api/messages response must produce 0 items safely
    assert _run_n8n_code_node(js_code, [[]]) == []

    # 2. Seed FastAPI database with one email-ready creator and one DM-only creator
    db_path = tmp_path / "n8n.db"
    settings = Settings(
        database_path=str(db_path),
        outputs_dir=str(tmp_path / "outputs"),
        api_auth_token=TEST_API_KEY,
        llm_provider="fallback",
    )
    db = OutreachDatabase(db_path)
    p_email = InfluencerProfile(
        run_id="run-n8n",
        id="yt_email_ok",
        name="Email Creator",
        platform="YouTube",
        profile_url="https://www.youtube.com/@emailcreator",
        followers=20_000,
        engagement_rate=1.2,
        niche="Technology & AI Gadgets",
        contact_email="collab@emailcreator.io",
        status=FilterStatus.QUALIFIED,
        tier="Tier A (Priority Micro)",
        filter_score=85.0,
    )
    p_dm = InfluencerProfile(
        run_id="run-n8n",
        id="yt_dm_only",
        name="DM Creator",
        platform="YouTube",
        profile_url="https://www.youtube.com/@dmcreator",
        followers=15_000,
        engagement_rate=1.1,
        niche="Technology & AI Gadgets",
        contact_email="Not Found",
        instagram_url="https://instagram.com/dmcreator",
        status=FilterStatus.QUALIFIED,
        tier="Tier B (Standard Micro)",
        filter_score=74.0,
    )
    db.upsert_influencers([p_email, p_dm], run_id="run-n8n", mark_previous_inactive=True)
    msgs = [
        PersonalizedMessage(
            run_id="run-n8n",
            influencer_id=p.id,
            influencer_name=p.name,
            platform=p.platform,
            contact_email=p.contact_email,
            instagram_url=p.instagram_url,
            collaboration_angle="Sponsorship",
            email_subject=f"VoltFlow x {p.name}",
            email_pitch=" ".join(["word"] * 68),
            email_word_count=68,
            instagram_dm=" ".join(["dm"] * 18),
            dm_word_count=18,
            personalization_signals_used=["niche:tech"],
            llm_provider="contextual_fallback",
            llm_model="dynamic-nlg-v1",
            generated_at=datetime.now(UTC).isoformat(),
        )
        for p in (p_email, p_dm)
    ]
    db.save_run_messages(msgs, run_id="run-n8n")
    InstagramDMWorkflow(db).sync_queue(msgs, run_id="run-n8n")

    client = TestClient(create_app(settings))
    headers = {"X-API-Key": TEST_API_KEY}

    # Fetch array from /api/messages using X-API-Key (plus inject a malformed missing-ID entry)
    assert client.get("/api/messages").status_code == 401
    api_array = client.get("/api/messages", headers=headers).json()
    assert isinstance(api_array, list)
    api_array_with_malformed = [*api_array, {"influencer_id": None, "contact_email": "bad@x.com"}]

    # Execute actual n8n Code node on the single-item JSON array payload from HTTP Request node
    emitted_items = _run_n8n_code_node(js_code, [api_array_with_malformed])
    assert len(emitted_items) == 2

    for item in emitted_items:
        data = item["json"]
        if data["has_valid_email"] and data["contact_email"] != "Not Found":
            res = client.post(
                "/api/outreach/send",
                headers=headers,
                json={"mode": "simulate", "influencer_ids": [data["influencer_id"]]},
            )
            assert res.status_code == 200
            assert res.json()["sent"] == 1
        else:
            res = client.post(f"/api/dm/{data['influencer_id']}/simulate", headers=headers)
            assert res.status_code == 200
            assert res.json()["status"] == "Simulated Sent"


def test_n8n_contract_edge_cases_invalid_email_no_ig_dedup_and_summary(tmp_path: Path) -> None:
    wf = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    js_code = next(
        n["parameters"]["jsCode"] for n in wf["nodes"] if n["name"] == "Split & Validate Message Items"
    )

    # Empty, null, and invalid-email items handled safely without crash
    assert _run_n8n_code_node(js_code, []) == []
    assert _run_n8n_code_node(js_code, [None]) == []
    bad_email_items = _run_n8n_code_node(
        js_code,
        [[{"influencer_id": "yt_bad_email", "contact_email": "not-an-email", "instagram_url": "Not Found"}]],
    )
    assert len(bad_email_items) == 1
    assert bad_email_items[0]["json"]["has_valid_email"] is False

    # Full contract with valid email, invalid email + valid IG, and no-email + no-IG + duplicate prevention
    db_path = tmp_path / "n8n_edge.db"
    settings = Settings(
        database_path=str(db_path),
        outputs_dir=str(tmp_path / "outputs"),
        api_auth_token=TEST_API_KEY,
        llm_provider="fallback",
    )
    db = OutreachDatabase(db_path)
    creators = [
        InfluencerProfile(
            run_id="run-edge",
            id="yt_valid_e",
            name="Valid Email Creator",
            platform="YouTube",
            profile_url="https://www.youtube.com/@valid",
            followers=20_000,
            engagement_rate=1.2,
            niche="Technology & AI Gadgets",
            contact_email="hello@valid.io",
            instagram_url="Not Found",
            status=FilterStatus.QUALIFIED,
            tier="Tier A (Priority Micro)",
            filter_score=85.0,
        ),
        InfluencerProfile(
            run_id="run-edge",
            id="yt_bad_e_ig",
            name="Bad Email With IG",
            platform="YouTube",
            profile_url="https://www.youtube.com/@bad_e_ig",
            followers=18_000,
            engagement_rate=1.1,
            niche="Technology & AI Gadgets",
            contact_email="not-an-email",
            instagram_url="https://instagram.com/bad_e_ig",
            status=FilterStatus.QUALIFIED,
            tier="Tier B (Standard Micro)",
            filter_score=74.0,
        ),
        InfluencerProfile(
            run_id="run-edge",
            id="yt_none_both",
            name="No Contact Either",
            platform="YouTube",
            profile_url="https://www.youtube.com/@noneboth",
            followers=16_000,
            engagement_rate=1.1,
            niche="Technology & AI Gadgets",
            contact_email="Not Found",
            instagram_url="Not Found",
            status=FilterStatus.QUALIFIED,
            tier="Tier B (Standard Micro)",
            filter_score=72.0,
        ),
    ]
    db.upsert_influencers(creators, run_id="run-edge", mark_previous_inactive=True)
    msgs = [
        PersonalizedMessage(
            run_id="run-edge",
            influencer_id=p.id,
            influencer_name=p.name,
            platform=p.platform,
            contact_email=p.contact_email,
            instagram_url=p.instagram_url,
            collaboration_angle="Sponsorship",
            email_subject=f"VoltFlow x {p.name}",
            email_pitch=" ".join(["word"] * 68),
            email_word_count=68,
            instagram_dm=" ".join(["dm"] * 18),
            dm_word_count=18,
            personalization_signals_used=["niche:tech"],
            llm_provider="contextual_fallback",
            llm_model="dynamic-nlg-v1",
            generated_at=datetime.now(UTC).isoformat(),
        )
        for p in creators
    ]
    db.save_run_messages(msgs, run_id="run-edge")
    InstagramDMWorkflow(db).sync_queue(msgs, run_id="run-edge")

    client = TestClient(create_app(settings))
    headers = {"X-API-Key": TEST_API_KEY}
    raw_msgs = client.get("/api/messages", headers=headers).json()
    emitted = _run_n8n_code_node(js_code, [raw_msgs])
    assert len(emitted) == 3

    for item in emitted:
        d = item["json"]
        if d["has_valid_email"]:
            r1 = client.post(
                "/api/outreach/send",
                headers=headers,
                json={"mode": "simulate", "influencer_ids": [d["influencer_id"]]},
            )
            assert r1.status_code == 200 and r1.json()["sent"] == 1
            # Duplicate send must be blocked
            r2 = client.post(
                "/api/outreach/send",
                headers=headers,
                json={"mode": "simulate", "influencer_ids": [d["influencer_id"]]},
            )
            assert r2.status_code == 200 and r2.json()["sent"] == 0
            assert r2.json()["entries"][0]["status"] == "Skipped - Duplicate"
        else:
            dm_res = client.post(f"/api/dm/{d['influencer_id']}/simulate", headers=headers)
            assert dm_res.status_code == 200
            if d["influencer_id"] == "yt_none_both":
                assert dm_res.json()["status"] == "Skipped - No Instagram Handle"
                assert dm_res.json()["sent"] is False
            else:
                assert dm_res.json()["status"] == "Simulated Sent"
                assert dm_res.json()["sent"] is True

    # Final /api/summary requires auth and reports exact run metrics
    assert client.get("/api/summary").status_code == 401
    summary = client.get("/api/summary", headers=headers).json()
    assert summary["emails_sent_count"] == 1
    assert summary["dm_sent_count"] == 1
    assert summary["dm_skipped_no_handle"] == 2  # yt_valid_e & yt_none_both both lack Instagram URLs

