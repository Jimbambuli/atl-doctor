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


def test_wheel_shim_builds_and_loads(built):
    import subprocess
    if subprocess.run(["pkg-config", "--exists", "wayland-client", "xkbcommon"]).returncode != 0:
        pytest.skip("wayland-client/xkbcommon missing")
    lib = built / "libatl_doctor_wheel.so"
    assert lib.exists()
    r = subprocess.run(["env", f"LD_PRELOAD={lib}", "true"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == ""


def test_input_helper_protocol_has_wheel_and_smooth(built):
    import subprocess
    if subprocess.run(["pkg-config", "--exists", "wayland-client", "xkbcommon"]).returncode != 0:
        pytest.skip("wayland-client/xkbcommon missing")
    strings = subprocess.run(["strings", str(built / "atl-doctor-input")], capture_output=True, text=True).stdout
    assert "wheel %" in strings and "wheel-smooth %" in strings


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


def test_agent_memory_lines_on_host_jvm(built, tmp_path):
    """marks=FILE: one M line per mark; with heap also C lines and the JNI global count."""
    import subprocess
    java, javac = shutil.which("java"), shutil.which("javac")
    if not java or not javac:
        pytest.skip("no JDK")
    (tmp_path / "Main.java").write_text(
        "public class Main { static Object[] keep = new Object[1000];\n"
        "  public static void main(String[] a) throws Exception {\n"
        "    for (int i = 0; i < 1000; i++) keep[i] = new int[256]; Thread.sleep(1500); } }\n")
    subprocess.run([javac, "-d", str(tmp_path), str(tmp_path / "Main.java")], check=True, capture_output=True)
    marks = tmp_path / "marks.tsv"
    marks.write_text("0\t1\t1\tstart\n1\t2\t2\tOpen\n")
    out = tmp_path / "agent.tsv"
    r = subprocess.run(["nice", "-n", "19", java, "-cp", str(tmp_path),
                        f"-agentpath:{built / 'libatl_doctor_agent.so'}={out},nolc,noex,marks={marks},heap", "Main"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rows = [ln.split("\t") for ln in out.read_text().splitlines()]
    m = [x for x in rows if x[0] == "M"]
    assert [x[2] for x in m] == ["0", "1"] and all(int(x[3]) > 0 and int(x[5]) >= 0 for x in m)
    c = [x for x in rows if x[0] == "C" and x[2] == "1"]
    assert 0 < len(c) <= 50 and any(x[3] == "[I" and int(x[4]) >= 1000 for x in c)


def test_agent_exception_line_with_very_long_method_names(built, tmp_path):
    """E line: a catcher name longer than the line buffer, and many frames with 1000-character names."""
    import subprocess
    java, javac = shutil.which("java"), shutil.which("javac")
    if not java or not javac:
        pytest.skip("no JDK")
    catcher, frame = "c" * 20000, "f" * 1000
    (tmp_path / "Main.java").write_text(
        "public class Main {\n"
        f"  static void {frame}(int d) {{ if (d == 0) throw new IllegalStateException(\"x\"); {frame}(d - 1); }}\n"
        f"  static void {catcher}() {{ try {{ {frame}(30); }} catch (IllegalStateException e) {{}} }}\n"
        "  public static void main(String[] a) {\n"
        f"    {catcher}();\n"
        f"    try {{ {frame}(30); }} catch (IllegalStateException e) {{}} }} }}\n")
    subprocess.run([javac, "-d", str(tmp_path), str(tmp_path / "Main.java")], check=True, capture_output=True)
    out = tmp_path / "agent.tsv"
    r = subprocess.run(["nice", "-n", "19", java, "-cp", str(tmp_path),
                        f"-agentpath:{built / 'libatl_doctor_agent.so'}={out},nolc", "Main"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    raw = out.read_bytes()
    assert raw.endswith(b"\n") and b"\0" not in raw
    lines = raw.decode().splitlines()
    e = [ln for ln in lines if ln.startswith("E\t") and "IllegalStateException" in ln]
    assert len(e) == 2 and all(len(ln) < 16384 for ln in e)
    assert all(ln[0] in "EILCHMGP" and ln[1] == "\t" for ln in lines)
