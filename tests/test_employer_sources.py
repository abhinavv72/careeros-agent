import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

import agent_server
import jobpilot


@pytest.mark.parametrize(("url", "ats", "board"), [
    ("https://boards.greenhouse.io/acme", "greenhouse", "acme"),
    ("https://jobs.lever.co/acme", "lever", "acme"),
    ("https://jobs.ashbyhq.com/acme", "ashby", "acme"),
    ("https://jobs.smartrecruiters.com/acme", "smartrecruiters", "acme"),
    ("https://apply.workable.com/acme", "workable", "acme"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External", "workday", "acme"),
])
def test_career_board_url_maps_to_supported_public_adapter(url, ats, board):
    source=agent_server.parse_career_board_url(url,"Acme")
    assert source["ats"]==ats
    assert source["boardId"]==board


@pytest.mark.parametrize("url", [
    "http://jobs.lever.co/acme",
    "https://example.com/careers",
    "https://user:pass@jobs.ashbyhq.com/acme",
])
def test_career_board_rejects_unsupported_or_insecure_urls(url):
    with pytest.raises(Exception):
        agent_server.parse_career_board_url(url,"Acme")


def test_employer_registration_is_local_and_idempotent():
    with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/"tmp") as directory:
        with patch("jobpilot.DATA",Path(directory)):
            payload=agent_server.CareerSourceInput(company="Acme",url="https://jobs.ashbyhq.com/acme")
            first=agent_server.add_employer(payload)
            second=agent_server.add_employer(payload)
            assert first["added"] is True
            assert second["duplicate"] is True
            saved=json.loads((Path(directory)/"employer-sources.json").read_text(encoding="utf-8"))
            assert saved==[first["source"]]


def test_custom_employer_boards_join_curated_discovery_list():
    with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/"tmp") as directory:
        data=Path(directory)
        (data/"employer-sources.json").write_text(json.dumps([
            {"company":"Custom Acme","ats":"ashby","boardId":"custom-acme","enabled":True}
        ]),encoding="utf-8")
        with patch("jobpilot.DATA",data),patch("jobpilot.load_json",return_value=[]):
            companies=jobpilot.configured_companies()
        assert companies==[{"company":"Custom Acme","ats":"ashby","boardId":"custom-acme","enabled":True}]
