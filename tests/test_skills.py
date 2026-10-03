from pathlib import Path

from app.plugins.skills import load_plugin_skills


def test_all_official_packages_include_usage_instructions():
    root = Path(__file__).resolve().parents[1] / "plugins"
    expected = {
        "money": "money-management",
        "token_monitor": "token-usage",
        "kanban": "task-board",
    }
    for plugin_id, name in expected.items():
        skills = load_plugin_skills(root / plugin_id, plugin_id)
        assert [skill.id for skill in skills] == [f"plugin__{plugin_id}__{name}"]
        assert skills[0].description and skills[0].body
