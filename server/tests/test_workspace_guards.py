"""run_command should refuse to kill the desktop it shares, and should find
every package an apt install line asks for so wrong names are caught first."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.tools.workspace import apt_packages, refused


def test_refuses_killing_the_desktop():
    for cmd in ["pkill xfdesktop", "killall -9 xfwm4", "pkill -f xfce4-panel; sleep 1",
                "xfdesktop --quit", "tmux kill-server", "sudo systemctl restart lightdm",
                "pkill xfce4-terminal"]:
        assert refused(cmd), cmd


def test_lets_ordinary_commands_through():
    for cmd in ["ls ~/Pictures", "pkill -f my_script.py", "xfdesktop --reload",
                "xfconf-query -c xfce4-desktop -l", "kill %1", "tmux ls",
                "grep xfwm4 /etc/xdg -r"]:
        assert refused(cmd) is None, cmd


def test_finds_apt_package_names():
    assert apt_packages("sudo apt-get update && sudo apt-get install -y arc-theme papirus-icon-theme "
                        "fonts-jetbrains-mono 2>&1 | tail") == [
        "arc-theme", "papirus-icon-theme", "fonts-jetbrains-mono"]
    assert apt_packages("sudo DEBIAN_FRONTEND=noninteractive apt install -yq ./x.deb python3-pil") == ["python3-pil"]
    assert apt_packages("pip install pillow") == []
    assert apt_packages("apt search nerd") == []
