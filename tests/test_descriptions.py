"""Static checks on the platform description tables.

These need no Home Assistant harness and no fixtures: they validate the
tables in `sensor.py` and `binary_sensor.py` against Home Assistant's OWN
legality tables and against the translation files.

They exist because the alternative was a snapshot. A snapshot only proves
the output still matches what the code produced when the snapshot was
generated -- it cannot tell right from wrong. The realistic failure is
someone editing the descriptions, seeing the suite go red, running
`pytest --snapshot-update` to make it green, and shipping
`SensorDeviceClass.WATER` with millilitres: a log warning on every single
update and broken long-term statistics, with a permanently green suite.
Home Assistant does not raise on that combination, it warns, so nothing
else would catch it either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor.const import (
    DEVICE_CLASS_STATE_CLASSES,
    DEVICE_CLASS_UNITS,
)
from homeassistant.const import EntityCategory, UnitOfVolume

from custom_components.alwaysfull import binary_sensor, sensor
from custom_components.alwaysfull.binary_sensor import BINARY_SENSORS
from custom_components.alwaysfull.sensor import (
    SENSORS,
    AlwaysFullSensorEntityDescription,
)

from .conftest import bowl_data

TRANSLATIONS = Path(__file__).parents[1] / "custom_components/alwaysfull/translations/en.json"
STRINGS = Path(__file__).parents[1] / "custom_components/alwaysfull/strings.json"

# `units` 1 = millilitres, 2 = fluid ounces. Both bowls a user can own.
UNITS_VALUES = (1, 2)

# What each sensor is meant to be categorised as, stated here so the choice
# survives a snapshot regeneration. `last_alert` is deliberately NOT
# diagnostic: Home Assistant collapses diagnostic entities by default, and
# this is the entity that says what actually went wrong.
EXPECTED_CATEGORIES = {
    "water_today": None,
    "filter_life": None,
    "filter_remaining": None,
    "last_alert": None,
    "water_source": EntityCategory.DIAGNOSTIC,
    "firmware": EntityCategory.DIAGNOSTIC,
}
EXPECTED_BINARY_CATEGORIES = {
    "online": EntityCategory.DIAGNOSTIC,
    "system_problem": None,
    "fill_alarm": None,
    "pump_alarm": None,
    "not_level": None,
    "filter_fault": None,
    "water_source_detached": None,
}
# Only firmware ships off by default; it duplicates the device page.
EXPECTED_DISABLED_BY_DEFAULT = {"firmware"}


def possible_units(description: AlwaysFullSensorEntityDescription) -> set[str]:
    """Return every unit this description can ever report.

    A dynamic `unit_fn` is exercised over both of the vendor's `units`
    values rather than skipped, because a dynamic unit is exactly the case
    a static read of the table would miss.
    """
    units = {description.native_unit_of_measurement, description.suggested_unit_of_measurement}
    if description.unit_fn is not None:
        units |= {description.unit_fn(bowl_data(units=value)) for value in UNITS_VALUES}
    return {unit for unit in units if unit is not None}


def assert_sensor_description_legal(description: AlwaysFullSensorEntityDescription) -> None:
    """Assert one sensor description against Home Assistant's own tables."""
    device_class = description.device_class
    state_class = description.state_class

    if device_class is None:
        return

    if device_class in DEVICE_CLASS_UNITS:
        allowed = DEVICE_CLASS_UNITS[device_class]
        for unit in possible_units(description):
            assert unit in allowed, (
                f"{description.key}: unit {unit!r} is not permitted for "
                f"{device_class}; permitted units are {sorted(map(str, allowed))}"
            )

    if state_class is not None:
        allowed_state_classes = DEVICE_CLASS_STATE_CLASSES.get(device_class, set())
        assert state_class in allowed_state_classes, (
            f"{description.key}: state class {state_class} is not permitted for "
            f"{device_class}; permitted state classes are "
            f"{sorted(map(str, allowed_state_classes))}"
        )

    if device_class is SensorDeviceClass.ENUM:
        assert description.options, f"{description.key}: an ENUM sensor needs options"
        assert len(description.options) == len(set(description.options)), (
            f"{description.key}: duplicate options"
        )
        assert state_class is None, f"{description.key}: an ENUM sensor cannot have a state class"
        assert not possible_units(description), f"{description.key}: an ENUM sensor has no unit"


@pytest.mark.parametrize("description", SENSORS, ids=lambda d: d.key)
def test_sensor_descriptions_are_legal(
    description: AlwaysFullSensorEntityDescription,
) -> None:
    """Every shipped sensor obeys Home Assistant's device-class rules.

    The two combinations this is really guarding are WATER + millilitres
    (WATER is the water-METER class and rejects mL) and VOLUME +
    MEASUREMENT (VOLUME takes only TOTAL and TOTAL_INCREASING). Both are
    warnings rather than errors at runtime, so both would ship silently.
    """
    assert_sensor_description_legal(description)


@pytest.mark.parametrize(
    ("device_class", "state_class", "unit", "expected_message"),
    [
        (
            SensorDeviceClass.WATER,
            SensorStateClass.TOTAL_INCREASING,
            UnitOfVolume.MILLILITERS,
            "is not permitted for",
        ),
        (
            SensorDeviceClass.VOLUME,
            SensorStateClass.MEASUREMENT,
            UnitOfVolume.MILLILITERS,
            "state class .* is not permitted for",
        ),
    ],
)
def test_the_legality_check_rejects_the_two_traps(
    device_class: SensorDeviceClass,
    state_class: SensorStateClass,
    unit: str,
    expected_message: str,
) -> None:
    """Negative control: the check above must actually be able to fail.

    A validator that has only ever been seen green is not a validator. Both
    of these are the exact mistakes the brief called out, and both are
    accepted silently by Home Assistant itself.
    """
    illegal = AlwaysFullSensorEntityDescription(
        key="illegal",
        device_class=device_class,
        state_class=state_class,
        native_unit_of_measurement=unit,
        value_fn=lambda _bowl: None,
    )
    with pytest.raises(AssertionError, match=expected_message):
        assert_sensor_description_legal(illegal)


@pytest.mark.parametrize("description", BINARY_SENSORS, ids=lambda d: d.key)
def test_binary_sensor_device_classes_are_real(description: Any) -> None:
    """Every binary sensor names a device class Home Assistant knows."""
    assert description.device_class is not None
    assert description.device_class in set(BinarySensorDeviceClass)


def test_keys_and_translation_keys_are_unique() -> None:
    """A duplicate key would collide two entities onto one unique id."""
    for table in (SENSORS, BINARY_SENSORS):
        keys = [description.key for description in table]
        assert len(keys) == len(set(keys))
        translation_keys = [description.translation_key for description in table]
        assert len(translation_keys) == len(set(translation_keys))
        assert all(translation_keys)


@pytest.mark.parametrize("platform", ["sensor", "binary_sensor"])
def test_every_entity_has_english_text(platform: str) -> None:
    """Both translation files must carry a name for every entity.

    `strings.json` is not read at runtime for a custom integration, so
    `translations/en.json` is what users actually see -- and a missing name
    does not raise, it silently changes the entity id. The two files are
    also asserted identical, since only one of them is ever exercised.
    """
    translations = json.loads(TRANSLATIONS.read_text())
    assert translations == json.loads(STRINGS.read_text())

    table = SENSORS if platform == "sensor" else BINARY_SENSORS
    section = translations["entity"][platform]
    for description in table:
        text = section.get(description.translation_key)
        assert text is not None, f"{description.translation_key}: no English text"
        assert text.get("name"), f"{description.translation_key}: no name"

        options = getattr(description, "options", None)
        if options:
            assert set(text.get("state", {})) == set(options), (
                f"{description.translation_key}: the translated states and the "
                "declared options disagree"
            )


@pytest.mark.parametrize("module", [sensor, binary_sensor], ids=lambda m: m.__name__.split(".")[-1])
def test_read_platforms_declare_parallel_updates(module: Any) -> None:
    """Both read platforms state `PARALLEL_UPDATES = 0` explicitly.

    Honest framing: this is a convention check, not a behaviour check.
    Nothing here talks to the device, so the value cannot change any
    observable outcome -- but the integration quality scale expects every
    platform to declare it rather than leave a reader guessing, and this
    keeps it from being dropped in a later edit.
    """
    assert module.PARALLEL_UPDATES == 0


def test_entity_categories_and_default_enablement() -> None:
    """The categorisation of each entity is a decision, not an accident.

    Stated here as well as in the snapshot so that regenerating snapshots
    cannot quietly bury `last_alert` under the diagnostics fold again.
    """
    assert {d.key: d.entity_category for d in SENSORS} == EXPECTED_CATEGORIES
    assert {d.key: d.entity_category for d in BINARY_SENSORS} == EXPECTED_BINARY_CATEGORIES

    disabled = {
        description.key
        for description in (*SENSORS, *BINARY_SENSORS)
        if not description.entity_registry_enabled_default
    }
    assert disabled == EXPECTED_DISABLED_BY_DEFAULT
