import shutil
from pathlib import Path

from agent_insights_quality.catalog_docs import generate_catalog_views, render_catalog_views
from agent_insights_quality.catalogs import load_catalog

ROOT = Path(__file__).resolve().parents[2]


def test_readable_views_cover_catalog_without_touching_traffic(tmp_path):
    shutil.copytree(ROOT / "catalogs", tmp_path / "catalogs")
    traffic = tmp_path / "agents" / "synthetic" / "traffic.json"
    traffic.parent.mkdir(parents=True)
    traffic.write_bytes(b'{"synthetic":"unchanged"}')
    catalog = load_catalog(tmp_path)
    documents = render_catalog_views(catalog)
    assert set(documents) == {"AGENT_CATALOG.md", "ISSUE_CATALOG.md"}
    for target in catalog.targets:
        assert target.unit_id.agent in documents["AGENT_CATALOG.md"]
        if not target.is_baseline:
            assert f'id="{target.unit_id.logical_version}"' in documents["ISSUE_CATALOG.md"]
    assert set(generate_catalog_views(catalog)) == set(documents)
    for name, content in documents.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content
    assert traffic.read_bytes() == b'{"synthetic":"unchanged"}'


def test_markdown_table_cells_cannot_create_extra_rows():
    catalog = load_catalog(ROOT)
    catalog._documents[1]["issues"][0]["title"] = "synthetic | title\nnext <tag>"
    text = render_catalog_views(catalog)["ISSUE_CATALOG.md"]
    assert "synthetic &#124; title next &lt;tag&gt;" in text
