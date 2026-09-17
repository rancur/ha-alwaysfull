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
    UNIT_CONVERTERS,
)
from homeassistant.const import EntityCategory, UnitOfTime, UnitOfVolume

from custom_components.alwaysfull import (
    binary_sensor,
    button,
    event,
    number,
    select,
    sensor,
    switch,
    time,
)
from custom_components.alwaysfull.binary_sensor import BINARY_SENSORS
from custom_components.alwaysfull.button import BUTTONS, AlwaysFullResetFilterButton
from custom_components.alwaysfull.const import (
    ALERT_OPTIONS,
    ALERT_TYPE_OPTIONS,
    ALERT_TYPES,
    UNKNOWN,
)
from custom_components.alwaysfull.event import ALERT_EVENTS
from custom_components.alwaysfull.models import water_unit
from custom_components.alwaysfull.number import NUMBERS, AlwaysFullNumber
from custom_components.alwaysfull.select import SELECTS, AlwaysFullSelect
from custom_components.alwaysfull.sensor import (
    SENSORS,
    AlwaysFullSensorEntityDescription,
)
from custom_components.alwaysfull.switch import (
    NOTIFY_SWITCHES,
    SWITCHES,
    AlwaysFullNotifySwitch,
    AlwaysFullSwitch,
)
from custom_components.alwaysfull.time import TIMES, AlwaysFullTime

from .conftest import bowl_data

TRANSLATIONS = Path(__file__).parents[1] / "custom_components/alwaysfull/translations/en.json"
STRINGS = Path(__file__).parents[1] / "custom_components/alwaysfull/strings.json"

# `units` 1 = millilitres, 2 = fluid ounces. Both bowls a user can own.
UNITS_VALUES = (1, 2)

# Every description table this module validates, keyed by the platform its
# translations live under.
PLATFORM_TABLES = {
    "sensor": SENSORS,
    "binary_sensor": BINARY_SENSORS,
    "event": ALERT_EVENTS,
    "number": NUMBERS,
    "switch": (*SWITCHES, *NOTIFY_SWITCHES),
    "select": SELECTS,
    "time": TIMES,
    "button": BUTTONS,
}

# The write platforms, and the value each must declare. Unlike the read
# platforms these DO talk to the vendor, one request per entity the user
# touches, so the value is a real throttle rather than a convention.
WRITE_MODULES = (number, switch, select, time, button)

# Everything the user can change is a setting, not a reading: Home
# Assistant's own convention is that these belong in the device's
# configuration section rather than on a dashboard card.
WRITE_TABLES = (NUMBERS, SWITCHES, NOTIFY_SWITCHES, SELECTS, TIMES, BUTTONS)

# Every concrete entity class the write platforms add.
WRITE_ENTITY_CLASSES = (
    AlwaysFullNumber,
    AlwaysFullSwitch,
    AlwaysFullNotifySwitch,
    AlwaysFullSelect,
    AlwaysFullTime,
    AlwaysFullResetFilterButton,
)

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
    # Three fields the vendor's own app never reads. Useful, but none of
    # them is what an owner opens the dashboard to look at.
    "device_used_time": EntityCategory.DIAGNOSTIC,
    "online_time": EntityCategory.DIAGNOSTIC,
    "offline_time": EntityCategory.DIAGNOSTIC,
    "firmware": EntityCategory.DIAGNOSTIC,
}
# The alert event is the headline feature of this integration; burying it
# under the diagnostics fold would defeat the point.
EXPECTED_EVENT_CATEGORIES = {"alert": None}
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


@pytest.mark.parametrize("units", UNITS_VALUES)
def test_every_unit_that_follows_the_bowl_comes_from_one_definition(units: int) -> None:
    """The sensor and the numbers must report the SAME volume unit.

    Three entities take their unit from the bowl rather than from
    themselves -- water consumed, and the two daily thresholds -- and they
    live in two different platform modules. The mapping was written out
    twice, verbatim, which is a unit conversion waiting to be half-fixed:
    a bowl switched to fluid ounces would then read its daily maximum in
    millilitres while its consumption read in ounces, and nothing would
    raise.

    Asserted against `models.water_unit`, the single definition, rather
    than the two against each other: two copies that drifted TOGETHER
    would satisfy the weaker form.
    """
    bowl = bowl_data(units=units)
    unit_fns = [
        description.unit_fn
        for table in (SENSORS, NUMBERS)
        for description in table
        if description.unit_fn is not None
    ]
    assert len(unit_fns) == 3, "water consumed and the two daily thresholds"
    assert {unit_fn(bowl) for unit_fn in unit_fns} == {water_unit(units)}


def assert_sensor_description_legal(description: AlwaysFullSensorEntityDescription) -> None:
    """Assert one sensor description against Home Assistant's own tables.

    The checks come in two halves, and the split is deliberate. The first
    half holds for EVERY sensor, including one that declares no device
    class -- which two of the shipped sensors already do, and which is the
    easiest kind of sensor for somebody to add next. This function used to
    return early on a missing device class, so everything below was
    unexamined for exactly those.
    """
    device_class = description.device_class
    state_class = description.state_class

    # -- True of any sensor, device class or not -------------------------

    assert state_class is None or state_class in set(SensorStateClass), (
        f"{description.key}: {state_class!r} is not a Home Assistant state class"
    )

    # Home Assistant RAISES on options without the ENUM device class
    # ("is providing enum options, but is missing the enum device class"),
    # which is a broken entity rather than a warning.
    assert description.options is None or device_class is SensorDeviceClass.ENUM, (
        f"{description.key}: options need the ENUM device class, not {device_class}"
    )

    # A suggested unit is only legal if something can convert the native
    # unit into it: the same unit, or a converter registered for this
    # device class that knows both. With no device class there is no
    # converter, so any differing suggestion raises at runtime.
    suggested = description.suggested_unit_of_measurement
    native = description.native_unit_of_measurement
    if suggested is not None and suggested != native:
        converter = UNIT_CONVERTERS.get(device_class)
        convertible = (
            converter is not None
            and native in converter.VALID_UNITS
            and suggested in converter.VALID_UNITS
        )
        assert convertible, (
            f"{description.key}: native unit {native!r} cannot be converted to the "
            f"suggested unit {suggested!r} under device class {device_class}"
        )

    # -- The rest are about a device class, and need one ------------------

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
        # NO DEVICE CLASS, and still illegal. The check used to return
        # early here, so everything below was unexamined for any sensor
        # that declared no device class -- which two of the shipped ones
        # (the connection stamps) already do, and which is the easiest
        # kind of sensor to add.
        (
            None,
            "totally_increasing",
            None,
            "not a Home Assistant state class",
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

    A validator that has only ever been seen green is not a validator. The
    first two are the exact mistakes the brief called out, and both are
    accepted silently by Home Assistant itself. The third carries no device
    class at all, which is the case the check used to skip entirely.
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


def test_the_legality_check_rejects_options_without_the_enum_device_class() -> None:
    """A sensor with options and no ENUM device class is rejected at runtime.

    Home Assistant RAISES `ValueError` on it -- "is providing enum options,
    but is missing the enum device class" -- which is a broken entity, not
    a warning. Checked without reference to the device class because the
    illegal case IS the missing device class.
    """
    illegal = AlwaysFullSensorEntityDescription(
        key="illegal",
        options=["on", "off"],
        value_fn=lambda _bowl: None,
    )
    with pytest.raises(AssertionError, match="options"):
        assert_sensor_description_legal(illegal)


def test_the_legality_check_rejects_an_unconvertible_suggested_unit() -> None:
    """A suggested unit needs something able to convert the native one into it.

    With no device class there is no unit converter, so Home Assistant
    raises `ValueError` ("suggest an incorrect unit of measurement") the
    first time the entity is added. Another thing the early return let
    through.
    """
    illegal = AlwaysFullSensorEntityDescription(
        key="illegal",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.DAYS,
        value_fn=lambda _bowl: None,
    )
    with pytest.raises(AssertionError, match="cannot be converted"):
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


@pytest.mark.parametrize("platform", list(PLATFORM_TABLES))
def test_every_entity_has_english_text(platform: str) -> None:
    """Every translation file must carry a name for every entity.

    `strings.json` is not read at runtime for a custom integration, so
    `translations/en.json` is what users actually see -- and a missing name
    does not raise, it silently changes the entity id. The two files are
    also asserted identical, since only one of them is ever exercised.

    An event entity keeps its per-value labels one level deeper than an
    enum sensor does (under `state_attributes.event_type.state`), which is
    the sort of detail that silently ships as raw `hardware_fault` in the
    UI if nobody checks it.
    """
    translations = json.loads(TRANSLATIONS.read_text())
    assert translations == json.loads(STRINGS.read_text())

    section = translations["entity"][platform]
    for description in PLATFORM_TABLES[platform]:
        text = section.get(description.translation_key)
        assert text is not None, f"{description.translation_key}: no English text"
        assert text.get("name"), f"{description.translation_key}: no name"

        options = getattr(description, "options", None)
        if options:
            assert set(text.get("state", {})) == set(options), (
                f"{description.translation_key}: the translated states and the "
                "declared options disagree"
            )

        event_types = getattr(description, "event_types", None)
        if event_types:
            labelled = text.get("state_attributes", {}).get("event_type", {}).get("state", {})
            assert set(labelled) == set(event_types), (
                f"{description.translation_key}: the translated event types and "
                "the declared ones disagree"
            )


@pytest.mark.parametrize(
    "module", [sensor, binary_sensor, event], ids=lambda m: m.__name__.split(".")[-1]
)
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
    assert {d.key: d.entity_category for d in ALERT_EVENTS} == EXPECTED_EVENT_CATEGORIES

    disabled = {
        description.key
        for description in (*SENSORS, *BINARY_SENSORS, *ALERT_EVENTS)
        if not description.entity_registry_enabled_default
    }
    assert disabled == EXPECTED_DISABLED_BY_DEFAULT


def test_the_alert_mapping_is_the_only_one() -> None:
    """One vendor->option mapping, reported identically by both platforms.

    `last_alert` publishes it as enum `options` and the alert event as
    `event_types`. Someone writing an automation against one and then the
    other must not have to learn two spellings, and an alert added to
    `ALERT_TYPES` without a mapping would silently report `unknown` for
    ever -- which looks exactly like a vendor type nobody has seen yet
    rather than like the bug it is.
    """
    assert set(ALERT_TYPE_OPTIONS) == set(ALERT_TYPES)
    assert "unknown" not in ALERT_TYPE_OPTIONS.values()
    assert tuple(ALERT_TYPE_OPTIONS.values()) == ALERT_OPTIONS[:-1]
    assert ALERT_OPTIONS[-1] == UNKNOWN

    last_alert = next(d for d in SENSORS if d.key == "last_alert")
    alert_event = next(d for d in ALERT_EVENTS if d.key == "alert")
    assert tuple(last_alert.options) == tuple(alert_event.event_types) == ALERT_OPTIONS
    # Each platform holds its OWN list. Handing both the same one would let
    # anything that reordered or extended it do so for both at once.
    assert last_alert.options is not alert_event.event_types


# -- Write platforms ------------------------------------------------------


@pytest.mark.parametrize("module", WRITE_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_write_platforms_declare_parallel_updates_of_one(module: Any) -> None:
    """Every write platform states `PARALLEL_UPDATES = 1`.

    Not a convention check, unlike the read platforms' `0`. These modules
    are the only ones that send requests of their own, and the value has a
    real effect: `helpers/service.py::entity_service_call` routes every
    entity service call through `Entity.async_request_call`, which
    acquires the platform's semaphore, so `1` serialises concurrent writes
    WITHIN a platform. Dropping the declaration lets Home Assistant fan out
    as many simultaneous requests as the user touched settings on that
    platform.

    What it does not do is serialise ACROSS the five write platforms --
    each has its own semaphore -- which is what the coordinator's write
    lock is for. `tests/test_write_serialisation.py` covers that half and
    records the experiment behind the distinction.
    """
    assert module.PARALLEL_UPDATES == 1


def test_every_writable_entity_is_a_configuration_entity() -> None:
    """Settings belong in the device's configuration section.

    Set once on the two write bases rather than on twenty-odd
    descriptions, so the assertion is on the bases -- plus a check that no
    description quietly overrides it, since a description's category would
    be ignored anyway and the disagreement would be invisible.

    Stated here rather than only in the snapshots because a
    `--snapshot-update` would happily bless a dashboard full of sliders.
    """
    # Read off an INSTANCE, because that is the only place the answer is
    # the real one: Home Assistant turns every `_attr_*` into a property on
    # the class, and `Entity.entity_category` resolves the `_attr_` value,
    # the description's value and the default in a specific order. A bare
    # `__new__` skips the constructor, which needs a coordinator; nothing
    # in this property depends on anything the constructor sets.
    for entity_class in WRITE_ENTITY_CLASSES:
        entity = object.__new__(entity_class)
        assert entity.entity_category is EntityCategory.CONFIG, entity_class.__name__

    for table in WRITE_TABLES:
        for description in table:
            assert description.entity_category is None, (
                f"{description.key}: the base class already sets the category, "
                "and a description's value would be ignored"
            )


def test_write_entity_keys_and_translation_keys_are_unique_per_platform() -> None:
    """A duplicate key collides two entities onto one unique id."""
    for table in (NUMBERS, (*SWITCHES, *NOTIFY_SWITCHES), SELECTS, TIMES, BUTTONS):
        keys = [description.key for description in table]
        assert len(keys) == len(set(keys))
        translation_keys = [description.translation_key for description in table]
        assert len(translation_keys) == len(set(translation_keys))
        assert all(translation_keys)


def test_number_ranges_are_ordered_and_stepped() -> None:
    """A minimum above the maximum makes an entity Home Assistant cannot set.

    Home Assistant does not validate this at definition time; the entity
    simply rejects every value the user picks.
    """
    for description in NUMBERS:
        assert description.native_min_value < description.native_max_value, description.key
        assert description.native_step is not None, description.key
        assert description.native_step > 0, description.key


def test_the_alert_switches_do_not_restate_the_alert_mapping() -> None:
    """One vendor-spelling table, shared by the sensor, the event and these.

    The switches key off `ALERT_TYPE_OPTIONS.values()`; a hand-written list
    here that dropped or misspelled one entry would leave that alert
    permanently unreachable from Home Assistant, with nothing red.
    """
    alert_switches = [d for d in NOTIFY_SWITCHES if d.key.startswith("alert_")]
    assert [d.key for d in alert_switches] == [
        f"alert_{option}" for option in ALERT_TYPE_OPTIONS.values()
    ]
    assert len(alert_switches) == len(ALERT_TYPES)
