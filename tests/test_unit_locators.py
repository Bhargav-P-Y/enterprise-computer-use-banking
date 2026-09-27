"""Level 1 Unit Tests: Locator Cascade Models and Strategy Ordering."""

from src.schemas.artifact import LocatorCascade, LocatorStrategyType, LocatorTier


def test_locator_cascade_tier_ordering() -> None:
    """Verify tier ordering preserves preference: AXTree -> Label Proximity -> XPath -> Coords."""
    cascade = LocatorCascade(
        tiers=[
            LocatorTier(strategy=LocatorStrategyType.ROLE_AND_NAME, role="textbox", name="Member ID"),
            LocatorTier(strategy=LocatorStrategyType.LABEL_PROXIMITY, label_text="Member ID:", direction="right"),
            LocatorTier(strategy=LocatorStrategyType.STRUCTURAL_XPATH, xpath="//table//input[1]"),
            LocatorTier(strategy=LocatorStrategyType.COORDINATE_RATIO, coords_ratio=[0.25, 0.40]),
        ],
        robustness_rationale="Evaluates high-precision AXTree before falling back to proximity and XPath.",
    )

    assert len(cascade.tiers) == 4
    assert cascade.tiers[0].strategy == LocatorStrategyType.ROLE_AND_NAME
    assert cascade.tiers[1].strategy == LocatorStrategyType.LABEL_PROXIMITY
    assert cascade.tiers[2].strategy == LocatorStrategyType.STRUCTURAL_XPATH
    assert cascade.tiers[3].strategy == LocatorStrategyType.COORDINATE_RATIO
