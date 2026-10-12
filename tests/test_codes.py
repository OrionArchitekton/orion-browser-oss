"""Result codes: the numbers match orion-phone's, and JSON carries the code name."""

from __future__ import annotations

from orion_browser.codes import BrowserError, Code, Result


def test_codes_match_orion_phone_numbers():
    assert {code.name: int(code) for code in Code} == {
        "OK": 0, "INTERNAL": 1, "USAGE": 2, "BUSY": 3, "NOT_HOLDER": 4, "BROWSER_DOWN": 6, "UNHEALTHY": 7,
    }


def test_result_json_carries_the_code_name_and_exit():
    assert Result(Code.BUSY, "held", {"holder": "a"}).to_json() == {
        "ok": False, "code": "BUSY", "exit": 3, "message": "held", "data": {"holder": "a"},
    }
    assert Result(Code.OK, "done").to_json()["ok"] is True


def test_errors_carry_code_message_and_data():
    error = BrowserError(Code.USAGE, "bad", {"key": "x"})
    assert (error.code, error.message, error.data, str(error)) == (Code.USAGE, "bad", {"key": "x"}, "bad")
    assert BrowserError(Code.USAGE, "bad").data == {}
