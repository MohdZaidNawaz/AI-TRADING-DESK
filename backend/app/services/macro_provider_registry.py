"""Pinned, auditable canonical indicator mappings for Phase 5.2 providers."""

from __future__ import annotations

from app.models.macro_intelligence import MacroSourceType
from app.models.macro_providers import (
    MacroMappingStatus,
    MacroProviderId,
    MacroProviderLicense,
    MacroProviderMapping,
)


def _mapping(
    provider: MacroProviderId,
    indicator: str,
    series: str | None,
    publisher_id: str,
    publisher_name: str,
    source_type: MacroSourceType,
    license_type: MacroProviderLicense,
    status: MacroMappingStatus,
    notes: str,
    *,
    vintages: bool = False,
) -> MacroProviderMapping:
    return MacroProviderMapping(
        provider_id=provider,
        indicator_id=indicator,
        provider_series_id=series,
        original_publisher_id=publisher_id,
        original_publisher_name=publisher_name,
        source_type=source_type,
        license=license_type,
        status=status,
        supports_vintages=vintages,
        notes=notes,
    )


FRED_MAPPINGS: tuple[MacroProviderMapping, ...] = (
    _mapping(MacroProviderId.FRED, "us_cpi_all_items_index", "CPIAUCSL", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "Seasonally adjusted all-items CPI level.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_core_cpi_index", "CPILFESL", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "Seasonally adjusted CPI excluding food and energy.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_pce_price_index", "PCEPI", "bea", "U.S. Bureau of Economic Analysis", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "PCE price index level.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_core_pce_price_index", "PCEPILFE", "bea", "U.S. Bureau of Economic Analysis", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "PCE price index excluding food and energy.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_nonfarm_payrolls", "PAYEMS", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "All-employees total nonfarm level in thousands.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_unemployment_rate", "UNRATE", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "Civilian unemployment rate.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_average_hourly_earnings", "CES0500000003", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "Average hourly earnings of all employees, total private.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_real_gdp_level", "GDPC1", "bea", "U.S. Bureau of Economic Analysis", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "Real gross domestic product level.", vintages=True),
    _mapping(MacroProviderId.FRED, "us_retail_sales_level", "RSAFS", "census", "U.S. Census Bureau", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "Advance retail and food services sales level.", vintages=True),
    _mapping(MacroProviderId.FRED, "fed_policy_rate", "FEDFUNDS", "federal_reserve", "Board of Governors of the Federal Reserve System", MacroSourceType.CENTRAL_BANK, MacroProviderLicense.FRED_TERMS, MacroMappingStatus.ENABLED, "Effective federal funds rate monthly average; direct decisions remain Federal Reserve events.", vintages=True),
)


BLS_MAPPINGS: tuple[MacroProviderMapping, ...] = (
    _mapping(MacroProviderId.BLS, "us_cpi_all_items_index", "CUSR0000SA0", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.ENABLED, "Seasonally adjusted CPI-U all items."),
    _mapping(MacroProviderId.BLS, "us_core_cpi_index", "CUSR0000SA0L1E", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.ENABLED, "Seasonally adjusted CPI-U less food and energy."),
    _mapping(MacroProviderId.BLS, "us_nonfarm_payrolls", "CES0000000001", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.ENABLED, "All employees, total nonfarm."),
    _mapping(MacroProviderId.BLS, "us_unemployment_rate", "LNS14000000", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.ENABLED, "Civilian unemployment rate."),
    _mapping(MacroProviderId.BLS, "us_average_hourly_earnings", "CES0500000003", "bls", "U.S. Bureau of Labor Statistics", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.ENABLED, "Average hourly earnings, total private."),
)


TREASURY_MAPPINGS: tuple[MacroProviderMapping, ...] = tuple(
    _mapping(
        MacroProviderId.US_TREASURY,
        f"us_nominal_treasury_yield_{maturity.lower()}",
        f"daily_treasury_yield_curve:{maturity}",
        "us_treasury",
        "U.S. Department of the Treasury",
        MacroSourceType.OFFICIAL_OBSERVATION,
        MacroProviderLicense.PUBLIC_GOVERNMENT,
        MacroMappingStatus.ENABLED,
        "Official daily nominal par yield curve.",
    )
    for maturity in ("1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "20Y", "30Y")
) + tuple(
    _mapping(
        MacroProviderId.US_TREASURY,
        f"us_real_treasury_yield_{maturity.lower()}",
        f"daily_treasury_real_yield_curve:{maturity}",
        "us_treasury",
        "U.S. Department of the Treasury",
        MacroSourceType.OFFICIAL_OBSERVATION,
        MacroProviderLicense.PUBLIC_GOVERNMENT,
        MacroMappingStatus.ENABLED,
        "Official daily real par yield curve.",
    )
    for maturity in ("5Y", "10Y", "30Y")
)


BLOCKED_MAPPINGS: tuple[MacroProviderMapping, ...] = (
    _mapping(MacroProviderId.BEA, "us_real_gdp_level", None, "bea", "U.S. Bureau of Economic Analysis", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.BLOCKED_UNVERIFIED, "BEA dataset/table/line-code mapping must be verified against current metadata before activation."),
    _mapping(MacroProviderId.BEA, "us_pce_price_index", None, "bea", "U.S. Bureau of Economic Analysis", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.BLOCKED_UNVERIFIED, "BEA PCE table and line-code mapping is intentionally unavailable until verified."),
    _mapping(MacroProviderId.BEA, "us_core_pce_price_index", None, "bea", "U.S. Bureau of Economic Analysis", MacroSourceType.OFFICIAL_OBSERVATION, MacroProviderLicense.PUBLIC_GOVERNMENT, MacroMappingStatus.BLOCKED_UNVERIFIED, "BEA core PCE table and line-code mapping is intentionally unavailable until verified."),
    _mapping(MacroProviderId.TRADING_ECONOMICS, "us_manufacturing_pmi", None, "licensed_pmi_publisher", "Licensed PMI publisher", MacroSourceType.COMMERCIAL_OBSERVATION, MacroProviderLicense.COMMERCIAL_RESTRICTED, MacroMappingStatus.DISABLED_COMMERCIAL, "Requires verified series identity and commercial redistribution authorization."),
    _mapping(MacroProviderId.TRADING_ECONOMICS, "us_services_pmi", None, "licensed_pmi_publisher", "Licensed PMI publisher", MacroSourceType.COMMERCIAL_OBSERVATION, MacroProviderLicense.COMMERCIAL_RESTRICTED, MacroMappingStatus.DISABLED_COMMERCIAL, "Requires verified series identity and commercial redistribution authorization."),
    _mapping(MacroProviderId.TRADING_ECONOMICS, "us_dollar_index_context", None, "licensed_dxy_publisher", "Licensed DXY publisher", MacroSourceType.MARKET_PRICE_DERIVED, MacroProviderLicense.COMMERCIAL_RESTRICTED, MacroMappingStatus.DISABLED_COMMERCIAL, "No exact validated DXY series or redistribution entitlement is configured."),
)


MACRO_PROVIDER_MAPPINGS: tuple[MacroProviderMapping, ...] = (
    *FRED_MAPPINGS,
    *BLS_MAPPINGS,
    *TREASURY_MAPPINGS,
    *BLOCKED_MAPPINGS,
)


def mappings_for_provider(provider_id: MacroProviderId) -> tuple[MacroProviderMapping, ...]:
    return tuple(item for item in MACRO_PROVIDER_MAPPINGS if item.provider_id is provider_id)


def mapping_for_indicator(
    provider_id: MacroProviderId,
    indicator_id: str,
) -> MacroProviderMapping | None:
    return next(
        (
            item
            for item in MACRO_PROVIDER_MAPPINGS
            if item.provider_id is provider_id and item.indicator_id == indicator_id
        ),
        None,
    )
