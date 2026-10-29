"""Export the fictional dashboard as static files for public hosting."""

from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dashboard import prepare_dashboard, render_dashboard  # noqa: E402


def main():
    output = ROOT / "site"
    output.mkdir(exist_ok=True)
    html = render_dashboard(prepare_dashboard()).replace(
        " · Local and read-only", " · Read-only"
    )
    (output / "index.html").write_text(html, encoding="utf-8")
    shutil.copyfile(ROOT / "src" / "dashboard.css", output / "dashboard.css")


if __name__ == "__main__":
    main()
