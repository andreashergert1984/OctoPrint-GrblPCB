from octoprint_grblpcb import grbl_comm


def test_additional_port_globs_are_listed(tmp_path, monkeypatch):
    port = tmp_path / "ttyGRBL"
    port.touch()
    monkeypatch.setattr(grbl_comm, "_additional_port_patterns", lambda: [str(tmp_path / "tty*")])

    assert str(port) in grbl_comm.grbl_serial_list()


def test_without_settings_no_additional_ports():
    # Settings are not initialised in unit tests; listing must not fail
    assert grbl_comm._additional_port_patterns() == []
