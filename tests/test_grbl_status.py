from octoprint_grblpcb.grbl_comm import GrblComm


def _parse(*lines):
    seen = []
    comm = GrblComm("/dev/null", status_callback=seen.append)
    for line in lines:
        comm._parse_status(line)
    return seen


def test_mpos_with_wco():
    (status,) = _parse("<Idle|MPos:10.000,10.000,-1.000|FS:0,0|WCO:10.000,10.000,0.000>")
    assert status["state"] == "Idle"
    assert status["mpos"] == {"x": 10.0, "y": 10.0, "z": -1.0}
    assert status["wco"] == {"x": 10.0, "y": 10.0, "z": 0.0}


def test_fields_in_between_do_not_drop_wco_and_overrides():
    # Real GRBL inserts Bf, Ln and Pn depending on $10 and pin state
    (status,) = _parse(
        "<Run|MPos:1.000,2.000,3.000|Bf:15,128|FS:500,10000|Pn:P|WCO:1.000,2.000,0.500|Ov:100,100,90>"
    )
    assert status["wco"] == {"x": 1.0, "y": 2.0, "z": 0.5}
    assert status["overrides"] == {"feed": 100, "rapid": 100, "spindle": 90}
    assert status["fs"] == {"feed": 500, "spindle": 10000}
    assert status["pins"] == "P"


def test_last_wco_is_kept_between_reports():
    _first, second = _parse(
        "<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:5.000,6.000,7.000>",
        "<Jog|MPos:1.000,0.000,0.000|FS:1000,0>",
    )
    assert second["wco"] == {"x": 5.0, "y": 6.0, "z": 7.0}


def test_hold_substate_and_feed_only_field():
    (status,) = _parse("<Hold:0|WPos:1.000,2.000,3.000|F:250>")
    assert status["state"] == "Hold:0"
    assert status["wpos"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert status["fs"] == {"feed": 250, "spindle": 0}
