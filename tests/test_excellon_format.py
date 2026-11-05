"""Excellon number formats: units, LZ/TZ, explicit formats, modal axes."""
import pytest

from octoprint_grblpcb.gerber_parser import _excellon_number, parse_excellon_file


def _holes(tmp_path, body):
    path = tmp_path / "drill.drl"
    path.write_text(body)
    _layer, drills = parse_excellon_file(str(path))
    return [hole for drill in drills for hole in drill.holes]


@pytest.mark.parametrize(
    "text, digits, zeros, value",
    [
        ("12.5", (3, 3), None, 12.5),
        ("012500", (3, 3), "TZ", 12.5),
        ("12500", (3, 3), "TZ", 12.5),
        ("0125", (3, 3), "LZ", 12.5),
        ("-0125", (3, 3), "LZ", -12.5),
        ("15000", (2, 4), "TZ", 1.5),
        ("015", (2, 4), "LZ", 1.5),
    ],
)
def test_number_conversion(text, digits, zeros, value):
    assert _excellon_number(text, digits, zeros) == pytest.approx(value)


def test_metric_without_format_uses_3_3(tmp_path):
    holes = _holes(tmp_path, "M48\nMETRIC,TZ\nT1C0.8\n%\nT1\nX010000Y020000\nM30\n")
    assert holes == [(pytest.approx(10.0), pytest.approx(20.0))]


def test_inch_without_format_uses_2_4(tmp_path):
    holes = _holes(tmp_path, "M48\nINCH,TZ\nT1C0.035\n%\nT1\nX010000Y020000\nM30\n")
    assert holes == [(pytest.approx(25.4), pytest.approx(50.8))]


def test_leading_zero_format_pads_right(tmp_path):
    holes = _holes(tmp_path, "M48\nMETRIC,LZ\nT1C0.8\n%\nT1\nX25Y0125\nM30\n")
    assert holes == [(pytest.approx(250.0), pytest.approx(12.5))]


def test_explicit_format_in_unit_line(tmp_path):
    holes = _holes(tmp_path, "M48\nMETRIC,TZ,0000.00\nT1C0.8\n%\nT1\nX1250Y500\nM30\n")
    assert holes == [(pytest.approx(12.5), pytest.approx(5.0))]


def test_kicad_file_format_comment(tmp_path):
    body = "M48\n;FILE_FORMAT=4:4\nMETRIC,TZ\nT1C0.8\n%\nT1\nX125000Y50000\nM30\n"
    assert _holes(tmp_path, body) == [(pytest.approx(12.5), pytest.approx(5.0))]


def test_single_axis_keeps_other_axis(tmp_path):
    holes = _holes(tmp_path, "M48\nMETRIC\nT1C0.8\n%\nT1\nX1.0Y2.0\nX3.0\nY4.0\nM30\n")
    assert holes == [(1.0, 2.0), (3.0, 2.0), (3.0, 4.0)]


def test_tool_definition_with_feed_and_speed(tmp_path):
    path = tmp_path / "drill.drl"
    path.write_text("M48\nMETRIC\nT1F00S00C0.800\n%\nT1\nX1.0Y2.0\nM30\n")
    _layer, drills = parse_excellon_file(str(path))
    assert drills[0].diameter == pytest.approx(0.8)
    assert drills[0].holes == [(1.0, 2.0)]
