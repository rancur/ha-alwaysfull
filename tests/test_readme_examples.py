"""Every automation in the README is validated by Home Assistant itself.

The README tells a stranger these blocks are "real YAML a user can paste".
That is a promise, and a promise in a README is exactly the kind of thing
that is true on the day it is written and quietly false a release later --
after an entity is renamed, or after Home Assistant changes what a trigger
may contain. Nothing about the test suite would notice.

So the blocks are not retyped here. They are READ OUT OF README.md and run
through `automation.config.async_validate_config_item`, which is the same
code path the automation editor uses when a user pastes YAML into it. A
block that fails here is a block that would fail in the user's face.

The entity ids cannot be resolved against a live registry, since they are
named after the reader's own bowl. What is checked instead is the WHOLE id
against the set this integration would produce, derived from
`translations/en.json` -- because Home Assistant builds an entity id by
slugifying the English name, so editing a name in that file silently
invalidates every example in the README that used it. Checking only the
domain would let `binary_sensor.water_bowl_fill_alarm` through, which is in
the right domain and still wrong.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.util import slugify

from custom_components.alwaysfull.const import ALERT_OPTIONS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

README = Path(__file__).parent.parent / "README.md"

YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.DOTALL)

TRANSLATIONS = Path(__file__).parent.parent / "custom_components/alwaysfull/translations/en.json"

# The device name the README tells the reader to substitute. Home Assistant
# builds an entity id as `<domain>.<device name>_<entity name>`, both
# slugified, so this is the prefix every example id carries.
EXAMPLE_DEVICE_SLUG = "water_bowl"

ENTITY_ID = re.compile(rf"\b([a-z_]+)\.({EXAMPLE_DEVICE_SLUG}_[a-z0-9_]+)\b")


def _documented_entity_ids() -> dict[str, set[str]]:
    """Return `{domain: {object_id, ...}}` for every entity this integration makes.

    Derived from `translations/en.json` rather than listed here, because
    the ENGLISH NAME is what Home Assistant slugifies into an entity id --
    so the set of legal ids changes the moment a translation is edited, and
    a hand-maintained copy would go stale exactly when it mattered.

    `homeassistant.util.slugify` is HA's own function, the one that
    actually builds the id. Reimplementing it here would be asserting the
    README against a guess at Home Assistant's behaviour.
    """
    entities = json.loads(TRANSLATIONS.read_text())["entity"]
    return {
        domain: {
            slugify(f"{EXAMPLE_DEVICE_SLUG} {definition['name']}")
            for definition in platform.values()
        }
        for domain, platform in entities.items()
    }


def _automation_blocks() -> list[tuple[str, dict[str, Any]]]:
    """Return every README code block that is an automation, with its alias."""
    blocks: list[tuple[str, dict[str, Any]]] = []
    for index, raw in enumerate(YAML_BLOCK.findall(README.read_text()), start=1):
        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict) and "triggers" in parsed:
            blocks.append((parsed.get("alias") or f"block {index}", parsed))
    return blocks


def test_the_readme_still_contains_automation_examples() -> None:
    """Guard the guard: an empty parse would make every test below vacuous.

    If the block delimiters change, or the examples are moved to another
    file, `_automation_blocks()` returns `[]` -- and a parametrised test
    over an empty list is a test that passes without running. The brief
    for this README asked for at least two worked examples, so that is
    the floor asserted here.
    """
    assert len(_automation_blocks()) >= 2


@pytest.mark.parametrize(
    ("alias", "config"),
    _automation_blocks(),
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_readme_automations_validate(
    hass: HomeAssistant, alias: str, config: dict[str, Any]
) -> None:
    """Home Assistant accepts the block exactly as the README prints it."""
    # Raises `vol.Invalid` with a humanised message on anything wrong,
    # which is a better failure than an assertion would produce.
    assert await async_validate_config_item(hass, alias, config) is not None


@pytest.mark.parametrize(("alias", "config"), _automation_blocks(), ids=lambda v: v if isinstance(v, str) else "")
def test_readme_automations_reference_entities_this_integration_creates(
    alias: str, config: dict[str, Any]
) -> None:
    """Every `water_bowl_*` id is one this integration would actually create.

    The ids are placeholders for the reader's own bowl, so they cannot be
    resolved against a live registry. What CAN be checked is the whole id,
    not merely its domain: `binary_sensor.water_bowl_fill_alarm` is in the
    right domain and is still wrong, because the entity is named "Water
    fill alarm" and slugifies to `water_bowl_water_fill_alarm`.

    Checking only the domain would miss precisely the thing that rots --
    the suffix is what changes when someone edits a name in
    translations/en.json, and nothing else in the suite reads the README.
    """
    documented = _documented_entity_ids()
    found = ENTITY_ID.findall(yaml.safe_dump(config))
    assert found, f"{alias} references no entities at all"
    for domain, object_id in found:
        assert domain in documented, (
            f"{alias}: {domain}.{object_id} -- this integration creates no {domain} entities"
        )
        assert object_id in documented[domain], (
            f"{alias}: {domain}.{object_id} is not an entity this integration creates. "
            f"Valid {domain} ids: {sorted(documented[domain])}"
        )


def test_readme_only_quotes_real_alert_types() -> None:
    """Every alert type named in the README is one the event entity declares.

    The README's alert table and its automation conditions are the two
    places a reader copies a `daily_decreased` from. A typo there sends
    them to a condition that is never true, with nothing anywhere to say
    so.
    """
    text = README.read_text()
    # The table's backticked codes, plus anything quoted inside a condition.
    quoted = set(re.findall(r"`([a-z][a-z_]+)`", text)) | set(
        re.findall(r"'([a-z][a-z_]+)'", text)
    )
    alert_like = {
        value
        for value in quoted
        # Only judge the ones that look like an alert type: a word with an
        # underscore that is not obviously something else. Every real alert
        # type contains one.
        if "_" in value and value.split("_")[0] in {t.split("_")[0] for t in ALERT_OPTIONS}
    }
    unknown = alert_like - set(ALERT_OPTIONS)
    assert not unknown, f"README names alert types the integration does not declare: {sorted(unknown)}"
