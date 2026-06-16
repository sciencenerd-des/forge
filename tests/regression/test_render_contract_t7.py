"""Regression: the render-variety check (T7) must be satisfiable AND correct.

Two distinct historical bugs:
  1. The original ``od -An -tu1`` byte-variety test was UNSATISFIABLE for a P3
     (ASCII) PPM — its bytes are only digits/space/newline (~13 distinct) however
     varied the image — while T6 permits P3. The loop chased an impossible target.
  2. The python rewrite that fixed (1) then got dropped by the quality gate.

T7 is now pure shell and format-aware. These tests run the ACTUAL command the
render template generates against real P3/P6 fixtures.
"""
import subprocess

from src.auditor import detect_stack, template_contract

STACK = detect_stack("raytracer render image")
T7 = next(t for t in template_contract(STACK, "raytracer render")["tests"] if t["id"] == "T7")


def _run_t7(tmp_path, ppm_bytes):
    (tmp_path / "render.ppm").write_bytes(ppm_bytes)
    r = subprocess.run(T7["command"], shell=True, cwd=tmp_path,
                       capture_output=True, text=True)
    return T7["expect_substring"] in r.stdout  # True == VARIED/pass


def _p3(rows):  # rows: list of "r g b" strings
    body = "\n".join(rows)
    return f"P3\n{len(rows)} 1\n255\n{body}\n".encode()


def test_p3_varied_passes(tmp_path):
    rows = [f"{i*6} {255-i*5} {i*3+10}" for i in range(40)]  # many distinct ints
    assert _run_t7(tmp_path, _p3(rows)) is True


def test_p3_uniform_fails(tmp_path):
    rows = ["100 100 100"] * 40  # one distinct value -> not varied
    assert _run_t7(tmp_path, _p3(rows)) is False


def test_p6_varied_passes(tmp_path):
    header = b"P6\n8 8\n255\n"
    body = bytes([(i * 7) % 256 for i in range(8 * 8 * 3)])  # many distinct bytes
    assert _run_t7(tmp_path, header + body) is True


def test_p6_uniform_fails(tmp_path):
    header = b"P6\n8 8\n255\n"
    body = bytes([128]) * (8 * 8 * 3)  # one distinct byte
    assert _run_t7(tmp_path, header + body) is False
