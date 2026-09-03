from __future__ import annotations

from agent_insights_quality.util import ROOT


def test_all_hosted_authorities_require_dynamic_foundry_identity() -> None:
    app_paths = sorted(
        [
            *ROOT.glob("agents/finance-agent/**/source/app.py"),
            *ROOT.glob("agents/travel-agent/**/source/app.py"),
            *ROOT.glob("agents/support-ticket-agent/**/source/app.py"),
        ]
    )
    identity_paths = sorted(
        [
            *ROOT.glob("agents/finance-agent/**/source/runtime_identity.py"),
            *ROOT.glob("agents/travel-agent/**/source/runtime_identity.py"),
            *ROOT.glob(
                "agents/support-ticket-agent/**/source/runtime_identity.py"
            ),
        ]
    )
    assert len(app_paths) == len(identity_paths) == 27
    assert len({path.read_bytes() for path in identity_paths}) == 1
    helper = identity_paths[0].read_text(encoding="utf-8")
    assert 'os.getenv("FOUNDRY_AGENT_NAME", "")' in helper
    assert 'os.getenv("FOUNDRY_AGENT_VERSION", "")' in helper
    assert "are required" in helper
    assert '"gen_ai.agent.name", self.name' in helper
    assert '"gen_ai.agent.version", self.version' in helper
    for path in app_paths:
        text = path.read_text(encoding="utf-8")
        assert "RUNTIME_IDENTITY = require_foundry_runtime_identity()" in text
        assert (
            "trace.get_tracer(RUNTIME_IDENTITY.name, RUNTIME_IDENTITY.version)"
            in text
        )
        assert "tracer.start_as_current_span(" not in text
        assert "RUNTIME_IDENTITY.start_span(tracer," in text


def test_observability_resources_bind_foundry_name_and_version() -> None:
    paths = sorted(
        [
            *ROOT.glob("agents/finance-agent/**/source/observability.py"),
            *ROOT.glob("agents/travel-agent/**/source/observability.py"),
            *ROOT.glob(
                "agents/support-ticket-agent/**/source/observability.py"
            ),
        ]
    )
    assert len(paths) == 27
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "service.name" in text
        assert "service.version" in text
        assert "gen_ai.agent.name" in text
        assert "gen_ai.agent.version" in text
