import importlib.util
import json
import re
from pathlib import Path

import pytest

from tests.fakes import FIXTURES

SPEC = importlib.util.spec_from_file_location(
    "probe", Path(__file__).resolve().parents[1] / "scripts" / "probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def raw_sample() -> dict:
    return {
        "user_me": {"u": {"id": "111", "name": "Echter Name", "email": "real@mail.example"}},
        "leagues_selection": {
            "it": [
                {"i": "500", "n": "Meine Liga", "cpi": "1"},
                {"i": "501", "n": "Alte Liga", "cpi": "1"},
            ]
        },
        "league_ranking": {
            "ti": "Meine Liga",
            "us": [
                {"i": "111", "n": "Echter Name", "uim": "user/me.png", "sp": 10},
                {"i": "222", "n": "Ohne Bild", "sp": 5},  # no profile image
            ],
        },
        "league_market": {
            "it": [
                {
                    "i": "77",
                    "n": "Spielername",
                    "u": {"i": "222", "n": "Ohne Bild", "uim": "user/x.png"},
                    "ofs": [{"u": "333", "unm": "Bieter", "uop": 1}],
                }
            ]
        },
        "league_player": {"i": "77", "oui": "111", "uim": "user/me.png", "tn": "Paderborn"},
    }


def test_anonymize_replaces_all_managers_and_leagues():
    anonymized, replaced = probe.anonymize_all(raw_sample(), "Meine Liga")
    text = json.dumps(anonymized)
    for secret in (
        "Echter Name",
        "Ohne Bild",
        "Bieter",
        "real@mail.example",
        "Meine Liga",
        "Alte Liga",
        '"111"',
        '"222"',
        '"333"',
        "user/me.png",
        "user/x.png",
    ):
        assert secret not in text, secret
    assert anonymized["league_ranking"]["ti"] == "Testliga"
    assert anonymized["league_market"]["it"][0]["n"] == "Spielername"  # public player data
    assert anonymized["league_player"]["tn"] == "Paderborn"
    assert replaced >= 9


def test_ids_stay_consistent_across_endpoints():
    anonymized, _ = probe.anonymize_all(raw_sample(), "Meine Liga")
    owner_id = anonymized["user_me"]["u"]["id"]
    assert anonymized["league_player"]["oui"] == owner_id
    assert anonymized["league_ranking"]["us"][0]["i"] == owner_id


def test_leak_check_blocks_unhandled_personal_data():
    raw = raw_sample()
    raw["league_ranking"]["note"] = "contact real@mail.example"  # e-mail hidden in free text
    with pytest.raises(probe.LeakError):
        probe.anonymize_all(raw, "Meine Liga")


def test_committed_fixtures_contain_no_personal_data():
    text = " ".join(p.read_text(encoding="utf-8") for p in FIXTURES.glob("*.json"))
    emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text))
    assert emails <= {"manager@example.com"}
    assert '"user/' not in text  # user images
    assert '"tkn"' not in text  # no login responses with tokens
    names = {u["n"] for u in json.loads((FIXTURES / "league_ranking.json").read_text())["us"]}
    assert all(re.fullmatch(r"Manager \d+", n) for n in names)


def test_owner_names_are_anonymised_too():
    raw = raw_sample()
    raw["competition_player"] = {"i": "77", "oui": "4444444", "onm": "Fremder Manager"}
    anonymized, _ = probe.anonymize_all(raw, "Meine Liga")
    text = json.dumps(anonymized)
    assert "Fremder Manager" not in text
    assert "4444444" not in text


def test_unregistered_manager_name_key_is_reported_with_location():
    anonymizer = probe.Anonymizer()
    leaks = anonymizer.leaks({"market": {"it": [{"ofs": [{"unm": "Unbekannt"}]}]}}, set())
    assert leaks == ["market:$.it[0].ofs[0].unm"]


def test_short_names_are_checked_exactly():
    anonymizer = probe.Anonymizer()
    anonymizer.add_user("12", "Tom")
    assert anonymizer.leaks({"x": {"n": "Tom"}}, set()) == ["x:$.n"]
    assert anonymizer.leaks({"x": {"n": "Tomás Pérez"}}, set()) == []  # player names stay


def test_fixture_manager_fields_are_all_anonymised():
    for path in FIXTURES.glob("*.json"):
        for d in probe.find_dicts(json.loads(path.read_text(encoding="utf-8"))):
            for name_key in ("unm", "onm"):
                if name_key in d:
                    assert re.fullmatch(r"Manager \d+", d[name_key]), (path.name, name_key)
            for id_key in ("oui", "uoid"):
                if id_key in d:
                    assert str(d[id_key]) in {"0", ""} or re.fullmatch(r"9\d{6}", str(d[id_key]))


def test_transfer_buyers_and_sellers_are_anonymised():
    raw = raw_sample()
    raw["league_activities_transfers"] = {
        "af": [
            {"i": 1, "t": 15, "data": {"pi": "77", "byr": "Ehemaliger Käufer", "t": 1, "trp": 5}},
            {
                "i": 2,
                "t": 15,
                "data": {"pi": "78", "slr": "Ehemaliger Verkäufer", "t": 2, "trp": 5},
            },
        ]
    }
    anonymized, _ = probe.anonymize_all(raw, "Meine Liga")
    text = json.dumps(anonymized["league_activities_transfers"], ensure_ascii=False)
    assert "Ehemalig" not in text
    data = [a["data"] for a in anonymized["league_activities_transfers"]["af"]]
    assert re.fullmatch(r"Manager \d+", data[0]["byr"])
    assert re.fullmatch(r"Manager \d+", data[1]["slr"])
