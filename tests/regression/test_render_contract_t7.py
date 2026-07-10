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


# --- T8: object-edge check. Live incident (2026-07-07, raytracer stress
# goal): a GENUINE raytracer whose camera pointed away from every sphere
# rendered only the sky gradient — T5/T6/T7 all passed (valid PPM, 444
# distinct colors) and the goal was marked verified over an image showing
# nothing. Distinct-count within a row is also defeated (lens falloff gives
# ~46 near-identical triplets/row, adjacent deltas ~1 — measured on the real
# false-completion artifact). The discriminator: the largest adjacent-pixel
# channel jump within sampled rows — smooth shading moves ~1-2/pixel; an
# object silhouette is a discontinuity of tens.

T8 = next(t for t in template_contract(STACK, "raytracer render")["tests"] if t["id"] == "T8")


def _run_t8(tmp_path, ppm_bytes):
    (tmp_path / "render.ppm").write_bytes(ppm_bytes)
    r = subprocess.run(T8["command"], shell=True, cwd=tmp_path,
                       capture_output=True, text=True)
    return T8["expect_substring"] in r.stdout


def _gradient_p3(w=64, h=48):
    # Vertical gradient with subtle per-pixel lens falloff — the exact shape
    # of the live false-completion render (rows near-constant, deltas ~1).
    lines = [f"P3\n{w} {h}\n255"]
    for y in range(h):
        base = int(255 * y / h)
        row = []
        for x in range(w):
            falloff = abs(x - w // 2) // 16  # ±1-2 within a row
            row += [str(min(255, base + falloff)), str(min(255, base + falloff)), "255"]
        lines.append(" ".join(row))
    return ("\n".join(lines) + "\n").encode()


def _scene_p3(w=64, h=48):
    lines = [f"P3\n{w} {h}\n255"]
    for y in range(h):
        row = []
        for x in range(w):
            dx, dy = x - w // 2, y - h // 2
            if dx * dx + dy * dy < 200:      # a sphere silhouette
                row += ["220", "30", "30"]
            else:                             # gradient background
                row += ["120", "160", str(int(255 * y / h))]
        lines.append(" ".join(row))
    return ("\n".join(lines) + "\n").encode()


def test_t8_rejects_a_pure_gradient_background(tmp_path):
    assert _run_t8(tmp_path, _gradient_p3()) is False


def test_t8_passes_a_scene_with_object_silhouettes(tmp_path):
    assert _run_t8(tmp_path, _scene_p3()) is True


def test_t8_handles_p6_binary_ppm(tmp_path):
    w, h = 32, 24
    body = bytearray()
    for y in range(h):
        for x in range(w):
            dx, dy = x - w // 2, y - h // 2
            if dx * dx + dy * dy < 60:
                body += bytes((220, 30, 30))
            else:
                body += bytes((120, 160, int(255 * y / h)))
    assert _run_t8(tmp_path, f"P6\n{w} {h}\n255\n".encode() + bytes(body)) is True


def test_t8_survives_the_validate_tests_gate(tmp_path):
    from src.auditor import validate_tests
    tests = template_contract(STACK, "raytracer render")["tests"]
    kept, dropped = validate_tests(tests, STACK)
    assert all(t["id"] != "T8" for t in dropped), "T8 must not be silently stripped (Lesson 1)"
