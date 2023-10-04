"""Config validation helper for the automation integration."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import voluptuous as vol
from voluptuous.humanize import humanize_error

from homeassistant.components import blueprint
from homeassistant.components.trace import TRACE_CONFIG_SCHEMA
from homeassistant.config import config_without_domain
from homeassistant.const import (
    CONF_ALIAS,
    CONF_CONDITION,
    CONF_DESCRIPTION,
    CONF_ID,
    CONF_VARIABLES,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_per_platform, config_validation as cv, script
from homeassistant.helpers.condition import async_validate_conditions_config
from homeassistant.helpers.trigger import async_validate_trigger_config
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.yaml.input import UndefinedSubstitution

from .const import (
    CONF_ACTION,
    CONF_HIDE_ENTITY,
    CONF_INITIAL_STATE,
    CONF_TRACE,
    CONF_TRIGGER,
    CONF_TRIGGER_VARIABLES,
    DOMAIN,
    LOGGER,
)
from .helpers import async_get_blueprints

PACKAGE_MERGE_HINT = "list"

_MINIMAL_PLATFORM_SCHEMA = vol.Schema(
    {
        CONF_ID: str,
        CONF_ALIAS: cv.string,
        vol.Optional(CONF_DESCRIPTION): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)


PLATFORM_SCHEMA = vol.All(
    cv.deprecated(CONF_HIDE_ENTITY),
    script.make_script_schema(
        {
            # str on purpose
            CONF_ID: str,
            CONF_ALIAS: cv.string,
            vol.Optional(CONF_DESCRIPTION): cv.string,
            vol.Optional(CONF_TRACE, default={}): TRACE_CONFIG_SCHEMA,
            vol.Optional(CONF_INITIAL_STATE): cv.boolean,
            vol.Optional(CONF_HIDE_ENTITY): cv.boolean,
            vol.Required(CONF_TRIGGER): cv.TRIGGER_SCHEMA,
            vol.Optional(CONF_CONDITION): cv.CONDITIONS_SCHEMA,
            vol.Optional(CONF_VARIABLES): cv.SCRIPT_VARIABLES_SCHEMA,
            vol.Optional(CONF_TRIGGER_VARIABLES): cv.SCRIPT_VARIABLES_SCHEMA,
            vol.Required(CONF_ACTION): cv.SCRIPT_SCHEMA,
        },
        script.SCRIPT_MODE_SINGLE,
    ),
)


async def _async_validate_config_item(
    hass: HomeAssistant,
    config: ConfigType,
    raise_on_errors: bool,
    warn_on_errors: bool,
) -> AutomationConfig:
    """Validate config item."""
    raw_config = None
    raw_blueprint_inputs = None
    uses_blueprint = False
    with suppress(ValueError):
        raw_config = dict(config)

    automation_name = get_automation_name(config)
    uses_blueprint, raw_blueprint_inputs, config = await handle_blueprint(
        hass, config, uses_blueprint, raw_blueprint_inputs,
        automation_name, warn_on_errors, raise_on_errors
    )

    try:
        validated_config = PLATFORM_SCHEMA(config)
    except vol.Invalid as err:
        return handle_invalid_configuration(
            err, automation_name, "could not be validated", config,
            raise_on_errors, warn_on_errors, raw_blueprint_inputs, raw_config
        )

    automation_config = AutomationConfig(validated_config)
    automation_config.raw_blueprint_inputs = raw_blueprint_inputs
    automation_config.raw_config = raw_config

    return await validate_config_sections(
        hass, validated_config, automation_config,
        automation_name, raise_on_errors
    )

def get_automation_name(config):
    """Determine the name of the automation for logging."""
    automation_name = "Unnamed automation"
    if isinstance(config, Mapping):
        if CONF_ALIAS in config:
            automation_name = f"Automation with alias '{config[CONF_ALIAS]}'"
        elif CONF_ID in config:
            automation_name = f"Automation with ID '{config[CONF_ID]}'"
    return automation_name

async def handle_blueprint(
    hass, config, uses_blueprint, raw_blueprint_inputs,
    automation_name, warn_on_errors, raise_on_errors
):
    """Handle blueprint-related logic and return updated variables."""
    if blueprint.is_blueprint_instance_config(config):
        uses_blueprint = True
        blueprints = async_get_blueprints(hass)
        try:
            blueprint_inputs = await blueprints.async_inputs_from_config(config)
        except blueprint.BlueprintException as err:
            handle_blueprint_exception(err, warn_on_errors, raise_on_errors)
            return uses_blueprint, None, config

        raw_blueprint_inputs = blueprint_inputs.config_with_inputs

        try:
            config = blueprint_inputs.async_substitute()
            raw_config = dict(config)
        except UndefinedSubstitution as err:
            handle_undefined_substitution(err, blueprint_inputs, warn_on_errors, raise_on_errors)
            return uses_blueprint, raw_blueprint_inputs, config

    return uses_blueprint, raw_blueprint_inputs, config

def handle_blueprint_exception(err, warn_on_errors, raise_on_errors):
    """Handle blueprint exception."""
    if warn_on_errors:
        LOGGER.error(
            "Failed to generate automation from blueprint: %s",
            err,
        )
    if raise_on_errors:
        raise

def handle_undefined_substitution(err, blueprint_inputs, warn_on_errors, raise_on_errors):
    """Handle undefined substitution exception."""
    if warn_on_errors:
        LOGGER.error(
            "Blueprint '%s' failed to generate automation with inputs %s: %s",
            blueprint_inputs.blueprint.name,
            blueprint_inputs.inputs,
            err,
        )
    if raise_on_errors:
        raise HomeAssistantError(err) from err

def handle_invalid_configuration(
    err, automation_name, problem, config,
    raise_on_errors, warn_on_errors, raw_blueprint_inputs, raw_config
):
    """Handle invalid configuration and return a minimal config if needed."""
    log_invalid_automation(err, automation_name, problem, config, warn_on_errors)
    if raise_on_errors:
        raise
    return _minimal_config(raw_blueprint_inputs, raw_config)

def log_invalid_automation(err, automation_name, problem, config, warn_on_errors):
    """Log an error about invalid automation."""
    if not warn_on_errors:
        return

    LOGGER.error(
        "%s %s and has been disabled: %s",
        automation_name,
        problem,
        humanize_error(config, err) if isinstance(err, vol.Invalid) else err,
    )

def _minimal_config(raw_blueprint_inputs, raw_config):
    """Generate a minimal configuration for invalid setups."""
    minimal_config = _MINIMAL_PLATFORM_SCHEMA(config)
    automation_config = AutomationConfig(minimal_config)
    automation_config.raw_blueprint_inputs = raw_blueprint_inputs
    automation_config.raw_config = raw_config
    automation_config.validation_failed = True
    return automation_config

async def validate_config_sections(
    hass, validated_config, automation_config,
    automation_name, raise_on_errors
):
    """Validate the configuration sections and return the full config."""
    try:
        automation_config[CONF_TRIGGER] = await async_validate_trigger_config(
            hass, validated_config[CONF_TRIGGER]
        )
    except (vol.Invalid, HomeAssistantError) as err:
        handle_section_error(
            err, automation_name, "failed to setup triggers", validated_config, 
            raise_on_errors, automation_config
        )
        return automation_config

    if CONF_CONDITION in validated_config:
        try:
            automation_config[CONF_CONDITION] = await async_validate_conditions_config(
                hass, validated_config[CONF_CONDITION]
            )
        except (vol.Invalid, HomeAssistantError) as err:
            handle_section_error(
                err, automation_name, "failed to setup conditions", validated_config, 
                raise_on_errors, automation_config
            )
            return automation_config

    try:
        automation_config[CONF_ACTION] = await script.async_validate_actions_config(
            hass, validated_config[CONF_ACTION]
        )
    except (vol.Invalid, HomeAssistantError) as err:
        handle_section_error(
            err, automation_name, "failed to setup actions", validated_config, 
            raise_on_errors, automation_config
        )
        return automation_config

    return automation_config

def handle_section_error(
    err, automation_name, problem, validated_config, 
    raise_on_errors, automation_config
):
    """Handle section validation error."""
    log_invalid_automation(
        err, automation_name, problem, validated_config, True
    )
    if raise_on_errors:
        raise
    automation_config.validation_failed = True


class AutomationConfig(dict):
    """Dummy class to allow adding attributes."""

    raw_config: dict[str, Any] | None = None
    raw_blueprint_inputs: dict[str, Any] | None = None
    validation_failed: bool = False


async def _try_async_validate_config_item(
    hass: HomeAssistant,
    config: dict[str, Any],
) -> AutomationConfig | None:
    """Validate config item."""
    try:
        return await _async_validate_config_item(hass, config, False, True)
    except (vol.Invalid, HomeAssistantError):
        return None


async def async_validate_config_item(
    hass: HomeAssistant,
    config_key: str,
    config: dict[str, Any],
) -> AutomationConfig | None:
    """Validate config item, called by EditAutomationConfigView."""
    return await _async_validate_config_item(hass, config, True, False)


async def async_validate_config(hass: HomeAssistant, config: ConfigType) -> ConfigType:
    """Validate config."""
    automations = list(
        filter(
            lambda x: x is not None,
            await asyncio.gather(
                *(
                    _try_async_validate_config_item(hass, p_config)
                    for _, p_config in config_per_platform(config, DOMAIN)
                )
            ),
        )
    )

    # Create a copy of the configuration with all config for current
    # component removed and add validated config back in.
    config = config_without_domain(config, DOMAIN)
    config[DOMAIN] = automations

    return config
