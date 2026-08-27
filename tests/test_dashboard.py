import os
import sys
sys.path.insert(0, os.path.abspath("."))
import json
import tempfile
from app import load_jsonl, save_decision, load_decisions, remove_decision

def test_sample_10_schema():
    sample_path = os.path.join("data", "sample_10.jsonl")
    assert os.path.exists(sample_path), "sample_10.jsonl does not exist"
    
    records = load_jsonl(sample_path)
    assert len(records) == 10, f"Expected 10 sample records, got {len(records)}"
    
    required_fields = ["lead_id", "name", "domain", "city", "category", "score", "band", "findings", "subject", "body"]
    for r in records:
        for field in required_fields:
            assert field in r, f"Field '{field}' missing in record {r.get('lead_id')}"
        assert r["band"] in ["A", "B", "C"], f"Invalid band {r['band']}"
        assert 0 <= r["score"] <= 100, f"Score out of range {r['score']}"
        assert isinstance(r["findings"], list), "findings must be a list"
    print("[PASS] test_sample_10_schema passed: all 10 records adhere to lead schema contract.")

def test_decision_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_approved_file = os.path.join(tmpdir, "06_approved.jsonl")
        
        sample_lead = {
            "lead_id": "test_001",
            "name": "Test Dental Clinic",
            "domain": "testdental.com",
            "city": "London",
            "category": "dentist",
            "score": 90,
            "band": "A",
            "subject": "Original Subject",
            "body": "Original Body",
            "custom_field_from_stage_x": "keep_this_safe"
        }
        
        # 1. Test Approve
        updated = save_decision(
            lead_record=sample_lead,
            decision="approve",
            final_subject="Approved Subject",
            final_body="Approved Body",
            reviewer="Umer Mujahid",
            approved_file=test_approved_file
        )
        
        assert updated["decision"] == "approve"
        assert updated["final_body"] == "Approved Body"
        assert updated["custom_field_from_stage_x"] == "keep_this_safe"
        assert "decided_at" in updated
        assert updated["decided_by"] == "Umer Mujahid"
        
        # 2. Test Load Decisions
        decisions = load_decisions(test_approved_file)
        assert "test_001" in decisions
        assert decisions["test_001"]["decision"] == "approve"
        
        # 3. Test Edit & Update In Place
        save_decision(
            lead_record=sample_lead,
            decision="approve",
            final_subject="Edited Subject",
            final_body="Edited Body",
            reviewer="Umer Mujahid",
            approved_file=test_approved_file
        )
        decisions = load_decisions(test_approved_file)
        assert len(decisions) == 1, "Expected single record after update, not duplicated"
        assert decisions["test_001"]["final_subject"] == "Edited Subject"
        
        # 4. Test Revert
        remove_decision("test_001", test_approved_file)
        decisions = load_decisions(test_approved_file)
        assert "test_001" not in decisions
        
    print("[PASS] test_decision_lifecycle passed: save, reload, in-place update, and revert verified.")

if __name__ == "__main__":
    test_sample_10_schema()
    test_decision_lifecycle()
    print("ALL TESTS PASSED SUCCESSFULLY!")
