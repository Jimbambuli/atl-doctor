# SPDX-License-Identifier: GPL-3.0-or-later
import shutil

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("meson"), reason="meson missing")


def test_agent_builds_without_warnings(built):
    assert (built / "libatl_doctor_agent.so").exists()


def test_input_helper_builds(built):
    import subprocess
    if subprocess.run(["pkg-config", "--exists", "wayland-client", "xkbcommon"]).returncode != 0:
        pytest.skip("wayland-client/xkbcommon missing")
    assert (built / "atl-doctor-input").exists()


def test_agent_builds_with_frames_and_hang(built, tmp_path):
    """Built with -Dwerror; on a host JVM (no GTK) frames are reported unavailable, hangs and GCs are seen."""
    import subprocess
    java, javac = shutil.which("java"), shutil.which("javac")
    if not java or not javac:
        pytest.skip("no JDK")
    (tmp_path / "Main.java").write_text(
        "public class Main {\n"
        "  static void busy(long ms) { long e = System.nanoTime() + ms * 1000000L; while (System.nanoTime() < e) {} }\n"
        "  static void nested(int d, long ms) { if (d == 0) busy(ms); else nested(d - 1, ms); }\n"
        "  public static void main(String[] a) throws Exception {\n"
        "    Thread.sleep(600); nested(6, 600); Thread.sleep(400);\n"
        "    for (int i = 0; i < 20; i++) { byte[] b = new byte[1 << 22]; } System.gc(); Thread.sleep(300); } }\n")
    subprocess.run([javac, "-d", str(tmp_path), str(tmp_path / "Main.java")], check=True, capture_output=True)
    out = tmp_path / "agent.tsv"
    r = subprocess.run(["nice", "-n", "19", java, "-cp", str(tmp_path),
                        f"-agentpath:{built / 'libatl_doctor_agent.so'}={out},nolc,noex,frames,hang=100", "Main"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    lines = out.read_text().splitlines()
    assert any(ln.startswith("I\t") and "frames unavailable" in ln for ln in lines)
    assert any(ln.startswith("G\t") for ln in lines)
    hangs = [ln.split("\t") for ln in lines if ln.startswith("H\t")]
    assert len(hangs) == 1 and 500 <= int(hangs[0][3]) <= 900
    assert hangs[0][4].startswith("Main.busy|Main.nested|")
